"""模式 B（模型只能否决）的边界测试。

这里只测**纪律**，不测模型效果：模型说什么不重要，重要的是它**不能**做什么。
每一条断言都对应一个"越权"路径：

* 模型想新增候选 → 没有接口；
* 模型想提升置信（keep） → 与 unsure 同一分支；
* 模型想给几何（坐标/尺寸/阈值） → 整条记录丢弃；
* 模型给未知 id → 忽略；
* 没有 key → 与不接模型**逐字节一致**。

全部为纯单元测试：不联网、不读图纸、不调用 run_recognition。
"""
from __future__ import annotations

from dwg_reader.recognition import llm_review as LR


def _door(handle, width=900.0, **kw):
    item = {"handle": handle, "width": width, "thickness": 200.0,
            "methods": ["block"], "block": "$DorLib2D$00000001"}
    item.update(kw)
    return item


def _window(handle, width=1500.0, **kw):
    item = {"handle": handle, "width": width, "thickness": 200.0,
            "methods": ["block"], "block": "$WinLib2D$00000001"}
    item.update(kw)
    return item


# --------------------------------------------------------------- 无 key：完全不变
def test_no_api_key_is_noop():
    doors = [_door("D1"), _door("D2")]
    windows = [_window("W1")]
    kept_d, kept_w, stats = LR.review_openings(doors, windows, api_key=None)
    assert kept_d == doors and kept_w == windows
    assert stats == {"status": "unavailable"}

    cols = [{"handle": "C1", "w": 400.0, "h": 400.0, "method": "block"}]
    kept_c, cstats = LR.reject_columns(cols, api_key=None)
    assert kept_c == cols
    assert cstats == {"status": "unavailable"}


# --------------------------------------------------------------- reject：只做减法
def test_reject_only_removes_rejected(monkeypatch):
    doors = [_door("D1"), _door("D2")]
    windows = [_window("W1"), _window("W2")]
    monkeypatch.setattr(LR, "review", lambda cases, **kw: {
        "door:D2": {"verdict": "reject", "reason": "不在墙缺口上"},
        "window:W1": {"verdict": "reject", "reason": "长宽比像家具"},
    })
    kept_d, kept_w, stats = LR.review_openings(doors, windows, api_key="k")

    assert [d["handle"] for d in kept_d] == ["D1"]
    assert [w["handle"] for w in kept_w] == ["W2"]
    assert stats["rejected_doors"] == 1 and stats["rejected_windows"] == 1
    reasons = {r["id"]: r["reason"] for r in stats["rejected"]}
    assert reasons == {"door:D2": "不在墙缺口上", "window:W1": "长宽比像家具"}
    # 被否决的不删除、留痕：句柄仍在报告里
    assert {r["handle"] for r in stats["rejected"]} == {"D2", "W1"}


# --------------------------------------------------------------- keep/unsure：零效果
def test_keep_and_unsure_change_nothing(monkeypatch):
    doors = [_door("D1", evidence_state="strong")]
    windows = [_window("W1")]
    before_d, before_w = [dict(d) for d in doors], [dict(w) for w in windows]
    monkeypatch.setattr(LR, "review", lambda cases, **kw: {
        "door:D1": {"verdict": "keep", "reason": "像门"},
        "window:W1": {"verdict": "unsure", "reason": "看不清"},
    })
    kept_d, kept_w, stats = LR.review_openings(doors, windows, api_key="k")

    assert kept_d == before_d and kept_w == before_w
    # keep 绝不能变成"确认/提升"：不新增任何状态字段
    assert "review_state" not in kept_d[0]
    assert kept_d[0]["evidence_state"] == "strong"
    assert stats["rejected_doors"] == 0 and stats["rejected_windows"] == 0


# --------------------------------------------------------------- 模型不能新增候选
def test_model_cannot_add_candidates(monkeypatch):
    doors = [_door("D1")]
    windows = []
    monkeypatch.setattr(LR, "review", lambda cases, **kw: {
        "door:D1": {"verdict": "keep", "reason": "ok"},
        "door:D999": {"verdict": "keep", "reason": "模型臆想的门"},
        "window:W404": {"verdict": "keep", "reason": "模型臆想的窗"},
    })
    kept_d, kept_w, stats = LR.review_openings(doors, windows, api_key="k")
    assert len(kept_d) == 1 and kept_w == []
    assert stats["kept_doors"] + stats["kept_windows"] <= stats["cases"]


# --------------------------------------------------------------- 几何字段：整条丢弃
def test_geometry_fields_are_dropped():
    ids = {"door:D1", "door:D2", "door:D3", "door:D4"}
    raw = {"results": [
        {"id": "door:D1", "verdict": "reject", "reason": "ok"},
        {"id": "door:D2", "verdict": "reject", "x": 100.0},
        {"id": "door:D3", "verdict": "reject", "width": 900},
        {"id": "door:D4", "verdict": "maybe", "reason": "闭集外"},
    ]}
    got = LR._sanitize(raw, ids)
    assert set(got) == {"door:D1"}


def test_unknown_ids_are_ignored():
    got = LR._sanitize({"results": [{"id": "nope", "verdict": "reject"}]}, {"door:D1"})
    assert got == {}


# --------------------------------------------------------------- 事实包不含坐标
def test_case_facts_have_no_coordinates():
    items = [_door("D1", width=1500.0, center=[1.0, 2.0], insert=[3.0, 4.0],
                   axis=[[0.0, 0.0], [1.0, 1.0]], hinges=[[5.0, 6.0]],
                   wall_distance=0.0, space_connection=True)]
    case = dict(LR._facts(items[0], "door"), id=LR._case_id(items[0], 0, "door"))
    for forbidden in ("x", "y", "center", "insert", "axis", "hinges", "bbox"):
        assert forbidden not in case
    assert case["id"] == "door:D1"
    assert case["aspect"] == 7.5           # 1500 × 200
    assert case["block_family"] == "$DorLib2D$"


# --------------------------------------------------------------- 柱：同样只做减法
def test_columns_reject_only_subtracts(monkeypatch):
    cols = [{"handle": "C1", "w": 400.0, "h": 400.0, "method": "block",
             "status": "confirmed"},
            {"handle": "C2", "w": 400.0, "h": 400.0, "method": "polyline",
             "status": "confirmed"}]
    monkeypatch.setattr(LR, "review", lambda cases, **kw: {
        "column:C1": {"verdict": "keep", "reason": "近方形贴墙"},
        "column:C2": {"verdict": "reject", "reason": "孤立方块，无墙无轴网"},
    })
    kept, stats = LR.reject_columns(cols, api_key="k")
    assert [c["handle"] for c in kept] == ["C1"]
    assert stats["rejected"] == 1
    assert stats["rejected_items"][0]["handle"] == "C2"


# --------------------------------------------------------------- 缓存键含提示词版本
def test_cache_key_tracks_prompt_version():
    payload = [{"id": "door:D1", "kind": "door"}]
    first = LR._cache_key(payload)
    assert first == LR._cache_key(payload)
    original = LR.PROMPT_VERSION
    try:
        LR.PROMPT_VERSION = original + "-changed"
        assert LR._cache_key(payload) != first
    finally:
        LR.PROMPT_VERSION = original


# --------------------------------------------------------------- 截图级：只为拿不准的补图
def test_vision_goes_to_evidence_weak_not_text_strong(monkeypatch):
    """谁补截图按证据强度，不按文字结论：强证据的 keep 不补，弱证据的才补。"""
    strong = _door("D1", on_wall_gap=True)          # 落墙缺口，强证据
    weak_reject = _door("D2")                        # 没证据，文字还判了 reject
    weak_unsure = _door("D3")                        # 没证据，文字拿不准
    items = [strong, weak_reject, weak_unsure]
    text_verdicts = {"door:D1": {"verdict": "keep", "reason": "ok"},
                     "door:D2": {"verdict": "reject", "reason": "事实像家具"},
                     "door:D3": {"verdict": "unsure", "reason": "拿不准"}}
    monkeypatch.setattr(LR, "_post", lambda *a, **kw: json_dumps(text_verdicts))
    rendered = []
    monkeypatch.setattr(LR, "_render_case",
                        lambda item, bm: rendered.append(item["handle"]) or None)
    out = LR.review([{"id": f"door:{d['handle']}", "facts": LR._facts(d, "door"),
                      "_item": d} for d in items],
                    api_key="k", base_map={"segments": [], "texts": []})
    assert rendered == ["D2", "D3"]          # 强证据的 D1 不补图
    assert out["door:D1"]["verdict"] == "keep"


def test_vision_overrides_text_reject(monkeypatch):
    """文字级说 reject、截图级看得清说是 keep → 以截图级为准。"""
    item = _door("D1")
    monkeypatch.setattr(LR, "_post", lambda *a, **kw: json_dumps(
        {"door:D1": {"verdict": "reject", "reason": "文字级误判", "stage": "text"}})
        if not kw.get("image_b64") else
        json_dumps({"door:D1": {"verdict": "keep", "reason": "截图看得清是门"}}))
    monkeypatch.setattr(LR, "_render_case", lambda item, bm: _tmp_png())
    out = LR.review([{"id": "door:D1", "facts": LR._facts(item, "door"),
                      "_item": item}],
                    api_key="k", base_map={"segments": [], "texts": []},
                    cache_dir=str(_tempfile.mkdtemp()))
    assert out["door:D1"]["verdict"] == "keep"
    assert out["door:D1"]["stage"] == "vision"


def test_vision_budget_is_respected(monkeypatch, tmp_path):
    """每张图最多补 MAX_IMAGES 张，超出部分不再花图。"""
    items = [_door(f"D{i}") for i in range(60)]

    def _post_stub(prompt, *a, **kw):
        if not kw.get("image_b64"):
            return json_dumps({f"door:{d['handle']}": {"verdict": "unsure",
                                                       "reason": "x"}
                               for d in items})
        m = _re.search(r'(door:D\d+)', prompt)
        return json_dumps({m.group(1): {"verdict": "keep", "reason": "看清"}})

    monkeypatch.setattr(LR, "_post", _post_stub)
    monkeypatch.setattr(LR, "MAX_IMAGES", 5)
    rendered = []
    monkeypatch.setattr(LR, "_render_case",
                        lambda item, bm: rendered.append(item["handle"]) or _tmp_png())
    LR.review([{"id": f"door:{d['handle']}", "facts": LR._facts(d, "door"),
                "_item": d} for d in items],
              api_key="k", base_map={"segments": [], "texts": []},
              cache_dir=str(tmp_path))
    assert len(rendered) == 5


# --------------------------------------------------------------- 底图
def test_basemap_from_snapshot_collects_native_lines():
    snap = {"entities": [
        {"type": "LINE", "start": [0, 0], "end": [100, 0]},
        {"type": "LWPOLYLINE", "points": [[0, 0], [10, 0], [10, 10]], "closed": True},
        {"type": "TEXT", "insert": [5, 5], "text": "办公室"},
        {"type": "INSERT", "name": "$WinLib2D$00000001"},   # 块本体不进底图
    ]}
    base = LR._basemap_from_snapshot(snap)
    assert len(base["segments"]) == 4      # 1 直线 + 3 段闭合多段线
    assert base["texts"] == [[5.0, 5.0, "办公室"]]


# --------------------------------------------------------------- 工具
import json as _json
import os as _os
import re as _re
import tempfile as _tempfile


def json_dumps(verdicts):
    """把 {id: {...}} 包装成模型返回的 JSON 文本。"""
    return _json.dumps({"results": [{"id": k, **v} for k, v in verdicts.items()]},
                       ensure_ascii=False)


def _tmp_png():
    """一张最小的合法 PNG，供 _render_case 的桩返回。"""
    import base64
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    fd, path = _tempfile.mkstemp(suffix=".png")
    with _os.fdopen(fd, "wb") as fh:
        fh.write(png)
    return path


def test_vision_goes_to_weak_evidence_not_text_verdict(monkeypatch, tmp_path):
    """有硬证据（落墙缺口/有开启弧）的文字级 keep 不补图；没硬证据的即使文字级 keep 也补。"""
    strong = _door("D1", on_wall_gap=True)          # 硬证据，文字 keep -> 不补图
    weak = _door("D2")                               # 无证据，文字 keep -> 补图
    monkeypatch.setattr(LR, "_post", lambda *a, **kw: json_dumps(
        {"door:D1": {"verdict": "keep", "reason": "ok"},
         "door:D2": {"verdict": "keep", "reason": "事实看不出问题"}}))
    rendered = []
    monkeypatch.setattr(LR, "_render_case",
                        lambda item, bm: rendered.append(item["handle"]) or _tmp_png())
    out = LR.review([{"id": f"door:{d['handle']}", "facts": LR._facts(d, "door"),
                      "_item": d} for d in (strong, weak)],
                    api_key="k", base_map={"segments": [], "texts": []},
                    cache_dir=str(tmp_path))
    assert rendered == ["D2"]         # 只有证据弱的补了图
    assert out["door:D2"]["stage"] == "vision"


# --------------------------------------------------------------- 否决分两档：形态 vs 上下文
def test_reject_with_context_becomes_conflict_not_deleted(monkeypatch, tmp_path):
    """模型看截图能否决"长什么样"，不能否决"它在哪儿"：
    带贴墙/洞口/两侧空间证据的候选，reject → conflict + needs_review，不删。"""
    with_wall = _door("D1", touches_wall=True)          # 有上下文证据
    free = _door("D2")                                    # 只有形态证据
    monkeypatch.setattr(LR, "_post", lambda *a, **kw: json_dumps(
        {"door:D1": {"verdict": "reject", "reason": "截图里像家具"},
         "door:D2": {"verdict": "reject", "reason": "截图里像家具"}}))
    monkeypatch.setattr(LR, "_render_case", lambda item, bm: None)   # 不要截图级
    kept_d, kept_w, stats = LR.review_openings([with_wall, free], [], api_key="k",
                                               snapshot=None, vision=False)
    # 上下文证据在 → 冲突降级，没删
    assert with_wall in kept_d
    assert with_wall["review_state"] == "conflict"
    assert with_wall["needs_review"] is True
    assert "touches_wall" in with_wall["review_note"]
    # 只有形态证据 → 否决成立
    assert free not in kept_d
    assert stats["rejected_doors"] == 1
    assert len(stats["conflicts"]) == 1
    assert stats["conflicts"][0]["handle"] == "D1"


def test_context_keys_only_when_truthy():
    """没有位置证据的候选不制造"假上下文"。"""
    assert LR._context_of({"on_wall_gap": None, "touches_wall": False,
                           "space_connection": None}) == []
    assert LR._context_of({"on_wall_gap": True}) == ["on_wall_gap"]


# --------------------------------------------------------------- 冲突项必须落进 undecided
def test_conflict_record_lands_in_undecided():
    """模型否决但位置证据冲突的记录：不能算 confirmed，必须进 undecided 可人工复核。"""
    from dwg_reader.recognition import finalstate as FS

    base = {"id": "COL-01", "category": "column", "confidence": 0.9,
            "srcs": [{"handle": "A1"}, {"handle": "A2"}],
            "methods": ["block", "polyline"], "faces": 2}
    conflict = dict(base, review_state="conflict", evidence_state="weak",
                    needs_review=True,
                    review_note="模型按截图判不像column，但持有上下文证据：touches_wall")
    clean = dict(base, id="COL-02")          # 对照组必须换 id，否则字典推导会互相覆盖
    state = {"walls": [], "columns": [conflict, clean], "doors": [], "windows": []}
    out = FS.calibrate(state)

    col = {c["id"]: c for c in out["columns"]}
    assert col["COL-01"]["status"] == "uncertain"        # 冲突项不确认
    assert col["COL-01"]["needs_review"] is True
    reasons = {u["id"]: u["reason"] for u in out["undecided"]}
    assert reasons.get("COL-01") == "模型按截图否决，但与位置证据冲突"
    assert out["calibration"]["stats"]["confirmed"] >= 1  # 对照项仍能确认
