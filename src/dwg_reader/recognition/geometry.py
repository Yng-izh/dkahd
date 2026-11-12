"""统一几何归一层：单位解释 + 方向无关几何基元 + 轮廓度量。

管线（与 ``work/packaging/architecture.md`` §2 对应）::

    DXF 原始实体
        -> 单位与坐标解释      detect_units() / apply_units()
        -> 块嵌套变换（展开）  snapshot.build_snapshot(derived=...)
        -> 方向无关几何原语    as_support() / proj() / perp() / dir_of()
        -> 轮廓度量            min_area_rect() / polygon_area() / perimeter()

单位规则：``$INSUNITS`` 只作为**一个候选**，不直接决定缩放。缩放由几何证据
（图面跨度 + 文字字高）投票决定；票数不足时返回 ``unit_unresolved``，本层
不做任何猜测性缩放。LLM 可以提出单位假设，但必须在这里被证据确认才算数。

本层不含任何识别规则、不判断构件身份、运行期不调用模型。
"""
from __future__ import annotations

import math

# --------------------------------------------------------------------------- 单位
#: ``$INSUNITS`` -> (单位名, mm 系数)；0/未列出的编码表示"未定义"
INSUNITS = {
    0: (None, None),
    1: ("inch", 25.4), 2: ("ft", 304.8), 3: ("mil", 0.0254),
    4: ("mm", 1.0), 5: ("cm", 10.0), 6: ("m", 1000.0),
    7: ("km", 1_000_000.0), 8: ("mil", 0.0254), 9: ("yd", 914.4),
    10: ("in", 25.4), 11: ("mm", 1.0), 12: ("nm", 1e-6), 13: ("um", 1e-3),
    14: ("dm", 100.0), 15: ("dam", 10_000.0), 16: ("hm", 100_000.0),
    17: ("gm", 1e9), 18: ("au", 1.495978707e14), 19: ("ly", 9.4607e21),
    20: ("pc", 3.0857e22),
}

#: 单位名 -> mm 系数（可人工/LLM 提出的假设集合）
UNIT_TO_MM = {"mm": 1.0, "cm": 10.0, "dm": 100.0, "m": 1000.0,
              "inch": 25.4, "in": 25.4, "ft": 304.8}

#: 一张建筑平面图的合理外廓（mm），用于给候选单位打分
PLAN_SPAN_MM = (5_000.0, 3_000_000.0)
#: 图面文字字高的合理区间（mm，1:1 绘制的注释文字）
TEXT_HEIGHT_MM = (80.0, 1_200.0)
#: "典型平面图尺度"，只用于同票数时排序候选，不参与判定
TYPICAL_SPAN_MM = 30_000.0

#: 判定"轴向"的容差（mm）：垂直偏移不超过它的线段仍按 H/V 处理（内部加速路径）
AXIS_TOL = 1.5
#: 用字高判定单位时最少要有的文字样本数（太少不足以代表这张图的字高）
MIN_TEXT_SAMPLE = 8
#: "典型房间名字高" = 字高区间的**几何中点**（80 与 1200 的几何均值 ≈ 310mm）。
#: 不另立常数：字高区间本身就编码了"建筑注释文字长什么样"，取它的几何中点作为
#: 锚点，既不需要新数字，也不会与区间脱节。它只在"字高同时容得下两个单位"时
#: 用来挑更像建筑注释的那个。
_TYPICAL_TEXT_MM = math.sqrt(TEXT_HEIGHT_MM[0] * TEXT_HEIGHT_MM[1])


def insunits_name(code) -> str | None:
    """``$INSUNITS`` 编码 -> 单位名；未知编码返回 None。"""
    try:
        return INSUNITS.get(int(code), (None, None))[0]
    except (TypeError, ValueError):
        return None


def _record_points(rec):
    for key in ("start", "end", "insert", "center"):
        p = rec.get(key)
        if p:
            yield p
    for p in rec.get("points") or ():
        yield p


#: 求内容包围盒时裁掉的尾部比例：真实图纸常有个别图元被丢在几十公里外
#: （块复制残留、外部参照炸开），裸 min/max 会把图面跨度放大几万倍，单位判定
#: 与按跨度推的尺度全部失效。裁掉 0.5% 的两端即可稳住，且不引入毫米常数。
_BBOX_CLIP = 0.005


def _clipped(vals):
    if not vals:
        return None, None
    vals = sorted(vals)
    n = len(vals)
    k = int(_BBOX_CLIP * n)
    if n > 200 and k > 0:
        return vals[k], vals[n - 1 - k]
    return vals[0], vals[-1]


def content_bbox(snapshot) -> list[float] | None:
    """图元内容的**稳健**包围盒（不信任 $EXTMIN/$EXTMAX，也不信任裸 min/max）。"""
    xs: list[float] = []
    ys: list[float] = []
    for rec in snapshot.get("entities", ()):
        for p in _record_points(rec):
            xs.append(float(p[0]))
            ys.append(float(p[1]))
        r = rec.get("radius")
        c = rec.get("center")
        if r and c:
            xs.extend([float(c[0]) - float(r), float(c[0]) + float(r)])
            ys.extend([float(c[1]) - float(r), float(c[1]) + float(r)])
    if not xs:
        head = snapshot.get("header") or {}
        lo, hi = head.get("extmin"), head.get("extmax")
        if lo and hi:
            return [float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])]
        return None
    x0, x1 = _clipped(xs)
    y0, y1 = _clipped(ys)
    return [x0, y0, x1, y1]


def _median(values):
    vals = sorted(float(v) for v in values)
    if not vals:
        return None
    return vals[len(vals) // 2]


def text_height_mm_probe(snapshot) -> float | None:
    """文字字高（世界坐标下的中位数），单位判定的一等证据。"""
    return _median(rec.get("height") for rec in snapshot.get("entities", ())
                   if rec.get("type") in ("TEXT", "MTEXT") and rec.get("height"))


def _in_band(value, band) -> bool:
    return value is not None and band[0] <= value <= band[1]


def detect_units(snapshot, *, header_units: str | None = None,
                 hypothesis: str | None = None) -> dict:
    """单位判定：``$INSUNITS`` / 图面跨度 / 文字字高 三方投票。

    参数
    ----
    header_units
        覆盖 ``snapshot["header"]["insunits"]``（若调用方已解析过）。
    hypothesis
        外部（人工或离线 LLM）提出的单位假设，作为**弱证据 +0.5 票**参与投票，
        最终仍以几何证据为准；假设被采纳或否决都写进 ``notes``。

    返回
    ----
    ``{"units", "scale", "resolved", "source", "candidates", "evidence", "notes"}``
    ``scale`` 是"世界坐标 -> mm"的乘数；``resolved=False`` 时恒为 1.0。
    """
    head = snapshot.get("header") or {}
    hdr = header_units if header_units is not None else insunits_name(head.get("insunits"))
    hdr = (hdr or "").lower() or None
    if hdr == "in":
        hdr = "inch"

    bbox = content_bbox(snapshot)
    span = max(bbox[2] - bbox[0], bbox[3] - bbox[1]) if bbox else None
    th = text_height_mm_probe(snapshot)
    text_n = sum(1 for rec in snapshot.get("entities", ())
                 if rec.get("type") in ("TEXT", "MTEXT") and rec.get("height"))

    votes: dict[str, list[str]] = {}
    weights: dict[str, float] = {}

    def add(unit, weight, why):
        if unit not in UNIT_TO_MM:
            return
        votes.setdefault(unit, []).append(why)
        weights[unit] = weights.get(unit, 0.0) + weight

    if hdr:
        add(hdr, 1.0, "header:$INSUNITS")
    hyp = (hypothesis or "").lower() or None
    if hyp == "in":
        hyp = "inch"
    if hyp:
        add(hyp, 0.5, "hypothesis")

    span_hits = []
    for unit, factor in UNIT_TO_MM.items():
        if unit == "in":
            continue
        if span is not None and _in_band(span * factor, PLAN_SPAN_MM):
            span_hits.append(unit)
    # 命中过多说明该证据区分度低：降权保留，而不是整条丢弃
    span_weight = 1.0 if len(span_hits) <= 2 else 0.5
    for unit in span_hits:
        add(unit, span_weight, "span")

    text_hits = []
    for unit, factor in UNIT_TO_MM.items():
        if unit == "in":
            continue
        if th is not None and _in_band(th * factor, TEXT_HEIGHT_MM):
            text_hits.append(unit)
    text_weight = 1.5 if len(text_hits) <= 2 else 0.75
    for unit in text_hits:
        add(unit, text_weight, "text_height")

    notes = []
    if span is None:
        notes.append("无图元包围盒，跨度证据缺失")
    if th is None:
        notes.append("无文字字高，字高证据缺失")

    candidates = [{"units": u, "scale": UNIT_TO_MM[u], "score": round(weights[u], 2),
                   "evidence": sorted(votes[u]),
                   "span_mm": None if span is None else round(span * UNIT_TO_MM[u], 1)}
                  for u in sorted(weights, key=lambda u: (-weights[u], u))]

    # ---- 决定性判据：文字字高是唯一"物理含义固定"的量。
    # 房间名字高在模型空间里总是 80~1200mm 这个量级，而单位之间的换算系数是 10 倍
    # 一级，所以**通常只有一个单位能让中位字高落进这个区间**。这一条比"投票分数
    # 够不够 2.0"可靠得多：后者在有杂散图元把图面跨度撑大时必然失败，于是整张图
    # 按错单位处理，墙/窗/房间全线塌掉（实测把一张图从 mm 换算成 m 后，构件从
    # 187 掉到 63）。
    # 字高区间有 15 倍宽，单靠它偶尔会有两个候选同时落进去（例如 0.297 在米制下
    # 是 297mm，在英尺制下是 90mm——两个都像"注释文字"）。这时用**图面跨度**再
    # 交一次：建筑平面总在 5m~3km 这个量级。两条都是"建筑图长什么样"的声明，
    # 不是拟合出来的阈值，所以两个约束取交集不引入图纸专属数值。
    text_hits_unique = [u for u in UNIT_TO_MM
                        if th is not None and u != "in"
                        and _in_band(th * UNIT_TO_MM[u], TEXT_HEIGHT_MM)]
    if th is not None and len(text_hits_unique) > 1:
        # 字高区间有 15 倍宽，偶尔两个单位都落进去（0.297 在米制下是 297mm、
        # 在英尺制下是 91mm）。这时选**更接近典型房间名字高**的那个——用跨度去
        # 分辨是不可靠的：真实图纸常有个别图元丢在几公里外，图面跨度会被放大
        # 上万倍（这张图真实 24m，内容跨度 6km）。
        def _near_typical(u):
            return abs(math.log(max(th * UNIT_TO_MM[u], 1e-9) / _TYPICAL_TEXT_MM))
        text_hits_unique = [min(text_hits_unique, key=_near_typical)]
    if th is not None and len(text_hits_unique) == 1 and text_n >= MIN_TEXT_SAMPLE:
        units, resolved = text_hits_unique[0], True
        source = ("header+text_height" if hdr == units else "text_height")
        notes.append(f"由字高 {th:.3g} 唯一确定单位为 {units}"
                     f"（{units}->{th * UNIT_TO_MM[units]:.0f}mm 落在 "
                     f"{TEXT_HEIGHT_MM[0]:.0f}~{TEXT_HEIGHT_MM[1]:.0f}mm 内）")
        return _units_result(units, resolved, source, hdr, hyp, candidates,
                             span, th, bbox, notes)

    units, resolved, source = None, False, "unresolved"
    if candidates:
        top = candidates[0]
        tied = [c for c in candidates if abs(c["score"] - top["score"]) < 1e-9]
        if len(tied) > 1:
            # 同票：选"更接近典型平面图尺度"的那个（只排序，不额外加分）
            tied.sort(key=lambda c: abs(math.log(max(1.0, c["span_mm"] or 1.0)
                                                / TYPICAL_SPAN_MM)))
            top = tied[0]
            notes.append("同票按平面图典型尺度排序：" +
                         "、".join(f"{c['units']}={c['score']}" for c in candidates[:3]))
        if top["score"] >= 2.0:
            units, resolved = top["units"], True
            source = "geometry" if "header:$INSUNITS" not in top["evidence"] else "header+geometry"
            if hdr and hdr != units:
                source = "geometry"
                notes.append(f"$INSUNITS={hdr} 与几何证据冲突，按几何证据取 {units}")
        else:
            notes.append(f"证据票数不足（最高 {top['units']}={top['score']}），标记 unit_unresolved")

    if hyp:
        if units == hyp:
            notes.append(f"假设单位 {hyp} 被几何证据采纳")
        else:
            notes.append(f"假设单位 {hyp} 未被采纳（证据取 {units or 'unresolved'}）")

    return _units_result(units, resolved, source, hdr, hyp, candidates,
                         span, th, bbox, notes)


def _units_result(units, resolved, source, hdr, hyp, candidates,
                  span, th, bbox, notes):
    if hyp:
        if units == hyp:
            notes = notes + [f"假设单位 {hyp} 被几何证据采纳"]
        else:
            notes = notes + [f"假设单位 {hyp} 未被采纳（证据取 {units or 'unresolved'}）"]
    return {
        "units": units,
        "scale": UNIT_TO_MM[units] if resolved else 1.0,
        "resolved": resolved,
        "source": source,
        "header_units": hdr,
        "hypothesis": hyp,
        "candidates": candidates,
        "evidence": {"span": None if span is None else round(span, 3),
                     "text_height": None if th is None else round(th, 3),
                     "bbox": None if bbox is None else [round(v, 3) for v in bbox]},
        "notes": notes,
    }


_SCALE_FIELDS = ("start", "end", "insert", "center")
#: 需要跟着单位换算的**标量长度**字段。
#: ``xscale``/``yscale`` 必须在内：块参照的缩放因子本身就是长度（天正门块的
#: xscale 直接就是洞口宽）。漏掉它们的后果很具体——整张图从 mm 换算成米以后
#: 坐标变了、门块的 xscale 还是 0.65 米，门宽被算成 0.65mm，所有门被当"太窄"
#: 滤掉（实测门 30 → 0、窗 32 → 2）。
_SCALE_SCALARS = ("radius", "height", "xscale", "yscale")


def _scale_record(rec: dict, factor: float) -> dict:
    out = dict(rec)
    for key in _SCALE_FIELDS:
        p = rec.get(key)
        if p:
            out[key] = [float(p[0]) * factor, float(p[1]) * factor]
    if rec.get("points"):
        out["points"] = [[float(p[0]) * factor, float(p[1]) * factor]
                         for p in rec["points"]]
    for key in _SCALE_SCALARS:
        if isinstance(rec.get(key), (int, float)):
            out[key] = float(rec[key]) * factor
    # 块参照的 4x4 仿射矩阵：平移分量（12/13/14）与线性部分都是长度/比例，
    # 必须一起换算。漏掉矩阵的后果是"实体插入点换了单位、门块内部几何没换"，
    # 于是门弧铰点落在离墙几千米的地方、洞口宽度被算成 1.7mm 而被整批滤掉
    # （实测单位换算后门 30 → 0，而单独的 recognize_doors 明明是 30）。
    m = rec.get("matrix")
    if isinstance(m, (list, tuple)) and len(m) == 16:
        out["matrix"] = [float(v) * factor for v in m[:15]] + [float(m[15])]
    return out


def apply_units(snapshot, info: dict) -> dict:
    """按 ``detect_units`` 的结论把快照坐标换算到 mm（``scale == 1`` 时原样返回）。"""
    factor = float(info.get("scale") or 1.0)
    if abs(factor - 1.0) < 1e-12:
        return snapshot
    out = dict(snapshot)
    out["entities"] = [_scale_record(r, factor) for r in snapshot.get("entities", ())]
    if snapshot.get("derived"):
        out["derived"] = [_scale_record(r, factor) for r in snapshot["derived"]]
    blocks = {}
    for name, blk in (snapshot.get("blocks") or {}).items():
        blocks[name] = {
            "arcs": [[*(float(v) * factor for v in arc[:3]), *arc[3:]]
                     for arc in blk.get("arcs", ())],
            "lines": [[float(v) * factor for v in line] for line in blk.get("lines", ())],
            # loops（闭合多段线，柱/洞口轮廓）与嵌套层数必须一起换算并保留，
            # 否则一旦发生单位换算，块里只剩 arcs/lines，柱轮廓凭空消失——
            # 表现为"单位 mm→m 后柱数变化"，与不变性验收互为镜像。
            "loops": [[[float(p_[0]) * factor, float(p_[1]) * factor] for p_ in loop]
                      for loop in blk.get("loops", ())],
            "nest_depth": blk.get("nest_depth"),
        }
    out["blocks"] = blocks
    head = dict(snapshot.get("header") or {})
    for key in ("extmin", "extmax"):
        p = head.get(key)
        if p:
            head[key] = [float(p[0]) * factor, float(p[1]) * factor]
    head["units_applied"] = info.get("units")
    head["units_scale"] = factor
    out["header"] = head
    return out


# --------------------------------------------------------------------------- 线段基元
def norm_dir(dx: float, dy: float) -> tuple[float, float]:
    length = math.hypot(dx, dy)
    if length < 1e-12:
        return (1.0, 0.0)
    return (dx / length, dy / length)


def as_support(p, q, *, layer=None, handle=None, kind="LINE", extra=None) -> dict | None:
    """两点 -> 方向无关的线支撑（LinearSupport）。

    字段：``origin`` / ``dir`` / ``nrm`` / ``pos`` / ``lo`` / ``hi`` / ``length`` /
    ``angle``（度，0~180）/ ``orient``（``H`` / ``V`` / ``A``）。

    ``orient`` 只是**加速标签**：H/V 时 ``pos`` = 法向坐标、``lo``/``hi`` = 轴向投影，
    与改造前的 ``(pos, lo, hi)`` 数值完全一致；任意方向（``A``）用同一套点积定义，
    因此后续所有共线 / 平行 / 投影 / 距离计算都不需要分情况。
    """
    (x1, y1), (x2, y2) = p, q
    dx, dy = float(x2) - float(x1), float(y2) - float(y1)
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return None
    ux, uy = norm_dir(dx, dy)
    # A geometrical support is undirected.  Normalize the sign BEFORE
    # computing the normal, otherwise reversing the source LINE flips pos
    # and makes identical oblique walls belong to unrelated groups.
    if uy < -1e-12 or (abs(uy) <= 1e-12 and ux < 0):
        ux, uy = -ux, -uy
    if abs(dy) <= AXIS_TOL and abs(dx) > AXIS_TOL:
        orient, nrm, ux, uy = "H", (0.0, 1.0), 1.0, 0.0
    elif abs(dx) <= AXIS_TOL and abs(dy) > AXIS_TOL:
        orient, nrm, ux, uy = "V", (1.0, 0.0), 0.0, 1.0
    else:
        orient, nrm = "A", (-uy, ux)
    origin = (float(x1), float(y1))
    lo = ux * origin[0] + uy * origin[1]
    hi = ux * float(x2) + uy * float(y2)
    pos = nrm[0] * origin[0] + nrm[1] * origin[1]
    if hi < lo:
        lo, hi = hi, lo
        origin = (float(x2), float(y2))
        pos = nrm[0] * origin[0] + nrm[1] * origin[1]
    angle = math.degrees(math.atan2(uy, ux)) % 180.0
    rec = {"origin": [origin[0], origin[1]], "dir": [ux, uy], "nrm": [nrm[0], nrm[1]],
           "pos": pos, "lo": lo, "hi": hi, "length": length, "angle": angle,
           "orient": orient, "kind": kind, "layer": layer, "handle": handle}
    if extra:
        rec.update(extra)
    return rec


def proj(seg, p) -> float:
    """点沿 ``seg`` 方向的投影坐标。"""
    ux, uy = seg["dir"]
    return ux * p[0] + uy * p[1]


def perp(seg, p) -> float:
    """点到 ``seg`` 所在直线的有符号法向距离。"""
    nx, ny = seg["nrm"]
    return nx * p[0] + ny * p[1] - seg["pos"]


def point_at(seg, u: float) -> tuple[float, float]:
    """``seg`` 所在直线上投影坐标为 ``u`` 的点。"""
    ux, uy = seg["dir"]
    nx, ny = seg["nrm"]
    # p = u * dir + pos * nrm
    return (u * ux + seg["pos"] * nx, u * uy + seg["pos"] * ny)


def is_same_line(a, b, *, angle_tol=1.0, pos_tol=1.5) -> bool:
    """两条支撑是否共线（方向平行 + 法向偏移一致）。"""
    da = abs(a["angle"] - b["angle"])
    da = min(da, 180.0 - da)
    if da > angle_tol:
        return False
    return abs(a["pos"] - b["pos"]) <= pos_tol


def overlap_1d(a_lo, a_hi, b_lo, b_hi) -> float:
    return min(a_hi, b_hi) - max(a_lo, b_lo)


# --------------------------------------------------------------------------- 轮廓度量
def polygon_area(pts) -> float:
    n = len(pts)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def perimeter(pts) -> float:
    n = len(pts)
    if n < 2:
        return 0.0
    return sum(math.dist(pts[i], pts[(i + 1) % n]) for i in range(n))


def convex_hull(pts) -> list[list[float]]:
    """Andrew monotone chain；点集不共线退化时返回原凸包顶点。"""
    p = sorted({(round(float(x), 6), round(float(y), 6)) for x, y in pts})
    if len(p) <= 2:
        return [[x, y] for x, y in p]

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for pt in p:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], pt) <= 0:
            lower.pop()
        lower.append(pt)
    upper: list[tuple[float, float]] = []
    for pt in reversed(p):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], pt) <= 0:
            upper.pop()
        upper.append(pt)
    hull = lower[:-1] + upper[:-1]
    return [[x, y] for x, y in hull]


def min_area_rect(pts) -> dict:
    """最小面积外接矩形（旋转卡壳的"沿凸包边旋转"版本）。

    返回 ``{"width","height","angle","area","bbox","corners"}``：
    旋转柱 / 斜放矩形不再被轴对齐 bbox 误判。
    """
    hull = convex_hull(pts)
    if len(hull) < 3:
        xs = [p[0] for p in pts] or [0.0]
        ys = [p[1] for p in pts] or [0.0]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        return {"width": w, "height": h, "angle": 0.0, "area": w * h,
                "bbox": [min(xs), min(ys), max(xs), max(ys)],
                "corners": [[min(xs), min(ys)], [max(xs), min(ys)],
                            [max(xs), max(ys)], [min(xs), max(ys)]]}
    best = None
    n = len(hull)
    for i in range(n):
        x1, y1 = hull[i]
        x2, y2 = hull[(i + 1) % n]
        ux, uy = norm_dir(x2 - x1, y2 - y1)
        nx, ny = -uy, ux
        us = [ux * p[0] + uy * p[1] for p in hull]
        vs = [nx * p[0] + ny * p[1] for p in hull]
        w, h = max(us) - min(us), max(vs) - min(vs)
        area = w * h
        if best is None or area < best["area"] - 1e-9:
            u0, v0 = min(us), min(vs)
            corners = []
            for cu, cv in ((u0, v0), (u0 + w, v0), (u0 + w, v0 + h), (u0, v0 + h)):
                corners.append([cu * ux + cv * nx, cu * uy + cv * ny])
            xs = [p[0] for p in hull]
            ys = [p[1] for p in hull]
            best = {"width": w, "height": h,
                    "angle": math.degrees(math.atan2(uy, ux)) % 180.0,
                    "area": area,
                    "bbox": [min(xs), min(ys), max(xs), max(ys)],
                    "corners": corners}
    return best


def shape_of(outline, area=None, peri=None) -> dict:
    """轮廓形态特征：面积/周长/最小外接矩形/是否轴对齐/细长度。"""
    rect = min_area_rect(outline)
    a = polygon_area(outline) if area is None else area
    p = perimeter(outline) if peri is None else peri
    rect_area = rect["area"] or 1.0
    long_side = max(rect["width"], rect["height"]) or 1.0
    short_side = min(rect["width"], rect["height"])
    return {"area": a, "perimeter": p, "rect": rect,
            "aspect": long_side / (short_side or 1.0),
            "fill": a / rect_area,
            "axis_aligned": rect["angle"] % 90.0 <= 1.0 or 90.0 - rect["angle"] % 90.0 <= 1.0,
            "rotated": 1.0 < rect["angle"] % 90.0 < 89.0}
