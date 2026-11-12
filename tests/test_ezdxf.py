"""ezdxf 路径集成测试：覆盖范围块展开(virtual_entities)、MTEXT 堆叠、繁中码页解码。

依赖 ezdxf；若环境未安装则整体跳过。通过 ezdxf 生成的 DXF 同时验证了
extract_texts.py / parse_dxf.py 以及流解析器对码页字节(如 ±)的解码。
"""
import os
import sys

import pytest

ezdxf = pytest.importorskip("ezdxf")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import run_script, STREAM, EZDXF, PARSE


def _make_ezdxf_dxf(path):
    doc = ezdxf.new("R2000")
    msp = doc.modelspace()
    msp.add_text("标题", dxfattribs={"insert": (0, 0)})
    # MTEXT 含堆叠公差与码页字符 ±
    msp.add_mtext(r"公差 \S+0.1^-0.1 与 ±0.05",
                  dxfattribs={"insert": (0, 10)})
    # 块 + 块参照，验证 virtual_entities 展开
    blk = doc.blocks.new("B1")
    blk.add_text("块文字", dxfattribs={"insert": (0, 0)})
    msp.add_blockref("B1", (50, 50))
    doc.saveas(path)
    return path


def test_extract_texts_ezdxf(tmp_path):
    p = _make_ezdxf_dxf(str(tmp_path / "e.dxf"))
    out = run_script(EZDXF, p).stdout
    assert "标题" in out
    assert "块文字" in out
    # MTEXT 堆叠 + 码页 ± 均被正确解码
    assert "+0.1/-0.1" in out
    assert "±0.05" in out


def test_parse_dxf_texts(tmp_path):
    p = _make_ezdxf_dxf(str(tmp_path / "e.dxf"))
    out = run_script(PARSE, p, "--texts").stdout
    assert "标题" in out
    assert "块文字" in out
    assert "+0.1/-0.1" in out


def test_stream_codepage_decode(tmp_path):
    # ezdxf 以码页字节写入 ±，流解析器应按 DWGCODEPAGE 正确解码为 ±
    p = _make_ezdxf_dxf(str(tmp_path / "e.dxf"))
    out = run_script(STREAM, p).stdout
    assert "±0.05" in out
    assert "�" not in out
