"""通用几何算子：每个算子声明适用条件，自行产出候选（不判最终身份）。

算子只消费 :mod:`primitives` 的原语，输出"候选 + 证据"，由构件识别器决定采纳
与否。算子不做图层白名单之外的语义判断，也不产生阈值副作用。

=============== =============================================================
``arc_feature`` 圆弧：全部有效弧都产出（是否像门由洞口判定说了算）
``closed_contour`` 闭合轮廓：用真实折线 + 最小外接矩形，旋转柱不再被漏掉
``line_bundle`` 平行线束：**局部线对**生成（不再传递式聚类）
``parallel_pair`` 平行线对：单个线对，不做线束合并
``paired_ticks`` 对称刻度：轴线 / 标注线上的成对短笔画
``hatch_region`` 闭合填充边界：构造柱 / 实心墙 / 柱墩
=============== =============================================================

算子名可由 profile 的 ``operator_hints`` 排序，但**不是硬开关**：确定性候选
生成器在满足算子前置条件时照常执行，模型漏选不会导致识别条件缺失。
"""
from __future__ import annotations

from . import geometry as GEO
from . import primitives as PRIM

OPERATORS: dict[str, callable] = {}

#: 线束/线对的角向分箱（度）
ANGLE_BUCKET = 1.0
#: 闭合轮廓的形态约束（与人工标注口径一致：150~2000mm 的矩形柱）
CONTOUR_SHORT = (150.0, 2000.0)
CONTOUR_ASPECT = 1.6
CONTOUR_FILL = 0.85
CONTOUR_MAX_VERTICES = 8


def operator(name: str):
    def deco(fn):
        OPERATORS[name] = fn
        return fn
    return deco


def _layers_ok(seg, wanted) -> bool:
    return not wanted or seg.get("layer") in wanted


def _sweep_ok(arc) -> bool:
    return 0.0 < float(arc.get("radius") or 0.0)


@operator("arc_feature")
def arc_feature(prims, layers, thick, cfg):
    """全部有效弧 -> 弧特征候选（含圆心/半径/张角/端点）。"""
    out = []
    for a in prims["arcs"]:
        if not _sweep_ok(a) or not _layers_ok(a, layers):
            continue
        out.append({"kind": "arc", "p1": a["p1"], "p2": a["p2"],
                    "center": a["center"], "radius": a["radius"],
                    "sweep": a["sweep"], "full": a["full"],
                    "chord": a["chord"], "layer": a["layer"],
                    "handles": [a["handle"]]})
    return out


@operator("closed_contour")
def closed_contour(prims, layers, thick, cfg):
    """闭合轮廓候选：保留真实折线，用最小外接矩形判形态（支持旋转）。"""
    out = []
    for c in prims["closed"]:
        if not _layers_ok(c, layers):
            continue
        if c.get("n", 0) > CONTOUR_MAX_VERTICES or c.get("has_bulge"):
            continue
        shape = c.get("shape") or GEO.shape_of(c["outline"])
        rect = shape["rect"]
        short = min(rect["width"], rect["height"])
        long_side = max(rect["width"], rect["height"])
        if not (CONTOUR_SHORT[0] <= short and long_side <= CONTOUR_SHORT[1]):
            continue
        if long_side / (short or 1.0) > CONTOUR_ASPECT:
            continue
        if shape["fill"] < CONTOUR_FILL:
            continue
        out.append({"kind": "contour", "outline": c["outline"], "bbox": c["bbox"],
                    "area": shape["area"], "rect": rect,
                    "rotated": shape["rotated"], "layer": c["layer"],
                    "handles": [c["handle"]]})
    return out


@operator("hatch_region")
def hatch_region(prims, layers, thick, cfg):
    """闭合填充边界 -> 区域候选（构造柱、实心墙、柱墩）。

    填充边界是"画出来的实体轮廓"，比闭合多段线更贴近实体柱；只在 profile 指明
    语义的图层上产出，避免把装饰填充当构件。
    """
    out = []
    for h in prims.get("hatches") or ():
        if not _layers_ok(h, layers):
            continue
        loops = h.get("loops") or ()
        if not loops:
            continue
        loop = max(loops, key=len)
        if len(loop) < 3:
            continue
        shape = GEO.shape_of(loop)
        rect = shape["rect"]
        short = min(rect["width"], rect["height"])
        long_side = max(rect["width"], rect["height"])
        if not (CONTOUR_SHORT[0] <= short and long_side <= CONTOUR_SHORT[1]):
            continue
        if long_side / (short or 1.0) > CONTOUR_ASPECT:
            continue
        if shape["fill"] < CONTOUR_FILL:
            continue
        out.append({"kind": "hatch_region", "outline": loop, "bbox": rect["bbox"],
                    "area": shape["area"], "rect": rect,
                    "rotated": shape["rotated"], "layer": h["layer"],
                    "handles": [h["handle"]]})
    return out


def _angle_buckets(segs):
    buckets: dict[float, list] = {}
    for s in segs:
        key = round(s["angle"] / ANGLE_BUCKET) * ANGLE_BUCKET
        buckets.setdefault(key, []).append(s)
    return buckets


def _local_pairs(segs, tol, min_overlap=150.0, min_ratio=0.5):
    """局部线对：**只连相邻位置的平行线**，不做传递式聚类。

    ``tol`` 是线对之间的最大垂距（不是"整束跨度"），因此一束线不会因为中间
    夹着无关平行线而被粘成一大团。
    """
    pairs = []
    order = sorted(segs, key=lambda s: s["pos"])
    for i, a in enumerate(order):
        for b in order[i + 1:]:
            d = b["pos"] - a["pos"]
            if d > tol:
                break
            if d <= 0:
                continue
            ov = GEO.overlap_1d(a["lo"], a["hi"], b["lo"], b["hi"])
            short = min(a["length"], b["length"]) or 1.0
            if ov < max(min_overlap, min_ratio * short):
                continue
            pairs.append((a, b, ov))
    return pairs


def _components(segs, pairs):
    parent = {id(s): id(s) for s in segs}
    index = {id(s): s for s in segs}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for a, b, _ov in pairs:
        ra, rb = find(id(a)), find(id(b))
        if ra != rb:
            parent[rb] = ra
    groups: dict[int, list] = {}
    for s in segs:
        groups.setdefault(find(id(s)), []).append(index[id(s)])
    return list(groups.values())


@operator("parallel_pair")
def parallel_pair(prims, layers, thick, cfg):
    """平行线对：每个线对单独输出（门窗双线、线脚画法）。"""
    tol = max(120.0, 1.5 * (thick or 200.0))
    segs = [s for s in prims["lines"] if _layers_ok(s, layers)]
    out = []
    for ang, group in _angle_buckets(segs).items():
        for a, b, ov in _local_pairs(group, tol):
            out.append({"kind": "pair", "angle": ang, "dir": a["dir"], "nrm": a["nrm"],
                        "pos": (a["pos"] + b["pos"]) / 2.0,
                        "gap": abs(b["pos"] - a["pos"]),
                        "lo": max(a["lo"], b["lo"]), "hi": min(a["hi"], b["hi"]),
                        "overlap": ov, "layer": a["layer"],
                        "handles": [a["handle"], b["handle"]]})
    return out


@operator("line_bundle")
def line_bundle(prims, layers, thick, cfg):
    """平行线束：局部线对 -> 连通线束；单位是"一樘"而不是"一串"。

    与旧实现的差别：旧版用传递式聚类（A~B、B~C 就把 A、C 并到一起），一条
    墙线上零散的窗框线会被粘成一个大线束，产生重复窗；这里每个线对都必须
    满足"垂距 <= tol 且沿向重叠达标"，跨得过远的线对不成立。
    """
    tol = max(120.0, 1.5 * (thick or 200.0))
    min_span = max(150.0, thick or 150.0)
    segs = [s for s in prims["lines"] if _layers_ok(s, layers)]
    out = []
    for ang, group in _angle_buckets(segs).items():
        pairs = _local_pairs(group, tol)
        if not pairs:
            continue
        for comp in _components(group, pairs):
            if len(comp) < 2:
                continue
            lo = max(s["lo"] for s in comp)
            hi = min(s["hi"] for s in comp)
            if hi - lo < min_span:
                continue
            pos = sum(s["pos"] for s in comp) / len(comp)
            handles, seen = [], set()
            for s in sorted(comp, key=lambda s: (s["lo"], s["handle"])):
                if s["handle"] in seen:
                    continue
                seen.add(s["handle"])
                handles.append(s["handle"])
            first = comp[0]
            out.append({"kind": "bundle", "angle": ang, "orient": first["orient"],
                        "dir": first["dir"], "nrm": first["nrm"], "pos": pos,
                        "lo": lo, "hi": hi, "layer": first["layer"],
                        "members": len(comp), "handles": handles})
    return out


@operator("paired_ticks")
def paired_ticks(prims, layers, thick, cfg):
    """对称刻度：轴线/尺寸线上的成对短笔画（轴网画法的一种）。

    判据：一条长线 + 两侧各一条与之近似垂直的短笔画，沿向位置差 <= 150mm。
    产出 ``kind="tick_axis"``，只作为轴线候选，不做"这是几号轴"的解释。
    """
    ticks = [t for t in prims["ticks"] if _layers_ok(t, layers)]
    longs = [s for s in prims["lines"] if _layers_ok(s, layers) and s["length"] >= 3000.0]
    out = []
    for line in longs:
        u0, u1 = line["lo"], line["hi"]
        near = []
        for t in ticks:
            da = abs(t["angle"] - line["angle"])
            da = min(da, 180.0 - da)
            if abs(da - 90.0) > 4.0:
                continue
            mid = t["origin"]
            u = GEO.proj(line, t["origin"])
            v = GEO.perp(line, t["origin"])
            if not (u0 - 200.0 <= u <= u1 + 200.0) or abs(v) > 120.0:
                continue
            near.append((u, v, t, mid))
        near.sort(key=lambda it: it[0])
        for i in range(len(near)):
            for j in range(i + 1, len(near)):
                ua, va, ta, _ = near[i]
                ub, vb, tb, _ = near[j]
                if ub - ua > 150.0:
                    break
                if va * vb >= 0:            # 必须分居两侧
                    continue
                out.append({"kind": "tick_axis", "angle": line["angle"],
                            "dir": line["dir"], "nrm": line["nrm"],
                            "pos": line["pos"], "location": [(ua + ub) / 2.0],
                            "layer": line["layer"],
                            "handles": [line["handle"], ta["handle"], tb["handle"]]})
                break
    return out


def available() -> list[str]:
    return sorted(OPERATORS)


def run(names, snapshot, layers=None, thick=None, cfg=None, prims=None):
    """按算子名执行；算子自行判断前置条件，不做外部硬过滤。

    ``layers=None`` 表示该算子对全部图层生效；若要限定图层请显式传集合。
    """
    prims = prims or PRIM.extract(snapshot)
    out = []
    for name in names or ():
        fn = OPERATORS.get(name)
        if fn is None:
            continue
        out.extend(fn(prims, layers, thick, cfg))
    return out
