"""把 profile 解析成识别执行方案（plan）。

识别器只认 plan["layers"][语义] 的图层列表，不写死图层名；profile 为空时
退回默认行为，保证兼容。

profile 允许的三类内容（与 ``architecture.md`` §4 一致）：

* ``layers``：图层 -> 语义（闭集），第一优先；
* ``operator_hints``：语义 -> 算子优先级，**只是排序提示**，不是开关；
* ``unit_hypothesis``：单位假设，必须由 :mod:`geometry` 的几何证据确认。

不接受图纸专属数值阈值（``tolerances`` 已移除）：阈值属于确定性代码。
"""
from __future__ import annotations

import json
from pathlib import Path

SEMANTICS = ("wall", "column", "door", "window", "room_name", "axis",
             "stair", "step", "apron", "north", "frame")

#: 图层名归一化：去掉装饰性前缀/后缀、大小写与分隔符差异后再比对。
#: 绘图过程里图层常被加前缀（"PZ_散水"、"X-REF$WALL"、"墙-改"），名字里的语义
#: 部分其实没变；如果只做全等匹配，一张被批量改过图层名的图就会整体失效。
_NAME_NOISE = ("pz_", "pz-", "xref_", "xref-", "$0$", "a-$0$")


def normalize_layer(name) -> str:
    """图层名 -> 用于比对的归一化键（保留中文与字母数字，丢掉其余符号）。"""
    s = str(name or "").strip().lower()
    for noise in _NAME_NOISE:
        if s.startswith(noise):
            s = s[len(noise):]
    return "".join(ch for ch in s if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def resolve_layers(declared, available):
    """把声明的图层名**对齐到图纸里真实存在的图层名**。

    先全等，再按归一化键匹配——这样"散水"能匹配到"PZ_散水"、"WALL"能匹配到
    "a-$0$WALL"。匹配不到就保留原声明名（可能确实不存在），不擅自猜别层。
    """
    have = {str(n): str(n) for n in (available or ())}
    if not have:
        return [str(n) for n in (declared or ())]
    by_norm = {}
    for name in have:
        by_norm.setdefault(normalize_layer(name), name)
    out = []
    for name in declared or ():
        if str(name) in have:
            out.append(str(name))
            continue
        hit = by_norm.get(normalize_layer(name))
        if hit:
            out.append(hit)
    return out

MIN_CONFIDENCE = 0.5


def load_plan(profile=None) -> dict:
    """profile 可为 dict、JSON 路径或 None。"""
    if not profile:
        return empty_plan()
    if isinstance(profile, (str, Path)):
        profile = json.loads(Path(profile).read_text(encoding="utf-8"))
    layers: dict[str, list[str]] = {s: [] for s in SEMANTICS}
    for name, item in (profile.get("layers") or {}).items():
        if float(item.get("confidence", 0)) < MIN_CONFIDENCE:
            continue
        sems = item.get("semantics", item.get("semantic", "ignore"))
        if isinstance(sems, str):
            sems = [sems]
        for sem in sems:
            if sem in layers:
                layers[sem].append(name)
    hints = {}
    for sem, names in (profile.get("operator_hints") or {}).items():
        if sem in layers and isinstance(names, (list, tuple)):
            hints[sem] = [str(n) for n in names]
    return {"layers": layers,
            "symbols": profile.get("symbols") or {},
            "operator_hints": hints,
            "unit_hypothesis": (profile.get("unit_hypothesis")
                                or profile.get("units")),
            "scope": profile.get("scope") or {},
            "source": profile.get("source"),
            "profile_version": profile.get("profile_version"),
            "reviewed": bool(profile.get("reviewed"))}


def operators_for(plan: dict, semantic: str, defaults=()) -> list[str]:
    """该语义下应尝试的算子：提示优先排序，兜底默认全集（提示不是开关）。"""
    hinted = [n for n in (plan.get("operator_hints") or {}).get(semantic, ())]
    out = list(dict.fromkeys(hinted + list(defaults)))
    return out


def symbols_for(plan: dict, semantic: str) -> list[dict]:
    """把 profile 里的画法声明统一成 [{"kind":..., ...}] 结构。"""
    raw = (plan.get("symbols") or {}).get(semantic) or []
    out = []
    for item in raw:
        if isinstance(item, str):
            out.append({"kind": item})
        elif isinstance(item, dict) and item.get("kind"):
            out.append(item)
    return out


def has_symbol(plan: dict, semantic: str, kind: str) -> bool:
    return any(s["kind"] == kind for s in symbols_for(plan, semantic))


def empty_plan() -> dict:
    return {"layers": {s: [] for s in SEMANTICS}, "symbols": {},
            "operator_hints": {}, "unit_hypothesis": None,
            "scope": {}, "source": None, "profile_version": None,
            "reviewed": False}
