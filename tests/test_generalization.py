"""泛化架构的不变量测试。

这里测的不是"某张图认对了几个"，而是**架构承诺的那几条不变量**：

* 阈值随图内尺度变化，不随图纸文件名变化（``scale``）；
* 一个洞口只算一次，双扇门两条弧归一（``unified``）；
* 房间以面域为单位，房名只做挂靠，挂不上不丢（``rooms_from_faces``）；
* 拉开字距的散字词组能拼回整词（``merge_spaced_words``）；
* 柱允许缺边、长条形另计，不混进方柱（``_rects_from_lines`` / ``fuse_columns``）。
"""
from __future__ import annotations

import json
import math
import pathlib
import re

import pytest

from dwg_reader.recognition import elements as EL
from dwg_reader.recognition import rooms as RM
from dwg_reader.recognition import scale as SC
from dwg_reader.recognition import unified as UNI
from dwg_reader.recognition.base import merge_spaced_words


# --------------------------------------------------------------------------- 尺度
def test_thresholds_scale_with_drawing_not_with_absolute_mm():
    """同一套倍率，两种图纸尺度 -> 阈值按比例变化。"""
    small = SC.thresholds({"thickness": 100.0, "door_w": 700.0,
                           "text_h": 200.0, "bay": 3000.0})
    large = SC.thresholds({"thickness": 300.0, "door_w": 1500.0,
                           "text_h": 700.0, "bay": 9000.0})
    assert small["gap_min"] < large["gap_min"]
    assert small["col_short_max"] < large["col_short_max"]
    assert small["face_min_area"] < large["face_min_area"]
    # 倍率本身跨图纸恒定
    assert small["gap_min"] == pytest.approx(0.30 * 700.0)
    assert large["gap_min"] == pytest.approx(0.30 * 1500.0)


def test_thresholds_never_return_none():
    """量不出基准量也必须给出可算的数（下游不允许 None 参与算术）。"""
    th = SC.thresholds({})
    for key, val in th.items():
        assert isinstance(val, float) and val > 0, key


def test_wall_thickness_is_measured_from_parallel_lines():
    """没有墙识别结果时，墙厚要**从图里量**（平行线垂距众数），不是退到默认 200。

    两条相距 400 的平行线，本身就说明这张图的墙厚是 400——以前这里直接返回
    默认 200，等于把图上写着的信息丢了。
    """
    snap = {"entities": [
        {"type": "LINE", "start": [0, 0], "end": [4000, 0], "layer": "WALL"},
        {"type": "LINE", "start": [0, 400], "end": [4000, 400], "layer": "WALL"},
    ]}
    sc = SC.measure(snap, plan={"layers": {"wall": ["WALL"]}})
    assert sc["thickness_source"] == "measured"
    assert sc["thickness"] == pytest.approx(400.0, abs=1.0)


def test_measure_falls_back_only_when_nothing_to_measure():
    """真量不出来才允许退默认值，并且必须在出处里标明是 default。"""
    sc = SC.measure({"entities": []})
    assert sc["thickness_source"] == "default"
    assert sc["thickness"] == SC.DEFAULT_THICKNESS
    assert sc["provenance"]["thickness"]["source"] == "default"


def test_measure_uses_real_walls_when_given():
    snap = {"entities": [{"type": "LINE", "start": [0, 0], "end": [5000, 0]}]}
    sc = SC.measure(snap, walls=[{"thickness": 240}, {"thickness": 260}])
    assert sc["thickness_source"] == "measured"
    assert sc["thickness"] == pytest.approx(250.0, abs=1.0)   # 240/260 的中位数


def test_every_measurement_has_provenance():
    """每个基准量都要能回答"从哪来的"——这是换图可比的前提。"""
    snap = {"entities": [
        {"type": "LINE", "start": [0, 0], "end": [5000, 0]},
        {"type": "LINE", "start": [0, 200], "end": [5000, 200]},
        {"type": "TEXT", "insert": [10, 10], "height": 350, "text": "房间"},
    ]}
    sc = SC.measure(snap)
    for key in ("text_h", "thickness", "door_w", "bay", "noise"):
        row = sc["provenance"][key]
        assert row["source"] in ("measured", "default"), key
        assert row["estimator"], key
        assert row["value"] is not None, key


def test_structure_table_is_dimensionless():
    """STRUCTURE 里的每个数都必须是无量纲倍率——出现 100、2000 这种毫米数就是魔数。"""
    for name, value in SC.STRUCTURE.items():
        assert 0.0 < float(value) <= 20.0, f"{name}={value} 不像无量纲倍率"


def test_tolerances_follow_drawing_noise():
    """容差必须跟着"这张图画得有多准"走，而不是写死。"""
    clean = SC.thresholds({"thickness": 200, "door_w": 900, "text_h": 350,
                           "bay": 6000, "noise": 1.0})
    rough = SC.thresholds({"thickness": 200, "door_w": 900, "text_h": 350,
                           "bay": 6000, "noise": 30.0})
    assert rough["tol_align"] > clean["tol_align"]
    assert rough["tol_join"] > clean["tol_join"]
    assert rough["tol_dedupe"] > clean["tol_dedupe"]
    # 但不能被噪声放大到荒谬：上界由这张图的尺度兜住
    assert rough["tol_join"] <= 0.5 * clean["door_w"]


def test_thresholds_have_no_bare_millimetre_constants():
    """阈值表里每一行都必须能追到"基准量 × STRUCTURE 倍率"。"""
    sc = {"thickness": 200, "door_w": 900, "text_h": 350, "bay": 6000, "noise": 2.0}
    a = SC.thresholds(sc)
    b = SC.thresholds({k: v * 2 for k, v in sc.items()})
    # 把整张图放大一倍：除了"不计倍率"的固定下限，其余阈值必须同比例放大
    for key in ("col_short_max", "col_long_max", "split_res", "same_name_adj"):
        assert b[key] == pytest.approx(a[key] * 2, rel=0.02), key
    # 面积量是二次的：字高翻倍 -> 最小面域翻四倍
    assert b["face_min_area"] == pytest.approx(a["face_min_area"] * 4, rel=0.02)


# --------------------------------------------------------------------------- 统一洞口# --------------------------------------------------------------------------- 统一洞口
def _wall_lines():
    walls = [{"start": [0, 0], "end": [2000, 0], "thickness": 200},
             {"start": [2900, 0], "end": [6000, 0], "thickness": 200}]
    from dwg_reader.recognition import wallmodel as WM
    model = WM.build(walls, 200.0)
    return WM.lines(model["spans"])


def _double_leaf():
    return [
        {"block": None, "layer": "0", "handle": "A1", "insert": [2000, 0], "width": 450,
         "hinges": [[2000, 0]], "opening": [[2000, 0], [2450, 0]],
         "free_end": [2000, 450], "rotation": 0},
        {"block": None, "layer": "0", "handle": "A2", "insert": [2900, 0], "width": 450,
         "hinges": [[2900, 0]], "opening": [[2900, 0], [2450, 0]],
         "free_end": [2900, 450], "rotation": 0},
    ]


def test_double_leaf_arcs_become_one_opening():
    """双扇门两条弧 + 一个门块 -> **1** 樘，不是 3 樘。"""
    doors = _double_leaf() + [
        {"block": "$TCHSYS$DOOR2D", "attr": "M0921", "layer": "DOOR", "handle": "B1",
         "insert": [2000, 0], "width": 900, "hinges": [[2000, 0]],
         "opening": [[2000, 0], [2900, 0]], "free_end": [2000, 900], "rotation": 0}]
    res = UNI.cluster(doors=doors, lines=_wall_lines(), thick=200.0,
                      scale={"thickness": 200, "door_w": 900, "text_h": 350, "bay": 4000})
    assert len(res["openings"]) == 1
    one = res["openings"][0]
    assert one["explain"]["double"] is True
    assert one["merged"] == 3
    assert len(one["hinges"]) == 2
    assert one["block"] == "$TCHSYS$DOOR2D"


def test_two_distinct_blocks_stay_two_doors():
    """两个独立的门块不能被并成一樘（办公楼 30 樘曾经被并成 26 樘）。"""
    doors = [
        {"block": "$TCHSYS$DOOR2D", "layer": "DOOR", "handle": "B1", "insert": [1000, 0],
         "width": 900, "opening": [[600, 0], [1500, 0]], "hinges": [[600, 0]]},
        {"block": "$TCHSYS$DOOR2D", "layer": "DOOR", "handle": "B2", "insert": [1900, 0],
         "width": 900, "opening": [[1500, 0], [2400, 0]], "hinges": [[1500, 0]]},
    ]
    res = UNI.cluster(doors=doors, lines=_wall_lines(), thick=200.0,
                      scale={"thickness": 200, "door_w": 900, "text_h": 350, "bay": 4000})
    assert len(res["openings"]) == 2


def test_narrow_opening_is_dropped():
    """家具缝级别的"洞口"不是洞口。"""
    doors = [{"block": None, "layer": "0", "handle": "A9", "insert": [2000, 0],
              "width": 25, "hinges": [[2000, 0]],
              "opening": [[2000, 0], [2025, 0]], "free_end": [2000, 25]}]
    res = UNI.cluster(doors=doors, lines=_wall_lines(), thick=200.0,
                      scale={"thickness": 200, "door_w": 900, "text_h": 350, "bay": 4000})
    assert not res["openings"]
    assert res["stats"]["dropped"].get("too_narrow") == 1


def test_unanchored_block_survives_without_wall():
    """贴不上墙线的门块不能被丢掉：墙没识别出来不等于门不存在。"""
    doors = [{"block": "$TCHSYS$DOOR2D", "layer": "DOOR", "handle": "B7",
              "insert": [50000, 50000], "width": 900,
              "opening": [[49550, 50000], [50450, 50000]], "hinges": [[49550, 50000]]}]
    res = UNI.cluster(doors=doors, lines=[], thick=200.0,
                      scale={"thickness": 200, "door_w": 900, "text_h": 350, "bay": 4000})
    assert len(res["openings"]) == 1
    assert res["openings"][0]["unanchored"] is True


def test_split_routes_window_block_to_windows():
    res = UNI.cluster(
        doors=[],
        windows=[{"block": "$TCHSYS$WIN2D", "layer": "WINDOW", "handle": "W1",
                  "center": [1000, 0], "width": 1500,
                  "axis": [[250, 0], [1750, 0]], "hinges": []}],
        lines=_wall_lines(), thick=200.0,
        scale={"thickness": 200, "door_w": 900, "text_h": 350, "bay": 4000})
    ds, ws = UNI.split(res["openings"])
    assert len(ws) == 1 and not ds


# --------------------------------------------------------------------------- 房间
def _faces():
    return [
        {"id": "F1", "polygon": [[0, 0], [4000, 0], [4000, 3000], [0, 3000]],
         "bbox": [0, 0, 4000, 3000], "area": 12e6},
        {"id": "F2", "polygon": [[4000, 0], [8000, 0], [8000, 3000], [4000, 3000]],
         "bbox": [4000, 0, 8000, 3000], "area": 12e6},
    ]


def test_room_is_a_face_not_a_flood_from_label():
    labels = [{"name": "办公室", "text": "办公室", "x": 2000, "y": 1500, "handle": "T1"},
              {"name": "会议室", "text": "会议室", "x": 6000, "y": 1500, "handle": "T2"}]
    res = RM.rooms_from_faces(_faces(), labels)
    assert len(res["rooms"]) == 2
    assert {r["label"]["name"] for r in res["rooms"]} == {"办公室", "会议室"}
    assert all(r["boundary"] == "faces" for r in res["rooms"])


def test_two_labels_in_one_face_make_one_room_with_alias():
    """同一面域挂"前室"+"兼消防电梯" -> 一个房间，另一个记别名。"""
    labels = [{"name": "前室", "x": 1000, "y": 1500, "handle": "T1"},
              {"name": "兼消防电梯", "x": 1400, "y": 1300, "handle": "T2"}]
    res = RM.rooms_from_faces(_faces(), labels)
    assert len(res["rooms"]) == 1
    assert res["rooms"][0]["aliases"] == ["兼消防电梯"]


def test_orphan_label_is_reported_not_dropped():
    """挂不上面域的房名必须留在 orphans 里，并带到最近面域的距离。"""
    labels = [{"name": "远处的房间", "x": 999999, "y": 999999, "handle": "T9"}]
    res = RM.rooms_from_faces(_faces(), labels)
    assert not res["rooms"]
    assert len(res["orphans"]) == 1
    assert res["orphans"][0]["nearest_distance"] is not None


def test_label_on_wall_line_still_attaches():
    """房名压在墙线上（点不在任何多边形内）要挂到最近面域，不能整间丢掉。"""
    labels = [{"name": "走道", "x": 4000, "y": 1500, "handle": "T3"}]
    res = RM.rooms_from_faces(_faces(), labels)
    assert len(res["rooms"]) == 1
    assert res["rooms"][0]["boundary"] in ("faces", "nearest_face")


def test_two_real_rooms_in_one_face_are_split():
    """一面里两个**相距很远的不同房名** -> 按中垂线切成两间（分隔墙漏检的情形）。

    这是大图上 58 个房名只落成 42 个房间的主因：一面多名时旧做法只能保一个名字。
    """
    labels = [{"name": "核医学科", "x": 500, "y": 1500, "h": 375, "handle": "T1"},
              {"name": "侯梯厅", "x": 3600, "y": 1500, "h": 375, "handle": "T2"}]
    res = RM.rooms_from_faces(_faces(), labels)
    assert len(res["rooms"]) == 2
    assert {r["label"]["name"] for r in res["rooms"]} == {"核医学科", "侯梯厅"}
    assert all(r["boundary"] == "split_room" for r in res["rooms"])
    # 两块加起来应当正好等于原来的面域（切分不重不漏）
    total = sum(r["area"] for r in res["rooms"])
    assert total == pytest.approx(12e6, rel=0.02)


def test_compound_name_is_not_split():
    """"前室"+"兼消防电梯" 是复合房名，切开就会凭空多出房间。"""
    labels = [{"name": "前室", "x": 1900, "y": 1600, "h": 375, "handle": "T1"},
              {"name": "兼消防电梯", "x": 2100, "y": 1400, "h": 375, "handle": "T2"}]
    res = RM.rooms_from_faces(_faces(), labels)
    assert len(res["rooms"]) == 1


def test_same_name_across_adjacent_faces_merges():
    labels = [{"name": "走廊", "x": 2000, "y": 1500, "handle": "T1"},
              {"name": "走廊", "x": 6000, "y": 1500, "handle": "T2"}]
    res = RM.rooms_from_faces(_faces(), labels)
    assert len(res["rooms"]) == 1
    assert res["rooms"][0]["part_count"] >= 1


# --------------------------------------------------------------------------- 文字
def test_spaced_word_merges():
    """拉开字距的"病 人 廊"要拼回一个词。"""
    recs = [
        {"handle": "1", "layer": "FF", "text": "病", "x": 0.0, "y": 0.0, "h": 450.0, "rot": 0.0},
        {"handle": "2", "layer": "FF", "text": "人", "x": 1277.0, "y": 0.0, "h": 450.0, "rot": 0.0},
        {"handle": "3", "layer": "FF", "text": "廊", "x": 2554.0, "y": 0.0, "h": 450.0, "rot": 0.0},
    ]
    out = merge_spaced_words(recs)
    assert len(out) == 1
    assert out[0]["text"] == "病人廊"
    assert out[0]["spaced_word"] is True
    assert out[0]["parts"] == ["1", "2", "3"]


def test_single_chars_far_apart_do_not_merge():
    recs = [
        {"handle": "1", "layer": "FF", "text": "下", "x": 0.0, "y": 0.0, "h": 450.0, "rot": 0.0},
        {"handle": "2", "layer": "FF", "text": "下", "x": 9000.0, "y": 0.0, "h": 450.0, "rot": 0.0},
    ]
    out = merge_spaced_words(recs)
    assert len(out) == 2


def test_multi_char_names_never_merge():
    """多字记录已经是完整词，不能再互相拼接。"""
    recs = [
        {"handle": "1", "layer": "FF", "text": "男厕", "x": 0.0, "y": 0.0, "h": 450.0, "rot": 0.0},
        {"handle": "2", "layer": "FF", "text": "女厕", "x": 1400.0, "y": 0.0, "h": 450.0, "rot": 0.0},
    ]
    out = merge_spaced_words(recs)
    assert len(out) == 2


# --------------------------------------------------------------------------- 柱
def _col_snapshot(lines):
    return {"entities": [{"type": "LINE", "layer": "COLUMN", "start": a, "end": b,
                          "handle": f"H{i}"} for i, (a, b) in enumerate(lines)]}


def test_rect_with_missing_edge_is_recovered():
    """三条边的柱（贴墙柱常见）也要认出来，标 edges=3。"""
    snap = _col_snapshot([
        ([0, 0], [800, 0]),
        ([0, 800], [800, 800]),
        ([0, 0], [0, 800]),          # 只画了一条竖边
    ])
    rects = EL._rects_from_lines(snap, {"COLUMN"},
                                 short_min=270, short_max=1800, long_max=2700)
    assert len(rects) == 1
    assert rects[0]["edges"] == 3
    assert rects[0]["w"] == pytest.approx(800.0)


def test_full_four_edge_rect_is_preferred():
    snap = _col_snapshot([
        ([0, 0], [800, 0]), ([0, 800], [800, 800]),
        ([0, 0], [0, 800]), ([800, 0], [800, 800]),
    ])
    rects = EL._rects_from_lines(snap, {"COLUMN"},
                                 short_min=270, short_max=1800, long_max=2700)
    assert len(rects) == 1
    assert rects[0]["edges"] == 4


def test_oversized_rect_is_not_a_column():
    """3.3m × 2.4m 的房间级矩形不是柱。"""
    snap = _col_snapshot([
        ([0, 0], [3300, 0]), ([0, 2400], [3300, 2400]),
        ([0, 0], [0, 2400]), ([3300, 0], [3300, 2400]),
    ])
    rects = EL._rects_from_lines(snap, {"COLUMN"},
                                 short_min=270, short_max=1800, long_max=2700)
    assert not rects


def test_elongated_pier_is_labelled_not_dropped():
    """长条形（贴墙垛）保留为柱类构件，但标 elongated，不按方柱计。"""
    cands = [{"outline": [[0, 0], [2315, 0], [2315, 200], [0, 200]],
              "bbox": [0, 0, 2315, 200], "w": 2315.0, "h": 200.0,
              "center": [1157.5, 100.0], "method": "lines", "srcs": []}]
    out = EL.fuse_columns(cands, axis=((), ()),
                          scale={"thickness": 200, "door_w": 900,
                                 "text_h": 375, "bay": 8100})["columns"]
    assert len(out) == 1
    assert out[0]["section_type"] == "elongated"
    assert out[0]["status"] != "confirmed"


# --------------------------------------------------------------------------- 参数出处约束
#: 「不许再拍数字」的机械保证。
#:
#: 规则只有一条：识别器里所有阈值都必须来自 :mod:`dwg_reader.recognition.scale`
#: 的 ``STRUCTURE`` 表（无量纲倍率）× 图内基准量；除 ``scale.py`` 之外的模块
#: **不允许新增**模块级数值常量。
#:
#: 历史遗留的 88 个常量冻结在 ``bare_thresholds_baseline.json`` 里，只准减少、
#: 不准增加。新增一个 -> 单测直接红，并提示改走 STRUCTURE。
_LINT = re.compile(r"^_?([A-Z][A-Z0-9_]{2,})\s*(?::[^=]+)?=\s*\[?\(?\s*(-?[0-9]+\.?[0-9]*)")
_LINT_UNIT_HINTS = ("MM", "TOL", "GAP", "DIST", "LEN", "AREA", "RAD", "DEG",
                    "SIZE", "SLACK", "MARGIN", "RES", "NEAR", "ADJ",
                    "OVERLAP", "PAD", "SPAN", "DEPTH")


#: 逻辑预算，不是几何阈值：批大小 / 嵌套层数 / 单个块收录上限。
#: 它们的共同点是**永远不与坐标比较**（单位是"条/层/个"），所以不进入毫米阈值
#: 基线；但仍然必须在这里点名登记——否则这个口子会成为塞真阈值的后门。
_LINT_BUDGET_OK = {
    ("llm_review.py", "BATCH"),
    ("snapshot.py", "BLOCK_NEST_DEPTH"),
    ("snapshot.py", "MAX_BLOCK_ENTS"),
    # 栅格连通域检验的离散化预算：单元数上限 / 墙厚的几分之一。
    # 前者是"格数"，后者是比例，两者都不与坐标比较，也不含毫米口径。
    ("walls_net.py", "MAX_CELLS"),
    ("walls_net.py", "RES_SLICE"),
}


def _collect_threshold_constants(root):
    out = {}
    for path in sorted(pathlib.Path(root).glob("*.py")):
        if path.name == "scale.py":          # 唯一允许写常数的模块
            continue
        names = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith((" ", "\t")) or line.lstrip().startswith("#"):
                continue
            g = _LINT.match(line.strip())
            if not g:
                continue
            name, raw = g.group(1), g.group(2)
            try:
                value = abs(float(raw))
            except ValueError:
                continue
            if any(k in name for k in _LINT_UNIT_HINTS) or value >= 30:
                names.append(name)
        if names:
            out[path.name] = sorted(set(names))
    return out


def test_no_new_bare_thresholds():
    """不许新增拍脑袋的阈值常量——必须走 STRUCTURE 表 × 图内基准量。"""
    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "dwg_reader" / "recognition"
    baseline_path = pathlib.Path(__file__).parent / "bare_thresholds_baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    current = _collect_threshold_constants(root)

    current = {m: [n for n in names if (m, n) not in _LINT_BUDGET_OK]
               for m, names in current.items()}
    current = {m: names for m, names in current.items() if names}

    added = {}
    for module, names in current.items():
        extra = sorted(set(names) - set(baseline.get(module) or ()))
        if extra:
            added[module] = extra
    assert not added, (
        "检测到新增的裸阈值常量，请改成 scale.STRUCTURE 里的无量纲倍率 × 图内基准量："
        + json.dumps(added, ensure_ascii=False))

    total_now = sum(len(v) for v in current.values())
    total_base = sum(len(v) for v in baseline.values())
    assert total_now <= total_base, (
        f"遗留阈值常量只准减少：基线 {total_base}，现在 {total_now}")


def test_structure_table_covers_every_threshold():
    """thresholds() 的每一项都必须能追到 STRUCTURE 或基准量本身。

    做法：把基准量整体乘 2，凡是"来自 STRUCTURE"的阈值必须同比例变化（面积量
    四倍）。任何一项岿然不动，说明它其实是个写死的毫米数，只是躲在函数体里。
    """
    basis = {"thickness": 200.0, "door_w": 900.0, "text_h": 350.0,
             "bay": 6000.0, "noise": 2.0}
    dou = {k: v * 2 for k, v in basis.items()}
    a, b = SC.thresholds(basis), SC.thresholds(dou)
    # 这些是"基准量本身"，应当逐项等值放大
    for key in ("thickness", "door_w", "text_h", "bay", "noise"):
        assert b[key] == pytest.approx(a[key] * 2), key
    # 线性量
    for key in ("gap_min", "gap_max", "col_short_max", "col_long_max",
                "split_res", "same_name_adj", "seal_max", "col_short_min"):
        assert b[key] == pytest.approx(a[key] * 2, rel=0.02), key
    # 图里量到了真实洞口宽度分布时，下限走分布、不走比率（这是刻意的优先级）
    # 面积量（二次）
    assert b["face_min_area"] == pytest.approx(a["face_min_area"] * 4, rel=0.02)
