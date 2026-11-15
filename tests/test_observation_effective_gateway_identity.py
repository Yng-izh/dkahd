"""Offline regression coverage for effective observation gateway identity.

These tests intentionally fail until fingerprint-fix.patch is applied. All
requests use local SimpleNamespace fakes; no real gateway is constructed.
"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from dwg_reader.recognition.v2 import agent_controller as AG
from dwg_reader.recognition.v2 import multimodal_bootstrap as BOOT
from dwg_reader.recognition.v2 import multimodal_detector as DET
from dwg_reader.recognition.v2 import observation_state as OBS


GATEWAY_CHANGES = [
    ("model", "text-model-b"),
    ("vision_model", "vision-model-b"),
    ("max_calls", 7),
    ("max_ms", 90000.0),
    ("retries", 2),
    ("timeout", 15.0),
]


def fake_gateway():
    requests = []
    gateway = SimpleNamespace(
        available=True, model="text-model-a", vision_model="vision-model-a",
        max_calls=12, max_ms=180000.0, retries=1, timeout=60.0,
        calls=0, spent_ms=0.0, requests=requests,
    )

    def ask(**kwargs):
        requests.append(kwargs)
        gateway.calls += 1
        return {"status": "ok"}

    gateway.ask = ask
    return gateway


@pytest.fixture
def inputs():
    return dict(snapshot={"entities": [], "text": "offline drawing"}, ir={},
                families=[{"id": "f0"}], profile={}, budget={},
                ai={"mode": "propose", "budget": {"max_model_calls": 12},
                    "provider": {"model_env": "TEXT_MODEL",
                                 "vision_model_env": "VISION_MODEL"}})


@pytest.mark.parametrize("field,value", GATEWAY_CHANGES)
def test_effective_gateway_change_invalidates_state(inputs, field, value):
    gateway = fake_gateway()
    saved = OBS.make(OBS.fingerprint(**inputs, gateway=gateway), [], [], inputs["families"])
    setattr(gateway, field, value)
    with pytest.raises(ValueError, match="mismatch"):
        OBS.validate(saved, OBS.fingerprint(**inputs, gateway=gateway), inputs["families"])
    assert gateway.requests == []
    assert gateway.calls == 0


@pytest.mark.parametrize("selector", ["model_env", "vision_model_env"])
def test_provider_fallback_selector_invalidates_without_gateway(inputs, selector):
    before = OBS.fingerprint(**inputs)
    inputs["ai"]["provider"][selector] += "_OTHER"
    assert OBS.fingerprint(**inputs) != before


def test_ai_budget_invalidates_without_gateway(inputs):
    before = OBS.fingerprint(**inputs)
    inputs["ai"]["budget"]["max_model_calls"] = 7
    assert OBS.fingerprint(**inputs) != before


def test_gateway_is_optional_and_partial_fakes_are_supported(inputs):
    assert OBS.fingerprint(**inputs) == OBS.fingerprint(**inputs, gateway=None)
    assert OBS.fingerprint(**inputs, gateway=SimpleNamespace(available=True))


def test_secrets_urls_and_usage_are_not_identity(inputs, monkeypatch):
    gateway = fake_gateway()
    before = OBS.fingerprint(**inputs, gateway=gateway)
    # Only explicit nonsecret settings belong in identity, never arbitrary
    # gateway attributes, provider config, environment values, or live usage.
    inputs["ai"]["provider"].update(
        api_key="rotated-secret", base_url="https://other.invalid",
        api_key_env="OTHER_KEY", base_url_env="OTHER_URL", arbitrary="ignored")
    gateway.api_key = "other-secret"
    gateway.base_url = "https://gateway.invalid"
    gateway.calls = 4
    gateway.spent_ms = 500.0
    gateway.audit = [{"secret": "do-not-hash"}]
    monkeypatch.setenv("TEXT_MODEL", "not-read-directly")
    monkeypatch.setenv("VISION_MODEL", "not-read-directly-either")
    assert OBS.fingerprint(**inputs, gateway=gateway) == before
    assert gateway.requests == []


def test_legacy_schema_fails_closed(inputs):
    digest = OBS.fingerprint(**inputs)
    saved = OBS.make(digest, [], [], inputs["families"])
    saved["schema"] = "family-observation-state/1.0"
    with pytest.raises(ValueError, match="mismatch"):
        OBS.validate(saved, digest, inputs["families"])


@pytest.mark.parametrize("field,value", GATEWAY_CHANGES)
def test_agent_rejects_changed_gateway_before_requests(monkeypatch, field, value):
    """Exercise AG threading and ordering with real tracked fake requests."""
    stages = []

    def bootstrap(**kwargs):
        stages.append("bootstrap")
        return {"calls": 0, "items": [], "images_seen": 0}

    def read_family(family, **kwargs):
        stages.append("detector")
        kwargs["gateway"].ask(family=family["id"])
        return dict(status="ok", calls=1, category="door", confidence="high")

    monkeypatch.setattr(BOOT, "run", bootstrap)
    monkeypatch.setattr(DET, "read_family", read_family)
    monkeypatch.setattr(AG.ENGINES, "generate", lambda *a, **kw: {"hypotheses": []})
    gateway = fake_gateway()
    kwargs = dict(
        ir={}, snapshot={"entities": [], "text": "offline drawing"},
        families=[dict(id=f"f{i}", kind="block", category_guess="door", n=1,
                       priority=1, instances=[], bbox=[i, 0, i + 1, 1])
                  for i in range(3)],
        gateway=gateway, ai={"mode": "propose"},
        profile={"profile": "openings_columns", "arbitration": False,
                 "bootstrap_calls": 0, "detector_families": 1,
                 "detector_max_families": 3, "family_closeup": 0},
        budget={"max_model_calls": 1, "max_recheck": 0},
    )
    saved = deepcopy(AG.run(**kwargs)["observation_state"])
    assert len(gateway.requests) == gateway.calls == 1
    assert saved["attempted_ids"]
    # A new round uses a fresh gateway with exactly one changed effective
    # setting; remaining unseen families make accidental calls observable.
    changed = fake_gateway()
    setattr(changed, field, value)
    kwargs["gateway"] = changed
    stages.clear()
    with pytest.raises(ValueError, match="mismatch"):
        AG.run(**kwargs, observation_state=saved)
    assert stages == []
    assert changed.requests == []
    assert changed.calls == 0
