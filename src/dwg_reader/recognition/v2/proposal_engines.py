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

SOURCES = ("rule", "pattern", "ai", "propagation")

#: 逻辑预算（个）
BUDGET = {"rule_candidates": 4000, "pattern_candidates": 600, "ai_candidates": 240,
          "propagation_candidates": 800, "dedupe_ratio": 0.85}

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


#: 退化判定的容差（相对值，绝对毫米数在这里没有意义）
BOX_EPS = 1e-6
#: 实体并集相对"构件本身占地"的最大允许倍数（超过就是膨胀，不能当占地）
BBOX_GROWTH_MAX = 3.0


def _degenerate(box) -> bool:
    """bbox 是否退化（缺值、非有限数、面积为 0）。"""
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return True
    try:
        x0, y0, x1, y1 = (float(v) for v in box)
    except (TypeError, ValueError):
        return True
    if any(v != v or v in (float("inf"), float("-inf")) for v in (x0, y0, x1, y1)):
        return True
    return (x1 - x0) <= BOX_EPS or (y1 - y0) <= BOX_EPS


def _area(box) -> float:
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return 0.0
    return max(0.0, float(box[2]) - float(box[0])) * max(0.0, float(box[3]) - float(box[1]))


def _axis_of(rec: dict):
    """记录里的轴线：``axis`` / ``opening``，**或者 start+end**（墙只有端点）。"""
    axis = rec.get("axis") or rec.get("opening")
    if isinstance(axis, (list, tuple)) and len(axis) >= 2:
        return axis
    a, b = rec.get("start"), rec.get("end")
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return [a, b]
    line = rec.get("line")
    if isinstance(line, (list, tuple)) and len(line) >= 2:
        return line
    return None


def _box_from_axis(axis, thickness: float):
    """由"轴线 + 厚度"构造矩形（沿法向各扩半个厚度）。"""
    if not isinstance(axis, (list, tuple)) or len(axis) < 2:
        return None
    a, b = axis[0], axis[1]
    try:
        ax, ay = float(a[0]), float(a[1])
        bx, by = float(b[0]), float(b[1])
        t = float(thickness or 0.0)
    except (TypeError, ValueError, IndexError):
        return None
    length = math.hypot(bx - ax, by - ay)
    if length <= 0 or t <= 0:
        return None
    nx, ny = -(by - ay) / length * (t / 2.0), (bx - ax) / length * (t / 2.0)
    return _union([[ax + nx, ay + ny, ax + nx, ay + ny],
                   [ax - nx, ay - ny, ax - nx, ay - ny],
                   [bx + nx, by + ny, bx + nx, by + ny],
                   [bx - nx, by - ny, bx - nx, by - ny]])


def _outline_points(rec: dict) -> list:
    for key in ("outline", "outer", "points", "vertices", "pts"):
        seq = rec.get(key)
        if isinstance(seq, (list, tuple)) and len(seq) >= 3:
            pts = []
            for pt in seq:
                if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                    try:
                        pts.append([float(pt[0]), float(pt[1])])
                    except (TypeError, ValueError):
                        return []
            if len(pts) >= 3:
                return pts
    return []


def _bbox_of_record(rec: dict, *, ir: dict | None = None, uids=(), thickness=None,
                    axis=None, prefer: str = "auto"):
    """记录 -> 包围盒，**保证不退化**。返回 ``(box, 来源)``。

    优先级（几何正确性从高到低）：

    1. 真实轮廓顶点（柱、楼梯、图框这类本来就有轮廓的构件）；
    2. ``轴线 + 厚度``：门窗、墙这类"一条线 + 墙厚"的构件，这是唯一正确的矩形，
       ``start/end`` 也会先转成轴线再用；
    3. 图元包围盒并集（有 uid 就拿真实图元算），但**限制外扩幅度**，
       避免门扇开启弧/相邻长线把框撑大；
    4. 记录自带 ``bbox``；
    5. 仍退化则按墙厚沿退化方向最小扩，标明 ``thickness_expanded``；
    6. 实在算不出 → ``(None, "degenerate")``，调用方必须把它记成**未决**，
       不能当正常候选计数。
    """
    t = thickness
    if t is None:
        t = float((rec.get("thickness") or
                   (rec.get("attributes") or {}).get("thickness") or 0.0) or 0.0)
    if not t and ir:
        t = float((ir.get("basis") or {}).get("thickness") or 0.0)

    axis = _axis_of(rec)
    outline = _outline_points(rec)
    record_bbox = None
    if isinstance(rec.get("bbox"), (list, tuple)) and len(rec.get("bbox")) == 4:
        try:
            record_bbox = [float(v) for v in rec.get("bbox")]
        except (TypeError, ValueError):
            record_bbox = None

    if outline:
        box = _union([[p[0], p[1], p[0], p[1]] for p in outline])
        if not _degenerate(box):
            return box, "outline"

    by_axis = _box_from_axis(axis, t)
    if not _degenerate(by_axis):
        # 轴线+厚度就是这类构件真实占地：不再去追"图元并集更大就更好"
        return by_axis, "axis_plus_thickness"

    entities_box = None
    if ir and uids:
        by_uid = {e["uid"]: e for e in (ir.get("entities") or ())}
        entities_box = _union([by_uid[u].get("bbox") for u in uids if u in by_uid])
        if not _degenerate(entities_box):
            ref = record_bbox if not _degenerate(record_bbox) else None
            if ref and _area(ref) > 0:
                ratio = _area(entities_box) / _area(ref)
                if ratio > 4.0:            # 图元把框撑大了：不采信
                    entities_box = None
            if entities_box is not None:
                return entities_box, "entities"

    if not _degenerate(record_bbox):
        return record_bbox, "record_bbox"

    if record_bbox is not None and t:
        x0, y0, x1, y1 = record_bbox
        box = list(record_bbox)
        if (x1 - x0) <= BOX_EPS:
            box = [x0 - t / 2.0, y0, x1 + t / 2.0, y1]
        if (y1 - y0) <= BOX_EPS:
            box = [box[0], y0 - t / 2.0, box[2], y1 + t / 2.0]
        if not _degenerate(box):
            return [_r(v) for v in box], "thickness_expanded"
    return None, "degenerate"


def _drop_bboxless(rows, *, unresolved=None) -> list:
    """没有可用包围盒的候选一律不入池：宁缺勿错，别让 None 流进几何比较。"""
    out = []
    for hyp in rows or ():
        box = hyp.get("bbox")
        ok = (isinstance(box, (list, tuple)) and len(box) == 4
              and all(isinstance(v, (int, float)) for v in box)
              and (float(box[2]) - float(box[0])) > 0
              and (float(box[3]) - float(box[1])) > 0)
        if ok:
            out.append(hyp)
        elif unresolved is not None:
            unresolved.append({"category": hyp.get("category"),
                               "rule": (hyp.get("evidence") or {}).get("rule"),
                               "handles": list(hyp.get("handles") or ()),
                               "why": "DEGENERATE_BBOX"})
    return out


def _v1_status(rec: dict, *, default: str = "candidate") -> str:
    """把 v1 的状态原样带过来：pending（待核）不能被当成正常候选。

    以前这里丢掉了 status，于是 v1 明确判"待核"的柱到了 v2 变成普通候选，
    悄无声息地进了计数。
    """
    status = str((rec or {}).get("status") or "").strip().lower()
    return status or default


def _invalid_box_reason(box):
    """占地是否可用于几何判断/计数：必须是有限数、有序、正尺寸。"""
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return "MISSING_BBOX"
    try:
        x0, y0, x1, y1 = (float(v) for v in box)
    except (TypeError, ValueError):
        return "NON_NUMERIC"
    if not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
        return "NOT_FINITE"
    if x1 <= x0 or y1 <= y0:
        return "ZERO_AREA"
    return None


def hypothesis(*, category, subtype, source, engine, region, bbox, geometry,
               uids=(), handles=(), score=0.0, evidence=None, grammar=None,
               status="candidate", instances=None, family_id=None,
               transform=None) -> dict:
    """统一的候选结构（实现在 :func:`geometry_fitter.hypothesis`，这里只做转发）。"""
    return FIT.hypothesis(
        category=category, subtype=subtype, source=source, engine=engine,
        region=region, bbox=bbox, geometry=geometry, uids=uids, handles=handles,
        score=score, evidence=evidence, grammar=grammar, status=status,
        instances=instances, family_id=family_id, transform=transform)


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
    unresolved: list[dict] = []

    def _note_degenerate(rec, category, source_label):
        """几何算不出来的记录**不入池**：宁可记未决，也不能拿没有位置的候选计数。"""
        unresolved.append({"category": category, "rule": source_label,
                           "handles": [str(h) for h in (rec.get("handles") or ())]
                           or ([str(rec.get("handle"))] if rec.get("handle") else []),
                           "why": "DEGENERATE_BBOX"})

    for wall in walls or ():
        if not isinstance(wall, dict):
            continue
        bbox, bbox_source = _bbox_of_record(
            wall, ir=ir, uids=_uids_for(ir, wall.get("handles") or ()),
            thickness=wall.get("thickness"))
        if bbox is None:
            _note_degenerate(wall, "wall", "walls_seg")
            continue
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
            status=_v1_status(wall),
            evidence={"evidence_state": wall.get("evidence_state"),
                      "v1_status": wall.get("status"),
                      "bbox_source": bbox_source, "rule": "walls_seg"}))
    for column in columns or ():
        if not isinstance(column, dict):
            continue
        # 柱的出处可能全在 srcs 里（块参照/轮廓/填充/线段组合），只读 handles 会
        # 得到空来源：候选变成"没有图元、没有状态、没有证据"的孤儿，既追不了账
        # 也没法去重，还会让弱候选看起来和强候选一样。
        handles = [str(h) for h in (column.get("handles") or ())]
        for src in column.get("srcs") or ():
            if isinstance(src, dict) and src.get("handle"):
                handles.append(str(src["handle"]))
        if column.get("handle"):
            handles.append(str(column["handle"]))
        handles = list(dict.fromkeys(h for h in handles if h))
        uids = _uids_for(ir, handles)
        bbox, bbox_source = _bbox_of_record(column, ir=ir, uids=uids)
        if bbox is None:
            _note_degenerate(column, "column", "columns")
            continue
        scope = str(column.get("layer_scope") or "declared")
        status = _v1_status(column)
        pending_reason = column.get("pending_reason")
        pending_code = (str(pending_reason or "").strip().upper()
                        or "UNKNOWN_PENDING_REASON")
        layers = [str(x["layer"]) for x in (column.get("srcs") or ())
                  if isinstance(x, dict) and x.get("layer") is not None]
        if column.get("layer") is not None:
            layers.append(str(column["layer"]))
        evidence = {"category_confirmed": column.get("category_confirmed") is True,
                    "category_basis": column.get("category_basis"),
                    "geometry_status": column.get("geometry_status"),
                    "geometry_reason": column.get("geometry_reason"),
                    "edges": column.get("edges"),
                    "block_name": column.get("block_name"),
                    "evidence_state": column.get("evidence_state"),
                    "v1_status": column.get("status"),
                    "pending_reason": pending_reason,
                    "v1_reason": pending_reason,
                    "bbox_source": bbox_source, "rule": "columns",
                    "methods": list(column.get("methods") or ()),
                    "layers": list(dict.fromkeys(layers)),
                    "layer_scope": scope,
                    "section_type": column.get("section_type"),
                    "structural": bool(column.get("touches_wall")
                                       or "on_axis" in (column.get("evidence") or ())),
                    "touches_wall": bool(column.get("touches_wall")),
                    "touches_wall_state": column.get("touches_wall_state"),
                    "confidence": column.get("confidence"),
                    "notes": list(column.get("evidence") or ()),
                    "negatives": [{"code": pending_code}]
                                 if status == "pending" else []}
        out.append(hypothesis(
            category="column",
            subtype=str(column.get("section_type") or column.get("shape")
                        or "rect_column"),
            source="rule", engine="rule.columns", region=None,
            bbox=bbox,
            geometry={"outline": column.get("outline"), "bbox": bbox,
                      "w_mm": _r(column.get("w")), "h_mm": _r(column.get("h")),
                      "area_mm2": _r(column.get("area")),
                      "angle_deg": _r(column.get("rotation"), 2),
                      "solid": bool(column.get("area"))},
            uids=uids, handles=handles,
            score=_r(column.get("score") or
                     RULE_SCORE.get(str(column.get("evidence_state") or "strong"), 0.6)),
            status=status if status in ("candidate", "uncertain", "pending")
            else "candidate",
            evidence=evidence))
    for bucket, category in ((doors, "door"), (windows, "window")):
        for rec in bucket or ():
            if not isinstance(rec, dict):
                continue
            handles = [str(h) for h in (rec.get("handles") or ())]
            uids = _uids_for(ir, handles)
            axis = rec.get("axis") or rec.get("opening")
            bbox, bbox_source = _bbox_of_record(rec, ir=ir, uids=uids,
                                                thickness=rec.get("thickness"),
                                                prefer="axis")
            if bbox is None:
                _note_degenerate(rec, category, f"{category}_openings")
                continue
            out.append(hypothesis(
                category=category, subtype=str(rec.get("kind") or category),
                source="rule", engine=f"rule.{category}s", region=None,
                bbox=bbox,
                geometry={"axis": axis,
                          "width_mm": _r(rec.get("width")
                                         or (rec.get("attributes") or {}).get("width_mm")),
                          "wall_id": rec.get("wall_id"),
                          "host_ok": rec.get("host_ok")},
                uids=uids, handles=handles,
                score=RULE_SCORE.get(str(rec.get("evidence_state") or "strong"), 0.6),
                status=_v1_status(rec),
                evidence={"evidence_state": rec.get("evidence_state"),
                          "v1_status": rec.get("status"),
                          "bbox_source": bbox_source,
                          "rule": f"{category}_openings"}))
    # ---- 楼梯 / 台阶 / 散水：都是"成组轮廓"，几何形态不同但表达方式一致
    for rec in stairs or ():
        if not isinstance(rec, dict):
            continue
        attrs = rec.get("attributes") or {}
        handles = [str(h) for h in (rec.get("handles") or ())]
        out.append(hypothesis(
            category="stair", subtype="tread_series", source="rule",
            engine="rule.stairs", region=None,
            bbox=_bbox_of_record(rec, ir=ir,
                                 uids=_uids_for(ir, rec.get("handles") or ()))[0],
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
            bbox=_bbox_of_record(rec, ir=ir,
                                 uids=_uids_for(ir, rec.get("handles") or ()))[0],
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
            region=None,
            bbox=_bbox_of_record(rec, ir=ir,
                                 uids=_uids_for(ir, rec.get("handles") or ()))[0],
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
        box = _bbox_of_record(rec, ir=ir,
                              uids=_uids_for(ir, rec.get("handles") or ()))[0]
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
            engine="rule.grid", region=None,
            bbox=_bbox_of_record(rec, ir=ir,
                                 uids=_uids_for(ir, rec.get("handles") or ()))[0],
            geometry={"axis": rec.get("axis"), "label": rec.get("label"),
                      "orient": rec.get("orient")},
            uids=_uids_for(ir, handles), handles=handles, score=0.85,
            evidence={"rule": "grid.axes"}))
    for rec in ((grid or {}).get("intersections") or ()):
        if not isinstance(rec, dict):
            continue
        box = _bbox_of_record(rec, ir=ir,
                              uids=_uids_for(ir, [str(rec.get("handle") or "")]))[0]
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

    out = _drop_bboxless(out, unresolved=unresolved)
    if limit:
        out = out[:int(limit)]
    by_cat: dict[str, int] = {}
    for hyp in out:
        by_cat[hyp["category"]] = by_cat.get(hyp["category"], 0) + 1
    return {"source": "rule", "status": "ok", "candidates": out,
            "unresolved": unresolved,
            "stats": {"candidates": len(out), "by_category": by_cat,
                      "unresolved": len(unresolved)}}


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


# --------------------------------------------------------------------- 传播引擎
def propagation_candidates(ir: dict, graph=None, *, families=(), walls=(),
                           grid=None, limit: int | None = None,
                           categories=None) -> dict:
    """构件族 -> 全图实例候选（模型只理解一次，程序负责推广）。

    这条引擎是 ``v2-component-focus`` 的主力：族由
    :mod:`component_family` 发现，代表实例由模型判断一次，剩下的实例由
    :mod:`instance_propagator` 用形状签名 + 几何复核找出来。
    """
    from . import instance_propagator as PROP
    wanted = tuple(categories or ("door", "window", "column"))
    rows: list = []
    trace: list = []
    for family in families or ():
        if family.get("verdict") == "reject":
            # 模型已判定"这不是构件"（家具/图例/墙）：不再往全图推广
            trace.append({"family": family.get("id"), "status": "model_rejected",
                          "negatives": family.get("model_negative")})
            continue
        if not family.get("confirmed"):
            # **没有确认过的族不传播**：这是 propose 与 enhance 的共同底线。
            # 模型没看过（或没把握）的族只作为候选留在池子里，不允许把结论推广全图。
            trace.append({"family": family.get("id"), "status": "unconfirmed",
                          "category": family.get("category") or
                          family.get("category_guess")})
            continue
        category = family.get("category") or family.get("category_guess")
        if category not in wanted:
            trace.append({"family": family.get("id"),
                          "status": "category_not_target", "category": category})
            continue
        result = PROP.propagate(family, ir=ir, walls=walls, grid=grid,
                                category=category, limit=limit)
        trace.append({"family": family.get("id"), "category": category,
                      "searched": (result.get("stats") or {}).get("searched"),
                      "fitted": (result.get("stats") or {}).get("fitted"),
                      "status": (result.get("stats") or {}).get("status")})
        rows.extend(result.get("hypotheses") or ())
    by_cat: dict = {}
    for hyp in rows:
        by_cat[hyp["category"]] = by_cat.get(hyp["category"], 0) + 1
    return {"source": "propagation", "status": "ok" if rows else "no_candidate",
            "candidates": rows, "trace": trace,
            "stats": {"candidates": len(rows), "families": len(families or ()),
                      "by_category": by_cat}}


# --------------------------------------------------------------------- 合并去重
def dedupe(hypotheses, *, ratio: float | None = None) -> tuple[list[dict], int]:
    """同一构件被多个引擎提出来时只留一个（分高的），其余记成``alternatives``。

    **不去重跨类别的竞争解释**：一扇窗和一道墙占同一块地方，那是冲突图的活，
    不是去重的活——去重只处理"同一个东西说了两遍"。
    """
    ratio = float(BUDGET["dedupe_ratio"] if ratio is None else ratio)
    kept: list[dict] = []
    dropped = 0
    def rank(h):
        ev = h.get("evidence") or {}
        verified_category = (h.get("category") == "column"
                             and ev.get("category_confirmed") is True
                             and h.get("status") != "pending")
        return (not verified_category, h.get("status") == "pending",
                -float(h.get("score") or 0.0))

    for hyp in sorted(hypotheses or (), key=rank):
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
             regions=(), families=(), propagate: bool = False,
             categories=None, budget=None) -> dict:
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
    engines["rule"] = {"stats": rule["stats"], "status": rule["status"],
                       "unresolved": len(rule.get("unresolved") or ())}

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

    if categories:
        allowed = set(categories)
        rule["candidates"] = [h for h in rule["candidates"]
                              if h.get("category") in allowed]
        pattern["candidates"] = [h for h in pattern["candidates"]
                                 if h.get("category") in allowed]
        ai["candidates"] = [h for h in ai["candidates"]
                            if h.get("category") in allowed]

    propagation = {"candidates": [], "stats": {"candidates": 0},
                   "status": "off", "trace": []}
    if propagate and families:
        prop = propagation_candidates(
            ir, graph, families=families, walls=walls, grid=grid,
            limit=budget["propagation_candidates"], categories=categories)
        propagation = {"candidates": prop["candidates"], "trace": prop["trace"],
                       "status": prop["status"], "stats": prop["stats"]}
    engines["propagation"] = {"stats": propagation["stats"],
                              "status": propagation["status"],
                              "trace": propagation["trace"][:12]}

    pooled = list(rule["candidates"]) + list(pattern["candidates"]) + list(
        ai["candidates"]) + list(propagation["candidates"])
    # 统一入口校验：任何一个引擎（含 AI/传播）产出的候选都必须有**有限、正尺寸**
    # 的占地，否则不进池。以前它们在关系判断里"算不了冲突"，反而畅通无阻地进计数。
    unresolved = list(rule.get("unresolved") or ())
    rejected_members = {}
    for family in families or ():
        if family.get("verdict") != "reject":
            continue
        members = ([family.get("representative") or {}]
                   if family.get("authorization_scope") == "representative_only"
                   else family.get("instances") or ())
        for instance in members:
            key = frozenset(instance.get("uids") or ())
            if key:
                rejected_members[key] = family.get("id")
    validated = []
    for hyp in pooled:
        if hyp.get("category") == "column":
            ev = dict(hyp.get("evidence") or {})
            member = frozenset(hyp.get("uids") or ())
            if member and member in rejected_members:
                ev.update(category_confirmed=False, category_basis=None,
                          pending_reason="MODEL_FAMILY_REJECTED",
                          rejected_family=rejected_members[member])
                hyp["status"] = "pending"
            elif ev.get("category_confirmed") is not True:
                ev.setdefault("pending_reason", "CATEGORY_UNVERIFIED")
                hyp["status"] = "pending"
            hyp["evidence"] = ev
        why = _invalid_box_reason(hyp.get("bbox"))
        if why:
            unresolved.append({"category": hyp.get("category"),
                               "engine": hyp.get("engine"),
                               "source": hyp.get("source"),
                               "handles": list(hyp.get("handles") or ()),
                               "why": "INVALID_BBOX", "detail": why})
            continue
        validated.append(hyp)
    kept, dropped = dedupe(validated)
    by_source: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for hyp in kept:
        by_source[hyp["source"]] = by_source.get(hyp["source"], 0) + 1
        by_category[hyp["category"]] = by_category.get(hyp["category"], 0) + 1
    return {"schema": SCHEMA, "hypotheses": kept, "engines": engines,
            "unresolved": unresolved,
            "stats": {"pooled": len(pooled), "kept": len(kept),
                      "deduped": dropped, "by_source": by_source,
                      "by_category": by_category,
                      "rejected_invalid": len(pooled) - len(validated),
                      "unresolved": len(unresolved)},
            "budget": budget}


def summary(pool) -> dict:
    pool = pool or {}
    return {"schema": SCHEMA, "stats": pool.get("stats") or {},
            "engines": {k: v.get("stats") for k, v in (pool.get("engines") or {}).items()}}


def by_category(pool, category: str) -> list[dict]:
    return [h for h in (pool.get("hypotheses") or ())
            if h.get("category") == category]
