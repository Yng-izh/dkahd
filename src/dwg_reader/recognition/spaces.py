"""空间面域种子（spaces_seed）：墙网 -> 初步空间面域。

作用（见 ``work/packaging/architecture.md`` §3.4）：

1. 给洞口判定提供 ``space_connection`` 证据：门洞两侧是不是两个不同空间、
   窗洞是不是"室内 ↔ 室外"；
2. 给房间识别提供局部反馈：房间名所在空面、面域面积，用于判断连通域是否
   被正确围合（而不是整层粘成一片）。

本模块只回答"哪里是一块被墙围起来的空面"，不判断房间名、不做房间分类、
不产出构件。缺口按同一墙线临时闭合（门窗洞在墙网里也是缺口）。
"""
from __future__ import annotations

import math

from . import geometry as GEO
from . import rooms as RM
from . import walls as WALLS

#: 面域栅格分辨率（mm）。这里只要拓扑，比房间识别的 10mm 粗一档。
RES = 20.0
#: 小于该面积（mm²）的空面视为墙缝，不算空间
#: 观测面域允许补合的最大缺口（洞口尺度，mm）
OBSERVED_SEAL_MAX = 1500.0
MIN_FACE_AREA = 1.0e6          # 1 m²
#: 单面上限：超过它说明围合失败（与室外连通），不做轮廓
MAX_FACE_AREA = 5.0e9          # 5000 m²
#: 面数量上限（防止极端图纸把下游节点拖死）
MAX_FACES = 300


def _barrier_axis(item):
    """洞口记录 -> 它的轴线两点（``opening`` 优先，其次 ``axis``）。"""
    for key in ("opening", "axis"):
        seg = (item or {}).get(key)
        if isinstance(seg, (list, tuple)) and len(seg) == 2:
            try:
                return ([float(seg[0][0]), float(seg[0][1])],
                        [float(seg[1][0]), float(seg[1][1])])
            except (TypeError, ValueError, IndexError):
                continue
    return None


def _barrier_center(item):
    seg = _barrier_axis(item)
    if seg is None:
        return None
    return [(seg[0][0] + seg[1][0]) / 2.0, (seg[0][1] + seg[1][1]) / 2.0]


def barrier_segments(items, *, thick=200.0, scale=None) -> list:
    """已观测洞口 -> 可以当作"隔断墙"补进栅格的线段。

    准入只看两件与画法无关的物理量：

    * **有几何**：能给出轴线两端（门块 / 开启弧 / 墙缺口都行）；
    * **宽度合理**：比 2 个门宽更宽的开口不算"能被门关上的洞"，补上它等于造墙；
      比墙厚还短的"洞口"是接头断线（见 ``scale.drop_artifact_gaps``），补了也
      什么隔不开。
    """
    from . import scale as SC
    th = SC.thresholds(scale or {})
    door_w = float(th.get("door_w") or SC.DEFAULT_DOOR_W)
    # 上界来自 STRUCTURE（无量纲倍率 × 图内门宽），不在这里写死毫米数
    cap = float(SC.STRUCTURE["barrier_max_gap_door"]) * door_w
    floor = float(thick or th.get("thickness") or SC.DEFAULT_THICKNESS)
    out = []
    for item in items or ():
        seg = _barrier_axis(item)
        if seg is None:
            continue
        length = math.hypot(seg[1][0] - seg[0][0], seg[1][1] - seg[0][1])
        if length < floor or length > cap:
            continue
        out.append(seg)
    return out


def _wall_outline(wall, thick):
    t = float(wall.get("thickness") or thick or 200.0)
    a = WALLS._along(wall, wall["start"])
    b = WALLS._along(wall, wall["end"])
    if a > b:
        a, b = b, a
    return WALLS._outline(wall, a, b, t, wall["center"])


def build(walls_seg, columns=(), partitions=(), bbox=None, thick=None,
          res=RES, seal_gaps=True, max_gap=9000.0, max_faces=MAX_FACES,
          extra_edges=(), barriers=(), scale=None) -> dict:
    """墙段 -> 初步空间面域。

    返回 ``{"faces": [...], "grid": {...}, "stats": {...}}``；``faces`` 每项：

    ``id`` / ``polygon``（world 折线）/ ``bbox`` / ``area``（mm²）/``cells`` /
    ``center``。面域按面积从大到小排序，ID 形如 ``SPACE-03``。
    """
    walls = [w for w in walls_seg if w.get("start") and w.get("end")]
    if not walls:
        return {"faces": [], "grid": None,
                "stats": {"faces": 0, "sealed": 0, "reason": "no_walls"}}

    if thick is None:
        vals = sorted(float(w["thickness"]) for w in walls if w.get("thickness"))
        thick = vals[len(vals) // 2] if vals else 200.0

    if not bbox:
        xs = [p[0] for w in walls for p in (w["start"], w["end"])]
        ys = [p[1] for w in walls for p in (w["start"], w["end"])]
        pad = 600.0
        bbox = [min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad]

    g = RM.Grid(bbox, res=res)

    arc_used = 0
    for w in walls:
        if w.get("arc") and w.get("outline"):
            # 弧墙：铺折线带。按 start→end 画弦会把弧内侧的空间切掉一块，
            # 房间边界会变成一条直边（实测那张图最大房间虚胖到 365 m² 就是这个）。
            g.fill_poly(w["outline"])
            arc_used += 1
        elif w.get("orient") in ("H", "V"):
            g.fill_rect(_wall_outline(w, thick))
        else:                              # 斜墙：沿墙轴画带宽，避免 bbox 撑大
            g.fill_seg(w["start"], w["end"], half=float(thick) / 2.0)
    for c in columns:
        outline = c.get("outline") if isinstance(c, dict) else c
        if outline:
            g.fill_poly(outline)      # 真实轮廓：斜柱 / 非矩形柱不再占外接矩形
    part_rects = []
    for p in partitions:
        if p:
            g.fill_poly(p)
            xs = [float(q[0]) for q in p]
            ys = [float(q[1]) for q in p]
            part_rects.append([round(min(xs), 1), round(min(ys), 1),
                               round(max(xs), 1), round(max(ys), 1)])

    # 外部喂进来的推断边（来自缺陷修复试算）：只画进栅格用于闭合判断，不是真实墙
    inferred_used = 0
    for e in extra_edges or ():
        start, end = e.get("start"), e.get("end")
        if not start or not end:
            continue
        g.fill_seg(start, end, half=max(float(thick) / 2.0, res))
        inferred_used += 1

    # ---------- 已观测洞口当作"隔断"补齐 ----------
    # 门洞是墙上真实的缺口：不补，两间房就经门洞连成一个连通域，房间数永远等于
    # "楼层数 × 走到头"（实测艺术楼 56 道墙只围出 11 个面域，房名 14 间只落到 8）。
    # 洞口的**位置和宽度是已经确认的构件证据**（门块/开启弧），比"把两个墙端点
    # 连起来"可靠得多：后者要求两端共线且缺口在补缝上限内，斜墙和错位墙头都不成立。
    # 这也是唯一允许进**观测面域**的"补墙"——它补的是已观测到的洞口，不是猜的墙。
    barriers_used = 0
    barrier_segs = barrier_segments(barriers, thick=float(thick), scale=scale)
    for start, end in barrier_segs:
        g.fill_seg(start, end, half=float(thick) / 2.0)
        barriers_used += 1

    small_gap = max(150.0, 2.0 * float(thick))
    #: 观测面域允许的补缝上限 = 洞口尺度（门/窗宽）。门洞本身就是墙上的缺口，
    #: 一刀不补的话所有房间都经门连通到室外，一个闭合面域都不会有；
    #: 但走廊级的 9000mm 虚拟边不能进观测面域——那才是"凭空造房间"。
    observed_gap = min(max_gap, max(OBSERVED_SEAL_MAX, small_gap))

    # ---------- 观测面域（可靠拓扑基线）：只用真实墙体 + 端头小缝 ----------
    # 关键：**不补大缺口**。用虚拟边把 9000mm 的开口封起来，会把开放空间
    # （中庭、走道、大开间）变成房间，还会反向支持不存在的门窗——所以观测面域
    # 必须用"未补大缝"的栅格单独算一份，和推断面域并存，不能混为一谈。
    g_obs = _clone_grid(g)
    _seal(g_obs, walls, max_gap=observed_gap, virtual=[])
    observed_faces, _ = _faces_from_grid(g_obs, max_faces)

    # ---------- 推断面域（修复用）：允许大缺口虚拟闭合 ----------
    virtual = []                 # 临时补墙 = 虚拟边候选，只记录，不写回真实墙体
    sealed = 0
    if seal_gaps:
        sealed = _seal(g, walls, max_gap=max_gap, virtual=virtual)
    # 墙段之间的小缝（面线端头、柱脚）也补齐，否则面域会互相串通
    sealed += _seal(g, walls, max_gap=small_gap, virtual=virtual)
    faces, ncand = _faces_from_grid(g, max_faces)
    # 推断面域里，凡是"不靠走廊级虚拟边也已经闭合"的面域标 observed（两次栅格
    # 结果按中心点归属判定），其余标 hypothesis：这些房间是靠 9000mm 虚拟墙
    # 才成立的，必须作为竞争解释保留，不能当成已观测到的房间。
    for f in faces:
        f["seal_state"] = ("observed" if _face_contains(observed_faces, f["center"])
                           else "hypothesis")
    for f in observed_faces:
        f["seal_state"] = "observed"

    for i, e in enumerate(virtual, 1):
        e.setdefault("id", f"gap:{i}")
        e.setdefault("geometry_state", "inferred")
        e.setdefault("adopted", True)
    return {"faces": faces, "observed_faces": observed_faces, "open_spaces": [],
            "grid": {"res": res, "bbox": [round(v, 1) for v in bbox],
                     "nx": g.nx, "ny": g.ny},
            "stats": {"faces": len(faces),
                      "observed_faces": len(observed_faces),
                      "hypothesis_faces": sum(1 for f in faces
                                              if f.get("seal_state") == "hypothesis"),
                      "sealed": sealed,
                      "candidates": ncand, "thickness": round(float(thick), 1),
                      "total_area_m2": round(sum(f["area"] for f in faces) / 1e6, 2),
                      "virtual_edges": virtual,       # 全量保留，可逐条撤销
                      "virtual_edge_count": len(virtual),
                      "partition_rects": part_rects[:200],
                      "barriers_used": barriers_used,
                      "inferred_edges_used": inferred_used}}


def build_regions(walls_seg, regions=(), columns=(), partitions=(), bbox=None,
                  thick=None, res=RES, min_area=MIN_FACE_AREA, max_faces=MAX_FACES,
                  barriers=(), scale=None) -> dict:
    """**按建筑平面分区**建面域：一张图里贴了好几栋楼/好几个平面时，分别算。

    为什么必须分区：栅格是全图一张，外框按整张图算。当图里有两个相距几百万毫米
    的平面时（大图就是这样：主平面 + 另一栋的平面），栅格被撑到千万级单元，
    ``MAX_FACE_AREA``/``MAX_FACES`` 的限额全部被主平面吃掉，另一栋的面域一个都
    出不来——那些房名于是全部落到 "distant"，房间数凭空少掉三分之一。

    分区规则：只取 ``kind == "plan"`` 的区域；每个区域内只喂"中点落在该区域内"的
    墙段/柱/隔断；面域 id 带区域前缀，面积与栅格互不影响。
    """
    plans = [r for r in (regions or ()) if r.get("kind") == "plan" and r.get("bbox")]
    if len(plans) <= 1:
        out = build(walls_seg, columns=columns, partitions=partitions, bbox=bbox,
                    thick=thick, res=res, max_faces=max_faces,
                    barriers=barriers, scale=scale)
        out.setdefault("stats", {})["regions"] = 1
        return out

    def inside(pt, b, pad=2000.0):
        return (b[0] - pad <= pt[0] <= b[2] + pad
                and b[1] - pad <= pt[1] <= b[3] + pad)

    faces, observed, stats = [], [], {"regions": len(plans), "per_region": {}}
    for k, reg in enumerate(plans, 1):
        b = reg["bbox"]
        rid = reg.get("id") or f"REGION-{k:02d}"
        sub_walls = [w for w in walls_seg or ()
                     if w.get("start") and inside(
                         ((float(w["start"][0]) + float(w["end"][0])) / 2.0,
                          (float(w["start"][1]) + float(w["end"][1])) / 2.0), b)]
        if not sub_walls:
            stats["per_region"][rid] = {"walls": 0, "faces": 0}
            continue
        sub_cols = [c for c in (columns or ())
                    if (c.get("center") or (0, 0)) and inside(c["center"], b)]
        sub_parts = []
        for p in partitions or ():
            if not p:
                continue
            xs = [float(q[0]) for q in p]
            ys = [float(q[1]) for q in p]
            if inside(((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0), b):
                sub_parts.append(p)
        sub_doors = [d for d in (barriers or ()) if _barrier_center(d) is not None
                     and inside(_barrier_center(d), b)]
        part = build(sub_walls, columns=sub_cols, partitions=sub_parts, bbox=None,
                     thick=thick, res=res, max_faces=max_faces,
                     barriers=sub_doors, scale=scale)
        for f in part.get("faces") or ():
            faces.append(dict(f, id=f"{rid}:{f['id']}", region=rid))
        for f in part.get("observed_faces") or ():
            observed.append(dict(f, id=f"{rid}:{f['id']}", region=rid))
        st = part.get("stats") or {}
        stats["per_region"][rid] = {"walls": len(sub_walls),
                                    "faces": st.get("faces"),
                                    "observed": st.get("observed_faces")}
        for key in ("sealed", "candidates", "virtual_edge_count", "inferred_edges_used",
                    "barriers_used"):
            stats[key] = stats.get(key, 0) + int(st.get(key) or 0)
        stats.setdefault("virtual_edges", []).extend(
            [dict(e, region=rid) for e in (st.get("virtual_edges") or ())])

    faces.sort(key=lambda f: -(f.get("area") or 0))
    stats.update({"faces": len(faces), "observed_faces": len(observed),
                  "hypothesis_faces": sum(1 for f in faces
                                          if f.get("seal_state") == "hypothesis"),
                  "total_area_m2": round(sum(f.get("area") or 0 for f in faces) / 1e6, 2),
                  "thickness": round(float(thick), 1) if thick else None,
                  "partition_rects": []})
    return {"faces": faces, "observed_faces": observed, "open_spaces": [],
            "grid": None, "stats": stats}


def _seal(g, walls, max_gap=9000.0, virtual=None):
    """Bridge only *collinear* observed intervals, in a shared local basis.

    Raw (dir,nrm) tuples are not stable under rotation / CAD roundoff. Each
    line group's occupied intervals remain separate until this function
    explicitly creates a *virtual* gap with recorded provenance.
    """
    from . import linegroups as LG
    groups = LG.grouped(walls, angle_tol=0.3, pos_tol=2.5)
    sealed = 0
    for group in groups:
        if len(group) < 2:
            continue
        group.sort(key=lambda x: x['lo'])
        w0 = group[0]
        prev = None
        for w in group:
            a, b = w['lo'], w['hi']
            if prev is not None and 1.0 < a-prev <= max_gap:
                p1 = WALLS._world(w0, prev, w0['center'])
                p2 = WALLS._world(w0, a, w0['center'])
                g.fill_seg(p1, p2, half=max(30.0, g.res))
                sealed += 1
                if virtual is not None:
                    virtual.append({'orient': w0['orient'],
                                    'pos': round(float(w0['center']),1),
                                    'dir': w0['dir'], 'nrm': w0['nrm'],
                                    'start': p1, 'end': p2,
                                    'span': [round(prev,1),round(a,1)],
                                    'length': round(a-prev,1),
                                    'wall_id': w0.get('id'),
                                    'reason': 'gap_bridged'})
            prev = b if prev is None else max(prev, b)
    return sealed


# --------------------------------------------------------------------------- 查询
def locate(faces, x, y):
    """点所在的空间面域；不在任何面域内返回 None（= 室外 / 未围合）。"""
    for f in faces:
        b = f["bbox"]
        if not (b[0] <= x <= b[2] and b[1] <= y <= b[3]):
            continue
        if RM.point_in_polygon(x, y, f["polygon"]):
            return f
    return None


def connection(faces, seg, thick=200.0, probe=80.0):
    """洞口两侧的空间关系。

    返回 ``(side_a, side_b)``：面域 dict 表示室内空间，``None`` 表示室外/未围合。
    调用方据此判断"连两个空间""连室外""还是根本没连"。
    """
    if not faces or not seg or len(seg) != 2:
        return None, None
    (x1, y1), (x2, y2) = seg
    dx, dy = float(x2) - float(x1), float(y2) - float(y1)
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return None, None
    nx, ny = -dy / length, dx / length
    mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    off = max(float(thick) / 2.0, 60.0) + float(probe)
    return (locate(faces, mx + nx * off, my + ny * off),
            locate(faces, mx - nx * off, my - ny * off))


def attach_rooms(rooms, faces):
    """给房间记录补 ``space_id`` / ``space_area_m2``（诊断与面积校核用）。"""
    for r in rooms:
        lab = r.get("label") or {}
        f = locate(faces, lab.get("x", 0.0), lab.get("y", 0.0)) if faces else None
        r["space_id"] = f["id"] if f else None
        r["space_area"] = f["area"] if f else None
    return rooms


def _clone_grid(g):
    """复制栅格掩码（真实几何已画入、尚未补任何缺口）。"""
    import copy
    out = copy.copy(g)
    out.mask = bytearray(g.mask)
    return out


def _faces_from_grid(g, max_faces):
    """栅格连通域 -> 面域列表（按面积降序，ID 形如 SPACE-03）。"""
    comp = RM._Components(g)
    outside = comp.outside_root
    candidates = []
    for root, size in comp.size.items():
        if root == outside:
            continue
        area = size * g.res * g.res
        if area < MIN_FACE_AREA or area > MAX_FACE_AREA:
            continue
        candidates.append((area, root, size))
    candidates.sort(reverse=True)
    faces = []
    for k, (area, root, size) in enumerate(candidates[:max_faces], 1):
        # Trace boundary from RLE components: no O(area) set-of-cells copy.
        poly = RM.component_contour(comp, root, g)
        poly_area = GEO.polygon_area(poly)
        if len(poly) < 3:
            continue
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        b = [min(xs), min(ys), max(xs), max(ys)]
        faces.append({"id": f"SPACE-{k:02d}",
                      "polygon": [[float(p[0]), float(p[1])] for p in poly],
                      "bbox": [round(v, 1) for v in b],
                      "area": round(area, 1), "poly_area": round(poly_area, 1),
                      "cells": size,
                      "center": [round((b[0] + b[2]) / 2.0, 1),
                                 round((b[1] + b[3]) / 2.0, 1)]})
    return faces, len(candidates)


def _face_contains(faces, point, pad=1.0):
    """点是否落在某个面域的外接框内（用于把两次栅格结果对上号）。"""
    x, y = point
    for f in faces or ():
        b = f.get("bbox") or ()
        if len(b) == 4 and b[0] - pad <= x <= b[2] + pad \
                and b[1] - pad <= y <= b[3] + pad:
            return True
    return False
