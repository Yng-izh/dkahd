"""图纸区域分割：把模型空间切成"建筑平面 / 详图 / 图签"等候选区域。

与旧版的区别：旧版只保留结构图元最密集的**一个**区域并据此裁剪，其他平面、
低密度建筑区、长跨图元会被直接丢掉。现在：

1. 每个连通区域都作为候选返回，并给出 ``kind``（plan / detail / unknown）、
   所属图框、结构图元数量；
2. ``plan`` 仍是主区域（裁剪范围），但``multi_plan=True`` 时**不裁剪**——
   说明这张图上有多个同等量级的建筑平面，静默裁掉一个就是漏识别；
3. 判断区域依据图元真实包围范围（含线段端点），不再只看插入点。

区域上下文（本轮新增）：

* ``partition(..., tag=True)`` 给每条几何记录打 ``region`` 标记，下游（墙配对、
  空间、评估）因此可以**按建筑平面独立推理**，不同平面的墙线不会再参与同一次
  配对；
* 每个区域单独做单位合理性判定（``unit``）：跨设计院/跨软件的图纸里，同一份
  DWG 的不同视口可能用不同单位画（mm / cm / m / inch），全局一个换算系数会把
  小平面放大 1000 倍或缩到看不见；
* 每个区域记录自己的图层使用情况（``layers``），供诊断与评估按区域核对
  "这片区域到底用了哪些结构层"，而不是拿整张图的图层统计一刀切。
"""
from __future__ import annotations

import math

from .base import bbox_of_points

#: 格网边长（mm）：用于把图纸上的图元聚成"物理上连成一片"的区域
CELL = 20_000.0
#: 区域判为"建筑平面"所需的最少结构图元数
MIN_PLAN_STRUCT = 3
#: 次区域结构图元数达到主区域该比例，视为并列平面（多平面 / 多栋）
MULTI_PLAN_RATIO = 0.30
#: 区域面积小于主区域该比例，视为详图/局部大样
DETAIL_RATIO = 0.12

#: 一个"建筑平面"合理的最大跨度（mm）：400m。超过它说明该区域单位不是 mm
MAX_PLAN_SPAN = 400_000.0
#: 小于该跨度的区域不判定为单位异常（详图 / 局部大样本来就小）
MIN_PLAN_SPAN = 3_000.0
#: 单位候选系数（x 原始值 = mm）。只用于把"明显不可能"的尺寸拉回平面量级
UNIT_SCALES = (1000.0, 0.001, 100.0, 0.01, 10.0, 0.1, 25.4, 0.0254, 304.8)
#: 线段长度中位数的合理区间（mm）：50mm 的短墙线 ~ 30m 的长墙线
MIN_LINE_MM = 50.0
MAX_LINE_MM = 30_000.0

#: 区域里参与"图层使用"统计的图层数上限
LAYER_TOP = 8


def _closed_rects(snapshot, frame_layers):
    out = []
    for e in snapshot["entities"]:
        if e.get("type") != "LWPOLYLINE" or not e.get("closed"):
            continue
        if frame_layers and e.get("layer") not in frame_layers:
            continue
        pts = [[float(p[0]), float(p[1])] for p in e.get("points", ())]
        if len(pts) < 4:
            continue
        out.append({"bbox": [round(v, 1) for v in bbox_of_points(pts)],
                    "handle": e["handle"], "layer": e["layer"]})
    return out


def _entity_points(e):
    for key in ("start", "end", "insert", "center", "major_axis"):
        p = e.get(key)
        if p:
            yield p
    for p in e.get("points") or ():
        yield p


def _records(snapshot):
    """区域划分要看到的全部几何记录（模型空间 + 块展开）。"""
    for rec in snapshot.get("entities") or ():
        yield rec
    for rec in snapshot.get("derived") or ():
        yield rec


def components(snapshot, struct_layers=(), cell=CELL):
    """连通区域分量：``[(bbox, count, structs, [record, ...])]``，按结构图元数降序。

    与旧版 ``_clusters`` 的区别：额外返回成员记录本身（同一 dict 对象），因此
    ``partition(tag=True)`` 可以就地给记录打 ``region``，不需要下游再算一遍。
    """
    buckets: dict[tuple[int, int], list] = {}
    for e in _records(snapshot):
        pts = list(_entity_points(e))
        if not pts:
            continue
        # Mark the cells occupied by each short/medium geometry span, not
        # merely its first endpoint. Using only the first point makes region
        # partitioning change substantially under rotation or line reversal.
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        x0, x1 = int(min(xs) // cell), int(max(xs) // cell)
        y0, y1 = int(min(ys) // cell), int(max(ys) // cell)
        if (x1 - x0 + 1) * (y1 - y0 + 1) <= 16:
            for ix in range(x0, x1 + 1):
                for iy in range(y0, y1 + 1):
                    buckets.setdefault((ix, iy), []).append(e)
        else:
            # Very large axes/title-frame geometry cannot be used to connect
            # entire independent plans. Sample its endpoints only.
            for p in (pts[0], pts[-1]):
                key = (int(p[0] // cell), int(p[1] // cell))
                buckets.setdefault(key, []).append(e)

    wanted = set(struct_layers or ())
    seen, out = set(), []
    for key in buckets:
        if key in seen:
            continue
        stack, cells = [key], []
        seen.add(key)
        while stack:
            cx, cy = stack.pop()
            cells.append((cx, cy))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    nb = (cx + dx, cy + dy)
                    if nb in buckets and nb not in seen:
                        seen.add(nb)
                        stack.append(nb)
        pts, members, structs, member_ids = [], [], 0, set()
        for c in cells:
            for e in buckets[c]:
                if id(e) in member_ids:
                    continue
                member_ids.add(id(e))
                members.append(e)
                pts.extend(_entity_points(e))
                if wanted and e.get("layer") in wanted:
                    structs += 1
        if not pts:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        out.append(([min(xs), min(ys), max(xs), max(ys)], len(members), structs, members))
    out.sort(key=lambda r: -r[2])
    return out


def _clusters(snapshot, struct_layers, cell=CELL):
    """兼容旧签名：``[(bbox, entity_count, struct_count)]``。"""
    return [(b, n, s) for (b, n, s, _m) in components(snapshot, struct_layers, cell)]


def _area(bbox):
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _span(bbox):
    return max(bbox[2] - bbox[0], bbox[3] - bbox[1])


def _frame_of(bbox, frames):
    """区域落在哪个图框内（取面积最小的包含框）。"""
    best = None
    for f in frames:
        b = f["bbox"]
        if b[0] <= bbox[0] and b[1] <= bbox[1] and b[2] >= bbox[2] and b[3] >= bbox[3]:
            if best is None or _area(b) < _area(best["bbox"]):
                best = f
    return best["handle"] if best else None


# --------------------------------------------------------------------------- 单位
def _line_lengths(records):
    """区域里 LINE / 多段线段的长度（用于判定"这张图是按什么单位画的"）。"""
    out = []
    for e in records or ():
        if e.get("type") == "LINE" and e.get("start") and e.get("end"):
            (x1, y1), (x2, y2) = e["start"], e["end"]
            out.append(math.hypot(float(x2) - float(x1), float(y2) - float(y1)))
        elif e.get("type") in ("LWPOLYLINE", "POLYLINE"):
            pts = e.get("points") or ()
            for a, b in zip(pts, list(pts)[1:]):
                out.append(math.hypot(float(b[0]) - float(a[0]),
                                      float(b[1]) - float(a[1])))
    return [v for v in out if v > 0]


def unit_context(bbox, records=()) -> dict:
    """区域自己的单位上下文：只判定"明显不可能"，不做激进换算。

    判据用**线段长度的中位数**，不用包围盒跨度：真实图纸里常有零星远离主体的
    图元（图签、废弃块、注释），它们会把包围盒拉到几十公里，按跨度判单位会把
    一整张 mm 图纸当成 inch 缩掉 1000 倍。中位数对这种离群点免疫。

    mm 图纸的墙线段中位数落在 50mm~30m。中位数偏出这个区间，才去候选比例里
    找能把中位数和区域跨度**同时**拉回平面量级的换算系数；找不到就只报告
    ``implausible``，绝不换算（宁可不动，也不能把图纸缩没）。
    """
    span = _span(bbox) if bbox else 0.0
    lengths = sorted(_line_lengths(records))
    med = lengths[len(lengths) // 2] if lengths else 0.0
    out = {"span": round(span, 1), "median_len": round(med, 1),
           "scale": 1.0, "state": "ok"}
    if not med:
        out["state"] = "unknown"
        return out
    if MIN_LINE_MM <= med <= MAX_LINE_MM:
        if span and span < MIN_PLAN_SPAN:
            out["state"] = "small"
        return out
    # 短线段多（洞口/填充/图例）会把中位数压到 50mm 以下，但区域跨度仍在一个
    # 平面的合理量级里 —— 这说明单位没问题，只是这张图画得碎，不要报异常。
    if med < MIN_LINE_MM and span and MIN_PLAN_SPAN <= span <= MAX_PLAN_SPAN:
        out["state"] = "small_lines"
        return out
    best = None
    for factor in UNIT_SCALES:
        m2, sp2 = med * factor, span * factor
        if not (MIN_LINE_MM <= m2 <= MAX_LINE_MM):
            continue
        if span and not (MIN_PLAN_SPAN <= sp2 <= MAX_PLAN_SPAN):
            continue
        score = abs(math.log(m2 / 3_000.0)) + (abs(math.log(sp2 / 30_000.0))
                                               if span else 0.0)
        if best is None or score < best[0]:
            best = (score, factor, m2, sp2)
    if best is None:
        out["state"] = "implausible"
        return out
    out.update({"scale": best[1], "state": "rescaled",
                "scaled_median": round(best[2], 1),
                "scaled_span": round(best[3], 1)})
    return out


def _scale_value(v, s):
    if isinstance(v, (list, tuple)):
        return [_scale_value(x, s) for x in v]
    try:
        return float(v) * s
    except (TypeError, ValueError):
        return v


#: 记录里需要随单位换算一起缩放的坐标/长度字段
_SCALED_FIELDS = ("start", "end", "insert", "center", "major_axis", "points",
                  "loops", "bbox", "radius", "height", "w", "h", "width", "depth")


def scale_records(records, scale) -> int:
    """把一组快照记录按 ``scale`` 缩放（就地）。返回缩放条数。

    块参照的 ``matrix`` 线性部分与平移量一起缩放，否则门几何（由块定义经
    matrix 变换得到）会保持原尺寸，与缩放后的坐标对不上。
    """
    if not scale or abs(scale - 1.0) < 1e-12:
        return 0
    n = 0
    for rec in records or ():
        if not isinstance(rec, dict):
            continue
        for key in _SCALED_FIELDS:
            if rec.get(key) is None:
                continue
            val = rec.get(key)
            if isinstance(val, (int, float)):
                rec[key] = float(val) * scale
            else:
                rec[key] = _scale_value(val, scale)
        if rec.get("xscale") is not None:
            rec["xscale"] = float(rec["xscale"]) * scale
            rec["yscale"] = float(rec.get("yscale") or 1.0) * scale
        m = rec.get("matrix")
        if isinstance(m, list) and len(m) == 16:
            rec["matrix"] = [float(v) * scale if i not in (3, 7, 11, 15) else float(v)
                             for i, v in enumerate(m)]
            rec["matrix"][15] = 1.0
        n += 1
    return n


# --------------------------------------------------------------------------- 主体
#: 图框可信度下限：图框面积 / 区域面积。< 这个比例判为伪图框（详图符号、索引框），
#: 宁可整图不裁，也不能把图裁成一个小方块。
MIN_FRAME_RATIO = 0.05


def partition(snapshot, frame_layers=(), struct_layers=(), pad_ratio=0.05,
              max_regions=40, tag=False, resolve_units=False) -> dict:
    """返回 ``{"frames", "regions", "plan", "plans", "multi_plan", "method", ...}``。

    ``tag=True``：给区域内每条记录写 ``region`` 字段（就地）；
    ``resolve_units=True``：对判定为单位异常的区域就地换算。两者都报告条数。
    """
    frames = _closed_rects(snapshot, set(frame_layers) if frame_layers else ())
    comps = components(snapshot, set(struct_layers) if struct_layers else ())

    regions = []
    for k, (bbox, count, structs, members) in enumerate(comps, 1):
        layers = {}
        for e in members:
            lay = e.get("layer")
            if lay:
                layers[lay] = layers.get(lay, 0) + 1
        top = sorted(layers.items(), key=lambda kv: (-kv[1], str(kv[0])))[:LAYER_TOP]
        regions.append({"id": f"REGION-{k:02d}",
                        "bbox": [round(v, 1) for v in bbox],
                        "entities": count, "struct": structs,
                        "frame": _frame_of(bbox, frames),
                        "area_m2": round(_area(bbox) / 1e6, 1),
                        "layers": [{"layer": str(l), "count": n} for l, n in top],
                        "_members": members})
    plan, method, multi = None, "none", False
    primary_struct = regions[0]["struct"] if regions else 0
    if regions and primary_struct >= MIN_PLAN_STRUCT:
        plan, method = list(regions[0]["bbox"]), "structure-cluster"
        primary_area = _area(plan) or 1.0
        for r in regions[1:]:
            if r["struct"] >= MULTI_PLAN_RATIO * primary_struct:
                multi = True
                break
        for r in regions:
            if _area(r["bbox"]) < DETAIL_RATIO * primary_area:
                r["kind"] = "detail"
            elif r["struct"] >= MULTI_PLAN_RATIO * primary_struct:
                r["kind"] = "plan"
            else:
                r["kind"] = "unknown"
        regions[0]["kind"] = "plan"
    elif frames:
        # 图框只在"自己也占了一块地方"时才可信。单图层图实测过反例：整张
        # 126×89 m 的图里只有一个 415×427 mm 的详图符号是闭合矩形，旧规则
        # "挑最大的闭合矩形当图框"直接把它当成了图纸范围，于是图被裁掉 96%。
        region_area = _area(regions[0]["bbox"]) if regions else 0.0
        best = max(frames, key=lambda f: _area(f["bbox"]))
        frame_area = _area(best["bbox"])
        if region_area > 0 and frame_area / region_area >= MIN_FRAME_RATIO:
            plan, method = best["bbox"], "frame"
        else:
            plan, method = None, "no-crop"
        for r in regions:
            r.setdefault("kind", "unknown")
    else:
        for r in regions:
            r.setdefault("kind", "unknown")

    tagged = rescaled = 0
    for r in regions:
        unit = unit_context(r["bbox"], r["_members"])
        r["unit"] = unit
        if (resolve_units and unit.get("state") == "rescaled"
                and r.get("kind") == "plan"):
            rescaled += scale_records(r["_members"], unit["scale"])
            r["bbox"] = [round(v * unit["scale"], 1) for v in r["bbox"]]
            r["area_m2"] = round(_area(r["bbox"]) / 1e6, 1)
        if tag:
            rid = r["id"]
            for e in r["_members"]:
                e["region"] = rid
                tagged += 1

    if plan:
        pad = pad_ratio * max(plan[2] - plan[0], plan[3] - plan[1])
        plan = [round(plan[0] - pad, 1), round(plan[1] - pad, 1),
                round(plan[2] + pad, 1), round(plan[3] + pad, 1)]
    plans = [r["bbox"] for r in regions if r.get("kind") == "plan"]
    reported = [{k: v for k, v in r.items() if k != "_members"}
                for r in regions[:max_regions]]
    return {"frames": frames[:20], "regions": reported, "plan": plan,
            "plans": plans, "multi_plan": multi, "method": method,
            "tagged": tagged, "rescaled": rescaled,
            "regions_total": len(regions)}


# --------------------------------------------------------------------------- 区域取用
def region_of_point(regions, xy):
    """点落在哪个区域（取面积最小的包含区域）。"""
    if not xy:
        return None
    x, y = float(xy[0]), float(xy[1])
    best = None
    for r in regions or ():
        b = r.get("bbox")
        if not b or not (b[0] <= x <= b[2] and b[1] <= y <= b[3]):
            continue
        if best is None or _area(b) < _area(best["bbox"]):
            best = r
    return (best or {}).get("id")


def region_bbox(regions, region_id):
    for r in regions or ():
        if r.get("id") == region_id:
            return r.get("bbox")
    return None


def _in_bbox(rec, bbox) -> bool:
    pts = list(_entity_points(rec))
    if not pts:
        return False
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return not (max(xs) < bbox[0] or min(xs) > bbox[2]
                or max(ys) < bbox[1] or min(ys) > bbox[3])


def sub_snapshot(snapshot, region_id, regions=None) -> dict:
    """按区域切一份可独立识别的子快照（缺 region 标记时用 bbox 兜底）。"""
    bbox = region_bbox(regions, region_id) if regions else None
    out = dict(snapshot or {})
    keep = {"entities": [], "derived": []}
    for key in ("entities", "derived"):
        for rec in (snapshot or {}).get(key) or ():
            rid = rec.get("region")
            if rid == region_id or (rid is None and bbox and _in_bbox(rec, bbox)):
                keep[key].append(rec)
    out.update(keep)
    out["region"] = region_id
    return out


def summary(part: dict) -> dict:
    """区域摘要（进节点输出，便于审计"哪片区域被识别了/被换算过"）。"""
    regions = (part or {}).get("regions") or []
    kinds = {}
    for r in regions:
        kinds[r.get("kind") or "unknown"] = kinds.get(r.get("kind") or "unknown", 0) + 1
    return {"total": len(regions), "kinds": kinds,
            "multi_plan": bool((part or {}).get("multi_plan")),
            "method": (part or {}).get("method"),
            "rescaled": (part or {}).get("rescaled", 0),
            "plans": [r.get("id") for r in regions if r.get("kind") == "plan"],
            "unit_suspect": [r.get("id") for r in regions
                             if (r.get("unit") or {}).get("state")
                             in ("rescaled", "implausible")]}
