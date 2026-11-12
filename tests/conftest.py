"""pytest 公共 fixtures：DXF 样例构造 + 脚本运行助手。

注意：DXF 文本含大量反斜杠控制码（\\U+ \\M+5 \\S \\P），务必用原始字符串(r\"\"\")书写，
避免 Python 转义干扰。
"""
import os
import subprocess
import sys
import textwrap

# 测试永远不调用大模型：结果必须确定、离线、不花钱。要验模型相关的逻辑，
# 用例自己 monkeypatch 具体函数，而不是联网。
os.environ.setdefault("DWG_LLM_DISABLE", "1")

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_DIR = os.path.join(SKILL_DIR, "scripts")
STREAM = os.path.join(SCRIPT_DIR, "extract_texts_stream.py")
EZDXF = os.path.join(SCRIPT_DIR, "extract_texts.py")
PARSE = os.path.join(SCRIPT_DIR, "parse_dxf.py")
DWG_READ = os.path.join(SCRIPT_DIR, "dwg_read.py")


def _dxf(raw):
    """剥离首尾空行与可能的残留首反斜杠后去缩进。"""
    raw = raw.lstrip("\\").strip("\n")
    return textwrap.dedent(raw)


def run_script(script, dxf_path, *args):
    """以子进程运行脚本，返回 CompletedProcess（stdout 捕获）。"""
    cmd = [sys.executable, script, dxf_path, *args]
    return subprocess.run(cmd, capture_output=True, text=True)


# ---------------------------------------------------------------------------
# 原始 DXF 文本 fixtures（流解析器直接消费，无需 ezdxf）
# ---------------------------------------------------------------------------

def basic_dxf():
    """单个 TEXT + 一个含 \\S 堆叠的 MTEXT。"""
    return _dxf(r"""\
    0
    SECTION
    2
    ENTITIES
    0
    TEXT
    8
    0
    10
    0.0
    20
    0.0
    1
    普通文字
    0
    MTEXT
    8
    0
    10
    0.0
    20
    10.0
    1
    \S1/2倍率
    0
    ENDSEC
    0
    EOF
    """)


def block_attrib_dim_dxf():
    """块(BLOCK+INSERT)展开 + 独立 ATTRIB + DIMENSION 覆盖文字。"""
    return _dxf(r"""\
    0
    SECTION
    2
    BLOCKS
    0
    BLOCK
    2
    MYBLK
    10
    0.0
    20
    0.0
    0
    TEXT
    8
    0
    10
    5.0
    20
    5.0
    1
    块内文字
    0
    ENDBLK
    0
    SECTION
    2
    ENTITIES
    0
    INSERT
    2
    MYBLK
    10
    100.0
    20
    100.0
    41
    2.0
    42
    1.0
    50
    0.0
    0
    TEXT
    8
    DIM
    10
    1.0
    20
    1.0
    1
    普通文字
    0
    ATTRIB
    8
    0
    10
    2.0
    20
    2.0
    1
    属性值
    0
    DIMENSION
    8
    0
    1
    150
    42
    150.0
    0
    ENDSEC
    0
    EOF
    """)


def dimension_measure_dxf():
    """DIMENSION 仅含测量值(\\S<> 占位符)，应回退到 code 42。"""
    return _dxf(r"""\
    0
    SECTION
    2
    ENTITIES
    0
    DIMENSION
    8
    0
    1
    <>
    42
    123.45
    0
    ENDSEC
    0
    EOF
    """)


def table_dxf():
    """2 行 × 2 列对齐网格，用于 --table 还原。"""
    cells = [
        (20.0, 0.0, "A"), (20.0, 10.0, "B"),
        (10.0, 0.0, "C"), (10.0, 10.0, "D"),
    ]
    lines = ["0", "SECTION", "2", "ENTITIES"]
    for y, x, t in cells:
        lines += ["0", "TEXT", "8", "0", "10", f"{x}", "20", f"{y}", "1", t]
    lines += ["0", "ENDSEC", "0", "EOF"]
    return "\n".join(lines) + "\n"


def multilayer_dxf():
    """不同图层上的文字，用于 --by-layer 聚合。"""
    return _dxf(r"""\
    0
    SECTION
    2
    ENTITIES
    0
    TEXT
    8
    0
    10
    0.0
    20
    0.0
    1
    零层
    0
    TEXT
    8
    WALL
    10
    1.0
    20
    1.0
    1
    墙层
    0
    TEXT
    8
    WALL
    10
    2.0
    20
    2.0
    1
    墙层二
    0
    ENDSEC
    0
    EOF
    """)


def big5_font_dxf():
    """STYLE 表声明繁中大字体（字体名暗示 big5），TEXT 用 \\M+5 编码。"""
    return _dxf(r"""\
    0
    SECTION
    2
    TABLES
    0
    TABLE
    2
    STYLE
    0
    STYLE
    2
    TCFONT
    3
    txt.shx
    4
    tcsymbol.shx
    0
    ENDTAB
    0
    ENDSEC
    0
    SECTION
    2
    ENTITIES
    0
    TEXT
    7
    TCFONT
    8
    0
    10
    0.0
    20
    0.0
    1
    \M+5A440字
    0
    ENDSEC
    0
    EOF
    """)
