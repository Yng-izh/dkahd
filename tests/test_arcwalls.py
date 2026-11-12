"""弧墙（同心圆弧配对）判据：只看几何不变量，不看图层名、不看块名。"""
import math

import pytest

from dwg_reader.recognition import arcwalls as AW
from dwg_reader.recognition import walls as W


def _arc(cx, cy, r, a0, a1, layer="WALL", handle=None, derived=False):
    rec = {"type": "ARC", "layer": layer, "center": [cx, cy], "radius": r,
           "start_angle": a0, "end_angle": a1, "handle": handle or f"H{id(r)}"}
    if derived:
        rec["derived"] = True
    return rec


def _snapshot(*arcs):
    return {"entities": list(arcs), "derived": []}


def test_concentric_pair_becomes_one_arc_wall():
    walls, diag = AW.detect(_snapshot(_arc(0, 0, 1000, 0, 90, handle="A"),
                                      _arc(0, 0, 1200, 0, 90, handle="B")),
                            layers=["WALL"], thickness=200.0)
    assert len(walls) == 1
    wall = walls[0]
    assert wall["orient"] == "A" and wall["arc"] is True
    assert wall["thickness"] == pytest.approx(200.0)
    # 弧长按中线半径算：半圆角 90° × r=1100
    assert wall["length"] == pytest.approx(math.radians(90) * 1100, rel=1e-3)
    assert wall["curve"]["inner_radius"] == 1000.0
    assert wall["curve"]["outer_radius"] == 1200.0
    # 轮廓是闭合折线带（内弧 + 外弧），不是四点多边形
    assert len(wall["outline"]) > 8
    assert wall["evidence_state"] == "strong"          # 200 正好等于本图墙厚
    assert diag["walls"] == 1


def test_different_centers_are_not_a_wall():
    """两道弧半径差对得上，但圆心不同：各自是独立的弧，不是一道墙。"""
    walls, _ = AW.detect(_snapshot(_arc(0, 0, 1000, 0, 90),
                                   _arc(500, 0, 1200, 0, 90)),
                         layers=["WALL"], thickness=200.0)
    assert walls == []


def test_thickness_is_the_radius_difference_not_a_bucket():
    """半径差 2000mm 远超"一段墙厚"，那是两道不同的墙，不是一堵厚墙。"""
    walls, _ = AW.detect(_snapshot(_arc(0, 0, 1000, 0, 90),
                                   _arc(0, 0, 3000, 0, 90)),
                         layers=["WALL"], thickness=200.0)
    assert walls == []


def test_third_concentric_face_blocks_the_outer_pair():
    """三层面线（幕墙画法）：外层那对跨了中线，只剩相邻的一道，且只留一道。"""
    walls, diag = AW.detect(_snapshot(_arc(0, 0, 1000, 0, 90),
                                      _arc(0, 0, 1100, 0, 90),
                                      _arc(0, 0, 1200, 0, 90)),
                            layers=["WALL"], thickness=200.0)
    assert len(walls) == 1
    assert walls[0]["thickness"] == pytest.approx(100.0)
    assert diag["rejected"].get("中间夹第三条面线") == 1


def test_partial_overlap_crops_the_wall_and_keeps_no_opening():
    """两条弧只在 30°~90° 重叠：墙就是那一段，缺的部分是洞口，不补。"""
    walls, _ = AW.detect(_snapshot(_arc(0, 0, 1000, 0, 90),
                                   _arc(0, 0, 1200, 30, 120)),
                         layers=["WALL"], thickness=200.0)
    assert len(walls) == 1
    wall = walls[0]
    assert wall["arc_sweep_deg"] == pytest.approx(60.0, abs=1e-6)
    assert wall["length"] == pytest.approx(math.radians(60) * 1100, rel=1e-3)


def test_disjoint_angular_ranges_rejected():
    walls, _ = AW.detect(_snapshot(_arc(0, 0, 1000, 0, 30),
                                   _arc(0, 0, 1200, 180, 210)),
                         layers=["WALL"], thickness=200.0)
    assert walls == []


def test_block_geometry_is_not_a_wall_source():
    """块展开几何里的同心弧大半年是门扇/洁具：同一对弧，标 derived 就不认。"""
    snap = {"entities": [], "derived": [_arc(0, 0, 1000, 0, 90, derived=True),
                                        _arc(0, 0, 1200, 0, 90, derived=True)]}
    walls, _ = AW.detect(snap, layers=["WALL"], thickness=200.0)
    assert walls == []


def test_only_wall_layers_are_considered():
    walls, _ = AW.detect(_snapshot(_arc(0, 0, 1000, 0, 90, layer="家具"),
                                   _arc(0, 0, 1200, 0, 90, layer="家具")),
                         layers=["WALL"], thickness=200.0)
    assert walls == []


def test_merge_runs_passes_arc_walls_through_without_flattening():
    """合并环节必须原样放过弧墙：那里会按 orient/center 重新生成直线轮廓。"""
    walls, _ = AW.detect(_snapshot(_arc(0, 0, 1000, 0, 90),
                                   _arc(0, 0, 1200, 0, 90)),
                         layers=["WALL"], thickness=200.0)
    outline = [list(p) for p in walls[0]["outline"]]
    merged = W.merge_runs(list(walls), [], min_length=0)
    assert len(merged) == 1
    assert merged[0]["arc"] is True
    assert merged[0]["outline"] == outline
    assert merged[0]["length"] == pytest.approx(walls[0]["length"])


def test_merge_across_openings_keeps_arc_walls():
    walls, _ = AW.detect(_snapshot(_arc(0, 0, 1000, 0, 90),
                                   _arc(0, 0, 1200, 0, 90)),
                         layers=["WALL"], thickness=200.0)
    out = W.merge_across_openings(list(walls), [], min_length=0)
    assert [w["arc"] for w in out] == [True]
