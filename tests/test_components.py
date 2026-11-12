"""Tests for component identification rules."""

from __future__ import annotations

import json

import pytest
from conftest import _dxf

from dwg_reader.components import (
    BUILTIN_RULES,
    ComponentGroup,
    compile_rules,
    component_markdown,
    component_rows,
    identify_components,
    load_rules,
)
from dwg_reader.dxf import parse_dxf

COMPONENTS = r"""\
  0
SECTION
  2
ENTITIES
  0
INSERT
  2
DOOR_900
  8
DOORS
 10
0.0
 20
0.0
  0
INSERT
  2
DOOR_800
  8
DOORS
 10
10.0
 20
0.0
  0
INSERT
  2
WINDOW_C1200
  8
WINDOWS
 10
20.0
 20
0.0
  0
INSERT
  2
UNKNOWN_BLK
  8
MISC
 10
30.0
 20
0.0
  0
LINE
  8
DOORS
 10
0.0
 20
0.0
 11
1.0
 21
1.0
  0
TEXT
  8
DOOR_TAG
 10
5.0
 20
5.0
  1
M1021
  0
ENDSEC
  0
EOF
"""


def _drawing(tmp_path):
    path = tmp_path / "components.dxf"
    path.write_text(_dxf(COMPONENTS), encoding="utf-8")
    return parse_dxf(path)


def test_builtin_rules_identify_doors_and_windows(tmp_path):
    report = identify_components(_drawing(tmp_path))
    counts = {group.name: group.count for group in report.by_type}
    # Only block references count by default, so the DOOR_TAG text is ignored.
    assert counts["门"] == 2
    assert counts["窗"] == 1
    assert report.total == 3
    assert report.unmatched_inserts == 1


def test_layer_matches_count_once_per_layer(tmp_path):
    report = identify_components(_drawing(tmp_path), layer_matches=True)
    doors = next(group for group in report.by_type if group.name == "门")
    # Two block references plus one layer entry for DOOR_TAG.
    assert doors.count == 3
    assert doors.entity_count == 3
    assert "DOOR_TAG" in doors.layer_only
    assert report.layer_matches is True


def test_geometry_entities_are_not_matched_by_layer(tmp_path):
    report = identify_components(_drawing(tmp_path))
    doors = next(group for group in report.by_type if group.name == "门")
    assert doors.bbox() is not None
    assert "DOOR_900" in doors.blocks


def test_layer_filter_restricts_hits(tmp_path):
    import re

    report = identify_components(
        _drawing(tmp_path), layer_filter=re.compile("WINDOWS", re.IGNORECASE)
    )
    assert [group.name for group in report.by_type] == ["窗"]


def test_custom_rules_file(tmp_path):
    rules_path = tmp_path / "rules.json"
    rules_path.write_text(
        json.dumps(
            {
                "rules": [
                    {"name": "自定义块", "block_re": "UNKNOWN", "layer_re": "MISC"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    rules = load_rules(rules_path)
    report = identify_components(_drawing(tmp_path), rules=rules)
    assert [group.name for group in report.by_type] == ["自定义块"]
    assert report.total == 1


def test_compile_rules_rejects_invalid_regex():
    with pytest.raises(ValueError):
        compile_rules([("坏规则", "[", "")])


def test_builtin_table_is_complete():
    names = {name for name, _block, _layer in BUILTIN_RULES}
    assert {"门", "窗", "梁", "柱", "楼梯", "道路", "渠道"} <= names
    assert len(BUILTIN_RULES) >= 26


def test_markdown_and_rows(tmp_path):
    report = identify_components(_drawing(tmp_path))
    markdown = component_markdown(report)
    assert "构件" in markdown and "数量" in markdown
    rows = component_rows(report)
    assert rows and rows[0]["构件类型"]


def test_group_bbox_handles_empty_positions():
    assert ComponentGroup("空").bbox() is None
