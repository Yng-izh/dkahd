#!/usr/bin/env python3
"""Compatibility entry point for SHX to SHP decompilation."""

import sys
from pathlib import Path

import _bootstrap as _bootstrap  # noqa: F401  仅用于注入 sys.path

from dwg_reader.decompile import decompile
from dwg_reader.fonts import get_font

if len(sys.argv) < 2:
    print("用法: shx_decompile.py <字体.shx|字体名> [输出.shp]")
    raise SystemExit(1)
font = get_font(sys.argv[1])
if font is None:
    print(f"字体加载失败: {sys.argv[1]}", file=sys.stderr)
    raise SystemExit(2)
result = decompile(font)
if len(sys.argv) > 2:
    Path(sys.argv[2]).write_text(result, encoding="utf-8")
    print(f"已反编译: {sys.argv[2]} ({len(font)} shapes)")
else:
    print(result)
