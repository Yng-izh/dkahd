#!/usr/bin/env python3
"""解析 DXF 文件并输出结构化报告。

用法:
  python3 parse_dxf.py <file.dxf> [--entities] [--texts] [--layers] [--blocks] [--limits] [--explode-blocks]
"""
import argparse
import sys

try:
    from extract_texts_stream import resolve_font, decode_bigfont
    _HAVE_DECODE = True
except Exception:
    _HAVE_DECODE = False


def main():
    parser = argparse.ArgumentParser(description="解析 DXF 并输出结构化报告")
    parser.add_argument("file", help="DXF 文件路径")
    parser.add_argument("--entities", action="store_true", help="列出所有实体")
    parser.add_argument("--texts", action="store_true", help="提取全部文字标注")
    parser.add_argument("--layers", action="store_true", help="列出图层信息")
    parser.add_argument("--blocks", action="store_true", help="列出块定义")
    parser.add_argument("--limits", action="store_true", help="输出图纸范围")
    parser.add_argument("--explode-blocks", action="store_true", help="展开块引用实体")
    parser.add_argument("--layer-filter", help="按图层名关键词过滤（仅输出匹配图层的内容）")
    args = parser.parse_args()

    try:
        import ezdxf
    except ImportError:
        print("错误: 未安装 ezdxf，请执行 pip3 install ezdxf", file=sys.stderr)
        sys.exit(1)

    try:
        doc = ezdxf.readfile(args.file, errors="ignore")
    except Exception as e:
        print(f"错误: DXF 解析失败（{e}）。该文件可能损坏或缺少 EOF 标签；"
              f"请改用 extract_texts_stream.py 做流式提取。", file=sys.stderr)
        sys.exit(2)
    msp = doc.modelspace()

    # 建立 样式→字体 缓存，用于解码 BigFont(\M+) / \U+ / %% 转义
    _font = None
    _font_cache = None
    if _HAVE_DECODE:
        try:
            _font, _font_cache = resolve_font(None, args.file)
        except Exception:
            _font, _font_cache = None, None

    import re as _re

    # 图层过滤器（不区分大小写）
    _layer_re = None
    if args.layer_filter:
        _layer_re = _re.compile(args.layer_filter, _re.IGNORECASE)
    # 标注图层名中命中的辅助判断
    def _layer_ok(layer):
        if _layer_re is None:
            return True
        return bool(_layer_re.search(layer))

    def _emit_if_layer(layer):
        # 用于实体清单/文字是否输出
        return _layer_ok(layer)

    def _dec(text, style=""):
        """解码文字：\\M+ 大字体 / \\U+ Unicode / %% 转义；并清理 MTEXT 格式码。"""
        if not _HAVE_DECODE or not text:
            return text
        f = _font
        if f is None and _font_cache and style in _font_cache:
            f = _font_cache[style]
        s = decode_bigfont(text, f)
        # 清理 MTEXT 残留格式码：\P 换行，\...; 格式指令删除
        s = s.replace("\\P", "\n").replace("\\p", "\n")
        s = _re.sub(r"\\[A-Za-z][^;\\\n]*;?", "", s)
        return s.strip()

    # 基础信息
    print("== 图纸信息 ==")
    print(f"文件: {args.file}")
    print(f"DXF 版本: {doc.dxfversion}")
    print(f"模型空间实体数: {len(msp)}")

    # 实体类型统计
    counts = {}
    for e in msp:
        t = e.dxftype()
        counts[t] = counts.get(t, 0) + 1
    if counts:
        print(f"实体类型统计: {counts}")

    if args.limits:
        from ezdxf import bbox
        try:
            ext = bbox.extents(msp)
            print(f"图纸范围: min=({ext.extmin.x:.2f},{ext.extmin.y:.2f}) "
                  f"max=({ext.extmax.x:.2f},{ext.extmax.y:.2f})")
        except Exception as e:
            print(f"范围计算失败: {e}")

    if args.layers:
        print("\n== 图层 ==")
        for layer in doc.layers:
            print(f"  {layer.dxf.name}: color={layer.dxf.color}, "
                  f"linetype={layer.dxf.linetype}, "
                  f"off={layer.is_off()}, frozen={layer.is_frozen()}")

    if args.blocks:
        print("\n== 块定义 ==")
        for block in doc.blocks:
            if block.name.startswith("*"):
                continue
            ents = list(block)
            try:
                bp = block.base_point
            except Exception:
                bp = (0.0, 0.0, 0.0)
            print(f"  {block.name}: {len(ents)} 个实体, 基点=({bp})")

    if args.texts:
        print("\n== 文字标注 ==")
        cnt = [0]

        def emit(text, pos, src=""):
            cnt[0] += 1
            print(f"  TEXT{src}@{pos}: {text[:200]}")

        # 图层过滤：仅保留匹配图层
        def _text_ok(e):
            try:
                return _layer_ok(e.dxf.layer)
            except Exception:
                return True

        for e in msp:
            t = e.dxftype()
            if not _text_ok(e):
                continue
            if t == "MTEXT":
                style = getattr(e.dxf, "style", "")
                content = _dec(e.text, style).replace("\n", "\\n")
                emit(content, e.dxf.insert)
            elif t == "TEXT":
                style = getattr(e.dxf, "style", "")
                content = _dec(e.dxf.text, style)
                emit(content, e.dxf.insert)
            elif t == "DIMENSION":
                # 尺寸标注：优先覆盖文字，否则用测量值
                try:
                    txt = getattr(e.dxf, "text", "") or ""
                    if not txt or txt == "<>":
                        txt = f"{e.get_measurement():.2f}"
                    content = _dec(txt, getattr(e.dxf, "style", ""))
                    emit(content, e.dxf.insert, "(尺寸)")
                except Exception:
                    pass
            elif t == "INSERT":
                # 展开块引用，提取其中的文字与块属性（ATTRIB）
                try:
                    bname = e.dxf.name
                    for ve in e.virtual_entities():
                        vt = ve.dxftype()
                        if vt == "MTEXT":
                            content = _dec(ve.text, getattr(ve.dxf, "style", "")).replace("\n", "\\n")
                            emit(content, ve.dxf.insert, f"(块:{bname})")
                        elif vt in ("TEXT", "ATTRIB"):
                            content = _dec(ve.dxf.text, getattr(ve.dxf, "style", ""))
                            emit(content, ve.dxf.insert, f"(块:{bname})")
                except Exception:
                    pass
        if cnt[0] == 0:
            print("  (无文字实体)")

    if args.entities:
        print("\n== 实体清单 ==")
        for e in msp:
            t = e.dxftype()
            layer = e.dxf.layer
            if not _layer_ok(layer):
                continue
            if t == "LINE":
                print(f"  LINE[{layer}] ({e.dxf.start[0]:.2f},{e.dxf.start[1]:.2f}) -> ({e.dxf.end[0]:.2f},{e.dxf.end[1]:.2f})")
            elif t == "CIRCLE":
                print(f"  CIRCLE[{layer}] 圆心=({e.dxf.center[0]:.2f},{e.dxf.center[1]:.2f}) 半径={e.dxf.radius:.2f}")
            elif t == "ARC":
                print(f"  ARC[{layer}] 圆心=({e.dxf.center[0]:.2f},{e.dxf.center[1]:.2f}) 半径={e.dxf.radius:.2f} "
                      f"角度={e.dxf.start_angle:.1f}->{e.dxf.end_angle:.1f}")
            elif t == "TEXT":
                print(f"  TEXT[{layer}] \"{_dec(e.dxf.text, getattr(e.dxf, 'style', ''))}\" @({e.dxf.insert[0]:.2f},{e.dxf.insert[1]:.2f}) 高度={e.dxf.height}")
            elif t == "MTEXT":
                print(f"  MTEXT[{layer}] \"{_dec(e.text, getattr(e.dxf, 'style', ''))[:100]}\" @({e.dxf.insert[0]:.2f},{e.dxf.insert[1]:.2f})")
            elif t == "DIMENSION":
                meas = e.dxf.get("text", "")
                print(f"  DIMENSION[{layer}] 测量值={e.get_measurement():.2f} 文字=\"{meas}\"")
            elif t == "LWPOLYLINE":
                pts = list(e.get_points())
                print(f"  LWPOLYLINE[{layer}] {len(pts)} 顶点 闭合={e.closed} 顶点0=({pts[0][0]:.2f},{pts[0][1]:.2f})")
            elif t == "INSERT":
                print(f"  INSERT[{layer}] 块={e.dxf.name} @({e.dxf.insert[0]:.2f},{e.dxf.insert[1]:.2f}) 缩放={e.dxf.xscale:.2f}")
                if args.explode_blocks:
                    vcp = e.virtual_entities()
                    for ve in vcp:
                        print(f"    ├─ {ve.dxftype()}")
            else:
                print(f"  {t}[{layer}]")


if __name__ == "__main__":
    main()
