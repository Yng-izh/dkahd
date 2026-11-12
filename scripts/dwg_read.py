#!/usr/bin/env python3
"""一站式 DWG/DXF 图纸阅读流水线。

自动完成：
  1. 若输入是 DWG，调用 ODA File Converter 转为 DXF（与源文件同目录）
  2. 解析 DXF 输出结构化报告（实体 / 图层 / 块 / 尺寸 / 范围）
  3. 提取全部文字标注（TEXT + MTEXT + 块内文字 + ATTRIB + DIMENSION）
     支持 BigFont/Unicode/%% 解码、阅读顺序还原、表格/图层聚合
  4. 可选：尺寸标注精读、构件识别

用法：
  # 单文件
  python3 dwg_read.py <图纸.dwg|图纸.dxf> [--out 报告.md]
                      [--table] [--by-layer] [--no-convert] [--font 字体.shx]
                      [--dimensions] [--components] [--layer-filter 关键词]

  # 批量目录
  python3 dwg_read.py --batch <目录> [--out-dir 输出目录]
                      [--table] [--by-layer] [--dimensions] [--components]

  --table         以 Markdown 表格还原对齐网格（如门窗表）
  --by-layer      按图层聚合输出文字
  --no-convert    跳过 DWG→DXF 转换（输入已是 DXF 时加速）
  --font          显式指定大字体文件（默认按 STYLE 表自动匹配 fonts/）
  --dimensions    追加尺寸标注精读章节
  --components    追加构件识别章节
  --layer-filter  按图层名关键词过滤（仅匹配图层的相关内容）
  --batch         批量模式：扫描目录下所有 .dwg/.dxf
  --out-dir       批量模式的输出目录（每张图一个 .md + 总览 index.md）

单文件输出默认打印到 stdout；指定 --out 则写入文件。
批量模式必写入 --out-dir，并打印总览到 stdout。
"""
import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


_SILENT_ODA_APP = os.path.expanduser("~/Library/Application Support/dsh-oda/ODAFileConverter.app")


def _resolve_oda():
    """ODA 可执行文件路径：DWG_READER_ODA_PATH > 静默副本 > 原版（可指向 .app 或可执行文件）。"""
    raw = os.environ.get("DWG_READER_ODA_PATH", "").strip()
    if not raw:
        # 静默副本带 LSUIElement，跑起来不会在 Dock 弹一下
        if os.path.isdir(_SILENT_ODA_APP):
            return os.path.join(_SILENT_ODA_APP, "Contents", "MacOS", "ODAFileConverter")
        return "/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter"
    if raw.endswith(".app"):
        return os.path.join(raw, "Contents", "MacOS", "ODAFileConverter")
    return raw


ODA = _resolve_oda()
PARSE = os.path.join(HERE, "parse_dxf.py")
STREAM = os.path.join(HERE, "extract_texts_stream.py")
DIMS = os.path.join(HERE, "extract_dimensions.py")
COMPS = os.path.join(HERE, "identify_components.py")


def _run(script, dxf, *extra):
    cmd = [sys.executable, script, dxf, *extra]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(f"[warn] {os.path.basename(script)} 失败: {r.stderr.strip()}\n")
    return r.stdout


def convert_dwg(dwg):
    """用 ODA 把 DWG 转为同目录下的 DXF，返回 DXF 路径。失败返回 None。"""
    if not os.path.exists(ODA):
        sys.stderr.write(f"[error] 未找到 ODA File Converter: {ODA}\n")
        return None
    indir = os.path.dirname(os.path.abspath(dwg)) or "."
    outdir = indir  # 与源文件同目录
    name = os.path.splitext(os.path.basename(dwg))[0]
    target = os.path.join(outdir, name + ".dxf")
    # ODA 命令行签名：输入目录 输出目录 版本 DXF 递归 审计 过滤器
    cmd = [ODA, indir, outdir, "ACAD2018", "DXF", "0", "0", "*.dwg"]
    sys.stderr.write(f"[info] 调用 ODA 转换 DWG → DXF: {dwg}\n")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(f"[error] ODA 转换失败: {r.stderr.strip() or r.stdout.strip()}\n")
        return None
    if not os.path.exists(target):
        sys.stderr.write(f"[error] ODA 未生成预期 DXF: {target}\n")
        return None
    sys.stderr.write(f"[info] 已生成 DXF: {target}\n")
    return target


def resolve_dxf(src, no_convert):
    ext = os.path.splitext(src)[1].lower()
    if ext == ".dwg":
        if no_convert:
            dxf = os.path.splitext(src)[0] + ".dxf"
            if not os.path.exists(dxf):
                sys.stderr.write(f"[error] --no-convert 但找不到 {dxf}\n")
                return None
            return dxf
        return convert_dwg(src)
    if ext == ".dxf":
        return src
    sys.stderr.write(f"[error] 不支持的文件类型: {src}（仅支持 .dwg / .dxf）\n")
    return None


def build_report(src, dxf, args):
    """构建单张图的 Markdown 报告。"""
    # 展开图层别名：--layer-filter 中的用户词经 --layer-alias 映射为实际图层片段，
    # 让 parse_dxf 与 stream 两个脚本用同一套已展开的过滤表达式
    if args.layer_filter and args.layer_alias:
        try:
            with open(args.layer_alias, "r", encoding="utf-8") as f:
                alias = json.load(f)
            pats = [p.strip() for p in args.layer_filter.split(",") if p.strip()]
            for k in pats:
                if k in alias:
                    pats.append(str(alias[k]))
            args = argparse.Namespace(**{**vars(args), "layer_filter": ",".join(pats)})
        except Exception as e:
            sys.stderr.write(f"[warn] 图层别名表读取失败: {e}\n")
    parts = []
    parts.append("# 图纸解析报告\n")
    parts.append(f"> 源文件: `{os.path.basename(src)}`  | 解析文件: `{dxf}`\n")

    # 1. 结构化报告
    parse_extra = ["--entities", "--texts", "--layers", "--blocks", "--limits"]
    if args.layer_filter:
        parse_extra += ["--layer-filter", args.layer_filter]
    report = _run(PARSE, dxf, *parse_extra)
    if report.strip():
        parts.append("## 结构化解析\n")
        parts.append(report.rstrip("\n"))
    else:
        parts.append("## 结构化解析\n")
        parts.append("> ⚠️ ezdxf 严格解析失败（文件可能损坏 / 缺 EOF）。"
                     "已跳过结构化报告，下方文字提取基于流式解析，结果仍可用。")

    # 2. 文字提取
    extra = []
    if args.table:
        extra.append("--table")
    if args.by_layer:
        extra.append("--by-layer")
    if args.font:
        extra += ["--font", args.font]
    if args.layer_filter:
        extra += ["--layer-filter", args.layer_filter]
    texts = _run(STREAM, dxf, *extra)
    if texts.strip():
        title = "## 文字标注提取"
        if args.table:
            title += "（表格）"
        elif args.by_layer:
            title += "（按图层）"
        parts.append("\n" + title + "\n")
        parts.append(texts.rstrip("\n"))

    # 3. 尺寸标注精读
    if args.dimensions:
        dim_out = _run(DIMS, dxf)
        if dim_out.strip():
            parts.append("\n## 尺寸标注精读\n")
            parts.append(dim_out.rstrip("\n"))

    # 4. 构件识别
    if args.components:
        comp_out = _run(COMPS, dxf, "--format", "markdown")
        if comp_out.strip():
            parts.append("\n## 构件识别\n")
            parts.append(comp_out.rstrip("\n"))

    return "\n".join(parts).rstrip("\n") + "\n"


def _count_summary(md):
    """从报告文本粗略统计实体数/文字条数。"""
    # 优先取结构化报告中权威的实体总数
    m = re.search(r"模型空间实体数:\s*(\d+)", md)
    if m:
        ents = int(m.group(1))
    else:
        # 流式降级输出：按实体清单的“  类型[图层]”行计数（前缀为两个空格）
        ents = (
            md.count("  LINE[") + md.count("  CIRCLE[") + md.count("  ARC[")
            + md.count("  TEXT[") + md.count("  MTEXT[") + md.count("  INSERT[")
            + md.count("  DIMENSION[") + md.count("  LWPOLYLINE[")
            + md.count("  BLOCK[") + md.count("  POINT[") + md.count("  ELLIPSE[")
        )
    texts = md.count("TEXT@") + md.count("MTEXT@") + md.count("(尺寸)")
    # 流式输出按行计
    if texts == 0 and "## 文字标注提取" in md:
        # 取该小节后的行数
        idx = md.find("## 文字标注提取")
        tail = md[idx:]
        texts = tail.count("\n") - 1
    return ents, max(texts, 0)


def run_batch(args):
    """批量模式：扫描目录下所有 .dwg/.dxf，每张图一份报告 + 总览索引。"""
    if not os.path.isdir(args.batch):
        sys.stderr.write(f"[error] --batch 需要一个目录: {args.batch}\n")
        sys.exit(1)
    if not args.out_dir:
        sys.stderr.write("[error] 批量模式必须指定 --out-dir\n")
        sys.exit(1)
    os.makedirs(args.out_dir, exist_ok=True)

    files = []
    for name in sorted(os.listdir(args.batch)):
        if name.startswith("."):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext in (".dwg", ".dxf"):
            files.append(os.path.join(args.batch, name))

    if not files:
        sys.stderr.write(f"[warn] 目录下未找到 .dwg/.dxf 文件: {args.batch}\n")
        sys.exit(0)

    sys.stderr.write(f"[info] 发现 {len(files)} 张图纸，开始批量处理...\n")
    index_rows = []
    for i, src in enumerate(files, 1):
        name = os.path.basename(src)
        stem = os.path.splitext(name)[0]
        out_md = os.path.join(args.out_dir, f"{stem}.md")
        sys.stderr.write(f"\n[{i}/{len(files)}] {name}\n")
        dxf = resolve_dxf(src, args.no_convert)
        if not dxf:
            index_rows.append((name, "转换失败", 0, 0, "—"))
            continue
        try:
            md = build_report(src, dxf, args)
            with open(out_md, "w", encoding="utf-8") as f:
                f.write(md)
            ents, texts = _count_summary(md)
            index_rows.append((name, "成功", ents, texts, os.path.basename(out_md)))
        except Exception as e:
            sys.stderr.write(f"[error] 处理失败: {e}\n")
            index_rows.append((name, f"处理失败: {e}", 0, 0, "—"))

    # 写总览索引
    index_md = os.path.join(args.out_dir, "index.md")
    lines = ["# 批量图纸解析总览", "",
             f"**目录**: `{args.batch}`",
             f"**图数**: {len(files)}",
             f"**成功**: {sum(1 for r in index_rows if r[1] == '成功')}",
             f"**失败**: {sum(1 for r in index_rows if r[1] != '成功')}",
             "", "## 明细", "",
             "| # | 文件 | 状态 | 实体数 | 文字条数 | 报告 |",
             "|---|------|------|--------|----------|------|"]
    for i, (name, status, ents, texts, rep) in enumerate(index_rows, 1):
        link = f"[{rep}]({rep})" if rep != "—" else "—"
        lines.append(f"| {i} | {name} | {status} | {ents} | {texts} | {link} |")
    with open(index_md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    sys.stderr.write(f"\n[info] 总览已写入: {index_md}\n")
    # 同时打印总览到 stdout
    print("\n".join(lines))


def main():
    ap = argparse.ArgumentParser(description="DWG/DXF 图纸阅读一站式流水线")
    ap.add_argument("file", nargs="?", help="DWG 或 DXF 文件路径（与 --batch 二选一）")
    ap.add_argument("--batch", help="批量模式：扫描目录下所有 .dwg/.dxf")
    ap.add_argument("--out-dir", help="批量模式的输出目录")
    ap.add_argument("--out", help="单文件报告输出文件（默认 stdout）")
    ap.add_argument("--table", action="store_true", help="Markdown 表格还原")
    ap.add_argument("--by-layer", action="store_true", help="按图层聚合文字")
    ap.add_argument("--no-convert", action="store_true", help="跳过 DWG→DXF 转换")
    ap.add_argument("--font", help="显式指定大字体文件")
    ap.add_argument("--dimensions", action="store_true", help="追加尺寸标注精读章节")
    ap.add_argument("--components", action="store_true", help="追加构件识别章节")
    ap.add_argument("--layer-filter", help="按图层名关键词过滤（保留匹配图层的内容）")
    ap.add_argument("--layer-alias", help="图层别名表 JSON（{用户词: 实际图层片段}）")
    args = ap.parse_args()

    if args.batch:
        run_batch(args)
        return

    if not args.file:
        ap.error("必须提供 file 或 --batch")

    dxf = resolve_dxf(args.file, args.no_convert)
    if not dxf:
        sys.exit(1)

    blob = build_report(args.file, dxf, args)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(blob)
        sys.stderr.write(f"[info] 报告已写入: {args.out}\n")
    else:
        print(blob)


if __name__ == "__main__":
    main()
