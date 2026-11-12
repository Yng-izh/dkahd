"""Tests for Tianzheng (天正) custom object detection."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import _dxf

from dwg_reader.dxf import parse_dxf
from dwg_reader.pipeline import ReadOptions, read_drawing, resolve_dxf
from dwg_reader.tianzheng import TianzhengReport, detect_tianzheng, tianzheng_markdown

PROXY_HINT = '"TCH_KERNAL|缺乏解释器天正图形看不见|网址: www.tangent.com.cn"'


def _class_block(entries: list[tuple[str, str, str]]) -> str:
    lines: list[str] = []
    for dxf_name, cpp_name, app in entries:
        lines.extend(["  0", "CLASS", "  1", dxf_name, "  2", cpp_name, "  3", app, " 90", "0"])
    return "\n".join(lines)


def _drawing(tmp_path: Path, *, proxy: int = 0, text: int = 1, tianzheng: bool = True):
    classes = (
        _class_block([
            ("TCH_TEXT", "TDbText", PROXY_HINT),
            ("TCH_DIMENSION", "TDbDimension", PROXY_HINT),
            ("TCH_AXIS_LABEL", "TDbAxisLabel", PROXY_HINT),
        ])
        if tianzheng
        else ""
    )
    body: list[str] = []
    for index in range(proxy):
        body.extend(["  0", "ACAD_PROXY_ENTITY", "  8", "0", " 90", "0"])
    for index in range(text):
        body.extend([
            "  0", "TEXT", "  8", "0", " 10", "0.0", " 20", f"{index}.0", "  1", f"文字{index}",
        ])
    sections = ["  0", "SECTION", "  2", "ENTITIES", *body, "  0", "ENDSEC", "  0", "EOF"]
    if classes:
        sections = [
            "  0", "SECTION", "  2", "CLASSES", *classes.split("\n"), "  0", "ENDSEC", *sections,
        ]
    path = tmp_path / "tz.dxf"
    path.write_text(_dxf("\n".join(sections)), encoding="utf-8")
    return path


def _model(tmp_path: Path, **kwargs):
    return parse_dxf(_drawing(tmp_path, **kwargs))


def test_no_tianzheng_detected(tmp_path):
    report = detect_tianzheng(_model(tmp_path, tianzheng=False))
    assert report.detected is False
    assert report.severity == "none"
    assert "未检测到天正" in tianzheng_markdown(report)


def test_tianzheng_without_proxy_is_info(tmp_path):
    report = detect_tianzheng(_model(tmp_path, proxy=0))
    assert report.detected is True
    assert report.severity == "info"
    assert report.class_count == 3


def test_many_proxy_entities_raise_severe(tmp_path):
    report = detect_tianzheng(_model(tmp_path, proxy=250, text=5))
    assert report.proxy_entities == 250
    assert report.severity == "severe"
    assert "无法解析" in report.summary
    assert any("图形导出" in advice for advice in report.advice)


def test_few_proxy_entities_raise_warning(tmp_path):
    report = detect_tianzheng(_model(tmp_path, proxy=25, text=100))
    assert report.severity == "warning"


def test_app_names_and_classes_are_clean(tmp_path):
    report = detect_tianzheng(_model(tmp_path, proxy=250))
    assert report.apps == ["TCH_KERNAL"]
    assert "天正文字" in report.important_classes
    assert len(report.important_classes) == len(set(report.important_classes))


def test_markdown_lists_advice(tmp_path):
    markdown = tianzheng_markdown(detect_tianzheng(_model(tmp_path, proxy=250)))
    assert "## " not in markdown
    assert "TCH_TEXT" in markdown
    assert "EXPLODE" in markdown


def test_report_includes_warning_banner(tmp_path):
    drawing_path = _drawing(tmp_path, proxy=250)
    result = read_drawing(drawing_path)
    assert "天正图纸警告" in result.markdown
    assert "## 天正图纸检测" in result.markdown
    payload = result.payload["tianzheng"]
    assert payload["detected"] is True
    assert payload["severity"] == "severe"


def test_detection_can_be_disabled(tmp_path):
    drawing_path = _drawing(tmp_path, proxy=250)
    result = read_drawing(drawing_path, ReadOptions(detect_tianzheng=False))
    assert "tianzheng" not in result.payload
    assert "## 天正图纸检测" not in result.markdown


def test_t3_files_are_treated_as_dwg(tmp_path, monkeypatch):
    t3 = tmp_path / "plan.t3"
    t3.write_bytes(b"AC1032")
    captured = {}

    def fake_convert(source, output_dir, **kwargs):
        captured["source"] = Path(source)
        target = Path(output_dir) / f"{Path(source).stem}.dxf"
        target.write_text("  0\nEOF\n", encoding="utf-8")
        return {"output": str(target)}

    monkeypatch.setattr("dwg_reader.pipeline.convert_dwg_file", fake_convert)
    resolved = resolve_dxf(t3)
    assert resolved.suffix == ".dxf"
    assert captured["source"] == t3


def test_unsupported_suffix_message(tmp_path):
    path = tmp_path / "note.txt"
    path.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match=r"\.t3"):
        resolve_dxf(path)


def test_detect_reports_proxy_objects_field():
    report = TianzhengReport(detected=True, proxy_entities=3, proxy_objects=2)
    assert report.proxy_total == 5
    assert report.to_dict()["proxy_total"] == 5
