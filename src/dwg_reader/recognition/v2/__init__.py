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
from . import space_solver as SPACE

SCHEMA = "recognition-v2/1.0"
VERSION = "2.0.0-dev"


def analyze(*, snapshot: dict, scale=None, plan=None, walls=(), columns=(),
            doors=(), windows=(), stairs=(), steps=(), aprons=(), frames=(),
            grid=None, north=None, pattern_cases=(), faces=(), labels=None,
            space_walls=(), gateway=None,
            ai=None, budget=None, with_agent: bool = True,
            rooms: bool = True) -> dict:
    """给定快照与"底座结果"，跑完整的 v2 流程（纯函数，不读 DXF、不写文件）。

    ``rooms=True`` 时，房间在**墙/柱/门窗选完之后**由 :mod:`space_solver` 从选中
    墙网重算（单向链：墙 → 面域 → 房名），不允许反过来用房间造墙。

    ``space_walls`` 可以给一份**更细的墙段**（v1 的 ``walls_seg``）用于闭合面域：
    合并后的墙 run 便于计数与算量，但面域闭合要的是逐面线的原始墙段——两者混用
    会让房间数凭空变少（实测 16 vs 44）。给的是几何，不是判定。
    """
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
                          doors=doors, windows=windows, stairs=stairs,
                          steps=steps, aprons=aprons, frames=frames, grid=grid,
                          north=north, faces=faces, space_walls=space_walls,
                          labels=labels,
                          pattern_cases=pattern_cases, budget=budget)
        pool = agent["pool"]
        resolution = agent["resolution"]
        rounds = agent["rounds"]
        vision = agent["vision"]
        failures = agent["failures"]
        bootstrap = agent.get("bootstrap") or {}
    else:
        space_builder = (lambda selected: SPACE.solve(
            selected=selected, texts=ir.get("texts") or (),
            bbox=ir.get("bbox"), thick=float((ir.get("basis") or {})
                                             .get("thickness") or 0.0)))
        pool = ENGINES.generate(ir, graph, walls=walls, columns=columns,
                               doors=doors, windows=windows, stairs=stairs,
                               steps=steps, aprons=aprons, frames=frames,
                               grid=grid,
                               pattern_cases=pattern_cases, faces=faces)
        resolution = RESOLVER.staged(
            pool, thickness=float((ir.get("basis") or {}).get("thickness") or 200.0),
            faces=faces, space_builder=space_builder if rooms else None)
        rounds, vision, failures = [], {"ok": False, "reason": "AGENT_OFF"}, []
        bootstrap = {"status": "off", "calls": 0, "images_seen": 0}

    # 房间：agent 路径下 staged 还没算过空间阶段，这里在**最终选中**的墙网上补算
    space = None
    if rooms and not (resolution.get("stages") or {}).get("spaces", {}).get("status") == "ok":
        chosen_so_far = (resolution.get("final") or {}).get("selected") or []
        space = SPACE.solve(selected=list(space_walls) or chosen_so_far,
                            texts=ir.get("texts") or (),
                            labels=labels, bbox=ir.get("bbox"),
                            thick=float((ir.get("basis") or {}).get("thickness") or 0.0))
        extra = [h for h in (space.get("hypotheses") or ()) if isinstance(h, dict)]
        if extra:
            final = dict(resolution.get("final") or {})
            final["selected"] = list(final.get("selected") or ()) + extra
            counts = dict(final.get("counts") or {})
            counts["room"] = counts.get("room", 0) + len(extra)
            final["counts"] = counts
            resolution = dict(resolution, final=final)

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
                      # Native v2 result: complete fitted geometry and evidence,
                      # not just a shadow count or an AI suggestion.
                      "instances": [{k: h.get(k) for k in
                                     ("id", "category", "subtype", "source",
                                      "engine", "score", "status", "bbox",
                                      "geometry", "uids", "source_handles",
                                      "evidence", "grammar", "case_id")}
                                    for h in chosen],
                      "states": len((resolution.get("final") or {}).get("selected")
                                    or ())},
            "space": SPACE.summary(space) if space else (
                ((resolution.get("stages") or {}).get("spaces") or {})),
            "rounds": rounds, "vision": vision, "bootstrap": bootstrap,
            "failures": failures,
            "ai": {"mode": ai.get("mode") or "off"},
            "ms": round((time.perf_counter() - started) * 1000.0, 1),
            "_ir": ir, "_graph": graph, "_pool": pool, "_resolution": resolution}


def run_drawing(dxf_path: str, *, ai_mode: str | None = None, ai_config=None, jobs: int = 4,
                cache_dir=None, store_dir: str | None = None,
                memory_dir: str | None = None, budget=None) -> dict:
    """从一张 DXF 跑 v2：底座仍用旧 DAG（几何更成熟的那些），v2 负责多假设。

    返回 v2 结果 + ``base``（旧路径的计数，便于对照"到底改了什么"）。
    """
    from .. import ai_config as AIC
    from .. import model_gateway as GATEWAY
    from ..pipeline import run_recognition

    cfg = ai_config if isinstance(ai_config, dict) else AIC.load(
        str(ai_config) if ai_config else None, mode=ai_mode)
    if ai_mode is not None:
        cfg = dict(cfg, mode=ai_mode)

    gateway = GATEWAY.gateway_from_config(cfg) if AIC.enabled(cfg) else None
    # Enhance is explicitly a *multimodal* mode in v2. Do not silently turn
    # it into text-only guessing if the endpoint, image permission or renderer
    # is missing. Propose/off remain available without a vision endpoint.
    if cfg.get("mode") == "enhance":
        from .. import vision as VISION
        if gateway is None or not gateway.available:
            raise RuntimeError(
                "v2 enhance requires a configured multimodal model: "
                "set DWG_AI_BASE_URL, DWG_AI_API_KEY and DWG_AI_MODEL")
        if not bool((cfg.get("provider") or {}).get("vision")):
            raise RuntimeError("v2 enhance requires provider.vision=true")
        if not gateway.allow_remote_images:
            raise RuntimeError(
                "v2 enhance uploads local CAD crops; explicitly set "
                "provider.allow_remote_images=true after approving this upload")
        if not VISION.health()["ok"]:
            raise RuntimeError(
                "v2 enhance requires Pillow to render CAD images; "
                "install with pip install -e '.[dxf,vision]'")

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
    stairs = out.get("stairs") or ()
    steps = list(out.get("steps") or ()) + list(out.get("entrance_steps") or ())
    apron = out.get("apron") or ()
    frame = out.get("frame") or {}
    grid = out.get("grid") or {}
    north = out.get("north") or {}
    # 房名口径与 v1 一致：直接用它 label_filter 的结果（同一套预筛/几何提升）
    labels = out.get("label_filter") or {}
    pattern_cases = (out.get("pattern_discovery") or {}).get("cases") or ()
    faces = (out.get("spaces_seed") or {}).get("faces") or ()

    result = analyze(snapshot=snapshot, scale=scale, plan=plan, walls=walls,
                     columns=columns, doors=sel.get("doors") or (),
                     windows=sel.get("windows") or (), stairs=stairs,
                     steps=steps, aprons=apron,
                     frames=[frame] if isinstance(frame, dict) and frame else [],
                     grid=grid, north=north,
                     pattern_cases=pattern_cases, faces=faces, labels=labels,
                     gateway=gateway, ai=cfg, budget=budget)
    result["base"] = {"counts": (base.output.get("meta") or {}).get("counts") or {},
                      "ms": round((time.perf_counter() - t0 - float(result.get("ms")
                                                                    or 0) / 1000.0)
                                  * 1000.0, 1)}
    result["gateway"] = gateway.describe() if gateway is not None else {"available": False}
    if cfg.get("mode") == "enhance" and not result["gateway"].get("images_sent"):
        raise RuntimeError(
            "v2 enhance did not deliver any rendered image to the model; "
            "check drawing geometry, provider configuration and model errors")
    result["inputs"] = {"dxf": str(dxf_path), "ai_mode": cfg.get("mode") or "off",
                        "wall_source": ("wall_runs" if merged_runs else "walls_seg"),
                        "walls": len(walls), "columns": len(columns),
                        "doors": len(sel.get("doors") or ()),
                        "windows": len(sel.get("windows") or ()),
                        "pattern_cases": len(pattern_cases),
                        "stairs": len(stairs), "steps": len(steps),
                        "apron": len(apron),
                        "frame": bool(frame), "grid_axes": len(
                            (grid or {}).get("axis_lines") or ()),
                        "grid_intersections": len((grid or {}).get("intersections")
                                                  or ())}

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
            "bootstrap": {k: v for k, v in (result.get("bootstrap") or {}).items()
                          if k != "regions"},
            "ai": result.get("ai") or {},
            "vision": {"ok": (result.get("vision") or {}).get("ok"),
                       "model_on": (result.get("vision") or {}).get("model_on"),
                       "images_seen": (result.get("vision") or {}).get("images_seen")},
            "failures": result.get("failures") or []}


__all__ = ["AGENT", "ENGINES", "FITTER", "GRAMMAR", "GRAPH", "IR", "RESOLVER",
           "SPACE", "STORE", "SCHEMA", "VERSION", "analyze", "run_drawing",
           "summary"]
