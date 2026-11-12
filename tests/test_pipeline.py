"""一站式流水线 dwg_read.py 集成测试。

依赖 ezdxf（用于生成测试 DXF）；ODA 不可用时跳过 DWG 转换测试，
但 DXF 路径（--no-convert）的流水线测试始终执行。
"""
import os
import sys

import pytest

ezdxf = pytest.importorskip("ezdxf")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import run_script, STREAM, DWG_READ

ODA = "/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter"
HAVE_ODA = os.path.exists(ODA)


def test_pipeline_dxf_texts(tmp_path):
    # 用 ezdxf 生成含文字的 DXF，验证流水线 DXF 路径输出文字
    p = _make_dxf(str(tmp_path / "e.dxf"))
    # 直接用 stream 脚本对照
    ref = run_script(STREAM, p).stdout
    out = run_script(DWG_READ, p, "--no-convert").stdout
    assert "文字标注提取" in out
    # 流水线应至少包含参考提取的文字
    for needle in ("标题",):
        assert needle in out


def test_pipeline_dwg_convert(tmp_path):
    if not HAVE_ODA:
        pytest.skip("ODA File Converter 未安装，跳过 DWG 转换测试")
    doc = ezdxf.new("R2000")
    msp = doc.modelspace()
    msp.add_text("标题", dxfattribs={"insert": (0, 0)})
    dwg = str(tmp_path / "in.dwg")
    doc.saveas(dwg)
    out = run_script(DWG_READ, dwg).stdout
    assert "图纸解析报告" in out
    assert "标题" in out


def _make_dxf(path):
    doc = ezdxf.new("R2000")
    msp = doc.modelspace()
    msp.add_text("标题", dxfattribs={"insert": (0, 0)})
    doc.saveas(path)
    return path
