"""候选生成引擎：规则、图形模式、AI 三条路**并行**往同一个池子里投候选。

这是这次重构最核心的一点变化：以前"AI 的任务范围"由第一轮规则识别决定（只
能补救规则漏掉的东西），现在三条路**各自独立**出候选，谁都可以说"这块我有个
解释"，最后由 :mod:`global_resolver` 统一裁决。

三条路的分工与权限：

============= ============================================ ==================
引擎          怎么产生候选                                  能不能碰几何
============= ============================================ ==================
``rule``      现有识别器（墙/柱/门窗）的结果，转成统一候选    是（原样搬过来）
``pattern``   残余聚类 + 内置构件语法，由几何拟合器兑现      是（拟合器算坐标）
``ai``        模型提出**语法**（不是坐标），再由拟合器兑现    只能提语法
============= ============================================ ==================

产物是同一种东西（``hypothesis/1.0``），因此后面可以放进同一张冲突图里联合求解。
"""
from __future__ import annotations

import hashlib
import math

from . import component_grammar as G
from . import geometry_fitter as FIT

HYP_SCHEMA = FIT.HYP_SCHEMA
SCHEMA = "hypothesis-pool/1.0"

SOURCES = ("rule", "pattern", "ai")

#: 逻辑预算（个）
BUDGET = {"rule_candidates": 4000, "pattern_candidates": 600, "ai_candidates": 240,
          "dedupe_ratio": 0.85}

#: 规则引擎从旧识别器拿到的"证据强度" -> 候选先验分
RULE_SCORE = {"strong": 0.90, "weak": 0.55, "pending": 0.45}


def _r(v, nd: int = 1) -> float:
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return 0.0


def _sha(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _union(boxes):
    rows = [b for b in (boxes or ()) if b]
    if not rows:
        return None
    return [_r(min(b[0] for b in rows)), _r(min(b[1] for b in rows)),
            _r(max(b[2] for b in rows)), _r(max(b[3] for b in rows))]


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


def _bbox_of_record(rec: dict):
    box = rec.get("bbox")
    if isinstance(box, (list, tuple)) and len(box) == 4:
        return [float(v) for v in box]
    pts = []
    for key in ("start", "end", "center", "insert"):
        pt = rec.get(key)
        if isinstance(pt, (list, tuple)) and len(pt) >= 2:
            pts.append([float(pt[0]), float(pt[1])])
    for key in ("outline", "points", "axis", "opening"):
        seq = rec.get(key)
        if isinstance(seq, (list, tuple)):
            for pt in seq:
                if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                    pts.append([float(pt[0]), float(pt[1])])
    return _union([[p[0], p[1], p[0], p[1]] for p in pts])


def hypothesis(*, category, subtype, source, engine, region, bbox, geometry,
               uids=(), handles=(), score=0.0, evidence=None, grammar=None,
               status="candidate", instances=None) -> dict:
    """统一的候选结构（所有引擎都必须走这里，字段才一致）。"""
    body = {"c": category, "b": [_r(v) for v in (bbox or ())],
            "u": sorted(str(u) for u in (uids or ())),
            "s": source}
    return {"schema": HYP_SCHEMA, "id": "hyp:" + _sha(_dumps(body))[:12],
            "category": category, "subtype": subtype, "source": source,
            "engine": engine, "region": region, "bbox": bbox,
            "geometry": dict(geometry or {}),
            "uids": [str(u) for u in (uids or ())],
            "source_handles": [str(h) for h in (handles or ())],
            "evidence": dict(evidence or {}), "grammar": grammar,
            "instances": list(instances or ()), "score": _r(score, 3),
            "status": status}


def _dumps(obj) -> str:
    import json
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


# --------------------------------------------------------------------- 规则引擎
def _uids_for(ir: dict, handles) -> list[str]:
    out: list[str] = []
    index = (ir.get("index") or {}).get("handle_to_uid") or {}
    for handle in handles or ():
        for uid in index.get(str(handle), ()):
            if uid not in out:
                out.append(uid)
    return out


def rule_candidates(ir: dict, *, walls=(), columns=(), doors=(), windows=(),
                    stairs=(), steps=(), aprons=(), frames=(), grid=None,
                    rooms=(), limit: int | None = None) -> dict:
    """现有识别器的结果 -> 统一候选（不改判据，只换表达）。

    覆盖 11 类：墙/柱/门/窗/楼梯/台阶/散水/图框/轴网/房间（+ 门联窗走门窗）。
    楼梯与台阶在 v1 里是**两个节点**（``stairs`` 与 ``steps``/``entrance_steps``），
    这里合并成同一类别，来源记在 ``evidence.rule`` 上，不丢出处。
    """
    out: list[dict] = []
    for wall in walls or ():
        if not isinstance(wall, dict):
            continue
        bbox = _bbox_of_record(wall)
        axis = None
        if wall.get("start") and wall.get("end"):
            axis = [[_r(wall["start"][0]), _r(wall["start"][1])],
                    [_r(wall["end"][0]), _r(wall["end"][1])]]
        handles = [str(h) for h in (wall.get("handles") or ())]
        out.append(hypothesis(
            category="wall", subtype=wall.get("kind") or "double_line_wall",
            source="rule", engine="rule.walls",
            region=None, bbox=bbox,
            geometry={"axis": axis,
                      "thickness_mm": _r(wall.get("thickness")),
                      "length_mm": _r(wall.get("length")
                                      or (wall.get("attributes") or {}).get("length_mm"))},
            uids=_uids_for(ir, handles), handles=handles,
            score=RULE_SCORE.get(str(wall.get("evidence_state") or "strong"), 0.6),
            evidence={"evidence_state": wall.get("evidence_state"),
                      "rule": "walls_seg"}))
    for column in columns or ():
        if not isinstance(column, dict):
            continue
        handles = [str(h) for h in (column.get("handles") or ())]
        out.append(hypothesis(
            category="column", subtype=column.get("shape") or "rect_column",
            source="rule", engine="rule.columns", region=None,
            bbox=_bbox_of_record(column),
            geometry={"outline": column.get("outline"),
                      "w_mm": _r(column.get("w")),
                      "h_mm": _r(column.get("h"))},
            uids=_uids_for(ir, handles), handles=handles,
            score=RULE_SCORE.get(str(column.get("evidence_state") or "strong"), 0.6),
            evidence={"evidence_state": column.get("evidence_state"),
                      "rule": "columns"}))
    for bucket, category in ((doors, "door"), (windows, "window")):
        for rec in bucket or ():
            if not isinstance(rec, dict):
                continue
            handles = [str(h) for h in (rec.get("handles") or ())]
            axis = rec.get("axis") or rec.get("opening")
            out.append(hypothesis(
                category=category, subtype=str(rec.get("kind") or category),
                source="rule", engine=f"rule.{category}s", region=None,
                bbox=_bbox_of_record(rec),
                geometry={"axis": axis,
                          "width_mm": _r(rec.get("width")
                                         or (rec.get("attributes") or {}).get("width_mm")),
                          "wall_id": rec.get("wall_id"),
                          "host_ok": rec.get("host_ok")},
                uids=_uids_for(ir, handles), handles=handles,
                score=RULE_SCORE.get(str(rec.get("evidence_state") or "strong"), 0.6),
                evidence={"evidence_state": rec.get("evidence_state"),
                          "rule": f"{category}_openings"}))
    # ---- 楼梯 / 台阶 / 散水：都是"成组轮廓"，几何形态不同但表达方式一致
    for rec in stairs or ():
        if not isinstance(rec, dict):
            continue
        attrs = rec.get("attributes") or {}
        handles = [str(h) for h in (rec.get("handles") or ())]
        out.append(hypothesis(
            category="stair", subtype="tread_series", source="rule",
            engine="rule.stairs", region=None, bbox=_bbox_of_record(rec),
            geometry={"tread_count": attrs.get("tread_count"),
                      "spacing_mm": _r(attrs.get("tread_spacing_mm")),
                      "axis": rec.get("axis")},
            uids=_uids_for(ir, handles), handles=handles,
            score=RULE_SCORE.get(str(rec.get("evidence_state") or "strong"), 0.7),
            evidence={"rule": "stairs", "treads": attrs.get("tread_count")}))
    for rec in steps or ():
        if not isinstance(rec, dict):
            continue
        attrs = rec.get("attributes") or {}
        handles = [str(h) for h in (rec.get("handles") or ())]
        out.append(hypothesis(
            category="step", subtype=str(rec.get("shape") or "nested_u"),
            source="rule", engine="rule.steps", region=None,
            bbox=_bbox_of_record(rec),
            geometry={"count": attrs.get("steps"), "label": attrs.get("label")},
            uids=_uids_for(ir, handles), handles=handles,
            score=RULE_SCORE.get(str(rec.get("evidence_state") or "strong"), 0.7),
            evidence={"rule": "steps"}))
    for rec in aprons or ():
        if not isinstance(rec, dict):
            continue
        attrs = rec.get("attributes") or {}
        handles = [str(h) for h in (rec.get("handles") or ())]
        out.append(hypothesis(
            category="apron", subtype="band", source="rule", engine="rule.apron",
            region=None, bbox=_bbox_of_record(rec),
            geometry={"width_mm": _r(attrs.get("width_mm")),
                      "outline": rec.get("outline")},
            uids=_uids_for(ir, handles), handles=handles,
            score=RULE_SCORE.get(str(rec.get("evidence_state") or "strong"), 0.7),
            evidence={"rule": "apron"}))

    # ---- 图框：一张图通常只有一个"有内容"的图框，按面积给分
    for rec in frames or ():
        if not isinstance(rec, dict):
            continue
        handles = [str(h) for h in (rec.get("handles") or ())]
        box = _bbox_of_record(rec)
        out.append(hypothesis(
            category="frame", subtype="closed_polyline", source="rule",
            engine="rule.frame", region=None, bbox=box,
            geometry={"outer": rec.get("outer"), "inner": rec.get("inner"),
                      "width_mm": _r(rec.get("width")), "height_mm": _r(rec.get("height"))},
            uids=_uids_for(ir, handles), handles=handles, score=0.9,
            evidence={"rule": "frame"}))

    # ---- 轴网：轴线与交点都算 grid_axis 的两个 subtype
    for rec in ((grid or {}).get("axes") or ()):
        if not isinstance(rec, dict):
            continue
        handles = [str(h) for h in (rec.get("handles") or ())]
        out.append(hypothesis(
            category="grid_axis", subtype="line_with_numbering", source="rule",
            engine="rule.grid", region=None, bbox=_bbox_of_record(rec),
            geometry={"axis": rec.get("axis"), "label": rec.get("label"),
                      "orient": rec.get("orient")},
            uids=_uids_for(ir, handles), handles=handles, score=0.85,
            evidence={"rule": "grid.axes"}))
    for rec in ((grid or {}).get("intersections") or ()):
        if not isinstance(rec, dict):
            continue
        box = _bbox_of_record(rec)
        out.append(hypothesis(
            category="grid_axis", subtype="intersection", source="rule",
            engine="rule.grid", region=None, bbox=box,
            geometry={"point": rec.get("point"), "labels": rec.get("labels")},
            uids=_uids_for(ir, [str(rec.get("handle") or "")]), handles=[],
            score=0.8, evidence={"rule": "grid.intersections"}))

    # ---- 房间：由 space_solver 从**选中的墙网**重算，不从墙反推
    for hyp in rooms or ():
        if isinstance(hyp, dict) and hyp.get("schema") == HYP_SCHEMA:
            out.append(hyp)

    if limit:
        out = out[:int(limit)]
    by_cat: dict[str, int] = {}
    for hyp in out:
        by_cat[hyp["category"]] = by_cat.get(hyp["category"], 0) + 1
    return {"source": "rule", "status": "ok", "candidates": out,
            "stats": {"candidates": len(out), "by_category": by_cat}}


# --------------------------------------------------------------------- 模式引擎
def pattern_candidates(ir: dict, graph=None, *, pattern_cases=(), walls=(),
                       faces=(), limit: int | None = None) -> dict:
    """残余模式 -> 用内置语法拟合出几何候选（不依赖模型）。"""
    out: list[dict] = []
    trace: list[dict] = []
    for case in pattern_cases or ():
        if not isinstance(case, dict):
            continue
        kind = str(case.get("kind") or "")
        grammar = G.for_kind(kind) or G.for_category("column")
        if not grammar:
            trace.append({"case": case.get("id"), "status": "NO_TEMPLATE"})
            continue
        region = {"id": case.get("region_id"), "bbox": case.get("region")
                  or case.get("bbox")}
        seeds = _uids_for(ir, case.get("source_handles"))
        # 块族案例：种子用实例内部几何（块展开后的图元，handle 形如 2A3/1F）
        if not seeds:
            seeds = _expand_block_seeds(ir, case.get("source_handles") or ())
        result = FIT.fit(grammar, ir, graph, region=region, seed_uids=seeds,
                         walls=walls, faces=faces, source="pattern",
                         limit=(limit or BUDGET["pattern_candidates"]))
        trace.append({"case": case.get("id"), "kind": kind,
                      "template": grammar.get("template"),
                      "status": result.get("status"),
                      "candidates": len(result.get("candidates") or ())})
        for hyp in result.get("candidates") or ():
            hyp["case_id"] = case.get("id")
            hyp["pattern_kind"] = kind
            out.append(hyp)
        if limit and len(out) >= int(limit):
            break
    return {"source": "pattern", "status": "ok" if out else "no_candidate",
            "candidates": out, "trace": trace,
            "stats": {"candidates": len(out), "cases": len(pattern_cases or ())}}


def _expand_block_seeds(ir: dict, handles) -> list[str]:
    """块实例 handle -> 块内展开几何的 uid（``H/child`` 前缀匹配）。"""
    index = (ir.get("index") or {}).get("handle_to_uid") or {}
    out: list[str] = []
    for handle in handles or ():
        prefix = str(handle) + "/"
        for known, uids in index.items():
            if known.startswith(prefix):
                out.extend(uids)
    return sorted(set(out))


# --------------------------------------------------------------------- AI 引擎
def ai_candidates(ir: dict, graph=None, *, grammars=(), walls=(), faces=(),
                  region=None, seed_uids=(), case_id=None,
                  limit: int | None = None) -> dict:
    """模型提的语法 -> 拟合器兑现（模型不参与任何坐标计算）。"""
    out: list[dict] = []
    trace: list[dict] = []
    for raw in grammars or ():
        try:
            grammar = G.from_model(raw)
        except G.GrammarError as exc:
            trace.append({"status": "invalid_grammar", "error": exc.as_dict()})
            continue
        result = FIT.fit(grammar, ir, graph, region=region, seed_uids=seed_uids,
                         walls=walls, faces=faces, source="ai",
                         limit=(limit or BUDGET["ai_candidates"]))
        trace.append({"grammar": grammar.get("hash"),
                      "template": grammar.get("template"),
                      "status": result.get("status"),
                      "candidates": len(result.get("candidates") or ())})
        for hyp in result.get("candidates") or ():
            hyp["case_id"] = case_id
            hyp["ai_grammar"] = grammar.get("hash")
            out.append(hyp)
    return {"source": "ai", "status": "ok" if out else "no_candidate",
            "candidates": out, "trace": trace,
            "stats": {"candidates": len(out), "grammars": len(grammars or ())}}


# --------------------------------------------------------------------- 合并去重
def dedupe(hypotheses, *, ratio: float | None = None) -> tuple[list[dict], int]:
    """同一构件被多个引擎提出来时只留一个（分高的），其余记成``alternatives``。

    **不去重跨类别的竞争解释**：一扇窗和一道墙占同一块地方，那是冲突图的活，
    不是去重的活——去重只处理"同一个东西说了两遍"。
    """
    ratio = float(BUDGET["dedupe_ratio"] if ratio is None else ratio)
    kept: list[dict] = []
    dropped = 0
    for hyp in sorted(hypotheses or (), key=lambda h: -float(h.get("score") or 0.0)):
        same = None
        for row in kept:
            if row.get("category") != hyp.get("category"):
                continue
            if _iou(row.get("bbox"), hyp.get("bbox")) >= ratio:
                same = row
                break
        if same is None:
            kept.append(hyp)
        else:
            alt = list(same.get("alternatives") or ())
            alt.append({"id": hyp.get("id"), "source": hyp.get("source"),
                        "score": hyp.get("score"),
                        "uids": hyp.get("uids")})
            same["alternatives"] = alt
            dropped += 1
    return kept, dropped


def generate(ir: dict, graph=None, *, plan=None, walls=(), columns=(), doors=(),
             windows=(), stairs=(), steps=(), aprons=(), frames=(), grid=None,
             rooms=(), pattern_cases=(), ai_grammars=(), faces=(),
             regions=(), budget=None) -> dict:
    """三个引擎一起出候选，合成一个池子。

    ``ai_grammars`` 由 :mod:`agent_controller` 向模型要来（每条带 ``region`` /
    ``seed_uids`` / ``case_id``），这里只负责拟合与合并。
    """
    budget = dict(BUDGET, **(budget or {}))
    engines: dict[str, dict] = {}

    rule = rule_candidates(ir, walls=walls, columns=columns, doors=doors,
                           windows=windows, stairs=stairs, steps=steps,
                           aprons=aprons, frames=frames, grid=grid, rooms=rooms,
                           limit=budget["rule_candidates"])
    engines["rule"] = {"stats": rule["stats"], "status": rule["status"]}

    pattern = pattern_candidates(ir, graph, pattern_cases=pattern_cases,
                                 walls=walls, faces=faces,
                                 limit=budget["pattern_candidates"])
    engines["pattern"] = {"stats": pattern["stats"], "status": pattern["status"],
                          "trace": pattern["trace"][:12]}

    ai = {"candidates": [], "stats": {"candidates": 0}, "status": "off",
          "trace": []}
    if ai_grammars:
        ai_rows: list[dict] = []
        trace: list[dict] = []
        for item in ai_grammars:
            res = ai_candidates(ir, graph, grammars=item.get("grammars") or (),
                                walls=walls, faces=faces,
                                region=item.get("region"),
                                seed_uids=item.get("seed_uids") or (),
                                case_id=item.get("case_id"),
                                limit=budget["ai_candidates"])
            ai_rows.extend(res["candidates"])
            trace.extend(res["trace"])
        ai = {"candidates": ai_rows, "trace": trace, "status": "ok",
              "stats": {"candidates": len(ai_rows), "cases": len(ai_grammars)}}
    engines["ai"] = {"stats": ai["stats"], "status": ai["status"],
                     "trace": ai["trace"][:12]}

    pooled = list(rule["candidates"]) + list(pattern["candidates"]) + list(
        ai["candidates"])
    kept, dropped = dedupe(pooled)
    by_source: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for hyp in kept:
        by_source[hyp["source"]] = by_source.get(hyp["source"], 0) + 1
        by_category[hyp["category"]] = by_category.get(hyp["category"], 0) + 1
    return {"schema": SCHEMA, "hypotheses": kept, "engines": engines,
            "stats": {"pooled": len(pooled), "kept": len(kept),
                      "deduped": dropped, "by_source": by_source,
                      "by_category": by_category},
            "budget": budget}


def summary(pool) -> dict:
    pool = pool or {}
    return {"schema": SCHEMA, "stats": pool.get("stats") or {},
            "engines": {k: v.get("stats") for k, v in (pool.get("engines") or {}).items()}}


def by_category(pool, category: str) -> list[dict]:
    return [h for h in (pool.get("hypotheses") or ())
            if h.get("category") == category]
