"""Convert parsed SHX glyph commands to textual SHP definitions."""

from __future__ import annotations

from .fonts import ShxFont


def decompile(font: ShxFont) -> str:
    lines = [f"*BIGFONT {len(font)},1,0A1,0FE", "*0,4,font-name", "0,0,0,0"]
    for code in sorted(font.shapes):
        if code == 0:
            continue
        data = font.shapes[code]
        lines.append(f"*{code:04X},{len(data)},{font.get_char(code) or f'c{code:04X}'}")
        lines.extend(",".join(map(str, data[start:start + 20]))
                     for start in range(0, len(data), 20))
    return "\n".join(lines)

