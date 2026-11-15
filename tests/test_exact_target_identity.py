"""Exact snapshot identity survives repeated handles and skipped IR records."""
import base64
import io
from types import SimpleNamespace

import pytest

from dwg_reader.recognition import vision as V
from dwg_reader.recognition.v2 import cad_ir as IR
from dwg_reader.recognition.v2 import multimodal_detector as D

Image = pytest.importorskip("PIL.Image")
ImageFont = pytest.importorskip("PIL.ImageFont")


def line(handle, y, via=None):
    return dict(type="LINE", handle=handle, start=[10, y], end=[30, y], via=via)


@pytest.fixture(autouse=True)
def font(monkeypatch):
    monkeypatch.setattr(V, "_font", lambda size: ImageFont.load_default())


def test_overlapping_duplicate_handles_use_raw_order_and_exact_numbering():
    snapshot = {"entities": [{"type": "UNSUPPORTED"}, line("same", 20)],
                "derived": [line("same", 25), line("same", 30)]}
    ir = IR.build(snapshot)
    indices = D._indices_of(ir, [ir["entities"][1]["uid"]])
    assert indices == [2]  # raw position, not position in normalized entities
    case = {"image_bbox": [0, 0, 100, 100], "target_bbox": [10, 10, 30, 30],
            "source_handles": ["same"]}
    shot = V.render_case(snapshot, case, width=200, numbering=True, focus_indices=indices)
    assert (shot["focus"], shot["base"], shot["numbers"]) == (1, 2, 1)
    img = Image.open(io.BytesIO(base64.b64decode(shot["b64"])))
    for y in (20, 25, 30):
        assert img.getpixel((40, 200 - y * 2)) == V.COLORS["focus" if y == 25 else "base"]
    # Legacy callers retain their handle behavior; explicit empty selection does not.
    assert V.render_case(snapshot, case)["focus"] == 3
    assert V.render_case(snapshot, case, focus_indices=[])["focus"] == 0


def block_snapshot():
    return {"entities": [{"type": "INSERT", "handle": "A", "insert": [0, 0]},
                         {"type": "INSERT", "handle": "B", "insert": [0, 0]}],
            "derived": [line("shared-child", 20, {"insert": "A", "depth": 0}),
                        line("shared-child", 25, {"insert": "A", "depth": 0}),
                        line("shared-child", 30, {"insert": "B", "depth": 0})]}


def test_insert_only_uid_frames_and_numbers_its_children():
    snapshot = block_snapshot()
    ir = IR.build(snapshot)
    sample = {"id": "block", "uids": [ir["entities"][0]["uid"]], "bbox": [0, 0, 0, 0]}
    images, meta = D._sample_views([sample], ir=ir, snapshot=snapshot, walls=(),
                                  thickness=1, cfg={"images_per_family": 2})
    assert len(images) == 2
    assert all(m["focus"] == 2 and m["numbers"] == 2 and not m["focus_box"] for m in meta)
    close = D._closeup(sample, ir=ir, snapshot=snapshot, walls=(), thickness=1,
                       facts={}, cfg={"prompt_facts": 3600})
    assert close["request"]["images"][0]["focus"] == 2
    # Selecting a primitive does not expand its siblings, even with the same handle.
    views = D._render(snapshot, [0, 0, 40, 40], walls=(), thickness=1,
                      focus_handles=["shared-child"], focus_indices=[2])
    assert all(v["focus"] == v["numbers"] == 1 for v in views["views"])


@pytest.mark.parametrize("source_uids,expected", [(None, 3), (["e:00003"], 1), ([], 0)])
def test_recheck_prefers_source_uids_over_handle_fallback(source_uids, expected):
    snapshot = block_snapshot()
    ir = IR.build(snapshot)
    hyp = {"id": "candidate", "bbox": [0, 0, 40, 40], "source_handles": ["shared-child"]}
    if source_uids is not None:
        hyp["source_uids"] = source_uids
    gateway = SimpleNamespace(available=True, ask=lambda **kwargs: SimpleNamespace(
        ok=False, code="TEST", text="", images=len(kwargs["images"])))
    result = D.recheck([hyp], ir=ir, snapshot=snapshot, gateway=gateway)
    assert result["audit"][0]["request"]["images"][0]["focus"] == expected


def test_focus_indices_survive_image_size_retry(monkeypatch):
    snapshot = {"entities": [line("same", 20), line("same", 25)]}
    case = {"image_bbox": [0, 0, 100, 100], "target_bbox": [10, 10, 30, 30],
            "source_handles": ["same"]}
    original = V.render_case
    calls = []

    def capture(*args, **kwargs):
        calls.append(kwargs.get("focus_indices"))
        if len(calls) == 2:
            monkeypatch.setitem(V.RENDER, "max_bytes", 900000)
        return original(*args, **kwargs)

    monkeypatch.setattr(V, "render_case", capture)
    monkeypatch.setitem(V.RENDER, "max_bytes", 1)
    shot = V.render_case(snapshot, case, focus_indices=[1])
    assert calls == [[1], {1}]
    assert shot["focus"] == 1
