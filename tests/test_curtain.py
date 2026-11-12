"""幕墙带（共面三线归并）判据：幕墙算墙的一种，但口径必须是"带"而不是"半带"。"""
import pytest

from dwg_reader.recognition import arcwalls as AW
from dwg_reader.recognition import curtain as CT


def _line(y, x0, x1, layer="WALL", handle=None):
    return {"type": "LINE", "layer": layer, "start": [x0, y],
            "end": [x1, y], "handle": handle or f"L{y}"}


def _snap(*recs):
    return {"entities": list(recs), "derived": []}


def _band_triplet(middle="CURTWALL", gap=100.0, length=20000.0, y0=0.0):
    return _snap(_line(y0, 0, length, "WALL", "A"),
                 _line(y0 + gap, 0, length, middle, "B"),
                 _line(y0 + 2 * gap, 0, length, "WALL", "C"))


def test_three_coface_lines_merge_into_one_band():
    bands, diag = CT.detect(_band_triplet(), layers=["WALL", "CURTWALL"],
                            thickness=200.0, wall_layers=["WALL", "CURTWALL"])
    assert len(bands) == 1
    band = bands[0]
    # 厚度取外皮到内皮，不是相邻两条的间距
    assert band["thickness"] == pytest.approx(200.0)
    assert band["role"] == "curtain"
    assert band["band_lines"] == 3
    assert band["band_pitch"] == pytest.approx(100.0)
    assert band["length"] == pytest.approx(20000.0)
    assert diag["bands"] == 1


def test_structural_wall_with_centerline_is_not_curtain():
    """300 实体墙居中画了中线：三条线等距、也共覆盖，但全在墙层 -> 不是幕墙。"""
    bands, diag = CT.detect(_band_triplet(middle="WALL", gap=150.0),
                            layers=["WALL"], thickness=200.0, wall_layers=["WALL"])
    assert bands == []
    assert diag["rejected"].get("中间线不属于幕墙层") == 1


def test_uneven_gaps_are_not_a_band():
    snap = _snap(_line(0, 0, 20000, "WALL", "A"),
                 _line(100, 0, 20000, "CURTWALL", "B"),
                 _line(300, 0, 20000, "WALL", "C"))
    bands, _ = CT.detect(snap, layers=["WALL", "CURTWALL"],
                         thickness=200.0, wall_layers=["WALL", "CURTWALL"])
    assert bands == []


def test_band_extent_is_defined_by_the_inner_line():
    """带内线只画了一小段时，带就只到那一小段：外皮更长，那是普通墙的领地。"""
    snap = _snap(_line(0, 0, 40000, "WALL", "A"),
                 _line(100, 0, 6000, "CURTWALL", "B"),
                 _line(200, 0, 40000, "WALL", "C"))
    bands, _ = CT.detect(snap, layers=["WALL", "CURTWALL"],
                         thickness=200.0, wall_layers=["WALL", "CURTWALL"])
    assert len(bands) == 1
    assert bands[0]["length"] == pytest.approx(6000.0)
    assert bands[0]["start"][0] == pytest.approx(0.0)


def test_short_band_rejected():
    """长度不足 15 倍墙厚：那是门窗套线脚，不是幕墙。"""
    bands, _ = CT.detect(_band_triplet(length=2000.0), layers=["WALL", "CURTWALL"],
                         thickness=200.0, wall_layers=["WALL", "CURTWALL"])
    assert bands == []


def test_half_band_wall_is_suppressed():
    """配对器拆出来的 100 半带墙要被带取代；真正的 200 墙不动。"""
    band = {"orient": "H", "center": 100.0, "thickness": 200.0,
            "start": [0, 100], "end": [20000, 100]}
    half = {"orient": "H", "center": 50.0, "thickness": 100.0,
            "start": [0, 50], "end": [20000, 50]}
    real = {"orient": "H", "center": 9000.0, "thickness": 200.0,
            "start": [0, 9000], "end": [20000, 9000]}
    kept, dropped = CT.suppress_half_bands([half, real], [band])
    assert dropped == 1
    assert kept == [real]


def _arc(cx, cy, r, a0, a1, layer="WALL", handle=None):
    return {"type": "ARC", "layer": layer, "center": [cx, cy], "radius": r,
            "start_angle": a0, "end_angle": a1, "handle": handle or f"A{r}"}


def test_curved_curtain_wall_is_a_200_band():
    """三层同心弧等距 + 中间是幕墙层：一条 200 的弧形幕墙，不是两道 100。"""
    snap = _snap(_arc(0, 0, 1000, 0, 90, "WALL", "A"),
                 _arc(0, 0, 1100, 0, 90, "CURTWALL", "B"),
                 _arc(0, 0, 1200, 0, 90, "WALL", "C"))
    walls, _ = AW.detect(snap, layers=["WALL", "CURTWALL"], thickness=200.0,
                         is_curtain=CT.is_curtain_layer)
    assert len(walls) == 1
    wall = walls[0]
    assert wall["role"] == "curtain" and wall["arc"] is True
    assert wall["thickness"] == pytest.approx(200.0)
    assert wall["curve"]["inner_radius"] == 1000.0
    assert wall["curve"]["outer_radius"] == 1200.0


def test_curved_three_arcs_without_curtain_layer_keep_old_behaviour():
    """没有幕墙图层时，三层弧仍按原判据：只留相邻一道 100，行为不变。"""
    snap = _snap(_arc(0, 0, 1000, 0, 90, "WALL", "A"),
                 _arc(0, 0, 1100, 0, 90, "WALL", "B"),
                 _arc(0, 0, 1200, 0, 90, "WALL", "C"))
    walls, _ = AW.detect(snap, layers=["WALL"], thickness=200.0,
                         is_curtain=CT.is_curtain_layer)
    assert len(walls) == 1
    assert walls[0]["thickness"] == pytest.approx(100.0)
    assert walls[0].get("role") is None
