"""Constant-memory DXF tag parsing for text extraction and STYLE discovery."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from .fonts import DEFAULT_FONT_DIR, ShxFont, get_font
from .text import decode_text


@dataclass(frozen=True)
class TextRecord:
    text: str
    x: float = 0.0
    y: float = 0.0
    entity: str = "TEXT"
    layer: str = ""
    style: str = ""


def iter_tags(stream: TextIO) -> Iterator[tuple[int, str]]:
    while code_line := stream.readline():
        value_line = stream.readline()
        if not value_line:
            return
        try:
            yield int(code_line.strip()), value_line.rstrip("\r\n")
        except ValueError:
            continue


def _iter_entities(path: str | Path) -> Iterator[tuple[str, str, list[tuple[int, str]]]]:
    with Path(path).open(encoding="utf-8", errors="replace") as stream:
        section = ""
        current_type = ""
        tags: list[tuple[int, str]] = []
        for code, value in iter_tags(stream):
            if code == 0:
                if current_type:
                    if current_type == "SECTION":
                        section = next((item for tag, item in tags if tag == 2), "")
                    elif current_type == "ENDSEC":
                        section = ""
                    else:
                        yield section, current_type, tags
                current_type, tags = value, []
            else:
                tags.append((code, value))
        if current_type and current_type not in {"SECTION", "ENDSEC"}:
            yield section, current_type, tags


def discover_fonts(path: str | Path, font_dir: str | Path = DEFAULT_FONT_DIR) -> dict[str, ShxFont]:
    """Scan STYLE records without loading the DXF into memory."""
    result: dict[str, ShxFont] = {}
    for section, entity, tags in _iter_entities(path):
        if section != "TABLES" or entity != "STYLE":
            continue
        values = {code: value for code, value in tags if code in (2, 4)}
        if values.get(2) and values.get(4):
            font = get_font(values[4], font_dir)
            if font:
                result[values[2]] = font
    return result


def extract_texts(
    path: str | Path,
    *,
    font: ShxFont | None = None,
    font_dir: str | Path = DEFAULT_FONT_DIR,
) -> list[TextRecord]:
    fonts = {} if font else discover_fonts(path, font_dir)
    records: list[TextRecord] = []
    for section, entity, tags in _iter_entities(path):
        if section != "ENTITIES" or entity not in {"TEXT", "MTEXT"}:
            continue
        grouped: dict[int, list[str]] = {}
        for code, value in tags:
            grouped.setdefault(code, []).append(value)
        parts = grouped.get(3, []) + grouped.get(1, []) if entity == "MTEXT" else grouped.get(1, [])
        raw = "".join(parts)
        if not raw.strip():
            continue
        style = grouped.get(7, [""])[0]
        active_font = font or fonts.get(style)
        records.append(TextRecord(
            text=decode_text(raw, active_font),
            x=_number(grouped.get(10, ["0"])[0]),
            y=_number(grouped.get(20, ["0"])[0]),
            entity=entity,
            layer=grouped.get(8, [""])[0],
            style=style,
        ))
    return sorted(records, key=lambda item: (-item.y, item.x))


def _number(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        return 0.0
