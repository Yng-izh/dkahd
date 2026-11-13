"""First-pass multimodal CAD understanding, independent of residual recognition.

A vision model observes the whole drawing and representative geometric regions before
candidate resolution. It may propose only allowlisted component grammars. Coordinates,
geometry and acceptance remain the responsibility of the deterministic fitter/resolver.
"""
from __future__ import annotations

import json
import math

from . import component_grammar as GRAMMAR

SCHEMA = "multimodal-bootstrap/1.0"
PROMPT_VERSION = "multimodal-bootstrap/1.0"
DEFAULTS = {"max_regions": 6, "max_calls": 5, "max_grammars_per_region": 3,
            "max_entities_in_region": 280, "max_families": 3}

DOMAIN_KNOWLEDGE = {
    "wall": "double/single/curved faces, connected runs, corners and discontinuities",
    "column": "rectangular or irregular closed contours and repeated structural supports",
    "window": "parallel window frames, dividers, wall penetrations, corner glazing",
    "door": "swing arc and leaf, sliding leaves, framed opening without an arc",
    "stair": "repeating treads, landing and direction change",
    "room": "enclosed or open space bounded by walls, with nearby space labels",
    "negative": "furniture, dimensions, annotation, fixtures, legends and title frames",
    "invariance": "layer names, block names and orientation are weak hints; prefer topology",
}

SYSTEM = (
    "You are a multimodal architectural CAD interpreter. Understand the overall plan "
    "and propose component structures from visible vector evidence. "
    "You must not invent entities, coordinates, dimensions, bounding boxes or executable code. "
    "A deterministic CAD geometry fitter will calculate exact objects. Return only JSON."
)

PROMPT = """FIRST-PASS CAD INTERPRETATION. You are not limited to entities missed by rules.
This region has not yet been adjudicated. Different categories may compete.

Region facts (authoritative entity ids and geometry summaries):
{facts}

Drawing-wide scene understanding from the overview:
{scene}

Architectural knowledge (generic, not drawing-specific):
{knowledge}

Allowed grammar vocabulary:
{catalog}

Rules:
- For the overview region: describe the scene in a compact \"scene\" object, grammars=[].
- For a local region: propose up to {limit} component grammars with parts and relations.
- Grammars must use only catalog categories, primitives, relations and anchors.
- Do not include geometry, coordinates, lengths, unit thresholds, or new entity ids.
- Layer names and family names are weak hints; do not classify using names alone.
- Prefer geometry-backed categories; exclude furniture/annotations when uncertain.
- Images are context; precise geometry must be recovered from CAD entities.

Respond as JSON:
{{"scene":{{"layout":"...", "suspected_patterns":["..."]}},
  "grammars":[{{"category":"window","subtype":"punched_window",
    "parts":{{"frame":"parallel_line_pair","ends":"short_connectors"}},
    "relations":[["ends","connect","frame"]],
    "fit_mode":"constrained_geometry","instances":"find_similar"}}]}}
"""


def _valid_bbox(box):
    return (isinstance(box, (list, tuple)) and len(box) == 4
            and all(isinstance(v, (int, float)) and math.isfinite(v) for v in box)
            and box[0] <= box[2] and box[1] <= box[3])


def _union(boxes):
    boxes = [b for b in boxes if _valid_bbox(b)]
    if not boxes:
        return None
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _intersects(a, b):
    return _valid_bbox(a) and _valid_bbox(b) and not (
        a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def _quantile(values, frac):
    vals = sorted(values)
    if not vals:
        return 0.0
    return vals[min(len(vals) - 1, int(frac * (len(vals) - 1)))]


def _robust_bbox(entities):
    """Trim far-out annotation points without dropping structural outer faces."""
    structures = [e for e in entities if e.get("kind") not in ("text", "unknown")
                  and _valid_bbox(e.get("bbox"))]
    if not structures:
        structures = [e for e in entities if _valid_bbox(e.get("bbox"))]
    if not structures:
        return None
    if len(structures) < 30:
        return _union([e["bbox"] for e in structures])
    centers_x = [(e["bbox"][0] + e["bbox"][2]) / 2 for e in structures]
    centers_y = [(e["bbox"][1] + e["bbox"][3]) / 2 for e in structures]
    lo_x, hi_x = _quantile(centers_x, .02), _quantile(centers_x, .98)
    lo_y, hi_y = _quantile(centers_y, .02), _quantile(centers_y, .98)
    keep = [e["bbox"] for e, x, y in zip(structures, centers_x, centers_y)
            if lo_x <= x <= hi_x and lo_y <= y <= hi_y]
    return _union(keep) or _union([e["bbox"] for e in structures])


def select_regions(ir, *, max_regions=6):
    """Overview + representative block families + spatial tiles, even with no residual."""
    entities = ir.get("entities") or []
    bbox = _robust_bbox(entities)
    if bbox is None:
        return []
    thickness = float((ir.get("basis") or {}).get("thickness") or 200)
    by_uid = {e["uid"]: e for e in entities}
    cap = int(DEFAULTS["max_entities_in_region"])

    def make(name, box, *, seeds=(), kind="local"):
        pad = max(1., thickness * (1.5 if kind == "family" else .1))
        area = [box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad]
        members = [e["uid"] for e in entities if _intersects(e.get("bbox"), area)]
        if not members:
            return None
        return {"id": name, "kind": kind, "level": 0 if kind == "global" else 1,
                "bbox": area, "uids": members[:cap],
                "seed_uids": list(seeds)[:32] or members[:24]}

    # Overview always runs first; it is not a detector and produces no geometry.
    overview = make("boot:overview", bbox, kind="global")
    if overview is None:
        return []
    regions = [overview]
    seen = set()
    for fam in (ir.get("families") or ()):
        if len(regions) >= int(max_regions):
            break
        uids = [u for u in fam.get("uids") or () if u in by_uid]
        if not uids or (fam.get("hash") or "") in seen:
            continue
        first = by_uid[uids[0]]
        insert_handle = first.get("handle")
        pieces = [first]
        for ent in entities:
            if (ent.get("via") or {}).get("insert") == insert_handle:
                pieces.append(ent)
        fam_box = _union([e.get("bbox") for e in pieces])
        if not fam_box:
            continue
        region = make("boot:family:" + str(fam.get("hash") or len(seen))[:12],
                      fam_box, seeds=[e["uid"] for e in pieces], kind="family")
        if region is not None:
            seen.add(fam.get("hash") or "")
            regions.append(region)
        if len(regions) >= min(max_regions, 1 + DEFAULTS["max_families"]):
            break

    # Tiles cover ordinary line-only drawings and regions already classified by rules.
    mid_x = (bbox[0] + bbox[2]) / 2
    mid_y = (bbox[1] + bbox[3]) / 2
    tiles = [(bbox[0], bbox[1], mid_x, mid_y),
             (mid_x, bbox[1], bbox[2], mid_y),
             (bbox[0], mid_y, mid_x, bbox[3]),
             (mid_x, mid_y, bbox[2], bbox[3])]
    scored = []
    for idx, tile in enumerate(tiles):
        count = sum(_intersects(e.get("bbox"), tile) and e.get("kind") != "text"
                    for e in entities)
        if count >= 4:
            scored.append((-count, idx, tile))
    for _, idx, tile in sorted(scored):
        if len(regions) >= max_regions:
            break
        region = make(f"boot:tile:{idx}", tile, kind="tile")
        if region is not None:
            regions.append(region)
    if len(regions) == 1:
        fallback = make("boot:whole-plan", bbox, kind="local")
        if fallback:
            regions.append(fallback)
    return regions


def _scene(raw):
    if not isinstance(raw, dict):
        return {}
    scene = raw.get("scene")
    if not isinstance(scene, dict):
        return {}
    result = {}
    for key in ("layout", "building_type"):
        if isinstance(scene.get(key), str):
            result[key] = scene[key][:160]
    hints = scene.get("suspected_patterns") or []
    if isinstance(hints, list):
        result["suspected_patterns"] = [s[:100] for s in hints[:8]
                                         if isinstance(s, str)]
    return result


def run(*, ir, graph, snapshot, gateway, ai=None, budget=None, walls=()):
    """Send overview then local crops to one VLM gateway, within one shared budget."""
    from . import agent_controller as AGENT
    from .. import vision as VISION

    cfg = dict(DEFAULTS, **(budget or {}))
    result = {"schema": SCHEMA, "status": "off", "items": [], "regions": [],
              "scene": {}, "calls": 0, "images_seen": 0, "prompt_version": PROMPT_VERSION}
    if gateway is None or not getattr(gateway, "available", False):
        result["status"] = "unavailable"
        return result
    if not snapshot:
        result["status"] = "no_snapshot"
        return result
    regions = select_regions(ir, max_regions=int(cfg["max_regions"]))
    limit = min(int(cfg["max_calls"]), len(regions),
                int(cfg.get("max_model_calls", cfg["max_calls"])))
    model_vision = bool((ai or {}).get("provider", {}).get("vision", True))
    for reg in regions[:limit]:
        overview = reg["kind"] == "global"
        facts = AGENT.observe(ir, graph, reg)
        facts["seed_uids"] = reg["seed_uids"]
        # Only the regional geometry may be used for fitting; nothing is inferred
        # from the absolute position of a model-generated bounding box.
        if model_vision:
            views = VISION.render_views(snapshot,
                                        {"bbox": reg["bbox"],
                                         "respect_focus_global": True},
                                        scales=("global",) if overview else ("local", "detail"),
                                        walls=walls,
                                        thickness=(ir.get("basis") or {}).get("thickness") or 0)
        else:
            views = {"views": [], "ok": False, "reason": "VISION_DISABLED"}
        images = [v["b64"] for v in views.get("views") or ()
                  if v.get("status") == "rendered" and v.get("b64")][:2]
        prompt = PROMPT.format(
            facts=json.dumps(facts, ensure_ascii=False, default=str)[:4000],
            scene=json.dumps(result["scene"], ensure_ascii=False)[:1000],
            knowledge=json.dumps(DOMAIN_KNOWLEDGE, ensure_ascii=False),
            catalog=json.dumps(GRAMMAR.catalog(), ensure_ascii=False)[:5000],
            limit=0 if overview else int(cfg["max_grammars_per_region"]))
        raw = gateway.ask(system=SYSTEM, prompt=prompt, images=images,
                          vision=bool(images))
        result["calls"] += 1
        result["images_seen"] += int(getattr(raw, "images", 0) or 0)
        row = {"region": reg["id"], "kind": reg["kind"], "ok": bool(raw.ok),
               "images": int(getattr(raw, "images", 0) or 0),
               "reason": None if raw.ok else getattr(raw, "code", "MODEL_ERROR"),
               "grammars": 0}
        if raw.ok and isinstance(raw.data, dict):
            if overview:
                result["scene"] = _scene(raw.data)
            else:
                valid = []
                suggested = raw.data.get("grammars")
                if isinstance(suggested, list):
                    for obj in suggested[:int(cfg["max_grammars_per_region"])]:
                        try:
                            valid.append(GRAMMAR.from_model(obj))
                        except (GRAMMAR.GrammarError, TypeError, ValueError):
                            continue
                if valid:
                    # Spatial tiles must fit against *all* geometry in their
                    # ROI. Their first 24 uids are only visual/query samples.
                    # Family regions, in contrast, may safely focus on the
                    # originating insert and its expanded children.
                    fit_seeds = (reg["seed_uids"] if reg["kind"] == "family" else [])
                    result["items"].append({"region": reg, "seed_uids": fit_seeds,
                                            "grammars": valid, "case_id": reg["id"]})
                row["grammars"] = len(valid)
        result["regions"].append(row)
    result["status"] = "ok" if result["calls"] else "empty"
    return result
