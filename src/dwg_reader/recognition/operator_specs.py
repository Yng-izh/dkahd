"""封闭算子库 + 修复策略库（版本化 + 权限 + 预算 + 阶段依赖）。

LLM 只能在这里选**策略**或算子、指定已有证据 id，**不能带几何参数、不能注入代码**。

三档权限：

* 自动执行：原语提取、证据统计（不在本表调用面内）
* LLM 可建议：几何重组 / 候选再生成 / 局部拓扑重建（``executable_by_llm=True``）
* 仅求解器：候选最终选择、墙体实体更新、面积审计（``solver_only=True``）

每个算子都归属一个**阶段**（``stage``）。阶段之间是固定依赖图（见
``STAGE_DEPS``），因此"LLM 选中一串算子"可以被编译成一段**有依赖顺序的局部
子 DAG**，由 :mod:`recompute` 真正执行，而不是只给出一句"建议重跑"。
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: 局部子 DAG 的阶段（数据依赖顺序由 ``STAGE_DEPS`` 给出）
STAGES = ("columns", "walls", "wall_graph", "space", "openings", "rooms", "validate")

STAGE_DEPS: dict[str, tuple[str, ...]] = {
    "columns": (),
    "walls": (),
    "wall_graph": ("walls", "columns"),
    "space": ("wall_graph",),
    "openings": ("walls", "space"),
    "rooms": ("space", "openings"),
    "validate": ("columns", "walls", "wall_graph", "space", "openings", "rooms"),
}

#: 阶段 -> 外层 DAG 里对应要重算的节点（写进重算审计）
STAGE_NODES: dict[str, tuple[str, ...]] = {
    "columns": ("columns",),
    "walls": ("walls_seg",),
    "wall_graph": ("wall_model", "spaces_seed"),
    "space": ("spaces_seed",),
    "openings": ("openings_select", "doors", "windows"),
    "rooms": ("rooms",),
    "validate": (),
}


@dataclass(frozen=True)
class OperatorSpec:
    name: str
    version: str
    kind: str                                    # geometry / rebuild / topology / semantic / validate
    input_kinds: tuple[str, ...]
    output_kinds: tuple[str, ...]
    stage: str = "walls"                         # 所属局部阶段（见 STAGES）
    affects: tuple[str, ...] = ()                # 命中后需重算的 DAG 节点
    max_targets: int = 4
    executable_by_llm: bool = True
    solver_only: bool = False
    decision_only: bool = False                  # 只记录决策，不改几何
    budget: dict = field(default_factory=dict)   # {"max_entities": 400, "ms": 800}
    #: 这个算子**真正读取**的运行时参数（见 ``param_registry``）。
    #: 编译器据此判定"被编译进来却没人读"的幽灵参数；空元组表示纯确定性算子。
    params: tuple[str, ...] = ()


REGISTRY: dict[str, OperatorSpec] = {}


def register(spec: OperatorSpec) -> OperatorSpec:
    if spec.name in REGISTRY:
        raise ValueError(f"算子重复注册: {spec.name}")
    if spec.stage not in STAGES:
        raise ValueError(f"未知阶段: {spec.stage} ({spec.name})")
    REGISTRY[spec.name] = spec
    return spec


# ------------------------------------------------------------------ 几何重组
register(OperatorSpec(
    "merge_collinear", "1.0", "geometry",
    input_kinds=("line_support", "wall_candidate"), output_kinds=("wall_candidate",),
    stage="walls", affects=("walls_seg", "wall_model"), max_targets=200,
    budget={"max_entities": 400, "ms": 600}))
register(OperatorSpec(
    "pair_parallel", "1.0", "geometry",
    input_kinds=("line_support", "pattern"), output_kinds=("wall_candidate",),
    stage="walls", affects=("walls_seg", "wall_model", "spaces_seed"), max_targets=200,
    budget={"max_entities": 400, "ms": 800}))
register(OperatorSpec(
    "extract_contour", "1.0", "geometry",
    input_kinds=("entity", "hatch", "contour", "line_support", "column_candidate"),
    output_kinds=("contour", "column_candidate"),
    stage="columns", affects=("columns", "walls_seg"), max_targets=200))
register(OperatorSpec(
    "rebuild_contour", "1.0", "geometry",
    input_kinds=("hatch", "contour", "entity"), output_kinds=("contour", "column_candidate"),
    stage="columns", affects=("columns",), max_targets=60))
register(OperatorSpec(
    "group_line_bundle", "1.0", "geometry",
    input_kinds=("line_support", "pattern"), output_kinds=("pattern", "opening_candidate"),
    stage="openings", affects=("windows", "doors", "openings_select"), max_targets=120,
    params=("min_overlap_ratio", "parallel_angle_tol_deg", "line_bundle_min_count")))
register(OperatorSpec(
    "extract_arc", "1.0", "geometry",
    input_kinds=("entity", "arc", "line_support"), output_kinds=("arc",),
    stage="openings", affects=("doors", "grid"), max_targets=200))
register(OperatorSpec(
    "merge_duplicate", "1.0", "geometry",
    input_kinds=("wall_candidate", "column_candidate", "opening_candidate"),
    output_kinds=("wall_candidate", "column_candidate", "opening_candidate"),
    stage="validate", affects=("walls_seg", "columns", "openings_select"),
    max_targets=200))
register(OperatorSpec(
    "keep_thicker", "1.0", "geometry",
    input_kinds=("wall_candidate", "opening_candidate"), output_kinds=("wall_candidate",),
    stage="validate", affects=("walls_seg",), max_targets=60))

# ------------------------------------------------------------------ 候选重建
register(OperatorSpec(
    "retry_wall_pairing", "1.0", "rebuild",
    input_kinds=("line_support", "wall_candidate", "pattern", "entity"),
    output_kinds=("wall_candidate",),
    stage="walls",
    affects=("walls_seg", "wall_model", "spaces_seed", "openings_select",
             "walls_merge", "rooms"),
    max_targets=120, budget={"max_entities": 1600, "ms": 1800},
    params=("parallel_angle_tol_deg", "wall_gap_ratio", "min_overlap_ratio")))
register(OperatorSpec(
    "single_line_wall", "1.0", "rebuild",
    input_kinds=("line_support", "entity", "pattern"), output_kinds=("wall_candidate",),
    stage="walls", affects=("walls_seg", "wall_model", "spaces_seed"),
    max_targets=120))
register(OperatorSpec(
    "regenerate_wall", "1.0", "rebuild",
    input_kinds=("line_support", "wall_candidate", "pattern"),
    output_kinds=("wall_candidate",),
    stage="walls",
    affects=("walls_seg", "wall_model", "spaces_seed", "openings_select",
             "walls_merge", "rooms"),
    max_targets=80, budget={"max_entities": 1200, "ms": 1500}))
register(OperatorSpec(
    "regenerate_opening", "1.0", "rebuild",
    input_kinds=("pattern", "wall_candidate", "opening_candidate", "line_support"),
    output_kinds=("opening_candidate",),
    stage="openings", affects=("doors", "windows", "openings_select", "walls_merge", "rooms"),
    max_targets=40, budget={"max_entities": 600, "ms": 1200},
    params=("min_overlap_ratio", "parallel_angle_tol_deg", "wall_gap_ratio",
            "allow_unanchored_window_candidate")))
register(OperatorSpec(
    "regenerate_column", "1.0", "rebuild",
    input_kinds=("contour", "pattern", "entity", "line_support", "column_candidate"),
    output_kinds=("column_candidate",),
    stage="columns", affects=("columns", "spaces_seed"), max_targets=40,
    params=("column_aspect_ratio",)))
register(OperatorSpec(
    "treat_as_opening", "1.0", "rebuild",
    input_kinds=("wall_candidate", "line_support", "pattern"),
    output_kinds=("opening_candidate",),
    stage="openings", affects=("doors", "windows", "openings_select"), max_targets=8))

# ------------------------------------------------------------------ 拓扑求解
register(OperatorSpec(
    "rebuild_wall_graph", "1.0", "topology",
    input_kinds=("wall_candidate", "line_support", "pattern"),
    output_kinds=("wall_model", "wall_candidate"),
    stage="wall_graph", affects=("wall_model", "spaces_seed", "openings_select"),
    max_targets=200, budget={"ms": 1200}))
register(OperatorSpec(
    "rebuild_space_faces", "1.0", "topology",
    input_kinds=("wall_candidate", "space_face", "room_label"),
    output_kinds=("space_face",),
    stage="space", affects=("spaces_seed", "rooms"), max_targets=200, budget={"ms": 1500}))
register(OperatorSpec(
    "try_virtual_boundary", "1.0", "topology",
    input_kinds=("wall_candidate", "entity", "space_face", "line_support", "pattern"),
    output_kinds=("wall_candidate",),
    stage="walls", affects=("walls_seg", "spaces_seed", "rooms"), max_targets=8,
    budget={"ms": 400}))
register(OperatorSpec(
    "reconcile_openings", "1.0", "topology",
    input_kinds=("opening_candidate", "wall_candidate", "space_face"),
    output_kinds=("opening_candidate",),
    stage="openings", affects=("openings_select", "walls_merge", "rooms"),
    max_targets=120))
register(OperatorSpec(
    "associate_room_labels", "1.0", "topology",
    input_kinds=("room_label", "space_face", "entity"),
    output_kinds=("room_label",),
    stage="rooms", affects=("rooms",), max_targets=120))

# ------------------------------------------------------------------ 语义修复
register(OperatorSpec(
    "propose_layer_role", "1.0", "semantic",
    input_kinds=("layer", "entity", "line_support", "pattern", "wall_candidate",
                 "opening_candidate"),
    output_kinds=("layer_role",),
    stage="columns", affects=("load", "walls_seg", "columns", "doors", "windows"),
    max_targets=6))
register(OperatorSpec(
    "override_layer_role", "1.0", "semantic",
    input_kinds=("layer",), output_kinds=("layer_role", "wall_candidate",
                                          "opening_candidate", "column_candidate"),
    stage="walls", affects=("walls_seg", "columns", "doors", "windows", "spaces_seed"),
    max_targets=6, params=("region_layer_roles",)))
register(OperatorSpec(
    "ignore_layer", "1.0", "semantic",
    input_kinds=("layer",), output_kinds=("layer_role",),
    stage="validate", affects=(), max_targets=6, decision_only=True))
register(OperatorSpec(
    "treat_as_fill", "1.0", "semantic",
    input_kinds=("hatch", "contour", "column_candidate"),
    output_kinds=("audit",), stage="validate", affects=(), max_targets=40,
    decision_only=True))

# ------------------------------------------------------------------ 约束与验证
register(OperatorSpec(
    "resolve_conflicts", "1.0", "topology",
    input_kinds=("opening_candidate", "wall_candidate", "space_face"),
    output_kinds=("opening_candidate",),
    stage="validate", affects=("openings_select", "walls_merge", "rooms"), max_targets=60))
register(OperatorSpec(
    "reject_candidate", "1.0", "validate",
    input_kinds=("column_candidate", "opening_candidate", "wall_candidate",
                 "contour", "hatch"),
    output_kinds=("audit",), stage="validate", affects=(), max_targets=60,
    decision_only=True))
register(OperatorSpec(
    "validate_local_result", "1.0", "validate",
    input_kinds=("wall_candidate", "opening_candidate", "space_face", "contour"),
    output_kinds=("audit",), stage="validate", affects=(), max_targets=400,
    executable_by_llm=False, solver_only=True))


# ------------------------------------------------------------------ 修复策略
@dataclass(frozen=True)
class Strategy:
    """一段**受控的计算过程**：算子序列 + 允许的假设 + 预算。

    LLM 只能选 ``name``；展开成什么算子、按什么依赖执行，由编译器决定。
    """

    name: str
    version: str
    goal: str
    steps: tuple[str, ...]
    hypotheses: tuple[str, ...]
    max_targets: int = 24
    max_steps: int = 6
    budget: dict = field(default_factory=lambda: {"subdag_runs": 1, "ms": 3000})


STRATEGIES: dict[str, Strategy] = {}


def register_strategy(strategy: Strategy) -> Strategy:
    if strategy.name in STRATEGIES:
        raise ValueError(f"策略重复注册: {strategy.name}")
    STRATEGIES[strategy.name] = strategy
    return strategy


register_strategy(Strategy(
    "recover_wall_network", "1.0",
    goal="把未配对/短墙线重组为双线墙，并重建墙网与面域",
    steps=("retry_wall_pairing", "rebuild_wall_graph", "rebuild_space_faces",
           "reconcile_openings", "validate_local_result"),
    hypotheses=("fragmented_wall", "missing_wall_face", "weak_thickness_cluster",
                "single_line_wall"),
    max_targets=32))
register_strategy(Strategy(
    "recover_space_boundary", "1.0",
    goal="只为闭合试算虚拟边，不落地真实墙",
    steps=("try_virtual_boundary", "rebuild_space_faces", "associate_room_labels",
           "validate_local_result"),
    hypotheses=("broken_boundary", "room_needs_closure", "open_space_confirmed"),
    max_targets=16))
register_strategy(Strategy(
    "recover_openings", "1.0",
    goal="把平行线组/弧体重组为门窗洞口，并校正洞口归属",
    steps=("group_line_bundle", "reconcile_openings", "validate_local_result"),
    hypotheses=("window", "door", "opening_misassigned"),
    max_targets=24))
register_strategy(Strategy(
    "recover_column_geometry", "1.0",
    goal="从闭合轮廓/填充重建柱候选，并区分装饰填充",
    steps=("extract_contour", "rebuild_contour", "regenerate_column",
           "validate_local_result"),
    hypotheses=("column", "decoration_fill", "duplicate_column"),
    max_targets=24))
register_strategy(Strategy(
    "repair_semantics", "1.0",
    goal="给未知图层补语义并重算该图层的候选",
    steps=("override_layer_role", "regenerate_wall", "regenerate_opening",
           "regenerate_column", "rebuild_space_faces", "validate_local_result"),
    hypotheses=("wrong_layer", "unknown_pattern"),
    max_targets=8, max_steps=6))


# ------------------------------------------------------------------ 查询
def stage_of(operator: str) -> str:
    spec = REGISTRY.get(operator)
    return spec.stage if spec else ""


def strategy(name: str) -> Strategy | None:
    return STRATEGIES.get(name)


def steps_of(name: str) -> tuple[str, ...]:
    st = STRATEGIES.get(name)
    return st.steps if st else ()


def operators_of_strategy(name: str) -> tuple[str, ...]:
    """策略里**确实已注册**的算子（顺序保持不变）。"""
    return tuple(s for s in steps_of(name) if s in REGISTRY)


def llm_selectable_operators() -> list[str]:
    return sorted(n for n, s in REGISTRY.items() if s.executable_by_llm)


def audit() -> dict:
    """注册表自检：策略引用的算子必须存在，阶段图不能有环。"""
    problems: list[str] = []
    for st in STRATEGIES.values():
        if len(st.steps) > st.max_steps:
            problems.append(f"{st.name}:TOO_MANY_STEPS")
        for step in st.steps:
            if step not in REGISTRY:
                problems.append(f"{st.name}:UNKNOWN_OPERATOR:{step}")
            elif stage_of(step) not in STAGES:
                problems.append(f"{st.name}:UNKNOWN_STAGE:{step}")
    seen: dict[str, int] = {}

    def walk(stage: str, stack: tuple[str, ...]) -> None:
        if stage in stack:
            problems.append("STAGE_CYCLE:" + "->".join(stack + (stage,)))
            return
        if seen.get(stage):
            return
        for dep in STAGE_DEPS.get(stage, ()):
            walk(dep, stack + (stage,))
        seen[stage] = 1

    for stage in STAGES:
        walk(stage, ())
    for spec in REGISTRY.values():
        for name in spec.params:
            if name not in _PARAM_NAMES():
                problems.append(f"{spec.name}:UNREGISTERED_PARAM:{name}")
    return {"schema": "operator-registry/1.1", "problems": problems,
            "operators": len(REGISTRY), "strategies": len(STRATEGIES)}


def _PARAM_NAMES() -> frozenset:
    """延迟导入参数注册表（避免循环 import：param_registry 不依赖算子库）。"""
    try:
        from . import param_registry as PR
        return frozenset(PR.PARAMS)
    except Exception:                                  # pragma: no cover
        return frozenset()


def summary() -> dict:
    """算子库 + 策略库摘要（进 meta / 审计）。"""
    return {"schema": "operator-registry/1.1", "count": len(REGISTRY),
            "llm_selectable": llm_selectable_operators(),
            "solver_only": sorted(n for n, s in REGISTRY.items() if s.solver_only),
            "stages": {s: list(d) for s, d in STAGE_DEPS.items()},
            "versions": {n: s.version for n, s in sorted(REGISTRY.items())},
            "params": {n: list(s.params) for n, s in sorted(REGISTRY.items())
                       if s.params},
            "strategies": {n: {"version": s.version, "goal": s.goal,
                               "steps": list(s.steps),
                               "hypotheses": list(s.hypotheses),
                               "max_targets": s.max_targets}
                           for n, s in sorted(STRATEGIES.items())},
            "audit": audit()}
