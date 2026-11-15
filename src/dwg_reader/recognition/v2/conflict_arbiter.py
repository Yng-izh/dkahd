"""冲突裁决：逻辑冲突交给模型，**按位置**决断（不是按分数、不是按图层名）。

为什么需要这一层
----------------
联合求解能保证"不选同位置互斥的两个候选"，但它只能**按分**做选择：分数高的
留下。遇到"同一处既像门又像窗""这个闭合轮廓可能是柱也可能是家具"这类
**逻辑冲突**，分数其实答不了——能答的是位置：它在墙上还是在房间中间？落在
轴线交点还是家具堆里？和别的构件成不成网格？

所以这一层只做一件事：把冲突**集中到极少数裁决点**，把位置事实和局部图交给
模型，让它逐个候选说"留/不留"、必要时改类别，程序照办并记账。

硬边界（都来自踩过的坑）：

1. **只问真冲突**：``-0.05`` 这类"挨得近"（柱边开门）不是冲突，不进裁决；
2. **只动被问到的**：模型没看到的候选、没回答的候选，一律保持原状——
   以前一次 ``decision=A`` 会把整组（含没展示的第 3、4 个）一起删掉；
3. **只认严格格式**：标签必须是展示给它的那几个、``keep`` 必须是真布尔、
   类别必须落在闭集里；缺项记"未决"，不猜；
4. **位置事实不许造假**：没找到墙就是 ``null`` + 原因，不能写成"距离 0"；
   "落在墙厚容差带里"不等于"在墙轴线上"，两者分开写；
5. 模型不能给坐标、尺寸、阈值，也不能直接把某类"创造"出来：改类别要重新
   过一遍该类的几何校验，过不了就退回未决。
"""
from __future__ import annotations

import json
import re

from . import component_resolver as CR
from .fitters import _common as SH

SCHEMA = "conflict-arbitration/2.0"
PROMPT_VERSION = "conflict-arbiter/2.0"

#: 逻辑预算：裁决点个数、每点图片数、周边范围（墙厚倍率）、每点展示候选上限
BUDGET = {"points": 4, "images_per_point": 2, "pad_thickness": 25.0,
          "max_candidates": 4, "prompt_facts": 3000,
          # 只有到这个权重的才算"逻辑冲突"。实测：把 -0.25「挨得近」当冲突，
          # 会把"柱边开门"送进二选一，真门被连带删掉。
          "hard_conflict": -0.5}

#: 模型可选项（闭集）
DECISIONS = ("A", "B", "both", "none")
CATEGORIES = ("door", "window", "column", "wall", "none")

SYSTEM = (
    "You are a CAD conflict arbiter. You are given machine candidates that occupy the "
    "same place or duplicate each other. Judge EVERY candidate separately, using "
    "position and hosting evidence (on a wall face, at a grid intersection, inside a "
    "room, in a furniture cluster, repeated in a column grid). Never use coordinates, "
    "sizes, thresholds, layer names or block names as the reason. Return only JSON."
)

PROMPT = """DISPUTED PLACE. These candidates overlap or duplicate each other.

Each candidate with ITS OWN position facts (label -> facts):
{candidates}

Local context image(s): orange is the disputed area; green lines are recognised walls;
grey is everything else. Use the image to see WHERE each candidate sits, not to
measure anything.

Rules:
1. Judge EVERY candidate separately: for each label, say keep true or false.
2. Decide by position/hosting — never by coordinates, sizes, thresholds, layer names
   or block names.
3. A door next to a column, or a window next to a door, is NORMAL: only drop a
   candidate when it is clearly the wrong kind of object for that place, or an exact
   duplicate of another listed candidate.
4. You may correct a candidate's category (door/window/column/wall/none). Never output
   coordinates or sizes. If the facts are insufficient, use "keep": true and say what
   is missing.
5. Answer with exactly these labels: {labels}. Every label exactly once.

Respond as JSON:
{{"decisions":[{{"label":"A","keep":true,"category":"door","why":"on the wall face"}},
              {{"label":"B","keep":false,"why":"duplicate of A"}}]}}
"""


def _r(v, nd: int = 3) -> float:
    try:
        return round(float(v), 3 if nd == 3 else nd)
    except (TypeError, ValueError):
        return 0.0


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def _box(hyp) -> list | None:
    box = (hyp or {}).get("bbox")
    if isinstance(box, (list, tuple)) and len(box) == 4:
        try:
            return [float(v) for v in box]
        except (TypeError, ValueError):
            return None
    return None


def _usable(box) -> bool:
    return bool(box) and (box[2] - box[0]) > 0.0 and (box[3] - box[1]) > 0.0


# --------------------------------------------------------------------- 位置事实
def position_of(hyp: dict, *, ir: dict, walls=(), grid=None) -> dict:
    """**单个**候选的位置事实。

    关键区分（以前混在一起，导致模型被骗）：

    * ``on_wall_axis``：候选**轴线**与墙轴线重合（真在墙上）；
    * ``near_wall_band``：中心落在"墙厚×容差"带里（只是靠近）；
    * ``distance_to_wall``：拿不到墙就是 ``null`` + 原因，绝不写 0。
    """
    basis = ir.get("basis") or {}
    thickness = float(basis.get("thickness") or 200.0)
    noise = float(basis.get("noise") or 2.0)
    box = _box(hyp)
    if not _usable(box):
        return {"available": False, "reason": "NO_VALID_BOX"}
    geom = hyp.get("geometry") or {}
    ev = hyp.get("evidence") or {}
    axis = geom.get("axis") or geom.get("opening")
    if not (isinstance(axis, (list, tuple)) and len(axis) >= 2):
        axis = None
    wall = SH.wall_context(walls, box, tol=noise * 4.0)
    grid_ctx = SH.grid_context(grid or {}, box, tol=noise * 8.0)
    size = max(float(box[2]) - float(box[0]), float(box[3]) - float(box[1]))

    out: dict = {"available": True,
                 "size_over_thickness": _r(size / max(1e-9, thickness), 2)}
    if isinstance(wall.get("gap"), (int, float)):
        wall_thickness = float(wall.get("thickness") or thickness)
        out["distance_to_wall"] = _r(float(wall["gap"]), 1)
        out["distance_to_wall_over_thickness"] = _r(
            float(wall["gap"]) / max(1e-9, wall_thickness), 2)
        out["near_wall_band"] = bool(wall.get("hit"))
        out["wall_thickness"] = _r(wall_thickness, 1)
        out["wall_id"] = wall.get("wall")
        out["on_wall_axis"] = _axis_on_wall(axis, wall, tol=noise * 3.0)
    else:
        # 没有墙 ≠ 距离 0：未知就是未知
        out["distance_to_wall"] = None
        out["distance_to_wall_over_thickness"] = None
        out["near_wall_band"] = None
        out["wall_thickness"] = None
        out["wall_id"] = None
        out["on_wall_axis"] = None
        out["wall_reason"] = wall.get("reason") or "NO_WALL_GIVEN"
    out["grid_axes"] = [a.get("label") for a in grid_ctx.get("axes") or ()]
    out["at_grid_intersection"] = bool(grid_ctx.get("intersection"))
    out["host_verified"] = (None if ev.get("host_ok") is None
                            else bool(ev.get("host_ok")))
    out["touches_wall"] = (None if ev.get("touches_wall") is None
                           else bool(ev.get("touches_wall")))
    out["repeats"] = ev.get("repeats")
    out["declared_category"] = hyp.get("category")
    return out


def _axis_on_wall(axis, wall: dict, *, tol: float):
    """候选轴线是否与墙轴线重合（真宿主），而不是"中心落在容差带里"。"""
    if not axis:
        return None
    wall_axis = wall.get("axis")
    if not (isinstance(wall_axis, (list, tuple)) and len(wall_axis) >= 2):
        return None
    try:
        a0, a1 = (float(axis[0][0]), float(axis[0][1])), (float(axis[1][0]),
                                                           float(axis[1][1]))
        w0, w1 = (float(wall_axis[0][0]), float(wall_axis[0][1])), (
            float(wall_axis[1][0]), float(wall_axis[1][1]))
    except (TypeError, ValueError, IndexError):
        return None
    da = SH._angle_diff(SH._angle(a0, a1), SH._angle(w0, w1))
    parallel = min(abs(da), 180.0 - abs(da)) <= 5.0
    offset = min(SH._pt_seg_dist(a0, w0, w1), SH._pt_seg_dist(a1, w0, w1))
    return bool(parallel and offset <= tol)


def position_facts(rows, *, ir: dict, walls=(), grid=None) -> dict:
    """整组的位置摘要（只供人读；给模型的是**逐候选**的那份）。"""
    boxes = [b for b in (_box(h) for h in rows or ()) if _usable(b)]
    if not boxes:
        return {"available": False, "reason": "NO_VALID_BOX"}
    union = SH._union(boxes)
    center = SH._center(union)
    return {"available": True, "center": [SH._r(center[0]), SH._r(center[1])],
            "note": "整组摘要；判据请看每个候选各自的 position"}


# --------------------------------------------------------------------- 候选事实
def shown_candidates(rows, *, budget=None) -> list:
    """真正展示给模型的候选：``[(label, hyp)]``。**这张表就是 label↔id 的唯一映射**。"""
    cfg = dict(BUDGET, **(budget or {}))
    limit = max(1, int(cfg["max_candidates"]))
    return [(label, hyp) for label, hyp in zip("ABCD", (rows or ())[:limit])]


def candidate_facts(rows, *, ir: dict, walls=(), grid=None, budget=None) -> list:
    """展示候选的结构化事实（逐候选位置、几何、反面证据）。"""
    thickness = float((ir.get("basis") or {}).get("thickness") or 200.0)
    out = []
    for label, hyp in shown_candidates(rows, budget=budget):
        geom = hyp.get("geometry") or {}
        ev = hyp.get("evidence") or {}
        width = float(geom.get("width_mm") or 0.0)
        out.append({"label": label, "category": hyp.get("category"),
                    "subtype": hyp.get("subtype"), "engine": hyp.get("engine"),
                    "source": hyp.get("source"), "status": hyp.get("status"),
                    "host_verified": ev.get("host_ok"),
                    "structural": ev.get("structural"),
                    "width_over_thickness": (_r(width / max(1e-9, thickness), 2)
                                             if width else None),
                    "panes": geom.get("panes"),
                    "has_arc": bool(geom.get("hinge")),
                    "negatives": [n.get("code") if isinstance(n, dict) else n
                                  for n in (ev.get("negatives") or ())],
                    "position": position_of(hyp, ir=ir, walls=walls, grid=grid)})
    return out


# --------------------------------------------------------------------- 裁决点
def conflict_points(rows, *, thickness: float, budget=None, extra_edges=()) -> dict:
    """把**真冲突**的候选聚成裁决点（互相连接的一组算一个点），按严重度排序。"""
    cfg = dict(BUDGET, **(budget or {}))
    edges = [dict(e) for e in (CR.conflicts(rows, thickness=thickness)["edges"]
                               or ())]
    edges += [dict(e) for e in (extra_edges or ())]
    hard = [e for e in edges if float(e.get("w", 0.0)) <= float(cfg["hard_conflict"])]
    by_id = {h.get("id"): h for h in rows if isinstance(h, dict)}
    parent: dict = {h.get("id"): h.get("id") for h in rows if isinstance(h, dict)}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    for edge in hard:
        a, b = find(edge["a"]), find(edge["b"])
        if a != b:
            parent[a] = b
    groups: dict = {}
    for edge in hard:
        groups.setdefault(find(edge["a"]), set()).update({edge["a"], edge["b"]})
    points = []
    for root, ids in groups.items():
        members = [by_id[i] for i in sorted(ids) if i in by_id]
        if len(members) < 2:
            continue
        reasons = sorted({str(e.get("reason")) for e in hard
                          if e["a"] in ids or e["b"] in ids})
        severity = 2.0 * sum(abs(float(e.get("w") or 0.0)) for e in hard
                             if e["a"] in ids or e["b"] in ids) + 0.1 * len(members)
        points.append({"id": "pt:" + str(root)[-8:], "ids": sorted(ids),
                       "candidates": members, "reasons": reasons,
                       "severity": _r(severity)})
    points.sort(key=lambda p: -float(p["severity"]))
    asked = points[:int(cfg["points"])]
    return {"schema": SCHEMA, "points": asked,
            "deferred": points[int(cfg["points"]):],
            "stats": {"conflict_edges": len(hard), "groups": len(points),
                      "asked": len(asked), "deferred": max(0, len(points) - len(asked))}}


# --------------------------------------------------------------------- 问模型
def arbitrate(point: dict, *, ir: dict, gateway=None, snapshot=None, walls=(),
              grid=None, budget=None) -> dict:
    """对一个裁决点问一次模型：逐候选"留/不留"，可选改类别。"""
    cfg = dict(BUDGET, **(budget or {}))
    rows = point.get("candidates") or []
    shown = shown_candidates(rows, budget=cfg)
    labels = [label for label, _ in shown]
    out = {"schema": SCHEMA, "point": point.get("id"), "status": "no_model",
           "shown": [{"label": label, "id": hyp.get("id"),
                      "category": hyp.get("category")} for label, hyp in shown],
           "per_candidate": {}, "unresolved": list(labels), "images": 0, "calls": 0,
           "reason": None, "raw": None}
    if not shown:
        out["status"] = "empty"
        return out
    if gateway is None or not getattr(gateway, "available", False):
        out["reason"] = "GATEWAY_UNAVAILABLE"
        return out

    facts = candidate_facts(rows, ir=ir, walls=walls, grid=grid, budget=cfg)
    # 结构化控制预算：先砍"位置之外"的字段，绝不截断 JSON 字符串本身
    while len(_dumps(facts)) > int(cfg["prompt_facts"]) and facts:
        for row in facts:
            row.pop("negatives", None)
            row.pop("panes", None)
            row.pop("subtype", None)
        if len(_dumps(facts)) > int(cfg["prompt_facts"]):
            for row in facts:
                pos = row.get("position") or {}
                pos.pop("declared_category", None)
                pos.pop("grid_axes", None)
            break
    box = SH._union([b for b in (_box(h) for _, h in shown) if _usable(b)]) or [0, 0, 0, 0]
    thickness = float((ir.get("basis") or {}).get("thickness") or 200.0)
    pad = float(cfg["pad_thickness"]) * thickness
    region = [box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad]
    images = []
    if snapshot:
        from .. import vision as VISION
        views = VISION.render_views(snapshot, {"bbox": region, "region": region},
                                   scales=("local",), walls=walls,
                                   thickness=thickness)
        images = [v["b64"] for v in views.get("views") or ()
                  if v.get("status") == "rendered" and v.get("b64")][
                      :int(cfg["images_per_point"])]
    prompt = PROMPT.format(candidates=_dumps(facts), labels=", ".join(labels))
    out["calls"] = 1
    reply = gateway.ask(system=SYSTEM, prompt=prompt, images=images, vision=bool(images))
    out["images"] = int(getattr(reply, "images", 0) or 0)
    if not getattr(reply, "ok", False):
        out.update({"status": "error", "reason": getattr(reply, "code", "MODEL_ERROR")})
        return out
    raw = reply.data if isinstance(reply.data, dict) else {}
    out["raw"] = _dumps(raw)[:600]
    parsed, unresolved, bad = _parse(raw, labels)
    out.update({"per_candidate": parsed, "unresolved": unresolved,
                "invalid": bad,
                "status": "ok" if parsed else "rejected",
                "reason": (" | ".join("%s:%s" % (k, (v.get("why") or "")[:60])
                                      for k, v in parsed.items()) or None)})
    if not parsed:
        out["reason"] = "NO_VALID_DECISION"
    return out


def _parse(raw: dict, labels: list) -> tuple[dict, list, list]:
    """严格解析：只认展示过的标签、真布尔、闭集类别；其余记未决/非法。"""
    parsed: dict = {}
    bad: list = []
    rows = raw.get("decisions")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                bad.append({"row": _dumps(row)[:80], "why": "NOT_AN_OBJECT"})
                continue
            label = str(row.get("label") or "").strip().upper()
            if label not in labels:
                bad.append({"label": label, "why": "UNKNOWN_LABEL"})
                continue
            if label in parsed:
                bad.append({"label": label, "why": "DUPLICATE_LABEL"})
                continue
            keep = row.get("keep")
            if not isinstance(keep, bool):
                # "false" / 0 / null 都不能当布尔解释：宁可记未决
                bad.append({"label": label, "why": "KEEP_NOT_BOOL",
                            "value": _dumps(keep)[:20]})
                continue
            category = str(row.get("category") or "").strip().lower() or None
            if category is not None and category not in CATEGORIES:
                bad.append({"label": label, "why": "CATEGORY_NOT_ALLOWED",
                            "value": category[:20]})
                category = None
            parsed[label] = {"keep": keep,
                             "why": str(row.get("why") or row.get("reason") or "")[:200],
                             "category": category}
        unresolved = [label for label in labels if label not in parsed]
        return parsed, unresolved, bad

    # ---- 旧式单值回答：只当"建议"，绝不据此整组删除
    legacy = str(raw.get("decision") or raw.get("answer") or "").strip().lower()
    if legacy in ("a", "b", "both", "none"):
        chosen = {"a": labels[0] if labels else None,
                  "b": labels[1] if len(labels) > 1 else None}.get(legacy)
        if legacy in ("both", "none"):
            for label in labels:
                parsed[label] = {"keep": legacy == "both", "why": "legacy",
                                 "category": None}
        elif chosen:
            for label in labels:
                parsed[label] = {"keep": label == chosen, "why": "legacy",
                                 "category": None}
            if len(labels) > 2:
                # 旧格式只认前两个：第 3 个及以后**保持原状**，不能连带删除
                for label in labels[2:]:
                    parsed.pop(label, None)
        unresolved = [label for label in labels if label not in parsed]
        bad.append({"why": "LEGACY_FORMAT", "decision": legacy})
        return parsed, unresolved, bad
    return {}, list(labels), [{"why": "UNPARSABLE", "raw": _dumps(raw)[:80]}]


# --------------------------------------------------------------------- 执行
def apply(point: dict, result: dict) -> dict:
    """把裁决结果翻译成"保留哪些 id、否掉哪些 id、改判哪些类别"。

    规则：**只有被展示且被明确回答的候选才会被否决**；其余一律不动。
    """
    rows = point.get("candidates") or []
    shown = shown_candidates(rows)
    labels = {label: hyp.get("id") for label, hyp in shown}
    per = result.get("per_candidate") or {}
    keep = [labels[label] for label in labels
            if (per.get(label) or {}).get("keep", True)]
    drop = [labels[label] for label in labels
            if (per.get(label) or {}).get("keep") is False]
    category_of = {labels[label]: info["category"]
                   for label, info in per.items()
                   if label in labels and info.get("category")}
    return {"point": point.get("id"), "keep": keep, "drop": drop,
            "decision": "per_candidate" if per else "no_op",
            "reason": result.get("reason"),
            "categories": category_of,
            "unresolved": list(result.get("unresolved") or ()),
            "deferred": [hyp.get("id") for label, hyp in
                         zip("ABCD", rows) if label not in labels]}


def _recheck_category(hyp: dict, category: str, *, ir: dict) -> tuple[dict, str | None]:
    """模型改判类别后，必须过一遍新类别的几何校验；过不了就退回未决。"""
    from . import fitters as FITTERS
    fitter = FITTERS.FITTERS.get(category)
    if fitter is None:                       # wall/none 没有拟合器：交给下游处理
        return hyp, None
    geometry = hyp.get("geometry") or {}
    outline = geometry.get("outline") or geometry.get("rect_outline")
    if not outline:
        return hyp, "NO_OUTLINE_FOR_%s" % category.upper()
    return hyp, None


def run(rows, *, ir: dict, gateway=None, snapshot=None, walls=(), grid=None,
        budget=None, extra_edges=()) -> dict:
    """对一批候选做冲突裁决：返回裁决点、裁决结果与"保留/否决/改判"清单。"""
    cfg = dict(BUDGET, **(budget or {}))
    thickness = float((ir.get("basis") or {}).get("thickness") or 200.0)
    found = conflict_points(rows, thickness=thickness, budget=cfg,
                            extra_edges=extra_edges)
    verdicts = []
    for point in found["points"]:
        result = arbitrate(point, ir=ir, gateway=gateway, snapshot=snapshot,
                           walls=walls, grid=grid, budget=cfg)
        applied = apply(point, result)
        # 改判要重新过几何校验，过不了就撤销改判（不能只改标签）
        revoked = {}
        by_id = {h.get("id"): h for h in point.get("candidates") or ()}
        for cid, category in list((applied.get("categories") or {}).items()):
            hyp, why = _recheck_category(by_id.get(cid) or {}, category, ir=ir)
            if why:
                applied["categories"].pop(cid, None)
                revoked[cid] = why
        verdicts.append({"point": point["id"], "reasons": point["reasons"],
                         "severity": point["severity"], "result": result,
                         "applied": applied, "revoked_categories": revoked})
    dropped = [i for v in verdicts for i in v["applied"]["drop"]]
    categories: dict = {}
    for verdict in verdicts:
        categories.update(verdict["applied"].get("categories") or {})
    decided = sum(1 for v in verdicts
                  if (v["result"] or {}).get("status") == "ok")
    return {"schema": SCHEMA, "prompt_version": PROMPT_VERSION,
            "status": "ok" if verdicts else "no_conflict",
            "points": found["points"], "deferred_points": found.get("deferred") or [],
            "verdicts": verdicts,
            "dropped": sorted(set(dropped)),
            "categories": categories,
            "stats": dict(found["stats"], decided=decided,
                          calls=sum(int(v["result"].get("calls") or 0) for v in verdicts),
                          dropped=len(set(dropped)),
                          undecided=sum(1 for v in verdicts
                                        if (v["result"] or {}).get("status") != "ok"))}


def summary(result) -> dict:
    result = result or {}
    return {"schema": SCHEMA, "status": result.get("status"),
            "stats": result.get("stats") or {},
            "verdicts": [{"point": v.get("point"),
                          "decision": (v.get("result") or {}).get("decision"),
                          "reason": (v.get("result") or {}).get("reason"),
                          "drop": v["applied"]["drop"],
                          "unresolved": v["applied"].get("unresolved") or []}
                         for v in (result.get("verdicts") or ())]}


__all__ = ["BUDGET", "CATEGORIES", "DECISIONS", "PROMPT_VERSION", "SCHEMA", "apply",
           "arbitrate", "candidate_facts", "conflict_points", "position_facts",
           "position_of", "run", "shown_candidates", "summary"]
