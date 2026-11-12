"""房间名候选：图层清单只给优先级，不当准入条件（证据兜底）。

判据：
  1. 图层在 profile 的 room_name 清单里          -> source="profile"
  2. 未登记但形态像房名（短文本、字高 120~800mm、
     非纯数字/轴号/门窗编号/图名图号/材料说明）   -> source="fallback"
  3. 同名反复出现（办公室、卫生间…）            -> 加分
  4. 无任何证据 -> 进 excluded（可审计，不静默丢弃）
"""
from __future__ import annotations

import re

NAME_MAX = 12
HEIGHT_RANGE = (120.0, 800.0)
KEYWORDS = ("图名", "图号", "比例", "设计", "制图", "校对", "审核", "说明", "做法",
            "详图", "索引", "编号", "标高", "材料", "洞口", "门窗表", "大样",
            "层平面", "剖面", "立面", "总平面", "注", "见", "参见")
NUMERIC = re.compile(r"^[\d\.\-/×xX*\s]+$")
AXIS = re.compile(r"^[A-Za-z]{1,3}[-–]?\d{0,3}[a-zA-Z]?$")
CODE = re.compile(r"^[A-Z]{2,4}\d{2,}$")
PERCENT = re.compile(r"^\d+(\.\d+)?%$")
DIMENSION = re.compile(r"^\d+(\.\d+)?\s*[xX×]\s*\d+(\.\d+)?$|^\d+$")
AREA_RE = re.compile(r"^\d+(\.\d+)?\s*(m2|m²|㎡|平方米)$", re.I)


def _point(t):
    for key in ("insert", "insert_pt"):
        v = t.get(key)
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            try:
                return float(v[0]), float(v[1])
            except (TypeError, ValueError):
                continue
    try:
        return float(t.get("x")), float(t.get("y"))
    except (TypeError, ValueError):
        return None


def looks_like_room(text: str, height) -> tuple[bool, list[str]]:
    if not text or len(text) > NAME_MAX:
        return False, ["too_long"]
    if NUMERIC.match(text):
        return False, ["numeric"]
    if AXIS.match(text) or CODE.match(text):
        return False, ["axis_or_code"]
    if any(k in text for k in KEYWORDS):
        return False, ["keyword"]
    h = float(height or 0.0)
    reasons = []
    if h and not (HEIGHT_RANGE[0] <= h <= HEIGHT_RANGE[1]):
        return False, ["height_out_of_range"]
    if not h:
        reasons.append("no_height")
    return True, reasons


def collect(texts, *, layers=(), rank=None) -> dict:
    layers = set(layers or ())
    rank = dict(rank or {})
    labels, excluded = [], []
    seen: dict[str, int] = {}

    for t in texts or ():
        text = (t.get("text") or "").strip()
        pt = _point(t)
        if pt is None:
            excluded.append({"handle": t.get("handle"), "text": text,
                             "reason": "no_point"})
            continue
        x, y = pt
        height = t.get("height") or t.get("h")
        in_profile = t.get("layer") in layers
        ok, reasons = looks_like_room(text, height)
        if not in_profile and not ok:
            excluded.append({"handle": t.get("handle"), "text": text,
                             "layer": t.get("layer"), "reason": ",".join(reasons)})
            continue
        seen[text] = seen.get(text, 0) + 1
        score = 1.0 if in_profile else 0.55
        if len(text) <= 6:
            score += 0.05
        labels.append({"text": text, "x": x, "y": y, "insert": [x, y],
                       "height": height, "handle": t.get("handle"),
                       "layer": t.get("layer"),
                       "area": t.get("area") or t.get("declared_area"),
                       "source": "profile" if in_profile else "fallback",
                       "layer_rank": rank.get(t.get("layer"), 99),
                       "score": round(min(1.0, score), 2),
                       "reasons": list(reasons)})

    for lab in labels:
        if seen.get(lab["text"], 0) >= 2:
            lab["score"] = round(min(1.0, lab["score"] + 0.1), 2)
            lab["reasons"].append("repeated")
        if lab["source"] == "fallback" and lab["score"] < 0.55:
            lab["reasons"].append("weak_fallback")
    labels.sort(key=lambda l: (l["layer_rank"], -l["score"]))
    return {"labels": labels, "excluded": excluded[:200],
            "stats": {"total": len(labels) + len(excluded),
                      "profile": sum(1 for l in labels if l["source"] == "profile"),
                      "fallback": sum(1 for l in labels if l["source"] == "fallback"),
                      "excluded": len(excluded)}}


def prefilter(labels):
    """确定性预滤：明显的坡度/尺寸/面积/说明文字先摘出去，不等模型。"""
    keep, drop = [], []
    for lab in labels or ():
        text = (lab.get("text") or "").strip()
        reason = None
        if PERCENT.match(text):
            reason = "percent"
        elif AREA_RE.match(text):
            reason = "area_value"
        elif DIMENSION.match(text):
            reason = "dimension_or_number"
        elif text.endswith(("：", ":")):
            reason = "caption_colon"
        elif any(k in text for k in ("留洞", "洞顶", "详见", "参见", "同左", "同右")):
            reason = "note_text"
        if reason:
            drop.append(dict(lab, role="annotation", reason=reason))
        else:
            keep.append(dict(lab, role="pending"))
    return {"room_candidates": keep, "annotations": drop}


# Evidence-backed lexical anchors.  Unknown functional names are admitted
# only when their text layer has a strong population of these anchors AND the
# text lies on an observed/possible building face.  Do not promote every short
# annotation from title sheets, fixtures or assembly notes.
ROOM_ANCHOR = re.compile(
    r"(室|厅|间|房|廊|厕|井|梯|库|所|卧|铺|阳台|大堂|大厅|前室|走道|通道|门厅|中心|" 
    r"药房|护士站|更衣|配电|开闭所|机房|办公室|街|候梯|侯梯|值班|卫生间|" 
    r"等候|清洁|发放|设备|控制|治疗|抢救|观察|过厅|会议|财务|接待|办公)$")
ROOM_BAD = re.compile(
    r"(详|见|图|说明|施工|填充|厂家|专业|设计|防火分区隔墙|标高|分区|"
    r"楼上|楼下|箭头|施工|尺寸|高度|轴线|平面|系统|到|至|向)$")


def promote_with_faces(labels, faces, *, near_tol=650.0):
    """Promote *pending* text to room only with spatial and layer evidence.

    A CAD plan without an LLM still needs to recognize '男厕', '内走廊',
    '医技街'.  This does NOT treat all unlabeled text as room names: admission
    depends on (1) morphology or a dense room-name layer, and (2) geometry.
    Mutates no caller records; annotations/pending candidates remain audited.
    """
    from collections import Counter
    from . import rooms as RM
    items = list(labels or ())
    by_layer = Counter()
    anchors = Counter()
    for lab in items:
        name = str(lab.get('text') or lab.get('name') or '').strip()
        layer = lab.get('layer')
        if 2 <= len(name) <= NAME_MAX and not ROOM_BAD.search(name):
            by_layer[layer] += 1
            if ROOM_ANCHOR.search(name):
                anchors[layer] += 1
    supported = {layer for layer,n in by_layer.items()
                 if n >= 8 and anchors[layer] >= 5 and anchors[layer]/n >= 0.28}
    if not faces:
        return [dict(lab) for lab in items], {'promoted':0,'layers':sorted(str(x) for x in supported)}
    locate, _ = RM._face_locator(faces)
    admitted, out = 0, []
    for lab in items:
        obj = dict(lab)
        if obj.get('role') not in ('pending', 'uncertain', None):
            out.append(obj); continue
        name = str(obj.get('text') or obj.get('name') or '').strip()
        layer = obj.get('layer')
        if len(name) < 2 or len(name) > NAME_MAX or ROOM_BAD.search(name) or NUMERIC.fullmatch(name) or AXIS.fullmatch(name):
            out.append(obj); continue
        anchor = bool(ROOM_ANCHOR.search(name))
        if not anchor and layer not in supported:
            out.append(obj); continue
        if not any('\u4e00' <= c <= '\u9fff' for c in name):
            out.append(obj); continue
        h = float(obj.get('height') or obj.get('h') or 0.0)
        if h and not (HEIGHT_RANGE[0] <= h <= HEIGHT_RANGE[1]):
            out.append(obj); continue
        try:
            match, inside, dist = locate(float(obj['x']), float(obj['y']))
        except (KeyError, ValueError, TypeError):
            out.append(obj); continue
        if match is None or (not inside and (not anchor or dist > near_tol)):
            out.append(obj); continue
        obj['role'] = 'room'
        obj['name'] = name
        obj['role_source'] = 'geometry+lexical' if anchor else 'geometry+room_layer'
        obj['room_evidence'] = {'layer_support': layer in supported,
                                'inside_face': bool(inside),
                                'label_distance_mm': round(dist,1)}
        admitted += 1
        out.append(obj)
    return out, {'promoted':admitted,'layers':sorted(str(x) for x in supported)}
