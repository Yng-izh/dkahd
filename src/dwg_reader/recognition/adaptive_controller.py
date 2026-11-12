"""自适应控制器：外层 DAG 的一个节点，内部编译并执行**动态局部子 DAG**。

职责边界（这是本次架构改造的核心）：

* 它**决定**：哪些问题先修、用哪个策略、要不要问模型、试几次、什么时候停；
* 它**不**决定：坐标、阈值、算子实现、依赖顺序、是否提交——这些分别由
  ``executors`` / ``operator_specs`` / ``plan_compiler`` / ``local_solver`` 负责。

一次处理 = 编译 -> 影子执行 -> 六层验收 -> 提交或回滚。区域之间串行合并到同一
份状态（因此墙修好之后的房间用的是**同一版本**的墙网），单个区域被拒绝不影响
其他区域；全部被拒时状态一字节不变（纯回滚）。

预算（全部可在 ``budget`` 里覆盖）：``max_cases`` / ``max_runs`` / ``max_ms`` /
``max_llm_calls`` / ``plan_tries``。停止条件：预算耗尽、没有更多 case、或同一
``case_id`` 已经处理过（避免重排后无限重复）。
"""
from __future__ import annotations

import time
import hashlib
import json

from . import local_solver as SOLVER
from . import plan_compiler as COMPILER
from . import recompute as RC

SCHEMA = "adaptive-controller/1.1"

DEFAULT_BUDGET = {"max_cases": 12, "max_runs": 24, "max_ms": 20000.0,
                  "plan_tries": 2, "max_llm_calls": 6}

#: 影响面排序：单位/区域错误 -> 墙网缺失 -> 空间拓扑 -> 洞口归属 -> 语义/标签
PRIORITY = {"unit": 0, "area": 0, "wall": 1, "space": 2, "room": 2,
            "opening": 3, "semantic": 4, "other": 5}

#: 同一 case 下策略的尝试顺序（先便宜、再激进）
STRATEGY_ORDER = ("recover_openings", "recover_column_geometry",
                  "recover_wall_network", "recover_space_boundary",
                  "repair_semantics")


def run(*, state: dict, index: dict, cases, ledger=None, columns=(), partitions=(),
        thick=None, labels=(), repairs=(), plans=None, llm=None, hooks=None,
        budget=None) -> dict:
    """处理全部可修 case，返回最终状态 + 完整审计。

    ``plans``  : ``{case_id: [Plan]}`` 预编译好的确定性计划（可为空）
    ``llm``    : ``{case_id: proposal}`` 模型给出的策略提议（可为空）
    ``repairs``: 确定性修复候选（``repair_options`` 节点输出，含虚拟边坐标）
    ``hooks``  : ``{"layer_role": fn(layers, roles) -> patch}`` 语义重算回调
    """
    started = time.perf_counter()
    budget = dict(DEFAULT_BUDGET, **(budget or {}))
    hooks = dict(hooks or {})
    plans = dict(plans or {})
    llm = dict(llm or {})

    work = RC.freeze(state)
    ordered = _prioritize(cases)
    applied: list[dict] = []
    rolled_back: list[dict] = []
    rejected: list[dict] = []
    tried_cases: set[str] = set()
    runs = 0
    staged_roles: list[dict] = []
    regions_done: set[tuple] = set()
    status = "no_action"
    failed_fingerprints: set[tuple] = set()
    skipped_duplicates = 0

    for case in ordered:
        if runs >= int(budget["max_runs"]):
            status = "partial" if applied else "no_action"
            break
        if len(applied) >= int(budget["max_cases"]):
            break
        if (time.perf_counter() - started) * 1000.0 > float(budget["max_ms"]):
            status = "partial" if applied else "no_action"
            break
        cid = str(case.get("case_id") or "")
        if cid and cid in tried_cases:
            continue
        tried_cases.add(cid)
        region_key = tuple(round(float(v), -2) for v in (case.get("region") or ()))
        region = case.get("region")

        candidates = _plans_for(case, index=index, ledger=ledger, plans=plans,
                                llm=llm, budget=budget)
        if not candidates:
            rejected.append({"case_id": cid, "reason": "NO_RUNNABLE_PLAN"})
            continue
        accepted_here = False
        for plan in candidates[:int(budget["plan_tries"])]:
            if runs >= int(budget["max_runs"]):
                break
            if not plan.ok:
                rejected.append({"case_id": cid, "strategy": plan.strategy_id,
                                 "errors": plan.errors, "source": plan.source})
                continue
            if plan.empty:                       # NO_ACTION：明确的"不做"
                rejected.append({"case_id": cid, "strategy": plan.strategy_id,
                                 "reason": "NO_ACTION", "source": plan.source})
                continue
            independent = _independent_support(index, plan, case)
            if plan.strategy_id == "recover_space_boundary" and independent < 1:
                rejected.append({"case_id": cid, "strategy": plan.strategy_id,
                                 "reason": "NO_INDEPENDENT_GEOMETRY"})
                continue
            # Identical plan against an unchanged candidate-state cannot succeed
            # after it has failed once. Cache only within this invocation.
            # Region and case support are part of the fingerprint; two nearby
            # defects may share candidate IDs but require different geometry.
            fingerprint = (_state_fingerprint(work), plan.strategy_id,
                           tuple(sorted(str(t) for t in plan.target_ids)),
                           str(plan.hypothesis),
                           tuple(round(float(v), 2) for v in (region or ())),
                           tuple(sorted(str(v) for v in case.get("evidence_ids") or ())),
                           json.dumps(plan.steps, sort_keys=True, default=str))
            if fingerprint in failed_fingerprints:
                skipped_duplicates += 1
                rejected.append({"case_id": cid, "strategy": plan.strategy_id,
                                 "reason": "DUPLICATE_FAILED_PLAN"})
                continue
            runs += 1
            action = _actions_for(plan, case=case, index=index, repairs=repairs,
                                  labels=labels)
            pre, staged = _semantic_prefix(plan, hooks)
            start_state = _apply_pre(work, pre) if pre else work
            sub = RC.execute_local_subdag(plan, state=start_state, index=index,
                                          columns=columns, partitions=partitions,
                                          thick=thick, labels=labels,
                                          actions_by_op=action, hooks=hooks)
            if sub["status"] in ("empty", "no_change"):
                failed_fingerprints.add(fingerprint)
                rolled_back.append({"case_id": cid, "strategy": plan.strategy_id,
                                    "verdict": "NO_CHANGE", "runs": 1})
                continue
            support = _support_handles(index, region)
            verdict = SOLVER.shadow_evaluate(
                before=work, after=sub["state"], support=support, plan=plan,
                region=region, independent=independent)
            if not verdict["ok"]:
                failed_fingerprints.add(fingerprint)
                rolled_back.append({"case_id": cid, "strategy": plan.strategy_id,
                                    "verdict": verdict["verdict"],
                                    "failed_layers": [k for k, v in verdict["layers"].items()
                                                      if not v["ok"]],
                                    "layers": verdict["layers"]})
                continue
            patch = _diff(work, sub["state"], region=region)
            work = sub["state"]
            applied.append({"case_id": cid, "strategy": plan.strategy_id,
                            "hypothesis": plan.hypothesis, "source": plan.source,
                            "region": region, "trace": sub["trace"],
                            "implicit": sub["implicit"],
                            "changed": sub["changed"],
                            "verdict": verdict["verdict"],
                            "layers": verdict["layers"],
                            "executed_nodes": sub["executed_nodes"],
                            "patch": patch})
            if staged:
                staged_roles.extend(staged)
            accepted_here = True
            regions_done.add(region_key)
            break
        status = "applied" if (applied or status == "applied") else status
        if not accepted_here and cid:
            status = "partial" if applied else status

    return {"schema": SCHEMA, "state": work, "applied": applied,
            "rolled_back": rolled_back, "rejected": rejected,
            "status": "applied" if applied else "no_action",
            "cases_considered": len(ordered), "runs": runs,
            "regions": [{"key": list(k), "done": True} for k in sorted(regions_done)],
            "layer_role_staged": staged_roles,
            "executed_nodes": sorted({n for a in applied
                                      for n in a.get("executed_nodes") or ()}),
            "budget": dict(budget, used_runs=runs,
                           skipped_duplicate_runs=skipped_duplicates),
            # Net diff from the original baseline, not concatenated per-region
            # deltas: successive changes to one ID must be committed only once.
            "patch": _diff(state, work) if applied else {"status": "no_action"}}


# ------------------------------------------------------------------ 计划来源
def _plans_for(case: dict, *, index: dict, ledger, plans: dict, llm: dict,
               budget: dict) -> list:
    """候选计划：模型提议优先，其次确定性策略（都走同一个编译器）。"""
    cid = str(case.get("case_id") or "")
    out: list = []
    proposal = llm.get(cid)
    if isinstance(proposal, dict):
        out.append(COMPILER.compile_plan(proposal, index=index, ledger=ledger,
                                        budget=budget, source="llm"))
    for plan in plans.get(cid) or ():
        out.append(plan)
    if out:
        return out
    # 闸门已经算好"这个 case 允许哪些策略"，直接编译成确定性计划
    for strat in case.get("strategies") or ():
        sid = strat.get("strategy_id")
        targets = list(strat.get("target_ids") or ())
        if not sid or not targets:
            continue
        hypotheses = list(strat.get("hypotheses") or ())
        out.append(COMPILER.compile_plan(
            {"case_id": cid, "decision": "TRY", "strategy_id": sid,
             "target_ids": targets, "hypothesis": hypotheses[0] if hypotheses else None,
             "priority": case.get("priority") or "normal",
             "region": case.get("region")},
            index=index, ledger=ledger, budget=budget, source="deterministic"))
    return out


def deterministic_plans(cases, *, index: dict, ledger=None, budget=None) -> dict:
    """给编排层用：为每个 case 预编译确定性计划（LLM 不可用时的兜底路径）。"""
    out: dict[str, list] = {}
    for case in cases or ():
        plans = _plans_for(case, index=index, ledger=ledger, plans={}, llm={},
                           budget=budget or DEFAULT_BUDGET)
        if plans:
            out[str(case.get("case_id"))] = plans
    return out


def _actions_for(plan, *, case: dict, index: dict, repairs=(), labels=()) -> dict:
    """给需要"确定性上下文"的算子补输入（虚拟边坐标、房名坐标）。

    这些上下文全部由几何算法/证据账本给出，模型不参与，也拿不到。
    """
    action: dict = {}
    items = index.get("items") or {}
    wanted = set(plan.target_ids) | set(plan.support_ids) \
        | {str(x) for x in (case.get("evidence_ids") or ())}
    edges: list = []
    for opt in repairs or ():
        did = str(opt.get("defect_id") or "")
        if did and (did in wanted or f"ev:{did}" in wanted or not wanted):
            res = opt.get("result") or {}
            if res.get("ok") and int(res.get("independent") or 0) >= 1:
                edges.extend(res.get("inferred") or ())
    if edges:
        action["try_virtual_boundary"] = {"inferred_edges": edges[:6],
                                          "target_ids": list(plan.target_ids)}
    label_ids = [rid for rid in plan.target_ids
                 if (items.get(rid) or {}).get("kind") == "room_label"]
    picked = []
    for rid in label_ids:
        rec = items.get(rid) or {}
        picked.append({"id": str(rec.get("handle") or rid),
                       "x": rec.get("x"), "y": rec.get("y")})
    if not picked:
        picked = [{"id": str(l.get("handle") or l.get("id")),
                   "x": l.get("x"), "y": l.get("y")}
                  for l in (labels or ())[:24] if l.get("x") is not None]
    if picked:
        action["associate_room_labels"] = {"labels": picked,
                                           "target_ids": list(plan.target_ids)}
    return action


def _semantic_prefix(plan, hooks) -> tuple[dict, list[dict]]:
    """语义修复策略的前置钩子：``图层 -> 角色`` 由几何算法重算，模型只给角色。

    返回 ``(补丁, 待落盘的覆盖记录)``；没有图层目标时返回空（不假装成功）。
    """
    hook = (hooks or {}).get("layer_role")
    if not hook or plan.strategy_id != "repair_semantics":
        return {}, []
    layers = [str(t).split(":", 1)[-1] for t in plan.target_ids
              if str(t).startswith("layer:")]
    if not layers:
        return {}, []
    seq = [list(r) if isinstance(r, (list, tuple)) else [str(r)]
           for r in (plan.roles or ())]
    if not seq and plan.hypothesis:
        seq = [[plan.hypothesis]]
    if not seq:
        return {}, []
    mapping: dict[str, list] = {}
    for i, layer in enumerate(layers):
        roles = seq[i] if i < len(seq) else seq[0]
        roles = [r for r in roles if r in (
            "wall", "column", "door", "window", "room_name", "axis", "stair",
            "step", "apron", "north", "frame", "furniture", "ignore")][:2]
        if roles:
            mapping[layer] = roles
    if not mapping:
        return {}, []
    try:
        patch = hook(mapping) or {}
    except Exception:
        return {}, []
    if not patch:
        return {}, []
    return patch, [{"layer": k, "roles": v} for k, v in mapping.items()]


def _apply_pre(state: dict, patch: dict) -> dict:
    from . import executors as EXEC
    return EXEC.apply_patch(RC.freeze(state), patch)


# ------------------------------------------------------------------ 指标
def _support_handles(index: dict, region, limit: int = 600) -> set:
    """区域内的原始线证据（覆盖率的"分母"：这些线有多少被墙候选消化了）。"""
    out: set = set()
    for rid, rec in (index.get("items") or {}).items():
        if rec.get("kind") != "line_support" or not rec.get("handle"):
            continue
        if region and not _in_region(rec.get("bbox"), region):
            continue
        out.add(str(rec["handle"]))
        if len(out) >= limit:
            break
    return out


def _independent_support(index: dict, plan, case: dict) -> int:
    """独立证据条数：除房名之外、能各自独立指向同一结论的几何证据。"""
    items = index.get("items") or {}
    kinds = ("line_support", "entity", "contour", "hatch", "wall_candidate",
             "opening_candidate")
    got = {rid for rid in list(plan.target_ids) + list(plan.support_ids)
           if (items.get(rid) or {}).get("kind") in kinds}
    for eid in case.get("evidence_ids") or ():
        if (items.get(str(eid)) or {}).get("kind") in kinds:
            got.add(str(eid))
    return len(got)


def _prioritize(cases) -> list[dict]:
    def key(case):
        code = str(case.get("reason") or "").upper()
        band = PRIORITY["other"]
        for name, val in PRIORITY.items():
            if name.upper() in code and val < band:
                band = val
        return (band, -len(case.get("evidence_ids") or ()), str(case.get("case_id")))
    return sorted((c for c in (cases or ()) if isinstance(c, dict)), key=key)


def _record_id(rec: dict) -> str:
    """Stable ID when available; anonymous geometry is content-addressed."""
    rid = rec.get("id") or rec.get("handle")
    if rid is not None:
        return str(rid)
    raw = json.dumps(rec, sort_keys=True, ensure_ascii=False, default=str)
    return "anon:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _state_fingerprint(state: dict) -> str:
    raw = json.dumps({b: state.get(b) or [] for b in
                      ("walls", "columns", "doors", "windows", "faces")},
                     sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _diff(before: dict, after: dict, *, region=None) -> dict:
    """Lossless net change-set: added, updated and removed IDs plus face geometry.

    `region` is only audit metadata. A patch is applied by ID, never by appending
    all records when region is None (which created duplicate objects).
    """
    changes = {}
    added, updated, removed = [], [], []
    for bucket in ("walls", "columns", "doors", "windows"):
        old = {_record_id(r): r for r in (before.get(bucket) or ()) if isinstance(r, dict)}
        new = {_record_id(r): r for r in (after.get(bucket) or ()) if isinstance(r, dict)}
        upserts = [dict(r) for rid, r in new.items()
                   if rid not in old or old[rid] != r]
        gone = [rid for rid in old if rid not in new]
        changes[bucket] = {"upsert": upserts, "remove_ids": gone}
        added.extend(f"{bucket}:{rid}" for rid in new if rid not in old)
        updated.extend(f"{bucket}:{rid}" for rid in new if rid in old and new[rid] != old[rid])
        removed.extend(f"{bucket}:{rid}" for rid in gone)
    old_faces = before.get("faces") or []
    new_faces = after.get("faces") or []
    faces_changed = old_faces != new_faces  # detects same-count different polygons
    old_unresolved = before.get("unresolved") or []
    more_unresolved = [dict(r) if isinstance(r, dict) else r
                       for r in (after.get("unresolved") or [])
                       if r not in old_unresolved]
    old_inferred = before.get("inferred_edges") or []
    inferred = [dict(r) if isinstance(r, dict) else r
                for r in (after.get("inferred_edges") or []) if r not in old_inferred]
    return {"status": "accepted", "region": region, "schema": "candidate-patch/2",
            "changes": changes, "walls": [], "columns": [], "openings": [],
            "faces": list(new_faces) if faces_changed else [],
            "faces_changed": faces_changed, "inferred": inferred,
            "unresolved": more_unresolved, "added": added,
            "updated": updated, "removed": removed}


def _merge_patches(applied) -> dict:
    """Legacy helper for audit: consumers should use the net `_diff` above.

    Compose per-case ID changes in temporal order (last operation wins). This
    helper is retained for existing integrations, but is not used by `run`.
    """
    out = {"status": "accepted" if applied else "no_action", "region": None,
           "schema": "candidate-patch/2", "changes": {}, "faces": [],
           "faces_changed": False, "inferred": [], "unresolved": [],
           "added": [], "updated": [], "removed": []}
    for a in applied or ():
        patch = a.get("patch") or {}
        for b, delta in (patch.get("changes") or {}).items():
            cur = out["changes"].setdefault(b, {"upsert": [], "remove_ids": []})
            ups = {_record_id(r): r for r in cur["upsert"]}
            dels = set(cur["remove_ids"])
            for rid in delta.get("remove_ids") or ():
                ups.pop(str(rid), None)
                dels.add(str(rid))
            for r in delta.get("upsert") or ():
                rid = _record_id(r)
                ups[rid] = r
                dels.discard(rid)
            cur.update(upsert=list(ups.values()), remove_ids=sorted(dels))
        if patch.get("faces_changed"):
            out["faces_changed"] = True
            out["faces"] = list(patch.get("faces") or [])
        for k in ("inferred", "unresolved", "added", "updated", "removed"):
            out[k].extend(patch.get(k) or ())
    return out


def _in_region(b, region, pad=300.0) -> bool:
    if not b:
        return False
    return not (b[2] < region[0] - pad or b[0] > region[2] + pad
                or b[3] < region[1] - pad or b[1] > region[3] + pad)


def audit(result: dict) -> dict:
    """压缩成可审计摘要（进 final_audit）。"""
    result = result or {}
    return {"schema": SCHEMA,
            "status": result.get("status"),
            "cases": result.get("cases_considered"),
            "runs": result.get("runs"),
            "applied": [{"case": a.get("case_id"), "strategy": a.get("strategy"),
                         "source": a.get("source"),
                         "changed": a.get("changed"),
                         "steps": len(a.get("trace") or ()),
                         "implicit": a.get("implicit")} for a in result.get("applied") or ()],
            "rolled_back": [{"case": r.get("case_id"), "strategy": r.get("strategy"),
                             "why": r.get("verdict"),
                             "failed_layers": r.get("failed_layers")}
                            for r in result.get("rolled_back") or ()][:12],
            "rejected_plans": [{"case": r.get("case_id"), "strategy": r.get("strategy"),
                                "errors": r.get("errors"), "reason": r.get("reason")}
                               for r in result.get("rejected") or ()][:12],
            "executed_nodes": result.get("executed_nodes") or [],
            "budget": result.get("budget") or {}}


# ================================================================== AI 增强通道
# 上面 ``run()`` 是"确定性修复 case"的老通道，行为一个字节都不改。
# 下面 ``enhance()`` 是 AI 增强通道：**同一套**编译器 / 执行器 / 验收器，
# 只是多了"从残余图元发现 case"和"在同一基线上试多组参数"两件事。
AI_SCHEMA = "adaptive-controller/1.2"

#: 策略 -> 目标应该用哪种证据 kind（由该策略第一步算子的输入声明推导）
def _kinds_for(strategy_id: str) -> tuple[str, ...]:
    steps = COMPILER.OPSPEC.steps_of(strategy_id)
    for name in steps:
        spec = COMPILER.OPSPEC.REGISTRY.get(name)
        if spec is not None:
            return tuple(spec.input_kinds)
    return ()


def _targets_for(index: dict, handles, *, kinds=(), limit: int = 48) -> list[str]:
    """原始 handle -> 证据索引里**类型匹配**的 id（line_support 优先）。

    模型/案例给的是 DXF 原始 handle，而算子要的是证据 id（``pat:3`` 比 ``ev:A1``
    更具体）。这一步只做映射，不创造证据。
    """
    items = index.get("items") or {}
    by_handle: dict[str, list] = {}
    for rid, rec in items.items():
        handle = rec.get("handle")
        if handle:
            by_handle.setdefault(str(handle), []).append(rid)
    wanted = tuple(kinds) or ("line_support", "pattern", "contour", "hatch", "entity")

    def _ids_for(handle: str) -> list[str]:
        """handle 对应的证据 id；块实例还要**展开到它内部的派生几何**。

        PatternCase 里给的是块参照 handle（``2A3``），而块内几何在快照里记成
        ``2A3/1F``，线支撑原语又按这个子 handle 登记（``pat:n``）。少了这一步，
        洞口/墙类算子在块族案例上就永远"目标类型不匹配"。
        """
        out = list(by_handle.get(str(handle), ()))
        prefix = str(handle) + "/"
        for known, rids in by_handle.items():
            if known.startswith(prefix):
                out.extend(rids)
        return out

    ranked: list[str] = []
    for handle in handles or ():
        for rid in _ids_for(handle):
            kind = str((items.get(rid) or {}).get("kind") or "")
            if kind not in wanted:
                continue
            if rid not in ranked:
                ranked.append(rid)
            if len(ranked) >= limit:
                return ranked
    if ranked:
        return ranked
    # 类型不匹配时退回"任何引用这个 handle 的证据 id"：让编译器给出
    # TARGET_KIND_MISMATCH 这种**可读的失败码**，而不是静默地零计划。
    for handle in handles or ():
        for rid in _ids_for(handle):
            if rid not in ranked:
                ranked.append(rid)
        if len(ranked) >= limit:
            break
    return ranked


def _hypothesis_for(strategy_id: str | None, category: str) -> str | None:
    """把构件类别映射成该策略闭集里的假设名（不在闭集里就不给假设）。"""
    strat = COMPILER.OPSPEC.strategy(str(strategy_id or ""))
    if strat is None:
        return None
    if category in strat.hypotheses:
        return category
    alias = {"column": "column", "wall": "fragmented_wall", "window": "window",
             "door": "door", "room": "broken_boundary", "stair": "unknown_pattern",
             "step": "unknown_pattern", "apron": "unknown_pattern",
             "frame": "unknown_pattern", "north_arrow": "unknown_pattern",
             "grid_axis": "wrong_layer"}
    candidate = alias.get(str(category))
    return candidate if candidate in strat.hypotheses else None


def plans_from_proposal(case: dict, proposal: dict, *, index: dict, ledger=None,
                        budget=None, source: str = "ai") -> list:
    """模型提议 -> 可编译计划（每个假设一条，全部走同一个编译器）。"""
    out: list = []
    region = case.get("region")
    allowed = [region] if region else None
    for hyp in (proposal or {}).get("hypotheses") or ():
        strategy_id = hyp.get("strategy_id")
        kinds = _kinds_for(str(strategy_id or ""))
        targets = _targets_for(index, case.get("target_handles")
                               or case.get("source_handles") or (), kinds=kinds)
        if not targets:
            targets = [t for t in (case.get("target_handles") or ())
                       if str(t) in (index.get("items") or {})]
        out.append(COMPILER.compile_plan(
            {"case_id": case.get("id"), "decision": "TRY",
             "strategy_id": strategy_id,
             "hypothesis": _hypothesis_for(strategy_id, hyp.get("category")),
             "target_ids": targets,
             "operators": hyp.get("operators") or (),
             "search_space": hyp.get("search_space") or {},
             "priority": "normal", "region": region},
            index=index, ledger=ledger, budget=budget,
            allowed_regions=allowed, source=source))
    return out


def enhance(*, state: dict, index: dict, patterns, ledger=None, columns=(),
            partitions=(), thick=None, labels=(), repairs=(), hooks=None,
            gateway=None, ai=None, memory_dir: str = "",
            diagnostics_path: str | None = None, budget=None,
            plans_by_case: dict | None = None) -> dict:
    """AI 增强主流程：发现 -> 假设 -> 编译 -> 多试验 -> 两级验收 -> 提交/回滚。

    ``ai`` 是 :mod:`ai_config` 的配置（``mode`` 不是 ``enhance`` 时本函数不该被调用）。
    ``gateway`` 为空或不可用时，退化成"确定性计划 + 参数网格"，一条模型都不发。
    ``plans_by_case`` 是上游 ``ai_hypotheses`` 已经编译好的计划——给了就不重复
    问模型（一次增强只花一次模型调用，审计里也看得清是谁产出的）。
    """
    plans_by_case = dict(plans_by_case or {})
    from . import ai_planner as PLANNER
    from . import experiment_runner as RUNNER
    from . import rule_memory as MEMORY

    budget = dict(DEFAULT_BUDGET, **(budget or {}))
    budget.update({k: v for k, v in ((ai or {}).get("budget") or {}).items()
                   if k in RUNNER.BUDGET or k in DEFAULT_BUDGET})
    safety = dict((ai or {}).get("safety") or {})
    memory_cfg = dict((ai or {}).get("memory") or {})
    memory_dir = memory_dir or str(memory_cfg.get("directory") or "")
    hooks = dict(hooks or {})
    started = time.perf_counter()
    work = RC.freeze(state)
    applied: list[dict] = []
    rejected: list[dict] = []
    history: list[dict] = []
    proposals: dict = {}
    memory_hits: list[dict] = []
    trials = 0

    def _trial(base: dict):
        def run_trial(plan, cfg):
            action = _actions_for(plan, case=current["case"], index=index,
                                  repairs=repairs, labels=labels)
            pre, _staged = _semantic_prefix(plan, hooks)
            start = _apply_pre(base, pre) if pre else base
            sub = RC.execute_local_subdag(plan, state=start, index=index,
                                          columns=columns, partitions=partitions,
                                          thick=thick, labels=labels,
                                          actions_by_op=action, hooks=hooks,
                                          config=cfg)
            sub["patch"] = _diff(base, sub.get("state") or base,
                                 region=getattr(plan, "region", None))
            return sub
        return run_trial

    def _validate(base: dict):
        def validate(before, after, plan, params):
            support = _support_handles(index, getattr(plan, "region", None))
            indep = SOLVER.independent_support(before=before, after=after,
                                               support=support)
            verdict = SOLVER.shadow_evaluate(
                before=before, after=after, support=support, plan=plan,
                region=getattr(plan, "region", None),
                independent=int(indep["independent"]),
                global_check=bool(safety.get("global_consistency_check", True)),
                require_independent=bool(
                    safety.get("require_independent_support_for_confirmed", True)))
            verdict["independent_support"] = indep
            verdict["confirmation"] = SOLVER.confirm_level(
                after=after, independent=int(indep["independent"]),
                require_independent=bool(
                    safety.get("require_independent_support_for_confirmed", True)))
            return verdict
        return validate

    current: dict = {}
    ordered = sorted([c for c in (patterns or ()) if isinstance(c, dict)],
                     key=lambda c: (-float(c.get("priority") or 0.0),
                                    str(c.get("id") or "")))
    for case in ordered[:int(budget.get("max_cases") or DEFAULT_BUDGET["max_cases"])]:
        current["case"] = case
        if trials >= int((ai or {}).get("budget", {}).get(
                "max_total_trials", RUNNER.BUDGET["max_total_trials"])):
            break
        max_ms = float(budget.get("max_ms") or DEFAULT_BUDGET["max_ms"])
        if (time.perf_counter() - started) * 1000.0 > max_ms:
            rejected.append({"case_id": case.get("id"), "reasons": ["OUT_OF_BUDGET"]})
            break

        # 1) 经验召回（只产生提议，不跳过验收）
        if memory_dir:
            key = MEMORY.template_key(
                drawing_unit_context=(case.get("basis") or {}).get("units", ""),
                geometry_fingerprint=((case.get("features") or {})
                                      .get("block_family_hash") or
                                      (case.get("trace") or {}).get("why", [""])[-1]),
                block_family=(case.get("features") or {}).get("block_family_hash"))
            hits = MEMORY.recall(directory=memory_dir, key=key)
            if hits:
                memory_hits.extend(hits)
                proposals.setdefault(case.get("id"), {"hypotheses": []})
                for hit in hits:
                    proposals[case["id"]]["hypotheses"].append(
                        {"category": (case.get("features") or {}).get("category")
                         or "column",
                         "strategy_id": hit.get("strategy_id") or "repair_semantics",
                         "operators": [], "search_space": {},
                         "reason": "记忆库建议（" + str(hit.get("tier")) + "）"})

        # 2) 模型假设：上游已编译好的直接用，否则现问一次
        plans = list(plans_by_case.get(case.get("id")) or [])
        proposal = (plans_by_case.get("proposals") or {}).get(case.get("id")) or {}
        if plans:
            proposals[case.get("id")] = proposal or {"hypotheses": [], "status": "precompiled"}
        else:
            proposal = PLANNER.draft(case, gateway=gateway, budget=budget,
                                     tools=_readonly_tools(index, work))
            proposals[case.get("id")] = proposal
            plans = plans_from_proposal(case, proposal, index=index, ledger=ledger,
                                        budget=budget)
        # 3) 确定性兜底：模型不可用/无假设时，按类别选策略
        if not any(p.ok and not p.empty for p in plans):
            category = _category_of(case)
            hint = PLANNER.STRATEGY_HINT.get(str(case.get("kind") or "")) \
                or PLANNER.STRATEGY_HINT.get(category)
            fallback = {"hypotheses": [{"category": category,
                                        "strategy_id": hint or "repair_semantics",
                                        "operators": [], "search_space": {}}]}
            plans += plans_from_proposal(case, fallback, index=index, ledger=ledger,
                                         budget=budget, source="deterministic")
        if not any(getattr(p, "ok", False) and not getattr(p, "empty", True)
                   for p in plans):
            rejected.append({"case_id": case.get("id"),
                             "reasons": ["NO_RUNNABLE_PLAN"],
                             "plan_errors": [e for p in plans for e in
                                             (getattr(p, "errors", []) or ())][:6],
                             "trials": 0})
            continue
        rounds = int((ai or {}).get("budget", {}).get(
            "max_model_rounds_per_case", RUNNER.BUDGET["max_model_rounds_per_case"]))
        attempt = 0
        while True:
            res = RUNNER.run_case(case, baseline_state=work, plans=plans,
                                  trial_fn=_trial(work), validate_fn=_validate(work),
                                  budget=dict(RUNNER.BUDGET, **dict(
                                      (ai or {}).get("budget") or {})),
                                  base_state_hash=_state_fingerprint(work),
                                  start_index=trials, history=history)
            trials += int(res.get("trials") or 0)
            if res.get("accepted"):
                verdict = res.get("verdict") or {}
                work = res["state"]
                applied.append({"case_id": case.get("id"),
                                "strategy": (res.get("record") or {}).get("operators"),
                                "plan_hash": getattr(res.get("plan"), "plan_hash", ""),
                                "experiment_id": (res.get("record") or {}).get(
                                    "experiment_id"),
                                "params": dict(res.get("params") or {}),
                                "verdict": verdict.get("verdict"),
                                "confirmation": verdict.get("confirmation"),
                                "source": "ai" if gateway is not None else "deterministic",
                                "trials": res.get("trials")})
                if memory_dir:
                    MEMORY.remember(
                        {"key": MEMORY.template_key(
                            geometry_fingerprint=((case.get("features") or {})
                                                  .get("block_family_hash") or ""),
                            block_family=(case.get("features") or {}).get(
                                "block_family_hash")),
                         "tier": "candidate", "drawing": str(case.get("source_digest")),
                         "region": case.get("region"),
                         "category": _category_of(case),
                         "strategy_id": (res.get("record") or {}).get("operators"),
                         "parameters": dict(res.get("params") or {}),
                         "evidence": {"independent": (verdict.get("independent_support")
                                                      or {}).get("independent", 0)}},
                        directory=memory_dir)
                break
            attempt += 1
            failures = RUNNER.failure_digest(history)
            if attempt > rounds or gateway is None or not getattr(gateway, "available", False):
                rejected.append({"case_id": case.get("id"),
                                 "reasons": res.get("reasons") or ["NO_VALID_CANDIDATE"],
                                 "failure_digest": failures,
                                 "trials": res.get("trials")})
                break
            revised = PLANNER.revise(proposal if isinstance(proposal, dict) else {},
                                     case=case, failures=failures,
                                     gateway=gateway, budget=budget)
            proposals[case.get("id") + ":revise"] = revised
            new_plans = plans_from_proposal(case, revised, index=index, ledger=ledger,
                                            budget=budget, source="ai_revise")
            if not any(p.ok and not p.empty for p in new_plans):
                rejected.append({"case_id": case.get("id"),
                                 "reasons": (res.get("reasons") or [])
                                 + ["NO_RUNNABLE_PLAN_AFTER_REVISE"],
                                 "failure_digest": failures,
                                 "trials": res.get("trials")})
                break
            plans = new_plans
    if diagnostics_path:
        RUNNER.append_jsonl(diagnostics_path, history)

    stats = {"cases": len(ordered), "applied": len(applied),
             "rejected": len(rejected), "trials": trials,
             "memory_hits": len(memory_hits),
             "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1)}
    return {"schema": AI_SCHEMA, "state": work, "applied": applied,
            "rejected": rejected, "history": history,
            "proposals": {k: PLANNER.summary(v) for k, v in proposals.items()},
            "memory": MEMORY.summary(memory_dir) if memory_dir else {},
            "stats": stats,
            "failure_digest": RUNNER.failure_digest(history),
            "status": "applied" if applied else "no_action",
            "patch": _diff(state, work) if applied else {"status": "no_action"}}


def _category_of(case: dict) -> str:
    """案例大致属于哪类构件（只用来挑兜底策略，不作为识别结论）。"""
    case = case or {}
    kind = str(case.get("kind") or "")
    if kind == "line_bundle":
        return "window"
    if kind in ("repeated_block", "open_contour"):
        return "column"
    types = {str(t).upper() for t in ((case.get("features") or {}).get("types") or ())}
    if kind == "residual_cluster" and types and types <= {"LINE", "LWPOLYLINE",
                                                          "ARC", "CIRCLE"}:
        return "window"
    return "column"


def _readonly_tools(index: dict, state: dict) -> dict:
    """只读工具：模型可以点名要事实，但事实由程序算，模型只读不写。"""
    def inspect_pattern(case):
        return {"case_id": case.get("id"), "bbox": case.get("bbox"),
                "handles": len(case.get("source_handles") or ()),
                "instances": len(case.get("instances") or ()),
                "features": case.get("features") or {}}

    def inspect_neighbors(case):
        region = case.get("region")
        near = []
        for rec in (state.get("walls") or ()):
            if not isinstance(rec, dict):
                continue
            box = {"start": rec.get("start"), "end": rec.get("end")}
            if region and _in_region(rec.get("bbox"), region, pad=0.0):
                near.append({"id": rec.get("id"), "thickness": rec.get("thickness")})
        return {"walls": near[:12], "wall_handles": case.get("wall_handles") or []}

    def summarize_block_family(case):
        return {"family": (case.get("features") or {}).get("block_family_hash"),
                "repetition": (case.get("features") or {}).get("repetition_count"),
                "layers": (case.get("trace") or {}).get("layers") or []}

    def inspect_failed_trial(case):
        return {"case_id": case.get("id"),
                "hint": case.get("baseline") or {}}

    def render_crop(case):
        return {"image_ref": case.get("image_ref"), "available": bool(case.get("image_ref"))}

    return {"inspect_pattern": inspect_pattern, "inspect_neighbors": inspect_neighbors,
            "summarize_block_family": summarize_block_family,
            "inspect_failed_trial": inspect_failed_trial, "render_crop": render_crop}


def ai_audit(result: dict) -> dict:
    """AI 通道的审计摘要（进 final_audit / meta.ai_recognition）。"""
    res = result or {}
    return {"schema": AI_SCHEMA, "status": res.get("status"),
            "stats": res.get("stats") or {},
            "applied": res.get("applied") or [],
            "rejected": [{k: r.get(k) for k in ("case_id", "reasons", "trials")}
                         for r in (res.get("rejected") or ())][:12],
            "failure_digest": res.get("failure_digest") or [],
            "proposals": res.get("proposals") or {},
            "memory": res.get("memory") or {}}
