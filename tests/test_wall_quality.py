"""墙网质量回归：用**原始 handle 集合**比较改动前后的几何恢复情况。

构件总数不能用来评价墙网好坏（墙段 vs 连续墙实例口径不同），所以这里直接比
"哪些墙线被用上了"：

* 办公楼：已确认的墙体不许回退（``before - after`` 必须为空）；
* t5：370mm 主墙必须恢复，短墙段必须能成墙，且不再出现"厚度超出上限"的否决。

基准 ``fixtures/wall_handles.json`` 由
``dwg-dag-20261107-1700.zip``（改动前）与改动后的墙体分段实测生成。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(SKILL))          # 工作区根
sys.path.insert(0, os.path.join(SKILL, "src"))

from dwg_reader.recognition import walls as WALLS          # noqa: E402
from dwg_reader.recognition.pipeline import run_recognition  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "wall_handles.json")
OFFICE = os.path.join(ROOT, "办公楼.dxf")
T5 = os.path.join(ROOT, "work", "t5", "ai识图测试2_t5.dxf")
BASE = json.load(open(FIXTURE, encoding="utf-8"))["cases"]


def _run(dxf, profile):
    run = run_recognition(dxf, targets=("walls_seg",), profile=profile)
    out = run.outputs["walls_seg"]
    return out["walls_seg"], out["wdropped"], out["wdiag"]


def _handles(walls):
    return {s["handle"] for w in walls for s in (w.get("srcs") or []) if s.get("handle")}


def _profile(name):
    return os.path.join(ROOT, "work", "profiles", f"{name}.json")


# --------------------------------------------------------------------- 合成单元
def test_interval_pairing_keeps_gaps():
    """两面线只在同时存在的区段成墙：中间空段不被填成实体墙。"""
    g1 = {"pmax": 0.0, "members": [{"p": 0.0, "a": 0.0, "b": 1000.0, "srcs": []},
                                   {"p": 0.0, "a": 2000.0, "b": 3000.0, "srcs": []}]}
    g2 = {"pmin": 200.0, "members": [{"p": 200.0, "a": 0.0, "b": 1000.0, "srcs": []},
                                     {"p": 200.0, "a": 1500.0, "b": 3000.0, "srcs": []}]}
    spans = WALLS._pair_intervals(g1, g2)
    # 只有 g1[0]∩g2[0] 与 g1[1]∩g2[1] 是两面线同时存在的区段
    assert spans == [(0.0, 1000.0), (2000.0, 3000.0)]
    assert not any(a <= 1500 and b >= 1800 for a, b in spans), "缺口被填成了实体墙"
    assert all(b - a >= WALLS.MIN_OVERLAP for a, b in spans)


def test_thickness_profile_marks_isolated_artifact_weak():
    """孤立厚配对只降级为"弱证据"，不再作为墙存在性的否决。

    弱墙可以进墙网、可以被局部求解采用，但由 :mod:`nodes` 的装配阶段
    压到 uncertain，不得凭几何外观直接 confirmed。
    """
    cands = [{"thickness": 300.0, "a": 0.0, "b": 6000.0, "caps": []},
             {"thickness": 300.0, "a": 7000.0, "b": 12000.0, "caps": []},
             {"thickness": 370.0, "a": 0.0, "b": 4000.0, "caps": []},
             {"thickness": 370.0, "a": 5000.0, "b": 9000.0, "caps": []},
             {"thickness": 490.0, "a": 0.0, "b": 300.0, "caps": []}]
    clusters, ok, weak = WALLS._thickness_profile(cands)
    assert {300, 370} <= ok
    assert 490 not in ok
    assert 490 in weak
    assert clusters["490"]["weak"] is True
    assert clusters["370"]["n"] == 2


# --------------------------------------------------------------------- 真实图纸
@pytest.mark.skipif(not os.path.exists(OFFICE) or not os.path.exists(_profile("office")),
                    reason="缺少办公楼.dxf 或外部工作区 profile：work/profiles/office.json")
def test_office_wall_handles_do_not_regress():
    walls, _dropped, _diag = _run(OFFICE, _profile("office"))
    now = _handles(walls)
    lost = set(BASE["office"]["before"]["handles"]) - now
    assert not lost, f"墙体用的原始 handle 回退 {len(lost)} 个：{sorted(lost)[:8]}"
    assert len(walls) >= BASE["office"]["before"]["walls"]
    total = sum(float(w.get("length") or 0) for w in walls)
    assert total >= 0.99 * BASE["office"]["before"]["total_length"]


@pytest.mark.skipif(not os.path.exists(T5), reason="缺少 t5 测试图")
def test_t5_370_wall_recovered():
    walls, dropped, diag = _run(T5, _profile("tdw"))
    thick = {}
    for w in walls:
        t = int(round(float(w["thickness"]) / 10.0)) * 10
        thick[t] = thick.get(t, 0) + 1
    assert thick.get(370, 0) >= 20, f"370mm 主墙未恢复：{thick}"
    # 旧的"固定厚度上限"否决必须消失
    assert not [w for w in dropped if "超出墙厚上限" in (w.get("drop_reason") or "")]
    clusters = diag.get("thickness_clusters") or {}
    assert clusters.get("370", {}).get("ok") is True
    assert diag.get("dropped", 10 ** 9) < BASE["t5"]["before"]["dropped"]


@pytest.mark.skipif(not os.path.exists(T5), reason="缺少 t5 测试图")
def test_t5_short_segments_form_walls():
    walls, _dropped, diag = _run(T5, _profile("tdw"))
    now = _handles(walls)
    recovered = now & set(BASE["t5"]["recovered"])
    assert len(recovered) >= 100, f"短墙段恢复不足：{len(recovered)}"
    short = [w for w in walls if 200.0 <= float(w["length"]) <= 420.0]
    assert short, "没有 200~420mm 的短墙段成墙"
    total = sum(float(w.get("length") or 0) for w in walls)
    assert total > 1.15 * BASE["t5"]["before"]["total_length"]
