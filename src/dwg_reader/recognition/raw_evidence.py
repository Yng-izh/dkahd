"""原始证据库：任何图元都不丢弃，只登记"被谁用了 / 为什么没用 / 还能试什么"。

状态：unused / weak / used / rejected / inferred
（inferred = 只被推断几何引用，例如虚拟补墙边，不算真实图元使用）

每条记录都要能回答五个问题（PR-1 的核心要求）：

    原始 handle -> 原语 id -> 参与过的候选 -> 接受/拒绝原因 -> 可尝试的修复操作

因此记录里固定有 ``prim_ids / candidates / reason / reasons / repairs / decisions``。
"零丢失"指的是**证据不丢**，不是"候选随便膨胀"：搜索规模仍由空间索引、
候选冲突关系和预算控制（见 :func:`pool` 的分组上限）。
"""
from __future__ import annotations

UNUSED, WEAK, USED, REJECTED, INFERRED = ("unused", "weak", "used", "rejected",
                                          "inferred")

#: 每种"没用上"的原因对应可尝试的修复策略（闭集，与 operator_specs 一致）
REPAIRS_BY_REASON = {
    "unpaired_face": ("recover_wall_network",),
    "THICKNESS_CLUSTER_WEAK": ("recover_wall_network",),
    "short_segment": ("recover_wall_network",),
    "OPEN_CONTOUR": ("recover_column_geometry",),
    "UNSUPPORTED_PATTERN": ("recover_openings",),
    "WRONG_LAYER": ("repair_semantics",),
}

MAX_POOL_GROUPS = 24


def build(snapshot) -> dict:
    ledger = {}
    for e in snapshot.get("entities", ()):
        h = e.get("handle")
        if not h:
            continue
        ledger[h] = {"status": UNUSED, "type": e.get("type"), "layer": e.get("layer"),
                     "roles": [], "reason": None, "reasons": [], "candidates": [],
                     "prim_ids": [], "repairs": [], "decisions": [],
                     "bbox": _bbox_of(e)}
    for e in snapshot.get("derived", ()):
        h = e.get("handle")
        if h and h not in ledger:
            ledger[h] = {"status": UNUSED, "type": e.get("type"), "layer": e.get("layer"),
                         "roles": ["derived"], "reason": None, "reasons": [],
                         "candidates": [], "prim_ids": [], "repairs": [],
                         "decisions": [], "bbox": _bbox_of(e), "via": e.get("via")}
    return ledger


def mark(ledger, handle, status, *, role=None, reason=None, candidate=None) -> None:
    rec = (ledger or {}).get(str(handle))
    if rec is None:
        return
    if status in (USED, REJECTED):
        rec["status"] = status
    elif rec["status"] == UNUSED:
        rec["status"] = status
    if role and role not in rec["roles"]:
        rec["roles"].append(role)
    if reason:
        rec["reason"] = reason
        if reason not in (rec.get("reasons") or ()):
            rec.setdefault("reasons", []).append(reason)
        for strat in REPAIRS_BY_REASON.get(str(reason), ()):
            offer(ledger, [handle], (strat,))
    if candidate and candidate not in rec["candidates"]:
        rec["candidates"].append(candidate)


def mark_many(ledger, handles, status, **kw) -> None:
    for h in handles or ():
        mark(ledger, h, status, **kw)


def offer(ledger, handles, repairs) -> None:
    """登记"这条证据还能尝试哪些策略"（不是承诺，只是可选项）。"""
    for h in handles or ():
        rec = (ledger or {}).get(str(h))
        if rec is None:
            continue
        for r in repairs or ():
            if r and r not in rec["repairs"]:
                rec["repairs"].append(r)


def decide(ledger, handles, decision, *, reason=None) -> None:
    """登记一次决策（尝试过什么、结果如何），保证可回放。"""
    for h in handles or ():
        rec = (ledger or {}).get(str(h))
        if rec is None:
            continue
        rec["decisions"].append({"decision": str(decision), "reason": reason})


def annotate_prims(ledger, prims) -> None:
    """把 ``pat:<n>`` 原语 id 挂到对应 handle 上（与 evidence.build 的编号一致）。"""
    for i, p in enumerate((prims or {}).get("lines", ()), 1):
        h = p.get("handle")
        rec = (ledger or {}).get(str(h)) if h else None
        if rec is not None and f"pat:{i}" not in rec["prim_ids"]:
            rec["prim_ids"].append(f"pat:{i}")


def copy(ledger: dict) -> dict:
    """浅拷贝 + 内部列表深拷贝（PURE 节点里挂状态用）。"""
    out = {}
    for h, rec in (ledger or {}).items():
        item = dict(rec)
        for key in ("roles", "candidates", "prim_ids", "repairs", "decisions",
                    "reasons"):
            item[key] = list(rec.get(key) or ())
        out[h] = item
    return out


def trace(ledger: dict, handle) -> dict | None:
    """单条证据的完整链路（handle -> 原语 -> 候选 -> 原因 -> 可试修复）。"""
    rec = (ledger or {}).get(str(handle))
    if rec is None:
        return None
    return {"handle": str(handle), "status": rec.get("status"),
            "layer": rec.get("layer"), "type": rec.get("type"),
            "prim_ids": list(rec.get("prim_ids") or ()),
            "candidates": list(rec.get("candidates") or ()),
            "reason": rec.get("reason"), "reasons": list(rec.get("reasons") or ()),
            "repairs": list(rec.get("repairs") or ()),
            "decisions": list(rec.get("decisions") or ())}


def pool(ledger: dict, *, statuses=(WEAK, UNUSED, REJECTED), limit: int = MAX_POOL_GROUPS) -> list[dict]:
    """可复用证据池：按 (图层, 原因, 可试策略) 分组，供局部策略重新取用。

    分组而不是逐条列出——这是"零丢失"与"搜索爆炸"之间的折中：每条证据都还在
    池子里（``handles`` 全量给出），但交给模型/编译器的是一组组可操作的行。
    """
    groups: dict[tuple, dict] = {}
    for h, rec in (ledger or {}).items():
        if rec.get("status") not in statuses:
            continue
        key = (rec.get("layer") or "(无图层)", rec.get("reason") or "unused",
               tuple(rec.get("repairs") or ()))
        slot = groups.setdefault(key, {"layer": key[0], "reason": key[1],
                                       "repairs": list(key[2]),
                                       "handles": [], "prim_ids": []})
        slot["handles"].append(str(h))
        slot["prim_ids"].extend(rec.get("prim_ids") or [])
    out = sorted(groups.values(), key=lambda g: (-len(g["handles"]), g["layer"]))
    for g in out[:limit]:
        g["handles"] = g["handles"][:200]
        g["prim_ids"] = g["prim_ids"][:200]
        g["count"] = len(g["handles"])
    return out[:limit]


def summary(ledger: dict) -> dict:
    by_status: dict[str, int] = {}
    by_layer: dict[str, dict[str, int]] = {}
    for rec in (ledger or {}).values():
        by_status[rec["status"]] = by_status.get(rec["status"], 0) + 1
        lay = rec.get("layer") or "(无图层)"
        by_layer.setdefault(lay, {})
        by_layer[lay][rec["status"]] = by_layer[lay].get(rec["status"], 0) + 1
    total = len(ledger or {}) or 1
    worst = sorted(({"layer": k, "unused": v.get(UNUSED, 0) + v.get(WEAK, 0),
                     "total": sum(v.values())} for k, v in by_layer.items()),
                   key=lambda x: -x["unused"])[:8]
    repairs: dict[str, int] = {}
    with_prims = 0
    for rec in (ledger or {}).values():
        if rec.get("prim_ids"):
            with_prims += 1
        for r in rec.get("repairs") or ():
            repairs[r] = repairs.get(r, 0) + 1
    return {"schema": "raw-evidence/1.1", "total": len(ledger or {}),
            "by_status": by_status,
            "utilization": round(by_status.get(USED, 0) / total, 3),
            "weak_or_unused": by_status.get(WEAK, 0) + by_status.get(UNUSED, 0),
            "traceable_to_primitives": with_prims,
            "repair_offers": dict(sorted(repairs.items())),
            "worst_layers": worst}


def weak(ledger: dict, layer=None) -> list[str]:
    return [h for h, r in (ledger or {}).items()
            if r["status"] == WEAK and (layer is None or r.get("layer") == layer)]


def _bbox_of(rec) -> list | None:
    from . import evidence as EVID
    try:
        return EVID.bbox_of(rec)
    except Exception:
        return None
