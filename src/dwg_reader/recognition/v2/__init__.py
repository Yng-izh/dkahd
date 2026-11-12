"""识别 v2：多路径候选生成 + 全局联合求解（新编排层，旧 DAG 原封不动）。

架构对照（左边是旧 48 节点 DAG，右边是 v2 这条新编排）：

```text
旧：load -> 各识别器 -> 证据 -> 闸门 -> AI 补救 -> 补丁 -> 导出
                        ↑ AI 的作用范围被第一轮识别决定

新：CAD IR + 关系图
      ├── 规则引擎（现有识别器结果）
      ├── 图形模式引擎（残余聚类 + 内置语法）
      └── 多模态 AI（只提"构件语法"，不碰坐标）
              ↓
        统一构件假设池（同一图元可以有多个竞争解释）
              ↓
        参数化几何拟合（语法在原始图元上兑现）
              ↓
        全局联合识别与冲突求解（墙/柱/门窗/房间一起裁决，留多状态）
              ↓
        Confirmed / Uncertain / Unresolved + 来源证据 + 可复用规则
```

这一层**不替换** :mod:`nodes`：它把旧 DAG 当"几何底座"（读快照、读墙柱门窗
结果），自己在上面做多假设与联合求解。所以老路径的输出一个字都不变，想用新
架构就跑 :func:`run_drawing`。
"""
from __future__ import annotations

import time

from . import agent_controller as AGENT
from . import cad_ir as IR
from . import component_grammar as GRAMMAR
from . import experiment_store as STORE
from . import geometry_fitter as FITTER
from . import geometry_graph as GRAPH
from . import global_resolver as RESOLVER
from . import proposal_engines as ENGINES

SCHEMA = "recognition-v2/1.0"
VERSION = "2.0.0-dev"


def analyze(*, snapshot: dict, scale=None, plan=None, walls=(), columns=(),
            doors=(), windows=(), pattern_cases=(), faces=(), gateway=None,
            ai=None, budget=None, with_agent: bool = True) -> dict:
    """给定快照与"底座结果"，跑完整的 v2 流程（纯函数，不读 DXF、不写文件）。"""
    started = time.perf_counter()
    ai = dict(ai or {})

    interesting = []
    for case in pattern_cases or ():
        interesting.extend(case.get("source_handles") or ())
    ir = IR.build(snapshot, scale=scale, plan=plan, interesting=interesting)
    graph = GRAPH.build(ir, faces=faces)

    if with_agent:
        agent = AGENT.run(ir=ir, graph=graph, gateway=gateway, ai=ai,
                          snapshot=snapshot, walls=walls, columns=columns,
                          doors=doors, windows=windows, faces=faces,
                          pattern_cases=pattern_cases, budget=budget)
        pool = agent["pool"]
        resolution = agent["resolution"]
        rounds = agent["rounds"]
        vision = agent["vision"]
        failures = agent["failures"]
    else:
        pool = ENGINES.generate(ir, graph, walls=walls, columns=columns,
                               doors=doors, windows=windows,
                               pattern_cases=pattern_cases, faces=faces)
        resolution = RESOLVER.staged(
            pool, thickness=float((ir.get("basis") or {}).get("thickness") or 200.0),
            faces=faces)
        rounds, vision, failures = [], {"ok": False, "reason": "AGENT_OFF"}, []

    chosen = (resolution.get("final") or {}).get("selected") or []
    return {"schema": SCHEMA, "version": VERSION,
            "status": "ok" if chosen else "no_candidate",
            "ir": {"stats": (ir.get("stats") or {}), "digest": IR.digest(ir),
                   "regions": len(ir.get("regions") or ()),
                   "families": len([f for f in ir.get("families") or ()
                                    if f.get("n", 0) >= 2])},
            "graph": GRAPH.summary(graph)["stats"],
            "pool": ENGINES.summary(pool),
            "resolution": RESOLVER.summary(resolution),
            "final": {"counts": (resolution.get("final") or {}).get("counts") or {},
                      "selected": [{k: h.get(k) for k in
                                    ("id", "category", "subtype", "source",
                                     "score", "bbox", "uids")}
                                   for h in chosen],
                      "states": len((resolution.get("final") or {}).get("selected")
                                    or ())},
            "rounds": rounds, "vision": vision, "failures": failures,
            "ai": {"mode": ai.get("mode") or "off"},
            "ms": round((time.perf_counter() - started) * 1000.0, 1),
            "_ir": ir, "_graph": graph, "_pool": pool, "_resolution": resolution}


def run_drawing(dxf_path: str, *, ai_mode: str = "off", ai_config=None, jobs: int = 4,
                cache_dir=None, store_dir: str | None = None,
                memory_dir: str | None = None, budget=None) -> dict:
    """从一张 DXF 跑 v2：底座仍用旧 DAG（几何更成熟的那些），v2 负责多假设。

    返回 v2 结果 + ``base``（旧路径的计数，便于对照"到底改了什么"）。
    """
    from .. import ai_config as AIC
    from .. import model_gateway as GATEWAY
    from ..pipeline import run_recognition

    cfg = ai_config if isinstance(ai_config, dict) else AIC.load(
        str(ai_config) if ai_config else None, mode=ai_mode or "off")
    if ai_mode:
        cfg = dict(cfg, mode=ai_mode)

    t0 = time.perf_counter()
    base = run_recognition(str(dxf_path), jobs=jobs, targets=("assemble",),
                           cache_dir=cache_dir, ai_mode="off")
    out = base.outputs
    snapshot = out["load"]["snapshot"]
    scale = out["load"]["scale"]
    plan = out["load"].get("plan")
    # 墙用**合并后的连续墙 run**（walls_merge.wall_runs）：这是导出与算量用的口径，
    # 拿未合并的墙段去比计数会凭空多出一倍（同一道墙被门窗拆成几段）。
    merged_runs = (out.get("walls_merge") or {}).get("wall_runs") or ()
    walls = merged_runs or (out.get("walls_seg") or {}).get("walls_seg") or ()
    columns = (out.get("columns_verify") or {}).get("columns") or out.get("columns") or ()
    sel = out.get("openings_select") or {}
    pattern_cases = (out.get("pattern_discovery") or {}).get("cases") or ()
    faces = (out.get("spaces_seed") or {}).get("faces") or ()

    gateway = GATEWAY.gateway_from_config(cfg) if AIC.enabled(cfg) else None
    result = analyze(snapshot=snapshot, scale=scale, plan=plan, walls=walls,
                     columns=columns, doors=sel.get("doors") or (),
                     windows=sel.get("windows") or (), pattern_cases=pattern_cases,
                     faces=faces, gateway=gateway, ai=cfg, budget=budget)
    result["base"] = {"counts": (base.output.get("meta") or {}).get("counts") or {},
                      "ms": round((time.perf_counter() - t0 - float(result.get("ms")
                                                                    or 0) / 1000.0)
                                  * 1000.0, 1)}
    result["gateway"] = gateway.describe() if gateway is not None else {"available": False}
    result["inputs"] = {"dxf": str(dxf_path), "ai_mode": cfg.get("mode") or "off",
                        "wall_source": ("wall_runs" if merged_runs else "walls_seg"),
                        "walls": len(walls), "columns": len(columns),
                        "doors": len(sel.get("doors") or ()),
                        "windows": len(sel.get("windows") or ()),
                        "pattern_cases": len(pattern_cases)}

    if store_dir:
        entry = {"ir_digest": (result.get("ir") or {}).get("digest"),
                 "grammar_hashes": sorted({h.get("ai_grammar")
                                           for h in ((result.get("_pool") or {})
                                                     .get("hypotheses") or ())
                                           if h.get("ai_grammar")}),
                 "config_digest": (cfg or {}).get("digest") or "",
                 "objective": (result.get("resolution") or {}).get("objective"),
                 "counts": (result.get("final") or {}).get("counts") or {},
                 "winner": "prototype",
                 "resolver_version": VERSION,
                 "units": "mm"}
        result["experiment"] = STORE.record(entry, directory=store_dir)
        result["experiment_best"] = STORE.best(directory=store_dir)
        if memory_dir:
            result["rule"] = STORE.remember_rule(
                dict(entry, weights=(result.get("resolution") or {}).get("weights")),
                directory=store_dir, memory_dir=memory_dir)
    result.pop("_ir", None)
    result.pop("_graph", None)
    result.pop("_pool", None)
    result.pop("_resolution", None)
    return result


def summary(result) -> dict:
    """压成可进报告的一段（不含 IR/大数组）。"""
    result = result or {}
    return {"schema": SCHEMA, "version": result.get("version"),
            "status": result.get("status"),
            "ir": result.get("ir"), "graph": result.get("graph"),
            "pool": result.get("pool"), "resolution": result.get("resolution"),
            "final_counts": (result.get("final") or {}).get("counts") or {},
            "rounds": len(result.get("rounds") or ()),
            "ai": result.get("ai") or {},
            "vision": {"ok": (result.get("vision") or {}).get("ok"),
                       "model_on": (result.get("vision") or {}).get("model_on"),
                       "images_seen": (result.get("vision") or {}).get("images_seen")},
            "failures": result.get("failures") or []}


__all__ = ["AGENT", "ENGINES", "FITTER", "GRAMMAR", "GRAPH", "IR", "RESOLVER",
           "STORE", "SCHEMA", "VERSION", "analyze", "run_drawing", "summary"]
