"""组装后过滤器（图例图层 / 同位置重复 / 宽洞口标记）单元测试。

实测背景（艺术楼-图层混乱）：「设置」层是构造做法与图例，上面半径 10mm、40mm 的
装饰弧被当成两樘"平开门"，门窗大样里的块被当成门窗，同一个块还在同一坐标画了两遍。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(SKILL, "src"))

from dwg_reader.recognition.nodes import _filter_components, _is_detail_layer  # noqa: E402

SCALE = {"thickness": 200.0, "door_w": 1000.0, "text_h": 350.0,
         "bay": 6000.0, "noise": 2.0}


def _comp(cid, cat="door", xy=(0.0, 0.0), width=1000.0, layers=("WALL",)):
    return {"id": cid, "category": cat, "layer": layers[0],
            "insert_pt": list(xy),
            "attributes": {"width_mm": width},
            "source_entities": [{"handle": f"{cid}-h", "type": "LINE", "layer": l}
                                for l in layers],
            "evidence": {"detail": "test"}}


def test_detail_layers_are_recognised():
    for name in ("门窗大样", "图例", "构造做法", "DETAIL", "LEGEND"):
        assert _is_detail_layer(name), name
    # 「设置」不算详图层：实测艺术楼该层 186 个图元里 115 个落在卫生间平面内，
    # 是洁具与隔断（含两樘隔断门）的布置。把它整层排除会删掉真门。
    for name in ("WALL", "COLUMN", "WINDOW", "AXIS", "0", "设置"):
        assert not _is_detail_layer(name), name


def test_component_on_detail_layer_is_dropped():
    c = _comp("DOOR-01", layers=("门窗大样",))
    keep, dropped = _filter_components([c], SCALE)
    assert keep == [] and len(dropped) == 1
    assert "详图" in dropped[0]["drop_reason"]


def test_component_touching_a_real_layer_survives():
    """跨层构件（一半在大样层）不因为"有一个源在设置层"就被删。"""
    c = _comp("DOOR-02", layers=("设置", "WINDOW"))
    keep, dropped = _filter_components([c], SCALE)
    assert len(keep) == 1 and not dropped


def test_duplicate_at_same_position_dropped():
    a = _comp("DOOR-03", xy=(51622.0, 58741.0))
    b = _comp("DOOR-04", xy=(51622.0, 58741.0))
    keep, dropped = _filter_components([a, b], SCALE)
    assert [c["id"] for c in keep] == ["DOOR-03"]
    assert "重复" in dropped[0]["drop_reason"]


def test_wide_opening_is_flagged_not_deleted():
    """宽洞口可能是大门/卷帘门——只标记待核，不删。"""
    c = _comp("DOOR-05", width=5538.0)
    keep, dropped = _filter_components([c], SCALE)
    assert len(keep) == 1 and not dropped
    assert keep[0]["needs_review"] is True
    assert keep[0]["attributes"]["wide_opening"] is True
    assert "卷帘门" in keep[0]["evidence"]["wide_opening"]


def test_normal_door_untouched():
    c = _comp("DOOR-06", width=1000.0)
    keep, dropped = _filter_components([c], SCALE)
    assert len(keep) == 1 and not dropped
    assert not keep[0].get("needs_review")
