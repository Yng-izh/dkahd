"""Layer metadata must not decide whether identical column geometry exists.

These regressions use actual line reconstruction/fusion, without CAD files or models.
"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from dwg_reader.recognition import elements as E, nodes as N


def _square(layers, x=0.0, y=0.0, size=600.0):
    pts = [(x, y), (x + size, y), (x + size, y + size), (x, y + size)]
    return [{"type": "LINE", "handle": str(i), "layer": layers[i],
             "start": list(pts[i]), "end": list(pts[(i + 1) % 4])}
            for i in range(4)]


def _context(entities, declared=("COLUMN",)):
    return SimpleNamespace(inputs={"load": {
        "snapshot": {"entities": entities, "derived": [], "blocks": {}},
        "plan": {"layers": {"column": list(declared), "axis": []}},
        "scale": None}}, params={"llm_review": False})


def _isolate_node(monkeypatch):
    monkeypatch.setattr(N, "_shape_blocks", lambda *a, **kw: {})
    monkeypatch.setattr(E, "_footprint_blocks", lambda *a, **kw: {})
    monkeypatch.setattr(N.OPS, "run", lambda *a, **kw: [])


def _signature(result):
    return sorted((tuple(c["bbox"]), c["status"], c["score"],
                   c.get("pending_reason"), tuple(c["methods"]))
                  for c in result["columns"])


@pytest.mark.parametrize("layers", [
    ["Detail Frame"] * 4,
    ["家具1", "COLUMN", "DIM", "HATCH"],
    ["random"] * 4,
])
def test_layer_renaming_and_split_edges_leave_geometry_unchanged(monkeypatch, layers):
    _isolate_node(monkeypatch)
    clean = _context(_square(["COLUMN"] * 4))
    mixed = _context(_square(layers))
    a, b = N.node_columns(clean), N.node_columns(mixed)
    assert len(a["columns"]) == len(b["columns"]) == 1
    assert _signature(a) == _signature(b)
    assert {s["layer"] for s in b["columns"][0]["srcs"]} == set(layers)
    wall = {"start": [-1000, 300], "end": [1600, 300],
            "thickness": 200, "status": "confirmed"}
    for ctx, candidates in ((clean, a), (mixed, b)):
        ctx.inputs.update(columns=candidates, walls_seg={"walls_seg": [wall]})
    va, vb = N.node_columns_verify(clean), N.node_columns_verify(mixed)
    assert _signature(va) == _signature(vb)
    assert va["columns"][0]["geometry_status"] == "verified"
    assert va["columns"][0]["status"] == "pending"
    assert vb["columns"][0]["pending_reason"] == "CATEGORY_UNVERIFIED"


def test_layers_present_only_in_derived_geometry_are_scanned(monkeypatch):
    _isolate_node(monkeypatch)
    ctx = _context([])
    ctx.inputs["load"]["snapshot"]["derived"] = _square(["inside-block"] * 4)
    assert len(N.node_columns(ctx)["columns"]) == 1


def _rec(method="lines", handles=("a", "b", "c", "d"), edges=4):
    return {"bbox": [0, 0, 600, 600], "outline": [[0, 0], [600, 0], [600, 600], [0, 600]],
            "w": 600, "h": 600, "center": [300, 300], "method": method,
            "edges": edges, "layer_scope": "outside_declared",
            "srcs": [{"handle": h, "type": method, "layer": "任意层"} for h in handles]}


def test_repeated_fusion_preserves_independent_evidence_and_input():
    records = [_rec(edges=3), _rec("hatch", ("hatch1",))]
    before = deepcopy(records)
    first = E.fuse_columns(records)
    first_copy = deepcopy(first)
    second = E.fuse_columns(first["columns"])
    assert records == before
    assert first == first_copy
    assert _signature(first) == _signature(second)
    assert second["columns"][0]["independent_methods"] is True
    assert set(second["columns"][0]["methods"]) == {"lines", "hatch"}
    assert second["columns"][0]["geometry_status"] == "verified"
    assert second["columns"][0]["pending_reason"] == "CATEGORY_UNVERIFIED"


def test_same_polyline_seen_twice_is_not_independent_support():
    rows = [_rec("polyline", ("p1",)), _rec("contour", ("p1",))]
    result = E.fuse_columns(rows)["columns"][0]
    assert result["independent_methods"] is False
    assert result["status"] == "pending"
    assert result["pending_reason"] == "CATEGORY_UNVERIFIED"


def test_geometry_gate_applies_equally_to_declared_and_other_layers():
    for scope in ("declared", "outside_declared", "global", "mixed"):
        isolated = E.fuse_columns([dict(_rec(), layer_scope=scope)])["columns"][0]
        assert isolated["pending_reason"] == "CATEGORY_UNVERIFIED"
        missing = E.fuse_columns([dict(_rec(edges=3), layer_scope=scope)],
                                 axis=([300], [300]))["columns"][0]
        assert missing["pending_reason"] == "missing_edge_single_method"


def test_contour_evidence_is_fused_instead_of_skipped(monkeypatch):
    _isolate_node(monkeypatch)
    monkeypatch.setattr(N.OPS, "run", lambda *a, **kw: [
        {"kind": "hatch_region", "bbox": [0, 0, 600, 600],
         "outline": [[0, 0], [600, 0], [600, 600], [0, 600]],
         "area": 360000, "handles": ["hatch1"], "layer": "HATCH"}])
    result = N.node_columns(_context(_square(["COLUMN"] * 4)))
    assert len(result["columns"]) == 1
    assert set(result["columns"][0]["methods"]) == {"lines", "hatch"}


def test_pending_overlap_cannot_suppress_verified_candidate():
    verified = dict(_rec(), status="confirmed", score=0.7)
    pending = dict(_rec(), bbox=[-50, -50, 650, 650], status="pending", score=1.0)
    assert E._suppress_overlapping_columns([pending, verified]) == [verified]


def test_first_stage_keeps_overlapping_alternatives_until_verification():
    small = _rec()
    large = dict(_rec(), bbox=[-100, -100, 700, 700], w=800, h=800)
    assert len(E.fuse_columns([small, large], suppress=False)["columns"]) == 2


def test_spatial_buckets_work_across_negative_coordinate_boundaries():
    entities = _square(["a", "b", "c", "d"], x=-604.9, y=-600.1)
    rows = E._rects_from_lines({"entities": entities, "derived": []}, {"a", "b", "c", "d"})
    assert len(rows) == 1
    assert rows[0]["edges"] == 4
    assert rows[0]["w"] == pytest.approx(600)


def test_pair_prefilter_preserves_endpoint_tolerance_at_minimum_size():
    lines = [{"type": "LINE", "layer": "mixed", "handle": str(i),
              "start": a, "end": b}
             for i, (a, b) in enumerate([
                 ([0, 0], [149, 0]), ([1, 300], [150, 300]), ([0, 0], [0, 300])])]
    rows = E._rects_from_lines({"entities": lines}, {"mixed"})
    assert len(rows) == 1
    assert rows[0]["bbox"] == [0, 0, 150, 300]
    assert rows[0]["edges"] == 3


def test_rotated_contour_uses_actual_section_and_real_hatch_kind():
    import math
    from dwg_reader.recognition import operators as OPS
    span = 600 / math.sqrt(2)
    outline = [[0, -span], [span, 0], [0, span], [-span, 0]]
    bbox = [-span, -span, span, span]
    prims = {"closed": [{"outline": outline, "bbox": bbox,
                         "n": 4, "handle": "p1", "layer": "random"}],
             "hatches": [{"loops": [outline], "handle": "h1", "layer": "random"}]}
    contours = OPS.closed_contour(prims, ["random"], 200, {})
    hatches = OPS.hatch_region(prims, ["random"], 200, {})
    assert len(contours) == len(hatches) == 1
    records = [N._column_from_contour(row, layer_scope="outside_declared")
               for row in contours + hatches]
    assert records[0]["w"] == pytest.approx(600)
    assert records[1]["h"] == pytest.approx(600)
    fused = E.fuse_columns(records)["columns"]
    assert len(fused) == 1
    assert set(fused[0]["methods"]) == {"contour", "hatch"}
    assert fused[0]["independent_methods"] is True
