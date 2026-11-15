"""全图实例传播：一个已确认的构件族，推广到整张图的所有实例。

这是 ``v2-component-focus`` 的核心收益点：模型只理解**一个**代表实例，剩下的事
交给程序——因为同一画法在全图里可能重复几十次。

传播只看两件事，都不依赖模型：

1. **形状签名**（:func:`fitters._common.shape_signature`）：把实例搬到自身坐标
   系再离散化，所以旋转、镜像、缩放都不影响比较；
2. **几何复核**：每个命中的实例都要过一遍该类别的专用拟合器，拟合失败就不算，
   不"因为长得像"直接算一个构件。

跨图层也在这里发生：签名不含图层、块名、颜色，所以放到别的图层上的同画法实例
照样能被找到；反过来，块名/图层只作为弱证据出现在 ``evidence`` 里，不做判据。
"""
from __future__ import annotations

import hashlib

from . import cad_ir as IR
from . import component_family as FAM
from . import geometry_fitter as FIT
from .fitters import _common as SH
from .fitters import build_near_index
from .fitters import fit_instance as fit_one

SCHEMA = "instance-propagation/1.0"

#: 逻辑预算（个）
BUDGET = {"candidates": 2400, "instances": 240, "siblings": 400,
          "signature_distance": 0.42}

#: 近邻搜索半径（按实例跨度倍率，不是毫米数）
FITTERS_PAD = 3.0

#: 一次运行内缓存"某类种子实例"，避免每个族都重扫一遍图
_CACHE: dict = {}


def _cache_key(ir: dict, kind: str) -> tuple:
    return (IR.digest(ir), str(kind))


def candidate_instances(ir: dict, kind: str, *, family=None) -> list:
    """某一来源的全部实例种子；规则族使用已发现的精确成员。"""
    if kind == "rule":
        # Rule members cannot be regenerated from IR alone. Never cache them
        # by IR digest: different families have different member sets.
        return list((family or {}).get("instances") or ())
    key = _cache_key(ir, kind)
    if key in _CACHE:
        return _CACHE[key]
    if kind == "block":
        rows = FAM.block_instances(ir)
    elif kind == "arc":
        rows = FAM.arc_instances(ir)
    elif kind == "bundle":
        rows = FAM.bundle_instances(ir)
    elif kind == "pair":
        rows = FAM.pair_instances(ir)
    elif kind == "contour":
        rows = FAM.contour_instances(ir)
    else:
        rows = []
    rows = rows[:int(BUDGET["candidates"])]
    _CACHE[key] = rows
    return rows


def _entity_box(by_uid: dict, uids) -> list:
    return SH.bbox_of([by_uid[u] for u in uids or () if u in by_uid]) or [0, 0, 0, 0]


def _transform_of(instance, ref_box, instance_box) -> dict:
    scale = 1.0
    if ref_box and instance_box:
        ref = max(float(ref_box[2]) - float(ref_box[0]),
                  float(ref_box[3]) - float(ref_box[1]))
        here = max(float(instance_box[2]) - float(instance_box[0]),
                   float(instance_box[3]) - float(instance_box[1]))
        scale = (here / ref) if ref > 0 else 1.0
    tr = dict(instance.get("transform") or {})
    return {"rotation_deg": SH._r(tr.get("rotation") or 0.0, 2),
            "mirror": bool(tr.get("mirror")),
            "scale": SH._r(scale, 4),
            "source": tr.get("source") or "geometry"}


def propagate(family: dict, *, ir: dict, graph=None, walls=(), grid=None,
              category=None, limit=None, tol=None, kinds=None) -> dict:
    """把一个族推广到全图：返回该族在整张图里能站得住的全部实例。"""
    by_uid = IR.by_uid(ir)
    basis = dict(ir.get("basis") or {})
    limit = int(limit if limit is not None else BUDGET["instances"])
    tol = float(tol if tol is not None else BUDGET["signature_distance"])
    ref = family.get("signature")
    ref_box = (family.get("representative") or {}).get("bbox") or _entity_box(
        by_uid, (family.get("representative") or {}).get("uids") or ())
    category = category or family.get("category") or family.get("category_guess")
    model = family.get("model") or {}
    confidence = str(model.get("confidence") or "").lower()
    representative_uids = set((family.get("representative") or {}).get("uids") or ())
    representative_entities = [by_uid[u] for u in representative_uids if u in by_uid]
    # Explicit authorization can only narrow the geometric safety scope.
    # Plain column contours cannot transfer semantics to matching rectangles.
    representative_only = bool(
        family.get("authorization_scope") == "representative_only"
        or model.get("authorization_scope") == "representative_only"
        or (category == "column" and representative_entities
            and all(e.get("kind") in ("poly", "segment", "text")
                    for e in representative_entities)))
    confirmed_column = bool(
        family.get("confirmed") is True and family.get("id")
        and family.get("verdict") != "reject"
        and model.get("category") == "column"
        and family.get("category") == "column"
        and confidence == "high")
    kinds = list(kinds or [family.get("kind") or "bundle"])
    if family.get("verdict") == "reject" or (
            category == "column" and not confirmed_column):
        return {"schema": SCHEMA, "family": family.get("id"), "category": category,
                "hypotheses": [], "rejected": [],
                "stats": {"searched": 0, "matched": 0, "fitted": 0,
                          "by_category": {}, "kinds": kinds,
                          "authorization_scope": "representative_only" if representative_only else "family",
                          "status": "model_rejected" if (family.get("verdict") == "reject"
                                                         and not representative_only)
                          else "unconfirmed"}}

    seeds = list(family.get("instances") or ())
    if family.get("representative"):
        seeds.append(family["representative"])
    # Column semantics apply only to the observed family. A global scan of
    # similarly sized rectangles would silently extend that authorization.
    if category != "column":
        if family.get("kind") == "block":
            kinds = ["block", "bundle", "pair"]
        for kind in kinds:
            seeds.extend(candidate_instances(ir, kind, family=family))
    pool: list = []
    seen: set = set()
    for inst in seeds:
        key = tuple(sorted(set(inst.get("uids") or ())))
        if not key or key in seen:
            continue
        seen.add(key)
        pool.append({**inst, "uids": list(key)})
    pool = pool[:int(BUDGET["candidates"])]
    near = build_near_index(ir)
    matched: list = []
    rejected: list = []
    for inst in pool:
        entities = [by_uid[u] for u in inst.get("uids") or () if u in by_uid]
        if not entities or len(entities) != len(inst.get("uids") or ()):
            if len(rejected) < 40:
                rejected.append({"instance": inst.get("id"), "why": "MISSING_ENTITIES"})
            continue
        box = SH.bbox_of(entities) or inst.get("bbox") or [0, 0, 0, 0]
        dist, mirrored = SH.best_signature_distance(
            ref, lambda flip: SH.shape_signature(entities, mirror=flip))
        if dist > tol:
            if len(rejected) < 40:
                rejected.append({"instance": inst.get("id"), "why": "SIGNATURE_FAR",
                                 "distance": SH._r(dist, 3), "mirror": bool(mirrored)})
            continue
        neighbors = SH.near_entities(near, box, pad=float(FITTERS_PAD) * max(
            1.0, float(box[2]) - float(box[0]))) if near else ()
        reading = fit_one(category, inst, entities=entities, walls=walls,
                          grid=grid, basis=basis, family=family,
                          neighbors=neighbors)
        if reading is None:
            if len(rejected) < 40:
                rejected.append({"instance": inst.get("id"), "why": "FIT_FAILED",
                                 "distance": SH._r(dist, 3)})
            continue
        reading["instance"] = inst
        reading["_propagation"] = {"distance": SH._r(dist, 3),
                                  "mirrored": bool(mirrored),
                                  "transform": _transform_of(inst, ref_box, box)}
        matched.append(reading)
        if len(matched) >= limit:
            break

    hypotheses: list = []
    for reading in matched:
        inst = reading["instance"]
        geom = reading.get("geometry") or {}
        evidence = dict(reading.get("evidence") or {})
        evidence.update({"family": family.get("id"),
                         "family_kind": family.get("kind"),
                         "propagated": True,
                         "similarity": SH._r(1.0 - float(
                             reading["_propagation"]["distance"]), 3),
                         "mirrored": reading["_propagation"]["mirrored"],
                         "instances_in_family": family.get("n"),
                         "how": reading.get("how") or (),
                         "negatives": reading.get("negatives") or [],
                         "scales": reading.get("scales") or {}})
        authorized = not representative_only or set(inst.get("uids") or ()) == representative_uids
        model_confirmed = bool(
            family.get("confirmed") is True and family.get("id")
            and family.get("verdict") != "reject"
            and model.get("category") == reading["category"]
            and family.get("category") == reading["category"]
            and confidence == "high")
        if model_confirmed or not authorized:
            evidence.update({"category_confirmed": authorized,
                             "category_basis": "model_family" if authorized else "unconfirmed",
                             "scope": "representative_only" if representative_only else "family",
                             "pending_reason": None if authorized else "MEMBER_REQUIRES_MODEL_REVIEW",
                             "geometry_status": "verified",
                             "model_confidence": confidence,
                             "model": dict(model),
                             "family_representative": (family.get("representative")
                                                       or {}).get("id"),
                             "family_instance": inst.get("id")})
        hypotheses.append(FIT.hypothesis(
            category=reading["category"], subtype=reading.get("subtype"),
            source="propagation", engine=f"propagate.{reading['category']}",
            region=None, bbox=geom.get("bbox") or inst.get("bbox"),
            geometry=geom, uids=inst.get("uids") or (),
            handles=sorted({str(by_uid[u]["handle"]) for u in inst.get("uids") or ()
                            if u in by_uid and by_uid[u].get("handle")}),
            score=SH._r(max(0.05, min(0.97, float(reading.get("score") or 0.0)
                                    - 0.4 * float(reading["_propagation"]["distance"]))), 3),
            evidence=evidence,
            grammar={"fitter": reading["category"], "family": family.get("id"),
                     "rules": [h.get("rule") for h in reading.get("how") or ()]},
            status=(reading.get("status") or "candidate") if authorized else "pending",
            family_id=family.get("id"),
            transform=reading["_propagation"]["transform"]))
    by_cat: dict = {}
    for hyp in hypotheses:
        by_cat[hyp["category"]] = by_cat.get(hyp["category"], 0) + 1
    return {"schema": SCHEMA, "family": family.get("id"), "category": category,
            "hypotheses": hypotheses, "rejected": rejected,
            "stats": {"searched": len(pool), "matched": len(matched),
                      "fitted": len(hypotheses), "by_category": by_cat,
                      "authorization_scope": "representative_only" if representative_only else "family",
                      "authorized": sum(h.get("evidence", {}).get("category_confirmed") is True
                                        for h in hypotheses),
                      "pending": sum(h.get("status") == "pending" for h in hypotheses),
                      "kinds": kinds,
                      "status": "ok" if hypotheses else "no_instance"}}


def propagate_families(families, *, ir: dict, graph=None, walls=(), grid=None,
                       limit=None, categories=None) -> dict:
    """多个族一起传播（族之间按 uid 去重，保留分高的）。"""
    wanted = tuple(categories or ("window", "column", "door"))
    rows: list = []
    trace: list = []
    seen: dict = {}
    for family in families or ():
        category = family.get("category") or family.get("category_guess")
        if category not in wanted:
            trace.append({"family": family.get("id"),
                          "status": "category_not_target", "category": category})
            continue
        result = propagate(family, ir=ir, graph=graph, walls=walls, grid=grid,
                           category=category, limit=limit)
        trace.append({"family": family.get("id"), "category": category,
                      **{k: v for k, v in result["stats"].items() if k != "by_category"}})
        for hyp in result["hypotheses"]:
            key = tuple(sorted(hyp.get("uids") or ()))
            if not key:
                continue
            better = seen.get(key)
            if better is None or float(hyp.get("score") or 0) > float(
                    better.get("score") or 0):
                seen[key] = hyp
    rows = list(seen.values())
    by_cat: dict = {}
    for hyp in rows:
        by_cat[hyp["category"]] = by_cat.get(hyp["category"], 0) + 1
    return {"schema": SCHEMA, "hypotheses": rows, "trace": trace,
            "stats": {"families": len(families or ()), "instances": len(rows),
                      "by_category": by_cat,
                      "status": "ok" if rows else "no_instance"}}


def summary(result) -> dict:
    result = result or {}
    return {"schema": SCHEMA, "family": result.get("family"),
            "stats": result.get("stats") or {},
            "rejected": len(result.get("rejected") or ())}


__all__ = ["BUDGET", "SCHEMA", "candidate_instances", "propagate",
           "propagate_families", "summary"]
