"""柱专用拟合器：矩形柱、旋转柱、异形柱、填充柱、散线柱。

柱最容易被家具顶掉，所以这里不只算轮廓，还把"结构位置"当证据一起算：

* **闭合轮廓**（多段线闭合 / 填充外环）→ 直接取轮廓；
* **近闭合轮廓**（四条散线围出来的柱）→ 用方向分组 + 极值反推四角，再逐边
  验证"每条边附近确有图元"，验证不过就不认；
* **结构位置**：是否压在轴线上、是否在轴线交点、是否与墙线相交、是否成柱网
  重复——这四条是柱区别于家具/装饰的主要依据；
* **家具特征**：孤立、尺寸远小于墙厚平方、周围挤着一堆大小不一的同类轮廓、
  不贴墙也不落轴线。这些记成 ``negatives``，扣分并标 ``uncertain``，不直接删。

轮廓顶点与旋转角度都保留：下游要按真实轮廓算量，不是只给个包围盒。
"""
from __future__ import annotations

import math

from . import _common as C

#: 无量纲比例（相对墙厚/噪声，不是毫米数）
LIMITS = {
    "area_min": 0.25,          # 柱面积 / 墙厚² 的下限
    "area_max": 400.0,         # 柱面积 / 墙厚² 的上限（再大就不是柱）
    "aspect_max": 12.0,        # 长宽比上限
    "edge_tol": 0.35,          # 轮廓边与图元的贴合容差 / 短边
    "repeat_size": 0.12,       # 同尺寸判定：边长相对差
    "cluster_radius": 3.0,     # 家具簇邻域 / 柱长边
    "min_vertices": 3,
    "rect_angle_tol": 5.0,     # 两条方向"垂直"的容差（度）
}


def _closed_outline(entities) -> tuple[list, bool]:
    """优先取闭合多段线/填充外环的顶点；返回 ``(顶点, 是否实心)``。"""
    best: list = []
    solid = False
    best_area = 0.0
    for e in entities or ():
        geom = e.get("geom") or {}
        pts = None
        if e.get("kind") == "poly" and geom.get("closed"):
            pts = geom.get("pts")
        elif e.get("kind") == "hatch":
            loops = geom.get("loops") or ()
            pts = loops[0] if loops else None
            solid = True
        if not pts or len(pts) < int(LIMITS["min_vertices"]):
            continue
        rect = C.min_area_rect(pts)
        area = rect["area"] if rect else 0.0
        if area > best_area:
            best_area = area
            best = [[C._r(p[0]), C._r(p[1])] for p in pts]
    return best, solid


def _reconstruct(entities, *, cell: float):
    """近闭合轮廓：两条主方向 + 四角反推，并逐边验证图元确实在那儿。"""
    edges = C.contour_edges(entities)
    groups = [g for g in edges["groups"] if len(g["segs"]) >= 1]
    if len(groups) < 2:
        return None
    groups.sort(key=lambda g: -sum(s["len"] for s in g["segs"]))
    main = groups[0]
    perp = None
    for group in groups[1:]:
        if 60.0 <= C._angle_diff(group["angle"], main["angle"]) <= 120.0:
            perp = group
            break
    if perp is None:
        return None
    if abs(C._angle_diff(main["angle"], perp["angle"]) - 90.0) > float(
            LIMITS["rect_angle_tol"]):
        return None
    ang = math.radians(main["angle"])
    ux, uy = math.cos(ang), math.sin(ang)
    nx, ny = -uy, ux
    u_vals: list = []
    n_vals: list = []
    for seg in main["segs"]:
        n_vals.extend([seg["a"][0] * nx + seg["a"][1] * ny,
                       seg["b"][0] * nx + seg["b"][1] * ny])
    for seg in perp["segs"]:
        u_vals.extend([seg["a"][0] * ux + seg["a"][1] * uy,
                       seg["b"][0] * ux + seg["b"][1] * uy])
    if not u_vals or not n_vals:
        return None
    u0, u1, n0, n1 = min(u_vals), max(u_vals), min(n_vals), max(n_vals)
    corners = [[C._r(ux * u + nx * n), C._r(uy * u + ny * n)]
               for u, n in ((u0, n0), (u1, n0), (u1, n1), (u0, n1))]
    # 逐边验证：每条边附近必须有图元（否则只是"碰巧有四条线"）
    short = min(u1 - u0, n1 - n0)
    tol = max(cell, float(LIMITS["edge_tol"]) * short)
    verified = 0
    for i in range(4):
        a, b = corners[i], corners[(i + 1) % 4]
        mid = [(a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0]
        if any(C._pt_seg_dist(mid, seg["a"], seg["b"]) <= tol
               for seg in edges["segments"]):
            verified += 1
    if verified < 4:
        return None
    return {"outline": corners, "verified_edges": verified,
            "w": C._r(max(u1 - u0, n1 - n0)), "h": C._r(min(u1 - u0, n1 - n0)),
            "angle": C._r(main["angle"], 2)}


def fit_instance(instance, *, entities, walls=(), grid=None, basis=None,
                 family=None, siblings=(), neighbors=()) -> dict | None:
    """一根柱（或"疑似柱的轮廓"）的几何与结构证据。"""
    entities = [e for e in (entities or ()) if e.get("kind") != "text"]
    if not entities:
        return None
    basis = dict(basis or {})
    scales = C.local_scales(entities, basis)
    thickness = float(scales["wall"] or 200.0)
    noise = float(scales["noise"] or 1.0)
    box = C.bbox_of(entities)
    how: list = []
    negatives: list = []

    outline, solid = _closed_outline(entities)
    rect = None
    if outline:
        rect = C.min_area_rect(outline)
        how.append({"rule": "CLOSED_CONTOUR", "vertices": len(outline),
                    "solid": solid})
    else:
        rebuilt = _reconstruct(entities, cell=noise * 3.0)
        if rebuilt:
            outline = rebuilt["outline"]
            rect = C.min_area_rect(outline)
            how.append({"rule": "RECONSTRUCTED_FROM_LINES",
                        "verified_edges": rebuilt["verified_edges"]})
    if not outline or not rect:
        return None

    w, h = max(rect["w"], rect["h"]), min(rect["w"], rect["h"])
    area = rect["area"]
    if area <= 0 or w <= 0 or h <= 0:
        return None
    if area < float(LIMITS["area_min"]) * thickness * thickness:
        negatives.append({"code": "AREA_TOO_SMALL",
                          "detail": C._r(area / (thickness * thickness), 3)})
    if area > float(LIMITS["area_max"]) * thickness * thickness:
        negatives.append({"code": "AREA_TOO_LARGE",
                          "detail": C._r(area / (thickness * thickness), 3)})
    if w / max(1e-9, h) > float(LIMITS["aspect_max"]):
        negatives.append({"code": "ASPECT_TOO_LONG",
                          "detail": C._r(w / max(1e-9, h), 2)})

    wall = C.wall_context(walls, box, tol=noise * 4.0)
    grid_ctx = C.grid_context(grid, box, tol=noise * 8.0)
    repeats = _repeat_count(rect, siblings, box)
    cluster = _cluster_density(box, siblings, span=max(w, h))

    structural = bool(wall.get("hit") or grid_ctx.get("axes")
                      or grid_ctx.get("intersection") or repeats >= 2)
    if not structural:
        negatives.append({"code": "NO_STRUCTURAL_CONTEXT",
                          "hint": "不贴墙、不落轴线、不成柱网：更像家具或装饰轮廓"})
    if cluster >= 3 and repeats <= 1:
        negatives.append({"code": "FURNITURE_CLUSTER",
                          "detail": cluster, "hint": "周围挤着一堆不同尺寸的轮廓"})

    angle = rect["angle"]
    rotated = min(abs(angle % 90.0), 90.0 - abs(angle % 90.0)) > float(
        LIMITS["rect_angle_tol"])
    subtype = "filled_column" if solid else "rect_column"
    if rotated:
        subtype = "rotated_column"
        how.append({"rule": "ROTATED", "angle_deg": angle})
    if len(outline) > 4 and not solid:
        subtype = "irregular_column"
    elif not solid and not rotated and not any(h["rule"] == "CLOSED_CONTOUR"
                                               for h in how):
        subtype = "composite_column"

    geometry = {
        "bbox": box,
        "outline": outline,
        "w_mm": C._r(w), "h_mm": C._r(h),
        "area_mm2": C._r(area),
        "angle_deg": C._r(angle, 2),
        "rotated": rotated,
        "solid": solid,
        "centroid": rect["center"],
        "rect_outline": rect["outline"],
        "host": ({"wall": wall.get("wall"), "gap_mm": C._r(wall.get("gap")),
                  "axis": wall.get("axis")} if wall.get("hit") else None),
    }
    score = 0.52
    score += 0.14 if wall.get("hit") else 0.0
    score += 0.14 if grid_ctx.get("intersection") else 0.0
    score += 0.08 if grid_ctx.get("axes") else 0.0
    score += 0.12 if repeats >= 2 else 0.0
    score += 0.05 if solid else 0.0
    score -= 0.16 * len(negatives)
    return {"category": "column", "subtype": subtype, "how": how,
            "geometry": geometry, "negatives": negatives,
            "scales": {k: scales[k] for k in ("span", "depth", "noise", "wall")},
            "score": C._r(max(0.05, min(0.97, score)), 3),
            "status": "candidate" if not negatives else "uncertain",
            "evidence": {"fitter": "column", "rule": [h["rule"] for h in how],
                         "structural": structural,
                         "on_grid": bool(grid_ctx.get("axes")
                                         or grid_ctx.get("intersection")),
                         "grid_axes": [a.get("label") for a in grid_ctx.get("axes") or ()],
                         "touches_wall": bool(wall.get("hit")),
                         "repeats": repeats, "cluster": cluster,
                         "negatives": [n["code"] for n in negatives]}}


def _repeat_count(rect, siblings, box) -> int:
    """同尺寸重复次数：柱网最硬的证据（一份图纸里柱是按开间重复的）。"""
    if not siblings:
        return 0
    count = 0
    for sib in siblings:
        if not isinstance(sib, dict):
            continue
        other = sib.get("geometry") or sib
        w = float(other.get("w_mm") or 0.0)
        h = float(other.get("h_mm") or 0.0)
        if not w or not h:
            continue
        if abs(w - rect["w"]) / max(1e-9, rect["w"]) <= float(LIMITS["repeat_size"]) and \
                abs(h - rect["h"]) / max(1e-9, rect["h"]) <= float(LIMITS["repeat_size"]):
            count += 1
    return count


def _cluster_density(box, siblings, *, span: float) -> int:
    """邻域里"尺寸各异"的轮廓数：家具符号常常成堆出现且大小不一。"""
    if not span:
        return 0
    radius = float(LIMITS["cluster_radius"]) * span
    count = 0
    for sib in siblings or ():
        if not isinstance(sib, dict):
            continue
        other = sib.get("bbox")
        if not other or other == box:
            continue
        if C._box_gap(box, other) > radius:
            continue
        size = max(float(other[2]) - float(other[0]), float(other[3]) - float(other[1]))
        if abs(size - span) / max(1e-9, span) > float(LIMITS["repeat_size"]) * 2.0:
            count += 1
    return count


__all__ = ["LIMITS", "fit_instance"]
