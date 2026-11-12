"""控制层（PR-0 ~ PR-4）的行为测试：编译 / 子 DAG 执行 / 影子验收 / 事务提交。

全部用合成数据，不依赖外部模型：LLM 路径只在"无 key"/"重放"模式下覆盖。
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(SKILL, "src"))

import pytest

from dwg_reader.recognition import adaptive_controller as ADAPT
from dwg_reader.recognition import case_builder as CB
from dwg_reader.recognition import evidence as EVID
from dwg_reader.recognition import executors as EXEC
from dwg_reader.recognition import finalstate as FS
from dwg_reader.recognition import layer_roles as ROLES
from dwg_reader.recognition import local_solver as SOLVER
from dwg_reader.recognition import operator_specs as OPSPEC
from dwg_reader.recognition import plan_compiler as COMPILER
from dwg_reader.recognition import raw_evidence as RAV
from dwg_reader.recognition import recompute as RC
from dwg_reader.recognition import repair_planner as PLANNER


# ------------------------------------------------------------------ 合成数据
def _snapshot(n=6, layer="WALL"):
    """n 对平行墙面线（间距 200mm），构成 n 段可配对但未入库的墙。"""
    ents = []
    for i in range(n):
        x0 = 1000.0 + i * 3000.0
        ents.append({"handle": f"A{i}", "type": "LINE", "layer": layer,
                     "start": [x0, 0.0], "end": [x0 + 2600.0, 0.0]})
        ents.append({"handle": f"B{i}", "type": "LINE", "layer": layer,
                     "start": [x0, 200.0], "end": [x0 + 2600.0, 200.0]})
    return {"header": {"insunits": 4}, "entities": ents, "derived": [], "blocks": {},
            "partitions": []}


def _plan(snapshot=None):
    return {"layers": {"wall": ["WALL"], "window": ["CURTWALL"], "column": ["COLUMN"]},
            "block_families": {}, "operator_hints": {}, "unit_hypothesis": None}


def _index(n=6):
    return EVID.build(snapshot=_snapshot(n))


# ------------------------------------------------------------------ 算子/策略库
def test_registry_and_strategies_are_consistent():
    audit = OPSPEC.audit()
    assert audit["problems"] == []
    assert audit["strategies"] >= 5
    for name in ("recover_wall_network", "recover_space_boundary",
                 "recover_openings", "recover_column_geometry", "repair_semantics"):
        st = OPSPEC.strategy(name)
        assert st and st.steps and st.hypotheses
        assert all(s in OPSPEC.REGISTRY for s in st.steps)


def test_every_operator_has_a_stage():
    for name, spec in OPSPEC.REGISTRY.items():
        assert spec.stage in OPSPEC.STAGES, name
    order = RC.stage_order()
    for stage, deps in OPSPEC.STAGE_DEPS.items():
        for dep in deps:
            assert order.index(dep) < order.index(stage), (dep, stage)


# ------------------------------------------------------------------ 计划编译
def _wall_targets(index):
    return [i for i, v in index["items"].items()
            if v.get("kind") == "line_support"][:4]


def test_compile_expands_strategy_into_dependent_steps():
    idx = _index()
    plan = COMPILER.compile_plan(
        {"case_id": "case_001", "decision": "TRY",
         "strategy_id": "recover_wall_network",
         "target_ids": _wall_targets(idx), "hypothesis": "fragmented_wall"},
        index=idx)
    assert plan.ok, plan.errors
    ops = [s["operator"] for s in plan.steps]
    assert ops[0] == "retry_wall_pairing"
    assert "rebuild_space_faces" in ops and "validate_local_result" in ops
    # 依赖必须"下游依赖上游"，且墙网在面域之前
    space = next(s for s in plan.steps if s["stage"] == "space")
    wall = next(s for s in plan.steps if s["stage"] == "walls")
    assert wall["index"] in space["deps"]
    assert list(plan.stages()) == sorted(plan.stages(), key=RC.stage_order().index)


def test_compile_no_action_is_valid_empty_plan():
    plan = COMPILER.compile_plan({"case_id": "c", "decision": "NO_ACTION"},
                                 index=_index())
    assert plan.ok and plan.empty and plan.steps == []


@pytest.mark.parametrize("proposal,code", [
    ({"decision": "MAYBE", "strategy_id": "recover_wall_network"}, "UNKNOWN_DECISION"),
    ({"decision": "TRY", "strategy_id": "no_such_strategy",
      "target_ids": ["pat:1"]}, "UNKNOWN_STRATEGY"),
    ({"decision": "TRY", "strategy_id": "recover_wall_network",
      "target_ids": ["pat:999"]}, "TARGET_NOT_FOUND"),
    ({"decision": "TRY", "strategy_id": "recover_wall_network",
      "target_ids": ["pat:1"], "hypothesis": "spaceship"}, "HYPOTHESIS_NOT_ALLOWED"),
])
def test_compile_rejects_out_of_contract_proposals(proposal, code):
    idx = _index()
    proposal = dict(proposal, case_id="c1")
    plan = COMPILER.compile_plan(proposal, index=idx)
    assert not plan.ok
    assert code in [e["code"] for e in plan.errors]


def test_compile_rejects_unauthorized_region():
    idx = _index()
    plan = COMPILER.compile_plan(
        {"case_id": "c1", "decision": "TRY", "strategy_id": "recover_wall_network",
         "target_ids": _wall_targets(idx), "region": [100.0, 100.0, 200.0, 200.0]},
        index=idx, allowed_regions=[[0.0, 0.0, 50.0, 50.0]])
    assert not plan.ok
    assert "REGION_NOT_ALLOWED" in [e["code"] for e in plan.errors]


def test_compile_truncates_targets_to_budget():
    idx = _index(20)
    targets = [i for i, v in idx["items"].items() if v.get("kind") == "line_support"]
    plan = COMPILER.compile_plan(
        {"case_id": "c1", "decision": "TRY", "strategy_id": "recover_wall_network",
         "target_ids": targets}, index=idx)
    assert plan.ok and len(plan.target_ids) <= 32
    assert any(w.startswith("TARGETS_TRUNCATED") for w in plan.warnings)


def test_plan_roundtrip_is_executable():
    idx = _index()
    plan = COMPILER.compile_plan(
        {"case_id": "c1", "decision": "TRY", "strategy_id": "recover_wall_network",
         "target_ids": _wall_targets(idx), "hypothesis": "fragmented_wall"},
        index=idx)
    again = COMPILER.plan_from_dict(json.loads(json.dumps(plan.as_dict())))
    assert again.ok and [s["operator"] for s in again.steps] == \
        [s["operator"] for s in plan.steps]


# ------------------------------------------------------------------ 子 DAG 执行
def test_subdag_executes_and_does_not_mutate_input_state():
    idx = _index()
    targets = _wall_targets(idx)
    plan = COMPILER.compile_plan(
        {"case_id": "c1", "decision": "TRY", "strategy_id": "recover_wall_network",
         "target_ids": targets, "hypothesis": "fragmented_wall"}, index=idx)
    state = {"walls": [], "columns": [], "doors": [], "windows": [],
             "faces": [], "inferred_edges": [], "unresolved": []}
    before = json.dumps(state, sort_keys=True)
    out = RC.execute_local_subdag(plan, state=state, index=idx)
    assert json.dumps(state, sort_keys=True) == before, "子 DAG 不得修改入参状态"
    assert out["state"]["walls"], "必须真的算出墙候选"
    assert out["changed"]["walls"] > 0
    assert out["status"] == "ok"
    ops = [t["operator"] for t in out["trace"]]
    assert "retry_wall_pairing" in ops
    assert "space" in out["stages"] or "space" in out["implicit"], \
        "墙变了之后面域必须重算（策略里没写就自动补）"


def test_subdag_is_deterministic():
    idx = _index()
    targets = _wall_targets(idx)
    plan = COMPILER.compile_plan(
        {"case_id": "c1", "decision": "TRY", "strategy_id": "recover_wall_network",
         "target_ids": targets}, index=idx)
    state = {"walls": [], "columns": [], "doors": [], "windows": [], "faces": []}
    a = RC.execute_local_subdag(plan, state=state, index=idx)
    b = RC.execute_local_subdag(plan, state=state, index=idx)
    assert json.dumps(a["state"], sort_keys=True) == json.dumps(b["state"],
                                                                sort_keys=True)


# ------------------------------------------------------------------ 影子验收
def _wall(start, end, handles, **kw):
    return {"id": "w1", "start": start, "end": end,
            "bbox": EVID.bbox_of({"start": start, "end": end}),
            "handles": handles, "srcs": [{"handle": h} for h in handles], **kw}


def test_shadow_rejects_count_only_gain():
    """多出几堵墙但没多覆盖任何原始线 -> 不构成收益。"""
    before = {"walls": [], "columns": [], "doors": [], "windows": [], "faces": [],
              "unresolved": []}
    after = {"walls": [_wall([0, 0], [1000, 0], ["X1", "X2"])], "columns": [],
             "doors": [], "windows": [], "faces": [], "unresolved": []}
    support = {"S1", "S2", "S3"}
    ev = SOLVER.shadow_evaluate(before=before, after=after, support=support,
                                plan={"strategy_id": "recover_wall_network"})
    assert not ev["ok"]
    assert "evidence" in ev["verdict"]


def test_shadow_accepts_real_coverage_gain():
    before = {"walls": [], "columns": [], "doors": [], "windows": [], "faces": [],
              "unresolved": []}
    after = {"walls": [_wall([0, 0], [1000, 0], ["S1", "S2"])], "columns": [],
             "doors": [], "windows": [], "faces": [], "unresolved": []}
    ev = SOLVER.shadow_evaluate(before=before, after=after, support={"S1", "S2", "S3"},
                                plan={"strategy_id": "recover_wall_network"})
    assert ev["ok"], ev["verdict"]
    assert ev["after"]["coverage"] > ev["before"]["coverage"]


def test_shadow_blocks_self_proof_without_independent_evidence():
    """补墙只靠"房名需要闭合"，没有第二条独立证据 -> 不许提交。"""
    before = {"walls": [], "columns": [], "doors": [], "windows": [], "faces": [],
              "unresolved": []}
    after = {"walls": [_wall([0, 0], [1000, 0], ["S1"], geometry_state="inferred")],
             "columns": [], "doors": [], "windows": [], "faces": [],
             "unresolved": []}
    ev = SOLVER.shadow_evaluate(before=before, after=after, support=set(),
                                plan={"strategy_id": "recover_space_boundary"},
                                independent=0)
    assert not ev["ok"] and "no_self_proof" in ev["verdict"]


def test_shadow_detects_change_outside_region():
    before = {"walls": [_wall([9000, 9000], [9500, 9000], ["P1", "P2"])],
              "columns": [], "doors": [], "windows": [], "faces": [], "unresolved": []}
    after = dict(before)
    after["walls"] = list(before["walls"]) + [
        _wall([20000, 20000], [20500, 20000], ["Q1", "Q2"], id="w2")]
    ev = SOLVER.shadow_evaluate(before=before, after=after, support={"Q1", "Q2"},
                                plan={"strategy_id": "recover_wall_network"},
                                region=[0, 0, 1000, 1000])
    assert not ev["ok"] and "isolation" in ev["verdict"]


# ------------------------------------------------------------------ 事务式提交
def test_finalstate_does_not_mutate_patch():
    patch = {"status": "accepted", "region": None,
             "walls": [{"id": "w1", "start": [0, 0], "end": [1000, 0],
                        "handles": ["A", "B"], "srcs": [{"handle": "A"}]}],
             "openings": [], "columns": [], "faces": [], "unresolved": []}
    snapshot = json.dumps(patch, sort_keys=True)
    state = FS.build(walls_seg=[], columns=[], doors=[], windows=[],
                     openings_select={"doors": [], "windows": []},
                     spaces={"stats": {}}, patch=patch)
    assert json.dumps(patch, sort_keys=True) == snapshot, "补丁必须不可变"
    assert state["walls"] and state["accept_state"]["w1"] == "accepted"


def test_finalstate_rolls_back_inferred_walls():
    patch = {"status": "accepted", "region": None,
             "walls": [{"id": "wi", "start": [0, 0], "end": [1000, 0],
                        "geometry_state": "inferred", "srcs": []}],
             "openings": [], "columns": [], "faces": [], "unresolved": []}
    state = FS.build(walls_seg=[], columns=[], doors=[], windows=[],
                     openings_select={"doors": [], "windows": []},
                     spaces={"stats": {}}, patch=patch)
    assert state["walls"] == [] and state["inferred_walls"] == 1


# ------------------------------------------------------------------ 控制器
def _case(index, targets, reason="DEFECT_UNPAIRED_FACE", region=None):
    return {"case_id": "case_001", "reason": reason,
            "region": region or [0, -500, 20000, 500],
            "evidence_ids": list(targets),
            "strategies": [{"strategy_id": "recover_wall_network",
                            "goal": "重配墙线",
                            "hypotheses": ["fragmented_wall"],
                            "target_ids": list(targets)}],
            "patterns": [], "alternatives": []}


def test_controller_applies_accepted_plan_and_keeps_ledger():
    idx = _index()
    ledger = RAV.build(_snapshot())
    targets = _wall_targets(idx)
    state = {"walls": [], "columns": [], "doors": [], "windows": [], "faces": [],
             "inferred_edges": [], "unresolved": []}
    out = ADAPT.run(state=state, index=idx, cases=[_case(idx, targets)],
                    ledger=ledger, columns=(), partitions=(), thick=200.0)
    assert out["status"] in ("applied", "no_action")
    assert out["runs"] >= 1
    # 无论采纳与否，都必须留下可审计的结论
    assert out["applied"] or out["rolled_back"] or out["rejected"]
    assert ADAPT.audit(out)["schema"] == ADAPT.SCHEMA


def test_controller_rollback_keeps_state_identical():
    idx = _index()
    targets = _wall_targets(idx)
    state = {"walls": [], "columns": [], "doors": [], "windows": [], "faces": [],
             "inferred_edges": [], "unresolved": []}
    before = json.dumps(state, sort_keys=True)
    case = _case(idx, targets)
    case["strategies"] = []
    case["alternatives"] = []
    out = ADAPT.run(state=state, index=idx, cases=[case], columns=(), partitions=(),
                    thick=200.0)
    assert out["status"] == "no_action"
    assert RC.counts(out["state"]) == RC.counts(state)
    assert out["state"]["walls"] == state["walls"]


def test_controller_respects_budget():
    idx = _index()
    targets = _wall_targets(idx)
    cases = [dict(_case(idx, targets), case_id=f"case_{i:03d}") for i in range(5)]
    out = ADAPT.run(state={"walls": [], "columns": [], "doors": [], "windows": [],
                           "faces": []}, index=idx, cases=cases,
                    columns=(), partitions=(), thick=200.0,
                    budget={"max_runs": 2, "max_cases": 2})
    assert out["runs"] <= 2


# ------------------------------------------------------------------ 证据池
def test_ledger_trace_covers_handle_to_repairs():
    snap = _snapshot()
    ledger = RAV.build(snap)
    from dwg_reader.recognition import primitives as PRIM
    RAV.annotate_prims(ledger, PRIM.extract(snap))
    RAV.mark(ledger, "A0", RAV.WEAK, role="unpaired_face", reason="unpaired_face",
             candidate="wall:1")
    tr = RAV.trace(ledger, "A0")
    assert tr["status"] == RAV.WEAK
    assert tr["candidates"] == ["wall:1"]
    assert tr["reason"] == "unpaired_face"
    assert "recover_wall_network" in tr["repairs"], "必须给出可尝试的修复策略"
    assert tr["prim_ids"], "必须能追溯到原语 id"


def test_pool_groups_reusable_evidence():
    ledger = RAV.build(_snapshot())
    RAV.mark(ledger, "A0", RAV.WEAK, reason="THICKNESS_CLUSTER_WEAK")
    RAV.mark(ledger, "A1", RAV.WEAK, reason="THICKNESS_CLUSTER_WEAK")
    pool = RAV.pool(ledger)
    weak = next(g for g in pool if g["reason"] == "THICKNESS_CLUSTER_WEAK")
    assert weak["count"] >= 2
    assert weak["repairs"] == ["recover_wall_network"]


# ------------------------------------------------------------------ Case 几何
def test_case_builder_uses_degrees_and_vector_walls():
    snap = _snapshot()
    idx = EVID.build(snapshot=snap)
    case = {"case_id": "c", "reason": "WINDOW_EVIDENCE_WITHOUT_CANDIDATES",
            "region": [0, -1000, 20000, 2000], "evidence_ids": []}
    built = CB.build(case, idx, walls=[{"id": "w1", "start": [0, 100], "end": [9000, 100]}])
    assert built["patterns"], "线束特征必须给出"
    pat = max(built["patterns"], key=lambda p: p["wall_alignment"])
    assert pat["parallel_consistency"] == 1.0     # 两条平行线：角度制比较
    assert pat["pair_gap_mm"] == 200.0
    assert pat["angle_deg"] == 0.0
    assert pat["wall_alignment"] > 0.99           # 真实向量对齐，不是 bbox 猜 H/V
    assert built["strategies"], "必须给出闭集策略供模型选择"


def test_case_builder_slanted_wall_is_not_treated_as_horizontal():
    """45° 的墙不能被当成水平墙（旧版用 bbox 长宽比推断方向）。"""
    snap = _snapshot()
    idx = EVID.build(snapshot=snap)
    case = {"case_id": "c", "reason": "WINDOW_EVIDENCE_WITHOUT_CANDIDATES",
            "region": [0, -1000, 20000, 2000], "evidence_ids": []}
    built = CB.build(case, idx, walls=[{"id": "d1",
                                        "start": [0.0, 0.0], "end": [3000.0, 3000.0]}])
    pat = max(built["patterns"], key=lambda p: p["wall_alignment"])
    assert pat["nearby_wall"] == "d1"
    assert pat["wall_alignment"] < 0.8, "平行线束与 45° 墙不应判为同向"


# ------------------------------------------------------------------ 语义缓存
def test_layer_role_cache_is_scoped_by_drawing():
    ok, why = ROLES.validate("CURTWALL", ["wall", "window"])
    assert ok and why == "OK"
    a = ROLES.stage({"CURTWALL": ["window"]}, accepted=True,
                    file_digest="digest-A", model="m1")
    b = ROLES.stage({"CURTWALL": ["window"]}, accepted=True,
                    file_digest="digest-B", model="m1")
    c = ROLES.stage({"CURTWALL": ["window"]}, accepted=True,
                    file_digest="digest-A", model="m2")
    assert len({a[0]["key"], b[0]["key"], c[0]["key"]}) == 3, \
        "图纸哈希 / 模型版本必须参与缓存键"


def test_layer_role_stage_does_not_write_files(tmp_path, monkeypatch):
    monkeypatch.setenv("DWG_LAYER_ROLE_CACHE", str(tmp_path / "roles.json"))
    ROLES.stage({"CURTWALL": ["window"]}, accepted=True, file_digest="d")
    assert not (tmp_path / "roles.json").exists(), "PURE 节点不得写盘"
    entries = ROLES.stage({"CURTWALL": ["window"]}, accepted=True, file_digest="d")
    assert ROLES.commit_staged(entries) == 1
    assert (tmp_path / "roles.json").exists()


# ------------------------------------------------------------------ 规划器
def test_planner_degrades_without_key(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    idx = _index()
    case = CB.build({"case_id": "c1", "reason": "DEFECT_UNPAIRED_FACE",
                     "region": [0, -500, 20000, 500],
                     "evidence_ids": _wall_targets(idx)}, idx)
    out = PLANNER.analyze([case], index=idx)
    assert out["status"] == "unavailable" and out["proposals"] == {}


def test_planner_sanitizes_out_of_contract_output():
    idx = _index()
    case = CB.build({"case_id": "c1", "reason": "DEFECT_UNPAIRED_FACE",
                     "region": [0, -500, 20000, 500],
                     "evidence_ids": _wall_targets(idx)}, idx)
    bundle = PLANNER.build_case(case, index=idx)
    raw = {"decisions": [{"case_id": "c1", "decision": "TRY",
                          "strategy_id": "no_such_strategy",
                          "target_ids": ["pat:1"], "x": 1234.5},
                         {"case_id": "c1", "decision": "TRY",
                          "strategy_id": "recover_wall_network",
                          "target_ids": ["pat:1", "nope:9"],
                          "hypothesis": "1.2m", "width": 900}]}
    props = PLANNER.sanitize(raw, bundle)
    assert len(props) == 2
    assert props[0]["decision"] == "NO_ACTION" and props[0]["strategy_id"] is None
    assert props[1]["strategy_id"] == "recover_wall_network"
    assert props[1]["hypothesis"] is None, "带数字的假设一律丢弃"
    assert props[1]["target_ids"] == ["pat:1"], "解析不到的证据 id 一律丢弃"
    assert all("x" not in p and "width" not in p for p in props)


def test_planner_heuristic_picks_cheapest_strategy():
    idx = _index()
    targets = _wall_targets(idx)
    case = {"case_id": "c1", "strategies": [
        {"strategy_id": "recover_wall_network", "hypotheses": ["fragmented_wall"],
         "target_ids": targets},
        {"strategy_id": "recover_openings", "hypotheses": ["window"],
         "target_ids": targets}]}
    pick = PLANNER.heuristic(case)
    assert pick["strategy_id"] == "recover_openings"
    assert PLANNER.needs_llm(case) is True
    assert PLANNER.needs_llm({"strategies": [case["strategies"][0]]}) is False
