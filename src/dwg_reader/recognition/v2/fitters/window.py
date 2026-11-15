"""窗专用拟合器：块窗、四线窗、线束窗、推拉窗、转角窗、幕墙分格窗。

与通用语法拟合的区别（也是当初要改的原因）：

* **尺度按实例自己量**：框间距取这扇窗自己平行线之间的距离，不再用"全图墙厚 ×
  带"当门槛。窄窗框、落地窗、竖向窗都用自己的量；
* **块窗先还原块内几何**：同一块定义的实例在 IR 里已经按插入变换展开，这里直接
  用展开后的图元算精确轴线与外框，模型只负责确认"这是窗"；
* **分格数来自几何**：几条平行线就是几分格，不由模型报数；
* 宿主墙只提供**定位**（洞口落在哪道墙上、开多宽），不提供"是不是窗"。

拟合器不判断"该不该信"——它给出几何与证据，裁决在
:mod:`..component_resolver`。
"""
from __future__ import annotations

import math

from . import _common as C

#: 无量纲比例（相对实例自身跨度/深度/噪声，不是毫米数）
LIMITS = {
    "along_ratio": 0.5,        # 沿长轴的线：长度 / 跨度 的下限
    "cap_ratio": 1.4,          # 端封线：长度 / 深度 的上限
    "parallel_tol_deg": 3.0,   # 与长轴"平行"的角度容差
    "offset_tol": 0.35,        # 合并同一侧线的相对容差（/最小框间距）
    "curtain_aspect": 5.0,     # 幕墙：跨度 / 深度 下限
    "curtain_offsets": 4,      # 幕墙：至少几条纵向线
    "sliding_ratio": 0.35,     # 推拉扇：单扇长度 / 跨度 的上限
    "thin_ratio": 0.02,        # 太薄（跨度 / 深度）视为退化
    "min_span_noise": 8.0,     # 跨度 / 噪声 的下限（小于它基本是图例）
    "end_search": 2.0,         # 找端部封口/延续线的搜索长度 / 跨度
    "continue_ratio": 2.0,     # 两侧共线延续总长 / 跨度：超过它说明是墙，不是窗
    "depth_min_ratio": 0.35,   # 窗框深度 / 墙厚 的下限（10~50 mm 的线不是窗框）
    "depth_max_ratio": 2.0,    # 窗框深度 / 墙厚 的上限
}


def _lines_in_frame(entities, axis_angle: float):
    """把实例里的线按"沿长轴 / 横跨 / 歪斜"分组（只看方向，不看语义）。"""
    along: list = []
    across: list = []
    skew: list = []
    for e in entities or ():
        for a, b in C.segments_of(e):
            length = C._len(a, b)
            if length <= 0:
                continue
            direction = C._angle(a, b)
            row = {"a": a, "b": b, "len": length, "angle": direction,
                   "mid": [(a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0], "uid": e.get("uid")}
            if C._angle_diff(direction, axis_angle) <= float(LIMITS["parallel_tol_deg"]):
                along.append(row)
            elif C._angle_diff(direction, axis_angle + 90.0) <= float(
                    LIMITS["parallel_tol_deg"]):
                across.append(row)
            else:
                skew.append(row)
    return along, across, skew


def _offsets(lines, axis_angle: float, *, tol: float):
    """每条线在"垂直于长轴"方向上的位置，按侧向位置合并成组（组=一根框线）。"""
    ang = math.radians(axis_angle)
    nx, ny = -math.sin(ang), math.cos(ang)
    rows = []
    for line in lines:
        side = (line["a"][0] * nx + line["a"][1] * ny
                + line["b"][0] * nx + line["b"][1] * ny) / 2.0
        rows.append(dict(line, side=side))
    rows.sort(key=lambda r: r["side"])
    groups: list = []
    for row in rows:
        if groups and abs(row["side"] - groups[-1]["side"]) <= tol:
            groups[-1]["lines"].append(row)
            groups[-1]["side"] = (groups[-1]["side"] * (len(groups[-1]["lines"]) - 1)
                                  + row["side"]) / len(groups[-1]["lines"])
        else:
            groups.append({"side": row["side"], "lines": [row]})
    return groups


def _ends(entities, axis_angle: float, span: float, box, neighbors):
    """端部证据：两端有没有封口线、轴向外侧有没有共线延续（墙判别用）。

    窗是**墙上的一个洞**：它的两条框线在端部被截断（封口线、或与墙返/墙垛相接），
    外侧不会有"同一对线一直延续下去"。双线墙则相反——外侧共线延续很长。
    这一条是窗和墙段唯一稳定的几何差别，所以必须查，而不是靠"看起来像窗"。
    """
    reach = float(LIMITS["end_search"]) * span
    ang = math.radians(axis_angle)
    ux, uy = math.cos(ang), math.sin(ang)
    nx, ny = -uy, ux
    ts = [pt[0] * ux + pt[1] * uy for pt in
          ([box[0], box[1]], [box[2], box[1]], [box[2], box[3]], [box[0], box[3]])]
    lo, hi = min(ts), max(ts)
    caps_left = caps_right = 0
    cont_left = cont_right = 0.0
    own = {e.get("uid") for e in entities or ()}
    for e in neighbors or ():
        if e.get("uid") in own:
            continue
        for a, b in C.segments_of(e):
            length = C._len(a, b)
            if length <= 0:
                continue
            direction = C._angle(a, b)
            ta = a[0] * ux + a[1] * uy
            tb = b[0] * ux + b[1] * uy
            t0, t1 = min(ta, tb), max(ta, tb)
            if C._angle_diff(direction, axis_angle) <= 3.0:
                # 共线延续：完全落在区间外、且距离不远
                if 0 < t0 - hi <= reach:
                    cont_right += t1 - t0
                elif 0 < lo - t1 <= reach:
                    cont_left += t1 - t0
            elif C._angle_diff(direction, axis_angle + 90.0) <= 3.0:
                # 端部封口：短横线贴在区间两端
                side = (a[0] * nx + a[1] * ny + b[0] * nx + b[1] * ny) / 2.0
                del side
                if abs(t0 - hi) <= reach * 0.25 or abs(t1 - hi) <= reach * 0.25:
                    caps_right += 1
                elif abs(t0 - lo) <= reach * 0.25 or abs(t1 - lo) <= reach * 0.25:
                    caps_left += 1
    return {"caps": [bool(caps_left), bool(caps_right)],
            "continued": [C._r(cont_left), C._r(cont_right)],
            "continued_total": C._r(cont_left + cont_right)}


def fit_instance(instance, *, entities, walls=(), grid=None, basis=None,
                 family=None, siblings=(), neighbors=()) -> dict | None:
    """一扇窗（或"疑似窗"）的几何与证据。认不出来就返回 ``None``。"""
    entities = [e for e in (entities or ()) if e.get("kind") != "text"]
    if not entities:
        return None
    basis = dict(basis or {})
    scales = C.local_scales(entities, basis)
    points = C.points_of(entities)
    rect = C.min_area_rect(points)
    if not rect or rect["w"] <= 0 or rect["h"] <= 0:
        return None
    span, depth = max(rect["w"], rect["h"]), min(rect["w"], rect["h"])
    if span <= scales["noise"] * float(LIMITS["min_span_noise"]):
        return None
    axis_angle = rect["angle"]
    if rect["h"] > rect["w"]:
        axis_angle = (rect["angle"] + 90.0) % 180.0

    along, across, skew = _lines_in_frame(entities, axis_angle)
    long_along = [row for row in along if row["len"] >= float(LIMITS["along_ratio"]) * span]
    caps = [row for row in across if row["len"] <= float(LIMITS["cap_ratio"]) * depth]
    gap_probe = max(scales["noise"] * 2.0, depth * 0.02)
    offsets = _offsets(long_along, axis_angle, tol=gap_probe)
    n_offsets = len(offsets)
    spacings = [abs(offsets[i + 1]["side"] - offsets[i]["side"])
                for i in range(len(offsets) - 1)]
    frame_gap = C._median(spacings) if spacings else 0.0

    # ---- 子类型：每一条都写清依据，后面不允许"看起来像"就说成幕墙
    subtype = "window_unclassified"
    how = []
    block_name = (instance or {}).get("block")
    if block_name:
        subtype = "block_window"
        how.append({"rule": "BLOCK_DEFINITION", "block": block_name})
    if n_offsets >= int(LIMITS["curtain_offsets"]) and \
            span >= float(LIMITS["curtain_aspect"]) * max(1e-9, depth):
        subtype = "curtain_wall"
        how.append({"rule": "LONG_SPAN_MANY_MULLIONS", "offsets": n_offsets,
                    "aspect": C._r(span / max(1e-9, depth), 2)})
    elif n_offsets >= 3:
        spreads = [s for s in spacings if s > 0]
        uniform = (max(spreads) / max(1e-9, min(spreads)) <= 1.35) if spreads else False
        subtype = "line_bundle_unclassified" if not uniform else "punched_window"
        how.append({"rule": "LINE_BUNDLE", "offsets": n_offsets,
                    "uniform_spacing": uniform})
    elif n_offsets == 2 and caps:
        subtype = "punched_window"
        how.append({"rule": "FRAME_PAIR_WITH_CAPS", "caps": len(caps)})
    # 推拉窗：两对短扇沿长轴错开（每扇只占跨度一部分）
    short_leaves = [row for row in long_along
                    if row["len"] <= float(LIMITS["sliding_ratio"]) * span * 2.0]
    if len(short_leaves) >= 2 and len(offsets) >= 2:
        mids = sorted(row["mid"][0] + row["mid"][1] for row in short_leaves)
        if mids and (mids[-1] - mids[0]) > float(LIMITS["sliding_ratio"]) * span:
            subtype = "sliding_window"
            how.append({"rule": "OFFSET_LEAVES", "leaves": len(short_leaves)})
    # 转角窗：同一实例里两组互相垂直的框线，各成一对
    if len(offsets) < 2 and skew:
        angles = sorted({round(C._angle(s["a"], s["b"]) / 5.0) * 5.0 for s in skew})
        if len(angles) >= 2 and any(C._angle_diff(a, b) >= 70.0
                                    for a in angles for b in angles):
            subtype = "corner_window"
            how.append({"rule": "PERPENDICULAR_LEGS", "angles": angles[:4]})

    box = C.bbox_of(entities)
    ends = _ends(entities, axis_angle, span, box, neighbors)
    host = C.wall_context(walls, box, tol=scales["noise"] * 4.0)
    opening = None
    if host.get("hit") and host.get("axis"):
        opening = _project_opening(box, host["axis"])
    geometry = {
        "bbox": box,
        "axis": C.axis_from(points),
        "outline": rect["outline"],
        "width_mm": C._r(span), "depth_mm": C._r(depth),
        "frame_gap_mm": C._r(frame_gap),
        "offsets_mm": [C._r(g["side"]) for g in offsets][:8],
        "panes": max(1, n_offsets - 1) if n_offsets else 1,
        "block": block_name,
        "host": ({"wall": host.get("wall"), "gap_mm": host.get("gap"),
                  "axis": host.get("axis"), "opening": opening,
                  "thickness_mm": host.get("thickness")}
                 if host.get("hit") else None),
        "host_ok": bool(host.get("hit")),
        "ends": ends,
    }
    negatives: list = []
    # 墙 vs 窗：外侧共线延续很长，说明这是墙上的一段，不是窗
    if ends["continued_total"] >= float(LIMITS["continue_ratio"]) * span:
        negatives.append({"code": "WALL_RUN_CONTINUES",
                          "detail": ends["continued"]})
    # 窗必须两端有截断（封口线、或 block 自带端边、或多线分格）
    if not any(ends["caps"]) and n_offsets < 3 and not block_name:
        negatives.append({"code": "NO_FRAME_END_CAPS"})
    depth_ratio = depth / max(1e-9, float(scales["wall"] or 200.0))
    if not (float(LIMITS["depth_min_ratio"]) <= depth_ratio
            <= float(LIMITS["depth_max_ratio"])):
        negatives.append({"code": "DEPTH_NOT_WALL_SCALE",
                          "detail": C._r(depth_ratio, 3),
                          "hint": "深度与墙厚不同量级：是薄线/图例，不是窗框"})
    if not host.get("hit"):
        negatives.append({"code": "NO_HOST_WALL", "hint": "没有宿主墙：可能是图例或家具"})
    line_total = len(along) + len(across) + len(skew)
    if line_total and len(skew) / float(line_total) > 0.5:
        negatives.append({"code": "MOSTLY_SKEW_LINES", "ratio":
                          C._r(len(skew) / float(line_total), 2)})
    if any(e.get("kind") == "arc" for e in entities):
        negatives.append({"code": "ARC_PRESENT", "hint": "窗里出现圆弧，形态可疑"})

    score = 0.52
    score += 0.16 if host.get("hit") else 0.0
    score += 0.12 if block_name else 0.0
    score += 0.10 if n_offsets >= 2 else 0.0
    score += 0.06 if caps else 0.0
    score -= 0.12 * len(negatives)
    return {"category": "window", "subtype": subtype, "how": how,
            "geometry": geometry, "negatives": negatives,
            "scales": {k: scales[k] for k in ("span", "depth", "frame_gap",
                                              "noise", "wall")},
            "score": C._r(max(0.05, min(0.97, score)), 3),
            "status": "candidate" if not negatives else "uncertain",
            "evidence": {"fitter": "window", "rule": [h["rule"] for h in how],
                         "host_ok": bool(host.get("hit")),
                         "grid": C.grid_context(grid, geometry["bbox"],
                                                tol=scales["noise"] * 8.0)}}


def _project_opening(box, wall_axis):
    """把窗的包围盒投到宿主墙轴线上，得到"开在墙上的那一截"。"""
    if not box or not wall_axis or len(wall_axis) < 2:
        return None
    a, b = wall_axis[0], wall_axis[1]
    ang = math.radians(C._angle(a, b))
    dx, dy = math.cos(ang), math.sin(ang)
    corners = [[box[0], box[1]], [box[2], box[1]], [box[2], box[3]], [box[0], box[3]]]
    ts = [(pt[0] - a[0]) * dx + (pt[1] - a[1]) * dy for pt in corners]
    lo, hi = min(ts), max(ts)
    return [[C._r(a[0] + dx * lo), C._r(a[1] + dy * lo)],
            [C._r(a[0] + dx * hi), C._r(a[1] + dy * hi)]]


__all__ = ["LIMITS", "fit_instance"]
