"""柱块"实心四边形"识别路径的单元测试。

背景：天正/探索者一类软件把柱画成**单位方块 + 插入缩放**（``M_S0BKA`` 400×400、
92 个实例）。这类块名字不在任何白名单里、块里也只有一个 SOLID，所以"块名 /
闭合多段线 / 线段组合"三条老路都走不通——实测 100 根柱只认出 7 根。
这里锁定新路径的四条否决条件，防止它变成"见块就算柱"的过检后门。
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(SKILL, "src"))

from dwg_reader.recognition import elements as E  # noqa: E402

UNIT_QUAD = [[0.5, 0.5], [0.5, -0.5], [-0.5, 0.5], [-0.5, -0.5]]
TRIANGLE = [[0.0, 0.0], [-1.0, 0.1], [-1.0, -0.1]]

SCALE = {"thickness": 200.0, "door_w": 1000.0, "text_h": 350.0,
         "bay": 6000.0, "noise": 2.0}


def _block(solids=None, arcs=None, attdefs=0, extent=(1.0, 1.0)):
    return {"solids": list(solids or ()), "arcs": list(arcs or ()),
            "loops": [], "lines": [], "attdefs": attdefs,
            "extent": [extent[0], extent[1]]}


def _insert(name, x=0.0, y=0.0, scale=400.0, rot=0.0, layer="COLUMN", handle=None):
    return {"type": "INSERT", "name": name, "layer": layer,
            "handle": handle or f"{name}-{x}-{y}",
            "insert": [x, y], "xscale": scale, "yscale": scale,
            "rotation": rot}


def _snap(blocks, inserts):
    return {"blocks": blocks, "entities": list(inserts)}


def test_unit_square_solid_block_is_a_column_family():
    snap = _snap({"M_S0BKA": _block([UNIT_QUAD])},
                 [_insert("M_S0BKA", x=1000 * i) for i in range(3)])
    found = E._footprint_blocks(snap, scale=SCALE)
    assert list(found) == ["M_S0BKA"]
    assert "实心四边形" in found["M_S0BKA"]["why"]


def test_rotated_instances_use_true_section_not_bbox():
    """旋转 45° 的 400×400 柱，外接框是 565——不能因此被当成 565 的柱或超限。"""
    snap = _snap({"M_S0BKA": _block([UNIT_QUAD])},
                 [_insert("M_S0BKA", x=0, y=0),
                  _insert("M_S0BKA", x=3000, y=0, rot=45.0)])
    found = E._footprint_blocks(snap, scale=SCALE)
    assert "M_S0BKA" in found
    assert found["M_S0BKA"]["size"][0] == pytest.approx(400.0)


def test_section_size_matches_scale_times_extent():
    """块画成真实尺寸 + 缩放 1，也必须算出真实截面（不能算成 1mm）。"""
    rec = _insert("REAL", scale=1.0)
    blocks = {"REAL": _block([UNIT_QUAD], extent=(400.0, 400.0))}
    assert E._section_size(rec, blocks) == (400.0, 400.0)


def test_triangle_solid_is_not_a_column():
    """箭头/指北针也是 SOLID 块，但是三角形。"""
    snap = _snap({"_ARROW": _block([TRIANGLE])},
                 [_insert("_ARROW", x=0), _insert("_ARROW", x=1000)])
    assert E._footprint_blocks(snap, scale=SCALE) == {}


def test_block_with_attributes_is_not_a_column():
    """带编号属性的是门窗/设备块，柱不带属性。"""
    snap = _snap({"XBLK": _block([UNIT_QUAD], attdefs=1)},
                 [_insert("XBLK", x=0), _insert("XBLK", x=1000)])
    assert E._footprint_blocks(snap, scale=SCALE) == {}


def test_block_with_arc_is_not_a_column():
    snap = _snap({"DBLK": _block([UNIT_QUAD], arcs=[[0, 0, 500, 0, 90]])},
                 [_insert("DBLK", x=0), _insert("DBLK", x=1000)])
    assert E._footprint_blocks(snap, scale=SCALE) == {}


def test_single_instance_is_not_a_column_family():
    snap = _snap({"ONE": _block([UNIT_QUAD])}, [_insert("ONE", x=0)])
    assert E._footprint_blocks(snap, scale=SCALE) == {}


def test_wrong_size_block_is_not_a_column():
    """2400 的方块是设备基础/装饰，不是柱（超过 3 个门宽）。"""
    snap = _snap({"BIG": _block([UNIT_QUAD])},
                 [_insert("BIG", x=0, scale=2400.0), _insert("BIG", x=9000, scale=2400.0)])
    assert E._footprint_blocks(snap, scale=SCALE) == {}


def test_elongated_block_is_not_a_column():
    snap = _snap({"LONG": _block([UNIT_QUAD])},
                 [{"type": "INSERT", "name": "LONG", "layer": "COLUMN", "handle": "a",
                   "insert": [0, 0], "xscale": 3000.0, "yscale": 400.0, "rotation": 0.0},
                  {"type": "INSERT", "name": "LONG", "layer": "COLUMN", "handle": "b",
                   "insert": [9000, 0], "xscale": 3000.0, "yscale": 400.0, "rotation": 0.0}])
    assert E._footprint_blocks(snap, scale=SCALE) == {}


def test_family_with_mixed_sections_still_counts():
    """同一张图里 400/565 两种柱并存是常态，不能因为"尺寸不一致"整族否掉。"""
    snap = _snap({"M_S0BKA": _block([UNIT_QUAD])},
                 [_insert("M_S0BKA", x=0, scale=400.0),
                  _insert("M_S0BKA", x=3000, scale=565.0),
                  _insert("M_S0BKA", x=6000, scale=400.0)])
    found = E._footprint_blocks(snap, scale=SCALE)
    assert "M_S0BKA" in found
    assert found["M_S0BKA"]["n"] == 3
