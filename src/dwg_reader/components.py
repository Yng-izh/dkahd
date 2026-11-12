"""Component identification from block names and layer names.

Rules map a logical component (door, window, beam, column, ...) to a block name
pattern and a layer name pattern.  The built-in table matches the JSON rule file
that ships with the skill and can be replaced or extended at runtime.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .dxf import Drawing, PlacedEntity, iter_placed

#: ``(name, block_regex, layer_regex)`` triples, matched case-insensitively.
BUILTIN_RULES: tuple[tuple[str, str, str], ...] = (
    ("门", r"^(M_?DOOR|DOOR|门|MC|MH|TM|DOOR_)", r"(DOOR|门|MCDOOR)"),
    ("窗", r"^(W_?WIN|WIN|WC|WINDOW|窗|CHUANG)", r"(WINDOW|窗|WCWIN)"),
    ("梁", r"(BEAM|梁|LIANG|KL|WKL|XL)", r"(BEAM|梁|S_BEAM|STRB)"),
    ("柱", r"(COLUMN|COL|柱|ZHU|KZ)", r"(COLUMN|柱|S_COL|STRC)"),
    ("钢筋", r"(REBAR|BAR|钢筋|REINFORCE|STEEL)", r"(REBAR|钢筋|STEEL|REIN)"),
    ("楼梯", r"(STAIR|楼梯|LT|LOUTI)", r"(STAIR|楼梯)"),
    ("电梯", r"(LIFT|ELEVATOR|电梯|DT)", r"(LIFT|ELEVATOR|电梯)"),
    ("墙", r"(WALL|墙|QIANG)", r"(WALL|墙|S_WALL)"),
    ("板", r"(SLAB|板|BAN|^B_|_SLAB)", r"(SLAB|板|S_SLAB)"),
    ("基础", r"(FOUND|基础|JC|JICHU)", r"(FOUND|基础|S_FD)"),
    ("管道", r"(PIPE|管|GUAN|GD)", r"(PIPE|管|冷凝|排水|给水|水管)"),
    ("设备", r"(EQUIP|设备|FAN|AHU|KT|SB)", r"(EQUIP|设备|暖通|HVAC|电气)"),
    ("家具", r"(FURNITURE|FURN|家具|SOFA|BED|TABLE|DESK|CHAIR|JIAJU)", r"(FURNITURE|家具|FF)"),
    ("轴线/轴号", r"^(AXIS|CIRCLE_AXIS|轴)", r"(AXIS|轴|C-AXIS|ZH)"),
    ("标高", r"^(ELEV|LEVEL|标高|BG)", r"(ELEV|标高|LEVEL|高程|断面高程)"),
    ("索引/详图", r"^(DETAIL|INDEX|SECTION|索引|详图)", r"(DETAIL|索引|详图|SECTION)"),
    ("标注", r"^(DIM|DIMENSION|标注)", r"(DIM|标注|注释)"),
    ("图签/图框", r"(TITLE|FIGURE|图签|图框|TK)", r"(图签|图框|TITLE|TK)"),
    ("指北针", r"(NORTH|指北针|COMPASS)", r"(NORTH|指北针|COMPASS)"),
    ("道路", r"(ROAD|路|LU)", r"(ROAD|路|一级路|二级路|公路)"),
    ("渠道", r"(CANAL|渠道|QUDAO)", r"(CANAL|渠道|渠)"),
    ("排水沟", r"(DITCH|排水|排沟|PAIGOU)", r"(DITCH|排沟|排水沟|排水)"),
    ("建筑物", r"(BUILDING|建筑|FANGWU)", r"(BUILDING|建筑|居民点|房屋)"),
    ("等高线", r"(CONTOUR|DGX|等高线)", r"(CONTOUR|等高线|DGX)"),
    ("桩号", r"(ZHUANGHAO|桩号|KZD)", r"(KZD|桩号|里程)"),
    ("断面", r"(SECTION|断面|DMX|DXPM)", r"(断面|DMX|DXPM|断面注释)"),
)

#: Geometry-only entity types ignored for layer based matching, because their
#: layer names are usually drafting aids rather than components.
_GEOMETRY_ONLY = frozenset({"LINE", "LWPOLYLINE", "CIRCLE", "ARC", "POINT", "ELLIPSE", "SPLINE"})


#: AutoCAD names anonymous blocks ``*U12`` or ``A$C0A1B2C3D``; they are
#: implementation details (dynamic block instances, hatches, tables) and must
#: never be reported as building components.
def _is_anonymous_block(name: str) -> bool:
    return name.startswith("*") or name.upper().startswith("A$C")


@dataclass(frozen=True)
class Rule:
    """A compiled component rule."""

    name: str
    block: re.Pattern[str]
    layer: re.Pattern[str]


@dataclass
class ComponentGroup:
    """Aggregated hits for one component type.

    ``count`` is the number of components (block references, plus one entry per
    layer when ``layer_matches`` is enabled).  ``entity_count`` records how many
    entities actually matched, which is usually far larger because layer names
    also label text, hatches and other drafting entities.
    """

    name: str
    count: int = 0
    entity_count: int = 0
    layers: set[str] = field(default_factory=set)
    blocks: set[str] = field(default_factory=set)
    positions: list[tuple[float, float]] = field(default_factory=list)
    layer_only: set[str] = field(default_factory=set)

    def bbox(self) -> dict[str, float] | None:
        if not self.positions:
            return None
        xs = [pos[0] for pos in self.positions]
        ys = [pos[1] for pos in self.positions]
        return {
            "xmin": round(min(xs), 1),
            "xmax": round(max(xs), 1),
            "ymin": round(min(ys), 1),
            "ymax": round(max(ys), 1),
        }


@dataclass
class ComponentReport:
    """Result of component identification."""

    file: str
    by_type: list[ComponentGroup] = field(default_factory=list)
    unmatched_inserts: int = 0
    degraded: bool = False
    layer_matches: bool = False

    @property
    def total(self) -> int:
        return sum(group.count for group in self.by_type)

    @property
    def entity_total(self) -> int:
        return sum(group.entity_count for group in self.by_type)

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "total_components": self.total,
            "total_entities": self.entity_total,
            "layer_matches": self.layer_matches,
            "by_type": [
                {
                    "name": group.name,
                    "count": group.count,
                    "entity_count": group.entity_count,
                    "layers": sorted(group.layers),
                    "blocks": sorted(group.blocks)[:5],
                    "layer_only": sorted(group.layer_only),
                    "bbox": group.bbox(),
                }
                for group in self.by_type
            ],
            "unmatched_inserts": self.unmatched_inserts,
            "degraded": self.degraded,
        }


def compile_rules(rules: Iterable[tuple[str, str, str] | dict]) -> list[Rule]:
    """Compile ``(name, block_re, layer_re)`` tuples or JSON-style dicts."""
    compiled: list[Rule] = []
    for item in rules:
        if isinstance(item, dict):
            name = str(item.get("name", "")).strip()
            block = str(item.get("block_re", "") or "")
            layer = str(item.get("layer_re", "") or "")
        else:
            name, block, layer = item
        if not name:
            continue
        try:
            compiled.append(
                Rule(
                    name=name,
                    block=re.compile(block or r"(?!x)x", re.IGNORECASE),
                    layer=re.compile(layer or r"(?!x)x", re.IGNORECASE),
                )
            )
        except re.error as exc:
            raise ValueError(f"invalid component rule {name!r}: {exc}") from exc
    return compiled


def load_rules(path: str | Path | None = None) -> list[Rule]:
    """Load rules from a JSON file, or return the built-in table."""
    if path is None:
        return compile_rules(BUILTIN_RULES)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = data.get("rules", []) if isinstance(data, dict) else data
    return compile_rules(entries)


def identify_components(
    drawing: Drawing,
    *,
    rules: Sequence[Rule] | None = None,
    layer_filter: re.Pattern[str] | None = None,
    layer_matches: bool = False,
) -> ComponentReport:
    """Count components by matching block names (and optionally layer names)."""
    active = list(rules) if rules is not None else compile_rules(BUILTIN_RULES)
    groups: dict[str, ComponentGroup] = {}
    seen_layer_hits: set[tuple[str, str]] = set()
    unmatched = 0

    for item in iter_placed(drawing, include_inserts=True):
        if layer_filter is not None and not layer_filter.search(item.layer or ""):
            continue
        is_insert = item.kind == "INSERT"
        # Component counts come from block references.  Layer names only become
        # components when explicitly requested, and then each layer counts once,
        # otherwise a single title-block layer would report thousands of frames.
        if not is_insert and not layer_matches:
            continue
        if not is_insert and item.kind in _GEOMETRY_ONLY:
            continue
        if (item.layer or "").upper() == "DEFPOINTS" and not is_insert:
            continue
        block_name = item.block if is_insert else ""
        if block_name and _is_anonymous_block(block_name):
            continue
        matched = False
        for rule in active:
            hit_block = bool(block_name and rule.block.search(block_name))
            hit_layer = bool(item.layer and rule.layer.search(item.layer))
            if not (hit_block or hit_layer):
                continue
            group = groups.setdefault(rule.name, ComponentGroup(rule.name))
            group.entity_count += 1
            if item.layer:
                group.layers.add(item.layer)
            if block_name:
                group.blocks.add(block_name)
            group.positions.append((round(item.pos[0], 1), round(item.pos[1], 1)))
            if is_insert:
                group.count += 1
            else:
                key = (rule.name, item.layer)
                if key not in seen_layer_hits:
                    seen_layer_hits.add(key)
                    group.count += 1
                    group.layer_only.add(item.layer)
            matched = True
            break
        if not matched and is_insert and block_name and not _is_anonymous_block(block_name):
            unmatched += 1

    ordered = sorted(groups.values(), key=lambda group: (-group.count, group.name))
    return ComponentReport(
        file=drawing.path,
        by_type=ordered,
        unmatched_inserts=unmatched,
        layer_matches=layer_matches,
    )


def component_markdown(report: ComponentReport) -> str:
    """Render a component report as Markdown."""
    lines = [
        f"**文件**: `{report.file}`",
        f"**已识别构件总数**: {report.total}（匹配实体 {report.entity_total} 个）",
    ]
    if report.layer_matches:
        lines.append(
            "> _图层辅助模式：每个命中图层只计 1 个构件，以免同一图层上的大量文字/填充被重复计数。_"
        )
    if report.degraded:
        lines.append("> _注：结构化解析失败，已降级为流式提取；结果可能不完整。_")
    lines.append("")
    if report.by_type:
        lines.extend(
            [
                "| 构件 | 数量 | 匹配实体 | 图层 | 示例块名 | 位置范围 |",
                "|------|------|----------|------|----------|----------|",
            ]
        )
        for group in report.by_type:
            box = group.bbox()
            box_text = (
                f"X:{box['xmin']}~{box['xmax']} Y:{box['ymin']}~{box['ymax']}" if box else "—"
            )
            layers = ", ".join(
                f"{name}*(图层)" if name in group.layer_only else name
                for name in sorted(group.layers)
            )
            lines.append(
                f"| {group.name} | {group.count} | {group.entity_count} | {layers or '—'} "
                f"| {', '.join(sorted(group.blocks)[:5]) or '—'} | {box_text} |"
            )
    else:
        lines.append("_未识别到任何构件。可检查或扩展构件规则。_")
    if report.unmatched_inserts:
        lines.append("")
        lines.append(
            f"_另有 {report.unmatched_inserts} 个未匹配规则的 INSERT 块参照"
            + "（可用自定义规则覆盖更多类型）。_"
        )
    return "\n".join(lines)


def component_rows(report: ComponentReport) -> list[dict[str, object]]:
    """Flatten a component report into rows suitable for CSV export."""
    rows: list[dict[str, object]] = []
    for group in report.by_type:
        box = group.bbox() or {}
        rows.append(
            {
                "构件类型": group.name,
                "数量": group.count,
                "匹配实体数": group.entity_count,
                "图层": ";".join(sorted(group.layers)),
                "示例块名": ";".join(sorted(group.blocks)[:5]),
                "X最小值": box.get("xmin", ""),
                "X最大值": box.get("xmax", ""),
                "Y最小值": box.get("ymin", ""),
                "Y最大值": box.get("ymax", ""),
            }
        )
    return rows


def entity_payload(drawing: Drawing) -> list[PlacedEntity]:
    """Convenience helper returning all placed entities of a drawing."""
    return list(iter_placed(drawing, include_inserts=True))
