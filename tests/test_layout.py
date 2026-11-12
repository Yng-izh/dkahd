"""Tests for layout helpers: clustering, reading order, table recovery."""

from __future__ import annotations

from dwg_reader.layout import (
    TableBlock,
    TextItem,
    cluster_index,
    group_by_layer,
    reorder_by_columns,
    sort_reading_order,
    to_markdown_table,
)


def test_cluster_index_with_absolute_tolerance():
    values = [0.0, 0.2, 10.0, 10.1, 20.0]
    index = cluster_index(values, abs_tol=1.0)
    assert [index[value] for value in values] == [0, 0, 1, 1, 2]


def test_cluster_index_with_relative_gap():
    values = [0.0, 1.0, 2.0, 100.0]
    index = cluster_index(values, gap_factor=6.0)
    assert index[0.0] == index[2.0] == 0
    assert index[100.0] == 1


def test_cluster_index_edge_cases():
    assert cluster_index([]) == {}
    assert cluster_index([4.0]) == {4.0: 0}


def test_reorder_by_columns_keeps_columns_together():
    items = [
        TextItem("右栏上", x=100.0, y=10.0, layer="A"),
        TextItem("左栏下", x=6.0, y=0.0, layer="A"),
        TextItem("左栏上", x=0.0, y=10.0, layer="A"),
        TextItem("右栏下", x=106.0, y=0.0, layer="A"),
    ]
    ordered = [item.text for item in reorder_by_columns(items)]
    assert ordered == ["左栏上", "左栏下", "右栏上", "右栏下"]


def test_sort_reading_order_is_row_major():
    items = [
        TextItem("b", x=5.0, y=0.0),
        TextItem("a", x=0.0, y=0.0),
        TextItem("top", x=0.0, y=9.0),
    ]
    assert [item.text for item in sort_reading_order(items)] == ["top", "a", "b"]


def test_to_markdown_table_recovers_grid():
    items = [
        TextItem("编号", x=0.0, y=10.0),
        TextItem("宽度", x=50.0, y=10.0),
        TextItem("M1021", x=0.0, y=0.0),
        TextItem("1000", x=50.0, y=0.0),
    ]
    table = to_markdown_table(items)
    assert table.startswith("|")
    assert "编号" in table and "M1021" in table and "1000" in table
    # Header row must come first, data row second.
    assert table.index("编号") < table.index("M1021")


def test_to_markdown_table_rejects_scattered_text():
    # A single row of text is a sentence, not a table.
    items = [
        TextItem("说明", x=0.0, y=0.0),
        TextItem("本条", x=50.0, y=0.0),
        TextItem("共四段", x=100.0, y=0.0),
        TextItem("文字", x=900.0, y=0.0),
    ]
    assert to_markdown_table(items) == ""


def test_group_by_layer_buckets_items():
    items = [
        TextItem("a", x=0.0, y=0.0, layer="WALL"),
        TextItem("b", x=1.0, y=0.0, layer=""),
        TextItem("c", x=2.0, y=0.0, layer="WALL"),
    ]
    groups = group_by_layer(items)
    assert [item.text for item in groups["WALL"]] == ["a", "c"]
    assert [item.text for item in groups["0"]] == ["b"]


def test_table_block_pads_ragged_rows():
    block = TableBlock(rows=[["a", "b", "c"], ["d"]])
    markdown = block.to_markdown()
    assert markdown.splitlines()[0] == "| a | b | c |"
    assert markdown.splitlines()[-1] == "| d |  |  |"
