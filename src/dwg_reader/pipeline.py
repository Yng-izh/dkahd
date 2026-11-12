"""One-shot drawing reading pipeline producing Markdown or JSON reports.

This is the module behind ``dwg-reader read`` and the MCP ``read_drawing`` tool.
It ties together DXF parsing, SHX aware text decoding, layout ordering, table
recovery, dimension reading and component identification.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .backends import convert_dwg_file
from .components import (
    ComponentReport,
    component_markdown,
    identify_components,
    load_rules,
)
from .dimensions import DimensionReport, dimension_markdown, read_dimensions
from .dxf import TEXT_KINDS, Drawing, parse_dxf, summarize
from .fonts import ShxFont, get_font
from .layout import TextItem, group_by_layer, reorder_by_columns, rows_layout, to_markdown_table
from .text import decode_cad_text
from .tianzheng import TianzhengReport, detect_tianzheng, tianzheng_markdown

SUPPORTED_SUFFIXES = (".dxf", ".dwg", ".t3")

#: Entity types kept for text-oriented reads (no dimension geometry needed).
TEXT_KEEP = frozenset({"TEXT", "MTEXT", "ATTRIB", "DIMENSION", "INSERT", "SEQEND"})


# ---------------------------------------------------------------------------
# options and results
# ---------------------------------------------------------------------------


@dataclass
class ReadOptions:
    """Knobs shared by the CLI, batch mode and the MCP server."""

    table: bool = False
    by_layer: bool = False
    dimensions: bool = False
    components: bool = False
    layer_filter: str | None = None
    layer_alias: dict[str, str] | None = None
    layer_matches: bool = False
    detect_tianzheng: bool = True
    font: str | None = None
    rules: str | None = None
    max_depth: int = 8
    include_block_definitions: bool = True
    include_entity_list: bool = False


@dataclass
class ReadResult:
    """Outcome of reading a single drawing."""

    source: str
    dxf: str
    markdown: str
    payload: dict[str, Any] = field(default_factory=dict)
    text_count: int = 0
    entity_count: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class BatchItem:
    """Per-file outcome inside a batch run."""

    source: str
    status: str
    entity_count: int = 0
    text_count: int = 0
    report: str = ""
    error: str = ""


@dataclass
class BatchResult:
    """Outcome of reading a directory of drawings."""

    directory: str
    out_dir: str
    items: list[BatchItem] = field(default_factory=list)
    index_path: str = ""

    @property
    def succeeded(self) -> int:
        return sum(1 for item in self.items if item.status == "成功")

    @property
    def failed(self) -> int:
        return len(self.items) - self.succeeded

    def index_markdown(self) -> str:
        lines = [
            "# 批量图纸解析总览",
            "",
            f"**目录**: `{self.directory}`",
            f"**图数**: {len(self.items)}",
            f"**成功**: {self.succeeded}",
            f"**失败**: {self.failed}",
            "",
            "## 明细",
            "",
            "| # | 文件 | 状态 | 实体数 | 文字条数 | 报告 |",
            "|---|------|------|--------|----------|------|",
        ]
        for index, item in enumerate(self.items, 1):
            link = f"[{Path(item.report).name}]({Path(item.report).name})" if item.report else "—"
            status = item.status if not item.error else f"{item.status}: {item.error}"
            lines.append(
                f"| {index} | {Path(item.source).name} | {status} | "
                f"{item.entity_count} | {item.text_count} | {link} |"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def resolve_layer_filter(
    spec: str | None,
    alias: dict[str, str] | None = None,
) -> re.Pattern[str] | None:
    """Build the layer filter regex, expanding user words through ``alias``."""
    if not spec:
        return None
    patterns = [part.strip() for part in spec.split(",") if part.strip()]
    expanded: list[str] = []
    for pattern in patterns:
        expanded.append(pattern)
        if alias and pattern in alias:
            expanded.append(str(alias[pattern]))
    if not expanded:
        return None
    try:
        return re.compile("|".join(expanded), re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"invalid layer filter {spec!r}: {exc}") from exc


def build_decoder(
    drawing: Drawing,
    font: str | Path | ShxFont | None = None,
) -> Callable[[str, str, str], str]:
    """Return a ``decode(text, style, kind)`` callable bound to the drawing fonts."""
    explicit: ShxFont | None = None
    if isinstance(font, ShxFont):
        explicit = font
    elif font:
        explicit = get_font(font)
        if explicit is None:
            raise ValueError(f"font not found or unsupported: {font}")

    cache: dict[str, ShxFont] = {}
    if explicit is None:
        for style, filename in drawing.styles.items():
            loaded = get_font(filename)
            if loaded is not None:
                cache[style] = loaded

    def decode(raw: str, style: str = "", kind: str = "TEXT") -> str:
        active = explicit or cache.get(style)
        return decode_cad_text(raw, active, mtext=(kind == "MTEXT"))

    return decode


def resolve_dxf(
    source: str | Path,
    *,
    convert: bool = True,
    oda_path: str | Path | None = None,
    backend: str = "auto",
) -> Path:
    """Return the DXF path for ``source``, converting DWG when needed.

    Tianzheng ``.t3``/``.dwt`` files are plain Autodesk DWG containers, so they
    go through the same conversion path as ``.dwg``.
    """
    path = Path(source)
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在: {path}")
    suffix = path.suffix.lower()
    if suffix == ".dxf":
        return path
    if suffix not in (".dwg", ".t3", ".dwt"):
        raise ValueError(f"不支持的格式: {path.suffix}（仅支持 .dwg / .t3 / .dxf）")
    if not convert:
        candidate = path.with_suffix(".dxf")
        if candidate.is_file():
            return candidate
        raise FileNotFoundError(f"已禁用转换，但找不到同名 DXF: {candidate}")
    result = convert_dwg_file(path, path.parent, backend=backend, oda_path=oda_path)
    return Path(result["output"])


def extract_text_items(
    drawing: Drawing,
    decode: Callable[[str, str, str], str],
    *,
    layer_filter: re.Pattern[str] | None = None,
    max_depth: int = 8,
    include_block_definitions: bool = True,
) -> list[TextItem]:
    """Collect decoded text placed on the drawing, in reading order."""
    items: list[TextItem] = []
    for placed in _placed_iter(
        drawing,
        max_depth=max_depth,
        include_block_definitions=include_block_definitions,
    ):
        if placed.kind not in TEXT_KINDS:
            continue
        if layer_filter is not None and not layer_filter.search(placed.layer or ""):
            continue
        text = decode(placed.text, placed.style, placed.kind)
        if not text or text == "<>":
            continue
        items.append(
            TextItem(
                text=text,
                x=placed.pos[0],
                y=placed.pos[1],
                layer=placed.layer,
                kind=placed.kind,
            )
        )
    return reorder_by_columns(items)


def _placed_iter(drawing: Drawing, *, max_depth: int, include_block_definitions: bool):
    from .dxf import iter_placed

    return iter_placed(
        drawing,
        max_depth=max_depth,
        include_block_definitions=include_block_definitions,
    )


# ---------------------------------------------------------------------------
# markdown rendering
# ---------------------------------------------------------------------------


def _overview_markdown(drawing: Drawing, source: str, dxf: str) -> list[str]:
    summary = summarize(drawing)
    lines = [
        f"> 源文件: `{Path(source).name}`  | 解析文件: `{Path(dxf).name}`",
        "",
        "## 图纸概览",
        "",
        f"- DXF 版本: `{drawing.version or '未知'}`",
        f"- 模型空间实体数: {drawing.entity_count}",
        f"- 块定义数: {len(drawing.blocks)}",
        f"- 图层数: {len(drawing.layers)}",
        f"- 块内实体数: {sum(drawing.block_counts.values())}",
        "",
    ]
    if drawing.counts:
        lines.extend(["### 实体类型统计", "", "| 类型 | 数量 |", "|------|------|"])
        for kind, count in sorted(drawing.counts.items(), key=lambda pair: -pair[1]):
            lines.append(f"| {kind} | {count} |")
        lines.append("")
    if drawing.layers:
        lines.extend(
            [
                "### 图层",
                "",
                "| 图层 | 颜色 | 线型 | 状态 |",
                "|------|------|------|------|",
            ]
        )
        for layer in drawing.layers:
            flags = []
            if layer.off:
                flags.append("关闭")
            if layer.frozen:
                flags.append("冻结")
            lines.append(
                f"| {layer.name} | {layer.color} | {layer.linetype} | {'/'.join(flags) or '可见'} |"
            )
        lines.append("")
    named_blocks = [block for block in drawing.blocks.values() if not block.anonymous]
    if named_blocks:
        lines.extend(
            ["### 块定义（前 50）", "", "| 块名 | 实体数 | 基点 |", "|------|--------|------|"]
        )
        for block in named_blocks[:50]:
            lines.append(
                f"| {block.name} | {len(block.entities)} "
                f"| ({block.base[0]:.1f}, {block.base[1]:.1f}) |"
            )
        if len(named_blocks) > 50:
            lines.append(f"| … | 其余 {len(named_blocks) - 50} 个 | — |")
        lines.append("")
    if summary["warnings"]:
        lines.extend(["### 解析警告", ""])
        lines.extend(f"- {warning}" for warning in summary["warnings"])
        lines.append("")
    return lines


def _texts_markdown(
    items: Sequence[TextItem],
    *,
    options: ReadOptions,
) -> list[str]:
    if not items:
        return ["## 文字标注提取", "", "_未提取到文字实体。_", ""]
    title = "## 文字标注提取"
    if options.table:
        title += "（表格）"
    elif options.by_layer:
        title += "（按图层）"
    lines = [title, ""]
    if options.table:
        table = to_markdown_table(items)
        if table:
            lines.extend([table, ""])
            return lines
        lines.extend(["> 未检测到规则表格，已按行还原二维布局（同一行的文字用 `|` 分隔）。", ""])
        lines.extend(f"- {row}" for row in rows_layout(items))
        lines.append("")
        return lines
    if options.by_layer:
        for layer, group in sorted(group_by_layer(items).items()):
            lines.extend([f"### 图层: {layer}", ""])
            lines.extend(f"- {item.text}" for item in group)
            lines.append("")
        return lines
    lines.extend(f"- {item.text}" for item in items)
    lines.append("")
    return lines


def render_markdown(
    drawing: Drawing,
    *,
    source: str,
    dxf: str,
    items: Sequence[TextItem],
    options: ReadOptions,
    dimensions: DimensionReport | None = None,
    components: ComponentReport | None = None,
    tianzheng: TianzhengReport | None = None,
) -> str:
    """Compose the full Markdown report."""
    lines = ["# 图纸解析报告", ""]
    if tianzheng is not None and tianzheng.severity in ("warning", "severe"):
        lines.extend(
            [
                f"> ⚠️ **天正图纸警告**：{tianzheng.summary}",
                "> 处理方式见文末「天正图纸检测」章节。",
                "",
            ]
        )
    lines.extend(_overview_markdown(drawing, source, dxf))
    lines.extend(_texts_markdown(items, options=options))
    if dimensions is not None:
        lines.extend(["## 尺寸标注精读", "", dimension_markdown(dimensions), ""])
    if components is not None:
        lines.extend(["## 构件识别", "", component_markdown(components), ""])
    if tianzheng is not None:
        lines.extend(["## 天正图纸检测", "", tianzheng_markdown(tianzheng), ""])
    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# main entry points
# ---------------------------------------------------------------------------


def read_drawing(
    source: str | Path,
    options: ReadOptions | None = None,
    *,
    convert: bool = True,
    oda_path: str | Path | None = None,
    backend: str = "auto",
) -> ReadResult:
    """Read one DWG/DXF file and build a Markdown report plus a JSON payload."""
    options = options or ReadOptions()
    dxf_path = resolve_dxf(source, convert=convert, oda_path=oda_path, backend=backend)
    # Geometry entities are only materialised when a consumer needs them;
    # this roughly halves memory on multi-hundred-megabyte DXF files.
    keep = None if options.dimensions else TEXT_KEEP
    drawing = parse_dxf(dxf_path, keep=keep)
    decode = build_decoder(drawing, options.font)
    layer_filter = resolve_layer_filter(options.layer_filter, options.layer_alias)

    items = extract_text_items(
        drawing,
        decode,
        layer_filter=layer_filter,
        max_depth=options.max_depth,
        include_block_definitions=options.include_block_definitions,
    )

    dimensions = None
    if options.dimensions:
        dimensions = read_dimensions(
            drawing,
            decode=lambda text, style: decode(text, style, "TEXT"),
            layer_filter=layer_filter,
        )

    components = None
    if options.components:
        rules = load_rules(options.rules)
        components = identify_components(
            drawing,
            rules=rules,
            layer_filter=layer_filter,
            layer_matches=options.layer_matches,
        )

    tianzheng = detect_tianzheng(drawing) if options.detect_tianzheng else None

    markdown = render_markdown(
        drawing,
        source=str(source),
        dxf=str(dxf_path),
        items=items,
        options=options,
        dimensions=dimensions,
        components=components,
        tianzheng=tianzheng,
    )

    payload: dict[str, Any] = {
        "drawing": summarize(drawing),
        "texts": [
            {
                "text": item.text,
                "x": round(item.x, 3),
                "y": round(item.y, 3),
                "layer": item.layer,
                "kind": item.kind,
            }
            for item in items
        ],
        "text_count": len(items),
    }
    if dimensions is not None:
        payload["dimensions"] = dimensions.to_dict()
    if components is not None:
        payload["components"] = components.to_dict()
    if tianzheng is not None:
        payload["tianzheng"] = tianzheng.to_dict()
    if options.include_entity_list:
        payload["entities"] = _entity_payload(drawing)

    return ReadResult(
        source=str(source),
        dxf=str(dxf_path),
        markdown=markdown,
        payload=payload,
        text_count=len(items),
        entity_count=drawing.entity_count,
        warnings=list(drawing.warnings),
    )


def _entity_payload(drawing: Drawing) -> list[dict[str, Any]]:
    from .dxf import iter_placed

    entities = []
    for placed in iter_placed(drawing, include_inserts=True):
        entities.append(
            {
                "kind": placed.kind,
                "layer": placed.layer,
                "block": placed.block,
                "x": round(placed.pos[0], 3),
                "y": round(placed.pos[1], 3),
                "points": [[round(x, 3), round(y, 3)] for x, y in placed.points],
                "radius": round(placed.radius, 3),
                "text": placed.text,
            }
        )
    return entities


def iter_drawing_files(directory: str | Path, *, recursive: bool = False) -> list[Path]:
    """List drawing files in a directory, optionally descending into subfolders."""
    root = Path(directory)
    if not root.is_dir():
        raise NotADirectoryError(f"不是目录: {root}")
    pattern = "**/*" if recursive else "*"
    files = [
        path
        for path in sorted(root.glob(pattern))
        if path.is_file()
        and path.suffix.lower() in SUPPORTED_SUFFIXES
        and not path.name.startswith(".")
    ]
    return files


def read_batch(
    directory: str | Path,
    out_dir: str | Path,
    options: ReadOptions | None = None,
    *,
    recursive: bool = False,
    convert: bool = True,
    oda_path: str | Path | None = None,
    backend: str = "auto",
    progress: Callable[[int, int, Path], None] | None = None,
) -> BatchResult:
    """Read every drawing in ``directory`` and write one report per file."""
    options = options or ReadOptions()
    root = Path(directory)
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    files = iter_drawing_files(root, recursive=recursive)
    result = BatchResult(directory=str(root), out_dir=str(target))
    used_names: set[str] = set()

    for index, source in enumerate(files, 1):
        if progress is not None:
            progress(index, len(files), source)
        # ``plan.dwg`` and ``plan.dxf`` must not overwrite each other.
        name = f"{source.stem}.md"
        if name.lower() in used_names:
            extension = source.suffix.lstrip(".").lower() or "file"
            name = f"{source.stem}_{extension}.md"
            counter = 2
            while name.lower() in used_names:
                name = f"{source.stem}_{extension}_{counter}.md"
                counter += 1
        used_names.add(name.lower())
        report_path = target / name
        try:
            read_result = read_drawing(
                source,
                options,
                convert=convert,
                oda_path=oda_path,
                backend=backend,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            result.items.append(
                BatchItem(
                    source=str(source),
                    status="失败",
                    error=str(exc),
                )
            )
            continue
        report_path.write_text(read_result.markdown, encoding="utf-8")
        result.items.append(
            BatchItem(
                source=str(source),
                status="成功",
                entity_count=read_result.entity_count,
                text_count=read_result.text_count,
                report=str(report_path),
            )
        )

    index_path = target / "index.md"
    index_path.write_text(result.index_markdown() + "\n", encoding="utf-8")
    result.index_path = str(index_path)
    return result
