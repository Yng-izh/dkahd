"""Explicit caller walltime limits survive the v2 profile defaults, offline."""

from copy import deepcopy
import hashlib
from types import SimpleNamespace
import json
import pytest

from dwg_reader.recognition import model_gateway, pipeline, v2


@pytest.mark.parametrize('limit, expected', [(60, 60), (180, 180), (None, 600)])
@pytest.mark.parametrize('as_file', [False, True])
def test_run_drawing_preserves_explicit_walltime_budget(monkeypatch, tmp_path, limit, expected, as_file):
    drawing_bytes = b"DXF fixture bytes; recognition is stubbed"
    drawing = tmp_path / "input.dxf"
    drawing.write_bytes(drawing_bytes)
    config = {"mode": "propose", "budget": {} if limit is None else {"max_walltime_seconds": limit}}
    original_config = deepcopy(config)
    gateway_configs = []
    gateways = []
    events = []
    analysis_calls = []

    def gateway_factory(config):
        events.append("gateway")
        # Snapshot each call so later mutation cannot hide a raised limit.
        gateway_configs.append(deepcopy(config))
        gateway = SimpleNamespace(describe=lambda: {"available": True})
        gateways.append(gateway)
        return gateway

    def recognition_stub(path, **kwargs):
        events.append("recognition")
        assert path == str(drawing)
        assert kwargs["ai_mode"] == "off"
        assert kwargs["llm_review"] is False
        return SimpleNamespace(outputs={"load": {"snapshot": {}, "scale": {}}})

    def analyze_stub(**kwargs):
        events.append("analyze")
        analysis_calls.append(kwargs)
        return {"ms": 0}

    monkeypatch.setattr(model_gateway, "gateway_from_config", gateway_factory)
    monkeypatch.setattr(pipeline, "run_recognition", recognition_stub)
    monkeypatch.setattr(v2, "analyze", analyze_stub)

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    result = v2.run_drawing(
        str(drawing), ai_config=str(config_path) if as_file else config, profile="openings_columns"
    )

    assert events == ["gateway", "recognition", "gateway", "analyze"]
    assert [cfg["budget"]["max_walltime_seconds"] for cfg in gateway_configs] == [expected, expected]
    assert len(analysis_calls) == 1
    analysis = analysis_calls[0]
    assert analysis["profile"]["walltime_seconds"] == 600
    assert analysis["ai"]["budget"]["max_walltime_seconds"] == expected
    assert analysis["gateway"] is gateways[1]
    assert analysis["drawing_identity"] == hashlib.sha256(drawing_bytes).hexdigest()
    assert config == original_config
    assert result["inputs"]["ai_mode"] == "propose"
    assert result["gateway"] == {"available": True}
