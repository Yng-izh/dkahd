"""幕墙：把"三层共面线"归并成一条带（幕墙算墙的一种，不新增类别）。

为什么必须归并，而不是任由面线配对去凑
--------------------------------------
一道幕墙在平面上常画成**三条共面线**：外皮、内皮，加上中间一条玻璃/分格基准线。
实测 06住院综合楼：外两条与 WALL 层墙皮完全重合，中间那条只出现在 CURTWALL 层，
三层间距恒为 100，带总厚 200。

面线配对只认"两道面线配一对"，于是同一条带会被拆成两道各 100 厚的候选，再被
"共用面线 = 面层/装饰线"的去重规则删掉一道——**口径从 200 掉到 100，还白丢一条**。
归并发生在配对之后、采纳之前：先认出"这是一条带"，再用它取代那两道半带。

判据（全部是图内倍率，没有裸毫米数）
------------------------------------
1. 同方向、共覆盖的平行线 >=3 条，**相邻间距相等**（+-curtain_gap_tol）；
2. 每段间距 <= curtain_band_max_thickness x 本图墙厚；
3. 沿线重叠长度 >= curtain_min_len_thickness x 墙厚；
4. **中间那条线的图层必须是幕墙类**（幕墙/玻璃/CURTWALL...）**或不在墙层里**。

第 4 条是安全阀：一道 300 厚、居中画了中线的**实体墙**，三条线等距、也共覆盖，
前三条全中——只有第 4 条能把它挡在外面（三条线都在 WALL 层 -> 不是幕墙）。
"""
from __future__ import annotations

import collections
import re

from . import scale as SC
from .base import merge_intervals, segments_from, seg_len
from .snapshot import geometry_records

#: 幕墙类图层名特征。与 taxonomy.RULES 里 curtwall/幕墙/玻璃 那条同源，
#: 这里只用来回答"这条线是不是幕墙层画的"，不参与角色打分。
CURTAIN_HINTS = re.compile(r"curtwall|curtain|幕墙|玻璃|glass", re.IGNORECASE)


def is_curtain_layer(name) -> bool:
    return bool(name) and bool(CURTAIN_HINTS.search(str(name)))


def _lines(snapshot, layers):
    """指定图层上的直线段：[(orient, pos, lo, hi, layer, handle)]。"""
    wanted = set(layers or ())
    out = []
    for rec in geometry_records(snapshot):
        if rec.get("derived"):
            continue
        if rec.get("layer") not in wanted:
            continue
        if rec.get("type") not in ("LINE", "LWPOLYLINE", "POLYLINE"):
            continue
        for (x1, y1, x2, y2) in segments_from(rec):
            if seg_len(x1, y1, x2, y2) < 8.0:
                continue
            if abs(y2 - y1) <= 1.5 and abs(x2 - x1) > 3:
                out.append(("H", (y1 + y2) / 2.0, min(x1, x2), max(x1, x2),
                            rec.get("layer"), rec.get("handle")))
            elif abs(x2 - x1) <= 1.5 and abs(y2 - y1) > 3:
                out.append(("V", (x1 + x2) / 2.0, min(y1, y2), max(y1, y2),
                            rec.get("layer"), rec.get("handle")))
    return out


def _faces(lines, tol):
    """同方向同位置的线聚成一条"面线"：[(pos, [(lo,hi,layer,handle)])]。"""
    rows = sorted(lines, key=lambda r: r[1])
    groups = []
    for row in rows:
        if groups and row[1] - groups[-1][0][-1] <= tol:
            groups[-1][0].append(row[1])
        else:
            groups.append([[row[1]], []])
        groups[-1][1].append(row)
    return [(sum(p) / len(p), [(m[2], m[3], m[4], m[5]) for m in members])
            for p, members in groups]


def _covered(members):
    """一条面线的覆盖区间（合并后）与其总长。"""
    spans = merge_intervals([(m[0], m[1]) for m in members], tol=1.5)
    return spans, sum(b - a for a, b in spans)


def _common_span(chain):
    """链条上所有面线共同覆盖的区间：逐段求交。"""
    spans = None
    for _pos, members in chain:
        cur = merge_intervals([(m[0], m[1]) for m in members], tol=1.5)
        if spans is None:
            spans = cur
            continue
        nxt = []
        for a0, a1 in spans:
            for b0, b1 in cur:
                lo, hi = max(a0, b0), min(a1, b1)
                if hi - lo > 0:
                    nxt.append((lo, hi))
        spans = nxt
        if not spans:
            return []
    return spans or []


def _is_curtain_band(chain, wall_layers) -> bool:
    """中间那条面线的图层必须"是幕墙层"或"不在墙层"——见模块头第 4 条。"""
    for _pos, members in chain[1:-1]:
        for _lo, _hi, layer, _handle in members:
            if is_curtain_layer(layer):
                return True
            if layer not in wall_layers:
                return True
    return False


def _mean_gap(chain) -> float:
    """带内相邻面线的平均间距（记录"三层怎么排的"，不是判据）。"""
    if len(chain) < 2:
        return 1.0
    return (chain[-1][0] - chain[0][0]) / float(len(chain) - 1)


def detect(snapshot, layers, *, thickness=None, wall_layers=None):
    """找出幕墙带。返回 (bands, diag)，band 记录与墙同构（role="curtain"）。"""
    base_t = float(thickness or SC.DEFAULT_THICKNESS)
    wall_layers = set(wall_layers if wall_layers is not None else (layers or ()))
    gap_tol = SC.k("curtain_gap_tol")
    max_gap = SC.k("curtain_band_max_thickness") * base_t
    cover_ratio = SC.k("curtain_cover_ratio")
    min_len = SC.k("curtain_min_len_thickness") * base_t
    pos_tol = SC.k("curtain_pos_tol_thickness") * base_t
    piece_min = SC.k("curtain_piece_min_thickness") * base_t

    raw = _lines(snapshot, layers)
    bands, rejected = [], collections.Counter()
    for orient in ("H", "V"):
        faces = _faces([r for r in raw if r[0] == orient], pos_tol)
        # 先剪掉本身就很短的面线：0.2m 的图例/线脚同样可能与邻居"等距"，
        # 一旦混进链条就把共覆盖长度压到 0.2m（实测这张图就这样一条都出不来）。
        faces = [(pos, m) for pos, m in faces if _covered(m)[1] >= min_len]
        n = len(faces)
        i = 0
        while i < n - 2:
            gap = faces[i + 1][0] - faces[i][0]
            if gap <= 0 or gap > max_gap:
                i += 1
                continue
            chain = [faces[i], faces[i + 1]]
            j = i + 2
            while j < n:
                g = faces[j][0] - faces[j - 1][0]
                if abs(g - gap) > gap_tol * gap or g > max_gap:
                    break
                chain.append(faces[j])
                j += 1
            if len(chain) < 3:
                i += 1
                continue
            spans = _common_span(chain)
            total = sum(b - a for a, b in spans)
            # 带的长度由**带内线**划定：外皮往往比幕墙本身长（同一条墙线上还有
            # 别的墙段），那是普通墙的领地。只要求"带内线几乎整条都在带里"。
            inner_cov = [_covered(m)[1] for _p, m in chain[1:-1]]
            ref = min(inner_cov) if inner_cov else min(_covered(m)[1] for _p, m in chain)
            if total <= 0 or total < min_len:
                rejected["共覆盖长度不足"] += 1
            elif total < cover_ratio * ref:
                rejected["覆盖比例不足"] += 1
            elif not _is_curtain_band(chain, wall_layers):
                rejected["中间线不属于幕墙层"] += 1
            else:
                # 幕墙被结构柱切成一片片（实测 6~10 片、柱宽约 600）。
                # 每片单独成一条带，缺口留给 merge_runs 按"同中心线 + 缺口小"
                # 缝回连续墙——分段口径与实体墙一致，不多造整条假通长。
                for lo, hi in spans:
                    if hi - lo >= piece_min:
                        bands.append(_band_record(orient, chain, lo, hi))
            i = j
    bands = _drop_duplicates(bands)
    return bands, {"lines": len(raw), "bands": len(bands),
                   "rejected": dict(rejected), "thickness_base": round(base_t, 1)}


def _band_record(orient, chain, lo, hi):
    pos_in, pos_out = chain[0][0], chain[-1][0]
    thickness = pos_out - pos_in
    center = (pos_in + pos_out) / 2.0
    t2 = thickness / 2.0
    if orient == "H":
        start, end = [lo, center], [hi, center]
        outline = [[lo, center - t2], [hi, center - t2],
                   [hi, center + t2], [lo, center + t2]]
    else:
        start, end = [center, lo], [center, hi]
        outline = [[center - t2, lo], [center - t2, hi],
                   [center + t2, hi], [center + t2, lo]]
    srcs = []
    for _pos, members in chain:
        for _lo, _hi, layer, handle in members:
            if handle and not any(s["handle"] == handle for s in srcs):
                srcs.append({"handle": handle, "type": "LINE",
                             "role": "curtain_face", "layer": layer})
    return {
        "orient": orient, "center": round(center, 1), "pos": round(center, 1),
        "start": start, "end": end,
        "thickness": thickness, "length": hi - lo, "outline": outline,
        "srcs": srcs, "caps_n": 0, "faces": 2,
        "role": "curtain", "band": True,
        "band_lines": len(chain), "band_pitch": round(_mean_gap(chain), 1),
        "recall_source": "curtain_band",
        "evidence_state": "strong", "geom_score": 0.9, "topo_score": 0.6,
        "layer_ok": True,
    }


def _drop_duplicates(bands):
    kept = []
    for b in sorted(bands, key=lambda x: (x["orient"], -x["length"])):
        dup = False
        for k in kept:
            if k["orient"] != b["orient"]:
                continue
            axis = 0 if b["orient"] == "H" else 1
            if (abs(k["center"] - b["center"]) <= 1.0
                    and abs(k["start"][axis] - b["start"][axis]) <= 1.0
                    and abs(k["end"][axis] - b["end"][axis]) <= 1.0):
                dup = True
                break
        if not dup:
            kept.append(b)
    return kept


def _axis_span(w):
    k = 0 if w.get("orient") == "H" else 1
    return (min(float(w["start"][k]), float(w["end"][k])),
            max(float(w["start"][k]), float(w["end"][k])))


def suppress_half_bands(walls, bands):
    """删掉被幕墙带取代的"半带假墙"。

    配对器会把一条带拆成两道各 100 厚的墙（再删一道），剩下那道与带同位置、
    厚度只有一半。口径改成"带"之后它必须让位，否则同一条幕墙会被数两次。
    """
    if not bands:
        return walls, 0
    out, dropped = [], 0
    for w in walls:
        half = False
        if not w.get("arc"):
            for b in bands:
                if w.get("orient") != b["orient"]:
                    continue
                if abs(float(w.get("center") or 0.0) - b["center"]) > b["thickness"] / 2.0:
                    continue
                if float(w.get("thickness") or 0.0) >= b["thickness"] * 0.75:
                    continue
                w0, w1 = _axis_span(w)
                b0, b1 = _axis_span(b)
                if min(w1, b1) - max(w0, b0) > 0.5 * max(w1 - w0, 1.0):
                    half = True
                    break
        if half:
            dropped += 1
            continue
        out.append(w)
    return out, dropped
