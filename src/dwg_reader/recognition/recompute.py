"""增量重算：局部子 DAG **真正执行** + 区域切片/回填的纯函数。

旧版只有 :func:`plan`（"建议重跑哪些节点"）；本版新增
:func:`execute_local_subdag`：把编译好的计划展开成有依赖的执行序列，按阶段
（列 → 墙 → 墙网 → 面域 → 洞口 → 房间 → 校验）真实重算，并把"墙/柱变了但策略
里没写面域重建"这种情况自动补上下游重算，避免出现"墙修好了、房间还是旧的"。

执行是**事务式**的：只在状态副本上进行，返回新状态 + 轨迹；是否提交由
:mod:`local_solver` 的影子验收决定，失败则整段丢弃（原状态一字节不变）。
"""
from __future__ import annotations

import time
import hashlib
import json

from . import executors as EXEC
from . import operator_specs as OPSPEC

DOWNSTREAM = {
    "load": ("walls_seg", "wall_model", "spaces_seed", "openings_select",
             "walls_merge", "rooms"),
    "columns": ("spaces_seed", "openings_select", "walls_merge", "rooms"),
    "walls_seg": ("wall_model", "spaces_seed", "openings_select", "walls_merge", "rooms"),
    "wall_model": ("spaces_seed", "openings_select", "walls_merge", "rooms"),
    "spaces_seed": ("openings_select", "walls_merge", "rooms"),
    "doors": ("openings_select", "walls_merge", "rooms"),
    "windows": ("openings_select", "walls_merge", "rooms"),
    "openings_select": ("walls_merge", "rooms"),
    "walls_merge": ("rooms",),
    "rooms": (),
}
ORDER = ("load", "walls_seg", "wall_model", "spaces_seed", "openings_select",
         "walls_merge", "rooms", "assemble")
COST = {"walls_seg": 3, "spaces_seed": 4, "openings_select": 2,
        "walls_merge": 2, "rooms": 3}
PAD = 300.0

#: 局部子 DAG 的阶段顺序（由 ``operator_specs.STAGE_DEPS`` 推导，缓存一次）
_STAGE_ORDER: list[str] = []

#: 由控制器"前置钩子"完成的算子（几何在钩子里算，步骤本身不改状态）
HOOK_OPS = ("override_layer_role",)

#: 预算默认值（可被计划里的 budget 覆盖）
MAX_STEPS = 8
MAX_MS = 4000.0
MAX_FACES = 4000


def plan(nodes) -> dict:
    todo = set()
    for n in nodes or ():
        todo.add(n)
        todo |= set(DOWNSTREAM.get(n, ()))
    seq = [n for n in ORDER if n in todo]
    return {"nodes": seq, "count": len(seq),
            "cost_hint": sum(COST.get(n, 1) for n in seq)}


def stage_order() -> list[str]:
    """阶段的确定性拓扑序（列 / 墙 -> 墙网 -> 面域 -> 洞口 -> 房间 -> 校验）。"""
    if _STAGE_ORDER:
        return list(_STAGE_ORDER)
    done: list[str] = []

    def visit(stage: str, stack: tuple[str, ...]) -> None:
        if stage in done:
            return
        if stage in stack:
            return
        for dep in OPSPEC.STAGE_DEPS.get(stage, ()):
            visit(dep, stack + (stage,))
        done.append(stage)

    for stage in OPSPEC.STAGES:
        visit(stage, ())
    _STAGE_ORDER.extend(done)
    return list(done)


# ------------------------------------------------------------------ 区域切片
def slice_region(records, region) -> list:
    if not region:
        return list(records or ())
    x0, y0, x1, y1 = region
    out = []
    for r in records or ():
        b = r.get("bbox")
        if not (isinstance(b, (list, tuple)) and len(b) == 4):
            b = None
            s, e = r.get("start"), r.get("end")
            if isinstance(s, (list, tuple)) and isinstance(e, (list, tuple)):
                b = [min(s[0], e[0]), min(s[1], e[1]), max(s[0], e[0]), max(s[1], e[1])]
            else:
                p = r.get("insert") or r.get("center")
                # wall 的 "center" 是法向偏移量（标量），不能当点用
                if isinstance(p, (list, tuple)) and len(p) >= 2:
                    b = [p[0], p[1], p[0], p[1]]
        if not b:
            continue
        if (b[2] >= x0 - PAD and b[0] <= x1 + PAD
                and b[3] >= y0 - PAD and b[1] <= y1 + PAD):
            out.append(r)
    return out


def _key(rec) -> str | None:
    if not isinstance(rec, dict):
        return None
    value = rec.get("id") or rec.get("handle")
    if value is not None:
        return str(value)
    raw = json.dumps(rec, sort_keys=True, ensure_ascii=False, default=str)
    return "anon:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def apply_record_changes(base, *, upsert=(), remove_ids=()) -> list:
    """Apply a transactional ID-based set of additions, updates and deletions."""
    drop = {str(rid) for rid in remove_ids or ()}
    replacing = {_key(r) for r in upsert or () if _key(r) is not None}
    keep = [r for r in base or () if _key(r) not in (drop | replacing)]
    return keep + [dict(r) for r in (upsert or ()) if isinstance(r, dict)]


def merge_region(base, patch, region) -> list:
    """Replace a spatial slice; with no region, use ID upsert (not append)."""
    if not patch:
        return list(base or ())
    if not region:
        return apply_record_changes(base, upsert=patch)
    keep = [r for r in (base or ()) if r not in slice_region([r], region)]
    return apply_record_changes(keep, upsert=patch)


def apply_to_walls(walls_seg, patch, region) -> list:
    return merge_region(walls_seg, patch, region)


def apply_to_openings(doors, windows, patch, region) -> tuple[list, list]:
    """Apply accepted openings using ID upserts; never duplicate an existing ID."""
    if not patch:
        return list(doors or ()), list(windows or ())
    new_d, new_w = [], []
    for o in patch:
        if not _accepted(o):
            continue
        kind = o.get("kind") or o.get("category")
        (new_d if kind == "door" else new_w).append(o)
    if not region:
        return (apply_record_changes(doors, upsert=new_d),
                apply_record_changes(windows, upsert=new_w))
    keep_d = [d for d in (doors or ()) if not slice_region([d], region)]
    keep_w = [w for w in (windows or ()) if not slice_region([w], region)]
    return (apply_record_changes(keep_d, upsert=new_d),
            apply_record_changes(keep_w, upsert=new_w))


def _accepted(rec) -> bool:
    return bool(rec.get("status") == "accept" or rec.get("accept_state") == "accepted"
                or rec.get("_llm_accept"))


def summary(patched: dict) -> dict:
    """二次求解摘要（进审计）。"""
    return {"status": patched.get("status"),
            "region": patched.get("region"),
            "walls": len(patched.get("walls") or ()),
            "openings": len(patched.get("openings") or ()),
            "faces": len(patched.get("faces") or ()),
            "recomputed": patched.get("recomputed") or ()}


# ------------------------------------------------------------------ 状态工具
_STATE_BUCKETS = ("walls", "columns", "doors", "windows", "openings", "faces",
                  "labels", "unresolved", "inferred_edges")


def freeze(state: dict) -> dict:
    """状态副本：外层 dict 与内部 record 都是新对象（调用方状态永不被改）。"""
    out: dict = {}
    for k, v in (state or {}).items():
        if isinstance(v, list):
            out[k] = [dict(r) if isinstance(r, dict) else r for r in v]
        elif isinstance(v, dict):
            out[k] = dict(v)
        else:
            out[k] = v
    for bucket in _STATE_BUCKETS:
        out.setdefault(bucket, [])
    return out


def counts(state: dict) -> dict:
    st = state or {}
    walls = list(st.get("walls") or ())
    return {"walls": len(walls),
            "columns": len(st.get("columns") or ()),
            "doors": len(st.get("doors") or ()),
            "windows": len(st.get("windows") or ()),
            "faces": len(st.get("faces") or ()),
            "inferred_walls": sum(1 for w in walls
                                  if isinstance(w, dict)
                                  and w.get("geometry_state") == "inferred"),
            "unresolved": len(st.get("unresolved") or ())}


# ------------------------------------------------------------------ 子 DAG 执行
def execute_local_subdag(compiled_plan, *, state: dict, index: dict,
                         columns=(), partitions=(), thick=None, labels=None,
                         actions_by_op=None, budget=None, hooks=None) -> dict:
    """按编译好的计划执行一段真实局部重算（事务式，不改入参）。

    返回 ``{"state", "trace", "stages", "changed", "implicit", "status", "ms"}``。

    ``hooks`` 目前由控制器在调用**之前**应用（语义重算需要整图快照），这里只
    接受参数以保持签名稳定。
    """
    started = time.perf_counter()
    actions_by_op = dict(actions_by_op or {})
    budget = dict(budget or (getattr(compiled_plan, "budget", None) or {}))
    max_steps = int(budget.get("max_steps") or MAX_STEPS)
    max_ms = float(budget.get("ms") or MAX_MS)

    steps = list(getattr(compiled_plan, "steps", []) or ())
    targets = list(getattr(compiled_plan, "target_ids", ()) or ())
    hypothesis = getattr(compiled_plan, "hypothesis", None)
    if not steps:
        return {"state": freeze(state), "trace": [], "stages": [], "implicit": [],
                "changed": {}, "status": "empty",
                "ms": round((time.perf_counter() - started) * 1000.0, 3)}

    work = freeze(state)
    work.setdefault("partitions", list(partitions or ()))
    work.setdefault("thick", thick)
    work["labels"] = list(labels or work.get("labels") or ())

    order = stage_order()
    steps = sorted(steps, key=lambda s: (order.index(s["stage"])
                                        if s["stage"] in order else len(order),
                                        s.get("index", 0)))
    base = counts(work)
    trace: list[dict] = []
    stages: list[str] = []
    implicit: list[str] = []
    status = "ok"
    failed_indices: set[int] = set()
    failed_same_stage_producers: set[str] = set()
    for n, step in enumerate(steps):
        if n >= max_steps:
            status = "budget"
            break
        if (time.perf_counter() - started) * 1000.0 > max_ms:
            status = "budget"
            break
        operator = step["operator"]
        step_idx = int(step.get("index", n))
        if any(dep in failed_indices for dep in (step.get("deps") or ())):
            # The compiler creates dependency edges for a reason: a failed
            # upstream candidate generator must not trigger expensive global
            # topology rebuilding from unchanged geometry.
            failed_indices.add(step_idx)
            trace.append({"operator": operator, "stage": step["stage"],
                          "status": "skipped", "reason": "UPSTREAM_STEP_FAILED"})
            continue
        if (operator == "rebuild_space_faces"
                and "try_virtual_boundary" in failed_same_stage_producers):
            failed_indices.add(step_idx)
            trace.append({"operator": operator, "stage": step["stage"],
                          "status": "skipped", "reason": "UPSTREAM_STEP_FAILED"})
            continue
        if operator in HOOK_OPS:                 # 语义重算由控制器前置钩子完成
            trace.append({"operator": operator, "stage": step["stage"],
                          "status": "hook"})
            continue
        action = {"hypothesis": hypothesis}
        action.update(actions_by_op.get(operator) or {})
        try:
            patch = EXEC.execute_operator(
                operator, target_ids=action.get("target_ids") or targets,
                region=getattr(compiled_plan, "region", None), evidence_index=index,
                candidate_state=work, action=action)
        except Exception as exc:                     # 执行体异常 -> 该步不生效
            failed_indices.add(step_idx)
            if operator == "try_virtual_boundary":
                failed_same_stage_producers.add(operator)
            trace.append({"operator": operator, "stage": step["stage"],
                          "status": "failed", "error": type(exc).__name__})
            continue
        if patch.get("reason"):
            failed_indices.add(step_idx)
            if operator == "try_virtual_boundary":
                failed_same_stage_producers.add(operator)
            trace.append({"operator": operator, "stage": step["stage"],
                          "status": "skipped", "reason": patch["reason"]})
            continue
        # Some operators emit an empty patch without setting `reason`.
        # For generators that is still failure; downstream must not run on
        # the original unchanged geometry and claim a successful repair.
        if (operator in EXEC._GENERATORS
                and not any(patch.get(k) for k in ("walls", "columns", "openings"))):
            failed_indices.add(step_idx)
            trace.append({"operator": operator, "stage": step["stage"],
                          "status": "skipped", "reason": "NO_CANDIDATES_GENERATED"})
            continue
        work = EXEC.apply_patch(work, patch)
        if step["stage"] not in stages:
            stages.append(step["stage"])
        trace.append({"operator": operator, "stage": step["stage"], "status": "ok",
                      "added": len(patch.get("added") or ()),
                      "dropped": len(patch.get("drop_ids") or ()),
                      "stats": patch.get("stats") or {}})

    work = _ensure_downstream(work, stages=stages, trace=trace, implicit=implicit,
                              index=index, targets=targets)
    after = counts(work)
    changed = {k: after.get(k, 0) - base.get(k, 0)
               for k in ("walls", "columns", "doors", "windows", "faces")}
    geometry_changed = any(work.get(bucket) != state.get(bucket)
                           for bucket in ("walls", "columns", "doors", "windows", "faces"))
    changed["geometry_changed"] = bool(geometry_changed)
    changed["faces_geometry_changed"] = work.get("faces") != state.get("faces")
    if not any(changed.values()) and not implicit:
        status = "budget" if status == "budget" else "no_change"
    return {"state": work, "trace": trace, "stages": stages,
            "implicit": implicit, "changed": changed, "status": status,
            "counts_before": base, "counts_after": after,
            "executed_nodes": sorted({n for s in trace
                                      for n in OPSPEC.STAGE_NODES.get(s["stage"], ())})}


def _ensure_downstream(state: dict, *, stages, trace, implicit, index, targets) -> dict:
    """墙/柱改了但计划没写面域重建时，自动补一次面域（同一状态版本）。"""
    touched = {"walls", "columns"} & set(stages)
    if not touched:
        return state
    if "space" in stages:
        return state
    patch = EXEC.execute_operator("rebuild_space_faces", candidate_state=state,
                                 evidence_index=index, target_ids=targets)
    if patch.get("faces"):
        state = EXEC.apply_patch(state, patch)
        implicit.append("space")
        trace.append({"operator": "rebuild_space_faces", "stage": "space",
                      "status": "ok", "implicit": True,
                      "stats": patch.get("stats") or {}})
    if "wall_graph" not in stages:
        patch = EXEC.execute_operator("rebuild_wall_graph", candidate_state=state,
                                     evidence_index=index, target_ids=targets)
        if patch.get("stats"):
            state = EXEC.apply_patch(state, patch)
            implicit.append("wall_graph")
            trace.append({"operator": "rebuild_wall_graph", "stage": "wall_graph",
                          "status": "ok", "implicit": True,
                          "stats": patch.get("stats") or {}})
    return state
