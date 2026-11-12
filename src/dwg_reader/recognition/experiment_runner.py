"""多试验执行器：在**同一份基线状态**上试多组参数，只提交验收通过的那一次。

分工（这条分工是这个模块存在的理由）：

* 本模块只做**编排**：生成实验点、串行/并行调度、去重、记账、选优、预算刹车；
* 几何执行永远是 :func:`recompute.execute_local_subdag`（真实算子，不是并行新引擎）；
* 验收永远是 :meth:`local_solver.shadow_evaluate` + ``global_consistency``；
* 失败原因只发**机器可验证**的闭集码给模型（见 ``ai_planner.FAILURE_CODES``）。

两条容易写错、这里刻意写死的规则：

1. **每组试验都从同一份 baseline 开始**：失败的试验不会污染下一组（否则后一组
   的成功可能只是"捡了前一组的漏"）；
2. **提交是按 case 串行的事务**：一个 case 通过就把净变更合并进基线，下一个
   case 拿到的是**更新后**的基线（墙改过之后房间必须用同一版本的墙网）。
"""
from __future__ import annotations

import hashlib
import itertools
import json
import time

from . import param_registry as PARAMS

SCHEMA = "experiment-record/1.0"

#: 逻辑预算（次/秒/张），全部可在 ``ai_config.budget`` 里覆盖
BUDGET = {
    "max_trials_per_case": 12,
    "max_total_trials": 72,
    "max_model_rounds_per_case": 2,
    "max_regions": 12,
    "max_walltime_seconds": 180,
    "grid_points": 3,            # 每个参数在授权区间里取几个点（默认/低/中/高）
    "max_param_combos": 8,       # 多参数时组合数上限（笛卡尔积截断）
}


def _sha(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def digest_params(values: dict) -> str:
    raw = json.dumps(values, sort_keys=True, ensure_ascii=False, default=str)
    return _sha(raw)[:16]


def grid_points(plan, *, count: int | None = None, limit: int | None = None) -> list[dict]:
    """把 ``plan.param_ranges`` 展开成具体实验点（默认点优先，再低/中/高）。

    只用规则网格/随机点：首版不需要贝叶斯优化库，可复现比"聪明"重要。
    """
    ranges = dict(getattr(plan, "param_ranges", {}) or {})
    if not ranges:
        return [dict(getattr(plan, "param_values", {}) or {})]
    count = int(BUDGET["grid_points"] if count is None else count)
    limit = int(BUDGET["max_param_combos"] if limit is None else limit)
    per_param: list[list] = []
    names: list[str] = []
    for name, rng in ranges.items():
        names.append(name)
        if str(rng.get("type")) in ("float", "int"):
            per_param.append(PARAMS.points(name, rng["min"], rng["max"], count))
        else:
            per_param.append([PARAMS.spec(name).check(rng.get("value"))])
    combos: list[dict] = []
    for combo in itertools.islice(itertools.product(*per_param), limit):
        try:
            combos.append(PARAMS.validate_mapping(dict(zip(names, combo))))
        except PARAMS.ParameterError:
            continue
    if not combos:
        combos = [dict(getattr(plan, "param_values", {}) or {})]
    return combos


def failure_digest(history) -> list[dict]:
    """给模型看的失败摘要：只保留闭集码 + 一句机器生成的提示。"""
    out: list[dict] = []
    seen: set = set()
    for row in (history or ()):
        status = str(row.get("accept_status") or "")
        if status == "accepted":
            continue
        for code in (row.get("rejection_reasons") or ()):
            key = str(code)
            if key in seen:
                continue
            seen.add(key)
            out.append({"code": key, "hint": _HINTS.get(key, "")})
            if len(out) >= 6:
                return out
    return out


_HINTS = {
    "NO_HOST_WALL": "候选不贴任何已确认墙",
    "SOURCE_HANDLE_MISSING": "新几何追不到原始 handle",
    "GEOMETRY_SELF_INTERSECTION": "几何自交或退化",
    "DUPLICATE_CONFIRMED": "与已确认构件重复",
    "INSUFFICIENT_INDEPENDENT_EVIDENCE": "只有单一来源证据",
    "NO_VALID_CANDIDATE": "参数点没产出任何候选",
    "GLOBAL_TOPOLOGY_REGRESSION": "全局拓扑变差",
    "PARAM_UNCONSUMED": "参数没有被算子读取",
    "RANGE_EMPTY": "授权区间交集为空",
    "OUT_OF_BUDGET": "超出预算",
    "TARGET_KIND_MISMATCH": "目标类型与算子不匹配",
    "REGION_NOT_ALLOWED": "区域未授权",
    "EXECUTION_FAILED": "算子执行异常",
}


def record(*, case: dict, plan, params: dict, baseline_hash: str,
           result: dict | None, verdict: dict | None, accept: bool,
           reasons, duration_ms: float, index: int) -> dict:
    """一条实验记录（字段按方案 §3.4；JSON-native、可回放）。"""
    patch = (result or {}).get("patch") or {}
    body = {"case_id": case.get("id"), "plan": getattr(plan, "plan_hash", ""),
            "params": params}
    exp_id = "exp:" + _sha(json.dumps(body, sort_keys=True, default=str))[:12]
    steps = [s.get("operator") for s in (getattr(plan, "steps", []) or ())]
    return {
        "schema": SCHEMA,
        "experiment_id": exp_id,
        "index": int(index),
        "input_digest": str(case.get("source_digest") or ""),
        "region_id": str(case.get("region_id") or ""),
        "case_id": str(case.get("id") or ""),
        "base_state_hash": str(baseline_hash),
        "proposal_id": str(getattr(plan, "plan_hash", "")),
        "operator_versions": {str(s.get("operator")): str(s.get("version"))
                              for s in (getattr(plan, "steps", []) or ())},
        "operators": steps,
        "param_values": dict(params or {}),
        "param_hash": digest_params(params or {}),
        "output_patch_hash": _sha(json.dumps(patch, sort_keys=True,
                                             default=str))[:16] if patch else "",
        "guard_results": (verdict or {}).get("layers") or {},
        "metrics": {"counts_before": (result or {}).get("counts_before"),
                    "counts_after": (result or {}).get("counts_after"),
                    "changed": (result or {}).get("changed")},
        "executed_steps": len((result or {}).get("trace") or ()),
        "params_unused": list((result or {}).get("params_unused") or ()),
        "duration_ms": round(float(duration_ms), 1),
        "token_cost": 0,
        "accept_status": "accepted" if accept else "rejected",
        "rejection_reasons": [str(r) for r in (reasons or ())],
    }


def run_case(case: dict, *, baseline_state: dict, plans, trial_fn, validate_fn,
             budget: dict | None = None, base_state_hash: str = "",
             start_index: int = 0, history: list | None = None) -> dict:
    """对单个 case 跑完所有计划 × 实验点，返回最优的**单次**结果。

    ``trial_fn(plan, params)``   -> ``{"state","trace","changed","params_unused",...}``
    ``validate_fn(before, after, plan, params)`` -> ``{"ok","verdict","layers"}``

    返回 ``{"accepted": bool, "state": <新状态或 None>, "record": {...},
    "history": [...], "reasons": [...]}``——**不修改** ``baseline_state``。
    """
    budget = dict(BUDGET, **(budget or {}))
    max_trials = int(budget.get("max_trials_per_case") or BUDGET["max_trials_per_case"])
    rows: list[dict] = []
    tried: set = set()
    chosen = None
    index = int(start_index)
    for plan in (plans or ()):
        if not getattr(plan, "ok", False) or getattr(plan, "empty", True):
            continue
        for params in grid_points(plan):
            if index - int(start_index) >= max_trials:
                break
            key = (str(getattr(plan, "plan_hash", "")), digest_params(params))
            if key in tried:
                continue
            tried.add(key)
            started = time.perf_counter()
            cfg = PARAMS.config(params, origin={k: "experiment" for k in params})
            try:
                result = trial_fn(plan, cfg)
            except Exception as exc:                     # 执行体异常 = 这次试验失败
                rows.append(record(case=case, plan=plan, params=params,
                                   baseline_hash=base_state_hash, result=None,
                                   verdict=None, accept=False,
                                   reasons=["EXECUTION_FAILED"],
                                   duration_ms=(time.perf_counter() - started) * 1000.0,
                                   index=index))
                index += 1
                continue
            duration = (time.perf_counter() - started) * 1000.0
            unused = list((result or {}).get("params_unused") or ())
            if unused:
                # 编译进来却没人读的参数：这次试验不算数（否则就是"假装调过"）
                rows.append(record(case=case, plan=plan, params=params,
                                   baseline_hash=base_state_hash, result=result,
                                   verdict=None, accept=False,
                                   reasons=["PARAM_UNCONSUMED"],
                                   duration_ms=duration, index=index))
                index += 1
                continue
            status = str((result or {}).get("status") or "")
            if status in ("no_change", "empty", "budget"):
                rows.append(record(case=case, plan=plan, params=params,
                                   baseline_hash=base_state_hash, result=result,
                                   verdict=None, accept=False,
                                   reasons=["NO_VALID_CANDIDATE"],
                                   duration_ms=duration, index=index))
                index += 1
                continue
            verdict = validate_fn(baseline_state, (result or {}).get("state") or {},
                                  plan, params)
            reasons = []
            if not verdict.get("ok"):
                reasons = [str(verdict.get("verdict") or "REJECTED")] + [
                    k for k, v in (verdict.get("layers") or {}).items()
                    if not v.get("ok")]
            row = record(case=case, plan=plan, params=params,
                         baseline_hash=base_state_hash, result=result,
                         verdict=verdict, accept=bool(verdict.get("ok")),
                         reasons=reasons, duration_ms=duration, index=index)
            rows.append(row)
            index += 1
            if verdict.get("ok"):
                chosen = {"plan": plan, "params": params, "result": result,
                          "verdict": verdict, "record": row}
                break
        if chosen:
            break
    if history is not None:
        history.extend(rows)
    if chosen:
        return {"accepted": True, "state": chosen["result"].get("state"),
                "plan": chosen["plan"], "params": chosen["params"],
                "verdict": chosen["verdict"], "record": chosen["record"],
                "history": rows, "reasons": [], "trials": len(rows)}
    reasons = sorted({r for row in rows for r in (row.get("rejection_reasons") or ())})
    return {"accepted": False, "state": None, "plan": None, "params": {},
            "verdict": None, "record": rows[-1] if rows else None,
            "history": rows, "reasons": reasons, "trials": len(rows)}


def run(cases, *, baseline_state, plans_for, trial_fn, validate_fn, budget=None,
        diagnostics_path=None, recorder=None) -> dict:
    """对多个 case 串行提交：每个 case 从**当时**的基线开始试。

    ``plans_for(case)`` 返回该 case 可编译的计划列表（可以带模型提议）。
    """
    budget = dict(BUDGET, **(budget or {}))
    max_total = int(budget.get("max_total_trials") or BUDGET["max_total_trials"])
    max_regions = int(budget.get("max_regions") or BUDGET["max_regions"])
    max_ms = float(budget.get("max_walltime_seconds") or 0) * 1000.0
    started = time.perf_counter()
    work = baseline_state
    applied: list[dict] = []
    rejected: list[dict] = []
    history: list[dict] = []
    trials = 0
    stop_reason = ""
    for case in list(cases or ())[:max_regions]:
        if trials >= max_total:
            stop_reason = "BUDGET_TOTAL_TRIALS"
            break
        if max_ms and (time.perf_counter() - started) * 1000.0 > max_ms:
            stop_reason = "BUDGET_WALLTIME"
            break
        plans = list(plans_for(case) or ())
        if not plans:
            rejected.append({"case_id": case.get("id"), "reasons": ["NO_PLAN"]})
            continue
        base_hash = _sha(json.dumps(work, sort_keys=True, default=str))[:16]
        res = run_case(case, baseline_state=work, plans=plans, trial_fn=trial_fn,
                       validate_fn=validate_fn, budget=budget,
                       base_state_hash=base_hash, start_index=trials,
                       history=history)
        trials += int(res.get("trials") or 0)
        if res.get("accepted"):
            work = res["state"]
            applied.append({"case_id": case.get("id"),
                            "experiment_id": (res.get("record") or {}).get("experiment_id"),
                            "plan_hash": getattr(res.get("plan"), "plan_hash", ""),
                            "params": dict(res.get("params") or {}),
                            "verdict": (res.get("verdict") or {}).get("verdict"),
                            "trials": res.get("trials")})
        else:
            rejected.append({"case_id": case.get("id"),
                             "reasons": res.get("reasons") or [],
                             "trials": res.get("trials")})
        if diagnostics_path and recorder is not None:
            try:
                recorder(diagnostics_path, res.get("history") or [])
            except Exception:
                pass
    return {"schema": "experiment-run/1.0", "state": work, "applied": applied,
            "rejected": rejected, "history": history,
            "stats": {"cases": len(list(cases or ())), "applied": len(applied),
                      "rejected": len(rejected), "trials": trials,
                      "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1),
                      "stop_reason": stop_reason},
            "failure_digest": failure_digest(history)}


def append_jsonl(path: str, rows) -> None:
    """诊断日志（JSONL）：一次实验一行，便于事后回放。"""
    import os
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        for row in rows or ():
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                    default=str) + "\n")


def summary(result) -> dict:
    """进审计的摘要。"""
    res = result or {}
    return {"schema": "experiment-run/1.0", "stats": res.get("stats") or {},
            "applied": res.get("applied") or [], "rejected": res.get("rejected") or [],
            "history": len(res.get("history") or ()),
            "failure_digest": res.get("failure_digest") or []}
