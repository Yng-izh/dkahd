"""Streaming DXF model reader built on the Python standard library.

Unlike the :mod:`ezdxf` based helpers in :mod:`dwg_reader.report` this module
never loads the whole drawing into memory and tolerates malformed files
(missing ``EOF``, stray ``SECTION`` tags, empty values).  It produces a small
in-memory model that later stages (text extraction, dimension reading,
component identification, quantity take-off) build on.

Only the sections needed for reading a drawing are interpreted:
``HEADER`` (code page), ``TABLES`` (layers and text styles), ``BLOCKS`` and
``ENTITIES``.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

#: ``$DWGCODEPAGE`` name -> Python codec.
CODEPAGE_MAP = {
    "ANSI_1252": "cp1252",
    "ANSI_1250": "cp1250",
    "ANSI_1251": "cp1251",
    "ANSI_1253": "cp1253",
    "ANSI_1254": "cp1254",
    "ANSI_1255": "cp1255",
    "ANSI_1256": "cp1256",
    "ANSI_1257": "cp1257",
    "ANSI_1258": "cp1258",
    "ANSI_936": "gbk",
    "ANSI_950": "big5",
    "ANSI_949": "cp949",
    "ANSI_932": "cp932",
    "ANSI_874": "cp874",
    "DOS_437": "cp437",
    "DOS_850": "cp850",
    "DOS_852": "cp852",
    "DOS_866": "cp866",
    "DOS_936": "gbk",
    "MACINTOSH": "mac_roman",
    "UTF-8": "utf8",
    "UTF8": "utf8",
    "UTF-16": "utf-16",
}

#: Entity types whose payload is human readable text.
TEXT_KINDS = frozenset({"TEXT", "MTEXT", "ATTRIB"})

#: Entity types that carry geometry usable for dimension association.
GEOMETRY_KINDS = frozenset({"LINE", "CIRCLE", "ARC", "LWPOLYLINE", "POLYLINE", "ELLIPSE"})

#: Entity types kept in the model by default.
DEFAULT_KINDS = {
    "TEXT",
    "MTEXT",
    "ATTRIB",
    "DIMENSION",
    "INSERT",
    "LINE",
    "CIRCLE",
    "ARC",
    "LWPOLYLINE",
    "POLYLINE",
    "ELLIPSE",
    "POINT",
    "SOLID",
    "HATCH",
    "SPLINE",
}


class DxfFormatError(ValueError):
    """Raised when a file is clearly not a DXF document."""


def detect_codepage(raw: bytes) -> str:
    """Return the Python codec named by ``$DWGCODEPAGE`` (default ``utf-8``)."""
    match = re.search(rb"\$DWGCODEPAGE\b.*?\n\s*3\s*\n([^\n]+)", raw, re.DOTALL)
    if match:
        name = match.group(1).decode("ascii", "ignore").strip().upper()
        if name in CODEPAGE_MAP:
            return CODEPAGE_MAP[name]
    return "utf-8"


def read_dxf_text(path: str | Path) -> str:
    """Read a DXF file, preferring UTF-8 and falling back to its code page."""
    raw = Path(path).read_bytes()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(detect_codepage(raw), errors="replace")


def _file_encoding(path: str | Path) -> str:
    """Guess the file encoding from its header without reading everything."""
    with Path(path).open("rb") as stream:
        head = stream.read(65536)
    try:
        head.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError as exc:
        # A multi-byte character split at the 64 KiB boundary is not a real
        # encoding problem, so only trust a failure well before the end.
        if exc.start >= len(head) - 4:
            return "utf-8"
    return detect_codepage(head)


def iter_tags(stream: TextIO) -> Iterator[tuple[int, str]]:
    """Yield ``(group_code, value)`` pairs from an open DXF text stream."""
    while code_line := stream.readline():
        value_line = stream.readline()
        if not value_line:
            return
        try:
            yield int(code_line.strip()), value_line.rstrip("\r\n")
        except ValueError:
            continue


def iter_records(path: str | Path) -> Iterator[tuple[str, str, list[tuple[int, str]]]]:
    """Yield ``(section, entity_type, tags)`` triples for every DXF record."""
    with Path(path).open(encoding=_file_encoding(path), errors="replace") as stream:
        section = ""
        current = ""
        tags: list[tuple[int, str]] = []
        for code, value in iter_tags(stream):
            if code != 0:
                tags.append((code, value))
                continue
            if current:
                if current == "SECTION":
                    section = next((item for tag, item in tags if tag == 2), "").strip()
                elif current == "ENDSEC":
                    section = ""
                else:
                    yield section, current, tags
            current, tags = value.strip(), []
        if current and current not in {"SECTION", "ENDSEC"}:
            yield section, current, tags


def _acad_version(path: str | Path) -> str:
    """Read ``$ACADVER`` from the file header (the header record is not yielded)."""
    try:
        with Path(path).open("rb") as stream:
            head = stream.read(16384)
    except OSError:
        return ""
    match = re.search(rb"\$ACADVER\s*\n\s*1\s*\n\s*([A-Za-z0-9_.]+)", head)
    return match.group(1).decode("ascii", "ignore") if match else ""


@dataclass
class Layer:
    """One ``LAYER`` table record."""

    name: str
    color: int = 7
    linetype: str = "CONTINUOUS"
    flags: int = 0

    @property
    def off(self) -> bool:
        return bool(self.flags & 1)

    @property
    def frozen(self) -> bool:
        return bool(self.flags & 2)


@dataclass
class Entity:
    """A DXF entity reduced to the attributes this skill cares about."""

    kind: str
    layer: str = "0"
    style: str = ""
    handle: str = ""
    text: str = ""
    pos: tuple[float, float] = (0.0, 0.0)
    end: tuple[float, float] | None = None
    points: list[tuple[float, float]] = field(default_factory=list)
    radius: float = 0.0
    start_angle: float = 0.0
    end_angle: float = 0.0
    rotation: float = 0.0
    height: float = 0.0
    block: str = ""
    scale: tuple[float, float] = (1.0, 1.0)
    dimtype: int = 0
    measurement: float | None = None
    closed: bool = False
    attribs: list[Entity] = field(default_factory=list)

    @property
    def block_path(self) -> str:
        return self.block


@dataclass
class BlockDef:
    """A ``BLOCK`` definition and the entities it contains."""

    name: str
    base: tuple[float, float] = (0.0, 0.0)
    layer: str = "0"
    entities: list[Entity] = field(default_factory=list)

    @property
    def anonymous(self) -> bool:
        return self.name.startswith("*")


@dataclass
class Drawing:
    """Parsed DXF drawing model."""

    path: str
    version: str = ""
    entities: list[Entity] = field(default_factory=list)
    blocks: dict[str, BlockDef] = field(default_factory=dict)
    layers: list[Layer] = field(default_factory=list)
    styles: dict[str, str] = field(default_factory=dict)
    counts: Counter = field(default_factory=Counter)
    block_counts: Counter = field(default_factory=Counter)
    attribs: int = 0
    #: ``(dxf_name, cpp_name, application)`` for every CLASSES record.
    classes: list[tuple[str, str, str]] = field(default_factory=list)
    proxy_objects: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def entity_count(self) -> int:
        return sum(self.counts.values()) + self.attribs

    @property
    def proxy_entities(self) -> int:
        return self.counts.get("ACAD_PROXY_ENTITY", 0)

    def layer_names(self) -> list[str]:
        return [layer.name for layer in self.layers]


@dataclass(frozen=True)
class PlacedEntity:
    """An entity resolved into world coordinates (INSERT chains unfolded)."""

    kind: str
    layer: str
    style: str
    text: str
    pos: tuple[float, float]
    points: tuple[tuple[float, float], ...] = ()
    radius: float = 0.0
    rotation: float = 0.0
    dimtype: int = 0
    measurement: float | None = None
    block: str = ""
    path: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------


def _floats(values: Sequence[str]) -> list[float]:
    result = []
    for value in values:
        try:
            result.append(float(value))
        except ValueError:
            result.append(0.0)
    return result


def _first(values: Sequence[str], default: str = "") -> str:
    return values[0] if values else default


def _build_entity(kind: str, tags: list[tuple[int, str]]) -> Entity:
    grouped: dict[int, list[str]] = {}
    for code, value in tags:
        grouped.setdefault(code, []).append(value)

    entity = Entity(
        kind=kind,
        layer=_first(grouped.get(8, []), "0").strip() or "0",
        style=_first(grouped.get(7, [])).strip(),
        handle=_first(grouped.get(5, [])).strip(),
    )

    if kind in TEXT_KINDS:
        # MTEXT splits long content across code 3 chunks followed by code 1.
        chunks = list(grouped.get(3, [])) + list(grouped.get(1, []))
        entity.text = "".join(chunks) if kind == "MTEXT" else _first(grouped.get(1, []))
        entity.rotation = _first(_floats(grouped.get(50, [])), 0.0) if 50 in grouped else 0.0
        height = _floats(grouped.get(40, []))
        entity.height = height[0] if height else 0.0
        xs, ys = _floats(grouped.get(10, [])), _floats(grouped.get(20, []))
        if xs and ys:
            entity.pos = (xs[0], ys[0])
    elif kind == "DIMENSION":
        entity.text = _first(grouped.get(1, []))
        # code 11 is the text midpoint, code 10 the definition point.
        xs, ys = (
            _floats(grouped.get(11, []) or grouped.get(10, [])),
            _floats(grouped.get(21, []) or grouped.get(20, [])),
        )
        if xs and ys:
            entity.pos = (xs[0], ys[0])
        entity.dimtype = int(_first(_floats(grouped.get(70, [])), 0))
        measurement = _floats(grouped.get(42, []))
        entity.measurement = measurement[0] if measurement else None
        entity.block = _first(grouped.get(2, [])).strip()
    elif kind == "INSERT":
        entity.block = _first(grouped.get(2, [])).strip()
        xs, ys = _floats(grouped.get(10, [])), _floats(grouped.get(20, []))
        if xs and ys:
            entity.pos = (xs[0], ys[0])
        sx = _floats(grouped.get(41, []))
        sy = _floats(grouped.get(42, []))
        entity.scale = (sx[0] if sx else 1.0, sy[0] if sy else 1.0)
        rotation = _floats(grouped.get(50, []))
        entity.rotation = rotation[0] if rotation else 0.0
    elif kind == "LINE":
        xs, ys = _floats(grouped.get(10, [])), _floats(grouped.get(20, []))
        xe, ye = _floats(grouped.get(11, [])), _floats(grouped.get(21, []))
        start = (xs[0] if xs else 0.0, ys[0] if ys else 0.0)
        end = (xe[0] if xe else 0.0, ye[0] if ye else 0.0)
        entity.pos, entity.end = start, end
        entity.points = [start, end]
    elif kind in {"CIRCLE", "ARC"}:
        xs, ys = _floats(grouped.get(10, [])), _floats(grouped.get(20, []))
        if xs and ys:
            entity.pos = (xs[0], ys[0])
        radius = _floats(grouped.get(40, []))
        entity.radius = radius[0] if radius else 0.0
        if kind == "ARC":
            start = _floats(grouped.get(50, []))
            end = _floats(grouped.get(51, []))
            entity.start_angle = start[0] if start else 0.0
            entity.end_angle = end[0] if end else 0.0
        entity.points = [entity.pos]
    elif kind == "LWPOLYLINE":
        xs, ys = _floats(grouped.get(10, [])), _floats(grouped.get(20, []))
        entity.points = list(zip(xs, ys))
        if entity.points:
            entity.pos = entity.points[0]
        entity.closed = bool(int(_first(_floats(grouped.get(70, [])), 0)) & 1)
    elif kind == "POINT":
        xs, ys = _floats(grouped.get(10, [])), _floats(grouped.get(20, []))
        if xs and ys:
            entity.pos = (xs[0], ys[0])
        entity.points = [entity.pos]
    elif kind in {"ELLIPSE", "SPLINE", "SOLID", "HATCH", "POLYLINE"}:
        xs, ys = _floats(grouped.get(10, [])), _floats(grouped.get(20, []))
        if xs and ys:
            entity.pos = (xs[0], ys[0])
        entity.points = [entity.pos]
        if kind == "SOLID":
            ordered = [(xs[i], ys[i]) for i in range(min(len(xs), len(ys)))]
            if ordered:
                entity.points = ordered
    return entity


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def parse_dxf(path: str | Path, *, keep: Iterable[str] | None = None) -> Drawing:
    """Parse a DXF file into a :class:`Drawing`.

    ``keep`` limits which entity types are retained (all types from
    :data:`DEFAULT_KINDS` are kept by default); skipped types are still counted.
    """
    source = Path(path)
    if not source.is_file():
        raise DxfFormatError(f"DXF not found: {source}")
    wanted = set(keep) if keep else set(DEFAULT_KINDS)
    drawing = Drawing(path=str(source), version=_acad_version(source))

    current_block: BlockDef | None = None
    last_insert: Entity | None = None
    orphans: list[Entity] = []

    for section, kind, tags in iter_records(source):
        if section == "HEADER":
            variable = ""
            for code, value in tags:
                if code == 9:
                    variable = value.strip()
                elif code in (1, 2) and variable == "$ACADVER":
                    drawing.version = value.strip()
            continue
        if section == "TABLES":
            grouped: dict[int, list[str]] = {}
            for code, value in tags:
                grouped.setdefault(code, []).append(value)
            if kind == "LAYER" and grouped.get(2):
                drawing.layers.append(
                    Layer(
                        name=grouped[2][0].strip(),
                        color=int(_first(_floats(grouped.get(62, [])), 7)),
                        linetype=_first(grouped.get(6, []), "CONTINUOUS").strip(),
                        flags=int(_first(_floats(grouped.get(70, [])), 0)),
                    )
                )
            elif kind == "STYLE" and grouped.get(2) and grouped.get(4):
                drawing.styles[grouped[2][0].strip()] = grouped[4][0].strip()
            continue
        if section == "CLASSES":
            # Custom (non-AutoCAD) classes such as Tianzheng's TCH_* objects.
            if kind == "CLASS":
                grouped: dict[int, list[str]] = {}
                for code, value in tags:
                    grouped.setdefault(code, []).append(value)
                drawing.classes.append(
                    (
                        _first(grouped.get(1, [])).strip(),
                        _first(grouped.get(2, [])).strip(),
                        _first(grouped.get(3, [])).strip(),
                    )
                )
            continue
        if section == "OBJECTS":
            if kind == "ACAD_PROXY_OBJECT":
                drawing.proxy_objects += 1
            continue
        if section == "BLOCKS":
            if kind == "BLOCK":
                name = next((value.strip() for code, value in tags if code == 2), "")
                base = (
                    _first(_floats([v for c, v in tags if c == 10]), 0.0),
                    _first(_floats([v for c, v in tags if c == 20]), 0.0),
                )
                layer = next((value.strip() for code, value in tags if code == 8), "0")
                current_block = BlockDef(name=name, base=base, layer=layer or "0")
                continue
            if kind == "ENDBLK":
                if current_block is not None:
                    drawing.blocks[current_block.name] = current_block
                    current_block = None
                continue
            if kind in wanted:
                drawing.block_counts[kind] += 1
                entity = _build_entity(kind, tags)
                if current_block is not None:
                    current_block.entities.append(entity)
                else:
                    orphans.append(entity)
            continue
        if section != "ENTITIES":
            continue

        drawing.counts[kind] += 1
        if kind not in wanted:
            continue
        entity = _build_entity(kind, tags)
        if kind == "ATTRIB" and last_insert is not None:
            last_insert.attribs.append(entity)
            drawing.attribs += 1
            continue
        drawing.entities.append(entity)
        last_insert = entity if kind == "INSERT" else None

    # Malformed files sometimes keep the payload outside any BLOCK/ENTITIES
    # record; keep those entities so text is not silently dropped.
    if orphans:
        drawing.entities.extend(orphans)
        drawing.warnings.append(f"{len(orphans)} entities found outside BLOCKS/ENTITIES")

    if not drawing.entities and not drawing.blocks:
        raise DxfFormatError(f"no DXF entities found in {source}")
    return drawing


Matrix = tuple[float, float, float, float, float, float]

IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def compose(outer: Matrix, inner: Matrix) -> Matrix:
    """Return the matrix applying ``inner`` first, then ``outer``."""
    a1, b1, c1, d1, e1, f1 = outer
    a2, b2, c2, d2, e2, f2 = inner
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def apply(matrix: Matrix, point: tuple[float, float]) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    x, y = point
    return (a * x + c * y + e, b * x + d * y + f)


def insert_matrix(entity: Entity, base: tuple[float, float]) -> Matrix:
    """Matrix mapping block coordinates to world coordinates for an INSERT."""
    sx, sy = entity.scale
    angle = math.radians(entity.rotation)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    bx, by = base
    ix, iy = entity.pos
    a, b = sx * cos_a, sx * sin_a
    c, d = -sy * sin_a, sy * cos_a
    return (a, b, c, d, ix - (a * bx + c * by), iy - (b * bx + d * by))


def iter_placed(
    drawing: Drawing,
    *,
    max_depth: int = 8,
    include_block_definitions: bool = True,
    include_inserts: bool = False,
) -> Iterator[PlacedEntity]:
    """Yield world-space entities, unfolding INSERT references recursively.

    ``include_block_definitions`` also emits entities from block definitions
    that were never referenced, because some (non-standard) drawings keep the
    whole payload inside an anonymous block.  ``include_inserts`` additionally
    emits the INSERT references themselves, which component counting needs.
    """
    referenced: set[str] = set()

    def walk(
        entity: Entity, matrix: Matrix, layers: tuple[str, ...], path: tuple[str, ...], depth: int
    ) -> Iterator[PlacedEntity]:
        layer = entity.layer if entity.layer not in ("", "0") or not layers else layers[-1]
        if entity.kind == "INSERT":
            if include_inserts:
                yield _placed(entity, matrix, path, layer)
            block = drawing.blocks.get(entity.block)
            if block is None or depth >= max_depth or entity.block in path:
                return
            referenced.add(entity.block)
            world = compose(matrix, insert_matrix(entity, block.base))
            # ``layer`` is already resolved against the parent chain, so the
            # children inherit the nearest non-"0" layer.
            block_layers = layers + (layer,)
            for child in block.entities:
                yield from walk(child, world, block_layers, path + (entity.block,), depth + 1)
            for attrib in entity.attribs:
                yield from walk(attrib, matrix, layers, path, depth)
            return
        yield _placed(entity, matrix, path, layer)

    for top in drawing.entities:
        yield from walk(top, IDENTITY, (), (), 0)

    if include_block_definitions:
        for block in drawing.blocks.values():
            if block.name in referenced or block.anonymous:
                continue
            for entity in block.entities:
                yield from walk(entity, IDENTITY, (block.layer,), (block.name,), 1)


def _placed(entity: Entity, matrix: Matrix, path: tuple[str, ...], layer: str) -> PlacedEntity:
    pos = apply(matrix, entity.pos)
    points = tuple(apply(matrix, point) for point in entity.points)
    scale = math.hypot(matrix[0], matrix[1])
    return PlacedEntity(
        kind=entity.kind,
        layer=layer,
        style=entity.style,
        text=entity.text,
        pos=pos,
        points=points,
        radius=entity.radius * scale,
        rotation=entity.rotation,
        dimtype=entity.dimtype,
        measurement=entity.measurement,
        block=entity.block,
        path=path,
    )


def summarize(drawing: Drawing) -> dict[str, Any]:
    """Return a JSON-friendly structural summary."""
    return {
        "file": drawing.path,
        "version": drawing.version,
        "entity_count": drawing.entity_count,
        "entity_types": dict(sorted(drawing.counts.items())),
        "block_entity_types": dict(sorted(drawing.block_counts.items())),
        "attribs": drawing.attribs,
        "layers": [
            {
                "name": layer.name,
                "color": layer.color,
                "linetype": layer.linetype,
                "off": layer.off,
                "frozen": layer.frozen,
            }
            for layer in drawing.layers
        ],
        "blocks": [
            {"name": block.name, "entities": len(block.entities), "base": list(block.base)}
            for block in drawing.blocks.values()
            if not block.anonymous
        ],
        "styles": dict(sorted(drawing.styles.items())),
        "proxy_entities": drawing.proxy_entities,
        "proxy_objects": drawing.proxy_objects,
        "custom_classes": len(drawing.classes),
        "warnings": list(drawing.warnings),
    }
