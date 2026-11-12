"""墙体识别：双线墙的面线配对 -> 中心线 + 厚度 + 轮廓。

判据链（全部来自本图原生图元，不含任何图像/模型推理）：
  1) 只取 WALL 图层上的 LINE / LWPOLYLINE 线段，拆成水平面线（H）与竖直面线（V）；
  2) 同坐标面线聚类、沿墙向合并区间；
  3) 两条平行面线间距 ∈ [60,500]mm 且沿墙向重叠 >= 40mm -> 墙候选；
  4) 两面线之间不得夹着第三条平行面线（否则是跨墙误配）；
  5) 两条面线不得来自同一图元（同一多段线自围合的是壁龛/窗套，不是墙）；
  6) 间距 <=30mm 的近重合面线合并（外墙外皮与保温/面层线），取使厚度最大的组合；
  7) 厚度取本图众数值（200/300 及长墙实测 100）；长度 >=3m 的墙不受众数限制；
  8) 候选中心必须落在长墙（>=3m）包络内，剔除贴在墙外的窗套/线脚；
  9) 同厚、同中心线、首尾相接的候选合并为一段墙。
"""
from __future__ import annotations

import collections
import math
import os

from . import geometry as GEO
from .base import cluster_1d, merge_intervals, segments_from, seg_len
from .snapshot import geometry_records
from . import arcwalls as ARC
from . import curtain as CURTAIN

THICK_MIN, THICK_MAX = 60.0, 500.0
TOL = 2.0
MIN_OVERLAP = 40.0
DUP_GAP = 30.0
LONG_WALL = 3000.0
#: 不再用"某张图的墙厚口径"当全局否决：THICK_MIN..THICK_MAX 只是面线配对的搜索范围，
#: 某个厚度能否成墙由 :func:`_thickness_profile` 的簇证据（总长 / 段数）判定。
MIN_CLUSTER_LEN = 1200.0
MIN_CLUSTER_RUNS = 2
#: 中线判定：被第三条线切出的两侧子墙都不小于该值，才认定这是"两堵墙共用中线"
MIDLINE_MIN_SUB = 80.0
ENVELOPE_MARGIN = 60.0
#: 斜墙方向分箱（度）；同箱的线段才进入同一局部坐标系
OBLIQUE_BUCKET = 1.0


# --------------------------------------------------------------------------- 局部坐标
def _basis(wall):
    """墙 -> (dir, nrm)。斜墙读写 ``dir/nrm``，轴向墙由 ``orient`` 推出。"""
    if wall.get("dir") and wall.get("nrm"):
        return tuple(wall["dir"]), tuple(wall["nrm"])
    if wall.get("orient") == "H":
        return (1.0, 0.0), (0.0, 1.0)
    return (0.0, 1.0), (1.0, 0.0)


def _along(wall, point):
    """点的沿墙坐标（H 取 x、V 取 y、斜墙取投影）。"""
    (ux, uy), _ = _basis(wall)
    return ux * point[0] + uy * point[1]


def _world(wall, u, v):
    """局部坐标 -> 世界坐标（``v`` 是绝对法向位置，与 ``center`` 同口径）。"""
    (ux, uy), (nx, ny) = _basis(wall)
    return [u * ux + v * nx, u * uy + v * ny]


def _outline(wall, a, b, thickness, center):
    t2 = thickness / 2.0
    return [_world(wall, a, center - t2), _world(wall, b, center - t2),
            _world(wall, b, center + t2), _world(wall, a, center + t2)]


def _segments_of(snapshot, layers):
    """指定图层的全部线段（模型空间 + 块展开几何）。"""
    wanted = set(layers or ("WALL",))
    for e in geometry_records(snapshot):
        if e.get("type") not in ("LINE", "LWPOLYLINE", "POLYLINE"):
            continue
        if e.get("layer") not in wanted:
            continue
        for (x1, y1, x2, y2) in segments_from(e):
            yield (x1, y1, x2, y2, e["handle"], e["type"], seg_len(x1, y1, x2, y2))


def _collect(snapshot, layers=None):
    """从快照里取指定图层的线段，并按"轴向 / 斜向"分流（不接触 live ezdxf）。"""
    h, v, other = [], [], []
    for (x1, y1, x2, y2, handle, kind, length) in _segments_of(snapshot, layers):
        if length < 8:
            continue
        rec = (x1, y1, x2, y2, handle, kind, length)
        if abs(y2 - y1) <= 1.5 and abs(x2 - x1) > 3:
            h.append(rec)
        elif abs(x2 - x1) <= 1.5 and abs(y2 - y1) > 3:
            v.append(rec)
        else:
            other.append(rec)
    return h, v, other


def _oblique_walls(other, thick_hint=None):
    """Pair oblique wall faces, retaining actual occupied line intervals.

    Previous code used min(lo)/max(hi) for all collinear source lines, silently
    turning door openings into solid wall.  It also compared offsets written in
    slightly different local bases.  Group in one common basis first, then
    intersect individual covered intervals, never the full envelope.
    """
    if not other:
        return []
    from . import linegroups as LG
    records = []
    for x1, y1, x2, y2, handle, kind, length in other:
        records.append({"start": [x1, y1], "end": [x2, y2],
                        "srcs": [{"handle": handle, "type": kind}]})
    grouped = LG.grouped(records, angle_tol=0.3, pos_tol=TOL)
    # grouped() keeps all intervals but normalizes parallel lines to the same
    # direction/normal. Group distinct faces sharing a canonical angle.
    by_angle = collections.defaultdict(list)
    for members in grouped:
        if not members or members[0]['orient'] in ('H', 'V'):
            continue
        faces = []
        for a, b in merge_intervals([(w['lo'], w['hi']) for w in members], tol=1.5):
            srcs = [src for w in members if w['lo'] <= b+1.5 and w['hi'] >= a-1.5
                    for src in w['srcs']]
            faces.append({'p': members[0]['center'], 'a': a, 'b': b, 'srcs': srcs})
        by_angle[round(members[0]['angle'], 6)].append((members[0], faces))
    out = []
    for angle, lines in by_angle.items():
        lines.sort(key=lambda x: x[0]['center'])
        for i, (w1, f1s) in enumerate(lines):
            for w2, f2s in lines[i+1:]:
                t = w2['center'] - w1['center']
                if t > THICK_MAX:
                    break
                if t < THICK_MIN:
                    continue
                if thick_hint and abs(t - thick_hint) > max(80.0, 0.5*thick_hint):
                    continue
                center = (w1['center'] + w2['center']) / 2.0
                geom = {'dir': w1['dir'], 'nrm': w1['nrm']}
                for f1 in f1s:
                    for f2 in f2s:
                        a, b = max(f1['a'], f2['a']), min(f1['b'], f2['b'])
                        if b-a < MIN_OVERLAP:
                            continue
                        # Match the axis-aligned wall rule: the two paired
                        # faces cannot straddle a THIRD visible parallel face
                        # over the same interval.  This removes 225/250mm
                        # phantom walls made by window sill and nested skins.
                        blocked = False
                        for wk, fks in lines[i+1:]:
                            if wk['center'] >= w2['center'] - 1.0:
                                break
                            for fk in fks:
                                ov = min(b,fk['b'])-max(a,fk['a'])
                                if ov >= max(MIN_OVERLAP,0.35*(b-a)):
                                    blocked = True; break
                            if blocked:
                                break
                        if blocked:
                            continue
                        # One source polyline may form both faces of a pocket;
                        # require independent source geometry where available.
                        out.append({
                            'orient': f"O{angle:.0f}", 'angle': angle,
                            'dir': w1['dir'], 'nrm': w1['nrm'],
                            'start': _world(geom, a, center),
                            'end': _world(geom, b, center),
                            'thickness': t, 'length': b-a,
                            'outline': _outline(geom, a, b, t, center),
                            'srcs': f1['srcs'] + f2['srcs'],
                            'caps_n': 0, 'center': center, 'oblique': True})
    return out


def _cluster_faces(group, pos_vals):
    """把同方向、法向偏移接近的斜向线段聚成"面线"。"""
    for center, members in cluster_1d(pos_vals, TOL):
        yield center, [s for s in group if abs(s["pos"] - center) <= TOL]


def _faces(recs, axis):
    vals, ivs = [], {}
    for (x1, y1, x2, y2, hd, ty, L) in recs:
        if axis == 0:
            key, a, b = (y1 + y2) / 2.0, min(x1, x2), max(x1, x2)
        else:
            key, a, b = (x1 + x2) / 2.0, min(y1, y2), max(y1, y2)
        vals.append(key)
        ivs.setdefault(round(key, 3), []).append((a, b, hd, ty))
    out = []
    for center, members in cluster_1d(vals, TOL):
        segs = []
        for m in members:
            segs.extend(ivs.get(round(m, 3), []))
        for (a, b) in merge_intervals([(a, b) for a, b, _, _ in segs], tol=1.5):
            srcs = [{"handle": hd, "type": ty} for aa, bb, hd, ty in segs
                    if aa <= b + 1.5 and bb >= a - 1.5]
            out.append({"p": center, "a": a, "b": b, "srcs": srcs})
    out.sort(key=lambda f: f["p"])
    return out


def _caps(recs):
    out = []
    for (x1, y1, x2, y2, hd, ty, L) in recs:
        if THICK_MIN <= L <= THICK_MAX + 20:
            vert = abs(x2 - x1) <= 1.5
            out.append({"p": (x1 + x2) / 2.0 if vert else (y1 + y2) / 2.0,
                        "lo": min(y1, y2) if vert else min(x1, x2),
                        "hi": max(y1, y2) if vert else max(x1, x2),
                        "handle": hd, "type": ty})
    return out


def _group_faces(faces):
    """把间距 <= DUP_GAP 且沿墙向重叠的面线合并为一组（近重合线）。"""
    n = len(faces)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if faces[j]["p"] - faces[i]["p"] > DUP_GAP:
                break
            ov = min(faces[i]["b"], faces[j]["b"]) - max(faces[i]["a"], faces[j]["a"])
            short = min(faces[i]["b"] - faces[i]["a"], faces[j]["b"] - faces[j]["a"])
            if short > 0 and ov >= 0.5 * short:
                parent[find(j)] = find(i)
    groups = collections.defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    out = []
    for idxs in groups.values():
        fs = [faces[i] for i in idxs]
        out.append({"members": fs, "pmin": min(f["p"] for f in fs),
                    "pmax": max(f["p"] for f in fs),
                    "a": min(f["a"] for f in fs), "b": max(f["b"] for f in fs),
                    "p": sum(f["p"] for f in fs) / len(fs)})
    out.sort(key=lambda g: g["p"])
    return out


def _pair_intervals(g1, g2, merge_gap=100.0):
    """两组面线的"真实覆盖区间"交集。

    墙被门窗、柱、构造柱切碎时，两面线只在局部同时存在；只在**两边都有面线**的
    位置成墙候选，中间空段原样保留为缺口（洞口假设），不填成实体墙。
    """
    out = []
    for f1 in g1["members"]:
        for f2 in g2["members"]:
            a, b = max(f1["a"], f2["a"]), min(f1["b"], f2["b"])
            if b - a >= MIN_OVERLAP:
                out.append([a, b])
    if not out:
        return []
    out.sort()
    merged = [out[0]]
    for a, b in out[1:]:
        if a - merged[-1][1] <= merge_gap:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def _split_by_middle(groups, g1, g2, a, b):
    """两面线之间若还有一条线把区间切成两段"真墙"，当前这对就是跨中线的外层配对。

    判据是两侧子厚度都不小于 ``MIDLINE_MIN_SUB``：贴在面线内侧 20~40mm 的填充/面层
    边界线不会触发，而居中画的中线（300 墙分 150/150）会触发。
    """
    span = b - a
    if span <= 0:
        return False
    for g in groups:
        if not (g1["pmax"] + DUP_GAP < g["p"] < g2["pmin"] - DUP_GAP):
            continue
        if g["p"] - g1["pmax"] < MIDLINE_MIN_SUB or g2["pmin"] - g["p"] < MIDLINE_MIN_SUB:
            continue
        for f in g["members"]:
            if min(b, f["b"]) - max(a, f["a"]) >= 0.6 * span:
                return True
    return False


def _thickness_profile(cands):
    """墙厚分布 -> 厚度簇证据。返回 (簇统计, 可用簇集合)。

    证据 = 该厚度簇的候选总长与段数：370mm 的墙不会因为别的图用 300mm 就被否决，
    孤立的配对伪影也会因为总长/段数不够而被排除。
    """
    buckets = collections.defaultdict(lambda: {"len": 0.0, "n": 0, "caps": 0})
    for w in cands:
        t = int(round(w["thickness"] / 10.0)) * 10
        b = buckets[t]
        b["len"] += max(0.0, w["b"] - w["a"])
        b["n"] += 1
        b["caps"] += len(w.get("caps") or ())
    total = sum(v["len"] for v in buckets.values()) or 1.0
    ok = {t for t, v in buckets.items()
          if v["n"] >= MIN_CLUSTER_RUNS
          and v["len"] >= max(MIN_CLUSTER_LEN, 0.01 * total)}
    # 弱簇 = 有候选但证据不足以独立成"主流厚度"。它**不再是存在性否决**，
    # 只作为排序证据：弱墙可以进墙网、可以参与局部求解，但不能自动 confirmed。
    weak = {t for t in buckets if t not in ok}
    out = {str(t): {"len": round(v["len"], 1), "n": v["n"], "caps": v["caps"],
                    "ok": t in ok, "weak": t in weak}
           for t, v in sorted(buckets.items())}
    return out, ok, weak


def extract_walls(snapshot, layers=None, allow_single_line=False, allow_oblique=True):
    """墙体识别：输入为 ``DrawingSnapshot``（纯 Python 数据）。

    ``allow_single_line``：双线配对失败时，把长墙线当作单线墙（画法 A/B 兜底）。
    ``allow_oblique``：非轴向（斜墙 / 旋转图纸）线段走局部坐标系的同一套双线
    配对，不再被"只认 H/V"直接丢弃。
    """
    h, v, other = _collect(snapshot, layers)
    hf = _faces(h, 0)
    vf = _faces(v, 1)
    hcaps = _caps(v)
    vcaps = _caps(h)
    hg = _group_faces(hf)
    vg = _group_faces(vf)

    cands = []

    def _scan(groups, caps, orient):
        for i, g1 in enumerate(groups):
            for g2 in groups[i + 1:]:
                if g2["pmin"] - g1["pmax"] > THICK_MAX:
                    break
                t = g2["pmax"] - g1["pmin"]
                if t < THICK_MIN or t > THICK_MAX:
                    continue
                f1 = min(g1["members"], key=lambda f: f["p"])
                f2 = max(g2["members"], key=lambda f: f["p"])
                if {s["handle"] for s in f1["srcs"]} == {s["handle"] for s in f2["srcs"]}:
                    continue
                # 共线支撑 + 真实区间：只在两面线都存在的区段成墙，缺口保留
                for a, b in _pair_intervals(g1, g2):
                    if _split_by_middle(groups, g1, g2, a, b):
                        continue
                    cs = [c for c in caps if abs(c["p"] - a) <= 8
                          and abs(c["lo"] - f1["p"]) <= 8 and abs(c["hi"] - f2["p"]) <= 8]
                    cs += [c for c in caps if abs(c["p"] - b) <= 8
                           and abs(c["lo"] - f1["p"]) <= 8 and abs(c["hi"] - f2["p"]) <= 8]
                    cands.append({"orient": orient, "center": (f1["p"] + f2["p"]) / 2.0,
                                  "thickness": f2["p"] - f1["p"], "a": a, "b": b,
                                  "face1": f1, "face2": f2, "caps": cs})

    _scan(hg, hcaps, "H")
    _scan(vg, vcaps, "V")

    uniq = []
    for w in sorted(cands, key=lambda w: (w["orient"], round(w["center"], 1), w["a"])):
        if any(u["orient"] == w["orient"] and abs(u["center"] - w["center"]) <= TOL
               and abs(u["thickness"] - w["thickness"]) <= TOL
               and abs(u["a"] - w["a"]) <= TOL and abs(u["b"] - w["b"]) <= TOL for u in uniq):
            continue
        uniq.append(w)

    hist = collections.Counter(round(w["thickness"]) for w in uniq)
    top = max(hist.values()) if hist else 0
    dominant = {t for t, c in hist.items() if c >= 0.3 * top}
    clusters, cluster_ok, cluster_weak = _thickness_profile(uniq)

    # 包络：众数厚度且长度 >=600mm 的候选（含被窗洞切碎的外墙段），取轮廓外接矩形
    envsrc = [w for w in uniq if round(w["thickness"]) in dominant and (w["b"] - w["a"]) >= 600]
    if envsrc:
        xs, ys = [], []
        for w in envsrc:
            t2 = w["thickness"] / 2.0
            if w["orient"] == "H":
                xs += [w["a"], w["b"]]
                ys += [w["center"] - t2, w["center"] + t2]
            else:
                ys += [w["a"], w["b"]]
                xs += [w["center"] - t2, w["center"] + t2]
        env = [min(xs), min(ys), max(xs), max(ys)]
    else:
        env = [-1e18, -1e18, 1e18, 1e18]

    kept, dropped = [], []
    for w in uniq:
        cx = w["center"] if w["orient"] == "V" else (w["a"] + w["b"]) / 2
        cy = w["center"] if w["orient"] == "H" else (w["a"] + w["b"]) / 2
        reason = None
        if (cx < env[0] - ENVELOPE_MARGIN or cx > env[2] + ENVELOPE_MARGIN
                or cy < env[1] - ENVELOPE_MARGIN or cy > env[3] + ENVELOPE_MARGIN):
            reason = "包络外（贴墙的窗套/线脚，非墙）"
        if reason:
            w["drop_reason"] = reason
            dropped.append(w)
            continue
        t_cluster = int(round(w["thickness"] / 10.0)) * 10
        if t_cluster in cluster_ok:
            w["evidence_state"] = "strong"
            w["thickness_cluster"] = "strong"
        else:
            # 弱厚度簇：保留（进入墙网与局部求解），但标弱证据。
            # 以前这里直接丢弃，孤立但真实的短墙就永远进不了主墙网，
            # 房间边界随之断开、房间被漏检——连锁漏检的源头之一。
            w["evidence_state"] = "weak"
            w["thickness_cluster"] = "weak"
            w["weak_reason"] = (f"厚度 {w['thickness']:.0f}mm 的簇总长/段数不足"
                                f"（{len(w.get('caps') or ())} 个端头），"
                                f"按弱证据保留待后续连接/柱/空间证据确认")
        kept.append(w)

    # 与别的候选共用面线的是面层/装饰线（如外墙外皮与 300 墙内皮共用），不是独立墙。
    # 保留谁不再"唯厚度"：以厚度簇的证据强度（该厚度的总长）排序，证据强的先占位，
    # 这样"300 主墙 + 贴面 400 线对"时留下 300，而不是留更厚的那个。
    support = {int(t): v["len"] for t, v in clusters.items()}

    def _rank(w):
        t = int(round(w["thickness"] / 10.0)) * 10
        return (support.get(t, 0.0), w["thickness"])

    kept.sort(key=_rank, reverse=True)
    accepted = []
    for w in kept:
        dup = None
        for a in accepted:
            if a["orient"] != w["orient"]:
                continue
            for p1 in (w["face1"]["p"], w["face2"]["p"]):
                for p2 in (a["face1"]["p"], a["face2"]["p"]):
                    if abs(p1 - p2) <= 5:
                        ov = min(w["b"], a["b"]) - max(w["a"], a["a"])
                        if ov > 0.5 * max(w["b"] - w["a"], 1):
                            dup = a
                            break
                if dup:
                    break
            if dup:
                break
        if dup:
            w["drop_reason"] = "与更厚墙共用面线（面层/装饰线，非独立墙）"
            dropped.append(w)
        else:
            accepted.append(w)
    kept = accepted

    kept.sort(key=lambda w: (w["orient"], round(w["center"], 1), w["a"]))
    merged = []
    for w in kept:
        hit = None
        for m in merged:
            if (m["orient"] == w["orient"] and abs(m["center"] - w["center"]) <= TOL
                    and abs(m["thickness"] - w["thickness"]) <= TOL
                    and not (w["b"] < m["a"] - 2 or w["a"] > m["b"] + 2)):
                hit = m
                break
        if hit:
            hit["a"] = min(hit["a"], w["a"])
            hit["b"] = max(hit["b"], w["b"])
            hit["caps"] = hit["caps"] + w["caps"]
            hit["face1"] = {"p": hit["face1"]["p"], "a": hit["a"], "b": hit["b"],
                            "srcs": hit["face1"]["srcs"] + w["face1"]["srcs"]}
            hit["face2"] = {"p": hit["face2"]["p"], "a": hit["a"], "b": hit["b"],
                            "srcs": hit["face2"]["srcs"] + w["face2"]["srcs"]}
        else:
            merged.append(dict(w))

    out = []
    for m in merged:
        t = m["thickness"]
        wall = {"orient": m["orient"]}
        p1 = _world(wall, m["a"], m["center"])
        p2 = _world(wall, m["b"], m["center"])
        outline = _outline(wall, m["a"], m["b"], t, m["center"])
        srcs, seen = [], set()
        for f in (m["face1"], m["face2"]):
            for s in f["srcs"]:
                if s["handle"] in seen:
                    continue
                seen.add(s["handle"])
                srcs.append({"handle": s["handle"], "type": s["type"], "layer": "WALL", "role": "face"})
        for c in m["caps"]:
            if c["handle"] in seen:
                continue
            seen.add(c["handle"])
            srcs.append({"handle": c["handle"], "type": c["type"], "layer": "WALL", "role": "cap"})
        out.append({"orient": m["orient"], "start": p1, "end": p2, "thickness": t,
                    "length": m["b"] - m["a"], "outline": outline, "srcs": srcs,
                    "caps_n": len(m["caps"]), "center": m["center"],
                    "angle": 0.0 if m["orient"] == "H" else 90.0,
                    # 证据状态必须跟着结果走：以前这两项在最后重建记录时被丢掉，
                    # 下游（assemble 的弱墙降级、候选过滤）读到的永远是 None。
                    "faces": 2,
                    "evidence_state": m.get("evidence_state") or "strong",
                    "thickness_cluster": m.get("thickness_cluster"),
                    "weak_reason": m.get("weak_reason")})
    out.sort(key=lambda w: (w["orient"], w["center"], _along(w, w["start"])))

    oblique = _oblique_walls(other, thick_hint=(sorted(
        w["thickness"] for w in out)[len(out) // 2] if out else None)) if allow_oblique else []
    oblique = [w for w in oblique if THICK_MIN <= w["thickness"] <= THICK_MAX]
    for w in oblique:
        w.setdefault("faces", 2)
        w.setdefault("evidence_state", "strong")
        w.setdefault("thickness_cluster", "strong")
    if oblique:
        out.extend(oblique)
        out.sort(key=lambda w: (w["orient"], w["center"], _along(w, w["start"])))
    diag = {"h_faces": len(hf), "v_faces": len(vf), "diag_segments": len(other),
            "candidates": len(uniq), "dropped": len(dropped),
            "dominant_thickness": sorted(dominant), "envelope": env,
            "thickness_clusters": clusters,
            "cluster_rejected": sum(1 for x in dropped
                                    if "厚度簇证据不足" in (x.get("drop_reason") or "")),
            "oblique_walls": len(oblique)}

    if allow_single_line:
        wanted = set(layers or ("WALL",))
        used = {s.get("handle") for w in out for s in w.get("srcs", []) if s.get("handle")}
        ts = sorted(w["thickness"] for w in out)
        t = ts[len(ts) // 2] if ts else 100.0
        added = 0
        for e in geometry_records(snapshot):
            if e.get("type") != "LINE" or e.get("layer") not in wanted:
                continue
            if e["handle"] in used:
                continue
            x1, y1 = e["start"]
            x2, y2 = e["end"]
            L = seg_len(x1, y1, x2, y2)
            if L < 1000:
                continue
            if abs(y1 - y2) <= 1.5:
                orient, center, a, b = "H", y1, min(x1, x2), max(x1, x2)
            elif abs(x1 - x2) <= 1.5:
                orient, center, a, b = "V", x1, min(y1, y2), max(y1, y2)
            else:
                continue
            if orient == "H":
                p1, p2 = [a, center], [b, center]
                outline = [[a, center - t / 2], [b, center - t / 2],
                           [b, center + t / 2], [a, center + t / 2]]
            else:
                p1, p2 = [center, a], [center, b]
                outline = [[center - t / 2, a], [center - t / 2, b],
                           [center + t / 2, b], [center + t / 2, a]]
            out.append({"orient": orient, "start": p1, "end": p2, "thickness": t,
                        "length": b - a, "outline": outline,
                        "srcs": [{"handle": e["handle"], "type": "LINE"}],
                        "caps_n": 0, "center": center,
                        "segments": [[round(a, 1), round(b, 1)]], "single_line": True})
            added += 1
        diag["single_line_added"] = added

    return out, dropped, diag


def openings_from(doors, windows):
    """把门/窗实例转成 (orient, pos, lo, hi) 洞口区间。"""
    out = []
    for item in list(doors) + list(windows):
        seg = item.get("opening") or item.get("axis")
        if not seg:
            continue
        (x1, y1), (x2, y2) = seg
        if abs(y1 - y2) <= abs(x1 - x2):
            out.append(("H", (y1 + y2) / 2.0, min(x1, x2), max(x1, x2)))
        else:
            out.append(("V", (x1 + x2) / 2.0, min(y1, y2), max(y1, y2)))
    return out


#: 证据强度秩（合并时取**最强**的一段：一段有双线证据的 20m 墙接了一个 500mm
#: 的待验证段，整段仍然是真墙；反过来只有"整段都待验证"才保持 pending）
_EVIDENCE_RANK = {"strong": 2, "weak": 1, "pending": 0}


def _merge_state(a, b) -> str:
    """两个证据状态合并后的状态（取更强的一个）。"""
    ra = _EVIDENCE_RANK.get(a or "strong", 2)
    rb = _EVIDENCE_RANK.get(b or "strong", 2)
    return a if ra >= rb else b


def _role_of(role_lens, total):
    """一段 run 的角色与幕墙占比。

    同一条墙线上"幕墙段 + 实体墙段"会被合并成一道墙（它们位置相同，本来就该是
    一道）。此时不能一刀切：占比 >= 50% 才把整道标成幕墙，同时始终记录占比，
    避免"合并把幕墙吃掉"或"一小段幕墙把整道墙染色"。
    """
    total = float(total or 0.0)
    lens = {k: float(v or 0.0) for k, v in (role_lens or {}).items()}
    curtain = lens.get("curtain", 0.0)
    if total <= 0 or not lens:
        return None, 0.0
    share = round(curtain / total, 3)
    if share >= 0.5:
        return "curtain", share
    return None, share


def _arc_runs(arcs, min_length=0.0):
    """弧墙直通：它们已经是完整一道墙，没有"同线相邻段"可合并。

    绝不能进 ``_run_groups``——那里按 ``orient``/``center`` 分组后用
    ``_outline`` 重新生成**直线**轮廓，圆弧会被抹成弦。
    """
    out = []
    for w in arcs or ():
        if float(w.get("length") or 0.0) < float(min_length or 0.0):
            continue
        rec = dict(w)
        rec["arc"] = True
        rec.setdefault("caps_n", 0)
        rec.setdefault("evidence_state", "strong")
        rec.setdefault("geom_score", 0.95)
        rec.setdefault("topo_score", 0.6)
        rec["segments"] = rec.get("segments") or [[0.0, round(float(w.get("length") or 0.0), 1)]]
        out.append(rec)
    return out


def rect_of(item, *, default_t=200.0):
    """从各种记录形状里取出轴对齐包围盒 ``(x0, y0, x1, y1)``。"""
    for key in ("bbox", "bbox_model", "box"):
        b = item.get(key)
        if b and len(b) == 4:
            return (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
    c = item.get("center")
    # 墙记录的 center 是**标量**（法向位置），柱记录的 center 才是 [x, y]
    if isinstance(c, (list, tuple)) and len(c) >= 2:
        w = float(item.get("width") or item.get("depth") or default_t)
        h = float(item.get("depth") or item.get("width") or default_t)
        return (float(c[0]) - w / 2.0, float(c[1]) - h / 2.0,
                float(c[0]) + w / 2.0, float(c[1]) + h / 2.0)
    pts = item.get("outline") or item.get("polygon") or ()
    if pts:
        xs = [float(p[0]) for p in pts]
        ys = [float(p[1]) for p in pts]
        return (min(xs), min(ys), max(xs), max(ys))
    a, b = item.get("start"), item.get("end")
    if a and b:
        t2 = float(item.get("thickness") or default_t) / 2.0
        return (min(a[0], b[0]) - t2, min(a[1], b[1]) - t2,
                max(a[0], b[0]) + t2, max(a[1], b[1]) + t2)
    return None


def _seg_hits_box(p, q, box, *, eps=1.0):
    """线段 pq 是否穿过轴对齐盒子（只碰端点不算：同一道墙的两段会在端点相接）。"""
    x0, y0, x1, y1 = box[0] + eps, box[1] + eps, box[2] - eps, box[3] - eps
    if x1 <= x0 or y1 <= y0:
        return False
    # 取样：线段一般是轴向的（墙中心线），沿线段每 1mm 太多，用参数化区间求交
    (px, py), (qx, qy) = p, q
    dx, dy = qx - px, qy - py
    t0, t1 = 0.0, 1.0
    # Liang–Barsky：p·t <= q
    for pnum, qnum in ((-dx, px - x0), (dx, x1 - px),
                       (-dy, py - y0), (dy, y1 - py)):
        if abs(pnum) < 1e-12:
            if qnum < 0:
                return False
            continue
        t = qnum / pnum
        if pnum < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return False
    return (t1 - t0) > 1e-9


def merge_collinear(walls, *, blockers=(), max_gap=3000.0, min_length=250.0,
                    off_tol_ratio=0.5) -> list:
    """把"一面墙"接成一个实例：同一条中心线、同厚度、缺口里没有柱子/横墙。

    口径（用户给定，与人工算量一致）：

    * 一堵墙开了门或窗，仍然是**一堵墙**——门窗只是墙上的洞；
    * 只有被**柱子**或**另一道墙**拦住才断开。

    与 :func:`merge_across_openings` 的分工：那边要求缺口被"已采纳的门窗洞口"
    覆盖，所以门没识别出来时同一面墙仍会碎成两段；这边不看缺口里是什么，
    只看缺口里有没有柱子/横墙挡着。故本函数必须在门窗合并之后跑。

    ``blockers`` 是柱等占位物（取 ``bbox``/``center+width``/``outline`` 任一）；
    横墙直接从 ``walls`` 自己身上取，不需要外部传。
    """
    arcs = [w for w in walls if w.get("arc")]
    body = [w for w in walls if not w.get("arc")]
    boxes = [b for b in (rect_of(x) for x in blockers) if b]
    boxes += [b for b in (rect_of(w) for w in body) if b]

    groups: dict[tuple, list] = {}
    for w in body:
        groups.setdefault((w["orient"], round(float(w["center"]), 1)), []).append(w)

    runs = []
    for (_orient, _center), items in groups.items():
        items.sort(key=lambda w: _along(w, w["start"]))
        cur = None
        for w in items:
            a, b = _along(w, w["start"]), _along(w, w["end"])
            if cur is None:
                cur = {"wall": w, "a": a, "b": b,
                       "segments": [list(x) for x in (w.get("segments") or [[a, b]])],
                       "merged": []}
                continue
            gap = a - cur["b"]
            same_t = abs(float(w["thickness"]) - float(cur["wall"]["thickness"])) <= 2.0
            if 0.0 <= gap <= max_gap and same_t:
                p = _world(cur["wall"], cur["b"], cur["wall"]["center"])
                q = _world(w, a, w["center"])
                hit = next((bx for bx in boxes if _seg_hits_box(p, q, bx)), None)
                if hit is None:
                    cur["b"] = max(cur["b"], b)
                    cur["segments"].extend(
                        [list(x) for x in (w.get("segments") or [[a, b]])])
                    cur["merged"].append(round(gap, 1))
                    continue
            runs.append(cur)
            cur = {"wall": w, "a": a, "b": b,
                   "segments": [list(x) for x in (w.get("segments") or [[a, b]])],
                   "merged": []}
        if cur is not None:
            runs.append(cur)

    out = []
    for run in runs:
        w = run["wall"]
        if run["b"] - run["a"] < min_length:
            continue
        rec = dict(w)
        center = float(w["center"])
        rec.update({
            "start": _world(w, run["a"], center),
            "end": _world(w, run["b"], center),
            "length": run["b"] - run["a"],
            "outline": _outline(w, run["a"], run["b"], float(w["thickness"]), center),
            "segments": run["segments"],
            "merged_gaps": run["merged"],
            "srcs": list(w.get("srcs") or ()),
        })
        out.append(rec)
    out.extend(_arc_runs(arcs))
    out.sort(key=lambda w: (w["orient"], float(w.get("center") or 0.0),
                            _along(w, w["start"])))
    return out


def _run_groups(walls):
    """Group same-line wall spans, including oblique segments with jitter."""
    from . import linegroups as LG
    axes, oblique = {}, []
    for w in walls:
        if w.get('orient') in ('H', 'V'):
            axes.setdefault((w['orient'], round(w['center'],1)), []).append(w)
        else:
            oblique.append(w)
    for family in LG.grouped(oblique, angle_tol=0.3, pos_tol=2.5):
        w = family[0]
        axes[(w['orient'], round(w['center'],3))] = family
    return axes


def merge_runs(walls, openings=(), max_gap=1000.0, slack=120.0, min_length=250.0):
    """把同一墙线上的墙段合并为"一段墙"（wall run）。

    口径（与人工标注一致）：
      - **门窗洞口处断开**：一堵墙开了窗，窗两侧是两个墙垛，不是一堵墙；
      - **柱位、面线错位等非洞口缺口合并**：墙穿柱而过仍是同一堵墙。
    因此合并条件是「缺口 <= max_gap 且缺口未被任何门窗洞口覆盖」。
    """
    arcs = [w for w in walls if w.get("arc")]
    walls = [w for w in walls if not w.get("arc")]
    groups = _run_groups(walls)

    def covered(orient, pos, a, b):
        for (o, p, lo, hi) in openings:
            if o == orient and abs(p - pos) <= slack and lo <= a + slack and hi >= b - slack:
                return True
        return False

    runs = []
    for (orient, center), items in groups.items():
        items.sort(key=lambda w: _along(w, w["start"]))
        cur = None
        for w in items:
            a = _along(w, w["start"])
            b = _along(w, w["end"])
            if cur is not None:
                gap = a - cur["b"]
                if 0 <= gap <= max_gap and not covered(orient, center, cur["b"], a):
                    cur["b"] = max(cur["b"], b)
                    cur["segments"].append([round(a, 1), round(b, 1)])
                    cur["srcs"].extend(w["srcs"])
                    rl = cur.setdefault("role_lens", {})
                    rl[w.get("role")] = rl.get(w.get("role"), 0.0) + (b - a)
                    cur["caps_n"] += w["caps_n"]
                    cur["state"] = _merge_state(cur.get("state"),
                                           w.get("evidence_state") or "strong")
                    cur["geom_score"] = max(cur.get("geom_score") or 0.0,
                                            float(w.get("geom_score") or 0.0))
                    cur["topo_score"] = max(cur.get("topo_score") or 0.0,
                                            float(w.get("topo_score") or 0.0))
                    continue
            cur = {"orient": orient, "center": center, "a": a, "b": b,
                   "thickness": w["thickness"], "srcs": list(w["srcs"]),
                   "dir": w.get("dir"), "nrm": w.get("nrm"),
                   "role_lens": {w.get("role"): b - a},
                   "caps_n": w["caps_n"], "segments": [[round(a, 1), round(b, 1)]],
                   "state": w.get("evidence_state") or "strong",
                   "geom_score": float(w.get("geom_score") or 0.0),
                   "topo_score": float(w.get("topo_score") or 0.0)}
            runs.append(cur)

    out = []
    for r in runs:
        if r["b"] - r["a"] < min_length:      # 50~225mm 的残段是配对噪声，不是墙
            continue
        t = r["thickness"]
        p1 = _world(r, r["a"], r["center"])
        p2 = _world(r, r["b"], r["center"])
        outline = _outline(r, r["a"], r["b"], t, r["center"])
        role, share = _role_of(r.get("role_lens"), r["b"] - r["a"])
        out.append({"orient": r["orient"], "start": p1, "end": p2,
                    "thickness": t, "length": r["b"] - r["a"], "outline": outline,
                    "role": role, "band": bool(role),
                    "curtain_share": share,
                    "srcs": r["srcs"], "caps_n": r["caps_n"], "center": r["center"],
                    "dir": r.get("dir"), "nrm": r.get("nrm"),
                    "segments": r["segments"],
                    "evidence_state": r.get("state") or "strong",
                    "geom_score": round(float(r.get("geom_score") or 0.0), 3),
                    "topo_score": round(float(r.get("topo_score") or 0.0), 3)})
    out.extend(_arc_runs(arcs, min_length))
    out.sort(key=lambda w: (w["orient"], float(w.get("center") or 0.0),
                            _along(w, w["start"])))
    return out


def _opening_record(op):
    """wall run 上的洞口记录（JSON-native，供 attributes.openings 使用）。"""
    return {"category": op["category"], "id": op["id"],
            "width": op.get("width"), "span": [op["lo"], op["hi"]],
            "type_code": op.get("type_code"), "handle": op.get("handle"),
            "layer": op.get("layer"), "block": op.get("block")}


def merge_across_openings(walls, openings, max_gap=3000.0, slack=120.0,
                          min_length=250.0):
    """把同一墙线上被门窗洞口覆盖的缺口合并成连续墙实例（wall run）。

    与 :func:`merge_runs` 的区别：``merge_runs`` 遇到门窗洞口就断开（房间/台阶/
    散水仍用它）；本函数反过来把「缺口被门/窗洞口覆盖且缺口 <= max_gap」的相邻
    墙段接起来，形成一道连续墙，并把覆盖缺口的那几樘门/窗记录在 ``openings``。

    ``openings`` 的每项需含 ``orient/pos/lo/hi/category/id/width/type_code/
    handle/layer/block``；``pos`` 为墙中心线坐标，``lo/hi`` 为沿墙轴绝对坐标。
    返回值中的 ``segments`` 保留合并前的原子墙段，供组件层展示。
    """
    arcs = [w for w in walls if w.get("arc")]
    walls = [w for w in walls if not w.get("arc")]
    groups = _run_groups(walls)

    def covering(orient, center, a, b):
        out = []
        for op in openings:
            if op.get("orient") != orient:
                continue
            if abs(op["pos"] - center) > slack:
                continue
            if op["lo"] <= a + slack and op["hi"] >= b - slack:
                out.append(op)
        return out

    def new_run(orient, center, wall, a, b):
        segments = wall.get("segments") or [[round(a, 1), round(b, 1)]]
        return {"orient": orient, "center": center, "a": a, "b": b,
                "thickness": wall["thickness"], "srcs": list(wall["srcs"]),
                "role_lens": {wall.get("role"): b - a},
                "caps_n": wall["caps_n"], "dir": wall.get("dir"),
                "nrm": wall.get("nrm"),
                "segments": [list(seg) for seg in segments], "openings": [],
                "state": wall.get("evidence_state") or "strong",
                "geom_score": float(wall.get("geom_score") or 0.0),
                "topo_score": float(wall.get("topo_score") or 0.0)}

    runs = []
    for (orient, center), items in groups.items():
        items.sort(key=lambda w: _along(w, w["start"]))
        cur = None
        for wall in items:
            a = _along(wall, wall["start"])
            b = _along(wall, wall["end"])
            if cur is None:
                cur = new_run(orient, center, wall, a, b)
                continue
            gap = a - cur["b"]
            cover = covering(orient, center, cur["b"], a) if 0 <= gap <= max_gap else []
            # 同中心线但厚度不同（面层/装饰线等）不得合并；当前 merge_runs 已按中心
            # 合并，实际数据无此情况，这里是函数级防御。
            same_thickness = abs(wall["thickness"] - cur["thickness"]) <= 2.0
            if cover and same_thickness:
                cur["b"] = max(cur["b"], b)
                cur["segments"].extend(
                    [list(seg) for seg in (wall.get("segments") or
                                           [[round(a, 1), round(b, 1)]])])
                cur["srcs"].extend(wall["srcs"])
                rl = cur.setdefault("role_lens", {})
                rl[wall.get("role")] = rl.get(wall.get("role"), 0.0) + (b - a)
                cur["caps_n"] += wall["caps_n"]
                cur["state"] = _merge_state(cur.get("state"),
                                       wall.get("evidence_state") or "strong")
                cur["geom_score"] = max(cur.get("geom_score") or 0.0,
                                        float(wall.get("geom_score") or 0.0))
                cur["topo_score"] = max(cur.get("topo_score") or 0.0,
                                        float(wall.get("topo_score") or 0.0))
                for op in cover:
                    if all(item["handle"] != op.get("handle") for item in cur["openings"]):
                        cur["openings"].append(_opening_record(op))
            else:
                runs.append(cur)
                cur = new_run(orient, center, wall, a, b)
        if cur is not None:
            runs.append(cur)

    out = []
    for run in runs:
        if run["b"] - run["a"] < min_length:
            continue
        t = run["thickness"]
        p1 = _world(run, run["a"], run["center"])
        p2 = _world(run, run["b"], run["center"])
        outline = _outline(run, run["a"], run["b"], t, run["center"])
        role, share = _role_of(run.get("role_lens"), run["b"] - run["a"])
        out.append({"orient": run["orient"], "start": p1, "end": p2,
                    "thickness": t, "length": run["b"] - run["a"], "outline": outline,
                    "role": role, "band": bool(role),
                    "curtain_share": share,
                    "srcs": run["srcs"], "caps_n": run["caps_n"],
                    "center": run["center"], "dir": run.get("dir"),
                    "nrm": run.get("nrm"),
                    "segments": run["segments"], "openings": run["openings"],
                    "evidence_state": run.get("state") or "strong",
                    "geom_score": round(float(run.get("geom_score") or 0.0), 3),
                    "topo_score": round(float(run.get("topo_score") or 0.0), 3)})
    out.extend(_arc_runs(arcs))
    out.sort(key=lambda w: (w["orient"], float(w.get("center") or 0.0),
                            _along(w, w["start"])))
    return out


# ===========================================================================
# 两阶段识别：先高召回生成候选，再用几何 + 拓扑联合验证（不在早期永久丢弃）
# ===========================================================================
#: 参与"共线合并"的短线段长度上限（mm）
SHORT_SEG_MAX = 600.0
#: 共线合并允许的最大缝隙（mm）
SHORT_MERGE_GAP = 150.0
#: 合并后至少要有的长度（mm）——太短的合并没有配对价值
SHORT_MERGE_MIN = 1_200.0
#: 端头连接判定容差（mm）：墙端到墙身 / 柱轮廓
END_CONNECT_TOL = 300.0
#: 同一直线上两段墙之间可以补的缝隙上限（mm）：短断口通常是绘制断点
BRIDGE_GAP_MAX = 300.0
#: 单线墙候选的最短长度（mm）
SINGLE_MIN_LEN = 1_500.0
#: 单线墙候选与已识别墙的平行重复判据（mm）
SINGLE_DUP_TOL = 200.0
#: 单线墙候选数量上限
MAX_SINGLE = 400
#: 无独立几何证据（单面线）时，拓扑得分至少要到这个值才允许成为候选墙
TOPO_FLOOR = 0.50
#: 独立几何证据达到这个分数即可单独成立（双面线 + 端头/厚度簇）
GEOM_STRONG = 0.60
#: 独立几何证据的最低门槛；低于它的候选一律拒绝（拓扑不能单独证明墙存在）
GEOM_FLOOR = 0.40
#: 几何 / 拓扑融合权重（各 0.5）
W_GEOM, W_TOPO = 0.5, 0.5


def _wall_ends(w):
    s, e = w.get("start"), w.get("end")
    if not s or not e:
        return []
    return [[float(s[0]), float(s[1])], [float(e[0]), float(e[1])]]


def _pt_seg(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    den = dx * dx + dy * dy
    if den <= 1e-9:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / den))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _end_hits_end(end, other, tol) -> bool:
    for p in _wall_ends(other):
        if math.hypot(end[0] - p[0], end[1] - p[1]) <= tol:
            return True
    return False


def _end_hits_body(end, other, tol) -> bool:
    pts = _wall_ends(other)
    if len(pts) < 2:
        return False
    return _pt_seg(end[0], end[1], pts[0][0], pts[0][1],
                   pts[1][0], pts[1][1]) <= tol


def _end_in_column(end, col, tol) -> bool:
    b = col.get("bbox")
    if not b:
        cx, cy = (col.get("center") or [None, None])[:2]
        if cx is None or cy is None:
            return False
        b = [cx - 1, cy - 1, cx + 1, cy + 1]
    return (b[0] - tol <= end[0] <= b[2] + tol
            and b[1] - tol <= end[1] <= b[3] + tol)


def topology_score(wall, walls, columns=(), tol=END_CONNECT_TOL) -> dict:
    """拓扑支持度：端头是否接到别的墙 / 柱。

    只做"支持"，不决定存在性——拓扑可以把弱候选留下来观察，但单独闭合
    不能证明一堵墙存在（那只是自证）。
    """
    ends = _wall_ends(wall)
    score, notes = 0.0, []
    for i, end in enumerate(ends):
        hit = None
        wid = wall.get("id")
        for other in walls:
            if other is wall:
                continue
            oid = other.get("id")
            # 只有两边都有 id 且相同时才跳过（没有 id 的墙段不能被当成"同一堵墙"）
            if wid is not None and oid is not None and oid == wid:
                continue
            if _end_hits_end(end, other, tol) or _end_hits_body(end, other, tol):
                hit = "wall"
                break
        if hit is None:
            for col in columns or ():
                if _end_in_column(end, col, tol):
                    hit = "column"
                    break
        if hit == "wall":
            score += 0.30
            notes.append(f"end{i}:wall")
        elif hit == "column":
            score += 0.20
            notes.append(f"end{i}:column")
        else:
            notes.append(f"end{i}:free")
    return {"score": round(min(1.0, score), 3), "notes": notes}


def geometry_score(cand) -> dict:
    """独立几何证据（不依赖任何其它构件）：面线数 / 端头封口 / 厚度簇 / 图层。"""
    s, notes = 0.0, []
    nfaces = int(cand.get("faces") or 0)
    if nfaces >= 2:
        s += 0.40
        notes.append(f"faces{nfaces}")
    elif nfaces == 1:
        s += 0.10
        notes.append("face1")
    caps = int(cand.get("caps_n") or 0)
    if caps >= 2:
        s += 0.30
        notes.append("caps2")
    elif caps == 1:
        s += 0.15
        notes.append("caps1")
    cluster = cand.get("thickness_cluster")
    if cluster == "strong":
        s += 0.20
        notes.append("thick_strong")
    elif cluster == "weak":
        s += 0.05
        notes.append("thick_weak")
    if cand.get("layer_ok"):
        s += 0.10
        notes.append("layer")
    return {"score": round(min(1.0, s), 3), "notes": notes}


def validate(cands, *, walls=(), columns=(), region=None, tol=END_CONNECT_TOL) -> dict:
    """阶段 B：几何 ⊗ 拓扑联合验证 -> 接受 / 待定 / 拒绝（带可审计理由）。

    硬规则：**独立几何证据不足的候选，无论拓扑多好都不能通过**——"补上它房间
    就闭合了"只证明拓扑可行，不证明这堵墙真实存在。
    """
    pool = list(walls or ())
    cand_list = list(cands or ())
    # 参考厚度只用"双面线配对"的墙来定：单面线不参与——它们在图上就是一个线段，
    # 拿它们定墙厚会把家具线、立面线的间距也当成墙厚。
    #
    # 而且**必须是多档位**：一张图合法墙厚不止一种（办公楼 = 200 有 81 段 + 300
    # 有 60 段）。只认单一中位数会把整档 300 墙判成"非墙"，实测墙 31→29 破线。
    # 口径：把配对墙按 10mm 归档，段数占比 ≥10% 的档位都算主流档位。
    paired_t = [float(w.get("thickness") or 0) for w in cand_list
                if int(w.get("faces") or 0) >= 2 and float(w.get("thickness") or 0) > 0]
    tiers: list[float] = []
    if paired_t:
        bins: dict[int, int] = {}
        for t in paired_t:
            bins[int(round(t / 10.0)) * 10] = bins.get(int(round(t / 10.0)) * 10, 0) + 1
        floor = max(1, int(0.10 * len(paired_t)))
        tiers = sorted(float(t) for t, n in bins.items() if n >= floor)
    accepted, pending, rejected = [], [], []
    for idx, cand in enumerate(cand_list):
        w = dict(cand)
        w.setdefault("id", f"WC-{idx + 1:03d}")
        if region:
            w.setdefault("region", region)
        geom = geometry_score(w)
        topo = topology_score(w, pool + cand_list, columns=columns, tol=tol)
        total = W_GEOM * geom["score"] + W_TOPO * topo["score"]
        w["geom_score"] = geom["score"]
        w["topo_score"] = topo["score"]
        w["score"] = round(total, 3)
        w["evidence_geom"] = geom["notes"]
        w["evidence_topo"] = topo["notes"]

        faces_n = int(w.get("faces") or 0)
        ends_free = sum(1 for n in (topo.get("notes") or ()) if n.endswith(":free"))

        # ---- 单面线（faces==1）不是"一堵墙"：图上只有一条线。它最多待核，
        # 不能凭"端头有连接"升级成 strong；两端还都自由的，直接判非墙。
        if faces_n <= 1 and ends_free >= 2:
            w["drop_reason"] = "孤立单线：既无配对面线也无端头连接（家具线/立面线/引线的典型特征）"
            w["verify"] = w["drop_reason"]
            rejected.append(w)
            continue

        if geom["score"] >= GEOM_STRONG:
            if faces_n <= 1:
                # 单面线要分清两种：
                #  * 接在双线墙上的 —— 那是同一堵墙的延续（墙端、洞口边），可信；
                #  * 孤立的 —— 家具线/立面线/引线，最多待核。
                # 实测：一刀切降级会让办公楼墙 run 从 31 掉到 29（那 15 段单面线
                # 正是双线墙的延续）。
                paired_near = 0
                for end in _wall_ends(w):
                    for other in cand_list:
                        if other is w or int(other.get("faces") or 0) < 2:
                            continue
                        if (_end_hits_end(end, other, tol)
                                or _end_hits_body(end, other, tol)):
                            paired_near += 1
                            break
                if paired_near == 0:
                    w["evidence_state"] = "pending"
                    w["verify"] = ("单面线且未接双线墙：几何分够但缺配对证据，"
                                   "保留待验证（不得 confirmed）")
                    pending.append(w)
                    continue
                # 接在双线墙上的单面线 = 同一堵墙的延续（墙端/洞口边），
                # 正常收下；否则办公楼这类图会少算两段墙（31→29）。
                w["evidence_state"] = w.get("evidence_state") or "strong"
                w["verify"] = "单面线但接在双线墙上：视为该墙的延续"
                accepted.append(w)
                continue
            w["evidence_state"] = w.get("evidence_state") or "strong"
            w["verify"] = "独立几何证据成立"
            accepted.append(w)
        elif geom["score"] >= GEOM_FLOOR and topo["score"] >= TOPO_FLOOR:
            w["evidence_state"] = "pending"
            w["verify"] = "几何证据弱，靠端头/柱连接保留待验证（不得 confirmed）"
            pending.append(w)
        else:
            why = ("独立几何证据不足（单面线且无端头/厚度簇支持）"
                   if geom["score"] < GEOM_FLOOR
                   else "几何证据弱且缺乏拓扑连接（家具/装饰平行线的典型特征）")
            w["drop_reason"] = why
            w["verify"] = why
            rejected.append(w)
    out_walls = accepted + pending
    for w in out_walls:
        w.pop("drop_reason", None)
    return {"accepted": accepted, "pending": pending, "rejected": rejected,
            "walls": out_walls,
            "stats": {"candidates": len(cands or ()),
                      "accepted": len(accepted), "pending": len(pending),
                      "rejected": len(rejected),
                      "geom_strong": GEOM_STRONG, "topo_floor": TOPO_FLOOR}}


# ------------------------------------------------------------------ 召回补充
def _used_handles(walls, dropped):
    used = set()
    for w in list(walls or []) + list(dropped or []):
        for s in w.get("srcs") or ():
            if s.get("handle"):
                used.add(str(s["handle"]))
    return used


def virtual_from_shorts(snapshot, layers=None, used=(), max_len=SHORT_SEG_MAX,
                        gap=SHORT_MERGE_GAP):
    """短碎线先共线合并，再尝试配对：返回 ``(virtual_snapshot, 映射)``。

    "墙被窗洞/柱/断线切碎"是真实图纸里最常见的漏识别来源：每段都不足以单独
    配对，合起来却是一堵完整的墙。这里只做合并，配对仍复用同一套双线逻辑。
    """
    wanted = set(layers or ("WALL",))
    groups: dict[tuple, list] = {}
    for (x1, y1, x2, y2, handle, kind, length) in _segments_of(snapshot, wanted):
        if str(handle) in used or length < 8 or length > max_len:
            continue
        # Normalize every orientation to a 1-degree local basis. Axial lines
        # retain their legacy exact H/V keys to avoid altering old baselines.
        if abs(y2 - y1) <= 1.5 and abs(x2 - x1) > 3:
            key, lo, hi = ("H", round(y1, 1)), min(x1, x2), max(x1, x2)
        elif abs(x2 - x1) <= 1.5 and abs(y2 - y1) > 3:
            key, lo, hi = ("V", round(x1, 1)), min(y1, y2), max(y1, y2)
        else:
            angle = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180.0
            bucket = round(angle / OBLIQUE_BUCKET) * OBLIQUE_BUCKET
            rad = math.radians(bucket)
            ux, uy = math.cos(rad), math.sin(rad)
            nx, ny = -uy, ux
            pos = (x1 * nx + y1 * ny + x2 * nx + y2 * ny) / 2.0
            a, b = x1 * ux + y1 * uy, x2 * ux + y2 * uy
            key, lo, hi = ("A", round(bucket, 4), round(pos, 1)), min(a, b), max(a, b)
        groups.setdefault(key, []).append((lo, hi, handle, kind))
    entities, mapping = [], {}
    idx = 0
    for key, items in sorted(groups.items()):
        orient, pos = key[0], key[-1]
        if len(items) < 2:
            continue
        items.sort()
        run_lo, run_hi, run_srcs = items[0][0], items[0][1], [items[0]]
        spans = []
        for lo, hi, handle, kind in items[1:]:
            if lo - run_hi <= gap:
                run_hi = max(run_hi, hi)
                run_srcs.append((lo, hi, handle, kind))
                continue
            spans.append((run_lo, run_hi, run_srcs))
            run_lo, run_hi, run_srcs = lo, hi, [(lo, hi, handle, kind)]
        spans.append((run_lo, run_hi, run_srcs))
        for lo, hi, srcs in spans:
            if len(srcs) < 2 or hi - lo < SHORT_MERGE_MIN:
                continue
            idx += 1
            vh = f"virt:{orient}{idx:04d}"
            if orient == "H":
                start, end = [lo, pos], [hi, pos]
            elif orient == "V":
                start, end = [pos, lo], [pos, hi]
            else:
                rad = math.radians(key[1]); ux, uy = math.cos(rad), math.sin(rad)
                nx, ny = -uy, ux
                start = [ux * lo + nx * pos, uy * lo + ny * pos]
                end = [ux * hi + nx * pos, uy * hi + ny * pos]
            entities.append({"handle": vh, "type": "LINE",
                             "layer": (list(layers)[0] if layers else "WALL"),
                             "start": start, "end": end})
            mapping[vh] = [str(s[2]) for s in srcs]
    return {"entities": entities, "derived": [], "blocks": {}, "header": {}}, mapping


def single_line_candidates(snapshot, layers=None, thickness=200.0, used=(),
                           walls=(), min_len=SINGLE_MIN_LEN):
    """单线弱墙候选：长墙线但找不到配对（画法 A/B、外墙只画一条皮线）。"""
    wanted = set(layers or ("WALL",))
    out = []
    for (x1, y1, x2, y2, handle, kind, length) in _segments_of(snapshot, wanted):
        if str(handle) in used or length < min_len:
            continue
        if abs(y2 - y1) <= 1.5:
            orient, pos, lo, hi = "H", y1, min(x1, x2), max(x1, x2)
            geom = {"orient": "H"}
        elif abs(x2 - x1) <= 1.5:
            orient, pos, lo, hi = "V", x1, min(y1, y2), max(y1, y2)
            geom = {"orient": "V"}
        else:
            angle = math.atan2(y2 - y1, x2 - x1) % math.pi
            ux, uy = math.cos(angle), math.sin(angle)
            nx, ny = -uy, ux
            pos = (x1 * nx + y1 * ny + x2 * nx + y2 * ny) / 2
            lo, hi = sorted((x1 * ux + y1 * uy, x2 * ux + y2 * uy))
            orient = f"O{math.degrees(angle):.0f}"
            geom = {"orient": orient, "dir": [ux, uy], "nrm": [nx, ny]}
        dup = False
        for w in walls or ():
            if w.get("orient") != orient:
                continue
            if abs(float(w.get("center") or 0.0) - pos) > SINGLE_DUP_TOL:
                continue
            a, b = _along(w, w["start"]), _along(w, w["end"])
            if min(b, hi) - max(a, lo) > 0.3 * (hi - lo):
                dup = True
                break
        if dup:
            continue
        start = _world(geom, lo, pos)
        end = _world(geom, hi, pos)
        out.append({"orient": orient, "dir": geom.get("dir"),
                    "nrm": geom.get("nrm"), "start": start, "end": end,
                    "thickness": thickness, "length": length,
                    "outline": _outline(geom, lo, hi, thickness, pos),
                    "center": pos, "a": lo, "b": hi, "caps_n": 0,
                    "faces": 1, "kind": "single_line",
                    "thickness_cluster": "strong", "layer_ok": True,
                    "recall_source": "single_line",
                    "srcs": [{"handle": handle, "type": kind, "role": "single_line"}]})
        if len(out) >= MAX_SINGLE:
            break
    return out


def bridge_candidates(walls, gap_max=BRIDGE_GAP_MAX, thickness_tol=25.0):
    """同一直线上两段墙之间的短断口 -> 待验证连接候选（不是新墙，是断点桥接）。"""
    out = []
    walls = [w for w in walls or () if w.get("start") and w.get("end")]
    byline: dict[tuple, list] = {}
    for w in walls:
        byline.setdefault((w.get("orient"), round(float(w.get("center") or 0.0), 1)),
                          []).append(w)
    for (orient, center), group in sorted(byline.items(), key=lambda kv: (str(kv[0]))):
        group.sort(key=lambda w: _along(w, w["start"]))
        for a, b in zip(group, group[1:]):
            if abs(float(a.get("thickness") or 0) - float(b.get("thickness") or 0)) > thickness_tol:
                continue
            if (a.get("region") or "") != (b.get("region") or ""):
                continue
            a_hi = max(_along(a, a["start"]), _along(a, a["end"]))
            b_lo = min(_along(b, b["start"]), _along(b, b["end"]))
            gap = b_lo - a_hi
            if gap <= 2 or gap > gap_max:
                continue
            geom = {"orient": orient, "dir": a.get("dir"), "nrm": a.get("nrm")}
            t = float(a.get("thickness") or 200.0)
            out.append({"orient": orient, "dir": geom.get("dir"),
                        "nrm": geom.get("nrm"), "start": _world(geom, a_hi, center),
                        "end": _world(geom, b_lo, center),
                        "thickness": t, "length": gap,
                        "outline": _outline(geom, a_hi, b_lo, t, center),
                        "center": center, "a": a_hi, "b": b_lo, "caps_n": 0,
                        "faces": 0, "kind": "bridge", "bridge_gap": round(gap, 1),
                        "thickness_cluster": "strong", "layer_ok": True,
                        "recall_source": "bridge",
                        "srcs": [s for s in (a.get("srcs") or ())[:1]]
                        + [s for s in (b.get("srcs") or ())[:1]]})
    return out


def _region_groups(snapshot):
    """按 ``region`` 标记分组：不同建筑平面绝不参与同一次墙线配对。"""
    buckets: dict[str, dict] = {}
    for rec in geometry_records(snapshot):
        rid = rec.get("region") or "REGION-ALL"
        bucket = buckets.get(rid)
        if bucket is None:
            bucket = {"entities": [], "derived": [], "blocks": {}, "header": {}}
            buckets[rid] = bucket
        (bucket["derived"] if rec.get("derived") else bucket["entities"]).append(rec)
    if not buckets:
        return [("REGION-ALL", snapshot)]
    return sorted(buckets.items())


#: A/B 实验开关（默认全开）：
#: ``DWG_WALL_RECALL=0`` 只跑原始双线配对（版本 1），``DWG_WALL_VERIFY=0``
#: 保留高召回候选但跳过拓扑验证（版本 2），两者都开 = 版本 3。
#: 这样"收益来自召回还是来自验证"可以分别量出来，而不是靠猜。
WALL_RECALL = os.environ.get("DWG_WALL_RECALL", "1") != "0"
#: 弧墙（同心圆弧配对）开关：``DWG_WALL_ARCS=0`` 回到"只认直线面线"的旧行为，
#: 便于把弧墙的收益单独量出来。
WALL_ARCS = os.environ.get("DWG_WALL_ARCS", "1") != "0"
#: 幕墙带（共面三线归并）开关：``DWG_CURTAIN=0`` 回到"让配对器按双线去凑"的旧行为。
WALL_CURTAIN = os.environ.get("DWG_CURTAIN", "1") != "0"
WALL_VERIFY = os.environ.get("DWG_WALL_VERIFY", "1") != "0"


def _net_min_space(context) -> float:
    """分隔检验里"一块空间"的面积下界（mm²）：最小房间 ≈ 4 个字高的方格。

    字高量不出来时退回建筑常识值——**不**退回某张图的像素口径。
    """
    from . import scale as SC
    from . import walls_net as WNET
    scale = (context or {}).get("scale") or {}
    text_h = float(scale.get("text_h") or SC.DEFAULT_TEXT_H)
    return (SC.k("face_min_text") * text_h) ** 2 * WNET.SPACE_RATIO


def _net_door_width(context) -> float:
    """本图的门宽标尺（mm）：补合缺口的尺度以它为单位，不写死毫米数。"""
    from . import scale as SC
    scale = (context or {}).get("scale") or {}
    return float(scale.get("door_w") or SC.DEFAULT_DOOR_W)


def extract_walls_ext(snapshot, layers=None, *, allow_single_line=False,
                      allow_oblique=True, columns=(), context=None
                      ) -> tuple[list, list, dict]:
    """两阶段输出：``(walls, candidates, diag)``。

    * 阶段 A（高召回）：双线配对 + 短碎线共线合并后再配对 + 单线候选 +
      同线短断口桥接；按 ``region`` 独立配对。
    * 阶段 B（联合验证）：:func:`validate` 用独立几何证据 + 端头/柱拓扑打分，
      接受 / 待定 / 拒绝都给理由。被拒候选进第二个返回值（``candidates``），
      **不丢弃**，供缺陷诊断与修复算子继续取用。
    """
    ctx = dict(context or {})
    col_list = list(columns or ctx.get("columns") or ())
    from . import walls_net as WNET
    all_walls, candidates, diag_all = [], [], {
        "regions": {}, "stage_a": {}, "stage_b": {}, "recall": {}}
    net_diags = []
    for region_id, sub in _region_groups(snapshot):
        paired, dropped, diag = extract_walls(
            sub, layers=layers, allow_single_line=allow_single_line,
            allow_oblique=allow_oblique)
        for w in paired:
            w["region"] = region_id
            w.setdefault("faces", 2)
            w.setdefault("layer_ok", True)
            w.setdefault("recall_source", "pair")
        for w in dropped:
            w["region"] = region_id
        used = _used_handles(paired, [])
        cands = [w for w in paired if w.get("evidence_state") == "strong"]

        # --- 召回 1：短碎线共线合并后配对
        vsnap, vmap = (virtual_from_shorts(sub, layers=layers, used=used)
                       if WALL_RECALL else ({}, {}))
        merged_n = 0
        if vsnap.get("entities"):
            vwalls, _vdropped, _vdiag = extract_walls(
                vsnap, layers=layers, allow_single_line=False,
                allow_oblique=allow_oblique)
            # Activate rotated-short-wall synthesis only where an independently
            # observed oblique wall network exists. Isolated diagonal furniture
            # segments must not perturb an otherwise axial floor plan.
            oblique_context = sum(1 for p in paired if p.get("oblique")) >= 3
            for w in vwalls:
                if w.get("oblique") and not oblique_context:
                    continue
                if w.get("evidence_state") != "strong":
                    continue
                srcs = []
                for s in w.get("srcs") or ():
                    for orig in vmap.get(str(s.get("handle")), ()):
                        srcs.append({"handle": orig, "type": "LINE",
                                     "role": "merged_short"})
                if not srcs:
                    continue
                w["srcs"] = srcs
                w["faces"] = 2
                w["layer_ok"] = True
                w["kind"] = "merged_short"
                w["recall_source"] = "short_merge"
                w["region"] = region_id
                # 与已配对墙重合的（同一堵墙）不再重复计入
                if any(_same_wall(w, a) for a in cands):
                    continue
                cands.append(w)
                merged_n += 1

        # --- 召回 2：单线弱墙候选
        ts = sorted(float(w["thickness"]) for w in cands if w.get("thickness"))
        thick = ts[len(ts) // 2] if ts else 200.0
        singles = (single_line_candidates(sub, layers=layers, thickness=thick,
                                         used=_used_handles(cands, dropped),
                                         walls=cands) if WALL_RECALL else [])
        oblique_context = sum(1 for p in paired if p.get("oblique")) >= 3
        singles = [w for w in singles if not w.get("dir") or oblique_context]
        for w in singles:
            w["region"] = region_id
        cands.extend(singles)

        # --- 召回 3：同线短断口桥接（要等墙有了位置才能算）
        bridges = (bridge_candidates(list(paired) + [c for c in cands
                                                     if c.get("kind") != "single_line"])
                   if WALL_RECALL else [])
        for w in bridges:
            w["region"] = region_id
        cands.extend(bridges)

        # --- 生成门：只有"确实把空间分开"的候选才有资格进阶段 B。
        # 理由见 walls_net：墙的定义属性是分隔空间，不是"两根平行线间距合适"。
        # 单图层图纸里标注链/轴网/家具在厚度与端头上和真墙完全同构，只能在这里分。
        if WNET.mode() == "off":
            net_diags.append({"net": "off", "candidates": len(cands),
                              "reason": "disabled"})
        elif cands:
            kept_net, cut_net, net_diag = WNET.separation_gate(
                cands, columns=col_list,
                min_space=_net_min_space(ctx), door_w=_net_door_width(ctx))
            net_diags.append(net_diag)
            # evidence 档只打标记不删候选；strict 档才真正剔除（默认关闭，见 walls_net）
            if WNET.mode() == "strict" and cut_net:
                cut_ids = {id(w) for w in cut_net}
                for w in cut_net:
                    candidates.append(_as_candidate(w, "NET_SAME_SPACE"))
                cands = kept_net
                paired = [w for w in paired if id(w) not in cut_ids]
                diag["net_dropped"] = net_diag.get("dropped")
                diag["net_kept"] = net_diag.get("kept")

        if WALL_VERIFY:
            ver = validate(cands, walls=paired, columns=col_list, region=region_id)
        else:                      # 版本 2：不做拓扑验证，候选全进（A/B 对照）
            ver = {"accepted": list(cands), "pending": [], "rejected": [],
                   "walls": list(cands),
                   "stats": {"candidates": len(cands), "accepted": len(cands),
                             "pending": 0, "rejected": 0, "verify": "off"}}
        region_walls = []
        for w in ver["walls"]:
            w["region"] = region_id
            region_walls.append(w)
        # 被验证淘汰的候选 + 主线配对阶段就淘汰的候选，都留给下游诊断
        for w in ver["rejected"]:
            w.setdefault("region", region_id)
            candidates.append(_as_candidate(w, "VERIFY_REJECTED"))
        for w in dropped:
            candidates.append(_as_candidate(w, "PAIR_REJECTED"))
        # --- 弧墙：同心圆弧配对。**独立的几何不变量**，不进上面的直线配对/
        # 召回/验证链——那条链每一步都以"直线 + 常量法向偏移"为前提，圆弧进去
        # 只会被抹成弦（外接矩形、弦长、弦角度全错）。
        # --- 幕墙带：共面三线先归并成一条带，再取代被它拆出来的两道半带墙。
        # 必须在采纳之后做：半带候选是配对器的产物，归并在配对之前会把
        # 300 实体墙（居中画中线）也吃进去。
        thick_hint = sorted(float(w["thickness"]) for w in region_walls
                            if w.get("thickness"))
        thick_hint = thick_hint[len(thick_hint) // 2] if thick_hint else None
        bands, curtain_diag = [], {}
        if WALL_CURTAIN:
            bands, curtain_diag = CURTAIN.detect(
                sub, layers=layers,
                thickness=(ctx.get("scale") or {}).get("thickness") or thick_hint,
                wall_layers=layers)
            region_walls, half_dropped = CURTAIN.suppress_half_bands(region_walls, bands)
            curtain_diag["half_bands_dropped"] = half_dropped
            for b in bands:
                b["region"] = region_id
            diag_all.setdefault("curtain", {})
            diag_all["curtain"][region_id] = curtain_diag

        arc_walls, arc_diag = [], {}
        if WALL_ARCS:
            arc_walls, arc_diag = ARC.detect(
                sub, layers=layers,
                thickness=(ctx.get("scale") or {}).get("thickness") or thick_hint,
                thickness_hint=thick_hint,
                is_curtain=(CURTAIN.is_curtain_layer if WALL_CURTAIN else None))
            for w in arc_walls:
                w["region"] = region_id
            diag_all.setdefault("arc", {})
            diag_all["arc"][region_id] = arc_diag
        all_walls.extend(region_walls)
        all_walls.extend(bands)
        all_walls.extend(arc_walls)
        diag_all["regions"][region_id] = {
            "paired": len(paired), "merged_short": merged_n,
            "single_line": len(singles), "bridge": len(bridges),
            "accepted": len(ver["accepted"]), "pending": len(ver["pending"]),
            "rejected": len(ver["rejected"]), "arc": len(arc_walls),
            "curtain": len(bands)}
        for key in ("h_faces", "v_faces", "diag_segments", "candidates",
                    "dropped", "dominant_thickness", "envelope",
                    "thickness_clusters", "oblique_walls"):
            if key in diag:
                diag_all["stage_a"].setdefault(key, 0)
                if isinstance(diag[key], (int, float)):
                    diag_all["stage_a"][key] += diag[key]
                else:
                    diag_all["stage_a"][key] = diag[key]
        diag_all["stage_b"][region_id] = ver["stats"]

    all_walls.sort(key=lambda w: (w.get("orient") or "",
                                  round(float(w.get("center") or 0.0), 1),
                                  _along(w, w["start"]) if w.get("start") else 0.0))
    total = {"paired": len(all_walls),
             "pending": sum(1 for w in all_walls if w.get("evidence_state") == "pending"),
             "weak": sum(1 for w in all_walls if w.get("evidence_state") == "weak"),
             "candidates": len(candidates),
             "recall_added": sum(1 for w in all_walls
                                 if w.get("recall_source") in ("short_merge", "single_line", "bridge")),
             "arc": sum(1 for w in all_walls if w.get("arc")),
             "curtain": sum(1 for w in all_walls if w.get("role") == "curtain"),
             "regions": len(diag_all["regions"])}
    diag = dict(diag_all["stage_a"], **total)
    if net_diags:
        diag["net"] = net_diags[0] if len(net_diags) == 1 else {
            "regions": net_diags,
            "candidates": sum(n.get("candidates") or 0 for n in net_diags),
            "kept": sum(n.get("kept") or 0 for n in net_diags),
            "dropped": sum(n.get("dropped") or 0 for n in net_diags)}
        diag["net_summary"] = " | ".join(WNET.summary(n) for n in net_diags)
    diag["unpaired"] = len(candidates)
    diag["weak_thickness"] = sum(1 for u in candidates if u.get("weak"))
    diag["stage_details"] = diag_all
    return all_walls, candidates, diag


def _same_wall(a, b, tol=30.0) -> bool:
    if not (a.get("start") and b.get("start") and a.get("end") and b.get("end")):
        return False
    if (a.get("orient") or "") != (b.get("orient") or ""):
        return False
    if abs(float(a.get("center") or 0.0) - float(b.get("center") or 0.0)) > tol:
        return False
    a0, a1 = sorted((_along(a, a["start"]), _along(a, a["end"])))
    b0, b1 = sorted((_along(b, b["start"]), _along(b, b["end"])))
    return min(a1, b1) - max(a0, b0) > 0.5 * max(1.0, min(a1 - a0, b1 - b0))


def _as_candidate(w, why) -> dict:
    """统一候选格式：下游（诊断/修复算子）只认这一种。"""
    return {"kind": w.get("kind") or "unpaired",
            "candidate_state": why,
            "region": w.get("region"),
            "bbox": w.get("bbox"),
            "srcs": list(w.get("srcs") or ()),
            "handles": [x.get("handle") for x in (w.get("srcs") or ())
                        if x.get("handle")],
            "why": w.get("drop_reason") or w.get("verify") or why,
            "weak": ("THICKNESS_CLUSTER_WEAK"
                     if "厚度簇" in str(w.get("drop_reason") or "") else None),
            "orient": w.get("orient"),
            "pos": w.get("center"), "lo": w.get("a"), "hi": w.get("b"),
            "thickness": w.get("thickness"),
            "geom_score": w.get("geom_score"), "topo_score": w.get("topo_score"),
            "score": w.get("score"),
            "start": w.get("start"), "end": w.get("end")}
