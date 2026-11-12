"""Decode AutoCAD text escape sequences and MTEXT formatting codes."""

from __future__ import annotations

import re

from .fonts import ShxFont

PERCENT_ESCAPES = {"c": "Ø", "d": "°", "p": "±", "%": "%", "u": "", "o": ""}

_UNICODE_RE = re.compile(r"\\U\+([0-9A-Fa-f]{4,8})")
_BIGFONT_RE = re.compile(r"\\M\+5([0-9A-Fa-f]{4})")
_PERCENT_RE = re.compile(r"%%([A-Za-z%]|\d{1,3})")
_FORMAT_RE = re.compile(r"\\[A-Za-z][^;\\\n]*;?")
_STACK_RE = re.compile(r"\\S(.+?)([\^#/])(.+?)(?=\s|\\|\{|\}|$)")

#: Fallback codecs tried when the style font does not resolve a bigfont code.
FALLBACK_CODECS = ("gbk", "big5", "cp949", "shift_jis")


def _bigfont_char(code: int, font: ShxFont | None) -> str | None:
    if font is not None:
        try:
            character = font.get_char(code)
        except (ValueError, LookupError):
            character = None
        if character:
            return character
    raw = bytes(((code >> 8) & 0xFF, code & 0xFF))
    codecs = (font.encoding,) if font and font.encoding != "unknown" else ()
    for codec in dict.fromkeys((*codecs, *FALLBACK_CODECS)):
        try:
            return raw.decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
    return None


def decode_text(value: str, font: ShxFont | None = None) -> str:
    """Decode Unicode, bigfont, and percent escapes without altering formatting codes."""
    if not value:
        return value

    def unicode_repl(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 16))

    def bigfont_repl(match: re.Match[str]) -> str:
        character = _bigfont_char(int(match.group(1), 16), font)
        return character if character is not None else match.group(0)

    def percent_repl(match: re.Match[str]) -> str:
        token = match.group(1)
        if token.lower() in PERCENT_ESCAPES:
            return PERCENT_ESCAPES[token.lower()]
        if token.isdigit() and int(token) < 256:
            return chr(int(token))
        return match.group(0)

    value = _UNICODE_RE.sub(unicode_repl, value)
    value = _BIGFONT_RE.sub(bigfont_repl, value)
    return _PERCENT_RE.sub(percent_repl, value)


def _clean_stack_part(part: str) -> str:
    part = _FORMAT_RE.sub("", part)
    return part.replace("{", "").replace("}", "").strip()


def resolve_stack(value: str) -> str:
    """Turn MTEXT stacked fractions (``\\S1^2``) into readable text."""

    def repl(match: re.Match[str]) -> str:
        top = _clean_stack_part(match.group(1))
        separator = "#" if match.group(2) == "#" else "/"
        bottom = _clean_stack_part(match.group(3))
        return f"{top}{separator}{bottom}"

    return _STACK_RE.sub(repl, value)


def clean_mtext(value: str) -> str:
    """Decode formatting codes of MTEXT content into plain readable text."""
    if not value:
        return ""
    value = resolve_stack(value)
    value = value.replace(r"\P", "\n").replace(r"\p", "\n")
    value = value.replace("{", "").replace("}", "")
    value = _FORMAT_RE.sub("", value)
    value = value.replace("~", " ")
    value = re.sub(r"[ \t]+", " ", value)
    return re.sub(r"\n\s*\n+", "\n", value).strip()


def decode_cad_text(value: str, font: ShxFont | None = None, *, mtext: bool = False) -> str:
    """Full decode pipeline used by the reporting layer.

    Handles ``\\U+``, ``\\M+5`` bigfonts, ``%%`` escapes and — when ``mtext`` is
    true — MTEXT stacked fractions plus residual formatting codes.
    """
    decoded = decode_text(value, font)
    if mtext:
        return clean_mtext(decoded)
    return decoded.strip()
