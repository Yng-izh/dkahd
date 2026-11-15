"""Column identity needs semantic evidence independent of geometric eligibility."""
from types import SimpleNamespace
import pytest
from dwg_reader.recognition.v2 import component_resolver as CR
from dwg_reader.recognition.v2 import agent_controller as AG
from dwg_reader.recognition.v2 import multimodal_detector as MD


def column(**overrides):
    h = dict(id="c", category="column", status="pending", source="rule",
             bbox=[0, 0, 500, 500], uids=["c"],
             geometry=dict(outline=[[0, 0], [500, 0], [500, 500], [0, 500]],
                           w_mm=500, h_mm=500),
             evidence=dict(geometry_status="verified", v1_reason="CATEGORY_UNVERIFIED"))
    h.update(overrides)
    return h


def confirmed(**overrides):
    h = column(status="candidate", **overrides)
    h["evidence"].update(category_confirmed=True, category_basis="model_family")
    return h


def test_unverified_geometry_is_not_a_category_even_with_high_score():
    h = column(status="candidate", score=1)
    h["evidence"].update(on_grid=True, touches_wall=True, repeats=100)
    r = CR.staged([h])
    assert r["final"]["counts"].get("column", 0) == 0
    assert r["pending"] == ["c"]
    assert r["rejected"][0]["why"] == "CATEGORY_UNVERIFIED"


@pytest.mark.parametrize("basis", ["model_family", "catalog_block"])
def test_confirmed_category_and_verified_geometry_count(basis):
    h = confirmed()
    h["evidence"]["category_basis"] = basis
    assert CR.solve([h])["counts"] == {"column": 1}


def test_column_propagation_uses_column_geometry_not_door_jambs():
    h = confirmed(source="propagation")
    assert CR._propagation_ok(h) == (True, "COLUMN_GEOMETRY")
    h["geometry"] = {"host_ok": True, "hinge": [0, 0]}
    assert not CR._propagation_ok(h)[0]


@pytest.mark.parametrize("geometry_status", ["verified", "pending"])
def test_arbitration_keep_promotes_only_valid_geometry(geometry_status):
    h = column()
    h["evidence"]["geometry_status"] = geometry_status
    r = CR.staged([h], constraints={"keep": ["c"]})
    assert r["final"]["counts"].get("column", 0) == int(geometry_status == "verified")
    if geometry_status == "verified":
        assert r["final"]["selected"][0]["evidence"]["category_basis"] == "model_arbitration"


def test_reclassification_requires_refitting():
    r = CR.staged([confirmed()], constraints={"categories": {"c": "window"}})
    assert not r["final"]["selected"]


def test_reject_removes_even_block_defined_from_count_and_reports():
    h = confirmed()
    h["evidence"]["block_defined"] = True
    r, dropped = AG._apply_verdicts(CR.staged([h]), [{"id": "c", "verdict": "reject"}])
    assert not r["final"]["selected"]
    assert dropped[0]["why"] == "MODEL_REJECT"


@pytest.mark.parametrize("verdict", ["unknown", "unavailable", "rejected", "reject"])
def test_failed_or_unknown_decisions_never_promote(verdict):
    h = column()
    r, _ = AG._apply_verdicts(CR.staged([h]), [{"id": "c", "verdict": verdict}], candidates=[h])
    assert not r["final"]["selected"]


def test_recheck_pending_can_promote_and_rechecks_conflicts():
    h = column()
    r, _ = AG._apply_verdicts(CR.staged([h]), [dict(id="c", verdict="keep", category="column", confidence="high")], candidates=[h])
    assert r["final"]["counts"] == {"column": 1}
    assert not r["rejected"]


def test_later_stage_does_not_revive_conflicting_column():
    c = confirmed()
    w = dict(id="w", category="window", source="rule", status="candidate", bbox=c["bbox"],
             uids=["w"], geometry=dict(axis=[[0,0],[500,0]], width_mm=500, frame_gap_mm=500,
                                      outline=c["geometry"]["outline"]),
             evidence=dict(evidence_state="strong", host_ok=True))
    r = CR.staged([c, w], constraints={"keep": ["w"]}, budget={"rounds": 0})
    assert [h["id"] for h in r["final"]["selected"]] == ["w"]
    assert any(h["id"] == "c" for h in r["rejected"])


@pytest.mark.parametrize("data", [{}, {"verdict": "keep", "component": {"category": "unknown", "confidence": "high"}},
                                    {"verdict": "keep", "component": {"category": "column"}},
                                    {"verdict": "maybe", "component": {"category": "column", "confidence": "high"}}])
def test_detector_unknown_responses_are_not_keep(data):
    gateway = SimpleNamespace(available=True, ask=lambda **kw: SimpleNamespace(ok=True, data=data, images=0))
    r = MD.recheck([column()], ir={}, gateway=gateway)
    assert r["verdicts"][0]["verdict"] == "unknown"


def test_arbitration_unanswered_candidates_are_not_kept():
    arb = {"verdicts": [{"result": {"status": "ok", "shown": [
        {"label": "A", "id": "c"}, {"label": "B", "id": "b"}],
        "per_candidate": {"B": {"keep": False}}},
        "applied": {"keep": ["c"], "drop": ["b"]}}]}
    constraints = AG._arbitration_constraints(arb, [column(), column(id="b")])
    assert constraints["keep"] == set()
    assert constraints["drop"] == {"b"}


def test_recheck_cannot_override_geometry_failure():
    h = column()
    h["evidence"]["geometry_status"] = "pending"
    r, _ = AG._apply_verdicts(CR.staged([h]), [dict(
        id="c", verdict="keep", category="column", confidence="high")], candidates=[h])
    assert not r["final"]["selected"]


@pytest.mark.parametrize("confidence", [None, "certain", "HIGHISH"])
def test_family_read_rejects_invalid_confidence(confidence):
    data = {"component": {"category": "column", "confidence": confidence}}
    gateway = SimpleNamespace(available=True, ask=lambda **kw: SimpleNamespace(ok=True, data=data, images=0))
    r = MD.read_family({"id": "f", "instances": []}, ir={}, gateway=gateway)
    assert r["status"] == "rejected"
    assert r["reason"] == "UNKNOWN_CONFIDENCE"


@pytest.mark.parametrize("decision,expected", [
    ("off", 0), ("error", 0), ("high", 1), ("none", 0), ("medium", 0),
])
def test_real_node_four_line_pending_family_controller(monkeypatch, decision, expected):
    from dwg_reader.recognition import nodes as N, elements as E
    from dwg_reader.recognition.v2 import cad_ir as IR

    # Exercise actual line reconstruction and its list-valued v1 evidence contract.
    pts = [[0, 0], [600, 0], [600, 600], [0, 600]]
    entities = [dict(type="LINE", handle=str(i), layer="0", start=pts[i],
                     end=pts[(i + 1) % 4]) for i in range(4)]
    snapshot = {"entities": entities, "derived": [], "blocks": {}}
    ctx = SimpleNamespace(inputs={"load": {"snapshot": snapshot,
        "plan": {"layers": {"column": [], "axis": []}}, "scale": None}},
        params={"llm_review": False})
    monkeypatch.setattr(N, "_shape_blocks", lambda *a, **kw: {})
    monkeypatch.setattr(E, "_footprint_blocks", lambda *a, **kw: {})
    monkeypatch.setattr(N.OPS, "run", lambda *a, **kw: [])
    columns = N.node_columns(ctx)["columns"]
    assert len(columns) == 1
    assert columns[0]["status"] == "pending"
    assert columns[0]["pending_reason"] == "CATEGORY_UNVERIFIED"
    assert columns[0]["geometry_status"] == "verified"
    assert isinstance(columns[0]["evidence"], list)
    calls = []

    def ask(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(ok=decision != "error", code="TEST_MODEL_ERROR",
            images=0, data={"component": {
                "category": "none" if decision == "none" else "column",
                "subtype": "rect_column",
                "confidence": "medium" if decision == "medium" else "high"}})

    ir = IR.build(snapshot, scale={"thickness": 200, "noise": 2})
    result = AG.run(ir=ir, columns=columns,
                    gateway=SimpleNamespace(available=True, ask=ask),
                    ai={"mode": "off" if decision == "off" else "enhance"},
                    profile="openings_columns",
                    budget={"max_model_calls": 4 if decision == "none" else 1})
    assert result["resolution"]["final"]["counts"].get("column", 0) == expected
    assert len(calls) == (0 if decision == "off" else 1)
    if decision == "high":
        selected = result["resolution"]["final"]["selected"]
        assert selected[0]["evidence"]["category_confirmed"] is True
        assert selected[0]["evidence"]["category_basis"] == "model_family"
    if decision == "none":
        assert result["families"]["rejected"]


def test_model_off_does_not_reuse_or_mutate_supplied_family_confirmation():
    from copy import deepcopy
    from dwg_reader.recognition.v2 import cad_ir as IR, component_family as FAMILY
    pts = [[0, 0], [600, 0], [600, 600], [0, 600]]
    snapshot = {"entities": [dict(type="LWPOLYLINE", handle="c", layer="0",
                                   points=pts, closed=True)], "derived": []}
    ir = IR.build(snapshot, scale={"thickness": 200, "noise": 2})
    families = FAMILY.discover(ir)["families"]
    assert families
    for family in families:
        family.update(confirmed=True, category="column", verdict="keep",
                      model={"category": "column", "confidence": "high"})
    before = deepcopy(families)
    result = AG.run(ir=ir, families=families, ai={"mode": "off"},
                    profile="openings_columns")
    assert result["resolution"]["final"]["counts"].get("column", 0) == 0
    assert result["families"]["confirmed"] == []
    assert families == before


def test_controller_recheck_respects_remaining_call_budget(monkeypatch):
    from dwg_reader.recognition.v2 import proposal_engines as PE
    pending = [column(id=str(i)) for i in range(4)]
    monkeypatch.setattr(PE, "generate", lambda *a, **kw: {"hypotheses": pending})
    calls = []

    def ask(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(ok=False, code="TEST_MODEL_ERROR", images=0, data={})

    result = AG.run(ir={}, gateway=SimpleNamespace(available=True, ask=ask),
                    ai={"mode": "enhance"}, profile={"profile": "openings_columns",
                    "families": False, "detector": False, "arbitration": False},
                    budget={"max_model_calls": 1, "max_recheck": 4})
    assert len(calls) == 1
    assert not result["resolution"]["final"]["selected"]
