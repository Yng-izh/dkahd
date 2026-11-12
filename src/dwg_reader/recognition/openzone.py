"""开放分区（open zone）：有房名、无闭合边界、附近没有断墙证据的空间。

判据（三选一，与"墙没认出来"严格区分）：
  1. 有房名 + 附近没有断墙/漏房间缺陷 + 缺口证据不成立 -> open zone（不补墙）
  2. 有房名 + 附近有断墙证据（共线墙端、100~6000mm 缺口）-> 走修复，不在这里建
  3. 没房名 + 没边界 -> 不建

open zone 一律 status="open"，不参与"墙围房间"的算量；面积按房名聚类的包围盒估算，
并显式标注 area_source="bbox_estimate"。
"""
from __future__ import annotations

import math

CLUSTER_RADIUS = 8000.0        # 同一功能分区内房名的最大相邻距离
DEFECT_EXCLUDE = 3000.0        # 离断墙缺陷这么近就不算开放空间，交给修复


def _bbox(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return [min(xs), min(ys), max(xs), max(ys)]


def _rect(bbox):
    x0, y0, x1, y1 = bbox
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def _dist_to_bbox(x, y, b) -> float:
    dx = max(b[0] - x, 0.0, x - b[2])
    dy = max(b[1] - y, 0.0, y - b[3])
    return (dx * dx + dy * dy) ** 0.5


def _clusters(labels):
    """单链聚类：同名字/相邻 8m 内的房名归成一个功能分区。"""
    items = [dict(lab) for lab in labels]
    used = [False] * len(items)
    out = []
    for i, lab in enumerate(items):
        if used[i]:
            continue
        group = [lab]
        used[i] = True
        changed = True
        while changed:
            changed = False
            for j, other in enumerate(items):
                if used[j]:
                    continue
                if other["text"] == lab["text"] or any(
                        math.hypot(other["x"] - g["x"], other["y"] - g["y"]) <= CLUSTER_RADIUS
                        for g in group):
                    group.append(other)
                    used[j] = True
                    changed = True
        out.append(group)
    return out


def build(*, labels, enclosed_labels=(), defects=()) -> list[dict]:
    """→ open zone 记录列表（与房间记录字段对齐，便于直接进 assemble）。"""
    blocked = []
    for d in defects or ():
        if d.get("type") in ("BROKEN_WALL", "ROOM_LEAK") and d.get("region"):
            blocked.append(d["region"])

    zones = []
    for group in _clusters(labels or ()):
        head = max(group, key=lambda g: (g.get("score") or 0, len(g.get("text") or "")))
        pts = [(g["x"], g["y"]) for g in group]
        pt = (head["x"], head["y"])
        if any(_dist_to_bbox(pt[0], pt[1], b) <= DEFECT_EXCLUDE for b in blocked):
            continue                      # 附近有断墙：交给修复，不当开放空间
        # 面域内已经算过房间的，不重复建
        if any(abs(head["x"] - e.get("x", 1e18)) < 1.0
               and abs(head["y"] - e.get("y", 1e18)) < 1.0 for e in enclosed_labels or ()):
            continue
        b = _bbox(pts)
        pad = 400.0                        # 每个房名至少给一点范围
        b = [b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad]
        w = max(b[2] - b[0], 800.0)
        h = max(b[3] - b[1], 800.0)
        zones.append({
            "id": f"ZONE-{len(zones) + 1:03d}",
            "kind": "open_zone", "status": "open",
            "room_name": head["text"],
            "label": dict(head, name=head["text"],
                           area=head.get("area") or (head.get("declared_area"))),
            "labels": [{"text": g["text"], "handle": g.get("handle"), "x": g["x"],
                        "y": g["y"]} for g in group],
            "bbox": [round(v, 1) for v in b],
            "polygon": _rect([round(v, 1) for v in b]),
            "area": round(w * h, 1), "area_m2": round(w * h / 1e6, 2),
            "area_source": "bbox_estimate",
            "boundary": "label_cluster",
            "note": "无闭合边界，按功能分区处理（不计入墙围房间算量）",
            "handles": [g.get("handle") for g in group if g.get("handle")],
        })
    return zones
