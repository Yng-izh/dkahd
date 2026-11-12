#!/usr/bin/env python3
"""SHX 大字体（BigFont）解析器 — 全字体支持版。

解析 AutoCAD 大字体 SHX 文件，建立 shape number → 字符 的映射表。
用于解码 DXF 中 TEXT/MTEXT 的 `\\M+5XXXX` 大字体编码。

关键结论（已用 gbcbig/hztxt/tssdchn/KOR 等多字体验证）：
- bigfont 索引表 shape number = 该字体的字符编码
  - 中文字体（gbcbig/hztxt/tssdchn 等）→ GBK 码
  - 韩文字体（KORdansun 等）→ cp949 码
  - 日文字体 → shift_jis 码
  - 繁中字体 → big5 码
- DXF 中 `\\M+5XXXX` 去掉前导 `5` 后即 shape number

字体索引位于 fonts/index.json，记录每个字体的编码体系。
"""
from __future__ import annotations

import json
import os
import struct

BIGFONT_MAGIC = b"AutoCAD-86 bigfont"
UNIFONT_MAGIC = b"AutoCAD-86 unifont"

# 各编码的 Python codec 名
ENCODING_CODEC = {
    "gbk": "gbk",
    "cp949": "cp949",
    "shift_jis": "shift_jis",
    "big5": "big5",
    "unicode": "utf-16-le",
}


class ShxBigFont:
    """SHX 大字体解析器。"""

    def __init__(self, filename: str, encoding: str | None = None):
        with open(filename, "rb") as f:
            self.data = f.read()
        self.filename = filename
        self.name = os.path.basename(filename)
        self.is_bigfont = self.data.startswith(BIGFONT_MAGIC)
        self.is_unifont = self.data.startswith(UNIFONT_MAGIC)
        self.encoding = encoding or self._detect_encoding_from_index() or "gbk"
        self.shapes: dict[int, bytes] = {}
        if self.is_bigfont:
            self._parse_bigfont()
        # unifont 的 get_char 直接按 chr(code) 解码，无需解析字形数据

    def _detect_encoding_from_index(self) -> str | None:
        """从 fonts/index.json 读取编码。"""
        try:
            idx_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "..", "fonts", "index.json"
            )
            with open(idx_path, "r", encoding="utf-8") as f:
                index = json.load(f)
            key = self.name.lower()
            if key in index:
                return index[key].get("encoding")
            # 去扩展名再找
            base = key.rsplit(".", 1)[0]
            if base in index:
                return index[base].get("encoding")
        except Exception:
            pass
        return None

    def _parse_bigfont(self):
        data = self.data
        sub = data.find(b"\x1a")
        if sub == -1:
            raise ValueError(f"{self.filename}: 缺少 0x1A 头分隔符")
        pos = sub + 1
        pos += 2  # marker
        n, = struct.unpack_from("<H", data, pos)
        pos += 2
        k, = struct.unpack_from("<H", data, pos)
        pos += 2
        pos += k * 4  # ranges
        for _ in range(n):
            code, length, offset = struct.unpack_from("<HHI", data, pos)
            pos += 8
            if offset + length <= len(data):
                self.shapes[code] = data[offset:offset + length]

    def contains(self, code: int) -> bool:
        return code in self.shapes

    def get_char(self, code: int) -> str | None:
        """将 shape number 解码为字符（按字体编码体系，带多编码回退）。"""
        if code == 0:
            return None
        if self.is_unifont:
            return chr(code) if 0 < code < 0x10FFFF else None
        if code not in self.shapes and code < 0x100:
            # ASCII 字符可能不单列，直接映射
            if 0x20 <= code < 0x7F:
                return chr(code)
        if code not in self.shapes:
            return None
        b = bytes([(code >> 8) & 0xFF, code & 0xFF])
        # 尝试顺序：索引编码 → gbk → cp949 → big5 → shift_jis
        codec_order = [self.encoding] if self.encoding != "unknown" else []
        for extra in ("gbk", "cp949", "big5", "shift_jis"):
            if extra not in codec_order:
                codec_order.append(extra)
        for codec in codec_order:
            try:
                s = b.decode(ENCODING_CODEC.get(codec, codec), errors="strict")
                if s and not s.startswith("\ufffd"):
                    return s
            except Exception:
                continue
        return None

    def __len__(self):
        return len(self.shapes)


# 字体查找路径与缓存
DEFAULT_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fonts")
FONT_CACHE: dict[str, "object"] = {}


def encoding_from_name(name: str | None) -> str | None:
    """从字体名启发式推断编码体系（无需字体文件即可解码字节）。

    覆盖：繁中 big5 / 韩文 cp949 / 日文 shift_jis；其余返回 None（保持默认 gbk）。
    """
    n = (name or "").lower()
    if any(k in n for k in ("big5", "b5", "tc", "tch", "trad", "tw", "hk", "cht")):
        return "big5"
    if "kor" in n or "kr" in n:
        return "cp949"
    if any(k in n for k in ("jp", "japan", "jis")):
        return "shift_jis"
    return None


class EncodingOnlyFont:
    """无字形文件的轻量解码器：仅按指定编码把 shape number(两字节)解码为字符。

    用于字体文件缺失但字体名已暗示编码（如繁中 big5 字体）的兜底，
    使 \\M+ 大字体编码仍能正确解码。
    """

    def __init__(self, encoding: str):
        self.encoding = encoding
        self.name = encoding
        self.is_bigfont = False
        self.is_unifont = False

    def get_char(self, code: int) -> str | None:
        if code == 0:
            return None
        if 0x20 <= code < 0x7F:
            return chr(code)
        b = bytes([(code >> 8) & 0xFF, code & 0xFF])
        codec = ENCODING_CODEC.get(self.encoding, self.encoding)
        try:
            return b.decode(codec, errors="strict")
        except Exception:
            return None

    def __len__(self):
        return 0


def get_font(name: str | None = None):
    """按字体名加载大字体，带缓存，自动匹配编码。

    支持：
    - 直接路径
    - 字体名（大小写不敏感，自动补 .shx 扩展名）
    - 探索者/天正常用字体名
    """
    if name is None:
        return None

    # 直接路径（绝对/相对）
    if os.path.isfile(name):
        key = os.path.basename(name).lower()
        if key in FONT_CACHE:
            return FONT_CACHE[key]
        try:
            font = ShxBigFont(name)
            FONT_CACHE[key] = font
            return font
        except Exception:
            return None

    # STYLE 表里的字体名可能带路径前缀（如 fonts/tssdchn.shx、C:\...\x.shx），
    # 统一取 basename 后再做匹配，避免拼接 DEFAULT_FONT_DIR 时路径污染
    base = os.path.basename(name)
    key = base.lower()
    if key in FONT_CACHE:
        return FONT_CACHE[key]

    candidates = [base, key]
    if not key.endswith(".shx"):
        candidates += [base + ".shx", key + ".shx"]
    for fn in candidates:
        p = os.path.join(DEFAULT_FONT_DIR, fn)
        if os.path.isfile(p):
            try:
                font = ShxBigFont(p)
                FONT_CACHE[key] = font
                return font
            except Exception:
                continue
    # 文件缺失：按字体名推断编码，返回轻量解码器（无需字形文件也能解码字节）
    enc = encoding_from_name(name)
    if enc:
        dec = EncodingOnlyFont(enc)
        FONT_CACHE[key] = dec
        return dec
    return None


def decode_mcode(mcode: str, font: ShxBigFont | None = None) -> str:
    """解码单个 `\\M+5XXXX` 编码。"""
    hexstr = mcode
    if hexstr.startswith("5"):
        hexstr = hexstr[1:]
    try:
        code = int(hexstr, 16)
    except ValueError:
        return mcode
    if font is not None:
        ch = font.get_char(code)
        if ch is not None and "\ufffd" not in ch:
            return ch
    # 回退: 按字体编码或 GBK 启发式
    codec = "gbk"
    if font is not None:
        codec = ENCODING_CODEC.get(font.encoding, "gbk")
    try:
        return bytes([(code >> 8) & 0xFF, code & 0xFF]).decode(codec, errors="replace")
    except Exception:
        return mcode


# AutoCAD %% 转义序列 → Unicode（key 为 %% 后的单个字母，小写）
PERCENT_ESCAPES = {
    "c": "Ø",   # 直径符号（%%C / %%c）
    "d": "°",   # 度（%%D / %%d）
    "p": "±",   # 正负（%%P / %%p）
    "%": "%",   # 百分号（%%%）
    "u": "",    # 下划线开/关（标记，输出时空）
    "o": "",    # 上划线开/关
}


def decode_percent_escapes(s: str) -> str:
    """解码 AutoCAD `%%` 转义序列（%%C/%%D/%%P/%%% 等，大小写不敏感）。

    常见映射（AutoCAD 标准）：
    - %%C / %%c → Ø（直径）
    - %%D / %%d → °（度）
    - %%P / %%p → ±（正负）
    - %%%       → %（百分号）
    - %%U / %%u → 下划线开/关（去掉标记）
    - %%O / %%o → 上划线开/关（去掉标记）
    - 数字形式（%%130、%%141 等）→ GBK 两字节码
    """
    import re

    def repl(m):
        code = m.group(1).lower()
        if code in PERCENT_ESCAPES:
            return PERCENT_ESCAPES[code]
        # 数字形式: %%130 等 → 单字节字符
        try:
            n = int(m.group(1), 10)
            if n < 256:
                return chr(n)
        except ValueError:
            pass
        return m.group(0)

    # 匹配 %%c / %%d / %%p / %%% / %%数字
    return re.sub(r"%%([A-Za-z%]|\d{1,3})", repl, s)


def decode_bigfont_str(s: str, font: ShxBigFont | None = None) -> str:
    """解码字符串中所有 `\\M+5XXXX`、`\\U+XXXX` 与 `%%` 转义序列。"""
    import re

    def repl_u(m):
        try:
            return chr(int(m.group(1), 16))
        except Exception:
            return m.group(0)

    def repl_m(m):
        return decode_mcode(m.group(1), font)

    s = re.sub(r"\\U\+([0-9A-Fa-f]{4})", repl_u, s)
    s = re.sub(r"\\M\+5([0-9A-Fa-f]{4})", repl_m, s)
    s = decode_percent_escapes(s)
    return s


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: shxfont.py <字体.shx|字体名> [测试编码...]")
        sys.exit(1)
    font = get_font(sys.argv[1])
    if font is None:
        print(f"字体加载失败: {sys.argv[1]}")
        sys.exit(1)
    print(f"字体: {font.name} [{font.encoding}] type={'bigfont' if font.is_bigfont else 'unifont' if font.is_unifont else '?'}")
    print(f"shape 数量: {len(font)}")
    if len(sys.argv) > 2:
        for c in sys.argv[2:]:
            code = int(c, 16)
            print(f"  0x{code:04X} → {font.get_char(code)!r}")
