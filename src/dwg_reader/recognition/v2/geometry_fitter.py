"""几何拟合：把一份构件语法**在原始图元上兑现**成精确几何。

分工（这一句就是整个模块的意义）：

* 语法说"这个构件由哪些部分、怎么组合"（:mod:`component_grammar`）；
* 拟合器负责**在图里找**这些部分、检查关系、算出坐标/尺寸/轮廓；
* 模型既不提供坐标，也不参与计算。

四个原语匹配器是真正干活的地方：平行线对、重复线束、闭合轮廓、圆弧串；
其余原语（短连接件、填充、共线串、文字锚点、刻度对）用于**关系校验**与加分。

所有容差都从"图内自噪声/墙厚"来（:mod:`scale`），不存在写死的毫米数；
匹配数量受 :data:`BUDGET` 限制，避免一条语法在大图上炸出几万个候选。
"""
from __future__ import annotations

import hashlib
import math

SCHEMA = "fit-result/1.0"
HYP_SCHEMA = "hypothesis/1.0"

#: 逻辑预算（条/个）
BUDGET = {
    "seeds_per_part": 400,      # 单个原语最多匹配多少个种子
    "candidates": 240,          # 单次拟合最多产出多少候选
    "instances": 60,            # find_similar 最多找多少个同类实例
    "pairs": 40000,             # 平行线对枚举上限
    "overlap_ratio": 0.55,      # 两面线沿向重叠占较短者的比例（配对门槛）
    "thickness_ratio": 0.45,    # 面线间距 / 墙厚 的合理带（0.45~2.6 倍）
    "thickness_max": 2.6,
    "connector_ratio": 0.6,     # 短连接件长 / 间距 的上限
    "tick_ratio": 0.25,         # 刻度线长 / 间距 的上限
}


def _r(v, nd: int = 1) -> float:
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return 0.0


def _sha(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _len(a, b) -> float:
    return math.hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1]))


def _angle(a, b) -> float:
    return math.degrees(math.atan2(float(b[1]) - float(a[1]),
                                   float(b[0]) - float(a[0]))) % 180.0


def _angle_diff(x, y) -> float:
    d = abs(float(x) - float(y)) % 180.0
    return min(d, 180.0 - d)


def _pt_seg_dist(p, a, b) -> float:
    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 <= 0:
        return math.hypot(float(p[0]) - ax, float(p[1]) - ay)
    t = max(0.0, min(1.0, ((float(p[0]) - ax) * dx + (float(p[1]) - ay) * dy) / L2))
    return math.hypot(float(p[0]) - (ax + t * dx), float(p[1]) - (ay + t * dy))


def _union(boxes):
    rows = [b for b in (boxes or ()) if b]
    if not rows:
        return None
    return [_r(min(b[0] for b in rows)), _r(min(b[1] for b in rows)),
            _r(max(b[2] for b in rows)), _r(max(b[3] for b in rows))]


def _box_gap(a, b) -> float:
    if not a or not b:
        return float("inf")
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def _seg_of(e: dict):
    g = e.get("geom") or {}
    if e.get("kind") == "segment":
        return g.get("a"), g.get("b")
    if e.get("kind") == "poly":
        pts = g.get("pts") or ()
        return (pts[0], pts[-1]) if len(pts) >= 2 else (None, None)
    return None, None


# --------------------------------------------------------------------- 原语
def m_parallel_line_pair(entities, *, thickness: float, tol: float, seed_uids=()):
    """两条平行线 + 沿向重叠 → 一对"面线"（墙/窗框/门框的共同骨架）。"""
    lines = [e for e in entities
             if e["kind"] in ("segment", "poly") and e.get("angle") is not None]
    if seed_uids:
        keep = set(seed_uids)
        seeded = [e for e in lines if e["uid"] in keep]
        lines = seeded or lines
    out = []
    t_lo = float(BUDGET["thickness_ratio"]) * thickness
    t_hi = float(BUDGET["thickness_max"]) * thickness
    n = 0
    for i, a in enumerate(lines):
        a0, a1 = _seg_of(a)
        if not a0 or not a1:
            continue
        for b in lines[i + 1:]:
            n += 1
            if n > int(BUDGET["pairs"]):
                return out
            b0, b1 = _seg_of(b)
            if not b0 or not b1:
                continue
            if _angle_diff(a["angle"], b["angle"]) > math.degrees(
                    math.atan2(tol, max(1.0, min(a["length"], b["length"])))):
                continue
            gap = (_pt_seg_dist(b0, a0, a1) + _pt_seg_dist(b1, a0, a1)) / 2.0
            if gap < t_lo or gap > t_hi:
                continue
            # 沿向重叠：把 b 投影到 a 的方向上
            ang = math.radians(float(a["angle"]))
            dx, dy = math.cos(ang), math.sin(ang)
            pa = [(a0[0] * dx + a0[1] * dy), (a1[0] * dx + a1[1] * dy)]
            pb = [(b0[0] * dx + b0[1] * dy), (b1[0] * dx + b1[1] * dy)]
            lo = max(min(pa), min(pb))
            hi = min(max(pa), max(pb))
            overlap = hi - lo
            shorter = min(abs(pa[1] - pa[0]), abs(pb[1] - pb[0]))
            if shorter <= 0 or overlap / shorter < float(BUDGET["overlap_ratio"]):
                continue
            # 中心线：两条线按 pos 平均
            nrm = (-dy, dx)
            pa_mid = (min(pa) + max(pa)) / 2.0
            pb_mid = (min(pb) + max(pb)) / 2.0
            base = a0[0] * dx + a0[1] * dy
            side = a0[0] * nrm[0] + a0[1] * nrm[1]
            side_b = b0[0] * nrm[0] + b0[1] * nrm[1]
            mid_side = (side + side_b) / 2.0
            p0 = [dx * lo + nrm[0] * mid_side, dy * lo + nrm[1] * mid_side]
            p1 = [dx * hi + nrm[0] * mid_side, dy * hi + nrm[1] * mid_side]
            out.append({"prim": "parallel_line_pair", "uids": [a["uid"], b["uid"]],
                        "axis": [[_r(p0[0]), _r(p0[1])], [_r(p1[0]), _r(p1[1])]],
                        "gap": _r(gap), "overlap": _r(overlap),
                        "angle": _r(a["angle"], 2),
                        "bbox": _union([a["bbox"], b["bbox"]]),
                        "layers": sorted({a["layer"], b["layer"]})})
            if len(out) >= int(BUDGET["seeds_per_part"]):
                return out
    return out


def m_repeated_line_bundle(entities, *, thickness: float, tol: float, seed_uids=()):
    """≥3 条平行等距线：幕墙分格、窗分格、踏面线、台阶线都是这个形态。"""
    lines = [e for e in entities
             if e["kind"] in ("segment", "poly") and e.get("angle") is not None]
    if seed_uids:
        keep = set(seed_uids)
        lines = [e for e in lines if e["uid"] in keep] or lines
    groups: dict[tuple, list] = {}
    for e in lines:
        groups.setdefault((round(float(e["angle"]) / 1.0),), []).append(e)
    out = []
    for _key, members in groups.items():
        if len(members) < 3:
            continue
        ang = math.radians(float(members[0]["angle"]))
        dx, dy = math.cos(ang), math.sin(ang)
        nrm = (-dy, dx)
        rows = []
        for e in members:
            a, b = _seg_of(e)
            if not a or not b:
                continue
            side = (a[0] * nrm[0] + a[1] * nrm[1] + b[0] * nrm[0] + b[1] * nrm[1]) / 2.0
            pa = [a[0] * dx + a[1] * dy, b[0] * dx + b[1] * dy]
            rows.append({"e": e, "side": side, "lo": min(pa), "hi": max(pa)})
        rows.sort(key=lambda r: r["side"])
        run = [rows[0]]
        for nxt in rows[1:]:
            prev = run[-1]
            overlap = min(prev["hi"], nxt["hi"]) - max(prev["lo"], nxt["lo"])
            shorter = min(prev["hi"] - prev["lo"], nxt["hi"] - nxt["lo"])
            if shorter <= 0 or overlap / shorter < float(BUDGET["overlap_ratio"]):
                run = [nxt]
                continue
            run.append(nxt)
            if len(run) >= 3:
                sides = [r["side"] for r in run]
                steps = [b - a for a, b in zip(sides, sides[1:])]
                med = sorted(steps)[len(steps) // 2]
                if med <= 0:
                    continue
                spread = max(abs(s - med) for s in steps)
                if spread > 0.35 * med:
                    continue
                lo = max(r["lo"] for r in run)
                hi = min(r["hi"] for r in run)
                if hi - lo <= 0:
                    continue
                uids = [r["e"]["uid"] for r in run]
                mid = (min(sides) + max(sides)) / 2.0
                out.append({"prim": "repeated_line_bundle", "uids": uids,
                            "count": len(run), "spacing": _r(med),
                            "bbox": _union([r["e"]["bbox"] for r in run]),
                            "axis": [[_r(dx * lo + nrm[0] * mid),
                                      _r(dy * lo + nrm[1] * mid)],
                                     [_r(dx * hi + nrm[0] * mid),
                                      _r(dy * hi + nrm[1] * mid)]],
                            "layers": sorted({r["e"]["layer"] for r in run})})
                if len(out) >= int(BUDGET["seeds_per_part"]):
                    return out
    return out


def m_closed_contour(entities, *, thickness: float, tol: float, seed_uids=()):
    """闭合轮廓（多段线闭合 / 填充外环）：柱、墩、异形块的最常见表达。"""
    out = []
    for e in entities:
        if seed_uids and e["uid"] not in set(seed_uids):
            continue
        geom = e.get("geom") or {}
        if e["kind"] == "poly" and geom.get("closed"):
            pts = geom.get("pts") or ()
        elif e["kind"] == "hatch":
            loops = geom.get("loops") or ()
            pts = loops[0] if loops else ()
        else:
            continue
        if len(pts) < 3:
            continue
        xs = [float(p[0]) for p in pts]
        ys = [float(p[1]) for p in pts]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        if w <= tol or h <= tol:
            continue
        area = abs(sum(float(pts[i][0]) * float(pts[i + 1][1])
                       - float(pts[i + 1][0]) * float(pts[i][1])
                       for i in range(len(pts) - 1))) / 2.0
        out.append({"prim": "closed_contour", "uids": [e["uid"]],
                    "outline": [[_r(p[0]), _r(p[1])] for p in pts],
                    "bbox": [_r(min(xs)), _r(min(ys)), _r(max(xs)), _r(max(ys))],
                    "w": _r(w), "h": _r(h), "area": _r(area),
                    "solid": bool(geom.get("solid")),
                    "layers": [e["layer"]]})
        if len(out) >= int(BUDGET["seeds_per_part"]):
            break
    return out


def m_arc_run(entities, *, thickness: float, tol: float, seed_uids=()):
    """圆弧串：门扇开启弧。半径与门宽同量级时才算"开启弧"。"""
    dw = max(1.0, float(thickness) * 2.0)
    out = []
    for e in entities:
        if e["kind"] != "arc":
            continue
        if seed_uids and e["uid"] not in set(seed_uids):
            continue
        geom = e.get("geom") or {}
        radius = float(geom.get("radius") or 0.0)
        if radius <= 0 or radius > dw * 4.0:
            continue
        outline = []
        center = geom.get("center")
        a0, a1 = float(geom.get("start_deg") or 0.0), float(geom.get("end_deg") or 0.0)
        if center:
            for i in range(9):
                ang = math.radians(a0 + (a1 - a0) * i / 8.0)
                outline.append([_r(float(center[0]) + radius * math.cos(ang)),
                                _r(float(center[1]) + radius * math.sin(ang))])
        out.append({"prim": "arc_run", "uids": [e["uid"]], "radius": _r(radius),
                    "center": [_r(center[0]), _r(center[1])] if center else None,
                    "sweep": _r(abs(a1 - a0)),
                    "bbox": e.get("bbox"), "outline": outline,
                    "layers": [e["layer"]]})
    return out


def m_short_connectors(entities, *, thickness: float, tol: float, seed_uids=()):
    """短线段：窗框两端封口、连接件。长度上限按"间距"比例给。"""
    cap = float(BUDGET["connector_ratio"]) * float(thickness) * 6.0
    out = []
    for e in entities:
        if e["kind"] != "segment":
            continue
        if seed_uids and e["uid"] not in set(seed_uids):
            continue
        length = float(e.get("length") or 0.0)
        if length <= tol or length > cap:
            continue
        a, b = _seg_of(e)
        out.append({"prim": "short_connectors", "uids": [e["uid"]],
                    "segment": [[_r(a[0]), _r(a[1])], [_r(b[0]), _r(b[1])]],
                    "bbox": e.get("bbox"), "length": _r(length),
                    "layers": [e["layer"]]})
        if len(out) >= int(BUDGET["seeds_per_part"]):
            break
    return out


def m_collinear_run(entities, *, thickness: float, tol: float, seed_uids=()):
    """共线相接的长线串（墙线延伸）。"""
    segs = [e for e in entities if e["kind"] in ("segment", "poly")
            and (e.get("length") or 0) >= thickness]
    if seed_uids:
        keep = set(seed_uids)
        segs = [e for e in segs if e["uid"] in keep] or segs
    segs.sort(key=lambda e: -float(e.get("length") or 0.0))
    out = []
    for e in segs[:int(BUDGET["seeds_per_part"])]:
        a, b = _seg_of(e)
        if not a or not b:
            continue
        out.append({"prim": "collinear_run", "uids": [e["uid"]],
                    "axis": [[_r(a[0]), _r(a[1])], [_r(b[0]), _r(b[1])]],
                    "bbox": e.get("bbox"), "length": _r(float(e["length"])),
                    "angle": e.get("angle"), "layers": [e["layer"]]})
    return out


def m_tick_pair(entities, *, thickness: float, tol: float, seed_uids=()):
    """成对短刻度线（窗编号/标高符号），只做加分证据。"""
    cap = float(BUDGET["tick_ratio"]) * float(thickness)
    short = [e for e in entities if e["kind"] == "segment"
             and 0 < float(e.get("length") or 0.0) <= cap]
    out = []
    for i, a in enumerate(short[:int(BUDGET["seeds_per_part"])]):
        out.append({"prim": "tick_pair", "uids": [a["uid"]],
                    "bbox": a.get("bbox"), "angle": a.get("angle"),
                    "layers": [a["layer"]]})
    return out


def m_text_anchor(entities, *, thickness: float, tol: float, seed_uids=()):
    out = []
    for e in entities:
        if e["kind"] != "text":
            continue
        out.append({"prim": "text_anchor", "uids": [e["uid"]],
                    "text": (e.get("geom") or {}).get("text"),
                    "bbox": e.get("bbox"), "layers": [e["layer"]]})
    return out


MATCHERS = {
    "parallel_line_pair": m_parallel_line_pair,
    "repeated_line_bundle": m_repeated_line_bundle,
    "short_connectors": m_short_connectors,
    "closed_contour": m_closed_contour,
    "arc_run": m_arc_run,
    "collinear_run": m_collinear_run,
    "tick_pair": m_tick_pair,
    "text_anchor": m_text_anchor,
    # 实心四边形与填充区域都按闭合轮廓处理（差别体现在 area/solid 上）
    "solid_quad": m_closed_contour,
    "hatch_region": m_closed_contour,
}


# --------------------------------------------------------------------- 关系校验
def check_relation(name: str, left: dict, right: dict, *, tol: float,
                   walls=(), faces=()) -> tuple[bool, dict]:
    """校验一条语法关系。返回 ``(是否满足, 证据)``。"""
    lb, rb = left.get("bbox"), right.get("bbox")
    if name in ("inside", "enclose"):
        ok = bool(lb and rb and lb[0] >= rb[0] - tol and lb[1] >= rb[1] - tol
                  and lb[2] <= rb[2] + tol and lb[3] <= rb[3] + tol)
        return ok, {"gap": _r(_box_gap(lb, rb))}
    if name == "connect":
        # A parallel-line pair exposes its center axis, whereas an end cap
        # touches the *two face lines*. Testing the cap against the centerline
        # endpoints wrongly rejects an otherwise exact rectangular window.
        frame = (left if left.get("prim") == "parallel_line_pair" else
                 right if right.get("prim") == "parallel_line_pair" else None)
        cap = right if frame is left else left
        frame_box = (frame or {}).get("bbox")
        cap_seg = cap.get("segment")
        if frame_box and cap_seg and len(cap_seg) == 2:
            def on_frame_edge(pt):
                x, y = float(pt[0]), float(pt[1])
                x0, y0, x1, y1 = frame_box
                return (x0 - tol <= x <= x1 + tol and
                        y0 - tol <= y <= y1 + tol and
                        min(abs(x - x0), abs(x - x1),
                            abs(y - y0), abs(y - y1)) <= 3.0 * tol)
            if all(on_frame_edge(pt) for pt in cap_seg):
                return True, {"frame_cap": True}
        ls, rs = left.get("segment") or left.get("axis"), \
            right.get("segment") or right.get("axis")
        if not ls or not rs:
            return bool(lb and rb and _box_gap(lb, rb) <= tol), {}
        d = min(_len(ls[0], rs[0]), _len(ls[0], rs[1]),
                _len(ls[1], rs[0]), _len(ls[1], rs[1]))
        return d <= max(tol, 3.0 * tol), {"min_endpoint_dist": _r(d)}
    if name in ("parallel_with", "symmetric_about"):
        la, ra = left.get("angle"), right.get("angle")
        if la is None or ra is None:
            ok = bool(lb and rb and _box_gap(lb, rb) <= tol)
            return ok, {}
        return _angle_diff(la, ra) <= 2.0, {"angle_diff": _r(_angle_diff(la, ra))}
    if name in ("aligned_with", "adjacent_to"):
        # 贴墙：目标构件的包围盒与某道墙的轴线足够近
        best = None
        for wall in walls or ():
            box = wall.get("bbox") or _union([
                [wall["start"][0], wall["start"][1], wall["start"][0], wall["start"][1]],
                [wall["end"][0], wall["end"][1], wall["end"][0], wall["end"][1]]])
            if not box or not lb:
                continue
            gap = _box_gap(lb, box)
            if best is None or gap < best:
                best = gap
        if best is None:
            return name == "adjacent_to", {"no_wall": True}
        return best <= max(tol, float(walls[0].get("thickness") or 0.0) if walls else tol), \
            {"wall_gap": _r(best)}
    if name == "repeats_along":
        return bool(lb and rb and _box_gap(lb, rb) <= 2.0 * tol), {}
    return False, {"unsupported": name}


# --------------------------------------------------------------------- 拟合主流程
def _candidate_id(category: str, uids, bbox) -> str:
    raw = category + "|" + ",".join(sorted(str(u) for u in uids)) + "|" + \
        ",".join(str(_r(v)) for v in (bbox or ()))
    return "hyp:" + _sha(raw)[:12]


def _geometry_for(grammar: dict, bound: dict, *, category: str) -> dict:
    """按类别把已绑定的部分合成几何（坐标只从这里出来）。"""
    main = None
    for part in (grammar.get("parts") or {}):
        if part in bound:
            main = bound[part]
            break
    geom: dict = {"bbox": (main or {}).get("bbox")}
    if category in ("window", "door"):
        axis = (main or {}).get("axis")
        geom["axis"] = axis
        geom["outline"] = [list(map(float, axis[0])), list(map(float, axis[1]))] \
            if axis else None
        if (main or {}).get("gap"):
            geom["thickness_mm"] = _r(main["gap"])
        if axis:
            geom["width_mm"] = _r(_len(axis[0], axis[1]))
        if (main or {}).get("count"):
            geom["panes"] = int(main["count"])
    elif category == "column":
        if (main or {}).get("outline"):
            geom["outline"] = main["outline"]
        for key in ("w", "h", "area"):
            if (main or {}).get(key) is not None:
                geom[f"{key}_mm"] = (main or {})[key]
        if (main or {}).get("solid"):
            geom["solid"] = True
    elif category == "wall":
        axis = (main or {}).get("axis")
        geom["axis"] = axis
        if (main or {}).get("gap"):
            geom["thickness_mm"] = _r(main["gap"])
        if axis:
            geom["length_mm"] = _r(_len(axis[0], axis[1]))
    else:
        axis = (main or {}).get("axis")
        if axis:
            geom["axis"] = axis
        if (main or {}).get("spacing"):
            geom["spacing_mm"] = _r(main["spacing"])
        if (main or {}).get("count"):
            geom["count"] = int(main["count"])
    return geom


def fit(grammar: dict, ir: dict, graph=None, *, region=None, seed_uids=(),
        walls=(), faces=(), tol: float | None = None,
        source: str = "grammar", limit: int | None = None) -> dict:
    """把语法兑现成候选几何。

    返回 ``{"schema", "candidates": [hypothesis...], "trace", "status"}``。
    ``status``：``ok`` / ``no_part``（原语一个都没匹配上）/ ``no_relation``
    （匹配上了但关系不成立）/ ``empty_region``。
    """
    from . import cad_ir as IR
    from . import component_grammar as G

    try:
        grammar = grammar if grammar.get("hash") else G.validate(grammar)
    except G.GrammarError as exc:
        return {"schema": SCHEMA, "status": "invalid_grammar",
                "candidates": [], "errors": [exc.as_dict()], "trace": []}

    basis = ir.get("basis") or {}
    thickness = float(basis.get("thickness") or 200.0)
    noise = float(basis.get("noise") or 2.0)
    tol = float(tol if tol is not None else max(noise * 2.0, thickness * 0.02))

    scope = IR.in_region(ir, region) if region else list(ir.get("entities") or ())
    if not scope:
        return {"schema": SCHEMA, "status": "empty_region", "candidates": [],
                "trace": []}
    scope_uids = {e["uid"] for e in scope}
    seeds = [u for u in (seed_uids or ()) if u in scope_uids]

    parts = dict(grammar.get("parts") or {})
    matches: dict[str, list] = {}
    trace: list[dict] = []
    for name, prim in parts.items():
        matcher = MATCHERS.get(prim)
        if matcher is None:
            matches[name] = []
            trace.append({"part": name, "primitive": prim, "status": "NO_MATCHER"})
            continue
        rows = matcher(scope, thickness=thickness, tol=tol, seed_uids=seeds)
        matches[name] = rows[:int(BUDGET["seeds_per_part"])]
        trace.append({"part": name, "primitive": prim, "matched": len(matches[name])})
    if not any(matches.values()):
        return {"schema": SCHEMA, "status": "no_part", "candidates": [],
                "trace": trace}

    # 以"第一个匹配到的部分"为骨架，按关系往里绑其它部分
    order = [n for n in parts if matches.get(n)]
    basis_name = order[0]
    candidates: list[dict] = []
    hard = {"inside", "connect", "enclose"}
    for seed in matches[basis_name][:int(BUDGET["seeds_per_part"])]:
        bound: dict[str, dict] = {basis_name: seed}
        satisfied: list[list] = []
        failed: list[list] = []
        for rel in grammar.get("relations") or ():
            left, name, right = rel
            if left == basis_name and right in matches:
                target, other = right, left
            elif right == basis_name and left in matches:
                target, other = left, right
            else:
                continue
            # 找满足关系的部分实例
            best = None
            for cand in matches[target]:
                ok, ev = check_relation(name, cand if target != basis_name else seed,
                                        bound[other], tol=tol, walls=walls, faces=faces)
                if ok:
                    best = cand
                    break
            if best is not None:
                bound[target] = best
                satisfied.append([other, name, target])
            else:
                failed.append([other, name, target])
                if name in hard:
                    break
        if any(rel[1] in hard for rel in failed):
            continue
        uids = []
        for part_name, shape in bound.items():
            uids.extend(shape.get("uids") or ())
        uids = sorted(set(uids))
        if not uids:
            continue
        geom = _geometry_for(grammar, bound, category=grammar["category"])
        bbox = geom.get("bbox") or _union(
            [s.get("bbox") for s in bound.values()])
        hypothesis = {
            "schema": HYP_SCHEMA,
            "id": _candidate_id(grammar["category"], uids, bbox),
            "category": grammar["category"], "subtype": grammar.get("subtype"),
            "source": str(source), "engine": "geometry_fitter",
            "grammar": {"hash": grammar.get("hash"),
                        "template": grammar.get("template"),
                        "parts": dict(parts),
                        "relations": [list(r) for r in grammar.get("relations") or ()]},
            "region": (region or {}).get("id") if isinstance(region, dict) else None,
            "bbox": bbox, "geometry": geom,
            "source_handles": [], "uids": uids,
            "parts": {k: {"prim": v.get("prim"), "uids": list(v.get("uids") or ())}
                      for k, v in bound.items()},
            "evidence": {"parts_bound": len(bound), "parts_total": len(parts),
                         "relations_satisfied": len(satisfied),
                         "relations_failed": len(failed),
                         "wall_anchor": any(r[1] in ("aligned_with", "adjacent_to")
                                            for r in satisfied)},
            "score": 0.0, "status": "candidate",
        }
        candidates.append(hypothesis)
        if len(candidates) >= int(BUDGET["candidates"] if limit is None else limit):
            break

    # 打分：绑定的部分越多、关系满足越多、越贴墙，分越高（只用于排序）
    for row in candidates:
        ev = row["evidence"]
        parts_ratio = ev["parts_bound"] / max(1, ev["parts_total"])
        rel_ratio = (ev["relations_satisfied"]
                     / max(1, ev["relations_satisfied"] + ev["relations_failed"]))
        row["score"] = _r(0.55 * parts_ratio + 0.30 * rel_ratio
                          + (0.15 if ev["wall_anchor"] else 0.0), 3)
    candidates.sort(key=lambda c: -float(c["score"]))

    status = "ok" if candidates else "no_relation"
    if candidates and grammar.get("instances") == "find_similar":
        candidates = _find_similar(candidates, matches, grammar, ir, region=region)
    return {"schema": SCHEMA, "status": status, "grammar": grammar.get("hash"),
            "candidates": candidates, "trace": trace,
            "stats": {"candidates": len(candidates),
                      "primitives": {k: len(v) for k, v in matches.items()}}}


def _find_similar(candidates, matches, grammar, ir, *, region=None) -> list:
    """同族实例：把第一个候选的"局部形状签名"在其余匹配里重复找到。"""
    if not candidates:
        return candidates
    head = candidates[0]
    signature = _local_signature(head)
    out = [head]
    seen = {head["id"]}
    for row in candidates[1:]:
        if _local_signature(row) != signature:
            continue
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        out.append(row)
        if len(out) >= int(BUDGET["instances"]):
            break
    return out


def _local_signature(hyp: dict) -> str:
    """局部形状签名：只保留相对关系（部分数量/关系数量/类别），不含绝对坐标。"""
    geom = hyp.get("geometry") or {}
    body = {"cat": hyp.get("category"), "sub": hyp.get("subtype"),
            "parts": len(hyp.get("parts") or {}),
            "rels": len((hyp.get("grammar") or {}).get("relations") or ()),
            "panes": geom.get("panes"), "solid": geom.get("solid")}
    return _sha(json_dumps(body))[:12]


def json_dumps(obj) -> str:
    import json as _json
    return _json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


def summary(result) -> dict:
    result = result or {}
    return {"schema": SCHEMA, "status": result.get("status"),
            "grammar": result.get("grammar"),
            "candidates": len(result.get("candidates") or ()),
            "stats": result.get("stats") or {},
            "top": [{"id": c.get("id"), "category": c.get("category"),
                     "score": c.get("score"), "parts": len(c.get("parts") or {})}
                    for c in (result.get("candidates") or ())[:5]]}
