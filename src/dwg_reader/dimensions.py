"""Dimension annotation reading and geometry association."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .dxf import GEOMETRY_KINDS, Drawing, PlacedEntity, iter_placed

#: ``dimtype`` low nibble -> human readable type.
DIM_TYPES = {
    0: "线性",
    1: "对齐",
    2: "角度",
    3: "直径",
    4: "半径",
    5: "坐标",
    6: "角度3p",
    7: "其他(7)",
    8: "弧长",
}


@dataclass(frozen=True)
class RelatedGeometry:
    """A geometry entity near a dimension annotation."""

    kind: str
    layer: str
    distance: float
    description: str


@dataclass
class DimRecord:
    """One dimension annotation."""

    type: str
    text: str
    layer: str
    pos: tuple[float, float]
    raw_text: str = ""
    measurement: float | None = None
    related: list[RelatedGeometry] = field(default_factory=list)


@dataclass
class DimensionReport:
    """Result of dimension reading."""

    file: str
    dimensions: list[DimRecord] = field(default_factory=list)
    degraded: bool = False

    @property
    def count(self) -> int:
        return len(self.dimensions)

    def by_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for dimension in self.dimensions:
            counts[dimension.type] = counts.get(dimension.type, 0) + 1
        return counts

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "count": self.count,
            "by_type": self.by_type(),
            "degraded": self.degraded,
            "dimensions": [
                {
                    "type": dim.type,
                    "text": dim.text,
                    "layer": dim.layer,
                    "pos": [round(dim.pos[0], 1), round(dim.pos[1], 1)],
                    "measurement": dim.measurement,
                    "related": [
                        {
                            "type": related.kind,
                            "layer": related.layer,
                            "dist": round(related.distance, 1),
                            "desc": related.description,
                        }
                        for related in dim.related
                    ],
                }
                for dim in self.dimensions
            ],
        }


def dimension_type(dimtype: int) -> str:
    """Map a raw ``dimtype`` value to its Chinese type name."""
    code = (dimtype or 0) & 0xF
    return DIM_TYPES.get(code, f"其他({code})")


def _representative(item: PlacedEntity) -> tuple[float, float]:
    if item.kind == "LINE" and len(item.points) >= 2:
        (x1, y1), (x2, y2) = item.points[0], item.points[1]
        return ((x1 + x2) / 2, (y1 + y2) / 2)
    return item.pos


def _describe(item: PlacedEntity) -> str:
    if item.kind == "LINE" and len(item.points) >= 2:
        (x1, y1), (x2, y2) = item.points[0], item.points[1]
        return f"LINE({x1:.0f},{y1:.0f})→({x2:.0f},{y2:.0f})"
    if item.kind in ("CIRCLE", "ARC"):
        return f"{item.kind} 圆心({item.pos[0]:.0f},{item.pos[1]:.0f}) R={item.radius:.1f}"
    return f"{item.kind} @({item.pos[0]:.0f},{item.pos[1]:.0f})"


def nearest_geometry(
    point: tuple[float, float],
    geometry: Sequence[PlacedEntity],
    *,
    k: int = 2,
) -> list[RelatedGeometry]:
    """Return the ``k`` closest geometry entities to ``point``."""
    if not geometry or k <= 0:
        return []
    scored = []
    for item in geometry:
        rx, ry = _representative(item)
        distance = math.hypot(point[0] - rx, point[1] - ry)
        scored.append((distance, item))
    scored.sort(key=lambda pair: pair[0])
    return [
        RelatedGeometry(
            kind=item.kind,
            layer=item.layer,
            distance=distance,
            description=_describe(item),
        )
        for distance, item in scored[:k]
    ]


def read_dimensions(
    drawing: Drawing,
    *,
    decode: Callable[[str, str], str] | None = None,
    neighbours: int = 2,
    layer_filter: re.Pattern[str] | None = None,
    max_geometry: int = 20000,
) -> DimensionReport:
    """Extract every DIMENSION entity and associate nearby geometry.

    ``decode`` receives ``(raw_text, style)`` and returns decoded text, so the
    caller can plug in the SHX/bigfont decoder without a circular import.
    """
    decode = decode or (lambda value, _style: value)
    placed = list(iter_placed(drawing))
    geometry: list[PlacedEntity] = [item for item in placed if item.kind in GEOMETRY_KINDS][
        :max_geometry
    ]
    dimensions: list[DimRecord] = []

    for item in placed:
        if item.kind != "DIMENSION":
            continue
        if layer_filter is not None and not layer_filter.search(item.layer or ""):
            continue
        raw = item.text or ""
        text = raw
        if (not text or text == "<>") and item.measurement is not None:
            text = f"{item.measurement:.2f}"
        text = decode(text, item.style).strip()
        dimensions.append(
            DimRecord(
                type=dimension_type(item.dimtype),
                text=text,
                layer=item.layer,
                pos=item.pos,
                raw_text=raw,
                measurement=item.measurement,
                related=nearest_geometry(item.pos, geometry, k=neighbours),
            )
        )

    return DimensionReport(file=drawing.path, dimensions=dimensions)


def dimension_markdown(report: DimensionReport) -> str:
    """Render a dimension report as Markdown."""
    lines = [
        f"**文件**: `{report.file}`",
        f"**总尺寸数**: {report.count}",
    ]
    if report.degraded:
        lines.append(
            "> _注：结构化解析失败，已降级为流式提取；类型/位置可能不完整，关联几何不可用。_"
        )
    lines.append("")
    counts = report.by_type()
    if counts:
        lines.extend(["| 类型 | 数量 |", "|------|------|"])
        for name, value in sorted(counts.items(), key=lambda pair: -pair[1]):
            lines.append(f"| {name} | {value} |")
        lines.append("")
    if report.dimensions:
        lines.extend(
            [
                "### 尺寸明细",
                "",
                "| # | 类型 | 标注值 | 图层 | 位置 | 关联几何 |",
                "|---|------|--------|------|------|----------|",
            ]
        )
        for index, dim in enumerate(report.dimensions, 1):
            related = ", ".join(item.description for item in dim.related) or "—"
            lines.append(
                f"| {index} | {dim.type} | {dim.text} | {dim.layer} "
                f"| ({dim.pos[0]:.1f},{dim.pos[1]:.1f}) | {related} |"
            )
    else:
        lines.append("_未发现 DIMENSION 实体。_")
    return "\n".join(lines)


def dimension_rows(report: DimensionReport) -> list[dict[str, object]]:
    """Flatten a dimension report into rows suitable for CSV export."""
    rows: list[dict[str, object]] = []
    for index, dim in enumerate(report.dimensions, 1):
        rows.append(
            {
                "序号": index,
                "类型": dim.type,
                "标注值": dim.text,
                "图层": dim.layer,
                "X": round(dim.pos[0], 1),
                "Y": round(dim.pos[1], 1),
                "关联几何": ";".join(item.description for item in dim.related),
            }
        )
    return rows
