"""洞口：候选 → 分区证据 → 双路径接受 → 互斥选择。

证据按来源分组（S 语义 / G 构造 / T 拓扑 / W 墙体），接受分两条路径：
- 可信块门：S + W 即可，门扇几何缺失只记 unavailable，不作否证
- 炸开几何门：需要 G（铰点+门扇闭合+摆动）且 (洞口构造 或 空间服务)
"""
from __future__ import annotations

from . import geometry as GEO
from . import spaces as SPACES
from . import wallmodel

POS, NEG, UNAVAILABLE = "positive", "negative", "unavailable"


def _seg(item, kind):
    seg = item.get("opening") if kind == "door" else item.get("axis")
    if not seg:
        g = item.get("geometry") or {}
        seg = g.get("opening") if kind == "door" else g.get("axis")
    return seg


def _local(line, x, y):
    """世界点 -> (沿墙坐标 u, 法向偏移 v)。

    ``u`` 是**绝对**沿墙坐标，与 ``line["spans"]`` 里 ``lo``/``hi`` 同一量纲。
    早期版本这里减掉了 ``line["lo"]``，在原点附近看不出问题，但坐标量级到 1e7
    时 ``_in_gap`` 会退化成恒真（"洞口中"永远成立），把几何证据废掉。
    """
    return wallmodel.local(line, x, y)


def _in_gap(line, lo, hi, tol=50.0):
    """洞口是否落在该墙线的实体缺口里（缺口可观测时才算 positive）。

    ``lo``/``hi`` 与 ``line["spans"]`` 同为绝对沿墙坐标。三种算"在缺口里"：
    两实体之间、墙线起点之前、墙线终点之后——共同点是该处确实没有墙实体。
    """
    spans = sorted(line["spans"], key=lambda s: s["lo"])
    if not spans:
        return False
    for a, b in zip(spans, spans[1:]):
        if a["hi"] - tol <= lo and hi <= b["lo"] + tol:
            return True
    return hi <= spans[0]["lo"] + tol or lo >= spans[-1]["hi"] - tol


def collect(item, kind, lines, thick, cfg, spaces=None):
    ev = {}
    seg = _seg(item, kind)
    if item.get("block"):
        ev["block"] = POS
    # 语义裁决 = 一条独立证据（模型说这是门/窗），但**不能单独成立**：
    # 还要有墙体与缺口几何背书，见 classify 的 judge 分支。
    if str(item.get("judged_role") or "").lower() in ("door", "window", "opening"):
        ev["judge"] = POS
    if not seg or len(seg) != 2:
        return ev

    (x1, y1), (x2, y2) = seg
    hinge = (x1, y1) if kind == "door" else ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

    line = wallmodel.nearest_line(lines, hinge[0], hinge[1], 2.5 * thick)
    if line is None:
        state = NEG if lines else UNAVAILABLE
        ev["wall_proximity"] = state
        ev["span_overlap"] = state
        ev["opening_geometry"] = UNAVAILABLE
        ev["hinge_jamb"] = UNAVAILABLE
        # 墙没识别出来不等于洞口不存在：空间证据仍然可以给出服务关系
        ev["space_connection"], ev["space_detail"] = _service(seg, thick, spaces)
        return ev

    half = max(thick / 2.0, 1.0)
    lim = cfg.near_wall_factor * half
    u, v = _local(line, hinge[0], hinge[1])
    u2, v2 = _local(line, x2, y2)
    lo, hi = min(u, u2), max(u, u2)

    ev["wall_proximity"] = POS if abs(v) <= lim else NEG
    ev["span_overlap"] = (POS if line["lo"] - lim <= lo and hi <= line["hi"] + lim
                          else NEG)
    ev["opening_geometry"] = POS if _in_gap(line, lo, hi) else NEG

    if kind == "door":
        leaf = POS if abs(v2) <= 1.2 * half else NEG
        free = item.get("free_end")
        if free:
            _, vf = _local(line, free[0], free[1])
            swing = POS if abs(vf) >= 1.0 * half else NEG
        else:
            swing = UNAVAILABLE
        ev["leaf_returns"] = leaf
        ev["swing_clear"] = swing
        ev["hinge_jamb"] = (POS if (leaf == POS and swing == POS)
                            else (NEG if leaf == NEG else UNAVAILABLE))
        ev["space_connection"], ev["space_detail"] = _service(seg, thick, spaces)
    else:
        # 窗扇应与墙同向：比较方向向量而不是比较 H/V 标签
        ux, uy = line.get("dir") or ((1.0, 0.0) if line["orient"] == "H" else (0.0, 1.0))
        dx, dy = GEO.norm_dir(x2 - x1, y2 - y1)
        aligned = abs(ux * dy - uy * dx) <= 0.15
        ev["wall_alignment"] = POS if aligned else NEG
        ev["leaf_returns"] = UNAVAILABLE
        ev["swing_clear"] = UNAVAILABLE
        ev["hinge_jamb"] = UNAVAILABLE
        ev["space_connection"], ev["space_detail"] = _service(seg, thick, spaces)
    return ev


def _service(seg, thick, spaces):
    """洞口两侧的空间关系 -> (证据状态, 细节)。

    * 两侧都是不同空间 -> POS（门/窗服务两个空间）
    * 一侧空间、一侧室外 -> POS（外墙洞口）
    * 两侧同一个空间 -> NEG（洞口没有分隔任何空间，多半不是门窗）
    * 两侧都不在任何空间里 -> NEG（未围合）
    * 没有空间面域（节点缺失/图纸无墙）-> UNAVAILABLE，不作否证
    """
    if not spaces:
        return UNAVAILABLE, "no_space_seed"
    a, b = SPACES.connection(spaces, seg, thick)
    if a is None and b is None:
        # 两侧都找不到空间时无法区分"墙网缺失"和"真的在室外"：判 UNAVAILABLE。
        # 否则"墙没认出→面域不闭合→洞口被否→面域更难闭合"会连锁放大。
        return UNAVAILABLE, "outside_both_unsealed"
    if a is not None and b is not None:
        if a.get("id") == b.get("id"):
            return NEG, f"same_space:{a.get('id')}"
        return POS, f"{a.get('id')}|{b.get('id')}"
    one = a or b
    return POS, f"{one.get('id')}|outside"


def score(evidence, cfg):
    total = got = 0.0
    for key, state in evidence.items():
        w = getattr(cfg, "w_" + key, 0.0)
        if w <= 0 or state == UNAVAILABLE:
            continue
        total += w
        if state == POS:
            got += w
    return (got / total) if total > 0 else None


def _overlap(a, b):
    la, lb = a["line"], b["line"]
    if la is None or lb is None:
        return False
    if la["orient"] != lb["orient"] or abs(la["pos"] - lb["pos"]) > 1.0:
        return False
    ov = min(a["hi"], b["hi"]) - max(a["lo"], b["lo"])
    return ov > 0.3 * max(1.0, min(a["hi"] - a["lo"], b["hi"] - b["lo"]))


def classify(entry, kind):
    """返回 accept / pending / reject 与原因。"""
    ev = entry["evidence"]
    trusted = ev.get("block") == POS
    anchored = ev.get("wall_proximity") == POS or ev.get("span_overlap") == POS

    if trusted:
        if anchored:
            return "accept", "trusted_block+wall"
        # 找不到墙 ≠ 门是假的：墙可能没被识别出来，降级为待核而不是丢弃
        return "pending", "trusted_block_wall_unconfirmed"

    # 语义裁决背书 + 墙体/缺口几何背书 -> 采纳。
    # 抽象画法（推拉门、隐形门、只画一条斜线、干脆不画）没有铰点/门扇/摆弧，
    # 走不到 hinge_jamb 那条路径；这里的第二重证据是"洞口确实开在墙的缺口上"。
    if ev.get("judge") == POS and anchored and ev.get("opening_geometry") == POS:
        return "accept", "judge+wall_gap"

    if kind == "window":
        if ev.get("wall_alignment") == POS and anchored:
            return "accept", "window_lines+wall"
        return ("pending" if ev.get("wall_alignment") == UNAVAILABLE else "reject"), "window_unconfirmed"

    if ev.get("hinge_jamb") == POS:
        if ev.get("opening_geometry") == POS:
            return "accept", "arc+hinge_jamb+opening"
        if ev.get("space_connection") == POS:
            return "accept", "arc+hinge_jamb+space"
        if ev.get("space_connection") == UNAVAILABLE:
            return "pending", "arc_hinge_ok_no_service"
    return "reject", "arc_insufficient"


def _entry(item, kind, lines, thick, cfg, spaces=None):
    seg = _seg(item, kind)
    e = {"item": item, "line": None, "lo": 0.0, "hi": 0.0}
    if seg and len(seg) == 2:
        (x1, y1), (x2, y2) = seg
        hinge = (x1, y1) if kind == "door" else ((x1 + x2) / 2.0, (y1 + y2) / 2.0)
        e["line"] = wallmodel.nearest_line(lines, hinge[0], hinge[1], 2.5 * thick)
        if e["line"] is not None:
            u1, _ = _local(e["line"], x1, y1)
            u2, _ = _local(e["line"], x2, y2)
            e["lo"], e["hi"] = min(u1, u2), max(u1, u2)
    e["kind"] = kind
    e["evidence"] = collect(item, kind, lines, thick, cfg, spaces)
    e["score"] = score(e["evidence"], cfg)
    e["status"], e["reason"] = classify(e, kind)
    return e


def _typed(item):
    """构件是否有编号（门联窗的窗扇有编号，门洞上的框线束没有）。"""
    if not isinstance(item, dict):
        return False
    attrs = item.get("attributes") or {}
    return bool(str(item.get("type_code") or attrs.get("type_code") or "").strip())


def _resolve_conflicts(door_entries, window_entries, min_ratio=0.7):
    """门窗跨类别互斥：同一墙线上洞口区间几乎重合时，只保留一种解释。

    * 窗**没有编号/块语义**且与已采纳门洞重叠 >= ``min_ratio`` -> 视为门洞上的
      框线束，丢弃（办公楼 WIN-13/14 就是这一类）；
    * 窗有编号（门联窗的窗扇）或只是与门相邻不重叠 -> 保留，允许复合构件共存。
    """
    kept, dropped = [], []
    for w in window_entries:
        item = w["item"]
        bad = False
        if w["line"] is not None and not _typed(item):
            for d in door_entries:
                if d["line"] is None:
                    continue
                if d["line"]["orient"] != w["line"]["orient"]:
                    continue
                if abs(d["line"]["pos"] - w["line"]["pos"]) > 1.0:
                    continue
                span = min(w["hi"] - w["lo"], d["hi"] - d["lo"])
                if span <= 0:
                    continue
                ov = min(w["hi"], d["hi"]) - max(w["lo"], d["lo"])
                if ov >= min_ratio * span:
                    bad = True
                    break
        if bad:
            w["reason"] = "door_window_conflict"
            dropped.append(w)
        else:
            kept.append(w)
    return [w["item"] for w in kept], kept, dropped


def select(doors, windows, lines, thick, cfg, spaces=None):
    report = {
        "candidates": {"doors": len(doors), "windows": len(windows)},
        "generated": len(doors) + len(windows),
        "wall_associated": 0,
        "structurally_supported": 0,
        "accepted": {"doors": 0, "windows": 0},
        "pending": {"doors": 0, "windows": 0},
        "space_evidence": {"positive": 0, "negative": 0, "unavailable": 0},
        "rejected": {},
        "top_rejected": [],
    }

    def pick(items, kind):
        pool = [_entry(it, kind, lines, thick, cfg, spaces) for it in items]
        for e in pool:
            st = e["evidence"].get("space_connection")
            key = {POS: "positive", NEG: "negative"}.get(st, "unavailable")
            report["space_evidence"][key] += 1
        report["wall_associated"] += sum(1 for e in pool if e["line"] is not None)
        report["structurally_supported"] += sum(1 for e in pool if e["status"] == "accept")
        pool.sort(key=lambda e: (-(e["score"] if e["score"] is not None else -1.0),
                                 -sum(1 for v in e["evidence"].values() if v == POS)))
        accepted, rejected = [], []
        for e in pool:
            if e["status"] == "pending":
                report["pending"]["doors" if kind == "door" else "windows"] += 1
                # A verified four-parallel-stroke window block is a real
                # *candidate* even if the wall model missed its host. Keep it
                # uncertain, as we already do for trusted door blocks. Generic
                # window-line candidates still require wall confirmation.
                trusted_door = kind == "door" and e["evidence"].get("block") == POS
                trusted_window = (kind == "window" and
                                  str(e["item"].get("block_evidence") or "")
                                  .startswith("geometric:window_block_parallel_strokes") and
                                  e["evidence"].get("block") == POS)
                if trusted_door or trusted_window:
                    e["item"]["_pending"] = True
                    accepted.append(e)
                else:
                    rejected.append(e)
                continue
            if e["status"] == "reject":
                report["rejected"][e["reason"]] = report["rejected"].get(e["reason"], 0) + 1
                rejected.append(e)
                continue
            if any(_overlap(e, a) for a in accepted):
                e["reason"] = "overlap_with_selected"
                report["rejected"][e["reason"]] = report["rejected"].get(e["reason"], 0) + 1
                rejected.append(e)
                continue
            accepted.append(e)
        return [a["item"] for a in accepted], accepted, rejected, pool

    picked_doors, ent_doors, rej_doors, pool_doors = pick(doors, "door")
    picked_windows, ent_windows, rej_windows, pool_windows = pick(windows, "window")
    # 跨类别物理洞口互斥：同一洞口不能既是门又是窗（真实门联窗除外）
    picked_windows, ent_kept, conflicts = _resolve_conflicts(ent_doors, ent_windows)
    rej_windows.extend(conflicts)
    report["conflicts"] = {"door_window": len(conflicts)}
    report["accepted"]["doors"] = len(picked_doors)
    report["accepted"]["windows"] = len(picked_windows)
    rej = rej_doors + rej_windows
    rej.sort(key=lambda e: -(e["score"] if e["score"] is not None else -1.0))
    report["top_rejected"] = [
        {"handle": e["item"].get("handle"), "layer": e["item"].get("layer"),
         "reason": e["reason"],
         "score": None if e["score"] is None else round(e["score"], 3),
         "evidence": e["evidence"],
         "line": None if e["line"] is None else
         {"orient": e["line"]["orient"], "pos": e["line"]["pos"],
          "lo": e["line"]["lo"], "hi": e["line"]["hi"]}}
        for e in rej[:20]
    ]
    report["funnel"] = (f"generated {report['generated']} -> wall {report['wall_associated']} "
                        f"-> structural {report['structurally_supported']} "
                        f"-> accepted {report['accepted']['doors'] + report['accepted']['windows']}")
    return {"doors": picked_doors, "windows": picked_windows, "report": report,
            "entries": pool_doors + pool_windows}
