"""自动图纸适配：**让模型自己产出 profile**，不再依赖人工写的 profile.json。

为什么需要它：识别链路对 profile 的依赖是硬依赖（墙在哪几层、门在哪几层），
以前这张图能跑出结果是因为有人给它写了一份 ``work/profiles/tdw.json``。换一张
新图就门 0 窗 0——这不是识别算法的问题，是"没有方案"的问题。本模块把"写方案"
这件事也交给模型，并且在**运行期**完成，不需要先离线跑一遍。

分工（与全局纪律一致）：

1. 程序负责**观测**：每层的实体构成、线方向分布、块名、样例文字、整图单位线索；
2. 模型负责**归类与画法说明**：层语义（闭集）、symbols、operator_hints、
   unit_hypothesis、图框层；不允许给坐标、尺寸、阈值、代码；
3. 程序的几何指纹仍然算一遍，与模型的判断**各占 0.5 融合**（见
   :func:`layer_roles_infer.infer_roles`）：模型说 A、几何说 B 时按证据打架处理，
   不盲从任何一边；
4. 别名规则（``taxonomy.match_layer``）先命中，只把**没命中**的层送模型 ——
   省 token，也避免模型把 ``WALL`` 判成别的；
5. 拿不到模型（无 key / 超时 / 输出不合规）就退回纯几何 + 关键词兜底，
   识别照常继续，只是方案弱一些；
6. 结果按**文件内容哈希**写进 registry（``.dwg-profiles/<digest>/<digest>.json``）：
   同一张图第二次跑直接读缓存，方案可审计、可人工修正后放回。

一次调用、硬超时、不重试：宁可方案弱一点，也不能把识别卡在网络上。
"""
from __future__ import annotations

import hashlib
import json
import os
import time

from . import llm_credentials as CRED
from . import llm_profile as LP
from . import registry
from .taxonomy import SEMANTICS, is_never_role, match_layer

PROMPT_VERSION = "auto_profile/1.0"
#: 超时预算：加入画法指纹后请求体变大，实测正常回答就要 76~79 秒（60 层的大图），
#: 90 秒会贴边失败并降级成关键词兜底。给到 2 倍余量，仍是一次调用不重试。
DEFAULT_TIMEOUT = int(os.environ.get("DWG_AUTO_PROFILE_TIMEOUT", "180"))


# ------------------------------------------------------------------ 入口
def derive(msp, *, doc=None, dxf_path: str | None = None, digest: str = "",
           api_key: str | None = None, base: str | None = None,
           model: str | None = None, root=None, use_llm: bool = True,
           timeout: int | None = None, fps: dict | None = None) -> dict:
    """观测 -> 模型归类 -> 闭集校验 -> 缓存。

    返回 ``{"profile", "prior", "summary", "source", "layers"}``：

    * ``profile``：可直接喂给 :func:`plan.load_plan` 的完整 profile；
    * ``prior``：``{layer: {role: score}}``，给几何指纹做融合；
    * ``source``：``registry`` / ``auto-llm`` / ``auto-alias`` / ``auto-keyword``
      / ``auto-geometry``，记进节点输出便于追责。
    """
    sig_all = LP.signature_from_msp(msp, doc=doc, fps=fps)
    # 结构性排除先于一切：0 / Defpoints 既不值得送模型，也不许进方案
    sig = {k: v for k, v in sig_all.items()
           if not k.startswith("_") and not is_never_role(k)}
    drawing = sig_all.get("_drawing") or {}
    summary = {"schema": "auto-profile/1.0", "prompt_version": PROMPT_VERSION,
               "layers": len(sig), "status": "skipped", "source": None,
               "alias_layers": 0, "llm_layers": 0, "geometry_only": 0,
               "cached": 0, "errors": None, "digest": (digest or "")[:16]}
    if not sig:
        return _pack({}, summary, "auto-geometry")

    cached = _cached(digest, root)
    if cached:
        summary.update({"status": "cached", "cached": 1, "source": "registry"})
        return _pack(cached, summary, "registry")

    layers: dict[str, dict] = {}
    unmatched: list[str] = []
    for name in sig:
        sems = match_layer(name)
        if sems:
            layers[name] = {"semantics": list(sems), "confidence": 1.0,
                            "reason": "图层名别名规则", "source": "alias"}
        else:
            unmatched.append(name)
    summary["alias_layers"] = len(layers)

    api_key = CRED.api_key(api_key)
    model = model or os.environ.get("LLM_MODEL") or LP.DEFAULT_MODEL
    base = base or os.environ.get("LLM_BASE") or LP.DEFAULT_BASE
    status = "skipped"
    if unmatched and use_llm and api_key:
        raw, status = _ask(sig, drawing, api_key=api_key, base=base, model=model,
                           digest=digest, timeout=timeout or DEFAULT_TIMEOUT)
        if raw:
            prof = LP.validate(raw, set(sig))
            for name, item in prof["layers"].items():
                if name in layers or float(item.get("confidence") or 0) < 0.5:
                    continue
                layers[name] = dict(item, source="llm")
            summary["symbols"], summary["operator_hints"] = \
                prof.get("symbols") or {}, prof.get("operator_hints") or {}
            summary["unit_hypothesis"] = prof.get("unit_hypothesis")
            summary["scope"] = prof.get("scope") or {}
            summary["errors"] = (prof.get("errors") or [])[:8] or None
    elif unmatched and not api_key:
        status = "no_key"

    # 模型没覆盖的层：关键词兜底（弱证据，confidence 只给 0.5，融合时会输给几何）
    if unmatched:
        kw = LP.heuristic_profile({n: sig[n] for n in unmatched})
        for name, item in (kw.get("layers") or {}).items():
            layers.setdefault(name, {"semantics": list(item["semantics"]),
                                     "confidence": 0.5,
                                     "reason": "关键词兜底", "source": "keyword"})
    summary.update({"status": status,
                    "llm_layers": sum(1 for r in layers.values()
                                      if r.get("source") == "llm")})
    profile = {"layers": layers,
               "symbols": (summary.get("symbols")
                           or LP.infer_symbols(sig, layers)),
               "operator_hints": summary.get("operator_hints") or {},
               "unit_hypothesis": summary.get("unit_hypothesis") or None,
               "scope": summary.get("scope") or {},
               "profile_version": registry.PROFILE_VERSION,
               "source": "auto-" + ("llm" if status in ("ok", "cached") else "heuristic")}
    # 只有模型真的答成才落盘：兜底方案不写进注册表，免得下次把它当"已验证"复用
    if status == "ok":
        _remember(profile, digest, dxf_path, root)
    summary["source"] = profile["source"]
    return _pack(profile, summary, profile["source"])


# ------------------------------------------------------------------ 模型调用
def _ask(sig, drawing, *, api_key, base, model, digest, timeout):
    """调用一次模型；失败/超时一律返回 ({}, 原因)，不重试。"""
    cache_path = os.path.join(os.getcwd(), ".dwg-auto-profile-cache.json")
    log_path = os.path.join(os.getcwd(), ".dwg-auto-profile-log.jsonl")
    key = _key(digest, sig, model)
    cache = _load(cache_path)
    hit = cache.get(key)
    if hit is not None:
        return hit, "cached"
    if os.environ.get("DWG_AUTO_PROFILE_REPLAY") == "1":
        return {}, "replay_miss"

    prompt = (LP.PROMPT.replace("{semantics}", str(SEMANTICS))
              .replace("{operators}", str(sorted(_operator_names()))))
    prompt += ("\n只能从上面的语义枚举与算子名中选择，不得新增类别；不确定选 [\"ignore\"]。\n"
               "整图统计：\n" + json.dumps(drawing, ensure_ascii=False) +
               "\n图层清单：\n" + json.dumps(sig, ensure_ascii=False, indent=1))
    started = time.perf_counter()
    try:
        raw = LP.call_llm(prompt, api_key=api_key, base=base, model=model,
                          timeout=timeout)
    except Exception as exc:                      # 超时/网络/JSON 全部降级
        _log(log_path, {"prompt": PROMPT_VERSION, "model": model, "ok": False,
                        "sec": round(time.perf_counter() - started, 1),
                        "error": str(exc)[:200]})
        return {}, "llm_error"
    _log(log_path, {"prompt": PROMPT_VERSION, "model": model, "ok": True,
                    "sec": round(time.perf_counter() - started, 1),
                    "layers": len(raw.get("layers") or {})})
    cache[key] = raw
    _save(cache_path, cache)
    return raw, "ok"


def _operator_names():
    from .operators import OPERATORS

    return OPERATORS.keys()


def _key(digest, sig, model):
    blob = json.dumps({"v": PROMPT_VERSION, "model": model, "digest": digest,
                       "sig": sig}, sort_keys=True, ensure_ascii=False,
                      default=str).encode()
    return hashlib.sha256(blob).hexdigest()


# ------------------------------------------------------------------ 缓存 / 注册表
def _cached(digest, root):
    if not digest:
        return None
    try:
        got = registry.lookup(digest=digest, root=root)
    except Exception:
        return None
    if not got:
        return None
    # 只有"模型产出并校验过"的方案才值得复用，兜底方案不进注册表
    if not str(got.get("source") or "").startswith("auto-"):
        return got
    return got


def _remember(profile, digest, dxf_path, root):
    try:
        registry.register(profile, dxf_path=dxf_path, digest=digest or None,
                          root=root)
    except Exception:
        pass                                   # 落盘失败不影响本次识别


def _pack(profile, summary, source):
    layers = {name: list(item.get("semantics") or ())
              for name, item in (profile.get("layers") or {}).items()}
    prior: dict[str, dict[str, float]] = {}
    for name, sems in layers.items():
        if not sems:
            continue
        conf = float((profile["layers"][name] or {}).get("confidence") or 0.0)
        prior[name] = {sem: round(max(0.05, conf), 3) for sem in sems}
    return {"profile": profile, "prior": prior, "summary": summary,
            "source": source, "layers": layers}


def _load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save(path, data):
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
    except Exception:
        pass


def _log(path, row):
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass
