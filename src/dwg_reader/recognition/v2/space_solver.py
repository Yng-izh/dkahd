"""空间与房间求解：房间**必须**由选中的墙网重算，不许凭空造。

为什么单独一层
--------------
全局联合求解给出的是"墙/柱/门窗"的选择结果。房间不是候选竞赛的产物，它是
**墙网闭合出来的面域 + 挂在上面的房名**。所以顺序必须是：

```text
选中的墙/柱  ──> 面域（栅格连通 + 封缝）──> 房名挂靠 ──> 房间候选
```

这条链如果写反，最典型的错法是"用闭合房间反推墙"——那会凭空造出真实不存在的墙。
这里坚持只往一个方向走：墙在先是墙，房间在后是结果。

三个已知的坑（都在 v1 里踩过，这里照抄结论）：

1. 弧墙按弦画会把弧内侧空间切掉 → 直接吃 v1 :func:`spaces.build` 的弧墙处理；
2. 多房名挂同一面域必然丢一个 → 用 v1 :func:`rooms.rooms_from_faces` 的
   "专名优先 + 别名"策略；
3. 房名压墙线 → 允许小偏移挂靠，挂不上的进 ``orphans``，不丢弃。
"""
from __future__ import annotations

import math

SCHEMA = "space-solve/1.0"

#: 逻辑预算：单块面域上限、分块边长（mm 口径的"计算预算"，不是识别阈值）
#: 为什么必须分块：整张大图一次栅格化会把内存打爆（实测住院楼 22k 图元时
#: 进程被 OOM 杀掉）。分块边长取"约 20 m"，单块栅格降到百万级单元。
BUDGET = {"max_faces": 400, "tile_span": 20000.0, "dedupe_iou": 0.55,
          "max_cells": 4000000}


def _r(v, nd: int = 1) -> float:
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return 0.0


def _axis_of(hyp: dict):
    axis = (hyp.get("geometry") or {}).get("axis")
    if isinstance(axis, (list, tuple)) and len(axis) == 2:
        try:
            a = [float(axis[0][0]), float(axis[0][1])]
            b = [float(axis[1][0]), float(axis[1][1])]
            return a, b
        except (TypeError, IndexError, ValueError):
            return None, None
    return None, None


def walls_from(hypotheses) -> list[dict]:
    """候选或 v1 墙记录 -> v1 墙网能吃的墙记录（start/end/thickness/id）。

    两种输入都收：``hypothesis/1.0``（有 category/geometry.axis）与 v1
    ``walls_seg`` 的原始墙记录（有 start/end）。面域闭合要的是**墙段**，
    所以这里不挑输入来源，只挑"有没有可用的中心线"。
    """
    out: list[dict] = []
    for hyp in hypotheses or ():
        if not isinstance(hyp, dict):
            continue
        if hyp.get("category") not in (None, "wall"):
            continue
        a, b = _axis_of(hyp)
        if not a or not b:
            start, end = hyp.get("start"), hyp.get("end")
            try:
                a = [float(start[0]), float(start[1])]
                b = [float(end[0]), float(end[1])]
            except (TypeError, IndexError, ValueError):
                continue
        thickness = float((hyp.get("geometry") or {}).get("thickness_mm")
                          or hyp.get("thickness") or 0.0)
        box = hyp.get("bbox")
        if not box:                       # v1 的墙段记录没有 bbox：就地补一个
            box = [_r(min(a[0], b[0])), _r(min(a[1], b[1])),
                   _r(max(a[0], b[0])), _r(max(a[1], b[1]))]
        out.append({"id": str(hyp.get("id") or hyp.get("handle") or ""),
                    "start": a, "end": b, "thickness": thickness or None,
                    "bbox": box,
                    "length": _r(math.hypot(b[0] - a[0], b[1] - a[1])),
                    "evidence_state": "strong",
                    "source_hyp": hyp.get("id")})
    return out


def columns_from(hypotheses) -> list[dict]:
    out: list[dict] = []
    for hyp in hypotheses or ():
        if not isinstance(hyp, dict) or hyp.get("category") != "column":
            continue
        geom = hyp.get("geometry") or {}
        box = hyp.get("bbox")
        if not box:
            continue
        out.append({"id": str(hyp.get("id")), "bbox": box,
                    "w": geom.get("w_mm") or (box[2] - box[0]),
                    "h": geom.get("h_mm") or (box[3] - box[1]),
                    "outline": geom.get("outline")})
    return out


def _normalize_labels(rows):
    """统一房名记录：v1 的 ``label_filter`` 给 ``text``，``rooms`` 读 ``name``。

    两个口径来自不同历史阶段，这里在边界处对齐一次，不去改任何一边的字段。
    """
    out = []
    for lab in rows or ():
        if not isinstance(lab, dict):
            continue
        row = dict(lab)
        row.setdefault("name", row.get("text") or row.get("name") or "")
        if not row.get("name"):
            continue
        if row.get("x") is None or row.get("y") is None:
            ins = row.get("insert") or row.get("point") or ()
            if len(ins) >= 2:
                row["x"], row["y"] = float(ins[0]), float(ins[1])
        if row.get("x") is None:
            continue
        out.append(row)
    return out


def labels_from(texts, *, layers=()) -> dict:
    """IR 文字 -> 房名候选（复用 v1 的确定性预筛，并补齐 rooms 需要的字段）。

    ``labels.collect`` 给的是 ``text``，而 :func:`rooms.rooms_from_faces` 读的是
    ``name``：这里显式补 ``name``（同一个值），不去改 v1 的字段口径。
    """
    from .. import labels as LAB
    rows = []
    for t in (texts or ()):
        box = t.get("bbox") or ()
        if not t.get("text") or len(box) < 2:
            continue
        rows.append({"text": t.get("text"), "x": float(box[0]), "y": float(box[1]),
                     "height": t.get("height"), "handle": t.get("handle"),
                     "layer": t.get("layer")})
    got = LAB.collect(rows, layers=list(layers or ()))
    out = []
    for lab in got.get("labels") or ():
        row = dict(lab)
        row.setdefault("name", row.get("text"))
        out.append(row)
    return {"labels": out, "excluded": got.get("excluded") or (),
            "stats": got.get("stats") or {}}


def _box_gap(a, b) -> float:
    if not a or not b:
        return float("inf")
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return (dx * dx + dy * dy) ** 0.5


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


def _tiles(bbox, *, span: float) -> list:
    if not bbox:
        return []
    x0, y0, x1, y1 = [float(v) for v in bbox]
    span = max(1.0, float(span))
    nx = max(1, int(math.ceil((x1 - x0) / span)))
    ny = max(1, int(math.ceil((y1 - y0) / span)))
    dx, dy = (x1 - x0) / nx, (y1 - y0) / ny
    out = []
    for i in range(nx):
        for j in range(ny):
            out.append([x0 + i * dx, y0 + j * dy, x0 + (i + 1) * dx, y0 + (j + 1) * dy])
    return out


def _res_for(bbox, *, base: float) -> float:
    """栅格分辨率：先用 v1 给的分辨率，太细就按"单元总数上限"放粗。

    这是**计算预算**，不是识别阈值：一张 60 m × 40 m 的图按 8 mm 铺栅格是
    1500 万格，直接 OOM；放到 30 mm 仍然远小于墙厚（200 mm），墙带照样解析得出来。
    """
    if not bbox:
        return float(base)
    try:
        x0, y0, x1, y1 = [float(v) for v in bbox]
    except (TypeError, ValueError):
        return float(base)
    area = max(1.0, (x1 - x0) * (y1 - y0))
    cap = float(BUDGET["max_cells"])
    need = (area / cap) ** 0.5
    return float(max(float(base or 1.0), need))


def build_faces(walls, *, columns=(), bbox=None, thick=None, res=None) -> dict:
    """墙网 -> 面域。**分块**调用 v1 :func:`spaces.build`，再按重合度去重。

    不分块会把整张大图的栅格一次铺开（实测 22k 图元的图直接 OOM）。分块后
    每块只带"挨着它的墙"（带一点外扩，避免边界处的墙被判成开放），
    最后按面域包围盒 IoU 去重、保留面积大的那个。
    """
    from .. import spaces as SP
    if not walls:
        return {"faces": [], "stats": {"faces": 0, "reason": "no_walls"}}
    boxes = [w.get("bbox") for w in walls if w.get("bbox")]
    if not bbox and boxes:
        bbox = [min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes)]
    base_res = float(res or getattr(SP, "RES", 0.0) or 1.0)
    # 分块是主路径：块内保留细栅格（细栅格才封得住薄缝），块之间靠外扩对齐。
    # 整图一次算在 60×40 m 的图上会退化（粗栅格糊住薄缝 → 一处泄漏 → 全连通）。
    span = float(BUDGET["tile_span"])
    adaptive = _res_for(bbox, base=base_res)
    tiles = _tiles(bbox, span=span) or [bbox]
    pad = max(1.0, float(thick or 200.0) * 50.0)
    faces: list[dict] = []
    used_tiles = 0
    for tile in tiles:
        window = [tile[0] - pad, tile[1] - pad, tile[2] + pad, tile[3] + pad]
        subset = [w for w in walls if _box_gap(w.get("bbox"), window) <= 0.0]
        if len(subset) < 2:
            continue
        used_tiles += 1
        # 分辨率按**这块窗口的真实尺寸**定：窗口含外扩（pad），按 tile 定会偏细，
        # 单位格总数一超上限就会 OOM/被跳过（实测 595 面线时只剩 20 个面域）。
        res_tile = _res_for(window, base=base_res)
        try:
            got = SP.build(subset, columns=columns, bbox=window, thick=thick,
                           res=res_tile, max_faces=int(BUDGET["max_faces"]))
        except Exception:                             # 单块失败不影响其余块
            continue
        for face in got.get("faces") or ():
            faces.append(face)
    # 去重：同一面域会被相邻两块各算一次
    kept: list[dict] = []
    for face in sorted(faces, key=lambda f: -(float(f.get("area") or 0.0))):
        box = face.get("bbox")
        if any(_iou(box, other.get("bbox")) >= float(BUDGET["dedupe_iou"])
               for other in kept):
            continue
        kept.append(face)
        if len(kept) >= int(BUDGET["max_faces"]):
            break
    return {"faces": kept, "stats": {"faces": len(kept), "tiles": len(tiles),
                                     "tiles_used": used_tiles,
                                     "raw_faces": len(faces), "res": adaptive,
                                     "mode": "tiles"}}


def rooms_from_faces(faces, labels) -> dict:
    """面域 + 房名 -> 房间（专名优先、别名保留、挂不上的进 orphans）。"""
    from .. import rooms as RM
    if not faces:
        return {"rooms": [], "orphans": [], "stats": {"rooms": 0, "reason": "no_faces"}}
    return RM.rooms_from_faces(faces, labels or ())


def to_hypotheses(rooms) -> list[dict]:
    """房间 -> 统一候选结构（category=room），供下游统一处理。"""
    from . import proposal_engines as ENGINES
    out: list[dict] = []
    seen: set = set()
    for room in rooms or ():
        if not isinstance(room, dict):
            continue
        sig = (json_dumps(room.get("bbox")), str(room.get("name") or ""))
        if sig in seen:
            continue
        seen.add(sig)
        poly = room.get("polygon") or room.get("outline") or ()
        box = room.get("bbox")
        if not box and poly:
            xs = [float(p[0]) for p in poly]
            ys = [float(p[1]) for p in poly]
            box = [_r(min(xs)), _r(min(ys)), _r(max(xs)), _r(max(ys))]
        out.append(ENGINES.hypothesis(
            category="room", subtype=room.get("seal_state") or "closed_face",
            source="rule", engine="space_solver.rooms", region=None, bbox=box,
            geometry={"outline": poly,
                      "area_m2": _r((room.get("area") or 0) / 1.0e6, 2),
                      "name": room.get("name")},
            uids=(), handles=[str(h) for h in (room.get("handles") or ())],
            score=0.8 if room.get("seal_state") != "hypothesis" else 0.5,
            evidence={"rule": "rooms_from_faces",
                      "labels": room.get("labels") or room.get("aliases") or [],
                      "seal_state": room.get("seal_state")}))
    return out


def solve(*, selected, texts=(), labels=None, bbox=None, thick=None,
          label_layers=()) -> dict:
    """选中构件 -> 面域 -> 房名 -> 房间候选（一条单向链）。

    ``labels`` 可以直接给（例如 v1 ``label_filter`` 已经筛过的那份），
    这样 v2 与 v1 用的是同一套房名口径，不会因为"另起一套文字筛选"而少房间。
    """
    walls = walls_from(selected)
    columns = columns_from(selected)
    faces = build_faces(walls, columns=columns, bbox=bbox, thick=thick)
    if labels is None:
        labels = labels_from(texts, layers=label_layers)
    label_rows = (labels or {}).get("labels") if isinstance(labels, dict) else labels
    label_rows = _normalize_labels(label_rows)
    rooms = rooms_from_faces(faces.get("faces") or (), label_rows)
    hypotheses = to_hypotheses(rooms.get("rooms") or ())
    return {"schema": SCHEMA,
            "status": "ok" if hypotheses else ("no_room" if faces.get("faces")
                                               else "no_walls"),
            "walls": len(walls), "faces": len(faces.get("faces") or ()),
            "hypotheses": hypotheses,
            "stats": {"walls": len(walls), "faces": len(faces.get("faces") or ()),
                      "rooms": len(hypotheses),
                      "orphans": len(rooms.get("orphans") or ()),
                      "labels": len(labels.get("labels") or ()),
                      "face_stats": faces.get("stats") or {},
                      "room_stats": rooms.get("stats") or {}}}


def json_dumps(obj) -> str:
    import json as _json
    return _json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


def summary(result) -> dict:
    result = result or {}
    return {"schema": SCHEMA, "status": result.get("status"),
            "stats": {k: v for k, v in (result.get("stats") or {}).items()
                      if k not in ("face_stats", "room_stats")},
            "rooms": len(result.get("hypotheses") or ())}
