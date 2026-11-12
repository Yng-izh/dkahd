"""算子执行器：统一签名 + 目标约束 + 来源校验 + 只返回补丁。

契约（与 ``operator_specs`` 对齐，LLM 无法绕过）：

    execute_operator(name, *, target_ids, region, evidence_index, candidate_state, action)

1. 只取 ``target_ids`` 指向的局部证据（候选会**穿透到它引用的原始图元**，不再整层重扫）；
2. 只调用已注册的确定性算子；
3. 生成结果必须能追溯到本次输入（handle 交集非空），否则丢弃；
4. 返回增量补丁，**不修改**传入的候选状态。

补丁键（闭集）：``added / removed / drop_ids / updated / walls / columns / openings /
faces / labels / unresolved / ignored / stats / inferred / reason``。
"""
from __future__ import annotations

import math

from . import evidence as EVID
from . import geometry as GEO
from . import operator_specs as OPSPEC
from .operator_specs import REGISTRY

#: 生成物必须能回溯到这些 kinds 的输入
_SOURCE_KINDS = ("line_support", "contour", "hatch", "entity", "wall_candidate",
                 "opening_candidate")

#: 单次局部执行最多取多少条线（预算，不是判据）
MAX_LOCAL_LINES = 800


def local_evidence(index: dict, *, target_ids=(), region=None) -> dict:
    """按目标 id（并集区域）取局部证据，转成算子库认识的 prims 结构。"""
    items = index.get("items") or {}
    picked = {str(t): items[str(t)] for t in (target_ids or ()) if str(t) in items}
    if not picked and region:
        picked = {rid: rec for rid, rec in items.items()
                  if EVID.bbox_of(rec) and _in_region(EVID.bbox_of(rec), region)}
    picked = _expand_through(index, picked)

    lines, closed, hatches, handles, layers = [], [], [], set(), set()
    for rid, rec in picked.items():
        kind = rec.get("kind")
        if rec.get("layer"):
            layers.add(rec["layer"])
        for h in EVID.handles_of(rec):
            handles.add(h)
        if kind == "line_support" and rec.get("pos") is not None:
            if len(lines) >= MAX_LOCAL_LINES:
                continue
            lines.append({"handle": rec.get("handle"), "layer": rec.get("layer"),
                          "dir": rec.get("dir"), "nrm": rec.get("nrm"),
                          "pos": rec.get("pos"), "lo": rec.get("lo"),
                          "hi": rec.get("hi"), "angle": rec.get("angle"),
                          "length": rec.get("length"),
                          "orient": rec.get("orient") or "H"})
        elif kind == "contour":
            closed.append({"outline": rec.get("outline"), "bbox": rec.get("bbox"),
                           "n": rec.get("n"), "has_bulge": rec.get("has_bulge"),
                           "shape": rec.get("shape"), "layer": rec.get("layer"),
                           "handle": rec.get("handle")})
        elif kind == "hatch":
            hatches.append({"handle": rec.get("handle"), "layer": rec.get("layer"),
                            "loops": rec.get("loops") or ()})
    prims = {"lines": lines, "closed": closed, "arcs": [], "anchors": [],
             "ticks": [], "hatches": hatches}
    return {"prims": prims, "handles": handles, "layers": layers or None,
            "target_ids": list(picked)}


def _expand_through(index: dict, picked: dict) -> dict:
    """候选 id -> 它引用的原始图元 -> 这些图元的线支撑（局部证据穿透）。

    少了这一步，"重试配对"这类算子拿到的是空证据（旧的 PROVENANCE_LOST 根因之一）。
    """
    items = index.get("items") or {}
    rel = index.get("relations") or []
    by_dst: dict[str, list[str]] = {}
    by_src: dict[str, list[str]] = {}
    for r in rel:
        by_src.setdefault(str(r.get("src")), []).append(str(r.get("dst")))
        by_dst.setdefault(str(r.get("dst")), []).append(str(r.get("src")))
    out = dict(picked)
    frontier = list(picked)
    for _ in range(2):                       # 只穿透两层，避免把整图拉进来
        nxt: list[str] = []
        for rid in frontier:
            for dst in list(by_src.get(rid) or ()) + list(by_dst.get(rid) or ()):
                if dst in out or dst not in items:
                    continue
                out[dst] = items[dst]
                nxt.append(dst)
            handle = items.get(rid, {}).get("handle")
            if handle:
                for sib in by_dst.get(f"ev:{handle}", ()):
                    if sib not in out and sib in items:
                        out[sib] = items[sib]
                        nxt.append(sib)
        frontier = nxt
        if not frontier:
            break
    return out


def _empty_patch(reason: str | None = None) -> dict:
    return {"added": [], "removed": [], "drop_ids": [], "updated": [],
            "walls": [], "columns": [], "openings": [], "faces": [], "labels": [],
            "unresolved": [], "ignored": [], "stats": {}, "dropped": [],
            "reason": reason}


def _provenance_ok(generated, local) -> bool:
    """生成物是否可追溯到本次输入：handle 有交集，或来自同批轮廓/填充。"""
    if not generated:
        return False
    want = set(local.get("handles") or ())
    for rec in generated:
        got = {h for h in EVID.handles_of(rec) if h}
        if not got and rec.get("handles"):
            got = {h for h in rec["handles"] if h}
        if not got or (want and not (got & want)):
            return False
    return True


def _wall_record(pair: dict, name: str = "wall") -> dict:
    """平行线对 -> 墙候选（坐标一律由算法算，和 LLM 无关）。"""
    dirv = pair.get("dir") or (1.0, 0.0)
    nrm = pair.get("nrm") or (0.0, 1.0)
    pos = float(pair.get("pos") or 0.0)
    lo, hi = float(pair.get("lo") or 0.0), float(pair.get("hi") or 0.0)
    p1 = [pos * nrm[0] + lo * dirv[0], pos * nrm[1] + lo * dirv[1]]
    p2 = [pos * nrm[0] + hi * dirv[0], pos * nrm[1] + hi * dirv[1]]
    handles = [h for h in (pair.get("handles") or ()) if h]
    return {"id": name, "start": [round(p1[0], 2), round(p1[1], 2)],
            "end": [round(p2[0], 2), round(p2[1], 2)],
            "length": round(hi - lo, 1), "thickness": pair.get("thickness"),
            "dir": list(dirv), "nrm": list(nrm), "pos": pos, "lo": lo, "hi": hi,
            "center": pos,
            "angle": round(math.degrees(math.atan2(dirv[1], dirv[0])) % 180.0, 3),
            "orient": "H" if abs(dirv[0]) >= abs(dirv[1]) else "V",
            "bbox": EVID.bbox_of({"start": p1, "end": p2}),
            "handles": handles, "srcs": [{"handle": h, "role": "wall_face"}
                                         for h in handles],
            "geometry_state": "derived", "status": "candidate"}


# ------------------------------------------------------------------ 各算子实现
def _op_regenerate_opening(local, action, state=None):
    """线支撑 -> 洞口候选：直接取"相邻平行线对"的沿向重叠区间（不依赖整层重扫）。"""
    lines = [l for l in local["prims"]["lines"] if l.get("pos") is not None]
    groups: dict[tuple, list] = {}
    for l in lines:
        groups.setdefault((l.get("layer"), round(float(l.get("angle") or 0.0), 2)),
                          []).append(l)
    out = []
    for (layer, _angle), members in groups.items():
        members.sort(key=lambda l: float(l["pos"]))
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                gap = abs(float(b["pos"]) - float(a["pos"]))
                if gap > 500.0:
                    break
                if gap < 60.0:
                    continue
                lo, hi = max(a["lo"], b["lo"]), min(a["hi"], b["hi"])
                if hi - lo < 300.0:
                    continue
                dirv = a.get("dir") or (1.0, 0.0)
                nrm = a.get("nrm") or (0.0, 1.0)
                pos = (float(a["pos"]) + float(b["pos"])) / 2.0
                p1 = [pos * nrm[0] + lo * dirv[0], pos * nrm[1] + lo * dirv[1]]
                p2 = [pos * nrm[0] + hi * dirv[0], pos * nrm[1] + hi * dirv[1]]
                out.append({"id": "new", "kind": action.get("hypothesis") or "window",
                            "axis": [p1, p2],
                            "bbox": EVID.bbox_of({"axis": [p1, p2]}),
                            "thickness": round(gap, 1), "layer": layer,
                            "handles": [h for h in (a.get("handle"), b.get("handle")) if h],
                            "geometry_state": "derived",
                            "status": "candidate"})
    return out


def _op_regenerate_wall(local, action, state=None):
    from . import operators as OPS
    pairs = OPS.run(["parallel_pair"], {"entities": []}, local["layers"],
                    None, None, prims=local["prims"]) or []
    return [_wall_record(dict(p, thickness=p.get("gap")), "w") for p in pairs]


def _op_retry_wall_pairing(local, action, state=None):
    """重试配对：先按常规平行线对，再放宽到"共线段"（端点接近即视为同一道墙的两面）。

    与旧实现的区别：厚度簇证据弱不再直接丢弃，只要成对且沿向重叠达标就出候选，
    由求解器按独立证据决定要不要。
    """
    from . import operators as OPS
    pairs = OPS.run(["parallel_pair"], {"entities": []}, local["layers"],
                    None, None, prims=local["prims"]) or []
    out = [_wall_record(dict(p, thickness=p.get("gap")), "w") for p in pairs]
    seen = {(tuple(w["start"]), tuple(w["end"])) for w in out}
    lines = [l for l in local["prims"]["lines"] if l.get("pos") is not None]
    for ang in sorted({round(float(l.get("angle") or 0.0), 1) for l in lines}):
        group = [l for l in lines if abs(round(float(l.get("angle") or 0.0), 1) - ang) <= 1.5]
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                gap = abs(float(b["pos"]) - float(a["pos"]))
                if gap < 40.0 or gap > 900.0:
                    continue
                ov = min(float(a["hi"]), float(b["hi"])) - max(float(a["lo"]), float(b["lo"]))
                if ov < 200.0:
                    continue
                rec = _wall_record({"dir": a.get("dir"), "nrm": a.get("nrm"),
                                    "pos": (float(a["pos"]) + float(b["pos"])) / 2.0,
                                    "lo": max(float(a["lo"]), float(b["lo"])),
                                    "hi": min(float(a["hi"]), float(b["hi"])),
                                    "thickness": round(gap, 1),
                                    "handles": [h for h in (a.get("handle"),
                                                            b.get("handle")) if h]}, "w")
                key = (tuple(rec["start"]), tuple(rec["end"]))
                if key in seen:
                    continue
                seen.add(key)
                out.append(rec)
    return out


def _op_single_line_wall(local, action, state=None):
    """单线墙：只有一面线的墙体（装修图常见），厚度未知但仍可进墙网试算。"""
    lines = [l for l in local["prims"]["lines"] if l.get("pos") is not None]
    lens = sorted(float(l.get("length") or 0.0) for l in lines)
    median = lens[len(lens) // 2] if lens else 0.0
    out = []
    for l in lines:
        if float(l.get("length") or 0.0) < max(600.0, median * 0.5):
            continue
        rec = _wall_record({"dir": l.get("dir"), "nrm": l.get("nrm"),
                            "pos": l.get("pos"), "lo": l.get("lo"), "hi": l.get("hi"),
                            "thickness": None,
                            "handles": [h for h in (l.get("handle"),) if h]}, "w")
        rec["thickness_source"] = "single_line"
        out.append(rec)
    return out


def _op_extract_contour(local, action, state=None):
    from . import operators as OPS
    got = OPS.run(["closed_contour", "hatch_region"], {"entities": []},
                  local["layers"], None, None, prims=local["prims"]) or []
    return [{"id": "new", "bbox": r.get("bbox"), "outline": r.get("outline"),
             "area": r.get("area"), "evidence": r.get("kind"),
             "handles": [h for h in (r.get("handles") or ()) if h],
             "geometry_state": "derived", "status": "candidate"} for r in got]


def _op_rebuild_contour(local, action, state=None):
    from . import repair as REP
    out = []
    for h in local["prims"]["hatches"]:
        res = REP.rebuild_contour(hatch=h)
        if not res.get("ok"):
            continue
        for rec in res["inferred"]:
            rec = dict(rec)
            rec["handles"] = [x for x in (h.get("handle"),) if x]
            out.append(rec)
    return out


def _op_extract_arc(local, action, state=None):
    from . import operators as OPS
    arcs = OPS.run(["arc_feature"], {"entities": []}, local["layers"], None, None,
                   prims=local["prims"]) or []
    return [{"id": "new", "kind": "door", "center": a.get("center"),
             "handles": [h for h in (a.get("handles") or ()) if h],
             "geometry_state": "derived", "status": "candidate",
             "bbox": EVID.bbox_of({"center": a.get("center")})} for a in arcs[:60]]


def _op_treat_as_opening(local, action, state=None):
    """把墙里的"断口"解释成洞口：同向线段的沿向空档 -> 洞口候选。"""
    lines = [l for l in local["prims"]["lines"] if l.get("pos") is not None]
    groups: dict[str, list] = {}
    for l in lines:
        groups.setdefault(str(l.get("layer")), []).append(l)
    out = []
    for layer, members in groups.items():
        members.sort(key=lambda l: (round(float(l.get("angle") or 0.0), 1),
                                    float(l.get("lo") or 0.0)))
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if abs(float(a.get("pos") or 0) - float(b.get("pos") or 0)) > 400.0:
                    continue
                gap = float(b["lo"]) - float(a["hi"])
                if gap < 300.0 or gap > 3000.0:
                    continue
                dirv = a.get("dir") or (1.0, 0.0)
                nrm = a.get("nrm") or (0.0, 1.0)
                pos = (float(a["pos"]) + float(b["pos"])) / 2.0
                p1 = [pos * nrm[0] + float(a["hi"]) * dirv[0],
                      pos * nrm[1] + float(a["hi"]) * dirv[1]]
                p2 = [pos * nrm[0] + float(b["lo"]) * dirv[0],
                      pos * nrm[1] + float(b["lo"]) * dirv[1]]
                out.append({"id": "new", "kind": action.get("hypothesis") or "door",
                            "axis": [p1, p2], "layer": layer,
                            "bbox": EVID.bbox_of({"axis": [p1, p2]}),
                            "width": round(gap, 1), "geometry_state": "derived",
                            "handles": [h for h in (a.get("handle"), b.get("handle")) if h],
                            "status": "candidate"})
    return out


_GENERATORS = {
    "group_line_bundle": ("openings", _op_regenerate_opening),
    "regenerate_opening": ("openings", _op_regenerate_opening),
    "pair_parallel": ("walls", _op_regenerate_wall),
    "regenerate_wall": ("walls", _op_regenerate_wall),
    "merge_collinear": ("walls", _op_regenerate_wall),
    "retry_wall_pairing": ("walls", _op_retry_wall_pairing),
    "single_line_wall": ("walls", _op_single_line_wall),
    "extract_contour": ("columns", _op_extract_contour),
    "regenerate_column": ("columns", _op_extract_contour),
    "rebuild_contour": ("columns", _op_rebuild_contour),
    "extract_arc": ("openings", _op_extract_arc),
    "treat_as_opening": ("openings", _op_treat_as_opening),
}

#: 只调整状态、不产新候选的动作（在候选状态上执行）
_STATE_OPS = ("rebuild_wall_graph", "rebuild_space_faces", "reconcile_openings",
              "associate_room_labels", "resolve_conflicts", "merge_duplicate",
              "keep_thicker", "validate_local_result")

#: 只记录决策、不改几何的动作
_DECISION_OPS = ("reject_candidate", "treat_as_fill", "ignore_layer")


def _with_pos(w):
    """补算墙模型需要的 pos/lo/hi（没有 dir/nrm 的墙保持"按 orient 归角"的老路径）。"""
    if not isinstance(w, dict):
        return w
    s, e = w.get("start"), w.get("end")
    if not (isinstance(s, (list, tuple)) and isinstance(e, (list, tuple))):
        return w
    dx, dy = float(e[0]) - float(s[0]), float(e[1]) - float(s[1])
    ln = math.hypot(dx, dy)
    if ln <= 1e-9:
        return w
    horiz = abs(dx) >= abs(dy)
    out = dict(w)
    out.setdefault("orient", "H" if horiz else "V")
    if out.get("pos") is None:
        out["pos"] = round(((float(s[1]) + float(e[1])) / 2.0) if horiz
                           else ((float(s[0]) + float(e[0])) / 2.0), 2)
    if out.get("center") is None:
        out["center"] = out["pos"]
    if out.get("lo") is None or out.get("hi") is None:
        out["lo"] = round(min(float(s[0]), float(e[0])) if horiz
                          else min(float(s[1]), float(e[1])), 2)
        out["hi"] = round(max(float(s[0]), float(e[0])) if horiz
                          else max(float(s[1]), float(e[1])), 2)
    if out.get("dir") and out.get("nrm") and out.get("angle") is None:
        d = out["dir"]
        out["angle"] = round(math.degrees(math.atan2(float(d[1]), float(d[0]))) % 180.0, 3)
    out.setdefault("thickness", None)
    out.setdefault("length", round(ln, 1))
    return out


# ------------------------------------------------------------------ 推断几何
def execute_inferred_edges(edges, *, target_ids=(), thickness=None) -> dict:
    """推断边 -> 推断墙候选（geometry_state=inferred，来源写进 srcs）。"""
    patch = _empty_patch()
    for i, e in enumerate(edges or (), 1):
        start, end = e.get("start"), e.get("end")
        if not start or not end:
            continue
        handles = [str(t).split(":", 1)[-1] for t in (target_ids or ())]
        mid = [(float(start[0]) + float(end[0])) / 2.0,
               (float(start[1]) + float(end[1])) / 2.0]
        dx, dy = float(end[0]) - float(start[0]), float(end[1]) - float(start[1])
        ln = math.hypot(dx, dy) or 1.0
        dirv = (dx / ln, dy / ln)
        patch["walls"].append({
            "id": f"wall:inferred{i}", "start": list(start), "end": list(end),
            "length": e.get("length"), "thickness": thickness,
            "dir": list(dirv), "nrm": [-dirv[1], dirv[0]],
            "angle": round(math.degrees(math.atan2(dirv[1], dirv[0])) % 180.0, 3),
            "orient": "H" if abs(dirv[0]) >= abs(dirv[1]) else "V",
            "center": round(mid[1] if abs(dirv[0]) >= abs(dirv[1]) else mid[0], 2),
            "pos": round(mid[1] if abs(dirv[0]) >= abs(dirv[1]) else mid[0], 2),
            "bbox": EVID.bbox_of({"start": start, "end": end}),
            "geometry_state": "inferred", "confidence": e.get("confidence"),
            "supports": list(e.get("supports") or ()),
            "srcs": [{"handle": h, "role": "inferred_edge"} for h in handles if h],
        })
        patch["added"].append(f"wall:inferred{i}")
    patch["inferred_count"] = len(patch["walls"])
    patch["inferred"] = [dict(e) for e in (edges or ())]
    if not patch["walls"]:
        patch["reason"] = "NO_INFERRED_EDGE"
    return patch


# ------------------------------------------------------------------ 语义修复
def execute_layer_role(roles_by_layer: dict, *, snapshot, texts=(), walls_seg=(),
                       columns=()) -> dict:
    """图层角色覆盖 -> 该图层的候选重算（门/窗/柱/墙四类都支持）。"""
    from . import elements as R
    from . import walls as WALLS
    patch = _empty_patch()
    patch["layer_roles"] = {str(k): list(v) for k, v in (roles_by_layer or {}).items()}
    if not roles_by_layer:
        patch["reason"] = "NO_ROLES"
        return patch
    win_layers, door_layers, col_layers, wall_layers = [], [], [], []
    for layer, roles in roles_by_layer.items():
        if "window" in roles:
            win_layers.append(layer)
        if "door" in roles:
            door_layers.append(layer)
        if "column" in roles:
            col_layers.append(layer)
        if "wall" in roles:
            wall_layers.append(layer)
    for w in R.recognize_windows(snapshot, texts, layers=win_layers,
                                 walls=walls_seg) or ():
        patch["openings"].append(dict(w, kind="window",
                                      handles=[h for h in [w.get("handle")] if h],
                                      geometry_state="derived"))
    for d in R.recognize_doors(snapshot, texts, walls=walls_seg,
                               layers=door_layers) or ():
        patch["openings"].append(dict(d, kind="door",
                                      handles=[h for h in [d.get("handle")] if h],
                                      geometry_state="derived"))
    for c in R.recognize_columns(snapshot, layers=col_layers) or ():
        patch["columns"].append(dict(c, handles=[h for h in [c.get("handle")] if h],
                                     geometry_state="derived"))
    if wall_layers:
        got, _unpaired, _diag = WALLS.extract_walls_ext(snapshot, layers=wall_layers)
        for i, w in enumerate(got or (), 1):
            rec = dict(w, id=f"wall:layer{i}", geometry_state="derived",
                       status="candidate")
            rec.setdefault("handles", [x.get("handle") for x in (w.get("srcs") or ())
                                       if x.get("handle")])
            patch["walls"].append(rec)
    patch["added"] = ([f"{o['kind']}:{o.get('handle')}" for o in patch["openings"]]
                      + [str(c.get("handle")) for c in patch["columns"]]
                      + [w["id"] for w in patch["walls"]])
    patch["stats"] = {"openings": len(patch["openings"]),
                      "columns": len(patch["columns"]), "walls": len(patch["walls"])}
    return patch


# ------------------------------------------------------------------ 状态类算子
def _op_rebuild_space_faces(state, region, target_ids, action=None):
    from . import spaces as SPACES
    patch = _empty_patch()
    walls = [w for w in (state.get("walls") or ()) if w.get("start") and w.get("end")]
    if not walls:
        patch["reason"] = "NO_WALLS"
        return patch
    extra = state.get("inferred_edges") or []
    try:
        # 重算面域时**仍然要把已观测洞口当隔断补上**，否则修复分支算出来的房间
        # 会比种子阶段少（种子补了门洞、这里没补，两边口径不一致）。
        built = SPACES.build(walls, columns=state.get("columns") or (),
                             partitions=state.get("partitions") or (),
                             thick=state.get("thick"), extra_edges=extra,
                             barriers=state.get("doors") or (),
                             scale=(state.get("scale") or None))
    except Exception:
        patch["reason"] = "SPACE_BUILD_FAILED"
        return patch
    patch["faces"] = built.get("faces") or []
    patch["stats"] = {"faces": len(patch["faces"]),
                      "barriers_used": (built.get("stats") or {}).get("barriers_used") or 0,
                      "inferred_edges_used":
                          (built.get("stats") or {}).get("inferred_edges_used") or 0}
    return patch


def _op_rebuild_wall_graph(state, region, target_ids, action=None):
    from . import wallmodel as WM
    patch = _empty_patch()
    walls = [_with_pos(w) for w in (state.get("walls") or ())
             if w.get("start") and w.get("end")]
    walls = [w for w in walls if w.get("pos") is not None]
    if not walls:
        patch["reason"] = "NO_WALLS"
        return patch
    model = WM.build(walls, state.get("thick"))
    patch["wall_model"] = {"spans": len(model.get("spans") or ()),
                           "gaps": len(model.get("gaps") or ())}
    patch["stats"] = {"spans": len(model.get("spans") or ()),
                      "gaps": len(model.get("gaps") or ())}
    return patch


def _op_reconcile_openings(state, region, target_ids, action=None):
    """洞口归属校正：贴到最近墙上，冲突记未决（不静默删除）。"""
    patch = _empty_patch()
    walls = [w for w in (state.get("walls") or ()) if w.get("start") and w.get("end")]
    openings = list(state.get("doors") or ()) + list(state.get("windows") or ())
    host_ok, orphan, conflicts = 0, 0, 0
    seen: list = []
    for op in openings:
        seg = op.get("axis") or op.get("opening")
        hid = op.get("handle") or op.get("id")
        if not seg:
            continue
        mid = [(float(seg[0][0]) + float(seg[1][0])) / 2.0,
               (float(seg[0][1]) + float(seg[1][1])) / 2.0]
        best, host = None, None
        for w in walls:
            d = _point_seg_dist(mid, w["start"], w["end"])
            half = float(w.get("thickness") or 200.0) / 2.0
            if best is None or d < best:
                best, host = d, w
        if host is not None and best is not None and best <= float(host.get("thickness") or 200.0) / 2.0 + 300.0:
            host_ok += 1
            patch["updated"].append({"id": hid, "wall_id": host.get("id"),
                                     "host_ok": True})
        else:
            orphan += 1
            patch["updated"].append({"id": hid, "wall_id": None, "host_ok": False})
            patch["unresolved"].append({"reason": "OPENING_WITHOUT_WALL", "id": hid})
        clash = next((s for s in seen
                      if s.get("kind") != op.get("kind") and _same_interval(s, op)), None)
        if clash:
            conflicts += 1
            patch["unresolved"].append({"reason": "MUTEX",
                                        "ids": [hid, clash.get("handle") or clash.get("id")]})
        else:
            seen.append(op)
    patch["stats"] = {"host_ok": host_ok, "orphan": orphan, "conflicts": conflicts,
                      "openings": len(openings)}
    return patch


def _op_associate_room_labels(state, region, target_ids, action=None):
    from . import spaces as SPACES
    patch = _empty_patch()
    labels = list((action or {}).get("labels") or state.get("labels") or ())
    faces = [f for f in (state.get("faces") or ()) if f.get("polygon")]
    bound = 0
    out = []
    for lab in labels:
        x, y = lab.get("x"), lab.get("y")
        lid = lab.get("id") or lab.get("handle")
        if x is None or y is None or not faces:
            out.append({"id": lid, "status": "no_point", "face_id": None})
            patch["labels"].append(out[-1])
            continue
        try:
            hit = SPACES.locate(faces, float(x), float(y))
        except Exception:
            hit = None
        if hit:
            bound += 1
            patch["labels"].append({"id": lid, "status": "bound",
                                    "face_id": hit.get("id")})
        else:
            patch["labels"].append({"id": lid, "status": "leak", "face_id": None})
    patch["stats"] = {"bound": bound, "labels": len(labels), "faces": len(faces)}
    return patch


def _op_merge_duplicate(state, region, target_ids, action=None, prefer="handles"):
    patch = _empty_patch()
    dropped = 0
    for bucket in ("walls", "columns"):
        recs = list(state.get(bucket) or ())
        keep: list = []
        for rec in recs:
            dup = next((k for k in keep if _duplicate(k, rec)), None)
            if dup is None:
                keep.append(rec)
                continue
            winner, loser = _pick_winner(dup, rec, prefer)
            keep[keep.index(dup)] = winner
            patch["drop_ids"].append(str(loser.get("id") or loser.get("handle")))
            dropped += 1
        patch.setdefault("stats", {})[bucket + "_duplicates"] = dropped
    patch["stats"]["dropped"] = dropped
    return patch


def _op_keep_thicker(state, region, target_ids, action=None):
    return _op_merge_duplicate(state, region, target_ids, prefer="thickness")


def _op_validate_local_result(state, region, target_ids, action=None):
    patch = _empty_patch()
    orphans = 0
    for bucket in ("walls", "columns", "doors", "windows"):
        for rec in state.get(bucket) or ():
            if not EVID.handles_of(rec):
                orphans += 1
                patch["unresolved"].append({"reason": "NO_PROVENANCE",
                                            "id": rec.get("id") or rec.get("handle")})
    patch["stats"] = {"no_provenance": orphans}
    return patch


def _op_decision_only(name, state, target_ids, action):
    patch = _empty_patch()
    patch["ignored"] = [str(t) for t in (target_ids or ())]
    patch["stats"] = {"decision": name, "targets": len(patch["ignored"])}
    return patch


_STATE_HANDLERS = {
    "rebuild_space_faces": _op_rebuild_space_faces,
    "rebuild_wall_graph": _op_rebuild_wall_graph,
    "reconcile_openings": _op_reconcile_openings,
    "resolve_conflicts": _op_reconcile_openings,
    "associate_room_labels": _op_associate_room_labels,
    "merge_duplicate": _op_merge_duplicate,
    "keep_thicker": _op_keep_thicker,
    "validate_local_result": _op_validate_local_result,
}


# ------------------------------------------------------------------ 入口
def execute_operator(name: str, *, target_ids=(), region=None, evidence_index=None,
                     candidate_state=None, action=None) -> dict:
    """执行一个已校验的算子，返回补丁（不含坐标以外任何 LLM 输入）。"""
    action = dict(action or {})
    state = dict(candidate_state or {})
    if name == "try_virtual_boundary":
        return execute_inferred_edges(action.get("inferred_edges"),
                                      target_ids=target_ids)
    spec = REGISTRY.get(name)
    if spec is None:
        return _empty_patch(f"UNKNOWN_OPERATOR:{name}")
    if name in _DECISION_OPS or (spec.decision_only and name not in _STATE_HANDLERS):
        return _op_decision_only(name, state, target_ids, action)
    if name in _STATE_HANDLERS:
        return _STATE_HANDLERS[name](state, region, target_ids, action)

    bucket_fn = _GENERATORS.get(name)
    if bucket_fn is None:
        return _empty_patch(f"NO_EXECUTOR:{name}")
    bucket, fn = bucket_fn

    local = local_evidence(evidence_index or {}, target_ids=target_ids, region=region)
    if not local["prims"]["lines"] and not local["prims"]["closed"] \
            and not local["prims"]["hatches"]:
        return _empty_patch("NO_LOCAL_EVIDENCE")

    generated = fn(local, {"hypothesis": action.get("hypothesis")}, state) or []
    if not _provenance_ok(generated, local):
        return _empty_patch("PROVENANCE_LOST")

    patch = _empty_patch()
    for i, rec in enumerate(generated, 1):
        rec = dict(rec)
        rec["id"] = f"{bucket[:-1]}:new{i}"
        rec["action"] = name
        patch[bucket].append(rec)
        patch["added"].append(rec["id"])
    patch["stats"] = {"generated": len(generated)}
    return patch


def apply_patch(state: dict, patch: dict) -> dict:
    """把补丁合进状态副本（不修改入参）。"""
    out = {k: list(v) if isinstance(v, list) else v for k, v in (state or {}).items()}
    drops = {str(d) for d in (patch.get("drop_ids") or ()) if d}
    if drops:
        for bucket in ("walls", "columns", "doors", "windows", "openings", "faces"):
            if not out.get(bucket):
                continue
            out[bucket] = [r for r in out[bucket]
                           if str(r.get("id") or r.get("handle")) not in drops]
    for bucket in ("walls", "columns", "openings", "faces"):
        if patch.get(bucket):
            out.setdefault(bucket, [])
            out[bucket] = list(out[bucket]) + list(patch[bucket])
    if patch.get("openings"):
        for op in patch["openings"]:
            key = "doors" if (op.get("kind") or op.get("category")) == "door" else "windows"
            out.setdefault(key, [])
            out[key] = list(out[key]) + [op]
    if patch.get("labels"):
        known = {str(r.get("id")): r for r in (out.get("labels") or ())}
        for rec in patch["labels"]:
            known[str(rec.get("id"))] = rec
        out["labels"] = list(known.values())
    if patch.get("updated"):
        upd = {str(u.get("id")): u for u in patch["updated"]}
        for bucket in ("doors", "windows", "walls", "columns"):
            if not out.get(bucket):
                continue
            merged = []
            for rec in out[bucket]:
                key = str(rec.get("handle") or rec.get("id"))
                u = upd.get(key)
                merged.append(dict(rec, **{k: v for k, v in u.items() if k != "id"})
                              if u else rec)
            out[bucket] = merged
    if patch.get("unresolved"):
        out["unresolved"] = list(out.get("unresolved") or ()) + list(patch["unresolved"])
    if patch.get("inferred"):
        out["inferred_edges"] = list(out.get("inferred_edges") or ()) \
            + list(patch["inferred"])
    if patch.get("wall_model"):
        out["wall_model"] = dict(patch["wall_model"])
    if patch.get("stats"):
        out["stats"] = dict(out.get("stats") or {}, **patch["stats"])
    return out


# ------------------------------------------------------------------ 工具
def _point_seg_dist(p, a, b) -> float:
    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    dx, dy = bx - ax, by - ay
    L = math.hypot(dx, dy) or 1.0
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / (L * L)))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def _same_interval(a, b) -> bool:
    sa = a.get("opening") or a.get("axis")
    sb = b.get("opening") or b.get("axis")
    if not sa or not sb:
        return False
    return all(abs(float(sa[i][j]) - float(sb[i][j])) <= 1e-6
               for i in (0, 1) for j in (0, 1))


def _iou(a, b) -> float:
    ba, bb = a.get("bbox"), b.get("bbox")
    if not ba or not bb:
        return 0.0
    lo_x, hi_x = max(ba[0], bb[0]), min(ba[2], bb[2])
    lo_y, hi_y = max(ba[1], bb[1]), min(ba[3], bb[3])
    if hi_x <= lo_x or hi_y <= lo_y:
        return 0.0
    inter = (hi_x - lo_x) * (hi_y - lo_y)
    area_a = max(1e-9, (ba[2] - ba[0]) * (ba[3] - ba[1]))
    area_b = max(1e-9, (bb[2] - bb[0]) * (bb[3] - bb[1]))
    return inter / (area_a + area_b - inter)


def _duplicate(a, b) -> bool:
    return _iou(a, b) >= 0.8 and (a.get("orient") == b.get("orient")
                                  or a.get("kind") == b.get("kind"))


def _pick_winner(a, b, prefer="handles"):
    if prefer == "thickness":
        ka = float(a.get("thickness") or 0.0)
        kb = float(b.get("thickness") or 0.0)
        return (a, b) if ka >= kb else (b, a)
    ka = len(EVID.handles_of(a) or (a.get("handles") or ()))
    kb = len(EVID.handles_of(b) or (b.get("handles") or ()))
    return (a, b) if ka >= kb else (b, a)


def _in_region(b, region, pad=300.0) -> bool:
    return not (b[2] < region[0] - pad or b[0] > region[2] + pad
                or b[3] < region[1] - pad or b[1] > region[3] + pad)


#: 兼容旧调用点
EXECUTORS = {name: (lambda state, action, index, _n=name:
                    apply_patch(state, execute_operator(_n, target_ids=action.get("target_ids"),
                                                        evidence_index=index)))
             for name in list(_GENERATORS) + list(_STATE_HANDLERS)
             + ["try_virtual_boundary"] + list(_DECISION_OPS)}
