"""Authorization scope is independent of category and geometric representation."""
from copy import deepcopy

import pytest

from dwg_reader.recognition.v2 import instance_propagator as P
from dwg_reader.recognition.v2.fitters import _common as C


def fixture(category, representation="hatch", scope="representative_only"):
    entities, instances = [], []
    for i in range(2):
        x = i * 5000
        pts = [[x, 0], [x+500, 0], [x+500, 500], [x, 500]]
        if category == "window":
            pts = [[x, 0], [x+1200, 0], [x+1200, 60], [x, 60]]
        if category == "door":
            rows = [{"kind": "arc", "geom": {"center": [x, 0], "radius": 900,
                     "start_deg": 0, "end_deg": 90}, "bbox": [x, 0, x+900, 900]}]
        elif representation == "hatch":
            rows = [{"kind": "hatch", "geom": {"loops": [pts]},
                     "bbox": [x, 0, pts[2][0], pts[2][1]]}]
        else:
            rows = [{"kind": "poly", "geom": {"pts": pts, "closed": True},
                     "bbox": [x, 0, pts[2][0], pts[2][1]]}]
            if representation == "block":
                rows.append({"kind": "insert", "geom": {}, "bbox": rows[0]["bbox"]})
        for j, row in enumerate(rows):
            row.update(uid=f"{i}:{j}", handle=f"H{i}:{j}", layer="0")
        entities.extend(rows)
        instances.append({"id": f"i{i}", "uids": [e["uid"] for e in rows],
                          "bbox": C.bbox_of(rows)})
    ir = {"entities": entities, "basis": {"thickness": 200, "noise": 2}}
    family = {"id": "f", "kind": "rule", "instances": instances,
              "representative": instances[0], "n": 2, "category": category,
              "confirmed": True, "authorization_scope": scope,
              "model": {"category": category, "confidence": "high"},
              "signature": C.shape_signature(entities[:len(entities)//2])}
    return ir, family


@pytest.mark.parametrize("representation", ["hatch", "block"])
@pytest.mark.parametrize("scope", ["representative_only", "family"])
def test_column_scope_with_real_geometry(representation, scope):
    ir, family = fixture("column", representation, scope)
    result = P.propagate(family, ir=ir)
    assert len(result["hypotheses"]) == 2
    assert all(h["geometry"]["outline"] for h in result["hypotheses"])
    assert result["stats"]["authorized"] == (1 if scope == "representative_only" else 2)
    assert result["stats"]["pending"] == (1 if scope == "representative_only" else 0)
    sibling = result["hypotheses"][1]
    assert sibling["evidence"]["category_basis"] == (
        "unconfirmed" if scope == "representative_only" else "model_family")


@pytest.mark.parametrize("category", ["door", "window", "wall"])
def test_every_category_overrides_inherited_semantics(monkeypatch, category):
    ir, family = fixture(category)
    # Exercise the propagation boundary even if a fitter supplies stale semantics.
    monkeypatch.setattr(P, "fit_one", lambda cat, inst, **kw: {
        "category": cat, "geometry": {"bbox": inst["bbox"]}, "score": .9,
        "status": "candidate", "evidence": {
            "category_confirmed": True, "category_basis": "model_family"}})
    before = deepcopy(family)
    result = P.propagate(family, ir=ir)
    assert len(result["hypotheses"]) == 2
    rep, sibling = result["hypotheses"]
    assert rep["evidence"]["category_confirmed"] is True
    assert sibling["status"] == "pending"
    assert sibling["evidence"]["category_confirmed"] is False
    assert sibling["evidence"]["category_basis"] == "unconfirmed"
    assert sibling["evidence"]["pending_reason"] == "MEMBER_REQUIRES_MODEL_REVIEW"
    assert family == before


def test_family_scope_cannot_relax_plain_column_geometry():
    ir, family = fixture("column", "poly", "family")
    result = P.propagate(family, ir=ir)
    assert result["stats"]["authorization_scope"] == "representative_only"
    assert result["stats"]["authorized"] == 1
    assert result["stats"]["pending"] == 1


def test_model_scope_cannot_be_widened_by_family():
    ir, family = fixture("column", "hatch", "family")
    family["model"]["authorization_scope"] = "representative_only"
    result = P.propagate(family, ir=ir)
    assert result["stats"]["authorized"] == 1
    assert result["stats"]["pending"] == 1


@pytest.mark.parametrize("category", ["door", "window"])
@pytest.mark.parametrize("scope", ["representative_only", "family"])
def test_opening_scope_with_real_fitters(category, scope):
    ir, family = fixture(category, "poly", scope)
    result = P.propagate(family, ir=ir)
    assert len(result["hypotheses"]) == 2
    assert result["stats"]["authorized"] == (1 if scope == "representative_only" else 2)
    assert result["stats"]["pending"] == (1 if scope == "representative_only" else 0)
    assert all(h["evidence"]["geometry_status"] == "verified"
               for h in result["hypotheses"])


@pytest.mark.parametrize("category", ["column", "door", "window"])
def test_representative_rejection_is_not_family_rejection(category):
    ir, family = fixture(category)
    family.update(verdict="reject", confirmed=False)
    result = P.propagate(family, ir=ir)
    assert result["stats"]["status"] == "unconfirmed"
    assert result["rejected"] == []
    assert result["hypotheses"] == []


def test_family_rejection_still_applies_to_whole_family():
    ir, family = fixture("column", "hatch", "family")
    family.update(verdict="reject", confirmed=False)
    assert P.propagate(family, ir=ir)["stats"]["status"] == "model_rejected"
