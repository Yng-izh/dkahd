"""自适应链路的行为测试：闸门路由 / 提议校验 / 局部求解判定 / Reason 重放。

只用合成数据，不依赖外部大模型：所有 LLM 相关代码路径都在"无 key"或"只重放"
模式下被覆盖。
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(SKILL, "src"))

import pytest

from dwg_reader.recognition import evidence as EVID
from dwg_reader.recognition import llm_reason as REASON
from dwg_reader.recognition import local_solver as SOLVER
from dwg_reader.recognition import proposal_validator as VALID
from dwg_reader.recognition import quality_gate as GATE
from dwg_reader.recognition import recompute as RC
from dwg_reader.recognition.operator_specs import REGISTRY


# ------------------------------------------------------------------ 合成数据
def _snapshot():
    ents = []
    for i in range(8):                              # 窗图层上画 8 条长线
        ents.append({"handle": f"W{i}", "type": "LINE", "layer": "CURTWALL",
                     "start": [i * 1000.0, 0.0], "end": [i * 1000.0 + 900.0, 0.0]})
    ents.append({"handle": "T1", "type": "TEXT", "layer": "SPACE",
                 "text": "办公室", "insert": [0.0, 0.0], "height": 250.0})
    return {"header": {"insunits": 4}, "entities": ents, "derived": [], "blocks": {},
            "partitions": []}


def _plan():
    return {"layers": {"wall": ["WALL"], "window": ["CURTWALL"], "column": ["COLUMN"]},
            "block_families": {}, "operator_hints": {}, "unit_hypothesis": None}


def _entry(handle="D1", kind="door", x=0.0):
    return {"kind": kind, "item": {"handle": handle, "block": "$DorLib2D$00000001",
                                  "layer": "WINDOW", "width": 1000.0},
            "evidence": {"block": "positive", "wall_proximity": "negative",
                         "space_connection": "positive", "space_detail": "S1|S2"},
            "score": 0.43, "status": "pending"}


# ------------------------------------------------------------------ 证据索引
def test_evidence_index_has_line_supports():
    snap = _snapshot()
    idx = EVID.build(snapshot=snap)
    kinds = set(idx["counts"])
    assert "entity" in kinds and "line_support" in kinds
    pats = [i for i in idx["items"] if i.startswith("pat:")]
    assert pats, "线支撑必须进证据图（否则 LLM 的算子建议没有合法 target）"
    assert all(idx["items"][p].get("dir") is not None for p in pats)


# ------------------------------------------------------------------ 质量闸门
def test_gate_flags_window_evidence_without_candidates():
    snap = _snapshot()
    idx = EVID.build(snapshot=snap)
    gate = GATE.check(snapshot=snap, plan=_plan(), windows=(), doors=(),
                      columns=[], spaces={"faces": []}, room_labels=(), evidence=idx)
    assert gate["need_llm"] is True
    assert "WINDOW_EVIDENCE_WITHOUT_CANDIDATES" in gate["reason_codes"]
    assert gate["cases"] and gate["cases"][0]["case_id"].startswith("case_")
    assert gate["budget"]["max_rounds"] >= 1


def test_gate_is_quiet_when_candidates_exist():
    gate = GATE.check(snapshot=_snapshot(), plan=_plan(),
                      windows=[{"handle": "W1", "axis": [[0, 0], [1800, 0]]}],
                      doors=[], columns=[], spaces={"faces": []}, room_labels=[])
    assert "WINDOW_EVIDENCE_WITHOUT_CANDIDATES" not in gate["reason_codes"]


def test_gate_flags_room_boundary():
    snap = _snapshot()
    labels = [{"handle": f"T{i}", "text": "房间", "x": 0.0, "y": 0.0} for i in range(4)]
    idx = EVID.build(snapshot=snap, room_labels=labels)
    gate = GATE.check(snapshot=snap, plan=_plan(),
                      windows=[{"handle": "W1", "axis": [[0, 0], [1800, 0]]}],
                      doors=[], columns=[], spaces={"faces": []},
                      room_labels=labels, evidence=idx)
    assert "ROOM_BOUNDARY_UNRESOLVED" in gate["reason_codes"]


# ------------------------------------------------------------------ 提议校验
def _index_with_targets():
    return {"items": {"pat:1": {"kind": "line_support", "bbox": [0, 0, 100, 100]},
                      "ev:W0": {"kind": "entity", "bbox": [0, 0, 10, 10]},
                      "op:1": {"kind": "opening_candidate", "bbox": [0, 0, 10, 10]}}}


def test_validator_accepts_closed_set_action():
    ok, why = VALID.validate({"action": "group_line_bundle", "target_ids": ["pat:1"],
                              "hypothesis": "window", "evidence_ids": ["ev:W0"],
                              "reason": "平行线组"}, index=_index_with_targets())
    assert ok and why == "OK"


@pytest.mark.parametrize("proposal,reason", [
    ({"action": "no_such_op", "target_ids": ["pat:1"]}, "UNKNOWN_OPERATOR:no_such_op"),
    ({"action": "validate_local_result", "target_ids": ["pat:1"]},
     "OPERATOR_NOT_DELEGABLE:validate_local_result"),
    ({"action": "group_line_bundle", "target_ids": ["pat:999"]},
     "TARGET_NOT_FOUND:pat:999"),
    ({"action": "group_line_bundle", "target_ids": ["op:1"], "hypothesis": "window"},
     "TARGET_KIND_MISMATCH:op:1:opening_candidate"),
    ({"action": "group_line_bundle", "target_ids": ["pat:1"], "width": 1800},
     "EXTRA_FIELDS:width"),
])
def test_validator_rejects(proposal, reason):
    ok, why = VALID.validate(proposal, index=_index_with_targets())
    assert not ok and why == reason


def test_validator_rejects_geometry_payload():
    ok, why = VALID.validate({"action": "group_line_bundle", "target_ids": ["pat:1"],
                              "hypothesis": "dxf.window(12580,3260)"},
                             index=_index_with_targets())
    assert not ok and why == "GEOMETRY_IN_PROPOSAL"


# ------------------------------------------------------------------ 局部求解
def _plan_obj(with_provenance=True):
    op = {"id": "op:1", "kind": "door", "axis": [[0, 0], [1000, 0]],
          "status": "accept"}
    if with_provenance:
        op["handles"] = ["D1"]
    return SOLVER.Plan(region=[-500, -500, 2000, 2000], openings=[op], walls=[])


def test_solver_rejects_missing_provenance():
    q = SOLVER.evaluate(_plan_obj(with_provenance=False))
    assert not q["ok"]
    assert any(v.startswith("no_provenance") for v in q["violations"])


def test_solver_rejects_conflict_increase():
    old = SOLVER.evaluate(_plan_obj())
    new = dict(old)
    new["conflicts"] = old["conflicts"] + 1
    ok, why = SOLVER.improved(old, new)
    assert not ok and why == "CONFLICT_INCREASED"


def test_solver_rejects_no_provable_gain():
    q = SOLVER.evaluate(_plan_obj())
    ok, why = SOLVER.improved(q, dict(q))
    assert not ok and why == "NO_PROVABLE_GAIN"


def test_solver_accepts_when_unresolved_drops():
    old = dict(SOLVER.evaluate(_plan_obj()))
    new = dict(old)
    new["unresolved"] = max(0, old["unresolved"] - 1)
    ok, why = SOLVER.improved(old, new)
    if old["unresolved"] == 0:
        assert not ok                       # 没有可减少的未决，就不算改善
    else:
        assert ok and why == "ACCEPTED"


# ------------------------------------------------------------------ 算子库与重算
def test_registry_closed_and_permissioned():
    assert len(REGISTRY) >= 12
    assert REGISTRY["validate_local_result"].solver_only is True
    assert REGISTRY["group_line_bundle"].executable_by_llm is True


def test_recompute_plan_expands_downstream():
    out = RC.plan(["walls_seg"])
    assert out["nodes"][0] == "walls_seg"
    assert "rooms" in out["nodes"] and out["cost_hint"] > 0


def test_recompute_region_roundtrip():
    base = [{"id": "a", "bbox": [0, 0, 10, 10]}, {"id": "b", "bbox": [9000, 9000, 9010, 9010]}]
    patch = [{"id": "c", "bbox": [1, 1, 2, 2]}]
    out = RC.merge_region(base, patch, [0, 0, 500, 500])
    assert [r["id"] for r in out] == ["b", "c"]


# ------------------------------------------------------------------ LLM Reason
def test_reason_skipped_without_cases():
    out = REASON.analyze([], index={"items": {}})
    assert out["status"] == "skipped"


def test_reason_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    cases = [{"case_id": "case_001", "reason": "WINDOW_EVIDENCE_WITHOUT_CANDIDATES",
              "evidence_ids": [], "region": [0, 0, 100, 100]}]
    out = REASON.analyze(cases, index={"items": {}})
    assert out["status"] == "unavailable"


def test_reason_sanitizes_proposals(monkeypatch):
    raw = {"proposals": [{"action": "group_line_bundle", "target_ids": ["pat:1"],
                          "hypothesis": "window", "evidence_ids": ["ev:W0"],
                          "reason": "平行线", "x": 123.0}]}
    out = REASON._sanitize(raw, "case_001")
    assert out["case_id"] == "case_001"
    assert out["proposals"][0]["action"] == "group_line_bundle"
    assert "x" not in out["proposals"][0]


def test_reason_replay_reads_cache(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    cases = [{"case_id": "case_001", "reason": "ROOM_BOUNDARY_UNRESOLVED",
              "evidence_ids": [], "region": [0, 0, 100, 100]}]
    index = {"items": {}}
    from dwg_reader.recognition.llm_profile import DEFAULT_MODEL
    monkeypatch.delenv("LLM_MODEL", raising=False)
    bundle = REASON.build_case(cases[0], index=index)
    key = REASON._key(bundle, DEFAULT_MODEL)
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({key: {"case_id": "case_001", "proposals": [
        {"action": "rebuild_space_faces", "target_ids": ["wall:1"],
         "hypothesis": None, "evidence_ids": [], "reason": "面域重建"}]}}),
        encoding="utf-8")
    out = REASON.replay(cases, index=index, cache_path=str(cache))
    assert out["status"] == "replay"
    assert out["proposals"][0]["action"] == "rebuild_space_faces"


# ------------------------------------------------------------------ 端到端（模拟 LLM）
def _window_lines_snapshot(n=8):
    """4~8 条短平行线：窗图层有证据、但默认识别不出窗。"""
    ents = []
    for i in range(n):
        x = 1000.0 + i * 400.0
        ents.append({"handle": f"W{i}", "type": "LINE", "layer": "CURTWALL",
                     "start": [x, 0.0], "end": [x + 1200.0, 0.0]})
        ents.append({"handle": f"V{i}", "type": "LINE", "layer": "CURTWALL",
                     "start": [x, 200.0], "end": [x + 1200.0, 200.0]})
    return {"header": {"insunits": 4}, "entities": ents, "derived": [], "blocks": {},
            "partitions": []}


def test_case_builder_gives_geometric_features_and_alternatives():
    snap = _window_lines_snapshot()
    idx = EVID.build(snapshot=snap)
    gate = GATE.check(snapshot=snap, plan=_plan(), windows=(), doors=(), columns=(),
                      spaces={"faces": []}, room_labels=(), evidence=idx)
    assert gate["need_llm"] is True
    case = gate["cases"][0]
    assert case["patterns"], "必须给出线束几何特征"
    pat = case["patterns"][0]
    assert pat["line_count"] >= 2 and "parallel_consistency" in pat
    assert any(a["operation"] == "regenerate_opening" for a in case["alternatives"])


def test_end_to_end_window_recovered_by_llm_choice():
    """原始线 -> 闸门 -> 替代方案 -> (模拟 LLM 选择) -> 校验 -> 重算 -> 统一状态。"""
    from dwg_reader.recognition import executors as EXEC
    from dwg_reader.recognition import finalstate as FS

    snap = _window_lines_snapshot()
    idx = EVID.build(snapshot=snap)
    gate = GATE.check(snapshot=snap, plan=_plan(), windows=(), doors=(), columns=(),
                      spaces={"faces": []}, room_labels=(), evidence=idx)
    case = gate["cases"][0]
    alt = next(a for a in case["alternatives"] if a["operation"] == "regenerate_opening")

    # 模拟模型：选中 alt（只有 id 与理由，不含任何几何）
    chosen = {"action": alt["operation"], "target_ids": alt["target_ids"],
              "hypothesis": alt["hypothesis"], "alternative": alt["id"],
              "evidence_ids": alt["target_ids"][:2], "reason": "平行线组与墙同向"}
    ok, why = VALID.validate(chosen, index=idx, region=case.get("region"))
    assert ok, why

    state = {"walls": [], "columns": [], "doors": [], "windows": [],
             "faces": [], "unresolved": []}
    out = SOLVER.reconcile(state=state, actions=[chosen], index=idx,
                           region=case.get("region"))
    assert out["status"] == "accepted", out["log"]
    assert out["patch"]["openings"], "重算必须真的产出洞口候选"

    final = FS.build(walls_seg=[], columns=[], doors=[], windows=[],
                     openings_select={"doors": [], "windows": []},
                     spaces={"stats": {}}, patch=dict(out["patch"], status="accepted"))
    assert final["windows"], "统一状态里必须出现新窗"
    win = final["windows"][0]
    assert win.get("handles"), "新窗必须带可追溯的 source handles"


def test_rejected_patch_does_not_change_final_state():
    from dwg_reader.recognition import finalstate as FS
    base = FS.build(walls_seg=[{"start": [0, 0], "end": [1, 0], "srcs": [{"handle": "A"}]}],
                    columns=[], doors=[], windows=[],
                    openings_select={"doors": [], "windows": []}, spaces={"stats": {}},
                    patch={"status": "skipped", "walls": [{"start": [0, 0], "end": [9, 9]}]})
    assert len(base["walls"]) == 1
    assert base["provenance"]["patch"] == "skipped"


def test_layer_role_override_is_per_drawing():
    from dwg_reader.recognition import layer_roles as ROLES
    ok, why = ROLES.validate("CURTWALL", ["wall", "window"])
    assert ok and why == "OK"
    bad_ok, bad_why = ROLES.validate("CURTWALL", ["window", "wall", "column"])
    assert not bad_ok and bad_why == "TOO_MANY_ROLES"
    unknown_ok, unknown_why = ROLES.validate("CURTWALL", ["spaceship"])
    assert not unknown_ok and unknown_why.startswith("UNKNOWN_ROLE")
    plan = {"layers": {"wall": ["WALL"], "window": []}}
    merged = ROLES.apply_to_plan(plan, {"CURTWALL": ["wall", "window"]})
    assert "CURTWALL" in merged["layers"]["window"]
    assert "CURTWALL" not in plan["layers"]["window"], "不得污染原 profile 对象"


def test_validator_rejects_layer_role_on_wrong_target():
    idx = {"items": {"pat:1": {"kind": "line_support", "bbox": [0, 0, 9, 9]}}}
    ok, why = VALID.validate({"action": "propose_layer_role",
                              "target_ids": ["pat:1"], "roles": ["window"]}, index=idx)
    assert not ok and why == "NO_LAYER_TARGET"
