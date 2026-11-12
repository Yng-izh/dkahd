"""Agent 控制器：决定"看哪儿、问什么、试完怎么改"，并驱动多条引擎收敛。

它只做三件"决策"类的事，绝不碰几何：

1. **看哪儿**：按"残余量 / 重复度 / 冲突数"给小区域排序，选出这一轮要看的区域；
2. **问什么**：把区域事实（结构特征 + 关系子图 + 多尺度视图）发给模型，要一份
   **构件语法**（不是坐标）；模型给不出就退到内置模板；
3. **怎么改**：把拟合出的候选喂给联合求解，把"冲突/低可信"压成机器可验证的失败
   原因，喂回下一轮。

真正的几何在 :mod:`geometry_fitter`，裁决在 :mod:`global_resolver`。控制器只
负责把这两件事按预算串起来，并留下可审计的轨迹（每次调用、每张图、每个决策）。
"""
from __future__ import annotations

import json
import time

from . import component_grammar as G
from . import geometry_graph as GG
from . import global_resolver as RESOLVER
from . import proposal_engines as ENGINES

SCHEMA = "agent-run/1.0"
PROMPT_VERSION = "agent-controller/1.0"

#: 逻辑预算（区域/调用/轮次/秒）
BUDGET = {"max_regions": 8, "max_model_calls": 8, "max_rounds": 2,
          "max_walltime_seconds": 180, "max_images_per_region": 2,
          "max_grammars_per_region": 3, "min_score": 0.35}

#: 失败原因闭集（只发机器可验证的东西给模型）
FAILURE_CODES = ("NO_HOST_WALL", "GEOMETRY_SELF_INTERSECTION", "DUPLICATE_CONFIRMED",
                 "INSUFFICIENT_INDEPENDENT_EVIDENCE", "NO_VALID_CANDIDATE",
                 "MUTUALLY_EXCLUSIVE", "SAME_FOOTPRINT", "HOST_TOO_FAR",
                 "INVALID_GRAMMAR", "NO_PART", "NO_RELATION")

SYSTEM = ("你是 CAD 构件的**语法设计者**。你只描述'构件由哪些部分、怎么组合'，"
          "不提供任何坐标、尺寸或阈值。几何由确定性拟合器从原始图元算出来。"
          "只输出 JSON。")

PROMPT = """下面是一块**规则与模式引擎都没能确定解释**的局部区域。请给出你判断的
构件语法：它由哪些几何部分构成、这些部分之间是什么关系。

区域事实（全部由几何程序计算，比例类数值已按本图墙厚归一化）：
{facts}

拓扑速写（每个种子图元的关系，数值是"关系条数"，不是尺寸）：
{topology}

可用语法目录（**原语与关系都是闭集**，不许造新词）：
{catalog}

上一轮的失败原因（若有，只能据此调整方向）：{failures}

要求：
1. 输出 1~{max_grammars} 份语法，按可能性从高到低；
2. category 必须在目录的 categories 里；parts 只能用目录里的 primitives；
   relations 只能用目录里的 relations，且只能引用你声明的 part 名，
   或锚点 {anchors}；
3. **绝对不许输出坐标/包围盒/毫米数/阈值**（出现即整份作废）；
4. 你如果认为"这块根本不是构件"，输出 {{"grammars": []}}；
5. 附了局部截图时，图只用于确认形态（几根线、有没有弧、是不是块、贴不贴墙）；
   任何尺寸都不要从图里读。

输出格式：
{{"grammars":[{{"category":"window","subtype":"line_bundle_unclassified",
  "parts":{{"outer":"parallel_line_pair","inner":"repeated_line_bundle"}},
  "relations":[["inner","inside","outer"],["outer","aligned_with","wall"]],
  "fit_mode":"constrained_geometry","instances":"find_similar",
  "note":"外框+分格"}}],
 "next_step_if_failed":"inspect_neighbors"}}
"""


def _sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:12]


def _dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


def _r(v, nd: int = 3) -> float:
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------- 观察
def region_priority(ir: dict, graph, *, cases=()) -> list[dict]:
    """给小区域排序：残余多、重复多、冲突多的先看（分数只用于排队）。"""
    by_region_cases: dict[str, int] = {}
    for case in cases or ():
        rid = str(case.get("region_id") or "")
        if rid:
            by_region_cases[rid] = by_region_cases.get(rid, 0) + 1
    rows = []
    for reg in ir.get("regions") or ():
        if reg.get("kind") == "global":
            continue
        uids = reg.get("uids") or ()
        degree = 0
        for uid in uids[:80]:
            degree += graph.degree(uid) if isinstance(graph, GG.Graph) else 0
        repeats = 0
        for fam in ir.get("families") or ():
            if fam.get("n", 0) >= 3 and set(fam.get("uids") or ()) & set(uids):
                repeats += int(fam["n"])
        score = (0.5 * min(1.0, by_region_cases.get(reg.get("id"), 0) / 2.0)
                 + 0.3 * min(1.0, repeats / 12.0)
                 + 0.2 * min(1.0, degree / 120.0))
        rows.append({"region": reg, "score": _r(score),
                     "cases": by_region_cases.get(reg.get("id"), 0),
                     "repeats": repeats})
    rows.sort(key=lambda row: -row["score"])
    return rows


def observe(ir: dict, graph, region: dict, *, cases=()) -> dict:
    """把一块区域压成"结构事实"（模型能读、且不含任何阈值的东西）。"""
    uids = list(region.get("uids") or ())
    by_uid = {e["uid"]: e for e in (ir.get("entities") or ())}
    picked = [by_uid[u] for u in uids if u in by_uid]
    kinds: dict[str, int] = {}
    angles: dict[float, int] = {}
    layers: dict[str, int] = {}
    for e in picked:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
        layers[e["layer"]] = layers.get(e["layer"], 0) + 1
        if e.get("angle") is not None:
            key = round(float(e["angle"]) / 15.0) * 15.0
            angles[key] = angles.get(key, 0) + 1
    fams = [{"name": f["name"], "n": f["n"], "hash": f["hash"],
             "layers": f.get("layers")}
            for f in (ir.get("families") or ())
            if set(f.get("uids") or ()) & set(uids)]
    cases_here = [{"case_id": c.get("id"), "kind": c.get("kind"),
                   "priority": c.get("priority"),
                   "reasons": (c.get("baseline") or {}).get("reasons")}
                  for c in (cases or ())
                  if str(c.get("region_id") or "") == str(region.get("id"))]
    texts = [t["text"] for t in (ir.get("texts") or ())
             if t.get("bbox") and _bbox_hits(t["bbox"], region.get("bbox"))][:6]
    return {"region": region.get("id"), "level": region.get("level"),
            "bbox": region.get("bbox"), "entities": len(picked),
            "kinds": kinds, "dominant_angles_deg": sorted(angles,
                                                          key=lambda a: -angles[a])[:3],
            "layers": dict(sorted(layers.items(), key=lambda kv: -kv[1])[:6]),
            "families": fams[:6], "cases": cases_here[:4],
            "texts": texts,
            "seed_uids": uids[:24], "basis": ir.get("basis") or {}}


def _bbox_hits(box, region) -> bool:
    if not box or not region:
        return False
    return not (box[2] < region[0] or box[0] > region[2]
                or box[3] < region[1] or box[1] > region[3])


def topology_of(graph, uids, *, limit: int = 6) -> list[dict]:
    """种子的拓扑速写（只给关系种类与条数，不给坐标）。"""
    out = []
    for uid in (uids or ())[:limit]:
        if isinstance(graph, GG.Graph):
            loo = graph.describe(uid, limit=3)
        else:
            rows = (graph or {}).get("by_uid", {}).get(uid, [])
            loo = {"uid": uid, "relations": {}, "degree": len(rows)}
        out.append({"uid": uid,
                    "relations": {k: len(v) for k, v in (loo.get("relations")
                                                         or {}).items()},
                    "degree": loo.get("degree")})
    return out


# --------------------------------------------------------------------- 问模型
def propose_grammars(facts: dict, *, gateway, views=None, budget=None,
                     failures=(), tools=None) -> dict:
    """向模型要语法（一次调用）。模型不可用 / 输出非法都不抛异常。"""
    budget = dict(BUDGET, **(budget or {}))
    if gateway is None or not getattr(gateway, "available", False):
        return {"status": "unavailable", "grammars": [], "errors": [],
                "images": 0, "prompt_version": PROMPT_VERSION}
    images = []
    for view in (views or {}).get("views") or ():
        if view.get("status") == "rendered" and view.get("b64"):
            images.append(view["b64"])
        if len(images) >= int(budget["max_images_per_region"]):
            break
    prompt = PROMPT.format(
        facts=json.dumps(facts, ensure_ascii=False, sort_keys=True)[:4000],
        topology=json.dumps(topology_of(tools.get("graph") if tools else None,
                                        facts.get("seed_uids") or ()),
                            ensure_ascii=False)[:1200],
        catalog=json.dumps(G.catalog(), ensure_ascii=False)[:5000],
        failures=json.dumps(list(failures or ())[:6], ensure_ascii=False),
        max_grammars=int(budget["max_grammars_per_region"]),
        anchors=list(G.ANCHORS))
    result = gateway.ask(system=SYSTEM, prompt=prompt, images=images,
                         vision=bool(images))
    out: dict = {"status": "ok" if result.ok else "error",
                 "images": int(result.images or 0),
                 "gateway": result.as_dict(),
                 "prompt_version": PROMPT_VERSION, "grammars": [], "errors": []}
    if not result.ok:
        out["errors"].append({"code": result.code, "detail": result.detail})
        return out
    raw = result.data or {}
    if not isinstance(raw, dict):
        out["errors"].append({"code": "NOT_AN_OBJECT"})
        return out
    out["next_step_if_failed"] = str(raw.get("next_step_if_failed") or "")
    for item in (raw.get("grammars") or ())[:int(budget["max_grammars_per_region"])]:
        try:
            grammar = G.from_model(item)
        except G.GrammarError as exc:
            out["errors"].append(exc.as_dict())
            continue
        out["grammars"].append(grammar)
    if not out["grammars"] and not out["errors"]:
        out["status"] = "no_hypothesis"
    return out


# --------------------------------------------------------------------- 主循环
def run(*, ir: dict, graph=None, pool=None, gateway=None, ai=None, snapshot=None,
        walls=(), columns=(), doors=(), windows=(), faces=(), pattern_cases=(),
        budget=None) -> dict:
    """一轮或多轮：观察 -> 要语法 -> 拟合 -> 联合求解 -> 反馈。

    ``ai`` 是 :mod:`ai_config` 的配置（``mode=off`` 时不会调用模型，但**仍然**
    用内置语法跑一遍候选生成+联合求解，这条路径本身就是确定性算法）。
    """
    from .. import ai_config as AIC
    from .. import vision as VISION

    started = time.perf_counter()
    budget = dict(BUDGET, **(budget or {}))
    ai_cfg = dict(ai or {})
    model_on = bool(gateway is not None and getattr(gateway, "available", False))
    graph_obj = graph if isinstance(graph, GG.Graph) else GG.Graph(graph or {}, ir)
    thickness = float((ir.get("basis") or {}).get("thickness") or 200.0)

    rounds: list[dict] = []
    vision_rows: list[dict] = []
    failures: list[dict] = []
    visited: list[str] = []
    ai_items: list[dict] = []
    calls = 0

    for round_no in range(1, int(budget["max_rounds"]) + 1):
        ranking = region_priority(ir, graph_obj, cases=pattern_cases)
        todo = [row for row in ranking if row["region"]["id"] not in visited]
        todo = todo[:int(budget["max_regions"])]
        round_rows: list[dict] = []
        for row in todo:
            region = row["region"]
            visited.append(region["id"])
            facts = observe(ir, graph_obj, region, cases=pattern_cases)
            views = {"ok": False, "reason": "NO_SNAPSHOT", "views": [], "rendered": 0}
            if snapshot and bool((ai_cfg.get("provider") or {}).get("vision", True)):
                views = VISION.render_views(
                    snapshot, {"bbox": region.get("bbox")},
                    scales=("local", "detail"), walls=walls, thickness=thickness,
                    focus_handles=facts.get("seed_uids") or ())
                vision_rows.append(dict(VISION.views_summary(views),
                                        region=region["id"]))
            elif snapshot:
                views = VISION.render_views({"bbox": region.get("bbox")},
                                            {"bbox": region.get("bbox")},
                                            scales=())
            proposal = {"status": "off", "grammars": [], "errors": [], "images": 0}
            if model_on and calls < int(budget["max_model_calls"]):
                proposal = propose_grammars(facts, gateway=gateway, views=views,
                                            budget=budget, failures=failures,
                                            tools={"graph": graph_obj})
                calls += 1
            grammars = list(proposal.get("grammars") or ())
            # 模型没给出可用语法时，退到内置模板（确定性，不花调用）
            if not grammars:
                for case in facts.get("cases") or ():
                    tpl = G.for_kind(str(case.get("kind") or ""))
                    if tpl:
                        grammars.append(tpl)
            if grammars:
                ai_items.append({"region": region, "seed_uids": facts.get("seed_uids"),
                                 "grammars": [g for g in grammars
                                              if g.get("category")],
                                 "case_id": (facts.get("cases") or [{}])[0].get("case_id")})
            round_rows.append({"region": region["id"], "priority": row["score"],
                               "entities": facts.get("entities"),
                               "grammars": len(grammars),
                               "model_status": proposal.get("status"),
                               "images": proposal.get("images"),
                               "errors": proposal.get("errors")})
            if (time.perf_counter() - started) > float(budget["max_walltime_seconds"]):
                break

        pool_now = ENGINES.generate(ir, graph_obj, walls=walls, columns=columns,
                                    doors=doors, windows=windows,
                                    pattern_cases=pattern_cases,
                                    ai_grammars=ai_items, faces=faces)
        resolution = RESOLVER.staged(pool_now, thickness=thickness, faces=faces)
        failures = _failure_digest(pool_now, resolution)
        rounds.append({"round": round_no, "regions": round_rows,
                       "pool": ENGINES.summary(pool_now),
                       "resolution": RESOLVER.summary(resolution),
                       "failures": failures[:8]})
        if not failures or not model_on:
            break
        if (time.perf_counter() - started) > float(budget["max_walltime_seconds"]):
            break

    pool_now = ENGINES.generate(ir, graph_obj, walls=walls, columns=columns,
                                doors=doors, windows=windows,
                                pattern_cases=pattern_cases, ai_grammars=ai_items,
                                faces=faces) if rounds else (pool or {"hypotheses": []})
    resolution = RESOLVER.staged(pool_now, thickness=thickness, faces=faces)

    vision_ok = any(v.get("ok") for v in vision_rows)
    return {"schema": SCHEMA,
            "status": "ok" if (pool_now.get("hypotheses") or []) else "no_candidate",
            "pool": pool_now, "resolution": resolution, "rounds": rounds,
            "vision": {"ok": vision_ok, "regions": vision_rows,
                       "model_on": model_on,
                       "images_seen": int(sum(int(r.get("images") or 0)
                                              for rd in rounds
                                              for r in rd.get("regions") or ())),
                       "renderer": VISION.health()},
            "ai": {"mode": ai_cfg.get("mode") or "off", "model_on": model_on,
                   "calls": calls, "walltime_s": round(
                       time.perf_counter() - started, 1),
                   "prompt_version": PROMPT_VERSION},
            "failures": failures[:12],
            "budget": budget}


# --------------------------------------------------------------------- 反馈
def _failure_digest(pool, resolution) -> list[dict]:
    """把"冲突/没选中/拟合失败"压成闭集失败码，供下一轮使用。"""
    out: list[dict] = []
    seen: set = set()

    def _add(code: str, **detail):
        if code not in FAILURE_CODES or code in seen:
            return
        seen.add(code)
        out.append({"code": code, "hint": _HINTS.get(code, ""), **detail})

    stats = (pool or {}).get("stats") or {}
    if not stats.get("kept"):
        _add("NO_VALID_CANDIDATE")
    for engine, info in ((pool or {}).get("engines") or {}).items():
        for row in (info.get("trace") or ()):
            status = str(row.get("status") or "")
            if status == "invalid_grammar":
                _add("INVALID_GRAMMAR")
            elif status == "no_part":
                _add("NO_PART")
            elif status == "no_relation":
                _add("NO_RELATION")
    for stage, info in ((resolution or {}).get("stages") or {}).items():
        counts = (info.get("conflicts") or {})
        if counts.get("conflicts"):
            _add("MUTUALLY_EXCLUSIVE", stage=stage)
    return out


_HINTS = {
    "NO_HOST_WALL": "候选不贴任何已确认墙",
    "GEOMETRY_SELF_INTERSECTION": "几何自交或退化",
    "DUPLICATE_CONFIRMED": "与已确认构件重复",
    "INSUFFICIENT_INDEPENDENT_EVIDENCE": "只有单一来源证据",
    "NO_VALID_CANDIDATE": "没有任何候选通过拟合",
    "MUTUALLY_EXCLUSIVE": "同位置互斥构件冲突",
    "SAME_FOOTPRINT": "两个类别落在同一块地方",
    "HOST_TOO_FAR": "宿主墙太远",
    "INVALID_GRAMMAR": "语法非法（用了闭集外的原语/关系）",
    "NO_PART": "语法里的原语在图里一个都没匹配上",
    "NO_RELATION": "原语匹配上了但关系不成立",
}


def audit(result: dict) -> dict:
    result = result or {}
    vision = result.get("vision") or {}
    return {"schema": SCHEMA, "status": result.get("status"),
            "rounds": len(result.get("rounds") or ()),
            "pool": (result.get("pool") or {}).get("stats") or {},
            "resolution": (result.get("resolution") or {}).get("stages") or {},
            "final_counts": ((result.get("resolution") or {}).get("final") or {})
            .get("counts") or {},
            "ai": result.get("ai") or {},
            "vision": {"ok": vision.get("ok"), "model_on": vision.get("model_on"),
                       "images_seen": vision.get("images_seen"),
                       "renderer": vision.get("renderer")},
            "failures": result.get("failures") or []}
