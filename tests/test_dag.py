"""DAG 执行器与识别流程节点化的单元测试。

覆盖：拓扑校验（环/重复/未知/自依赖）、确定性拓扑序、并行等价与真并发、
部分执行、缓存命中/失效/清除、PURE 线程守卫、异常传播、节点统计，
以及快照的 JSON-native 与门块矩阵等价性（``_door_geom`` vs ``Matrix44``）。
"""
from __future__ import annotations

import inspect
import json
import threading
import time
from pathlib import Path

import ezdxf
import pytest

from dwg_reader.recognition import (DAG, DagCycleError, DagExecutionError,
                                    DagValidationError, Node, NodeContext,
                                    build_recognition_dag, clear_cache,
                                    engine_fingerprint, is_json_native, recognize,
                                    run_dag, run_recognition)
from dwg_reader.recognition import pipeline as pipeline_module
from dwg_reader.recognition.elements import DOOR_BLOCKS, _door_geom
from dwg_reader.recognition.snapshot import build_snapshot


def canon(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _ctx(file_digest: str = "digest-1", params: dict | None = None) -> NodeContext:
    return NodeContext(dxf_path="dummy.dxf", file_digest=file_digest,
                       params=params or {}, kind="main", name="<test>")


def _noop(ctx):
    return None


# --------------------------------------------------------------------------- 结构
EXPECTED_NODES = {
    "load", "texts", "walls_seg", "columns", "doors", "windows", "walls_merge",
    "grid", "frame", "north", "stairs", "steps", "entrance_steps", "apron",
    "rooms", "assemble", "export", "envelope", "room_labels", "partitions",
    # 拓扑闭环新增的三个节点（墙模型 / 空间种子 / 洞口选择）
    "wall_model", "spaces_seed", "openings_select",
    # 自适应链路：证据图 / 质量闸门 / 条件 LLM / 校验 / 重算 / 审计
    "evidence_graph", "quality_gate", "llm_reason", "validate_proposal",
    "affected_recompute", "final_audit",
    # 控制层改造（PR-2/3/4）：规划 -> 编译 -> 动态局部子 DAG -> 补丁定稿
    "repair_planner", "plan_compile", "adaptive_controller", "finalize_patch",
    # 统一结果状态 + 房间名种子（PR-1：让求解结果真正进入最终构件）
    "final_candidate_state", "room_labels_seed",
    # 容错识别：证据账本 / 缺陷诊断 / 修复候选 / 修复审计
    "raw_evidence", "defect_detect", "repair_options", "repair_audit",
    "label_filter",
    # 空间性质语义裁决（断墙 vs 开放分区）
    "space_cases", "space_judge", "space_routing",
    # 洞口语义裁决（候选程序枚举，性质交给模型）
    "opening_judge",
    # 统一洞口：门/窗所有画法先归并成"一个洞"，再判性质
    "openings_unify",
    # 柱两阶段：形态候选 -> 墙网拓扑复核
    "columns_verify",
}


def test_recognition_dag_shape():
    dag = build_recognition_dag()
    assert set(dag.nodes) == EXPECTED_NODES
    assert len(dag.nodes) == 46
    assert dag.deps("export") == ("assemble", "final_audit", "adaptive_controller")
    assert dag.deps("walls_merge") == ("walls_seg", "wall_model", "doors", "windows",
                                       "openings_select", "final_candidate_state")
    assert dag.deps("wall_model") == ("walls_seg", "load")
    assert dag.deps("openings_select") == ("load", "wall_model", "spaces_seed",
                                           "openings_unify", "opening_judge",
                                           "doors", "windows")
    assert dag.deps("openings_unify") == ("load", "doors", "windows", "walls_seg",
                                          "wall_model", "opening_judge")
    # 自适应链路：闸门先于 LLM，LLM 先于校验，校验先于局部求解，求解先于审计
    assert dag.deps("evidence_graph")[0] == "load"
    assert dag.deps("quality_gate")[:2] == ("load", "evidence_graph")
    assert dag.deps("llm_reason") == ("quality_gate", "evidence_graph")
    assert dag.deps("validate_proposal") == ("quality_gate", "llm_reason",
                                             "evidence_graph")
    # 控制层：规划 -> 编译 -> 控制器（局部子 DAG）-> 补丁定稿
    assert dag.deps("repair_planner")[0] == "quality_gate"
    assert dag.deps("plan_compile")[0] == "quality_gate"
    assert "repair_planner" in dag.deps("plan_compile")
    assert dag.deps("adaptive_controller")[0] == "quality_gate"
    assert "plan_compile" in dag.deps("adaptive_controller")
    assert dag.deps("finalize_patch") == ("adaptive_controller",)
    assert dag.deps("affected_recompute") == ("quality_gate", "adaptive_controller")
    assert dag.deps("final_audit")[0] == "quality_gate"
    assert "final_audit" in dag.deps("assemble")
    assert "adaptive_controller" in dag.deps("final_candidate_state")
    assert "final_candidate_state" in dag.deps("walls_merge")
    assert "final_candidate_state" in dag.deps("rooms")
    assert "final_candidate_state" in dag.deps("assemble")
    assert dag.deps("room_labels_seed") == ("texts", "load")
    assert "label_filter" in dag.deps("quality_gate")
    assert "openings_select" in dag.deps("quality_gate")
    # 拓扑顺序：空间种子与墙模型必须先于洞口选择，洞口选择必须先于墙实例合并
    order = dag.toposort()
    for before, after in (("walls_seg", "wall_model"), ("wall_model", "openings_select"),
                          ("spaces_seed", "openings_select"),
                          ("openings_select", "walls_merge"),
                          ("spaces_seed", "rooms"), ("openings_select", "assemble"),
                          ("evidence_graph", "quality_gate"),
                          ("quality_gate", "llm_reason"),
                          ("llm_reason", "validate_proposal"),
                          ("validate_proposal", "plan_compile"),
                          ("repair_planner", "plan_compile"),
                          ("plan_compile", "adaptive_controller"),
                          ("adaptive_controller", "finalize_patch"),
                          ("finalize_patch", "final_candidate_state"),
                          ("adaptive_controller", "affected_recompute"),
                          ("affected_recompute", "final_audit"),
                          ("final_audit", "assemble")):
        assert order.index(before) < order.index(after), (before, after)
    assert dag.deps("rooms") == ("load", "wall_model", "spaces_seed",
                                 "final_candidate_state", "room_labels",
                                 "label_filter", "stairs", "defect_detect",
                                 "space_routing")
    order = dag.toposort()
    for name in EXPECTED_NODES:
        for dep in dag.deps(name):
            assert order.index(dep) < order.index(name), (dep, name)
    assert order[-1] == "export"
    assert dag.terminals() == ("export",)
    assert dag.toposort() == dag.toposort()


def test_cycle_rejected_with_path():
    with pytest.raises(DagCycleError) as excinfo:
        DAG([Node("a", _noop, deps=("b",)), Node("b", _noop, deps=("a",))])
    cycle = excinfo.value.cycle
    assert cycle[0] == cycle[-1]
    assert set(cycle) == {"a", "b"}
    assert " -> ".join(cycle) == "a -> b -> a"


@pytest.mark.parametrize("nodes", [
    [Node("a", _noop), Node("a", _noop)],
    [Node("a", _noop, deps=("missing",))],
    [Node("a", _noop, deps=("a",))],
    [Node("", _noop)],
])
def test_invalid_dag_rejected(nodes):
    with pytest.raises(DagValidationError):
        DAG(nodes)


def test_run_dag_argument_validation():
    dag = DAG([Node("a", _noop)])
    with pytest.raises(ValueError):
        run_dag(dag, context=_ctx(), jobs=0)
    with pytest.raises(ValueError):
        run_dag(dag, context=_ctx(), targets=())
    with pytest.raises(DagValidationError):
        run_dag(dag, context=_ctx(), targets=("nope",))


# --------------------------------------------------------------------------- 调度
def test_parallel_equivalence():
    def a(ctx):
        return 1

    def b(ctx):
        return [2]

    def c(ctx):
        return {"x": 3}

    def join(ctx):
        return ctx.inputs["a"] + ctx.inputs["b"][0] + ctx.inputs["c"]["x"]

    def make():
        return DAG([Node("a", a), Node("b", b), Node("c", c),
                    Node("join", join, deps=("a", "b", "c"))])

    serial = run_dag(make(), context=_ctx(), jobs=1)
    parallel = run_dag(make(), context=_ctx(), jobs=4)
    assert serial.output == 6
    assert canon(serial.output) == canon(parallel.output)
    assert canon(serial.outputs) == canon(parallel.outputs)


def test_parallel_nodes_really_overlap():
    first, second = threading.Event(), threading.Event()

    def a(ctx):
        first.set()
        assert second.wait(1.0), "node a did not run concurrently with b"
        return "a"

    def b(ctx):
        second.set()
        assert first.wait(1.0), "node b did not run concurrently with a"
        return "b"

    dag = DAG([Node("a", a), Node("b", b)])
    run = run_dag(dag, context=_ctx(), jobs=2)
    assert run.output == {"a": "a", "b": "b"}

    # jobs=1 时两个节点无法互相等待 -> 必须失败（证明不是“假并行”）
    first.clear()
    second.clear()
    with pytest.raises(DagExecutionError):
        run_dag(dag, context=_ctx(), jobs=1)


def test_partial_execution_closure():
    def a(ctx):
        return 1

    def b(ctx):
        return ctx.inputs["a"] + 1

    def c(ctx):
        return ctx.inputs["b"] * 2

    def d(ctx):
        return ctx.inputs["a"] * 10

    def e(ctx):
        return 99

    def make():
        return DAG([Node("a", a), Node("b", b, deps=("a",)),
                    Node("c", c, deps=("b",)), Node("d", d, deps=("a",)),
                    Node("e", e)])

    full = run_dag(make(), context=_ctx(), jobs=1)
    part = run_dag(make(), context=_ctx(), jobs=1, targets=("b",))
    assert set(part.outputs) == {"a", "b"}
    assert part.output == 2
    assert [stat.name for stat in part.stats] == ["a", "b"]
    assert canon(part.outputs["b"]) == canon(full.outputs["b"])
    assert canon(part.outputs["a"]) == canon(full.outputs["a"])

    multi = run_dag(make(), context=_ctx(), targets=("c", "e"))
    assert multi.output == {"c": 4, "e": 99}
    assert set(multi.outputs) == {"a", "b", "c", "e"}


def test_pure_node_cannot_access_live_document():
    def bad(ctx):
        return ctx.doc

    dag = DAG([Node("bad", bad)])
    run = run_dag(dag, context=_ctx(), jobs=1, raise_on_error=False)
    assert run.stats[0].status == "failed"
    assert "RuntimeError" in (run.stats[0].error or "")
    assert "pure node" in (run.stats[0].error or "")

    with pytest.raises(DagExecutionError) as excinfo:
        run_dag(dag, context=_ctx(), jobs=1)
    assert isinstance(excinfo.value.cause, RuntimeError)


def test_failure_propagation_and_skip_reasons():
    def boom(ctx):
        raise ValueError("kaboom")

    def down(ctx):
        return 1

    def independent(ctx):
        return 2

    def make():
        return DAG([Node("a", boom), Node("b", down, deps=("a",)),
                    Node("c", independent)])

    run = run_dag(make(), context=_ctx(), jobs=1, fail_fast=True, raise_on_error=False)
    by_name = {stat.name: stat for stat in run.stats}
    assert by_name["a"].status == "failed"
    assert "ValueError: kaboom" in (by_name["a"].error or "")
    assert by_name["b"].status == "skipped"
    assert by_name["b"].reason == "upstream_failed:a"
    assert by_name["c"].status == "skipped"
    assert by_name["c"].reason == "aborted"
    assert run.output == {"b": None, "c": None}

    single = run_dag(make(), context=_ctx(), targets=("b",), jobs=1,
                     fail_fast=True, raise_on_error=False)
    assert single.output is None

    with pytest.raises(DagExecutionError) as excinfo:
        run_dag(make(), context=_ctx(), jobs=1)
    assert isinstance(excinfo.value.cause, ValueError)
    assert [item.node for item in excinfo.value.failures] == ["a"]
    assert excinfo.value.skipped == ["b", "c"]

    # fail_fast=False：独立分支继续执行
    run2 = run_dag(make(), context=_ctx(), jobs=1, fail_fast=False, raise_on_error=False)
    by_name2 = {stat.name: stat for stat in run2.stats}
    assert by_name2["c"].status == "ok"
    assert by_name2["b"].status == "skipped"


def test_node_stats_and_threads():
    def a(ctx):
        time.sleep(0.05)
        return [1, 2, 3]

    def b(ctx):
        time.sleep(0.05)
        return {"x": 1}

    def m(ctx):
        return "m"

    dag = DAG([Node("a", a), Node("b", b),
               Node("m", m, kind="main", cacheable=False, deps=("a", "b"))])
    main_ident = threading.get_ident()
    run = run_dag(dag, context=_ctx(), jobs=2, collect_stats=True)
    assert [stat.name for stat in run.stats] == dag.toposort()
    assert all(stat.duration_ms >= 0 for stat in run.stats)
    assert all(stat.output_digest and len(stat.output_digest) == 64 for stat in run.stats)
    assert run.stats[-1].thread == main_ident
    assert any(stat.thread != main_ident for stat in run.stats if stat.kind == "pure")

    main_stat = run.stats[-1]
    assert main_stat.input_summaries["a"] == {"type": "list", "len": 3}
    assert set(main_stat.input_digests) == {"a", "b"}
    summary = run.summary()
    assert summary["failed"] == 0 and summary["skipped"] == 0
    assert summary["slowest"]["name"] in ("a", "b")


# --------------------------------------------------------------------------- 缓存
def _cache_dag(calls: dict, version: str = "v1") -> DAG:
    def a(ctx):
        calls["a"] = calls.get("a", 0) + 1
        return {"v": ctx.params.get("bump", 0)}

    def b(ctx):
        calls["b"] = calls.get("b", 0) + 1
        return ctx.inputs["a"]["v"] * 2

    return DAG([Node("a", a, version=version, params=("bump",)),
                Node("b", b, deps=("a",))])


def test_cache_hit_invalidate_and_clear(tmp_path):
    cache = tmp_path / "cache"
    calls: dict = {}
    r1 = run_dag(_cache_dag(calls), context=_ctx("file-1", {"bump": 0}), cache_dir=cache)
    assert r1.cache_hits == 0
    assert calls == {"a": 1, "b": 1}

    r2 = run_dag(_cache_dag(calls), context=_ctx("file-1", {"bump": 0}), cache_dir=cache)
    assert r2.cache_hits == 2
    assert calls == {"a": 1, "b": 1}
    assert canon(r1.output) == canon(r2.output)

    entries = [json.loads(path.read_text(encoding="utf-8"))
               for path in (cache / "dwg-dag-cache-v1").rglob("*.json")]
    assert entries
    for entry in entries:
        assert {"cache", "node", "version", "engine", "file", "params", "deps",
                "output", "output_digest"} <= set(entry)

    # 参数变化 -> 本节点与下游都 miss
    r3 = run_dag(_cache_dag(calls), context=_ctx("file-1", {"bump": 5}), cache_dir=cache)
    assert r3.cache_hits == 0
    assert calls["a"] == 2 and calls["b"] == 2

    # 文件内容变化 -> 全部 miss
    r4 = run_dag(_cache_dag(calls), context=_ctx("file-2", {"bump": 0}), cache_dir=cache)
    assert r4.cache_hits == 0

    # 节点版本变化 -> 该节点 miss（下游输出未变则仍可命中，内容寻址）
    r5 = run_dag(_cache_dag(calls, version="v2"), context=_ctx("file-1", {"bump": 0}),
                 cache_dir=cache)
    assert r5.cache_hits < 2

    # 清除
    removed = clear_cache(cache)
    assert removed > 0
    assert not list(Path(cache).rglob("*.json"))
    r6 = run_dag(_cache_dag(calls), context=_ctx("file-1", {"bump": 0}), cache_dir=cache)
    assert r6.cache_hits == 0


def test_cache_corrupt_entry_is_miss_not_crash(tmp_path):
    cache = tmp_path / "cache"
    calls: dict = {}
    run_dag(_cache_dag(calls), context=_ctx(), cache_dir=cache)
    entries = list((cache / "dwg-dag-cache-v1").rglob("*.json"))
    assert entries
    for path in entries:
        path.write_text("{not json", encoding="utf-8")
    run = run_dag(_cache_dag(calls), context=_ctx(), cache_dir=cache)
    assert run.cache_hits == 0
    assert any("corrupt" in warning for warning in run.warnings)
    assert run.output == 0


def test_cache_dir_unwritable_degrades_to_warning(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    calls: dict = {}
    run = run_dag(_cache_dag(calls), context=_ctx(), cache_dir=blocker)
    assert run.output == 0
    assert any("cache disabled" in warning for warning in run.warnings)


def test_engine_fingerprint_tracks_source_changes(tmp_path):
    directory = tmp_path / "engine"
    directory.mkdir()
    (directory / "a.py").write_text("x = 1\n", encoding="utf-8")
    first = engine_fingerprint(directory)
    (directory / "a.py").write_text("x = 2\n", encoding="utf-8")
    second = engine_fingerprint(directory)
    assert len(first) == 64 and len(second) == 64
    assert first != second


# --------------------------------------------------------------------------- 快照
def _door_drawing(tmp_path):
    doc = ezdxf.new("R2010")
    block = doc.blocks.new("$TCHSYS$DOOR2D")
    block.add_arc((0, 0), 0.975, 0, 90)
    block.add_line((0, 0), (1, 0))
    msp = doc.modelspace()
    msp.add_blockref("$TCHSYS$DOOR2D", (100, 200),
                     dxfattribs={"layer": "DOOR", "rotation": 30,
                                 "xscale": -900, "yscale": 900})
    path = tmp_path / "door.dxf"
    doc.saveas(path)
    return path


def _legacy_door_geom(entity, doc):
    """重构前 ``elements._door_geom`` 的逐行复刻，作为矩阵等价性的参照。"""
    import math

    try:
        blk = doc.blocks[entity.dxf.name]
        m = entity.matrix44()
    except Exception:
        return None
    arcs, lines = [], []
    for x in blk:
        t = x.dxftype()
        if t == "ARC":
            c = x.dxf.center
            r = x.dxf.radius
            a1 = math.radians(x.dxf.start_angle)
            a2 = math.radians(x.dxf.end_angle)
            arcs.append({
                "c": m.transform((c.x, c.y, 0.0)),
                "s": m.transform((c.x + r * math.cos(a1), c.y + r * math.sin(a1), 0.0)),
                "e": m.transform((c.x + r * math.cos(a2), c.y + r * math.sin(a2), 0.0)),
            })
        elif t == "LINE":
            lines.append((m.transform(x.dxf.start), m.transform(x.dxf.end)))
    if not arcs:
        return None
    hinges = [[a["c"].x, a["c"].y] for a in arcs]
    leaf_tip, best = None, -1.0
    for a in arcs:
        c = (a["c"].x, a["c"].y)
        for p, q in lines:
            for pt in (p, q):
                d = math.hypot(pt.x - c[0], pt.y - c[1])
                if d > best:
                    best, leaf_tip = d, [pt.x, pt.y]
    if len(arcs) >= 2:
        h1, h2 = hinges[0], hinges[1]
        return {"kind": "double", "hinges": [h1, h2], "opening": [h1, h2],
                "width": math.hypot(h2[0] - h1[0], h2[1] - h1[1])}
    a = arcs[0]
    h = hinges[0]
    s = [a["s"].x, a["s"].y]
    t = [a["e"].x, a["e"].y]
    tip = leaf_tip or s
    closed = t if math.hypot(s[0] - tip[0], s[1] - tip[1]) < math.hypot(t[0] - tip[0], t[1] - tip[1]) else s
    w = math.hypot(closed[0] - h[0], closed[1] - h[1])
    return {"kind": "single", "hinges": [h], "opening": [h, closed], "width": w,
            "leaf_tip": tip}


def test_snapshot_is_json_native_and_door_matrix_matches(tmp_path):
    path = _door_drawing(tmp_path)
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    snapshot = build_snapshot(doc, msp, str(path), block_names=tuple(DOOR_BLOCKS))

    assert is_json_native(snapshot)
    json.dumps(snapshot)
    assert set(snapshot) >= {"header", "entities", "blocks", "partitions"}
    assert len(snapshot["entities"]) == 1
    insert = snapshot["entities"][0]
    assert insert["type"] == "INSERT"
    assert len(insert["matrix"]) == 16

    got = _door_geom(insert, snapshot["blocks"])
    expected = _legacy_door_geom(msp.query("INSERT")[0], doc)
    assert got is not None
    assert got == expected


# --------------------------------------------------------------------------- 识别集成
def _simple_drawing(tmp_path):
    """4000×4000 房间：双线墙(200) + 柱 + 房间名/面积标注。"""
    doc = ezdxf.new("R2010")
    for name in ("WALL", "SPACE", "COLUMN"):
        doc.layers.add(name)
    msp = doc.modelspace()
    for (x1, y1, x2, y2) in [
        (200, 0, 3800, 0), (200, 200, 3800, 200),
        (200, 3800, 3800, 3800), (200, 3600, 3800, 3600),
        (0, 200, 0, 3800), (200, 200, 200, 3800),
        (3800, 200, 3800, 3800), (3600, 200, 3600, 3800),
        (0, 0, 200, 0), (3800, 0, 4000, 0),
        (0, 3800, 200, 3800), (3800, 3800, 4000, 3800),
    ]:
        msp.add_line((x1, y1), (x2, y2), dxfattribs={"layer": "WALL"})
    block = doc.blocks.new("_FZH")
    block.add_lwpolyline([(-0.5, -0.5), (0.5, -0.5), (0.5, 0.5), (-0.5, 0.5)], close=True)
    msp.add_blockref("_FZH", (2000, 2000),
                     dxfattribs={"layer": "COLUMN", "xscale": 400, "yscale": 400})
    msp.add_text("办公室", height=300,
                 dxfattribs={"layer": "SPACE", "insert": (1500, 1800)})
    msp.add_text("11.40m", height=200,
                 dxfattribs={"layer": "SPACE", "insert": (1500, 1500)})
    msp.add_text("2", height=100,
                 dxfattribs={"layer": "SPACE", "insert": (1900, 1560)})
    path = tmp_path / "room.dxf"
    doc.saveas(path)
    return path


def test_run_recognition_parallel_partial_and_json_native(tmp_path):
    path = _simple_drawing(tmp_path)
    serial = run_recognition(str(path), jobs=1, targets=("assemble",))
    parallel = run_recognition(str(path), jobs=4, targets=("assemble",))
    assert canon(serial.output) == canon(parallel.output)
    assert serial.output["meta"]["counts"].get("room") == 1

    for name, value in serial.outputs.items():
        if name == "assemble":          # assemble 允许含 tuple（axis_line）
            continue
        assert is_json_native(value), name

    part = run_recognition(str(path), targets=("walls_merge",))
    assert {"walls_seg", "load"} <= set(part.outputs)
    assert "assemble" not in part.outputs and "export" not in part.outputs
    assert canon(part.outputs["walls_merge"]) == canon(serial.outputs["walls_merge"])

    rooms = run_recognition(str(path), targets=("rooms",))
    assert "frame" not in rooms.outputs and "north" not in rooms.outputs
    assert "assemble" not in rooms.outputs and "export" not in rooms.outputs
    assert canon(rooms.outputs["rooms"]) == canon(serial.outputs["rooms"])


def test_run_recognition_export_target(tmp_path):
    path = _simple_drawing(tmp_path)
    run = run_recognition(str(path), targets=("export",), basemap_file="bm.png")
    assert run.output["schema"] == "dwg-components-records/1.1"
    assert run.output["basemap"]["file"] == "bm.png"
    assert {"assemble", "export"} <= set(run.outputs)
    assert len(run.stats) == 46

    default = run_recognition(str(path), targets=None)
    assert default.output["schema"] == "dwg-components-records/1.1"


def test_run_recognition_cache_roundtrip(tmp_path):
    path = _simple_drawing(tmp_path)
    cache = tmp_path / "cache"
    first = run_recognition(str(path), cache_dir=cache)
    second = run_recognition(str(path), cache_dir=cache)
    assert first.cache_hits == 0
    assert second.cache_hits >= 17
    assert canon(first.output) == canon(second.output)
    assert all(stat.status in ("ok", "cached") for stat in second.stats)
    assert clear_cache(cache) > 0


def test_recognize_signature_and_result_shape(tmp_path):
    assert str(inspect.signature(recognize)) == "(dxf_path: str)"
    result = recognize(str(_simple_drawing(tmp_path)))
    assert list(result) == ["meta", "components", "unrecognized"]
    assert result["meta"]["counts"].get("room") == 1


def test_recognize_reraises_original_node_exception(tmp_path, monkeypatch):
    drawing = tmp_path / "dummy.dxf"
    drawing.write_text("not parsed by the fake DAG", encoding="utf-8")

    def boom(ctx):
        raise KeyError("original failure")

    fake = DAG([Node("assemble", boom, kind="main", cacheable=False)])
    monkeypatch.setattr(pipeline_module, "build_recognition_dag", lambda: fake)
    with pytest.raises(KeyError) as excinfo:
        pipeline_module.recognize(str(drawing))
    assert str(excinfo.value) == "'original failure'"
