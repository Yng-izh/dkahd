"""Arbitration attempts consume budget even without a valid decision."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dwg_reader.recognition.v2 import agent_controller as AG
from dwg_reader.recognition.v2 import conflict_arbiter as ARB
from dwg_reader.recognition.v2 import multimodal_detector as DET


def point():
    return {"id": "pt:1", "reasons": [], "severity": 1,
            "candidates": [{"id": "a", "category": "door"},
                           {"id": "b", "category": "window"}]}


def gateway(*, ok=True, data=None, available=True):
    return SimpleNamespace(available=available, ask=Mock(return_value=SimpleNamespace(
        ok=ok, data=data or {}, images=0, code="MODEL_ERROR")))


@pytest.mark.parametrize("gw", [None, gateway(available=False)])
def test_unavailable_gateway_costs_zero(gw):
    result = ARB.arbitrate(point(), ir={}, gateway=gw)
    assert result["calls"] == 0
    assert result["status"] == "no_model"
    if gw is not None:
        gw.ask.assert_not_called()


def test_empty_point_costs_zero():
    gw = gateway()
    assert ARB.arbitrate({"candidates": []}, ir={}, gateway=gw)["calls"] == 0
    gw.ask.assert_not_called()


@pytest.mark.parametrize("ok,data,status", [
    (False, {}, "error"),
    (True, {"unexpected": "answer"}, "rejected"),
    (True, {"decisions": [{"label": "A", "keep": True}]}, "ok"),
])
def test_every_attempt_costs_one(ok, data, status):
    gw = gateway(ok=ok, data=data)
    result = ARB.arbitrate(point(), ir={}, gateway=gw)
    assert result["status"] == status
    assert result["calls"] == gw.ask.call_count == 1


def test_run_sums_attempts_separately_from_decisions(monkeypatch):
    points = [dict(point(), id=f"pt:{i}") for i in range(3)]
    monkeypatch.setattr(ARB, "conflict_points", lambda *a, **kw: {
        "points": points, "stats": {}, "deferred": []})
    gw = gateway()
    gw.ask.side_effect = [SimpleNamespace(ok=False, code="TIMEOUT"),
                          SimpleNamespace(ok=True, data={}),
                          SimpleNamespace(ok=True, data={"decision": "both"})]
    result = ARB.run([], ir={}, gateway=gw)
    assert result["stats"]["calls"] == gw.ask.call_count == 3
    assert result["stats"]["decided"] == 1
    assert result["stats"]["undecided"] == 2


@pytest.mark.parametrize("available", [False, True])
def test_run_without_attempts_reports_zero(monkeypatch, available):
    monkeypatch.setattr(ARB, "conflict_points", lambda *a, **kw: {
        "points": [] if available else [point()], "stats": {}, "deferred": []})
    gw = gateway(available=available)
    result = ARB.run([], ir={}, gateway=gw)
    assert result["stats"]["calls"] == 0
    gw.ask.assert_not_called()


@pytest.mark.parametrize("stats,expected", [
    ({"calls": 1, "decided": 0}, 1),
    ({"calls": 0, "decided": 1}, 0),
    ({"decided": 1}, 1),
])
def test_controller_charges_calls_with_legacy_fallback(monkeypatch, stats, expected):
    monkeypatch.setattr(AG.ENGINES, "generate", lambda *a, **kw: {"hypotheses": []})
    monkeypatch.setattr(ARB, "run", lambda *a, **kw: {
        "stats": stats, "verdicts": [], "status": "ok"})
    result = AG.run(ir={}, families=[], gateway=gateway(), ai={"mode": "enhance"},
                    profile={"profile": "openings_columns", "arbitration": True},
                    budget={"max_model_calls": 1, "max_recheck": 0})
    assert result["ai"]["calls"] == expected


def test_failed_arbitration_exhausts_budget_before_recheck(monkeypatch):
    rows = [dict(h, status="pending", score=0.5) for h in point()["candidates"]]
    monkeypatch.setattr(AG.ENGINES, "generate", lambda *a, **kw: {"hypotheses": rows})
    monkeypatch.setattr(ARB, "conflict_points", lambda *a, **kw: {
        "points": [point()], "stats": {}, "deferred": []})
    recheck = Mock(side_effect=AssertionError("budget already exhausted"))
    monkeypatch.setattr(DET, "recheck", recheck)
    gw = gateway(ok=False)
    result = AG.run(ir={}, families=[], gateway=gw, ai={"mode": "enhance"},
                    profile={"profile": "openings_columns", "arbitration": True},
                    budget={"max_model_calls": 1, "max_recheck": 1})
    assert result["ai"]["calls"] == gw.ask.call_count == 1
    assert result["arbitration"]["stats"]["decided"] == 0
    recheck.assert_not_called()
