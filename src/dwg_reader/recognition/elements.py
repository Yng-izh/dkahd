"""构件识别器：图框 / 轴网 / 指北针 / 柱 / 门 / 窗 / 楼梯 / 台阶 / 散水。

全部基于原生图元（图层名、块名、块属性、文字、几何与拓扑），不含图像识别。
"""
from __future__ import annotations

import math
import re

from .base import bbox_of_points, point_seg_dist, polygon_area, segments_from, seg_len
from .snapshot import geometry_records
from . import sizing as SIZING


def _geo(snapshot):
    """全部几何记录：模型空间图元 + **块展开后的几何**。

    这是"块炸不炸开都认得出"的关键：只看 ``entities`` 时，一张把柱/墙画在块里的
    图，块一炸开识别就归零；只看 ``derived`` 时，块参照本身的语义（块名、属性）又
    丢了。两者都喂进去，同一构件会拿到两条独立证据，由融合阶段去重打分。
    """
    return geometry_records(snapshot or {})


def _v2(v):
    return [float(v[0]), float(v[1])]


def _rect_from_insert(rec, blocks=None):
    """块参照的四个角点（考虑旋转/镜像/缩放）；``rec`` 为快照 INSERT 记录。

    ``blocks`` 给出块定义自身尺寸时按"块尺寸 × 插入缩放"换算：柱块通常画成
    **单位方块 + 插入缩放 400**（天正 ``M_S0BKA``），也有画成真实尺寸 + 缩放 1 的。
    只认 ``xscale`` 会把后者算成 1mm，只认块尺寸会漏掉前者，两者相乘才对。
    """
    ins = rec["insert"]
    sx, sy = rec["xscale"], rec["yscale"]
    ext = ((blocks or {}).get(rec.get("name")) or {}).get("extent")
    if ext and len(ext) == 2:
        if ext[0] and abs(ext[0]) > 1e-6:
            sx = sx * abs(ext[0])
        if ext[1] and abs(ext[1]) > 1e-6:
            sy = sy * abs(ext[1])
    rot = math.radians(rec["rotation"])
    pts = []
    for (lx, ly) in ((-0.5, -0.5), (0.5, -0.5), (0.5, 0.5), (-0.5, 0.5)):
        x, y = lx * sx, ly * sy
        pts.append([ins[0] + x * math.cos(rot) - y * math.sin(rot),
                    ins[1] + x * math.sin(rot) + y * math.cos(rot)])
    return pts


# ------------------------------------------------------------------ 图框
def recognize_frame(snapshot, texts, frame_layer="PUB_TITLE"):
    polys = []
    for e in snapshot["entities"]:
        if e.get("type") != "LWPOLYLINE" or e.get("layer") != frame_layer:
            continue
        pts = [[float(p[0]), float(p[1])] for p in e.get("points", ())]
        polys.append({"pts": pts, "closed": e.get("closed"), "handle": e["handle"]})
    if not polys:
        return None, []
    polys.sort(key=lambda p: -polygon_area(p["pts"]))
    outer, inner = polys[0], polys[-1]
    ob = bbox_of_points(outer["pts"])
    ib = bbox_of_points(inner["pts"])

    # 图签区：内框右侧、图名/图号文字所在竖带
    tb_texts = [t for t in texts if t["x"] > ib[2] - 0.12 * (ib[2] - ib[0]) and t["y"] < ib[1] + 0.3 * (ib[3] - ib[1])]
    tb_texts.sort(key=lambda t: -t["y"])
    name = number = project = None
    for t in tb_texts:
        s = t["text"].strip()
        if re.search(r"平面图|立面图|剖面图|详图|总平面", s):
            name = name or s
        elif re.match(r"^[A-Z0-9][A-Z0-9\-\.]{4,}$", s):
            number = number or s
        elif re.search(r"[\u4e00-\u9fff].*[-—]", s) and project is None:
            project = s
    # 图名优先取图签区，其次取图面下方标题
    if not name:
        for t in texts:
            if re.search(r"平面图$", t["text"].strip()):
                name = t["text"].strip()
                break
    frame = {
        "outer": ob, "inner": ib,
        "width": ob[2] - ob[0], "height": ob[3] - ob[1],
        "title_block": {"x0": ib[2] - 7000, "x1": ib[2], "y0": ib[1], "y1": ib[3]},
        "drawing_name": name, "drawing_number": number, "project": project,
        "polys": polys,
        "srcs": [{"handle": p["handle"], "type": "LWPOLYLINE", "layer": "PUB_TITLE"} for p in polys],
        "title_texts": [{"handle": t["handle"], "text": t["text"], "layer": t["layer"]} for t in tb_texts],
    }
    return frame, tb_texts


# ------------------------------------------------------------------ 轴网
def recognize_grid(snapshot, layers=None):
    entities = snapshot["entities"]
    # 轴号气泡：_AXISO 块 + 属性
    axis_layers = set(layers or ("DOTE", "AXIS"))
    bubbles = []
    for e in entities:
        if e.get("type") != "INSERT" or e.get("name") != "_AXISO":
            continue
        lab = (e.get("attribs") or {}).get("A")
        lab = lab.strip() if isinstance(lab, str) else None
        bubbles.append({"label": lab, "x": e["insert"][0], "y": e["insert"][1],
                        "r": abs(e["xscale"]) * 0.5, "handle": e["handle"]})
    # 轴线：DOTE 图层通长轴线
    axis_lines = []
    for e in entities:
        if e.get("type") != "LINE" or e.get("layer") not in axis_layers:
            continue
        x1, y1 = e["start"]
        x2, y2 = e["end"]
        L = seg_len(x1, y1, x2, y2)
        if L < 3000:
            continue
        axis_lines.append({"orient": "V" if abs(x2 - x1) < 1 else "H",
                           "pos": (x1 + x2) / 2 if abs(x2 - x1) < 1 else (y1 + y2) / 2,
                           "lo": min(x2, x1) if abs(y2 - y1) < 1 else min(y1, y2),
                           "hi": max(x2, x1) if abs(y2 - y1) < 1 else max(y1, y2),
                           "handle": e["handle"], "layer": e["layer"]})

    vs = {}
    hs = {}
    for b in bubbles:
        if b["label"] is None:
            continue
        # 气泡在轴线端点外：竖直轴线看 x，水平轴线看 y
        vs.setdefault(round(b["x"], 1), []).append(b)
        hs.setdefault(round(b["y"], 1), []).append(b)

    # 用气泡成对出现确定轴线位置（同一条轴线两端各一个气泡）
    vaxes, haxes = [], []
    for x, bs in vs.items():
        if len(bs) >= 2 and len({b["label"] for b in bs}) == 1:
            vaxes.append({"label": bs[0]["label"], "pos": x,
                          "bubbles": [b["handle"] for b in bs],
                          "span": [min(b["y"] for b in bs), max(b["y"] for b in bs)]})
    for y, bs in hs.items():
        if len(bs) >= 2 and len({b["label"] for b in bs}) == 1:
            haxes.append({"label": bs[0]["label"], "pos": y,
                          "bubbles": [b["handle"] for b in bs],
                          "span": [min(b["x"] for b in bs), max(b["x"] for b in bs)]})
    vaxes.sort(key=lambda a: a["pos"])
    haxes.sort(key=lambda a: a["pos"])

    # 与 DOTE 通长轴线匹配
    for a in vaxes:
        m = [l for l in axis_lines if l["orient"] == "V" and abs(l["pos"] - a["pos"]) < 5]
        a["line_handles"] = [l["handle"] for l in m]
        a["extent"] = [m[0]["lo"], m[0]["hi"]] if m else a["span"]
    for a in haxes:
        m = [l for l in axis_lines if l["orient"] == "H" and abs(l["pos"] - a["pos"]) < 5]
        a["line_handles"] = [l["handle"] for l in m]
        a["extent"] = [m[0]["lo"], m[0]["hi"]] if m else a["span"]

    # 轴距
    vsp = [round(vaxes[i + 1]["pos"] - vaxes[i]["pos"], 1) for i in range(len(vaxes) - 1)]
    hsp = [round(haxes[i + 1]["pos"] - haxes[i]["pos"], 1) for i in range(len(haxes) - 1)]

    inter = []
    for a in vaxes:
        for b in haxes:
            inter.append({"x": a["pos"], "y": b["pos"], "v": a["label"], "h": b["label"]})

    return {"vaxes": vaxes, "haxes": haxes, "v_spans": vsp, "h_spans": hsp,
            "intersections": inter, "bubbles": bubbles, "axis_lines": axis_lines}


# ------------------------------------------------------------------ 指北针
def recognize_north(snapshot, texts, layers=None):
    """指北针：圆 + 过圆心指针线。``layers`` 由 plan 给出，不写死图层名。"""
    wanted = set(layers or ("DIM_SYMB", "0", "PUB_TEXT"))
    entities = list(_geo(snapshot))
    circles = [e for e in entities if e.get("type") == "CIRCLE"]
    best = None
    for c in circles:
        # 找圆内/附近的指针线（DIM_SYMB 图层的开放多段线，穿过圆心）
        cx, cy, r = c["center"][0], c["center"][1], c["radius"]
        for e in entities:
            if e.get("type") != "LWPOLYLINE":
                continue
            if e.get("layer") not in wanted:
                continue
            pts = [(p[0], p[1]) for p in e.get("points", ())]
            if len(pts) != 2:
                continue
            (x1, y1), (x2, y2) = pts
            if abs(math.hypot(x2 - x1, y2 - y1) - 2 * r) < 0.35 * r and \
               point_seg_dist(cx, cy, x1, y1, x2, y2)[0] < 0.2 * r:
                best = (c, (x1, y1), (x2, y2))
    if not best:
        return None
    c, p1, p2 = best
    cx, cy = c["center"][0], c["center"][1]
    # 指针指向 "北" 字一侧
    north_txt = [t for t in texts if t["text"].strip() in ("北", "N")]
    if north_txt:
        nt = min(north_txt, key=lambda t: math.hypot(t["x"] - cx, t["y"] - cy))
        tip = p1 if math.hypot(p1[0] - nt["x"], p1[1] - nt["y"]) < math.hypot(p2[0] - nt["x"], p2[1] - nt["y"]) else p2
        label = nt["text"]
    else:
        tip = p2 if p2[1] > p1[1] else p1
        label = None
    dx, dy = tip[0] - cx, tip[1] - cy
    az = (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0
    return {"center": [cx, cy], "radius": c["radius"],
            "needle": [_v2(p1), _v2(p2)], "north_tip": _v2(tip),
            "azimuth_deg": round(az, 2),
            # 相对图纸正上方的偏角：正 = 顺时针偏东，负 = 逆时针偏西
            "rotation_from_sheet_up_deg": round(az if az <= 180 else az - 360.0, 2),
            "label": label,
            "srcs": [{"handle": c["handle"], "type": "CIRCLE", "layer": c["layer"]}]}


# ------------------------------------------------------------------ 柱
def _rects_from_lines(snapshot, layers, *, short_min=150.0, short_max=2000.0,
                      long_max=2000.0, aspect_max=12.0, edge_tol=120.0, scale=None):
    """块被炸开时的柱：指定图层上的线段首尾相接成（轴对齐）矩形。

    允许**缺边**：只画出三条边、第四条共线在柱面上（贴墙柱、构造柱最常见）也
    成立——判据是"两条平行边 + 端点处有垂直边封口"，不要求四条边齐全。缺边的
    候选打 ``edges=3`` 并降权，交给 :func:`fuse_columns` 用墙体/轴网连接关系补证。

    尺寸判据全部来自图内尺度（``scale``），不再用绝对毫米数：短边 ≥
    ``short_min``（约 0.75 墙厚）、长边 ≤ ``long_max``、长短比 ≤ ``aspect_max``。
    """
    if scale:
        from . import scale as _SC
        th = _SC.thresholds(scale)
        short_min = th["col_short_min"]
        short_max = th["col_short_max"]
        long_max = th["col_long_max"]
        aspect_max = PIER_ASPECT
        # 提取阶段就放到"长条形"的下界：贴墙垛的短边只有一墙厚，若在这里用
        # 方柱下界砍掉，fuse 阶段再放宽也救不回来。
        short_min = min(short_min, th.get("pier_short_min") or short_min)

    hs, vs = [], []
    for e in _geo(snapshot):
        if e.get("type") != "LINE" or e.get("layer") not in layers:
            continue
        (x1, y1), (x2, y2) = e["start"], e["end"]
        if abs(y1 - y2) <= 1.5 and abs(x2 - x1) > 50:
            hs.append((min(x1, x2), max(x1, x2), y1, e["handle"]))
        elif abs(x1 - x2) <= 1.5 and abs(y2 - y1) > 50:
            vs.append((min(y1, y2), max(y1, y2), x1, e["handle"]))

    out, seen = [], set()

    def _emit(x0, x1, ylo, yhi, handles, edges):
        w, h = x1 - x0, yhi - ylo
        short, long_ = min(w, h), max(w, h)
        if not (short_min <= short <= short_max and long_ <= long_max):
            return
        if short <= 0 or long_ / short > aspect_max:
            return
        key = (round(x0 / 10), round(ylo / 10), round(x1 / 10), round(yhi / 10))
        if key in seen:
            return
        seen.add(key)
        out.append({"outline": [[x0, ylo], [x1, ylo], [x1, yhi], [x0, yhi]],
                    "bbox": [x0, ylo, x1, yhi], "w": w, "h": h,
                    "rotation": 0.0, "center": [(x0 + x1) / 2.0, (ylo + yhi) / 2.0],
                    "handle": handles[0], "edges": edges,
                    "srcs": [{"handle": hd, "type": "LINE"} for hd in handles if hd]})

    # ---- 两条平行边 + 端部封口（水平边给上下界，竖直边给左右界）
    for i in range(len(hs)):
        ax0, ax1, y0, h0 = hs[i]
        for j in range(i + 1, len(hs)):
            bx0, bx1, y1, h1 = hs[j]
            if abs(ax0 - bx0) > 5 or abs(ax1 - bx1) > 5 or abs(y0 - y1) < 50:
                continue
            ylo, yhi = min(y0, y1), max(y0, y1)
            x0, x1 = min(ax0, bx0), max(ax1, bx1)
            left = [v for v in vs if abs(v[2] - x0) <= 5 and v[0] <= ylo + 5 and v[1] >= yhi - 5]
            right = [v for v in vs if abs(v[2] - x1) <= 5 and v[0] <= ylo + 5 and v[1] >= yhi - 5]
            if left and right:
                _emit(x0, x1, ylo, yhi, [h0, h1, left[0][3], right[0][3]], 4)
            elif left or right:
                _emit(x0, x1, ylo, yhi, [h0, h1, (left or right)[0][3]], 3)

    # ---- 竖向为主：两条竖直边 + 端部横向封口（高窄柱、构造柱）
    for i in range(len(vs)):
        ay0, ay1, x0, v0 = vs[i]
        for j in range(i + 1, len(vs)):
            by0, by1, x1, v1 = vs[j]
            if abs(ay0 - by0) > 5 or abs(ay1 - by1) > 5 or abs(x0 - x1) < 50:
                continue
            xlo, xhi = min(x0, x1), max(x0, x1)
            ylo, yhi = min(ay0, by0), max(ay1, by1)
            bot = [h for h in hs if abs(h[2] - ylo) <= 5 and h[0] <= xlo + 5 and h[1] >= xhi - 5]
            top = [h for h in hs if abs(h[2] - yhi) <= 5 and h[0] <= xlo + 5 and h[1] >= xhi - 5]
            if bot and top:
                _emit(xlo, xhi, ylo, yhi, [v0, v1, bot[0][3], top[0][3]], 4)
            elif bot or top:
                _emit(xlo, xhi, ylo, yhi, [v0, v1, (bot or top)[0][3]], 3)

    return out


def _rotated_rects_from_lines(snapshot, layers, *, scale=None):
    """Identify fully closed four-edge rectangular columns at any rotation.

    Uses endpoint adjacency (O(n * local_degree^3)) rather than pairwise global
    line comparisons. Unlike three-edge axial-column inference, oblique
    candidates require all four real edges to avoid furniture false positives.
    """
    if scale:
        from . import scale as _SC
        limits = _SC.thresholds(scale)
        short_min = min(limits["col_short_min"], limits.get("pier_short_min") or 150)
        long_max = limits["col_long_max"]
    else:
        short_min, long_max = 150.0, 2000.0
    vertices, segments = {}, []
    for rec in _geo(snapshot):
        if rec.get("type") != "LINE" or rec.get("layer") not in layers:
            continue
        a, b = rec.get("start"), rec.get("end")
        if not a or not b:
            continue
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if not (0.8 * short_min <= length <= 1.1 * long_max):
            continue
        # This recognizer handles only truly oblique rectangles.
        if abs(dx) <= 1.5 or abs(dy) <= 1.5:
            continue
        pa = (round(a[0] / 3), round(a[1] / 3))
        pb = (round(b[0] / 3), round(b[1] / 3))
        if pa == pb:
            continue
        index = len(segments)
        segments.append((pa, pb, rec, length))
        vertices.setdefault(pa, []).append(index)
        vertices.setdefault(pb, []).append(index)
    results, seen = [], set()
    for i, (a, b, rec, length) in enumerate(segments):
        # Traverse four-edge cycles, limiting adjacency to CAD junctions of
        # reasonable degree. Dense hatches and furniture are not rectangles.
        if len(vertices.get(a, [])) > 6 or len(vertices.get(b, [])) > 6:
            continue
        for j in vertices.get(b, []):
            if j == i:
                continue
            c = segments[j][1] if segments[j][0] == b else segments[j][0]
            if c == a or len(vertices.get(c, [])) > 6:
                continue
            for k in vertices.get(c, []):
                if k in (i, j):
                    continue
                d = segments[k][1] if segments[k][0] == c else segments[k][0]
                if d in (a, b, c):
                    continue
                for l in vertices.get(d, []):
                    if l in (i, j, k):
                        continue
                    other = segments[l][1] if segments[l][0] == d else segments[l][0]
                    if other != a:
                        continue
                    ids = frozenset((i, j, k, l))
                    if ids in seen:
                        continue
                    seen.add(ids)
                    pts = [a, b, c, d]
                    vectors = [(pts[(q + 1) % 4][0] - pts[q][0],
                                pts[(q + 1) % 4][1] - pts[q][1]) for q in range(4)]
                    lengths = [3.0 * math.hypot(*v) for v in vectors]
                    if not (short_min <= min(lengths) and max(lengths) <= long_max
                            and max(lengths) / min(lengths) <= PIER_ASPECT):
                        continue
                    if any(abs(vectors[q][0] * vectors[(q + 1) % 4][0]
                               + vectors[q][1] * vectors[(q + 1) % 4][1])
                           > 0.05 * lengths[q] * lengths[(q + 1) % 4]
                           for q in range(4)):
                        continue
                    xy = [[float(x * 3), float(y * 3)] for x, y in pts]
                    bbox = bbox_of_points(xy)
                    handles = [segments[t][2].get("handle") for t in ids]
                    results.append({"outline": xy, "bbox": bbox,
                                    "w": min(lengths), "h": max(lengths),
                                    "rotation": math.degrees(math.atan2(vectors[0][1], vectors[0][0])) % 180,
                                    "center": [sum(q[0] for q in xy) / 4, sum(q[1] for q in xy) / 4],
                                    "handle": handles[0], "edges": 4,
                                    "srcs": [{"handle": h, "type": "LINE"} for h in handles if h]})
    return results


def _rects_from_lwpolyline(snapshot, layers):
    """Closed rectangular column polylines in their *local*, oriented frame.

    Screening the world-axis-aligned bbox is not rotation invariant.  A 200x350
    pier can turn into an apparently square 403x348 bbox after a 30 degree
    rotation, and a rotated 650x650 column can have a false 888mm 'section'.
    Preserve actual CAD vertices and judge real edge lengths/perpendicularity.
    """
    out = []
    for e in _geo(snapshot):
        if e.get("type") != "LWPOLYLINE" or not e.get("closed"):
            continue
        if e.get("layer") not in layers:
            continue
        pts = [[float(p[0]), float(p[1])] for p in e.get("points", ())]
        if len(pts) == 5 and math.dist(pts[0], pts[-1]) < 2.0:
            pts.pop()
        if len(pts) != 4:
            continue
        edges = [[pts[(j + 1) % 4][0] - pts[j][0],
                  pts[(j + 1) % 4][1] - pts[j][1]] for j in range(4)]
        sizes = [math.hypot(*v) for v in edges]
        if not (150 <= min(sizes) <= max(sizes) <= 2000):
            continue
        # Same section-shape policy independent of drawing orientation.  The
        # stricter near-square criterion is this detector's existing policy;
        # elongated structural piers remain other candidates for verification.
        if abs(sizes[0] - sizes[1]) > 0.35 * max(sizes[0], sizes[1]):
            continue
        if abs(sizes[0] - sizes[2]) > max(5.0, 0.02 * sizes[0]):
            continue
        if abs(sizes[1] - sizes[3]) > max(5.0, 0.02 * sizes[1]):
            continue
        if any(abs(edges[j][0] * edges[(j + 1) % 4][0]
                   + edges[j][1] * edges[(j + 1) % 4][1])
               > 0.03 * sizes[j] * sizes[(j + 1) % 4]
               for j in range(4)):
            continue
        b = bbox_of_points(pts)
        out.append({"outline": pts, "bbox": b, "w": sizes[0], "h": sizes[1],
                    "rotation": math.degrees(math.atan2(edges[0][1], edges[0][0])) % 180,
                    "center": [sum(p[0] for p in pts) / 4,
                               sum(p[1] for p in pts) / 4],
                    "handle": e["handle"],
                    "srcs": [{"handle": e["handle"], "type": "LWPOLYLINE"}]})
    return out


def _section_size(rec, blocks=None):
    """块参照的**真实截面**（宽,高），已乘缩放、且不受旋转影响。

    旋转 45° 的 400×400 柱，外接框是 565×565；拿外接框当截面会把旋转柱判成
    超尺寸、或者不同旋转的同一族柱判成"尺寸不一致"。
    """
    ext = ((blocks or {}).get(rec.get("name")) or {}).get("extent")
    sx = abs(float(rec.get("xscale") or 0.0))
    sy = abs(float(rec.get("yscale") or 0.0))
    if ext and len(ext) == 2:
        if ext[0] and abs(ext[0]) > 1e-6:
            sx *= abs(ext[0])
        if ext[1] and abs(ext[1]) > 1e-6:
            sy *= abs(ext[1])
    return sx, sy


def _solid_quad(poly) -> bool:
    """填充轮廓是不是一个"实心四边形"（柱截面）；三角形多是箭头/符号。"""
    if not poly or len(poly) < 3:
        return False
    uniq = {(round(float(p[0]), 3), round(float(p[1]), 3)) for p in poly}
    return len(uniq) >= 4


def _footprint_blocks(snapshot, scale=None) -> dict:
    """族表里"几何指纹判成柱"的块名 -> {依据, 实例数, 截面}。

    具体判据（实心四边形 / 无弧 / 无编号属性 / 尺寸像柱 / 重复出现）在
    :mod:`blockfamily` 里，这里只做一层薄适配：柱识别只需要"哪些块是柱"。
    """
    from . import blockfamily as BLOCKF
    out = {}
    for name, rec in BLOCKF.table(snapshot, scale=scale).items():
        if rec.get("role") != "column":
            continue
        if not str(rec.get("why") or "").startswith("几何指纹"):
            continue
        out[name] = {"why": rec["why"], "n": rec.get("n"),
                     "size": (rec.get("sizes") or [[0, 0]])[0]}
    return out


def recognize_columns(snapshot, layers=None, *, scale=None, block_names=None):
    """柱候选：**多画法全部生成**，不因为"已经找到块"就停掉其它画法。

    办公楼那种 38 个 ``_FZH`` 块参照的图，块识别最省事；但同一套系统在只看
    块的时候会在大图上直接失效（大型测试图的柱层是 444 条 LINE）。所以四种
    画法（块 / 线段组合 / 闭合多段线 / 闭合轮廓或填充——后两者由算子层产出）
    并行生成，由 :func:`fuse_columns` 统一去重与打分。
    """
    out = []
    # 块名只是溯源标签，不是准入条件：名单外的块只要长相判成柱也进来
    # （``M_S0BKA`` 这类结构柱块、图库里没见过的柱块）。
    names = {"_FZH"} | {str(n) for n in (block_names or ())}
    footprint = _footprint_blocks(snapshot, scale=scale)
    names |= set(footprint)
    blocks = snapshot.get("blocks") or {}
    for e in snapshot["entities"]:
        if e.get("type") != "INSERT" or e.get("name") not in names:
            continue
        pts = _rect_from_insert(e, blocks)
        bb = bbox_of_points(pts)
        sec_w, sec_h = _section_size(e, blocks)
        out.append({"outline": pts, "bbox": bb,
                    "w": sec_w, "h": sec_h,
                    "rotation": e["rotation"],
                    "center": [e["insert"][0], e["insert"][1]],
                    "handle": e["handle"], "method": "block",
                    "layer": e.get("layer"),
                    "named": bool(e.get("name") == "_FZH"),
                    "footprint": (footprint.get(e.get("name")) or {}).get("why"),
                    "srcs": [{"handle": e["handle"], "type": "INSERT",
                              "layer": e.get("layer"), "role": "柱块参照"}]})
    if layers:
        for r in _rects_from_lines(snapshot, set(layers), scale=scale):
            r["method"] = "lines"
            r["layer"] = (r.get("srcs") or [{}])[0].get("layer")
            out.append(r)
        for r in _rotated_rects_from_lines(snapshot, set(layers), scale=scale):
            r["method"] = "lines"
            r["layer"] = (r.get("srcs") or [{}])[0].get("layer")
            out.append(r)
        for r in _rects_from_lwpolyline(snapshot, set(layers)):
            r["method"] = "polyline"
            r["layer"] = (r.get("srcs") or [{}])[0].get("layer")
            out.append(r)
    return out


# ------------------------------------------------------------------ 柱多画法融合
#: 柱截面合理范围（mm）：统一的柱判据第一条
COL_MIN_MM = 150.0
COL_MAX_MM = 2_500.0
#: 截面长短边之比上限（方柱 / 矩形柱）
COL_ASPECT = 3.0
#: "长条形"上限：贴墙垛 / 构造柱保留为柱类构件，但标 elongated，不按方柱计
PIER_ASPECT = 12.0
#: 同位置去重：中心距离容差 = max(该值, 0.5 * 短边)
COL_DEDUPE_MM = 150.0
#: 与墙体 / 轴网连接判定的容差（mm）
COL_TOUCH_MM = 250.0
#: 柱尺寸判据相对"图内尺度"的倍率（见 recognition/scale.py）
SHORT_MIN_RATIO = 0.75      # 短边 ≥ 0.75 墙厚
LONG_MAX_RATIO = 4.0        # 长边 ≤ 4 门宽
#: 各画法的证据权重（块最硬，轮廓/填充次之，线段组合最软）
COL_METHOD_W = {"block": 0.50, "hatch": 0.30, "contour": 0.30,
                "polyline": 0.28, "lines": 0.24}
#: 闭合边界（轮廓/填充/多段线/线段组合）落在 profile 声明的柱图层上，本身就是一条
#: 独立证据——它和"块参照"不是同一个画法，不能因为不是块就当没看见
COL_BOUNDARY_W = 0.12
COL_CONFIRM = 0.70
COL_REVIEW = 0.40


def _col_size(rec):
    b = rec.get("bbox") or [0, 0, 0, 0]
    w = float(rec.get("w") or (b[2] - b[0]) or 0.0)
    h = float(rec.get("h") or (b[3] - b[1]) or 0.0)
    return abs(w), abs(h)


def _col_center(rec):
    c = rec.get("center")
    if c:
        return float(c[0]), float(c[1])
    b = rec.get("bbox") or [0, 0, 0, 0]
    return (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0


def axis_coords(snapshot, axis_layers):
    """轴网坐标：轴层上长直线的 x / y 位置（用于"柱在轴网交点上"这条判据）。"""
    xs, ys = set(), set()
    wanted = set(axis_layers or ())
    if not wanted:
        return xs, ys
    for e in (snapshot or {}).get("entities") or ():
        if e.get("type") != "LINE" or e.get("layer") not in wanted:
            continue
        (x1, y1), (x2, y2) = e["start"], e["end"]
        if abs(y1 - y2) <= 1.5 and abs(x2 - x1) > 3000:
            ys.add(round(y1, 1))            # 水平轴：给出 y 坐标
        elif abs(x1 - x2) <= 1.5 and abs(y2 - y1) > 3000:
            xs.add(round(x1, 1))            # 竖直轴：给出 x 坐标
    return xs, ys


def _pt_seg(px, py, x1, y1, x2, y2) -> float:
    """点到线段距离（柱-墙连接判定用）。"""
    dx, dy = x2 - x1, y2 - y1
    den = dx * dx + dy * dy
    if den <= 1e-9:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / den))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _near(values, v, tol) -> bool:
    return any(abs(float(u) - v) <= tol for u in values or ())


def fuse_columns(cands, *, walls=(), axis=(), tol_touch=COL_TOUCH_MM,
                 scale=None) -> dict:
    """柱候选多画法融合：统一判据 + 同位置去重 + 打分（防过检）。

    判据（缺一条就降级，不直接删）：
    1) 截面尺寸在 150~2000mm 且长短边比 <= 2（长条形归墙，不归柱）；
    2) 至少一条独立来源（块 / 轮廓 / 填充 / 线段组合），来源数越多越可信；
    3) 同位置不同画法互为佐证（合并成一个柱，不重复计数）；
    4) 与墙体或轴网有连接关系时加分——孤立、尺寸离群的轮廓只标"待复核"。
    """
    xs, ys = axis or ((), ())
    th = {}
    if scale:
        from . import scale as _SC
        th = _SC.thresholds(scale)
    short_min = float(th.get("col_short_min") or COL_MIN_MM)
    short_max = float(th.get("col_short_max") or COL_MAX_MM)
    pier_min = float(th.get("pier_short_min") or short_min)
    long_max = float(th.get("col_long_max") or COL_MAX_MM)
    dedupe = float(th.get("col_dedupe") or COL_DEDUPE_MM)
    pool = []
    for rec in cands or ():
        r = dict(rec)
        w, h = _col_size(r)
        r["w"], r["h"] = w, h
        r["methods"] = [r.get("method") or "contour"]
        r.setdefault("srcs", [])
        pool.append(r)

    merged = []
    for rec in pool:
        cx, cy = _col_center(rec)
        short = min(_col_size(rec))
        limit = max(dedupe, 0.5 * short)
        hit = None
        for m in merged:
            mx, my = _col_center(m)
            if abs(mx - cx) <= limit and abs(my - cy) <= limit:
                hit = m
                break
        if hit is None:
            rec["center"] = [cx, cy]
            merged.append(rec)
            continue
        hit["methods"] = sorted(set(hit["methods"]) | set(rec["methods"]))
        seen = {(s.get("handle"), s.get("type")) for s in hit.get("srcs") or ()}
        for s in rec.get("srcs") or ():
            if (s.get("handle"), s.get("type")) not in seen:
                hit["srcs"].append(s)
        # A rotated INSERT has a precise transformed outline.  A larger
        # axis-aligned bbox from derived graphics must never overwrite it.
        # Prefer a block's affine geometry; otherwise keep the first valid
        # source, avoiding 'largest box wins' across drawing rotations.
        original_method = hit.get("method")
        if rec.get("method") == "block" and original_method != "block":
            hit["outline"], hit["bbox"] = rec.get("outline"), rec.get("bbox")
            hit["w"], hit["h"] = rec["w"], rec["h"]
            hit["center"] = [cx, cy]
            hit["method"] = "block"

    out = []
    for rec in merged:
        w, h = _col_size(rec)
        short, long_ = min(w, h), max(w, h)
        score, notes = 0.0, []
        for method in rec["methods"]:
            score += COL_METHOD_W.get(method, 0.15)
        if any(m != "block" for m in rec["methods"]):
            score += COL_BOUNDARY_W
            notes.append("closed_boundary")
        if len(rec["methods"]) >= 2:
            notes.append("multi_method")
        ratio = long_ / (short or 1.0)
        # 方柱/矩形柱：短边要够"柱"，长短比 ≤ 3
        plausible = (short_min <= short <= short_max and long_ <= long_max
                     and ratio <= COL_ASPECT)
        # 长条形（贴墙垛、构造柱）：短边松到一墙厚，长短比放宽到 12，
        # 但**不按方柱计 confirmed**——它是结构构件，不是柱截面。
        elongated = (pier_min <= short < short_min and long_ <= long_max
                     and ratio <= PIER_ASPECT) or \
                    (short_min <= short <= short_max and long_ <= long_max
                     and COL_ASPECT < ratio <= PIER_ASPECT)
        if plausible:
            score += 0.20
            notes.append("section_ok")
        elif elongated:
            # 长条形（贴墙垛 / 构造柱）：不是方形柱，但确实是结构构件。
            # 保留并如实标注，不因为"不够方"就当没看见，也不当成方柱计数。
            score += 0.10
            notes.append("section_elongated")
        else:
            notes.append("section_odd")
        cx, cy = _col_center(rec)
        on_axis = _near(xs, cx, tol_touch) and _near(ys, cy, tol_touch)
        if on_axis:
            score += 0.10
            notes.append("on_axis")
        # 贴墙必须是**贴已确认的墙**：单图层图上"附近有任意一条线"对 100% 的
        # 候选都成立，那种 touches_wall 没有区分力，也不能用来挡模型的否决。
        touches = False
        touch_state = None
        for wall in walls or ():
            s0, e0 = wall.get("start"), wall.get("end")
            if not s0 or not e0:
                continue
            d = _pt_seg(cx, cy, s0[0], s0[1], e0[0], e0[1])
            if d <= tol_touch + 0.5 * max(w, h):
                strong = (str(wall.get("evidence_state") or "") in ("strong", "confirmed")
                          or str(wall.get("status") or "") == "confirmed")
                if strong:
                    score += 0.10
                    notes.append("touches_wall")
                    touches = True
                    touch_state = "strong"
                    break
                if touch_state is None:
                    touch_state = "weak"
                    notes.append("touches_weak_wall")
        # 缺边的矩形（只画了三条边）**必须有第二种独立画法背书**才算柱：
        # 第四条边不在图上，说明那一侧是别的构件（墙、洞口框、家具），单靠"再加
        # 一条边"就把三条边补成矩形属于自证。贴墙垛/构造柱确实只画三条边，但那
        # 类构件在图上通常还有填充或闭合轮廓——那才是第二条证据。
        missing_edge = int(rec.get("edges") or 4) <= 3
        if missing_edge:
            notes.append("missing_edge")
            if (on_axis or touches):
                notes.append("missing_edge_topo_supported")
            if len(rec["methods"]) < 2:
                notes.append("missing_edge_single_method")
        score = round(min(1.0, score), 3)
        rec["score"] = score
        rec["evidence"] = sorted(set(notes))
        rec["status"] = ("confirmed" if score >= COL_CONFIRM
                         else "uncertain" if score >= COL_REVIEW else "pending")
        if elongated and rec["status"] == "confirmed":
            # 长条形不能当"已确认的方柱"：证据成立，但形态是墙垛
            rec["status"] = "uncertain"
        rec["section_type"] = ("elongated" if elongated
                               else "regular" if plausible else "odd")
        # 上下文证据的强度要如实记录：模式 B 只信"strong"（贴已确认的墙）
        rec["touches_wall"] = touches
        rec["touches_wall_state"] = touch_state
        if missing_edge and len(rec["methods"]) < 2 and rec["status"] != "pending":
            # 缺边 + 只有一种画法 -> 只留待核，不进构件（防止靠"补第 4 条边"过检）
            rec["status"] = "pending"
            rec["pending_reason"] = "missing_edge_single_method"
        # 置信度跟着证据走：单一画法（尤其只有轮廓/填充）不会因为"看得见"就是高分
        rec["confidence"] = round(min(0.95, 0.40 + 0.60 * score), 3)
        rec["needs_review"] = rec["status"] != "confirmed"
        rec.setdefault("handle", (rec["srcs"] or [{}])[0].get("handle")
                       if rec.get("srcs") else None)
        out.append(rec)

    # ---- 非极大值抑制：一组线能枚举出多个互相包含的矩形（1810×300 / 600 / 1200），
    # 中心距去重（0.5×短边）挡不住它们。这不是"调容差"，是同一个解被枚举了三次。
    before = len(out)
    out = _suppress_overlapping_columns(out)
    suppressed = before - len(out)

    stats = {"candidates": len(pool), "fused": len(out), "suppressed": suppressed,
             "confirmed": sum(1 for c in out if c["status"] == "confirmed"),
             "uncertain": sum(1 for c in out if c["status"] == "uncertain"),
             "pending": sum(1 for c in out if c["status"] == "pending"),
             "by_method": {}}
    for c in out:
        for m in c["methods"]:
            stats["by_method"][m] = stats["by_method"].get(m, 0) + 1
    return {"columns": out, "stats": stats}


def _box_area(b) -> float:
    if not b or len(b) < 4:
        return 0.0
    return max(0.0, float(b[2]) - float(b[0])) * max(0.0, float(b[3]) - float(b[1]))


def _overlap_ratio(a, b) -> float:
    """交叠面积 / 较小面积：一个矩形被另一个包住时 = 1.0（比 IoU 敏感）。"""
    if not a or not b or len(a) < 4 or len(b) < 4:
        return 0.0
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    small = min(_box_area(a), _box_area(b))
    return inter / small if small > 0 else 0.0


def _suppress_overlapping_columns(recs):
    """柱候选 NMS：按分数贪心保留，被已保留矩形基本包住的丢掉。

    "一个柱只能在同一个位置出现一次"。同一组墙线/家具线可以枚举出多个尺寸不同
    但互相包含的矩形，它们不是五个柱，是一个解的五个写法。
    """
    from . import scale as _SC
    if not recs:
        return recs
    keep = []
    ranked = sorted(recs, key=lambda r: (-float(r.get("score") or 0.0),
                                         -_box_area(r.get("bbox") or ())))
    for rec in ranked:
        box = rec.get("bbox") or ()
        if any(_overlap_ratio(box, k.get("bbox") or ()) >= _SC.COL_NMS_CONTAIN
               for k in keep):
            continue
        keep.append(rec)
    order = {id(r): i for i, r in enumerate(recs)}
    return sorted(keep, key=lambda r: order.get(id(r), 0))


# ------------------------------------------------------------------ 门 / 窗
DOOR_BLOCKS = {"$TCHSYS$DOOR2D": "单扇平开门", "$DorLib2D$00000001": "防火门/双扇平开门",
               "$DorLib2D$00000002": "双扇平开门", "$TCHSYS$APDOOR2D": "隔断门"}
WIN_BLOCKS = {"$TCHSYS$WIN2D": "平开窗/固定窗"}


def _attr(rec):
    """块属性 A（快照 ``attribs`` 字典）。"""
    value = (rec.get("attribs") or {}).get("A")
    return value.strip() if isinstance(value, str) else None


def _transform(m, x, y, z=0.0):
    """与 ``ezdxf.math.Matrix44.transform`` 同序的仿射变换（行向量约定）。"""
    return (x * m[0] + y * m[4] + z * m[8] + m[12],
            x * m[1] + y * m[5] + z * m[9] + m[13],
            x * m[2] + y * m[6] + z * m[10] + m[14])


def _door_geom(rec, blocks):
    """由块定义 + INSERT 变换矩阵求：铰点、门扇、洞口段（负缩放/镜像安全）。

    ``blocks`` 是快照里预提取的门块 ARC/LINE 原始参数，``rec["matrix"]`` 是
    ``Matrix44`` 的 16 个浮点数；本函数是纯函数，可在 PURE 节点/线程池里执行。
    """
    block = blocks.get(rec.get("name"))
    m = rec.get("matrix")
    if block is None or m is None:
        return None
    arcs, lines = [], []
    for (cx, cy, r, a1_deg, a2_deg) in block.get("arcs", ()):
        a1 = math.radians(a1_deg)
        a2 = math.radians(a2_deg)
        arcs.append({
            "c": _transform(m, cx, cy, 0.0),
            "s": _transform(m, cx + r * math.cos(a1), cy + r * math.sin(a1), 0.0),
            "e": _transform(m, cx + r * math.cos(a2), cy + r * math.sin(a2), 0.0),
            "sweep": abs((a2_deg - a1_deg) % 360.0),
        })
    for (x1, y1, x2, y2) in block.get("lines", ()):
        lines.append((_transform(m, x1, y1, 0.0), _transform(m, x2, y2, 0.0)))
    if not arcs:
        return None
    # 摆动弧优先：60~120° 的弧排在前面，单扇门取的就是它，装饰弧不会抢铰点
    arcs.sort(key=lambda a: (not (60.0 <= a.get("sweep", 0.0) <= 120.0),
                             -a.get("sweep", 0.0)))
    hinges = [[a["c"][0], a["c"][1]] for a in arcs]
    leaf_tip, best = None, -1.0
    for a in arcs:
        c = (a["c"][0], a["c"][1])
        for p, q in lines:
            for pt in (p, q):
                d = math.hypot(pt[0] - c[0], pt[1] - c[1])
                if d > best:
                    best, leaf_tip = d, [pt[0], pt[1]]
    if len(arcs) >= 2:
        h1, h2 = hinges[0], hinges[1]
        return {"kind": "double", "hinges": [h1, h2], "opening": [h1, h2],
                "width": math.hypot(h2[0] - h1[0], h2[1] - h1[1])}
    a = arcs[0]
    h = hinges[0]
    s = [a["s"][0], a["s"][1]]
    t = [a["e"][0], a["e"][1]]
    tip = leaf_tip or s
    closed = t if math.hypot(s[0] - tip[0], s[1] - tip[1]) < math.hypot(t[0] - tip[0], t[1] - tip[1]) else s
    w = math.hypot(closed[0] - h[0], closed[1] - h[1])
    return {"kind": "single", "hinges": [h], "opening": [h, closed], "width": w,
            "leaf_tip": tip}


def recognize_doors(snapshot, texts, walls=None, layers=None, scan_all=False, extra=None):
    out = []
    blocks = snapshot.get("blocks", {})
    # 块名白名单不是唯一入口：按块长相/族表补进来的块同样进入候选
    extra = extra or {}
    for e in snapshot["entities"]:
        if e.get("type") != "INSERT":
            continue
        nm = e.get("name")
        subtype = DOOR_BLOCKS.get(nm) or extra.get(nm)
        if not subtype:
            continue
        g = _door_geom(e, blocks)
        attr = _attr(e)
        # 门宽口径统一走 sizing：编号 > 块内几何 > 属性声明 > |缩放|×块净尺寸。
        # 只认 |xscale| 时，"按真实尺寸画"的门块会被读成 1mm（实测艺术楼
        # M_E24TTP 的 500×650 隔断门），而这个 1mm 会顺流污染门宽中位数、
        # 洞口上下限、房间封缝上限，且全程不报错。
        code_size = None
        m_code = re.match(r"^[A-Z]*M(\d{2})(\d{2})", attr) if attr else None
        if m_code:
            code_size = (int(m_code.group(1)) * 100, int(m_code.group(2)) * 100)
        sized = SIZING.measure(e, blocks, opening=(g or {}).get("opening"),
                               kind="door", code_size=code_size)
        width = sized["width"]
        # 铰点 = 块内摆动弧圆心；块插入点可能画在洞口外（如 $DorLib2D$00000131 偏 750mm）
        if g and g.get("hinges"):
            hinge, anchor = [g["hinges"][0][0], g["hinges"][0][1]], "arc_center"
        else:
            hinge, anchor = [e["insert"][0], e["insert"][1]], "block_insert"
        rec = {"block": nm, "subtype": subtype, "layer": e["layer"],
               "insert": hinge, "insert_raw": [e["insert"][0], e["insert"][1]],
               "anchor": anchor,
               "rotation": e["rotation"], "scale": [e["xscale"], e["yscale"]],
               "attr": attr, "width": round(width, 1), "handle": e["handle"],
               "width_source": sized["source"],
               "width_candidates": sized["candidates"]}
        if g:
            rec["hinges"] = g["hinges"]
            rec["opening"] = g["opening"]
            rec["swing"] = _swing(g)
        # 门类型编码 M1021 -> 1000 宽 2100 高
        if code_size:
            rec["code_width"] = code_size[0]
            rec["code_height"] = code_size[1]
        rec["width_geom"] = round(width, 1)
        rec["width_insert"] = round(abs(float(e["xscale"] or 0.0)), 1)
        # 就近门编号文字
        lab = _nearest_text(texts, rec["insert"], 1600, r"FM|M\d{4}")
        if lab:
            rec["label"] = lab["text"]
            rec["label_handle"] = lab["handle"]
        out.append(rec)
    # Block and arc door representations may coexist in a single plan.
    # Run both branches, then deduplicate by handle/hinge position. A scan-all
    # fallback is only used if no arc was found on declared door layers.
    arc_candidates = _doors_from_arcs(snapshot, set(layers), walls or []) if layers else []
    # 全图扫弧不是"按门层扫不到才启用"的兜底，而是一路**独立证据**：门层是从
    # 图层名推断的，推断错了（例如把 LATRINE 当门层）时只扫该层会拿到 1 个候选，
    # 兜底条件因此不成立，整张图的门全丢（大图实测 0 樘）。两路合并即可：handle
    # 去重 + 下游按铰点位置去重，不会重复计数。
    if scan_all:
        _seen_arcs = {str(c.get("handle")) for c in arc_candidates}
        for _c in _doors_from_arcs(snapshot, None, walls or []):
            if str(_c.get("handle")) not in _seen_arcs:
                arc_candidates.append(_c)
                _seen_arcs.add(str(_c.get("handle")))
    seen = {str(d.get("handle")) for d in out}
    for arc in arc_candidates:
        if str(arc.get("handle")) in seen:
            continue
        anchor = arc.get("insert") or [0, 0]
        width = float(arc.get("width") or 0)
        if any(math.hypot(anchor[0] - (d.get("insert") or [0, 0])[0],
                          anchor[1] - (d.get("insert") or [0, 0])[1]) < max(80, 0.18 * width)
               for d in out):
            continue
        seen.add(str(arc.get("handle")))
        out.append(arc)
    return out


def _pct(vals, q):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return 0.0
    k = max(0, min(len(vals) - 1, int(round(q * (len(vals) - 1)))))
    return vals[k]


def _med(vals, default=0.0):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return default
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0


def _wall_lines(walls):
    """把墙段按"墙线"归组：({"H"/"V", 位置}: [(lo, hi), ...])。"""
    lines = {}
    for w in walls:
        (x1, y1), (x2, y2) = w["start"], w["end"]
        if abs(y1 - y2) <= 1.5:
            lines.setdefault(("H", round(y1)), []).append((min(x1, x2), max(x1, x2)))
        elif abs(x1 - x2) <= 1.5:
            lines.setdefault(("V", round(x1)), []).append((min(y1, y2), max(y1, y2)))
    for runs in lines.values():
        runs.sort()
    return lines


def _oriented_wall_runs(walls, seg, tol):
    """Closest collinear wall-face runs, in the opening's own local coordinates.

    Works for axial and oblique wall runs; the tolerance is *normal* distance,
    not distance to an endpoint. This avoids false exclusions at wall gaps.
    """
    (x0, y0), (x1, y1) = seg
    length = math.hypot(x1 - x0, y1 - y0)
    if length < 1e-8:
        return None
    ux, uy = (x1 - x0) / length, (y1 - y0) / length
    nx, ny = -uy, ux
    pos = x0 * nx + y0 * ny
    groups = []
    for w in walls or ():
        if w.get("arc"):
            # 弧墙的 start→end 是弦，不是墙线：拿弦去和洞口轴比方向会误判宿主
            continue
        p, q = w.get("start"), w.get("end")
        if not p or not q:
            continue
        dx, dy = q[0] - p[0], q[1] - p[1]
        wl = math.hypot(dx, dy)
        if wl < 1e-8 or abs((dx * uy - dy * ux) / wl) > 0.035:
            continue
        wp = (p[0] * nx + p[1] * ny + q[0] * nx + q[1] * ny) / 2
        if abs(wp - pos) > tol:
            continue
        a = p[0] * ux + p[1] * uy
        b = q[0] * ux + q[1] * uy
        groups.append((abs(wp - pos), (min(a, b), max(a, b))))
    if not groups:
        return None
    dmin = min(d for d, _ in groups)
    return sorted(run for d, run in groups if d <= dmin + min(20.0, tol * 0.2))


def _opening_on_wall_gap(walls, seg, thick):
    """Orientation-invariant opening validation against original wall runs."""
    runs = _oriented_wall_runs(walls, seg, 1.5 * float(thick or 200))
    if not runs:
        return False
    (x0, y0), (x1, y1) = seg
    length = math.hypot(x1 - x0, y1 - y0)
    ux, uy = (x1 - x0) / length, (y1 - y0) / length
    lo, hi = sorted((x0 * ux + y0 * uy, x1 * ux + y1 * uy))
    return _in_gap_or_end(runs, lo, hi, 1.5 * float(thick or 200))


def _in_line_extent(runs, lo, hi, tol):
    """洞口是否落在该墙线的总延伸范围内（墙跨洞口合并后没有缺口，用延伸范围判定）。"""
    lo0 = min(r[0] for r in runs)
    hi0 = max(r[1] for r in runs)
    return lo0 - tol <= lo and hi <= hi0 + tol


def _in_gap_or_end(runs, lo, hi, tol):
    """洞口是否落在墙段之间的缺口里，或落在墙线端部（门洞常开在墙端）。"""
    for i in range(len(runs) - 1):
        g0, g1 = runs[i][1], runs[i + 1][0]
        if g1 > g0 and g0 - tol <= lo and hi <= g1 + tol:
            return True
    return lo <= runs[0][0] + tol or hi >= runs[-1][1] - tol


def _line_of(walls_lines, orient, pos, tol):
    best = None
    for (o, p), runs in walls_lines.items():
        if o != orient:
            continue
        d = abs(p - pos)
        if d <= tol and (best is None or d < best[3]):
            best = (o, p, runs, d)
    return best


#: 开启弧张角的合理区间（度）：门扇 90° 左右，推拉/折叠门更小
ARC_SWEEP = (40.0, 150.0)


def _doors_from_arcs(snapshot, layers, walls):
    """门 = 候选弧 + 约束：铰点在墙线上、门扇闭合端回落到同一墙线、洞口落在墙缺口/端部。

    不按半径/扫掠的绝对值筛，装饰弧、图例弧因为贴不上墙线自然被淘汰。
    """
    if not walls:
        return []
    thick = _med([float(w.get("thickness") or 0) for w in walls], 200.0)
    tol = 1.5 * thick
    out = []
    for e in _geo(snapshot):
        if e.get("type") != "ARC":
            continue
        if layers is not None and e.get("layer") not in layers:
            continue
        # A full-drawing fallback traverses furniture, sanitary fixtures and
        # decorative swing arcs. Those layers are negative semantic evidence;
        # explicit door-layer assignments can still override this veto.
        layer_name = str(e.get("layer") or "").lower()
        if layers is None and any(word in layer_name for word in (
                "家具", "洁具", "卫生洁具", "furniture", "sanitary",
                "fixture", "latrine", "软装")):
            continue
        r = float(e["radius"])
        # 门扇半径 = 门宽。图例里的"点""圆圈"也是 90° 弧，只有尺寸能把它们分开：
        # 实测艺术楼"设置"层上半径 10mm、40mm 的装饰弧被当成两樘平开门。
        # 下限取 1.5 倍墙厚（门扇不可能比墙还细），与图纸比例无关。
        if r < 1.5 * thick:
            continue
        cx, cy = float(e["center"][0]), float(e["center"][1])

        a1, a2 = math.radians(float(e["start_angle"])), math.radians(float(e["end_angle"]))
        p1 = [cx + r * math.cos(a1), cy + r * math.sin(a1)]
        p2 = [cx + r * math.cos(a2), cy + r * math.sin(a2)]
        # Use the center -> closed-leaf-end direction as the aperture axis.
        # Either arc endpoint may denote the closed position.
        choices = []
        for closed, free in ((p1, p2), (p2, p1)):
            axis = [[cx, cy], closed]
            runs = _oriented_wall_runs(walls, axis, tol)
            if not runs:
                continue
            if _opening_on_wall_gap(walls, axis, thick):
                choices.append((closed, free, axis))
        if not choices:
            continue
        closed, free, axis = choices[0]
        lo, hi = 0.0, math.hypot(closed[0] - cx, closed[1] - cy)
        # 开启弧的张角也要像门：小于 40° 是装饰弧，大于 150° 是整圆分段
        sweep = abs((float(e["end_angle"]) - float(e["start_angle"])) % 360.0)
        if not (ARC_SWEEP[0] <= sweep <= ARC_SWEEP[1]):
            continue

        free = p2 if closed is p1 else p1
        out.append({"block": None, "subtype": "平开门（弧线）", "layer": e["layer"],
                    "insert": [cx, cy], "rotation": float(e["start_angle"]),
                    "scale": [r, r], "attr": None, "width": round(hi - lo, 1),
                    "handle": e["handle"], "hinges": [[cx, cy]],
                    "opening": [[cx, cy], closed], "free_end": free, "swing": None})
    return out


def _swing(g):
    if g["kind"] == "double":
        return {"type": "双扇", "direction": "两扇对开"}
    h = g["hinges"][0]
    tip = g.get("leaf_tip")
    closed = g["opening"][1]
    if not tip:
        return None
    vx, vy = closed[0] - h[0], closed[1] - h[1]
    L = math.hypot(vx, vy) or 1.0
    vx, vy = vx / L, vy / L
    wx, wy = tip[0] - h[0], tip[1] - h[1]
    cross = vx * wy - vy * wx
    dot = vx * wx + vy * wy
    return {"hinge": h, "leaf_tip": tip, "open_side": "左" if cross > 0 else "右",
            "open_angle_deg": round(math.degrees(math.acos(max(-1, min(1, dot / (math.hypot(wx, wy) or 1))))), 1),
            "swing_side_normal_sign": 1 if cross > 0 else -1}


def _host_wall_mode(walls, axis, thick):
    """候选窗的宿主墙：返回 ``"gap"``（落在墙缺口里）/ ``"line"``（压在墙线上）/ None。

    窗是"墙上的一个洞"，没有宿主的窗就是误检——单图层图上仅凭"长宽比 ≥3"会
    放出上百个家具块，这条判据是唯一能挡住它们的结构性事实。
    """
    if not walls or not axis:
        return None
    tol = 1.5 * float(thick or 200)
    runs = _oriented_wall_runs(walls, axis, tol)
    if not runs:
        return None
    (x0, y0), (x1, y1) = axis
    length = math.hypot(x1 - x0, y1 - y0)
    if length < 1e-8:
        return None
    ux, uy = (x1 - x0) / length, (y1 - y0) / length
    lo, hi = sorted((x0 * ux + y0 * uy, x1 * ux + y1 * uy))
    if _in_gap_or_end(runs, lo, hi, tol):
        return "gap"
    if _in_line_extent(runs, lo, hi, tol):
        return "line"
    return None


def recognize_windows(snapshot, texts, layers=None, walls=None, extra=None):
    out = []
    thick = _med([float(w.get("thickness") or 0) for w in (walls or ())], 200.0)
    for e in snapshot["entities"]:
        if e.get("type") != "INSERT":
            continue
        nm = e.get("name")
        if not (nm in WIN_BLOCKS or (extra or {}).get(nm)):
            continue
        rot = math.radians(e["rotation"])
        # 与门同一口径：单位块靠 |缩放|×块净尺寸，真实尺寸块靠块净尺寸。
        # 只认 |xscale| 会把"按真实尺寸画的窗块"读成 1mm 的退化轴线。
        blocks = snapshot.get("blocks") or {}
        sized = SIZING.measure(e, blocks, kind="window")
        w = sized["width"]
        t = sized["height"] or abs(e["yscale"])
        cx, cy = e["insert"][0], e["insert"][1]
        ux, uy = math.cos(rot), math.sin(rot)
        p1 = [cx - ux * w / 2, cy - uy * w / 2]
        p2 = [cx + ux * w / 2, cy + uy * w / 2]
        # 长相判定出来的陌生窗块：**必须有宿主墙**（白名单块保留原行为，避免动老图）
        why = (extra or {}).get(nm)
        host = None
        if isinstance(why, str) and why.startswith("shape:"):
            host = _host_wall_mode(walls, [p1, p2], thick)
            if host is None:
                continue
        rec = {"block": nm, "layer": e["layer"], "center": [cx, cy],
               "host_wall": host, "on_wall_gap": (host == "gap") or None,
               "rotation": e["rotation"] % 360.0, "width": round(w, 1),
               "thickness": round(t, 1), "axis": [p1, p2],
               "attr": _attr(e), "handle": e["handle"],
               "width_source": sized["source"],
               "width_candidates": sized["candidates"],
               "block_evidence": (extra or {}).get(nm)}
        lab = _nearest_text(texts, [cx, cy], 2200, r"^[A-Z]{1,4}\d{3,5}")
        if lab:
            rec["label"] = lab["text"]
            rec["label_handle"] = lab["handle"]
        out.append(rec)
    if layers:
        thick = _med([float(w.get("thickness") or 0) for w in (walls or [])], 200.0)
        # Keep both block and line-drawn windows, dedupe at the same aperture.
        for cand in _windows_from_collinear(snapshot, set(layers), thick, walls, texts):
            # A long facade line is not one window: reject line candidates
            # wider than the unified opening maximum (2.6 m).
            if float(cand.get("width") or 0) > max(1400.0, 13.0 * thick):
                continue
            # When block windows coexist, require an independently observed
            # wall gap for line-drawn windows; extent-only evidence is too weak
            # and can shadow an adjacent block door during unified selection.
            if out and not _opening_on_wall_gap(walls or [], cand.get("axis"), thick):
                continue
            center = cand.get("center") or [0, 0]
            if any(math.hypot(center[0] - (w.get("center") or [0, 0])[0],
                              center[1] - (w.get("center") or [0, 0])[1])
                   <= max(100.0, min(float(cand.get("width") or 0), float(w.get("width") or 0)) * 0.25)
                   for w in out):
                continue
            out.append(cand)
    return out


def _windows_from_collinear(snapshot, layers, thick=200.0, walls=None, texts=None):
    """窗候选：窗/幕墙图层上的线全部进池（不按长度预筛），
    按方向 + 横向间距 + 沿墙相邻成组；单线需有窗号；同线相邻组并成一樘。"""
    wall_lines = _wall_lines(walls or [])
    tol = max(120.0, 1.5 * thick)
    segs = []
    for e in _geo(snapshot):
        if e.get("type") != "LINE" or e.get("layer") not in layers:
            continue
        (x1, y1), (x2, y2) = e["start"], e["end"]
        if abs(y1 - y2) <= 1.5 and abs(x2 - x1) >= 20:
            segs.append(("H", y1, min(x1, x2), max(x1, x2), e["handle"], e["layer"]))
        elif abs(x1 - x2) <= 1.5 and abs(y2 - y1) >= 20:
            segs.append(("V", x1, min(y1, y2), max(y1, y2), e["handle"], e["layer"]))
    if not segs:
        return []

    segs.sort(key=lambda s: (s[0], s[1], s[2]))
    used, groups = set(), []
    for s in segs:
        if s[4] in used:
            continue
        grp = [s]
        used.add(s[4])
        changed = True
        while changed:
            changed = False
            for t in segs:
                if t[4] in used or t[0] != s[0]:
                    continue
                for g in grp:
                    if abs(t[1] - g[1]) <= tol and not (t[3] < g[2] - tol or t[2] > g[3] + tol):
                        grp.append(t)
                        used.add(t[4])
                        changed = True
                        break
        groups.append(grp)

    out = []
    for grp in groups:
        orient = grp[0][0]
        lo = min(g[2] for g in grp)
        hi = max(g[3] for g in grp)
        pos = sum(g[1] for g in grp) / len(grp)
        line = _line_of(wall_lines, orient, pos, tol)
        if line is None or not _in_line_extent(line[2], lo, hi, tol):
            continue
        if len({g[4] for g in grp}) < 2:
            mid = ((lo + hi) / 2.0, pos) if orient == "H" else (pos, (lo + hi) / 2.0)
            if not _nearest_text(texts or [], mid, 2500, r"^[A-Z]{1,4}\d{3,5}$"):
                continue
        axis = [[lo, pos], [hi, pos]] if orient == "H" else [[pos, lo], [pos, hi]]
        out.append({"block": None, "layer": grp[0][5],
                    "center": [(axis[0][0] + axis[1][0]) / 2, (axis[0][1] + axis[1][1]) / 2],
                    "rotation": 0.0 if orient == "H" else 90.0,
                    "width": round(hi - lo, 1),
                    "thickness": round(max(g[1] for g in grp) - min(g[1] for g in grp), 1),
                    "axis": axis, "attr": None, "handle": grp[0][4],
                    "srcs": [{"handle": g[4], "type": "LINE"} for g in grp]})

    # 同线相邻窗并成一樘（幕墙分格不该按每条线计一次）
    out.sort(key=lambda w: (0 if abs(w["axis"][0][1] - w["axis"][1][1]) < 1 else 1,
                            w["axis"][0][1], w["axis"][0][0]))
    merged = []
    for w in out:
        if merged:
            m = merged[-1]
            same_orient = abs(w["axis"][0][1] - w["axis"][1][1]) < 1 and                 abs(m["axis"][0][1] - m["axis"][1][1]) < 1
            if same_orient and abs(w["axis"][0][1] - m["axis"][0][1]) <= 1.0:
                a0, a1 = m["axis"][0][0], m["axis"][1][0]
                b0, b1 = w["axis"][0][0], w["axis"][1][0]
                if min(a0, a1, b0, b1) >= min(a0, a1) - 1 and max(b0, b1) - min(a0, a1) <= max(a1, b1) - min(a0, a1) + 2 * thick:
                    lo2, hi2 = min(a0, a1, b0, b1), max(a0, a1, b0, b1)
                    m["axis"] = [[lo2, m["axis"][0][1]], [hi2, m["axis"][0][1]]]
                    m["center"] = [(lo2 + hi2) / 2.0, m["axis"][0][1]]
                    m["width"] = round(hi2 - lo2, 1)
                    m["srcs"] = m["srcs"] + w["srcs"]
                    continue
        merged.append(w)
    return merged


def _nearest_text(texts, pt, maxd, pattern=None):
    best, bd = None, maxd
    for t in texts:
        s = t["text"].strip()
        if not s:
            continue
        if pattern and not re.search(pattern, s):
            continue
        d = math.hypot(t["x"] - pt[0], t["y"] - pt[1])
        if d < bd:
            bd, best = d, t
    return best


# ------------------------------------------------------------------ 楼梯
#: 一个梯段至少几条踏步线。少于 4 条的"等距平行线"在平面图上到处都是（墙的
#: 粉刷线、门窗线束、铺装分格），必须以"够跑一跑"为门槛。
STAIR_MIN_TREADS = 4
#: 相邻踏步高差（线间距）的允许波动：同一梯段的踏步是等分的，人工画图误差
#: 不会超过 1/4 个踏步宽。
STAIR_SPACING_JITTER = 0.25


def _stairs_rulers(scale):
    """楼梯判据的两把尺子：踏步宽与踏面线长度，都用图内门宽换算。

    踏步宽（250~320mm）≈ 0.3 个门宽，踏面线长度（梯段宽 1.2~2.0m）≈ 1.3~2.2
    个门宽——这两个比例来自"人能走的楼梯"这个物理事实，与图纸比例、单位、
    图层命名都无关，所以不写死毫米数。
    """
    from . import scale as SC
    th = SC.thresholds(scale or {})
    door = float(th.get("door_w") or SC.DEFAULT_DOOR_W)
    st = SC.STRUCTURE
    return {
        "spacing_lo": st["stair_tread_min_door"] * door,
        "spacing_hi": st["stair_tread_max_door"] * door,
        "tread_lo": st["stair_width_min_door"] * door,
        "tread_hi": st["stair_width_max_door"] * door,
        "same_tol": st["stair_align_door"] * door,
    }


def _seg_orient(x1, y1, x2, y2, tol):
    """轴对齐的线段 -> ("H"|"V", 沿线段方向的 [lo, hi], 法向位置)；斜线返回 None。"""
    if abs(y1 - y2) <= tol:
        return "H", min(x1, x2), max(x1, x2), (y1 + y2) / 2.0
    if abs(x1 - x2) <= tol:
        return "V", min(y1, y2), max(y1, y2), (x1 + x2) / 2.0
    return None


def _flight_runs(items, *, spacing_lo, spacing_hi):
    """同一"跨度签名"下，按法向位置排序，切出等距连续的踏步串。"""
    items.sort(key=lambda it: it["pos"])
    runs, run = [], []
    for it in items:
        if not run:
            run = [it]
            continue
        gap = it["pos"] - run[-1]["pos"]
        base = run[-1]["pos"] - run[-2]["pos"] if len(run) > 1 else gap
        ok = (spacing_lo <= gap <= spacing_hi
              and abs(gap - base) <= STAIR_SPACING_JITTER * base)
        if ok:
            run.append(it)
        else:
            runs.append(run)
            run = [it]
    if run:
        runs.append(run)
    return runs


def recognize_stairs(snapshot, texts, layers=None, scale=None):
    """楼梯：**踏步串**识别（平行 + 等距 + 够长），不是"某个图层上的线聚类"。

    老做法把楼梯层上的线按 x 位置聚成一簇，再取簇的外接框当楼梯——只要该层上
    有一根贯穿全图的线（剖断线、扶手、标注引线），外接框就被拉成整栋楼那么长，
    于是"楼梯"变成图面上一条横贯的长条。实测艺术楼 3 部楼梯全部是这个形状。

    真正的梯段有很强的形态特征，且与画法无关：

    * 一组**平行**的短线（踏面线），长度≈梯段宽；
    * 相邻间距**等分**（踏步宽），波动不超过 1/4；
    * 至少 4 条才算"一跑"。

    所以这里按"跨度签名（首尾对齐）+ 法向等距"直接找梯段，外接框只取踏步本身，
    图层上那些长线自然进不来。判据的两把尺子是门宽（见 ``_stairs_rulers``）。
    """
    wanted = set(layers or ("STAIR",))
    tol = 1.5
    rulers = _stairs_rulers(scale)
    segs = []
    for e in _geo(snapshot):
        if e.get("type") != "LINE" or e.get("layer") not in wanted:
            continue
        x1, y1 = e["start"]
        x2, y2 = e["end"]
        got = _seg_orient(x1, y1, x2, y2, tol)
        if got is None:
            continue
        orient, lo, hi, pos = got
        length = hi - lo
        if not (rulers["tread_lo"] <= length <= rulers["tread_hi"]):
            continue                     # 梯段宽有物理区间：太长/太短都不是踏面线
        segs.append({"orient": orient, "lo": lo, "hi": hi, "pos": pos,
                     "length": length, "handle": e["handle"], "layer": e.get("layer")})
    if not segs:
        return []

    # 同一梯段的踏面线首尾对齐：按 (lo, hi) 聚类，容差 = 0.1 个门宽
    buckets = []
    for s in segs:
        for b in buckets:
            if (b["orient"] == s["orient"]
                    and abs(b["lo"] - s["lo"]) <= rulers["same_tol"]
                    and abs(b["hi"] - s["hi"]) <= rulers["same_tol"]):
                b["items"].append(s)
                break
        else:
            buckets.append({"orient": s["orient"], "lo": s["lo"], "hi": s["hi"],
                            "items": [s]})

    out = []
    for b in buckets:
        for run in _flight_runs(b["items"], spacing_lo=rulers["spacing_lo"],
                                spacing_hi=rulers["spacing_hi"]):
            if len(run) < STAIR_MIN_TREADS:
                continue
            spac = [round(run[i + 1]["pos"] - run[i]["pos"], 1)
                    for i in range(len(run) - 1)]
            lo = min(it["lo"] for it in run)
            hi = max(it["hi"] for it in run)
            p0 = min(it["pos"] for it in run)
            p1 = max(it["pos"] for it in run)
            bb = [lo, p0, hi, p1] if b["orient"] == "H" else [p0, lo, p1, hi]
            mid = [(bb[0] + bb[2]) / 2.0, (bb[1] + bb[3]) / 2.0]
            near = max(0.5 * (bb[2] - bb[0] + bb[3] - bb[1]), rulers["tread_lo"])
            lab = _nearest_text(texts, mid, near * 2.0, r"楼梯")
            if lab:
                # 编号与"楼梯"分属不同字高，就近补齐前缀（如 1# / 2#）
                pre = _nearest_text(texts, [lab["x"] - 0.3 * near, lab["y"]],
                                    0.5 * near, r"^\d+#?$")
                if pre and not lab["text"].startswith(pre["text"]):
                    lab = dict(lab, text=pre["text"].rstrip("#") + "#" + lab["text"])
            up = _nearest_text(texts, mid, near, r"^上$")
            if b["orient"] == "H":
                lines = [{"y": round(it["pos"], 1), "x0": round(it["lo"], 1),
                          "x1": round(it["hi"], 1), "handle": it["handle"]} for it in run]
            else:
                lines = [{"x": round(it["pos"], 1), "y0": round(it["lo"], 1),
                          "y1": round(it["hi"], 1), "handle": it["handle"]} for it in run]
            out.append({"bbox": [round(v, 1) for v in bb],
                        "orient": b["orient"],
                        "tread_count": len(run),
                        "tread_spacing": round(sum(spac) / len(spac), 1),
                        "tread_width": round(hi - lo, 1),
                        "tread_lines": lines,
                        "label": lab["text"] if lab else None,
                        "label_handle": lab["handle"] if lab else None,
                        "up_label": up["text"] if up else None,
                        "srcs": [{"handle": it["handle"], "type": "LINE",
                                  "layer": it["layer"]} for it in run]})
    # 同一部楼梯可能被画成两条平行的跑梯（中间一条缝），按外接框相交合并
    out.sort(key=lambda r: -r["tread_count"])
    merged = []
    for r in out:
        for m in merged:
            if (m["bbox"][0] <= r["bbox"][2] and r["bbox"][0] <= m["bbox"][2]
                    and m["bbox"][1] <= r["bbox"][3] and r["bbox"][1] <= m["bbox"][3]):
                m["tread_count"] += r["tread_count"]
                m["tread_lines"] = list(m["tread_lines"]) + list(r["tread_lines"])
                m["bbox"] = [min(m["bbox"][0], r["bbox"][0]),
                             min(m["bbox"][1], r["bbox"][1]),
                             max(m["bbox"][2], r["bbox"][2]),
                             max(m["bbox"][3], r["bbox"][3])]
                m["srcs"] = list(m["srcs"]) + list(r["srcs"])
                m.setdefault("parallel_runs", 1)
                m["parallel_runs"] += 1
                break
        else:
            merged.append(dict(r))
    return merged


# ------------------------------------------------------------------ 散水 / 台阶
def recognize_apron(snapshot, walls, layers=None):
    """散水：外圈闭合/半闭合轮廓。``layers`` 来自 profile，不写死图层名。"""
    wanted = set(layers or ("散水",))
    polys = []
    for e in _geo(snapshot):
        if e.get("layer") not in wanted:
            continue
        for s in segments_from(e):
            polys.append({"seg": s, "handle": e["handle"], "type": e["type"]})
    if not polys:
        return None, []
    xs = [v for s in polys for v in (s["seg"][0], s["seg"][2])]
    ys = [v for s in polys for v in (s["seg"][1], s["seg"][3])]
    bb = [min(xs), min(ys), max(xs), max(ys)]
    # 散水宽度 = 外边线到最近外墙面的距离
    outer = [s for s in polys if seg_len(*s["seg"]) > 3000]
    widths = []
    for s in outer:
        x1, y1, x2, y2 = s["seg"]
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        best = 1e18
        for w in walls:
            d, _ = point_seg_dist(mx, my, *w["start"], *w["end"])
            best = min(best, d - w["thickness"] / 2)
        if best < 2000:
            widths.append(round(best, 0))
    return {"bbox": bb, "segments": [{"p1": [s["seg"][0], s["seg"][1]], "p2": [s["seg"][2], s["seg"][3]],
                                     "handle": s["handle"], "type": s["type"]} for s in polys],
            "width_candidates": widths,
            "width": max(set(widths), key=widths.count) if widths else None}, polys


def recognize_steps(snapshot, texts, walls, layers=None):
    """台阶：由成组"U 形/折线"轮廓与台阶边线识别。"""
    wanted = set(layers or ("散水",))
    polylines = []
    for e in _geo(snapshot):
        if e.get("layer") not in wanted or e.get("type") != "LWPOLYLINE":
            continue
        pts = [[float(p[0]), float(p[1])] for p in e.get("points", ())]
        polylines.append({"pts": pts, "handle": e["handle"], "closed": e.get("closed")})
    # 找形如 U 的开口轮廓：3~4 点，两边平行
    cands = []
    for p in polylines:
        pts = p["pts"]
        if len(pts) not in (3, 4, 5):
            continue
        bb = bbox_of_points(pts)
        w, h = bb[2] - bb[0], bb[3] - bb[1]
        if 800 <= w <= 8000 and 500 <= h <= 4000:
            cands.append({"pts": pts, "bb": bb, "w": w, "h": h, "handle": p["handle"]})
    # 同中心、尺寸相差 ~300 的成组 = 踏步层
    groups = []
    for c in sorted(cands, key=lambda c: -c["w"] * c["h"]):
        hit = None
        for g in groups:
            gc = g[0]
            if (abs((c["bb"][0] + c["bb"][2]) / 2 - (gc["bb"][0] + gc["bb"][2]) / 2) < 500
                    and abs((c["bb"][1] + c["bb"][3]) / 2 - (gc["bb"][1] + gc["bb"][3]) / 2) < 500):
                hit = g
                break
        if hit:
            hit.append(c)
        else:
            groups.append([c])
    out = []
    for g in groups:
        if len(g) < 2:
            continue
        bb = [min(c["bb"][0] for c in g), min(c["bb"][1] for c in g),
              max(c["bb"][2] for c in g), max(c["bb"][3] for c in g)]
        steps = len(g)
        # 每级踏步深度
        hs = sorted({round(c["h"], 0) for c in g})
        depths = [round(hs[i + 1] - hs[i], 0) for i in range(len(hs) - 1)]
        lab = _nearest_text(texts, [(bb[0] + bb[2]) / 2, bb[3] + 300], 3500, r"台阶|坡道")
        out.append({"bbox": bb, "levels": steps, "tread_depths": depths,
                    "label": lab["text"] if lab else None,
                    "label_handle": lab["handle"] if lab else None,
                    "srcs": [{"handle": c["handle"], "type": "LWPOLYLINE", "layer": "散水"} for c in g]})
    return out


def recognize_entrance_steps(snapshot, walls, min_len=3000.0, max_len=9000.0,
                             layers=None):
    """入口台阶（非嵌套 U 形画法）：由散水图层上的平行台阶边线识别。

    本图南入口台阶画成两条相距 300mm 的横线（12181/12481），
    与"嵌套 U 形"的北侧台阶画法不同，需要单独一条规则。
    """
    if not walls:
        return []
    ymin = min(min(w["start"][1], w["end"][1]) for w in walls)
    wanted = set(layers or ("散水",))
    segs = []
    for e in _geo(snapshot):
        if e.get("layer") not in wanted:
            continue
        for (x1, y1, x2, y2) in segments_from(e):
            if abs(y1 - y2) <= 1 and min_len <= abs(x2 - x1) <= max_len and y1 < ymin - 200:
                segs.append((min(x1, x2), max(x1, x2), y1, e["handle"]))
    if len(segs) < 2:
        return []

    # 按 x 重叠聚类，要求间距 200~600mm 且横向重叠 >= 50%
    segs.sort(key=lambda t: -t[2])
    groups = []
    for a, b, y, hd in segs:
        placed = False
        for grp in groups:
            ga, gb, gy = grp[-1][0], grp[-1][1], grp[-1][2]
            ov = min(b, gb) - max(a, ga)
            if ov >= 0.5 * min(b - a, gb - ga) and 200 <= abs(y - gy) <= 600:
                grp.append((a, b, y, hd))
                placed = True
                break
        if not placed:
            groups.append([(a, b, y, hd)])

    out = []
    for grp in groups:
        if len(grp) < 2:
            continue
        x0 = min(g[0] for g in grp)
        x1 = max(g[1] for g in grp)
        ys = sorted(g[2] for g in grp)
        out.append({"bbox": [x0, ys[0], x1, ymin], "edge_count": len(grp),
                    "tread_depths": [round(ys[i + 1] - ys[i], 0) for i in range(len(ys) - 1)],
                    "srcs": [{"handle": g[3], "type": "LWPOLYLINE" if False else "LINE",
                              "layer": "散水"} for g in grp]})
    return out
