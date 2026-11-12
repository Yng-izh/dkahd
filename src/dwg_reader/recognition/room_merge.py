"""同名房间去重：处理"同一空间被切成几块"与"同名标注重复出现"。

规则：
  * 同名 + 几何相邻（bbox 间距 <= 500mm） -> 合并，面积相加，记 part_count
  * 同名 + 相距较远                        -> 各自保留并标 same_name_group（供人工核）
  * 交通空间（走道/门厅/中庭/楼梯间…）     -> 标 kind="circulation"，不参与"确认房间"判定
"""
from __future__ import annotations

ADJACENT = 500.0
CIRCULATION_WORDS = ("走道", "走廊", "过道", "门厅", "中庭", "共享", "楼梯间", "前室",
                     "电梯厅", "连廊", "开敞", "通道")


def _gap(a, b) -> float:
    dx = max(b[0] - a[2], 0.0, a[0] - b[2])
    dy = max(b[1] - a[3], 0.0, a[1] - b[3])
    return (dx * dx + dy * dy) ** 0.5


def _bbox_of(polygon):
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    return [min(xs), min(ys), max(xs), max(ys)]


def dedupe(records, *, adjacent: float = ADJACENT):
    groups: dict[str, list] = {}
    for r in records or ():
        label = r.get("label") or {}
        name = label.get("name") or ""
        groups.setdefault(name, []).append(r)

    out, stats = [], {"merged": 0, "same_name_groups": 0, "circulation": 0,
                      "kept": 0}
    for name, items in groups.items():
        if not name or len(items) == 1:
            out.extend(items)
            stats["kept"] += len(items)
            continue
        items.sort(key=lambda r: -(r.get("area") or 0))
        kept: list[dict] = []
        for r in items:
            bbox = r.get("bbox") or (_bbox_of(r["polygon"]) if r.get("polygon") else None)
            if not bbox:
                kept.append(r)
                continue
            r["bbox"] = bbox
            target = next((k for k in kept if _gap(k["bbox"], bbox) <= adjacent), None)
            if target is None:
                kept.append(r)
                continue
            target["area"] = (target.get("area") or 0) + (r.get("area") or 0)
            tb = target["bbox"]
            target["bbox"] = [min(tb[0], bbox[0]), min(tb[1], bbox[1]),
                              max(tb[2], bbox[2]), max(tb[3], bbox[3])]
            target["part_count"] = target.get("part_count", 1) + 1
            target["source_face_ids"] = list(dict.fromkeys(
                (target.get("source_face_ids") or []) + (r.get("source_face_ids") or [])))
            if (target.get("boundary_state") != "observed"
                    or r.get("boundary_state") != "observed"):
                target["boundary_state"] = "hypothesis"
                target["needs_review"] = True
            h = (r.get("label") or {}).get("handle")
            if h:
                target.setdefault("merged_handles", []).append(h)
            stats["merged"] += 1
        if len(kept) > 1:
            stats["same_name_groups"] += 1
            for k in kept:
                k["same_name_group"] = name
        out.extend(kept)
        stats["kept"] += len(kept)

    for r in out:                       # 交通空间：保留，但不当作"房间"
        name = (r.get("label") or {}).get("name") or ""
        if any(w in name for w in CIRCULATION_WORDS):
            r["kind"] = "circulation"
            stats["circulation"] += 1
    return out, stats
