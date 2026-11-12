"""图层角色推导：几何指纹（含**上下文**）+ 图层名先验，等权 0.5/0.5 后统一决策。

1. 几何看**上下文**：窗的双线和墙的双线在层内统计上几乎一样，区别在于窗框线
   贴在别层的墙线上、跨度短；墙是长双线。故有 ``on_wall_ratio`` 与
   ``long_pair_rate / short_pair_rate`` 两个分档。
2. 分数不饱和。``room_name`` 对纯文字层上限 0.85；``ignore`` 有下限形式。
3. **墙的判据不看名字**（见 ``_geom_scores`` 里的 ``wall``）：一条墙 = 两条面线，
   重叠长度 ≥ 6 倍面线间距。先在图上量出"墙厚是哪几个值"（细长线对直方图的
   若干峰），再逐层审"这层有多少线是该厚度上的双线 / 多细长 / 整层是否几乎都是
   墙线"。实体被搬到别的图层、或图层名被改掉时，这三条都不变。
   旧判据 ``long_pair_rate``（要求重叠 ≥1500mm）实测把真墙层判成 0.30 分、把
   楼梯踏步判成 0.51 分——判据指反了，墙实际上是靠图层名撑着，名字一乱整层就丢。

决策一次定音（不再是单一绝对阈值）：

    score = 0.5 * 几何 + 0.5 * 名字先验（缺一侧按另一侧归一）
    几何否决优先（填充层不许当墙/柱；成对率高不许当家具；带尺寸/文字的层
    不许当墙；比常规墙厚一半还薄的平行线对不许当墙）
    接受条件（满足任一）：
      * best >= 0.50 且领先第二名 >= 0.03
      * 名字极强 prior >= 0.90 且几何没有强反对（差 < 0.75）
      * 几何极强 geom >= 0.75 且名字不反对 prior >= 0.25
    几何一票定音（**只对墙**）：geom.wall >= 0.85 且领先第二名 >= 0.25 时，
      名字先验不能再否决——"这层叫家具、里面全是墙线"就是这样。
    0.35 ~ 接受线 -> ambiguous（交控制器试候选 + 影子验收）；< 0.35 视为无信息
"""
from __future__ import annotations

import collections
import math

from .scale import DEFAULT_THICKNESS as DEFAULT_THICK, STRUCTURE as SC

MIN_ENTITIES = 2
THICK_BAND = 20.0
ACCEPT = 0.50
HOLD = 0.35
MARGIN = 0.03
STRONG_PRIOR = 0.90
STRONG_GEOM = 0.75
CONTRADICTION = 0.75
CELL = 1000.0


# ------------------------------------------------------------------ 指纹
def fingerprints_from_msp(doc, msp) -> dict:
    """读 live 文档算每个图层的几何指纹（流式：只留紧凑元组，不留 support dict）。"""
    per: dict[str, dict] = {}
    buckets: dict[tuple, list] = {}
    dirs: dict[tuple, tuple] = {}
    long_grid: dict[tuple, list] = collections.defaultdict(list)

    def slot(layer):
        return per.setdefault(layer, {"n": 0, "types": {}, "lines": 0, "arcs": 0,
                                      "closed": 0, "fills": 0, "texts": 0,
                                      "blocks": 0, "texts_sample": [],
                                      "bbox": None, "heights": []})

    for e in msp:
        try:
            lay = str(e.dxf.layer)
            t = e.dxftype()
        except Exception:
            continue
        s = slot(lay)
        s["n"] += 1
        s["types"][t] = s["types"].get(t, 0) + 1
        if t == "LINE":
            try:
                p, q = e.dxf.start, e.dxf.end
                dx, dy = float(q.x) - float(p.x), float(q.y) - float(p.y)
                ln = math.hypot(dx, dy)
                if ln > 1e-9:
                    ux, uy = dx / ln, dy / ln
                    nx, ny = -uy, ux
                    pos = float(p.x) * nx + float(p.y) * ny
                    lo = float(p.x) * ux + float(p.y) * uy
                    hi = float(q.x) * ux + float(q.y) * uy
                    if hi < lo:
                        lo, hi = hi, lo
                    bkey = (lay, round((math.degrees(math.atan2(uy, ux)) % 180.0) / 1.5))
                    dirs.setdefault(bkey, (ux, uy))
                    buckets.setdefault(bkey, []).append(
                        (round(pos, 2), round(lo, 2), round(hi, 2), round(ln, 2)))
                    s["lines"] += 1
                    if ln >= 2000.0:                 # 长直线：窗/门的"墙上下文"
                        for cell in _cells(min(p.x, q.x), min(p.y, q.y),
                                           max(p.x, q.x), max(p.y, q.y)):
                            long_grid[cell].append((lay, float(p.x), float(p.y),
                                                    float(q.x), float(q.y)))
            except Exception:
                pass
        elif t in ("ARC", "CIRCLE"):
            s["arcs"] += 1
        elif t in ("LWPOLYLINE", "POLYLINE"):
            try:
                s["closed"] += 1 if e.closed else 0
            except Exception:
                s["closed"] += 1
        elif t in ("HATCH", "SOLID"):
            s["fills"] += 1
        elif t in ("TEXT", "MTEXT"):
            s["texts"] += 1
            try:
                txt = (e.dxf.text if t == "TEXT" else e.text) or ""
                if txt.strip() and len(s["texts_sample"]) < 5:
                    s["texts_sample"].append(txt.strip()[:14])
                if len(s["heights"]) < 64:
                    s["heights"].append(float(e.dxf.height))
            except Exception:
                pass
        elif t == "INSERT":
            s["blocks"] += 1
        b = _ent_bbox(e)
        if b:
            cur = s["bbox"]
            s["bbox"] = b if not cur else [min(cur[0], b[0]), min(cur[1], b[1]),
                                           max(cur[2], b[2]), max(cur[3], b[3])]

    all_bbox = None
    for s in per.values():
        b = s.get("bbox")
        if b:
            all_bbox = b if not all_bbox else [
                min(all_bbox[0], b[0]), min(all_bbox[1], b[1]),
                max(all_bbox[2], b[2]), max(all_bbox[3], b[3])]
    span_w = max(1.0, (all_bbox[2] - all_bbox[0]) if all_bbox else 1.0)
    span_h = max(1.0, (all_bbox[3] - all_bbox[1]) if all_bbox else 1.0)

    layer_buckets: dict[str, list] = collections.defaultdict(list)
    for bkey, members in buckets.items():
        layer_buckets[bkey[0]].append((bkey, members))   # 带上方向，算世界坐标用
    buckets.clear()

    obs: dict[str, list] = {}          # 层 -> [(面线间距, 重叠长度)]
    raw: dict[str, dict] = {}          # 第一遍算出来的与角色无关的统计
    for lay, s in per.items():
        n = s["n"] or 1
        if s["n"] < MIN_ENTITIES:
            continue
        lens, gaps = [], []
        pairs = short_pairs = long_pairs = on_wall = 0
        stripe: list[tuple[float, float]] = []      # (面线间距, 重叠长度)
        for bkey, members in layer_buckets.get(lay) or ():
            ux, uy = dirs.get(bkey) or (1.0, 0.0)
            nx, ny = -uy, ux
            members.sort()
            lens.extend(m[3] for m in members)
            for i, a in enumerate(members):
                for b in members[i + 1:]:
                    d = b[0] - a[0]
                    if d > 900.0:
                        break
                    if d < 30.0:
                        continue
                    ov = min(a[2], b[2]) - max(a[1], b[1])
                    if ov < 0.5 * min(a[3], b[3] or 1.0):
                        continue
                    pairs += 1
                    gaps.append(d)
                    stripe.append((d, ov))
                    if ov >= 1500.0:
                        long_pairs += 1
                    elif 300.0 <= ov <= 3000.0 and 60.0 <= d <= 400.0:
                        short_pairs += 1
                        pos_mid = (a[0] + b[0]) / 2.0
                        u_mid = (max(a[1], b[1]) + min(a[2], b[2])) / 2.0
                        if _near_other_long(lay, pos_mid * nx + u_mid * ux,
                                            pos_mid * ny + u_mid * uy, long_grid):
                            on_wall += 1
        lines = len(lens) or 1
        long_ratio = (sum(1 for L in lens if L >= 1500.0) / len(lens)) if lens else 0.0
        thick, conc = _mode(gaps, THICK_BAND)
        b = s.get("bbox") or [0, 0, 0, 0]
        cross = max((b[2] - b[0]) / span_w, (b[3] - b[1]) / span_h)
        obs[lay] = stripe
        raw[lay] = {
            "n": s["n"], "types": s["types"], "line_len": float(sum(lens)),
            "line_ratio": len(lens) / n, "long_ratio": round(long_ratio, 3),
            "pair_rate": round(min(1.0, pairs * 2.0 / lines), 3),
            "long_pair_rate": round(min(1.0, long_pairs * 2.0 / lines), 3),
            "short_pair_rate": round(min(1.0, short_pairs * 2.0 / lines), 3),
            "on_wall_ratio": round(on_wall / short_pairs, 3) if short_pairs else 0.0,
            "thickness_mm": thick, "thickness_concentration": round(conc, 3),
            "arc_ratio": s["arcs"] / n, "closed_ratio": s["closed"] / n,
            "fill_ratio": s["fills"] / n, "text_ratio": s["texts"] / n,
            "block_ratio": s["blocks"] / n,
            "cross_span": round(min(1.0, cross), 3),
            "text_height": _median(s["heights"], 0.0),
            "texts_sample": s["texts_sample"], "bbox": s.get("bbox"),
        }

    # ---- 墙的几何判据，全程不看图层名，分两步：
    #
    # ① 先在这张图上量出"墙厚可能是哪几个值"。做法：取**细长线对**（重叠长度
    #    ≥ 6 倍面线间距——踏步≈3 倍、家具围合 1~3 倍、柱≈1 倍，都不够细长，
    #    不参与投票），按配对长度加权做直方图，取若干局部峰。一张图可以有两三种
    #    墙厚（砌体 200、轻钢隔墙 100、幕墙带），所以不假设唯一值。
    # ② 再拿这些候选墙厚逐个审图层：这一层有多少线是"该厚度上的双线"、这些双线
    #    有多细长、整层是不是几乎都是墙线。层自己的厚度不在候选里时（例如楼梯层
    #    的 310），它在候选厚度上的成对长度自然很少，判不成墙。
    #
    # 为什么不用旧的 long_pair_rate：它要求两面线对齐重叠 ≥1500mm，而墙线常被
    # 洞口、柱子、填充打断成短段，这一项永远上不去。实测会所清晰版 WALL 层（真墙）
    # 只得 0.30 分，楼梯踏步层得 0.51 分——判据指反了，所以墙其实是靠图层名撑着
    # 的，名字一乱、或实体被搬到别的层，整层就丢。
    strip_ratio = SC["layer_strip_ratio"]
    voters = [items for lay, items in obs.items()
              if not _annotations(raw.get(lay) or {})]
    hist: dict[int, float] = collections.defaultdict(float)
    for items in voters:
        for d, ov in items:
            if d > 0 and ov >= strip_ratio * d:
                hist[int(d // THICK_BAND)] += ov
    smooth = {k: (0.5 * hist.get(k - 1, 0.0) + hist[k] + 0.5 * hist.get(k + 1, 0.0))
              for k in hist}
    peaks: list[float] = []
    if smooth:
        top = max(smooth.values()) or 1.0
        for k in sorted(smooth):
            if smooth[k] < SC["layer_peak_share"] * top:
                continue
            if smooth[k] < smooth.get(k - 1, 0.0) or smooth[k] < smooth.get(k + 1, 0.0):
                continue
            lo, hi = (k - 1) * THICK_BAND, (k + 2) * THICK_BAND
            acc = wsum = 0.0
            for items in voters:
                for d, ov in items:
                    if lo <= d < hi and ov >= strip_ratio * d:
                        acc += ov
                        wsum += ov * d
            peaks.append((wsum / acc) if acc else (k + 0.5) * THICK_BAND)

    band_lo_r, band_hi_r = SC["layer_band_lo"], SC["layer_band_hi"]
    cons_lo_r, cons_hi_r = SC["layer_consensus_lo"], SC["layer_consensus_hi"]
    out: dict[str, dict] = {}
    for lay, f in raw.items():
        pairs = [(d, ov) for d, ov in (obs.get(lay) or ()) if d > 0]
        thick, hit = 0.0, 0.0                 # 该层最贴合的候选墙厚
        for t in peaks:
            got = sum(ov for d, ov in pairs if cons_lo_r * t <= d <= cons_hi_r * t)
            if got > hit:
                thick, hit = t, got
        cons_pieces = sorted((d, ov) for d, ov in pairs
                             if cons_lo_r * thick <= d <= cons_hi_r * thick) \
            if thick else []
        band_pieces = [(d, ov) for d, ov in pairs
                       if band_lo_r * thick <= d <= band_hi_r * thick] \
            if thick else []
        strip_len = float(sum(ov for _d, ov in band_pieces))
        # 细长比：面积加权中位数。短碎对数量再多也拉不偏，长的墙段说了算。
        aspect, acc, wsum = 0.0, 0.0, 0.0
        half = sum(ov for _d, ov in cons_pieces) / 2.0
        for d, ov in cons_pieces:
            acc += ov
            wsum += ov * (ov / d)
            if acc >= half:
                aspect = wsum / acc
                break
        bb = f.get("bbox") or [0.0, 0.0, 0.0, 0.0]
        diag = math.hypot(bb[2] - bb[0], bb[3] - bb[1]) or 1.0
        gaps = collections.Counter(int(d // THICK_BAND) for d, _ov in band_pieces)
        own = collections.Counter(int(d // THICK_BAND) for d, ov in pairs
                                  if ov >= strip_ratio * d)
        line_len = max(1.0, f.get("line_len") or 1.0)
        # 本层自己的主流间距（诊断 + "薄得不像墙"的否决用）
        f["pair_thickness_mm"] = ((own.most_common(1)[0][0] + 0.5) * THICK_BAND
                                  if own else None)
        f["pair_matched_mm"] = round(thick, 1)        # 对上的候选墙厚（诊断）
        f["pair_peaks_mm"] = [round(t, 1) for t in peaks]
        f["pair_wall_share"] = round(strip_len / line_len, 4)
        f["pair_aspect"] = round(aspect, 1)
        f["pair_density"] = round(strip_len / diag, 3)
        f["pair_band_concentration"] = round(
            (gaps.most_common(1)[0][1] / len(band_pieces)) if band_pieces else 0.0, 3)
        f["annotation_ratio"] = round(
            _annotations(f) / max(1.0, float(f.get("n") or 1.0)), 3)
        out[lay] = f
    return out


def _cells(x0, y0, x1, y1):
    out = []
    for cx in range(int(x0 // CELL), int(x1 // CELL) + 1):
        for cy in range(int(y0 // CELL), int(y1 // CELL) + 1):
            out.append((cx, cy))
    return out


def _near_other_long(lay, px, py, grid, tol=300.0) -> bool:
    """该平行对的中点是否贴在某条**别层**长直线上（窗框线贴着墙线）。"""
    for cx in (int(px // CELL) - 1, int(px // CELL), int(px // CELL) + 1):
        for cy in (int(py // CELL) - 1, int(py // CELL), int(py // CELL) + 1):
            for (olat, x0, y0, x1, y1) in grid.get((cx, cy)) or ():
                if olat == lay:
                    continue
                if _pt_seg(px, py, x0, y0, x1, y1) <= tol:
                    return True
    return False


def _pt_seg(px, py, x0, y0, x1, y1) -> float:
    dx, dy = x1 - x0, y1 - y0
    L = math.hypot(dx, dy) or 1.0
    t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / (L * L)))
    return math.hypot(px - (x0 + t * dx), py - (y0 + t * dy))


def _mode(values, band) -> tuple[float | None, float]:
    vals = [float(v) for v in (values or ())]
    if not vals:
        return None, 0.0
    bins = collections.Counter(int(v // band) for v in vals)
    top, count = bins.most_common(1)[0]
    return round((top + 0.5) * band, 1), count / len(vals)


def _median(vals, default=0.0):
    vals = sorted(float(v) for v in (vals or ()))
    if not vals:
        return default
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _ent_bbox(e):
    try:
        t = e.dxftype()
    except Exception:
        return None
    try:
        if t == "LINE":
            p, q = e.dxf.start, e.dxf.end
            return [min(p.x, q.x), min(p.y, q.y), max(p.x, q.x), max(p.y, q.y)]
        if t in ("TEXT", "MTEXT", "INSERT"):
            p = e.dxf.insert
            return [p.x, p.y, p.x, p.y]
        if t in ("ARC", "CIRCLE"):
            p, r = e.dxf.center, float(e.dxf.radius)
            return [p.x - r, p.y - r, p.x + r, p.y + r]
        if t == "LWPOLYLINE":                    # 图框主体是多段线，必须算进来
            pts = [(float(x), float(y)) for x, y in e.get_points("xy")]
            if pts:
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                return [min(xs), min(ys), max(xs), max(ys)]
        if t == "POLYLINE":
            pts = [(float(v.dxf.location.x), float(v.dxf.location.y)) for v in e.vertices]
            if pts:
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                return [min(xs), min(ys), max(xs), max(ys)]
    except Exception:
        return None
    return None


# ------------------------------------------------------------------ 打分
def _annotations(f: dict) -> int:
    """该层的"描述性"图元数：尺寸标注 + 文字。带这些的层不参与墙厚投票。"""
    types = f.get("types") or {}
    return int(types.get("DIMENSION") or 0) + int(types.get("TEXT") or 0) \
        + int(types.get("MTEXT") or 0)


def _ramp(v, lo, hi) -> float:
    """无量纲量 -> 0~1：``lo`` 以下算 0，``hi`` 以上算 1，中间线性过渡。"""
    try:
        v = float(v)                                  # None / 缺键 -> 0
    except (TypeError, ValueError):
        return 0.0
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


def _in_range(v, lo, hi) -> float:
    if v is None:
        return 0.0
    v = float(v)
    if v < lo or v > hi:
        return 0.0
    return 1.0 - abs(v - (lo + hi) / 2.0) / ((hi - lo) / 2.0) * 0.5


def _geom_scores(f: dict) -> dict:
    """与命名无关的角色打分（0~1）。

    墙/窗靠"长双线 vs 贴在别层长线上的短双线"区分；墙本身另有一套不看名字的
    判据（细长双线占比 × 细长比 × 整层墙线密度），见下方 ``wall`` 的注释。
    """
    text = f["text_ratio"]
    empty = max(0.0, 1.0 - min(1.0, f["line_ratio"] + text + f["closed_ratio"]
                               + f["arc_ratio"] + f["fill_ratio"]))
    return {
        # 墙：四条几何证据，一条都不看名字，所以实体被搬到别的层、或图层名被
        # 改掉时结论不变。
        #   ① 这层的线里有多少落在"墙厚级别的双线"上（见指纹里的 pair_wall_share：
        #      分子只收落在候选墙厚窄带内的配对长度，分母是本层全部线长）
        #   ② 这些双线有多细长（重叠 / 间距：墙 8~27、踏步≈3、柱≈1、家具围合 1~3）
        #   ③ 整层是不是几乎都是墙线（零散装饰线凑不出这个量级）
        #   ④ 规模：面线太少时统计量不可信（只剩十几条线的层会靠几条长线冒充）
        # ①~③ 相乘而非相加：相加会把"某一条特别高"算成墙，实测把楼梯踏步
        # （占比高但一点都不细长）和窗框层（细长且占比高，但整层只有一圈框）
        # 都算成了墙层。
        "wall": min(1.0, f["n"] / (2.0 * SC["layer_min_lines"]))
                * (1.0 - min(1.0, f["text_ratio"]))
                * _ramp(f.get("pair_wall_share"),
                        SC["layer_share_lo"], SC["layer_share_hi"])
                * _ramp(f.get("pair_aspect"), SC["layer_aspect_lo"],
                        SC["layer_aspect_hi"])
                * _ramp(f.get("pair_density"), SC["layer_density_lo"],
                        SC["layer_density_hi"]),
        "window": (0.45 * f["short_pair_rate"] * f["on_wall_ratio"]
                   + 0.30 * f["on_wall_ratio"]
                   + 0.25 * _in_range(f["thickness_mm"], 60, 400)),
        "door": (0.45 * f["arc_ratio"] + 0.30 * f["block_ratio"]
                 + 0.25 * f["short_pair_rate"] * f["on_wall_ratio"]),
        "column": (0.55 * (f["closed_ratio"] + f["fill_ratio"])
                   + 0.25 * _in_range(f["thickness_mm"], 150, 1500)
                   + 0.20 * min(1.0, f["n"] / 200.0)),
        "axis": (0.60 * f["long_ratio"] * f["cross_span"]
                 + 0.40 * (1.0 - f["pair_rate"])),
        "room_name": (0.45 * text + 0.25 * (1.0 if _room_like(f) else 0.0)
                      + 0.15 * (1.0 - f["line_ratio"])),
        "stair": 0.60 * f["arc_ratio"] + 0.25 * f["closed_ratio"] + 0.15 * f["line_ratio"],
        "furniture": (0.45 * f["arc_ratio"]
                      + 0.35 * f["closed_ratio"] * (1.0 - f["fill_ratio"])
                      + 0.20 * f["block_ratio"]),
        "ignore": max(f["fill_ratio"], 0.85 * empty),
    }


def _room_like(f) -> bool:
    for txt in f.get("texts_sample") or ():
        if 1 <= len(txt) <= 12 and not any(ch.isdigit() for ch in txt):
            return True
    return False


def _veto(f: dict, scores: dict) -> list[str]:
    out = []
    if f["fill_ratio"] > 0.8:
        out.extend(["wall", "column"])
    if f["pair_rate"] > 0.7 and f["thickness_concentration"] > 0.5:
        out.extend(["furniture", "ignore"])
    # 带尺寸标注/大批文字的层是在"描述"墙（尺寸线、引线、轴号），不是墙本身。
    # 它们的平行双线在几何上与墙一模一样，只有实体构成能区分开。
    if float(f.get("annotation_ratio") or 0.0) > SC["layer_annotation_veto"]:
        out.append("wall")
    # 比常规墙厚一半还薄的平行线对不是墙（线脚 / 踏步 / 窗框 / 管道）。
    thick = f.get("pair_thickness_mm")
    if thick and float(thick) < SC["wall_min_thickness_ratio"] * DEFAULT_THICK:
        out.append("wall")
    return [r for r in dict.fromkeys(out) if r in scores]


def fuse(geom: dict, prior: dict | None) -> dict:
    """几何指纹 ⊕ 名字先验：等权 0.5 / 0.5，缺一侧时按另一侧归一。"""
    prior = {r: float(v) for r, v in (prior or {}).items() if float(v) > 0}
    if not prior:
        return {r: round(min(1.0, v), 3) for r, v in geom.items()}
    score = {r: 0.5 * v for r, v in geom.items()}
    for role, p in prior.items():
        score[role] = score.get(role, 0.0) + 0.5 * p
    return {r: round(min(1.0, v), 3) for r, v in score.items()}


def infer_roles(fps: dict, *, prior: dict | None = None) -> dict:
    """指纹 + 先验 -> {layer: {roles, confidence, candidates, prior, vetoed, why}}。"""
    prior = prior or {}
    out = {}
    for layer, f in fps.items():
        geom = _geom_scores(f)
        score = fuse(geom, prior.get(layer))
        vetoed = _veto(f, score)
        for role in vetoed:
            score.pop(role, None)
        ranked = sorted(score.items(), key=lambda kv: -kv[1])[:3]
        best_role, best = (ranked[0] if ranked else (None, 0.0))
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        g_best = geom.get(best_role, 0.0) if best_role else 0.0
        p_best = float((prior.get(layer) or {}).get(best_role, 0.0)) if best_role else 0.0
        g_max = max(geom.values() or [0.0])
        base = {"confidence": round(best, 3),
                "margin": round(best - second, 3),
                "candidates": [r for r, v in ranked if v >= HOLD],
                "prior": dict(prior.get(layer) or {}), "vetoed": vetoed,
                "role_scores": dict(sorted(score.items(), key=lambda kv: -kv[1])),
                "geom": {r: round(v, 3) for r, v in sorted(geom.items(),
                                                           key=lambda kv: -kv[1])[:3]}}
        decided = ((best >= ACCEPT and best - second >= MARGIN)
                   or (p_best >= STRONG_PRIOR and g_max - g_best < CONTRADICTION)
                   or (g_best >= STRONG_GEOM and p_best >= 0.25))
        # 几何一票定音（只对墙）：这一层的线本身就长得像墙，而且证据压倒性，
        # 那么"这层叫家具/叫屋顶"的名字先验就是错的。实体被搬到别的图层时正是
        # 这种情况——名字说 A、几何说 B，而几何才是直接证据。
        g_ranked = sorted(((r, v) for r, v in geom.items() if r not in vetoed),
                          key=lambda kv: -kv[1])
        g_role, g_top = (g_ranked[0] if g_ranked else (None, 0.0))
        g_next = g_ranked[1][1] if len(g_ranked) > 1 else 0.0
        forced = (g_role == "wall" and g_top >= SC["layer_geom_force"]
                  and g_top - g_next >= SC["layer_geom_margin"])
        if forced and best_role != "wall":
            # 融合结论不是墙、可几何压倒性说是墙：名字先验在顶牛，让几何定音。
            # 这一步必须在"融合分够高"之前判——否则名字极强（prior=1.0）时
            # 融合分刚好过线（0.501 vs 0.449），错的那个角色就把层抢走了。
            out[layer] = dict(base, roles=["wall"],
                              confidence=round(max(best, g_top), 3),
                              why="geometry_override")
        elif best_role and decided:
            agrees = p_best >= 0.6 and g_best >= 0.3
            out[layer] = dict(base, roles=[best_role],
                              why="agreed" if agrees else "fused")
        elif forced:
            out[layer] = dict(base, roles=["wall"],
                              confidence=round(max(best, g_top), 3),
                              why="geometry_override")
        elif best >= HOLD:
            out[layer] = dict(base, roles=[], why="ambiguous")
        else:
            out[layer] = dict(base, roles=[], why="low")
    return out


def to_profile_layers(roles: dict) -> dict:
    """{layer: roles} -> profile.layers 段（load_plan 认的形状）。"""
    return {name: {"semantics": list(rec["roles"]), "confidence": rec["confidence"],
                   "reason": rec.get("why")} for name, rec in (roles or {}).items()
            if rec.get("roles")}


def summary(roles: dict, prior_result: dict | None = None) -> dict:
    roles = roles or {}
    prior_result = prior_result or {}
    by_role: dict[str, list[str]] = collections.defaultdict(list)
    for layer, rec in roles.items():
        for r in rec.get("roles") or ():
            by_role[r].append(layer)
    ambiguous = [l for l, r in roles.items() if not r.get("roles")]
    return {"schema": "layer-roles-infer/2.0",
            "layers": len(roles),
            "assigned": len([1 for r in roles.values() if r.get("roles")]),
            "ambiguous": len(ambiguous),
            "ambiguous_layers": sorted(ambiguous)[:12],
            "roles": {k: v for k, v in sorted(by_role.items())},
            "vetoes": sum(len(r.get("vetoed") or ()) for r in roles.values()),
            "name_prior": {"status": prior_result.get("status"),
                           "layers": len(prior_result.get("priors") or ())}}
