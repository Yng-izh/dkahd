"""门窗柱专用几何拟合器（``v2-component-focus``）。

以前只有"一套通用语法 + 一套通用原语匹配"，于是：

* 窗框间距被"全图墙厚 × 带"卡住，窄框窗直接匹配不上；
* 柱只看"有没有闭合轮廓"，家具轮廓一样闭合；
* 门只看"有没有弧 + 有没有线"，家具圆弧一样满足。

现在每类各有一套拟合器（:mod:`.door` / :mod:`.window` / :mod:`.column`），各自
按**本实例自身的尺度**估计，各自带**类别特有的反面证据**（家具、图例、装饰）。
通用语法拟合仍在 :mod:`..geometry_fitter`，两条路互不影响。

这一层只产出"读到的几何 + 证据"，不裁决；裁决在 :mod:`..component_resolver`。
"""
from __future__ import annotations

from .. import cad_ir as IR
from .. import geometry_fitter as FIT
from . import _common as C
from . import column as COLUMN
from . import door as DOOR
from . import window as WINDOW

SCHEMA = "fitters/1.0"
CATEGORIES = ("door", "window", "column")
FITTERS = {"door": DOOR, "window": WINDOW, "column": COLUMN}

#: 逻辑预算（个）
BUDGET = {"instances": 240, "siblings": 400, "near_cell": 12.0, "near_pad": 3.0}

#: 近邻索引缓存（按 IR 指纹）：一次运行里只建一次
_NEAR: dict = {}


def build_near_index(ir: dict) -> dict:
    """给"看旁边有什么"用的格网索引（端部封口、共线延续都要它）。"""
    from .. import cad_ir as IR
    key = IR.digest(ir)
    if key in _NEAR:
        return _NEAR[key]
    thickness = float((ir.get("basis") or {}).get("thickness") or 200.0)
    index = C.near_index(ir.get("entities") or (),
                         cell=max(1.0, float(BUDGET["near_cell"]) * thickness))
    _NEAR[key] = index
    return index


def guess_category(entities) -> str:
    """没有模型确认时，用**几何形态**猜一个类别去试（猜错不要紧，会拟合失败）。"""
    kinds = {e.get("kind") for e in entities or ()}
    if "arc" in kinds:
        return "door"
    if entities and C.is_closed(entities[0]):
        return "column"
    for e in entities or ():
        if C.is_closed(e):
            return "column"
    return "window"


def fit_instance(category: str, instance: dict, *, entities, walls=(), grid=None,
                 basis=None, family=None, siblings=(), neighbors=()) -> dict | None:
    module = FITTERS.get(str(category))
    if module is None:
        return None
    return module.fit_instance(instance, entities=entities, walls=walls, grid=grid,
                               basis=basis, family=family, siblings=siblings,
                               neighbors=neighbors)


def _neighbors(near, box) -> tuple:
    """实例附近的图元（端部封口、共线延续都要看旁边有什么）。"""
    if not near or not box:
        return ()
    span = max(1.0, float(box[2]) - float(box[0]))
    return tuple(C.near_entities(near, box,
                                 pad=float(BUDGET["near_pad"]) * span))


def readings(family: dict, *, ir: dict, walls=(), grid=None, basis=None,
             limit=None, near=None) -> dict:
    """把一个构件族的每个实例都拟合一遍，返回 ``{"readings", "trace"}``。

    两遍拟合：第一遍各自独立（拿到每根柱的尺寸），第二遍把"同族其它实例"当
    ``siblings`` 传进去——柱网重复度、门联窗这类判断必须看邻居，不能只看自己。
    """
    by_uid = IR.by_uid(ir)
    basis = dict(basis or ir.get("basis") or {})
    rows: list = []
    trace: list = []
    cap = int(limit if limit is not None else BUDGET["instances"])
    for inst in (family.get("instances") or ())[:cap]:
        entities = [by_uid[u] for u in inst.get("uids") or () if u in by_uid]
        if not entities:
            trace.append({"instance": inst.get("id"), "status": "no_entities"})
            continue
        category = family.get("category_guess") or guess_category(entities)
        neighbors = _neighbors(near, inst.get("bbox") or C.bbox_of(entities))
        reading = fit_instance(category, inst, entities=entities, walls=walls,
                              grid=grid, basis=basis, family=family,
                              neighbors=neighbors)
        if reading is None:
            other = "column" if category != "column" else "window"
            reading = fit_instance(other, inst, entities=entities, walls=walls,
                                   grid=grid, basis=basis, family=family,
                                   neighbors=neighbors)
            if reading is not None:
                trace.append({"instance": inst.get("id"),
                              "fallback": other, "from": category})
        if reading is None:
            trace.append({"instance": inst.get("id"), "status": "no_fit",
                          "category": category})
            continue
        reading["instance"] = inst
        rows.append(reading)

    # 第二遍：同族实例当邻居（只对"需要邻居证据"的类别重跑，省时间）
    if rows:
        siblings = [{"category": r["category"], "bbox": r["geometry"].get("bbox"),
                     "geometry": r["geometry"], "id": (r.get("instance") or {}).get("id")}
                    for r in rows][:int(BUDGET["siblings"])]
        refined: list = []
        for reading in rows:
            if reading["category"] not in ("column", "door"):
                refined.append(reading)
                continue
            inst = reading.get("instance") or {}
            entities = [by_uid[u] for u in inst.get("uids") or () if u in by_uid]
            neighbors = _neighbors(near, inst.get("bbox") or C.bbox_of(entities))
            again = fit_instance(reading["category"], inst, entities=entities,
                                 walls=walls, grid=grid, basis=basis,
                                 family=family, siblings=siblings,
                                 neighbors=neighbors)
            refined.append(dict(again or reading, instance=inst))
        rows = refined
    return {"readings": rows, "trace": trace}


def fit_family(family: dict, *, ir: dict, walls=(), grid=None, limit=None,
               source: str = "propagation", model_reading=None) -> dict:
    """族 -> 一批统一候选（每个实例一条）。"""
    basis = dict(ir.get("basis") or {})
    near = build_near_index(ir)
    built = readings(family, ir=ir, walls=walls, grid=grid, basis=basis,
                     limit=limit, near=near)
    hypotheses: list = []
    for reading in built["readings"]:
        inst = reading.get("instance") or {}
        geom = reading.get("geometry") or {}
        category = reading["category"]
        evidence = dict(reading.get("evidence") or {})
        evidence.update({"family": family.get("id"),
                         "family_kind": family.get("kind"),
                         "instances_in_family": family.get("n"),
                         "how": reading.get("how") or (),
                         "negatives": reading.get("negatives") or [],
                         "scales": reading.get("scales") or {},
                         "transform": inst.get("transform")})
        if model_reading:
            evidence["model"] = {"category": model_reading.get("category"),
                                 "subtype": model_reading.get("subtype"),
                                 "confidence": model_reading.get("confidence")}
        hypotheses.append(FIT.hypothesis(
            category=category, subtype=reading.get("subtype"), source=source,
            engine=f"fitter.{category}", region=None,
            bbox=geom.get("bbox") or inst.get("bbox"), geometry=geom,
            uids=inst.get("uids") or (),
            handles=[inst["handle"]] if inst.get("handle") else (),
            score=reading.get("score") or 0.0, evidence=evidence,
            grammar={"fitter": category, "family": family.get("id"),
                     "rules": [h.get("rule") for h in reading.get("how") or ()]},
            status=reading.get("status") or "candidate",
            family_id=family.get("id"), transform=inst.get("transform")))
    by_cat: dict = {}
    for hyp in hypotheses:
        by_cat[hyp["category"]] = by_cat.get(hyp["category"], 0) + 1
    return {"schema": SCHEMA, "family": family.get("id"),
            "category": family.get("category_guess"),
            "hypotheses": hypotheses, "trace": built["trace"][:30],
            "stats": {"instances": len(family.get("instances") or ()),
                      "fitted": len(hypotheses), "by_category": by_cat,
                      "status": "ok" if hypotheses else "no_fit"}}


def summary(result) -> dict:
    result = result or {}
    return {"schema": SCHEMA, "family": result.get("family"),
            "category": result.get("category"),
            "stats": result.get("stats") or {}}


__all__ = ["BUDGET", "CATEGORIES", "FITTERS", "SCHEMA", "fit_family",
           "fit_instance", "guess_category", "readings", "summary"]
