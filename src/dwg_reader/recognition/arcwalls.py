"""弧墙：同心圆弧配对（同圆心 + 半径差 = 墙厚 + 角度区间重叠）。

为什么必须单开一条通路
----------------------
``walls`` 的面线配对建立在"直线 + 单一法向偏移"上：轴向路径 ``_faces`` 按一个
坐标值聚类，斜向路径 ``_oblique_walls`` 按角度分桶后同桶配对。圆弧的每一小段
法向都不同，"厚度"也不是常量偏移而是**半径差**——把阈值放宽多少都配不出来，
而 ``segments_from`` 只认 LINE/LWPOLYLINE，ARC 连候选池都进不去。

圆弧有它自己的不变量，而且跨图纸不变：

* 一道弧墙 = 两条**同圆心**圆弧；
* 两弧**半径差 = 墙厚**；
* 两弧**角度区间重叠**（重叠部分才是墙，缺的是洞口）。

这里只做这一件事：按上面三条配对，输出与直线墙同构的记录（``orient="A"``，
``outline`` 折线化成闭合带），下游照常消费。判据全部是图内倍率，没有裸毫米数。
"""
from __future__ import annotations

import collections
import math

from . import scale as SC
from .snapshot import geometry_records

#: 只有模型空间图元进弧墙候选。
#: 块展开几何（``derived``）里的弧大半是门扇开启弧、洁具、图例，它们同样成对、
#: 同样同圆心，按"半径差 = 墙厚"能凑出大量假弧墙。弧墙画在模型空间是常态，
#: 逐图实测比放宽阈值更可靠。
ARC_SOURCES = ("entities",)


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _sweep(start_angle, end_angle) -> float:
    """圆弧张角（度，0~360]，与 DXF 一致：逆时针 start→end。"""
    s = (_num(end_angle) - _num(start_angle)) % 360.0
    return 360.0 if s <= 1e-6 else s


def _intervals(start_angle, sweep):
    """把 [start, start+sweep] 拆成 [0,360) 内不超过两段。"""
    lo = _num(start_angle) % 360.0
    hi = lo + sweep
    if hi <= 360.0:
        return [(lo, hi)]
    return [(lo, 360.0), (0.0, hi - 360.0)]


def _overlap(a, b):
    """两段角区间的交叠长度（度）。"""
    total = 0.0
    for a0, a1 in a:
        for b0, b1 in b:
            total += max(0.0, min(a1, b1) - max(a0, b0))
    return total


def _clip(start_angle, sweep, other):
    """当前弧上被 ``other`` 覆盖的子区间（度，已回到当前弧的起始基准）。"""
    base = _num(start_angle) % 360.0
    out = []
    for lo, hi in _intervals(start_angle, sweep):
        for olo, ohi in other:
            a, b = max(lo, olo), min(hi, ohi)
            if b > a:
                out.append(((a - base) % 360.0, (b - base) % 360.0))
    if not out:
        return None
    # 取覆盖最长的一段：弧墙只按一段连续重叠输出。返回 (起始偏移, 长度)。
    out.sort(key=lambda iv: iv[1] - iv[0], reverse=True)
    lo, hi = out[0]
    return lo, hi - lo


def _point(cx, cy, radius, deg):
    a = math.radians(deg)
    return [cx + radius * math.cos(a), cy + radius * math.sin(a)]


def _band_outline(cx, cy, r_in, r_out, start_angle, sweep, step_deg):
    """把弧带折线化成闭合多边形：内弧正序 + 外弧逆序。"""
    n = int(math.ceil(abs(sweep) / max(step_deg, 0.1)))
    n = max(2, min(72, n))
    angs = [start_angle + sweep * i / float(n) for i in range(n + 1)]
    inner = [_point(cx, cy, r_in, a) for a in angs]
    outer = [_point(cx, cy, r_out, a) for a in reversed(angs)]
    return inner + outer


def collect_arcs(snapshot, layers) -> list:
    """墙图层上的模型空间圆弧：``{center, radius, start_angle, sweep, handle, layer}``。"""
    wanted = set(layers or ())
    out = []
    for rec in geometry_records(snapshot):
        if rec.get("type") != "ARC":
            continue
        if rec.get("derived"):        # 块展开几何不进弧墙候选，见模块头注释
            continue
        if rec.get("layer") not in wanted:
            continue
        center = rec.get("center") or ()
        radius = _num(rec.get("radius"))
        if len(center) != 2 or radius <= 0.0:
            continue
        sweep = _sweep(rec.get("start_angle"), rec.get("end_angle"))
        out.append({"center": [_num(center[0]), _num(center[1])],
                    "radius": radius,
                    "start_angle": _num(rec.get("start_angle")) % 360.0,
                    "sweep": sweep,
                    "handle": rec.get("handle") or rec.get("dxf_handle"),
                    "layer": rec.get("layer"),
                    "region": rec.get("region")})
    return out


def _center_groups(arcs, tol):
    """按圆心聚类（容差 = 倍率 × 墙厚，不是裸毫米数）。"""
    groups = []
    for arc in sorted(arcs, key=lambda a: (round(a["center"][0], 1),
                                           round(a["center"][1], 1),
                                           a["radius"])):
        placed = False
        for g in groups:
            cx, cy = g["center"]
            if (abs(arc["center"][0] - cx) <= tol
                    and abs(arc["center"][1] - cy) <= tol):
                g["arcs"].append(arc)
                n = len(g["arcs"])
                g["center"] = [(cx * (n - 1) + arc["center"][0]) / n,
                               (cy * (n - 1) + arc["center"][1]) / n]
                placed = True
                break
        if not placed:
            groups.append({"center": list(arc["center"]), "arcs": [arc]})
    return groups


def detect(snapshot, layers, *, thickness=None, thickness_hint=None,
           is_curtain=None) -> tuple[list, dict]:
    """从快照里找弧墙。

    ``thickness``：本图墙厚基准（量出来的），用于把"半径差"换算成倍率；
    ``thickness_hint``：面线配对的中心值，用于在多层弧（幕墙）里挑更可信的那对。
    返回 ``(walls, diag)``，walls 与 :mod:`walls` 的墙记录同构（``orient="A"``）。
    """
    base_t = _num(thickness) or _num(thickness_hint) or SC.DEFAULT_THICKNESS
    tol = SC.STRUCTURE["arc_center_tol_thickness"] * base_t
    t_min = SC.STRUCTURE["arc_thickness_min_thickness"] * base_t
    t_max = SC.STRUCTURE["arc_thickness_max_thickness"] * base_t
    min_r = SC.STRUCTURE["arc_radius_min_thickness"] * base_t
    span_overlap = SC.STRUCTURE["arc_span_overlap"]
    min_sweep = SC.STRUCTURE["arc_min_sweep_deg"]
    mid_ratio = SC.STRUCTURE["arc_mid_block_ratio"]
    step_deg = SC.STRUCTURE["arc_tess_deg"]
    hint = _num(thickness_hint) or base_t

    arcs = [a for a in collect_arcs(snapshot, layers) if a["radius"] >= min_r]
    groups = _center_groups(arcs, tol)
    walls, rejected = [], collections.Counter()
    for group in groups:
        members = sorted(group["arcs"], key=lambda a: a["radius"])
        cx, cy = group["center"]
        # 三层同心弧（弧形幕墙）：等距 + 中间那层是幕墙层 -> 一条带，取外两条为面。
        # 与直线幕墙同一条口径，见 curtain.py 模块头。非幕墙的三层弧保持原判据
        # （中间夹第三条否决外层），不改动已有行为。
        band = _curtain_band(cx, cy, members, base_t, step_deg, is_curtain, hint)
        if band is not None:
            walls.append(band)
            continue
        for i, inner in enumerate(members):
            for outer in members[i + 1:]:
                t = outer["radius"] - inner["radius"]
                if t < t_min:
                    continue
                if t > t_max:
                    break
                ov = _clip(inner["start_angle"], inner["sweep"],
                           _intervals(outer["start_angle"], outer["sweep"]))
                if ov is None:
                    rejected["角度无重叠"] += 1
                    continue
                ov_lo, ov_len = ov
                if ov_len < min_sweep:
                    rejected["重叠弧长过短"] += 1
                    continue
                shorter = min(inner["sweep"], outer["sweep"])
                if ov_len < span_overlap * shorter:
                    rejected["覆盖比例不足"] += 1
                    continue
                # 与直线墙同构的"跨中线"否决：两弧之间还有一条弧把厚度劈成两段
                # 真墙（三线幕墙就是这样），当前这对就是外层跨中配对，不是一道墙。
                blocked = False
                for mid in members[i + 1:]:
                    if mid["radius"] >= outer["radius"] - 1.0:
                        break
                    if (mid["radius"] - inner["radius"] < mid_ratio * t
                            or outer["radius"] - mid["radius"] < mid_ratio * t):
                        continue
                    ov_mid = _overlap(_intervals(mid["start_angle"], mid["sweep"]),
                                      _intervals(inner["start_angle"] + ov_lo,
                                                 ov_len))
                    if ov_mid >= 0.6 * ov_len:
                        blocked = True
                        break
                if blocked:
                    rejected["中间夹第三条面线"] += 1
                    continue
                walls.append(_wall_record(cx, cy, inner, outer, ov_lo, ov_len, t,
                                          base_t=base_t, hint=hint,
                                          step_deg=step_deg))
    walls = _drop_duplicate_faces(walls)
    diag = {"arcs": len(arcs), "centers": len(groups), "walls": len(walls),
            "rejected": dict(rejected), "thickness_base": round(base_t, 1)}
    return walls, diag


def _curtain_band(cx, cy, members, base_t, step_deg, is_curtain, hint):
    """三层等距同心弧 + 中间层是幕墙层 -> 一条弧形幕墙带（取外两条为面）。"""
    if is_curtain is None or len(members) < 3:
        return None
    gap = members[1]["radius"] - members[0]["radius"]
    if gap <= 0 or gap > SC.STRUCTURE["curtain_band_max_thickness"] * base_t:
        return None
    tol = SC.STRUCTURE["curtain_gap_tol"] * gap
    chain = [members[0], members[1]]
    for arc in members[2:]:
        g = arc["radius"] - chain[-1]["radius"]
        if abs(g - gap) > tol or g > SC.STRUCTURE["curtain_band_max_thickness"] * base_t:
            break
        chain.append(arc)
    if len(chain) < 3:
        return None
    inner, outer = chain[0], chain[-1]
    if not any(is_curtain(a["layer"]) for a in chain[1:-1]):
        return None
    ov = _clip(inner["start_angle"], inner["sweep"],
               _intervals(outer["start_angle"], outer["sweep"]))
    if ov is None:
        return None
    ov_lo, ov_len = ov
    if ov_len < SC.STRUCTURE["arc_min_sweep_deg"]:
        return None
    thickness = outer["radius"] - inner["radius"]
    wall = _wall_record(cx, cy, inner, outer, ov_lo, ov_len, thickness,
                        base_t=base_t, hint=hint, step_deg=step_deg)
    wall["role"] = "curtain"
    wall["band"] = True
    wall["band_lines"] = len(chain)
    wall["band_pitch"] = round(thickness / float(len(chain) - 1), 1)
    wall["recall_source"] = "curtain_band"
    return wall


def _wall_record(cx, cy, inner, outer, ov_lo, ov_len, thickness, *, base_t, hint,
                 step_deg) -> dict:
    r_in, r_out = inner["radius"], outer["radius"]
    r_mid = (r_in + r_out) / 2.0
    start_angle = inner["start_angle"] + ov_lo
    length = math.radians(ov_len) * r_mid
    same = abs(thickness - hint) <= 0.25 * hint
    return {
        "orient": "A",
        "arc": True,
        "curve": {"type": "arc",
                  "center": [round(cx, 1), round(cy, 1)],
                  "radius": round(r_mid, 1),
                  "inner_radius": round(r_in, 1),
                  "outer_radius": round(r_out, 1),
                  "start_angle": round(start_angle % 360.0, 2),
                  "end_angle": round((start_angle + ov_len) % 360.0, 2),
                  "thickness": round(thickness, 1)},
        "start": _point(cx, cy, r_mid, start_angle),
        "end": _point(cx, cy, r_mid, start_angle + ov_len),
        # 标量占位：排序/分组要一个数，弧墙用中线半径，语义由 curve 承载
        "center": round(r_mid, 1),
        "pos": round(r_mid, 1),
        "thickness": thickness,
        "length": length,
        "outline": _band_outline(cx, cy, r_in, r_out, start_angle, ov_len, step_deg),
        "srcs": [{"handle": inner["handle"], "type": "ARC", "role": "arc_face"},
                 {"handle": outer["handle"], "type": "ARC", "role": "arc_face"}],
        "caps_n": 0,
        "faces": 2,
        "layer_ok": True,
        "recall_source": "arc_pair",
        "evidence_state": "strong" if same else "weak",
        "geom_score": 0.95 if same else 0.7,
        "topo_score": 0.6,
        "arc_sweep_deg": round(ov_len, 1),
        "arc_thickness_ratio": round(thickness / base_t, 3),
    }


def _drop_duplicate_faces(walls):
    """三层弧（幕墙）会给出两道共用面线的 100 墙：只留一道。

    与直线墙的"共用面线 = 面层/装饰线"同一条口径：共用一条面线的两个候选里，
    更接近本图墙厚基准的那个留下。
    """
    # 内侧那对先入（半径升序）：三道面线时留下内层玻璃带，行为确定。
    walls = sorted(walls, key=lambda w: (w["curve"]["inner_radius"], -w["length"]))
    kept = []
    for w in walls:
        dup = None
        for k in kept:
            if (abs(k["curve"]["center"][0] - w["curve"]["center"][0]) <= 1.0
                    and abs(k["curve"]["center"][1] - w["curve"]["center"][1]) <= 1.0):
                faces_k = {k["curve"]["inner_radius"], k["curve"]["outer_radius"]}
                faces_w = {w["curve"]["inner_radius"], w["curve"]["outer_radius"]}
                if faces_k & faces_w:
                    dup = k
                    break
        if dup is None:
            kept.append(w)
    return kept
