"""v2 编排层（多路径候选 + 全局联合求解）的不变量测试。

测的是"架构承诺"，不是"某张图认对了几个"：

* **统一几何表示**：每个图元一个稳定 uid，handle 可反查；多尺度区域共享同一套 id；
* **关系图**：关系是闭集；平行的、贴合的、同族的都得连上；
* **语法**：原语/关系/锚点是闭集，坐标一出现就整份作废；
* **拟合**：几何只能由拟合器算出来；匹配不上要给出可读的失败码；
* **联合求解**：互斥/重复不许同选，宿主支持要加分，阶段化求解不许假装算过房间；
* **引擎**：规则/模式/AI 三条路各自出候选，去重只处理"同一个东西说两遍"；
* **可复现**：同一输入两次运行的 IR 指纹一致。
"""
from __future__ import annotations

import json

import pytest

from dwg_reader.recognition.v2 import cad_ir as IR
from dwg_reader.recognition.v2 import component_grammar as G
from dwg_reader.recognition.v2 import experiment_store as STORE
from dwg_reader.recognition.v2 import geometry_fitter as FITTER
from dwg_reader.recognition.v2 import geometry_graph as GG
from dwg_reader.recognition.v2 import global_resolver as RESOLVER
from dwg_reader.recognition.v2 import proposal_engines as ENGINES


# --------------------------------------------------------------------------- 夹具
def _snapshot():
    """一张 9000×7000 的小平面：外墙双线 + 一扇画出来的窗 + 两根方柱。"""
    ents = []
    for (x1, y1, x2, y2) in ((0, 0, 9000, 0), (0, 7000, 9000, 7000),
                             (0, 0, 0, 7000), (9000, 0, 9000, 7000),
                             (200, 200, 8800, 200), (200, 6800, 8800, 6800),
                             (200, 200, 200, 6800), (8800, 200, 8800, 6800)):
        ents.append({"handle": f"W{len(ents)}", "type": "LINE", "layer": "WALL",
                     "start": [x1, y1], "end": [x2, y2]})
    y0 = 3000.0
    for (x1, y1, x2, y2) in ((3000, y0, 4200, y0), (3000, y0 + 100, 4200, y0 + 100),
                             (3000, y0, 3000, y0 + 100), (4200, y0, 4200, y0 + 100)):
        ents.append({"handle": f"I{len(ents)}", "type": "LINE", "layer": "WIN",
                     "start": [x1, y1], "end": [x2, y2]})
    for k, ox in enumerate((1500.0, 7000.0)):
        ents.append({"handle": f"C{k}", "type": "LWPOLYLINE", "layer": "COL",
                     "closed": True,
                     "points": [[ox, 5000.0], [ox + 500.0, 5000.0],
                                [ox + 500.0, 5500.0], [ox, 5500.0]]})
    ents.append({"handle": "T1", "type": "TEXT", "layer": "TXT", "text": "办公室",
                 "insert": [2000.0, 4000.0], "height": 350})
    return {"entities": ents, "derived": []}


@pytest.fixture()
def ir():
    return IR.build(_snapshot(), scale={"thickness": 200.0, "noise": 2.0})


# --------------------------------------------------------------------------- IR
def test_ir_gives_every_entity_a_stable_uid(ir):
    assert ir["schema"] == "cad-ir/1.0"
    uids = [e["uid"] for e in ir["entities"]]
    assert len(uids) == len(set(uids))
    assert all(u.startswith("e:") for u in uids)
    # handle 可反查（关系和证据都靠它）
    assert IR.uids_for_handle(ir, "W0")
    assert IR.digest(ir) == IR.digest(IR.build(_snapshot(),
                                               scale={"thickness": 200.0,
                                                      "noise": 2.0}))


def test_ir_kinds_are_closed_and_normalized(ir):
    kinds = {e["kind"] for e in ir["entities"]}
    assert kinds <= set(IR.KINDS)
    assert {"segment", "poly", "text"} <= kinds
    for e in ir["entities"]:
        assert e["bbox"] and len(e["bbox"]) == 4
        assert e["source"]["handle"]


def test_ir_regions_are_multi_scale(ir):
    levels = {r["level"] for r in ir["regions"]}
    assert 0 in levels                       # 全局
    assert any(r["kind"] == "local" for r in ir["regions"])


# --------------------------------------------------------------------------- 关系图
def test_graph_relations_are_from_a_closed_set(ir):
    g = GG.build(ir)
    for edge in g["edges"]:
        assert edge["rel"] in GG.RELATIONS


def test_graph_links_parallel_and_touching_lines(ir):
    g = GG.build(ir)
    by_rel = g["stats"]["by_rel"]
    assert by_rel["parallel_to"] >= 2        # 双线墙的两面
    assert by_rel["touches"] >= 3            # 窗框四边首尾相接


def test_graph_query_is_read_only_and_bounded(ir):
    g = GG.Graph(GG.build(ir), ir)
    uid = ir["entities"][0]["uid"]
    loo = GG.query(g, uid, depth=1)
    assert loo["uid"] == uid
    assert isinstance(loo["relations"], dict)
    assert len(loo["edges"]) <= 24


# --------------------------------------------------------------------------- 语法
def test_grammar_vocabulary_is_closed():
    bad = {"category": "window", "parts": {"a": "magic_shape"},
           "relations": []}
    with pytest.raises(G.GrammarError) as exc:
        G.from_model(bad)
    assert exc.value.code == "UNKNOWN_PRIMITIVE"


def test_grammar_rejects_coordinates():
    bad = {"category": "window", "parts": {"a": "parallel_line_pair"},
           "relations": [], "bbox": [0, 0, 1, 1]}
    with pytest.raises(G.GrammarError) as exc:
        G.from_model(bad)
    assert exc.value.code == "MODEL_GEOMETRY"


def test_grammar_relations_must_reference_declared_parts():
    bad = {"category": "window", "parts": {"a": "parallel_line_pair"},
           "relations": [["a", "inside", "ghost"]]}
    with pytest.raises(G.GrammarError) as exc:
        G.from_model(bad)
    assert exc.value.code == "UNKNOWN_PART"


def test_builtin_templates_are_all_valid():
    for name, row in G.TEMPLATES.items():
        grammar = G.validate(dict(row))
        assert grammar["hash"], name
        assert grammar["parts"], name


def test_grammar_hash_is_content_addressed():
    a = G.validate(dict(G.TEMPLATES["window.parallel_frame"]))
    b = G.validate(dict(G.TEMPLATES["window.parallel_frame"]))
    assert a["hash"] == b["hash"]


# --------------------------------------------------------------------------- 拟合
def test_fitter_finds_the_drawn_window(ir):
    grammar = G.validate(dict(G.TEMPLATES["window.parallel_frame"]))
    res = FITTER.fit(grammar, ir, GG.build(ir))
    assert res["status"] == "ok"
    assert res["candidates"], "画出来的窗框应当被拟合出来"
    top = res["candidates"][0]
    assert top["category"] == "window"
    assert top["geometry"]["axis"], "窗的轴向必须由拟合器算出来"
    assert top["geometry"]["width_mm"] > 0
    assert top["uids"], "候选必须能追到原始图元 uid"


def test_fitter_reports_readable_failure(ir):
    grammar = G.validate({"category": "column", "subtype": "irregular_column",
                          "parts": {"body": "solid_quad"},
                          "relations": [], "fit_mode": "bbox_only",
                          "instances": "single"})
    res = FITTER.fit(grammar, ir, GG.build(ir), region={"bbox": [8000, 6000, 9000, 7000]})
    assert res["status"] in ("no_part", "no_relation", "empty_region")


def test_fitter_column_candidate_carries_outline(ir):
    grammar = G.validate(dict(G.TEMPLATES["column.solid_or_contour"]))
    res = FITTER.fit(grammar, ir, GG.build(ir))
    assert res["status"] == "ok"
    assert res["candidates"]
    assert res["candidates"][0]["geometry"].get("outline")


# --------------------------------------------------------------------------- 求解
def _hyp(category, bbox, *, score_source="rule", **kw):
    return ENGINES.hypothesis(category=category, subtype=None, source=score_source,
                             engine="test", region=None, bbox=bbox,
                             geometry=kw.pop("geometry", {"axis": [[0, 0], [1, 1]]}),
                             uids=kw.pop("uids", ()), score=kw.pop("score", 0.8),
                             evidence=kw.pop("evidence", {}))


def test_resolver_never_picks_mutually_exclusive_pair():
    door = _hyp("door", [0, 0, 900, 200], score=0.9)
    window = _hyp("window", [0, 0, 900, 200], score=0.85)
    res = RESOLVER.solve({"hypotheses": [door, window]}, thickness=200.0)
    cats = [h["category"] for h in res["selected"]]
    assert not ("door" in cats and "window" in cats)


def test_resolver_dedupes_same_category_overlap():
    a = _hyp("wall", [0, 0, 3000, 200], score=0.9)
    b = _hyp("wall", [0, 0, 3000, 200], score=0.7, score_source="pattern")
    res = RESOLVER.solve({"hypotheses": [a, b]}, thickness=200.0)
    assert len([h for h in res["selected"] if h["category"] == "wall"]) == 1


def test_resolver_prefers_hosted_opening():
    wall = _hyp("wall", [0, 0, 4000, 200], score=0.75)
    win = _hyp("window", [1000, 0, 1900, 200], score=0.85)
    res = RESOLVER.solve({"hypotheses": [wall, win]}, thickness=200.0)
    ids = {h["id"] for h in res["selected"]}
    assert wall["id"] in ids and win["id"] in ids


def test_staged_resolution_does_not_fake_rooms(ir):
    pool = ENGINES.generate(ir, GG.build(ir))
    res = RESOLVER.staged(pool, thickness=200.0)
    assert res["stages"]["spaces"]["status"] == "needs_geometry"
    assert res["needs"], "房间必须由墙网重算，不能凭空造"


# --------------------------------------------------------------------------- 引擎
def test_engines_pool_has_unified_schema(ir):
    graph = GG.build(ir)
    pool = ENGINES.generate(ir, graph,
                            walls=[{"id": "w1", "start": [0, 0], "end": [9000, 0],
                                    "thickness": 200, "handles": ["W0", "W4"]}],
                            columns=[{"id": "c1", "bbox": [1500, 5000, 2000, 5500],
                                      "w": 500, "h": 500, "handles": ["C0"]}])
    assert pool["stats"]["kept"] >= 2
    for hyp in pool["hypotheses"]:
        assert hyp["schema"] == ENGINES.HYP_SCHEMA
        assert hyp["source"] in ENGINES.SOURCES
        assert hyp["id"].startswith("hyp:")


def test_engines_dedupe_keeps_higher_score_and_records_alternative():
    a = _hyp("wall", [0, 0, 3000, 200], score=0.9)
    b = _hyp("wall", [0, 0, 3000, 200], score=0.4, score_source="ai")
    kept, dropped = ENGINES.dedupe([a, b])
    assert dropped == 1
    assert kept[0]["score"] == pytest.approx(0.9)
    assert kept[0]["alternatives"][0]["source"] == "ai"


# --------------------------------------------------------------------------- 实验库
def test_experiment_store_roundtrip(tmp_path):
    entry = {"ir_digest": "abc", "grammar_hashes": ["g1"], "objective": 1.5,
             "counts": {"wall": 3}}
    assert STORE.record(entry, directory=str(tmp_path))["ok"]
    rows = STORE.load(str(tmp_path))
    assert len(rows) == 1 and rows[0]["objective"] == 1.5
    assert STORE.best(directory=str(tmp_path))["objective"] == 1.5
    STORE.record(dict(entry, objective=2.5), directory=str(tmp_path))
    assert STORE.best(directory=str(tmp_path))["objective"] == 2.5
    cmp = STORE.compare(STORE.load(str(tmp_path)))
    assert cmp["delta"] == pytest.approx(1.0)


def test_experiment_store_key_depends_on_config_and_grammar():
    a = STORE.key_for(ir_digest="x", grammar_hashes=["g1"], config_digest="c1")
    b = STORE.key_for(ir_digest="x", grammar_hashes=["g2"], config_digest="c1")
    c = STORE.key_for(ir_digest="x", grammar_hashes=["g1"], config_digest="c2")
    assert len({a, b, c}) == 3
