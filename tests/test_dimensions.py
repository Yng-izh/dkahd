"""Tests for dimension reading and geometry association."""

from __future__ import annotations

import pytest
from conftest import _dxf

from dwg_reader.dimensions import (
    DIM_TYPES,
    dimension_markdown,
    dimension_rows,
    dimension_type,
    nearest_geometry,
    read_dimensions,
)
from dwg_reader.dxf import parse_dxf

DIMENSIONS = r"""\
  0
SECTION
  2
ENTITIES
  0
DIMENSION
  8
DIM
 70
0
 11
5.0
 21
5.0
 42
3000.0
  1

  0
DIMENSION
  8
DIM
 70
4
 11
50.0
 21
50.0
  1
R500
  0
DIMENSION
  8
DIM
 70
35
 11
0.0
 21
0.0
  1

  0
LINE
  8
WALL
 10
0.0
 20
0.0
 11
10.0
 21
0.0
  0
CIRCLE
  8
WALL
 10
60.0
 20
50.0
 40
5.0
  0
ENDSEC
  0
EOF
"""


def _drawing(tmp_path):
    path = tmp_path / "dims.dxf"
    path.write_text(_dxf(DIMENSIONS), encoding="utf-8")
    return parse_dxf(path)


def test_dimension_type_mapping():
    assert dimension_type(0) == "线性"
    assert dimension_type(1) == "对齐"
    assert dimension_type(4) == "半径"
    # High bits carry flags; only the low nibble is the type.
    assert dimension_type(32 | 1) == "对齐"
    assert dimension_type(7).startswith("其他")


def test_measurement_used_when_text_missing(tmp_path):
    report = read_dimensions(_drawing(tmp_path))
    first = report.dimensions[0]
    assert first.type == "线性"
    assert first.text == "3000.00"
    assert first.measurement == pytest.approx(3000.0)


def test_override_text_is_decoded(tmp_path):
    report = read_dimensions(_drawing(tmp_path), decode=lambda value, _style: value.lower())
    assert report.dimensions[1].text == "r500"


def test_low_nibble_ignores_flag_bits(tmp_path):
    report = read_dimensions(_drawing(tmp_path))
    assert report.dimensions[2].type == "直径"


def test_nearest_geometry_returns_closest_first(tmp_path):
    report = read_dimensions(_drawing(tmp_path), neighbours=1)
    first = report.dimensions[0]
    assert first.related
    assert first.related[0].kind == "LINE"
    assert first.related[0].distance == pytest.approx(5.0, abs=0.6)


def test_nearest_geometry_respects_k():
    from dwg_reader.dxf import PlacedEntity

    geometry = [
        PlacedEntity(
            kind="LINE",
            layer="A",
            style="",
            text="",
            pos=(0.0, 0.0),
            points=((0.0, 0.0), (0.0, 0.0)),
        ),
        PlacedEntity(kind="CIRCLE", layer="B", style="", text="", pos=(100.0, 0.0), radius=1.0),
    ]
    assert len(nearest_geometry((0.0, 0.0), geometry, k=1)) == 1
    assert nearest_geometry((0.0, 0.0), [], k=2) == []
    assert nearest_geometry((0.0, 0.0), geometry, k=0) == []


def test_layer_filter_excludes_dimensions(tmp_path):
    import re

    report = read_dimensions(_drawing(tmp_path), layer_filter=re.compile("OTHER"))
    assert report.count == 0


def test_markdown_and_rows(tmp_path):
    report = read_dimensions(_drawing(tmp_path))
    markdown = dimension_markdown(report)
    assert "尺寸明细" in markdown
    rows = dimension_rows(report)
    assert len(rows) == 3
    assert rows[0]["类型"] in DIM_TYPES.values()
