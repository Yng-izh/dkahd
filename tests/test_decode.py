"""纯函数单元测试：大字体解码 / 堆叠文字 / 几何排序 / 表格还原。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.dirname(__file__))  # 便于 from conftest import

from extract_texts_stream import (
    decode_bigfont, resolve_stack, reorder_by_columns, to_markdown_table,
)


def test_unicode_escape():
    assert decode_bigfont(r"\U+4E2D") == "中"


def test_percent_escapes():
    assert decode_bigfont("%%C") == "Ø"
    assert decode_bigfont("%%D") == "°"
    assert decode_bigfont("%%P") == "±"
    assert decode_bigfont("%%%") == "%"


def test_bigfont_fallback_decodes():
    # 无字体时回退到 gbk 字节解码，结果应为非空的单个汉字（非原始串）
    out = decode_bigfont(r"\M+5A440")
    assert out != r"\M+5A440"
    assert out.strip() != ""


def test_resolve_stack_fraction():
    assert resolve_stack(r"\S1/2") == "1/2"


def test_resolve_stack_tolerance():
    assert resolve_stack(r"\S+0.1^-0.1") == "+0.1/-0.1"


def test_resolve_stack_noline():
    assert resolve_stack(r"\S3#4") == "3#4"


def test_resolve_stack_braced_height():
    # 含 \H 高度控制码的括号形式：剥离控制码保留文字
    assert resolve_stack(r"\S{\H0.7x;±0.02}^0") == "±0.02/0"


def test_resolve_stack_no_stack_unchanged():
    assert resolve_stack("普通文字") == "普通文字"


def test_decode_bigfont_integrates_stack():
    # decode_bigfont 应能同时完成 \U+/\% 与 \S 解析
    out = decode_bigfont(r"\S1/2倍率")
    assert "1/2" in out
    assert out.endswith("倍率")


def test_reorder_by_columns():
    # 两栏：左栏(x≈0) 应整体排在右栏(x≈100) 之前
    texts = [
        (5.0, 100.0, "TEXT", "L", "右上"),
        (5.0, 0.0, "TEXT", "L", "左上"),
        (15.0, 100.0, "TEXT", "L", "右下"),
        (15.0, 0.0, "TEXT", "L", "左下"),
    ]
    out = reorder_by_columns(texts)
    left = [t for t in out if t[1] < 50]
    right = [t for t in out if t[1] > 50]
    assert len(left) == 2 and len(right) == 2
    assert out.index(left[0]) < out.index(right[0])


def test_to_markdown_table():
    texts = [
        (20.0, 0.0, "TEXT", "L", "A"),
        (20.0, 10.0, "TEXT", "L", "B"),
        (10.0, 0.0, "TEXT", "L", "C"),
        (10.0, 10.0, "TEXT", "L", "D"),
    ]
    md = to_markdown_table(texts)
    assert md != ""
    assert "---" in md
    for ch in ("A", "B", "C", "D"):
        assert ch in md
