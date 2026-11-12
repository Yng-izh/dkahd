"""Read AutoCAD SHX bigfont/unifont indexes and map shape numbers to text."""

from __future__ import annotations

import json
import os
import struct
import sys
from functools import lru_cache
from pathlib import Path

BIGFONT_MAGIC = b"AutoCAD-86 bigfont"
UNIFONT_MAGIC = b"AutoCAD-86 unifont"
CODECS = {"gbk": "gbk", "cp949": "cp949", "shift_jis": "shift_jis", "big5": "big5"}
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def font_directories() -> tuple[Path, ...]:
    """Return font locations in explicit, installed, then source-tree order."""
    candidates = []
    if configured := os.environ.get("DWG_READER_FONT_DIR"):
        candidates.append(Path(configured).expanduser())
    candidates.extend((
        Path(sys.prefix) / "share" / "dwg-reader" / "fonts",
        Path(__file__).with_name("fonts_data"),
        PROJECT_ROOT / "fonts",
    ))
    return tuple(dict.fromkeys(path.resolve() for path in candidates))


def default_font_dir() -> Path:
    return next((path for path in font_directories() if path.is_dir()), font_directories()[0])


DEFAULT_FONT_DIR = default_font_dir()


class ShxFont:
    """Parsed SHX font index.

    Glyph drawing commands are retained as bytes; character decoding uses the
    shape number and the encoding recorded in ``fonts/index.json``.
    """

    def __init__(self, filename: str | Path, encoding: str | None = None) -> None:
        self.path = Path(filename)
        self.data = self.path.read_bytes()
        self.name = self.path.name
        self.is_bigfont = self.data.startswith(BIGFONT_MAGIC)
        self.is_unifont = self.data.startswith(UNIFONT_MAGIC)
        if not (self.is_bigfont or self.is_unifont):
            raise ValueError(f"{self.path}: unsupported SHX format")
        self.encoding = encoding or self._index_encoding() or "gbk"
        self.shapes: dict[int, bytes] = {}
        self._parse_bigfont() if self.is_bigfont else self._parse_unifont()

    def _index_encoding(self) -> str | None:
        index_path = next(
            (directory / "index.json" for directory in font_directories()
             if (directory / "index.json").is_file()),
            DEFAULT_FONT_DIR / "index.json",
        )
        try:
            item = json.loads(index_path.read_text(encoding="utf-8")).get(self.name.lower())
            return item.get("encoding") if item else None
        except (OSError, ValueError, AttributeError):
            return None

    def _parse_bigfont(self) -> None:
        pos = self.data.find(b"\x1a")
        if pos < 0:
            raise ValueError(f"{self.path}: missing SHX header delimiter")
        pos += 3
        count, range_count = struct.unpack_from("<HH", self.data, pos)
        pos += 4 + range_count * 4
        for _ in range(count):
            if pos + 8 > len(self.data):
                raise ValueError(f"{self.path}: truncated SHX index")
            code, length, offset = struct.unpack_from("<HHI", self.data, pos)
            pos += 8
            if offset + length <= len(self.data):
                self.shapes[code] = self.data[offset : offset + length]

    def _parse_unifont(self) -> None:
        pos = self.data.find(b"\x1a")
        if pos < 0 or pos + 7 > len(self.data):
            raise ValueError(f"{self.path}: invalid unifont header")
        pos += 1
        count = struct.unpack_from("<I", self.data, pos)[0]
        pos += 6
        while pos < len(self.data) and self.data[pos] != 0:
            pos += 1
        pos += 7
        for _ in range(max(0, count - 1)):
            if pos + 4 > len(self.data):
                break
            code, length = struct.unpack_from("<HH", self.data, pos)
            pos += 4
            if pos + length > len(self.data):
                break
            self.shapes[code] = self.data[pos : pos + length]
            pos += length

    def get_char(self, code: int) -> str | None:
        if not 0 < code <= 0x10FFFF:
            return None
        if self.is_unifont:
            return chr(code)
        if code not in self.shapes:
            return chr(code) if 0x20 <= code < 0x7F else None
        raw = bytes(((code >> 8) & 0xFF, code & 0xFF))
        order = [self.encoding, "gbk", "cp949", "big5", "shift_jis"]
        for encoding in dict.fromkeys(order):
            try:
                return raw.decode(CODECS.get(encoding, encoding))
            except (UnicodeDecodeError, LookupError):
                continue
        return None

    def __len__(self) -> int:
        return len(self.shapes)


ShxBigFont = ShxFont  # backward-compatible public name


@lru_cache(maxsize=64)
def _load(path: str) -> ShxFont:
    return ShxFont(path)


def get_font(name: str | Path | None, font_dir: str | Path | None = None) -> ShxFont | None:
    """Resolve an SHX font by path or case-insensitive file name."""
    if not name:
        return None
    requested = Path(name)
    if requested.is_file():
        try:
            return _load(str(requested.resolve()))
        except (OSError, ValueError):
            return None
    target = requested.name.lower()
    if not target.endswith(".shx"):
        target += ".shx"
    directories = (Path(font_dir),) if font_dir else font_directories()
    for directory in directories:
        try:
            match = next(p for p in directory.iterdir() if p.name.lower() == target)
            return _load(str(match.resolve()))
        except (OSError, StopIteration, ValueError):
            continue
    return None
