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
from . import space_solver as SPACE

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
                 "INVALID_GRAMMAR", "NO_PART", "NO_RELATION",
                 # v2-component-focus 新增：族没读懂 / 传播零命中 / 家具嫌疑 / 模型否掉
                 "FAMILY_UNREAD", "FAMILY_CONFLICT", "PROPAGATION_EMPTY",
                 "FURNITURE_SUSPECTED",
                 "MODEL_REJECT")

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
        walls=(), columns=(), doors=(), windows=(), stairs=(), steps=(),
        aprons=(), frames=(), grid=None, north=None, faces=(), pattern_cases=(),
        budget=None, rooms=None, space_walls=(), labels=None, profile=None,
        families=None, rules=None, observation_state=None, drawing_identity=None) -> dict:
    """观察 -> 要判断 -> 拟合 -> 联合求解 -> 复查。

    两条执行路径：

    * **focused（``recognition_profile=openings_columns``）**：先做构件族发现，让
      模型只对族给语义，再由专用拟合器 + 全图传播找出所有实例，最后用门窗柱专用
      求解器裁决。房间完全不参与。
    * **full**：老路径——按区域要构件语法，通用联合求解。

    ``ai`` 是 :mod:`ai_config` 的配置（``mode=off`` 时不会调用模型，但仍然用内置
    语法与专用拟合器跑一遍；这条路径本身就是确定性算法）。
    """
    from .. import vision as VISION
    from . import cad_ir as IRMOD
    from . import component_family as FAMILY
    from . import component_resolver as FOCUS_RESOLVER
    from . import conflict_arbiter as ARBITER
    from . import multimodal_detector as DETECTOR
    from . import multimodal_bootstrap as BOOT
    from . import profiles as PROFILES
    from . import observation_state as OBS

    def _handles_for(uids) -> list:
        """uid → 图元 handle：渲染高亮要的是 handle，以前这里传的是 uid，永远对不上。"""
        by_uid = IRMOD.by_uid(ir)
        out = []
        for uid in uids or ():
            handle = (by_uid.get(uid) or {}).get("handle")
            if handle:
                out.append(str(handle))
        return out

    started = time.perf_counter()
    prof = PROFILES.resolve(profile)
    focused = PROFILES.focused(prof)
    budget = dict(BUDGET, **(budget or {}))
    if rooms is None:
        rooms = bool(prof.get("rooms"))
    ai_cfg = dict(ai or {})
    mode = str(ai_cfg.get("mode") or "off")
    model_on = bool(mode in ("propose", "enhance") and gateway is not None
                    and getattr(gateway, "available", False))
    graph_obj = graph if isinstance(graph, GG.Graph) else GG.Graph(graph or {}, ir)
    thickness = float((ir.get("basis") or {}).get("thickness") or 200.0)

    rounds: list[dict] = []
    vision_rows: list[dict] = []
    failures: list[dict] = []
    visited: list[str] = []
    ai_items: list[dict] = []
    readings: list[dict] = []
    dropped: list[dict] = []
    #: 每次模型请求的留档：提示词原文、图片元数据、模型原始答复。没有它，
    #: "模型为什么这样判"只能靠猜（上次跑完只剩一个类别字符串）。
    model_audit: list[dict] = []
    calls = 0
    detector_calls = 0
    arbitration: dict = {"status": "off", "stats": {}, "verdicts": []}
    constraints: dict = {}
    bootstrap = {"status": "off", "calls": 0, "images_seen": 0,
                 "regions": [], "items": [], "scene": {}}
    family_result = {"schema": FAMILY.SCHEMA, "families": [],
                     "stats": {"families": 0, "instances": 0}}
    rule_sets = rules or {"doors": doors, "windows": windows, "columns": columns}
    # 图内洞口宽度分布：给模型的"分位"用（没有它就无从判断一个宽度合不合理）
    widths = [float(rec.get("width") or 0.0)
              for bucket in ("doors", "windows")
              for rec in (rule_sets.get(bucket) or ())
              if float((rec or {}).get("width") or 0.0) > 0]

    # ---- 1) 构件族发现（门窗柱优先）
    if families is None and prof.get("families"):
        family_result = FAMILY.discover(ir, graph=graph_obj, walls=walls,
                                        rules=rule_sets,
                                        limit=int(budget.get("max_families") or 0) or None)
        families = family_result["families"]
    # Family geometry may be reused; semantic decisions belong to this run only.
    from copy import deepcopy
    families = deepcopy(list(families or ()))
    for family in families:
        for key in ("confirmed", "model", "verdict", "model_negative",
                    "category_confirmed", "category_basis", "category", "subtype"):
            family.pop(key, None)
        family["confirmed"] = False
    family_result["families"] = families

    identity = OBS.fingerprint(
        snapshot=snapshot, ir=ir, families=families, profile=prof,
        budget=budget, ai=ai_cfg, gateway=gateway, drawing_identity=drawing_identity,
        context={"walls": walls, "rules": rule_sets})
    previous_coverage = OBS.validate(observation_state, identity, families)
    observed_ids = set(previous_coverage["attempted_ids"])
    successful_ids = set(previous_coverage["successful_ids"])
    category_counts = dict(previous_coverage["category_counts"])
    # Failed preflight/rendering must not become persisted observed coverage.
    considered_ids = set(observed_ids)

    # ---- 2) 首轮视觉（整图 + 族优先）
    if model_on and snapshot:
        bootstrap = BOOT.run(
            ir=ir, graph=graph_obj, snapshot=snapshot, gateway=gateway,
            ai=ai_cfg, walls=walls, families=families,
            priority=("window", "column", "door") if focused else None,
            budget={"max_model_calls": int(budget["max_model_calls"]),
                    "max_calls": int(prof.get("bootstrap_calls", 5)),
                    "max_regions": 6})
        calls += int(bootstrap["calls"])
        if mode == "enhance":
            ai_items.extend(bootstrap["items"])

    # ---- 3) 构件级理解：模型只判断"族是什么"，不报坐标
    family_observation = {"min_families": 0, "max_families": 0, "observed": 0,
                          "unobserved": 0, "stopped": "off", "reserved_calls": 0}
    if model_on and focused and prof.get("detector") and families:
        min_families = int(prof.get("detector_families") or 0)
        # 上限是"最多看多少族"，不再等于"至少也要停在这里"：以前 limit=4 之后
        # 循环就结束，剩下 9 个柱候选族和 3 次未用调用一起浪费掉。
        max_families = max(min_families, int(
            prof.get("detector_max_families") or DETECTOR.BUDGET["max_families"]))
        total_calls = int(budget["max_model_calls"])
        recheck_reserve = max(0, int(budget.get("max_recheck", 4)))
        # 给"疑难复查"留出调用：族观察不能把预算吃光，复查也不能被饿死
        reserve = min(recheck_reserve, max(0, total_calls - calls - min_families))
        audit_rows: list = []
        while len(readings) < max_families:
            left = total_calls - calls
            if left <= reserve:
                family_observation["stopped"] = "BUDGET"
                break
            row = FAMILY.next_observation(
                families, observed=considered_ids, category_counts=category_counts,
                categories=PROFILES.categories(prof))
            if row is None:
                family_observation["stopped"] = "EXHAUSTED"
                break
            family = row["family"]
            considered_ids.add(str(family.get("id")))
            gateway_calls_before = getattr(gateway, "calls", None)
            reading = DETECTOR.read_family(
                family, ir=ir, graph=graph_obj, gateway=gateway,
                snapshot=snapshot, walls=walls, failures=failures, widths=widths,
                budget={"images_per_family": int(
                            prof.get("images_per_family") or 3),
                        "samples": int(prof.get("family_samples") or 3),
                        "closeup": min(max(0, int(prof.get("family_closeup", 1))),
                                       max(0, left - reserve - 1))})
            reading_calls = max(0, int(reading.get("calls", 1)))
            # Gateway preflight can reject ask() without an actual request.
            gateway_calls_after = getattr(gateway, "calls", None)
            actual_attempts = (max(0, gateway_calls_after - gateway_calls_before)
                               if isinstance(gateway_calls_before, int)
                               and isinstance(gateway_calls_after, int)
                               else reading_calls)
            if actual_attempts > 0:
                observed_ids.add(str(family["id"]))
                category_counts = OBS.category_counts(observed_ids, families)
                if reading.get("status") == "ok":
                    successful_ids.add(str(family["id"]))
            calls += reading_calls
            detector_calls += reading_calls
            readings.append({"family": family.get("id"),
                             "status": reading.get("status"),
                             "category": reading.get("category"),
                             "subtype": reading.get("subtype"),
                             "confidence": reading.get("confidence"),
                             "negatives": reading.get("negatives"),
                             "images": reading.get("images"),
                             "calls": reading_calls,
                             "reason": reading.get("reason"),
                             "family_consistent": reading.get("family_consistent"),
                             "outliers": reading.get("outliers"),
                             "scope": reading.get("scope"),
                             "agrees_with_geometry_guess": reading.get(
                                 "agrees_with_geometry_guess"),
                             "grammar_error": reading.get("grammar_error"),
                             "wall_relation": (reading.get("facts") or {}).get("wall_relation"),
                             "width_percentile": (reading.get("facts") or {}).get(
                                 "opening_width_percentile"),
                             "geometry_guess": reading.get("geometry_guess"),
                             "representative": (family.get("representative") or {}).get("id"),
                             "representative_reason": family.get("representative_reason")})
            # 请求留档：提示词原文 + 每张图元数据 + 模型原始答复
            audit_rows.append({"family": family.get("id"),
                               "status": reading.get("status"),
                               "request": reading.get("request"),
                               "reply": reading.get("gateway"),
                               "raw": reading.get("raw"),
                               "raw_text": reading.get("raw_text"),
                               "calls": reading_calls,
                               "closeup": reading.get("closeup")})
            if reading.get("status") == "ok":
                category = reading.get("category")
                confidence = str(reading.get("confidence") or "").lower()
                need = str(prof.get("propagate_confidence") or "high").lower()
                order = {"low": 0, "medium": 1, "high": 2}
                enough = (confidence == "high" if category == "column"
                          else order.get(confidence, -1) >= order.get(need, 2))
                # 推广影响大的族必须**两次独立判读一致**才允许整族推广：
                # 实测同一张图上同一个族两次分别被说成"扶手椅符号"和"双扇门"、
                # "填充补丁"和"柱墩"，单次 high 就推广会把 66 扇门/28 根柱一次性写进结果。
                corroborate = int(prof.get("corroborate_instances") or 0)
                big = corroborate > 0 and int(family.get("n") or 0) >= corroborate
                # Scope can only narrow, including negative answers. The current
                # propagator does not enforce explicit representative scope for
                # all categories, so retain these families pending below.
                def restrict_scope(answer):
                    if (answer.get("authorization_scope") == "representative_only"
                            or answer.get("scope") == "representative_only"
                            or answer.get("family_consistent") is False
                            or answer.get("outliers")):
                        family["authorization_scope"] = "representative_only"

                restrict_scope(reading)
                family_scope = family.get("authorization_scope", "family") == "family"
                needs_second = bool(big and family_scope and enough
                                    and category in ("door", "window", "column"))
                second = None
                if (needs_second
                        and calls < total_calls - reserve):
                    second = DETECTOR.read_family(
                        family, ir=ir, graph=graph_obj, gateway=gateway,
                        snapshot=snapshot, walls=walls, failures=failures,
                        widths=widths,
                        budget={"images_per_family": int(
                                    prof.get("images_per_family") or 3),
                                "samples": int(prof.get("family_samples") or 3),
                                "closeup": 0, "verification": True})
                    second_calls = max(0, int(second.get("calls", 1)))
                    calls += second_calls
                    detector_calls += second_calls
                    restrict_scope(second)
                    audit_rows.append({"family": family.get("id"),
                                       "verification": "second_read",
                                       "status": second.get("status"),
                                       "request": second.get("request"),
                                       "reply": second.get("gateway"),
                                       "raw": second.get("raw"),
                                       "raw_text": second.get("raw_text"),
                                       "calls": second_calls,
                                       "closeup": second.get("closeup")})
                    readings[-1]["second_read"] = {
                        "status": second.get("status"),
                        "category": second.get("category"),
                        "confidence": second.get("confidence"),
                        "calls": second_calls,
                        "scope": second.get("authorization_scope") or second.get("scope"),
                        "images": second.get("images")}
                agreed = not needs_second
                if needs_second and second is None:
                    failures.append({"code": "FAMILY_UNREAD",
                                     "family": family.get("id"),
                                     "detail": "CORROBORATION_BUDGET_UNAVAILABLE"})
                if second is not None:
                    second_cat = (second.get("category")
                                  if second.get("status") == "ok" else None)
                    second_conf = str((second or {}).get("confidence") or "").lower()
                    second_enough = (
                        second_conf == "high" if second_cat == "column"
                        else order.get(second_conf, -1) >= order.get(need, 2))
                    same_category = bool(second_cat == category)
                    agreed = bool(same_category and second_enough)
                    family["confirmations"] = 2 if agreed else 1
                    if not agreed:
                        family["model_disagreement"] = {
                            "first": [category, confidence],
                            "second": [second_cat, second_conf]}
                        # 两次类别不同才是真冲突；类别一致但把握不够只是没到门槛
                        failures.append({
                            "code": ("FAMILY_UNREAD" if same_category or second_cat is None
                                     else "FAMILY_CONFLICT"),
                            "family": family.get("id"),
                            "detail": "%s/%s vs %s/%s" % (
                                category, confidence, second_cat, second_conf)})
                family_scope = family.get("authorization_scope", "family") == "family"
                readings[-1]["authorization_scope"] = family.get("authorization_scope", "family")
                singleton = (len(family.get("instances") or ()) == 1
                             and int(family.get("n") or 1) == 1)
                authorized = family_scope or singleton
                if not authorized:
                    family["pending_reason"] = "REPRESENTATIVE_SCOPE_REQUIRES_REVIEW"
                if category == "none" or category in ("wall",):
                    # 模型说这是墙/家具/图例：整个族否掉，不推广
                    if authorized:
                        family["verdict"] = "reject"
                    family["model_negative"] = reading.get("negatives")
                else:
                    family["category"] = category
                    family["subtype"] = reading.get("subtype")
                    family["model"] = {"category": category,
                                       "subtype": reading.get("subtype"),
                                       "confidence": confidence}
                    # 模型说成员不一致：只授权代表实例，整族不许自动推广
                    family["model_consistent"] = reading.get("family_consistent")
                    family["model_outliers"] = reading.get("outliers")
                    # 只有模型确认到指定把握的族才允许推广（默认 high）
                    family["confirmed"] = bool(enough and agreed and authorized)
                    if not family["confirmed"] and agreed:
                        failures.append({"code": "FAMILY_UNREAD",
                                         "family": family.get("id"),
                                         "detail": (family["pending_reason"] if not authorized
                                                    else "confidence=%s < %s" % (confidence, need))})
            else:
                failures.append({"code": "FAMILY_UNREAD",
                                 "family": family.get("id"),
                                 "detail": reading.get("reason")})
        model_audit.extend(audit_rows)
        family_observation.update({
            "min_families": min_families, "max_families": max_families,
            "observed": len(readings),
            "unobserved": max(0, len(FAMILY.observation_queue(
                families, categories=PROFILES.categories(prof))) - len(readings)),
            "reserved_calls": reserve,
            "by_category": dict(category_counts),
            "calls_remaining": max(0, total_calls - calls)})
        if family_observation["stopped"] == "off":
            family_observation["stopped"] = "MAX_FAMILIES"

    def _arbitration_rows(pool_dict, *, targets, per_category=24):
        """挑给模型看的候选：每个目标类别按分数取前 N 个（足够覆盖冲突即可）。"""
        wanted = set(targets or ())
        buckets: dict = {}
        for hyp in pool_dict.get("hypotheses") or ():
            cat = str(hyp.get("category"))
            if cat in wanted:
                buckets.setdefault(cat, []).append(hyp)
        rows: list = []
        for cat, items in buckets.items():
            items.sort(key=lambda h: -float(h.get("score") or 0.0))
            rows.extend(items[:per_category])
        rows.sort(key=lambda h: -float(h.get("score") or 0.0))
        return rows

    def _constraints_from(arb, rows_all):
        """裁决 -> 求解约束。

        * 模型说"不要"的：硬约束排除（它看过图与位置事实）；
        * 模型说"要"的：保留明确裁决，由求解器独立检查几何资格；
        * 类别改判：先进入待拟合，不沿用旧类别几何计数。
        """
        return _arbitration_constraints(arb, rows_all)

    def _merge(new_rows):
        seen = {(row.get("code"), str(row.get("detail") or "")) for row in failures}
        out = list(failures)
        for row in new_rows:
            key = (row.get("code"), str(row.get("detail") or ""))
            if key not in seen:
                seen.add(key)
                out.append(row)
        return out

    def _pool():
        return ENGINES.generate(
            ir, graph_obj, walls=walls, columns=columns, doors=doors,
            windows=windows, stairs=stairs, steps=steps, aprons=aprons,
            frames=frames, grid=grid, pattern_cases=pattern_cases,
            ai_grammars=ai_items, faces=faces, families=families,
            propagate=bool(prof.get("propagate")) and bool(families),
            categories=(PROFILES.categories(prof) if focused else None))

    def _resolve(pool_now, constraints=None):
        if focused:
            return FOCUS_RESOLVER.staged(pool_now, thickness=thickness,
                                         profile=prof, budget=budget,
                                         constraints=constraints)
        return RESOLVER.staged(
            pool_now, thickness=thickness, faces=faces,
            space_builder=(lambda selected: SPACE.solve(
                selected=list(space_walls) or selected,
                texts=ir.get("texts") or (), labels=labels,
                bbox=ir.get("bbox"), thick=thickness)) if rooms else None)

    pool_now = {"hypotheses": []}
    resolution: dict = {}
    if focused:
        pool_now = _pool()
        # ---- 3.5) 逻辑冲突：**先**把完整候选摆给模型按位置裁决，再按结论重解。
        # 以前是"先按分数淘汰、再把剩下的交给模型"：同位置的另一种解释早被删掉，
        # 模型根本看不到选项，只能对着半张桌子点头。
        arbitration = {"status": "off", "stats": {}, "verdicts": []}
        if model_on and prof.get("arbitration") and calls < int(budget["max_model_calls"]):
            rows_all = _arbitration_rows(pool_now, targets=PROFILES.categories(prof))
            arbitration = ARBITER.run(
                rows_all, ir=ir, gateway=gateway, snapshot=snapshot, walls=walls,
                grid=grid,
                budget={"points": min(int(prof.get("arbitration_points") or 4),
                                      max(0, int(budget["max_model_calls"]) - calls))})
            calls += int((arbitration.get("stats") or {}).get("calls", (arbitration.get("stats") or {}).get("decided")) or 0)
            constraints = _constraints_from(arbitration, rows_all)
        resolution = _resolve(pool_now, constraints)
        failures = _merge(_failure_digest(pool_now, resolution))
        rounds.append({"round": 1, "regions": [],
                       "pool": ENGINES.summary(pool_now),
                       "resolution": FOCUS_RESOLVER.summary(resolution),
                       "arbitration": ARBITER.summary(arbitration),
                       "constraints": {"drop": len((constraints or {}).get("drop") or ()),
                                       "keep": len((constraints or {}).get("keep") or ())},
                       "failures": failures[:8]})

        # ---- 4) 疑难构件主动复查（模型只否决/改判，不生成几何）
        if model_on and calls < int(budget["max_model_calls"]):
            remaining = int(budget["max_model_calls"]) - calls
            recheck_limit = min(remaining, int(budget.get("max_recheck", 4)))
            uncertain = [h for h in pool_now.get("hypotheses") or ()
                         if h.get("status") in ("uncertain", "pending")
                         and (h.get("evidence") or {}).get("pending_reason")
                         != "MODEL_FAMILY_REJECTED"][:recheck_limit]
            if uncertain:
                recheck = DETECTOR.recheck(uncertain, ir=ir, gateway=gateway,
                                           snapshot=snapshot, walls=walls,
                                           failures=failures, budget=dict(
                                               budget, recheck=recheck_limit,
                                               max_model_calls=remaining))
                calls += int(recheck["calls"])
                model_audit.extend(recheck.get("audit") or [])
                verdicts = recheck.get("verdicts") or []
                resolution, dropped = _apply_verdicts(resolution, verdicts, candidates=pool_now.get("hypotheses"),
                                                       resolve=lambda rows: _resolve({"hypotheses": rows}, constraints))
                rounds[-1]["reinspect"] = {"calls": recheck["calls"],
                                           "verdicts": verdicts}
    else:
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
                        focus_handles=_handles_for(facts.get("seed_uids")))
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
                if not grammars:
                    for case in facts.get("cases") or ():
                        tpl = G.for_kind(str(case.get("kind") or ""))
                        if tpl:
                            grammars.append(tpl)
                if grammars and mode == "enhance":
                    ai_items.append({"region": region,
                                     "seed_uids": facts.get("seed_uids"),
                                     "grammars": [g for g in grammars if g.get("category")],
                                     "case_id": (facts.get("cases") or [{}])[0].get("case_id")})
                round_rows.append({"region": region["id"], "priority": row["score"],
                                   "entities": facts.get("entities"),
                                   "grammars": len(grammars),
                                   "model_status": proposal.get("status"),
                                   "images": proposal.get("images"),
                                   "errors": proposal.get("errors")})
                if (time.perf_counter() - started) > float(budget["max_walltime_seconds"]):
                    break

            pool_now = _pool()
            resolution = _resolve(pool_now)
            failures = _failure_digest(pool_now, resolution)
            rounds.append({"round": round_no, "regions": round_rows,
                           "pool": ENGINES.summary(pool_now),
                           "resolution": RESOLVER.summary(resolution),
                           "failures": failures[:8]})
            if not failures or not model_on:
                break
            if (time.perf_counter() - started) > float(budget["max_walltime_seconds"]):
                break

    saved_coverage = OBS.make(identity, observed_ids, successful_ids, families)
    coverage = OBS.coverage(saved_coverage, previous_coverage, families,
                            [str(row["family"]["id"]) for row in FAMILY.observation_queue(
                                families, categories=PROFILES.categories(prof))])
    family_observation.update(coverage=coverage,
                              observed=coverage["round"]["attempted"]["count"],
                              unobserved=coverage["unseen"]["count"],
                              by_category=coverage["round"]["attempted"]["by_category"])
    vision_ok = any(v.get("ok") for v in vision_rows)
    return {"schema": SCHEMA, "observation_state": saved_coverage,
            "status": "ok" if (pool_now.get("hypotheses") or []) else "no_candidate",
            "profile": PROFILES.summary(prof),
            "pool": pool_now, "resolution": resolution, "rounds": rounds,
            "families": {"schema": FAMILY.SCHEMA,
                         "stats": family_result.get("stats") or {},
                         "top": FAMILY.summary(family_result).get("top") or [],
                         "count": len(families),
                         "confirmed": [f.get("id") for f in families
                                       if f.get("confirmed")],
                         "unconfirmed": len([f for f in families
                                             if not f.get("confirmed")
                                             and f.get("verdict") != "reject"]),
                         "rejected": [f.get("id") for f in families
                                      if f.get("verdict") == "reject"]},
            "arbitration": {"schema": ARBITER.SCHEMA,
                            "stats": (arbitration or {}).get("stats") or {},
                            "verdicts": ARBITER.summary(arbitration).get("verdicts"),
                            "constraints": {
                                "drop": sorted((constraints or {}).get("drop") or ()),
                                "keep": sorted((constraints or {}).get("keep") or ()),
                                "categories": (constraints or {}).get("categories") or {},
                            }},
            "unresolved": (pool_now.get("unresolved") or [])[:80],
            "detector": {"readings": readings,
                         "calls": detector_calls,
                         "observation": family_observation,
                         "understood": sum(1 for r in readings
                                           if r.get("status") == "ok"
                                           and r.get("category") not in (None, "none"))},
            # 每次请求的提示词、图片元数据与模型原始答复（可留存、可复盘）
            "model_audit": model_audit,
            "dropped": dropped,
            "vision": {"ok": vision_ok or bootstrap["images_seen"] > 0,
                       "regions": vision_rows,
                       "model_on": model_on,
                       "images_seen": int(bootstrap["images_seen"]) + int(
                           sum(int(r.get("images") or 0)
                               for rd in rounds for r in rd.get("regions") or ()))
                       + sum(int(r.get("images") or 0)
                              + int((r.get("second_read") or {}).get("images") or 0)
                              for r in readings),
                       "renderer": VISION.health()},
            "ai": {"mode": ai_cfg.get("mode") or "off", "model_on": model_on,
                   "calls": calls, "walltime_s": round(
                       time.perf_counter() - started, 1),
                   "prompt_version": PROMPT_VERSION},
            "bootstrap": {k: v for k, v in bootstrap.items() if k != "items"},
            "failures": failures[:12],
            "budget": budget}


def _has_hard_definition(hyp: dict) -> bool:
    """块参照/图库里定义好的构件：这是硬事实，模型说"不是"只能降级，不能删除。"""
    ev = hyp.get("evidence") or {}
    geom = hyp.get("geometry") or {}
    if "block" in (ev.get("methods") or ()):
        return True
    if ev.get("block_defined") or ev.get("catalog_block"):
        return True
    if str(ev.get("family_kind") or "") == "block" and ev.get("block_hit"):
        return True
    return bool(geom.get("block_defined"))


def _arbitration_constraints(arb, rows_all):
    """Only explicit answers for shown candidates can confirm a category."""
    known = {h.get("id") for h in rows_all or ()}
    drop, keep, categories = set(), set(), {}
    for verdict in arb.get("verdicts") or ():
        result = verdict.get("result") or {}
        if result.get("status") != "ok":
            continue
        invalid = {v.get("label") for v in result.get("invalid") or ()}
        shown = {v.get("label"): v.get("id") for v in result.get("shown") or ()}
        for label, decision in (result.get("per_candidate") or {}).items():
            hid = shown.get(label)
            if hid not in known or label in invalid or decision.get("why") == "legacy":
                continue
            if decision.get("keep") is False:
                drop.add(hid)
            elif decision.get("keep") is True:
                keep.add(hid)
            category = decision.get("category")
            if category in ("door", "window", "column", "none"):
                categories[hid] = category
    return {"drop": drop, "keep": keep - drop, "categories": categories}


def _apply_verdicts(resolution: dict, verdicts, *, candidates=None, resolve=None) -> tuple[dict, list]:
    """Apply explicit semantic decisions, then solve geometry/conflicts again."""
    from . import component_resolver as FOCUS_RESOLVER
    decisions = {v.get("id"): v for v in verdicts or () if isinstance(v, dict)}
    final = dict(resolution.get("final") or {})
    rows = list(candidates if candidates is not None else final.get("selected") or ())
    accepted, dropped = [], []
    for hyp in rows:
        decision = decisions.get(hyp.get("id")) or {}
        verdict, category = decision.get("verdict"), decision.get("category")
        if verdict == "reject":
            dropped.append({"id": hyp.get("id"), "category": hyp.get("category"),
                            "why": "MODEL_REJECT"})
            continue
        valid = (verdict == "keep" and category in ("door", "window", "column")
                 and decision.get("confidence") in ("high", "medium", "low"))
        if valid and category != hyp.get("category"):
            hyp = dict(hyp, status="pending", evidence=dict(
                hyp.get("evidence") or {}, category_confirmed=False,
                requested_category=category, geometry_status="pending",
                v1_reason="REFIT_REQUIRED"))
        elif valid and category == "column" and FOCUS_RESOLVER.column_geometry_ok(hyp):
            hyp = dict(hyp, status="candidate", evidence=dict(
                hyp.get("evidence") or {}, category_confirmed=True,
                category_basis="model_recheck"))
        accepted.append(hyp)
    solved = resolve(accepted) if resolve else FOCUS_RESOLVER.staged(accepted)
    solved["rejected"] = list(solved.get("rejected") or ()) + dropped
    return dict(resolution, **solved), dropped


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
    propagation = (((pool or {}).get("engines") or {}).get("propagation") or {})
    if propagation.get("status") == "no_candidate":
        _add("PROPAGATION_EMPTY")
    # 家具嫌疑：被选中的柱/门缺结构位置证据——这类要主动复查，而不是留着充数
    for hyp in ((resolution or {}).get("final") or {}).get("selected") or ():
        codes = [str(n.get("code") if isinstance(n, dict) else n)
                 for n in ((hyp.get("evidence") or {}).get("negatives") or ())]
        if "NO_STRUCTURAL_CONTEXT" in codes or "FURNITURE_CLUSTER" in codes:
            _add("FURNITURE_SUSPECTED", category=hyp.get("category"))
            break
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
    "FAMILY_UNREAD": "构件族没有拿到可用的语义判断",
    "PROPAGATION_EMPTY": "已确认的族在整张图里没有推广出实例",
    "FURNITURE_SUSPECTED": "候选更像家具/图例（缺结构位置证据）",
    "MODEL_REJECT": "模型复查判定不是该构件",
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
