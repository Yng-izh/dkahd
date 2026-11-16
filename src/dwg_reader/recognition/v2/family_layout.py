"""族成员的**整体布局**与**墙线交接**：从全部成员算出来的可核对事实。

为什么要有这一层（实测，2026-11-15）：两个"单个实心方块填充"的柱族都只读到
``column/medium``，模型给出的备选解释是"墙体填充（poché）"和"填充标注"。原来
给模型的事实只有代表实例的局部量（长宽比、离墙距离），**看不到 28 个成员在整张
平面上怎么分布，也看不到墙线在方块边界上是断开还是穿过**——而这两件事恰好是
区分"柱 / 墙体填充 / 标注"的几何依据。

这里只算关系量与比值，不下类别结论，也不读层名/块名：

* :func:`member_layout`：成员中心是否成行成列、行列内间距是否重复、彼此离多远、
  有没有重叠重复画；
* :func:`wall_interaction`：每个成员四条边外侧的线是"在边界上终止"（墙被它打断）、
  "从下面穿过"（它压在墙上）、"与边齐平并两侧延续"（它贴着墙面），还是周围无线；
* :func:`fill_types`：成员填充是实心还是图案。

这些数同时写进族判读结果，作为与模型无关的独立几何证据留档。
"""
from __future__ import annotations

import math

from . import cad_ir as IR
from .fitters import _common as SH

SCHEMA = "family-layout/1.0"

#: 比率参数（相对成员尺寸/墙厚/噪声，不是图纸绝对阈值）
LIMITS = {
    "max_members": 240,        # 最多统计多少个成员
    "align_ratio": 0.10,       # 共线容差 = 成员尺寸 × 该比值（不低于 4×噪声）
    "min_line": 3,             # 至少几个成员才算一行/一列
    "gap_ratio": 0.05,         # 间距"重复"的相对容差
    "reach_ratio": 1.0,        # 边外侧取线范围 = max(墙厚, 成员尺寸) × 该比值
    "parallel_deg": 5.0,       # 与成员边同向的角度容差
    "edge_ratio": 0.08,        # "落在边界上"的容差 = 成员尺寸 × 该比值（不低于 4×噪声）
    "coincide_ratio": 0.25,    # 两个成员中心距 < 尺寸 × 该比值 → 视为重复画
}



def _r(value, nd: int = 3):
    return None if value is None else SH._r(value, nd)


def _visible(inst: dict, by_uid: dict) -> list:
    """成员的可见图元：块参照本身（IR 包围盒只是插入点）不算，算它展开后的子图元。"""
    return [by_uid[u] for u in inst.get("uids") or ()
            if u in by_uid and by_uid[u].get("kind") != "insert"
            and by_uid[u].get("bbox")]


def _members(family: dict, ir: dict) -> list:
    """``[(instance, box, visible_entities)]``：box 取可见图元外接框。

    实测：块族实例的 ``bbox`` 是插入点（零面积），拿它当成员尺寸会让"间距/尺寸"
    变成 1e12 这种数直接进提示词。可见图元算不出框时才退回实例自带 bbox；
    两者都是零面积的成员不参与统计。
    """
    by_uid = IR.by_uid(ir)
    rows = []
    for inst in (family.get("instances") or ())[:int(LIMITS["max_members"])]:
        if not isinstance(inst, dict):
            continue
        visible = _visible(inst, by_uid)
        box = SH._union([e["bbox"] for e in visible]) if visible else None
        if not box or _size(box) <= 0:
            box = inst.get("bbox")
        if isinstance(box, (list, tuple)) and len(box) == 4 and _size(box) > 0:
            rows.append((inst, list(box), visible))
    return rows


def _size(box) -> float:
    return max(float(box[2]) - float(box[0]), float(box[3]) - float(box[1]), 0.0)


def _groups(values, tol: float) -> list:
    """一维聚类：按值排序，与组首差不超过 ``tol`` 的归一组（不链式漂移）。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    groups: list = []
    for index in order:
        if groups and values[index] - values[groups[-1][0]] <= tol:
            groups[-1].append(index)
        else:
            groups.append([index])
    return groups


def member_layout(family: dict, ir: dict) -> dict:
    """成员中心的排列规律（全部成员，不是抽样）。

    * ``aligned_fraction``：落在某条"≥3 个成员共线"的 X 或 Y 线上的成员比例；
    * ``grid_fraction``：同时落在一条 X 线和一条 Y 线上的成员比例（成网格）；
    * ``spacing``：行/列内相邻成员间距的重复程度（``repeat_fraction``）与
      不同间距档数；间距只给"相对成员尺寸"的倍数；
    * ``nearest_over_size``：最近邻距离 / 成员尺寸的中位与最小值；
    * ``coincident_pairs``：中心几乎重合的成员对（同一处重复画）；
    * ``spread_over_drawing``：成员外接框对角线 / 全图外接框对角线。
    """
    members = _members(family, ir)
    out = {"schema": SCHEMA, "members": len(members),
           "total_members": int(family.get("n") or len(family.get("instances") or ()))}
    if len(members) < 2:
        out["available"] = False
        return out
    basis = ir.get("basis") or {}
    noise = float(basis.get("noise") or 2.0)
    centers = [SH._center(box) for _, box, _ in members]
    size = max(noise, SH._median([_size(box) for _, box, _ in members]))
    tol = max(4.0 * noise, float(LIMITS["align_ratio"]) * size)
    need = int(LIMITS["min_line"])
    xs = [c[0] for c in centers]
    ys = [c[1] for c in centers]
    # x 相同 → 一列（竖线）；y 相同 → 一行（横线）
    columns = [g for g in _groups(xs, tol) if len(g) >= need]
    rows = [g for g in _groups(ys, tol) if len(g) >= need]
    on_col = {i for g in columns for i in g}
    on_row = {i for g in rows for i in g}
    gaps = []
    for group, axis in [(g, 1) for g in columns] + [(g, 0) for g in rows]:
        line = sorted(centers[i][axis] for i in group)
        gaps.extend(b - a for a, b in zip(line, line[1:]) if b - a > tol)
    spacing = {"gaps": len(gaps)}
    if gaps:
        rel = float(LIMITS["gap_ratio"])
        bins: list = []
        for gap in sorted(gaps):
            if bins and abs(gap - bins[-1][0]) <= rel * bins[-1][0]:
                bins[-1].append(gap)
            else:
                bins.append([gap])
        repeated = sum(len(b) for b in bins if len(b) >= 2)
        spacing.update({"repeat_fraction": _r(repeated / float(len(gaps))),
                        "distinct": len(bins),
                        "median_over_size": _r(SH._median(gaps) / size, 2)})
    nearest = []
    coincident = 0
    for i, (cx, cy) in enumerate(centers):
        best = None
        for j, (ox, oy) in enumerate(centers):
            if i == j:
                continue
            d = math.hypot(cx - ox, cy - oy)
            if j > i and d < float(LIMITS["coincide_ratio"]) * size:
                coincident += 1
            best = d if best is None or d < best else best
        if best is not None:
            nearest.append(best / size)
    union = SH._union([box for _, box, _ in members])
    drawing = ir.get("bbox")
    spread = None
    if union and isinstance(drawing, (list, tuple)) and len(drawing) == 4:
        diag = math.hypot(float(drawing[2]) - float(drawing[0]),
                          float(drawing[3]) - float(drawing[1]))
        if diag > 0:
            spread = math.hypot(union[2] - union[0], union[3] - union[1]) / diag
    out.update({"available": True,
                "aligned_fraction": _r(len(on_col | on_row) / float(len(members))),
                "grid_fraction": _r(len(on_col & on_row) / float(len(members))),
                "lines": {"vertical": len(columns), "horizontal": len(rows),
                          "largest": max([len(g) for g in columns + rows] or [0])},
                "spacing": spacing,
                "nearest_over_size": {"median": _r(SH._median(nearest), 2),
                                      "min": _r(min(nearest), 2)} if nearest else {},
                "coincident_pairs": int(coincident),
                "spread_over_drawing": _r(spread)})
    return out


def _frame(entities, box):
    """成员自己的局部坐标系：中心、u 轴角度、半长 hu/hv（旋转成员也成立）。"""
    rect = SH.min_area_rect(SH.points_of(entities)) if entities else None
    if rect and rect["w"] > 0 and rect["h"] > 0:
        return (rect["center"], float(rect["angle"]),
                float(rect["w"]) / 2.0, float(rect["h"]) / 2.0)
    center = SH._center(box)
    return (center, 0.0, (float(box[2]) - float(box[0])) / 2.0,
            (float(box[3]) - float(box[1])) / 2.0)


def _classify(a, b, frame, *, edge: float, parallel: float):
    """一条线相对成员的关系：``(kind, side)``；不相关返回 ``None``。

    kind ∈ ``through``（从成员内部穿过）、``flush``（沿成员边齐平并两侧伸出）、
    ``ends_at``（在成员某条边上终止、向外延伸）、``outline``（就在成员边界上，
    不伸出）、``ends_inside``（从外面伸进成员内部后终止）。
    """
    center, angle, hu, hv = frame
    ang = math.radians(angle)
    ux, uy = math.cos(ang), math.sin(ang)

    def local(p):
        dx, dy = float(p[0]) - center[0], float(p[1]) - center[1]
        return dx * ux + dy * uy, -dx * uy + dy * ux

    (au, av), (bu, bv) = local(a), local(b)
    seg_angle = SH._angle(a, b)
    if SH._angle_diff(seg_angle, angle) <= parallel:
        along, offset, half_along, half_across, sides = (
            (au, bu), (av + bv) / 2.0, hu, hv, ("u-", "u+"))
    elif SH._angle_diff(seg_angle, angle + 90.0) <= parallel:
        along, offset, half_along, half_across, sides = (
            (av, bv), (au + bu) / 2.0, hv, hu, ("v-", "v+"))
    else:
        return None
    if abs(offset) > half_across + edge:
        return None                         # 在成员旁边平行走过，不与它交接
    lo, hi = min(along), max(along)
    if hi < -half_along - edge or lo > half_along + edge:
        return None                         # 整条在成员一侧之外（被索引带进来）
    on_edge = abs(abs(offset) - half_across) <= edge
    out_lo, out_hi = lo < -half_along - edge, hi > half_along + edge
    if out_lo and out_hi:
        return ("flush", None) if on_edge else ("through", None)
    if out_lo and abs(hi + half_along) <= edge:
        return ("ends_at", sides[0])
    if out_hi and abs(lo - half_along) <= edge:
        return ("ends_at", sides[1])
    if not out_lo and not out_hi:
        return ("outline", None) if on_edge else None
    return ("ends_inside", None)


def _line_index(ir: dict, cell: float):
    rows = [e for e in ir.get("entities") or ()
            if e.get("kind") in ("segment", "poly") and e.get("bbox")]
    return SH.near_index(rows, cell=cell)


def wall_interaction(family: dict, ir: dict, walls=()) -> dict:
    """墙线在每个成员边界上的交接方式（全部成员）。

    柱打断墙：墙面线在柱的两条对边上终止（``interrupted_opposite``）；
    墙体填充：墙面线从填充下面穿过或沿边齐平延续（``through`` / ``flush``）；
    标注/孤立填充：周围没有与之交接的线（``isolated``）。
    同时给已接受墙轴线（``walls``）的同类统计。只给比例，不下结论。
    """
    members = _members(family, ir)
    out = {"schema": SCHEMA, "members": len(members)}
    if not members:
        out["available"] = False
        return out
    basis = ir.get("basis") or {}
    noise = float(basis.get("noise") or 2.0)
    thickness = float(basis.get("thickness") or 200.0)
    size = max(noise, SH._median([_size(box) for _, box, _ in members]))
    edge = max(4.0 * noise, float(LIMITS["edge_ratio"]) * size)
    parallel = float(LIMITS["parallel_deg"])
    pad = max(thickness, size) * float(LIMITS["reach_ratio"])
    index = _line_index(ir, cell=max(1.0, 4.0 * max(thickness, size)))
    wall_segments = [(w["start"], w["end"]) for w in walls or ()
                     if isinstance(w, dict) and w.get("start") and w.get("end")]
    tally = {"interrupted_opposite": 0, "ends_at_any": 0, "through": 0,
             "flush": 0, "isolated": 0, "outlined": 0, "ends_inside": 0}
    axis_tally = {"ends_at": 0, "through": 0, "none": 0}
    for member, box, entities in members:
        own = set(member.get("uids") or ())
        frame = _frame(entities, box)
        sides, kinds = set(), {}
        for entity in SH.near_entities(index, box, pad=pad):
            if entity.get("uid") in own:
                continue
            for a, b in SH.segments_of(entity):
                hit = _classify(a, b, frame, edge=edge, parallel=parallel)
                if hit is None:
                    continue
                kinds[hit[0]] = kinds.get(hit[0], 0) + 1
                if hit[1]:
                    sides.add(hit[1])
        if {"u-", "u+"} <= sides or {"v-", "v+"} <= sides:
            tally["interrupted_opposite"] += 1
        tally["ends_at_any"] += int(bool(sides))
        tally["through"] += int(kinds.get("through", 0) > 0)
        tally["flush"] += int(kinds.get("flush", 0) > 0)
        tally["ends_inside"] += int(kinds.get("ends_inside", 0) > 0)
        tally["outlined"] += int(kinds.get("outline", 0) >= 2)
        tally["isolated"] += int(not any(kinds.get(k) for k in
                                         ("through", "flush", "ends_at", "ends_inside")))
        if wall_segments:
            axis = [_classify(a, b, frame, edge=max(edge, thickness / 2.0),
                              parallel=parallel) for a, b in wall_segments]
            axis = [h for h in axis if h is not None]
            if any(h[0] == "through" for h in axis):
                axis_tally["through"] += 1
            elif any(h[0] in ("ends_at", "ends_inside") for h in axis):
                axis_tally["ends_at"] += 1
            else:
                axis_tally["none"] += 1
    n = float(len(members))
    out.update({"available": True,
                "fractions": {key: _r(value / n) for key, value in tally.items()}})
    if wall_segments:
        out["wall_axes"] = {key: _r(value / n) for key, value in axis_tally.items()}
    else:
        out["wall_axes"] = {"available": False}
    return out


def fill_types(family: dict, ir: dict) -> dict:
    """成员的填充类型计数：实心 / 图案 / 无填充。"""
    counts = {"solid": 0, "pattern": 0, "no_hatch": 0}
    for _, _, visible in _members(family, ir):
        hatches = [e for e in visible if e.get("kind") == "hatch"]
        if not hatches:
            counts["no_hatch"] += 1
        elif any((h.get("geom") or {}).get("solid") for h in hatches):
            counts["solid"] += 1
        else:
            counts["pattern"] += 1
    return counts


def facts(family: dict, ir: dict, walls=()) -> dict:
    """三组事实合并成一个紧凑字典（进提示词，也进判读结果留档）。"""
    return {"member_layout": member_layout(family, ir),
            "wall_interaction": wall_interaction(family, ir, walls),
            "fill_types": fill_types(family, ir)}


__all__ = ["LIMITS", "SCHEMA", "facts", "fill_types", "member_layout",
           "wall_interaction"]
