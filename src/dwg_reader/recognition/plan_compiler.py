"""计划编译器：把"LLM 选的策略 + 证据 id"编译成一段**有依赖的局部子 DAG**。

分工是硬边界：

* LLM/确定性规则只提供 ``decision / strategy_id / target_ids / hypothesis``；
* 本模块负责把 ``strategy_id`` 展开成算子序列、按阶段推导依赖、检查权限与
  类型、检查预算与区域授权；
* 编译器**不执行任何几何运算**，执行在 :func:`recompute.execute_local_subdag`。

任何一步不通过就返回 ``ok=False`` 和结构化错误码，绝不"尽力而为"地跑一半。
错误码（闭集）：

``NO_DECISION`` ``UNKNOWN_DECISION`` ``UNKNOWN_STRATEGY`` ``UNKNOWN_OPERATOR``
``TARGET_NOT_FOUND`` ``TARGET_KIND_MISMATCH`` ``TOO_MANY_TARGETS``
``HYPOTHESIS_NOT_ALLOWED`` ``TOO_MANY_STEPS`` ``OUT_OF_BUDGET``
``REGION_NOT_ALLOWED`` ``SOLVER_ONLY`` ``EMPTY_PLAN``
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import operator_specs as OPSPEC

MAX_PLANS_PER_CASE = 3
MAX_HYPOTHESIS_LEN = 40

#: 旧版"单动作"提议 -> 策略（保持向后兼容，但同样走编译与执行）
ACTION_STRATEGY = {
    "regenerate_wall": "recover_wall_network",
    "pair_parallel": "recover_wall_network",
    "merge_collinear": "recover_wall_network",
    "retry_wall_pairing": "recover_wall_network",
    "single_line_wall": "recover_wall_network",
    "rebuild_wall_graph": "recover_wall_network",
    "regenerate_opening": "recover_openings",
    "group_line_bundle": "recover_openings",
    "extract_arc": "recover_openings",
    "reconcile_openings": "recover_openings",
    "extract_contour": "recover_column_geometry",
    "regenerate_column": "recover_column_geometry",
    "rebuild_contour": "recover_column_geometry",
    "rebuild_space_faces": "recover_space_boundary",
    "try_virtual_boundary": "recover_space_boundary",
    "associate_room_labels": "recover_space_boundary",
    "propose_layer_role": "repair_semantics",
    "override_layer_role": "repair_semantics",
    "ignore_layer": "repair_semantics",
    "resolve_conflicts": "recover_openings",
    "reject_candidate": "recover_wall_network",
    "treat_as_fill": "recover_column_geometry",
    "treat_as_opening": "recover_openings",
    "merge_duplicate": "recover_column_geometry",
    "keep_thicker": "recover_wall_network",
}


@dataclass
class Plan:
    """一段已编译、可执行的局部计划（不改几何，只描述怎么算）。"""

    case_id: str
    decision: str = "NO_ACTION"
    strategy_id: str | None = None
    hypothesis: str | None = None
    priority: str = "normal"
    target_ids: tuple[str, ...] = ()
    support_ids: tuple[str, ...] = ()
    roles: list = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source: str = "deterministic"
    region: list | None = None
    budget: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def empty(self) -> bool:
        return self.ok and not self.steps

    def stages(self) -> tuple[str, ...]:
        out: list[str] = []
        for step in self.steps:
            if step["stage"] not in out:
                out.append(step["stage"])
        return tuple(out)

    def affects(self) -> tuple[str, ...]:
        nodes: list[str] = []
        for step in self.steps:
            for node in step.get("affects") or ():
                if node not in nodes:
                    nodes.append(node)
        return tuple(nodes)

    def as_dict(self) -> dict:
        return {"case_id": self.case_id, "decision": self.decision,
                "strategy_id": self.strategy_id, "hypothesis": self.hypothesis,
                "priority": self.priority, "target_ids": list(self.target_ids),
                "support_ids": list(self.support_ids), "roles": [list(r) for r in self.roles],
                "source": self.source,
                "region": self.region, "ok": self.ok,
                "steps": [dict(s) for s in self.steps],
                "errors": [dict(e) for e in self.errors],
                "warnings": list(self.warnings),
                "affects": list(self.affects()),
                "budget": dict(self.budget)}


# ------------------------------------------------------------------ 目标归一化
_PREFIX_ALIAS = {"pattern": "pat", "entity": "ev", "line_support": "pat",
                 "handle": "ev"}


def resolve_target(target, *, index: dict, ledger: dict | None = None) -> str | None:
    """把 LLM 写的 id 归一成证据索引里真实存在的 id；解析不到返回 ``None``。

    允许 ``W102``（裸 handle）、``ev:W102``、``pat:3``、``wall:7``、``layer:WALL``。
    """
    if target is None:
        return None
    t = str(target).strip()
    if not t:
        return None
    items = index.get("items") or {}
    if t in items:
        return t
    if ":" in t:
        head, _, tail = t.partition(":")
        alias = _PREFIX_ALIAS.get(head)
        if alias and f"{alias}:{tail}" in items:
            return f"{alias}:{tail}"
        cand = f"{head}:{tail}"
        if cand in items:
            return cand
    if f"ev:{t}" in items:
        return f"ev:{t}"
    if ledger and str(t) in (ledger or {}):
        return f"ev:{t}"
    return None


def _kind_of(index: dict, item_id: str) -> str:
    return str(((index.get("items") or {}).get(item_id) or {}).get("kind") or "")


def _error(code: str, **detail) -> dict:
    return {"code": code, **detail}


# ------------------------------------------------------------------ 编译
def compile_plan(proposal: dict, *, index: dict, ledger: dict | None = None,
                 budget: dict | None = None, allowed_regions=None,
                 source: str = "llm") -> Plan:
    """编译一条策略提议（LLM 或确定性规则产出，形状一致）。"""
    proposal = dict(proposal or {})
    budget = dict(budget or {})
    case_id = str(proposal.get("case_id") or "")
    decision = str(proposal.get("decision") or "").upper()
    plan = Plan(case_id=case_id, decision=decision or "NO_ACTION",
                strategy_id=proposal.get("strategy_id"),
                hypothesis=proposal.get("hypothesis"),
                priority=str(proposal.get("priority") or "normal"),
                region=proposal.get("region"), source=source, budget=budget)

    if decision not in ("TRY", "NO_ACTION"):
        plan.errors.append(_error("UNKNOWN_DECISION" if decision else "NO_DECISION",
                                 decision=decision))
        return plan
    if decision == "NO_ACTION":
        return plan                                   # 合法：明确"什么都不做"

    strategy = OPSPEC.strategy(plan.strategy_id)
    if strategy is None:
        plan.errors.append(_error("UNKNOWN_STRATEGY",
                                 strategy_id=plan.strategy_id))
        return plan
    plan.budget = dict(strategy.budget, **budget)

    # 1) 目标必须真实存在（LLM 只能指向证据库预先生成的 id）
    targets: list[str] = []
    for raw in proposal.get("target_ids") or ():
        rid = resolve_target(raw, index=index, ledger=ledger)
        if rid is None:
            plan.errors.append(_error("TARGET_NOT_FOUND", target=str(raw)))
        elif rid not in targets:
            targets.append(rid)
    plan.target_ids = tuple(targets)
    roles = proposal.get("roles")
    if isinstance(roles, list):
        plan.roles = [list(r) if isinstance(r, (list, tuple)) else [str(r)]
                      for r in roles][:len(targets)]
    if plan.errors:
        return plan
    if not targets:
        plan.errors.append(_error("EMPTY_PLAN", why="no_targets"))
        return plan
    first = next((OPSPEC.REGISTRY[s] for s in OPSPEC.steps_of(strategy.name)
                  if s in OPSPEC.REGISTRY), None)
    limit = min(int(strategy.max_targets),
                int(first.max_targets) if first else strategy.max_targets)
    if len(targets) > limit:                      # 预算内截断（确定性取前 N 个）
        plan.target_ids = tuple(targets[:limit])
        targets = list(plan.target_ids)
        plan.warnings.append(f"TARGETS_TRUNCATED:{limit}")

    # 2) 假设必须在该策略允许的闭集内
    hyp = plan.hypothesis
    if hyp not in (None, "") and hyp not in strategy.hypotheses:
        plan.errors.append(_error("HYPOTHESIS_NOT_ALLOWED", hypothesis=hyp,
                                 allowed=list(strategy.hypotheses)))
        return plan
    if isinstance(hyp, str) and len(hyp) > MAX_HYPOTHESIS_LEN:
        plan.errors.append(_error("HYPOTHESIS_NOT_ALLOWED", hypothesis=hyp))
        return plan

    # 3) 支撑证据只做归一，不要求全部命中（允许模型引用未入库的 handle）
    kept_support: list[str] = []
    dropped = 0
    for raw in proposal.get("supporting_evidence_ids") or ():
        rid = resolve_target(raw, index=index, ledger=ledger)
        if rid is None:
            dropped += 1
        elif rid not in kept_support:
            kept_support.append(rid)
    plan.support_ids = tuple(kept_support)
    if dropped:
        plan.warnings.append(f"SUPPORT_DROPPED:{dropped}")

    # 4) 区域授权（只允许在当前识别认为可修的区域内动手）
    region = plan.region
    if region and allowed_regions:
        if not any(_region_within(region, r) for r in allowed_regions):
            plan.errors.append(_error("REGION_NOT_ALLOWED", region=list(region)))
            return plan

    # 5) 展开成算子序列 + 推导步骤依赖
    steps: list[dict] = []
    produced: set[str] = set()
    for name in OPSPEC.steps_of(strategy.name):
        spec = OPSPEC.REGISTRY.get(name)
        if spec is None:
            plan.errors.append(_error("UNKNOWN_OPERATOR", operator=name))
            continue
        # solver_only 的步骤允许出现在策略里（由求解器执行），但 LLM 不能自己选它；
        # "LLM 直接选 solver 算子"由 proposal_validator 拦（OPERATOR_NOT_DELEGABLE）。

        if len(targets) > spec.max_targets:
            plan.errors.append(_error("TOO_MANY_TARGETS", operator=name,
                                     count=len(targets), limit=spec.max_targets))
        check_kind = not (produced & set(spec.input_kinds))
        if check_kind:
            usable = [t for t in targets if _kind_of(index, t) in spec.input_kinds]
            if not usable:
                plan.errors.append(_error("TARGET_KIND_MISMATCH", operator=name,
                                         target=targets[0] if targets else "",
                                         kind=_kind_of(index, targets[0]) if targets else ""))
                break
            if len(usable) < len(targets):
                plan.warnings.append(f"TARGETS_FILTERED:{name}:{len(usable)}")
                plan.target_ids = tuple(usable)
                targets = list(usable)
        produced.update(spec.output_kinds)
        steps.append({
            "index": len(steps), "operator": name, "version": spec.version,
            "stage": spec.stage, "kind": spec.kind,
            "affects": list(spec.affects),
            "decision_only": bool(spec.decision_only),
            "max_targets": spec.max_targets,
            "deps": [],
        })
    if plan.errors:
        return plan
    if not steps:
        plan.errors.append(_error("EMPTY_PLAN", why="no_steps"))
        return plan
    if len(steps) > strategy.max_steps:
        plan.errors.append(_error("TOO_MANY_STEPS", count=len(steps),
                                 limit=strategy.max_steps))
        return plan

    # 依赖 = 同一策略内、处于上游阶段的所有步骤（阶段图来自 operator_specs）
    for step in steps:
        ancestors = _stage_ancestors(step["stage"])
        step["deps"] = [s["index"] for s in steps
                        if s["index"] != step["index"] and s["stage"] in ancestors]
    plan.steps = steps

    max_runs = int(budget.get("subdag_runs", strategy.budget.get("subdag_runs", 1)))
    if max_runs < 1:
        plan.errors.append(_error("OUT_OF_BUDGET", why="subdag_runs"))
    return plan


def plan_from_dict(data: dict) -> Plan:
    """把编译结果的 JSON 形态还原成可执行 Plan（不再重新编译）。"""
    plan = Plan(case_id=str(data.get("case_id") or ""),
                decision=str(data.get("decision") or "NO_ACTION"),
                strategy_id=data.get("strategy_id"),
                hypothesis=data.get("hypothesis"),
                priority=str(data.get("priority") or "normal"),
                target_ids=tuple(data.get("target_ids") or ()),
                support_ids=tuple(data.get("support_ids") or ()),
                errors=[dict(e) for e in (data.get("errors") or ())],
                warnings=list(data.get("warnings") or ()),
                source=str(data.get("source") or "deterministic"),
                region=data.get("region"), budget=dict(data.get("budget") or {}))
    plan.roles = [list(r) if isinstance(r, (list, tuple)) else [str(r)]
                  for r in (data.get("roles") or ())]
    plan.steps = [dict(s) for s in (data.get("steps") or ())]
    return plan


def compile_actions(actions, *, index: dict, case_id: str = "", region=None,
                    ledger: dict | None = None, allowed_regions=None,
                    source: str = "llm") -> list[Plan]:
    """兼容旧版"单动作列表"（``llm_reason`` 的 proposals）：动作 -> 单步计划。

    映射到策略只是为了让"动作"也走同一条编译/执行/验收通道；实际执行时只跑
    该动作本身（``steps`` 里就一步），不会偷偷把整条策略都跑一遍。
    """
    out: list[Plan] = []
    for act in actions or ():
        if not isinstance(act, dict):
            continue
        action = act.get("action") or act.get("operation")
        strategy_id = ACTION_STRATEGY.get(str(action))
        if not action or strategy_id is None:
            out.append(Plan(case_id=case_id or str(act.get("case_id") or ""),
                            decision="TRY", errors=[_error("UNKNOWN_OPERATOR",
                                                           operator=str(action))],
                            source=source))
            continue
        strg = OPSPEC.strategy(strategy_id)
        plan = Plan(case_id=case_id or str(act.get("case_id") or ""),
                    decision="TRY", strategy_id=strategy_id,
                    hypothesis=(act.get("hypothesis") if act.get("hypothesis")
                                in strg.hypotheses else None),
                    target_ids=(), source=source, region=region,
                    budget=dict(strg.budget))
        targets: list[str] = []
        for raw in act.get("target_ids") or ():
            rid = resolve_target(raw, index=index, ledger=ledger)
            if rid is None:
                plan.errors.append(_error("TARGET_NOT_FOUND", target=str(raw)))
            elif rid not in targets:
                targets.append(rid)
        plan.target_ids = tuple(targets)
        spec = OPSPEC.REGISTRY.get(str(action))
        if spec is None:
            plan.errors.append(_error("UNKNOWN_OPERATOR", operator=str(action)))
        elif not targets:
            plan.errors.append(_error("EMPTY_PLAN", why="no_targets"))
        else:
            if len(targets) > spec.max_targets:
                plan.errors.append(_error("TOO_MANY_TARGETS", count=len(targets),
                                          limit=spec.max_targets))
            bad = [t for t in targets if _kind_of(index, t) not in spec.input_kinds]
            if bad:
                plan.errors.append(_error("TARGET_KIND_MISMATCH", operator=str(action),
                                          target=bad[0], kind=_kind_of(index, bad[0])))
            plan.steps = [{"index": 0, "operator": str(action),
                           "version": spec.version, "stage": spec.stage,
                           "kind": spec.kind, "affects": list(spec.affects),
                           "decision_only": bool(spec.decision_only),
                           "max_targets": spec.max_targets, "deps": []}]
        if region and allowed_regions and not any(
                _region_within(region, r) for r in allowed_regions):
            plan.errors.append(_error("REGION_NOT_ALLOWED", region=list(region)))
        out.append(plan)
    return out[:MAX_PLANS_PER_CASE * max(1, len(actions or ()))]


# ------------------------------------------------------------------ 工具
def _stage_ancestors(stage: str) -> set[str]:
    out: set[str] = set()
    stack = [stage]
    while stack:
        cur = stack.pop()
        for dep in OPSPEC.STAGE_DEPS.get(cur, ()):
            if dep not in out:
                out.add(dep)
                stack.append(dep)
    return out


def _region_within(inner, outer, pad: float = 0.0) -> bool:
    try:
        return (inner[0] >= outer[0] - pad and inner[1] >= outer[1] - pad
                and inner[2] <= outer[2] + pad and inner[3] <= outer[3] + pad)
    except (TypeError, IndexError):
        return False


def summary(plans) -> dict:
    """计划集摘要（进审计）。"""
    plans = list(plans or ())
    ok = [p for p in plans if p.ok and not p.empty]
    return {"schema": "repair-plans/1.0", "plans": len(plans), "runnable": len(ok),
            "rejected": sum(1 for p in plans if not p.ok),
            "no_action": sum(1 for p in plans if p.ok and p.empty),
            "strategies": sorted({p.strategy_id for p in ok if p.strategy_id}),
            "errors": [e for p in plans for e in p.errors][:12],
            "affects": sorted({n for p in ok for n in p.affects()})}
