"""Geometric layout helpers: reading order, column grouping, table recovery.

The functions here are pure geometry: they take text items that already carry
world coordinates and rearrange or tabulate them.  They are shared by the
report pipeline, the quantity take-off and the CLI.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TextItem:
    """A text fragment placed on the drawing."""

    text: str
    x: float
    y: float
    layer: str = ""
    kind: str = "TEXT"

    @property
    def pos(self) -> tuple[float, float]:
        return (self.x, self.y)


def cluster_index(
    values: Sequence[float],
    *,
    gap_factor: float = 6.0,
    abs_tol: float | None = None,
) -> dict[float, int]:
    """Split sorted values into clusters separated by large gaps.

    ``abs_tol`` forces an absolute gap threshold (good for table alignment),
    otherwise the median gap times ``gap_factor`` is used (good for columns of
    running text).
    """
    if not values:
        return {}
    if len(values) < 2:
        return {values[0]: 0}
    if abs_tol is not None:
        threshold = abs_tol
    else:
        diffs = [values[i + 1] - values[i] for i in range(len(values) - 1)]
        threshold = max(statistics.median(diffs) * gap_factor, 1e-9)
    index = {values[0]: 0}
    current = 0
    previous = values[0]
    for value in values[1:]:
        if value - previous > threshold:
            current += 1
        index[value] = current
        previous = value
    return index


def reorder_by_columns(items: Sequence[TextItem], *, gap_factor: float = 6.0) -> list[TextItem]:
    """Sort by column (X clusters), then top-to-bottom and left-to-right.

    This reads multi-column drawings better than a plain ``(-y, x)`` sort,
    which interleaves text that shares a Y band across columns.
    """
    if len(items) < 2:
        return list(items)
    xs = sorted({item.x for item in items})
    index = cluster_index(xs, gap_factor=gap_factor)
    columns: dict[int, list[TextItem]] = {}
    for item in items:
        columns.setdefault(index.get(item.x, 0), []).append(item)
    ordered: list[TextItem] = []
    for key in sorted(columns):
        ordered.extend(sorted(columns[key], key=lambda item: (-item.y, item.x)))
    return ordered


def sort_reading_order(items: Sequence[TextItem]) -> list[TextItem]:
    """Plain top-to-bottom, left-to-right ordering."""
    return sorted(items, key=lambda item: (-item.y, item.x))


def _build_grid(
    items: Sequence[TextItem], y_tol: float, x_tol: float
) -> tuple[list[list[str]], int, int]:
    ys = sorted({item.y for item in items})
    xs = sorted({item.x for item in items})
    y_index = cluster_index(ys, abs_tol=y_tol)
    x_index = cluster_index(xs, abs_tol=x_tol)
    rows = max(y_index.values()) + 1
    cols = max(x_index.values()) + 1
    grid: list[list[str]] = [["" for _ in range(cols)] for _ in range(rows)]
    for item in items:
        row = rows - 1 - y_index[item.y]
        col = x_index[item.x]
        existing = grid[row][col]
        grid[row][col] = f"{existing} {item.text}".strip() if existing else item.text
    return grid, rows, cols


def _blank_ratio(grid: Sequence[Sequence[str]]) -> float:
    cells = sum(len(row) for row in grid)
    if not cells:
        return 1.0
    filled = sum(1 for row in grid for cell in row if cell.strip())
    return 1.0 - filled / cells


def _tolerance_candidates(span: float) -> list[float]:
    """Absolute tolerances from "very tight" to "coarse", scaled to the drawing."""
    base = max(span, 1e-6)
    return [max(base / divisor, 1e-6) for divisor in (400, 200, 100, 60, 40, 25, 15, 10)]


def to_markdown_table(
    items: Sequence[TextItem],
    *,
    min_rows: int = 2,
    min_cols: int = 2,
    max_cols: int = 20,
    max_blank_ratio: float = 0.45,
) -> str:
    """Recover a Markdown table from aligned text, or return ``""``.

    Drawing units vary wildly between drawings (mm, m, paper units), so the
    cluster tolerance is derived from the data instead of being fixed.  Several
    tolerances are tried and the first one that yields a plausible grid wins;
    an empty string means the text is not tabular and callers should fall back
    to :func:`rows_layout`.
    """
    if len(items) < max(4, min_rows * min_cols):
        return ""
    xs = [item.x for item in items]
    ys = [item.y for item in items]
    x_span = max(xs) - min(xs)
    y_span = max(ys) - min(ys)
    if x_span <= 0 or y_span <= 0:
        return ""

    best: tuple[float, list[list[str]]] | None = None
    y_candidates = [max(y_span / divisor, 1e-6) for divisor in (300, 100, 40)]
    for x_tol in _tolerance_candidates(x_span):
        for y_tol in y_candidates:
            grid, rows, cols = _build_grid(items, y_tol, x_tol)
            if rows < min_rows or cols < min_cols or cols > max_cols:
                continue
            ratio = _blank_ratio(grid)
            if ratio <= max_blank_ratio:
                header = "| " + " | ".join("" for _ in range(cols)) + " |"
                separator = "|" + "|".join("---" for _ in range(cols)) + "|"
                body = ["| " + " | ".join(cell.strip() for cell in row) + " |" for row in grid]
                return "\n".join([header, separator, *body])
            if best is None or ratio < best[0]:
                best = (ratio, grid)
    return ""


def rows_layout(items: Sequence[TextItem], *, max_rows: int = 5000) -> list[str]:
    """Render text as logical rows: same-line fragments joined left to right.

    This preserves the two-dimensional reading order of drawings that are not
    strictly tabular (title blocks, legends, drawing directories).
    """
    if not items:
        return []
    ys = [item.y for item in items]
    span = max(ys) - min(ys)
    y_tol = max(span / 300, 1e-6) if span > 0 else 1e-6
    index = cluster_index(sorted({item.y for item in items}), abs_tol=y_tol)
    bands: dict[int, list[TextItem]] = {}
    for item in items:
        bands.setdefault(index.get(item.y, 0), []).append(item)
    lines: list[str] = []
    # cluster index grows with Y, so the highest band is printed first.
    for key in sorted(bands, reverse=True):
        row = sorted(bands[key], key=lambda item: item.x)
        merged = " | ".join(part for part in (item.text.strip() for item in row) if part)
        if merged:
            lines.append(merged)
        if len(lines) >= max_rows:
            lines.append("…")
            break
    return lines


def group_by_layer(items: Iterable[TextItem]) -> dict[str, list[TextItem]]:
    """Group items by layer, preserving the incoming order inside each group."""
    groups: dict[str, list[TextItem]] = {}
    for item in items:
        groups.setdefault(item.layer or "0", []).append(item)
    return groups


@dataclass
class TableBlock:
    """A recovered table with its bounding box."""

    rows: list[list[str]] = field(default_factory=list)
    x_range: tuple[float, float] = (0.0, 0.0)
    y_range: tuple[float, float] = (0.0, 0.0)

    def to_markdown(self) -> str:
        if not self.rows:
            return ""
        width = max(len(row) for row in self.rows)
        padded = [row + [""] * (width - len(row)) for row in self.rows]
        header = "| " + " | ".join(padded[0]) + " |"
        separator = "|" + "|".join("---" for _ in range(width)) + "|"
        body = ["| " + " | ".join(row) + " |" for row in padded[1:]]
        return "\n".join([header, separator, *body])
