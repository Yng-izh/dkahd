"""v2-component-focus：门窗柱专用路径的不变量测试。

测的是架构承诺，不是"某张图认对几个"：

* 档位真的少跑了节点（房间退出主流程）；
* 窄框窗不再被"全图墙厚"卡死（局部尺度）；
* 一个族能推广到旋转/镜像/缩放的其它实例；
* 家具轮廓不会被当成柱（缺结构位置要扣分并保留 uncertain）；
* 门和窗在同一位置不能同时成立。
"""
from __future__ import annotations

import pytest

from dwg_reader.recognition.v2 import cad_ir as IR
from dwg_reader.recognition.v2 import component_family as FAM
from dwg_reader.recognition.v2 import component_resolver as CR
from dwg_reader.recognition.v2 import fitters as FITTERS
from dwg_reader.recognition.v2 import geometry_graph as GG
from dwg_reader.recognition.v2 import instance_propagator as PROP
from dwg_reader.recognition.v2 import profiles as PROFILES
from dwg_reader.recognition.v2 import proposal_engines as ENGINES


def _line(handle, a, b, layer="0"):
    return {"handle": handle, "layer": layer, "type": "LINE", "start": a, "end": b}


def _window(x, y, *, w=1200, gap=60, rot=0.0, tag="W"):
    """一扇四线窗：两条长框线 + 两端封口；gap 可以远小于墙厚。"""
    import math
    cos, sin = math.cos(math.radians(rot)), math.sin(math.radians(rot))

    def pt(u, v):
        return [x + u * cos - v * sin, y + u * sin + v * cos]

    return [
        _line(f"{tag}a", pt(0, 0), pt(w, 0)),
        _line(f"{tag}b", pt(0, gap), pt(w, gap)),
        _line(f"{tag}c", pt(0, 0), pt(0, gap)),
        _line(f"{tag}d", pt(w, 0), pt(w, gap)),
    ]


def _snapshot():
    ents = []
    ents += _window(0, 0, tag="W1")
    ents += _window(4000, 0, tag="W2")                     # 平移
    ents += _window(8000, 0, rot=90.0, tag="W3")           # 旋转 90°
    ents += _window(0, 3000, gap=-60, tag="W4")            # 镜像
    # 柱：闭合轮廓压在墙线上
    ents.append({"handle": "C1", "layer": "COL", "type": "LWPOLYLINE",
                 "points": [[20000, 0], [20500, 0], [20500, 500], [20000, 500]],
                 "closed": True})
    ents.append({"handle": "C2", "layer": "COL", "type": "LWPOLYLINE",
                 "points": [[24000, 0], [24500, 0], [24500, 500], [24000, 500]],
                 "closed": True})
    # 家具：孤立的小轮廓（不贴墙、不成柱网）
    ents.append({"handle": "F1", "layer": "FURN", "type": "LWPOLYLINE",
                 "points": [[12000, 5000], [12300, 5000], [12300, 5200],
                            [12000, 5200]], "closed": True})
    return {"entities": ents, "derived": []}


def _ir():
    return IR.build(_snapshot(), scale={"thickness": 240, "noise": 2, "door_w": 900})


# --------------------------------------------------------------------------- 档位
def test_focused_profile_skips_room_nodes():
    prof = PROFILES.resolve("openings_columns")
    assert prof["rooms"] is False
    assert "assemble" not in prof["dag_targets"]
    assert "rooms" not in prof["dag_targets"]
    assert "space_routing" not in prof["dag_targets"]
    assert prof["resolver"] == "component_focus"
    assert PROFILES.categories(prof) == ("door", "window", "column", "wall",
                                         "grid_axis")


def test_full_profile_keeps_old_targets():
    prof = PROFILES.resolve("full")
    assert prof["dag_targets"] == ("assemble",)
    assert prof["rooms"] is True


# --------------------------------------------------------------------------- 拟合
def test_narrow_window_frame_is_fitted_with_local_scale():
    """窗框间距只有 60，图里墙厚 240：旧的"墙厚×带"会直接排除，新拟合器不该。"""
    ir = _ir()
    ents = [e for e in ir["entities"] if e["kind"] != "text"][:4]
    instance = {"id": "inst:test", "uids": [e["uid"] for e in ents],
                "bbox": [0, 0, 1200, 60], "kind": "lines"}
    reading = FITTERS.fit_instance("window", instance, entities=ents,
                                   walls=[], grid=None, basis=ir["basis"])
    assert reading is not None
    assert reading["geometry"]["frame_gap_mm"] == pytest.approx(60, abs=5)
    assert reading["geometry"]["width_mm"] == pytest.approx(1200, abs=5)


def test_column_needs_structural_context_and_is_kept_as_uncertain():
    ir = _ir()
    by_uid = IR.by_uid(ir)
    furn = [e for e in ir["entities"] if e.get("handle") == "F1"]
    reading = FITTERS.fit_instance("column", {"uids": [e["uid"] for e in furn]},
                                   entities=furn, walls=[], grid=None,
                                   basis=ir["basis"])
    assert reading is not None
    codes = [n["code"] for n in reading["negatives"]]
    assert "NO_STRUCTURAL_CONTEXT" in codes
    assert reading["status"] == "uncertain"


def test_column_with_wall_contact_has_structural_evidence():
    ir = _ir()
    col = [e for e in ir["entities"] if e.get("handle") == "C1"]
    walls = [{"id": "w1", "start": [19000, 0], "end": [26000, 0], "thickness": 240}]
    reading = FITTERS.fit_instance("column", {"uids": [e["uid"] for e in col]},
                                   entities=col, walls=walls, grid=None,
                                   basis=ir["basis"])
    assert reading is not None
    assert reading["evidence"]["touches_wall"] is True
    assert reading["status"] == "candidate"


# --------------------------------------------------------------------------- 族
def test_family_discovery_groups_the_same_window_drawn_four_ways():
    ir = _ir()
    found = FAM.discover(ir, graph=GG.Graph(GG.build(ir), ir))
    families = [f for f in found["families"] if f["category_guess"] == "window"]
    assert families, found["stats"]
    best = max(families, key=lambda f: f["n"])
    assert best["n"] >= 2, "平移/旋转/镜像的同画法窗应归到同一族"


def test_propagation_finds_rotated_and_mirrored_instances():
    ir = _ir()
    graph = GG.Graph(GG.build(ir), ir)
    found = FAM.discover(ir, graph=graph)
    families = [f for f in found["families"] if f["category_guess"] == "window"]
    best = max(families, key=lambda f: f["n"])
    result = PROP.propagate(best, ir=ir, graph=graph, category="window")
    assert result["stats"]["fitted"] >= 2
    assert result["hypotheses"][0]["source"] == "propagation"


# --------------------------------------------------------------------------- 求解
def _hyp(category, bbox, *, source="rule", score=0.8, evidence=None, geometry=None):
    return ENGINES.hypothesis(category=category, subtype=None, source=source,
                              engine="test", region=None, bbox=bbox,
                              geometry=geometry or {"axis": [[0, 0], [1, 1]],
                                                    "width_mm": 900},
                              uids=(), score=score, evidence=evidence or {})


def test_component_resolver_keeps_door_and_window_apart():
    door = _hyp("door", [0, 0, 900, 240], evidence={"host_ok": True})
    window = _hyp("window", [0, 0, 900, 240], evidence={"host_ok": True})
    res = CR.solve({"hypotheses": [door, window]}, thickness=240.0)
    cats = {h["category"] for h in res["selected"]}
    assert not {"door", "window"} <= cats


def test_component_resolver_prefers_structurally_supported_column():
    hosted = _hyp("column", [0, 0, 500, 500],
                  evidence={"structural": True, "touches_wall": True, "repeats": 3},
                  geometry={"outline": [[0, 0], [500, 0], [500, 500], [0, 500]],
                            "w_mm": 500, "h_mm": 500})
    lonely = _hyp("column", [9000, 9000, 9300, 9200],
                  evidence={"structural": False},
                  geometry={"outline": [[9000, 9000], [9300, 9000], [9300, 9200],
                                        [9000, 9200]],
                            "w_mm": 300, "h_mm": 200})
    assert CR.score(hosted) > CR.score(lonely)


def test_focused_staged_resolution_reports_no_rooms():
    prof = PROFILES.resolve("openings_columns")
    pool = {"hypotheses": [
        _hyp("wall", [0, 0, 4000, 240], geometry={"axis": [[0, 0], [4000, 0]],
                                                  "thickness_mm": 240}),
        _hyp("window", [1000, 0, 2200, 240], evidence={"host_ok": True}),
        _hyp("column", [0, 0, 500, 500], evidence={"structural": True, "category_confirmed": True,
                      "category_basis": "catalog_block"},
             geometry={"outline": [[0, 0], [500, 0], [500, 500]], "w_mm": 500,
                       "h_mm": 500})]}
    res = CR.staged(pool, thickness=240.0, profile=prof)
    assert "room" not in (res["final"]["counts"] or {})
    assert res["final"]["counts"].get("column") == 1
    assert res["stages"]["context"]["status"] == "ok"


# --------------------------------------------------- 模型确认是传播的前置条件
def test_unconfirmed_family_never_propagates():
    """模型没看过 / 没把握的族，一律不许推广（这是上一轮 26 个假窗的直接原因）。"""
    ir = _ir()
    families = FAM.discover(ir, graph=GG.Graph(GG.build(ir), ir))["families"]
    assert families
    pool = ENGINES.propagation_candidates(ir, None, families=families[:3])
    assert pool["candidates"] == []
    assert all(row["status"] == "unconfirmed" for row in pool["trace"])

    families[0]["confirmed"] = True
    families[0]["category"] = "window"
    pool2 = ENGINES.propagation_candidates(ir, None, families=families[:3])
    assert pool2["trace"][0]["status"] != "unconfirmed"


def test_model_wall_verdict_blocks_propagation():
    ir = _ir()
    families = FAM.discover(ir, graph=GG.Graph(GG.build(ir), ir))["families"]
    families[0].update({"confirmed": True, "verdict": "reject", "category": "window"})
    pool = ENGINES.propagation_candidates(ir, None, families=families[:1])
    assert pool["candidates"] == []
    assert pool["trace"][0]["status"] == "model_rejected"


def test_thin_line_pair_is_rejected_by_depth_scale():
    """深度 50 mm 的平行线对（图内墙厚 240）不是窗框：必须留下反面证据。"""
    ir = _ir()
    ents = [_line("TH1", [0, 0], [2345, 0]), _line("TH2", [0, 50], [2345, 50])]
    snap = {"entities": ents, "derived": []}
    ir2 = IR.build(snap, scale={"thickness": 240, "noise": 2, "door_w": 900})
    es = [e for e in ir2["entities"] if e["kind"] != "text"]
    reading = FITTERS.fit_instance("window", {"uids": [e["uid"] for e in es],
                                              "bbox": es[0]["bbox"]},
                                   entities=es, walls=[], grid=None,
                                   basis=ir2["basis"])
    if reading is None:
        pytest.skip("退化输入")
    assert "DEPTH_NOT_WALL_SCALE" in [n["code"] for n in reading["negatives"]]


def test_detector_prompt_shows_wall_option_and_no_self_guess():
    """词表必须有 wall（否则"这是墙"无法表达），且不许把我猜的类别写进提示词。"""
    from dwg_reader.recognition.v2 import multimodal_detector as DET
    assert "wall" in DET.CONTEXT_CATEGORIES
    assert "wall" in DET.NEGATIVES
    ir = _ir()
    family = FAM.discover(ir, graph=GG.Graph(GG.build(ir), ir))["families"][0]
    facts = FAM.family_facts(family, ir, walls=[], widths=[])
    assert "geometry_guess" not in facts
    assert "wall_relation" in facts


# ----------------------------------------------------------- 冲突裁决（按位置）
def _conflict_pool():
    door = _hyp("door", [0, 0, 900, 240], evidence={"host_ok": True},
                geometry={"axis": [[0, 0], [900, 0]], "width_mm": 900,
                          "outline": [[0, 0], [900, 0], [900, 240], [0, 240]]})
    window = _hyp("window", [0, 0, 900, 240], evidence={"host_ok": True},
                  geometry={"axis": [[0, 0], [900, 0]], "width_mm": 900,
                            "frame_gap_mm": 60})
    far = _hyp("column", [9000, 9000, 9500, 9500], evidence={"structural": True},
               geometry={"outline": [[9000, 9000], [9500, 9000], [9500, 9500]],
                         "w_mm": 500, "h_mm": 500})
    return [door, window, far]


def test_conflict_points_group_only_real_conflicts():
    from dwg_reader.recognition.v2 import conflict_arbiter as ARB
    rows = _conflict_pool()
    found = ARB.conflict_points(rows, thickness=240.0)
    assert found["points"], found["stats"]
    point = found["points"][0]
    cats = {h["category"] for h in point["candidates"]}
    assert cats == {"door", "window"}          # 远处的柱不参与
    assert "MUTUALLY_EXCLUSIVE" in point["reasons"]


def test_arbiter_apply_maps_labels_to_candidate_ids():
    from dwg_reader.recognition.v2 import conflict_arbiter as ARB
    rows = _conflict_pool()
    point = {"id": "pt:test", "candidates": rows[:2], "reasons": ["MUTUALLY_EXCLUSIVE"]}
    strict = ARB.apply(point, {"per_candidate": {
        "A": {"keep": True, "why": "on wall", "category": "door"},
        "B": {"keep": False, "why": "duplicate", "category": None}}})
    assert strict["keep"] == [rows[0]["id"]] and strict["drop"] == [rows[1]["id"]]
    assert strict["categories"] == {rows[0]["id"]: "door"}
    # 旧式单值：只作为"建议"生效，且绝不连累第三个候选
    legacy = ARB.apply({"id": "pt:x", "candidates": rows[:3]},
                       {"per_candidate": {"A": {"keep": True}}})
    assert legacy["drop"] == []
    assert rows[2]["id"] in legacy["keep"]
    none = ARB.apply(point, {"per_candidate": {"A": {"keep": False},
                                               "B": {"keep": False}}})
    assert len(none["drop"]) == 2
    both = ARB.apply(point, {"per_candidate": {"A": {"keep": True},
                                               "B": {"keep": True}}})
    assert both["drop"] == []


def test_arbitration_without_model_is_a_no_op():
    from dwg_reader.recognition.v2 import conflict_arbiter as ARB
    ir = _ir()
    out = ARB.run(_conflict_pool(), ir=ir, gateway=None)
    assert out["status"] == "ok"
    assert out["dropped"] == []
    assert all((v["result"] or {}).get("status") == "no_model"
               for v in out["verdicts"])


def test_position_facts_are_relation_based_not_layer_based():
    from dwg_reader.recognition.v2 import conflict_arbiter as ARB
    ir = _ir()
    walls = [{"id": "w1", "start": [0, 120], "end": [2000, 120], "thickness": 240}]
    facts = ARB.position_of(_conflict_pool()[0], ir=ir, walls=walls,
                            grid={"axes": [{"axis": [[0, 0], [0, 5000]],
                                            "label": "A"}],
                                  "intersections": [{"point": [0, 120]}]})
    for key in ("distance_to_wall", "near_wall_band", "on_wall_axis",
                "at_grid_intersection"):
        assert key in facts and facts["available"] is True
    blob = str(facts)
    assert "家具" not in blob and "WALL" not in blob   # 图层名不得当判据


def test_missing_wall_is_reported_as_unknown_not_zero_distance():
    """没给墙时必须写 null+原因：写 0 会让模型以为构件就在墙上。"""
    from dwg_reader.recognition.v2 import conflict_arbiter as ARB
    facts = ARB.position_of(_conflict_pool()[0], ir=_ir(), walls=(), grid={})
    assert facts["distance_to_wall"] is None
    assert facts["near_wall_band"] is None
    assert facts["on_wall_axis"] is None
    assert facts.get("wall_reason")


def test_per_candidate_position_facts_are_given_for_each_candidate():
    from dwg_reader.recognition.v2 import conflict_arbiter as ARB
    ir = _ir()
    walls = [{"id": "w1", "start": [0, 100], "end": [2000, 100], "thickness": 200}]
    rows = _conflict_pool()
    facts = ARB.candidate_facts(rows, ir=ir, walls=walls, grid={})
    assert all("position" in f for f in facts)
    assert facts[0]["position"]["available"] is True


# ------------------------------------------------ 几何/口径：回归（这一轮修改）
def test_wall_with_only_endpoints_gets_a_real_box():
    """只有 start/end 的墙也要能算出盒子（以前返回 None，让无位置候选混进计数）。"""
    from dwg_reader.recognition.v2 import proposal_engines as E
    box, source = E._bbox_of_record({"start": [0, 0], "end": [3000, 0]},
                                    thickness=200.0)
    assert source == "axis_plus_thickness"
    assert abs((box[3] - box[1]) - 200.0) < 1e-6


def test_degenerate_record_is_unresolved_not_pooled():
    from dwg_reader.recognition.v2 import proposal_engines as E
    pool = E.rule_candidates({"basis": {"thickness": 200.0}, "entities": []},
                             walls=[{"start": [0, 0], "end": [0, 0]},
                                    {"start": [0, 0], "end": [3000, 0],
                                     "thickness": 200}])
    boxes = [h.get("bbox") for h in pool["candidates"]]
    assert all(b and len(b) == 4 and b[2] > b[0] and b[3] > b[1] for b in boxes)
    assert pool["stats"]["unresolved"] >= 1
    assert any(u.get("why") == "DEGENERATE_BBOX" for u in pool["unresolved"])


def test_pending_candidates_are_not_counted():
    from dwg_reader.recognition.v2 import component_resolver as CR
    ok = _hyp("column", [0, 0, 600, 600], geometry={"outline": [[0, 0], [600, 0],
                                                                [600, 600]],
                                                    "w_mm": 600, "h_mm": 600},
              score=0.9)
    ok["evidence"].update(category_confirmed=True, category_basis="catalog_block")
    pending = _hyp("column", [5000, 5000, 5600, 5600],
                   geometry={"outline": [[5000, 5000], [5600, 5000]]},
                   score=0.9)
    pending["status"] = "pending"
    res = CR.solve({"hypotheses": [ok, pending]}, thickness=200.0)
    assert res["counts"].get("column") == 1
    assert any(r.get("why") == "GEOMETRY_UNVERIFIED" for r in res["rejected"])


def test_solver_honours_arbitration_constraints():
    from dwg_reader.recognition.v2 import component_resolver as CR
    door = _hyp("door", [0, 0, 900, 200], geometry={"axis": [[0, 0], [900, 0]],
                                                    "width_mm": 900, "outline":
                                                    [[0, 0], [900, 0], [900, 200]]})
    win = _hyp("window", [3000, 0, 3900, 200], geometry={"axis": [[3000, 0],
                                                                  [3900, 0]],
                                                         "width_mm": 900,
                                                         "frame_gap_mm": 60})
    rows = [door, win]
    dropped = CR.solve({"hypotheses": rows}, thickness=200.0,
                       constraints={"drop": {door["id"]}})
    assert dropped["counts"] == {"window": 1}
    forced = CR.solve({"hypotheses": rows}, thickness=200.0,
                      constraints={"keep": {win["id"]}})
    assert win["id"] in forced["selected_ids"]


def test_conflict_points_ignore_adjacency_and_use_footprints():
    from dwg_reader.recognition.v2 import conflict_arbiter as ARB
    door = _hyp("door", [0, 0, 900, 200], geometry={"outline": [[0, 0], [900, 0],
                                                                [900, 200]]})
    column = _hyp("column", [900, 0, 1400, 500],
                  geometry={"outline": [[900, 0], [1400, 0], [1400, 500]],
                            "w_mm": 500, "h_mm": 500})
    # 真实轮廓相接但不重叠 -> 不该成为裁决点
    assert ARB.conflict_points([door, column], thickness=200.0)["points"] == []


def test_family_discovery_does_not_let_one_kind_take_all_slots():
    from dwg_reader.recognition.v2 import component_family as FAM
    from dwg_reader.recognition.v2 import geometry_fitter as FIT
    from dwg_reader.recognition.v2.fitters import _common as SH

    def inst(uid_box, kind):
        uids = ["u%d" % uid_box[0]]
        return FAM._instance(uids, uid_box, kind=kind)

    calls = {"n": 0}
    original = FAM._group

    def fake_group(ir, instances, *, key_of=None, physical=False, walls=()):
        calls["n"] += 1
        out = []
        for i, row in enumerate(instances):
            out.append({"key": None, "signature": {"kinds": {}, "aspect": 1.0},
                        "instances": [dict(row, signature={"kinds": {},
                                                           "aspect": 1.0})],
                        "_dist": [0.0]})
        return out

    monkey = {"pair": 60, "block": 6, "contour": 3}
    FAM._group = fake_group
    FAM.block_instances = lambda ir: [inst([i, 0, i + 10, 10], "block")
                                      for i in range(6)]
    FAM.pair_instances = lambda ir: [inst([i, 0, i + 10, 10], "lines")
                                     for i in range(60)]
    FAM.contour_instances = lambda ir: [inst([i, 0, i + 10, 10], "contour")
                                       for i in range(3)]
    FAM.arc_instances = lambda ir: []
    try:
        res = FAM.discover({"entities": [], "basis": {"thickness": 200.0}},
                           kinds=("pair", "block", "contour"), rules={})
    finally:
        FAM._group = original
    kinds = {f["kind"] for f in res["families"]}
    assert {"pair", "block"} <= kinds, kinds   # 块画法不能被线对挤掉


def test_layer_name_does_not_gate_column_visibility_but_requires_context():
    from dwg_reader.recognition import elements as E
    rec = {"outline": [[0, 0], [600, 0], [600, 600], [0, 600]],
           "bbox": [0, 0, 600, 600], "w": 600, "h": 600, "center": [300, 300],
           "methods": ["contour"], "method": "contour", "layer": "家具",
           "layer_scope": "outside_declared", "srcs": [{"handle": "A1",
                                                        "type": "CONTOUR",
                                                        "layer": "家具"}]}
    fused = E.fuse_columns([dict(rec)], walls=(), axis=((), ()),
                           scale=None)["columns"][0]
    # 无结构佐证的孤立矩形在任何层都待核，不按层名断言它是家具或柱。
    assert fused["status"] == "pending"
    assert fused["pending_reason"] == "CATEGORY_UNVERIFIED"
    declared = E.fuse_columns([dict(rec, layer_scope="declared")], walls=(),
                              axis=((), ()), scale=None)["columns"][0]
    assert (declared["status"], declared["score"]) == (fused["status"], fused["score"])

    # 几何和轴网支持不代替类别确认，仍等待模型复查。
    on_axis = E.fuse_columns([dict(rec)], walls=(),
                             axis=([300.0], [300.0]), scale=None)["columns"][0]
    assert on_axis["status"] == "pending"
    assert on_axis["pending_reason"] == "CATEGORY_UNVERIFIED"

    # 块参照（图库里定义好的柱块）不受层名限制：这是"图层名不决定可见性"的那一半
    block = dict(rec, methods=["block"], method="block", layer_scope="outside_declared",
                 category_confirmed=True, category_basis="catalog_block")
    fused_block = E.fuse_columns([block], walls=(), axis=((), ()),
                                 scale=None)["columns"][0]
    assert fused_block["status"] in ("confirmed", "uncertain")
