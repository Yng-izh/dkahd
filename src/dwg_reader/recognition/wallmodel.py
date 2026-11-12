"""墙体模型：WallSpan（跨洞口连续）/ WallSolid（实体）/ Gap（缺口证据）。

改造要点（相对只看 H/V 的旧版）：

* 墙带与墙线一律用 ``(dir, nrm, pos, lo, hi)`` 表达，H / V 只是 ``dir`` 取轴向
  的特例，因此斜墙、旋转图纸不需要单独一套逻辑；
* 墙厚允许未知（``thickness is None``）：缺双线证据时不再硬塞一个默认 200mm，
  而是把 ``thickness_source`` 标成 ``unknown``，缺口容差退化为"按长度比例"。

``pos`` 是法向坐标、``lo``/``hi`` 是沿墙向坐标，两者都由点积定义，所以
``nearest_line`` / ``locate`` 这些查询对任意方向写法一致。
"""
from __future__ import annotations

import math

from . import geometry as GEO
from . import linegroups as LG

#: 墙厚未知时，缺口容差的兜底（mm）
NOMINAL_GAP = 1_200.0
#: 角度分箱（度）：同箱才可能是同一墙线
ANGLE_BUCKET = 0.5


def _as_support(item):
    if item.get("dir") and item.get("nrm"):
        return item
    sup = GEO.as_support(item["start"], item["end"],
                         layer=item.get("layer"), handle=item.get("handle"))
    if sup is None:
        return None
    sup["thickness"] = item.get("thickness")
    sup["srcs"] = item.get("srcs", [])
    sup["orig"] = item
    return sup


def _key(sup):
    return (round(sup["angle"] / ANGLE_BUCKET), round(sup["pos"], 1))


def build(walls_seg, thick=None, max_gap_factor=6.0):
    """墙段 -> 墙带/墙实体/缺口。

    ``thick`` 可缺省；``walls_seg`` 的每项可以是带 ``dir/nrm`` 的支撑，也可以是
    旧格式的 ``{"orient","start","end","thickness"}`` 墙段，两者等价。
    """
    groups: dict[tuple, list] = {}
    # Use a common geometric basis per observed support, never raw floating
    # world offsets or exact direction tuples as an identity key.
    for n, family in enumerate(LG.grouped(walls_seg, angle_tol=0.3, pos_tol=2.5)):
        groups[(n,)] = [_as_support(item) for item in family]
    groups = {key: [s for s in items if s is not None]
              for key, items in groups.items()}

    nominal = float(thick) if thick else None
    spans, solids, gaps = [], [], []
    for key, items in sorted(groups.items()):
        items.sort(key=lambda s: s["lo"])
        max_gap = max_gap_factor * (nominal or NOMINAL_GAP)
        cur = None
        for sup in items:
            solids.append({"orient": sup["orient"], "pos": sup["pos"],
                           "dir": sup["dir"], "nrm": sup["nrm"], "angle": sup["angle"],
                           "lo": sup["lo"], "hi": sup["hi"],
                           "thickness": sup.get("thickness"),
                           "srcs": sup.get("srcs", [])})
            if cur is None:
                cur = {"orient": sup["orient"], "angle": sup["angle"],
                       "dir": sup["dir"], "nrm": sup["nrm"], "pos": sup["pos"],
                       "lo": sup["lo"], "hi": sup["hi"],
                       "thickness": sup.get("thickness"),
                       "solids": [[sup["lo"], sup["hi"]]], "gaps": [],
                       "members": 1}
                spans.append(cur)
                continue
            gap = sup["lo"] - cur["hi"]
            if 0 <= gap <= max_gap:
                if gap > 0:
                    cur["gaps"].append([round(cur["hi"], 1), round(sup["lo"], 1)])
                    gaps.append({"orient": sup["orient"], "pos": sup["pos"],
                                 "dir": sup["dir"], "lo": cur["hi"], "hi": sup["lo"],
                                 "width": gap})
                cur["hi"] = max(cur["hi"], sup["hi"])
                cur["solids"].append([sup["lo"], sup["hi"]])
                cur["members"] += 1
                if cur.get("thickness") is None:
                    cur["thickness"] = sup.get("thickness")
            else:
                cur = {"orient": sup["orient"], "angle": sup["angle"],
                       "dir": sup["dir"], "nrm": sup["nrm"], "pos": sup["pos"],
                       "lo": sup["lo"], "hi": sup["hi"],
                       "thickness": sup.get("thickness"),
                       "solids": [[sup["lo"], sup["hi"]]], "gaps": [],
                       "members": 1}
                spans.append(cur)

    thick_vals = [s["thickness"] for s in spans if s.get("thickness")]
    thickness = sorted(thick_vals)[len(thick_vals) // 2] if thick_vals else None
    return {"spans": spans, "solids": solids, "gaps": gaps,
            "thickness": thickness,
            "thickness_source": "span" if thickness else "unknown",
            "max_gap": max_gap_factor * (nominal or NOMINAL_GAP)}


# --------------------------------------------------------------------------- 查询
def _local(line, x, y):
    """世界点 -> (沿墙坐标 u, 法向偏移 v)。"""
    dirv, nrmv = line.get("dir"), line.get("nrm")
    if dirv and nrmv:
        u = dirv[0] * x + dirv[1] * y
        v = nrmv[0] * x + nrmv[1] * y - line["pos"]
        return u, v
    if line["orient"] == "H":
        return x, y - line["pos"]
    return y, x - line["pos"]


def local(line, x, y):
    """公开接口：世界点 -> (沿墙坐标 u, 法向偏移 v)。"""
    return _local(line, x, y)


def locate(spans, orient, pos, lo, hi, tol):
    for s in spans:
        if s["orient"] != orient or abs(s["pos"] - pos) > tol:
            continue
        if s["lo"] - tol <= lo and hi <= s["hi"] + tol:
            return s
    return None


def nearest_span(spans, x, y, tol):
    """点最近的墙带：沿墙范围覆盖该点（±tol），横向偏移最小。"""
    best = None
    for s in spans:
        u, v = _local(s, x, y)
        if not (s["lo"] - tol <= u <= s["hi"] + tol):
            continue
        if abs(v) <= tol and (best is None or abs(v) < best[1]):
            best = (s, abs(v))
    return best[0] if best else None


def nearest_line_pos(spans, orient, coord, tol):
    best = None
    for s in spans:
        if s["orient"] != orient:
            continue
        d = abs(s["pos"] - coord)
        if d <= tol and (best is None or d < best[1]):
            best = (s["pos"], d)
    return best[0] if best else None


def lines(spans):
    """Return co-linear wall lines with intact member span intervals."""
    prepared = []
    for s in spans:
        if not s.get('dir') or not s.get('nrm'):
            continue
        a, b = GEO.point_at(s, s['lo']), GEO.point_at(s, s['hi'])
        prepared.append(dict(s, start=a, end=b))
    out = []
    for family in LG.grouped(prepared, angle_tol=0.3, pos_tol=2.5):
        if not family:
            continue
        ref = family[0]
        out.append({'orient': ref['orient'], 'angle': ref['angle'],
                    'dir': ref['dir'], 'nrm': ref['nrm'], 'pos': ref['pos'],
                    'lo': min(s['lo'] for s in family),
                    'hi': max(s['hi'] for s in family),
                    'thickness': next((s.get('thickness') for s in family
                                       if s.get('thickness')), None),
                    'spans': family})
    return out


def nearest_line(ls, x, y, tol):
    """点所属的墙线：沿墙方向落在该线延伸范围内（缺口也算），横向偏移最小。"""
    best = None
    for L in ls:
        u, v = _local(L, x, y)
        if abs(v) > tol:
            continue
        if not (L["lo"] - tol <= u <= L["hi"] + tol):
            continue
        if best is None or abs(v) < best[1]:
            best = (L, abs(v))
    return best[0] if best else None


def line_endpoints(line, shrink=0.0):
    """墙线 -> 两端世界坐标（斜墙也能正确还原）。"""
    return (GEO.point_at(line, line["lo"] + shrink),
            GEO.point_at(line, line["hi"] - shrink))


# --------------------------------------------------------------------------- 墙网
#: 端头合并为同一节点的容差（mm）
NODE_TOL = 300.0
#: 判为"断口"的距离上限系数（x 名义墙厚/缺口长度）
GAP_FACTOR = 6.0


def network(walls_seg, tol=NODE_TOL, columns=()) -> dict:
    """墙网图与拓扑指标：连通分量 / 孤立墙 / 自由端 / 断口。

    这是"拓扑验证"的公共口径：墙体候选的端头是否接进已有的墙网、整张图的墙
    分裂成几个连通片、有多少自由端其实是两个几乎重合的断口。修复验收也用同一
    套指标比较修复前后（避免出现"数量变了但拓扑没变/更碎"的自证式收益）。
    """
    walls = [w for w in walls_seg or ()
             if isinstance(w, dict) and w.get("start") and w.get("end")]
    ids = [str(w.get("id") or f"wall{i}") for i, w in enumerate(walls)]
    ends = []
    for w in walls:
        ends.append(([float(w["start"][0]), float(w["start"][1])],
                     [float(w["end"][0]), float(w["end"][1])]))
    # 端点 -> 节点（容差内合并）
    parent: list[int] = []

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    points: list[tuple[float, float]] = []
    for s, e in ends:
        points.extend([(s[0], s[1]), (e[0], e[1])])
    parent = list(range(len(points)))
    cell: dict[tuple[int, int], list[int]] = {}
    for i, (x, y) in enumerate(points):
        cell.setdefault((int(x // tol), int(y // tol)), []).append(i)
    for (cx, cy), members in cell.items():
        near = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                near.extend(cell.get((cx + dx, cy + dy), ()))
        for i in members:
            for j in near:
                if i < j and math.hypot(points[i][0] - points[j][0],
                                        points[i][1] - points[j][1]) <= tol:
                    union(i, j)
    # 墙面上的 T 接：端点落在另一堵墙身上也算接上（只影响 degree/连通判定）
    wall_of = {}
    for wi, (s, e) in enumerate(ends):
        wall_of[2 * wi] = wi
        wall_of[2 * wi + 1] = wi
    tj = 0
    t_points: set[int] = set()
    for i, (x, y) in enumerate(points):
        owner = wall_of.get(i)
        for wi, (s, e) in enumerate(ends):
            if wi == owner:
                continue
            dx, dy = e[0] - s[0], e[1] - s[1]
            den = dx * dx + dy * dy
            if den <= 1e-9:
                continue
            t = max(0.0, min(1.0, ((x - s[0]) * dx + (y - s[1]) * dy) / den))
            if math.hypot(x - (s[0] + t * dx), y - (s[1] + t * dy)) <= tol:
                union(i, 2 * wi)
                t_points.add(i)
                tj += 1
                break
    comps: dict[int, list[int]] = {}
    for wi in range(len(walls)):
        root = find(2 * wi)
        comps.setdefault(root, []).append(wi)
    node_members: dict[int, list[int]] = {}
    for i in range(len(points)):
        node_members.setdefault(find(i), []).append(i)
    degree = {}
    for wi in range(len(walls)):
        deg = 0
        for endpoint in (2 * wi, 2 * wi + 1):
            others = [j for j in node_members.get(find(endpoint), ())
                      if wall_of.get(j) != wi]
            if others or endpoint in t_points:
                deg += 1
        degree[ids[wi]] = deg
    isolated = [ids[wi] for wi in range(len(walls))
                if len(comps.get(find(2 * wi), ())) == 1 and degree[ids[wi]] == 0]
    # 断口：两个自由端之间距离 <= GAP_FACTOR * 名义缺口
    free = [i for i in range(len(points))]
    gaps = []
    nominal = 0.0
    for w in walls:
        nominal = max(nominal, float(w.get("thickness") or 0.0))
    limit = GAP_FACTOR * (nominal or NOMINAL_GAP)
    for i in range(len(free)):
        for j in range(i + 1, len(free)):
            if wall_of.get(i) == wall_of.get(j):
                continue
            d = math.hypot(points[i][0] - points[j][0], points[i][1] - points[j][1])
            if 0 < d <= limit:
                gaps.append(round(d, 1))
    total_len = sum(math.hypot(e[0] - s[0], e[1] - s[1]) for s, e in ends)
    biggest = max((len(v) for v in comps.values()), default=0)
    return {"walls": len(walls), "nodes": len({find(i) for i in range(len(points))}),
            "components": len(comps), "largest_component": biggest,
            "isolated": len(isolated), "isolated_ids": isolated[:20],
            "degree": degree, "t_junctions": tj,
            "free_ends": sum(2 - degree.get(i, 0) for i in ids) if ids else 0,
            "gap_pairs": len(gaps), "min_gap": min(gaps) if gaps else None,
            "length": round(total_len, 1)}
