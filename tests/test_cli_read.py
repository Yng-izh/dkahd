"""End-to-end tests for the ``dwg-reader read`` / ``quantity`` commands."""

from __future__ import annotations

import json

import pytest
from conftest import _dxf

from dwg_reader.cli import main

DRAWING = r"""\
  0
SECTION
  2
ENTITIES
  0
INSERT
  2
DOOR_900
  8
DOORS
 10
0.0
 20
0.0
  0
INSERT
  2
DOOR_900
  8
DOORS
 10
10.0
 20
0.0
  0
TEXT
  8
NOTES
 10
0.0
 20
50.0
  1
一层平面图
  0
MTEXT
  8
NOTES
 10
0.0
 20
40.0
  1
\S3/2建筑面积
  0
TEXT
  8
NOTES
 10
0.0
 20
30.0
  1
%%C20@200
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
3600.0
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
3.0
 21
4.0
  0
ENDSEC
  0
EOF
"""


@pytest.fixture()
def drawing_file(tmp_path):
    path = tmp_path / "plan.dxf"
    path.write_text(_dxf(DRAWING), encoding="utf-8")
    return path


def test_read_markdown_output(drawing_file, capsys):
    assert main(["read", str(drawing_file)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# 图纸解析报告")
    assert "一层平面图" in out
    # MTEXT stacked fraction and percent escapes are decoded.
    assert "3/2建筑面积" in out
    assert "Ø20@200" in out


def test_read_writes_report_file(drawing_file, tmp_path):
    target = tmp_path / "out" / "report.md"
    assert main(["read", str(drawing_file), "-o", str(target)]) == 0
    assert target.read_text(encoding="utf-8").startswith("# 图纸解析报告")


def test_read_json_payload(drawing_file, capsys):
    assert (
        main(["read", str(drawing_file), "--format", "json", "--dimensions", "--components"]) == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["text_count"] == 3
    assert payload["drawing"]["entity_types"]["INSERT"] == 2
    assert payload["dimensions"]["count"] == 1
    assert payload["dimensions"]["dimensions"][0]["text"] == "3600.00"
    components = {group["name"]: group["count"] for group in payload["components"]["by_type"]}
    assert components["门"] == 2


def test_read_table_mode(drawing_file, capsys):
    assert main(["read", str(drawing_file), "--table"]) == 0
    assert "文字标注提取" in capsys.readouterr().out


def test_read_layer_filter(drawing_file, capsys):
    assert main(["read", str(drawing_file), "--layer-filter", "WALL"]) == 0
    out = capsys.readouterr().out
    assert "一层平面图" not in out


def test_read_batch(drawing_file, tmp_path, capsys):
    source = drawing_file.parent / "drawings"
    source.mkdir()
    drawing_file.rename(source / drawing_file.name)
    out_dir = tmp_path / "reports"
    assert main(["read", "--batch", str(source), "--out-dir", str(out_dir)]) == 0
    index = out_dir / "index.md"
    assert index.is_file()
    assert "批量图纸解析总览" in index.read_text(encoding="utf-8")
    assert (out_dir / "plan.md").is_file()
    assert "批量图纸解析总览" in capsys.readouterr().out


def test_read_batch_requires_out_dir(drawing_file, tmp_path, capsys):
    assert main(["read", "--batch", str(drawing_file.parent)]) == 2
    assert "out-dir" in capsys.readouterr().err


def test_quantity_export(drawing_file, tmp_path):
    target = tmp_path / "quant.csv"
    assert main(["quantity", str(drawing_file), "-o", str(target)]) == 0
    text = target.read_text(encoding="utf-8-sig")
    assert "[构件清单]" in text
    assert "[图层几何量]" in text


def test_quantity_stdout(drawing_file, capsys):
    assert main(["quantity", str(drawing_file)]) == 0
    assert "算量清单" in capsys.readouterr().out


def test_missing_input_reports_error(tmp_path, capsys):
    assert main(["read", str(tmp_path / "nope.dxf")]) == 2
    assert "error:" in capsys.readouterr().err


def test_invalid_suffix_reports_error(tmp_path, capsys):
    path = tmp_path / "file.txt"
    path.write_text("x", encoding="utf-8")
    assert main(["read", str(path), "--no-convert"]) == 2
    assert "不支持的格式" in capsys.readouterr().err


def test_doctor_json(capsys):
    assert main(["doctor", "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert "backends" in report
    assert "oda" in report["backends"]
    assert report["platform"]


def test_no_command_prints_help(capsys):
    assert main([]) == 2
    assert "usage" in capsys.readouterr().err.lower()
