"""Tests for the streaming DXF model reader (no ezdxf required)."""

from __future__ import annotations

import math

import pytest
from conftest import _dxf

from dwg_reader.dxf import (
    IDENTITY,
    DxfFormatError,
    apply,
    compose,
    iter_placed,
    parse_dxf,
    summarize,
)

BASIC = r"""\
  0
SECTION
  2
HEADER
  9
$ACADVER
  1
AC1015
  0
ENDSEC
  0
SECTION
  2
ENTITIES
  0
TEXT
  8
NOTES
  7
CN
 10
10.0
 20
20.0
 40
2.5
  1
标高 %%P0.000
  0
MTEXT
  8
TEXT
 10
0.0
 20
0.0
  3
\S1/2
  1
倍率
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
CIRCLE
  8
WALL
 10
5.0
 20
5.0
 40
2.0
  0
LWPOLYLINE
  8
WALL
 70
1
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
ENDSEC
  0
EOF
"""

NESTED = r"""\
  0
SECTION
  2
BLOCKS
  0
BLOCK
  2
INNER
  8
0
 10
0.0
 20
0.0
  0
TEXT
  8
0
 10
1.0
 20
1.0
  1
内层
  0
ENDBLK
  0
BLOCK
  2
OUTER
  8
0
 10
0.0
 20
0.0
  0
INSERT
  2
INNER
  8
0
 10
10.0
 20
0.0
 41
2.0
 42
2.0
  0
ENDBLK
  0
ENDSEC
  0
SECTION
  2
ENTITIES
  0
INSERT
  2
OUTER
  8
DOOR
 10
100.0
 20
100.0
 41
3.0
 42
3.0
 50
0.0
 66
1
  0
ATTRIB
  8
DOOR
 10
100.0
 20
100.0
  1
M1021
  0
SEQEND
  0
ENDSEC
  0
EOF
"""


def _write(tmp_path, raw: str, name: str = "sample.dxf", encoding: str = "utf-8"):
    path = tmp_path / name
    path.write_text(_dxf(raw), encoding=encoding)
    return path


def test_parse_records_text_and_geometry(tmp_path):
    drawing = parse_dxf(_write(tmp_path, BASIC))
    assert drawing.version == "AC1015"
    assert drawing.counts["TEXT"] == 1
    assert drawing.counts["MTEXT"] == 1
    assert drawing.counts["LINE"] == 1
    assert drawing.counts["CIRCLE"] == 1
    assert drawing.counts["LWPOLYLINE"] == 1
    assert [layer.name for layer in drawing.layers] == []

    text = next(entity for entity in drawing.entities if entity.kind == "TEXT")
    assert text.layer == "NOTES"
    assert text.style == "CN"
    assert text.pos == (10.0, 20.0)
    assert text.height == 2.5
    assert text.text == "标高 %%P0.000"

    mtext = next(entity for entity in drawing.entities if entity.kind == "MTEXT")
    assert mtext.text == r"\S1/2倍率"


def test_parse_line_geometry(tmp_path):
    drawing = parse_dxf(_write(tmp_path, BASIC))
    line = next(entity for entity in drawing.entities if entity.kind == "LINE")
    assert line.points == [(0.0, 0.0), (3.0, 4.0)]
    circle = next(entity for entity in drawing.entities if entity.kind == "CIRCLE")
    assert circle.radius == 2.0
    polyline = next(entity for entity in drawing.entities if entity.kind == "LWPOLYLINE")
    assert polyline.closed is True
    assert len(polyline.points) == 3


def test_nested_blocks_and_attribs(tmp_path):
    drawing = parse_dxf(_write(tmp_path, NESTED))
    assert set(drawing.blocks) == {"INNER", "OUTER"}
    assert drawing.attribs == 1

    placed = list(iter_placed(drawing))
    texts = {item.text: item for item in placed if item.kind == "TEXT"}
    assert "内层" in texts
    # INNER text at (1,1) -> OUTER insert (10,0) scale 2 -> (12,2)
    # -> top INSERT (100,100) scale 3 -> (136, 106)
    assert texts["内层"].pos == pytest.approx((136.0, 106.0))
    assert texts["内层"].layer == "DOOR"

    attribs = [item for item in placed if item.kind == "ATTRIB"]
    assert attribs and attribs[0].text == "M1021"
    assert attribs[0].pos == pytest.approx((100.0, 100.0))


def test_insert_references_can_be_included(tmp_path):
    drawing = parse_dxf(_write(tmp_path, NESTED))
    with_inserts = list(iter_placed(drawing, include_inserts=True))
    blocks = [item.block for item in with_inserts if item.kind == "INSERT"]
    assert blocks == ["OUTER", "INNER"]


def test_layer_inheritance_uses_parent_layer(tmp_path):
    drawing = parse_dxf(_write(tmp_path, NESTED))
    placed = [item for item in iter_placed(drawing) if item.kind == "TEXT"]
    # Block entities live on layer "0" and inherit the INSERT layer.
    assert all(item.layer == "DOOR" for item in placed)


def test_tolerates_missing_eof(tmp_path):
    raw = _dxf(BASIC).replace("  0\nEOF", "").rstrip()
    path = tmp_path / "broken.dxf"
    path.write_text(raw, encoding="utf-8")
    drawing = parse_dxf(path)
    assert drawing.counts["TEXT"] == 1


def test_reads_gbk_encoded_file(tmp_path):
    body = _dxf(BASIC).replace("  9\n$ACADVER\n  1\nAC1015", "  9\n$DWGCODEPAGE\n  3\nANSI_936")
    path = tmp_path / "gbk.dxf"
    path.write_bytes(body.encode("gbk"))
    drawing = parse_dxf(path)
    text = next(entity for entity in drawing.entities if entity.kind == "TEXT")
    assert "标高" in text.text


def test_rejects_non_drawings(tmp_path):
    path = tmp_path / "empty.dxf"
    path.write_text("not a drawing\n", encoding="utf-8")
    with pytest.raises(DxfFormatError):
        parse_dxf(path)
    with pytest.raises(DxfFormatError):
        parse_dxf(tmp_path / "missing.dxf")


def test_keep_filter_drops_geometry(tmp_path):
    drawing = parse_dxf(_write(tmp_path, BASIC), keep={"TEXT", "MTEXT"})
    kinds = {entity.kind for entity in drawing.entities}
    assert kinds == {"TEXT", "MTEXT"}


def test_matrix_helpers():
    translate = (1.0, 0.0, 0.0, 1.0, 5.0, 7.0)
    scale = (2.0, 0.0, 0.0, 2.0, 0.0, 0.0)
    assert apply(compose(translate, scale), (1.0, 1.0)) == pytest.approx((7.0, 9.0))
    assert apply(IDENTITY, (3.0, 4.0)) == (3.0, 4.0)
    rotated = compose(IDENTITY, (0.0, 1.0, -1.0, 0.0, 0.0, 0.0))
    x, y = apply(rotated, (1.0, 0.0))
    assert math.isclose(x, 0.0, abs_tol=1e-9) and math.isclose(y, 1.0, abs_tol=1e-9)


def test_summarize_shape(tmp_path):
    summary = summarize(parse_dxf(_write(tmp_path, BASIC)))
    assert summary["version"] == "AC1015"
    assert summary["entity_count"] == 5
    assert summary["entity_types"]["TEXT"] == 1
    assert isinstance(summary["blocks"], list)
