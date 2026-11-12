"""Structured reports produced from DXF documents."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .stream import TextRecord, extract_texts
from .text import clean_mtext, decode_text


def text_dict(record: TextRecord) -> dict[str, Any]:
    return asdict(record)


def inspect_report(path: str | Path, *, expand_blocks: bool = False, max_depth: int = 8) -> dict[str, Any]:
    try:
        import ezdxf
        from ezdxf import bbox
    except ImportError as exc:
        raise ValueError("install the DXF extra: pip install 'dwg-reader-skill[dxf]'") from exc

    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    texts = extract_texts(path)
    if expand_blocks:
        texts.extend(_block_texts(msp, max_depth=max_depth))
        texts.sort(key=lambda item: (-item.y, item.x))
    counts = Counter(entity.dxftype() for entity in msp)
    extent = bbox.extents(msp)
    dimensions = []
    for entity in msp.query("DIMENSION"):
        dimensions.append({
            "layer": entity.dxf.layer,
            "measurement": entity.get_measurement(),
            "text": entity.dxf.get("text", ""),
        })
    return {
        "drawing": {
            "file": str(path),
            "dxf_version": doc.dxfversion,
            "entity_count": len(msp),
            "entity_types": dict(sorted(counts.items())),
            "extents": {
                "min": list(extent.extmin),
                "max": list(extent.extmax),
            },
        },
        "layers": [{
            "name": layer.dxf.name,
            "color": layer.dxf.color,
            "linetype": layer.dxf.linetype,
            "off": layer.is_off(),
            "frozen": layer.is_frozen(),
        } for layer in doc.layers],
        "blocks": [{"name": block.name, "entity_count": len(list(block))}
                   for block in doc.blocks if not block.name.startswith("*")],
        "texts": [text_dict(record) for record in texts],
        "dimensions": dimensions,
        "warnings": [],
    }


def _block_texts(layout: Any, *, max_depth: int) -> list[TextRecord]:
    records: list[TextRecord] = []

    def visit(insert: Any, depth: int, chain: tuple[str, ...]) -> None:
        if depth > max_depth or insert.dxf.name in chain:
            return
        for attrib in insert.attribs:
            position = attrib.dxf.insert
            records.append(TextRecord(
                text=decode_text(attrib.dxf.text), x=position.x, y=position.y,
                entity="ATTRIB", layer=attrib.dxf.layer, style=attrib.dxf.style,
            ))
        try:
            virtual = list(insert.virtual_entities())
        except (ValueError, TypeError, AttributeError):
            return
        for entity in virtual:
            kind = entity.dxftype()
            if kind in {"TEXT", "MTEXT"}:
                position = entity.dxf.insert
                value = entity.dxf.text if kind == "TEXT" else clean_mtext(entity.text)
                records.append(TextRecord(
                    text=decode_text(value), x=position.x, y=position.y,
                    entity=f"BLOCK_{kind}", layer=entity.dxf.layer,
                    style=entity.dxf.get("style", ""),
                ))
            elif kind == "INSERT":
                visit(entity, depth + 1, chain + (insert.dxf.name,))

    for top_insert in layout.query("INSERT"):
        visit(top_insert, 1, ())
    return records
