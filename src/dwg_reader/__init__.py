"""Read DWG/DXF drawings: text, geometry, dimensions, components and quantities."""

from .components import ComponentReport, identify_components, load_rules
from .recognition import (build_document, recognize, render_markdown, schema_json,
                          to_schema, validate, write_outputs)
from .dimensions import DimensionReport, read_dimensions
from .dxf import Drawing, PlacedEntity, iter_placed, parse_dxf, summarize
from .fonts import ShxFont, get_font
from .layout import TextItem, reorder_by_columns, to_markdown_table
from .pipeline import ReadOptions, ReadResult, read_batch, read_drawing, resolve_dxf
from .quantity import QuantityReport, build_quantity, export
from .text import decode_cad_text, decode_text
from .tianzheng import TianzhengReport, detect_tianzheng

__all__ = [
    "ComponentReport",
    "DimensionReport",
    "Drawing",
    "PlacedEntity",
    "QuantityReport",
    "ReadOptions",
    "ReadResult",
    "ShxFont",
    "TextItem",
    "TianzhengReport",
    "build_quantity",
    "decode_cad_text",
    "decode_text",
    "detect_tianzheng",
    "export",
    "get_font",
    "identify_components",
    "recognize",
    "build_document",
    "validate",
    "schema_json",
    "render_markdown",
    "to_schema",
    "write_outputs",
    "iter_placed",
    "load_rules",
    "parse_dxf",
    "read_batch",
    "read_dimensions",
    "read_drawing",
    "reorder_by_columns",
    "resolve_dxf",
    "summarize",
    "to_markdown_table",
]
__version__ = "0.7.0"
