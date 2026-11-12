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
``REGION_NOT_ALLOWED`` ``SOLVER_ONLY`` ``EMPTY_PLAN`` ``UNKNOWN_PARAM``
``PARAM_NOT_AUTHORIZED`` ``PARAM_RANGE_INVALID`` ``PARAM_UNCONSUMED``
``ROLE_NOT_ALLOWED`` ``DUPLICATE_PLAN``
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import operator_specs as OPSPEC
from . import param_registry as PARAMS

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
    #: 参数方案：``param_values`` 是这一版试验用的具体值，
    #: ``param_ranges`` 是模型给的授权区间（供实验器生成更多实验点）
    param_values: dict = field(default_factory=dict)
    param_ranges: dict = field(default_factory=dict)
    #: 编译器自己校验过的参数（不需要算子再读一遍，但仍算"已消费"）
    param_consumed: tuple = ()
    plan_hash: str = ""
    case_kind: str = ""
    evidence_ids: tuple = ()

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
                "budget": dict(self.budget),
                "param_values": dict(self.param_values),
                "param_ranges": {k: dict(v) for k, v in self.param_ranges.items()},
                "param_consumed": list(self.param_consumed),
                "plan_hash": self.plan_hash, "case_kind": self.case_kind,
                "evidence_ids": list(self.evidence_ids)}


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
    #    模型可以**指定算子子集**（``operators``），但顺序只当偏好：真正的执行
    #    顺序由 ``STAGE_DEPS`` 的拓扑深度决定，模型不能改依赖图。
    steps: list[dict] = []
    produced: set[str] = set()
    sequence = _operator_sequence(proposal.get("operators"), strategy=strategy,
                                  errors=plan.errors, warnings=plan.warnings)
    if plan.errors:
        return plan
    for name in sequence:
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
            "params": list(spec.params),
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

    # 6) 参数：区间求交 -> 默认点；必须被本计划的算子真正读取
    values, ranges, consumed, perrors, pwarns = compile_params(
        proposal.get("search_space") or {}, steps=steps)
    plan.param_values = values
    plan.param_ranges = ranges
    plan.param_consumed = tuple(consumed)
    plan.errors.extend(perrors)
    for w in pwarns:
        plan.warnings.append(f"PARAM_DROPPED:{w.get('param')}")

    # 7) 区域图层角色：只能写在授权闭集里（且显式给了 region_layer_roles 时更严）
    if plan.roles:
        allowed_roles = set(PARAMS.spec("region_layer_roles").allowed)
        if "region_layer_roles" in values:
            allowed_roles = {str(values["region_layer_roles"])}
            if "region_layer_roles" not in consumed:
                consumed.add("region_layer_roles")
                plan.param_consumed = tuple(consumed)
        bad_roles = sorted({str(r) for seq in plan.roles for r in seq
                            if str(r) not in allowed_roles})
        if bad_roles:
            plan.errors.append(_error("ROLE_NOT_ALLOWED", roles=bad_roles,
                                      allowed=sorted(allowed_roles)))

    plan.plan_hash = _hash_of(plan)
    seen = budget.get("seen_plans") or ()
    if plan.plan_hash and plan.plan_hash in set(seen):
        plan.errors.append(_error("DUPLICATE_PLAN", plan_hash=plan.plan_hash))
        return plan

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
    plan.param_values = dict(data.get("param_values") or {})
    plan.param_ranges = {str(k): dict(v) for k, v in
                         (data.get("param_ranges") or {}).items()}
    plan.param_consumed = tuple(data.get("param_consumed") or ())
    plan.plan_hash = str(data.get("plan_hash") or "")
    plan.case_kind = str(data.get("case_kind") or "")
    plan.evidence_ids = tuple(data.get("evidence_ids") or ())
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


def _operator_sequence(declared, *, strategy, errors, warnings) -> tuple[str, ...]:
    """模型给的算子子集 -> 按阶段依赖排序的执行序列。

    * 未注册 / 不可委托的算子直接报错（``UNKNOWN_OPERATOR`` / ``SOLVER_ONLY``）；
    * 不在该策略步骤里的算子只记一条警告（允许探索，但审计里看得见）；
    * 空列表或没给就退回策略自己的步骤序列。
    """
    names: list[str] = []
    for op in (declared or ()):
        name = op.get("name") if isinstance(op, dict) else str(op)
        spec = OPSPEC.REGISTRY.get(str(name))
        if spec is None:
            errors.append(_error("UNKNOWN_OPERATOR", operator=str(name)))
            continue
        if not spec.executable_by_llm:
            errors.append(_error("SOLVER_ONLY", operator=str(name)))
            continue
        if name not in names:
            names.append(str(name))
    if not names:
        return OPSPEC.steps_of(strategy.name)
    outside = [n for n in names if n not in OPSPEC.steps_of(strategy.name)]
    if outside:
        warnings.append("OPERATORS_OUTSIDE_STRATEGY:" + ",".join(outside))
    # 注意：key 里不能再读正在被排序的那个列表（CPython 排序期间列表是空的），
    # 所以先把"谁给谁供料"算成固定的键，再排序。
    snapshot = tuple(names)
    order = {n: i for i, n in enumerate(snapshot)}
    keys: dict = {}
    for name in snapshot:
        spec = OPSPEC.REGISTRY[name]
        feed = set()
        for other in snapshot:
            if other != name:
                feed |= set(OPSPEC.REGISTRY[other].output_kinds)
        # 输入要靠别人产出的算子排后面（同阶段内的数据流顺序）
        needs = 1 if (set(spec.input_kinds) & feed) else 0
        keys[name] = (len(_stage_ancestors(OPSPEC.stage_of(name))), needs, order[name])
    names.sort(key=lambda n: keys[n])
    if len(names) > strategy.max_steps:
        errors.append(_error("TOO_MANY_STEPS", count=len(names),
                             limit=strategy.max_steps))
    return tuple(names)


def compile_params(space: dict, *, steps) -> tuple[dict, dict, set, list, list]:
    """把模型的搜索空间编译成"本版试验的具体值 + 授权区间"。

    规则（每条都能拒绝）：

    * 参数名必须在 :mod:`param_registry` 里（``UNKNOWN_PARAM``）；
    * 区间与绝对授权区间求交，空集即 ``PARAM_RANGE_INVALID``；
    * 枚举/布尔必须给值，且落在闭集内；
    * **参数必须被本计划的算子真正读取**，否则 ``PARAM_UNCONSUMED``——宁可报错，
      也不让一个没人读的参数混进试验记录冒充"调过了"。
    """
    read_by_plan: set = set()
    for step in (steps or ()):
        read_by_plan |= {str(n) for n in (step.get("params") or ())}
    values: dict = {}
    ranges: dict = {}
    consumed: set = set()
    errors: list[dict] = []
    warnings: list[dict] = []
    for name, rng in (space or {}).items():
        try:
            spec = PARAMS.spec(str(name))
        except PARAMS.ParameterError:
            errors.append(_error("UNKNOWN_PARAM", param=str(name)))
            continue
        if not isinstance(rng, dict):
            errors.append(_error("PARAM_RANGE_INVALID", param=spec.name))
            continue
        if spec.value_type in ("float", "int"):
            try:
                lo = float(rng.get("min", spec.bounds()[0]))
                hi = float(rng.get("max", spec.bounds()[1]))
                lo, hi = spec.within(min(lo, hi), max(lo, hi))
            except (TypeError, ValueError):
                errors.append(_error("PARAM_RANGE_INVALID", param=spec.name))
                continue
            except PARAMS.ParameterError as exc:
                errors.append(_error("PARAM_NOT_AUTHORIZED", param=spec.name,
                                     detail=exc.detail))
                continue
            ranges[spec.name] = {"type": spec.value_type, "min": lo, "max": hi}
            point = spec.default if lo <= float(spec.default) <= hi else (lo + hi) / 2.0
            values[spec.name] = spec.check(point)
        else:
            value = rng.get("value", rng.get("default", spec.default))
            try:
                values[spec.name] = spec.check(value)
            except PARAMS.ParameterError as exc:
                errors.append(_error("PARAM_RANGE_INVALID", param=spec.name,
                                     detail=exc.detail))
                continue
        if spec.name not in read_by_plan:
            # 模型猜了一个"这套算子根本不读"的参数：这不是安全问题，是没读懂目录。
            # 明确记一条警告并把该参数从试验里拿掉——试验照跑（用默认值），
            # 但审计里看得见"这个参数没被用"，不会有"假装调过"的记录。
            warnings.append(_error("PARAM_UNCONSUMED", param=spec.name,
                                   readers=sorted(read_by_plan)))
            values.pop(spec.name, None)
            ranges.pop(spec.name, None)
        else:
            consumed.add(spec.name)
    return values, ranges, consumed, errors, warnings


def _hash_of(plan) -> str:
    """计划指纹：策略 + 算子 + 目标 + 参数值，任一变化就是另一份计划。"""
    import hashlib
    import json as _json
    body = {"case": plan.case_id, "strategy": plan.strategy_id,
            "hypothesis": plan.hypothesis,
            "targets": list(plan.target_ids),
            "steps": [s.get("operator") for s in plan.steps],
            "params": dict(plan.param_values)}
    raw = _json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


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
            "affects": sorted({n for p in ok for n in p.affects()}),
            "params": sorted({name for p in ok for name in p.param_values}),
            "param_values": {p.plan_hash: dict(p.param_values)
                             for p in ok if p.param_values}}
