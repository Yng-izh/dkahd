"""Unified command-line interface."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from . import __version__
from .backends import convert_dwg_file, doctor_report, openscad_extrude
from .fonts import get_font
from .pipeline import ReadOptions, read_batch, read_drawing
from .quantity import build_quantity, export, quantity_markdown
from .report import inspect_report, text_dict
from .stream import extract_texts

DEFAULT_ODA = Path("/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter")


def _add_read_options(parser: argparse.ArgumentParser) -> None:
    """Options shared by ``read`` and ``quantity``."""
    parser.add_argument("--table", action="store_true", help="还原对齐网格为 Markdown 表格")
    parser.add_argument("--by-layer", action="store_true", help="按图层聚合输出文字")
    parser.add_argument("--dimensions", action="store_true", help="追加尺寸标注精读")
    parser.add_argument("--components", action="store_true", help="追加构件识别")
    parser.add_argument(
        "--layer-matches",
        action="store_true",
        help="构件识别额外按图层名匹配（每图层计 1 个，避免大量重复计数）",
    )
    parser.add_argument("--layer-filter", help="按图层名关键词/正则过滤（逗号分隔）")
    parser.add_argument("--layer-alias", type=Path, help="图层别名表 JSON（{用户词: 图层片段}）")
    parser.add_argument("--font", help="显式指定 SHX 大字体（默认按 STYLE 表自动匹配）")
    parser.add_argument("--rules", type=Path, help="自定义构件规则 JSON")
    parser.add_argument("--no-convert", action="store_true", help="跳过 DWG→DXF 转换")
    parser.add_argument("--backend", choices=("auto", "oda", "libredwg"), default="auto")
    parser.add_argument("--oda", type=Path, help="ODA File Converter 可执行文件路径")
    parser.add_argument("--max-depth", type=int, default=8, help="块嵌套展开最大深度")


def _read_options(args: argparse.Namespace) -> ReadOptions:
    alias = None
    if getattr(args, "layer_alias", None):
        try:
            alias = json.loads(args.layer_alias.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"图层别名表读取失败: {exc}") from exc
    return ReadOptions(
        table=getattr(args, "table", False),
        by_layer=getattr(args, "by_layer", False),
        dimensions=getattr(args, "dimensions", False),
        components=getattr(args, "components", False),
        layer_filter=getattr(args, "layer_filter", None),
        layer_alias=alias,
        layer_matches=getattr(args, "layer_matches", False),
        font=getattr(args, "font", None),
        rules=str(args.rules) if getattr(args, "rules", None) else None,
        max_depth=getattr(args, "max_depth", 8),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dwg-reader", description="Read DWG/DXF drawings")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")

    read = commands.add_parser("read", help="one-shot Markdown/JSON report (recommended)")
    read.add_argument("input", nargs="?", type=Path, help="DWG or DXF file")
    read.add_argument("-o", "--output", type=Path, help="write the report here (default stdout)")
    read.add_argument("--format", choices=("markdown", "json"), default="markdown")
    read.add_argument("--batch", type=Path, help="process every drawing in this directory")
    read.add_argument("--out-dir", type=Path, help="output directory for batch mode")
    read.add_argument("--recursive", action="store_true", help="recurse into subdirectories")
    read.add_argument("--entities", action="store_true", help="include entity list in JSON output")
    _add_read_options(read)

    comps = commands.add_parser("components", help="构件识别（实例级 + 几何推导）")
    comps.add_argument("input", nargs="?", type=Path, help="DWG or DXF file")
    comps.add_argument("-o", "--output", type=Path, help="write the result here (default stdout)")
    comps.add_argument("--format", choices=("json", "markdown", "files"),
                       default="json", help="json | markdown | files")
    comps.add_argument("--emit-schema", action="store_true",
                       help="print the JSON Schema definition instead of a drawing")
    comps.add_argument("--out-dir", type=Path, help="output directory for --format files")
    comps.add_argument("--basemap", help="basemap PNG path recorded in the schema output")
    comps.add_argument("--no-convert", action="store_true", help="input is already DXF")
    # ---- AI 增强（默认关闭：不给这个参数就与老行为逐条一致）
    comps.add_argument("--ai-mode", choices=("off", "propose", "enhance"), default="off",
                       help="off=纯几何（默认）| propose=只出提议 | enhance=试验通过才合并")
    comps.add_argument("--ai-config", type=Path,
                       help="AI 增强配置文件（JSON，见 ai-recognition-config/1.0）")
    comps.add_argument("--ai-memory-dir", type=Path,
                       help="规则记忆库目录（默认取配置里的 memory.directory）")
    comps.add_argument("--ai-diagnostics", type=Path,
                       help="实验诊断日志（JSONL，一行一次试验）")
    # 视觉链路：本地渲染 ROI 小图发给多模态模型（默认跟随配置；这里可强开/强关）
    comps.add_argument("--ai-vision", dest="ai_vision", action="store_true",
                       default=None, help="强制打开视觉链路（发 ROI 小图给多模态模型）")
    comps.add_argument("--no-ai-vision", dest="ai_vision", action="store_false",
                       default=None, help="强制关闭视觉链路（只用结构特征）")

    eval_ = commands.add_parser("ai-eval",
                                help="独立真值集对照：基础模式 vs 增强模式")
    eval_.add_argument("--manifest", type=Path, required=True,
                       help="清单 JSON：{drawings:[{path, gt}], project}")
    eval_.add_argument("--out", type=Path, help="评估结果 JSON")
    eval_.add_argument("--ai-mode", choices=("off", "propose", "enhance"), default="off")
    eval_.add_argument("--ai-config", type=Path)
    eval_.add_argument("--ai-vision", dest="ai_vision", action="store_true",
                       default=None)
    eval_.add_argument("--no-ai-vision", dest="ai_vision", action="store_false",
                       default=None)

    quantity = commands.add_parser("quantity", help="export a quantity take-off (CSV/XLSX)")
    quantity.add_argument("input", type=Path, help="DWG or DXF file")
    quantity.add_argument("-o", "--output", type=Path, help="output file (.csv or .xlsx)")
    quantity.add_argument("--format", choices=("csv", "xlsx"), help="override output format")
    quantity.add_argument(
        "--sections",
        default="components,layers,dimensions",
        help="comma separated sections: components,layers,dimensions",
    )
    _add_read_options(quantity)

    text = commands.add_parser("text", help="stream text from a DXF file")
    text.add_argument("input", type=Path)
    text.add_argument("legacy_output", nargs="?", type=Path, help=argparse.SUPPRESS)
    text.add_argument("-o", "--output", type=Path)
    text.add_argument("--font", help="SHX path or bundled font name")
    text.add_argument("--with-metadata", action="store_true")
    text.add_argument("--format", choices=("text", "json"), default="text")
    text.add_argument("--expand-blocks", action="store_true")
    text.add_argument("--max-depth", type=int, default=8)

    inspect = commands.add_parser("inspect", help="inspect entities using ezdxf")
    inspect.add_argument("input", type=Path)
    inspect.add_argument("--entities", action="store_true")
    inspect.add_argument("--texts", action="store_true")
    inspect.add_argument("--layers", action="store_true")
    inspect.add_argument("--blocks", action="store_true")
    inspect.add_argument("--limits", action="store_true")
    inspect.add_argument("-o", "--output", type=Path)
    inspect.add_argument("--format", choices=("text", "json"), default="text")
    inspect.add_argument("--expand-blocks", "--explode-blocks", action="store_true")
    inspect.add_argument("--max-depth", type=int, default=8)

    convert = commands.add_parser("convert", help="convert a DWG file or directory to DXF")
    convert.add_argument("input", type=Path)
    convert.add_argument("output", type=Path)
    convert.add_argument("--version", default="ACAD2018")
    convert.add_argument("--recursive", action="store_true")
    convert.add_argument("--audit", action="store_true")
    convert.add_argument("--oda", type=Path, default=DEFAULT_ODA)
    convert.add_argument("--backend", choices=("auto", "oda", "libredwg"), default="auto")

    font = commands.add_parser("font", help="inspect a bundled or explicit SHX font")
    font.add_argument("name")
    font.add_argument("codes", nargs="*")

    doctor = commands.add_parser("doctor", help="detect optional CAD backends")
    doctor.add_argument("--format", choices=("text", "json"), default="text")

    scad = commands.add_parser("openscad", help="extrude a 2D DXF with OpenSCAD")
    scad.add_argument("input", type=Path)
    scad.add_argument("output", type=Path)
    scad.add_argument("--height", type=float, default=10.0)
    scad.add_argument("--layer")
    scad.add_argument("--convexity", type=int, default=10)
    scad.add_argument("--timeout", type=int, default=300)
    return parser


def _configure_stdio() -> None:
    """Keep legacy consoles (e.g. Windows cp936) from crashing on Ø/° etc."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            continue


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return 2
    _configure_stdio()
    try:
        if args.command == "read":
            return _read(args)
        if args.command == "components":
            return _components(args)
        if args.command == "ai-eval":
            return _ai_eval(args)
        if args.command == "quantity":
            return _quantity(args)
        if args.command == "text":
            return _text(args)
        if args.command == "convert":
            return _convert(args)
        if args.command == "font":
            return _font(args)
        if args.command == "doctor":
            return _doctor(args)
        if args.command == "openscad":
            return _openscad(args)
        return _inspect(args)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _progress(index: int, total: int, source: Path) -> None:
    print(f"[{index}/{total}] {source.name}", file=sys.stderr)


def _read(args: argparse.Namespace) -> int:
    options = _read_options(args)
    options.include_entity_list = getattr(args, "entities", False)
    convert = not args.no_convert
    if args.batch:
        if not args.out_dir:
            raise ValueError("批量模式必须指定 --out-dir")
        result = read_batch(
            args.batch,
            args.out_dir,
            options,
            recursive=args.recursive,
            convert=convert,
            oda_path=args.oda,
            backend=args.backend,
            progress=_progress,
        )
        print(result.index_markdown())
        print(f"总览已写入: {result.index_path}", file=sys.stderr)
        return 0 if result.failed == 0 else 1
    if not args.input:
        raise ValueError("必须提供图纸文件或 --batch 目录")
    result = read_drawing(
        args.input,
        options,
        convert=convert,
        oda_path=args.oda,
        backend=args.backend,
    )
    output = (
        json.dumps(result.payload, ensure_ascii=False, indent=2)
        if args.format == "json"
        else result.markdown
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
        print(f"报告已写入: {args.output}", file=sys.stderr)
    else:
        print(output)
    return 0


def _components(args: argparse.Namespace) -> int:
    """识别构件并输出实例级结果（JSON / schema / Markdown / 落盘）。"""
    import json as _json

    from .pipeline import resolve_dxf
    from .recognition import build_document, recognize, render_markdown, schema_json, write_outputs

    if args.emit_schema:
        print(schema_json())
        return 0
    if args.input is None:
        raise ValueError("必须提供图纸文件（或使用 --emit-schema 只打印 schema）")

    dxf = resolve_dxf(args.input, convert=not args.no_convert)

    ai_mode = getattr(args, "ai_mode", "off") or "off"
    if ai_mode == "off":
        result = recognize(str(dxf))
    else:
        # 走编排 API：AI 增强必须能拿到参数/统计/审计，recognize() 的签名保持不变
        from .recognition import run_recognition
        overrides = {}
        if getattr(args, "ai_vision", None) is not None:
            overrides["provider"] = {
                "vision": bool(args.ai_vision),
                "allow_remote_images": bool(args.ai_vision)}
        ai_arg = None
        if getattr(args, "ai_config", None):
            from .recognition import ai_config as AIC
            ai_arg = AIC.load(str(args.ai_config), mode=ai_mode,
                              overrides=overrides or None)
        elif overrides:
            ai_arg = dict(overrides)
        run = run_recognition(
            str(dxf), jobs=1, targets=("export",),
            ai_mode=ai_mode,
            ai_config=ai_arg,
            ai_memory_dir=(str(args.ai_memory_dir)
                           if getattr(args, "ai_memory_dir", None) else None),
            ai_diagnostics=(str(args.ai_diagnostics)
                            if getattr(args, "ai_diagnostics", None) else None))
        doc = run.output
        if args.format == "json" or args.format == "files":
            text = _json.dumps(doc, ensure_ascii=False, indent=1)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(text, encoding="utf-8")
                print(f"已写入: {args.output}", file=sys.stderr)
            else:
                print(text)
            return 0
        result = doc
    if args.format == "files":
        out_dir = args.out_dir or dxf.parent
        for key, path in write_outputs(result, str(dxf), str(out_dir),
                                       basemap_file=args.basemap).items():
            print(f"{key}: {path}", file=sys.stderr)
        print(_json.dumps(result["meta"]["counts"], ensure_ascii=False))
        return 0

    if args.format == "json":
        text = _json.dumps(build_document(result, str(dxf), basemap_file=args.basemap),
                           ensure_ascii=False, indent=1)
    else:
        text = render_markdown(result)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"已写入: {args.output}", file=sys.stderr)
    else:
        print(text)
    return 0


def _ai_eval(args: argparse.Namespace) -> int:
    """独立真值集对照（按**项目**隔离，避免同项目块族泄漏）。"""
    import json as _json

    from .recognition import evaluation as EV
    from .recognition import ai_config as AIC

    manifest = _json.loads(args.manifest.read_text(encoding="utf-8"))
    overrides = {}
    if getattr(args, "ai_vision", None) is not None:
        overrides["provider"] = {"vision": bool(args.ai_vision),
                                 "allow_remote_images": bool(args.ai_vision)}
    cfg = AIC.load(str(args.ai_config) if args.ai_config else None,
                   mode=args.ai_mode or "off", overrides=overrides or None)
    report = EV.run_manifest(manifest, ai_mode=cfg.get("mode") or "off", ai_config=cfg)
    text = _json.dumps(report, ensure_ascii=False, indent=1)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"已写入: {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


def _quantity(args: argparse.Namespace) -> int:
    options = _read_options(args)
    options.dimensions = True
    options.components = True
    report = build_quantity(
        args.input,
        options=options,
        convert=not args.no_convert,
        oda_path=args.oda,
        backend=args.backend,
    )
    sections = [part.strip() for part in args.sections.split(",") if part.strip()]
    if args.output:
        written = export(report, args.output, fmt=args.format, sections=sections)
        print(f"算量清单已写入: {written}", file=sys.stderr)
        return 0
    print(quantity_markdown(report))
    return 0


def _text(args: argparse.Namespace) -> int:
    explicit = get_font(args.font) if args.font else None
    if args.font and explicit is None:
        raise ValueError(f"font not found or unsupported: {args.font}")
    if args.expand_blocks:
        report = inspect_report(args.input, expand_blocks=True, max_depth=args.max_depth)
        records_json = report["texts"]
    else:
        records = extract_texts(args.input, font=explicit)
        records_json = [text_dict(record) for record in records]
    if args.format == "json":
        output = json.dumps({"texts": records_json}, ensure_ascii=False, indent=2)
    elif args.with_metadata:
        output = "\n".join(
            f"{r['y']:g}\t{r['x']:g}\t{r['entity']}\t{r['layer']}\t{r['text']}"
            for r in records_json
        )
    else:
        output = "\n".join(f"[{record['text']}]" for record in records_json)
    output_path = args.output or args.legacy_output
    if output_path:
        output_path.write_text(output, encoding="utf-8")
    else:
        print(output)
    print(f"extracted {len(records_json)} text records", file=sys.stderr)
    return 0


def _convert(args: argparse.Namespace) -> int:
    if args.input.is_file():
        result = convert_dwg_file(
            args.input,
            args.output,
            backend=args.backend,
            oda_path=args.oda,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if not args.oda.is_file():
        raise ValueError(f"ODA File Converter not found: {args.oda}")
    source = args.input.resolve()
    if source.is_file():
        input_dir, pattern = source.parent, source.name
    elif source.is_dir():
        input_dir, pattern = source, "*.dwg"
    else:
        raise ValueError(f"input not found: {source}")
    args.output.mkdir(parents=True, exist_ok=True)
    command = [
        str(args.oda),
        str(input_dir),
        str(args.output.resolve()),
        args.version,
        "DXF",
        str(int(args.recursive)),
        str(int(args.audit)),
        pattern,
    ]
    return subprocess.run(command, check=False).returncode


def _font(args: argparse.Namespace) -> int:
    font = get_font(args.name)
    if not font:
        raise ValueError(f"font not found or unsupported: {args.name}")
    kind = "bigfont" if font.is_bigfont else "unifont"
    print(f"{font.name}: type={kind}, encoding={font.encoding}, shapes={len(font)}")
    for token in args.codes:
        code = int(token, 16)
        print(f"0x{code:04X}\t{font.get_char(code) or ''}")
    return 0


def _doctor(args: argparse.Namespace) -> int:
    report = doctor_report()
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    for name, status in report["backends"].items():
        state = "ok" if status["available"] else "missing"
        detail = status["path"] or status["purpose"]
        print(f"{name}\t{state}\t{detail}")
    for name in ("python", "ezdxf", "mcp"):
        status = report[name]
        print(
            f"{name}\t{'ok' if status['available'] else 'missing'}\t{status.get('version') or ''}"
        )
    return 0


def _openscad(args: argparse.Namespace) -> int:
    result = openscad_extrude(
        args.input,
        args.output,
        height=args.height,
        layer=args.layer,
        convexity=args.convexity,
        timeout=args.timeout,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _inspect(args: argparse.Namespace) -> int:
    report = inspect_report(args.input, expand_blocks=args.expand_blocks, max_depth=args.max_depth)
    if args.format == "json":
        output = json.dumps(report, ensure_ascii=False, indent=2)
    else:
        drawing = report["drawing"]
        lines = [
            f"file: {drawing['file']}",
            f"version: {drawing['dxf_version']}",
            f"entities: {drawing['entity_count']}",
            f"types: {drawing['entity_types']}",
        ]
        if args.layers:
            lines.extend(
                f"layer\t{x['name']}\tcolor={x['color']}\tlinetype={x['linetype']}"
                for x in report["layers"]
            )
        if args.blocks:
            lines.extend(
                f"block\t{x['name']}\tentities={x['entity_count']}" for x in report["blocks"]
            )
        if args.texts:
            lines.extend(f"text\t{x['x']:g},{x['y']:g}\t{x['text']}" for x in report["texts"])
        if args.limits:
            lines.append(f"limits\t{drawing['extents']['min']}\t{drawing['extents']['max']}")
        if args.entities:
            lines.append("entity details are available in JSON output")
        output = "\n".join(lines)
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
