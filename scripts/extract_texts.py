#!/usr/bin/env python3
"""提取 DXF 全部文字（TEXT+MTEXT），按位置排序输出，供阅读理解。

自动解码 CAD 大字体（\\M+）、Unicode（\\U+）与 %% 转义；MTEXT 内嵌格式码
（\\P 换行、\\f 字体、\\H 高度等）在解码后清理。
"""
import sys
import re
import ezdxf

try:
    from extract_texts_stream import (
        resolve_font, decode_bigfont, reorder_by_columns,
        to_markdown_table, clean_mtext_format as _shared_clean,
    )
    _HAVE_DECODE = True
except Exception:
    _HAVE_DECODE = False


def _fallback_clean_mtext_format(s: str) -> str:
    """decode 模块不可用时的兜底 MTEXT 清理。"""
    s = s.replace("\\P", "\n").replace("\\p", "\n")
    s = re.sub(r"\{[^}]*\}", "", s)
    s = re.sub(r"\\[A-Za-z][^;\\\n]*;?", "", s)
    s = s.replace("~", "")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


_clean_mtext = _shared_clean if _HAVE_DECODE else _fallback_clean_mtext_format


def main():
    if len(sys.argv) < 2:
        print("用法: extract_texts.py <文件.dxf> [输出.txt] [--table] [--by-layer]")
        sys.exit(1)
    src = sys.argv[1]
    out = None
    table_mode = False
    by_layer = False
    for a in sys.argv[2:]:
        if a == "--table":
            table_mode = True
        elif a == "--by-layer":
            by_layer = True
        else:
            out = a

    doc = ezdxf.readfile(src)
    msp = doc.modelspace()

    # 样式→字体缓存，用于解码 BigFont
    _font = None
    _font_cache = None
    if _HAVE_DECODE:
        try:
            _font, _font_cache = resolve_font(None, src)
        except Exception:
            _font, _font_cache = None, None

    def dec(text, style=""):
        if not _HAVE_DECODE or not text:
            return text
        f = _font
        if f is None and _font_cache and style in _font_cache:
            f = _font_cache[style]
        return decode_bigfont(text, f)

    texts = []

    def grab(e):
        """提取单个实体（TEXT/MTEXT/ATTRIB）的可读文字与位置。

        ATTRIB 为块属性文字；virtual_entities() 展开 INSERT 后坐标已是世界坐标。
        """
        try:
            t = e.dxftype()
            pos = e.dxf.insert
            style = getattr(e.dxf, "style", "")
            layer = getattr(e.dxf, "layer", "")
            if t == "MTEXT":
                content = _clean_mtext(dec(e.text, style))
            elif t in ("TEXT", "ATTRIB"):
                content = dec(e.dxf.text, style).strip()
            else:
                return
        except Exception:
            return
        if content:
            texts.append((pos.y, pos.x, content, layer))

    # 遍历模型空间全部实体，并展开 INSERT 块（含嵌套块与块属性 ATTRIB）
    for e in msp:
        t = e.dxftype()
        if t in ("TEXT", "MTEXT", "ATTRIB"):
            grab(e)
        elif t == "INSERT":
            try:
                for ve in e.virtual_entities():
                    grab(ve)
            except Exception:
                pass
        elif t == "DIMENSION":
            # 尺寸标注：优先覆盖文字(code 1/dxf.text)，否则用测量值
            try:
                txt = getattr(e.dxf, "text", "") or ""
                if not txt or txt == "<>":
                    txt = f"{e.get_measurement():.2f}"
                style = getattr(e.dxf, "style", "")
                content = dec(txt, style).strip()
                pos = e.dxf.insert
                layer = getattr(e.dxf, "layer", "")
                if content:
                    texts.append((pos.y, pos.x, content, layer))
            except Exception:
                pass

    texts = reorder_by_columns(texts) if _HAVE_DECODE else sorted(texts, key=lambda t: (-t[0], t[1]))
    print(f"共提取 {len(texts)} 条文字", file=sys.stderr)

    if table_mode and _HAVE_DECODE:
        md = to_markdown_table(texts)
        if md:
            if out:
                with open(out, "w", encoding="utf-8") as f:
                    f.write(md + "\n")
                print(f"已写入: {out}", file=sys.stderr)
            else:
                print(md)
            return

    if by_layer:
        groups = {}
        for t in texts:
            groups.setdefault(t[3] or "0", []).append(t[2])
        lines = []
        for layer in sorted(groups):
            lines.append(f"\n### 图层: {layer}")
            lines.extend(f"  {c}" for c in groups[layer])
        blob = "\n".join(lines).strip("\n")
    else:
        blob = "\n".join(f"[{t[2]}]" for t in texts)

    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(blob + "\n")
        print(f"已写入: {out}", file=sys.stderr)
    else:
        print(blob)


if __name__ == "__main__":
    main()
