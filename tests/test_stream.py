"""流解析器集成测试：直接用原始 DXF 验证 #1-#7 各项能力。

这些测试不依赖 ezdxf，覆盖低内存解析路径（块展开/属性/尺寸/堆叠/表格/图层/繁中）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from conftest import run_script, STREAM, basic_dxf, block_attrib_dim_dxf, \
    dimension_measure_dxf, table_dxf, multilayer_dxf, big5_font_dxf


def _write(tmp_path, content):
    p = tmp_path / "sample.dxf"
    p.write_text(content, encoding="utf-8")
    return str(p)


def test_basic_text_and_mtext_stack(tmp_path):
    out = run_script(STREAM, _write(tmp_path, basic_dxf())).stdout
    assert "普通文字" in out
    # MTEXT 堆叠 \S1/2 → 1/2
    assert "1/2倍率" in out


def test_block_expansion_attrib_dimension(tmp_path):
    out = run_script(STREAM, _write(tmp_path, block_attrib_dim_dxf())).stdout
    # #1 块内文字通过 INSERT 展开
    assert "块内文字" in out
    # #1 块属性 ATTRIB
    assert "属性值" in out
    # 普通 TEXT
    assert "普通文字" in out
    # #6 尺寸标注覆盖文字
    assert "150" in out


def test_dimension_measure_fallback(tmp_path):
    out = run_script(STREAM, _write(tmp_path, dimension_measure_dxf())).stdout
    # #6 <> 占位符 → 回退到测量值 123.45
    assert "123.45" in out


def test_table_mode(tmp_path):
    out = run_script(STREAM, _write(tmp_path, table_dxf()), "--table").stdout
    assert "|" in out
    assert "---" in out
    for ch in ("A", "B", "C", "D"):
        assert ch in out


def test_by_layer(tmp_path):
    out = run_script(STREAM, _write(tmp_path, multilayer_dxf()), "--by-layer").stdout
    assert "### 图层: 0" in out
    assert "### 图层: WALL" in out
    assert "零层" in out
    assert "墙层" in out


def test_big5_font_decoding(tmp_path):
    # #3 字体名暗示 big5（tcsymbol → tc），\M+5A440 应解为「一」
    out = run_script(STREAM, _write(tmp_path, big5_font_dxf())).stdout
    assert "一" in out
    assert r"\M+5A440" not in out
