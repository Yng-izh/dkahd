"""DAG 执行器并发与线程守卫测试（对应 requirements.md A4/A8 的独立命令）。

与 ``test_dag.py`` 互补：这里只保留最直接的“并行等价 / 真并发 / MAIN 亲和 /
PURE 守卫”用例，便于按 A4/A8 的 ``-k parallel`` / ``-k "thread or pure"`` 单独执行。
"""
from __future__ import annotations

import json
import threading

import pytest

from dwg_reader.recognition import (DAG, DagExecutionError, Node, NodeContext,
                                    run_dag)


def _ctx() -> NodeContext:
    return NodeContext(dxf_path="dummy.dxf", file_digest="digest",
                       params={}, kind="main", name="<test>")


def _canon(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_parallel_results_equal_serial():
    def a(ctx):
        return [1, 2]

    def b(ctx):
        return {"x": 3}

    def c(ctx):
        return "c"

    def make():
        return DAG([Node("a", a), Node("b", b), Node("c", c)])

    serial = run_dag(make(), context=_ctx(), jobs=1)
    parallel = run_dag(make(), context=_ctx(), jobs=4)
    assert _canon(serial.output) == _canon(parallel.output)
    assert _canon(serial.outputs) == _canon(parallel.outputs)


def test_parallel_workers_really_overlap():
    first, second = threading.Event(), threading.Event()

    def a(ctx):
        first.set()
        assert second.wait(1.0)
        return "a"

    def b(ctx):
        second.set()
        assert first.wait(1.0)
        return "b"

    run = run_dag(DAG([Node("a", a), Node("b", b)]), context=_ctx(), jobs=2)
    assert run.output == {"a": "a", "b": "b"}

    first.clear()
    second.clear()
    with pytest.raises(DagExecutionError):
        run_dag(DAG([Node("a", a), Node("b", b)]), context=_ctx(), jobs=1)


def test_pure_node_guard_rejects_live_document():
    def bad(ctx):
        return ctx.msp

    run = run_dag(DAG([Node("bad", bad)]), context=_ctx(), jobs=1,
                  raise_on_error=False)
    assert run.stats[0].status == "failed"
    assert "RuntimeError" in (run.stats[0].error or "")
    assert "pure node" in (run.stats[0].error or "")


def test_main_nodes_stay_on_main_thread():
    main_ident = threading.get_ident()

    def pure_node(ctx):
        return 1

    def main_node(ctx):
        return ctx.inputs["pure"] + 1

    dag = DAG([Node("pure", pure_node),
               Node("main", main_node, kind="main", cacheable=False, deps=("pure",))])
    run = run_dag(dag, context=_ctx(), jobs=4)
    by_name = {stat.name: stat for stat in run.stats}
    assert by_name["main"].thread == main_ident
    assert by_name["pure"].thread != main_ident
