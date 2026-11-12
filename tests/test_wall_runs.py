"""跨门窗洞口墙合并的合成图回归测试。

覆盖：
  * 一条墙线 + 2 樘窗 -> 1 个连续 wall run + 2 个 openings；
  * 房间识别仍使用未合并墙段（门窗洞口处断开），连通性不变；
  * 墙组件 geometry 连续、segments 保留原子段、source_entities 含洞口 Handle；
  * 门窗 wall_id 指向合并后的墙实例。
"""
from __future__ import annotations

import json

import ezdxf

from dwg_reader.recognition import build_document, run_recognition
from dwg_reader.recognition.nodes import _rich_openings
from dwg_reader.recognition.walls import merge_across_openings


def canon(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _add_window_block(doc):
    block = doc.blocks.new("$TCHSYS$WIN2D")
    block.add_lwpolyline([(-0.5, -0.5), (0.5, -0.5), (0.5, 0.5), (-0.5, 0.5)], close=True)


def _add_windows(msp):
    for cx in (1400, 2800):
        ref = msp.add_blockref("$TCHSYS$WIN2D", (cx, 3700),
                               dxfattribs={"layer": "WINDOW", "xscale": 800,
                                           "yscale": 200, "rotation": 0})
        ref.add_attrib("A", "C1828", (0, 0), dxfattribs={"layer": "WINDOW"})


def _drawing(tmp_path, with_room: bool):
    """北墙 y=3700（面线 3600/3800）在 1000–1800、2400–3200 处开两樘窗。"""
    doc = ezdxf.new("R2010")
    for name in ("WALL", "SPACE", "WINDOW"):
        doc.layers.add(name)
    msp = doc.modelspace()

    # 北墙：两樘窗把面线切成三段
    for y in (3600, 3800):
        for a, b in ((200, 1000), (1800, 2400), (3200, 3800)):
            msp.add_line((a, y), (b, y), dxfattribs={"layer": "WALL"})
    # 北墙两端封口
    msp.add_line((200, 3600), (200, 3800), dxfattribs={"layer": "WALL"})
    msp.add_line((3800, 3600), (3800, 3800), dxfattribs={"layer": "WALL"})

    if with_room:
        # 南墙
        for y in (0, 200):
            msp.add_line((200, y), (3800, y), dxfattribs={"layer": "WALL"})
        # 西/东墙
        for x in (0, 200):
            msp.add_line((x, 200), (x, 3800), dxfattribs={"layer": "WALL"})
        for x in (3600, 3800):
            msp.add_line((x, 200), (x, 3800), dxfattribs={"layer": "WALL"})
        # 角部封口
        for (x1, y1, x2, y2) in [(0, 0, 200, 0), (3800, 0, 4000, 0),
                                 (0, 3800, 200, 3800), (3800, 3800, 4000, 3800)]:
            msp.add_line((x1, y1), (x2, y2), dxfattribs={"layer": "WALL"})
        msp.add_text("办公室", height=300,
                     dxfattribs={"layer": "SPACE", "insert": (2000, 1800)})
        msp.add_text("11.40m", height=200,
                     dxfattribs={"layer": "SPACE", "insert": (2000, 1500)})

    _add_window_block(doc)
    _add_windows(msp)

    path = tmp_path / ("window_room.dxf" if with_room else "window_wall.dxf")
    doc.saveas(path)
    return path


def test_wall_run_merges_across_two_windows(tmp_path):
    run = run_recognition(str(_drawing(tmp_path, with_room=False)),
                          targets=("assemble",))
    walls_merge = run.outputs["walls_merge"]

    # 未合并：窗洞把墙线断成 3 段
    assert len(walls_merge["walls"]) == 3
    # 合并：1 个连续 wall run，2 个洞口
    assert len(walls_merge["wall_runs"]) == 1
    wall_run = walls_merge["wall_runs"][0]
    assert wall_run["start"] == [200.0, 3700.0]
    assert wall_run["end"] == [3800.0, 3700.0]
    assert wall_run["length"] == 3600.0
    assert len(wall_run["openings"]) == 2
    assert [op["category"] for op in wall_run["openings"]] == ["window", "window"]
    assert [op["id"] for op in wall_run["openings"]] == ["WIN-01", "WIN-02"]
    assert [op["span"] for op in wall_run["openings"]] == [[1000.0, 1800.0],
                                                           [2400.0, 3200.0]]
    assert [op["width"] for op in wall_run["openings"]] == [800.0, 800.0]
    assert [op["type_code"] for op in wall_run["openings"]] == ["C1828", "C1828"]

    walls = [c for c in run.output["components"] if c["category"] == "wall"]
    assert len(walls) == 1
    wall = walls[0]
    assert wall["geometry"]["type"] == "wall_run"
    assert wall["geometry"]["start"] == [200.0, 3700.0]
    assert wall["geometry"]["end"] == [3800.0, 3700.0]
    assert wall["geometry"]["length"] == 3600.0
    assert wall["geometry"]["segment_count"] == 3
    assert len(wall["geometry"]["segments"]) == 3
    assert len(wall["attributes"]["openings"]) == 2
    assert "跨 2 个门窗洞口连续" in wall["evidence"]["detail"]

    opening_handles = {op["handle"] for op in wall["attributes"]["openings"]}
    source_handles = {src["handle"] for src in wall["source_entities"]}
    assert opening_handles <= source_handles
    assert all("洞口" in src["role"] for src in wall["source_entities"]
               if src["handle"] in opening_handles)


def test_room_connectivity_uses_unmerged_walls(tmp_path):
    run = run_recognition(str(_drawing(tmp_path, with_room=True)),
                          targets=("assemble",))
    result = run.output
    walls_merge = run.outputs["walls_merge"]

    # 房间/台阶/散水仍使用未跨洞口合并的 walls（北墙 3 段 + 南/西/东 3 段）
    assert len(walls_merge["walls"]) == 6
    assert len(walls_merge["wall_runs"]) == 4
    assert result["meta"]["counts"]["wall"] == 4
    assert result["meta"]["counts"]["room"] == 1
    assert result["meta"]["counts"]["window"] == 2

    rooms = [c for c in result["components"] if c["category"] == "room"]
    assert len(rooms) == 1
    assert rooms[0]["attributes"]["leak_to_outside"] is False
    assert rooms[0]["attributes"]["label_inside_outline"] is True

    windows = [c for c in result["components"] if c["category"] == "window"]
    north = [c for c in result["components"]
             if c["category"] == "wall" and c["geometry"]["start"][1] == 3700.0][0]
    assert len(north["attributes"]["openings"]) == 2
    assert all(w["attributes"]["wall_id"] == north["id"] for w in windows)


def _wall(orient, center, a, b, thickness=200.0):
    if orient == "H":
        start, end = [a, center], [b, center]
        outline = [[a, center - thickness / 2], [b, center - thickness / 2],
                   [b, center + thickness / 2], [a, center + thickness / 2]]
    else:
        start, end = [center, a], [center, b]
        outline = [[center - thickness / 2, a], [center - thickness / 2, b],
                   [center + thickness / 2, b], [center + thickness / 2, a]]
    return {"orient": orient, "center": center, "start": start, "end": end,
            "thickness": thickness, "length": b - a, "outline": outline,
            "srcs": [], "caps_n": 0, "segments": [[a, b]]}


def _opening(lo, hi, category="window", oid="WIN-01", pos=0.0):
    return {"orient": "H", "pos": pos, "lo": lo, "hi": hi,
            "category": category, "id": oid, "width": hi - lo,
            "type_code": "C1828", "handle": "A1", "layer": "WINDOW",
            "block": "$TCHSYS$WIN2D"}


def test_no_merge_across_uncovered_or_too_wide_gap():
    walls = [_wall("H", 0, 0, 1000), _wall("H", 0, 1400, 2400)]
    # 无洞口覆盖 -> 不合并
    assert len(merge_across_openings(walls, [])) == 2
    # 洞口覆盖 400mm 缺口 -> 合并
    merged = merge_across_openings(walls, [_opening(1000, 1400)])
    assert len(merged) == 1
    assert len(merged[0]["openings"]) == 1
    # 缺口 3001mm > max_gap -> 即使有洞口也不合并
    wide = [_wall("H", 0, 0, 1000), _wall("H", 0, 4001, 5000)]
    assert len(merge_across_openings(wide, [_opening(1000, 4001)])) == 2


def test_thickness_guard_prevents_merge():
    walls = [_wall("H", 0, 0, 1000, thickness=200),
             _wall("H", 0, 1400, 2400, thickness=300)]
    runs = merge_across_openings(walls, [_opening(1000, 1400)])
    assert len(runs) == 2


def test_door_without_opening_geometry_not_used_for_merge():
    door = {"block": "$TCHSYS$DOOR2D", "subtype": "单扇平开门", "layer": "DOOR",
            "insert": [0, 0], "rotation": 0, "scale": [1, 1], "attr": "M1021",
            "width": 1000, "handle": "D1"}
    assert _rich_openings([door], []) == []


def test_assemble_does_not_pollute_wall_runs(tmp_path):
    path = _drawing(tmp_path, with_room=False)
    partial = run_recognition(str(path), targets=("walls_merge",))
    full = run_recognition(str(path), targets=("assemble",))
    assert canon(partial.outputs["walls_merge"]) == canon(full.outputs["walls_merge"])
    assert all("id" not in wall for wall in full.outputs["walls_merge"]["wall_runs"])


def test_schema_document_cross_references(tmp_path):
    path = _drawing(tmp_path, with_room=True)
    run = run_recognition(str(path), targets=("assemble",))
    result = run.output
    document = build_document(result, str(path))

    schema_wall_ids = {c["id"] for c in document["components"] if c["category"] == "墙"}
    schema_door_ids = {c["id"] for c in document["components"] if c["category"] == "门"}
    schema_window_ids = {c["id"] for c in document["components"] if c["category"] == "窗"}
    assert schema_wall_ids and schema_window_ids

    wall_id_links = 0
    for rec in document["components"]:
        if rec["category"] not in ("门", "窗"):
            continue
        wall_id = rec.get("attributes", {}).get("wall_id")
        if wall_id is not None:
            assert wall_id in schema_wall_ids, (rec["id"], wall_id)
            wall_id_links += 1
    opening_links = 0
    for rec in document["components"]:
        if rec["category"] != "墙":
            continue
        for opening in rec.get("attributes", {}).get("openings", []):
            target = (schema_door_ids if opening.get("category") == "door"
                      else schema_window_ids)
            assert opening.get("id") in target, (rec["id"], opening.get("id"))
            opening_links += 1
    assert wall_id_links == 2
    assert opening_links == 2

    # build_document 只重写 schema 文档，不得污染上游组件 id
    comp_wall_ids = {c["id"] for c in result["components"] if c["category"] == "wall"}
    comp_door_ids = {c["id"] for c in result["components"] if c["category"] == "door"}
    comp_window_ids = {c["id"] for c in result["components"] if c["category"] == "window"}
    for rec in result["components"]:
        if rec["category"] in ("door", "window"):
            wall_id = rec.get("attributes", {}).get("wall_id")
            if wall_id is not None:
                assert wall_id in comp_wall_ids, (rec["id"], wall_id)
        elif rec["category"] == "wall":
            for opening in rec.get("attributes", {}).get("openings", []):
                target = (comp_door_ids if opening.get("category") == "door"
                          else comp_window_ids)
                assert opening.get("id") in target, (rec["id"], opening.get("id"))
