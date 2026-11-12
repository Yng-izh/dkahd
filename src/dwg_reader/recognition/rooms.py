"""房间识别：房间名文字 + 墙体/柱/门窗洞口围合的闭合轮廓（栅格泛洪 + 边界追踪）。

不依赖图像识别：栅格只是把"图元围合关系"离散化，用于求连通域与轮廓，
轮廓随后吸附回墙体面线坐标，面积以栅格单元数计。
"""
from __future__ import annotations

import math
import re

from .base import polygon_area

RES = 10.0          # mm / cell
PAD = 600.0         # 外扩

# Functional suffixes must not assume every room is called "...室".
# These are architectural space nouns; annotations are rejected below.
ROOM_SUFFIX = re.compile(
    r"(室|厅|卫|间|道|斗|阳台|厨房|井|梯|库|所|廊|厕|卧|铺|"
    r"阅览|办公|会议|财务|控制|接待|大堂|门厅|走道|通道|过厅)$")
ROOM_EXCLUDE = re.compile(
    r"留洞|挡烟|排烟|净高|面积|工程做法|高差|距地|参见|详见|做法|坡道|台阶|"
    r"平面图|立面图|剖面图|大样|图号|索引|设计|装修|栏杆|说明|墙身|"
    r"楼梯$|窗$|门$")
AREA_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*m$")

#: 房间名可能落在的图层（天正 SPACE 对象名 / 建筑房间名 / 公版文字）
ROOM_NAME_LAYERS = ("SPACE", "PUB_TEXT", "AD-NAME-ROOM", "AD-NAME", "ROOM",
                    "房间名", "A-NAME-ROOM", "A-AREA-NAME")
#: 无信息量的泛称：同一连通域里若有专名，优先专名
GENERIC_ROOM_NAMES = {"房间", "空间", "区域"}


def room_labels(texts, bbox, layers=None):
    """从文字中提取房间名与面积标注（按坐标去重，同点位重复标注只留一条）。

    ``layers`` 给了就只认这些图层（来自 profile）；否则用内置白名单。
    """
    wanted = set(layers) if layers else set(ROOM_NAME_LAYERS)
    names, areas = [], []
    seen_name, seen_area = set(), set()
    for t in texts:
        s = t["text"].strip()
        # CAD room labels are frequently letter-spaced, e.g. "书    库".
        # Remove whitespace only for Chinese-dominant short labels.
        if s and any("\u4e00" <= ch <= "\u9fff" for ch in s):
            s = re.sub(r"\s+", "", s)
        if not s or not (bbox[0] - 500 <= t["x"] <= bbox[2] + 500
                         and bbox[1] - 500 <= t["y"] <= bbox[3] + 500):
            continue
        if t["layer"] not in wanted:
            continue
        m = AREA_RE.match(s)
        if m:
            key = (round(t["x"]), round(t["y"]), s)
            if key in seen_area:
                continue
            seen_area.add(key)
            areas.append({"value": float(m.group(1)), "x": t["x"], "y": t["y"],
                          "handle": t["handle"], "layer": t["layer"], "text": s})
            continue
        if s == "2" and t["h"] < 150:
            continue
        if ROOM_SUFFIX.search(s) and not ROOM_EXCLUDE.search(s) and 2 <= len(s) <= 12:
            key = (round(t["x"]), round(t["y"]), s)
            if key in seen_name:
                continue
            seen_name.add(key)
            names.append({"name": s, "x": t["x"], "y": t["y"], "h": t["h"],
                          "handle": t["handle"], "layer": t["layer"]})
    # 面积标注配对：房间名下方/附近 1200mm 内
    for n in names:
        best, bd = None, 1400.0
        for a in areas:
            d = math.hypot(a["x"] - n["x"], a["y"] - n["y"])
            if d < bd:
                bd, best = d, a
        if best:
            n["area_label"] = best["value"]
            n["area_handle"] = best["handle"]
            # m² 的上标 "2" 单独成字，挂到面积标注上作为同一语义
            sup = None
            for t in texts:
                if t["text"].strip() == "2" and t["h"] < 150 and \
                        math.hypot(t["x"] - best["x"], t["y"] - best["y"]) < 1100:
                    sup = t
                    break
            if sup:
                n["area_sup_handle"] = sup["handle"]
    return names, areas


class Grid:
    def __init__(self, bbox, res=RES):
        self.res = res
        self.x0 = bbox[0] - PAD
        self.y0 = bbox[1] - PAD
        self.nx = int((bbox[2] - bbox[0] + 2 * PAD) / res) + 2
        self.ny = int((bbox[3] - bbox[1] + 2 * PAD) / res) + 2
        self.mask = bytearray(self.nx * self.ny)   # 1 = 障碍

    def idx(self, i, j):
        return j * self.nx + i

    def to_cell(self, x, y):
        return int((x - self.x0) / self.res), int((y - self.y0) / self.res)

    def to_world(self, i, j):
        return self.x0 + i * self.res, self.y0 + j * self.res

    def fill_poly(self, pts):
        """多边形填充（扫描线 + 奇偶规则）。

        非矩形轮廓（斜柱、L 形隔断、带缺口的面层）不再退化成外接矩形，
        避免占掉不属于它的空间。
        """
        pts = [[float(p[0]), float(p[1])] for p in pts if len(p) >= 2]
        if len(pts) < 3:
            return
        ys = [p[1] for p in pts]
        j0 = max(0, self.to_cell(0.0, min(ys))[1])
        j1 = min(self.ny - 1, self.to_cell(0.0, max(ys))[1])
        edges = []
        n = len(pts)
        for k in range(n):
            x1, y1 = pts[k]
            x2, y2 = pts[(k + 1) % n]
            if abs(y1 - y2) > 1e-9:
                edges.append((x1, y1, x2, y2))
        for j in range(j0, j1 + 1):
            yc = self.y0 + (j + 0.5) * self.res
            xs = [x1 + (yc - y1) * (x2 - x1) / (y2 - y1) for (x1, y1, x2, y2) in edges
                  if (y1 <= yc < y2) or (y2 <= yc < y1)]
            if not xs:
                continue
            xs.sort()
            base = j * self.nx
            for a, b in zip(xs[0::2], xs[1::2]):
                i0 = max(0, int((a - self.x0) / self.res))
                i1 = min(self.nx - 1, int((b - self.x0) / self.res))
                for i in range(i0, i1 + 1):
                    self.mask[base + i] = 1

    def fill_rect(self, pts):
        if not pts or len(pts) < 3:
            return
        x0, y0 = pts[0][0], pts[0][1]
        axis_aligned = all(abs(p[0] - x0) < 1e-6 or abs(p[1] - y0) < 1e-6 for p in pts)
        if len(pts) > 4 or not axis_aligned:
            self.fill_poly(pts)
            return
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        i0, j0 = self.to_cell(min(xs), min(ys))
        i1, j1 = self.to_cell(max(xs), max(ys))
        i0, j0 = max(0, i0), max(0, j0)
        i1, j1 = min(self.nx - 1, i1), min(self.ny - 1, j1)
        for j in range(j0, j1 + 1):
            base = j * self.nx
            for i in range(i0, i1 + 1):
                self.mask[base + i] = 1

    def fill_seg(self, p1, p2, half=40.0):
        x1, y1 = p1
        x2, y2 = p2
        L = math.hypot(x2 - x1, y2 - y1)
        n = max(2, int(L / (self.res * 0.8)) + 1)
        nx, ny = -(y2 - y1) / (L or 1), (x2 - x1) / (L or 1)
        for k in range(n + 1):
            t = k / n
            cx, cy = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
            for s in (-1, 0, 1):
                px, py = cx + nx * half * s, cy + ny * half * s
                i, j = self.to_cell(px, py)
                if 0 <= i < self.nx and 0 <= j < self.ny:
                    self.mask[self.idx(i, j)] = 1

    # -------------------------------------------------- 泛洪
    def flood(self, seeds, stop_at_mask=True):
        """返回 cell 集合（set of (i,j)）。"""
        seen = bytearray(self.nx * self.ny)
        out = set()
        stack = list(seeds)
        while stack:
            i, j = stack.pop()
            if i < 0 or j < 0 or i >= self.nx or j >= self.ny:
                continue
            k = self.idx(i, j)
            if seen[k]:
                continue
            if stop_at_mask and self.mask[k]:
                continue
            seen[k] = 1
            out.add((i, j))
            stack.append((i + 1, j))
            stack.append((i - 1, j))
            stack.append((i, j + 1))
            stack.append((i, j - 1))
        return out

    def flood_outside(self):
        seeds = []
        for i in range(self.nx):
            seeds.append((i, 0))
            seeds.append((i, self.ny - 1))
        for j in range(self.ny):
            seeds.append((0, j))
            seeds.append((self.nx - 1, j))
        return self.flood(seeds)


def contour(cells, grid):
    """由栅格连通域追踪边界（逆时针，内部在左），返回世界坐标折线。

    边界边按"起点 -> 终点"入邻接表；遇到夹点（同一顶点有多条出边）时按
    右手法则（相对入射方向优先右转）选择，保证沿同一连通域的外轮廓走完。
    """
    edges = []
    for (i, j) in cells:
        if (i, j - 1) not in cells:
            edges.append(((i, j), (i + 1, j)))            # 下边，向右
        if (i + 1, j) not in cells:
            edges.append(((i + 1, j), (i + 1, j + 1)))    # 右边，向上
        if (i, j + 1) not in cells:
            edges.append(((i + 1, j + 1), (i, j + 1)))    # 上边，向左
        if (i - 1, j) not in cells:
            edges.append(((i, j + 1), (i, j)))            # 左边，向下
    adj = {}
    for a, b in edges:
        adj.setdefault(a, []).append(b)

    start = min(adj.keys(), key=lambda p: (p[1], p[0]))
    # 起点处优先选向右的出边（内部在左）
    cand = adj[start]
    cur = start
    nxt = min(cand, key=lambda p: (0 if p[0] > start[0] else 1, abs(p[0] - start[0])))
    ring = [start]
    prev_dir = (nxt[0] - cur[0], nxt[1] - cur[1])
    cur = nxt
    guard = 0
    while cur != start and guard < 4 * len(edges) + 10:
        guard += 1
        ring.append(cur)
        outs = adj.get(cur)
        if not outs:
            break
        if len(outs) == 1:
            nxt = outs[0]
        else:
            # 右手法则：右转(-90) > 直行 > 左转(+90) > 掉头
            order = [(prev_dir[1], -prev_dir[0]), prev_dir,
                     (-prev_dir[1], prev_dir[0]), (-prev_dir[0], -prev_dir[1])]
            best, bi = None, 99
            for o in outs:
                d = (o[0] - cur[0], o[1] - cur[1])
                k = order.index(d) if d in order else 99
                if k < bi:
                    bi, best = k, o
            nxt = best
        prev_dir = (nxt[0] - cur[0], nxt[1] - cur[1])
        cur = nxt
    pts = [grid.to_world(i, j) for (i, j) in ring]
    return pts


def point_in_polygon(px, py, pts):
    inside = False
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if (y1 > py) != (y2 > py):
            xi = x1 + (py - y1) * (x2 - x1) / (y2 - y1 or 1e-9)
            if xi > px:
                inside = not inside
    return inside


def room_polygon(cells, grid, simplify=15.0):
    """栅格边界 -> 正交闭合轮廓。

    栅格边界本身就是正交的，只做"去共线点 + 小台阶合并"，不再做会引入斜边的
    正交吸附，避免轮廓与房间实际范围错位。
    """
    raw = contour(cells, grid)
    if len(raw) < 4:
        return raw, polygon_area(raw)
    # 1) 去掉共线中间点
    pts = []
    n = len(raw)
    for i in range(n):
        p0, p1, p2 = raw[i - 1], raw[i], raw[(i + 1) % n]
        if abs(p0[0] - p1[0]) < 1e-6 and abs(p1[0] - p2[0]) < 1e-6:
            continue
        if abs(p0[1] - p1[1]) < 1e-6 and abs(p1[1] - p2[1]) < 1e-6:
            continue
        pts.append(p1)
    # 2) 合并小于 simplify 的台阶（连续两段都短则删掉中间点）
    changed = True
    while changed and len(pts) > 4:
        changed = False
        for i in range(len(pts)):
            a, b, c = pts[i - 1], pts[i], pts[(i + 1) % len(pts)]
            l1 = abs(b[0] - a[0]) + abs(b[1] - a[1])
            l2 = abs(c[0] - b[0]) + abs(c[1] - b[1])
            if l1 < simplify and l2 < simplify:
                pts.pop(i)
                changed = True
                break
    return pts, polygon_area(pts)


def build_and_fill(walls, columns, doors, windows, labels, bbox, partitions=(),
                   seal_gaps=True, max_gap=9000.0):
    g = Grid(bbox)
    for w in walls:
        g.fill_rect(w["outline"])
    for c in columns:
        g.fill_rect(c["outline"])
    for p in partitions:          # 卫生间隔断等非 WALL 图层的分隔构件
        g.fill_rect(p)
    if seal_gaps:
        # 同一墙线上相邻墙段之间的洞口在栅格中临时闭合，保证每个房间独立成域
        import collections as _c
        lines = _c.defaultdict(list)
        for w in walls:
            # 弧墙没有"同一条墙线上的相邻段"这回事（它的相邻段是弧长方向上的
            # 另一段弧，按 H/V 的区间去补会补出横穿房间的假墙）。
            if w.get("arc") or w["orient"] not in ("H", "V"):
                continue
            key = (w["orient"], round(w["center"]))
            if w["orient"] == "H":
                lines[key].append((w["start"][0], w["end"][0]))
            else:
                lines[key].append((w["start"][1], w["end"][1]))
        for (ori, c), segs in lines.items():
            segs.sort()
            prev = None
            for a, b in segs:
                if prev is not None and 1.0 < a - prev <= max_gap:
                    if ori == "H":
                        g.fill_seg([prev, c], [a, c], half=40)
                    else:
                        g.fill_seg([c, prev], [c, a], half=40)
                prev = b if prev is None else max(prev, b)
    # 门窗洞口封堵（否则相邻房间会串通）。**一律沿轴线补**：只有 insert + width
    # 时按门的旋转角铺，而不是固定朝 +X——旋转 90° 的门若朝 +X 补，补出来的是一
    # 堵横穿房间的假墙，房间会被切碎。
    for d in doors:
        seg = d.get("opening") or d.get("axis")
        if seg and len(seg) == 2:
            g.fill_seg(seg[0], seg[1], half=60)
        elif d.get("insert"):
            ang = math.radians(float(d.get("rotation") or 0.0))
            wdt = float(d.get("width") or 0.0)
            ins = d["insert"]
            g.fill_seg(ins, [ins[0] + math.cos(ang) * wdt,
                             ins[1] + math.sin(ang) * wdt], half=60)
    for w in windows:
        seg = w.get("axis") or w.get("opening")
        if seg and len(seg) == 2:
            g.fill_seg(seg[0], seg[1], half=60)

    # 一次性连通域标记（逐行游程 + 并查集），复杂度 O(单元数)；
    # 逐个 seed 泛洪在 7M 单元的图上会退化到十几分钟，且每个房间要重跑一次。
    comp = _Components(g)
    outside_cells = comp.component_cells(comp.outside_root, limit=MAX_ROOM_CELLS)

    # 同一连通域可能挂着多条房间名（天正 SPACE 的泛称"房间" + 建筑专业专名），
    # 按连通域归并，专名优先，避免一个房间输出多条记录。
    grouped = {}
    for lab in labels:
        cell = _seed_cell(g, lab)
        if cell is None:
            continue
        root = comp.root_of(*cell)
        if root is None:
            continue
        grouped.setdefault(root, []).append((lab, cell))

    rooms = []
    for root, items in grouped.items():
        lab, _cell = max(items, key=lambda it: _label_rank(it[0]))
        leak = (root == comp.outside_root)
        size = comp.size[root]
        if leak or size > MAX_ROOM_CELLS:
            rooms.append({"label": lab, "cells": size, "area": size * RES * RES,
                          "polygon": [], "poly_area": 0.0, "leak": True,
                          "seed_inside": False,
                          "aliases": [it[0]["name"] for it in items if it[0] is not lab]})
            continue
        cells = comp.component_cells(root)
        poly, area = room_polygon(cells, g)
        contains = point_in_polygon(lab["x"], lab["y"], poly) if len(poly) >= 3 else False
        rooms.append({"label": lab, "cells": size, "area": size * RES * RES,
                      "polygon": poly, "poly_area": area, "leak": False,
                      "seed_inside": contains,
                      "aliases": [it[0]["name"] for it in items if it[0] is not lab]})
    return g, outside_cells, rooms


def fill_from_faces(rooms, faces):
    """连通域漏判的房间用空间面域补回（spaces_seed 的局部反馈）。

    触发条件很窄：连通域追踪失败（``leak`` 或没有轮廓）**且**房间名落在某个
    已围合的空间面域内。此时用该面域的轮廓当房间轮廓，并标记
    ``polygon_from="space_seed"``，让下游知道这是粗轮廓（20mm 栅格）。
    """
    if not faces:
        return rooms
    from . import spaces as _spaces
    for r in rooms:
        if r.get("polygon") and not r.get("leak"):
            continue
        lab = r.get("label") or {}
        f = _spaces.locate(faces, lab.get("x", 0.0), lab.get("y", 0.0))
        if not f:
            continue
        r["polygon"] = [list(p) for p in f["polygon"]]
        r["poly_area"] = f.get("poly_area") or f["area"]
        r["area"] = f["area"]
        r["cells"] = f.get("cells")
        r["leak"] = False
        r["seed_inside"] = True
        r["polygon_from"] = "space_seed"
        r["space_id"] = f["id"]
    return rooms


MAX_ROOM_CELLS = 2_000_000     # 单个连通域上限：超过即视为与外部连通，不再追踪轮廓


def _label_rank(lab):
    """房间名优先级：专名 > 泛称；建筑专业房间名图层 > 天正 SPACE 泛称。"""
    generic = lab["name"] in GENERIC_ROOM_NAMES
    layer_bonus = 2 if lab.get("layer") not in ("SPACE", "PUB_TEXT") else 0
    return (0 if generic else 1) * 10 + layer_bonus


def _seed_cell(g, lab):
    """文字落点 -> 空单元；压在墙线上时就近找空单元。"""
    i, j = g.to_cell(lab["x"], lab["y"])
    if not (0 <= i < g.nx and 0 <= j < g.ny):
        return None
    if not g.mask[g.idx(i, j)]:
        return (i, j)
    for r in range(1, 40):
        for di in range(-r, r + 1):
            for dj in (-r, r):
                ii, jj = i + di, j + dj
                if 0 <= ii < g.nx and 0 <= jj < g.ny and not g.mask[g.idx(ii, jj)]:
                    return (ii, jj)
    return None


class _Components:
    """栅格空闲单元的连通域标记（游程 + 并查集），一次遍历得到全部房间域。"""

    def __init__(self, g):
        nx, ny = g.nx, g.ny
        runs = []                       # (j, i0, i1) 含端点
        row_runs = [[] for _ in range(ny)]
        for j in range(ny):
            row = g.mask[j * nx:(j + 1) * nx]
            i = 0
            while i < nx:
                k = row.find(0, i)
                if k < 0:
                    break
                e = row.find(1, k)
                if e < 0:
                    e = nx
                row_runs[j].append(len(runs))
                runs.append((j, k, e - 1))
                i = e
        self.runs = runs
        self.row_runs = row_runs
        parent = list(range(len(runs)))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for j in range(ny - 1):
            A, B = row_runs[j], row_runs[j + 1]
            p = q = 0
            while p < len(A) and q < len(B):
                _, a1, a2 = runs[A[p]]
                _, b1, b2 = runs[B[q]]
                if a2 < b1:
                    p += 1
                elif b2 < a1:
                    q += 1
                else:
                    union(A[p], B[q])
                    if a2 <= b2:
                        p += 1
                    else:
                        q += 1
        self.find = find
        self.root_of_run = [find(i) for i in range(len(runs))]
        size = {}
        for rid, r in enumerate(self.root_of_run):
            size[r] = size.get(r, 0) + (runs[rid][2] - runs[rid][1] + 1)
        self.size = size
        # 外部连通域：触及栅格外边界的游程
        outside = None
        for rid, (j, a, b) in enumerate(runs):
            if j == 0 or j == ny - 1 or a == 0 or b == nx - 1:
                outside = self.root_of_run[rid]
                break
        self.outside_root = outside
        # 每个 root -> 游程 id 列表，便于取单元集合
        self.by_root = {}
        for rid, r in enumerate(self.root_of_run):
            self.by_root.setdefault(r, []).append(rid)

    def root_of(self, i, j):
        import bisect
        rr = self.row_runs[j]
        if not rr:
            return None
        starts = [self.runs[r][1] for r in rr]
        k = bisect.bisect_right(starts, i) - 1
        if k < 0:
            return None
        rid = rr[k]
        _, a, b = self.runs[rid]
        return self.root_of_run[rid] if a <= i <= b else None

    def component_cells(self, root, limit=None):
        if root is None:
            return set()
        if limit is not None and self.size.get(root, 0) > limit:
            return set()
        cells = set()
        for rid in self.by_root.get(root, ()):
            j, a, b = self.runs[rid]
            for i in range(a, b + 1):
                cells.add((i, j))
        return cells



def component_contour(comp, root, grid):
    """Trace a large component directly from scanline runs, O(perimeter).

    Creating a set of 1-8 million cells for every component is both very slow
    and memory hungry. Runs already partition the same exact free cells.
    Output is the same world-coordinate exterior boundary as contour(cells),
    without materializing all interior cells; interiors are not included.
    """
    if root is None:
        return []
    by_row = {}
    for rid in comp.by_root.get(root, ()):
        j, a, b = comp.runs[rid]
        by_row.setdefault(j, []).append((a, b+1))  # half-open
    for row in by_row.values():
        row.sort()

    def diff(current, below):
        """Intervals in current not covered by the adjacent row."""
        out = []
        k = 0
        for a,b in current:
            cur = a
            while k < len(below) and below[k][1] <= cur:
                k += 1
            idx = k
            while idx < len(below) and below[idx][0] < b:
                c,d = below[idx]
                if c > cur:
                    out.append((cur, min(c,b)))
                cur = max(cur,d)
                if cur >= b:
                    break
                idx += 1
            if cur < b:
                out.append((cur,b))
        return out

    adj = {}
    def edge(a,b):
        adj.setdefault(a,[]).append(b)
    for j in sorted(by_row):
        row = by_row[j]
        for a,b in diff(row,by_row.get(j-1,[])):
            edge((a,j),(b,j))
        for a,b in diff(row,by_row.get(j+1,[])):
            edge((b,j+1),(a,j+1))
        for a,b in row:
            edge((a,j+1),(a,j))
            edge((b,j),(b,j+1))
    if not adj:
        return []
    # Directed boundary edges follow the same interior-on-left convention
    # as contour(). At a self-touch, choose right -> straight -> left.
    start = min(adj, key=lambda p:(p[1],p[0]))
    nxt = min(adj[start], key=lambda p:(0 if p[0]>start[0] else 1,abs(p[0]-start[0])))
    def unit(a,b):
        dx,dy=b[0]-a[0],b[1]-a[1]
        return ((1 if dx>0 else -1) if dx else 0,
                (1 if dy>0 else -1) if dy else 0)
    route=[start]
    prev=unit(start,nxt)
    cur=nxt
    limit=sum(len(v) for v in adj.values())+10
    for _ in range(limit):
        if cur==start:
            break
        route.append(cur)
        outs=adj.get(cur) or []
        if not outs:
            break
        order=[(prev[1],-prev[0]),prev,(-prev[1],prev[0]),(-prev[0],-prev[1])]
        target=min(outs,key=lambda pt: order.index(unit(cur,pt))
                   if unit(cur,pt) in order else 99)
        prev=unit(cur,target)
        cur=target
    # Every piece is axis aligned already. Remove unnecessary intermediate
    # collinear points to prevent expensive point-in-polygon later.
    pts=[]
    for k,pt in enumerate(route):
        if len(route)>2:
            prev_pt=route[k-1]; next_pt=route[(k+1)%len(route)]
            if ((prev_pt[0]==pt[0]==next_pt[0]) or
                (prev_pt[1]==pt[1]==next_pt[1])):
                continue
        pts.append(grid.to_world(*pt))
    return pts

# --------------------------------------------------------------------------- 面域优先
#: 下面四个是**兜底默认值**，正常走 :func:`scale.thresholds` 派生（随字高/自噪声变）。
#: 只有调用方没给 params 时才用到它们（老测试、离线片段）。
FACE_MIN_AREA = 8.0e5          # 面域最小面积：比这小的是墙缝
LABEL_INSIDE_TOL = 1500.0      # 房名压在墙线上仍算"在这间房里"
LABEL_NEAR_TOL = 4500.0        # 房名落在面域外的就近归属上限
SAME_NAME_ADJ = 800.0          # 同名房间相邻合并


def _bbox_of(poly):
    xs = [float(p[0]) for p in poly]
    ys = [float(p[1]) for p in poly]
    return [min(xs), min(ys), max(xs), max(ys)]


def _bbox_gap(a, b):
    dx = max(b[0] - a[2], 0.0, a[0] - b[2])
    dy = max(b[1] - a[3], 0.0, a[1] - b[3])
    return (dx * dx + dy * dy) ** 0.5


def _poly_area(poly):
    return abs(polygon_area(poly)) if len(poly) >= 3 else 0.0


def _face_locator(faces, inside_tol=LABEL_INSIDE_TOL):
    """面域查询器：先按包围盒剪枝，再算点在多边形内 / 到包围盒距离。"""
    prepared = []
    for f in faces or ():
        poly = [[float(p[0]), float(p[1])] for p in (f.get("polygon") or ())]
        if len(poly) < 3:
            continue
        prepared.append({"face": f, "poly": poly, "bbox": f.get("bbox") or _bbox_of(poly)})

    def locate(x, y):
        """-> (face, inside, distance)。inside=True 表示点真的落在轮廓内。"""
        best = None
        for item in prepared:
            b = item["bbox"]
            pad = inside_tol
            if not (b[0] - pad <= x <= b[2] + pad and b[1] - pad <= y <= b[3] + pad):
                d = _bbox_gap([x, y, x, y], b)
                if best is None or d < best[2]:
                    best = (item, False, d)
                continue
            if point_in_polygon(x, y, item["poly"]):
                return item, True, 0.0
            d = _bbox_gap([x, y, x, y], b)
            if best is None or d < best[2]:
                best = (item, False, d)
        if best is None:
            return None, False, float("inf")
        return best

    return locate, prepared


def _clip_halfplane(poly, px, py, nx, ny):
    """Sutherland–Hodgman：保留 ``n·p <= n·a`` 一侧的凸多边形裁剪。"""
    if len(poly) < 3:
        return []
    lim = nx * px + ny * py
    out = []
    n = len(poly)
    for i in range(n):
        a = poly[i]
        b = poly[(i + 1) % n]
        da = nx * float(a[0]) + ny * float(a[1]) - lim
        db = nx * float(b[0]) + ny * float(b[1]) - lim
        if da <= 0:
            out.append([float(a[0]), float(a[1])])
        if (da < 0 < db) or (db < 0 < da):
            t = da / (da - db)
            out.append([float(a[0]) + t * (float(b[0]) - float(a[0])),
                        float(a[1]) + t * (float(b[1]) - float(a[1]))])
    return out


#: "复合房名"的限定词前缀：这些词不是另一个房间，而是同一房间的补充说明
COMPOUND_PREFIX = ("兼", "含", "带", "及", "其中", "局部", "扩大", "预留",
                   "（", "(")
#: 复合判定的距离上限：与最大字高的倍数
COMPOUND_TEXT_FACTOR = 3.0
#: 复合判定的距离上限：与面域对角线的比例
COMPOUND_DIAG_FACTOR = 0.15


def _compound(names_pts, poly) -> bool:
    """判定"一面多名"是**复合房名**还是**两块空间被合并**。

    复合房名（"前室"+"兼消防电梯"、"等候"+"扩大"）不是两个房间，切分会凭空多出
    房间；真正的两块空间（"核医学科"+"侯梯厅"）则必须切开。判据两条，满足任一条
    即视为复合：

    1. 有一方的名字以限定词开头（兼/含/带/及/局部/扩大/预留…）；
    2. 两个房名的文字落点相距不超过 max(3 个字高, 面域对角线的 15%)。
    """
    diag = 0.0
    if poly:
        b = _bbox_of(poly)
        diag = math.hypot(b[2] - b[0], b[3] - b[1])
    for i, (na, xa, ya, ha) in enumerate(names_pts):
        for nb, xb, yb, hb in names_pts[i + 1:]:
            if na.startswith(COMPOUND_PREFIX) or nb.startswith(COMPOUND_PREFIX):
                return True
            if na in nb or nb in na:
                return True
            h = max(float(ha or 0.0), float(hb or 0.0), 1.0)
            lim = max(COMPOUND_TEXT_FACTOR * h, COMPOUND_DIAG_FACTOR * diag)
            if math.hypot(xa - xb, ya - yb) <= lim:
                return True
    return False


#: 面域剖分的栅格分辨率（mm）：比房间轮廓的 10mm 粗，只用来定"哪块归谁"
SPLIT_RES = 100.0
#: 剖分块的最小面积（mm²）：小于它就是噪声，并回邻近那块
SPLIT_MIN_CELL = 1.0e5


def split_face_by_labels(poly, points, res=SPLIT_RES, min_cell=SPLIT_MIN_CELL):
    """一个面域里挂着多个房名 -> 按"到各房名的最近距离"把面域剖成各自的分区。

    为什么不用多边形裁剪：面域轮廓是 20mm 栅格追踪出来的**非凸**多边形，用
    Sutherland–Hodgman 做中垂线裁剪会直接剪成空集（实测 9 个一面多名的面域一个
    都切不开，全部退化成"一面一间"，这就是大图 58 个挂上面域的房名只落成 52 个
    房间的原因）。改成栅格剖分：面域内部每个格归给最近的房名，再对每块描边——
    对任意形状都成立，且各块面积之和等于原面域，不会重复算量。

    返回与 ``points`` 等长的多边形列表；剖分退化（某块太小）时返回 ``None``，
    由调用方整体保留。
    """
    if len(poly) < 3 or len(points) < 2:
        return None
    b = _bbox_of(poly)
    nx = int((b[2] - b[0]) / res) + 2
    ny = int((b[3] - b[1]) / res) + 2
    if nx <= 0 or ny <= 0 or nx * ny > 4_000_000:      # 4M 格 ≈ 40m×40m@100mm
        return None

    owner = [bytearray(ny + 1) for _ in range(nx + 1)]
    inside_cells = []
    for i in range(nx + 1):
        x = b[0] + i * res
        for j in range(ny + 1):
            y = b[1] + j * res
            if not point_in_polygon(x, y, poly):
                continue
            inside_cells.append((i, j))
            best, bd = 0, float("inf")
            for k, (px, py) in enumerate(points):
                d = (x - px) * (x - px) + (y - py) * (y - py)
                if d < bd:
                    bd, best = d, k
            owner[i][j] = best + 1
    if not inside_cells:
        return None

    # 每块描边：把"属于第 k 个房名"的格子当成一个连通域交给现有的轮廓追踪
    cells_by_owner = {}
    for (i, j) in inside_cells:
        cells_by_owner.setdefault(owner[i][j] - 1, set()).add((i, j))

    out = []
    for k in range(len(points)):
        cells = cells_by_owner.get(k) or set()
        area = len(cells) * res * res
        if area < min_cell:
            return None
        grid = _CellGrid(b, res, nx, ny)
        pts, poly_area = room_polygon(cells, grid)
        if len(pts) < 4 or poly_area < min_cell:
            return None
        out.append(pts)
    return out


class _CellGrid:
    """给 :func:`room_polygon` 用的最小栅格接口（只需 to_world）。"""

    def __init__(self, bbox, res, nx, ny):
        self.res, self.x0, self.y0 = res, bbox[0], bbox[1]
        self.nx, self.ny = nx + 1, ny + 1

    def to_world(self, i, j):
        return self.x0 + i * self.res, self.y0 + j * self.res


def rooms_from_faces(faces, labels, *, min_area=FACE_MIN_AREA,
                     near_tol=LABEL_NEAR_TOL,
                     inside_tol=LABEL_INSIDE_TOL,
                     same_name_adj=SAME_NAME_ADJ,
                     split_res=SPLIT_RES,
                     split_min_cell=SPLIT_MIN_CELL) -> dict:
    """**面域优先**的房间识别：房间 = 一个封闭面域 + 挂在上面的房名。

    与旧做法的关键差别：旧做法把房名当泛洪种子，一个连通域只出一个房间——
    同一域里挂着两个房名时必然丢一个（大图上 75 个房名只出了 25 个房间），房名
    压在墙线上时又整个漏掉。这里反过来：

    1. 面域由墙网独立算出（``spaces_seed``），与房名无关；
    2. 每个房名挂到它**所在**的面域；压在墙线上的挂到最近面域并记下偏移；
    3. 多个房名挂到同一面域 -> 一个房间（专名优先，其余记为别名）；
    4. 同一房名跨多个面域（被内部隔断切碎）-> 合并成一个房间；
    5. 挂不上任何面域的房名不丢弃，进 ``orphans`` 由开放分区/修复分支接管。

    返回 ``{"rooms", "orphans", "stats"}``；``orphans`` 里每条都带最近面域距离，
    可以据此判断"是墙网漏检还是真的开放空间"。
    """
    locate, prepared = _face_locator(faces, inside_tol=inside_tol)
    stats = {"faces": len(prepared), "labels": len(labels or ()),
             "inside": 0, "offset": 0, "beyond": 0, "faces_used": 0}

    by_face = {}          # 面域 id -> [label, ...]
    label_face = {}       # 房名 handle -> 面域 id（用于跨面域合并）
    orphans = []
    for lab in labels or ():
        try:
            x, y = float(lab.get("x")), float(lab.get("y"))
        except (TypeError, ValueError):
            continue
        item, inside, dist = locate(x, y)
        if item is None or (not inside and dist > near_tol):
            orphans.append(dict(lab, nearest_distance=None if item is None else round(dist, 1),
                                reason="no_face" if item is None else "too_far"))
            stats["beyond"] += 1
            continue
        fid = (item["face"].get("id") or id(item["face"]))
        stats["inside" if inside else "offset"] += 1
        entry = dict(lab, _inside=inside, _distance=round(dist, 1),
                     _face_bbox=item["bbox"], _face_poly=item["poly"],
                     _face_area=float(item["face"].get("area") or _poly_area(item["poly"])))
        by_face.setdefault(fid, []).append(entry)
        if lab.get("handle"):
            label_face[lab["handle"]] = fid

    # ---- 同一房名跨面域：合并（房间被内部隔断切碎）
    merged_groups = []
    seen = set()
    for fid, items in by_face.items():
        if fid in seen:
            continue
        group = list(items)
        seen.add(fid)
        changed = True
        while changed:
            changed = False
            handles = {g.get("handle") for g in group if g.get("handle")}
            for other, oitems in by_face.items():
                if other in seen:
                    continue
                if any(o.get("handle") in handles for o in oitems):
                    group.extend(oitems)
                    seen.add(other)
                    changed = True
        merged_groups.append(group)

    rooms = []
    for group in merged_groups:
        head = max(group, key=_label_rank)
        # 一面多名 -> 按房名中垂线切分（分隔墙漏检时保住每一个房名）
        distinct = []
        for g in group:
            name = (g.get("name") or "").strip()
            if name and name not in {d[0] for d in distinct}:
                distinct.append((name, float(g.get("x") or 0.0),
                                 float(g.get("y") or 0.0), g.get("h") or g.get("height")))
        split_cells = None
        if len(distinct) >= 2 and not _compound(distinct, head["_face_poly"]):
            split_cells = split_face_by_labels(
                head["_face_poly"], [(d[1], d[2]) for d in distinct],
                res=split_res, min_cell=split_min_cell)
        if split_cells:
            by_name = {}
            for g in group:
                by_name.setdefault((g.get("name") or "").strip(), g)
            for (name, _x, _y, _h), cell in zip(distinct, split_cells):
                g = by_name.get(name) or head
                area_c = _poly_area(cell)
                if area_c < min_area:
                    continue
                rooms.append({
                    "label": {k: g.get(k) for k in
                              ("name", "x", "y", "h", "handle", "layer",
                               "area_label", "area_handle", "area_sup_handle")},
                    "polygon": [list(p) for p in cell],
                    "area": round(area_c, 1), "poly_area": round(area_c, 1),
                    "bbox": [round(v, 1) for v in _bbox_of(cell)],
                    "leak": False, "seed_inside": True,
                    "aliases": [d[0] for d in distinct if d[0] != name],
                    "part_count": 1,
                    "boundary": "split_room",
                    "inferred": True,
                    "label_offset_mm": g.get("_distance"),
                    "faces_merged": 1,
                    "srcs": [{"handle": g.get("handle"), "type": "TEXT",
                              "layer": g.get("layer"), "role": "房间名"}],
                })
            continue
        if len(distinct) >= 2 and not _compound(distinct, head["_face_poly"]):
            # 剖分失败（某一块退化/面域过于破碎）：**不吞掉房名**，让每个房名各自
            # 成一个房间、共用这个面域的轮廓，面积按等分给（并标明是估算）。
            # 这比"一面一间、其余名字变别名"诚实：图上有 4 个房名就是 4 个房间，
            # 缺的是分隔墙，不是房间。
            face_area = _poly_area(head["_face_poly"])
            share = face_area / max(1, len(distinct))
            by_name = {}
            for g in group:
                by_name.setdefault((g.get("name") or "").strip(), g)
            for name, _x, _y, _h in distinct:
                g = by_name.get(name) or head
                rooms.append({
                    "label": {k: g.get(k) for k in
                              ("name", "x", "y", "h", "handle", "layer",
                               "area_label", "area_handle", "area_sup_handle")},
                    "polygon": [list(p) for p in head["_face_poly"]],
                    "area": round(share, 1), "poly_area": round(face_area, 1),
                    "bbox": [round(v, 1) for v in _bbox_of(head["_face_poly"])],
                    "leak": False, "seed_inside": True,
                    "aliases": [],
                    "part_count": len(distinct),
                    "boundary": "shared_face",
                    "area_source": "equal_share",
                    "inferred": True,
                    "label_offset_mm": g.get("_distance"),
                    "faces_merged": 1,
                    "srcs": [{"handle": g.get("handle"), "type": "TEXT",
                              "layer": g.get("layer"), "role": "房间名"}],
                })
            continue
        # 一个面域里挂着专名 + 泛称/邻房名时，其余全部记为别名，不单独出房间
        aliases = []
        for g in group:
            name = (g.get("name") or "").strip()
            if not name or name == (head.get("name") or ""):
                continue
            if name not in aliases:
                aliases.append(name)
        poly = head["_face_poly"]
        area = _poly_area(poly)
        if area < min_area:
            continue
        rooms.append({
            "label": {k: head.get(k) for k in
                      ("name", "x", "y", "h", "handle", "layer",
                       "area_label", "area_handle", "area_sup_handle")},
            "polygon": [list(p) for p in poly],
            "area": round(area, 1), "poly_area": round(area, 1),
            "bbox": [round(v, 1) for v in _bbox_of(poly)],
            "leak": False, "seed_inside": bool(head["_inside"]),
            "aliases": aliases,
            "part_count": len({g.get("handle") for g in group if g.get("handle")}) or 1,
            "boundary": "faces" if head["_inside"] else "nearest_face",
            "label_offset_mm": head["_distance"],
            "faces_merged": len(group),
            "srcs": [{"handle": g.get("handle"), "type": "TEXT", "layer": g.get("layer"),
                      "role": "房间名"} for g in group if g.get("handle")],
        })

    # Keep face provenance through label-splitting and same-name deduplication.
    # A geometrically closed *hypothesis* face cannot by itself confirm a room.
    for room in rooms:
        lab = room.get("label") or {}
        try:
            item, _inside, distance = locate(float(lab["x"]), float(lab["y"]))
        except (TypeError, ValueError, KeyError):
            item, distance = None, float("inf")
        face = (item or {}).get("face") or {}
        if face and distance <= near_tol:
            room["source_face_ids"] = [str(face.get("id"))] if face.get("id") else []
            room["face_id"] = face.get("id")
            seal = face.get("seal_state") or "unknown"
        else:
            room["source_face_ids"] = []
            seal = "unknown"
        room["boundary_state"] = ("hypothesis" if room.get("inferred")
                                   or seal == "hypothesis" else seal)
        if room["boundary_state"] != "observed":
            room["needs_review"] = True

    # ---- 同名相邻房间合并（被门洞/非结构线切开的同一个房间）
    rooms.sort(key=lambda r: -(r.get("area") or 0))
    out = []
    for r in rooms:
        name = r["label"].get("name") or ""
        hit = None
        for k in out:
            if (k["label"].get("name") or "") != name:
                continue
            if _bbox_gap(k["bbox"], r["bbox"]) <= same_name_adj:
                hit = k
                break
        if hit is None:
            out.append(r)
            continue
        hit["area"] = round((hit.get("area") or 0) + (r.get("area") or 0), 1)
        hit["bbox"] = [min(hit["bbox"][0], r["bbox"][0]), min(hit["bbox"][1], r["bbox"][1]),
                       max(hit["bbox"][2], r["bbox"][2]), max(hit["bbox"][3], r["bbox"][3])]
        hit["part_count"] = int(hit.get("part_count") or 1) + int(r.get("part_count") or 1)
        hit["source_face_ids"] = list(dict.fromkeys(
            (hit.get("source_face_ids") or []) + (r.get("source_face_ids") or [])))
        if hit.get("boundary_state") != "observed" or r.get("boundary_state") != "observed":
            hit["boundary_state"] = "hypothesis"
            hit["needs_review"] = True
        hit["faces_merged"] = int(hit.get("faces_merged") or 1) + int(r.get("faces_merged") or 1)

    stats["faces_used"] = len({r["label"].get("handle") for r in out})
    stats["rooms"] = len(out)
    stats["orphans"] = len(orphans)
    stats["same_name_merged"] = len(rooms) - len(out)
    return {"rooms": out, "orphans": orphans, "stats": stats}