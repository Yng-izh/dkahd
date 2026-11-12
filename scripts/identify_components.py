#!/usr/bin/env python3
"""构件识别（实例级 + 几何推导）。

与旧版「按块名/图层正则做类型聚合」不同，这里输出**逐个构件实例**：
图框、轴网、指北针、柱、墙、门、窗、门联窗、房间、楼梯、台阶、散水。

识别链（全部基于原生图元，无 AI / 无图像）：
  1) 墙体：WALL 图层双线面线配对 + 端部封口校验 -> 中心线/厚度/轮廓
  2) 柱：_FZH 单位方块块参照 × 缩放
  3) 门窗：块名 + 块属性 + 弧线几何；同墙线首尾相接的门/窗块归并为门联窗
  4) 轴网：_AXISO 块属性 + DOTE 通长轴线；图框：PUB_TITLE 闭合多段线 + 图签文字
  5) 指北针：圆 + 过圆心指针 + "北" 字方位
  6) 楼梯：STAIR 图层踏步线聚类；台阶：散水图层嵌套 U 形轮廓
  7) 散水：散水图层轮廓 + 到外墙面法向距离
  8) 房间：房间名文字作种子，墙/柱/隔断/洞口围合做障碍，10mm 栅格连通域 + 边界追踪

用法：
  python3 identify_components.py <文件.dwg|.dxf> [--format json|schema|markdown|files]
      [-o 输出文件] [--out-dir 目录] [--basemap 底图.png] [--no-convert]

输出格式（统一为单一 JSON 文档）：
  json      统一文档 dwg-components-records/1.1（默认）：meta / basemap /
            components / unrecognized / unmatched_blocks / scope / notes
  markdown  按类别统计的报告
  files     写出 components.json（统一文档）+ report.md
  --emit-schema  打印 JSON Schema 定义本身
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dwg_reader.pipeline import resolve_dxf  # noqa: E402
from dwg_reader.recognition import (  # noqa: E402
    build_document,
    recognize,
    render_markdown,
    schema_json,
    write_outputs,
)


def _dxf_path(source: Path, convert: bool) -> Path:
    """DXF 直接返回；DWG/T3 经 ODA（或 LibreDWG）转换后返回 DXF 路径。"""
    return resolve_dxf(source, convert=convert)


def main() -> int:
    ap = argparse.ArgumentParser(description="构件识别（实例级 + 几何推导）")
    ap.add_argument("file", nargs="?", type=Path, help="DWG 或 DXF 文件")
    ap.add_argument("--format", choices=("json", "markdown", "files"),
                    default="json", help="输出格式（默认 json，统一文档）")
    ap.add_argument("--emit-schema", action="store_true",
                    help="打印 JSON Schema 定义（dwg-components-records/1.1）")
    ap.add_argument("-o", "--out", type=Path, help="输出文件（默认 stdout）")
    ap.add_argument("--out-dir", type=Path, help="--format files 的输出目录")
    ap.add_argument("--basemap", help="底图 PNG 路径（仅写入 schema 的 basemap.file）")
    ap.add_argument("--no-convert", action="store_true", help="输入已是 DXF 时跳过转换")
    args = ap.parse_args()

    if args.emit_schema:
        print(schema_json())
        return 0
    if args.file is None:
        ap.error("必须提供图纸文件（或使用 --emit-schema 只打印 schema）")

    dxf = _dxf_path(args.file, convert=not args.no_convert)
    result = recognize(str(dxf))

    if args.format == "files":
        out_dir = args.out_dir or dxf.parent
        paths = write_outputs(result, str(dxf), str(out_dir), basemap_file=args.basemap)
        for k, v in paths.items():
            print(f"{k}: {v}", file=sys.stderr)
        print(json.dumps(result["meta"]["counts"], ensure_ascii=False))
        return 0

    if args.format == "json":
        text = json.dumps(build_document(result, str(dxf), basemap_file=args.basemap),
                          ensure_ascii=False, indent=1)
    else:
        text = render_markdown(result)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"已写入: {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
