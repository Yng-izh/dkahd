from __future__ import annotations

from pathlib import Path

import pytest

from dwg_reader.report import inspect_report

FIXTURE = Path(__file__).parent / "fixtures" / "architectural_excerpt.dxf"


def test_realistic_fixture_stream_contract():
    from dwg_reader.stream import extract_texts

    records = extract_texts(FIXTURE)
    assert [(item.text, item.x, item.y) for item in records] == [("Top Ø20", 0.0, 20.0)]


def test_report_expands_block_text_and_attributes():
    pytest.importorskip("ezdxf")
    report = inspect_report(FIXTURE, expand_blocks=True)
    texts = {item["text"]: item for item in report["texts"]}
    assert texts["A-101"]["entity"] == "ATTRIB"
    assert texts["ROOM"]["entity"] == "BLOCK_TEXT"
    assert texts["ROOM"]["x"] == pytest.approx(101.0)
    assert texts["ROOM"]["y"] == pytest.approx(202.0)
    assert report["drawing"]["entity_types"] == {"INSERT": 1, "TEXT": 1}

