"""Canonical CAD IR：把一张图纸归一成"可推理的图元 + 关系 + 多尺度区域"。

为什么要有这一层
----------------
旧的识别链路里，每个识别器各自去读 ``DrawingSnapshot`` 的裸字段（``start`` /
``points`` / ``loops`` …）。这带来三个后果：

1. 同一个图元在不同模块里可能被当成不同的东西（LINE 的两点 vs 多段线的边），
   候选之间没法比较，更没法联合求解；
2. 图元之间**没有关系**：一扇特殊窗可能是六条线组成的框架，单独看每条线都
   没有语义，必须"组合起来才成立"；旧结构里没有地方表达"这六条线是一伙的"；
3. 坐标口径散落各处（有的用模型空间，有的用块展开坐标），跨模块比较全靠约定。

所以 IR 做三件事，别的什么都不做：

* **统一 id**：每个图元一个稳定 ``uid``（``e:0001`` / ``t:0003``），
  ``handle -> uid`` 双向可查；候选、关系、证据、渲染都用这套 id；
* **统一几何**：按 kind 给出封闭的几何字段（segment/arc/poly/insert/hatch/text），
  额外算好 ``bbox / length / angle`` 这些各模块都要用的量；
* **多尺度区域**：全局 / 局部（按残余或实例聚出来的 ROI）/ 细节三档，
  三种观察层次共享同一套 id 与坐标系。

IR 不含任何"判据"：它不判断谁是墙、谁是窗，也不产生阈值。阈值仍然来自
:mod:`scale`，判定仍然在候选引擎与联合求解里。
"""
from __future__ import annotations

import hashlib
import math

SCHEMA = "cad-ir/1.0"

#: 图元 kind 闭集（识别与拟合只认这七种）
KINDS = ("segment", "arc", "poly", "insert", "hatch", "text", "unknown")

#: 多尺度区域：level 0=全局，1=局部 ROI，2=细节
REGION_LEVELS = {"global": 0, "local": 1, "detail": 2}

#: 逻辑预算（条/个）：单张图进入 IR 的图元上限、局部区域最多切几个
BUDGET = {"entities": 40000, "local_regions": 24, "detail_regions": 12,
          "cluster_thickness": 6.0, "min_local_entities": 4}


def _sha(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _r(v, nd: int = 1) -> float:
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return 0.0


def _xy(pt):
    if isinstance(pt, (list, tuple)) and len(pt) >= 2:
        return [float(pt[0]), float(pt[1])]
    return None


def _seg_len(a, b) -> float:
    if not a or not b:
        return 0.0
    return math.hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1]))


def _bbox_of_points(points):
    pts = [p for p in (points or ()) if isinstance(p, (list, tuple)) and len(p) >= 2]
    if not pts:
        return None
    xs = [float(p[0]) for p in pts]
    ys = [float(p[1]) for p in pts]
    return [_r(min(xs)), _r(min(ys)), _r(max(xs)), _r(max(ys))]


def _arc_points(center, radius, a0, a1, steps: int = 12):
    cx, cy = float(center[0]), float(center[1])
    r = float(radius)
    out = []
    for i in range(steps + 1):
        ang = math.radians(float(a0) + (float(a1) - float(a0)) * i / steps)
        out.append([cx + r * math.cos(ang), cy + r * math.sin(ang)])
    return out


# --------------------------------------------------------------------- 图元归一
def _entity_kind(rec: dict) -> str:
    kind = str(rec.get("type") or "").upper()
    if kind == "LINE":
        return "segment"
    if kind in ("LWPOLYLINE", "POLYLINE"):
        return "poly"
    if kind in ("ARC", "CIRCLE", "ELLIPSE"):
        return "arc"
    if kind == "INSERT":
        return "insert"
    if kind == "HATCH":
        return "hatch"
    if kind in ("TEXT", "MTEXT"):
        return "text"
    return "unknown"


def _normalize(rec: dict) -> dict | None:
    """快照图元 -> IR 图元（kind 封闭、几何字段统一、附带常用派生量）。"""
    kind = _entity_kind(rec)
    geom: dict = {}
    box = None
    length = 0.0
    angle = None
    if kind == "segment":
        a, b = _xy(rec.get("start")), _xy(rec.get("end"))
        if not a or not b:
            return None
        geom = {"a": a, "b": b}
        box = _bbox_of_points([a, b])
        length = _seg_len(a, b)
        angle = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180.0
    elif kind == "poly":
        pts = [_xy(p) for p in (rec.get("points") or ()) if _xy(p)]
        if len(pts) < 2:
            return None
        closed = bool(rec.get("closed"))
        geom = {"pts": pts, "closed": closed}
        box = _bbox_of_points(pts)
        length = sum(_seg_len(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
        if closed:
            length += _seg_len(pts[-1], pts[0])
        first, last = pts[0], pts[-1]
        if _seg_len(first, last) > 1e-9:
            angle = math.degrees(math.atan2(last[1] - first[1],
                                            last[0] - first[0])) % 180.0
    elif kind == "arc":
        center, radius = _xy(rec.get("center")), float(rec.get("radius") or 0.0)
        if not center or radius <= 0:
            return None
        a0 = float(rec.get("start_angle") or 0.0)
        a1 = float(rec.get("end_angle") or (360.0 if str(rec.get("type")).upper()
                                           == "CIRCLE" else 0.0))
        geom = {"center": center, "radius": _r(radius),
                "start_deg": _r(a0), "end_deg": _r(a1)}
        pts = _arc_points(center, radius, a0, a1)
        box = _bbox_of_points(pts)
        length = abs(math.radians(a1 - a0)) * radius
    elif kind == "insert":
        pt = _xy(rec.get("insert"))
        if not pt:
            return None
        geom = {"at": pt, "block": rec.get("name"),
                "rotation": _r(rec.get("rotation") or 0.0, 2),
                "scale": [_r(rec.get("xscale") or 1.0, 4),
                          _r(rec.get("yscale") or 1.0, 4)],
                "attribs": dict(rec.get("attribs") or {})}
        box = [pt[0], pt[1], pt[0], pt[1]]
    elif kind == "hatch":
        loops = [p for p in (rec.get("loops") or ()) if p]
        if not loops:
            return None
        geom = {"loops": loops, "solid": bool(rec.get("solid"))}
        box = _bbox_of_points([p for loop in loops for p in loop])
    elif kind == "text":
        pt = _xy(rec.get("insert"))
        text = str(rec.get("text") or "")
        if not pt:
            return None
        height = float(rec.get("height") or 0.0)
        geom = {"at": pt, "text": text, "height": _r(height)}
        est = max(1.0, 0.6 * height) * max(1, len(text))
        box = [pt[0], pt[1], pt[0] + est, pt[1] + (height or 1.0)]
    else:
        return None
    return {"kind": kind, "geom": geom, "bbox": box,
            "length": _r(length), "angle": (None if angle is None else _r(angle, 2))}


def _entity_record(rec: dict, *, uid: str, digits: int) -> dict | None:
    norm = _normalize(rec)
    if norm is None:
        return None
    via = rec.get("via") or {}
    return {
        "uid": uid,
        "handle": str(rec.get("handle") or ""),
        "kind": norm["kind"],
        "layer": str(rec.get("layer") or ""),
        "family": (str(rec.get("name")) if norm["kind"] == "insert" and rec.get("name")
                   else None),
        "geom": norm["geom"],
        "bbox": norm["bbox"],
        "length": norm["length"],
        "angle": norm["angle"],
        "derived": bool(rec.get("derived")),
        "via": ({"insert": via.get("insert"), "depth": via.get("depth")}
                if via else None),
        "source": {"handle": str(rec.get("handle") or ""),
                   "type": rec.get("type")},
        "order": int(uid.split(":")[1]),
    }


# --------------------------------------------------------------------- 族与区域
def _families(snapshot: dict, *, uid_of: dict, basis: dict) -> list[dict]:
    """块族：名字、实例数、几何指纹、图层、外接框（用于"同族一次推理"）。"""
    try:
        from .. import blockfamily as BLOCKFAM
        fams = BLOCKFAM.families(snapshot)
    except Exception:
        return []
    out: list[dict] = []
    for name, fam in (fams or {}).items():
        uids = [uid_of.get(str(e.get("handle"))) for e in (fam.get("instances") or ())
                if uid_of.get(str(e.get("handle")))]
        boxes = [uid for uid in uids]
        try:
            fingerprint = BLOCKFAM.fingerprint(fam)
        except Exception:
            fingerprint = ""
        sizes = fam.get("sizes") or ()
        med = list(sizes[len(sizes) // 2]) if sizes else None
        out.append({"hash": fingerprint or ("name:" + _sha(str(name))[:10]),
                    "name": str(name), "n": int(fam.get("n") or len(uids)),
                    "layer": (list(fam.get("layers") or ()) or [""])[0],
                    "layers": list(fam.get("layers") or ()),
                    "size": med, "uids": uids, "arcs": int(fam.get("arcs") or 0),
                    "solids": int(fam.get("solids") or 0),
                    "loops": int(fam.get("loops") or 0),
                    "attdefs": int(fam.get("attdefs") or 0),
                    "boxes": boxes,
                    "repetition": len(uids)})
    out.sort(key=lambda f: (-f["n"], f["name"]))
    return out


def _union(boxes):
    rows = [b for b in (boxes or ()) if b]
    if not rows:
        return None
    return [_r(min(b[0] for b in rows)), _r(min(b[1] for b in rows)),
            _r(max(b[2] for b in rows)), _r(max(b[3] for b in rows))]


def _expand(box, pad: float):
    return [_r(box[0] - pad), _r(box[1] - pad), _r(box[2] + pad), _r(box[3] + pad)]


def _gap(a, b) -> float:
    if not a or not b:
        return float("inf")
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def _regions(entities, *, basis: dict, interesting=()) -> list[dict]:
    """多尺度区域：全局 1 个 + 局部（残余/实例聚出来的）+ 细节（每个局部的前几个）。"""
    boxes = [e["bbox"] for e in entities if e.get("bbox")]
    global_box = _union(boxes) or [0.0, 0.0, 0.0, 0.0]
    regions = [{"id": "r:global", "level": REGION_LEVELS["global"], "kind": "global",
                "bbox": global_box, "uids": [e["uid"] for e in entities][:BUDGET["entities"]]}]

    thick = float(basis.get("thickness") or 200.0)
    radius = float(BUDGET["cluster_thickness"]) * thick

    # 局部区域：以"未被解释/重复实例"为种子做连通聚类（只聚类，不判定）
    seeds = [uid for uid in (interesting or ())]
    by_uid = {e["uid"]: e for e in entities}
    seed_boxes = [by_uid[u]["bbox"] for u in seeds if u in by_uid and by_uid[u].get("bbox")]
    clusters: list[list] = []
    used: set = set()
    for i, box in enumerate(seed_boxes):
        if i in used:
            continue
        group = [i]
        used.add(i)
        stack = [i]
        while stack:
            cur = stack.pop()
            for j, other in enumerate(seed_boxes):
                if j in used:
                    continue
                if _gap(seed_boxes[cur], other) <= radius:
                    used.add(j)
                    group.append(j)
                    stack.append(j)
        if len(group) >= int(BUDGET["min_local_entities"]):
            clusters.append(group)
    clusters = clusters[:int(BUDGET["local_regions"])]

    detail = 0
    for k, group in enumerate(clusters, 1):
        box = _union([seed_boxes[i] for i in group])
        if not box:
            continue
        local = _expand(box, radius)
        inside = [e["uid"] for e in entities
                  if e.get("bbox") and _gap(e["bbox"], local) <= 0.0]
        regions.append({"id": f"r:loc:{k:03d}", "level": REGION_LEVELS["local"],
                        "kind": "local", "bbox": local,
                        "seed_uids": [seeds[i] if i < len(seeds) else "" for i in group],
                        "uids": inside[:int(BUDGET["entities"])]})
        if detail < int(BUDGET["detail_regions"]):
            detail += 1
            regions.append({"id": f"r:detail:{k:03d}", "level": REGION_LEVELS["detail"],
                            "kind": "detail", "bbox": _expand(box, radius * 0.5),
                            "parent": f"r:loc:{k:03d}",
                            "uids": inside[:int(BUDGET["entities"])]})
    return regions


# --------------------------------------------------------------------- 构建入口
def build(snapshot: dict, *, scale=None, plan=None, interesting=()) -> dict:
    """快照 -> CAD IR。纯函数、JSON-native、不改快照。

    ``interesting`` 是"值得多看两眼"的 handle 列表（残余图元、未解释块实例…）：
    它们只用来**切局部区域**，不代表任何判定结论。
    """
    from .. import scale as SC
    basis = {"thickness": float((scale or {}).get("thickness") or SC.DEFAULT_THICKNESS),
             "door_w": float((scale or {}).get("door_w") or SC.DEFAULT_DOOR_W),
             "noise": float((scale or {}).get("noise") or SC.DEFAULT_NOISE),
             "text_h": float((scale or {}).get("text_h") or SC.DEFAULT_TEXT_H)}

    raw = list(snapshot.get("entities") or ()) + list(snapshot.get("derived") or ())
    digits = 5
    entities: list[dict] = []
    handle_to_uid: dict[str, list[str]] = {}
    for n, rec in enumerate(raw[:int(BUDGET["entities"])], 1):
        uid = f"e:{n:0{digits}d}"
        item = _entity_record(rec, uid=uid, digits=digits)
        if item is None:
            continue
        entities.append(item)
        handle = item["handle"]
        if handle:
            handle_to_uid.setdefault(handle, []).append(uid)

    texts = [{"uid": e["uid"], "handle": e["handle"], "layer": e["layer"],
              "text": e["geom"]["text"], "bbox": e["bbox"],
              "height": e["geom"]["height"]}
             for e in entities if e["kind"] == "text"]

    fams = _families(snapshot, uid_of={h: v[0] for h, v in handle_to_uid.items()},
                     basis=basis)

    seed_uids = []
    for handle in interesting or ():
        seed_uids.extend(handle_to_uid.get(str(handle), ()))
    # 重复族（同族多实例）也当"值得聚类"的种子：它们是最常见的残余来源
    for fam in fams:
        if fam["n"] >= 3:
            seed_uids.extend(fam["uids"][:6])
    regions = _regions(entities, basis=basis, interesting=seed_uids)

    boxes = [e["bbox"] for e in entities if e.get("bbox")]
    return {
        "schema": SCHEMA,
        "units": "mm",
        "bbox": _union(boxes) or [0.0, 0.0, 0.0, 0.0],
        "basis": basis,
        "entities": entities,
        "texts": texts,
        "families": fams,
        "regions": regions,
        "index": {"handle_to_uid": handle_to_uid},
        "stats": {"entities": len(entities),
                  "by_kind": {k: sum(1 for e in entities if e["kind"] == k)
                              for k in KINDS},
                  "layers": len({e["layer"] for e in entities}),
                  "families": len([f for f in fams if f["n"] >= 2]),
                  "regions": len(regions),
                  "truncated": len(raw) > int(BUDGET["entities"])},
    }


# --------------------------------------------------------------------- 查询工具
def by_uid(ir: dict) -> dict:
    return {e["uid"]: e for e in (ir.get("entities") or ())}


def uids_for_handle(ir: dict, handle: str) -> list[str]:
    return list(((ir.get("index") or {}).get("handle_to_uid") or {}).get(str(handle), ()))


def in_region(ir: dict, region) -> list[dict]:
    """区域内的图元（按 bbox 相交）。"""
    if not region:
        return list(ir.get("entities") or ())
    box = region.get("bbox") if isinstance(region, dict) else region
    out = []
    for e in ir.get("entities") or ():
        if e.get("bbox") and _gap(e["bbox"], box) <= 0.0:
            out.append(e)
    return out


def region(ir: dict, region_id: str) -> dict | None:
    for row in ir.get("regions") or ():
        if row.get("id") == region_id:
            return row
    return None


def digest(ir: dict) -> str:
    """IR 指纹（缓存/实验键用；不含坐标列表，只含 id 与几何指纹）。"""
    rows = [(e["uid"], e["kind"], e["handle"], e["layer"],
             None if e["bbox"] is None else tuple(_r(v) for v in e["bbox"]))
            for e in (ir.get("entities") or ())]
    return _sha(repr(rows))[:16]


def summary(ir: dict) -> dict:
    return {"schema": SCHEMA, "stats": ir.get("stats") or {},
            "basis": ir.get("basis") or {},
            "bbox": ir.get("bbox"),
            "regions": [{"id": r["id"], "level": r["level"], "kind": r["kind"]}
                        for r in (ir.get("regions") or ())][:12],
            "top_families": [{"name": f["name"], "n": f["n"], "hash": f["hash"]}
                             for f in (ir.get("families") or ())[:8]]}
