"""统一洞口：先定"洞口在哪、多宽"，再定"它是什么"。

问题
----
改造前，门和窗是**两条并行的候选流**：门块/门弧一路、窗线束一路，各自判断、
各自采纳，最后靠 :func:`openings._overlap` 事后压重叠。这注定重复计数——
同一个洞口被"弧"计一次、"语义裁定"再计一次，于是大图出现 82 樘门而实际
开启弧只有 76 条；家具层上的装饰弧因为"邻近洞口"被当成门，也是同一条缝里
漏进来的。

思路
----
一个物理洞口在图上只能有**一个**位置和**一个**宽度。所以：

1. 把所有候选（门块 / 门弧 / 窗块 / 窗线束 / 墙缺口 / 语义裁定）都投影到
   "所在墙线 + 沿墙区间"上；
2. 同一墙线上区间重叠的候选归并为**一个洞口对象**，证据取并集；
3. 类型由证据组合决定，不由加法条数决定。

这样双扇门的两条弧自然归一（同一洞口），装饰弧因为"不在洞口上"自然出局
（不需要靠 ±1500mm 的邻近判据），门窗互斥也从"事后几何比对"变成"本来就
是同一个对象"。

本模块只做归并与分类，不产生坐标（坐标全部取自候选本身），也不改图层语义。
"""
from __future__ import annotations

import math

from . import geometry as GEO
from . import scale as SC
from .elements import WIN_BLOCKS
from . import wallmodel as WM

#: 同一洞口的区间重叠判定：重叠量占较窄候选的比例（无量纲，跨图纸恒定）
MERGE_OVERLAP = 0.5
#: 两个**实体块**之间的合并阈值：块是独立实例，只有区间几乎重合才可能是同一樘
#: （双扇门被画成两块的情形），否则一律各算一樘——办公楼 30 个门块一度被并成
#: 26 樘，就是这条阈值没区分"块 vs 弧"造成的。
BLOCK_OVERLAP = 0.85
#: 两条同类候选中心相距不超过 **0.10 个典型门宽** 也算同一洞口。
#:
#: 这个数只描述"位置歧义有多大"（同一条弧被门块和弧线各记一次时的偏移），
#: 不是"洞口有多大"。取 0.4 门宽时，大图的门宽量到 1616，容差涨到 646mm，
#: 把相距 600mm 的两樘门并成一樘（门数 64 → 42）。真正"同一个洞"的判定靠
#: 区间重叠，中心距只是兜底，所以它必须显著小于最小洞口间距。
MERGE_CENTER_DOOR = 0.10
#: 开启弧的"像门"判据：扫角区间（角度，无量纲）与"半径≈洞口宽"的相对容差
ARC_SWEEP = (40.0, 150.0)
ARC_RADIUS_RATIO = 0.30


def _seg(item):
    return item.get("opening") or item.get("axis")


def _arc_info(item):
    """从候选记录里取出"开启弧"证据（弧线门、块门共用同一结构）。"""
    hinges = item.get("hinges") or ()
    if not hinges:
        return None
    seg = _seg(item)
    if not seg or len(seg) != 2:
        return None
    h = hinges[0]
    far = seg[1] if (abs(seg[0][0] - h[0]) + abs(seg[0][1] - h[1])) < 1e-6 else seg[0]
    r = math.hypot(far[0] - h[0], far[1] - h[1])
    if r <= 0:
        return None
    return {"hinge": [float(h[0]), float(h[1])],
            "radius": round(r, 1),
            "free": item.get("free_end"),
            "layer": item.get("layer"),
            "handle": item.get("handle")}


def _span(item, lines, thick):
    """候选 -> (墙线, lo, hi)；贴不上墙线返回 None（不参与归并）。"""
    seg = _seg(item)
    if not seg or len(seg) != 2:
        return None
    (x1, y1), (x2, y2) = seg
    mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    line = WM.nearest_line(lines, mx, my, 3.0 * thick)
    if line is None:
        return None
    u1, _ = WM.local(line, x1, y1)
    u2, _ = WM.local(line, x2, y2)
    lo, hi = min(u1, u2), max(u1, u2)
    if hi - lo <= 1e-9:
        return None
    return {"line": line, "lo": lo, "hi": hi,
            "pos": float(line.get("pos") if line.get("pos") is not None else my),
            "orient": line.get("orient")}


def _kind_of(item, category):
    """候选的"画法来源"，只记录事实，不判身份。"""
    kinds = []
    if item.get("block"):
        kinds.append(f"{category}_block")
    if _arc_info(item):
        kinds.append("swing_arc")
    if item.get("judged_role"):
        kinds.append("judged")
    if not kinds:
        kinds.append(f"{category}_geom")
    return kinds


def _merge_ok(a, b, same_category=True, a_block=False, b_block=False,
              center_tol=0.0):
    """两个候选是否落在同一个洞口上。

    跨类别（门 vs 窗）只认**真实区间重叠**：同一洞口被同时解释成门和窗，本身就是
    冲突，应当合成一个对象让分类阶段裁决。同类候选额外允许"中心相近"——门弧与
    门块之间常有几百毫米偏移，它们指向的是同一樘门。
    """
    ov = min(a["hi"], b["hi"]) - max(a["lo"], b["lo"])
    need = BLOCK_OVERLAP if (a_block and b_block) else MERGE_OVERLAP
    if ov > 0:
        narrow = min(a["hi"] - a["lo"], b["hi"] - b["lo"])
        if narrow > 0 and ov >= need * narrow:
            return True
    if not same_category or (a_block and b_block):
        return False
    ca, cb = (a["lo"] + a["hi"]) / 2.0, (b["lo"] + b["hi"]) / 2.0
    return abs(ca - cb) <= center_tol


def cluster(doors=(), windows=(), *, lines=(), thick=200.0, scale=None) -> dict:
    """候选 -> 统一洞口对象。返回 ``{"openings", "dropped", "unplaced", "stats"}``。

    每个洞口对象沿用门/窗记录的字段（``opening``/``axis``/``width``/``layer``
    /``handle``…），额外带 ``sources``（全部画法来源）、``arcs``（全部开启弧）、
    ``merged``（归并了几个候选）。下游 ``openings.select`` 可以直接消费。
    """
    th = SC.thresholds(scale or {})
    thick = float(thick or th["thickness"])
    center_tol = MERGE_CENTER_DOOR * float(th["door_w"])

    placed, unplaced = [], []
    unplaced_kind = {}
    for category, items in (("door", doors), ("window", windows)):
        for it in items or ():
            sp = _span(it, lines, thick)
            if sp is None:
                unplaced.append(it)
                unplaced_kind[id(it)] = category
                continue
            placed.append({"item": it, "category": category, "span": sp})

    # 稳定排序：同一条墙线上的候选挨在一起，归并结果与输入顺序无关
    placed.sort(key=lambda c: (c["category"], c["span"]["orient"],
                               round(c["span"]["pos"], 1), c["span"]["lo"]))

    buckets = []
    for cand in placed:
        target = None
        for b in buckets:
            if b["span"]["orient"] != cand["span"]["orient"]:
                continue
            if abs(b["span"]["pos"] - cand["span"]["pos"]) > 1.0:
                continue
            same = b["category"] == cand["category"]
            if not (same or not b.get("mixed")):
                continue
            if _merge_ok(b["span"], cand["span"], same_category=same,
                         a_block=b.get("has_block", False),
                         b_block=bool(cand["item"].get("block")),
                         center_tol=center_tol):
                target = b
                break
        if target is None:
            buckets.append({"category": cand["category"], "span": dict(cand["span"]),
                            "items": [cand["item"]], "categories": [cand["category"]],
                            "has_block": bool(cand["item"].get("block"))})
            continue
        target["items"].append(cand["item"])
        target["categories"].append(cand["category"])
        target["has_block"] = target.get("has_block") or bool(cand["item"].get("block"))
        target["span"]["lo"] = min(target["span"]["lo"], cand["span"]["lo"])
        target["span"]["hi"] = max(target["span"]["hi"], cand["span"]["hi"])
        if cand["category"] != target["category"]:
            target["mixed"] = True
            # 有门块/门弧的一方决定主类别（窗的解释更弱）
            if cand["category"] == "door":
                target["category"] = "door"

    # 贴不上任何墙线的候选**不能丢**：墙没被识别出来不等于洞口不存在（办公楼的门
    # 全部是块参照，一旦该段墙漏检，把门一并丢掉等于双重惩罚）。这类候选单独成
    # 洞口，标 unanchored，由其自身证据（块/弧）决定去留。
    for it in unplaced:
        seg = _seg(it)
        if not seg or len(seg) != 2:
            continue
        # 只放行**自带几何证据**的候选（门块 / 开启弧）。纯语义裁定出来的洞口
        # 一旦贴不上墙，就没有任何结构证据支持它存在——放行会把"模型觉得像门"
        # 直接变成构件（办公楼因此多出 2 樘门）。
        if not (it.get("block") or it.get("hinges")):
            continue
        (x1, y1), (x2, y2) = seg
        axis = [[round(float(x1), 1), round(float(y1), 1)],
                [round(float(x2), 1), round(float(y2), 1)]]
        # Preserve the original candidate class. A nonstandard window block
        # is not a door just because it is absent from the tiny built-in list.
        cat = unplaced_kind.get(id(it),
                                "window" if it.get("block") in WIN_BLOCKS else "door")
        buckets.append({"category": cat,
                        "span": {"lo": 0.0, "hi": math.hypot(x2 - x1, y2 - y1),
                                 "pos": 0.0, "orient": "H", "line": {"orient": "H",
                                 "pos": 0.0, "dir": (1.0, 0.0), "nrm": (0.0, 1.0),
                                 "angle": 0.0, "lo": 0.0, "hi": 0.0, "spans": []}},
                        "items": [it], "categories": [cat],
                        "has_block": bool(it.get("block")), "unanchored": True,
                        "free_axis": axis})

    # ---- 双扇门成对合并（结构判据，见 _leaf_pair）
    jamb_tol = max(float(th["tol_join"]), 0.02 * float(th["door_w"]))
    changed = True
    while changed:
        changed = False
        for i in range(len(buckets)):
            for j in range(i + 1, len(buckets)):
                a, b = buckets[i], buckets[j]
                if a.get("category") != "door" or b.get("category") != "door":
                    continue
                if a.get("unanchored") or b.get("unanchored"):
                    continue
                if a["span"]["orient"] != b["span"]["orient"]:
                    continue
                if abs(a["span"]["pos"] - b["span"]["pos"]) > 1.0:
                    continue
                if not _leaf_pair(a, b, jamb_tol):
                    continue
                a["items"].extend(b["items"])
                a["categories"].extend(b["categories"])
                a["span"]["lo"] = min(a["span"]["lo"], b["span"]["lo"])
                a["span"]["hi"] = max(a["span"]["hi"], b["span"]["hi"])
                a["has_block"] = a.get("has_block") or b.get("has_block")
                a["leaf_pair"] = True
                buckets.pop(j)
                changed = True
                break
            if changed:
                break

    merged_count = sum(len(b["items"]) - 1 for b in buckets)
    kept, dropped = [], []
    for b in buckets:
        o = _build(b, th)
        w = float(o.get("width") or 0.0)
        if w < th["gap_min"]:
            dropped.append(dict(o, drop_reason="too_narrow"))
            continue
        # Door-width calibration must not also cap independently validated
        # *window blocks*. In mixed drawings the inferred door width can be
        # ~400mm, while real ribbon windows are 2.7m long. Line-only window
        # candidates still obey the conservative generic aperture cap.
        independent_window = (b["category"] == "window" and
                              str(o.get("block_evidence") or "")
                              .startswith("geometric:window_block_parallel_strokes"))
        max_width = th["gap_max"]
        # 通长窗/带形窗可以很长（实测艺术楼 C15 是 5538mm），不能用"4 倍门宽"的
        # 洞口上限去卡它。判据不用毫米数，用**自洽性**：洞口宽度和这个窗块自己的
        # 尺寸（|缩放| × 块净尺寸）对得上，就说明这个洞就是这块窗，宽度没有第二
        # 种解释。尺寸对不上的才回到洞口上限（那是"一条线被当成整片幕墙"的情况）。
        nominal = [float(it.get("width") or 0.0) for it in b["items"]
                   if it.get("block") and it.get("width")]
        self_consistent = bool(
            independent_window and nominal
            and abs(w - max(nominal)) <= MERGE_OVERLAP * max(nominal))
        if w > max_width and "swing_arc" not in o["sources"] and not self_consistent:
            dropped.append(dict(o, drop_reason="too_wide"))
            continue
        kept.append(o)

    # 同一条墙线上仍然重叠的洞口（不同位置但区间交叠）：只保留证据最强的一个，
    # 防止"一个洞口两层画法"绕过上面的归并
    kept.sort(key=lambda o: (-len(o["sources"]), -float(o.get("merged") or 1),
                             -float(o.get("width") or 0.0)))
    final, shadows = [], []
    for o in kept:
        a = _span_of_record(o, lines, thick)
        clash = None
        if a is not None:
            for f in final:
                b = f.get("_span")
                if b is None or b["orient"] != a["orient"]:
                    continue
                if abs(b["pos"] - a["pos"]) > 1.0:
                    continue
                if _merge_ok(a, b, same_category=True, center_tol=center_tol):
                    clash = f
                    break
        if clash is not None:
            clash["merged"] = int(clash.get("merged") or 1) + 1
            clash["handles"] = sorted(set(clash.get("handles") or [])
                                      | set(o.get("handles") or []))
            shadows.append(o)
            continue
        o["_span"] = a
        final.append(o)

    for o in final:
        o.pop("_span", None)
    for o in shadows:
        o.pop("_span", None)
    for o in dropped:
        o.pop("_span", None)

    return {"openings": final, "dropped": dropped, "shadowed": shadows,
            "unplaced": unplaced,
            "stats": {"candidates": len(doors or ()) + len(windows or ()),
                      "openings": len(final), "merged": merged_count,
                      "dropped": dict(
                          (r, sum(1 for d in dropped if d["drop_reason"] == r))
                          for r in sorted({d["drop_reason"] for d in dropped})),
                      "shadowed": len(shadows),
                      "unplaced": len(unplaced),
                      "thresholds": {"gap_min": round(th["gap_min"], 1),
                                     "gap_max": round(th["gap_max"], 1)}}}


def _first_arc(bucket):
    """桶里第一条开启弧（铰点 + 半径）；没有弧返回 None。"""
    for it in bucket.get("items") or ():
        a = _arc_info(it)
        if a:
            return a
    return None


def _leaf_pair(a, b, jamb_tol):
    """两个开启弧簇是不是**同一樘双扇门的两片门扇**。

    不用"中心距离小于多少"来判——那个阈值在两张图上就要取到不同值（实测门数在
    42~102 之间摆动），等于让一个拍出来的毫米数替几何做决定。改用结构判据，全部
    与图纸尺度无关：

    1. 贴同一条墙线、同一侧（外层已保证）；
    2. 两段区间**相邻或重叠**（双扇门共用中间门梃，两片门扇紧挨着）；
    3. 两个铰点**分居合并区间的两端**（一左一右，这才是双扇门的定义）；
    4. 两片门扇宽度相近（差不超过较宽者的 35%）。
    """
    aa, ab = _first_arc(a), _first_arc(b)
    if aa is None or ab is None:
        return False
    sa, sb = a["span"], b["span"]
    lo, hi = min(sa["lo"], sb["lo"]), max(sa["hi"], sb["hi"])
    wa, wb = sa["hi"] - sa["lo"], sb["hi"] - sb["lo"]
    if wa <= 0 or wb <= 0:
        return False
    gap = max(sa["lo"], sb["lo"]) - min(sa["hi"], sb["hi"])
    if gap > jamb_tol:
        return False
    line = sa["line"]
    ua = WM.local(line, aa["hinge"][0], aa["hinge"][1])[0]
    ub = WM.local(line, ab["hinge"][0], ab["hinge"][1])[0]
    ends_a = min(abs(ua - lo), abs(ua - hi))
    ends_b = min(abs(ub - lo), abs(ub - hi))
    opposite = abs(ua - ub) >= 0.7 * (hi - lo)
    if not (ends_a <= jamb_tol and ends_b <= jamb_tol and opposite):
        return False
    return abs(wa - wb) <= 0.35 * max(wa, wb)


def _span_of_record(rec, lines, thick):
    seg = rec.get("axis") or rec.get("opening")
    if not seg:
        return None
    return _span({"axis": seg}, lines, thick)


def _build(bucket, th) -> dict:
    """一个候选簇 -> 一个洞口记录（字段与门/窗候选兼容）。"""
    span = bucket["span"]
    line = span["line"]
    if bucket.get("unanchored"):
        axis = [list(bucket["free_axis"][0]), list(bucket["free_axis"][1])]
    else:
        p1 = GEO.point_at(line, span["lo"])
        p2 = GEO.point_at(line, span["hi"])
        axis = [[round(p1[0], 1), round(p1[1], 1)], [round(p2[0], 1), round(p2[1], 1)]]
    width = round(math.hypot(axis[1][0] - axis[0][0], axis[1][1] - axis[0][1]), 1) \
        or round(span["hi"] - span["lo"], 1)

    items = bucket["items"]
    sources, arcs, srcs, methods = [], [], [], []
    block = attr = None
    layer = None
    handles = []
    judged = None
    subtype = None
    swing = None
    for it in items:
        for k in _kind_of(it, bucket["category"]):
            if k not in sources:
                sources.append(k)
        a = _arc_info(it)
        if a:
            arcs.append(a)
            if a.get("free"):
                methods.append("arc")
        if it.get("block"):
            block = it["block"]
            subtype = subtype or it.get("subtype")
        if it.get("attr") and not attr:
            attr = it["attr"]
        if layer is None and it.get("layer"):
            layer = it["layer"]
        if it.get("judged_role") and judged is None:
            judged = it
        if it.get("swing") and swing is None:
            swing = it["swing"]
        if it.get("handle"):
            handles.append(it["handle"])
            # A bare swing arc or a line-window cannot be reported as INSERT.
            # Prefer the original source type if the detector supplied it.
            existing = [src for src in (it.get("srcs") or ())
                        if src.get("handle") == it["handle"]]
            kind = (existing[0].get("type") if existing else
                    "INSERT" if it.get("block") else
                    "ARC" if it.get("free_end") else "LINE")
            srcs.append({"handle": it["handle"], "type": kind,
                         "layer": it.get("layer"), "role": "洞口候选"})
        for s in it.get("srcs") or ():
            if s.get("handle") and not any(
                    old.get("handle") == s["handle"] and old.get("type") == s.get("type")
                    for old in srcs):
                srcs.append(dict(s))
        for m in it.get("methods") or ():
            if m not in methods:
                methods.append(m)

    # 双扇门：同一洞口两条开启弧 -> 1 樘，铰点取相距最远的那一对。
    # 同一条弧被"门块"和"弧线"两条路径各记一次时两个铰点会重合，按单扇处理。
    uniq = []
    for a in arcs:
        if not any(abs(a["hinge"][0] - u[0]) < 1.0 and abs(a["hinge"][1] - u[1]) < 1.0
                   for u in uniq):
            uniq.append(a["hinge"])
    if len(uniq) >= 2:
        best = max(((p, q) for i, p in enumerate(uniq) for q in uniq[i + 1:]),
                   key=lambda pq: math.hypot(pq[1][0] - pq[0][0], pq[1][1] - pq[0][1]))
        hinge_pairs = [list(best[0]), list(best[1])]
        subtype = subtype or "双扇平开门"
        swing = swing or {"type": "双扇", "direction": "两扇对开"}
    elif uniq:
        hinge_pairs = [list(uniq[0])]
    else:
        c = ((axis[0][0] + axis[1][0]) / 2.0, (axis[0][1] + axis[1][1]) / 2.0)
        hinge_pairs = [list(items[0].get("insert") or c)] if items else [list(c)]

    rec = {
        "block_evidence": next((it.get("block_evidence") for it in items
                                if it.get("block_evidence")), None),
        "block": block, "subtype": subtype or ("窗" if bucket["category"] == "window"
                                               else "平开门"),
        "layer": layer, "attr": attr,
        "insert": hinge_pairs[0] if hinge_pairs else list(
            ((axis[0][0] + axis[1][0]) / 2.0, (axis[0][1] + axis[1][1]) / 2.0)),
        "center": [(axis[0][0] + axis[1][0]) / 2.0, (axis[0][1] + axis[1][1]) / 2.0],
        "rotation": next((it.get("rotation") for it in items if it.get("rotation")),
                         line.get("angle") or 0.0),
        "axis": axis, "opening": axis, "width": width,
        "thickness": round(float(th.get("thickness") or 0.0), 1),
        "hinges": hinge_pairs, "swing": swing,
        "handle": handles[0] if handles else None,
        "handles": sorted({h for h in handles if h}),
        "srcs": srcs, "sources": sources,
        "methods": sorted(set(methods)) or ["layer"],
        "merged": len(items),
        "openings_width": width,
        "geometry_state": "unanchored" if bucket.get("unanchored") else "observed",
        "unanchored": bool(bucket.get("unanchored")),
        "free_end": next((it.get("free_end") for it in items if it.get("free_end")), None),
        "no_leaf": any(it.get("no_leaf") for it in items),
        "judged_role": (judged or {}).get("judged_role"),
        "judge": (judged or {}).get("judge"),
        "case_kind": (judged or {}).get("case_kind"),
    }
    # 每樘洞口的"解释"：由证据组合给出，而不是由加法条数给出
    rec["explain"] = {
        "block": bool(block), "attr": bool(attr),
        "arcs": len(arcs),
        "double": len(arcs) >= 2,
        "judged": bool(rec["judged_role"]),
        "sources": list(sources),
    }
    if bucket["category"] == "window" and not block:
        rec["subtype"] = "窗（线束）"
    return rec


def split(openings) -> tuple[list, list]:
    """统一洞口 -> (门类, 窗类)；分类只看证据，不看来源列表。"""
    doors, windows = [], []
    for o in openings:
        is_window = ("window_block" in o["sources"]
                     or ("window_geom" in o["sources"] and "door_block" not in o["sources"]
                         and "swing_arc" not in o["sources"]))
        (windows if is_window else doors).append(o)
    return doors, windows


def summary(result) -> dict:
    st = (result or {}).get("stats") or {}
    return {"openings": st.get("openings"), "merged": st.get("merged"),
            "dropped": st.get("dropped"), "unplaced": st.get("unplaced"),
            "thresholds": st.get("thresholds")}
