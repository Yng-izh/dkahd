"""基础工具：DXF 加载、图元索引、文字合并、几何聚类。

本模块只做"原生 CAD 图元"层面的处理，不涉及任何图像/模型识别。
"""
from __future__ import annotations

import math
import re

import ezdxf

CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


# ---------------------------------------------------------------- 加载
def load(path):
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    return doc, msp


def entity_index(msp):
    """handle -> (entity, layer, type)"""
    idx = {}
    for e in msp:
        idx[e.dxf.handle] = e
    return idx


def src(e, role=""):
    """来源图元描述"""
    d = {"handle": e.dxf.handle, "type": e.dxftype(), "layer": e.dxf.layer}
    if role:
        d["role"] = role
    return d


#: **纯标注类图层**：尺寸、轴号编号、填充、文字说明。这些层上的线不是构件轮廓，
#: 但长得一模一样——尺寸线是平行双线（像墙）、界线是短垂线（像墙端头）、填充线
#: 是等距平行线（像踏步/窗线束）。它们只能靠图层名挡在图元池外面。
NOISE_LAYER_HINTS = ("DIM", "标注", "尺寸", "DEFPOINTS", "HATCH", "填充", "剖面线",
                     "AXIS_TEXT", "图例", "LEGEND", "TK_标注")


def is_noise_layer(name) -> bool:
    """图层名是不是"纯标注/填充"层。

    只在**没有声明图层角色**的兜底路径上使用（那时墙/窗候选会对全图开放）。
    一旦图纸声明了墙层，就按声明走，不做这层过滤——声明过的层即使名字里带
    "DIM" 也以声明为准。
    """
    n = str(name or "").upper()
    return any(h.upper() in n for h in NOISE_LAYER_HINTS)


# ---------------------------------------------------------------- 文字
def _clean_mtext(s):
    s = s.replace("\\P", " / ")
    s = re.sub(r"\\[A-Za-z][^\\;{}]*;", "", s)
    s = re.sub(r"\\[A-Za-z]", "", s)
    s = s.replace("{", "").replace("}", "")
    return s.strip()


def text_records(msp):
    """提取 TEXT/MTEXT 为统一记录。"""
    out = []
    for e in msp.query("TEXT MTEXT"):
        t = e.dxftype()
        raw = e.dxf.text if t == "TEXT" else e.text
        s = raw if t == "TEXT" else _clean_mtext(raw)
        ins = e.dxf.insert
        try:
            rot = e.dxf.rotation
        except Exception:
            rot = 0.0
        out.append({
            "handle": e.dxf.handle, "type": t, "layer": e.dxf.layer,
            "text": s, "raw": raw, "x": float(ins.x), "y": float(ins.y),
            "h": float(e.dxf.height) if t == "TEXT" else float(e.dxf.char_height),
            "rot": float(rot) % 360.0,
        })
    return out


def _char_w(ch):
    return 1.0 if CJK.match(ch) else 0.55


def text_width(s, h):
    return sum(_char_w(c) for c in s) * h


def merge_text_fragments(recs, y_tol=0.35, gap_tol=0.9, h_tol=0.15, max_rot=2.0):
    """把同一行被拆成多段的文字合并（如 'C1828(' + '消' + ')'）。

    判据：同图层、字高相近、旋转一致、基线 y 相近、水平间距小于 gap_tol*字高。
    """
    groups = {}
    for r in recs:
        key = (r["layer"], round(r["h"], 1), round(r["rot"] / 5.0))
        groups.setdefault(key, []).append(r)

    merged = []
    for key, items in groups.items():
        items.sort(key=lambda r: (r["x"], r["y"]))
        used = [False] * len(items)
        for i, a in enumerate(items):
            if used[i]:
                continue
            used[i] = True
            cur = dict(a)
            cur["parts"] = [a["handle"]]
            cur["x2"] = a["x"] + text_width(a["text"], a["h"])
            changed = True
            while changed:
                changed = False
                for j, b in enumerate(items):
                    if used[j]:
                        continue
                    if abs(b["y"] - cur["y"]) > y_tol * max(a["h"], 1e-6):
                        continue
                    if abs(b["h"] - cur["h"]) > h_tol * max(cur["h"], 1e-6):
                        continue
                    gap = b["x"] - cur["x2"]
                    if -0.2 * cur["h"] <= gap <= gap_tol * cur["h"]:
                        cur["text"] += b["text"]
                        cur["x2"] = b["x"] + text_width(b["text"], b["h"])
                        cur["parts"].append(b["handle"])
                        used[j] = True
                        changed = True
            cur["cx"] = (cur["x"] + cur["x2"]) / 2.0
            merged.append(cur)
    merged.sort(key=lambda r: (-r["y"], r["x"]))
    return merged


#: 拉开字距的"散字词组"：中文制图里常把"病 人 廊"排成一个大间距词组强调，
#: 每个字是独立 TEXT，与相邻字的空地比正常字距大 1.2~4 个字高。
SPACED_MIN_GAP = 1.2      # × 字高：小于它属于正常字距，第一遍就合并了
SPACED_MAX_GAP = 4.0      # × 字高：大于它基本是两个不同词组
SPACED_MAX_LEN = 6        # 合并后最长字数（超过就不是一个词）


def _is_single_cjk(s: str) -> bool:
    return len(s) == 1 and bool(CJK.match(s))


def _rot_close(a, b, tol=2.0) -> bool:
    d = abs(float(a) - float(b)) % 360.0
    return d <= tol or 360.0 - d <= tol


def merge_spaced_words(recs, *, y_tol=0.35, h_tol=0.15):
    """把"拉开字距的单字"拼回词组（横排），以及旋转 90° 的竖排单字拼接。

    与 :func:`merge_text_fragments` 的分工：那一遍处理**正常字距**被拆开的句子
    （gap <= 0.9 字高）；这一遍处理**有意拉开字距**的词组（gap 1.2~4 字高），以及
    旋转 90° 文字的字间堆叠。两遍都只做"同一行/同一列、同字高、同图层"的拼接，
    不改坐标、不猜内容。

    只拼**单字**是刻意的：多字记录已经是完整词，"病"、"人"、"廊" 才是被拆散的
    散字；这条约束同时挡住了把两个相邻房间名误并成一个词。
    """
    if not recs:
        return []
    groups = {}
    for r in recs:
        h = max(float(r.get("h") or 0.0), 1e-6)
        groups.setdefault((r.get("layer"), round(h, 1),
                           round(float(r.get("rot") or 0) / 5.0)), []).append(r)

    out = []
    for items in groups.values():
        rows = []
        for idx, r in enumerate(items):
            d = dict(r)
            d["_i"] = idx
            d["_w"] = text_width(d.get("text") or "", d.get("h") or 0.0)
            d["_x2"] = float(d.get("x") or 0.0) + d["_w"]
            rows.append(d)
        used = set()

        def _emit(cur, **extra):
            cur.pop("_i", None)
            cur.pop("_w", None)
            cur.pop("_x2", None)
            cur.update(extra)
            out.append(cur)

        def _peek(cands, pred):
            """取沿排字方向**最近**的候选（不是最近的坐标，是最近的空隙）。"""
            best, best_gap = None, None
            for b in cands:
                gap = pred(b)
                if gap is None:
                    continue
                if best_gap is None or gap < best_gap:
                    best, best_gap = b, gap
            return best, best_gap

        rows.sort(key=lambda r: (float(r.get("y") or 0.0), float(r.get("x") or 0.0)))

        # ---- 横排：同一基线，向右拉开字距
        for a in rows:
            if a["_i"] in used or not _is_single_cjk(a.get("text") or ""):
                continue
            rot = float(a.get("rot") or 0.0)
            if not (_rot_close(rot, 0.0) or _rot_close(rot, 180.0)):
                continue
            used.add(a["_i"])
            cur = dict(a)
            cur["parts"] = list(a.get("parts") or [a.get("handle")])
            spaced = False
            while len(cur["text"]) < SPACED_MAX_LEN:
                hh = max(float(cur.get("h") or 0.0), 1e-6)
                base_y = float(a.get("y") or 0.0)

                def _gap_h(b, hh=hh, base_y=base_y, cur=cur, rot=rot):
                    if b["_i"] in used or not _is_single_cjk(b.get("text") or ""):
                        return None
                    if not _rot_close(b.get("rot"), rot):
                        return None
                    if abs(float(b.get("y") or 0.0) - base_y) > y_tol * hh:
                        return None
                    if abs(float(b.get("h") or 0.0) - hh) > h_tol * hh:
                        return None
                    gap = float(b.get("x") or 0.0) - cur["_x2"]
                    if not (SPACED_MIN_GAP * hh <= gap <= SPACED_MAX_GAP * hh):
                        return None
                    return gap

                nxt, _g = _peek(rows, _gap_h)
                if nxt is None:
                    break
                spaced = True
                cur["text"] += nxt["text"]
                cur["_x2"] = nxt["_x2"]
                cur["parts"].append(nxt.get("handle"))
                used.add(nxt["_i"])
            if spaced:
                _emit(cur, cx=(float(cur.get("x") or 0.0) + cur["_x2"]) / 2.0,
                      x2=cur["_x2"], spaced_word=True)
            else:
                _emit(cur)

        # ---- 竖排：旋转 90° 的文字，同一列向上堆叠
        verticals = [r for r in rows if _rot_close(r.get("rot"), 90.0)
                     and _is_single_cjk(r.get("text") or "")]
        verticals.sort(key=lambda r: (round(float(r.get("x") or 0.0), 1),
                                      float(r.get("y") or 0.0)))
        for a in verticals:
            if a["_i"] in used:
                continue
            used.add(a["_i"])
            cur = dict(a)
            cur["parts"] = list(a.get("parts") or [a.get("handle")])
            ytop = float(a.get("y") or 0.0) + cur["_w"]
            got = False
            while len(cur["text"]) < SPACED_MAX_LEN:
                hh = max(float(cur.get("h") or 0.0), 1e-6)
                base_x = float(a.get("x") or 0.0)

                def _gap_v(b, hh=hh, base_x=base_x, ytop=ytop):
                    if b["_i"] in used or not _is_single_cjk(b.get("text") or ""):
                        return None
                    if not _rot_close(b.get("rot"), 90.0):
                        return None
                    if abs(float(b.get("x") or 0.0) - base_x) > y_tol * hh:
                        return None
                    gap = float(b.get("y") or 0.0) - ytop
                    if not (SPACED_MIN_GAP * hh <= gap <= SPACED_MAX_GAP * hh):
                        return None
                    return gap

                nxt, _g = _peek(verticals, _gap_v)
                if nxt is None:
                    break
                got = True
                cur["text"] += nxt["text"]
                ytop = float(nxt.get("y") or 0.0) + text_width(nxt["text"], nxt.get("h") or 0.0)
                cur["parts"].append(nxt.get("handle"))
                used.add(nxt["_i"])
            if got:
                _emit(cur, cx=float(cur.get("x") or 0.0), x2=cur["_x2"],
                      spaced_word=True, vertical=True)
            else:
                _emit(cur)

        for r in rows:
            if r["_i"] not in used:
                used.add(r["_i"])
                _emit(dict(r))

    out.sort(key=lambda r: (-float(r.get("y") or 0.0), float(r.get("x") or 0.0)))
    return out


# ---------------------------------------------------------------- 几何
def norm_seg(x1, y1, x2, y2):
    if (x1, y1) > (x2, y2):
        return x2, y2, x1, y1
    return x1, y1, x2, y2


def segments_from(e):
    """把 LINE / LWPOLYLINE 拆成线段（含 handle 与 layer）。

    ``e`` 可以是 ezdxf 实体，也可以是 ``snapshot.py`` 产出的纯 Python 记录：
    识别节点只读快照，因此这里同时支持 dict 形态，避免快照节点再碰 ezdxf。
    """
    out = []
    if isinstance(e, dict):
        t = e.get("type")
        if t == "LINE":
            s, d = e["start"], e["end"]
            out.append((s[0], s[1], d[0], d[1]))
        elif t == "LWPOLYLINE":
            pts = [(p[0], p[1]) for p in e.get("points", ())]
            n = len(pts)
            for i in range(n - 1):
                out.append((*pts[i], *pts[i + 1]))
            if e.get("closed") and n > 2:
                out.append((*pts[-1], *pts[0]))
        return out
    t = e.dxftype()
    if t == "LINE":
        s, d = e.dxf.start, e.dxf.end
        out.append((s.x, s.y, d.x, d.y))
    elif t == "LWPOLYLINE":
        pts = [(p[0], p[1]) for p in e.get_points()]
        n = len(pts)
        for i in range(n - 1):
            out.append((*pts[i], *pts[i + 1]))
        if e.closed and n > 2:
            out.append((*pts[-1], *pts[0]))
    return out


def seg_len(x1, y1, x2, y2):
    return math.hypot(x2 - x1, y2 - y1)


def cluster_1d(vals, tol):
    """把一维值聚类，返回 [(中心值, [成员])]，按值升序。"""
    if not vals:
        return []
    vs = sorted(vals)
    groups = [[vs[0]]]
    for v in vs[1:]:
        if v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [(sum(g) / len(g), g) for g in groups]


def merge_intervals(ivs, tol=1.5):
    """合并重叠/相邻区间 [(a,b)]，返回 [(a,b)]。"""
    if not ivs:
        return []
    ivs = sorted(ivs)
    out = [list(ivs[0])]
    for a, b in ivs[1:]:
        if a <= out[-1][1] + tol:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def point_seg_dist(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.hypot(px - x1, py - y1), 0.0
    t = ((px - x1) * dx + (py - y1) * dy) / L2
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy)), t


def polygon_area(pts):
    a = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def bbox_of_points(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return [min(xs), min(ys), max(xs), max(ys)]


def rdp(points, eps):
    """Douglas-Peucker 折线简化（闭合环先按最远点剖分）。"""
    if len(points) < 3:
        return list(points)
    pts = list(points)
    if pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) < 3:
        return pts

    # 以最远点对为种子，避免闭合环被压扁
    i0 = 0
    dmax, i1 = -1.0, 0
    for i in range(len(pts)):
        d = math.hypot(pts[i][0] - pts[i0][0], pts[i][1] - pts[i0][1])
        if d > dmax:
            dmax, i1 = d, i
    ring = pts[i0:] + pts[:i0]
    k = ring.index(pts[i1]) if pts[i1] in ring else 0
    a = _rdp_open(ring[:k + 1], eps)
    b = _rdp_open(ring[k:] + [ring[0]], eps)
    return a[:-1] + b[:-1]


def _rdp_open(pts, eps):
    if len(pts) < 3:
        return list(pts)
    x1, y1 = pts[0]
    x2, y2 = pts[-1]
    dmax, idx = -1.0, 0
    for i in range(1, len(pts) - 1):
        d, _ = point_seg_dist(pts[i][0], pts[i][1], x1, y1, x2, y2)
        if d > dmax:
            dmax, idx = d, i
    if dmax > eps:
        left = _rdp_open(pts[:idx + 1], eps)
        right = _rdp_open(pts[idx:], eps)
        return left[:-1] + right
    return [pts[0], pts[-1]]


def ortho_snap(pts, tol=45.0):
    """把近似水平/竖直的边吸附成正交边，返回正交化后的顶点表。"""
    n = len(pts)
    if n < 3:
        return pts
    out = []
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        dx, dy = x2 - x1, y2 - y1
        if abs(dy) < tol:
            out.append(("h", (x1 + x2) / 2.0))
        elif abs(dx) < tol:
            out.append(("v", (y1 + y2) / 2.0))
        else:
            out.append(("d", (x1, y1, x2, y2)))
    # 用相邻边的交点重建顶点
    res = []
    for i in range(n):
        kind, val = out[i]
        pk, pv = out[i - 1]
        if kind == "h" and pk == "v":
            res.append((pv, val))
        elif kind == "v" and pk == "h":
            res.append((val, pv))
        elif kind == "h" and pk == "h":
            res.append((pts[i][0], val))
        elif kind == "v" and pk == "v":
            res.append((val, pts[i][1]))
        else:
            res.append(pts[i])
    # 去掉重复/共线点
    ded = []
    for p in res:
        if not ded or math.hypot(p[0] - ded[-1][0], p[1] - ded[-1][1]) > 1.0:
            ded.append(p)
    return ded
