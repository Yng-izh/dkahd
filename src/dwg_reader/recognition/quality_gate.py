"""质量闸门：只挑"确定性算法已无法可靠消歧、且存在可执行修复"的问题交给模型。

与旧版的区别：

* 房间名是否孤儿：先做点落面（label -> space face）再统计，不再一律当孤儿；
* 门窗冲突：只看 ``openings_select`` 之后仍成立的冲突，已自动否决的不再触发；
* 每个 case 先用 :mod:`case_builder` 算出几何特征与合法替代方案，
  只有 ``NO_ACTION`` 以外一条都没有时才丢弃（不给模型无解的题）。
"""
from __future__ import annotations

import collections
import math
import os

from . import case_builder as CB

#: 每个区域的案例预算：单张图纸的问题分布是"某片区域一片狼藉，别处很干净"，
#: 固定 6 条的总预算会让修得好的区域继续吃预算，坏区域反而轮不到。
MAX_CASES_PER_REGION = int(os.environ.get("DWG_GATE_MAX_CASES_PER_REGION", "8"))
#: 总预算（跨区域）
MAX_CASES = int(os.environ.get("DWG_GATE_MAX_CASES", "40"))
#: 单个案例数量硬上限（防病态图纸把案例表撑爆）
HARD_CASES = int(os.environ.get("DWG_GATE_HARD_CASES", "400"))
MAX_CALLS = int(os.environ.get("DWG_LLM_MAX_CALLS", "8"))
MAX_ROUNDS = int(os.environ.get("DWG_LLM_MAX_ROUNDS", "2"))
MIN_SUPPORT = 3
MIN_LINES = 6
ORPHAN_RATIO = 0.25


def _label_point(lab):
    """房名坐标：兼容 insert / insert_pt / center / pos / (x,y) 五种写法。"""
    p = lab.get("insert") or lab.get("insert_pt") or lab.get("center") or lab.get("pos")
    if isinstance(p, (list, tuple)) and len(p) >= 2:
        try:
            return float(p[0]), float(p[1])
        except (TypeError, ValueError):
            return None
    x, y = lab.get("x"), lab.get("y")
    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
        return float(x), float(y)
    return None


def _point_in_poly(x, y, poly) -> bool:
    """本地射线法（不依赖外部签名，避免参数顺序写反）。"""
    if not poly or len(poly) < 3:
        return False
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = float(poly[i][0]), float(poly[i][1])
        x2, y2 = float(poly[(i + 1) % n][0]), float(poly[(i + 1) % n][1])
        if (y1 > y) != (y2 > y):
            if x < x1 + (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-9):
                inside = not inside
    return inside


def _bbox_distance(x, y, b) -> float:
    dx = max(b[0] - x, 0.0, x - b[2])
    dy = max(b[1] - y, 0.0, y - b[3])
    return (dx * dx + dy * dy) ** 0.5


OFFSET_TOL = 1500.0
LEAK_TOL = 6000.0


def associate_labels(labels, faces):
    """文字 -> 面域：bound / offset（压在墙线上）/ leak（面域没闭合）/ distant / no_point。"""
    out = []
    for lab in labels or ():
        pt = _label_point(lab)
        if pt is None:
            out.append({"label": lab, "face_id": None, "distance": None,
                        "declared_area": lab.get("area"), "status": "no_point"})
            continue
        x, y = pt
        hit, nearest = None, None
        for f in faces or ():
            b = f.get("bbox")
            if not b:
                continue
            if _point_in_poly(x, y, f.get("polygon") or ()):
                hit = f
                break
            d = _bbox_distance(x, y, b)
            if nearest is None or d < nearest[0]:
                nearest = (d, f)
        if hit is not None:
            status, face = "bound", hit
        elif nearest and nearest[0] <= OFFSET_TOL:
            status, face = "offset", nearest[1]
        elif nearest and nearest[0] <= LEAK_TOL:
            status, face = "leak", None
        else:
            status, face = "distant", None
        weak = lab.get("source") == "fallback"
        out.append({"label": lab, "face_id": (face or {}).get("id"),
                    "distance": None if nearest is None else round(nearest[0], 1),
                    "declared_area": lab.get("area"), "status": status,
                    "confidence": 0.55 if weak else 0.9,
                    "source": lab.get("source") or "profile"})
    return out


#: 各原因码的"预期收益"基权：能恢复空间边界的问题 > 构件漏检 > 语义缺口
BENEFIT_BASE = {
    "ROOM_BOUNDARY_UNRESOLVED": 3.0,
    "DEFECT_": 2.0,
    "OPENING_": 1.5,
    "SEMANTIC_GAP_UNKNOWN_LAYERS": 1.0,
}


def _benefit(case) -> float:
    """预期收益：这个案例修好以后能改善多少可量化的拓扑指标。

    收益只看"能恢复多少独立证据支持的结构"，不看案例数量；带几何冲突的案例
    要扣分——把冲突当收益修出来的东西是自证。
    """
    code = str(case.get("reason") or "")
    detail = case.get("detail") or {}
    b = 0.0
    for key, base in BENEFIT_BASE.items():
        if code.startswith(key):
            b += base
            break
    if code == "ROOM_BOUNDARY_UNRESOLVED":
        labels = max(1, int(detail.get("labels") or 1))
        orphan = int(detail.get("orphan") or 0)
        b += 2.0 * orphan / labels
        if not detail.get("faces"):
            b += 0.5
    if code.startswith("OPENING_"):
        b += 0.2 * int(detail.get("pairs") or 0)
    if "EVIDENCE_WITHOUT_CANDIDATES" in code:
        b += 0.5
    b += 0.3 * len(case.get("alternatives") or ())
    b -= 0.5 * len(detail.get("conflicts") or ())
    return round(b, 3)


def _group_of(case, regions) -> str:
    """案例属于哪个区域（按区域包围盒包含关系，取最小包含区域）。"""
    from .regions import _area, region_of_point
    b = case.get("region")
    if not b:
        return "REGION-UNKNOWN"
    cx, cy = (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0
    rid = region_of_point(regions, [cx, cy])
    if rid:
        return rid
    best = None
    for r in regions or ():
        rb = r.get("bbox")
        if not rb:
            continue
        if rb[0] <= cx <= rb[2] and rb[1] <= cy <= rb[3]:
            if best is None or _area(rb) < _area(best["bbox"]):
                best = r
    return (best or {}).get("id") or "REGION-UNKNOWN"


def check(*, snapshot, plan, prims=None, walls_seg=(), wall_model=None, spaces=None,
          doors=(), windows=(), columns=(), rooms=(), room_labels=(),
          evidence=None, openings_selected=None, ledger=None, defects=None,
          regions=None) -> dict:
    cases: list[dict] = []
    codes: set[str] = set()
    ents = snapshot.get("entities", ()) or ()
    index = evidence or {}
    regions = list(regions or index.get("regions") or [])
    faces = (spaces or {}).get("faces") or []
    sem = {k: list(v or ()) for k, v in (plan.get("layers") or {}).items()}

    def add(code, region_bbox, nodes, ev_ids, detail):
        codes.add(code)
        if len(cases) >= HARD_CASES:
            return
        # Never interchange executable DAG node names and evidence IDs.
        # Reject malformed cases before they can reach the repair planner.
        from . import recompute as RC
        aliases = {"door": "doors", "window": "windows", "column": "columns",
                   "resolve_conflicts": "openings_select"}
        nodes = [aliases.get(str(n), str(n)) for n in (nodes or ())]
        known = set(RC.ORDER) | set(RC.DOWNSTREAM)
        if not nodes or any(n not in known for n in nodes):
            return
        ev_ids = [str(e) for e in (ev_ids or ()) if e is not None]
        cases.append({"case_id": None, "reason": code,
                      "region": region_bbox, "affected_nodes": list(nodes),
                      "evidence_ids": list(dict.fromkeys(ev_ids))[:16],
                      "detail": dict(detail, layer_semantics=sem)})

    # ---- 1) 候选完整性（有证据、无候选）
    for kind, got in (("window", len(windows)), ("door", len(doors)),
                      ("column", len(columns))):
        layers = set(sem.get(kind) or ())
        if not layers:
            continue
        on_layer = [e for e in ents if e.get("layer") in layers]
        lines = [e for e in on_layer if e.get("type") == "LINE"]
        need = MIN_LINES if kind in ("window", "door") else MIN_SUPPORT
        if got == 0 and len(on_layer) >= need:
            add(f"{kind.upper()}_EVIDENCE_WITHOUT_CANDIDATES",
                _bbox(on_layer) or _region_bbox(regions), (kind, "openings_select"),
                _cand_ids(index, [e.get("handle") for e in on_layer[:10]], layers),
                {"layers": sorted(layers), "entities": len(on_layer),
                 "lines": len(lines), "candidates": got})

    # ---- 2) 空间完整性：先做文字-面域关联，再统计真正孤儿
    labels = list(room_labels or ())
    assoc = associate_labels(labels, faces)
    if labels:
        orphan = [a["label"] for a in assoc
                  if a.get("status") in ("leak", "distant")]
        if not faces or len(orphan) >= max(1, ORPHAN_RATIO * len(labels)):
            add("ROOM_BOUNDARY_UNRESOLVED", _bbox(orphan) or _region_bbox(regions),
                ("spaces_seed", "rooms"),
                _cand_ids(index, [l.get("handle") for l in orphan[:10]],
                          None, _bbox(orphan)),
                {"labels": len(labels), "orphan": len(orphan), "faces": len(faces),
                 "orphan_samples": [l.get("text") for l in orphan[:6]]})

    # ---- 3) 解释冲突：只看 openings_select 之后仍成立的冲突
    pairs = _door_window_conflicts(
        (openings_selected or {}).get("doors") or doors,
        (openings_selected or {}).get("windows") or windows)
    if pairs:
        a, b = pairs[0]
        add("OPENING_INTERPRETATION_CONFLICT", _bbox([a, b]) or _region_bbox(regions),
            ("openings_select", "resolve_conflicts"),
            _cand_ids(index, [a.get("handle"), b.get("handle")], None),
            {"door": a.get("handle"), "window": b.get("handle"),
             "pairs": len(pairs)})

    # ---- 4) 语义完整性
    known = set()
    for v in sem.values():
        known |= set(v)
    unknown = collections.Counter(
        e.get("layer") for e in ents
        if e.get("type") in ("LINE", "LWPOLYLINE", "INSERT") and e.get("layer") not in known)
    heavy = [(l, n) for l, n in unknown.most_common(3) if n >= 20]
    if heavy:
        add("SEMANTIC_GAP_UNKNOWN_LAYERS", _region_bbox(regions), ("load", "walls_seg"),
            [f"layer:{l}" for l, _n in heavy],
            {"layers": [{"layer": l, "count": n} for l, n in heavy]})

    # ---- 4.5) 缺陷诊断出的可修问题（比"数量异常"更贴近真实原因）
    for d in (defects or ()):
        if d.get("type") == "VALID_OPEN_SPACE":
            continue
        opts = [o for o in (d.get("repair_options") or ())
                if o in ("option:close_gap", "option:change_closure",
                         "option:retry_pair", "option:rebuild_contour",
                         "option:override_layer", "option:merge")]
        if not opts or not d.get("evidence_ids"):
            continue
        add("DEFECT_" + d["type"], d.get("region"),
            ("walls_seg", "rooms"), d.get("evidence_ids") or (),
            d.get("detail") or {})

    # ---- 5) 用 case_builder 补几何特征 + 合法替代方案，过滤无解案例
    kept = []
    for case in cases:
        enriched = CB.build(case, index)
        if len(enriched.get("alternatives") or ()) <= 1:      # 只剩 NO_ACTION
            continue
        enriched["benefit"] = _benefit(enriched)
        enriched["region_id"] = _group_of(enriched, regions)
        kept.append(enriched)

    # ---- 6) 预算：按区域分组 -> 组内按预期收益排序 -> 组预算 + 总预算
    groups: dict[str, list[dict]] = {}
    for case in kept:
        groups.setdefault(case["region_id"], []).append(case)
    selected: list[dict] = []
    group_report = []
    order = sorted(groups.items(),
                   key=lambda kv: (-max(c["benefit"] for c in kv[1]), kv[0]))
    for rid, items in order:
        items.sort(key=lambda c: (-c["benefit"], c["reason"],
                                  str(c.get("detail", {}).get("layers") or "")))
        take = items[:MAX_CASES_PER_REGION]
        room = MAX_CASES - len(selected)
        if room <= 0:
            group_report.append({"region_id": rid,
                                 "cases": len(items), "selected": 0,
                                 "benefit": round(sum(c["benefit"] for c in items), 3),
                                 "skipped_by_budget": len(items)})
            continue
        take = take[:room]
        selected.extend(take)
        group_report.append({"region_id": rid, "cases": len(items),
                             "selected": len(take),
                             "benefit": round(sum(c["benefit"] for c in items), 3),
                             "selected_benefit": round(sum(c["benefit"] for c in take), 3),
                             "skipped_by_budget": len(items) - len(take)})
    # case_id 在选定之后按稳定顺序分配，避免"预算变化导致 id 全变"
    for i, case in enumerate(selected, 1):
        case["case_id"] = f"case_{i:03d}"
    kept = selected

    need = bool(kept)
    return {"schema": "quality-gate/2.1", "need_llm": need,
            "reason_codes": sorted({c["reason"] for c in kept}),
            "regions": [c["region"] for c in kept if c.get("region")],
            "affected_nodes": sorted({n for c in kept for n in c["affected_nodes"]}),
            "evidence_ids": sorted({e for c in kept for e in c["evidence_ids"]}),
            "cases": kept,
            "groups": group_report,
            "dropped_cases": len(cases) - len(kept),
            "budget": {"max_cases": MAX_CASES, "per_region": MAX_CASES_PER_REGION,
                       "max_calls": MAX_CALLS, "max_rounds": MAX_ROUNDS,
                       "used": len(kept),
                       "regions_used": len([g for g in group_report if g["selected"]])},
            "label_association": {
                "total": len(assoc),
                "bound": sum(1 for a in assoc if a["status"] == "bound"),
                "offset": sum(1 for a in assoc if a["status"] == "offset"),
                "leak": sum(1 for a in assoc if a["status"] == "leak"),
                "distant": sum(1 for a in assoc if a["status"] == "distant"),
                "no_point": sum(1 for a in assoc if a["status"] == "no_point"),
                "fallback": sum(1 for a in assoc if a.get("source") == "fallback"),
                "weak_bound": sum(1 for a in assoc if a["status"] == "bound"
                                  and a.get("source") == "fallback")},
            "stats": {"entities": len(ents), "faces": len(faces),
                      "doors": len(doors), "windows": len(windows),
                      "columns": len(columns), "labels": len(labels)}}


# ------------------------------------------------------------------ 工具
def _cand_ids(index, handles, layers=None, region=None, limit=14) -> list[str]:
    items = index.get("items") or {}
    rev = {}
    for rel in index.get("relations") or ():
        dst = str(rel.get("dst") or "")
        if rel.get("type") == "supported_by" and dst.startswith("ev:"):
            rev.setdefault(dst[3:], []).append(str(rel.get("src") or ""))
    picked, seen = [], set()

    def take(rid):
        if rid in seen or len(picked) >= limit:
            return
        rec = items.get(rid)
        if not rec:
            return
        b = rec.get("bbox")
        if region and b and not _in_region(b, region):
            return
        if layers and rec.get("layer") and rec["layer"] not in layers:
            return
        seen.add(rid)
        picked.append(rid)

    for h in handles or ():
        for rid in rev.get(str(h), ()):
            if rid.split(":", 1)[0] in ("op", "wall", "col", "face", "label", "pat",
                                        "contour", "hatch"):
                take(rid)
    for rid in items:
        if rid.split(":", 1)[0] in ("op", "wall", "col", "face", "label", "pat",
                                    "contour", "layer"):
            take(rid)
    for h in handles or ():
        rid = f"ev:{h}"
        if h and rid in items and rid not in seen:
            take(rid)
    return picked


def _face_of(faces, label):
    if not faces or not isinstance(label, dict):
        return None
    x, y = label.get("x"), label.get("y")
    if x is None or y is None:
        return None
    try:
        from .rooms import point_in_polygon
    except Exception:
        return None
    for f in faces:
        b = f.get("bbox") or [0, 0, 0, 0]
        if not (b[0] <= x <= b[2] and b[1] <= y <= b[3]):
            continue
        if point_in_polygon(x, y, f.get("polygon") or ()):
            return f
    return None


def _door_window_conflicts(doors, windows):
    out = []
    for d in doors or ():
        ds = d.get("opening") or d.get("axis")
        if not ds:
            continue
        for w in windows or ():
            ws = w.get("axis") or w.get("opening")
            if ws and _overlap_ratio(ds, ws) >= 0.6:
                out.append((d, w))
    return out


def _overlap_ratio(s1, s2) -> float:
    (x1, y1), (x2, y2) = s1
    (u1, v1), (u2, v2) = s2
    l1 = math.hypot(x2 - x1, y2 - y1) or 1.0
    l2 = math.hypot(u2 - u1, v2 - v1) or 1.0
    nx, ny = -(y2 - y1) / l1, (x2 - x1) / l1
    if abs((u1 - x1) * nx + (v1 - y1) * ny) > 60.0:
        return 0.0
    dx, dy = (x2 - x1) / l1, (y2 - y1) / l1
    b1 = (u1 - x1) * dx + (v1 - y1) * dy
    b2 = (u2 - x1) * dx + (v2 - y1) * dy
    lo, hi = max(0.0, min(b1, b2)), min(l1, max(b1, b2))
    return max(0.0, hi - lo) / min(l1, l2)


def _bbox(recs):
    from .evidence import bbox_of
    xs, ys = [], []
    for r in recs or ():
        b = bbox_of(r if isinstance(r, dict) else {})
        if b:
            xs += [b[0], b[2]]
            ys += [b[1], b[3]]
    if not xs:
        return None
    return [round(min(xs), 1), round(min(ys), 1), round(max(xs), 1), round(max(ys), 1)]


def _region_bbox(regions):
    if not regions:
        return None
    b = regions[0].get("bbox")
    return [float(v) for v in b] if b else None


def _in_region(b, region, pad=300.0) -> bool:
    return not (b[2] < region[0] - pad or b[0] > region[2] + pad
                or b[3] < region[1] - pad or b[1] > region[3] + pad)
