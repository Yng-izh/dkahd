"""墙的图层判据不许看图层名：形状说了算。

背景（实测，会所/住院楼各一对"图层清晰 vs 图层混乱"图纸）：

* 住院楼清晰版 WALL 层 1397 条墙线，混乱版只剩 499 条，另外 909 条被整批搬到
  家具1 层——两个面一起搬走，所以**配对关系一根没断**（可配对墙面 658.6m vs
  658.4m）。丢墙的原因不是配不上对，是"只有 WALL 层算墙"这道硬门槛。
* 旧判据 ``long_pair_rate``（两面线对齐重叠 ≥1500mm）把清晰版的 WALL 层判成
  0.30 分（"看不出"），把楼梯踏步层判成 0.51 分（"是墙"）——墙实际上是靠图层名
  撑着的，名字一乱就整层丢。

这组用例锁住新判据的三件事：墙层分数高、冒充墙的东西分数低、结论与层名无关。
"""
from __future__ import annotations

import pytest

from dwg_reader.recognition import layer_roles_infer as LRI
from dwg_reader.recognition.scale import DEFAULT_THICKNESS, STRUCTURE

#: 指纹的完整字段（``_geom_scores`` 会读全部角色用到的项，缺一个就 KeyError）
BASE = {
    "n": 600, "line_ratio": 0.99, "text_ratio": 0.0, "closed_ratio": 0.0,
    "arc_ratio": 0.0, "fill_ratio": 0.0, "long_ratio": 0.3, "cross_span": 1.0,
    "pair_rate": 0.4, "short_pair_rate": 0.2, "long_pair_rate": 0.1,
    "on_wall_ratio": 0.2, "thickness_mm": 200.0, "thickness_concentration": 0.6,
    "block_ratio": 0.0, "pair_wall_share": 0.0, "pair_aspect": 0.0,
    "pair_density": 0.0, "pair_thickness_mm": 200.0, "annotation_ratio": 0.0,
}


def _fp(**over) -> dict:
    return dict(BASE, **over)


#: 真墙层（实测会所清晰版 WALL：细长 13.9 倍、成对占比 0.287、密度 2.16）
WALL_LAYER = _fp(n=660, closed_ratio=0.03, pair_wall_share=0.287,
                 pair_aspect=13.9, pair_density=2.164)
#: 楼梯踏步层：成对长度占比很高（1.3），但一点都不细长（2.4 倍）
STAIR_LAYER = _fp(n=322, pair_rate=0.9, pair_wall_share=1.319,
                  pair_aspect=2.4, pair_density=4.949)
#: 柱层：方框，细长比≈1，几乎没有成对墙面
COLUMN_LAYER = _fp(n=445, pair_wall_share=0.005, pair_aspect=11.0,
                   pair_density=0.018)
#: 尺寸标注层：双线长得和墙一模一样，但整层是标注
DIM_LAYER = _fp(n=400, pair_wall_share=0.283, pair_aspect=15.7,
                pair_density=1.143, annotation_ratio=0.335)


def _score(fingerprint: dict) -> float:
    return LRI._geom_scores(fingerprint)["wall"]


def test_wall_layer_scores_high_without_any_name():
    """真墙层的几何分数必须能独立过接受线（0.50）。"""
    assert _score(WALL_LAYER) >= 0.50


def test_stair_and_column_do_not_look_like_walls():
    """踏步（占比高但不细长）、柱（方框）都不许被判成墙。"""
    assert _score(STAIR_LAYER) < 0.30
    assert _score(COLUMN_LAYER) < 0.10


def test_dimension_layer_is_vetoed_even_though_geometry_matches():
    """标注层的双线和墙同形，只能靠实体构成否决。"""
    scores = LRI._geom_scores(DIM_LAYER)
    vetoed = LRI._veto(DIM_LAYER, scores)
    assert "wall" in vetoed


def test_thin_pairs_are_not_walls():
    """比常规墙厚一半还薄的平行线对（踏步 30、窗框 70、管道 70）不是墙。"""
    thin = dict(WALL_LAYER, pair_thickness_mm=0.4 * DEFAULT_THICKNESS)
    assert "wall" in LRI._veto(thin, LRI._geom_scores(thin))


def test_geometry_can_override_a_wrong_layer_name():
    """名字说"家具"、里面全是墙线时，几何一票定音（只对墙生效）。"""
    fps = {"家具1": dict(WALL_LAYER)}
    prior = {"家具1": {"furniture": 0.95}}
    rec = LRI.infer_roles(fps, prior=prior)["家具1"]
    assert rec["roles"] == ["wall"], rec
    assert rec["why"] in ("geometry_override", "agreed", "fused")


def test_override_does_not_apply_to_other_roles():
    """门/窗/柱不享受这条特赦：长相很容易被同名层骗，仍以名字为准。"""
    fps = {"门": dict(WALL_LAYER, block_ratio=0.9, arc_ratio=0.8)}
    prior = {"门": {"ignore": 0.95}}
    rec = LRI.infer_roles(fps, prior=prior)["门"]
    assert rec["roles"] != ["door"], rec


def test_wall_ratios_are_dimensionless():
    """新加的倍率全部无量纲，且都在合法区间里（不许偷偷塞毫米数）。"""
    for key in ("layer_strip_ratio", "layer_aspect_lo", "layer_aspect_hi",
                "layer_share_lo", "layer_share_hi", "layer_density_lo",
                "layer_density_hi", "layer_peak_share", "layer_geom_force",
                "layer_geom_margin", "layer_annotation_veto",
                "wall_min_thickness_ratio"):
        assert 0.0 < float(STRUCTURE[key]) <= 20.0, key
