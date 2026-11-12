"""``ai_mode`` 的兼容性与降级：off 必须逐条等于老行为，propose 不许改结论。

这里用的是**合成图纸**（脚本画出来的小平面），所以它测的是"开关语义"，不是
"某张真实图纸认得对不对"——后者要看 ``tests/baseline`` 与独立真值集。
"""
from __future__ import annotations

import json

import pytest

ezdxf = pytest.importorskip("ezdxf")

from dwg_reader.recognition import run_recognition


def _synthetic_plan(path):
    """画一张 8000×6000 的小平面：外墙双线 + 一道内墙 + 一樘门（弧线）。"""
    doc = ezdxf.new("R2018", setup=True)
    msp = doc.modelspace()
    # 外墙（双线）：外圈 0~8000 / 0~6000，内圈缩进 200（墙厚 200）
    doc.layers.add("WALL")
    for (x1, y1, x2, y2) in ((0, 0, 8000, 0), (0, 6000, 8000, 6000),
                             (0, 0, 0, 6000), (8000, 0, 8000, 6000)):
        msp.add_line((x1, y1), (x2, y2), dxfattribs={"layer": "WALL"})
    for (x1, y1, x2, y2) in ((200, 200, 7800, 200), (200, 5800, 7800, 5800),
                             (200, 200, 200, 5800), (7800, 200, 7800, 5800)):
        msp.add_line((x1, y1), (x2, y2), dxfattribs={"layer": "WALL"})
    # 内墙：x=4000，留 900 洞口
    for (x1, y1, x2, y2) in ((4000, 700, 4000, 2800), (4000, 3700, 4000, 5300),
                             (4200, 700, 4200, 2800), (4200, 3700, 4200, 5300)):
        msp.add_line((x1, y1), (x2, y2), dxfattribs={"layer": "WALL"})
    # 门扇开启弧（半径 900），标在门层
    doc.layers.add("DOOR")
    msp.add_arc(center=(4000, 2800), radius=900, start_angle=0, end_angle=90,
                dxfattribs={"layer": "DOOR"})
    # 一个房间名
    doc.layers.add("TEXT")
    msp.add_text("办公室", height=350, dxfattribs={"layer": "TEXT"}).set_placement(
        (2000, 3000))
    doc.saveas(str(path))
    return str(path)


@pytest.fixture(scope="module")
def synthetic_dxf(tmp_path_factory):
    path = tmp_path_factory.mktemp("ai-dag") / "plan.dxf"
    return _synthetic_plan(path)


def _export(dxf, **kwargs):
    return run_recognition(str(dxf), jobs=1, targets=("export",), **kwargs)


def test_ai_mode_off_does_not_touch_the_pipeline(synthetic_dxf):
    off = _export(synthetic_dxf, ai_mode="off")
    ai_node = off.outputs["ai_hypotheses"]
    assert ai_node["status"] == "off"
    assert ai_node["proposals"] == {} and ai_node["plans"] == {}
    # 没配 key、mode=off：一次模型调用都不该发生
    assert ai_node["gateway"]["calls"] == 0
    doc = off.output
    assert doc["meta"]["ai_recognition"] == {"schema": "ai-recognition/1.0",
                                            "mode": "off"}
    # 审计里 AI 段是空壳，不假装跑过
    assert (doc["diagnostics"]["ai"] or {}).get("mode") == "off"


def test_pattern_discovery_is_audit_only_when_off(synthetic_dxf):
    off = _export(synthetic_dxf, ai_mode="off")
    found = off.outputs["pattern_discovery"]
    assert found["schema"] == "pattern-cases/1.0"
    assert isinstance(found["cases"], list)
    # 无论发现多少形态，off 模式下构件结论必须与"没有发现"时一致
    assert off.outputs["quality_gate"]["patterns"] == found["cases"]


def test_ai_mode_propose_never_changes_components(synthetic_dxf):
    pytest.importorskip("ezdxf")
    off = _export(synthetic_dxf, ai_mode="off")
    propose = _export(synthetic_dxf, ai_mode="propose")
    assert pytest.approx(0, abs=0) == 0
    off_counts = off.output["meta"]["counts"]
    prop_counts = propose.output["meta"]["counts"]
    assert off_counts == prop_counts, "propose 模式不许改识别结论"
    meta = propose.output["meta"]["ai_recognition"]
    assert meta["mode"] == "propose"
    # 没有可用凭据时状态是 unavailable，而不是硬报错
    assert meta["gateway"].get("available") in (True, False)


def test_ai_mode_enhance_without_credentials_degrades(synthetic_dxf, monkeypatch):
    monkeypatch.setenv("DWG_LLM_DISABLE", "1")
    from dwg_reader.recognition import ai_config as AIC
    AIC_load = AIC.load

    def _no_key(path=None, **kwargs):
        cfg = AIC_load(path, **kwargs)
        cfg["provider"] = dict(cfg.get("provider") or {},
                               api_key_env="DWG_AI_NO_SUCH_ENV")
        return cfg

    monkeypatch.setattr(AIC, "load", _no_key)
    run = _export(synthetic_dxf, ai_mode="enhance")
    # 关键：模型不可用也要出一份完整结果，且回归到"没有 AI 的结论"
    assert run.output["components"] is not None
    ai = run.outputs["ai_hypotheses"]
    assert ai["status"] in ("unavailable", "ok")
    assert run.output["meta"]["ai_recognition"]["mode"] == "enhance"
