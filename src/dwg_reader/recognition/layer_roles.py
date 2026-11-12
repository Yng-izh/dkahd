"""本图语义覆盖层：LLM 提出的图层角色只对**当前这张图**生效，不写全局 profile。

缓存键 = ``sha256(图纸哈希 + 模型版本 + 算子版本 + 图层 + 角色)``：

* 换一张图 -> 键不同 -> 不可能串味（旧版只按 layer+roles 做键，跨图污染）；
* 换模型/换算子版本 -> 键不同 -> 旧结论不会继续生效；
* 写入是**事务式**的：PURE 节点只 ``stage()`` 到内存列表，真正的落盘在
  MAIN 节点（``commit_staged()``）里做 ``tmp -> fsync -> os.replace``。
"""
from __future__ import annotations

import hashlib
import json
import os

VALID_ROLES = ("wall", "column", "door", "window", "room_name", "axis", "stair",
               "step", "apron", "north", "frame", "furniture", "ignore")
CACHE_NAME = ".dwg-layer-roles.json"
OPERATOR_VERSION = "layer-roles/2.0"


def _cache_path() -> str:
    return os.environ.get("DWG_LAYER_ROLE_CACHE") or os.path.join(os.getcwd(), CACHE_NAME)


def _digest(*, file_digest: str, model: str, layer: str, roles) -> str:
    blob = json.dumps({"op": OPERATOR_VERSION, "file": str(file_digest or ""),
                       "model": str(model or ""), "layer": str(layer),
                       "roles": sorted(roles or ())}, ensure_ascii=False,
                      sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def validate(layer: str, roles) -> tuple[bool, str]:
    """角色必须是闭集；单个图层最多两角色（如 CURTWALL -> wall+window）。"""
    if not layer:
        return False, "NO_LAYER"
    if not roles:
        return False, "NO_ROLES"
    roles = list(roles)
    if len(roles) > 2:
        return False, "TOO_MANY_ROLES"
    bad = [r for r in roles if r not in VALID_ROLES]
    if bad:
        return False, "UNKNOWN_ROLE:" + ",".join(bad)
    return True, "OK"


def override_of(proposals) -> dict:
    """[{'layer':..., 'roles':[...]}] -> {layer: roles}（只保留校验通过的）。"""
    out = {}
    for p in proposals or ():
        if not isinstance(p, dict):
            continue
        layer, roles = p.get("layer"), p.get("roles")
        ok, _why = validate(layer, roles)
        if ok:
            out[str(layer)] = list(roles)
    return out


def apply_to_plan(plan: dict, overrides: dict) -> dict:
    """把覆盖层合进执行方案（返回新对象，不改入参；profile 缓存不受污染）。"""
    if not overrides:
        return plan
    layers = {k: list(v) for k, v in (plan.get("layers") or {}).items()}
    for layer, roles in overrides.items():
        for role in roles:
            layers.setdefault(role, [])
            if layer not in layers[role]:
                layers[role].append(layer)
    out = dict(plan)
    out["layers"] = layers
    out["layer_roles_applied"] = dict(overrides)
    return out


# ------------------------------------------------------------------ 事务式缓存
def load_cache() -> dict:
    try:
        with open(_cache_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def stage(overrides: dict, *, accepted: bool, stats: dict | None = None,
          file_digest: str = "", model: str = "") -> list[dict]:
    """把一次覆盖结果**记到内存**（PURE 节点只能做这一步，不写盘）。"""
    out = []
    for layer, roles in (overrides or {}).items():
        ok, _why = validate(layer, roles)
        if not ok:
            continue
        out.append({"key": _digest(file_digest=file_digest, model=model,
                                   layer=layer, roles=roles),
                    "layer": str(layer), "roles": list(roles),
                    "accepted": bool(accepted), "stats": dict(stats or {}),
                    "file": str(file_digest or ""), "model": str(model or ""),
                    "operator": OPERATOR_VERSION})
    return out


def commit_staged(entries) -> int:
    """MAIN 节点里落盘（原子写：tmp -> fsync -> replace）。返回写入条数。"""
    entries = [e for e in (entries or ()) if isinstance(e, dict) and e.get("key")]
    if not entries:
        return 0
    cache = load_cache()
    for e in entries:
        cache[e["key"]] = e
    path = _cache_path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return 0
    return len(entries)


def remember(layer: str, roles, *, accepted: bool, stats: dict | None = None,
             file_digest: str = "", model: str = "") -> dict:
    """便捷入口：直接落盘（**只允许**在 MAIN 节点调用）。"""
    entries = stage({str(layer): list(roles or ())}, accepted=accepted, stats=stats,
                    file_digest=file_digest, model=model)
    commit_staged(entries)
    return entries[0] if entries else {}


def accepted_overrides(plan_layers_hint=(), *, file_digest: str = "",
                       model: str = "") -> dict:
    """读缓存里"同图 + 同模型 + 已证明有效"的覆盖（用于下一次同图识别）。"""
    cache = load_cache()
    out: dict[str, list[str]] = {}
    for rec in cache.values():
        if not rec.get("accepted"):
            continue
        if file_digest and rec.get("file") and rec["file"] != str(file_digest):
            continue
        if model and rec.get("model") and rec["model"] != str(model):
            continue
        out[rec["layer"]] = list(rec.get("roles") or ())
    return out
