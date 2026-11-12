"""识别质量指标：硬约束（无阈值）+ 覆盖率 + 本图分布。

口径：
- 覆盖类：房间名覆盖率、面积守恒、立面覆盖率、构件落框率
- 离群类：门的孤儿率/重复率、窗贴墙率
- 分布类：墙长/厚度/门宽的 P50/P90、MAD
所有容差取自本图（墙厚中位数），不写死绝对阈值。
"""
from __future__ import annotations

import statistics

from .base import point_seg_dist


# ---------------------------------------------------------------- 小工具
def _median(vals, default=0.0):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else default


def _pct(vals, q):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return 0.0
    k = max(0, min(len(vals) - 1, int(round(q * (len(vals) - 1)))))
    return vals[k]


def _mad(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return 0.0
    m = statistics.median(vals)
    return statistics.median([abs(v - m) for v in vals])


def _w_se(w):
    g = w.get("geometry") or {}
    return g.get("start"), g.get("end")


def _w_thick(w):
    return float((w.get("attributes") or {}).get("thickness_mm")
                 or (w.get("geometry") or {}).get("thickness") or 0)


def _w_len(w):
    return float((w.get("attributes") or {}).get("length_mm")
                 or (w.get("geometry") or {}).get("length") or 0)


def _w_bbox(w):
    se = _w_se(w)
    if not se[0] or not se[1]:
        return None
    (x1, y1), (x2, y2) = se
    t = _w_thick(w) / 2.0
    return [min(x1, x2) - t, min(y1, y2) - t, max(x1, x2) + t, max(y1, y2) + t]


def _nearest_wall(px, py, walls):
    best = None
    for w in walls:
        se = _w_se(w)
        if not se[0] or not se[1]:
            continue
        d = point_seg_dist(px, py, se[0][0], se[0][1], se[1][0], se[1][1])[0]
        best = d if best is None or d < best else best
    return best


def _door_center(d):
    g = d.get("geometry") or {}
    op = g.get("opening")
    if op:
        return [(op[0][0] + op[1][0]) / 2.0, (op[0][1] + op[1][1]) / 2.0]
    c = g.get("insert") or [0, 0]
    return [c[0], c[1]]


def _window_center(w):
    g = w.get("geometry") or {}
    ax = g.get("axis") or []
    if len(ax) == 2:
        return [(ax[0][0] + ax[1][0]) / 2.0, (ax[0][1] + ax[1][1]) / 2.0]
    c = g.get("center") or g.get("insert") or [0, 0]
    return [c[0], c[1]]


def _span(v):
    (x1, y1), (x2, y2) = v
    if abs(y1 - y2) <= abs(x1 - x2):
        return "H", min(x1, x2), max(x1, x2), (y1 + y2) / 2.0
    return "V", min(y1, y2), max(y1, y2), (x1 + x2) / 2.0


def _opening_inside_wall(seg, walls, thick):
    orient, lo, hi, pos = _span(seg)
    for w in walls:
        se = _w_se(w)
        if not se[0] or not se[1]:
            continue
        wo, wlo, whi, wpos = _span(se)
        if wo != orient:
            continue
        if abs(wpos - pos) > 1.5 * max(thick, 1.0):
            continue
        if wlo - 100 <= lo and hi <= whi + 100:
            return True
    return False


def compute_metrics(components, walls, frames=(), labels=None) -> dict:
    doors = [c for c in components if c["category"] == "door"]
    windows = [c for c in components if c["category"] == "window"]
    rooms = [c for c in components if c["category"] == "room"]
    thick = _median([_w_thick(w) for w in walls], 200.0)

    # ---- 墙
    lens = sorted(_w_len(w) for w in walls)
    tc = {}
    for w in walls:
        t = round(_w_thick(w))
        tc[t] = tc.get(t, 0) + 1
    wall_stats = {
        "count": len(walls),
        "thickness_median": thick,
        "thickness_mode_share": round(max(tc.values()) / len(walls), 3) if walls else None,
        "length_p10_p50_p90": [round(_pct(lens, 0.1)), round(_median(lens)), round(_pct(lens, 0.9))],
        "short_share_lt_quarter_median": round(sum(1 for x in lens if x < 0.25 * _median(lens)) / len(lens), 3)
        if lens else None,
    }

    # ---- 包络与面积守恒
    bbs = [b for b in (_w_bbox(w) for w in walls) if b]
    if bbs:
        env = [min(b[0] for b in bbs), min(b[1] for b in bbs),
               max(b[2] for b in bbs), max(b[3] for b in bbs)]
        env_area = max(1.0, (env[2] - env[0]) * (env[3] - env[1]))
    else:
        env, env_area = None, None
    room_area = sum(float(r["attributes"].get("area_geom_m2") or 0) for r in rooms) * 1e6
    area_unexplained = round(1 - room_area / env_area, 3) if env_area else None

    # ---- 外墙（贴包络边）与立面覆盖
    ext_len = 0.0
    if env:
        for w in walls:
            se = _w_se(w)
            if not se[0]:
                continue
            cx = (se[0][0] + se[1][0]) / 2.0
            cy = (se[0][1] + se[1][1]) / 2.0
            tol = max(2 * thick, 300.0)
            if (abs(cx - env[0]) <= tol or abs(cx - env[2]) <= tol
                    or abs(cy - env[1]) <= tol or abs(cy - env[3]) <= tol):
                ext_len += _w_len(w)
    win_w = sum(float(w["geometry"].get("width") or 0) for w in windows)
    facade_coverage = round(win_w / ext_len, 3) if ext_len else None

    # ---- 门：贴墙 / 孤儿 / 重复
    d_dist, d_centers = [], []
    for d in doors:
        c = _door_center(d)
        d_centers.append((c, float(d["geometry"].get("width") or 0)))
        r = _nearest_wall(c[0], c[1], walls)
        if r is not None:
            d_dist.append(r)
    d_dup = 0
    for i in range(len(d_centers)):
        for j in range(i + 1, len(d_centers)):
            (c1, w1), (c2, w2) = d_centers[i], d_centers[j]
            lim = 0.5 * max(1.0, min(w1 or 1.0, w2 or 1.0))
            if abs(c1[0] - c2[0]) <= lim and abs(c1[1] - c2[1]) <= lim:
                d_dup += 1
    n = len(d_dist) or 1
    door_stats = {
        "count": len(doors),
        "on_wall_rate": round(sum(1 for r in d_dist if r <= 0.5 * thick) / n, 3) if d_dist else None,
        "orphan_rate": round(sum(1 for r in d_dist if r > 10 * thick) / n, 3) if d_dist else None,
        "duplicate_pairs": d_dup,
        "duplicate_rate": round(d_dup / max(1, len(doors)), 3) if doors else None,
        "dist_p50_p90": [round(_pct(d_dist, 0.5), 1), round(_pct(d_dist, 0.9), 1)],
        "width_median": round(_median([float(d["geometry"].get("width") or 0) for d in doors], 0), 1),
    }

    # ---- 窗：贴墙率与立面覆盖
    w_dist = []
    for w in windows:
        c = _window_center(w)
        r = _nearest_wall(c[0], c[1], walls)
        if r is not None:
            w_dist.append(r)
    window_stats = {
        "count": len(windows),
        "on_wall_rate": round(sum(1 for r in w_dist if r <= thick) / len(w_dist), 3) if w_dist else None,
        "facade_coverage": facade_coverage,
        "width_median": round(_median([float(w["geometry"].get("width") or 0) for w in windows], 0), 1),
    }

    # ---- 洞口落墙
    ok = tot = 0
    for c in doors + windows:
        g = c["geometry"]
        seg = g.get("opening") if c["category"] == "door" else g.get("axis")
        if not seg or len(seg) != 2:
            continue
        tot += 1
        if _opening_inside_wall((seg[0], seg[1]), walls, thick):
            ok += 1

    # ---- 房间：覆盖率与面积分布
    leak = [r["attributes"].get("leak_to_outside") for r in rooms]
    room_stats = {
        "count": len(rooms),
        "labels_covered": round(len(rooms) / labels, 3) if labels else None,
        "closed_rate": round(sum(1 for x in leak if not x) / len(leak), 3) if leak else None,
        "area_median": round(_median([r["attributes"].get("area_geom_m2") for r in rooms], 0), 2),
        "area_unexplained": area_unexplained,
        "area_dev_p50_mad": [
            round(_median([r["attributes"].get("area_deviation") for r in rooms
                           if r["attributes"].get("area_deviation") is not None], 0), 3),
            round(_mad([r["attributes"].get("area_deviation") for r in rooms
                        if r["attributes"].get("area_deviation") is not None]), 3),
        ],
    }

    # ---- 范围
    if frames:
        inside = total = 0
        for c in components:
            bb = (c.get("geometry") or {}).get("bbox")
            if not bb:
                continue
            total += 1
            if any(bb[0] >= f[0] - 1000 and bb[1] >= f[1] - 1000
                   and bb[2] <= f[2] + 1000 and bb[3] <= f[3] + 1000 for f in frames if f):
                inside += 1
        scope = {"frames": len(frames),
                 "inside_frame_rate": round(inside / total, 3) if total else None}
    else:
        scope = {"frames": 0, "inside_frame_rate": None}

    return {
        "walls": wall_stats,
        "door": door_stats,
        "window": window_stats,
        "opening_span": {"total": tot,
                         "inside_wall_rate": round(ok / tot, 3) if tot else None},
        "room": room_stats,
        "scope": scope,
    }
