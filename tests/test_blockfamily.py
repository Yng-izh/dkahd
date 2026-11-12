"""块族（blockfamily）单元测试：一次定角色、全族实例继承。

锁住三件事：
1. 族是按块名聚合的，同族多尺寸**不拆族**（窗宽 600/800/1500 是常态）；
2. **块名只是提名，几何必须确认**——"名字像门、块里没门弧"的块不许进名单；
3. 几何指纹跨图可迁移（不带块名），匿名块名也能用。
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(SKILL, "src"))

from dwg_reader.recognition import blockfamily as BF  # noqa: E402

SCALE = {"thickness": 200.0, "door_w": 1000.0, "text_h": 350.0,
         "bay": 6000.0, "noise": 2.0}
QUAD = [[0.5, 0.5], [0.5, -0.5], [-0.5, 0.5], [-0.5, -0.5]]
TRI = [[0.0, 0.0], [-1.0, 0.1], [-1.0, -0.1]]


def _snap(blocks, inserts):
    return {"blocks": blocks, "entities": inserts}


def _ins(name, x=0.0, sx=400.0, sy=None, layer="COLUMN", rot=0.0):
    return {"type": "INSERT", "name": name, "layer": layer, "handle": f"{name}:{x}",
            "insert": [x, 0.0], "xscale": sx, "yscale": sy if sy is not None else sx,
            "rotation": rot}


def _defs(**kw):
    d = {"arcs": [], "lines": [], "loops": [], "solids": [], "attdefs": 0,
         "extent": [1.0, 1.0]}
    d.update(kw)
    return d


# ------------------------------------------------------------------ 聚族
def test_families_group_by_block_name():
    snap = _snap({"M_S0BKA": _defs(solids=[QUAD])},
                 [_ins("M_S0BKA", x=0), _ins("M_S0BKA", x=5000)])
    fams = BF.families(snap)
    assert set(fams) == {"M_S0BKA"}
    assert fams["M_S0BKA"]["n"] == 2


def test_multiple_sizes_stay_in_one_family():
    """同族不同规格（窗宽 600/800/1500）不能拆成三个族。"""
    snap = _snap({"$WinLib2D$00000001": _defs(attdefs=1, lines=[
        [-0.5, 0.5, 0.5, 0.5], [-0.5, 0.2, 0.5, 0.2],
        [-0.5, -0.2, 0.5, -0.2], [-0.5, -0.5, 0.5, -0.5]])},
        [_ins("$WinLib2D$00000001", x=0, sx=600, sy=180),
         _ins("$WinLib2D$00000001", x=2000, sx=800, sy=180),
         _ins("$WinLib2D$00000001", x=4000, sx=1500, sy=180)])
    fams = BF.families(snap)
    assert fams["$WinLib2D$00000001"]["n"] == 3
    assert fams["$WinLib2D$00000001"]["n_sizes"] == 3


# ------------------------------------------------------------------ 角色
def test_solid_quad_family_is_column():
    snap = _snap({"M_S0BKA": _defs(solids=[QUAD])},
                 [_ins("M_S0BKA", x=0), _ins("M_S0BKA", x=5000)])
    role = BF.table(snap, scale=SCALE)["M_S0BKA"]
    assert role["role"] == "column"
    assert role["confidence"] >= BF.CONFIRM


def test_triangle_solid_is_not_column():
    snap = _snap({"_ARROW": _defs(solids=[TRI])},
                 [_ins("_ARROW", x=0), _ins("_ARROW", x=5000)])
    assert BF.table(snap, scale=SCALE)["_ARROW"]["role"] != "column"


def test_name_prior_alone_is_not_confirmed():
    """名字像门、块里没有门弧 —— 只许当假设，不许进识别名单。"""
    snap = _snap({"_DBLK2": _defs(lines=[[0, 0, 1, 0]])},
                 [_ins("_DBLK2", x=0, sx=1200), _ins("_DBLK2", x=3000, sx=1200)])
    rec = BF.table(snap, scale=SCALE)["_DBLK2"]
    assert rec["name_prior"] == "door"
    assert rec["confidence"] < BF.CONFIRM
    assert BF.names_for(snap, "door", scale=SCALE) == {}


def test_door_arc_confirms_door():
    snap = _snap({"_DBLK1": _defs(arcs=[[0, 0, 0.5, 0, 90]], attdefs=1)},
                 [_ins("_DBLK1", x=0, sx=1200), _ins("_DBLK1", x=3000, sx=1200)])
    assert "geometric:door_block_arc" == BF.names_for(
        snap, "door", scale=SCALE)["_DBLK1"]


def test_window_needs_parallel_separated_strokes():
    """四条完全叠在一处的线不是窗符号（那是图框/图例）。"""
    stacked = [[0, 0, 1, 0]] * 4
    snap = _snap({"_WBLK": _defs(lines=stacked, attdefs=1)},
                 [_ins("_WBLK", x=0, sx=2700, sy=180)])
    assert "_WBLK" not in BF.names_for(snap, "window", scale=SCALE)

    separated = [[-0.5, 0.5, 0.5, 0.5], [-0.5, 0.17, 0.5, 0.17],
                 [-0.5, -0.17, 0.5, -0.17], [-0.5, -0.5, 0.5, -0.5]]
    snap2 = _snap({"_WBLK": _defs(lines=separated, attdefs=1)},
                  [_ins("_WBLK", x=0, sx=2700, sy=180)])
    assert "_WBLK" in BF.names_for(snap2, "window", scale=SCALE)


# ------------------------------------------------------------------ 纪律
def test_declared_layers_gate_geometry_only_families():
    """声明了门窗层时，只靠几何认定的族必须出现在声明层上。"""
    separated = [[-0.5, 0.5, 0.5, 0.5], [-0.5, 0.17, 0.5, 0.17],
                 [-0.5, -0.17, 0.5, -0.17], [-0.5, -0.5, 0.5, -0.5]]
    snap = _snap({"_WBLK": _defs(lines=separated, attdefs=1)},
                 [_ins("_WBLK", x=0, sx=2700, sy=180, layer="0")])
    plan = {"layers": {"window": ["WINDOW"]}}
    assert BF.names_for(snap, "window", scale=SCALE, plan=plan) == {}
    assert "_WBLK" in BF.names_for(snap, "window", scale=SCALE)


def test_fingerprint_does_not_contain_block_name():
    """跨图迁移靠几何指纹，不能靠（每次保存都会变的）匿名块名。"""
    snap = _snap({"A$C68D277C8": _defs(solids=[QUAD])},
                 [_ins("A$C68D277C8", x=0), _ins("A$C68D277C8", x=5000)])
    fp = BF.table(snap, scale=SCALE)["A$C68D277C8"]["fingerprint"]
    assert "A$C" not in fp
    assert "solidquad" in fp


def test_summary_is_printable():
    snap = _snap({"M_S0BKA": _defs(solids=[QUAD])},
                 [_ins("M_S0BKA", x=0), _ins("M_S0BKA", x=5000)])
    rows = BF.summary(BF.table(snap, scale=SCALE))
    assert rows[0]["block"] == "M_S0BKA"
    assert rows[0]["role_cn"] == "柱"


# ------------------------------------------------------------------ 多族同角色
def test_one_role_can_hold_many_families():
    """同一个角色（门）在图上常常有好几个块名——这是常态，不是例外。"""
    arcs = [[0, 0, 0.5, 0, 90]]
    snap = _snap({"$DorLib2D$00000001": _defs(arcs=arcs, attdefs=1),
                  "$DorLib2D$00000013": _defs(arcs=arcs, attdefs=1),
                  "$TCHSYS$DOOR2D": _defs(arcs=arcs, attdefs=1)},
                 [_ins("$DorLib2D$00000001", x=0, sx=900),
                  _ins("$DorLib2D$00000013", x=3000, sx=900),
                  _ins("$TCHSYS$DOOR2D", x=6000, sx=1000)])
    names = BF.names_for(snap, "door", scale=SCALE)
    assert set(names) == {"$DorLib2D$00000001", "$DorLib2D$00000013",
                          "$TCHSYS$DOOR2D"}
    by = BF.by_role(BF.table(snap, scale=SCALE))
    assert len(by["door"]) == 3


def test_same_series_inherits_confirmed_role():
    """同图库系列：一族几何确认 -> 同系列其它编号的族继承（不必各有几何）。"""
    arcs = [[0, 0, 0.5, 0, 90]]
    snap = _snap({"$DorLib2D$00000001": _defs(arcs=arcs, attdefs=1),
                  "$DorLib2D$00000013": _defs(arcs=arcs, attdefs=1)},
                 [_ins("$DorLib2D$00000001", x=0, sx=900),
                  _ins("$DorLib2D$00000013", x=3000, sx=900)])
    rows = BF.table(snap, scale=SCALE)
    # 两族都会被判成门（各自几何都够），置信度不低于门槛
    assert rows["$DorLib2D$00000013"]["role"] == "door"
    assert rows["$DorLib2D$00000013"]["confidence"] >= BF.CONFIRM


def test_propagation_never_overrides_confirmed_role():
    """几何已确认成窗的门库块（门联窗），不许被系列互证改回门。"""
    separated = [[-0.5, 0.5, 0.5, 0.5], [-0.5, 0.17, 0.5, 0.17],
                 [-0.5, -0.17, 0.5, -0.17], [-0.5, -0.5, 0.5, -0.5]]
    snap = _snap({"$DorLib2D$00000001": _defs(arcs=[[0, 0, 0.5, 0, 90]], attdefs=1),
                  "$DorLib2D$00000019": _defs(lines=separated, attdefs=1)},
                 [_ins("$DorLib2D$00000001", x=0, sx=900),
                  _ins("$DorLib2D$00000019", x=3000, sx=2700, sy=180)])
    rows = BF.table(snap, scale=SCALE)
    assert rows["$DorLib2D$00000001"]["role"] == "door"
    assert rows["$DorLib2D$00000019"]["role"] == "window"


def test_series_key_groups_vendor_variants():
    assert BF.series_of("$DorLib2D$00000013") == BF.series_of("$DorLib2D$00000001")
    assert BF.series_of("_DBLK1") == BF.series_of("_DBLK")
    assert BF.series_of("$TCHSYS$DOOR2D") != BF.series_of("$TCHSYS$WIN2D")


# ------------------------------------------------------------------ 长相提名也要几何确认
def _fixture_def():
    """马桶块的真实长相（艺术楼 M_E24TTP）：椭圆盆 + 圆角矩形底座。

    7 条弧里 4 条是 90° 的小圆角（半径 30），2 条是画盆沿的大弧——**一条门扇
    摆动弧都没有**，但"有弧 + 长宽比像门"足以骗过只看形状的分类器。
    """
    arcs = [[-227.4, 412.8, 397.5, 337.5, 36.6], [227.4, 412.8, 397.5, 143.4, 202.5]]
    arcs += [[-220.0, 30.0, 30.0, 180.0, 270.0], [-220.0, 170.0, 30.0, 90.0, 180.0],
             [220.0, 30.0, 30.0, 270.0, 360.0], [220.0, 170.0, 30.0, 0.0, 90.0]]
    lines = [[-169.7, 200.4, -169.7, 430.4], [169.7, 200.4, 169.7, 430.4],
             [220.0, 0.0, -220.0, 0.0], [-139.7, 260.4, 139.7, 260.4],
             [-220.0, 200.0, 220.0, 200.0], [-250.0, 30.0, -250.0, 170.0]]
    return _defs(arcs=arcs, lines=lines, extent=[1250.0, 810.0])


def test_fixture_block_is_not_a_door():
    """**马桶不是门**：洁具用弧画轮廓，门用弧画开启——两者的弧不一样。"""
    ok, why = BF.signature_ok({"def": _fixture_def(), "xscale_med": 1.0},
                              "door", scale=SCALE)
    assert not ok, why
    assert "摆动弧" in why or "180" in why


def test_door_signature_accepts_unit_door_block():
    """单位门块（一块 1 个图形单位 + 一条 90° 摆动弧，插入缩放 1000）= 门。"""
    ok, _why = BF.signature_ok(
        {"def": _defs(arcs=[[-0.4, 0.1, 0.8, 359.9, 92.9]],
                      lines=[[-0.5, 0.1, -0.5, 0.9]]), "xscale_med": 1000.0},
        "door", scale=SCALE)
    assert ok


def test_door_signature_rejects_contour_arc():
    """一条 300° 的大弧是"画轮廓"用的（盆沿/圆形家具），不是门扇。"""
    ok, why = BF.signature_ok(
        {"def": _defs(arcs=[[0, 0, 0.8, 0, 300]]), "xscale_med": 1000.0},
        "door", scale=SCALE)
    assert not ok and "180" in why


def test_window_signature_rejects_arc_blocks():
    """带开启弧的块不是窗（窗没有铰点）。"""
    ok, _why = BF.signature_ok(
        {"def": _defs(arcs=[[0, 0, 0.5, 0, 90]]), "xscale_med": 1500.0},
        "window", scale=SCALE)
    assert not ok
