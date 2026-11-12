"""Quantity take-off: component counts, layer geometry totals, CSV/XLSX export.

All lengths are reported in *drawing units*: DWG/DXF files carry no unit
metadata, so callers must combine them with dimension annotations or the
drawing title block before quoting real-world numbers.
"""

from __future__ import annotations

import csv
import io
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .components import ComponentReport, Rule, identify_components, load_rules
from .dimensions import DimensionReport, read_dimensions
from .dxf import Drawing, parse_dxf
from .pipeline import (
    ReadOptions,
    build_decoder,
    resolve_dxf,
    resolve_layer_filter,
)

SHEET_NAMES = {
    "components": "构件清单",
    "layers": "图层几何量",
    "dimensions": "尺寸清单",
}


@dataclass
class LayerQuantity:
    """Geometry totals per layer, in drawing units."""

    layer: str
    line_count: int = 0
    line_length: float = 0.0
    polyline_count: int = 0
    polyline_length: float = 0.0
    circle_count: int = 0
    arc_count: int = 0
    text_count: int = 0

    def to_row(self) -> dict[str, Any]:
        return {
            "图层": self.layer,
            "直线数": self.line_count,
            "直线总长": round(self.line_length, 3),
            "多段线数": self.polyline_count,
            "多段线总长": round(self.polyline_length, 3),
            "圆数": self.circle_count,
            "圆弧数": self.arc_count,
            "文字数": self.text_count,
        }


@dataclass
class QuantityReport:
    """Aggregated quantities for one drawing."""

    source: str
    components: ComponentReport | None = None
    layers: list[LayerQuantity] = field(default_factory=list)
    dimensions: DimensionReport | None = None

    def sheets(self) -> dict[str, list[dict[str, Any]]]:
        data: dict[str, list[dict[str, Any]]] = {}
        if self.components is not None:
            data["components"] = [
                {
                    "构件类型": group.name,
                    "数量": group.count,
                    "匹配实体数": group.entity_count,
                    "图层": ";".join(sorted(group.layers)),
                    "示例块名": ";".join(sorted(group.blocks)[:5]),
                    "X最小值": (group.bbox() or {}).get("xmin", ""),
                    "X最大值": (group.bbox() or {}).get("xmax", ""),
                    "Y最小值": (group.bbox() or {}).get("ymin", ""),
                    "Y最大值": (group.bbox() or {}).get("ymax", ""),
                }
                for group in self.components.by_type
            ]
        if self.layers:
            data["layers"] = [layer.to_row() for layer in self.layers]
        if self.dimensions is not None:
            data["dimensions"] = [
                {
                    "序号": index,
                    "类型": dim.type,
                    "标注值": dim.text,
                    "图层": dim.layer,
                    "X": round(dim.pos[0], 1),
                    "Y": round(dim.pos[1], 1),
                }
                for index, dim in enumerate(self.dimensions.dimensions, 1)
            ]
        return data


def layer_quantities(drawing: Drawing) -> list[LayerQuantity]:
    """Sum line/polyline lengths and entity counts per layer."""
    from .dxf import iter_placed

    totals: dict[str, LayerQuantity] = {}
    for placed in iter_placed(drawing):
        entry = totals.setdefault(placed.layer or "0", LayerQuantity(placed.layer or "0"))
        if placed.kind == "LINE" and len(placed.points) >= 2:
            (x1, y1), (x2, y2) = placed.points[0], placed.points[1]
            entry.line_count += 1
            entry.line_length += math.hypot(x2 - x1, y2 - y1)
        elif placed.kind == "LWPOLYLINE" and placed.points:
            entry.polyline_count += 1
            points = list(placed.points)
            entry.polyline_length += sum(
                math.hypot(points[i + 1][0] - points[i][0], points[i + 1][1] - points[i][1])
                for i in range(len(points) - 1)
            )
        elif placed.kind == "CIRCLE":
            entry.circle_count += 1
        elif placed.kind == "ARC":
            entry.arc_count += 1
        elif placed.kind in ("TEXT", "MTEXT", "ATTRIB"):
            entry.text_count += 1
    return sorted(totals.values(), key=lambda item: item.layer)


def build_quantity(
    source: str | Path,
    *,
    options: ReadOptions | None = None,
    rules: Sequence[Rule] | None = None,
    convert: bool = True,
    oda_path: str | Path | None = None,
    backend: str = "auto",
) -> QuantityReport:
    """Compute component, layer and dimension quantities for one drawing."""
    options = options or ReadOptions()
    dxf_path = resolve_dxf(source, convert=convert, oda_path=oda_path, backend=backend)
    drawing = parse_dxf(dxf_path)
    layer_filter = resolve_layer_filter(options.layer_filter, options.layer_alias)
    decode = build_decoder(drawing, options.font)

    active_rules = list(rules) if rules is not None else load_rules(options.rules)
    components = identify_components(
        drawing,
        rules=active_rules,
        layer_filter=layer_filter,
        layer_matches=options.layer_matches,
    )
    dimensions = read_dimensions(
        drawing,
        decode=lambda text, style: decode(text, style, "TEXT"),
        layer_filter=layer_filter,
    )
    return QuantityReport(
        source=str(source),
        components=components,
        layers=layer_quantities(drawing),
        dimensions=dimensions,
    )


def _section_titles(keys: Iterable[str]) -> dict[str, str]:
    return {key: SHEET_NAMES.get(key, key) for key in keys}


def export_csv(
    report: QuantityReport, path: str | Path, *, sections: Sequence[str] | None = None
) -> Path:
    """Write quantities as a sectioned CSV (Excel friendly, UTF-8 with BOM)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = report.sheets()
    keys = [key for key in (sections or ("components", "layers", "dimensions")) if data.get(key)]
    titles = _section_titles(keys)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    for index, key in enumerate(keys):
        rows = data[key]
        if index:
            writer.writerow([])
        writer.writerow([f"[{titles[key]}]"])
        headers = list(rows[0].keys())
        writer.writerow(headers)
        for row in rows:
            writer.writerow([row.get(header, "") for header in headers])
    if not keys:
        writer.writerow(["无数据"])
    target.write_text(buffer.getvalue(), encoding="utf-8-sig")
    return target


def export_xlsx(
    report: QuantityReport, path: str | Path, *, sections: Sequence[str] | None = None
) -> Path:
    """Write one worksheet per section using openpyxl."""
    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise ValueError(
            "导出 Excel 需要 openpyxl：pip install openpyxl（或改用 --format csv）"
        ) from exc

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = report.sheets()
    keys = [key for key in (sections or ("components", "layers", "dimensions")) if data.get(key)]
    titles = _section_titles(keys)
    workbook = Workbook()
    workbook.remove(workbook.active)
    if not keys:
        sheet = workbook.create_sheet("无数据")
        sheet.append(["无数据"])
    for key in keys:
        rows = data[key]
        sheet = workbook.create_sheet(titles[key])
        headers = list(rows[0].keys())
        sheet.append(headers)
        for row in rows:
            sheet.append([row.get(header, "") for header in headers])
        for column_index, header in enumerate(headers, 1):
            width = max(len(str(header)) * 2, 12)
            sheet.column_dimensions[
                sheet.cell(row=1, column=column_index).column_letter
            ].width = width
    workbook.save(target)
    return target


def export(
    report: QuantityReport,
    path: str | Path,
    *,
    fmt: str | None = None,
    sections: Sequence[str] | None = None,
) -> Path:
    """Export a quantity report, inferring the format from the file suffix."""
    target = Path(path)
    suffix = (fmt or target.suffix).lower().lstrip(".")
    if suffix in ("xlsx", "xlsm"):
        return export_xlsx(report, target, sections=sections)
    return export_csv(report, target, sections=sections)


def quantity_markdown(report: QuantityReport) -> str:
    """Render quantities as Markdown for terminal output."""
    lines = [f"# 算量清单 — {Path(report.source).name}", ""]
    data = report.sheets()
    for key in ("components", "layers", "dimensions"):
        rows = data.get(key)
        if not rows:
            continue
        lines.extend([f"## {SHEET_NAMES[key]}", ""])
        headers = list(rows[0].keys())
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("|" + "|".join("---" for _ in headers) + "|")
        for row in rows:
            lines.append("| " + " | ".join(str(row.get(header, "")) for header in headers) + " |")
        lines.append("")
    if len(lines) == 2:
        lines.append("_未提取到可导出的算量数据。_")
    else:
        lines.append("> 长度单位为图纸单位（DWG/DXF 不含单位信息，需结合尺寸标注换算）。")
    return "\n".join(lines)
