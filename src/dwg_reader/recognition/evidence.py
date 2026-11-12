"""统一证据索引：候选 / 原语 / 构件 / 面域进同一张表，带来源与相互关系。

id 约定（稳定、可审计）：

``ev:<handle>``    DXF 图元
``pat:<n>``        原语组合（线束 / 平行线组 / 轮廓组合）
``wall:<n>`` ``col:<n>`` ``op:<n>`` ``room:<n>`` ``label:<n>`` ``face:<n>``
"""
from __future__ import annotations

import collections

_KIND_OF_TYPE = {"LINE": "entity", "LWPOLYLINE": "entity", "POLYLINE": "entity",
                 "ARC": "arc", "CIRCLE": "arc", "HATCH": "hatch",
                 "TEXT": "label", "MTEXT": "label", "INSERT": "entity"}


def build(*, snapshot, prims=None, walls_seg=(), wall_model=None, spaces=None,
          doors=(), windows=(), columns=(), rooms=(), room_labels=()) -> dict:
    """构建证据索引（纯函数，输出 JSON-native）。

    ``prims`` 缺省时在内部从快照提取原语——线支撑必须进图，否则 LLM 的
    ``group_line_bundle`` / ``pair_parallel`` 类建议找不到合法 target。
    """
    if prims is None:
        try:
            from . import primitives as PRIM
            prims = PRIM.extract(snapshot)
        except Exception:
            prims = {}
    items: dict[str, dict] = {}
    rel: list[dict] = []

    for e in snapshot.get("entities", ()):
        rid = f"ev:{e.get('handle')}"
        items[rid] = {"kind": _KIND_OF_TYPE.get(e.get("type"), "entity"),
                      "type": e.get("type"), "layer": e.get("layer"),
                      "bbox": bbox_of(e), "via": e.get("via")}

    for i, p in enumerate((prims or {}).get("lines", ())[:20000], 1):
        pid = f"pat:{i}"
        items[pid] = {"kind": "line_support", "layer": p.get("layer"),
                      "handle": p.get("handle"),
                      "angle": p.get("angle"), "length": p.get("length"),
                      "dir": p.get("dir"), "nrm": p.get("nrm"),
                      "orient": p.get("orient") or (1 if str(p.get("orient")) == "1" else None),
                      "pos": p.get("pos"), "lo": p.get("lo"), "hi": p.get("hi"),
                      "bbox": bbox_of(p)}
        h = p.get("handle")
        if h:
            rel.append({"src": pid, "dst": f"ev:{h}", "type": "from_entity"})

    # 图层本体进图：语义修复（propose_layer_role）需要合法的 layer target
    per_layer: dict[str, dict] = {}
    for e in snapshot.get("entities", ()):
        lay = e.get("layer")
        if not lay:
            continue
        rec = per_layer.setdefault(lay, {"kind": "layer", "layer": lay, "count": 0,
                                         "types": {}, "bbox": None})
        rec["count"] += 1
        rec["types"][e.get("type")] = rec["types"].get(e.get("type"), 0) + 1
        b = bbox_of({"start": e.get("start"), "end": e.get("end"),
                     "center": e.get("center"), "insert": e.get("insert")})
        if b:
            cur = rec["bbox"]
            rec["bbox"] = (b if not cur else
                           [min(cur[0], b[0]), min(cur[1], b[1]),
                            max(cur[2], b[2]), max(cur[3], b[3])])
    for lay, rec in per_layer.items():
        items[f"layer:{lay}"] = rec

    # 闭合轮廓/填充边界进图：extract_contour 类动作需要它们当 target
    for i, c in enumerate((prims or {}).get("closed", ())[:4000], 1):
        cid = f"contour:{i}"
        items[cid] = {"kind": "contour", "layer": c.get("layer"),
                      "handle": c.get("handle"), "bbox": c.get("bbox"),
                      "outline": c.get("outline"), "n": c.get("n"),
                      "has_bulge": c.get("has_bulge"), "shape": c.get("shape")}
        h = c.get("handle")
        if h:
            rel.append({"src": cid, "dst": f"ev:{h}", "type": "from_entity"})
    for i, h in enumerate((prims or {}).get("hatches", ())[:4000], 1):
        hid = f"hatch:{i}"
        items[hid] = {"kind": "hatch", "layer": h.get("layer"), "handle": h.get("handle"),
                      "loops": h.get("loops"), "bbox": bbox_of({"points": (h.get("loops") or [[]])[0]})}
        if h.get("handle"):
            rel.append({"src": hid, "dst": f"ev:{h['handle']}", "type": "from_entity"})

    def add(prefix, seq, kind):
        for n, rec in enumerate(seq or (), 1):
            rid = f"{prefix}:{n}"
            items[rid] = {"kind": kind, "bbox": bbox_of(rec),
                          "status": rec.get("status"),
                          "layer": (rec.get("source_entities") or [{}])[0].get("layer")
                          if rec.get("source_entities") else rec.get("layer")}
            for h in handles_of(rec):
                rel.append({"src": rid, "dst": f"ev:{h}", "type": "supported_by"})

    add("wall", walls_seg, "wall_candidate")
    add("col", columns, "column_candidate")
    add("op", list(doors) + list(windows), "opening_candidate")
    add("room", rooms, "room")
    add("label", room_labels, "room_label")

    for n, f in enumerate((spaces or {}).get("faces", ()) or (), 1):
        items[f"face:{n}"] = {"kind": "space_face", "bbox": f.get("bbox"),
                              "area": f.get("area"), "space_id": f.get("id")}
    for n, span in enumerate((wall_model or {}).get("spans", ()) or (), 1):
        items[f"walldef:{n}"] = {"kind": "wall_candidate",
                                 "bbox": center_bbox(span), "pos": span.get("pos")}

    counts = collections.Counter(v["kind"] for v in items.values())
    return {"schema": "evidence-index/1.0", "items": items, "relations": rel,
            "counts": dict(sorted(counts.items())),
            "regions": region_index(items)}


def by_kinds(index: dict, kinds) -> list[str]:
    kinds = set(kinds)
    return [i for i, v in (index.get("items") or {}).items() if v.get("kind") in kinds]


def get(index: dict, item_id: str) -> dict | None:
    return (index.get("items") or {}).get(item_id)


def in_region(index: dict, bbox) -> list[str]:
    if not bbox:
        return list((index.get("items") or {}))
    x0, y0, x1, y1 = bbox
    pad = 300.0
    out = []
    for i, v in (index.get("items") or {}).items():
        b = v.get("bbox")
        if not b:
            continue
        if b[2] >= x0 - pad and b[0] <= x1 + pad and b[3] >= y0 - pad and b[1] <= y1 + pad:
            out.append(i)
    return out


def handles_of(rec) -> tuple:
    if rec.get("handle"):
        return (rec["handle"],)
    return tuple(s.get("handle") for s in (rec.get("srcs") or ()) if s.get("handle"))


def bbox_of(rec) -> list | None:
    b = rec.get("bbox")
    if isinstance(b, (list, tuple)) and len(b) == 4:
        return [round(float(v), 1) for v in b]
    pts = []
    # A LinearSupport is represented in local tangent/normal coordinates.
    # Without this inverse projection its bbox was None, so every repair case
    # silently received the *same drawing-wide patterns* regardless of region.
    if (rec.get("pos") is not None and rec.get("lo") is not None
            and rec.get("hi") is not None and rec.get("dir") is not None):
        try:
            ux, uy = (float(v) for v in rec["dir"][:2])
            nx, ny = (float(v) for v in (rec.get("nrm") or (-uy, ux))[:2])
            pos, lo, hi = (float(rec[k]) for k in ("pos", "lo", "hi"))
            pts.extend(((lo * ux + pos * nx, lo * uy + pos * ny),
                        (hi * ux + pos * nx, hi * uy + pos * ny)))
        except (TypeError, ValueError, KeyError):
            pass
    for key in ("start", "end", "center", "insert"):
        p = rec.get(key)
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            pts.append((float(p[0]), float(p[1])))
    for key in ("outline", "polygon", "points", "axis", "opening"):
        for p in rec.get(key) or ():
            if isinstance(p, (list, tuple)) and len(p) >= 2 and not isinstance(p[0], (list, tuple)):
                pts.append((float(p[0]), float(p[1])))
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return [round(min(xs), 1), round(min(ys), 1), round(max(xs), 1), round(max(ys), 1)]


def center_bbox(span) -> list | None:
    c = span.get("center")
    a, b = span.get("lo"), span.get("hi")
    if c is None or a is None or b is None:
        return None
    t = float(span.get("thickness") or 0.0) / 2.0
    d = span.get("dir") or (1.0, 0.0)
    n = span.get("nrm") or (0.0, 1.0)
    pts = []
    for u in (a, b):
        for s in (-t, t):
            pts.append((c * n[0] + u * d[0] + s * n[0],
                        c * n[1] + u * d[1] + s * n[1]))
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return [round(min(xs), 1), round(min(ys), 1), round(max(xs), 1), round(max(ys), 1)]


def region_index(items: dict, cell: float = 20000.0) -> list[dict]:
    """把证据按 20m 网格粗分块，供 quality_gate 给"受影响区域"。"""
    buckets: dict[tuple, list] = collections.defaultdict(list)
    for i, v in items.items():
        b = v.get("bbox")
        if not b:
            continue
        key = (int((b[0] + b[2]) / 2 // cell), int((b[1] + b[3]) / 2 // cell))
        buckets[key].append(i)
    out = []
    for (cx, cy), ids in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        out.append({"region": f"region_{cx}_{cy}",
                    "bbox": [cx * cell, cy * cell, (cx + 1) * cell, (cy + 1) * cell],
                    "evidence": len(ids)})
    return out
