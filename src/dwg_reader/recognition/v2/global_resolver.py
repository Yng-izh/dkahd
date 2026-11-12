"""全局联合求解：把候选池里的**竞争解释**放在一起裁决，而不是逐个接受。

为什么不能局部逐个验收
----------------------
同一组线束可能同时是：幕墙分格、普通墙的装饰线、窗框、图例符号。局部看，每一
种解释都能自洽地通过"几何合法 + 有来源"，于是谁先被试谁就赢——这不是识别，
这是抽签。

所以要构造候选冲突图，联合选一组**整体最一致**的构件：

.. math::

    \\hat H = \\arg\\max_H \\Big[ \\sum_i S(h_i) + \\sum_{i,j} R(h_i, h_j) \\Big]

* :math:`S(h_i)` 单个候选的证据分（几何完整性 / 来源可信度 / 证据强度）；
* :math:`R(h_i,h_j)` 兼容性（墙托住洞口 +，同位置互斥 −）。

求解用手写的小规模优化（贪心 + 局部改进），不引入 ILP 依赖：先在**小区域**里
做，避免一上来对整张大图做组合搜索。权重是首版统一配置，必须用独立标注校准
（这份代码只提供口径，不给"我们调好了"的假象）。

多状态：结果不是"一个答案"，而是排名靠前的若干组合（``states``），
方便下游（房间/洞口）在多个假设下继续算，而不是早早把别的解释丢掉。
"""
from __future__ import annotations

import math

SCHEMA = "global-resolution/1.0"

#: 逻辑预算（个/次）
BUDGET = {"states": 3, "max_candidates": 4000, "rounds": 6,
          "exclusive_iou": 0.45, "duplicate_iou": 0.85, "support_ratio": 0.35}

#: 兼容性总影响的上限：一个候选周围有几百个邻居时，不能让"支持/冲突"的和
#: 无限增长（否则大图里必然出现"某项被邻居淹死"）。取值是数量级约定，不是阈值。
PAIR_SCALE = {"weight": 0.5, "cap": 1.0}

#: 得分权重（首版统一配置；必须用独立标注校准后才敢谈准确率）
WEIGHTS = {
    "evidence": 0.40,      # 证据强度（strong/weak、部分绑定比例、关系满足率）
    "geometry": 0.35,      # 几何完整度（有轴有宽有厚有名有轮廓）
    "source": 0.25,        # 来源可信度（规则 > 拟合 > 纯模型）
    "support": 0.25,       # 与已选构件的兼容性加成
    "conflict": 1.00,      # 冲突惩罚
}

#: 来源可信度先验
SOURCE_TRUST = {"rule": 0.95, "pattern": 0.70, "ai": 0.60}

#: 互斥的构件对（同一块地方不可能同时是这两样）
EXCLUSIVE = {
    frozenset(("door", "window")),
    frozenset(("column", "window")),
    frozenset(("column", "door")),
    frozenset(("stair", "room")),
}

#: 相容关系：宿主与被支撑（洞口的宿主是墙）
SUPPORTS = {("wall", "window"), ("wall", "door"), ("wall", "column"),
            ("stair", "step"), ("apron", "wall"),
            # 房间与洞口/墙是**互相支撑**的关系：房间在墙网里，洞口开在墙上
            ("room", "door"), ("room", "window"), ("room", "wall"),
            ("step", "door"), ("step", "apron")}

#: 空间类构件（房间）不与结构构件争夺同一块地：它们由墙网派生，允许共存
DERIVED_CATEGORIES = frozenset({"room", "grid_axis", "frame"})


def _r(v, nd: int = 3) -> float:
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return 0.0


def _iou(a, b) -> float:
    if not a or not b:
        return 0.0
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    area_a = max(1e-9, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1e-9, (b[2] - b[0]) * (b[3] - b[1]))
    return inter / (area_a + area_b - inter)


def _gap(a, b) -> float:
    if not a or not b:
        return float("inf")
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


# --------------------------------------------------------------------- 单点分
def score(hyp: dict, *, weights: dict | None = None) -> float:
    """单个候选的证据分 S(h)（0~1，只用于排序与选择）。"""
    w = dict(WEIGHTS, **(weights or {}))
    ev = hyp.get("evidence") or {}
    geom = hyp.get("geometry") or {}

    state = str(ev.get("evidence_state") or "")
    strength = {"strong": 1.0, "weak": 0.5, "pending": 0.35}.get(state, 0.6)
    parts_total = float(ev.get("parts_total") or 0.0)
    parts_bound = float(ev.get("parts_bound") or 0.0)
    if parts_total:
        strength = max(strength * 0.5, 0.5 * parts_bound / parts_total
                       + 0.5 * strength)
    rel_ok = float(ev.get("relations_satisfied") or 0.0)
    rel_bad = float(ev.get("relations_failed") or 0.0)
    if rel_ok or rel_bad:
        strength = 0.6 * strength + 0.4 * (rel_ok / (rel_ok + rel_bad))

    completeness = 0.0
    for key in ("axis", "outline", "thickness_mm", "width_mm", "w_mm", "h_mm",
                "length_mm", "spacing_mm"):
        if geom.get(key) not in (None, [], 0, 0.0):
            completeness += 1.0
    completeness = min(1.0, completeness / 4.0)
    if ev.get("wall_anchor"):
        completeness = min(1.0, completeness + 0.2)

    source = str(hyp.get("source") or "ai")
    trust = float(SOURCE_TRUST.get(source, 0.5))
    if hyp.get("alternatives"):
        trust = min(1.0, trust + 0.05)     # 多个引擎同意的，略加分

    return _r(w["evidence"] * strength + w["geometry"] * completeness
              + w["source"] * trust)


# --------------------------------------------------------------------- 两两关系
def relation(a: dict, b: dict, *, thickness: float) -> tuple[float, str]:
    """两个候选的兼容性 R(a,b)：正=互相支持，负=冲突，0=无关。"""
    cat_a, cat_b = str(a.get("category")), str(b.get("category"))
    iou = _iou(a.get("bbox"), b.get("bbox"))
    gap = _gap(a.get("bbox"), b.get("bbox"))

    if cat_a == cat_b:
        if iou >= float(BUDGET["duplicate_iou"]):
            return -1.0, "DUPLICATE"
        return 0.0, ""

    pair = frozenset((cat_a, cat_b))
    band = max(1.0, float(thickness))
    # 派生类（房间/轴网/图框）不与结构构件互相排斥：它们本来就叠在一起
    if cat_a in DERIVED_CATEGORIES or cat_b in DERIVED_CATEGORIES:
        return 0.0, ""
    if pair in EXCLUSIVE:
        if iou >= float(BUDGET["exclusive_iou"]):
            return -1.0, "MUTUALLY_EXCLUSIVE"
        if gap <= 0.0:                     # 紧贴：可能是同一个洞口的两种解释
            return -0.20, "TOO_CLOSE_EXCLUSIVE"
        return 0.0, ""                     # 只是离得近，不构成冲突
    if (cat_a, cat_b) in SUPPORTS or (cat_b, cat_a) in SUPPORTS:
        host, guest = (a, b) if (cat_a, cat_b) in SUPPORTS else (b, a)
        if iou >= float(BUDGET["duplicate_iou"]):
            # 完全重合：这多半是"同一个东西被两个类别解释"，属于竞争而非支持
            host_cat = str(host.get("category"))
            if host_cat in ("wall",) and pair == frozenset(("wall", "window")):
                return 0.05, "CURTAIN_OR_WINDOW"
            return -0.5, "SAME_FOOTPRINT"
        if gap <= 2.0 * band:
            return 0.25, "SUPPORTED_BY_HOST"
        # 离得远 ≠ 冲突：这里必须是 0。曾经给 -0.10，结果一堵墙旁边有 30 根柱时
        # 累加起来把墙"投"成负收益，导致整张图的墙全被弃掉（真实回归）。
        return 0.0, "HOST_TOO_FAR"
    if iou >= float(BUDGET["duplicate_iou"]):
        return -0.3, "OVERLAP_UNEXPLAINED"
    return 0.0, ""


def build_conflicts(pool, *, thickness: float) -> dict:
    """候选冲突图：只连"有实际关系"的对（无关对不建边，省内存也省解释）。"""
    rows = [h for h in (pool.get("hypotheses") if isinstance(pool, dict) else pool) or ()
            if isinstance(h, dict)]
    rows = rows[:int(BUDGET["max_candidates"])]
    edges: list[dict] = []
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            weight, reason = relation(a, b, thickness=thickness)
            if weight == 0.0:
                continue
            edges.append({"a": a.get("id"), "b": b.get("id"),
                          "w": _r(weight), "reason": reason})
    return {"schema": SCHEMA, "nodes": len(rows), "edges": edges,
            "stats": {"conflicts": sum(1 for e in edges if e["w"] < 0),
                      "supports": sum(1 for e in edges if e["w"] > 0)}}


# --------------------------------------------------------------------- 联合选择
def _objective(selected, singles: dict, pair: dict) -> float:
    total = sum(singles.get(h, 0.0) for h in selected)
    for i, a in enumerate(selected):
        for b in selected[i + 1:]:
            total += pair.get((a, b), pair.get((b, a), 0.0))
    return total


def _pair_table(edges) -> dict:
    out: dict[tuple, float] = {}
    for edge in edges or ():
        out[(edge["a"], edge["b"])] = float(edge["w"])
    return out


def solve(pool, *, thickness: float = 200.0, budget=None, weights=None) -> dict:
    """贪心 + 局部改进，返回排名靠前的若干组合（``states``）。

    约束永远先硬后软：互斥/重复这类**硬冲突**直接不允许同选（贪心时跳过、
    局部改进时拒绝）；支持关系只是加分。
    """
    budget = dict(BUDGET, **(budget or {}))
    rows = [h for h in (pool.get("hypotheses") if isinstance(pool, dict) else pool) or ()
            if isinstance(h, dict)]
    rows = rows[:int(budget["max_candidates"])]
    singles = {h["id"]: score(h, weights=weights) for h in rows}
    graph = build_conflicts({"hypotheses": rows}, thickness=thickness)
    pair = _pair_table(graph["edges"])
    hard_negative = {(e["a"], e["b"]) for e in graph["edges"]
                     if float(e["w"]) <= -0.9 or float(e["w"]) >= 0.9
                     and e["reason"] == "DUPLICATE"}
    hard_negative |= {(e["b"], e["a"]) for e in graph["edges"]
                      if float(e["w"]) <= -0.9}
    support = {(e["a"], e["b"]) for e in graph["edges"]
               if e["reason"] == "SUPPORTED_BY_HOST"}

    def _blocked(chosen: set, cid: str) -> bool:
        for other in chosen:
            if (cid, other) in hard_negative or (other, cid) in hard_negative:
                return True
        return False

    order = sorted(rows, key=lambda h: -(singles.get(h["id"], 0.0)
                                        + 0.3 * sum(1 for k in support
                                                    if h["id"] in k)))
    chosen: list[str] = []
    for hyp in order:
        if _blocked(set(chosen), hyp["id"]):
            continue
        pair_sum = sum(pair.get((hyp["id"], other),
                                pair.get((other, hyp["id"]), 0.0))
                       for other in chosen)
        pair_sum = max(-float(PAIR_SCALE["cap"]),
                       min(float(PAIR_SCALE["cap"]), pair_sum))
        gain = singles[hyp["id"]] + float(PAIR_SCALE["weight"]) * pair_sum
        if gain <= 0:
            continue
        chosen.append(hyp["id"])

    # 局部改进：尝试把每个未选中的换个已选中的，objective 变大才换
    for _ in range(int(budget["rounds"])):
        improved = False
        for hyp in order:
            if hyp["id"] in chosen:
                continue
            base = _objective(chosen, singles, pair)
            for victim in list(chosen):
                trial = [c for c in chosen if c != victim]
                if _blocked(set(trial), hyp["id"]):
                    continue
                trial.append(hyp["id"])
                if _objective(trial, singles, pair) > base + 1e-9:
                    chosen = trial
                    improved = True
                    break
            if improved:
                break
        if not improved:
            break

    def _state(ids) -> dict:
        picked = [h for h in rows if h["id"] in set(ids)]
        return {"ids": list(ids), "objective": _r(_objective(ids, singles, pair)),
                "counts": {c: sum(1 for h in picked if h["category"] == c)
                           for c in sorted({h["category"] for h in picked})},
                "candidates": picked}

    primary = _state(chosen)
    # 多状态：按 S 单独排序的"次优解释"各去掉一个换来，供下游比较
    alternates = []
    for hyp in order:
        if len(alternates) >= int(budget["states"]) - 1:
            break
        if hyp["id"] in chosen:
            continue
        if _blocked(set(chosen), hyp["id"]):
            continue
        alt = chosen + [hyp["id"]]
        alternates.append(_state(alt))
    states = [primary] + alternates
    rejected = [{"id": h["id"], "category": h["category"], "score": singles[h["id"]],
                 "why": "NOT_SELECTED"} for h in order if h["id"] not in set(chosen)]
    return {"schema": SCHEMA, "selected": primary["candidates"],
            "selected_ids": primary["ids"], "objective": primary["objective"],
            "counts": primary["counts"], "states": states, "rejected": rejected[:200],
            "conflicts": graph["stats"], "weights": dict(WEIGHTS, **(weights or {}))}


# --------------------------------------------------------------------- 分阶段
STAGES = ("walls", "openings", "spaces")

#: 每个阶段关心哪些类别
STAGE_CATEGORIES = {
    "walls": ("wall", "column"),
    "openings": ("door", "window"),
    "spaces": ("room",),
}


def staged(pool, *, thickness: float = 200.0, faces=(), budget=None,
           space_builder=None) -> dict:
    """分阶段联合求解：墙/柱 → 洞口 → 空间，每阶段保留多个状态。

    为什么分阶段：房间依赖墙网，洞口依赖宿主墙。如果一步到位把墙、洞口、房间
    一起优化，代价高且更容易过拟合；分阶段则可以把上一阶段的**成品**当下一阶段
    的硬上下文，同时仍然保留（有限个）备选解释。
    """
    budget = dict(BUDGET, **(budget or {}))
    rows = list((pool.get("hypotheses") if isinstance(pool, dict) else pool) or ())
    out: dict = {"schema": SCHEMA, "stages": {}, "needs": [], "states": []}
    carry: list[dict] = []
    for stage in STAGES:
        cats = STAGE_CATEGORIES[stage]
        if stage == "spaces":
            # 房间不是"候选竞赛"的产物：必须由**选中的墙网**重算面域再挂房名。
            # 给了 space_builder 就当场算（单向链：墙 -> 面域 -> 房名）；
            # 没给就明确写 needs_geometry，绝不假装算过。
            selection = list(carry)
            if space_builder is None:
                out["stages"][stage] = {"status": "needs_geometry",
                                        "categories": list(cats),
                                        "note": "房间面域必须由墙网重算（space_solver）"}
                out["needs"].append({"stage": stage, "action": "rebuild_faces",
                                     "walls": [h["id"] for h in selection
                                               if h.get("category") == "wall"]})
                continue
            try:
                built = space_builder(selection) or {}
            except Exception as exc:                     # 参数给错也不该拖垮求解
                out["stages"][stage] = {"status": "failed",
                                        "error": type(exc).__name__}
                continue
            rooms = [h for h in (built.get("hypotheses") or ())
                     if isinstance(h, dict)]
            carry = carry + rooms
            out["stages"][stage] = {"status": "ok", "categories": list(cats),
                                    "counts": {"room": len(rooms)},
                                    "faces": int((built.get("stats") or {}).get("faces")
                                                 or 0),
                                    "rooms": len(rooms),
                                    "orphans": int((built.get("stats") or {})
                                                   .get("orphans") or 0),
                                    "selected": [h.get("id") for h in rooms]}
            out["space_build"] = {"schema": built.get("schema"),
                                  "status": built.get("status"),
                                  "stats": built.get("stats")}
            continue
        subset = {"hypotheses": carry + [h for h in rows
                                         if h.get("category") in cats]}
        res = solve(subset, thickness=thickness, budget=budget)
        carry = res["selected"]
        out["stages"][stage] = {"status": "ok", "objective": res["objective"],
                                "counts": res["counts"],
                                "selected": [h["id"] for h in res["selected"]],
                                "states": len(res["states"]),
                                "conflicts": res["conflicts"]}
        out["states"].append({"stage": stage, "states": res["states"][:int(
            budget["states"])]})
    total = sum(float((info or {}).get("objective") or 0.0)
                for info in (out["stages"] or {}).values())
    out["final"] = {"selected": carry,
                    "objective": _r(total),
                    "counts": {c: sum(1 for h in carry if h.get("category") == c)
                               for c in sorted({h.get("category") for h in carry})}}
    out["objective"] = out["final"]["objective"]
    return out


def summary(result) -> dict:
    """既能总结单次 ``solve()``，也能总结 ``staged()``（两种返回形状）。"""
    result = result or {}
    staged = bool(result.get("stages"))
    selected = result.get("selected") or ((result.get("final") or {}).get("selected"))
    counts = result.get("counts") or ((result.get("final") or {}).get("counts"))
    objective = result.get("objective") if not staged else (
        (result.get("final") or {}).get("objective"))
    return {"schema": SCHEMA, "objective": objective,
            "counts": counts or {},
            "selected": len(selected or ()),
            "staged": staged,
            "stages": {k: {"counts": (v or {}).get("counts"),
                           "status": (v or {}).get("status")}
                       for k, v in (result.get("stages") or {}).items()},
            "rejected": len(result.get("rejected") or ()),
            "states": len(result.get("states") or ()),
            "conflicts": result.get("conflicts") or {},
            "weights": result.get("weights") or {}}
