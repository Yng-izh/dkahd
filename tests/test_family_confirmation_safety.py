"""Controller regressions for bounded, scoped family confirmation."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from dwg_reader.recognition.v2 import agent_controller as AG
from dwg_reader.recognition.v2 import multimodal_detector as DET


def answer(category="door", confidence="high", **extra):
    return dict(status="ok", category=category, confidence=confidence,
                subtype="unknown", calls=1, images=2, raw={"category": category},
                raw_text="original model text", **extra)


def exercise(monkeypatch, answers, *, total=12, reserve=0, closeup=1,
             scope="family", count=1, bootstrap=0):
    from dwg_reader.recognition.v2 import multimodal_bootstrap as BOOT
    captured, requests = [], []
    remaining = iter(answers)

    def read(family, **kwargs):
        requests.append(deepcopy(kwargs["budget"]))
        result = deepcopy(next(remaining))
        if result.pop("use_closeup", False) and kwargs["budget"]["closeup"]:
            result["calls"] += 1
            result["closeup"] = {"request": {"prompt": "tight view"},
                                 "raw": {"category": "door"},
                                 "reply": {"ok": True}, "calls": 1,
                                 "status": "ok", "raw_text": "close text"}
        return result

    def generate(*args, **kwargs):
        captured.extend(deepcopy(kwargs["families"]))
        return {"hypotheses": []}

    monkeypatch.setattr(DET, "read_family", read)
    monkeypatch.setattr(AG.ENGINES, "generate", generate)
    monkeypatch.setattr(BOOT, "run", lambda **kw: {
        "calls": bootstrap, "items": [], "images_seen": 0})
    families = [{"id": f"fam:{i}", "kind": "block", "category_guess": "door",
                 "priority": 1, "n": 20, "authorization_scope": scope,
                 "representative": {"id": "rep", "uids": [], "bbox": [0, 0, 1, 1]},
                 "instances": [], "bbox": [0, 0, 1, 1]} for i in range(count)]
    result = AG.run(ir={}, families=families, snapshot={"entities": []},
                    gateway=SimpleNamespace(available=True), ai={"mode": "enhance"},
                    profile={"profile": "openings_columns", "arbitration": False,
                             "detector_families": 1, "detector_max_families": count,
                             "family_closeup": closeup},
                    budget={"max_model_calls": total, "max_recheck": reserve})
    return result, captured, requests


def test_no_budget_for_required_second_read_stays_pending(monkeypatch):
    result, families, requests = exercise(monkeypatch, [answer()], bootstrap=11)
    assert len(requests) == 1
    assert requests[0]["closeup"] == 0
    assert not families[0]["confirmed"]
    assert result["ai"]["calls"] == 12
    assert any(f.get("detail") == "CORROBORATION_BUDGET_UNAVAILABLE"
               for f in result["failures"])


@pytest.mark.parametrize("confidence", ["medium", "low"])
def test_insufficient_first_answer_never_spends_second_call(monkeypatch, confidence):
    result, families, requests = exercise(monkeypatch, [answer(confidence=confidence)])
    assert len(requests) == 1
    assert not families[0]["confirmed"]
    assert result["detector"]["calls"] == 1


def test_second_read_verifies_without_closeup_and_audits_actual_calls(monkeypatch):
    result, families, requests = exercise(
        monkeypatch, [answer(use_closeup=True), answer()])
    assert families[0]["confirmed"]
    assert requests[1]["verification"] is True
    assert requests[1]["closeup"] == 0
    assert result["detector"]["calls"] == result["ai"]["calls"] == 3
    assert result["model_audit"][0]["closeup"]["raw_text"] == "close text"
    assert result["model_audit"][1]["raw_text"] == "original model text"
    assert result["detector"]["readings"][0]["second_read"]["calls"] == 1


def test_explicit_zero_reserve_and_closeup_are_preserved(monkeypatch):
    result, families, requests = exercise(monkeypatch, [answer(), answer()],
                                          total=2, closeup=0)
    assert len(requests) == 2
    assert all(r["closeup"] == 0 for r in requests)
    assert result["detector"]["observation"]["reserved_calls"] == 0
    assert families[0]["confirmed"]


def test_first_closeup_cannot_consume_reserved_stage_calls(monkeypatch):
    result, families, requests = exercise(monkeypatch, [answer(use_closeup=True)],
                                          total=3, reserve=2)
    assert requests[0]["closeup"] == 0
    assert result["ai"]["calls"] == 1
    assert not families[0]["confirmed"]


@pytest.mark.parametrize("category", ["door", "window", "column", "none", "wall"])
@pytest.mark.parametrize("restriction", ["existing", "answer", "inconsistent", "outlier"])
def test_restricted_answers_neither_confirm_nor_reject_whole_family(
        monkeypatch, category, restriction):
    extra = {"authorization_scope": "representative_only"} if restriction == "answer" else {}
    if restriction == "inconsistent":
        extra["family_consistent"] = False
    if restriction == "outlier":
        extra["outliers"] = ["other-member"]
    _, families, requests = exercise(
        monkeypatch, [answer(category, **extra)],
        scope="representative_only" if restriction == "existing" else "family")
    assert len(requests) == 1
    assert not families[0]["confirmed"]
    assert families[0].get("verdict") != "reject"
    assert families[0]["authorization_scope"] == "representative_only"


@pytest.mark.parametrize("second", [answer(family_consistent=False),
                                     answer(outliers=["unseen"]),
                                     answer(authorization_scope="representative_only")])
def test_second_answer_can_only_narrow_scope(monkeypatch, second):
    _, families, requests = exercise(monkeypatch, [answer(), second])
    assert len(requests) == 2
    assert families[0]["authorization_scope"] == "representative_only"
    assert not families[0]["confirmed"]


@pytest.mark.parametrize("second", [answer("window"), answer(confidence="medium"),
                                     {"status": "error", "calls": 1}])
def test_second_disagreement_or_failure_never_confirms(monkeypatch, second):
    _, families, _ = exercise(monkeypatch, [answer(), second])
    assert not families[0]["confirmed"]


def test_zero_actual_calls_is_not_counted_as_one(monkeypatch):
    result, _, _ = exercise(monkeypatch, [{"status": "no_model", "calls": 0}])
    assert result["detector"]["calls"] == result["ai"]["calls"] == 0
