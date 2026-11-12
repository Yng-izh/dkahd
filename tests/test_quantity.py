"""Tests for quantity take-off export."""

from __future__ import annotations

import csv
import io

import pytest
from conftest import _dxf

from dwg_reader.dxf import parse_dxf
from dwg_reader.quantity import (
    build_quantity,
    export,
    export_csv,
    layer_quantities,
    quantity_markdown,
)

DRAWING = r"""\
  0
SECTION
  2
ENTITIES
  0
LINE
  8
WALL
 10
0.0
 20
0.0
 11
3.0
 21
4.0
  0
LINE
  8
WALL
 10
0.0
 20
0.0
 11
0.0
 21
2.0
  0
LWPOLYLINE
  8
SLAB
 70
0
 10
0.0
 20
0.0
 10
3.0
 20
0.0
 10
3.0
 20
4.0
  0
CIRCLE
  8
SLAB
 10
9.0
 20
9.0
 40
1.5
  0
TEXT
  8
NOTES
 10
0.0
 20
0.0
  1
说明
  0
ENDSEC
  0
EOF
"""


def _dxf_path(tmp_path):
    path = tmp_path / "quant.dxf"
    path.write_text(_dxf(DRAWING), encoding="utf-8")
    return path


def test_layer_quantities_accumulate_lengths(tmp_path):
    rows = {row.layer: row for row in layer_quantities(parse_dxf(_dxf_path(tmp_path)))}
    wall = rows["WALL"]
    assert wall.line_count == 2
    assert wall.line_length == pytest.approx(7.0)
    slab = rows["SLAB"]
    assert slab.polyline_count == 1
    assert slab.polyline_length == pytest.approx(7.0)
    assert slab.circle_count == 1
    assert rows["NOTES"].text_count == 1


def test_build_quantity_reports_components_and_layers(tmp_path):
    report = build_quantity(_dxf_path(tmp_path))
    assert report.components is not None
    assert report.dimensions is not None
    sheets = report.sheets()
    assert "layers" in sheets
    assert {row["图层"] for row in sheets["layers"]} == {"WALL", "SLAB", "NOTES"}


def test_export_csv_is_excel_friendly(tmp_path):
    report = build_quantity(_dxf_path(tmp_path))
    target = export_csv(report, tmp_path / "out" / "quant.csv")
    raw = target.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM for Excel
    text = target.read_text(encoding="utf-8-sig")
    assert "[图层几何量]" in text
    parsed = list(csv.reader(io.StringIO(text)))
    assert any(row and row[0] == "图层" for row in parsed)


def test_export_infers_format_from_suffix(tmp_path):
    report = build_quantity(_dxf_path(tmp_path))
    target = export(report, tmp_path / "quant.csv")
    assert target.suffix == ".csv"
    assert target.is_file()


def test_export_xlsx_requires_openpyxl(tmp_path, monkeypatch):
    import builtins

    report = build_quantity(_dxf_path(tmp_path))
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "openpyxl":
            raise ImportError("no openpyxl")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ValueError, match="openpyxl"):
        export(report, tmp_path / "quant.xlsx")


def test_quantity_markdown_mentions_sections(tmp_path):
    report = build_quantity(_dxf_path(tmp_path))
    markdown = quantity_markdown(report)
    assert "图层几何量" in markdown
    assert "图纸单位" in markdown
