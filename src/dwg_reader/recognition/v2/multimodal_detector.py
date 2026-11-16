"""多模态构件理解：让模型**针对一个具体构件族**给语义判断，而不是给整块区域编语法。

与旧的首轮理解（:mod:`multimodal_bootstrap`）的区别：

* 旧：给一块区域 + 一张图，"你猜这里有什么构件、什么语法"——区域里可能有一百个
  图元、七八种构件，模型只能笼统应付；
* 新：给**一个族**（同画法的若干实例）+ 代表实例与若干成员的局部图，"这是什么
  构件、什么子类型、由哪些部分组成、有没有反面特征、这些成员是不是同一画法"——
  问题窄，答案能验证。

模型能说的（闭集）：

* 类别：``door`` / ``window`` / ``column`` / ``none``；
* 子类型：每类一个小闭集（见 :data:`SUBTYPES`）；
* 组成：部件的角色名 + 目录里的原语/关系（与 :mod:`component_grammar` 同一套）；
* 反面特征：家具、图例、标注、装饰、其它构件；
* 竞争解释：还可以是什么；
* 族一致性：这些成员是不是同一画法（不一致时程序只授权代表实例，不推广）。

模型不能说的（出现即整条作废）：坐标、包围盒、毫米数、阈值、可执行代码。
几何一律由 :mod:`fitters` 从原始图元算。

**每次请求都留档**：提示词原文、每张图的元数据（尺度/像素/目标高亮数/编号数/
字节数/SHA-256）以及模型原始答复都进 ``result["request"]`` / ``result["reply"]``，
``result["raw"]`` 是解析前的原文。没有这些，"模型为什么这样判"永远只能猜。
"""
from __future__ import annotations

import base64
import hashlib
import json
import math

from . import component_family as FAM
from . import component_grammar as GRAMMAR
from .fitters import _common as SH

SCHEMA = "multimodal-detector/1.0"
PROMPT_VERSION = "component-detector/2.1"

#: 本期只做这三类（房间退出主流程）
FOCUS = ("door", "window", "column")

#: 模型还可以回答"这是墙/墙面线"——以前没有这个选项，"两条平行线"只能被说成窗
CONTEXT_CATEGORIES = ("wall",)

#: 子类型闭集（模型只能从这里挑）
SUBTYPES = {
    "door": ("swing_door", "double_leaf_door", "sliding_door", "door_with_window",
             "cased_opening", "revolving_door", "unknown"),
    "window": ("punched_window", "four_line_window", "line_bundle_unclassified",
               "sliding_window", "corner_window", "curtain_wall", "block_window",
               "unknown"),
    "column": ("rect_column", "rotated_column", "irregular_column",
               "filled_column", "composite_column", "unknown"),
    "none": ("furniture", "legend_symbol", "annotation", "fixture", "other"),
}

#: 反面特征闭集
NEGATIVES = ("furniture", "legend_symbol", "annotation", "fixture", "title_frame",
             "dimension", "hatch_pattern", "other_component", "wall", "structure")

#: 逻辑预算：每族看几张图、看几个成员、要不要补看一次
#: ``overview``：是否在成员局部图之前加一张族分布总览图（另计，不占 images_per_family）；
#: ``layout_facts``：是否给全部成员的排列/墙线交接/填充类型事实。
BUDGET = {"max_families": 8, "images_per_family": 4, "samples": 3,
          "closeup": 1, "recheck": 4, "prompt_facts": 5200,
          "overview": False, "layout_facts": True}

#: 模型输出里绝不允许出现的键（出现即判"模型给几何"）
FORBIDDEN_KEYS = ("bbox", "box", "coord", "coords", "coordinates", "x", "y",
                  "width", "height", "length", "radius", "thickness", "mm",
                  "points", "polygon", "vertices")

SYSTEM = (
    "You are a component-level CAD interpreter. You are shown ONE component family "
    "(several instances drawn the same way) or ONE instance. Decide what component it "
    "is, its subtype, its composition and whether it is actually furniture/legend. "
    "Layer names, block names, text labels and family names are NOT evidence and must "
    "not decide the answer. Never output coordinates, sizes, thresholds or code: a "
    "deterministic fitter measures the geometry from CAD entities. Return only JSON."
)

PROMPT = """COMPONENT-LEVEL READING.

Family facts (computed from CAD entities, ids are stable):
{facts}

Instances in this family:
{instances}

Images (role "family_overview" shows ALL members of this family at once; every
other image is a local render; ORANGE = the target you must judge,
GREEN = already-accepted wall used only as context, GREY = surrounding geometry):
{images}

Scope of this answer: {scope}

Allowed vocabulary (closed sets; nothing else is valid):
- categories: {categories}
- subtypes per category:
{subtypes}
- part primitives: {primitives}
- relations: {relations}
- negative features: {negatives}

Rules:
1. Answer about THIS family. The same drawing method may repeat many times; the
   program will propagate your answer to every instance, so describe the *method*.
1a. First check that the shown members really share one composition. If they do not,
   set "family_consistent": false and list the instance ids that differ in
   "outliers". Then answer only for the representative instance.
1b. "wall" means this family IS wall geometry (wall faces, partition faces, finish
   lines). Two parallel lines are NOT evidence of a window: check wall_relation
   (covers_wall_faces / wall_continues / depth_over_thickness) and the width
   percentile before choosing a target category.
1c. Layer names, block names, text labels and the family name are untrusted metadata.
   They are NOT evidence. Decide from geometry only: closure, arcs, line offsets,
   wall relation, repetition, proportions.
1d. member_layout, wall_interaction and fill_types (when present) are measured over
   ALL members, not only the ones in the local images. member_layout tells whether
   member centres line up in rows/columns and how regular the spacing is.
   wall_interaction.fractions gives, per member, whether nearby lines END at its
   boundary (interrupted_opposite = on two opposite sides), run THROUGH it, run
   FLUSH along one of its edges past both ends, or whether nothing meets it
   (isolated); wall_axes does the same for accepted wall centre lines. Weigh these
   together with the overview image; none of them decides the category alone.
2. Composition must use only the catalog primitives/relations. Role names are yours.
3. If the geometry is furniture, a legend, an annotation or another component, use
   category="none" and give the matching negative feature.
4. Never output coordinates, bounding boxes, sizes, ratios, thresholds or code.
5. Give at most two alternative interpretations with a one-line reason each.
6. If the images are too tight or too small to judge, set "need_closeup": true.

Respond as JSON:
{{"component":{{"category":"window","subtype":"sliding_window",
  "parts":{{"frame":"parallel_line_pair","leaves":"repeated_line_bundle"}},
  "relations":[["leaves","inside","frame"]],
  "confidence":"high","negative_features":[],"note":"two offset leaves"}},
 "family_consistent":true,
 "outliers":[],
 "alternatives":[{{"category":"door","subtype":"sliding_door","why":"leaf offset"}}],
 "need_closeup":false}}
"""

CLOSEUP_PROMPT = """CLOSE-UP RE-EXAMINATION of the representative instance.

Family facts:
{facts}

Instance: {instance}

The image is a tight detail render of exactly this instance (ORANGE = target,
GREEN = accepted wall context, GREY = neighbours). Decode the local geometry:
closure, arcs, line offsets, wall contact, repetition.

Answer only about THIS instance:
- category ({categories}) and subtype, or "none" with a negative feature;
- one-line reason that refers to geometry, not to layer names or block names.
Never output coordinates, sizes or thresholds. JSON only:
{{"component":{{"category":"column","subtype":"rect_column",
  "confidence":"high","negative_features":[],"note":"..."}},
 "verdict":"keep|reject"}}
"""

RECHECK_PROMPT = """RE-EXAMINATION of a single uncertain candidate.

Candidate facts:
{facts}

Earlier machine failure codes: {failures}

The image is a tight detail render of exactly this candidate (ORANGE = target,
GREEN = accepted wall context, GREY = neighbours).

Answer only about this instance:
- category ({categories}) and subtype, or "none" with a negative feature;
- one-line reason that refers to geometry (arcs, offsets, closure, host wall), not to
  layer names or block names.
Never output coordinates, sizes or thresholds. JSON only:
{{"component":{{"category":"column","subtype":"rect_column",
  "confidence":"medium","negative_features":[],"note":"..."}},
 "verdict":"keep|reject"}}
"""


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def _numbers_present(obj, depth: int = 0) -> bool:
    """模型输出里是否出现数值几何（坐标/尺寸/阈值）。出现即作废。"""
    if depth > 4:
        return False
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).lower() in FORBIDDEN_KEYS and isinstance(value, (int, float)):
                return True
            if _numbers_present(value, depth + 1):
                return True
        return False
    if isinstance(obj, (list, tuple)):
        return any(_numbers_present(v, depth + 1) for v in obj)
    if isinstance(obj, bool):
        return False
    if isinstance(obj, (int, float)):
        return True
    return False


def IR_by_uid(ir: dict) -> dict:
    from . import cad_ir as CAD
    return CAD.by_uid(ir)


def _instance_rows(family: dict, ir: dict, *, limit: int = 6) -> list:
    by_uid = IR_by_uid(ir)
    rows = []
    for inst in (family.get("instances") or ())[:limit]:
        entities = [by_uid[u] for u in inst.get("uids") or () if u in by_uid]
        rect = SH.min_area_rect(SH.points_of(entities))
        rows.append({"instance": inst.get("id"),
                     "entity_count": len(entities),
                     "kinds": sorted({e.get("kind") for e in entities}),
                     "closed": sum(1 for e in entities if SH.is_closed(e)),
                     "aspect": None if not rect else SH._r(
                         rect["w"] / max(1e-9, rect["h"]), 2),
                     "transform": inst.get("transform")})
    return rows


# ------------------------------------------------------------------ 采样与出图
def _size_of(inst: dict) -> float:
    box = inst.get("bbox")
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return 0.0
    return max(0.0, float(box[2]) - float(box[0])) * max(
        0.0, float(box[3]) - float(box[1]))


def _center_of(inst: dict):
    box = inst.get("bbox")
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return (0.0, 0.0)
    return ((float(box[0]) + float(box[2])) / 2.0,
            (float(box[1]) + float(box[3])) / 2.0)


def sample_instances(family: dict, ir: dict, *, limit: int = 3,
                     noise: float = 2.0) -> list:
    """代表实例 + 若干**有差异**的成员（不是随便再取几个长得一样的）。

    差异按两件事算：尺寸档不同（超过 ``max(4×噪声, 5%)``）、离代表越远。只取长得
    一模一样的成员，模型看不出族内变化；只取离群成员，模型又会把特例当通则。
    """
    rep = family.get("representative") or {}
    instances = [inst for inst in (family.get("instances") or ())
                 if isinstance(inst, dict) and inst.get("id")]
    if not instances:
        return [rep] if rep.get("id") else []
    head = ([inst for inst in instances if inst.get("id") == rep.get("id")]
            or [instances[0]])[0]
    chosen = [head]
    chosen_ids = {head.get("id")}
    diag = 0.0
    box = family.get("bbox")
    if isinstance(box, (list, tuple)) and len(box) == 4:
        diag = max(1e-9, math.hypot(float(box[2]) - float(box[0]),
                                    float(box[3]) - float(box[1])))
    while len(chosen) < max(1, int(limit)):
        best, best_score = None, 0.0
        for inst in instances:
            if inst.get("id") in chosen_ids:
                continue
            # Farthest-first diversity: a new member must differ from every
            # selected member, rather than repeatedly differing from the head.
            distances = []
            size = math.sqrt(_size_of(inst))
            center = _center_of(inst)
            for selected in chosen:
                other_size = math.sqrt(_size_of(selected))
                tolerance = max(4.0 * float(noise),
                                0.05 * max(size, other_size))
                size_delta = float(size > 0 and other_size > 0
                                   and abs(size - other_size) > tolerance)
                other_center = _center_of(selected)
                spread = (math.hypot(center[0] - other_center[0],
                                     center[1] - other_center[1]) / diag) if diag else 0.0
                distances.append(size_delta * 2.0 + min(1.0, spread * 4.0))
            score = min(distances)
            if score > best_score + 1e-9:
                best, best_score = inst, score
        if best is None:
            break
        chosen.append(best)
        chosen_ids.add(best.get("id"))
    return chosen


def _handles_of(ir: dict, uids) -> list:
    by_uid = IR_by_uid(ir)
    handles = []
    for uid in uids or ():
        rec = by_uid.get(uid) or {}
        handle = rec.get("handle")
        if handle:
            handles.append(str(handle))
    return handles


def _indices_of(ir: dict, uids):
    """IR order is the raw snapshot ordinal, including normalization skips."""
    if uids is None:
        return None
    by_uid = IR_by_uid(ir)
    return [int(by_uid[uid]["order"]) - 1 for uid in uids
            if uid in by_uid and by_uid[uid].get("order")]


def _render(snapshot, box, *, walls, thickness, scales=("local", "detail"),
            focus_handles=(), focus_indices=None, numbering=True):
    if not snapshot:
        return {"views": [], "ok": False, "reason": "NO_SNAPSHOT"}
    from .. import vision as VISION
    if focus_indices is not None:
        focus_indices = set(focus_indices)
        raw = list(snapshot.get("entities") or ()) + list(snapshot.get("derived") or ())
        inserts = {str(raw[i].get("handle")) for i in focus_indices
                   if 0 <= i < len(raw) and raw[i].get("type") == "INSERT"
                   and raw[i].get("handle")}
        # An INSERT's IR bbox is just its insertion point; frame its visible children.
        child_boxes = [VISION._bbox_of(rec) for rec in raw
                       if str((rec.get("via") or {}).get("insert") or "") in inserts]
        boxes = [b for b in [box] + child_boxes if b]
        if inserts and boxes:
            box = [min(b[0] for b in boxes), min(b[1] for b in boxes),
                   max(b[2] for b in boxes), max(b[3] for b in boxes)]
    return VISION.render_views(snapshot, {"bbox": box}, scales=scales, walls=walls,
                               thickness=thickness, focus_handles=focus_handles,
                               numbering=numbering, focus_indices=focus_indices)


def _image_meta(view, **mapping):
    """Audit exactly the bytes sent, independently of renderer byte counts."""
    payload = base64.b64decode(view["b64"])
    return dict({key: view.get(key) for key in
                 ("scale", "width", "height", "focus", "focus_box", "numbers")},
                **mapping, bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(), b64=view["b64"])


def _reply_audit(reply):
    audit = reply.as_dict() if hasattr(reply, "as_dict") else {}
    return dict(audit, text=getattr(reply, "text", ""))


def _sample_views(samples, *, ir, snapshot, walls, thickness, cfg) -> tuple[list, list]:
    """按样本出图，返回 ``(images_b64, image_meta)``；代表给紧视图+细节图。"""
    images: list = []
    meta: list = []
    cap = max(1, int(cfg["images_per_family"]))
    for index, sample in enumerate(samples):
        if len(images) >= cap:
            break
        box = sample.get("bbox")
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            continue
        handles = _handles_of(ir, sample.get("uids"))
        if sample.get("handle"):
            handles.append(str(sample["handle"]))
        scales = ("local", "detail") if index == 0 else ("local",)
        views = _render(snapshot, box, walls=walls, thickness=thickness,
                        scales=scales, focus_handles=handles,
                        focus_indices=_indices_of(ir, sample.get("uids")))
        for view in views.get("views") or ():
            if len(images) >= cap:
                break
            if view.get("status") != "rendered" or not view.get("b64"):
                continue
            images.append(view["b64"])
            meta.append(_image_meta(view, image=len(images),
                                    instance=sample.get("id"),
                                    role="representative" if index == 0 else "sample"))
    return images, meta


def _overview_view(family: dict, *, ir, snapshot, walls, thickness) -> tuple[list, list]:
    """族分布总览：一张图里同时高亮**全部成员**（取景 = 成员外接框外扩）。

    局部图只能回答"这一个长什么样"；"这些方块是成行成列地落在墙的交点上，还是
    零散地压在墙面上"只有放在整张平面里才看得出来。不编号（几十个编号会盖住
    图形），只把成员画成橙色。画不出或一个成员都没高亮时返回空——没有指出目标
    的总览图不发给模型。
    """
    members = [m for m in (family.get("instances") or ())
               if isinstance(m, dict) and isinstance(m.get("bbox"), (list, tuple))
               and len(m["bbox"]) == 4]
    if len(members) < 2 or not snapshot:
        return [], []
    union = SH._union([m["bbox"] for m in members])
    span = max(union[2] - union[0], union[3] - union[1], 1.0)
    pad = max(4.0 * float(thickness or 0.0), 0.06 * span)
    box = [union[0] - pad, union[1] - pad, union[2] + pad, union[3] + pad]
    uids = [u for m in members for u in (m.get("uids") or ())]
    handles = _handles_of(ir, uids) + [str(m["handle"]) for m in members
                                       if m.get("handle")]
    views = _render(snapshot, box, walls=walls, thickness=thickness,
                    scales=("overview",), focus_handles=handles,
                    focus_indices=_indices_of(ir, uids) if uids else None,
                    numbering=False)
    for view in views.get("views") or ():
        if (view.get("status") == "rendered" and view.get("b64")
                and int(view.get("focus") or 0) > 0):
            return [view["b64"]], [_image_meta(view, image=1, instance=None,
                                               role="family_overview",
                                               members=len(members))]
    return [], []


def _scope_text(family: dict) -> str:
    if str(family.get("authorization_scope") or "family") == "representative_only":
        return ("the representative instance ONLY. Members of this family have too "
                "little shared structure to authorize them automatically, so your "
                "answer is applied to the representative instance only.")
    return ("the whole family: the program will apply your answer to every instance "
            "that matches this drawing method, after geometric verification.")


# ------------------------------------------------------------------ 主流程
def read_family(family: dict, *, ir: dict, graph=None, gateway=None, snapshot=None,
                walls=(), budget=None, failures=(), widths=()) -> dict:
    """Read a family; unavailable gateways return ``status="no_model"``.

    ``budget["verification"] = True`` samples only holdout members, excluding
    ``budget["primary_sample_ids"]`` (instance IDs from the first result's
    ``samples``). If omitted, the normal deterministic sample is excluded.
    No holdout members/images returns an error without a model call.
    """
    cfg = dict(BUDGET, **(budget or {}))
    samples = sample_instances(family, ir, limit=int(cfg["samples"]),
                               noise=float((ir.get("basis") or {}).get("noise") or 2.0))
    verification = bool(cfg.get("verification"))
    if verification:
        primary_ids = set(cfg.get("primary_sample_ids", [s.get("id") for s in samples]))
        holdouts = [s for s in family.get("instances") or ()
                    if s.get("id") and s.get("id") not in primary_ids]
        held_family = dict(family, instances=holdouts,
                           representative=holdouts[0] if holdouts else {})
        samples = sample_instances(held_family, ir, limit=int(cfg["samples"]),
                                   noise=float((ir.get("basis") or {}).get("noise") or 2.0))
    observed_family = dict(family, representative=samples[0] if samples else {})
    facts = FAM.family_facts(observed_family, ir, walls=walls, widths=widths,
                             layout=bool(cfg.get("layout_facts")))
    facts["observed_instance"] = samples[0].get("id") if samples else None
    result = {"schema": SCHEMA, "family": family.get("id"),
              "status": "no_model", "category": None, "subtype": None,
              "confidence": None, "parts": {}, "relations": [],
              "alternatives": [], "negatives": [], "images": 0, "calls": 0,
              "gateway": {}, "prompt_version": PROMPT_VERSION, "facts": facts,
              "samples": [{"instance": s.get("id"), "bbox": s.get("bbox"),
                           "handles": _handles_of(ir, s.get("uids"))}
                          for s in samples],
              "geometry_guess": family.get("category_guess"),
              "family_consistent": None, "outliers": [],
              "scope": family.get("authorization_scope") or "family"}
    if verification and not samples:
        result.update(status="error", reason="NO_HOLDOUT_MEMBERS")
        return result
    if gateway is None or not getattr(gateway, "available", False):
        result["reason"] = "GATEWAY_UNAVAILABLE"
        return result
    thickness = float((ir.get("basis") or {}).get("thickness") or 200.0)
    images, image_meta = _sample_views(samples, ir=ir, snapshot=snapshot, walls=walls,
                                       thickness=thickness, cfg=cfg)
    if verification and not images:
        result.update(status="error", reason="NO_HOLDOUT_IMAGES")
        return result
    if cfg.get("overview") and images:
        # 总览图放在最前：先看分布，再看局部；编号随之顺延
        over, over_meta = _overview_view(family, ir=ir, snapshot=snapshot,
                                         walls=walls, thickness=thickness)
        if over:
            images = over + images
            image_meta = over_meta + image_meta
            for number, meta in enumerate(image_meta, start=1):
                meta["image"] = number
    result["overview"] = any(m.get("role") == "family_overview" for m in image_meta)
    instance_block = _dumps(_instance_rows(dict(family, instances=samples), ir,
                                          limit=len(samples)))
    image_block = _dumps([{k: v for k, v in meta.items() if k != "b64"}
                         for meta in image_meta]) if image_meta else "no images available"
    prompt = PROMPT.format(
        facts=_dumps(facts)[:int(cfg["prompt_facts"])],
        instances=instance_block,
        images=image_block,
        scope=_scope_text(family),
        categories=list(FOCUS) + list(CONTEXT_CATEGORIES) + ["none"],
        subtypes=_dumps(SUBTYPES),
        primitives=sorted(GRAMMAR.PRIMITIVES),
        relations=sorted(GRAMMAR.RELATIONS),
        negatives=list(NEGATIVES))
    result["request"] = {"system": SYSTEM, "prompt": prompt,
                         "images": image_meta, "image_count": len(images)}
    reply = gateway.ask(system=SYSTEM, prompt=prompt, images=images,
                        vision=bool(images))
    result["calls"] = 1
    result["images"] = int(getattr(reply, "images", 0) or 0)
    result["gateway"] = _reply_audit(reply)
    result["reply"] = result["gateway"]
    result["raw_text"] = getattr(reply, "text", "")
    if not getattr(reply, "ok", False):
        result.update({"status": "error",
                       "reason": getattr(reply, "code", "MODEL_ERROR")})
        return result
    raw = reply.data if isinstance(reply.data, dict) else {}
    result["raw"] = raw
    reading, error = _parse_component(raw)
    if error:
        result.update({"status": "rejected", "reason": error})
        return result
    result["need_closeup"] = bool(raw.get("need_closeup"))
    # 模型说"看不清、要近看"：真的补一张更紧的图再问一次，而不是记下就算了
    if result["need_closeup"] and int(cfg["closeup"]) > 0 and snapshot and samples:
        close = _closeup(samples[0], ir=ir, snapshot=snapshot, walls=walls,
                         thickness=thickness, facts=facts, cfg=cfg,
                         gateway=gateway)
        if close:
            result["closeup"] = close
            result["calls"] += int(close.get("calls") or 0)
            if close.get("status") == "ok":
                reading = close["reading"]
                raw = close.get("raw") or raw
                result["need_closeup"] = False
                result["closeup_used"] = True
    if result["need_closeup"]:
        result.update(status="error", reason="CLOSEUP_UNANSWERED")
        return result
    result.update(_finish(reading, family=family, raw=raw))
    if (result.get("closeup_used") or
            (images and (reading.get("family_consistent") is not True
                         or len({m.get("instance") for m in image_meta}) < 2))):
        result["authorization_scope"] = "representative_only"
        result["scope"] = "representative_only"
    return result


def _closeup(sample, *, ir, snapshot, walls, thickness, facts, cfg,
             gateway=None) -> dict:
    """补看：只画代表实例的紧视图，再问一次同一个问题。"""
    box = sample.get("bbox")
    handles = _handles_of(ir, sample.get("uids"))
    if sample.get("handle"):
        handles.append(str(sample["handle"]))
    views = _render(snapshot, box, scales=("closeup",), walls=walls,
                    thickness=thickness, focus_handles=handles, numbering=True,
                    focus_indices=_indices_of(ir, sample.get("uids")))
    sent = [v for v in views.get("views") or ()
            if v.get("status") == "rendered" and v.get("b64")][:1]
    images = [v["b64"] for v in sent]
    meta = [_image_meta(v, image=i + 1, instance=sample.get("id"),
                        role="representative") for i, v in enumerate(sent)]
    prompt = CLOSEUP_PROMPT.format(
        facts=_dumps(facts)[:int(cfg["prompt_facts"])],
        instance=_dumps({"instance": sample.get("id"), "bbox": box,
                         "handles": handles})[:800],
        categories=list(FOCUS) + list(CONTEXT_CATEGORIES) + ["none"])
    out = {"request": {"system": SYSTEM, "prompt": prompt, "images": meta,
                       "image_count": len(images)},
           "status": "no_model", "calls": 0}
    if gateway is None or not getattr(gateway, "available", False):
        out["reason"] = "GATEWAY_UNAVAILABLE"
        return out
    if not images:
        out.update(status="error", reason="NO_CLOSEUP_IMAGES")
        return out
    reply = gateway.ask(system=SYSTEM, prompt=prompt, images=images,
                        vision=bool(images))
    out["calls"] = 1
    out["reply"] = _reply_audit(reply)
    out["raw_text"] = getattr(reply, "text", "")
    if not getattr(reply, "ok", False):
        out.update({"status": "error", "reason": getattr(reply, "code", "MODEL_ERROR")})
        return out
    raw = reply.data if isinstance(reply.data, dict) else {}
    out["raw"] = raw
    reading, error = _parse_component(raw)
    if error:
        out.update({"status": "rejected", "reason": error})
        return out
    if raw.get("need_closeup") or raw.get("verdict") not in {"keep", "reject"}:
        out.update(status="rejected", reason="INVALID_CLOSEUP_DECISION")
        return out
    if raw.get("verdict") == "reject":
        reading["category"] = "none"
        reading["subtype"] = "other"
    out.update({"status": "ok", "reading": reading})
    return out


def _parse_component(raw: dict) -> tuple[dict, str | None]:
    """闭集校验。返回 ``(reading, error_code)``；任何不合规都只给错误码。"""
    if not raw:
        return {}, "EMPTY_RESPONSE"
    reading = raw.get("component") if isinstance(raw.get("component"), dict) else raw
    if not isinstance(reading, dict) or not reading:
        return {}, "MALFORMED_RESPONSE"
    if _numbers_present(reading):
        return {}, "MODEL_GEOMETRY"
    category = str(reading.get("category") or "").lower()
    if category not in FOCUS + CONTEXT_CATEGORIES + ("none",):
        return {}, "UNKNOWN_CATEGORY"
    confidence = str(reading.get("confidence") or "").lower()
    if confidence not in {"high", "medium", "low"}:
        return {}, "UNKNOWN_CONFIDENCE"
    subtype = str(reading.get("subtype") or "unknown").lower()
    if subtype not in SUBTYPES.get(category, ("unknown",)):
        subtype = "unknown"
    parts = reading.get("parts") if isinstance(reading.get("parts"), dict) else {}
    relations = (reading.get("relations")
                 if isinstance(reading.get("relations"), list) else [])
    negatives = [str(n).lower() for n in (reading.get("negative_features") or ())
                 if str(n).lower() in NEGATIVES]
    if category in CONTEXT_CATEGORIES and "wall" not in negatives:
        negatives.append("wall")
    alternatives = []
    for alt in (raw.get("alternatives") or ())[:2]:
        if not isinstance(alt, dict):
            continue
        alt_cat = str(alt.get("category") or "").lower()
        if alt_cat in FOCUS:
            alternatives.append({"category": alt_cat,
                                 "subtype": str(alt.get("subtype") or "unknown").lower(),
                                 "why": str(alt.get("why") or "")[:120]})
    return {"category": category, "subtype": subtype, "confidence": confidence,
            "parts": {str(k): str(v) for k, v in parts.items()},
            "relations": [list(r) for r in relations if isinstance(r, list)],
            "negatives": negatives,
            "note": str(reading.get("note") or "")[:160],
            "alternatives": alternatives,
            "family_consistent": (raw.get("family_consistent")
                                  if isinstance(raw.get("family_consistent"), bool)
                                  else None),
            "outliers": [str(o) for o in (raw.get("outliers") or ())][:12]}, None


def _finish(reading: dict, *, family: dict, raw: dict) -> dict:
    """收尾：语法只是"组成描述"，写不进目录就记一笔，**不能因此丢掉类别判断**。

    实测：模型答 "square filled hatch → column/medium"，parts 用了目录外的原语名，
    旧行为会把整条 reading 判成 GRAMMAR_INVALID —— 类别答案明明合规却被丢掉。
    现在类别/把握走了闭集校验就成立，语法不合法只写 ``grammar_error``。
    """
    grammar = None
    grammar_error = None
    if reading.get("parts"):
        try:
            grammar = GRAMMAR.from_model({"category": reading["category"],
                                          "subtype": reading["subtype"],
                                          "parts": reading["parts"],
                                          "relations": reading["relations"]})
        except GRAMMAR.GrammarError as exc:
            grammar_error = exc.as_dict()
    consistent = reading.get("family_consistent")
    outliers = [o for o in reading.get("outliers") or () if o]
    return {"status": "ok", "category": reading["category"],
            "subtype": reading["subtype"], "confidence": reading["confidence"],
            "parts": reading["parts"], "relations": reading["relations"],
            "negatives": reading["negatives"], "note": reading.get("note"),
            "alternatives": reading["alternatives"],
            "grammar": grammar.get("hash") if grammar else None,
            "grammar_error": grammar_error,
            "agrees_with_geometry_guess": reading["category"]
            == (family.get("category_guess") or reading["category"]),
            "family_consistent": consistent,
            # 模型明说成员不一致：只授权代表实例，绝不整族推广
            "authorization_scope": ("representative_only"
                                    if (consistent is False or outliers) else
                                    (family.get("authorization_scope") or "family")),
            "outliers": outliers,
            "raw_consistent": bool(consistent) if consistent is not None else None}


def recheck(candidates, *, ir: dict, gateway=None, snapshot=None, walls=(),
            failures=(), budget=None) -> dict:
    """疑难构件主动复查（P2）：只对"低可信/有冲突"的实例再问一次模型。"""
    cfg = dict(BUDGET, **(budget or {}))
    out = {"schema": SCHEMA, "status": "off", "verdicts": [], "calls": 0,
           "images_seen": 0, "audit": []}
    if gateway is None or not getattr(gateway, "available", False):
        out["reason"] = "GATEWAY_UNAVAILABLE"
        return out
    thickness = float((ir.get("basis") or {}).get("thickness") or 200.0)
    seen: set = set()
    for hyp in candidates or ():
        if len(out["verdicts"]) >= int(cfg["recheck"]):
            break
        family_id = hyp.get("family_id") or hyp.get("id")
        if family_id in seen:
            continue
        seen.add(family_id)
        box = hyp.get("bbox")
        handles = [str(h) for h in (hyp.get("source_handles") or ()) if h]
        views = _render(snapshot, box, walls=walls, thickness=thickness,
                        scales=("detail",), focus_handles=handles, numbering=True,
                        focus_indices=_indices_of(ir, hyp.get("source_uids")))
        sent = [v for v in views.get("views") or ()
                if v.get("status") == "rendered" and v.get("b64")][:1]
        images = [v["b64"] for v in sent]
        facts = {"category": hyp.get("category"), "subtype": hyp.get("subtype"),
                 "status": hyp.get("status"), "score": hyp.get("score"),
                 "engine": hyp.get("engine"),
                 "negatives": [n.get("code") for n in
                               ((hyp.get("evidence") or {}).get("negatives") or ())],
                 "on_grid": (hyp.get("evidence") or {}).get("on_grid"),
                 "touches_wall": (hyp.get("evidence") or {}).get("touches_wall"),
                 "instances_in_family": (hyp.get("evidence") or {}).get(
                     "instances_in_family")}
        prompt = RECHECK_PROMPT.format(
            facts=_dumps(facts)[:1600],
            failures=_dumps(list(failures or ())[:6]),
            categories=list(FOCUS) + ["none"])
        out["audit"].append({"id": hyp.get("id"), "request": {
            "system": SYSTEM, "prompt": prompt,
            "image_count": len(images),
            "images": [_image_meta(v, image=i + 1, instance=hyp.get("id"),
                                   role="candidate") for i, v in enumerate(sent)]}})
        reply = gateway.ask(system=SYSTEM, prompt=prompt, images=images,
                            vision=bool(images))
        out["calls"] += 1
        out["images_seen"] += int(getattr(reply, "images", 0) or 0)
        out["audit"][-1]["reply"] = _reply_audit(reply)
        out["audit"][-1]["raw_text"] = getattr(reply, "text", "")
        out["audit"][-1]["raw"] = (reply.data
                                   if isinstance(getattr(reply, "data", None), dict)
                                   else None)
        if not getattr(reply, "ok", False):
            out["verdicts"].append({"id": hyp.get("id"), "verdict": "unavailable",
                                    "reason": getattr(reply, "code", "MODEL_ERROR")})
            continue
        raw = reply.data if isinstance(reply.data, dict) else {}
        reading, error = _parse_component(raw)
        verdict = str(raw.get("verdict") or "").lower()
        if error or verdict not in {"keep", "reject"}:
            out["verdicts"].append({"id": hyp.get("id"), "verdict": "unknown",
                                    "reason": error or "INVALID_MODEL_DECISION"})
            continue
        if reading["category"] == "none" or verdict == "reject":
            out["verdicts"].append({"id": hyp.get("id"), "verdict": "reject",
                                    "negatives": reading["negatives"],
                                    "note": reading.get("note")})
        else:
            out["verdicts"].append({
                "id": hyp.get("id"), "verdict": "keep",
                "category": reading["category"],
                "subtype": reading["subtype"],
                "confidence": reading["confidence"],
                "note": reading.get("note")})
    out["status"] = "ok" if out["verdicts"] else "empty"
    return out


def summary(result) -> dict:
    result = result or {}
    return {"schema": SCHEMA, "family": result.get("family"),
            "status": result.get("status"), "category": result.get("category"),
            "subtype": result.get("subtype"),
            "confidence": result.get("confidence"),
            "negatives": result.get("negatives") or [],
            "images": result.get("images"), "calls": result.get("calls"),
            "grammar_error": bool(result.get("grammar_error")),
            "family_consistent": result.get("family_consistent"),
            "outliers": result.get("outliers") or [],
            "scope": result.get("scope")}


__all__ = ["BUDGET", "FOCUS", "NEGATIVES", "PROMPT_VERSION", "SCHEMA", "SUBTYPES",
           "read_family", "recheck", "sample_instances", "summary"]
