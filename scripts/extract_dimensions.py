#!/usr/bin/env python3
"""提取并分类 DIMENSION 尺寸标注。

按 AutoCAD DIMENSION 类型分类输出：
  - 线性 (LINEAR, 0°/90°/旋转)        ALIGNED     对齐
  - ANGULAR                            角度
  - RADIUS / DIAMETER                  半径/直径
  - ORDINATE                           坐标
  - ARC_LENGTH                         弧长

每条尺寸关联最近的几何对象（LINE/ARC/CIRCLE/LWPOLYLINE），
输出 Markdown 表格 + 可选 JSON。

用法：
  python3 extract_dimensions.py <文件.dxf> [--json] [--out 输出.md]
"""
import argparse
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    from extract_texts_stream import resolve_font, decode_bigfont, clean_mtext_format
    _HAVE_DECODE = True
except Exception:
    _HAVE_DECODE = False

# DIMENSION 类型映射：ezdxf 的 dimtype 整数低 4 位为类型码
# 参考 ezdxf docs: DIMENSION.dxf.dimtype 低 4 bit
DIM_TYPE = {
    0: "线性",
    1: "对齐",
    2: "角度",
    3: "直径",
    4: "半径",
    5: "坐标",
    6: "角度3p",
    8: "弧长",
}


def _dist(p, q):
    return math.hypot(p[0] - q[0], p[1] - q[1])


def _nearest_geom(dim_pos, geoms, k=3):
    """geoms: [(dxftype, layer, center_or_start, end_or_radius, extra), ...]
    返回距离 dim_pos 最近的 k 个 (dxftype, layer, desc) 描述。
    """
    if not geoms:
        return []
    scored = []
    for g in geoms:
        t, layer, p1, p2, extra = g
        # 取代表点：LINE 取中点，ARC/CIRCLE 取圆心，POLYLINE 取首点
        if t == "LINE":
            mid = ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)
            d = _dist(dim_pos, mid)
        elif t in ("CIRCLE", "ARC"):
            d = _dist(dim_pos, p1)
        else:
            d = _dist(dim_pos, p1)
        scored.append((d, t, layer, p1, p2, extra))
    scored.sort(key=lambda x: x[0])
    out = []
    for d, t, layer, p1, p2, extra in scored[:k]:
        if t == "LINE":
            desc = f"LINE({p1[0]:.0f},{p1[1]:.0f})→({p2[0]:.0f},{p2[1]:.0f})"
        elif t in ("CIRCLE", "ARC"):
            r = p2 if isinstance(p2, (int, float)) else 0
            desc = f"{t} 圆心({p1[0]:.0f},{p1[1]:.0f}) R={r:.1f}"
        else:
            desc = f"{t} @({p1[0]:.0f},{p1[1]:.0f})"
        out.append({"type": t, "layer": layer, "dist": round(d, 1), "desc": desc})
    return out


def main():
    ap = argparse.ArgumentParser(description="提取并分类 DIMENSION 尺寸标注")
    ap.add_argument("file", help="DXF 文件路径")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而非 Markdown")
    ap.add_argument("--out", help="输出文件（默认 stdout）")
    args = ap.parse_args()

    try:
        import ezdxf
    except ImportError:
        print("错误: 未安装 ezdxf，请执行 pip3 install ezdxf", file=sys.stderr)
        sys.exit(1)

    try:
        doc = ezdxf.readfile(args.file, errors="ignore")
    except Exception as e:
        # 降级：用流式解析提取 DIMENSION 关键字段
        sys.stderr.write(f"[warn] ezdxf 解析失败（{e}），降级为流式提取。\n")
        dims = _stream_extract_dims(args.file)
        if args.json:
            out = json.dumps({
                "file": args.file, "count": len(dims),
                "by_type": _by_type(dims), "dimensions": dims,
                "degraded": True,
            }, ensure_ascii=False, indent=2)
        else:
            out = _to_markdown(args.file, dims, degraded=True)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(out + "\n")
            print(f"已写入: {args.out}", file=sys.stderr)
        else:
            print(out)
        return

    msp = doc.modelspace()

    # 解码器
    _font = None
    _font_cache = None
    if _HAVE_DECODE:
        try:
            _font, _font_cache = resolve_font(None, args.file)
        except Exception:
            pass

    def _dec(text, style=""):
        if not _HAVE_DECODE or not text:
            return text
        f = _font
        if f is None and _font_cache and style in _font_cache:
            f = _font_cache[style]
        s = decode_bigfont(text, f)
        s = clean_mtext_format(s) if "clean_mtext_format" in dir() else s
        return s.strip()

    # 收集几何对象用于关联（LINE/ARC/CIRCLE/LWPOLYLINE）
    geoms = []
    for e in msp:
        t = e.dxftype()
        try:
            if t == "LINE":
                geoms.append((t, e.dxf.layer, e.dxf.start, e.dxf.end, None))
            elif t == "CIRCLE":
                geoms.append((t, e.dxf.layer, e.dxf.center, e.dxf.radius, None))
            elif t == "ARC":
                geoms.append((t, e.dxf.layer, e.dxf.center, e.dxf.radius,
                              (e.dxf.start_angle, e.dxf.end_angle)))
            elif t == "LWPOLYLINE":
                pts = list(e.get_points())
                if pts:
                    geoms.append((t, e.dxf.layer, pts[0][:2], None, len(pts)))
        except Exception:
            pass

    # 提取 DIMENSION
    dims = []
    for e in msp:
        if e.dxftype() != "DIMENSION":
            continue
        try:
            dt = getattr(e.dxf, "dimtype", 0)
            type_code = dt & 0xF if dt else 0
            type_name = DIM_TYPE.get(type_code, f"其他({type_code})")
            pos = tuple(e.dxf.insert) if e.dxf.hasattr("insert") else (0, 0, 0)
            pos2d = (pos[0], pos[1])
            txt = getattr(e.dxf, "text", "") or ""
            if not txt or txt == "<>":
                try:
                    txt = f"{e.get_measurement():.2f}"
                except Exception:
                    txt = ""
            txt = _dec(txt, getattr(e.dxf, "style", ""))
            layer = e.dxf.layer
            # 关联最近几何
            related = _nearest_geom(pos2d, geoms, k=2)
            dims.append({
                "type": type_name,
                "text": txt,
                "layer": layer,
                "pos": [round(pos2d[0], 1), round(pos2d[1], 1)],
                "related": related,
            })
        except Exception as ex:
            sys.stderr.write(f"[warn] DIMENSION 跳过: {ex}\n")

    if args.json:
        out = json.dumps({
            "file": args.file,
            "count": len(dims),
            "by_type": _by_type(dims),
            "dimensions": dims,
        }, ensure_ascii=False, indent=2)
    else:
        out = _to_markdown(args.file, dims)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(out + "\n")
        print(f"已写入: {args.out}", file=sys.stderr)
    else:
        print(out)


def _by_type(dims):
    m = {}
    for d in dims:
        m[d["type"]] = m.get(d["type"], 0) + 1
    return m


def _to_markdown(file, dims, degraded=False):
    lines = [f"# 尺寸标注精读 — {os.path.basename(file)}", ""]
    lines.append(f"**文件**: `{file}`")
    lines.append(f"**总尺寸数**: {len(dims)}")
    if degraded:
        lines.append("> _注：ezdxf 解析失败，已降级为流式提取；类型/位置可能不完整，关联几何不可用。_")
    lines.append("")
    bt = _by_type(dims)
    if bt:
        lines.append("## 按类型统计")
        lines.append("| 类型 | 数量 |")
        lines.append("|------|------|")
        for t, n in sorted(bt.items(), key=lambda x: -x[1]):
            lines.append(f"| {t} | {n} |")
        lines.append("")
    if dims:
        lines.append("## 尺寸明细")
        lines.append("| # | 类型 | 标注值 | 图层 | 位置 | 关联几何 |")
        lines.append("|---|------|--------|------|------|----------|")
        for i, d in enumerate(dims, 1):
            related = ", ".join(r["desc"] for r in d["related"]) or "—"
            lines.append(f"| {i} | {d['type']} | {d['text']} | {d['layer']} "
                         f"| ({d['pos'][0]},{d['pos'][1]}) | {related} |")
        lines.append("")
    if not dims:
        lines.append("_未发现 DIMENSION 实体。_")
    return "\n".join(lines)


def _stream_extract_dims(path):
    """ezdxf 失败时的流式降级：从 DXF 文本逐行解析 DIMENSION 实体的关键字段。
    只提取 code 1 (文字)、code 10 (插入点)、code 70 (dimtype 标志)。
    不做几何关联。
    """
    try:
        from extract_texts_stream import _read_text, resolve_font, decode_bigfont
    except Exception:
        _read_text = lambda p: open(p, "r", encoding="utf-8", errors="ignore").read()

    text = _read_text(path)
    lines = text.splitlines()
    dims = []
    in_dim = False
    cur = {"text": "", "pos": [0.0, 0.0], "dimtype": 0, "layer": ""}
    # DXF group code 解析：line[i] 是 code，line[i+1] 是 value
    i = 0
    while i < len(lines) - 1:
        code_str = lines[i].strip()
        value = lines[i + 1]
        try:
            code = int(code_str)
        except ValueError:
            i += 1
            continue
        if code == 0 and value.strip() == "DIMENSION":
            if in_dim and (cur["text"] or cur["dimtype"]):
                t = cur["dimtype"] & 0xF
                dims.append({
                    "type": DIM_TYPE.get(t, f"其他({t})"),
                    "text": cur["text"] or "",
                    "layer": cur["layer"],
                    "pos": [round(cur["pos"][0], 1), round(cur["pos"][1], 1)],
                    "related": [],
                })
            cur = {"text": "", "pos": [0.0, 0.0], "dimtype": 0, "layer": ""}
            in_dim = True
        elif in_dim:
            if code == 1:
                cur["text"] = value.strip()
            elif code == 10:
                try:
                    cur["pos"][0] = float(value)
                except ValueError:
                    pass
            elif code == 20:
                try:
                    cur["pos"][1] = float(value)
                except ValueError:
                    pass
            elif code == 70:
                try:
                    cur["dimtype"] = int(value)
                except ValueError:
                    pass
            elif code == 8:
                cur["layer"] = value.strip()
        i += 2
    # flush 最后一个
    if in_dim and (cur["text"] or cur["dimtype"]):
        t = cur["dimtype"] & 0xF
        dims.append({
            "type": DIM_TYPE.get(t, f"其他({t})"),
            "text": cur["text"] or "",
            "layer": cur["layer"],
            "pos": [round(cur["pos"][0], 1), round(cur["pos"][1], 1)],
            "related": [],
        })
    # 尝试解码文字
    try:
        _f, _ = resolve_font(None, path)
        for d in dims:
            d["text"] = decode_bigfont(d["text"], _f).strip() or d["text"]
    except Exception:
        pass
    return dims


if __name__ == "__main__":
    main()
