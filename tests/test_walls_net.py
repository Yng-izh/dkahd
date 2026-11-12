"""墙网分隔检验（walls_net）单元测试。

这一层是"生成侧"判据：一条线只有把空间分成两半才有资格成为墙。默认**关闭**，
所以这里的测试分两类：
  * 判据本身算得对不对（合成平面：真墙分隔、悬空线不分隔）；
  * 档位（off/evidence/strict）行为与安全阀是否守得住。
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(SKILL, "src"))

from dwg_reader.recognition import walls_net as WNET  # noqa: E402


def _wall(orient, center, a, b, thickness=200.0):
    if orient == "H":
        start, end = [a, center], [b, center]
    else:
        start, end = [center, a], [center, b]
    return {"orient": orient, "center": float(center), "thickness": float(thickness),
            "start": start, "end": end, "srcs": [{"handle": f"{orient}{center}-{a}",
                                                  "type": "LINE"}]}


def _grid_plan(half=2000.0):
    """两横两竖的井字平面 -> 4 间房；再在左下角房里放一条悬空线。"""
    walls = []
    for y in (-half, 0.0, half):
        walls.append(_wall("H", y, -half, half))
    for x in (-half, 0.0, half):
        walls.append(_wall("V", x, -half, half))
    floating = _wall("H", -half / 2.0, -half * 0.8, -half * 0.2)
    return walls, floating


def _gate(walls, **kw):
    kw.setdefault("min_space", 800_000.0)      # 0.8 m²（合成平面里房间 ≈ 4 m²）
    kw.setdefault("door_w", 1000.0)
    kw.setdefault("keep_ratio", 0.0)           # 测试里关掉"太激进"安全阀
    return WNET.separation_gate(walls, **kw)


# --------------------------------------------------------------------- 基本行为
def test_off_by_default(monkeypatch):
    monkeypatch.setattr(WNET, "MODE", "off")
    assert WNET.mode() == "off"
    assert WNET.enabled() is False


@pytest.mark.parametrize("raw,expect", [
    ("1", "evidence"), ("on", "evidence"), ("evidence", "evidence"),
    ("strict", "strict"), ("cut", "strict"),
    ("0", "off"), ("off", "off"), ("", "off"),
])
def test_mode_mapping(monkeypatch, raw, expect):
    monkeypatch.setattr(WNET, "MODE", raw)
    assert WNET.mode() == expect


def test_gate_noop_when_off(monkeypatch):
    monkeypatch.setattr(WNET, "MODE", "off")
    walls, _floating = _grid_plan()
    kept, cut, diag = WNET.separation_gate(walls)
    assert kept == walls and cut == []
    assert diag["reason"] == "disabled"


# --------------------------------------------------------------------- 判据正确性
def test_wall_between_rooms_separates_space(monkeypatch):
    monkeypatch.setattr(WNET, "MODE", "evidence")
    walls, _floating = _grid_plan()
    kept, cut, diag = _gate(walls)
    assert diag.get("fallback") is None, diag
    assert diag["spaces"] >= 4
    for w in walls:
        assert w["net_state"] == "separates_space", (w["orient"], w["center"])


def test_floating_line_inside_room_does_not_separate(monkeypatch):
    monkeypatch.setattr(WNET, "MODE", "evidence")
    walls, floating = _grid_plan()
    _kept, cut, _diag = _gate(walls + [floating])
    assert floating["net_state"] == "same_space"
    assert floating in cut
    assert "悬空线" in (floating.get("net_reason") or "")


def test_evidence_mode_is_positive_only_via_caller(monkeypatch):
    """判据只给结论，删不删由调用方按档位决定（evidence 档不删）。"""
    monkeypatch.setattr(WNET, "MODE", "evidence")
    walls, floating = _grid_plan()
    kept, cut, _diag = _gate(walls + [floating])
    assert floating in cut and floating not in kept
    # evidence 档下调用方拿 kept ∪ cut 就等于原候选集，一条不少
    assert len(kept) + len(cut) == len(walls) + 1


# --------------------------------------------------------------------- 安全阀
def test_fallback_when_no_space_structure(monkeypatch):
    monkeypatch.setattr(WNET, "MODE", "evidence")
    two = [_wall("H", 0.0, 0.0, 5000.0), _wall("H", 1000.0, 0.0, 5000.0)]
    kept, cut, diag = WNET.separation_gate(two, min_space=800_000.0)
    assert kept == two and cut == []
    assert diag["reason"] in ("no_space_structure", "too_few_candidates")


def test_fallback_when_everything_would_be_cut(monkeypatch):
    """通过率低于安全阈值 -> 整张图判"墙网不闭合"，候选全部保留。"""
    monkeypatch.setattr(WNET, "MODE", "strict")
    walls, floating = _grid_plan()
    # keep_ratio=1.0：只要有一条候选被判"不分隔"就必然触发退化
    kept, cut, diag = WNET.separation_gate(walls + [floating], min_space=800_000.0,
                                           door_w=1000.0, keep_ratio=1.0)
    assert cut == []
    assert diag.get("fallback") == "too_aggressive"
    assert len(kept) == len(walls) + 1


def test_too_few_candidates_short_circuits(monkeypatch):
    monkeypatch.setattr(WNET, "MODE", "evidence")
    one = [_wall("H", 0.0, 0.0, 5000.0)]
    kept, cut, diag = WNET.separation_gate(one, min_space=800_000.0)
    assert kept == one and cut == []
    assert diag["reason"] == "too_few_candidates"


# --------------------------------------------------------------------- 栅格细节
def test_close_mask_joins_one_cell_gaps():
    """闭运算本身是对的（补格缝）；默认不用它，是因为它会连真缺口一起补上。"""
    from dwg_reader.recognition import rooms as RM
    g = RM.Grid([0.0, 0.0, 500.0, 100.0], res=10.0)
    nx, ny = g.nx, g.ny
    j = ny // 2
    for i in range(3, nx - 3):
        if i in (nx // 2, nx // 2 + 1):         # 挖两格假缝
            continue
        g.mask[j * nx + i] = 1
    before = g.mask[nx // 2 + j * nx]
    WNET._close_mask(g, 2)
    after = g.mask[nx // 2 + j * nx]
    assert before == 0 and after != 0


def test_probe_direction_never_crosses_the_wall():
    """探测点落在挡格上时只许往外找，不许跨到墙的另一侧。"""
    from dwg_reader.recognition import rooms as RM
    g = RM.Grid([0.0, 0.0, 400.0, 200.0], res=10.0)
    nx, ny = g.nx, g.ny
    j = ny // 2
    wall_i = nx // 2
    for i in range(wall_i - 2, wall_i + 2):      # 4 格厚的墙
        for jj in range(ny):
            g.mask[jj * nx + i] = 1
    comp = RM._Components(g)
    cx, cy = g.to_world(wall_i, j)
    left = WNET._probe_dir(comp, g, cx - 60.0, cy, -1.0, 0.0, 60.0)
    right = WNET._probe_dir(comp, g, cx + 60.0, cy, 1.0, 0.0, 60.0)
    assert left is not None and right is not None
    assert left != right


def test_summary_is_honest_when_off(monkeypatch):
    monkeypatch.setattr(WNET, "MODE", "off")
    assert "关闭" in WNET.summary({"net": "off"})
    assert "不适用" in WNET.summary({"net": "evidence", "fallback": "no_space_structure"})
