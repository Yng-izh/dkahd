"""判定优先级（priority）单元测试。

锁住这次踩到的坑：5.5m 长、180mm 厚（=墙厚）、写着 C15、画在 WINDOW 层的窗，
被门的形状判据抢先命中，输出成"5538mm 宽的门"。优先级裁决要求：**按证据强度定类，
不按谁先跑完**，并把覆盖留下痕迹。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(SKILL, "src"))

from dwg_reader.recognition import priority as P  # noqa: E402

PLAN = {"layers": {"window": ["WINDOW"], "door": ["DOOR"]}}
FAM_WIN = {"_GCBLK": {"role": "window", "confidence": 0.8, "why": "几何指纹:多笔画窗"}}
WALL_T = 180.0


def _thin_long():
    """5.5m 长、180mm 厚的通长窗记录（被门路径产出）。"""
    return {"block": "_GCBLK", "layer": "设置", "width": 5538.0,
            "thickness": 180.0, "attr": None, "swing": {"type": "双扇"},
            "hinges": [[57309.4, 64276.5]],
            "opening": [[52190.1, 64978.9], [57728.1, 64978.9]],
            "axis": [[52190.1, 64978.9], [57728.1, 64978.9]]}


def _swing_door(width=1000.0):
    return {"block": "_DBLK1", "layer": "WALL", "width": width, "thickness": WALL_T,
            "attr": None, "swing": {"type": "单扇"}, "hinges": [[0.0, 0.0]],
            "opening": [[0.0, 0.0], [width, 0.0]], "axis": [[0.0, 0.0], [width, 0.0]]}


# ------------------------------------------------------------------ 分级
def test_code_category_follows_drafting_convention():
    assert P.code_category("C15") == "window"
    assert P.code_category("GC4") == "window"
    assert P.code_category("M3") == "door"
    assert P.code_category("FHM1") == "door"
    assert P.code_category("GM2") == "door"
    assert P.code_category("") is None


def test_form_window_needs_no_leaf_geometry():
    cat, why = P.form_category({"thickness": 180.0, "width": 5538.0}, WALL_T)
    assert cat == "window" and "通长窗" in why


def test_form_door_needs_plausible_leaf():
    cat, _ = P.form_category(_swing_door(1000.0), WALL_T)
    assert cat == "door"
    # 5538mm 的"门扇"不存在：有弧也不算门
    cat2, _ = P.form_category(_thin_long(), WALL_T)
    assert cat2 != "door"


# ------------------------------------------------------------------ 裁决
def test_block_family_beats_producing_path():
    v = P.decide(_thin_long(), "door", plan=PLAN, families=FAM_WIN, wall_thick=WALL_T)
    assert v["category"] == "window"
    assert v["level"] == 2 and v["override"] is True


def test_explicit_code_beats_everything():
    rec = dict(_thin_long(), attr="M3")          # 编号却写着门
    v = P.decide(rec, "door", plan=PLAN, families=FAM_WIN, wall_thick=WALL_T)
    assert v["category"] == "door" and v["level"] == 1


def test_normal_door_not_touched():
    v = P.decide(_swing_door(1000.0), "door", plan=PLAN, families={}, wall_thick=WALL_T)
    assert v["category"] == "door" and v["override"] is False


def test_layer_role_is_lower_priority_than_form():
    rec = dict(_thin_long())
    rec.pop("swing"), rec.pop("hinges")
    rec["layer"] = "DOOR"                        # 图层说门，形态说窗
    v = P.decide(rec, "door", plan=PLAN, families={}, wall_thick=WALL_T)
    assert v["category"] == "window" and v["level"] == 3


# ------------------------------------------------------------------ 整批
def test_apply_moves_record_and_records_override():
    doors = [_thin_long(), _swing_door(1000.0)]
    windows = []
    d, w, rep = P.apply(doors, windows, plan=PLAN, families=FAM_WIN,
                        wall_thick=WALL_T)
    assert len(d) == 1 and len(w) == 1
    moved = w[0]
    assert moved["center"] and moved["axis"]
    assert moved["category_override"]["from"] == "door"
    assert rep["overrides"] and rep["overrides"][0]["block"] == "_GCBLK"
    assert rep["doors_in"] == 2 and rep["doors_out"] == 1


def test_apply_is_idempotent_on_clean_input():
    doors = [_swing_door(1000.0)]
    windows = [{"block": "_WBLK", "layer": "WINDOW", "width": 2700.0,
                "thickness": 180.0, "attr": "C1", "axis": [[0, 0], [2700, 0]],
                "center": [1350, 0]}]
    d, w, rep = P.apply(doors, windows, plan=PLAN, families={}, wall_thick=WALL_T)
    assert len(d) == 1 and len(w) == 1 and not rep["overrides"]
