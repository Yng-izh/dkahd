"""Regression tests for evidence routing, lossless local patches and provenance.

All inputs are synthetic and deterministic; no external LLM or CAD profile needed.
"""
import math
from types import SimpleNamespace

from dwg_reader.recognition import adaptive_controller as AC
from dwg_reader.recognition import finalstate as FS
from dwg_reader.recognition import quality_gate as QG
from dwg_reader.recognition import recompute as RC
from dwg_reader.recognition import rooms as ROOMS
from dwg_reader.recognition import scale as SCALE
from dwg_reader.recognition import local_solver as SOLVER
from dwg_reader.recognition.nodes import build_recognition_dag


def wall(x=0, *, rid="W1"):
    return {"id": rid, "start": [x, 0], "end": [x + 1000, 0],
            "bbox": [x, 0, x + 1000, 200], "geometry_state": "observed"}


def base(**kw):
    data = {"walls": [wall()], "columns": [], "doors": [], "windows": [],
            "faces": [], "unresolved": [], "inferred_edges": []}
    data.update(kw)
    return data


def test_gate_routes_defect_nodes_and_evidence_in_correct_order(monkeypatch):
    def mock_enrich(case, _index):
        return dict(case, alternatives=[{"action": "NO_ACTION"},
                                        {"action": "retry_pair"}])
    monkeypatch.setattr(QG.CB, "build", mock_enrich)
    defects = [{"type": "BROKEN_WALL", "region": [0, 0, 1000, 1000],
                "evidence_ids": ["ev:L1"],
                "repair_options": ["option:retry_pair"], "detail": {"gap": 42}}]
    result = QG.check(snapshot={"entities": []}, plan={"layers": {}},
                      defects=defects, evidence={"items": {"ev:L1": {}}})
    defect = next(c for c in result["cases"] if c["reason"] == "DEFECT_BROKEN_WALL")
    assert defect["affected_nodes"] == ["walls_seg", "rooms"]
    assert defect["evidence_ids"] == ["ev:L1"]
    assert "walls_seg" in result["affected_nodes"]
    assert "ev:L1" in result["evidence_ids"]


def test_patch_replaces_same_id_without_append_and_removes_deleted():
    before = base(walls=[wall(0), wall(2000, rid="W2")])
    after = base(walls=[wall(10), wall(3000, rid="W3")])
    change = AC._diff(before, after)
    assert "walls:W1" in change["updated"]
    assert "walls:W2" in change["removed"]
    assert "walls:W3" in change["added"]
    result = FS.commit(FS.base_state(walls_seg=before["walls"]), change)
    assert result["provenance"]["patch"] == "accepted"
    assert {x["id"] for x in result["walls"]} == {"W1", "W3"}
    assert next(x for x in result["walls"] if x["id"] == "W1")["start"] == [10, 0]
    assert len(result["walls"]) == 2


def test_same_count_modified_face_and_empty_face_are_committed():
    f1 = {"id": "F1", "polygon": [[0, 0], [1, 0], [1, 1]], "area": 0.5}
    f2 = {"id": "F1", "polygon": [[0, 0], [2, 0], [2, 1]], "area": 1}
    before = base(faces=[f1])
    after = base(faces=[f2])
    change = AC._diff(before, after)
    assert change["faces_changed"] is True
    res = FS.commit(FS.base_state(walls_seg=before["walls"],
                                  spaces={"faces": before["faces"]}), change)
    assert res["faces"] == [f2]
    drop = AC._diff(before, base(faces=[]))
    assert drop["faces_changed"] is True
    assert FS.commit(FS.base_state(spaces={"faces": [f1]}), drop)["faces"] == []


def test_regionless_upsert_and_outside_deletion_checks():
    base_w = [wall(0), wall(2000, rid="W2")]
    assert len(RC.merge_region(base_w, [wall(20)], None)) == 2
    assert RC.merge_region(base_w, [wall(20)], None)[1]["start"] == [20, 0]
    assert SOLVER._changed_outside({"walls": base_w},
                                   {"walls": [base_w[0]]},
                                   [-100, -100, 1500, 500]) > 0


def test_room_carries_observed_or_hypothetical_source_face():
    poly = [[0, 0], [1000, 0], [1000, 1000], [0, 1000]]
    lab = {"handle": "T1", "name": "办公室", "x": 500, "y": 500}
    states = []
    for seal in ("observed", "hypothesis"):
        faces = [{"id": "F1", "polygon": poly, "bbox": [0, 0, 1000, 1000],
                  "center": [500, 500], "area": 1e6, "seal_state": seal}]
        rooms = ROOMS.rooms_from_faces(faces, [lab], min_area=1.0)["rooms"]
        assert len(rooms) == 1
        assert rooms[0]["source_face_ids"] == ["F1"]
        states.append((rooms[0]["boundary_state"], bool(rooms[0].get("needs_review"))))
    assert states == [("observed", False), ("hypothesis", True)]


def test_scale_thickness_projection_survives_arbitrary_rotation():
    theta = math.radians(31)
    u = (math.cos(theta), math.sin(theta))
    n = (-u[1], u[0])
    entities = []
    for j in range(3):
        for off in (j * 5000.0, j * 5000.0 + 200.0):
            entities.append({"type": "LINE", "layer": "WALL",
                             "start": [off * n[0], off * n[1]],
                             "end": [4000 * u[0] + off * n[0],
                                     4000 * u[1] + off * n[1]]})
    value, n_samples = SCALE._thickness_mode({"entities": entities},
                                             ["WALL"], with_count=True)
    assert n_samples >= 6
    assert abs(value - 200) < 2
    measured = SCALE.measure({"entities": entities}, plan={"layers": {"wall": ["WALL"]}})
    assert abs(measured["thickness"] - 200) < 2
    assert measured["provenance"]["thickness"]["sample_status"] == "sufficient"


def test_count_neutral_geometry_change_is_not_marked_no_change(monkeypatch):
    def fake_exec(operator, **kw):
        return {"status": "ok", "added": [wall(10)], "drop_ids": ["W1"]}
    def fake_apply(state, _patch):
        return dict(state, walls=[wall(10)])
    monkeypatch.setattr(RC.EXEC, "execute_operator", fake_exec)
    monkeypatch.setattr(RC.EXEC, "apply_patch", fake_apply)
    monkeypatch.setattr(RC, "_ensure_downstream", lambda s, **kw: s)
    plan = SimpleNamespace(steps=[{"operator": "test", "stage": "walls", "index": 0}],
                           target_ids=["W1"], hypothesis=None, region=None,
                           budget={"ms": 4000})
    outcome = RC.execute_local_subdag(plan, state=base(), index={})
    assert outcome["status"] == "ok"
    assert outcome["changed"]["walls"] == 0
    assert outcome["changed"]["geometry_changed"] is True


def test_environment_dependent_nodes_never_reuse_stale_cache():
    dag = build_recognition_dag()
    for node in ("load", "openings_select", "quality_gate", "adaptive_controller",
                 "final_candidate_state"):
        assert dag.nodes[node].cacheable is False


def test_failed_geometry_generator_skips_dependent_topology(monkeypatch):
    calls = []
    def fail_generator(name, **_):
        calls.append(name)
        if name == "retry_wall_pairing":
            return {"reason": "NO_LOCAL_EVIDENCE"}
        raise AssertionError("No downstream operator should have executed")
    monkeypatch.setattr(RC.EXEC, "execute_operator", fail_generator)
    plan = SimpleNamespace(
        steps=[{"index": 0, "operator": "retry_wall_pairing", "stage": "walls", "deps": []},
               {"index": 1, "operator": "rebuild_wall_graph", "stage": "wall_graph", "deps": [0]},
               {"index": 2, "operator": "rebuild_space_faces", "stage": "space", "deps": [0, 1]}],
        target_ids=["pat:7"], hypothesis=None, region=[0, 0, 100, 100], budget={"ms": 4000})
    result = RC.execute_local_subdag(plan, state=base(), index={})
    assert calls == ["retry_wall_pairing"]
    assert [t["status"] for t in result["trace"]] == ["skipped"] * 3
    assert result["status"] == "no_change"


def test_failed_same_stage_virtual_boundary_skips_face_rebuild(monkeypatch):
    called = []
    def fail_boundary(name, **_):
        called.append(name)
        if name == "try_virtual_boundary":
            return {"reason": "NO_INFERRED_EDGES"}
        raise AssertionError("Face rebuild should have been skipped")
    monkeypatch.setattr(RC.EXEC, "execute_operator", fail_boundary)
    plan = SimpleNamespace(
        steps=[{"index": 0, "operator": "try_virtual_boundary", "stage": "space", "deps": []},
               {"index": 1, "operator": "rebuild_space_faces", "stage": "space", "deps": []}],
        target_ids=["face:1"], hypothesis=None, region=None, budget={"ms": 4000})
    result = RC.execute_local_subdag(plan, state=base(), index={})
    assert called == ["try_virtual_boundary"]
    assert result["status"] == "no_change"


def test_line_support_bbox_is_local_and_rotation_aware():
    from dwg_reader.recognition import evidence as EVID
    r = {"pos": 200.0, "lo": 100.0, "hi": 200.0,
         "dir": [0.0, 1.0], "nrm": [-1.0, 0.0]}
    assert EVID.bbox_of(r) == [-200.0, 100.0, -200.0, 200.0]
