"""First-pass multimodal recognition: executable behavior with a fake VLM.

No network, API key, or test-set labels are needed. These tests verify that the
model actually sees images, that its grammars are geometrically fitted, and
that propose mode never commits model-generated candidates.
"""
from __future__ import annotations

import pytest

from dwg_reader.recognition.v2 import agent_controller as AGENT
from dwg_reader.recognition.v2 import cad_ir as IR
from dwg_reader.recognition.v2 import geometry_graph as GG
from dwg_reader.recognition.v2 import multimodal_bootstrap as BOOT


def _snapshot():
    ents = []
    # One drawn window: 1200-long double-line frame with short end connectors.
    for i, (a, b) in enumerate((
        ([1000, 1000], [2200, 1000]),
        ([1000, 1100], [2200, 1100]),
        ([1000, 1000], [1000, 1100]),
        ([2200, 1000], [2200, 1100]),
    )):
        ents.append({"handle": f"WIN{i}", "layer": "0", "type": "LINE",
                     "start": a, "end": b})
    # Other geometry ensures local tiles exist without needing an unexplained
    # case produced by the legacy recognition pipeline.
    for i in range(12):
        y = 200 + i * 350
        ents.append({"handle": f"S{i}", "layer": "MIXED", "type": "LINE",
                     "start": [0, y], "end": [4000, y]})
    return {"entities": ents, "derived": []}


class _Response:
    def __init__(self, data, images):
        self.ok = True
        self.data = data
        self.images = images
        self.code = ""
        self.detail = ""

    def as_dict(self):
        return {"ok": True, "images": self.images}


class _VisionGateway:
    available = True
    allow_remote_images = True

    def __init__(self):
        self.requests = []

    def ask(self, *, system, prompt, images=(), vision=False, **kwargs):
        self.requests.append({"prompt": prompt, "images": list(images),
                              "vision": vision})
        if "boot:overview" in prompt:
            return _Response({"scene": {"layout": "floor_plan",
                                         "suspected_patterns": ["window frame"]},
                              "grammars": []}, len(images))
        return _Response({"grammars": [{
            "category": "window", "subtype": "punched_window",
            "parts": {"frame": "parallel_line_pair"},
            "relations": [], "fit_mode": "constrained_geometry",
            "instances": "single"}]}, len(images))


def _ir_graph():
    ir = IR.build(_snapshot(), scale={"thickness": 200, "noise": 2})
    return ir, GG.Graph(GG.build(ir), ir)


def test_bootstrap_regions_do_not_require_residual_cases():
    ir, _ = _ir_graph()
    regions = BOOT.select_regions(ir)
    assert regions and regions[0]["id"] == "boot:overview"
    assert any(r["kind"] != "global" for r in regions)
    known = {e["uid"] for e in ir["entities"]}
    for region in regions:
        assert region["uids"]
        assert set(region["uids"]) <= known
        assert set(region["seed_uids"]) <= known


def test_initial_multimodal_bootstrap_sees_images_and_emits_valid_grammars():
    pytest.importorskip("PIL")
    ir, graph = _ir_graph()
    gateway = _VisionGateway()
    result = BOOT.run(
        ir=ir, graph=graph, snapshot=_snapshot(), gateway=gateway,
        ai={"mode": "enhance", "provider": {"vision": True}})
    assert result["calls"] >= 2
    assert result["scene"].get("layout") == "floor_plan"
    assert result["images_seen"] >= 1
    assert result["items"], "The VLM must propose before the recovery loop"
    assert any(req["vision"] and req["images"] for req in gateway.requests)
    assert all(g.get("hash") for item in result["items"] for g in item["grammars"])


def test_enhance_fits_bootstrap_hypotheses_into_a_real_candidate_pool():
    pytest.importorskip("PIL")
    ir, graph = _ir_graph()
    run = AGENT.run(
        ir=ir, graph=graph, snapshot=_snapshot(),
        gateway=_VisionGateway(),
        ai={"mode": "enhance", "provider": {"vision": True}},
        rooms=False, budget={"max_rounds": 1, "max_regions": 0,
                             "max_model_calls": 6})
    assert run["bootstrap"]["calls"] >= 2
    assert run["pool"]["engines"]["ai"]["stats"]["candidates"] >= 1
    candidates = [h for h in run["pool"]["hypotheses"] if h["category"] == "window"]
    assert candidates
    assert all(h.get("bbox") and h.get("uids") for h in candidates)


def test_propose_mode_does_not_apply_model_candidates():
    pytest.importorskip("PIL")
    ir, graph = _ir_graph()
    run = AGENT.run(
        ir=ir, graph=graph, snapshot=_snapshot(),
        gateway=_VisionGateway(),
        ai={"mode": "propose", "provider": {"vision": True}},
        rooms=False, budget={"max_rounds": 1, "max_regions": 0,
                             "max_model_calls": 6})
    assert any(r["grammars"] for r in run["bootstrap"]["regions"]), (
        "Propose must inspect the drawing, even though no AI proposal is committed")
    assert run["pool"]["engines"]["ai"]["stats"]["candidates"] == 0


def test_v2_full_instances_include_real_geometry():
    from dwg_reader.recognition import v2
    result = v2.analyze(
        snapshot=_snapshot(), scale={"thickness": 200, "noise": 2},
        walls=[{"id": "w1", "start": [0, 200], "end": [4000, 200],
                "thickness": 200, "handles": ["S0"]}],
        with_agent=False, rooms=False)
    instances = result["final"]["instances"]
    assert instances
    assert all("geometry" in h and "evidence" in h for h in instances)
    assert len(instances) == len(result["final"]["selected"])


def test_enhance_fails_before_base_recognition_when_model_not_configured(monkeypatch):
    from dwg_reader.recognition import v2
    from dwg_reader.recognition import model_gateway as MODEL
    from dwg_reader.recognition import pipeline as PIPE

    def no_base(*args, **kwargs):
        raise AssertionError("base recognition must not run before validation")

    monkeypatch.setattr(PIPE, "run_recognition", no_base)
    monkeypatch.setattr(MODEL, "gateway_from_config",
                        lambda cfg: type("GW", (), {
                            "available": False,
                            "allow_remote_images": False})())
    with pytest.raises(RuntimeError, match="multimodal model"):
        v2.run_drawing("not-needed.dxf", ai_mode="enhance",
                       ai_config={"mode": "enhance",
                                  "provider": {"vision": True,
                                               "allow_remote_images": True}})
