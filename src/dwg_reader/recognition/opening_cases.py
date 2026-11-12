"""洞口候选枚举 + 把模型裁决落成几何（程序算坐标，模型只定性质）。

候选来源（全部确定性枚举，不判性质）：

* ``gap``     墙模型里**同一道墙线两实体之间的缺口**（墙在洞口处断开，这是唯一
              与画法无关的洞口证据：不管门画成弧线、推拉线还是干脆不画）；
* ``belt``    两侧面域相邻、中间没有任何墙段的窄带（洞/通道没画墙）；
* ``pending`` 现有 doors/windows 里还没定性的候选（让模型复核归属）。

每个候选都带一小撮**已算好的数字**（宽度、墙厚、两侧有无面域、附近有无弧线），
模型只在这些候选里选门/窗/洞口/无；几何由本模块按候选重新算。

关键实现约束（踩过的坑）：

* ``gap`` 必须读 ``wall_model["gaps"]``，不能从 ``walls_seg`` 自己找断口——墙段
  在那里没有 ``pos/dir``，且后续 ``merge_across_openings`` 会把断口抹平；
* 候选清单是**限额**分配的，不能让几百条垃圾门候选把缺口候选挤出预算；
* 宽度不可信的候选（< ``PEND_MIN``）直接不进清单：图上那些 10mm/20mm 的"门"
  是家具块里的弧线，不是洞口。
"""
from __future__ import annotations

import math
import os

from . import geometry as GEO
from . import wallmodel as WM

#: 洞口宽度可行域（mm）：窄于 250 不是门窗，宽于 3000 不是单樘洞口
GAP_MIN, GAP_MAX = 250.0, 3000.0
BELT_MIN, BELT_MAX = 400.0, 3000.0
PEND_MIN, PEND_MAX = 250.0, 6000.0
#: 候选总量上限：**只作病态图纸的安全阀**，正常图纸应当全量送模型。
#: 定额截断会把真实洞口饿死（300 段墙的平面图就有上百个缺口），所以这里不再
#: 按来源配额切，只保留一个远高于实际需求的硬上限（可用环境变量覆盖）。
MAX_CASES = int(os.environ.get("DWG_OPENING_MAX_CASES", "400"))
BELT_MAX_FACES = 400
BELT_NEIGHBORS = 8
#: 排序用的"典型洞口宽度"：偏离越小越先送模型
TYPICAL_WIDTH = 900.0

#: 规则版本：写进节点输出，便于回溯这批结论是哪一版规则给的
RULE_VERSION = "opening-rules/1.0"


# ------------------------------------------------------------------ 入口
def candidates(*, snapshot=None, wall_model=None, walls_seg=(), spaces=None,
               doors=(), windows=()) -> list[dict]:
    """枚举洞口候选（编号 + 区间 + 上下文数字 + 分诊状态）。"""
    spans, gaps = _spans_gaps(wall_model, walls_seg)
    faces = (spaces or {}).get("faces") or ()
    arcs = _arcs(snapshot)
    gap_items = _take(_gap_cases(gaps, spans, faces, arcs), MAX_CASES)
    rest = _absorb_pending(_pending_cases(doors, windows), gap_items)
    # 顺序即优先级：结构证据（缺口）先判，语义复核（旧候选）最后
    out = (gap_items
           + _take(_belt_cases(spans, faces, arcs), MAX_CASES)
           + rest)[:MAX_CASES]
    for i, item in enumerate(out, 1):
        item["id"] = f"opc:{i}"
        item["triage"], _rule = triage(item)
    return out


def _spans_gaps(wall_model, walls_seg):
    if wall_model:
        return (list(wall_model.get("spans") or ()),
                list(wall_model.get("gaps") or ()))
    segs = [w for w in (walls_seg or ()) if w.get("start") and w.get("end")]
    if not segs:
        return [], []
    ts = sorted(w["thickness"] for w in segs if w.get("thickness"))
    model = WM.build(segs, ts[len(ts) // 2] if ts else None)
    return list(model["spans"]), list(model["gaps"])


def _take(items, quota):
    """按"证据强弱 + 典型度"排序后截断，保证结果确定。"""
    return sorted(items, key=_rank)[:quota]


def _rank(item):
    return (-int(bool(item.get("faces_both_sides"))),
            abs(float(item.get("width_mm") or 0.0) - TYPICAL_WIDTH),
            str(item.get("kind")), float((item.get("axis") or [[0, 0]])[0][0]),
            float((item.get("axis") or [[0, 0]])[0][1]))


# ------------------------------------------------------------------ 分诊
def triage(item):
    """先判能不能在程序里定：返回 (state, (role, why) | None)。

    只有 ``uncertain`` 才值得送模型——"简单"的判断不该占用模型预算，
    也不该让模型有机会把已经确定的结论改掉。
    """
    width = float(item.get("width_mm") or 0.0)
    if not (GAP_MIN <= width <= GAP_MAX):
        return "decided", ("none", "宽度不在洞口域")
    src, arc = item.get("source_kind"), item.get("arc_nearby")
    both, thick = item.get("faces_both_sides"), item.get("thickness_mm")
    if src == "door" and arc:
        return "decided", ("door", "门弧+墙缺口")
    if src == "window" and thick:
        return "decided", ("window", "窗线束与墙同厚")
    if both is False and not arc:
        return "decided", ("none", "两侧无面域且无门窗几何")
    return "uncertain", None


def confirm(role, item):
    """模型判"门"必须有几何背书，否则不算门。

    模型只回答"像不像门"，"是不是门"由程序按证据复核：开启弧、门块、或本来就
    来自门节点的候选（有 handle/编号）三者至少一个。缺了背书的"门"降级为
    ``none``——**不进门数**。理由：模型在墙缺口上"看起来像门"就判门，是这套
    流程里最大的波动源（同一张图重跑会多出 1~2 樘门）。
    """
    if role != "door":
        return role, None
    backed = (bool(item.get("arc_nearby")) or bool(item.get("block"))
              or item.get("source_kind") == "door")
    if backed:
        return role, None
    return "none", "判门但无门弧/门块/门候选来源，不计入门"


def decide(items, *, digest="", api_key=None, use_model=True):
    """规则先判，只有真正疑难的才问模型；**不落盘**，每次运行都是干净重算。

    不做结果入库是刻意的：一旦把判定固化到磁盘，图纸或规则一改，旧结论就会被
    当成新证据复用，反而掩盖问题。稳定性靠的是"规则吃掉大部分候选 + 同一输入
    同一模型"，靠缓存堆出来的稳定是假稳定。
    """
    out: dict[str, dict] = {}
    todo: list[dict] = []
    from_rule = 0
    for item in items or ():
        rid = str(item.get("id"))
        state, rule = triage(item)
        if state == "decided" and rule:
            out[rid] = {"role": rule[0], "reason": rule[1], "source": "rule"}
            from_rule += 1
            continue
        todo.append(item)

    stats = {"items": len(items or ()), "from_rule": from_rule,
             "to_model": len(todo), "rule_version": RULE_VERSION}
    if todo and use_model:
        from . import llm_interpret as LINT

        res = LINT.judge("opening", todo, digest=digest, api_key=api_key)
        by_id = {str(it.get("id")): it for it in todo}
        downgraded = 0
        for rid, dec in (res.get("decisions") or {}).items():
            item = by_id.get(str(rid))
            if item is None:
                continue
            role, note = confirm(str((dec or {}).get("role") or "").lower(), item)
            rec = {"role": role, "reason": (note or (dec or {}).get("reason")),
                   "source": "model"}
            if note:
                rec["model_role"] = (dec or {}).get("role")
                downgraded += 1
            out[str(rid)] = rec
        stats.update({"model_status": res.get("status"),
                      "model_calls": res.get("calls"),
                      "model_errors": res.get("errors"),
                      "door_downgraded": downgraded})
    stats["decided"] = len(out)
    return out, stats


# ------------------------------------------------------------------ gap：墙线缺口
def _gap_cases(gaps, spans, faces, arcs) -> list[dict]:
    """墙模型缺口 -> 洞口候选（洞口的唯一无条件几何证据）。"""
    out = []
    for g in gaps or ():
        lo, hi = float(g.get("lo") or 0.0), float(g.get("hi") or 0.0)
        width = float(g.get("width") if g.get("width") is not None else hi - lo)
        if hi <= lo or not (GAP_MIN <= width <= GAP_MAX):
            continue
        d = tuple(g.get("dir") or (1.0, 0.0))
        line = {"dir": d, "nrm": (-d[1], d[0]), "pos": float(g.get("pos") or 0.0)}
        host = _host_span(spans, g)
        wall = {"dir": d, "nrm": line["nrm"],
                "thickness": (host or {}).get("thickness")}
        p1, p2 = GEO.point_at(line, lo), GEO.point_at(line, hi)
        mid = GEO.point_at(line, (lo + hi) / 2.0)
        item = _item("gap", [p1, p2], width, wall, faces, arcs, mid,
                     {"wall_pos": line["pos"], "wall_lo": lo, "wall_hi": hi,
                      "host_wall": (host or {}).get("id")})
        item["shadow_handles"] = []
        item["orient"] = g.get("orient")
        out.append(item)
    return out


def _host_span(spans, gap):
    """缺口所属的墙带（取墙厚与实体来源）。"""
    for s in spans or ():
        for a, b in (s.get("gaps") or ()):
            if abs(float(a) - float(gap.get("lo"))) <= 1.0 and \
                    abs(float(b) - float(gap.get("hi"))) <= 1.0:
                return s
    best = None
    for s in spans or ():
        if s.get("orient") != gap.get("orient"):
            continue
        if abs(float(s.get("pos") or 0.0) - float(gap.get("pos") or 0.0)) > 1.5:
            continue
        if s.get("thickness"):
            return s
        best = best or s
    return best


# ------------------------------------------------------------------ belt：无墙窄带
def _belt_cases(spans, faces, arcs) -> list[dict]:
    """两侧面域相邻、共享边界上没有任何墙段 —— 可能是没画墙的洞口/通道。"""
    fb = [f for f in faces or () if f.get("bbox")]
    if len(fb) < 2 or len(fb) > BELT_MAX_FACES:
        return []
    fb.sort(key=lambda f: (f["bbox"][0], f["bbox"][1]))
    out = []
    for i, fa in enumerate(fb):
        ba = fa["bbox"]
        for fbb in fb[i + 1:i + 1 + BELT_NEIGHBORS]:
            bb = fbb["bbox"]
            gap = _bbox_gap(ba, bb)
            if not (BELT_MIN <= gap <= BELT_MAX):
                continue
            span = _shared_span(ba, bb)
            if span is None:
                continue
            width = min(span[2] - span[0], span[3] - span[1])
            if width < BELT_MIN:
                continue
            horizontal = ba[2] <= bb[0]
            p1, p2 = _belt_axis(ba, bb, horizontal)
            mid = ((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0)
            if _wall_between(spans, mid):
                continue
            wall = {"dir": (1.0, 0.0) if horizontal else (0.0, 1.0),
                    "nrm": (0.0, 1.0) if horizontal else (1.0, 0.0),
                    "thickness": None}
            item = _item("belt", [p1, p2], width, wall, faces, arcs, mid,
                         {"face_a": fa.get("id"), "face_b": fbb.get("id")})
            item["faces_both_sides"] = True
            item["shadow_handles"] = []
            out.append(item)
    return out


def _belt_axis(ba, bb, horizontal):
    mid_x = (max(ba[0], bb[0]) + min(ba[2], bb[2])) / 2.0
    mid_y = (max(ba[1], bb[1]) + min(ba[3], bb[3])) / 2.0
    if horizontal:
        y = mid_y
        return ([ba[2], y], [max(bb[0], ba[2]), y])
    x = mid_x
    return ([x, ba[3]], [x, max(bb[1], ba[3])])


def _shared_span(ba, bb):
    x0, x1 = max(ba[0], bb[0]), min(ba[2], bb[2])
    y0, y1 = max(ba[1], bb[1]), min(ba[3], bb[3])
    if x1 > x0:
        return [x0, min(ba[1], bb[1]), x1, max(ba[3], bb[3])]
    if y1 > y0:
        return [min(ba[0], bb[0]), y0, max(ba[2], bb[2]), y1]
    return None


def _bbox_gap(ba, bb) -> float:
    dx = max(ba[0] - bb[2], bb[0] - ba[2], 0.0)
    dy = max(ba[1] - bb[3], bb[1] - ba[3], 0.0)
    return math.hypot(dx, dy)


def _wall_between(spans, point) -> bool:
    """该点附近有没有实墙（有墙就不是"没画墙的通道"）。"""
    for s in spans or ():
        if not s.get("dir"):
            continue
        u, v = WM.local(s, point[0], point[1])
        if abs(v) > (float(s.get("thickness") or 200.0) / 2.0 + 50.0):
            continue
        if s["lo"] - 50.0 <= u <= s["hi"] + 50.0:
            return True
    return False


# ------------------------------------------------------------------ pending：现有候选
def _pending_cases(doors, windows) -> list[dict]:
    """现有门/窗候选复核；尺寸不可信的（家具块弧线）不进清单。"""
    out = []
    for source, seq in (("door", doors), ("window", windows)):
        for rec in seq or ():
            seg = rec.get("opening") or rec.get("axis")
            if not seg or len(seg) != 2:
                continue
            width = _seg_len(seg)
            if not width:
                continue
            if not (PEND_MIN <= width <= PEND_MAX):
                continue
            axis = [[float(seg[0][0]), float(seg[0][1])],
                    [float(seg[1][0]), float(seg[1][1])]]
            out.append({"kind": "pending", "source_kind": source, "axis": axis,
                        "width_mm": round(width, 1),
                        "thickness_mm": rec.get("thickness"),
                        "host_wall": rec.get("wall_id"),
                        "handle": rec.get("handle"), "layer": rec.get("layer"),
                        "faces_both_sides": None,
                        "arc_nearby": bool(rec.get("free_end") or rec.get("hinges")),
                        "shadow_handles": [],
                        "bbox": _bbox(axis)})
    return out


def _absorb_pending(pending, hosts) -> list[dict]:
    """落在缺口候选上的旧候选不重复送模型：把它的实体句柄挂到缺口候选上。

    缺口是结构证据（一定有洞），旧候选是语义证据（有门弧/窗块）。同一条洞口
    只送一次，但两边的来源都保留，便于审计。
    """
    rest = []
    for p in pending:
        host = _shadow_host(p, hosts)
        if host is None:
            rest.append(p)
            continue
        if p.get("handle"):
            host.setdefault("shadow_handles", []).append(str(p["handle"]))
        if p.get("layer") and not host.get("layer"):
            host["layer"] = p.get("layer")
        host["source_kind"] = host.get("source_kind") or p.get("source_kind")
    return rest


def _shadow_host(item, hosts):
    (x1, y1), (x2, y2) = item["axis"]
    mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    for h in hosts:
        (a1, b1), (a2, b2) = h["axis"]
        tol = max(300.0, float(h.get("thickness_mm") or 200.0),
                  0.5 * float(h.get("width_mm") or 0.0))
        if _pt_seg(mx, my, a1, b1, a2, b2) <= tol and \
                _pt_seg(x1, y1, a1, b1, a2, b2) <= tol + 0.5 * item["width_mm"]:
            return h
    return None


# ------------------------------------------------------------------ 公共构件
def _item(kind, axis, width, wall, faces, arcs, mid, extra) -> dict:
    p1, p2 = axis
    item = {"kind": kind, "axis": [[float(p1[0]), float(p1[1])],
                                   [float(p2[0]), float(p2[1])]],
            "width_mm": round(float(width), 1),
            "thickness_mm": wall.get("thickness"),
            "faces_both_sides": False,
            "arc_nearby": _arc_near(mid, arcs),
            "bbox": _bbox(axis)}
    thr = float(wall.get("thickness") or 200.0)
    d = tuple(wall.get("dir") or (1.0, 0.0))
    n = tuple(wall.get("nrm") or (-d[1], d[0]))
    half = thr / 2.0 + 300.0
    side_a = _face_at(faces, mid[0] + n[0] * half, mid[1] + n[1] * half)
    side_b = _face_at(faces, mid[0] - n[0] * half, mid[1] - n[1] * half)
    item["faces_both_sides"] = bool(side_a and side_b)
    item["face_a"], item["face_b"] = side_a, side_b
    item["side_a"], item["side_b"] = bool(side_a), bool(side_b)
    item.update(extra)
    return item


def _face_at(faces, x, y):
    for f in faces or ():
        b = f.get("bbox")
        if b and b[0] <= x <= b[2] and b[1] <= y <= b[3]:
            return f.get("id")
    return None


def _arcs(snapshot) -> list[tuple]:
    out = []
    for e in (snapshot or {}).get("entities") or ():
        if e.get("type") != "ARC":
            continue
        c = e.get("center")
        if isinstance(c, (list, tuple)) and len(c) >= 2:
            out.append((float(c[0]), float(c[1]), float(e.get("radius") or 0.0)))
    return out


def _arc_near(mid, arcs, tol=1500.0) -> bool:
    for (x, y, r) in arcs or ():
        if math.hypot(mid[0] - x, mid[1] - y) <= tol + r:
            return True
    return False


def _pt_seg(px, py, x1, y1, x2, y2) -> float:
    dx, dy = x2 - x1, y2 - y1
    den = dx * dx + dy * dy
    if den <= 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / den))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _seg_len(seg) -> float:
    return math.hypot(float(seg[1][0]) - float(seg[0][0]),
                      float(seg[1][1]) - float(seg[0][1]))


def _bbox(seg) -> list:
    xs = [float(p[0]) for p in seg]
    ys = [float(p[1]) for p in seg]
    return [min(xs), min(ys), max(xs), max(ys)]


# ------------------------------------------------------------------ 裁决落地
def to_records(decisions: dict, items) -> dict:
    """模型的裁决 -> 构件记录（坐标全部按候选重算，模型只决定类别）。

    产出的记录必须能直接喂给下游（``openings.select`` / ``walls_merge`` /
    ``assemble``），所以这里补齐它们会读到的字段（``insert``/``center``/
    ``layer``/``subtype``/``rotation``/``handle``），并标 ``judged_role`` 让
    洞口选择认得"这条洞口有语义裁决背书"。
    """
    by_id = {str(it.get("id")): it for it in items or ()}
    doors, windows, rejected = [], [], []
    for rid, dec in (decisions or {}).items():
        item = by_id.get(str(rid))
        if item is None:
            continue
        role = str((dec or {}).get("role") or "").lower()
        if role not in ("door", "window", "opening"):
            rejected.append({"reason": "JUDGED_NONE", "item": rid,
                             "why": (dec or {}).get("reason")})
            continue
        rec = _record("window" if role == "window" else "door", role, rid, item,
                      (dec or {}).get("reason"))
        (windows if role == "window" else doors).append(rec)
    return {"doors": doors, "windows": windows, "rejected": rejected,
            "counts": {"doors": len(doors), "windows": len(windows),
                       "rejected": len(rejected)}}


def _record(kind, role, rid, item, reason) -> dict:
    axis = [[float(p[0]), float(p[1])] for p in item["axis"]]
    cx, cy = (axis[0][0] + axis[1][0]) / 2.0, (axis[0][1] + axis[1][1]) / 2.0
    width = float(item.get("width_mm") or _seg_len(axis))
    thick = float(item.get("thickness_mm") or 0.0)
    handles = [str(h) for h in (item.get("handle"),) if h]
    handles += [str(h) for h in (item.get("shadow_handles") or ()) if h]
    for key in ("host_wall", "host_wall_b"):
        if str(item.get(key) or "").startswith("ev:"):
            handles.append(str(item[key]).split(":", 1)[-1])
    rotation = round(math.degrees(math.atan2(axis[1][1] - axis[0][1],
                                             axis[1][0] - axis[0][0])) % 180.0, 1)
    rec = {"id": f"{kind}:judged:{rid}", "kind": kind,
           "judged_role": role, "judge_item": rid,
           "case_kind": item.get("kind"),
           "axis": axis, "opening": axis, "center": [cx, cy],
           "insert": [cx, cy], "width": width, "thickness": thick,
           "rotation": rotation, "bbox": list(item.get("bbox") or _bbox(axis)),
           "layer": item.get("layer") or "JUDGED",
           "handle": handles[0] if handles else f"judged:{rid}",
           "handles": sorted(set(handles)),
           "block": None, "attr": None, "type_code": None,
           "srcs": [{"handle": h, "type": "LINE", "role": "judged opening"}
                    for h in sorted(set(handles))],
           "free_end": None, "hinges": [],
           "geometry_state": "derived",
           "judge": {"item": rid, "reason": reason,
                     "case_kind": item.get("kind"),
                     "faces_both_sides": item.get("faces_both_sides"),
                     "arc_nearby": item.get("arc_nearby")}}
    if kind == "door":
        # 模型判"无门扇洞口"时仍是门类洞口（进墙网、进算量），但必须标明无门扇，
        # 否则会把它当成有扇的门统计。
        rec["subtype"] = "洞口（无门扇，语义裁决）" if role == "opening" \
            else "洞口（语义裁决，有门扇证据）"
        rec["swing"] = "unknown"
        rec["no_leaf"] = role == "opening"
    else:
        rec["label"] = None
    return rec
