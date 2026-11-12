"""几何关系图：把 CAD IR 里的图元连成一张带类型的图。

为什么关系图是这一版的核心
--------------------------
"一扇特殊窗由六条线组成"这句话，用包围盒和线条计数是表达不出来的：单看每条线
都是没语义的短线段，只有**它们之间的关系**（两条平行线是外框、中间三条是分格、
两端短线是连接件、整体贴着一道墙）才构成"窗"。

所以这里把关系显式建出来，并给 AI 一个可以直接查询的入口——模型问"这组子图
周围有什么拓扑"，而不是只拿到"33 条线"这种数字。

关系闭集（不可扩展成任意字符串，新增要走这里）：

================= ====================================================
``parallel_to``    两段夹角 ≤ 容差（默认 2°，来自图内自噪声口径）
``collinear_with`` 平行且垂距 ≤ 容差
``intersects``     两条线段真交叉
``touches``        端点距离 ≤ 容差（画图时的"接上"）
``encloses``       一个闭合图元的包围盒/环路包住另一个图元
``belongs_to_family`` 两个块参照同族（几何指纹相同，不看块名）
``adjacent_to_space`` 图元贴着某个空间面域（洞口/墙与房间的关系）
================= ====================================================

建图预算是硬的（见 :data:`BUDGET`）：先用规则网格把候选对压到邻居格，再做精判，
所以大图不会退化成 O(n²)。
"""
from __future__ import annotations

import math

SCHEMA = "geometry-graph/1.0"

RELATIONS = ("parallel_to", "collinear_with", "intersects", "touches",
             "encloses", "belongs_to_family", "adjacent_to_space")

#: 逻辑预算（条/个）：邻域格大小、单图元最多记多少条边、总边数上限
BUDGET = {"cell_thickness": 2.0, "max_edges_per_uid": 24, "max_edges": 400000,
          "angle_tol_deg": 2.0, "touch_ratio": 1.5, "space_ratio": 1.0}


def _r(v, nd: int = 3) -> float:
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return 0.0


def _seg(e: dict):
    g = e.get("geom") or {}
    if e.get("kind") == "segment":
        return g.get("a"), g.get("b")
    if e.get("kind") == "poly":
        pts = g.get("pts") or ()
        return (pts[0], pts[-1]) if len(pts) >= 2 else (None, None)
    return None, None


def _edges_of(e: dict):
    """图元 -> 线段列表（多段线按边拆、弧按弦近似）。"""
    kind = e.get("kind")
    g = e.get("geom") or {}
    if kind == "segment":
        a, b = g.get("a"), g.get("b")
        if a and b:
            yield (a, b)
        return
    if kind == "poly":
        pts = [p for p in (g.get("pts") or ()) if p]
        for i in range(len(pts) - 1):
            yield (pts[i], pts[i + 1])
        if g.get("closed") and len(pts) >= 3:
            yield (pts[-1], pts[0])
        return
    if kind == "arc":
        center, radius = g.get("center"), float(g.get("radius") or 0.0)
        if not center or radius <= 0:
            return
        a0 = float(g.get("start_deg") or 0.0)
        a1 = float(g.get("end_deg") or 0.0)
        steps = max(6, min(24, int(abs(a1 - a0) / 15.0) + 6))
        prev = None
        for i in range(steps + 1):
            ang = math.radians(a0 + (a1 - a0) * i / steps)
            pt = [float(center[0]) + radius * math.cos(ang),
                  float(center[1]) + radius * math.sin(ang)]
            if prev is not None:
                yield (prev, pt)
            prev = pt


def _len(a, b) -> float:
    return math.hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1]))


def _angle(a, b) -> float:
    return math.degrees(math.atan2(float(b[1]) - float(a[1]),
                                   float(b[0]) - float(a[0]))) % 180.0


def _angle_diff(x: float, y: float) -> float:
    d = abs(x - y) % 180.0
    return min(d, 180.0 - d)


def _point_seg_dist(p, a, b) -> float:
    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 <= 0:
        return math.hypot(float(p[0]) - ax, float(p[1]) - ay)
    t = max(0.0, min(1.0, ((float(p[0]) - ax) * dx + (float(p[1]) - ay) * dy) / L2))
    return math.hypot(float(p[0]) - (ax + t * dx), float(p[1]) - (ay + t * dy))


def _cross(o, a, b) -> float:
    return ((float(a[0]) - float(o[0])) * (float(b[1]) - float(o[1]))
            - (float(a[1]) - float(o[1])) * (float(b[0]) - float(o[0])))


def _segments_cross(a, b, c, d) -> bool:
    d1 = _cross(c, d, a)
    d2 = _cross(c, d, b)
    d3 = _cross(a, b, c)
    d4 = _cross(a, b, d)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return True
    return False


def _bbox_gap(a, b) -> float:
    if not a or not b:
        return float("inf")
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def _inside(pt, box, pad: float = 0.0) -> bool:
    if not pt or not box:
        return False
    return (box[0] - pad <= float(pt[0]) <= box[2] + pad
            and box[1] - pad <= float(pt[1]) <= box[3] + pad)


# --------------------------------------------------------------------- 网格
class _Grid:
    __slots__ = ("cell", "buckets")

    def __init__(self, entities, *, cell: float):
        self.cell = max(1.0, float(cell))
        self.buckets: dict = {}
        for e in entities:
            box = e.get("bbox")
            if not box:
                continue
            for key in self._keys(box):
                self.buckets.setdefault(key, []).append(e["uid"])

    def _keys(self, box):
        c = self.cell
        x0, y0 = int(math.floor(box[0] / c)), int(math.floor(box[1] / c))
        x1, y1 = int(math.floor(box[2] / c)), int(math.floor(box[3] / c))
        return [(i, j) for i in range(x0, x1 + 1) for j in range(y0, y1 + 1)]

    def near(self, box, pad: float = 0.0) -> set:
        """包围盒（可先按 pad 外扩）覆盖到的胞里的全部 uid。

        注意：多胞图元必须**外扩后**再查，否则"隔着半个胞的邻居"永远查不到——
        平行线对正是这种情形（两条线各占一行胞）。
        """
        if not box:
            return set()
        if pad:
            box = [box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad]
        out: set = set()
        for key in self._keys(box):
            out.update(self.buckets.get(key, ()))
        return out


# --------------------------------------------------------------------- 建图
def build(ir: dict, *, faces=(), tol: float | None = None) -> dict:
    """IR -> 关系图（``{"edges": [...], "by_uid": {...}, "stats": {...}}``）。

    ``faces`` 是空间面域（可选）：给了才有 ``adjacent_to_space`` 关系。
    容差默认取"图内自噪声 × 1.5"，与别的模块共用同一口径。
    """
    entities = [e for e in (ir.get("entities") or ()) if e.get("bbox")]
    basis = ir.get("basis") or {}
    noise = float(basis.get("noise") or 2.0)
    thick = float(basis.get("thickness") or 200.0)
    tol = float(tol if tol is not None else noise * BUDGET["touch_ratio"])
    angle_tol = float(BUDGET["angle_tol_deg"])

    grid = _Grid(entities, cell=max(1.0, float(BUDGET["cell_thickness"]) * thick))
    by_uid = {e["uid"]: e for e in entities}
    edges: list[dict] = []
    seen: set = set()
    counts: dict[str, int] = {}

    def _add(a: str, b: str, rel: str, w: float, detail: dict | None = None):
        if a == b or rel not in RELATIONS:
            return
        key = (a, b, rel) if a < b else (b, a, rel)
        if key in seen:
            return
        if counts.get(a, 0) >= int(BUDGET["max_edges_per_uid"]):
            return
        if counts.get(b, 0) >= int(BUDGET["max_edges_per_uid"]):
            return
        if len(edges) >= int(BUDGET["max_edges"]):
            return
        seen.add(key)
        counts[a] = counts.get(a, 0) + 1
        counts[b] = counts.get(b, 0) + 1
        edges.append({"a": key[0], "b": key[1], "rel": rel, "w": _r(w),
                      "detail": detail or {}})

    # 同族（块参照）：几何指纹相同即同族，与块名无关
    fam_of: dict[str, str] = {}
    for fam in ir.get("families") or ():
        for uid in fam.get("uids") or ():
            fam_of[uid] = fam["hash"]
    fam_index: dict[str, list] = {}
    for uid, fh in fam_of.items():
        fam_index.setdefault(fh, []).append(uid)
    for fh, uids in fam_index.items():
        for i, a in enumerate(uids[:24]):
            for b in uids[i + 1:24]:
                _add(a, b, "belongs_to_family", 1.0, {"family": fh})

    # 线类图元两两比较（网格压邻居）
    lines = [e for e in entities if e["kind"] in ("segment", "poly", "arc")]
    band = 3.0 * thick
    for e in lines:
        box = e.get("bbox")
        for other in grid.near(box, pad=band):
            if other == e["uid"]:
                continue
            f = by_uid.get(other)
            if f is None or f["kind"] not in ("segment", "poly", "arc"):
                continue
            if e["uid"] > other:                    # 每对只算一次
                continue
            gap = _bbox_gap(box, f.get("bbox"))
            # 平行/共线是"两条线隔着一段距离"的关系，包围盒天生就有间隙：
            # 所以按角度先分档——平行的那一档允许多倍墙厚的垂距，其余只认贴合。
            angle_close = (e.get("angle") is not None and f.get("angle") is not None
                           and _angle_diff(float(e["angle"]),
                                           float(f["angle"])) <= angle_tol)
            if gap > (band if angle_close else tol):
                continue
            if angle_close:
                diff = _angle_diff(float(e["angle"]), float(f["angle"]))
                a_seg, b_seg = _seg(e), _seg(f)
                if diff <= angle_tol and a_seg[0] and b_seg[0]:
                    dist = (_point_seg_dist(b_seg[0], a_seg[0], a_seg[1])
                            + _point_seg_dist(b_seg[1], a_seg[0], a_seg[1])) / 2.0
                    w = 1.0 - min(1.0, diff / max(1e-6, angle_tol))
                    if dist <= tol:
                        _add(e["uid"], f["uid"], "collinear_with", max(w, 0.6),
                             {"dist": _r(dist)})
                    else:
                        _add(e["uid"], f["uid"], "parallel_to", w,
                             {"angle_diff": _r(diff), "dist": _r(dist)})
            # 端点相接 / 交叉
            for a, b in list(_edges_of(e))[:24]:
                for c, d in list(_edges_of(f))[:24]:
                    if (_len(a, c) <= tol or _len(a, d) <= tol
                            or _len(b, c) <= tol or _len(b, d) <= tol):
                        _add(e["uid"], f["uid"], "touches", 1.0)
                        break
                    if _segments_cross(a, b, c, d):
                        _add(e["uid"], f["uid"], "intersects", 1.0)
                        break

    # encloses：闭合图元包住别的图元
    closed = [e for e in entities if (e["kind"] == "poly" and
                                      (e.get("geom") or {}).get("closed"))
              or e["kind"] == "hatch"]
    for outer in closed:
        box = outer.get("bbox")
        if not box:
            continue
        for uid in grid.near(box):
            if uid == outer["uid"]:
                continue
            inner = by_uid.get(uid)
            if inner is None or not inner.get("bbox"):
                continue
            if _inside(inner["bbox"][:2], box) and _inside(inner["bbox"][2:], box):
                _add(outer["uid"], uid, "encloses", 1.0)

    # adjacent_to_space：贴着一个空间面域
    for face in faces or ():
        box = face.get("bbox") if isinstance(face, dict) else None
        if not box:
            continue
        pad = tol
        for uid in grid.near([box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad]):
            item = by_uid.get(uid)
            if item is None or not item.get("bbox"):
                continue
            if _bbox_gap(item["bbox"], box) <= pad:
                _add(uid, str(face.get("id") or "face"), "adjacent_to_space", 1.0,
                     {"face": face.get("id")})

    by_uid_edges: dict[str, list] = {}
    for edge in edges:
        by_uid_edges.setdefault(edge["a"], []).append(
            {"other": edge["b"], "rel": edge["rel"], "w": edge["w"],
             "detail": edge["detail"]})
        by_uid_edges.setdefault(edge["b"], []).append(
            {"other": edge["a"], "rel": edge["rel"], "w": edge["w"],
             "detail": edge["detail"]})
    return {"schema": SCHEMA, "edges": edges, "by_uid": by_uid_edges,
            "stats": {"entities": len(entities), "edges": len(edges),
                      "by_rel": {r: sum(1 for e in edges if e["rel"] == r)
                                 for r in RELATIONS},
                      "tol": _r(tol), "angle_tol_deg": angle_tol}}


# --------------------------------------------------------------------- 查询
class Graph:
    """关系图的查询门面（只读）。"""

    __slots__ = ("data", "ir")

    def __init__(self, data: dict, ir: dict | None = None):
        self.data = data or {}
        self.ir = ir or {}

    def neighbors(self, uid: str, rel: str | None = None) -> list[dict]:
        rows = list((self.data.get("by_uid") or {}).get(str(uid), ()))
        if rel:
            rows = [row for row in rows if row["rel"] == rel]
        return rows

    def related(self, uid: str, rel: str) -> list[str]:
        return [row["other"] for row in self.neighbors(uid, rel)]

    def subgraph(self, uids, *, depth: int = 1) -> dict:
        """给定一组 uid，取出它和它们的邻域（AI 看拓扑用的子图）。"""
        keep = [str(u) for u in uids or ()]
        for _ in range(max(0, int(depth))):
            extra = []
            for uid in keep:
                for row in self.neighbors(uid):
                    if row["other"] not in keep and row["other"] not in extra:
                        extra.append(row["other"])
            if not extra:
                break
            keep.extend(extra)
        keep_set = set(keep)
        edges = [e for e in (self.data.get("edges") or ())
                 if e["a"] in keep_set and e["b"] in keep_set]
        return {"uids": keep, "edges": edges,
                "stats": {"nodes": len(keep), "edges": len(edges)}}

    def describe(self, uid: str, *, limit: int = 6) -> dict:
        """一个图元的拓扑速写（结构事实，不含判定）。"""
        item = (self.ir.get("entities") or ())
        me = next((e for e in item if e["uid"] == uid), None)
        rows = self.neighbors(uid)
        by_rel: dict[str, list] = {}
        for row in rows:
            by_rel.setdefault(row["rel"], []).append(row["other"])
        return {"uid": uid, "kind": (me or {}).get("kind"),
                "layer": (me or {}).get("layer"),
                "angle": (me or {}).get("angle"),
                "length": (me or {}).get("length"),
                "bbox": (me or {}).get("bbox"),
                "relations": {k: v[:limit] for k, v in by_rel.items()},
                "degree": len(rows)}

    def degree(self, uid: str) -> int:
        return len(self.neighbors(uid))


def query(graph, uid: str, *, depth: int = 1, limit: int = 6) -> dict:
    """AI 只读工具 ``inspect_neighbors`` 的实现：一个图元 + 它的子图。"""
    g = graph if isinstance(graph, Graph) else Graph(graph)
    loo = g.describe(uid, limit=limit)
    sub = g.subgraph([uid], depth=depth)
    loo["subgraph"] = {"nodes": sub["stats"]["nodes"], "edges": sub["stats"]["edges"]}
    loo["edges"] = sub["edges"][:limit * 4]
    return loo


def summary(graph) -> dict:
    data = graph.data if isinstance(graph, Graph) else (graph or {})
    return {"schema": SCHEMA, "stats": data.get("stats") or {}}
