"""Detector evidence integrity, without network or rendering dependencies."""
import base64
import hashlib
import json

import pytest

from dwg_reader.recognition.model_gateway import GatewayResult
from dwg_reader.recognition.v2 import multimodal_detector as D


PNG = b"\x89PNG\r\n\x1a\n" + b"image payload" * 400
B64 = base64.b64encode(PNG).decode("ascii")


def member(name, x=0, size=100):
    return {"id": name, "bbox": [x, 0, x + size, size], "uids": [],
            "block": "SECRET_BLOCK"}


def family():
    members = [member(str(i), i * 1000) for i in range(8)]
    return {"id": "f", "instances": members, "representative": members[0],
            "bbox": [0, 0, 8000, 100], "n": 8}


def answer(**extra):
    return dict({"component": {"category": "column", "subtype": "rect_column",
                               "confidence": "high"}, "family_consistent": True}, **extra)


class Gateway:
    available = True

    def __init__(self, *answers):
        self.answers = list(answers or [answer()])
        self.requests = []

    def ask(self, **request):
        self.requests.append(request)
        value = self.answers[min(len(self.requests) - 1, len(self.answers) - 1)]
        if value is None:
            return GatewayResult(ok=False, code="TIMEOUT", text="failure text")
        return GatewayResult(ok=True, data=value, text=json.dumps(value),
                             images=len(request["images"]))


@pytest.fixture(autouse=True)
def fake_geometry(monkeypatch):
    monkeypatch.setattr(D.FAM, "family_facts", lambda *a, **kw: {})
    monkeypatch.setattr(D, "IR_by_uid", lambda ir: {})
    monkeypatch.setattr(D, "_render", lambda *a, **kw: {"views": [
        {"status": "rendered", "b64": B64, "scale": scale, "bytes": 1}
        for scale in kw["scales"]]})


def read(fam=None, **kwargs):
    return D.read_family(fam or family(), ir={}, snapshot={"ok": True}, **kwargs)


def test_prompt_has_all_image_mappings_without_payload_or_block_names():
    gateway = Gateway()
    result = read(gateway=gateway)
    prompt = gateway.requests[0]["prompt"]
    assert B64 not in prompt
    assert "SECRET_BLOCK" not in prompt
    mappings = json.loads(prompt.split("GREY = surrounding geometry):\n")[1].split("\n\nScope")[0])
    assert len(mappings) == len(gateway.requests[0]["images"]) == 4
    assert [m["image"] for m in mappings] == [1, 2, 3, 4]
    for mapping, audit in zip(mappings, result["request"]["images"]):
        assert "b64" not in mapping
        assert audit["b64"] == B64
        assert audit["sha256"] == hashlib.sha256(PNG).hexdigest()
        assert audit["bytes"] == len(PNG)
    assert result["reply"]["text"] == result["raw_text"] == json.dumps(answer())


def test_diversity_compares_every_chosen_member():
    members = [member("a", size=100), member("b", size=200),
               member("duplicate-b", size=200), member("c", size=300)]
    selected = D.sample_instances({"instances": members, "representative": members[0]}, {})
    assert [s["id"] for s in selected] == ["a", "b", "c"]


def test_size_tolerance_is_relative_to_size():
    members = [member("a", size=10000), member("small-change", size=10100)]
    assert len(D.sample_instances({"instances": members, "representative": members[0]}, {})) == 1


def test_verification_uses_disjoint_holdouts():
    first = read(gateway=Gateway())
    primary_ids = [s["instance"] for s in first["samples"]]
    second = read(gateway=Gateway(), budget={"verification": True, "primary_sample_ids": primary_ids})
    assert second["status"] == "ok"
    assert set(primary_ids).isdisjoint(s["instance"] for s in second["samples"])
    implicit = read(gateway=Gateway(), budget={"verification": True})
    assert implicit["samples"] == second["samples"]


def test_verification_without_holdouts_does_not_call_model():
    fam = family()
    gateway = Gateway()
    result = read(fam, gateway=gateway, budget={"verification": True,
                  "primary_sample_ids": [s["id"] for s in fam["instances"]]})
    assert result["reason"] == "NO_HOLDOUT_MEMBERS"
    assert result["status"] == "error" and not gateway.requests


def test_verification_without_images_does_not_call_model(monkeypatch):
    monkeypatch.setattr(D, "_render", lambda *a, **kw: {"views": []})
    gateway = Gateway()
    result = read(gateway=gateway, budget={"verification": True})
    assert result["reason"] == "NO_HOLDOUT_IMAGES"
    assert not gateway.requests


@pytest.mark.parametrize("closeup_budget,second", [(0, answer()), (1, None),
                                                   (1, answer()),
                                                   (1, answer(verdict="keep", need_closeup=True))])
def test_unanswered_closeup_fails_closed(closeup_budget, second):
    result = read(gateway=Gateway(answer(need_closeup=True), second),
                  budget={"closeup": closeup_budget})
    assert result["status"] == "error"
    assert result["reason"] == "CLOSEUP_UNANSWERED"
    assert result["category"] is None


def test_closeup_keeps_first_answer_and_limits_authorization():
    first = answer(need_closeup=True)
    second = answer(verdict="keep")
    result = read(gateway=Gateway(first, second))
    assert result["status"] == "ok"
    assert result["authorization_scope"] == "representative_only"
    assert result["raw"] == first
    close = result["closeup"]
    assert close["raw"] == second
    assert close["raw_text"] == json.dumps(second)
    assert close["request"]["images"][0]["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert close["request"]["images"][0]["b64"] == B64


def test_missing_closeup_image_fails_closed(monkeypatch):
    original = D._render
    monkeypatch.setattr(D, "_render", lambda *a, **kw: {"views": []}
                        if kw["scales"] == ("closeup",) else original(*a, **kw))
    gateway = Gateway(answer(need_closeup=True))
    result = read(gateway=gateway)
    assert result["status"] == "error"
    assert result["closeup"]["reason"] == "NO_CLOSEUP_IMAGES"
    assert len(gateway.requests) == 1


def test_recheck_audits_exact_sent_images_and_raw_text():
    result = D.recheck([{"id": "candidate", "bbox": [0, 0, 10, 10]}],
                       ir={}, snapshot={"ok": True}, gateway=Gateway(answer(verdict="keep")))
    audit = result["audit"][0]
    assert audit["raw_text"] == json.dumps(answer(verdict="keep"))
    assert len(audit["request"]["images"]) == audit["request"]["image_count"] == 1
    assert audit["request"]["images"][0]["b64"] == B64
    assert audit["request"]["images"][0]["sha256"] == hashlib.sha256(PNG).hexdigest()
