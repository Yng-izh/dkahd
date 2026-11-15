"""族观察输入契约：目标要标出来、样本要有差异、授权范围要说清、请求要留档。

这些测试对应四个已复现的问题：

1. 送模截图没传目标 handle → ``focus`` 全是 0，模型只能对着一块区域猜；
2. 每族只看一个实例的两张缩放图 → 模型看不出族内差异，却要回答整族；
3. 观察循环固定 4 个族就结束 → 柱族被否掉后，剩下 9 个柱候选族和 3 次调用一起浪费；
4. 请求不留档 → 跑完只剩一个类别字符串，无法复盘模型看到了什么。
"""
import json
from types import SimpleNamespace

import pytest

from dwg_reader.recognition import vision as VISION
from dwg_reader.recognition.v2 import agent_controller as AG
from dwg_reader.recognition.v2 import cad_ir as IR
from dwg_reader.recognition.v2 import component_family as FAM
from dwg_reader.recognition.v2 import multimodal_detector as DET


def _line(handle, start, end, layer="0"):
    return {"type": "LINE", "handle": handle, "layer": layer,
            "start": list(start), "end": list(end)}


def _rect(handle, x, y, size, layer="0"):
    pts = [[x, y], [x + size, y], [x + size, y + size], [x, y + size]]
    return {"type": "LWPOLYLINE", "handle": handle, "layer": layer,
            "points": pts, "closed": True}


def _square_snapshot(count=2, size=600, layer="0"):
    entities = []
    for i in range(count):
        x = i * 4000
        entities.extend([
            _line("L%d0" % i, (x, 0), (x + size, 0), layer),
            _line("L%d1" % i, (x + size, 0), (x + size, size), layer),
            _line("L%d2" % i, (x + size, size), (x, size), layer),
            _line("L%d3" % i, (x, size), (x, 0), layer),
        ])
    return {"entities": entities, "derived": []}


# --------------------------------------------------------------- 1) 目标必须标出来
def test_render_views_marks_target_handles_and_numbers():
    snapshot = _square_snapshot(count=1)
    target = snapshot["entities"][0]["handle"]
    views = VISION.render_views(snapshot, {"bbox": [-100, -100, 700, 700]},
                                scales=("local",), thickness=200.0,
                                focus_handles=[target], numbering=True)
    rendered = [v for v in views["views"] if v["status"] == "rendered"]
    assert rendered, views
    assert rendered[0]["focus"] >= 1
    assert rendered[0]["numbers"] >= 1
    assert rendered[0]["focus_box"] is False


def test_render_views_falls_back_to_target_box_when_handles_miss():
    """handle 对不上（块展开、聚合实例）时也必须标出"看的是哪一处"。"""
    snapshot = _square_snapshot(count=1)
    views = VISION.render_views(snapshot, {"bbox": [-100, -100, 700, 700]},
                                scales=("local",), thickness=200.0,
                                focus_handles=["NOT-A-HANDLE"], numbering=True)
    rendered = [v for v in views["views"] if v["status"] == "rendered"]
    assert rendered
    assert rendered[0]["focus"] == 0
    assert rendered[0]["focus_box"] is True


# --------------------------------------------------------------- 2) 样本要有差异
def _synthetic_family(sizes=(500, 500, 500, 900, 900, 400), category="column"):
    instances = []
    for index, size in enumerate(sizes):
        x = index * 4000
        instances.append({"id": "inst:%d" % index, "uids": ["e:%d" % index],
                          "bbox": [x, 0, x + size, size],
                          "transform": {"rotation": 0.0, "mirror": False}})
    rep = instances[0]
    return {"id": "fam:test", "kind": "contour", "category_guess": category,
            "priority": 1.0, "n": len(instances),
            "authorization_scope": "family",
            "representative": {"id": rep["id"], "uids": rep["uids"],
                               "bbox": rep["bbox"]},
            "instances": instances, "bbox": [0, 0, 24000, 900],
            "explained_ratio": 0.0, "evidence": {}}


def _family_scene(families):
    """Give each family member actual geometry, handles, and canonical IR UIDs."""
    entities = []
    for family_index, family in enumerate(families):
        for index, instance in enumerate(family["instances"]):
            x0, y0, x1, y1 = instance["bbox"]
            y_offset = family_index * 4000
            instance["bbox"] = [x0, y0 + y_offset, x1, y1 + y_offset]
            handle = "F%dI%d" % (family_index, index)
            instance["handle"] = handle
            entities.append(_rect(handle, x0, y0 + y_offset, x1 - x0))
        family["representative"] = dict(family["instances"][0])
        boxes = [instance["bbox"] for instance in family["instances"]]
        family["bbox"] = [min(b[0] for b in boxes), min(b[1] for b in boxes),
                          max(b[2] for b in boxes), max(b[3] for b in boxes)]
    snapshot = {"entities": entities, "derived": []}
    ir = IR.build(snapshot, scale={"thickness": 200, "noise": 2})
    for family in families:
        for instance in family["instances"]:
            instance["uids"] = ir["index"]["handle_to_uid"][instance["handle"]]
        family["representative"] = dict(family["instances"][0])
    return snapshot, ir


def test_samples_add_diverse_members_not_just_the_representative():
    family = _synthetic_family()
    samples = DET.sample_instances(family, {}, limit=3)
    assert samples[0]["id"] == family["representative"]["id"]
    ids = [s["id"] for s in samples]
    assert len(set(ids)) == 3
    sizes = {round(s["bbox"][2] - s["bbox"][0]) for s in samples}
    assert len(sizes) > 1, "样本必须包含不同尺寸的成员"


def test_sample_views_render_one_image_per_sample_with_its_own_target():
    snapshot = _square_snapshot(count=2)
    ir = IR.build(snapshot, scale={"thickness": 200, "noise": 2})
    family = {"id": "fam:x", "kind": "bundle", "category_guess": "window",
              "authorization_scope": "family",
              "representative": {"id": "inst:a", "uids": [], "bbox": [0, 0, 600, 600]},
              "instances": [
                  {"id": "inst:a", "uids": [], "bbox": [0, 0, 600, 600]},
                  {"id": "inst:b", "uids": [], "bbox": [4000, 0, 4600, 600]}],
              "bbox": [0, 0, 4600, 600], "n": 2}
    samples = DET.sample_instances(family, ir, limit=2)
    images, meta = DET._sample_views(samples, ir=ir, snapshot=snapshot, walls=(),
                                     thickness=200.0, cfg={"images_per_family": 3})
    assert len(images) == len(meta) == 3
    assert {m["instance"] for m in meta} == {"inst:a", "inst:b"}
    assert all(m["focus"] > 0 or m["focus_box"] for m in meta)


# --------------------------------------------------------------- 3) 提示词与授权
class _Gateway:
    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = []
        self.available = True

    def ask(self, **kwargs):
        self.requests.append(kwargs)
        data = self.answers.pop(0) if self.answers else {}
        return SimpleNamespace(ok=True, data=data, images=len(kwargs.get("images") or ()),
                               as_dict=lambda: {"ok": True})


def test_prompt_states_scope_and_forbids_name_based_reasoning():
    family = _synthetic_family(category="column")
    family["authorization_scope"] = "representative_only"
    gateway = _Gateway([{"component": {"category": "column",
                                       "subtype": "rect_column",
                                       "confidence": "high"}}])
    snapshot, ir = _family_scene([family])
    reading = DET.read_family(family, ir=ir, snapshot=snapshot, gateway=gateway)
    prompt = gateway.requests[0]["prompt"]
    assert "representative instance ONLY" in prompt
    assert "Layer names, block names" in prompt
    assert "geometry only" in prompt
    facts = reading["facts"]
    for banned in ("layers", "name", "block"):
        assert banned not in facts
    assert "block_instance" in facts


def test_family_inconsistency_limits_authorization_to_representative():
    family = _synthetic_family(category="column")
    gateway = _Gateway([{"component": {"category": "column",
                                       "subtype": "rect_column",
                                       "confidence": "high"},
                         "family_consistent": False,
                         "outliers": ["inst:3"]}])
    snapshot, ir = _family_scene([family])
    reading = DET.read_family(family, ir=ir, snapshot=snapshot, gateway=gateway)
    assert reading["status"] == "ok"
    assert reading["family_consistent"] is False
    assert reading["authorization_scope"] == "representative_only"


def test_need_closeup_triggers_a_second_call_with_a_tighter_view():
    snapshot = _square_snapshot(count=2)
    ir = IR.build(snapshot, scale={"thickness": 200, "noise": 2})
    family = {"id": "fam:c", "kind": "contour", "category_guess": "column",
              "authorization_scope": "family",
              "representative": {"id": "inst:a", "uids": [], "bbox": [0, 0, 600, 600]},
              "instances": [{"id": "inst:a", "uids": [], "bbox": [0, 0, 600, 600]}],
              "bbox": [0, 0, 600, 600], "n": 1}
    gateway = _Gateway([
        {"component": {"category": "none", "subtype": "other", "confidence": "low"},
         "need_closeup": True},
        {"component": {"category": "column", "subtype": "rect_column",
                       "confidence": "high"},
         "verdict": "keep", "need_closeup": False, "family_consistent": True}])
    reading = DET.read_family(family, ir=ir, snapshot=snapshot, gateway=gateway)
    assert reading["calls"] == 2
    assert reading["closeup_used"] is True
    assert reading["category"] == "column"
    assert len(gateway.requests) == 2
    assert gateway.requests[1]["images"], "补看必须真的带一张更紧的图"


def test_repeated_closeup_request_remains_unresolved():
    family = _synthetic_family()
    snapshot, ir = _family_scene([family])
    gateway = _Gateway([
        {"component": {"category": "column", "subtype": "rect_column",
                       "confidence": "high"},
         "verdict": "keep", "family_consistent": True, "need_closeup": True},
        {"component": {"category": "column", "subtype": "rect_column",
                       "confidence": "high"},
         "verdict": "keep", "family_consistent": True, "need_closeup": True}])
    reading = DET.read_family(family, ir=ir, snapshot=snapshot, gateway=gateway)
    assert reading["calls"] == len(gateway.requests) == 2
    assert gateway.requests[1]["images"]
    assert reading["status"] == "error"
    assert reading["reason"] == "CLOSEUP_UNANSWERED"
    assert reading["need_closeup"] is True
    assert not reading.get("closeup_used")
    assert reading["category"] is None


# --------------------------------------------------------------- 4) 观察要续看
@pytest.fixture
def family_observation_only(monkeypatch):
    """Keep the separate global bootstrap from consuming family-test answers."""
    from dwg_reader.recognition.v2 import multimodal_bootstrap as BOOT

    monkeypatch.setattr(BOOT, "run", lambda **kwargs: {
        "status": "off", "calls": 0, "images_seen": 0,
        "regions": [], "items": [], "scene": {}})


def _family_record(fid, category, priority=0.5, kind="contour"):
    instances = [{"id": "inst:%s:%d" % (fid, i), "uids": [],
                  "bbox": [i * 4000, 0, i * 4000 + 600, 600]}
                 for i in range(3)]
    return {"id": fid, "kind": kind, "category_guess": category,
            "priority": priority, "n": len(instances), "authorization_scope": "family",
            "representative": dict(instances[0]), "instances": instances,
            "bbox": [0, 0, 8600, 600], "explained_ratio": 0.0, "evidence": {}}


def test_next_observation_skips_seen_families_and_balances_categories():
    families = [_family_record("fam:a", "window", 0.9),
                _family_record("fam:b", "window", 0.8),
                _family_record("fam:c", "column", 0.7),
                _family_record("fam:d", "column", 0.6)]
    first = FAM.next_observation(families, observed=(), category_counts={})
    assert first["family"]["id"] == "fam:a"
    second = FAM.next_observation(families, observed={"fam:a"},
                                 category_counts={"window": 1})
    assert second["family"]["id"] == "fam:c", "应当换成最缺观察的类别"
    third = FAM.next_observation(families, observed={"fam:a", "fam:c"},
                                category_counts={"window": 1, "column": 1})
    assert third["family"]["id"] == "fam:b"
    assert FAM.next_observation(families, observed={"fam:a", "fam:b", "fam:c", "fam:d"},
                               category_counts={}) is None


def test_controller_keeps_observing_after_a_family_is_rejected(family_observation_only):
    """柱族被否掉后必须继续看别的柱族，而不是把名额和剩余预算一起丢掉。"""
    from dwg_reader.recognition.v2 import proposal_engines as PE

    families = [_family_record("fam:c%d" % i, "column", 0.9 - i * 0.01)
                for i in range(4)]
    families += [_family_record("fam:w%d" % i, "window", 0.5 - i * 0.01)
                 for i in range(2)]
    answers = [{"component": {"category": "none", "subtype": "furniture",
                              "confidence": "high", "negative_features": ["furniture"]},
                "family_consistent": True}]
    answers += [{"component": {"category": "column", "subtype": "rect_column",
                               "confidence": "high"}, "family_consistent": True}] * 8
    gateway = _Gateway(answers)
    snapshot, ir = _family_scene(families)
    result = AG.run(ir=ir, snapshot=snapshot, families=families,
                    gateway=gateway, ai={"mode": "enhance"},
                    profile="openings_columns",
                    budget={"max_model_calls": 12, "max_recheck": 0})
    readings = result["detector"]["readings"]
    observed = [r["family"] for r in readings]
    assert len(observed) == 6, observed
    assert {"fam:c0", "fam:c1", "fam:c2", "fam:c3"} <= set(observed)
    assert result["families"]["rejected"] == ["fam:c0"]
    assert {"fam:c1", "fam:c2", "fam:c3"} <= set(result["families"]["confirmed"])
    assert gateway.requests, "控制器必须真的调用模型"
    audit = result["model_audit"]
    assert len(audit) == len(readings)
    assert all(row["request"]["prompt"] for row in audit)
    assert "Scope of this answer" in audit[0]["request"]["prompt"]


def test_controller_reserves_calls_for_rechecks_and_reports_unobserved(family_observation_only):
    families = [_family_record("fam:c%d" % i, "column", 0.9 - i * 0.01)
                for i in range(8)]
    gateway = _Gateway([{"component": {"category": "column",
                                       "subtype": "rect_column",
                                       "confidence": "high"}, "family_consistent": True}] * 20)
    snapshot, ir = _family_scene(families)
    result = AG.run(ir=ir, snapshot=snapshot, families=families, gateway=gateway,
                    ai={"mode": "enhance"}, profile="openings_columns",
                    budget={"max_model_calls": 6, "max_recheck": 2})
    observation = result["detector"]["observation"]
    assert observation["observed"] < 8
    assert observation["unobserved"] > 0
    assert observation["reserved_calls"] == 2
    assert observation["stopped"] == "BUDGET"
    assert len(gateway.requests) <= 6


# --------------------------------------------------------------- 5) 请求留档
def test_model_audit_persists_prompt_and_images_then_strips_bytes(tmp_path):
    import base64
    from dwg_reader.recognition.v2 import _persist_model_audit

    payload = base64.b64encode(b"\x89PNG pretend").decode("ascii")
    rows = [{"family": "fam:x", "status": "ok",
             "request": {"system": "sys", "prompt": "PROMPT TEXT",
                         "images": [{"image": 1, "instance": "inst:a",
                                     "scale": "local", "sha256": "abc",
                                     "b64": payload}],
                         "image_count": 1},
             "reply": {"ok": True}, "raw": {"component": {"category": "column"}}}]
    public = _persist_model_audit(rows, audit_dir=str(tmp_path / "audit"))
    assert "PROMPT TEXT" == public[0]["request"]["prompt"]
    assert "b64" not in public[0]["request"]["images"][0], "结果里不留图片正文"
    written = sorted(path.name for path in (tmp_path / "audit").iterdir())
    assert any(name.endswith(".png") for name in written)
    assert any(name.endswith(".json") for name in written)
    index = json.loads((tmp_path / "audit" / "index.json").read_text(encoding="utf-8"))
    assert index["calls"] == 1 and index["images"] == 1
    call = json.loads(next((tmp_path / "audit").glob("01-*.json")).read_text(
        encoding="utf-8"))
    assert call["request"]["prompt"] == "PROMPT TEXT"
    assert call["raw"]["component"]["category"] == "column"


def test_furniture_answer_with_parts_is_not_discarded_as_grammar_error():
    """实测：模型答"家具"并给出 parts，但语法目录里没有 none 类别 →
    旧行为把整条判成 GRAMMAR_INVALID，等于把"这是家具"的否决理由丢掉。"""
    family = _synthetic_family(category="column")
    gateway = _Gateway([{"component": {
        "category": "none", "subtype": "furniture", "confidence": "high",
        "parts": {"hatch": "repeated_line_bundle", "tie": "short_connectors"},
        "negative_features": ["furniture"]}, "family_consistent": True}])
    snapshot, ir = _family_scene([family])
    reading = DET.read_family(family, ir=ir, snapshot=snapshot, gateway=gateway)
    assert reading["status"] == "ok"
    assert reading["category"] == "none"
    assert reading["negatives"] == ["furniture"]
    assert reading["grammar"] is None
    assert reading["grammar_error"], "语法写不进目录要留痕，但不作废类别"


def test_unknown_primitive_is_recorded_but_keeps_the_category():
    family = _synthetic_family(category="column")
    gateway = _Gateway([{"component": {
        "category": "column", "subtype": "rect_column", "confidence": "high",
        "parts": {"body": "no_such_primitive"}}}])
    snapshot, ir = _family_scene([family])
    reading = DET.read_family(family, ir=ir, snapshot=snapshot, gateway=gateway)
    assert reading["status"] == "ok"
    assert reading["category"] == "column"
    assert reading["grammar_error"]["code"] == "UNKNOWN_PRIMITIVE"


def test_hatch_targets_render_and_get_highlighted():
    """填充块（HATCH）以前既进不了取景也画不出来：目标等于不存在。"""
    snapshot = {"entities": [
        {"handle": "H1", "type": "HATCH", "layer": "PUB_HATCH", "solid": True,
         "loops": [[[0, 0], [0, 800], [800, 800], [800, 0]]]},
        {"handle": "H2", "type": "HATCH", "layer": "PUB_HATCH", "solid": True,
         "loops": [[[2000, 0], [2000, 800], [2800, 800], [2800, 0]]]}],
        "derived": []}
    views = VISION.render_views(snapshot, {"bbox": [-200, -200, 1000, 1000]},
                                scales=("local",), thickness=200.0,
                                focus_handles=["H1"], numbering=True)
    rendered = [v for v in views["views"] if v["status"] == "rendered"]
    assert rendered
    assert rendered[0]["focus"] == 1
    assert rendered[0]["focus_box"] is False


def _big_family(fid="fam:big", n=12, category="column"):
    family = _family_record(fid, category, 0.9)
    family["n"] = n
    family["instances"] = [{"id": "inst:%s%d" % (fid, i), "uids": [],
                            "bbox": [i * 4000, 0, i * 4000 + 600, 600]}
                           for i in range(n)]
    family["representative"] = dict(family["instances"][0])
    family["bbox"] = [0, 0, (n - 1) * 4000 + 600, 600]
    return family


def test_large_family_needs_two_agreeing_reads_before_propagation(family_observation_only):
    families = [_big_family(n=12)]
    answers = [{"component": {"category": "column", "subtype": "rect_column",
                              "confidence": "high"}, "family_consistent": True}] * 2
    gateway = _Gateway(answers)
    snapshot, ir = _family_scene(families)
    result = AG.run(ir=ir, snapshot=snapshot, families=families, gateway=gateway,
                    ai={"mode": "enhance"}, profile="openings_columns",
                    budget={"max_model_calls": 8, "max_recheck": 0})
    assert result["families"]["confirmed"] == ["fam:big"]
    second = result["detector"]["readings"][0]["second_read"]
    assert second["status"] == "ok"
    assert second["category"] == "column"
    assert second["images"] > 0
    assert len(gateway.requests) == 2
    assert all(request["images"] for request in gateway.requests)
    primary_images, verification_images = [row["request"]["images"]
                                           for row in result["model_audit"]]
    primary_ids = {image["instance"] for image in primary_images}
    verification_ids = {image["instance"] for image in verification_images}
    assert len(primary_ids) == len(verification_ids) == 3
    assert primary_ids.isdisjoint(verification_ids)
    assert all(image["focus"] > 0
               for image in primary_images + verification_images)


def test_conflicting_second_read_blocks_family_propagation(family_observation_only):
    """同一族两次判读不一致（实测：扶手椅 vs 双扇门）时不许整族推广。"""
    families = [_big_family(n=12)]
    answers = [{"component": {"category": "door", "subtype": "swing_door",
                              "confidence": "high"}, "family_consistent": True},
               {"component": {"category": "none", "subtype": "furniture",
                              "confidence": "high",
                              "negative_features": ["furniture"]}, "family_consistent": True}]
    gateway = _Gateway(answers)
    snapshot, ir = _family_scene(families)
    result = AG.run(ir=ir, snapshot=snapshot, families=families, gateway=gateway,
                    ai={"mode": "enhance"}, profile="openings_columns",
                    budget={"max_model_calls": 8, "max_recheck": 0})
    assert result["families"]["confirmed"] == []
    codes = {row.get("code") for row in result["failures"]}
    assert "FAMILY_CONFLICT" in codes


def test_same_category_but_lower_second_confidence_is_not_a_conflict(family_observation_only):
    """实测：同一个柱族两次都答 column，但一次 high 一次 medium —— 这是没到门槛，
    不是类别冲突；失败码要能区分，否则复盘时看不出到底是哪种情况。"""
    families = [_big_family(n=12)]
    answers = [{"component": {"category": "column", "subtype": "rect_column",
                              "confidence": "high"}, "family_consistent": True},
               {"component": {"category": "column", "subtype": "rect_column",
                              "confidence": "medium"}, "family_consistent": True}]
    snapshot, ir = _family_scene(families)
    result = AG.run(ir=ir, snapshot=snapshot, families=families, gateway=_Gateway(answers),
                    ai={"mode": "enhance"}, profile="openings_columns",
                    budget={"max_model_calls": 8, "max_recheck": 0})
    assert result["families"]["confirmed"] == []
    codes = [row.get("code") for row in result["failures"]]
    assert "FAMILY_CONFLICT" not in codes
    assert "FAMILY_UNREAD" in codes
