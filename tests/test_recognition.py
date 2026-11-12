"""构件识别引擎（dwg_reader.recognition）的回归测试。"""

from __future__ import annotations

import json

import ezdxf
import pytest

from dwg_reader.recognition import (build_document, recognize, render_markdown,
                                    schema_json, validate)

ezdxf = pytest.importorskip("ezdxf")


def _drawing(tmp_path):
    """4000×4000 房间：双线墙(200) + 柱 + 房间名/面积标注。"""
    doc = ezdxf.new("R2010")
    for name in ("WALL", "SPACE", "COLUMN"):
        doc.layers.add(name)
    msp = doc.modelspace()

    # 外墙双线：内 0..3600，外 200..3800（厚度 200）
    for (x1, y1, x2, y2) in [
        (200, 0, 3800, 0), (200, 200, 3800, 200),        # 南墙两面
        (200, 3800, 3800, 3800), (200, 3600, 3800, 3600),  # 北墙两面
        (0, 200, 0, 3800), (200, 200, 200, 3800),        # 西墙两面
        (3800, 200, 3800, 3800), (3600, 200, 3600, 3800),  # 东墙两面
    ]:
        msp.add_line((x1, y1), (x2, y2), dxfattribs={"layer": "WALL"})

    # 墙端封口（让配对判据走"有封口"分支）
    for (x1, y1, x2, y2) in [(0, 0, 200, 0), (3800, 0, 4000, 0),
                             (0, 3800, 200, 3800), (3800, 3800, 4000, 3800)]:
        msp.add_line((x1, y1), (x2, y2), dxfattribs={"layer": "WALL"})

    blk = doc.blocks.new("_FZH")
    blk.add_lwpolyline([(-0.5, -0.5), (0.5, -0.5), (0.5, 0.5), (-0.5, 0.5)], close=True)
    msp.add_blockref("_FZH", (2000, 2000),
                     dxfattribs={"layer": "COLUMN", "xscale": 400, "yscale": 400})

    msp.add_text("办公室", height=300,
                 dxfattribs={"layer": "SPACE", "insert": (1500, 1800)})
    msp.add_text("11.40m", height=200,
                 dxfattribs={"layer": "SPACE", "insert": (1500, 1500)})
    msp.add_text("2", height=100,
                 dxfattribs={"layer": "SPACE", "insert": (1900, 1560)})

    path = tmp_path / "room.dxf"
    doc.saveas(path)
    return path


def test_recognize_room_column_wall(tmp_path):
    result = recognize(str(_drawing(tmp_path)))
    counts = result["meta"]["counts"]
    assert counts.get("column") == 1
    assert counts.get("room") == 1
    assert counts.get("wall", 0) >= 4

    room = [c for c in result["components"] if c["category"] == "room"][0]
    assert 11.0 <= room["attributes"]["area_geom_m2"] <= 11.8
    assert room["attributes"]["label_inside_outline"] is True
    assert room["source_entities"][0]["handle"]

    wall = [c for c in result["components"] if c["category"] == "wall"][0]
    assert wall["attributes"]["thickness_mm"] == 200
    assert wall["geometry"]["outline"]


def test_schema_structure(tmp_path):
    path = _drawing(tmp_path)
    result = recognize(str(path))
    schema = build_document(result, str(path))

    assert schema["schema"] == "dwg-components-records/1.1"
    for key in ("meta", "basemap", "components", "unrecognized",
                "unmatched_blocks", "unmatched_total", "scope", "notes"):
        assert key in schema
    assert schema["meta"]["total_records"] == len(schema["components"])
    assert schema["meta"]["unrecognized_count"] == len(schema["unrecognized"])
    assert validate(schema)["ok"] is True

    bw = schema["basemap"]["width"]
    bh = schema["basemap"]["height"]
    for rec in schema["components"]:
        for key in ("id", "category", "bbox_model", "bbox_px", "bbox_center_px",
                    "dxf_handle", "anchor", "depth"):
            assert key in rec
        left, top, right, bottom = rec["bbox_px"]
        assert 0 <= left <= right <= bw
        assert 0 <= top <= bottom <= bh


def test_schema_definition_is_valid_json():
    doc = json.loads(schema_json())
    assert doc["$schema"].startswith("https://json-schema.org/")
    assert doc["$id"] == "dwg-components-records/1.1"
    assert "components" in doc["properties"]


def test_schema_file_in_sync():
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "schema" / "dwg-components-records-1.1.schema.json"
    assert path.is_file(), "schema 文件缺失"
    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(schema_json()), \
        "schema 文件与代码中的定义不一致，请重新导出"


def test_validate_rejects_bad_document():
    bad = {"schema": "dwg-components-records/1.1", "meta": {}, "components": []}
    out = validate(bad)
    assert out["ok"] is False
    assert any("meta" in e for e in out["errors"])


def test_markdown_report(tmp_path):
    result = recognize(str(_drawing(tmp_path)))
    md = render_markdown(result)
    assert "构件识别报告" in md
    assert "## 八、房间" in md
    assert "办公室" in md
