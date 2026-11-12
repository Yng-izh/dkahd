"""规则记忆：把"在某个区域试成的一次"变成**受控的经验**，而不是自动生效的默认值。

三层状态（不满足条件就永远停在低层）：

``candidate``
    刚在一个区域/一张图里试成的规则。只能在同一张图上影子运行，**不许跨图**。
``project_verified``
    同项目里多个实例通过了几何验收并有抽样，允许在该项目其他区域影子运行。
``cross_project_verified``
    多个**独立项目**逐实例真值评测通过，才允许被推荐为全局候选规则。

为什么这么谨慎：一次成功往往只是贴合了那张图的画法（块名、图层名、绘图习惯），
拿去当默认规则，换一张图就会静默错判——这类回归最难查，因为它"看起来变好了"。

另外两条：

* 召回只产生**提议**，仍要过当前图纸的独立验收（经验不能跳过验证）；
* 模板键包含单位口径、几何指纹、块族、图层角色分布、算子版本与源码指纹；
  其中任何一项变了就不是同一条经验（源码指纹避免"改了算子还用老结论"）。
"""
from __future__ import annotations

import hashlib
import json
import os
import time

SCHEMA = "rule-memory/1.0"

TIERS = ("candidate", "project_verified", "cross_project_verified")
TIER_RANK = {t: i for i, t in enumerate(TIERS)}

#: 逻辑配额：单次召回最多几条、单个文件里最多留几条历史
LIMITS = {"recall": 3, "history": 12}


def _sha(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def template_key(*, drawing_unit_context="", geometry_fingerprint="", block_family="",
                 layer_role_distribution=None, operator_versions=None,
                 source_code_fingerprint="") -> str:
    """模板键：这六项任意一项变化，就不是"同一条经验"。"""
    body = {"units": str(drawing_unit_context or ""),
            "geometry": str(geometry_fingerprint or ""),
            "family": str(block_family or ""),
            "layers": sorted((layer_role_distribution or {}).items())
            if isinstance(layer_role_distribution, dict)
            else list(layer_role_distribution or ()),
            "operators": sorted((operator_versions or {}).items())
            if isinstance(operator_versions, dict)
            else list(operator_versions or ()),
            "code": str(source_code_fingerprint or "")}
    return _sha(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str))[:24]


def config_fingerprint(*, ai_config_digest="", model="", prompt_version="",
                       param_digest="", image_digest="") -> str:
    """缓存/经验键必须覆盖：配置、模型、提示词版本、参数、案例图像字节。"""
    body = {"config": str(ai_config_digest or ""), "model": str(model or ""),
            "prompt": str(prompt_version or ""), "params": str(param_digest or ""),
            "image": str(image_digest or "")}
    return _sha(json.dumps(body, sort_keys=True, ensure_ascii=False,
                           default=str))[:24]


def _path(directory: str, key: str) -> str:
    return os.path.join(str(directory), f"{key}.json")


def _read(path: str, default):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _write(path: str, payload: dict) -> str:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True,
                  default=str)
    os.replace(tmp, path)
    return path


def remember(entry: dict, *, directory: str, scope: str = "project") -> dict:
    """记一条经验（默认最低层 ``candidate``，绝不自动升层）。"""
    if not directory:
        return {"ok": False, "reason": "NO_DIRECTORY"}
    key = str((entry or {}).get("key") or "")
    if not key:
        return {"ok": False, "reason": "NO_KEY"}
    path = _path(directory, key)
    store = _read(path, {}) or {}
    history = list(store.get("history") or ())
    item = {
        "schema": SCHEMA,
        "key": key,
        "tier": str((entry or {}).get("tier") or "candidate"),
        "scope": str((entry or {}).get("scope") or scope),
        "drawing": str((entry or {}).get("drawing") or ""),
        "region": (entry or {}).get("region"),
        "category": str((entry or {}).get("category") or ""),
        "strategy_id": str((entry or {}).get("strategy_id") or ""),
        "parameters": dict((entry or {}).get("parameters") or {}),
        "config_fingerprint": str((entry or {}).get("config_fingerprint") or ""),
        "evidence": dict((entry or {}).get("evidence") or {}),
        "expectations": [str(x) for x in ((entry or {}).get("expectations") or ())][:4],
        "uses": int(store.get("uses") or 0),
        "wins": int(store.get("wins") or 0),
        "created": str(store.get("created") or time.strftime("%Y-%m-%dT%H:%M:%S")),
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if item["tier"] not in TIERS:
        item["tier"] = "candidate"
    history.append({"tier": item["tier"], "at": item["updated"],
                    "evidence": item["evidence"]})
    payload = dict(item, history=history[-int(LIMITS["history"]):])
    _write(path, payload)
    return {"ok": True, "path": path, "tier": item["tier"], "key": key}


def recall(*, directory: str, key: str, limit: int | None = None) -> list[dict]:
    """召回**提议**（不是结论）：同键的、且不是低层跨图使用的那几条。"""
    if not directory:
        return []
    data = _read(_path(directory, key), None)
    if not isinstance(data, dict):
        return []
    try:
        data["uses"] = int(data.get("uses") or 0) + 1
        _write(_path(directory, key), data)
    except OSError:                                  # 记忆写不进去不影响识别
        pass
    out = [{"key": key, "tier": data.get("tier") or "candidate",
            "strategy_id": data.get("strategy_id") or "",
            "parameters": dict(data.get("parameters") or {}),
            "expectations": list(data.get("expectations") or ()),
            "evidence": dict(data.get("evidence") or {}),
            "advisory": True}]
    return out[:int(LIMITS["recall"] if limit is None else limit)]


def promote(key: str, *, directory: str, tier: str, evidence: dict) -> dict:
    """升层：必须有**新的**证据，且一次只能升一级。"""
    if tier not in TIERS:
        return {"ok": False, "reason": "UNKNOWN_TIER"}
    data = _read(_path(directory, key), None)
    if not isinstance(data, dict):
        return {"ok": False, "reason": "NOT_FOUND"}
    current = str(data.get("tier") or "candidate")
    if TIER_RANK.get(tier, -1) > TIER_RANK.get(current, 0) + 1:
        return {"ok": False, "reason": "TIER_SKIP"}
    if not evidence:
        return {"ok": False, "reason": "NO_NEW_EVIDENCE"}
    if tier == "cross_project_verified":
        projects = set(str(p) for p in (evidence.get("projects") or ()))
        if len(projects) < 2:
            return {"ok": False, "reason": "NEED_TWO_PROJECTS"}
    data["tier"] = tier
    data["evidence"] = dict(data.get("evidence") or {}, **evidence)
    data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    history = list(data.get("history") or ())
    history.append({"tier": tier, "at": data["updated"], "evidence": evidence})
    data["history"] = history[-int(LIMITS["history"]):]
    _write(_path(directory, key), data)
    return {"ok": True, "tier": tier, "key": key}


def demote(key: str, *, directory: str, why: str) -> dict:
    """降层/作废：一次反例就该退回去（亏了要认）。"""
    data = _read(_path(directory, key), None)
    if not isinstance(data, dict):
        return {"ok": False, "reason": "NOT_FOUND"}
    data["tier"] = "candidate"
    data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    history = list(data.get("history") or ())
    history.append({"tier": "candidate", "at": data["updated"], "why": str(why)[:120],
                    "demoted": True})
    data["history"] = history[-int(LIMITS["history"]):]
    _write(_path(directory, key), data)
    return {"ok": True, "tier": "candidate", "key": key}


def load(directory: str) -> list[dict]:
    """列出记忆库里的全部条目（按层、按更新时间排序）。"""
    out: list[dict] = []
    if not directory or not os.path.isdir(directory):
        return out
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        data = _read(os.path.join(directory, name), None)
        if isinstance(data, dict) and data.get("key"):
            out.append({k: data.get(k) for k in
                        ("key", "tier", "category", "strategy_id", "parameters",
                         "uses", "wins", "updated", "drawing")})
    out.sort(key=lambda r: (TIER_RANK.get(str(r.get("tier")), 0),
                            str(r.get("updated") or "")))
    return out


def summary(directory: str) -> dict:
    """进审计的摘要：记忆库里有多少条、各层多少。"""
    rows = load(directory)
    counts = {t: sum(1 for r in rows if r.get("tier") == t) for t in TIERS}
    return {"schema": SCHEMA, "directory": str(directory), "entries": len(rows),
            "tiers": counts,
            "top": rows[-int(LIMITS["recall"]):]}
