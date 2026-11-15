"""Column pending evidence survives the v1/v2 bridge and report boundary."""
from types import SimpleNamespace

import pytest

from dwg_reader.recognition import pipeline
from dwg_reader.recognition import v2
from dwg_reader.recognition.v2 import component_resolver as CR
from dwg_reader.recognition.v2 import proposal_engines as ENGINES


def _column(**changes):
    return dict({"bbox": [0, 0, 400, 400], "status": "pending",
                 "score": 0.7, "methods": ["contour"],
                 "evidence": ["closed_contour"],
                 "srcs": [{"handle": "C1", "layer": "0"}]}, **changes)


def _pool(column):
    result = ENGINES.rule_candidates({}, columns=[column])
    return {"hypotheses": result["candidates"], "unresolved": result["unresolved"]}


@pytest.mark.parametrize("reason,code", [
    ("missing_edge_single_method", "MISSING_EDGE_SINGLE_METHOD"),
    ("OUTSIDE_LAYER_NOT_ON_AXIS", "OUTSIDE_LAYER_NOT_ON_AXIS"),
    ("insufficient_geometry_evidence", "INSUFFICIENT_GEOMETRY_EVIDENCE"),
    (None, "UNKNOWN_PENDING_REASON"),
    ("", "UNKNOWN_PENDING_REASON"),
])
def test_pending_reason_is_preserved_without_inventing_missing_edges(reason, code):
    column = _column(status=" Pending ", pending_reason=reason)
    hyp = _pool(column)["hypotheses"][0]
    assert hyp["status"] == "pending"
    assert hyp["evidence"]["v1_status"] == " Pending "
    assert hyp["evidence"]["pending_reason"] == reason
    assert hyp["evidence"]["v1_reason"] == reason
    assert hyp["evidence"]["negatives"] == [{"code": code}]
    assert hyp["source_handles"] == ["C1"]
    assert column["status"] == " Pending "


def test_column_notes_and_source_layers_are_not_truncated():
    notes = [f"note_{i}" for i in range(10)]
    srcs = [{"handle": f"C{i}", "layer": f"layer_{i}"} for i in range(8)]
    column = _column(evidence=notes, srcs=srcs, layer="record_layer")
    evidence = _pool(column)["hypotheses"][0]["evidence"]
    assert evidence["notes"] == notes
    assert evidence["layers"] == [f"layer_{i}" for i in range(8)] + ["record_layer"]


def test_confirmed_column_does_not_gain_a_pending_negative():
    hyp = _pool(_column(status="confirmed"))["hypotheses"][0]
    assert hyp["status"] == "candidate"
    assert hyp["evidence"]["v1_status"] == "confirmed"
    assert hyp["evidence"]["negatives"] == []


@pytest.mark.parametrize("staged", [False, True])
def test_solver_pending_columns_are_reported_with_original_reason(staged):
    pool = _pool(_column(pending_reason="insufficient_geometry_evidence"))
    resolution = CR.staged(pool) if staged else CR.solve(pool)
    selected = resolution["final"]["selected"] if staged else resolution["selected"]
    assert selected == []
    report = v2._pending_report(resolution, pool)
    assert report["counts"] == {"column": 1}
    assert report["total"] == 1
    sample = report["sample"][0]
    assert sample["id"] == pool["hypotheses"][0]["id"]
    assert sample["category"] == "column"
    assert sample["why"] == "GEOMETRY_UNVERIFIED"
    assert sample["codes"] == ["INSUFFICIENT_GEOMETRY_EVIDENCE"]
    assert sample["pending_reason"] == "insufficient_geometry_evidence"
    assert sample["v1_status"] == "pending"
    assert sample["notes"] == ["closed_contour"]
    assert sample["layers"] == ["0"]


def test_pending_report_keeps_legacy_reason_and_solver_codes():
    pool = _pool(_column(pending_reason="missing_edge_single_method"))
    hid = pool["hypotheses"][0]["id"]
    resolution = {"rejected": [{"id": hid, "category": "column",
                                "why": "PENDING_UNVERIFIED", "codes": ["LEGACY_CODE"]}]}
    sample = v2._pending_report(resolution, pool)["sample"][0]
    assert sample["why"] == "PENDING_UNVERIFIED"
    assert sample["codes"] == ["LEGACY_CODE", "MISSING_EDGE_SINGLE_METHOD"]
    assert "pending_reason" not in resolution["rejected"][0]
    assert resolution["rejected"][0]["codes"] == ["LEGACY_CODE"]


def test_report_recovery_does_not_duplicate_rejected_or_report_selected():
    pool = _pool(_column())
    hyp = pool["hypotheses"][0]
    selected = {"final": {"selected": [hyp]}}
    assert v2._pending_report(selected, pool)["total"] == 0
    rejected = {"rejected": [{"id": hyp["id"], "category": "column",
                              "why": "PENDING_EVIDENCE"}]}
    assert v2._pending_report(rejected, pool)["total"] == 1


def test_analyze_without_agent_keeps_pending_details_and_unresolved_count():
    result = v2.analyze(snapshot={"entities": [], "derived": []},
                        columns=[_column(pending_reason="insufficient_geometry_evidence"),
                                 {"status": "pending"}],
                        with_agent=False, rooms=False, profile="openings_columns")
    assert result["final"]["counts"].get("column", 0) == 0
    assert result["pending"]["counts"] == {"column": 1}
    assert result["pending"]["unresolved"] == 1
    assert result["rejected"][0]["id"] == result["pending"]["sample"][0]["id"]
    assert result["rejected"][0]["codes"] == ["INSUFFICIENT_GEOMETRY_EVIDENCE"]


@pytest.mark.parametrize("verified", [{"columns": []}, [], {}, None])
def test_empty_verification_never_restores_raw_columns(monkeypatch, tmp_path, verified):
    out = {"load": {"snapshot": {}, "scale": {}},
           "columns": {"columns": [_column()]}, "columns_verify": verified}
    seen = {}
    monkeypatch.setattr(pipeline, "run_recognition", lambda *args, **kwargs:
                        SimpleNamespace(outputs=out))

    def analyze_stub(**kwargs):
        seen.update(kwargs)
        return {"ms": 0}

    monkeypatch.setattr(v2, "analyze", analyze_stub)
    drawing = tmp_path / "input.dxf"
    drawing.write_bytes(b"fixture input; parser is stubbed")
    result = v2.run_drawing(str(drawing), ai_config={"mode": "off"},
                            profile="openings_columns")
    assert seen["columns"] == []
    assert seen["rules"]["columns"] == []
    assert result["inputs"]["columns"] == 0
    assert result["base"]["counts"]["column"] == 0


@pytest.mark.parametrize("node", ["columns", "columns_verify"])
@pytest.mark.parametrize("wrapped", [False, True])
def test_column_inputs_and_base_counts_use_records_not_wrapper_keys(monkeypatch, tmp_path, node, wrapped):
    columns = [_column(handle=f"C{i}") for i in range(3)]
    value = {"columns": columns, "stats": {"fused": 3}} if wrapped else columns
    out = {"load": {"snapshot": {}, "scale": {}}, node: value}
    seen = {}
    monkeypatch.setattr(pipeline, "run_recognition", lambda *args, **kwargs:
                        SimpleNamespace(outputs=out))

    def analyze_stub(**kwargs):
        seen.update(kwargs)
        return {"ms": 0}

    monkeypatch.setattr(v2, "analyze", analyze_stub)
    drawing = tmp_path / "input.dxf"
    drawing.write_bytes(b"fixture input; parser is stubbed")
    result = v2.run_drawing(str(drawing), ai_config={"mode": "off"},
                            profile="openings_columns")
    assert seen["columns"] == columns
    assert seen["rules"]["columns"] == columns
    assert result["inputs"]["columns"] == 3
    assert result["base"]["counts"]["column"] == 3
    assert result["base"]["caliber"] == "candidate"


def test_export_counts_keep_the_export_caliber():
    assert v2._base_counts({"meta": {"counts": {"column": 7}},
                            "columns_verify": {"columns": []}}) == ({"column": 7}, "export")
