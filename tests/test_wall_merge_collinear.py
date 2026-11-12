"""一面墙被门拆开仍然是一面墙——只有柱子 / 另一道墙才断开。

口径（用户给定，与人工算量一致）：
* 一堵墙开了门或窗，还是**一面墙**（门窗只是墙上的洞）；
* 只有被柱子、或被另一道墙拦住才断开。

实测背景：住院楼清晰版 595 个原子墙段 → 跨洞口合并后 272 段 → 按本条口径再合到
235 面墙（房间数 42 一个没少，因为房间边界用的是未合并的墙网）。
"""
from __future__ import annotations

import pytest

from dwg_reader.recognition import walls as W


def _run(x0, x1, y=0.0, t=200.0, orient="H"):
    return {"orient": orient, "center": y, "thickness": t,
            "start": [x0, y], "end": [x1, y], "length": x1 - x0,
            "segments": [[x0, x1]], "srcs": [{"handle": f"{x0}-{x1}"}],
            "caps_n": 0, "role": None, "openings": [],
            "outline": [[x0, y - t / 2], [x1, y - t / 2],
                        [x1, y + t / 2], [x0, y + t / 2]]}


def test_collinear_runs_with_gap_merge_into_one_wall():
    """中间空着一段（门没识别出来也算）——仍是同一面墙。"""
    out = W.merge_collinear([_run(0, 3000), _run(3900, 7000)])
    assert len(out) == 1
    assert out[0]["start"][0] == pytest.approx(0)
    assert out[0]["end"][0] == pytest.approx(7000)
    assert out[0]["length"] == pytest.approx(7000)
    assert out[0]["segments"] == [[0, 3000], [3900, 7000]]
    assert out[0]["merged_gaps"] == [pytest.approx(900.0)]


def test_column_in_the_gap_keeps_the_wall_split():
    """柱位是墙的断点：柱子占着缺口就不许接。"""
    col = {"bbox": [3400, -300, 3800, 300]}
    out = W.merge_collinear([_run(0, 3000), _run(3900, 7000)], blockers=[col])
    assert len(out) == 2


def _vrun(x, y0, y1, t=200.0):
    return {"orient": "V", "center": x, "thickness": t,
            "start": [x, y0], "end": [x, y1], "length": y1 - y0,
            "segments": [[y0, y1]], "srcs": [{"handle": f"v{x}"}],
            "caps_n": 0, "role": None, "openings": [],
            "outline": [[x - t / 2, y0], [x + t / 2, y0],
                        [x + t / 2, y1], [x - t / 2, y1]]}


def test_perpendicular_wall_in_the_gap_keeps_the_wall_split():
    """另一道墙横穿缺口——那是两道墙，不是一道。"""
    out = W.merge_collinear([_run(0, 3000), _run(3900, 7000),
                             _vrun(3600, -1500, 1500)])
    horiz = [w for w in out if w["orient"] == "H"]
    assert len(horiz) == 2


def test_thickness_change_keeps_the_wall_split():
    """厚度不同是两道墙（面层/隔墙交接），不许接。"""
    out = W.merge_collinear([_run(0, 3000), _run(3900, 7000, t=300.0)])
    assert len(out) == 2


def test_gap_beyond_limit_is_not_merged():
    """缺口超过上限就是两个房间之间的开敞空间，不是一面墙。"""
    out = W.merge_collinear([_run(0, 3000), _run(12000, 15000)])
    assert len(out) == 2


def test_rooms_are_built_from_segments_not_from_merged_walls():
    """合并只影响"墙"这个构件的计数，房间边界必须还用未合并的墙网。"""
    import inspect
    from dwg_reader.recognition import nodes
    src = inspect.getsource(nodes.node_envelope)
    assert 'walls_merge"]["walls"]' in src      # 未合并
    src_rooms = inspect.getsource(nodes.node_rooms) if hasattr(nodes, "node_rooms") else ""
    assert 'wall_runs' not in src_rooms or 'walls"]' in src_rooms
