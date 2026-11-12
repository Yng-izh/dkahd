"""Optional MCP server exposing the stable read-only CAD tool surface."""

from __future__ import annotations

from dataclasses import asdict

from .backends import doctor_report, openscad_extrude
from .pipeline import ReadOptions
from .pipeline import read_drawing as read_drawing_report
from .quantity import build_quantity, quantity_markdown
from .report import inspect_report
from .stream import extract_texts


def create_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError("install MCP support: pip install 'dwg-reader-skill[mcp]'") from exc

    server = FastMCP(
        "DWG Reader",
        json_response=True,
        instructions=(
            "Inspect local DWG-derived DXF drawings. Tools are read-only except explicit "
            "OpenSCAD exports, which write only to the requested output path."
        ),
    )

    @server.tool()
    def cad_doctor() -> dict:
        """Report available DWG, DXF, OpenSCAD, OpenCAD, and MCP backends."""
        return doctor_report()

    @server.tool()
    def extract_dxf_text(path: str, expand_blocks: bool = False, max_depth: int = 8) -> dict:
        """Extract ordered multilingual text and optional nested block attributes from DXF."""
        if expand_blocks:
            return {"texts": inspect_report(path, expand_blocks=True, max_depth=max_depth)["texts"]}
        return {"texts": [asdict(record) for record in extract_texts(path)]}

    @server.tool()
    def inspect_dxf(path: str, expand_blocks: bool = False, max_depth: int = 8) -> dict:
        """Return annotation-aware drawing, layer, block, dimension, and text JSON."""
        return inspect_report(path, expand_blocks=expand_blocks, max_depth=max_depth)

    @server.tool()
    def read_drawing(
        path: str,
        table: bool = False,
        by_layer: bool = False,
        dimensions: bool = False,
        components: bool = False,
        layer_filter: str | None = None,
    ) -> dict:
        """Read a DWG/DXF drawing: decoded texts, structure, dimensions, components."""
        options = ReadOptions(
            table=table,
            by_layer=by_layer,
            dimensions=dimensions,
            components=components,
            layer_filter=layer_filter,
        )
        result = read_drawing_report(path, options)
        return {"markdown": result.markdown, **result.payload}

    @server.tool()
    def drawing_quantities(path: str, layer_filter: str | None = None) -> dict:
        """Return component counts and per-layer geometry totals (drawing units)."""
        report = build_quantity(path, options=ReadOptions(layer_filter=layer_filter))
        return {"markdown": quantity_markdown(report), **report.sheets()}

    @server.tool()
    def extrude_dxf_with_openscad(
        path: str,
        output: str,
        height: float = 10.0,
        layer: str | None = None,
    ) -> dict:
        """Explicitly export a 2D DXF as STL/3MF/OFF using local OpenSCAD."""
        return openscad_extrude(path, output, height=height, layer=layer)

    return server


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
