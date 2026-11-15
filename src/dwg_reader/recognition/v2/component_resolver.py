"""门窗柱专用求解：按类别打分、按类别冲突裁决。

为什么不直接复用通用求解器（:mod:`global_resolver`）
------------------------------------------------------
通用求解器对所有类别用同一套权重和同一套来源可信度，默认"规则候选比 AI 候选可
信"。放到门窗柱上就会出两种错：

* 一扇散线画的窗，规则引擎根本没认出来，AI + 传播候选明明几何更完整，却因为
  来源是 ``ai`` 被压掉；
* 一根落在柱网上的柱和一张椅子轮廓，几何都是"闭合轮廓"，用同一套权重分不开。

所以这里做三件事，且只做这三件事：

1. **分类别打分**：门窗柱各自的权重、各自的来源可信度（规则并不总是最可信）；
2. **分类别冲突**：门窗同位置互斥、同实例重复、窗必须开在墙上、柱要有结构位置；
3. **保留不确定**：证据不足的候选标 ``uncertain`` 留在结果里，不静默删除——
   删掉的东西永远学不到东西。

权重仍然是**首版先验**，必须用独立标注按类别校准（代码只提供口径）。
"""
from __future__ import annotations

import math

SCHEMA = "component-resolution/1.0"

#: 逻辑预算
BUDGET = {"exclusive_iou": 0.45, "duplicate_iou": 0.80, "uid_overlap": 0.60,
          "rounds": 4, "states": 3, "max_candidates": 4000}

#: 通用权重（各类别在其上覆盖）
WEIGHTS = {"evidence": 0.30, "geometry": 0.26, "source": 0.20,
           "structure": 0.14, "family": 0.10}

#: 分类别权重：柱更看结构位置，门窗更看宿主与几何完整度
CATEGORY_WEIGHTS = {
    "door": {"evidence": 0.28, "geometry": 0.24, "source": 0.18,
             "structure": 0.20, "family": 0.10},
    "window": {"evidence": 0.28, "geometry": 0.28, "source": 0.16,
               "structure": 0.18, "family": 0.10},
    "column": {"evidence": 0.24, "geometry": 0.24, "source": 0.16,
               "structure": 0.26, "family": 0.10},
    "wall": {"evidence": 0.34, "geometry": 0.30, "source": 0.26,
             "structure": 0.00, "family": 0.10},
    "grid_axis": {"evidence": 0.40, "geometry": 0.30, "source": 0.30,
                  "structure": 0.00, "family": 0.00},
}

#: 分类别来源可信度：**按类别**校准的初值，不是全局常数
CATEGORY_TRUST = {
    "door": {"rule": 0.94, "propagation": 0.82, "ai": 0.72, "pattern": 0.60},
    "window": {"rule": 0.92, "propagation": 0.86, "ai": 0.76, "pattern": 0.62},
    "column": {"rule": 0.90, "propagation": 0.84, "ai": 0.70, "pattern": 0.60},
    "wall": {"rule": 0.96, "propagation": 0.80, "ai": 0.65, "pattern": 0.62},
    "grid_axis": {"rule": 0.95, "propagation": 0.80, "ai": 0.65, "pattern": 0.60},
}

#: 结构性否证：出现这些反面证据的候选**不参与选择**（但保留在 rejected 里可查）。
#: 只放"几何上已经否掉"的代码，不放"看起来不太像"的软证据。
REJECT_NEGATIVES = frozenset({"WALL_RUN_CONTINUES", "DEPTH_NOT_WALL_SCALE"})

#: 传播来的候选（"模型说这个族是门"）额外要求可验证证据：
#: 命中块定义、有门弧/门垛、或贴在已确认的墙上。只靠"长得像"不算。
#: 实测：一个 51 实例的块族被模型判成 door/high，程序按相似度灌进 61 个假门。
PROPAGATION_WEAK = frozenset({"NO_ARC_NO_JAMB", "NO_FRAME_END_CAPS",
                              "DEPTH_NOT_WALL_SCALE", "WALL_RUN_CONTINUES",
                              "AREA_TOO_SMALL", "AREA_TOO_LARGE",
                              "ASPECT_TOO_LONG"})


def column_geometry_ok(hyp: dict) -> bool:
    """Semantic confirmation cannot override an unresolved geometric fit."""
    ev = hyp.get("evidence") or {}
    status = ev.get("geometry_status", hyp.get("geometry_status"))
    if status is None:
        status = hyp.get("status")
    if str(status or "").lower() in ("pending", "rejected", "invalid", "failed"):
        return False
    geom = hyp.get("geometry") or {}
    codes = {str(n.get("code") if isinstance(n, dict) else n)
             for n in ev.get("negatives") or ()}
    if codes & {"AREA_TOO_SMALL", "AREA_TOO_LARGE", "ASPECT_TOO_LONG",
                "INVALID_GEOMETRY", "OPEN_CONTOUR"}:
        return False
    try:
        return (_valid(hyp.get("bbox")) and _poly_area(_polygon(geom.get("outline"))) > 0
                and all(math.isfinite(float(geom.get(k) or 0))
                        and float(geom.get(k) or 0) > 0 for k in ("w_mm", "h_mm")))
    except (TypeError, ValueError, OverflowError):
        return False


def column_category_confirmed(hyp: dict) -> bool:
    ev = hyp.get("evidence") or {}
    return (ev.get("category_confirmed") is True and ev.get("category_basis") in
            {"model_family", "catalog_block", "model_recheck", "model_arbitration"})


def _column_semantics_ok(hyp: dict) -> bool:
    return column_category_confirmed(hyp) and column_geometry_ok(hyp)


def _propagation_ok(hyp: dict) -> tuple[bool, str]:
    """传播候选能不能成立：必须有**可检查的几何/块证据**，不是只有相似度。"""
    geom = hyp.get("geometry") or {}
    ev = hyp.get("evidence") or {}
    codes = {str(n.get("code") if isinstance(n, dict) else n)
             for n in (ev.get("negatives") or ())}
    if hyp.get("category") == "column":
        return (True, "COLUMN_GEOMETRY") if column_geometry_ok(hyp) else (False, "COLUMN_GEOMETRY_UNVERIFIED")
    if codes & PROPAGATION_WEAK:
        return False, "WEAK_NEGATIVE"
    if ev.get("host_ok") or geom.get("host_ok"):
        return True, "HOSTED"
    if geom.get("jambs") or geom.get("hinge") or geom.get("leaf"):
        return True, "HAS_JAMB_OR_ARC"
    if ev.get("block_defined") or ev.get("catalog_block"):
        return True, "BLOCK_DEFINED"
    return False, "NO_VERIFIABLE_EVIDENCE"

#: 结构性否证：出现这些反面证据的候选**不参与选择**（但保留在 rejected 里可查）。
NEGATIVE_PENALTY = {"NO_HOST_WALL": 0.10, "ARC_TOO_SMALL": 0.16,
                    "ARC_TOO_LARGE": 0.10, "ARC_SWEEP_TOO_SMALL": 0.14,
                    "LEAF_NOT_FOUND": 0.08, "OPENING_TOO_NARROW": 0.10,
                    "AREA_TOO_SMALL": 0.14, "AREA_TOO_LARGE": 0.10,
                    "ASPECT_TOO_LONG": 0.10, "NO_STRUCTURAL_CONTEXT": 0.18,
                    "FURNITURE_CLUSTER": 0.14, "MOSTLY_SKEW_LINES": 0.08,
                    "ARC_PRESENT": 0.06, "NO_ARC_NO_JAMB": 0.12}

#: 分阶段：上下文（墙/轴网，不参与竞赛）→ 柱 → 洞口
STAGES = ("context", "columns", "openings")

STAGE_CATEGORIES = {"context": ("wall", "grid_axis"),
                    "columns": ("column",),
                    "openings": ("door", "window")}

#: 哪些类别互相排斥（同位置不可能同时成立）
EXCLUSIVE = {frozenset(("door", "window")),
             frozenset(("column", "window")),
             frozenset(("column", "door"))}

#: 谁支撑谁（宿主关系，只加分，不构成冲突）
SUPPORTS = {("wall", "door"), ("wall", "window"), ("wall", "column")}

#: 每类"要有哪些几何量"才算完整（缺项扣分，不当场判死）
REQUIRED = {"door": ("axis", "width_mm", "outline"),
            "window": ("axis", "width_mm", "frame_gap_mm"),
            "column": ("outline", "w_mm", "h_mm"),
            "wall": ("axis", "thickness_mm"),
            "grid_axis": ("axis",)}


def _r(v, nd: int = 3) -> float:
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return 0.0


def _valid(box) -> bool:
    """bbox 是否可用于几何比较（缺值 / 零面积一律不可用）。"""
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return False
    try:
        x0, y0, x1, y1 = (float(v) for v in box)
    except (TypeError, ValueError):
        return False
    return (x1 - x0) > 0.0 and (y1 - y0) > 0.0


def footprint(hyp: dict) -> list | None:
    """候选的**真实占用轮廓**（凸化后的多边形）；拿不到就返回 None。

    只用外包围盒判断冲突会把斜放的构件算成"重叠"：两个平行的斜洞口，外框
    大面积相交、真实矩形却互不相交。反过来，门扇的开启弧也会把外框撑大。
    """
    geom = (hyp or {}).get("geometry") or {}
    for key in ("outline", "rect_outline", "outer", "leaf"):
        seq = geom.get(key)
        pts = _polygon(seq)
        if pts:
            return pts
    return None


def _polygon(seq) -> list | None:
    if not isinstance(seq, (list, tuple)) or len(seq) < 3:
        return None
    pts = []
    for pt in seq:
        if isinstance(pt, (list, tuple)) and len(pt) >= 2:
            try:
                pts.append((float(pt[0]), float(pt[1])))
            except (TypeError, ValueError):
                return None
    if len(pts) < 3:
        return None
    return _convex_hull(pts)


def _convex_hull(pts) -> list:
    """单调链凸包（柱/洞口轮廓都是凸的，凸化只是去掉共线冗余）。"""
    pts = sorted(set(pts))
    if len(pts) < 3:
        return list(pts)

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _poly_area(pts) -> float:
    if not pts or len(pts) < 3:
        return 0.0
    total = 0.0
    for i, a in enumerate(pts):
        b = pts[(i + 1) % len(pts)]
        total += a[0] * b[1] - b[0] * a[1]
    return abs(total) / 2.0


def _clip_poly(subject, clip) -> list:
    """Sutherland–Hodgman：用凸多边形 clip 裁剪 subject，返回交叠多边形。"""
    def inside(p, a, b):
        return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]) >= -1e-12

    def intersect(p, q, a, b):
        x1, y1 = p
        x2, y2 = q
        x3, y3 = a
        x4, y4 = b
        den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
        if abs(den) <= 1e-12:
            return q
        t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / den
        return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))

    output = list(subject)
    for i, a in enumerate(clip):
        b = clip[(i + 1) % len(clip)]
        if not output:
            return []
        inp = output
        output = []
        prev = inp[-1]
        for cur in inp:
            if inside(cur, a, b):
                if not inside(prev, a, b):
                    output.append(intersect(prev, cur, a, b))
                output.append(cur)
            elif inside(prev, a, b):
                output.append(intersect(prev, cur, a, b))
            prev = cur
    return output


def footprint_overlap(a, b) -> float | None:
    """真实轮廓交叠率（交叠面积 / 较小面积）；任一方没有轮廓则返回 None。"""
    fa, fb = footprint(a), footprint(b)
    if not fa or not fb:
        return None
    inter = _poly_area(_clip_poly(fa, fb))
    small = min(_poly_area(fa), _poly_area(fb))
    if small <= 0:
        return None
    return max(0.0, min(1.0, inter / small))


def _iou(a, b) -> float:
    if not a or not b:
        return 0.0
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    area_a = max(1e-9, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1e-9, (b[2] - b[0]) * (b[3] - b[1]))
    return inter / (area_a + area_b - inter)


def _footprint(hyp: dict) -> list:
    """构件的**真实占地多边形**：有轴线+厚度就用旋转矩形，否则退回外框。

    外框（AABB）只够"找附近的东西"；判断"两个构件是不是占了同一块地方"必须用
    真实占地，否则并排的两道斜墙会因为外框相交被判成互斥。
    """
    geom = hyp.get("geometry") or {}
    axis = geom.get("axis") or geom.get("opening") or []
    box = hyp.get("bbox")
    thick = float(geom.get("thickness_mm") or 0.0)
    if not thick:
        box_t = float((hyp.get("evidence") or {}).get("thickness") or 0.0)
        thick = box_t
    if (isinstance(axis, (list, tuple)) and len(axis) >= 2 and thick > 0):
        try:
            ax, ay = float(axis[0][0]), float(axis[0][1])
            bx, by = float(axis[1][0]), float(axis[1][1])
        except (TypeError, ValueError, IndexError):
            ax = ay = bx = by = None
        if ax is not None:
            dx, dy = bx - ax, by - ay
            length = math.hypot(dx, dy)
            if length > 0:
                nx, ny = -dy / length * thick / 2.0, dx / length * thick / 2.0
                return [[ax + nx, ay + ny], [bx + nx, by + ny],
                        [bx - nx, by - ny], [ax - nx, ay - ny]]
    if _valid(box):
        x0, y0, x1, y1 = (float(v) for v in box)
        return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
    return []


def _poly_area(poly) -> float:
    if not poly or len(poly) < 3:
        return 0.0
    total = 0.0
    for i, (x0, y0) in enumerate(poly):
        x1, y1 = poly[(i + 1) % len(poly)]
        total += x0 * y1 - x1 * y0
    return abs(total) / 2.0


def _clip(subject, clip_poly) -> list:
    """Sutherland–Hodgman：凸多边形裁剪（OBB 与矩形都是凸的）。"""
    def inside(pt, a, b):
        return (b[0] - a[0]) * (pt[1] - a[1]) - (b[1] - a[1]) * (pt[0] - a[0]) >= -1e-9

    def cross(s, e, a, b):
        x1, y1, x2, y2 = s[0], s[1], e[0], e[1]
        dx1, dy1 = x2 - x1, y2 - y1
        dx2, dy2 = b[0] - a[0], b[1] - a[1]
        den = dx1 * dy2 - dy1 * dx2
        if abs(den) <= 1e-12:
            return e
        t = ((a[0] - x1) * dy2 - (a[1] - y1) * dx2) / den
        return [x1 + t * dx1, y1 + t * dy1]

    out = list(subject or [])
    for i, a in enumerate(clip_poly or []):
        b = clip_poly[(i + 1) % len(clip_poly)]
        if not out:
            return []
        cur = out
        out = []
        for j, pt in enumerate(cur):
            prev = cur[j - 1]
            if inside(pt, a, b):
                if not inside(prev, a, b):
                    out.append(cross(prev, pt, a, b))
                out.append(pt)
            elif inside(prev, a, b):
                out.append(cross(prev, pt, a, b))
    return out


def _footprint_overlap(a: dict, b: dict) -> float:
    """真实占地交叠 / 较小面积。1.0 表示小的那个被完全盖住。"""
    pa, pb = _footprint(a), _footprint(b)
    if len(pa) < 3 or len(pb) < 3:
        return 0.0
    inter = _poly_area(_clip(pa, pb))
    small = min(_poly_area(pa), _poly_area(pb))
    return (inter / small) if small > 0 else 0.0


def _gap(a, b) -> float:
    if not a or not b:
        return float("inf")
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def _uid_overlap(a, b) -> float:
    sa, sb = set(a.get("uids") or ()), set(b.get("uids") or ())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(min(len(sa), len(sb)))


# --------------------------------------------------------------------- 打分
def _strength(hyp: dict) -> float:
    ev = hyp.get("evidence") or {}
    value = 0.6
    state = str(ev.get("evidence_state") or "")
    if state:
        value = {"strong": 1.0, "weak": 0.55, "pending": 0.4}.get(state, 0.6)
    parts_total = float(ev.get("parts_total") or 0.0)
    parts_bound = float(ev.get("parts_bound") or 0.0)
    if parts_total:
        value = 0.5 * value + 0.5 * (parts_bound / parts_total)
    if ev.get("similarity") is not None:
        value = max(value, 0.4 + 0.5 * float(ev.get("similarity") or 0.0))
    # 模型的"我觉得像"**不进分数**：以前这里按 high/medium/low 直接抬分，结果
    # 开不开模型会让同一张图的柱在 67/64 之间漂（模型扰动改变了选择顺序）。
    # 模型的作用是"否决/改判/裁决"，不是给几何打分。
    model = ev.get("model") or {}
    if model.get("confidence") and model.get("disagrees_with_category"):
        value -= 0.05
    negs = [str(n.get("code") if isinstance(n, dict) else n)
            for n in (ev.get("negatives") or ())]
    if negs:
        value -= sum(float(NEGATIVE_PENALTY.get(code, 0.06)) for code in negs[:4])
    return max(0.0, min(1.0, value))


def _completeness(hyp: dict) -> float:
    geom = hyp.get("geometry") or {}
    required = REQUIRED.get(str(hyp.get("category")), ())
    if not required:
        return 0.6
    have = sum(1 for key in required if geom.get(key) not in (None, [], {}, 0, 0.0))
    base = have / float(len(required))
    # 额外的几何量（轮廓、分格、宿主、旋转角度…）再加一点分，但不超过 1
    extra_keys = ("outline", "rect_outline", "panes", "host", "leaf", "hinge",
                  "opening", "area_mm2", "angle_deg", "offsets_mm")
    extra = sum(1 for key in extra_keys if geom.get(key) not in (None, [], {}, 0, 0.0))
    return max(0.0, min(1.0, base + 0.05 * min(4, extra)))


def _structure(hyp: dict) -> float:
    ev = hyp.get("evidence") or {}
    geom = hyp.get("geometry") or {}
    cat = str(hyp.get("category"))
    if cat in ("wall", "grid_axis"):
        return 0.5
    value = 0.0
    if geom.get("host_ok") or ev.get("host_ok"):
        value += 0.55
    if ev.get("touches_wall"):
        value += 0.20
    if ev.get("on_grid"):
        value += 0.25
    if float(ev.get("repeats") or 0) >= 2:
        value += 0.20
    if ev.get("structural"):
        value += 0.10
    return max(0.0, min(1.0, value))


def score(hyp: dict, *, weights=None) -> float:
    """分类别的单点分 S(h)（0~1，只用于排序与选择）。"""
    cat = str(hyp.get("category"))
    w = dict(WEIGHTS)
    w.update(CATEGORY_WEIGHTS.get(cat) or {})
    w.update(weights or {})
    source = str(hyp.get("source") or "ai")
    trust = float((CATEGORY_TRUST.get(cat) or {}).get(source, 0.55))
    # 模型的"族判断/传播"同样**不进分数**：它一动，候选排序就变，同一张图的
    # 柱会在 67/64/63 之间漂。族信息只写进证据供人核对，不参与几何选择。
    total = (w["evidence"] * _strength(hyp) + w["geometry"] * _completeness(hyp)
             + w["source"] * trust + w["structure"] * _structure(hyp))
    return _r(max(0.0, min(1.0, total)))


# --------------------------------------------------------------------- 两两关系
def relation(a: dict, b: dict, *, thickness: float) -> tuple[float, str]:
    """两个候选的兼容性：负=冲突（不能同选），正=互相支持。"""
    cat_a, cat_b = str(a.get("category")), str(b.get("category"))
    if not (_valid(a.get("bbox")) and _valid(b.get("bbox"))):
        # 几何不完整时不许"猜关系"：宁可无关系，也不要凭空造一条冲突边
        return 0.0, "INVALID_BOX"
    iou = _iou(a.get("bbox"), b.get("bbox"))
    overlap = _uid_overlap(a, b)
    # 有真实轮廓就按轮廓判（外框只做粗筛）：斜放的洞口不会因为外框相交被误判互斥
    real = footprint_overlap(a, b)
    ratio = real if real is not None else iou
    if cat_a == cat_b:
        if ratio >= float(BUDGET["duplicate_iou"]) or overlap >= float(BUDGET["uid_overlap"]):
            return -1.0, "DUPLICATE"
        if cat_a == "column" and ratio > 0.0:
            return -0.6, "COLUMN_OVERLAP"
        return 0.0, ""
    pair = frozenset((cat_a, cat_b))
    if pair in EXCLUSIVE:
        if real is None and iou <= 0.0 and overlap <= 0.0:
            return 0.0, ""
        if ratio >= float(BUDGET["exclusive_iou"]) or overlap >= float(BUDGET["uid_overlap"]):
            return -1.0, "MUTUALLY_EXCLUSIVE"
        if _gap(a.get("bbox"), b.get("bbox")) <= 0.0:
            # 贴在一起不等于互斥（柱边开门、门窗相邻都正常）。只留一点点痕迹，
            # 低于硬冲突阈值，不构成"必须二选一"。
            return -0.05, "ADJACENT_NOT_CONFLICT"
        return 0.0, ""
    if (cat_a, cat_b) in SUPPORTS or (cat_b, cat_a) in SUPPORTS:
        # 与墙共用同一批图元 = 候选其实就是这堵墙自己，不是"墙上的洞"——这是冲突
        if overlap >= float(BUDGET["uid_overlap"]) or iou >= float(BUDGET["duplicate_iou"]):
            return -0.9, "SAME_LINES_AS_WALL"
        if _gap(a.get("bbox"), b.get("bbox")) <= max(1.0, float(thickness)) * 2.0:
            return 0.30, "HOST_SUPPORTS"
        return 0.0, "HOST_TOO_FAR"
    if iou >= float(BUDGET["duplicate_iou"]):
        return -0.3, "OVERLAP_UNEXPLAINED"
    return 0.0, ""


def conflicts(rows, *, thickness: float) -> dict:
    edges = []
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            weight, reason = relation(a, b, thickness=thickness)
            if weight == 0.0:
                continue
            edges.append({"a": a.get("id"), "b": b.get("id"), "w": _r(weight),
                          "reason": reason})
    return {"nodes": len(rows), "edges": edges,
            "stats": {"conflicts": sum(1 for e in edges if e["w"] < 0),
                      "supports": sum(1 for e in edges if e["w"] > 0)}}


# --------------------------------------------------------------------- 选择
def _objective(ids, singles, pair) -> float:
    total = sum(singles.get(i, 0.0) for i in ids)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            total += pair.get((a, b), pair.get((b, a), 0.0))
    return total


def _apply_constraints(rows, constraints) -> tuple[list, dict]:
    """把裁决约束（排除/保底保留/改判）落到候选上。"""
    constraints = constraints or {}
    drop = set(constraints.get("drop") or ())
    keep = set(constraints.get("keep") or ())
    categories = constraints.get("categories") or {}
    out: list = []
    forced_drop = 0
    reclassified = 0
    for hyp in rows or ():
        if not isinstance(hyp, dict):
            continue
        hid = hyp.get("id")
        if hid in drop:
            forced_drop += 1
            continue
        new_cat = categories.get(hid)
        if new_cat == "none":
            forced_drop += 1
            continue
        if new_cat and new_cat != hyp.get("category"):
            hyp = dict(hyp, status="pending", evidence=dict(
                hyp.get("evidence") or {}, arbitrated=True,
                requested_category=new_cat, category_confirmed=False,
                geometry_status="pending", v1_reason="REFIT_REQUIRED"))
            reclassified += 1
        elif hyp.get("category") == "column" and (hid in keep or new_cat == "column"):
            if column_geometry_ok(hyp):
                hyp = dict(hyp, status="candidate", evidence=dict(
                    hyp.get("evidence") or {}, category_confirmed=True,
                    category_basis="model_arbitration"))
        out.append(hyp)
    present = {h.get("id") for h in out}
    return out, {"forced_drop": forced_drop, "reclassified": reclassified,
                 "forced_keep": len(keep & present)}


def solve(pool, *, thickness: float = 200.0, budget=None, weights=None,
          constraints=None) -> dict:
    budget = dict(BUDGET, **(budget or {}))
    rows = [h for h in (pool.get("hypotheses") if isinstance(pool, dict) else pool) or ()
            if isinstance(h, dict)][:int(budget["max_candidates"])]
    original = rows
    rows, constraint_stats = _apply_constraints(rows, constraints)
    remaining = {h.get("id") for h in rows}
    blocked_rows = [{"id": h.get("id"), "category": h.get("category"),
                     "why": "MODEL_REJECT", "score": 0.0}
                    for h in original if h.get("id") not in remaining]
    checked = []
    for hyp in rows:
        if hyp.get("category") == "column":
            why = ("GEOMETRY_UNVERIFIED" if not column_geometry_ok(hyp) else
                   "CATEGORY_UNVERIFIED" if not column_category_confirmed(hyp) else None)
            if why:
                blocked_rows.append({"id": hyp.get("id"), "category": "column",
                                     "score": score(hyp), "why": why})
                continue
            if hyp.get("status") == "pending":
                hyp = dict(hyp, status="candidate")
        checked.append(hyp)
    rows = checked
    # 没有有效位置（零面积/缺值）的候选**不许参与选择**：它算不了冲突、也不该计数
    # 证据不足（pending）的候选可以进池、可以追溯，但**不参与计数**：
    # 它连形态/拓扑都没过，算进构件数就是把"可能"报成"已确认"。
    pending_rows = [h for h in rows if str(h.get("status")) == "pending"]
    if pending_rows:
        blocked_rows.extend({"id": h.get("id"), "category": h.get("category"),
                             "score": _r(score(h, weights=weights)),
                             "why": "PENDING_EVIDENCE"}
                            for h in pending_rows)
        rows = [h for h in rows if str(h.get("status")) != "pending"]
    bad_geometry = [h for h in rows if not _valid(h.get("bbox"))]
    if bad_geometry:
        blocked_rows.extend({"id": h.get("id"), "category": h.get("category"),
                             "score": 0.0, "why": "INVALID_GEOMETRY"}
                            for h in bad_geometry)
        rows = [h for h in rows if _valid(h.get("bbox"))]
    if REJECT_NEGATIVES:
        kept: list = []
        for hyp in rows:
            codes = {str(n.get("code") if isinstance(n, dict) else n)
                     for n in ((hyp.get("evidence") or {}).get("negatives") or ())}
            hit = codes & REJECT_NEGATIVES
            # 规则引擎自己的候选不因拟合器的否证被删（它有独立证据链）
            if hit and str(hyp.get("source")) != "rule":
                blocked_rows.append({"id": hyp.get("id"),
                                     "category": hyp.get("category"),
                                     "score": score(hyp, weights=weights),
                                     "why": "NEGATIVE_EVIDENCE",
                                     "codes": sorted(hit)})
                continue
            kept.append(hyp)
        rows = kept
    # 待核（v1 判为 pending）与几何无效的候选不参与选择：它们留在 rejected 里
    # 可查，但不进"确认构件"的计数。这是"确认/待核分开报告"的前提。
    selectable: list = []
    for hyp in rows:
        status = str(hyp.get("status") or "").strip().lower()
        if str(hyp.get("source")) == "propagation":
            ok, why = _propagation_ok(hyp)
            if not ok:
                blocked_rows.append({"id": hyp.get("id"), "category": hyp.get("category"),
                                     "score": score(hyp, weights=weights),
                                     "why": "PROPAGATION_WEAK_EVIDENCE",
                                     "codes": [why]})
                continue
        if status == "pending":
            blocked_rows.append({"id": hyp.get("id"), "category": hyp.get("category"),
                                 "score": score(hyp, weights=weights),
                                 "why": "PENDING_UNVERIFIED",
                                 "codes": [(hyp.get("evidence") or {}).get("v1_reason")]})
            continue
        selectable.append(hyp)
    rows = selectable
    singles = {h["id"]: score(h, weights=weights) for h in rows}
    graph = conflicts(rows, thickness=thickness)
    pair = {(e["a"], e["b"]): float(e["w"]) for e in graph["edges"]}
    hard = {(e["a"], e["b"]) for e in graph["edges"] if float(e["w"]) <= -0.9}
    hard |= {(e["b"], e["a"]) for e in graph["edges"] if float(e["w"]) <= -0.9}

    def blocked(chosen, cand_id) -> bool:
        return any((cand_id, other) in hard or (other, cand_id) in hard
                   for other in chosen)

    order = sorted(rows, key=lambda h: -singles.get(h["id"], 0.0))
    chosen: list = []
    # 模型明确"保留"且分数达标的候选先入座（仍受硬冲突约束，不会把互斥的一对塞进去）
    forced = set((constraints or {}).get("keep") or ())
    for hyp in order:
        if hyp["id"] not in forced:
            continue
        if blocked(chosen, hyp["id"]):
            continue
        chosen.append(hyp["id"])
    for hyp in order:
        if hyp["id"] in chosen:
            continue
        if blocked(chosen, hyp["id"]):
            continue
        gain = singles[hyp["id"]] + 0.5 * max(-1.0, min(1.0, sum(
            pair.get((hyp["id"], other), pair.get((other, hyp["id"]), 0.0))
            for other in chosen)))
        if gain <= 0:
            continue
        chosen.append(hyp["id"])

    for _ in range(int(budget["rounds"])):
        improved = False
        for hyp in order:
            if hyp["id"] in chosen:
                continue
            base = _objective(chosen, singles, pair)
            for victim in list(chosen):
                trial = [c for c in chosen if c != victim] + [hyp["id"]]
                if blocked([c for c in chosen if c != victim], hyp["id"]):
                    continue
                if _objective(trial, singles, pair) > base + 1e-9:
                    chosen = trial
                    improved = True
                    break
            if improved:
                break
        if not improved:
            break

    picked = [h for h in rows if h["id"] in set(chosen)]
    return {"schema": SCHEMA, "selected": picked, "selected_ids": chosen,
            "objective": _r(_objective(chosen, singles, pair)),
            "counts": {c: sum(1 for h in picked if h.get("category") == c)
                       for c in sorted({str(h.get("category")) for h in picked})},
            "scores": {h["id"]: singles[h["id"]] for h in rows},
            "conflicts": graph["stats"], "constraints": constraint_stats,
            "pending": [h.get("id") for h in pending_rows] + [h["id"] for h in blocked_rows if h["why"] in ("CATEGORY_UNVERIFIED", "GEOMETRY_UNVERIFIED")],
            "rejected": blocked_rows + [{"id": h["id"], "category": h.get("category"),
                                    "score": singles[h["id"]], "why": "NOT_SELECTED"}
                                   for h in order
                                   if h["id"] not in set(chosen)][:200],
            "weights": dict(WEIGHTS, **(weights or {}))}


def _dedupe(rows) -> list:
    """按候选 id 去重（保序）：阶段之间搬运候选时不允许同一实例算两次。"""
    seen: set = set()
    out: list = []
    for hyp in rows or ():
        key = hyp.get("id")
        if key in seen:
            continue
        seen.add(key)
        out.append(hyp)
    return out


def staged(pool, *, thickness: float = 200.0, profile=None, budget=None,
           weights=None, context=(), constraints=None) -> dict:
    """门窗柱分阶段求解：上下文（墙/轴网）→ 柱 → 洞口。

    墙在这里**不参与竞赛**：它们是上下文，删掉墙会让洞口没地方落。轴网同理。
    """
    budget = dict(BUDGET, **(budget or {}))
    rows = [h for h in (pool.get("hypotheses") if isinstance(pool, dict) else pool) or ()
            if isinstance(h, dict)]
    wanted = set((profile or {}).get("targets") or ("door", "window", "column"))
    out: dict = {"schema": SCHEMA, "stages": {}, "states": [], "needs": []}
    carry: list = []
    #: 目前仍然成立的实例 id（上下文 + 各阶段选中）。后面的阶段若淘汰某个实例，
    #: 它必须真的从 carry 里消失——以前是"整组带过"，被淘汰的柱会被重新加回结果。
    alive: set = set()
    for stage in STAGES:
        cats = STAGE_CATEGORIES[stage]
        if stage == "context":
            picked = _dedupe([h for h in _apply_constraints(
                [h for h in rows if h.get("category") in cats], constraints)[0]])
            carry = picked
            alive = {h.get("id") for h in picked}
            out["stages"][stage] = {"status": "ok", "categories": list(cats),
                                    "counts": {c: sum(1 for h in picked
                                                      if h.get("category") == c)
                                               for c in cats},
                                    "selected": [h.get("id") for h in picked],
                                    "note": "上下文构件不参与竞赛：删除墙会让洞口失去宿主"}
            continue
        subset = {"hypotheses": carry + [h for h in rows
                                         if h.get("category") in cats
                                         and h.get("category") in wanted]}
        res = solve(subset, thickness=thickness, budget=budget, weights=weights,
                    constraints=constraints)
        carry = _dedupe(res["selected"])
        alive = {h.get("id") for h in carry}
        out.setdefault("rejected", []).extend(res.get("rejected") or [])
        out["stages"][stage] = {"status": "ok", "objective": res["objective"],
                                "counts": res["counts"],
                                "selected": [h.get("id") for h in res["selected"]],
                                "conflicts": res["conflicts"],
                                "scores": res.get("scores"),
                                "pending": res.get("pending", []),
                                "rejected": res.get("rejected", []),
                                "uncertain": [h.get("id") for h in res["selected"]
                                              if h.get("status") == "uncertain"]}
        out["states"].append({"stage": stage,
                              "selected": [h.get("id") for h in res["selected"]]})
    total = sum(float((info or {}).get("objective") or 0.0)
                for info in (out["stages"] or {}).values())
    counts: dict = {}
    for hyp in carry:
        counts[str(hyp.get("category"))] = counts.get(str(hyp.get("category")), 0) + 1
    out["rejected"] = [h for h in _dedupe(out.get("rejected", [])) if h.get("id") not in alive]
    out["pending"] = list(dict.fromkeys(i for stage in out["stages"].values() for i in stage.get("pending", []) if i not in alive))
    out["final"] = {"selected": carry, "objective": _r(total), "counts": counts}
    out["objective"] = out["final"]["objective"]
    out["profile"] = (profile or {}).get("profile")
    return out


def summary(result) -> dict:
    result = result or {}
    staged = bool(result.get("stages"))
    selected = result.get("selected") or ((result.get("final") or {}).get("selected"))
    counts = result.get("counts") or ((result.get("final") or {}).get("counts"))
    return {"schema": SCHEMA, "staged": staged,
            "objective": result.get("objective"),
            "counts": counts or {}, "selected": len(selected or ()),
            "stages": {k: {"counts": (v or {}).get("counts"),
                           "status": (v or {}).get("status"),
                           "uncertain": len((v or {}).get("uncertain") or ()),
                           "pending": len((v or {}).get("pending") or ())}
                       for k, v in (result.get("stages") or {}).items()},
            "pending": {k: len((v or {}).get("pending") or ())
                        for k, v in (result.get("stages") or {}).items()},
            "weights": result.get("weights") or {},
            "profile": result.get("profile")}


__all__ = ["BUDGET", "CATEGORY_TRUST", "CATEGORY_WEIGHTS", "NEGATIVE_PENALTY",
           "SCHEMA", "STAGES", "WEIGHTS", "conflicts", "relation", "score",
           "solve", "staged", "summary"]
