"""族布局证据（全部成员）、分布总览图与完整待定清单。

对应实测问题（2026-11-15）：两个单块实心填充的柱族都只读到 medium，模型的备选是
"墙体填充"和"填充标注"；而提示词里只有代表实例的局部量，看不到成员在平面上的
分布，也看不到墙线在方块边界上是断开还是穿过。待定 350 条只给 12 条无 bbox 样本，
无法按位置核对。
"""
import base64
import json

import pytest

from dwg_reader.recognition import vision as VISION
from dwg_reader.recognition.model_gateway import GatewayResult
from dwg_reader.recognition import v2
from dwg_reader.recognition.v2 import cad_ir as IR
from dwg_reader.recognition.v2 import component_family as FAM
from dwg_reader.recognition.v2 import family_layout as LAYOUT
from dwg_reader.recognition.v2 import multimodal_detector as DET
from dwg_reader.recognition.v2 import profiles as PROFILES


def _hatch(handle, x, y, size, solid=True):
    return {"handle": handle, "type": "HATCH", "layer": "0", "solid": solid,
            "loops": [[[x, y], [x + size, y], [x + size, y + size], [x, y + size]]]}


def _line(handle, a, b):
    return {"type": "LINE", "handle": handle, "layer": "0",
            "start": list(a), "end": list(b)}


def _scene(positions, *, size=600, extra=(), solid=True):
    """每个位置一个实心方块填充成员，返回 ``(snapshot, ir, family)``。"""
    entities = [_hatch("H%d" % i, x, y, size, solid=solid)
                for i, (x, y) in enumerate(positions)]
    entities.extend(extra)
    snapshot = {"entities": entities, "derived": []}
    ir = IR.build(snapshot, scale={"thickness": 200, "noise": 2})
    uid = ir["index"]["handle_to_uid"]
    instances = [{"id": "inst:%d" % i, "uids": list(uid["H%d" % i]),
                  "handle": "H%d" % i,
                  "bbox": [x, y, x + size, y + size]}
                 for i, (x, y) in enumerate(positions)]
    boxes = [inst["bbox"] for inst in instances]
    family = {"id": "fam:layout", "kind": "contour", "n": len(instances),
              "instances": instances, "representative": dict(instances[0]),
              "bbox": [min(b[0] for b in boxes), min(b[1] for b in boxes),
                       max(b[2] for b in boxes), max(b[3] for b in boxes)],
              "category_guess": "column", "authorization_scope": "family",
              "explained_ratio": 0.0, "evidence": {}}
    return snapshot, ir, family


GRID = [(x * 6000, y * 6000) for x in range(3) for y in range(3)]


# ----------------------------------------------------------------- member_layout
def test_grid_members_are_aligned_with_repeated_spacing():
    _, ir, family = _scene(GRID)
    layout = LAYOUT.member_layout(family, ir)
    assert layout["available"] is True
    assert layout["members"] == 9
    assert layout["aligned_fraction"] == 1.0
    assert layout["grid_fraction"] == 1.0
    assert layout["lines"] == {"vertical": 3, "horizontal": 3, "largest": 3}
    assert layout["spacing"]["repeat_fraction"] == 1.0
    assert layout["spacing"]["distinct"] == 1
    assert layout["spacing"]["median_over_size"] == 10.0
    assert layout["coincident_pairs"] == 0


def test_scattered_members_are_not_reported_as_rows_or_columns():
    scattered = [(0, 0), (2300, 7100), (5900, 1700), (9100, 12500), (13300, 4100)]
    _, ir, family = _scene(scattered)
    layout = LAYOUT.member_layout(family, ir)
    assert layout["aligned_fraction"] == 0.0
    assert layout["grid_fraction"] == 0.0
    assert layout["spacing"] == {"gaps": 0}


def test_duplicate_members_at_one_place_are_counted():
    _, ir, family = _scene([(0, 0), (10, 0), (6000, 0), (12000, 0)])
    assert LAYOUT.member_layout(family, ir)["coincident_pairs"] == 1


# ------------------------------------------------------------- wall_interaction
ROW = [(0, 0), (6000, 0), (12000, 0)]


def _interrupted_faces():
    """墙面线 y=200/400 在每个方块的左右边上终止（柱打断墙）。"""
    lines, n = [], 0
    stops = [(-3000, 0), (600, 6000), (6600, 12000), (12600, 15600)]
    for x0, x1 in stops:
        for y in (200, 400):
            lines.append(_line("W%d" % n, (x0, y), (x1, y)))
            n += 1
    return lines


def test_wall_faces_ending_on_both_sides_mean_interrupted():
    walls = [{"start": [x0, 300], "end": [x1, 300], "thickness": 200}
             for x0, x1 in [(-3000, 0), (600, 6000), (6600, 12000), (12600, 15600)]]
    _, ir, family = _scene(ROW, extra=_interrupted_faces())
    result = LAYOUT.wall_interaction(family, ir, walls)
    fractions = result["fractions"]
    assert fractions["interrupted_opposite"] == 1.0
    assert fractions["through"] == 0.0
    assert fractions["isolated"] == 0.0
    assert result["wall_axes"] == {"ends_at": 1.0, "through": 0.0, "none": 0.0}


def test_wall_faces_running_through_members_mean_overlay():
    faces = [_line("T%d" % y, (-3000, y), (15600, y)) for y in (200, 400)]
    walls = [{"start": [-3000, 300], "end": [15600, 300], "thickness": 200}]
    _, ir, family = _scene(ROW, extra=faces)
    result = LAYOUT.wall_interaction(family, ir, walls)
    assert result["fractions"]["through"] == 1.0
    assert result["fractions"]["interrupted_opposite"] == 0.0
    assert result["wall_axes"]["through"] == 1.0


def test_line_along_member_edge_is_flush_not_through():
    _, ir, family = _scene(ROW, extra=[_line("F", (-3000, 0), (15600, 0))])
    fractions = LAYOUT.wall_interaction(family, ir)["fractions"]
    assert fractions["flush"] == 1.0
    assert fractions["through"] == 0.0


def test_members_without_any_meeting_line_are_isolated():
    far = [_line("X", (0, 5000), (15600, 5000))]
    _, ir, family = _scene(ROW, extra=far)
    result = LAYOUT.wall_interaction(family, ir)
    assert result["fractions"]["isolated"] == 1.0
    assert result["wall_axes"] == {"available": False}


def test_rotated_members_use_their_own_frame():
    """旋转 30° 的方块：墙面线沿它自己的边方向终止，仍判为被打断。"""
    import math
    ang = math.radians(30.0)
    ux, uy = math.cos(ang), math.sin(ang)
    vx, vy = -uy, ux

    def at(u, v, cx, cy):
        return [cx + u * ux + v * vx, cy + u * uy + v * vy]

    entities, lines = [], []
    for i, cx in enumerate((0.0, 8000.0, 16000.0)):
        loop = [at(du, dv, cx, 0.0) for du, dv in
                ((-300, -300), (300, -300), (300, 300), (-300, 300))]
        entities.append({"handle": "R%d" % i, "type": "HATCH", "layer": "0",
                         "solid": True, "loops": [loop]})
        for side, (u0, u1) in enumerate(((-3000, -300), (300, 3000))):
            for v in (-100, 100):
                lines.append(_line("RL%d%d%d" % (i, side, v),
                                   at(u0, v, cx, 0.0), at(u1, v, cx, 0.0)))
    snapshot = {"entities": entities + lines, "derived": []}
    ir = IR.build(snapshot, scale={"thickness": 200, "noise": 2})
    uid = ir["index"]["handle_to_uid"]
    instances = []
    for i in range(3):
        rec = next(e for e in ir["entities"] if e["handle"] == "R%d" % i)
        instances.append({"id": "inst:%d" % i, "uids": list(uid["R%d" % i]),
                          "bbox": rec["bbox"]})
    family = {"id": "fam:rot", "n": 3, "instances": instances}
    fractions = LAYOUT.wall_interaction(family, ir)["fractions"]
    assert fractions["interrupted_opposite"] == 1.0
    assert fractions["through"] == 0.0


# ------------------------------------------------------------------- fill_types
def test_fill_types_split_solid_and_pattern():
    _, ir, family = _scene(ROW, solid=False)
    assert LAYOUT.fill_types(family, ir) == {"solid": 0, "pattern": 3, "no_hatch": 0}
    _, ir, family = _scene(ROW)
    assert LAYOUT.fill_types(family, ir) == {"solid": 3, "pattern": 0, "no_hatch": 0}


def test_family_facts_adds_layout_only_on_request():
    _, ir, family = _scene(GRID)
    plain = FAM.family_facts(family, ir)
    assert "member_layout" not in plain and "wall_interaction" not in plain
    full = FAM.family_facts(family, ir, layout=True)
    assert full["member_layout"]["grid_fraction"] == 1.0
    assert full["fill_types"]["solid"] == 9
    assert full["wall_interaction"]["available"] is True


# ------------------------------------------------------- detector：事实与总览图
class _Gateway:
    available = True

    def __init__(self):
        self.requests = []
        self.calls = 0

    def ask(self, **request):
        self.requests.append(request)
        self.calls += 1
        data = {"component": {"category": "column", "subtype": "filled_column",
                              "confidence": "medium"}, "family_consistent": True}
        return GatewayResult(ok=True, data=data, text=json.dumps(data),
                             images=len(request["images"]))


def _facts_from(prompt):
    block = prompt.split("Family facts (computed from CAD entities, ids are stable):\n")[1]
    return json.loads(block.split("\n\nInstances in this family:")[0])


@pytest.mark.skipif(not VISION.health()["ok"], reason="needs Pillow")
def test_overview_is_first_image_and_layout_facts_reach_the_prompt():
    snapshot, ir, family = _scene(GRID, extra=_interrupted_faces())
    gateway = _Gateway()
    result = DET.read_family(family, ir=ir, snapshot=snapshot, gateway=gateway,
                             budget={"overview": True})
    request = gateway.requests[0]
    assert len(gateway.requests) == 1                      # 同一次调用
    meta = result["request"]["images"]
    assert meta[0]["role"] == "family_overview"
    assert meta[0]["instance"] is None and meta[0]["members"] == 9
    assert meta[0]["focus"] >= 9                           # 全部成员都高亮
    assert [m["image"] for m in meta] == list(range(1, len(meta) + 1))
    assert len(request["images"]) == len(meta) == 1 + DET.BUDGET["images_per_family"]
    assert result["overview"] is True
    facts = _facts_from(request["prompt"])                 # 未被截断，仍是合法 JSON
    assert facts["member_layout"]["grid_fraction"] == 1.0
    assert facts["fill_types"] == {"solid": 9, "pattern": 0, "no_hatch": 0}
    assert "interrupted_opposite" in facts["wall_interaction"]["fractions"]
    assert "family_overview" in request["prompt"]
    assert DET.PROMPT_VERSION == "component-detector/2.1"


@pytest.mark.skipif(not VISION.health()["ok"], reason="needs Pillow")
def test_overview_is_off_by_default_and_never_sent_without_highlight(monkeypatch):
    snapshot, ir, family = _scene(GRID)
    gateway = _Gateway()
    result = DET.read_family(family, ir=ir, snapshot=snapshot, gateway=gateway)
    assert all(m["role"] != "family_overview" for m in result["request"]["images"])
    assert result["overview"] is False
    original = DET._render

    def no_focus(*args, **kwargs):
        out = original(*args, **kwargs)
        if kwargs.get("scales") == ("overview",):
            for view in out.get("views") or ():
                view["focus"] = 0
        return out

    monkeypatch.setattr(DET, "_render", no_focus)
    result = DET.read_family(family, ir=ir, snapshot=snapshot, gateway=_Gateway(),
                             budget={"overview": True})
    assert result["overview"] is False
    assert [m["image"] for m in result["request"]["images"]][0] == 1


def test_large_family_facts_fit_the_prompt_budget():
    positions = [(x * 6000, y * 6000) for x in range(12) for y in range(10)]
    _, ir, family = _scene(positions)
    facts = FAM.family_facts(family, ir, layout=True)
    assert len(DET._dumps(facts)) < DET.BUDGET["prompt_facts"]


def test_profiles_turn_overview_on_for_focused_profiles_only():
    assert PROFILES.resolve("openings_columns")["family_overview"] is True
    assert PROFILES.resolve("openings_columns_lean")["family_overview"] is True
    assert not PROFILES.resolve("full").get("family_overview")
    assert PROFILES.summary("openings_columns")["observation"]["family_overview"] is True


# --------------------------------------------------------------- 完整待定清单
def _pending_scene():
    def hyp(hid, uids, bbox, family_id=None):
        row = {"id": hid, "category": "column", "status": "pending", "uids": uids,
               "bbox": bbox, "score": 0.6, "engine": "rule.columns",
               "source_handles": ["H" + hid[-1]],
               "evidence": {"negatives": [{"code": "CATEGORY_UNVERIFIED"}],
                            "layers": ["ANY"]}}
        if family_id:
            row["family_id"] = family_id
        return row

    pool = {"hypotheses": [hyp("hyp:a", ["e:1"], [0, 0, 600, 600]),
                           hyp("hyp:b", ["e:2"], [6000, 0, 6600, 600]),
                           hyp("hyp:c", ["e:3"], [0, 9000, 600, 9600]),
                           hyp("hyp:d", ["e:9"], [50, 50, 90, 90]),
                           hyp("hyp:e", ["e:7"], [1, 1, 2, 2], family_id="fam:fail")],
            "unresolved": []}
    resolution = {"rejected": [{"id": h["id"], "category": "column",
                                "why": "CATEGORY_UNVERIFIED"}
                               for h in pool["hypotheses"]]}
    families = [{"id": "fam:read", "instances": [
                    {"id": "inst:1", "uids": ["e:1"]}, {"id": "inst:2", "uids": ["e:2"]}]},
                {"id": "fam:unread", "instances": [{"id": "inst:3", "uids": ["e:3"]}]},
                {"id": "fam:fail", "instances": []}]
    readings = [{"family": "fam:read", "status": "ok", "category": "column",
                 "confidence": "medium", "scope": "family"},
                {"family": "fam:fail", "status": "error"}]
    return resolution, pool, families, readings


def test_pending_report_lists_every_item_with_bbox_and_family_group():
    resolution, pool, families, readings = _pending_scene()
    report = v2._pending_report(resolution, pool, families=families, readings=readings)
    assert report["total"] == 5 and report["counts"] == {"column": 5}
    items = {row["id"]: row for row in report["items"]}
    assert len(items) == 5 and report["truncated"] is False
    assert items["hyp:a"]["bbox"] == [0, 0, 600, 600]
    assert (items["hyp:a"]["family"], items["hyp:a"]["instance"]) == ("fam:read", "inst:1")
    assert items["hyp:a"]["group"] == "observed_family"
    assert items["hyp:c"]["group"] == "unobserved_family"
    assert items["hyp:d"]["group"] == "orphan" and items["hyp:d"]["family"] is None
    assert items["hyp:e"]["group"] == "observed_failed"
    assert report["groups"] == {"observed_family": {"column": 2},
                                "unobserved_family": {"column": 1},
                                "orphan": {"column": 1},
                                "observed_failed": {"column": 1}}
    top = report["by_family"][0]
    assert top["family"] == "fam:read" and top["count"] == 2
    assert top["reading"]["confidence"] == "medium"


def test_pending_report_without_families_keeps_legacy_fields():
    resolution, pool, _, _ = _pending_scene()
    report = v2._pending_report(resolution, pool)
    assert len(report["sample"]) == 5
    groups = {row["id"]: row["group"] for row in report["items"]}
    # 没给族表：只有自带 family_id 的候选能归族（且未被读过），其余都是孤立候选
    assert groups == {"hyp:a": "orphan", "hyp:b": "orphan", "hyp:c": "orphan",
                      "hyp:d": "orphan", "hyp:e": "unobserved_family"}
    assert all(row["bbox"] for row in report["items"])


def test_block_members_use_visible_children_not_the_insert_point():
    """块族实例的 bbox 只是插入点：实测"间距/尺寸"因此变成 1e12 进了提示词。"""
    _, ir, family = _scene(GRID)
    for inst in family["instances"]:
        x, y = inst["bbox"][0], inst["bbox"][1]
        inst["bbox"] = [x, y, x, y]                       # 零面积插入点
    layout = LAYOUT.member_layout(family, ir)
    assert layout["members"] == 9
    assert layout["spacing"]["median_over_size"] == 10.0
    assert layout["nearest_over_size"]["median"] == 10.0
    # 可见图元也没有、实例框又是零面积的成员不参与统计
    family["instances"].append({"id": "inst:ghost", "uids": [], "bbox": [1, 1, 1, 1]})
    assert LAYOUT.member_layout(family, ir)["members"] == 9
    assert LAYOUT.wall_interaction(family, ir)["members"] == 9
