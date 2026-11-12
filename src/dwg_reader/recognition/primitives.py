"""几何证据原语：只描述"能观测到什么"，允许字段缺失，全部可追溯 handle。

原语种类（方向无关；点积/叉积定义见 :mod:`geometry`）：

============ ==========================================================
``lines``    线支撑 LinearSupport：origin/dir/nrm/length/angle/orient
``closed``   闭合轮廓：真实折线 outline（bbox 只是它的一个特征）
``arcs``     圆弧：全角度提取，不再按角度预筛
``anchors``  语义锚点：块参照 / 文字 / 尺寸
``ticks``    短笔画：轴号刻度、符号细节（长度 5~300mm）
============ ==========================================================

不变量：

1. 原语**不判断构件身份**，也不做"像不像门/柱/窗"的过滤；唯一的丢弃是退化图元
   （零长线、0 半径弧）。
2. 字段可以缺失（``thickness`` 未知、``height`` 缺失等），用 ``None`` 或缺键表达。
3. 每条记录都带 ``handle``；块展开来的记录额外带 ``via``，可回溯到父块参照。
"""
from __future__ import annotations

import math

from . import geometry as GEO
from .snapshot import geometry_records

#: 线支撑的最小长度（低于此值视为符号细节，只进 ``ticks``）
MIN_LINE = 20.0
#: 短笔画区间
TICK_MIN, TICK_MAX = 4.0, 300.0


def _arc_ends(center, radius, a1, a2):
    c1, s1 = math.cos(a1), math.sin(a1)
    c2, s2 = math.cos(a2), math.sin(a2)
    return ([center[0] + radius * c1, center[1] + radius * s1],
            [center[0] + radius * c2, center[1] + radius * s2])


def _segments_of(rec):
    """把 LINE / LWPOLYLINE / POLYLINE 拆成线段（返回 [(p, q), ...]）。"""
    kind = rec.get("type")
    if kind == "LINE":
        s, e = rec.get("start"), rec.get("end")
        return [((s[0], s[1]), (e[0], e[1]))] if s and e else []
    if kind in ("LWPOLYLINE", "POLYLINE"):
        pts = [(float(p[0]), float(p[1])) for p in rec.get("points") or ()]
        if len(pts) < 2:
            return []
        pairs = list(zip(pts, pts[1:]))
        if rec.get("closed"):
            pairs.append((pts[-1], pts[0]))
        return pairs
    return []


def extract(snapshot, *, include_derived: bool = True,
            min_line: float = MIN_LINE) -> dict:
    """把快照拆成方向无关的几何原语。

    ``include_derived``：是否把块展开几何（``snapshot["derived"]``）也算进来，
    默认算——这正是"墙/柱画在块里也能识别"的入口。
    """
    lines: list[dict] = []
    closed: list[dict] = []
    arcs: list[dict] = []
    anchors: list[dict] = []
    ticks: list[dict] = []
    hatches: list[dict] = []

    records = geometry_records(snapshot) if include_derived else snapshot.get("entities", ())

    for rec in records:
        kind = rec.get("type")
        handle = rec.get("handle")
        layer = rec.get("layer")
        extra = {}
        if rec.get("via"):
            extra["via"] = rec["via"]

        if kind in ("LINE", "LWPOLYLINE", "POLYLINE"):
            segs = _segments_of(rec)
            for (p, q) in segs:
                sup = GEO.as_support(p, q, layer=layer, handle=handle,
                                     kind=kind, extra=dict(extra, source="polyline"
                                                           if kind != "LINE" else "line"))
                if sup is None:
                    continue
                if sup["length"] >= min_line:
                    lines.append(sup)
                if TICK_MIN <= sup["length"] <= TICK_MAX:
                    ticks.append(sup)
            if kind != "LINE" and rec.get("closed"):
                pts = [[float(p[0]), float(p[1])] for p in rec.get("points") or ()]
                if len(pts) >= 3:
                    shape = GEO.shape_of(pts)
                    closed.append({
                        "kind": "contour", "outline": pts,
                        "bbox": shape["rect"]["bbox"],
                        "area": shape["area"], "perimeter": shape["perimeter"],
                        "shape": shape, "n": len(pts),
                        "has_bulge": bool(rec.get("has_bulge")),
                        "layer": layer, "handle": handle,
                        **({"via": rec["via"]} if rec.get("via") else {}),
                    })

        elif kind == "ARC":
            radius = float(rec.get("radius") or 0.0)
            center = rec.get("center")
            if radius <= 0 or not center:
                continue
            a1 = math.radians(float(rec.get("start_angle") or 0.0))
            a2 = math.radians(float(rec.get("end_angle") or 0.0))
            sweep = (float(rec.get("end_angle") or 0.0)
                     - float(rec.get("start_angle") or 0.0)) % 360.0
            p1, p2 = _arc_ends(center, radius, a1, a2)
            arcs.append({"kind": "arc", "center": [center[0], center[1]],
                         "radius": radius,
                         "start_angle": float(rec.get("start_angle") or 0.0),
                         "end_angle": float(rec.get("end_angle") or 0.0),
                         "sweep": sweep, "full": sweep >= 359.9,
                         "p1": p1, "p2": p2,
                         "chord": math.dist(p1, p2),
                         "layer": layer, "handle": handle,
                         **({"via": rec["via"]} if rec.get("via") else {})})

        elif kind == "HATCH":
            loops = [[[float(p[0]), float(p[1])] for p in loop]
                     for loop in (rec.get("loops") or ()) if len(loop) >= 3]
            if loops:
                hatches.append({"kind": "hatch", "loops": loops,
                                "solid": bool(rec.get("solid")),
                                "layer": layer, "handle": handle,
                                **({"via": rec["via"]} if rec.get("via") else {})})

        elif kind in ("INSERT", "TEXT", "MTEXT"):
            anchors.append({"type": kind, "layer": layer, "handle": handle,
                            "name": rec.get("name"), "text": rec.get("text"),
                            "insert": rec.get("insert"),
                            "height": rec.get("height"),
                            "rotation": rec.get("rotation"),
                            "attribs": rec.get("attribs")})

    return {"lines": lines, "closed": closed, "arcs": arcs,
            "anchors": anchors, "ticks": ticks, "hatches": hatches}


# --------------------------------------------------------------------------- 统计视角
def angle_histogram(supports, buckets=12) -> dict:
    """线支撑的方向直方图（度，0~180，按 bucket 归并）——画法诊断用。"""
    step = 180.0 / buckets
    out: dict[str, int] = {}
    for s in supports:
        key = int((s["angle"] % 180.0) // step) * step
        out[f"{key:.0f}"] = out.get(f"{key:.0f}", 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def length_stats(supports) -> dict:
    lens = sorted(s["length"] for s in supports)
    if not lens:
        return {"count": 0, "min": None, "median": None, "max": None}
    return {"count": len(lens), "min": round(lens[0], 1),
            "median": round(lens[len(lens) // 2], 1), "max": round(lens[-1], 1)}


def summary(prims: dict) -> dict:
    """原语层摘要（进 meta / 诊断报告）。"""
    return {"lines": len(prims["lines"]), "closed": len(prims["closed"]),
            "arcs": len(prims["arcs"]), "anchors": len(prims["anchors"]),
            "ticks": len(prims["ticks"]),
            "hatches": len(prims.get("hatches") or ()),
            "angles": angle_histogram(prims["lines"]),
            "line_length": length_stats(prims["lines"])}
