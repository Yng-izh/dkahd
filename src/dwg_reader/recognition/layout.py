"""图纸版式：图框候选、内容图框选择与范围裁剪。

15 个图签只画一张图的图纸，先把识别范围锁到"有图面内容"的那个图框。
"""
from __future__ import annotations

from .base import bbox_of_points

MIN_FRAME = 5000.0        # 图框最小边长 mm
MIN_CONTENT_RATIO = 0.5   # 框内实体占比达到该值才裁剪


def _closed_rects(snapshot, frame_layers):
    """图层上的闭合多段线矩形（4~5 点、轴对齐、边长 >= MIN_FRAME）。"""
    out = []
    for e in snapshot["entities"]:
        if e.get("type") != "LWPOLYLINE" or not e.get("closed"):
            continue
        if frame_layers and e.get("layer") not in frame_layers:
            continue
        pts = [[float(p[0]), float(p[1])] for p in e.get("points", ())]
        if len(pts) < 4:
            continue
        b = bbox_of_points(pts)
        w, h = b[2] - b[0], b[3] - b[1]
        if w < MIN_FRAME or h < MIN_FRAME:
            continue
        out.append({"bbox": b, "w": w, "h": h, "handle": e["handle"], "layer": e["layer"]})
    return out


def _point_of(e):
    if e.get("type") in ("TEXT", "MTEXT", "INSERT"):
        return e.get("insert")
    if e.get("type") == "LINE":
        return e.get("start")
    if e.get("type") in ("CIRCLE", "ARC", "ELLIPSE"):
        return e.get("center")
    pts = e.get("points")
    if pts:
        return pts[0]
    return None


def _struct_bbox(snapshot, struct_layers):
    pts = []
    for e in snapshot["entities"]:
        if struct_layers and e.get("layer") not in struct_layers:
            continue
        if e.get("type") == "LINE":
            pts.extend([e["start"], e["end"]])
        elif e.get("type") == "LWPOLYLINE":
            pts.extend(e.get("points", ()))
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return [min(xs), min(ys), max(xs), max(ys)]


def detect_scope(snapshot, frame_layers=(), crop: bool = True, struct_layers=()) -> dict:
    """返回 {"frames", "content_frame", "crop", "method"}。

    优先选内容占比 >=0.2 的图框；否则用结构构件（墙/柱图层）的包围盒兜底，
    但仅当它不到全图面积一半时（多图签拼版的典型情况）。
    """
    frames = _closed_rects(snapshot, set(frame_layers) if frame_layers else ())
    if not frames:
        return {"frames": [], "content_frame": None, "crop": None}

    entities = [e for e in snapshot["entities"]
                if not (frame_layers and e.get("layer") in set(frame_layers))]
    total = len(entities) or 1
    for f in frames:
        b = f["bbox"]
        n = 0
        for e in entities:
            p = _point_of(e)
            if p and b[0] <= p[0] <= b[2] and b[1] <= p[1] <= b[3]:
                n += 1
        f["content"] = n
        f["ratio"] = n / total

    best = max(frames, key=lambda f: f["ratio"])
    crop_box, method = None, "none"
    if crop and best["ratio"] >= 0.2:
        crop_box, method = best["bbox"], "frame"
    elif crop and struct_layers:
        sb = _struct_bbox(snapshot, set(struct_layers))
        if sb:
            allpts = []
            for e in snapshot["entities"]:
                for p in (e.get("start"), e.get("end"), e.get("insert"), e.get("center")):
                    if p:
                        allpts.append(p)
                allpts.extend(e.get("points") or ())
            if allpts:
                xs = [p[0] for p in allpts]
                ys = [p[1] for p in allpts]
                ta = max(1.0, (max(xs) - min(xs)) * (max(ys) - min(ys)))
            else:
                ta = 1.0
            sa = max(1.0, (sb[2] - sb[0]) * (sb[3] - sb[1]))
            if sa < 0.5 * ta:
                pad = 0.05 * max(sb[2] - sb[0], sb[3] - sb[1])
                crop_box = [sb[0] - pad, sb[1] - pad, sb[2] + pad, sb[3] + pad]
                method = "structure"
    return {"frames": frames, "content_frame": best["bbox"], "crop": crop_box, "method": method}


def crop_snapshot(snapshot, box):
    """把快照实体裁到 box 内（LINE 两端、多段线顶点、点类取插入点）。"""
    if not box:
        return snapshot
    x0, y0, x1, y1 = box

    def inside(p):
        return p is not None and x0 <= p[0] <= x1 and y0 <= p[1] <= y1

    def keep(records):
        kept = []
        for e in records:
            kind = e.get("type")
            if kind == "LINE":
                ok = inside(e.get("start")) and inside(e.get("end"))
            elif kind in ("LWPOLYLINE", "POLYLINE"):
                ok = any(inside(p) for p in e.get("points", ()))
            else:
                p = _point_of(e)
                ok = True if p is None else inside(p)
            if ok:
                kept.append(e)
        return kept

    out = dict(snapshot)
    out["entities"] = keep(snapshot["entities"])
    # 块展开几何同样裁到同一范围，否则裁剪后会出现"墙在框外"的幽灵构件
    out["derived"] = keep(snapshot.get("derived", ()))
    out["scope"] = box
    return out
