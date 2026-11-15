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

import base64
import json
import time
from pathlib import Path

from . import agent_controller as AGENT
from . import cad_ir as IR
from . import component_family as FAMILY
from . import component_grammar as GRAMMAR
from . import component_resolver as COMPONENT_RESOLVER
from . import experiment_store as STORE
from . import geometry_fitter as FITTER
from . import geometry_graph as GRAPH
from . import global_resolver as RESOLVER
from . import profiles as PROFILES
from . import proposal_engines as ENGINES
from . import space_solver as SPACE

SCHEMA = "recognition-v2/1.0"
VERSION = "2.1.0-component-focus"


def _strip_image_bytes(rows) -> list:
    """结果里只留图片指纹（sha256/字节数），不留 4 张图的 base64 正文。"""
    out = []
    for row in rows or ():
        try:
            copy = json.loads(json.dumps(row, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            copy = dict(row) if isinstance(row, dict) else {}
        def strip(value):
            if isinstance(value, dict):
                value.pop("b64", None)
                for child in value.values():
                    strip(child)
            elif isinstance(value, list):
                for child in value:
                    strip(child)
        strip(copy)
        out.append(copy)
    return out


def _write_model_audit(rows, directory) -> dict:
    """把每次模型请求落盘：请求 JSON + 真正发给模型的 PNG。

    落盘的意义不是"多存一份"：上次跑完只剩一个类别字符串，谁也说不清模型看到的
    是家具还是柱、图里有没有标出目标、它为什么那样答。这里把**输入**和**输出**
    一起存下来，复盘时能逐张对照。
    """
    target = Path(str(directory))
    target.mkdir(parents=True, exist_ok=True)
    index = []
    for number, row in enumerate(rows or (), 1):
        tag = "%02d-%s" % (number, str(row.get("family") or "call").replace(":", "-"))
        images_saved = []
        call = json.loads(json.dumps(row, ensure_ascii=False, default=str))
        for holder_name in ("request", "closeup"):
            holder = call.get(holder_name) or {}
            holder = holder.get("request", holder)
            for image_index, image in enumerate(holder.get("images") or (), 1):
                if not isinstance(image, dict):
                    continue
                encoded = image.pop("b64", None)
                if not encoded:
                    continue
                name = "%s-%s-%02d-%s-%s.png" % (
                    tag, holder_name, image_index, image.get("instance") or "target",
                    image.get("scale") or "view")
                path = target / name.replace(":", "-").replace("/", "-")
                try:
                    path.write_bytes(base64.b64decode(encoded))
                except (ValueError, TypeError):
                    continue
                images_saved.append(path.name)
        call["saved_images"] = images_saved
        (target / ("%s.json" % tag)).write_text(
            json.dumps(call, ensure_ascii=False, indent=2), encoding="utf-8")
        index.append({"call": tag, "family": row.get("family"),
                      "status": row.get("status"), "images": images_saved})
    total_images = sum(len(row["images"]) for row in index)
    (target / "index.json").write_text(
        json.dumps({"calls": len(index), "images": total_images,
                    "entries": index}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return {"dir": str(target), "calls": len(index), "images": total_images}


def _persist_model_audit(rows, *, audit_dir=None) -> list:
    """保存观察审计；公开副本递归去除图片payload，但保留请求文字。"""
    rows = list(rows or ())
    if audit_dir:
        _write_model_audit(rows, audit_dir)
    return _strip_image_bytes(rows)


def _propagated_report(pool: dict, resolution: dict) -> dict:
    """单列已完成类别确认、实例拟合及冲突选择的推广结果来源。"""
    selected = (resolution.get("final") or {}).get("selected") or []
    rows = [h for h in selected if str(h.get("source")) == "propagation"]
    by_cat: dict = {}
    for h in rows:
        by_cat[str(h.get("category"))] = by_cat.get(str(h.get("category")), 0) + 1
    return {"counts": by_cat, "total": len(rows),
            "example": [{k: h.get(k) for k in ("category", "subtype", "score")}
                        for h in rows[:5]],
            "note": "模型确认类别后经逐实例几何验证并选中的构件，已包含在 final.counts 中"}


def _rejection_details(resolution: dict, pool: dict) -> list[dict]:
    """补回求解器精简掉的原始证据，保留拒绝原因与候选 ID 的对应关系。"""
    by_id = {h["id"]: h for h in (pool or {}).get("hypotheses") or ()
             if h.get("id") is not None}
    rejected_rows = list(resolution.get("rejected") or ())
    known_ids = {r.get("id") for r in rejected_rows}
    selected = ((resolution.get("final") or {}).get("selected")
                or resolution.get("selected") or ())
    selected_ids = {h.get("id") for h in selected}
    # staged 求解当前不带回 rejected：从池中补回未选中的显式待核项。
    for hid, hyp in by_id.items():
        if (hyp.get("status") == "pending" and hid not in known_ids
                and hid not in selected_ids):
            rejected_rows.append({"id": hid, "category": hyp.get("category"),
                                  "why": "PENDING_EVIDENCE"})
    rows = []
    for rejected in rejected_rows:
        row = dict(rejected)
        hyp = by_id.get(row.get("id")) or {}
        evidence = hyp.get("evidence") or {}
        if row.get("category") is None:
            row["category"] = hyp.get("category")
        codes = list(row.get("codes") or ())
        for negative in evidence.get("negatives") or ():
            code = negative.get("code") if isinstance(negative, dict) else negative
            if code and code not in codes:
                codes.append(code)
        row["codes"] = codes
        for key in ("pending_reason", "v1_reason", "v1_status", "notes", "layers"):
            if key not in row and key in evidence:
                row[key] = evidence[key]
        rows.append(row)
    return rows


def _pending_report(resolution: dict, pool: dict) -> dict:
    """待核候选与未决项：**与确认构件分开报**，不能混成一个数字。"""
    rows = [r for r in _rejection_details(resolution, pool)
            if str(r.get("why")) in ("PENDING_EVIDENCE", "PENDING_UNVERIFIED",
                                     "NEGATIVE_EVIDENCE", "PROPAGATION_WEAK_EVIDENCE",
                                     "MODEL_REJECT_KEPT_AS_PENDING", "CATEGORY_UNVERIFIED",
                                     "GEOMETRY_UNVERIFIED", "REFIT_REQUIRED")]
    unresolved = (pool or {}).get("unresolved") or []
    by_cat: dict = {}
    for r in rows:
        by_cat[str(r.get("category"))] = by_cat.get(str(r.get("category")), 0) + 1
    return {"counts": by_cat, "total": len(rows),
            "unresolved": len(unresolved),
            "sample": [{k: r.get(k) for k in
                        ("id", "category", "why", "codes", "pending_reason",
                         "v1_reason", "v1_status", "notes", "layers")}
                       for r in rows[:12]],
            "note": "这些候选证据不足（待核/否证），不计入确认构件"}


def analyze(*, snapshot: dict, scale=None, plan=None, walls=(), columns=(),
            doors=(), windows=(), stairs=(), steps=(), aprons=(), frames=(),
            grid=None, north=None, pattern_cases=(), faces=(), labels=None,
            space_walls=(), gateway=None,
            ai=None, budget=None, with_agent: bool = True,
            rooms: bool | None = None, profile=None, families=None,
            rules=None, observation_state=None, drawing_identity=None) -> dict:
    """给定快照与"底座结果"，跑完整的 v2 流程（纯函数，不读 DXF、不写文件）。

    ``observation_state`` accepts the previous result's coverage-only state.
    It skips attempted families, never restores semantic decisions or counts.
    Snapshot/family/config mismatches raise ValueError before model requests.
    ``drawing_identity`` is an optional full DXF SHA256 supplied by run_drawing.

    ``rooms=True`` 时，房间在**墙/柱/门窗选完之后**由 :mod:`space_solver` 从选中
    墙网重算（单向链：墙 → 面域 → 房名），不允许反过来用房间造墙。

    ``space_walls`` 可以给一份**更细的墙段**（v1 的 ``walls_seg``）用于闭合面域：
    合并后的墙 run 便于计数与算量，但面域闭合要的是逐面线的原始墙段——两者混用
    会让房间数凭空变少（实测 16 vs 44）。给的是几何，不是判定。
    """
    if observation_state is not None and not with_agent:
        raise ValueError("observation_state requires with_agent=True")
    agent: dict = {}
    started = time.perf_counter()
    prof = PROFILES.resolve(profile)
    focused = PROFILES.focused(prof)
    if rooms is None:
        rooms = bool(prof.get("rooms"))
    ai = dict(ai or {})
    if budget is None:
        # The gateway and agent must honor the same user-configured call/time
        # budget; previously the agent used a second, smaller hardcoded limit.
        upstream = ai.get("budget") or {}
        budget = {k: upstream[k] for k in
                  ("max_model_calls", "max_regions", "max_walltime_seconds")
                  if k in upstream}
    if focused:
        # focused 档：调用预算花在"族理解 + 疑难复查"上，而不是逐区域要语法
        budget.setdefault("max_model_calls", 12)
        budget.setdefault("max_regions", 0)

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
                          pattern_cases=pattern_cases, budget=budget,
                          profile=prof, families=families, rules=rules,
                          observation_state=observation_state,
                          drawing_identity=drawing_identity)
        pool = agent["pool"]
        resolution = agent["resolution"]
        rounds = agent["rounds"]
        vision = agent["vision"]
        failures = agent["failures"]
        bootstrap = agent.get("bootstrap") or {}
        family_info = agent.get("families") or {}
        detector_info = agent.get("detector") or {}
        reinspect_info = {"dropped": agent.get("dropped") or []}
        arb_info = agent.get("arbitration") or {}
    else:
        family_info, detector_info, reinspect_info, arb_info = {}, {}, {}, {}
        space_builder = (lambda selected: SPACE.solve(
            selected=selected, texts=ir.get("texts") or (),
            bbox=ir.get("bbox"), thick=float((ir.get("basis") or {})
                                             .get("thickness") or 0.0)))
        pool = ENGINES.generate(ir, graph, walls=walls, columns=columns,
                               doors=doors, windows=windows, stairs=stairs,
                               steps=steps, aprons=aprons, frames=frames,
                               grid=grid,
                               pattern_cases=pattern_cases, faces=faces)
        if focused:
            resolution = COMPONENT_RESOLVER.staged(
                pool, thickness=float((ir.get("basis") or {}).get("thickness") or 200.0),
                profile=prof, budget=budget)
        else:
            resolution = RESOLVER.staged(
                pool, thickness=float((ir.get("basis") or {}).get("thickness") or 200.0),
                faces=faces, space_builder=space_builder if rooms else None)
        rounds, vision, failures = [], {"ok": False, "reason": "AGENT_OFF"}, []
        bootstrap = {"status": "off", "calls": 0, "images_seen": 0}

    # 房间：agent 路径下 staged 还没算过空间阶段，这里在**最终选中**的墙网上补算。
    # focused 档 rooms=False：房间退出主流程，这一步整段跳过。
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
            "profile": PROFILES.summary(prof),
            "families": family_info,
            "detector": detector_info,
            "observation_state": agent.get("observation_state"),
            "model_audit": (agent or {}).get("model_audit") or [],
            "reinspect": reinspect_info,
            "arbitration": arb_info,
            "pending": _pending_report(resolution, pool),
            "propagated": _propagated_report(pool, resolution),
            "rejected": [{k: r.get(k) for k in
                          ("id", "category", "why", "codes", "score", "pending_reason",
                           "v1_reason", "v1_status", "notes", "layers")}
                         for r in _rejection_details(resolution, pool)[:120]],
            "ai": {"mode": ai.get("mode") or "off"},
            "ms": round((time.perf_counter() - started) * 1000.0, 1),
            "_ir": ir, "_graph": graph, "_pool": pool, "_resolution": resolution}


def run_drawing(dxf_path: str, *, ai_mode: str | None = None, ai_config=None, jobs: int = 4,
                cache_dir=None, store_dir: str | None = None,
                memory_dir: str | None = None, budget=None, profile=None,
                targets=None, rooms=None, audit_dir=None,
                observation_state=None) -> dict:
    """从一张 DXF 跑 v2：底座仍用旧 DAG（几何更成熟的那些），v2 负责多假设。

    ``profile`` 决定这次要什么（见 :mod:`profiles`）：

    * ``full``：老行为，旧 DAG 跑到 ``assemble``（房间在链里）；
    * ``openings_columns``（本次默认推荐给下游）：只跑到墙体上下文 / 柱 / 门窗
      候选 / 残余扫描，**不跑房间面域、房间路由、装配与导出**，v2 侧走门窗柱专用
      求解器。

    ``audit_dir``：把这次模型请求的**提示词原文、图片 PNG、模型原始答复**写到这个
    目录（每次调用一份 JSON + 实际发给模型的图）。不给就只在结果里留文本与图片
    指纹——"模型为什么这样判"至少要能复盘，图片不必每张都落盘。
    """
    from .. import ai_config as AIC
    from .. import model_gateway as GATEWAY
    from ..pipeline import run_recognition

    prof = PROFILES.resolve(profile, targets=targets, rooms=rooms)
    cfg = ai_config if isinstance(ai_config, dict) else AIC.load(
        str(ai_config) if ai_config else None, mode=ai_mode)
    # Preserve caller/file limits, while distinguishing loader defaults from
    # explicit limits. A preloaded dict is treated as an explicit configuration.
    if isinstance(ai_config, dict):
        explicit_walltime = "max_walltime_seconds" in (ai_config.get("budget") or {})
    elif ai_config:
        import json
        with open(str(ai_config), encoding="utf-8") as config_source:
            raw_config = json.load(config_source)
        explicit_walltime = "max_walltime_seconds" in (raw_config.get("budget") or {})
    else:
        explicit_walltime = False
    if not explicit_walltime and prof.get("walltime_seconds"):
        cfg = dict(cfg, budget=dict(cfg.get("budget") or {},
                                   max_walltime_seconds=float(prof["walltime_seconds"])))
    if ai_mode is not None:
        cfg = dict(cfg, mode=ai_mode)

    # 先只造一个"检查用"的网关做前置校验：**不要**用它发请求。
    # 真正的网关在底座 DAG 跑完之后再造，否则网关的墙钟预算会把底座那两分钟
    # 也算进去（实测：底座 160 s + 预算 180 s → 模型调用直接被判 BUDGET_EXHAUSTED）。
    checker = GATEWAY.gateway_from_config(cfg) if AIC.enabled(cfg) else None
    gateway = None
    # Enhance is explicitly a *multimodal* mode in v2. Do not silently turn
    # it into text-only guessing if the endpoint, image permission or renderer
    # is missing. Propose/off remain available without a vision endpoint.
    if cfg.get("mode") == "enhance":
        from .. import vision as VISION
        if checker is None or not checker.available:
            raise RuntimeError(
                "v2 enhance requires a configured multimodal model: "
                "set DWG_AI_BASE_URL, DWG_AI_API_KEY and DWG_AI_MODEL")
        if not bool((cfg.get("provider") or {}).get("vision")):
            raise RuntimeError("v2 enhance requires provider.vision=true")
        if not checker.allow_remote_images:
            raise RuntimeError(
                "v2 enhance uploads local CAD crops; explicitly set "
                "provider.allow_remote_images=true after approving this upload")
        if not VISION.health()["ok"]:
            raise RuntimeError(
                "v2 enhance requires Pillow to render CAD images; "
                "install with pip install -e '.[dxf,vision]'")

    import hashlib
    digest = hashlib.sha256()
    with open(dxf_path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    drawing_identity = digest.hexdigest()
    t0 = time.perf_counter()
    dag_targets = tuple(prof.get("dag_targets") or ("assemble",))
    base = run_recognition(str(dxf_path), jobs=jobs, targets=dag_targets,
                           cache_dir=cache_dir, ai_mode="off", llm_review=False)
    out = base.outputs
    if AIC.enabled(cfg):
        # Profile supplies a default only; never raise an explicit time limit.
        budget_cfg = dict(cfg.get("budget") or {})
        if "max_walltime_seconds" not in budget_cfg and prof.get("walltime_seconds"):
            budget_cfg["max_walltime_seconds"] = float(prof["walltime_seconds"])
            cfg = dict(cfg, budget=budget_cfg)
        gateway = GATEWAY.gateway_from_config(cfg)
    snapshot = out["load"]["snapshot"]
    scale = out["load"]["scale"]
    plan = out["load"].get("plan")
    # 墙用**合并后的连续墙 run**（walls_merge.wall_runs）：这是导出与算量用的口径，
    # 拿未合并的墙段去比计数会凭空多出一倍（同一道墙被门窗拆成几段）。
    merged_runs = (out.get("walls_merge") or {}).get("wall_runs") or ()
    walls = merged_runs or (out.get("walls_seg") or {}).get("walls_seg") or ()
    columns = _columns_from_outputs(out)
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
                     gateway=gateway, ai=cfg, budget=budget, profile=prof,
                      observation_state=observation_state,
                      drawing_identity=drawing_identity,
                     rules={"doors": sel.get("doors") or (),
                            "windows": sel.get("windows") or (),
                            "columns": columns})
    counts, caliber = _base_counts(out)
    result["base"] = {"counts": counts, "caliber": caliber,
                      "dag_targets": list(dag_targets),
                      "ms": round((time.perf_counter() - t0 - float(result.get("ms")
                                                                    or 0) / 1000.0)
                                  * 1000.0, 1)}
    result["gateway"] = gateway.describe() if gateway is not None else {"available": False}
    result["model_audit"] = _persist_model_audit(result.get("model_audit") or [],
                                                audit_dir=audit_dir)
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
                 "units": (scale or {}).get("unit") or "unspecified"}
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


def _columns_from_outputs(out: dict) -> list[dict]:
    """兼容柱节点的列表/字典输出；已复核的空集也必须优先于原候选。"""
    value = out.get("columns_verify") if "columns_verify" in out else out.get("columns")
    if isinstance(value, dict):
        value = value.get("columns")
    if not isinstance(value, (list, tuple)):
        return []
    return [row for row in value if isinstance(row, dict)]


def _base_counts(out: dict) -> tuple[dict, str]:
    """旧路径的计数 + 口径说明。

    跑到 ``assemble`` 时用 ``meta.counts``（导出口径，可与 v1 直接比）；focused 档
    不跑 ``assemble``，只能从各候选节点的输出数（**候选口径**）。两者数字不能互比，
    所以口径必须写在结果里，而不是让人去猜。
    """
    meta = out.get("meta") if isinstance(out.get("meta"), dict) else {}
    counts = dict(meta.get("counts") or {})
    if counts:
        return counts, "export"
    derived: dict = {}
    runs = ((out.get("walls_merge") or {}).get("wall_runs")
            or (out.get("walls_seg") or {}).get("walls_seg") or ())
    if runs:
        derived["wall"] = len(runs)
    columns = _columns_from_outputs(out)
    if "columns_verify" in out or "columns" in out:
        derived["column"] = len(columns)
    selected = out.get("openings_select") or {}
    doors = selected.get("doors") or out.get("doors") or ()
    windows = selected.get("windows") or out.get("windows") or ()
    if doors:
        derived["door"] = len(doors)
    if windows:
        derived["window"] = len(windows)
    for key, name in (("stairs", "stair"), ("steps", "step"),
                      ("entrance_steps", "step"), ("apron", "apron")):
        rows = out.get(key)
        if isinstance(rows, (list, tuple)) and rows:
            derived[name] = derived.get(name, 0) + len(rows)
    for key, name in (("frame", "frame"), ("grid", "grid_axis"), ("north", "north_arrow")):
        value = out.get(key)
        if isinstance(value, dict) and value.get("axes"):
            derived[name] = derived.get(name, 0) + len(value.get("axes") or ())
        elif isinstance(value, dict) and value:
            derived[name] = derived.get(name, 0) + 1
    return derived, "candidate"


def summary(result) -> dict:
    """压成可进报告的一段（不含 IR/大数组）。"""
    result = result or {}
    return {"schema": SCHEMA, "version": result.get("version"),
            "status": result.get("status"),
            "ir": result.get("ir"), "graph": result.get("graph"),
            "pool": result.get("pool"), "resolution": result.get("resolution"),
            "final_counts": (result.get("final") or {}).get("counts") or {},
            "profile": result.get("profile") or {},
            "arbitration": (result.get("arbitration") or {}).get("stats") or {},
            "families": result.get("families") or {},
            "detector": result.get("detector") or {},
            "base": result.get("base") or {},
            "rounds": len(result.get("rounds") or ()),
            "bootstrap": {k: v for k, v in (result.get("bootstrap") or {}).items()
                          if k != "regions"},
            "ai": result.get("ai") or {},
            "vision": {"ok": (result.get("vision") or {}).get("ok"),
                       "model_on": (result.get("vision") or {}).get("model_on"),
                       "images_seen": (result.get("vision") or {}).get("images_seen")},
            "failures": result.get("failures") or []}


__all__ = ["AGENT", "COMPONENT_RESOLVER", "ENGINES", "FAMILY", "FITTER", "GRAMMAR",
           "GRAPH", "IR", "PROFILES", "RESOLVER", "SPACE", "STORE", "SCHEMA",
           "VERSION", "analyze", "run_drawing", "summary"]
