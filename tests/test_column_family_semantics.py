"""Model family semantics must precede column instance geometry validation."""
from copy import deepcopy

import pytest

from dwg_reader.recognition.v2 import cad_ir as IR
from dwg_reader.recognition.v2 import component_family as FAM
from dwg_reader.recognition.v2 import instance_propagator as PROP


def _fixture():
    entities = []
    for i, layer in enumerate(("COL", "0", "FURN")):
        x = i * 4000
        entities.append({"handle": f"C{i}", "layer": layer, "type": "LWPOLYLINE",
                         "points": [[x, 0], [x+500, 0], [x+500, 500], [x, 500]],
                         "closed": True})
    ir = IR.build({"entities": entities, "derived": []},
                  scale={"thickness": 200, "noise": 2})
    rules = {"columns": [{"srcs": [{"handle": "C0"}]}, {"handle": "C1"}]}
    families = FAM.discover(ir, rules=rules, kinds=("rule",))["families"]
    family = families[0]
    family.update(category="column", confirmed=True,
                  model={"category": "column", "confidence": "high"})
    return ir, family, rules


def test_rule_sources_and_single_handle_survive_discovery():
    ir, family, rules = _fixture()
    assert family["n"] == 2
    assert family["layers"] == ["0", "COL"]
    assert family["explained_ratio"] == 0
    assert {i["handle"] for i in FAM.rule_instances(ir, rules=rules)} == {"C0", "C1"}
    combined = FAM.rule_instances(ir, rules={"columns": [
        {"handles": ["C0"], "srcs": [{"handle": "C1"}], "handle": "C2"}]})
    assert len(combined[0]["uids"]) == 3


def test_confirmed_rule_family_propagates_exact_cross_layer_members():
    ir, family, _ = _fixture()
    # Duplicate member with reordered/duplicated uids must not add an instance.
    duplicate = deepcopy(family["instances"][0])
    duplicate["uids"] *= 2
    family["instances"].append(duplicate)
    result = PROP.propagate(family, ir=ir)
    assert result["stats"]["searched"] == 2
    assert len(result["hypotheses"]) == 2
    assert {h for hyp in result["hypotheses"] for h in hyp["source_handles"]} == {"C0", "C1"}
    assert result["stats"]["authorized"] == 1
    assert result["stats"]["pending"] == 1
    assert family["authorization_scope"] == "representative_only"
    for hyp in result["hypotheses"]:
        evidence = hyp["evidence"]
        authorized = set(hyp["uids"]) == set(family["representative"]["uids"])
        assert evidence["category_confirmed"] is authorized
        assert evidence["scope"] == "representative_only"
        assert evidence["category_basis"] == ("model_family" if authorized else "unconfirmed")
        if not authorized:
            assert hyp["status"] == "pending"
        assert evidence["model_confidence"] == "high"
        assert evidence["family"] == family["id"]
        assert evidence["family_instance"]
        assert evidence["family_representative"]
        assert hyp["geometry"]["outline"]


@pytest.mark.parametrize("patch", [
    {"confirmed": False}, {"confirmed": "true"}, {"model": {}},
    {"model": {"category": "column", "confidence": "unknown"}},
    {"model": {"category": "column", "confidence": "low"}},
    {"model": {"category": "column", "confidence": "medium"}},
    {"model": {"category": "none", "confidence": "high"}},
    {"category": "unknown"}, {"verdict": "reject"},
])
def test_unconfirmed_or_invalid_model_cannot_propagate_columns(patch):
    ir, family, _ = _fixture()
    family.update(patch)
    assert not PROP.propagate(family, ir=ir, category="column")["hypotheses"]


def test_category_guess_alone_cannot_create_column():
    ir, family, _ = _fixture()
    for key in ("confirmed", "model", "category"):
        family.pop(key)
    family["category_guess"] = "column"
    assert not PROP.propagate_families([family], ir=ir)["hypotheses"]


def test_model_confirmation_does_not_bypass_geometry_fitter(monkeypatch):
    ir, family, _ = _fixture()
    monkeypatch.setattr(PROP, "fit_one", lambda *args, **kwargs: None)
    result = PROP.propagate(family, ir=ir)
    assert not result["hypotheses"]
    assert {r["why"] for r in result["rejected"]} == {"FIT_FAILED"}


def test_unknown_family_is_available_for_observation():
    family = {"id": "unknown", "category_guess": "unknown", "priority": 1}
    assert FAM.select_for_observation([family])[0]["family"] == family


def test_rule_candidate_cache_does_not_mix_family_members():
    ir, family, _ = _fixture()
    other = {"instances": family["instances"][1:]}
    assert len(PROP.candidate_instances(ir, "rule", family=family)) == 2
    assert len(PROP.candidate_instances(ir, "rule", family=other)) == 1


def test_v1_list_evidence_does_not_crash_or_confirm_category():
    ir, _, rules = _fixture()
    for rec in rules["columns"]:
        rec["evidence"] = [{"rule": "closed_contour"}, "wall contact"]
    found = FAM.discover(ir, rules=rules, kinds=("rule",))
    assert found["families"]
    assert all(f["explained_ratio"] == 0 for f in found["families"])


@pytest.mark.parametrize("kind", ["rule", "contour"])
def test_physical_size_and_wall_context_split_families_without_layer_gate(kind):
    entities = []
    for i, (size, y, layer) in enumerate(((500, 0, "COL"), (500, 0, "0"),
                                         (900, 0, "COL"), (500, 5000, "COL"))):
        x = i * 4000
        entities.append({"handle": f"P{i}", "layer": layer, "type": "LWPOLYLINE",
                         "points": [[x, y], [x+size, y], [x+size, y+size], [x, y+size]],
                         "closed": True})
    ir = IR.build({"entities": entities, "derived": []},
                  scale={"thickness": 200, "noise": 2})
    rules = {"columns": [{"handle": e["handle"], "evidence": []} for e in entities]}
    walls = [{"id": "w", "start": [-1000, 250], "end": [20000, 250], "thickness": 200}]
    found = FAM.discover(ir, rules=rules, walls=walls, kinds=(kind,))
    assert sorted(f["n"] for f in found["families"]) == [1, 1, 2]
    pair = next(f for f in found["families"] if f["n"] == 2)
    assert pair["layers"] == ["0", "COL"]


def test_observation_budget_covers_categories_before_extra_high_priority_windows():
    families = [{"id": str(i), "category_guess": cat, "priority": priority}
                for i, (cat, priority) in enumerate([
                    ("window", 1), ("window", .99), ("window", .98),
                    ("column", .8), ("door", .7), ("unknown", .1)])]
    selected = FAM.select_for_observation(families, limit=4)
    assert {r["why"]["category"] for r in selected} == {"window", "column", "door", "unknown"}
    assert [r["family"]["id"] for r in FAM.select_for_observation(families, limit=2)] == ["0", "1"]
    assert FAM.select_for_observation(families, limit=5)[-1]["family"]["id"] == "1"
