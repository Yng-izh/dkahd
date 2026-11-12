"""局部联合求解：硬约束 -> 逐动作验收 -> 影子试算后提交（或整段回滚）。

状态是普通 dict（``walls / columns / doors / windows / faces / unresolved``），
执行体只返回补丁，由这里决定接受到什么程度：

* 硬约束：来源 handle、洞口区间、区域范围、门窗互斥——**无来源一律不 ok**；
* 影子验收 :func:`shadow_evaluate`：几何合法性 / 来源 / 拓扑 / **独立证据** /
  区域隔离 / 反循环自证，六层全部通过才算"证明变好"；
* 与基线比较：区分"没证明变好"和"证明变差"，两种都记录。

数量（新增几堵墙、几樘窗）**永远不能单独作为验收依据**：一个候选如果没有
第二条独立证据（另一面墙线、填充边界、尺寸、门块、真实轮廓），即使它能闭合
房间也只是"拓扑上可行"，不能升级成实体。
"""
from __future__ import annotations

import math
import os
import json
from functools import lru_cache

from . import evidence as EVID
from . import wallmodel as WM

MAX_ROUNDS = int(os.environ.get("DWG_LOCAL_MAX_ROUNDS", "3"))
MUTEX_RATIO = 0.6
REGION_PAD = 300.0
MAX_FACE_AREA = 5.0e9
MAX_FACE_VERTICES = 4000


class Plan:
    """兼容旧接口的薄封装（内部一律用 dict 状态）。"""

    def __init__(self, region=None, walls=(), openings=(), faces=(), columns=(),
                 unresolved=(), provenance=None, notes=None):
        self.region = list(region) if region else None
        self.walls = list(walls or ())
        self.openings = list(openings or ())
        self.faces = list(faces or ())
        self.columns = list(columns or ())
        self.unresolved = list(unresolved or ())
        self.provenance = dict(provenance or {})
        self.notes = dict(notes or {})

    def bump(self, kind, label):
        k = f"{kind}:{label}"
        self.provenance[k] = self.provenance.get(k, 0) + 1

    def as_state(self) -> dict:
        doors = [o for o in self.openings if _kind(o) == "door"]
        windows = [o for o in self.openings if _kind(o) != "door"]
        return {"walls": list(self.walls), "columns": list(self.columns),
                "doors": doors, "windows": windows, "faces": list(self.faces),
                "unresolved": list(self.unresolved)}

    def to_dict(self) -> dict:
        state = self.as_state()
        return {"region": self.region, "walls": state["walls"],
                "columns": state["columns"], "openings": self.openings,
                "faces": state["faces"], "unresolved": state["unresolved"],
                "provenance": self.provenance, "notes": self.notes}


def _kind(rec) -> str:
    return str(rec.get("kind") or rec.get("category") or "")


def _openings_of(state) -> list:
    return list(state.get("doors") or ()) + list(state.get("windows") or ())


def _ids_of(rec) -> set:
    out = set()
    for key in ("id", "handle"):
        if rec.get(key):
            out.add(str(rec[key]))
    for h in (rec.get("handles") or ()):
        if h:
            out.add(str(h))
    for src in (rec.get("srcs") or ()):
        if isinstance(src, dict) and src.get("handle"):
            out.add(str(src["handle"]))
    return out


def _sources_of(rec) -> set:
    """一个候选引用的**原始图元 handle**（不是它自己的 id）。"""
    out = set()
    for key in ("handle", "handles"):
        v = rec.get(key)
        if isinstance(v, str):
            out.add(v)
        elif isinstance(v, (list, tuple)):
            out.update(str(h) for h in v if h)
    for src in (rec.get("srcs") or ()):
        if isinstance(src, dict) and src.get("handle"):
            out.add(str(src["handle"]))
    for sup in (rec.get("supports") or ()):
        s = str(sup)
        if s.startswith("ev:"):
            out.add(s.split(":", 1)[-1])
    return out


@lru_cache(maxsize=32)
def _network_from_json(frozen_geometry: str) -> dict:
    return WM.network(json.loads(frozen_geometry))


def _network_cached(walls) -> dict:
    # WM.network is relatively expensive and `shadow_evaluate` compares the same
    # baseline across many failed plans. Cache by geometry, never object identity.
    stripped = [{k: w.get(k) for k in ("id", "start", "end", "orient", "pos",
                                       "span", "thickness", "bbox") if k in w}
                for w in walls]
    return _network_from_json(json.dumps(stripped, sort_keys=True, default=str))


# ------------------------------------------------------------------ 硬约束
def evaluate(plan_or_state, region=None, focus=None) -> dict:
    """硬约束 + 规模指标。

    ``focus`` 不为 None 时，**只把本次补丁涉及的候选**计入硬约束违例；
    基线里既有的历史问题另记 ``baseline_issues``，不阻塞提交。
    """
    state = plan_or_state.as_state() if isinstance(plan_or_state, Plan) else plan_or_state
    region = region or (plan_or_state.region if isinstance(plan_or_state, Plan) else None)
    walls = list(state.get("walls") or ())
    columns = list(state.get("columns") or ())
    openings = _openings_of(state)
    faces = list(state.get("faces") or ())
    violations: list[str] = []
    baseline: list[str] = []

    def _scoped(rec) -> bool:
        if focus is None:
            return True
        ids = _ids_of(rec)
        return bool(ids & focus)

    def _note(msg: str, rec) -> None:
        (violations if _scoped(rec) else baseline).append(msg)

    for rec in walls:
        if not _has_source(rec):
            _note(f"no_provenance:{rec.get('id')}", rec)
        if not (rec.get("start") and rec.get("end")) and not rec.get("bbox"):
            _note(f"no_interval:{rec.get('id')}", rec)
    for rec in columns:
        if not _has_source(rec):
            _note(f"no_provenance:{rec.get('id')}", rec)
    for rec in openings:
        if not _has_source(rec):
            _note(f"no_provenance:{rec.get('id') or rec.get('handle')}", rec)
        if not (rec.get("opening") or rec.get("axis")):
            _note(f"no_interval:{rec.get('id')}", rec)

    mutex = 0
    for i, a in enumerate(openings):
        for b in openings[i + 1:]:
            if _kind(a) == _kind(b):
                continue
            if _overlap(a, b) >= MUTEX_RATIO:
                mutex += 1
                _note(f"mutex:{a.get('id')}|{b.get('id')}", a)

    out_of_region = 0
    if region:
        for rec in walls + columns + openings:
            b = rec.get("bbox")
            if b and not _inside(b, region):
                out_of_region += 1
                _note(f"out_of_region:{rec.get('id')}", rec)

    huge_faces = [f for f in faces if (f.get("area") or 0) > MAX_FACE_AREA]
    hard = [v for v in violations
            if v.startswith(("no_provenance", "no_interval", "mutex", "out_of_region"))]
    return {"ok": not hard, "violations": violations,
            "baseline_issues": baseline,
            "walls": len(walls), "columns": len(columns),
            "openings": len(openings),
            "confirmed": float(sum(1 for o in openings
                                   if o.get("status") == "accept" or o.get("_llm_accept"))),
            "conflicts": mutex, "unresolved": len(state.get("unresolved") or ()),
            "faces": len(faces), "huge_faces": len(huge_faces),
            "out_of_region": out_of_region}


# ------------------------------------------------------------------ 影子指标
def metrics(state, support=None) -> dict:
    """验收用的独立指标（不看数量，看"有没有真实证据"）。"""
    state = state or {}
    walls = [w for w in (state.get("walls") or ()) if isinstance(w, dict)]
    columns = [c for c in (state.get("columns") or ()) if isinstance(c, dict)]
    openings = [o for o in _openings_of(state) if isinstance(o, dict)]
    faces = [f for f in (state.get("faces") or ()) if isinstance(f, dict)]
    support = set(str(s) for s in (support or ()))
    covered = set()
    thin, inferred, obs_walls = 0, 0, 0
    for w in walls:
        src = _sources_of(w)
        if w.get("geometry_state") == "inferred":
            inferred += 1
            continue
        obs_walls += 1
        if len(src) < 2:
            thin += 1
        covered |= (src & support) if support else src
    valid_faces = [f for f in faces
                   if len(f.get("polygon") or ()) >= 3
                   and (f.get("area") or 0) <= MAX_FACE_AREA
                   and len(f.get("polygon") or ()) <= MAX_FACE_VERTICES]
    paired_openings = sum(1 for o in openings if len(_sources_of(o)) >= 2)
    orphan_openings = sum(1 for o in openings if o.get("host_ok") is False)
    # 观测面域 vs 推断面域：推断（靠虚拟封边补出来的）面域不能当独立证据用
    faces_obs = sum(1 for f in valid_faces if f.get("seal_state") == "observed")
    faces_hyp = sum(1 for f in valid_faces if f.get("seal_state") == "hypothesis")
    net = _network_cached([w for w in walls if w.get("geometry_state") != "inferred"])
    # 面积标注一致性（面域自带 declared_area 时才算，避免拿不到标注就编一个）
    with_area = [f for f in valid_faces if f.get("declared_area")]
    agree = 0
    for f in with_area:
        try:
            want = float(f["declared_area"]) * 1.0e6
            got = float(f.get("area") or 0.0)
        except (TypeError, ValueError):
            continue
        if got > 0 and abs(got - want) / want <= 0.05:
            agree += 1
    return {"walls": len(walls), "observed_walls": obs_walls, "inferred": inferred,
            "thin_walls": thin, "columns": len(columns),
            "openings": len(openings), "paired_openings": paired_openings,
            "orphan_openings": orphan_openings,
            "faces": len(faces), "valid_faces": len(valid_faces),
            "faces_observed": faces_obs, "faces_hypothesis": faces_hyp,
            "wall_components": net["components"],
            "wall_isolated": net["isolated"],
            "wall_free_ends": net["free_ends"],
            "wall_gap_pairs": net["gap_pairs"],
            "wall_length": net["length"],
            "area_labeled": len(with_area),
            "area_agreement": round(agree / len(with_area), 4) if with_area else None,
            "support": len(support),
            "covered": len(covered),
            "coverage": round(len(covered) / len(support), 4) if support else 0.0,
            "unresolved": len(state.get("unresolved") or ()),
            "faces_area": round(sum(float(f.get("area") or 0.0) for f in valid_faces)
                                / 1.0e6, 2)}


def shadow_evaluate(*, before: dict, after: dict, support=None, plan=None,
                    region=None, independent: int = 0, global_check: bool = False,
                    require_independent: bool = False) -> dict:
    """影子验收：先在副本里算，再由独立证据决定能不能提交。

    六层判据，任何一层不过就回滚（``verdict`` 以 ``REJECT:`` 开头）：
    ``geometry`` / ``provenance`` / ``topology`` / ``evidence`` / ``isolation`` /
    ``no_self_proof``。
    """
    strategy = None
    if plan is not None:
        strategy = (plan.get("strategy_id") if isinstance(plan, dict)
                    else getattr(plan, "strategy_id", None))
    b = metrics(before, support)
    a = metrics(after, support)
    # 只对**本次改动涉及的候选**做硬约束判定：基线里既有的历史问题不阻塞提交
    focus = _focus(before, after)
    q = evaluate(after, region, focus=focus or None)
    qb = evaluate(before, region, focus=focus or None)
    layers: dict[str, dict] = {}

    layers["geometry"] = {
        "ok": not [v for v in q["violations"]
                   if v.startswith(("no_interval", "out_of_region"))],
        "violations": q["violations"][:6]}
    layers["provenance"] = {
        "ok": not [v for v in q["violations"] if v.startswith("no_provenance")],
        "thin_walls": a["thin_walls"]}
    # 墙网不能更碎：新增墙本身可以变成一个(或几个)新连通片，但不允许把原有的
    # 墙网拆散。所以容差 = 本次新增的观测墙数。
    added_walls = max(0, a["observed_walls"] - b["observed_walls"])
    net_ok = (a["wall_components"] <= b["wall_components"] + added_walls
              and a["wall_isolated"] <= b["wall_isolated"] + added_walls)
    layers["topology"] = {
        "ok": q["conflicts"] <= qb["conflicts"]
              and a["orphan_openings"] <= b["orphan_openings"]
              and net_ok,
        "conflicts": [qb["conflicts"], q["conflicts"]],
        "orphan_openings": [b["orphan_openings"], a["orphan_openings"]],
        "wall_components": [b["wall_components"], a["wall_components"]],
        "wall_isolated": [b["wall_isolated"], a["wall_isolated"]],
        "wall_free_ends": [b["wall_free_ends"], a["wall_free_ends"]],
        "wall_gap_pairs": [b["wall_gap_pairs"], a["wall_gap_pairs"]]}
    # 独立证据：原墙线被消化（coverage 上升），或新候选自带两条以上真实来源。
    # 面域收益分两种：**观测**面域（真实封边）本身就算独立证据；**推断**面域
    # （靠虚拟封边补出来的）只有在墙网同时没有变碎时才算——否则就是自证。
    gained_coverage = a["coverage"] > b["coverage"] + 1e-9 if a["support"] else False
    paired_gain = a["paired_openings"] > b["paired_openings"]
    obs_face_gain = a["faces_observed"] > b["faces_observed"]
    hyp_face_gain = (a["faces_hypothesis"] > b["faces_hypothesis"]) and net_ok
    real_face_gain = obs_face_gain or hyp_face_gain
    # 数量不算收益：支持池非空时，新增墙必须真的消化了池里的原始墙面线
    uncovered_gain = (a["observed_walls"] > b["observed_walls"]
                      and a["thin_walls"] <= b["thin_walls"]
                      and (not a["support"] or a["covered"] > b["covered"]))
    evidence_ok = bool(gained_coverage or paired_gain or real_face_gain
                       or uncovered_gain)
    layers["evidence"] = {"ok": evidence_ok,
                          "coverage": [b["coverage"], a["coverage"]],
                          "paired_openings": [b["paired_openings"], a["paired_openings"]],
                          "valid_faces": [b["valid_faces"], a["valid_faces"]],
                          "faces_observed": [b["faces_observed"], a["faces_observed"]],
                          "faces_hypothesis": [b["faces_hypothesis"], a["faces_hypothesis"]],
                          "observed_walls": [b["observed_walls"], a["observed_walls"]],
                          "observed_only": not (hyp_face_gain and not obs_face_gain)}
    outside = _changed_outside(before, after, region)
    layers["isolation"] = {"ok": outside == 0, "changed_outside_region": outside}
    needs_independent = strategy in ("recover_space_boundary",) or (
        a["inferred"] > b["inferred"]
        or any(isinstance(w, dict) and w.get("geometry_state") == "inferred"
               for w in (after.get("walls") or ())))
    layers["no_self_proof"] = {"ok": (not needs_independent) or independent >= 1,
                               "independent": int(independent),
                               "inferred": [b["inferred"], a["inferred"]]}
    if require_independent:
        layers["independent_support"] = {
            "ok": int(independent) >= 1, "independent": int(independent),
            "require": True}
    if global_check:
        gc = global_consistency(before=before, after=after, region=region)
        layers["global"] = {"ok": gc["ok"], "problems": gc["problems"][:6],
                            "checks": gc["checks"]}
    failed = [k for k, v in layers.items() if not v["ok"]]
    return {"ok": not failed, "verdict": "ACCEPTED" if not failed else
            "REJECT:" + ",".join(failed),
            "layers": layers, "before": b, "after": a,
            "quality_before": qb, "quality_after": q, "strategy": strategy}


def _focus(before: dict, after: dict) -> set:
    """本次补丁"动过"的候选 id 集合（用于把硬约束限定在本次改动上）。"""
    out: set = set()
    for bucket in ("walls", "columns", "doors", "windows"):
        prev = {_sig(r) for r in (before.get(bucket) or ()) if isinstance(r, dict)}
        for rec in (after.get(bucket) or ()):
            if isinstance(rec, dict) and _sig(rec) not in prev:
                out |= _ids_of(rec)
    return out


def _changed_outside(before, after, region) -> int:
    """区域外是否被改动（墙体修复不允许"墙外漏改"）。"""
    if not region:
        return 0
    n = 0
    for bucket in ("walls", "columns", "doors", "windows"):
        prev = {_sig(r) for r in (before.get(bucket) or ()) if isinstance(r, dict)
                and not _inside_rec(r, region)}
        cur = {_sig(r) for r in (after.get(bucket) or ()) if isinstance(r, dict)
               and not _inside_rec(r, region)}
        n += len(cur.symmetric_difference(prev))
    return n


def _sig(rec) -> tuple:
    """Detect geometry updates even when ID, bbox or face count is unchanged."""
    fields = ("bbox", "start", "end", "polygon", "outline", "axis", "opening",
              "geometry_state", "thickness", "status")
    return (str(rec.get("id") or rec.get("handle")),
            json.dumps({k: rec.get(k) for k in fields if k in rec},
                       ensure_ascii=False, sort_keys=True, default=str))


def _inside_rec(rec, region) -> bool:
    b = rec.get("bbox")
    return True if not b else _inside(b, region)


# ------------------------------------------------------------------ 基线比较
def improved(old: dict, new: dict) -> tuple[bool, str]:
    """基线比较（保留旧签名，供单测与审计使用）。"""
    if not new.get("ok"):
        return False, "HARD_CONSTRAINT_VIOLATED"
    if new.get("confirmed", 0.0) + 1e-9 < old.get("confirmed", 0.0):
        return False, "CONFIRMED_REGRESSION"
    if new.get("conflicts", 0) > old.get("conflicts", 0):
        return False, "CONFLICT_INCREASED"
    if new.get("unresolved", 0) > old.get("unresolved", 0):
        return False, "UNRESOLVED_INCREASED"
    if (new.get("conflicts", 0) == old.get("conflicts", 0)
            and new.get("unresolved", 0) == old.get("unresolved", 0)
            and new.get("walls", 0) <= old.get("walls", 0)
            and new.get("columns", 0) <= old.get("columns", 0)
            and new.get("openings", 0) <= old.get("openings", 0)):
        return False, "NO_PROVABLE_GAIN"
    return True, "ACCEPTED"


def verdict(action: str, old: dict, new: dict, patch: dict) -> tuple[bool, str]:
    """逐动作验收：硬约束 -> 回退检查 -> 该动作自己的**证据**判据。

    数量不再是判据：新增候选必须自带来源（墙 ≥ 两条线，洞口 ≥ 两条边），
    单纯"多了几个"只会得到 ``NO_PROVABLE_GAIN``。
    """
    if not new.get("ok"):
        return False, "HARD_CONSTRAINT_VIOLATED"
    if new.get("confirmed", 0.0) + 1e-9 < old.get("confirmed", 0.0):
        return False, "REGRESSION:CONFIRMED"
    if new.get("conflicts", 0) > old.get("conflicts", 0):
        return False, "REGRESSION:CONFLICTS"
    if new.get("unresolved", 0) > old.get("unresolved", 0):
        return False, "REGRESSION:UNRESOLVED"
    if new.get("out_of_region", 0) > old.get("out_of_region", 0):
        return False, "REGRESSION:OUT_OF_REGION"

    added = [r for r in (list(patch.get("walls") or ())
                         + list(patch.get("columns") or ())
                         + list(patch.get("openings") or ())) if isinstance(r, dict)]
    if not added:
        if patch.get("faces") and new.get("huge_faces", 0) <= old.get("huge_faces", 0):
            return True, "ACCEPTED"
        if patch.get("drop_ids") or patch.get("ignored"):
            return True, "ACCEPTED"
        return False, "NO_PROVABLE_GAIN"

    if action in ("regenerate_opening", "group_line_bundle", "extract_arc",
                  "treat_as_opening"):
        ok = all(len(_sources_of(o)) >= 2 for o in (patch.get("openings") or ())
                 if isinstance(o, dict))
        return (True, "ACCEPTED") if ok else (False, "NO_PROVABLE_GAIN")
    if action in ("regenerate_wall", "pair_parallel", "rebuild_wall_graph",
                  "merge_collinear", "retry_wall_pairing"):
        walls = [w for w in (patch.get("walls") or ()) if isinstance(w, dict)]
        ok = walls and all(len(_sources_of(w)) >= 2 for w in walls
                           if w.get("geometry_state") != "inferred")
        return (True, "ACCEPTED") if ok else (False, "NO_PROVABLE_GAIN")
    if action == "single_line_wall":
        walls = [w for w in (patch.get("walls") or ()) if isinstance(w, dict)]
        return (True, "ACCEPTED") if walls else (False, "NO_PROVABLE_GAIN")
    if action in ("extract_contour", "regenerate_column", "rebuild_contour"):
        cols = [c for c in (patch.get("columns") or ()) if isinstance(c, dict)]
        ok = cols and all(_sources_of(c) for c in cols)
        return (True, "ACCEPTED") if ok else (False, "NO_PROVABLE_GAIN")
    if action == "rebuild_space_faces":
        if patch.get("faces") and new.get("huge_faces", 0) <= old.get("huge_faces", 0):
            return True, "ACCEPTED"
        return False, "NO_PROVABLE_GAIN"
    if action in ("resolve_conflicts", "reconcile_openings"):
        if new["conflicts"] < old["conflicts"]:
            return True, "ACCEPTED"
        if action == "reconcile_openings" and added:
            return True, "ACCEPTED"
        return False, "NO_PROVABLE_GAIN"
    return True, "ACCEPTED"


# ------------------------------------------------------------------ 求解主循环
def reconcile(*, state: dict, actions, index: dict, region=None,
              rounds: int = MAX_ROUNDS, shadow: bool = False, support=None,
              strategy: str | None = None, independent: int = 0) -> dict:
    """逐动作执行 -> 验收 -> 通过则合入基线；返回补丁与前后指标。

    ``shadow=True`` 时使用 :func:`shadow_evaluate`（六层判据），并把每个动作的
    完整层结果写进 ``log``（审计可回放）。
    """
    from . import executors as EXEC

    base = {k: list(v) if isinstance(v, list) else v for k, v in (state or {}).items()}
    base.setdefault("unresolved", [])
    before = evaluate(base, region)
    log: list[dict] = []
    patch: dict = {"added": [], "removed": [], "drop_ids": [], "updated": [],
                   "walls": [], "columns": [], "openings": [], "faces": [],
                   "labels": [], "unresolved": [], "ignored": [], "dropped": [],
                   "inferred": []}
    current = base
    sup = set(str(s) for s in (support or ()))
    for r in range(1, max(1, rounds) + 1):
        if not actions:
            break
        for act in actions:
            name = act.get("action") or act.get("operation")
            if not name:
                continue
            try:
                step = EXEC.execute_operator(
                    name, target_ids=act.get("target_ids") or (),
                    region=region, evidence_index=index, candidate_state=current,
                    action=act)
            except Exception as exc:
                log.append({"round": r, "action": name, "verdict": "EXEC_FAILED",
                            "error": type(exc).__name__})
                continue
            if step.get("reason"):
                log.append({"round": r, "action": name,
                            "verdict": f"DROPPED:{step['reason']}"})
                if step["reason"].startswith(("NO_LOCAL_EVIDENCE", "PROVENANCE_LOST")):
                    patch["dropped"].append(name)
                continue
            trial = EXEC.apply_patch(current, step)
            focus = set(step.get("added") or ())
            for bucket in ("walls", "columns", "openings", "faces"):
                for rec in step.get(bucket) or ():
                    focus |= _ids_of(rec)
            after = evaluate(trial, region, focus=focus or None)
            indep = int(act.get("independent", independent) or 0)
            if shadow:
                ev = shadow_evaluate(before=current, after=trial, support=sup or None,
                                     plan={"strategy_id": act.get("strategy") or strategy},
                                     region=region, independent=indep)
                ok, why = ev["ok"], ev["verdict"]
                layers = ev["layers"]
            elif act.get("independent") is not None or indep:
                ok, layers = verdict_layers(name, before=before, after=after,
                                            patch=step,
                                            support={"independent": indep})
                why = "ACCEPTED" if ok else "LAYERED_REJECT:" + ",".join(
                    k for k, v in layers.items() if not v["ok"])
            else:
                ok, why = verdict(name, before, after, step)
                layers = {}
            log.append({"round": r, "action": name, "verdict": why,
                        "layers": layers,
                        "walls": after["walls"], "columns": after["columns"],
                        "openings": after["openings"], "conflicts": after["conflicts"],
                        "unresolved": after["unresolved"]})
            if not ok:
                if why.startswith("REGRESSION") or why.startswith("REJECT") \
                        or why == "HARD_CONSTRAINT_VIOLATED":
                    patch["dropped"].append(name)
                continue
            current = trial
            before = after
            for key in ("walls", "columns", "openings", "faces", "unresolved",
                        "labels", "ignored", "drop_ids", "updated"):
                patch[key] += list(step.get(key) or ())
            patch["added"] += list(step.get("added") or ())
            for e in (act.get("inferred_edges") or ()):
                patch["inferred"].append(dict(e, defect_id=act.get("defect_id")))
        actions = []                       # 一轮收敛，避免震荡

    after = evaluate(current, region)
    accepted = any(entry["verdict"] == "ACCEPTED" for entry in log)
    return {"state": current, "before": base, "after": current,
            "quality_before": evaluate(base, region), "quality_after": after,
            "patch": patch if accepted else _empty(patch),
            "log": log, "rounds": len(log),
            "status": "accepted" if accepted else ("rejected" if log else "no_action"),
            "verdict": ("ACCEPTED" if accepted else
                        ("REJECTED" if log else "NO_ACTION"))}


def _empty(patch: dict) -> dict:
    out = {k: [] for k in ("added", "removed", "updated", "walls", "columns",
                           "openings", "faces", "labels", "unresolved", "ignored",
                           "drop_ids", "dropped", "inferred")}
    out["dropped"] = list(patch.get("dropped") or ())
    return out


def _has_source(rec) -> bool:
    return bool(EVID.handles_of(rec) or (rec.get("handles") or ()))


def _overlap(a, b) -> float:
    sa = a.get("opening") or a.get("axis")
    sb = b.get("opening") or b.get("axis")
    if not sa or not sb:
        return 0.0
    (x1, y1), (x2, y2) = sa
    (u1, v1), (u2, v2) = sb
    l1 = math.hypot(x2 - x1, y2 - y1) or 1.0
    l2 = math.hypot(u2 - u1, v2 - v1) or 1.0
    nx, ny = -(y2 - y1) / l1, (x2 - x1) / l1
    if abs((u1 - x1) * nx + (v1 - y1) * ny) > 60.0:
        return 0.0
    dx, dy = (x2 - x1) / l1, (y2 - y1) / l1
    b1 = (u1 - x1) * dx + (v1 - y1) * dy
    b2 = (u2 - x1) * dx + (v2 - y1) * dy
    lo, hi = max(0.0, min(b1, b2)), min(l1, max(b1, b2))
    return max(0.0, hi - lo) / min(l1, l2)


def _inside(b, region) -> bool:
    return not (b[2] < region[0] - REGION_PAD or b[0] > region[2] + REGION_PAD
                or b[3] < region[1] - REGION_PAD or b[1] > region[3] + REGION_PAD)


def verdict_layers(action, *, before, after, patch, support=None) -> tuple[bool, dict]:
    """五层验收：几何合法性 / 来源与推断 / 拓扑一致 / 建筑证据 / 局部质量。"""
    support = support or {}
    layers = {}
    layers["geometry"] = {"ok": not [v for v in after["violations"]
                                     if v.startswith(("no_interval", "out_of_region"))],
                          "violations": after["violations"][:6]}
    layers["provenance"] = {"ok": not [v for v in after["violations"]
                                       if v.startswith("no_provenance")],
                            "inferred": patch.get("inferred_count", 0)}
    layers["topology"] = {"ok": after["conflicts"] <= before["conflicts"]
                          and after["out_of_region"] <= before["out_of_region"]}
    layers["building_evidence"] = {
        "ok": (support.get("independent", 0) >= 1
               or action not in ("try_virtual_boundary", "option:close_gap",
                                 "rebuild_space_faces")),
        "independent_support": support.get("independent", 0)}
    layers["local_quality"] = {
        "ok": (after["unresolved"] <= before["unresolved"]
               and (after["faces"] >= before["faces"]
                    or after["openings"] > before["openings"]
                    or after["walls"] > before["walls"]
                    or after["columns"] > before["columns"])),
        "faces": [before["faces"], after["faces"]]}
    return all(v["ok"] for v in layers.values()), layers


# ------------------------------------------------------------------ 独立证据
#: 一条证据是什么"来源"。同一来源出两条，不算两条独立证据。
SOURCE_KINDS = {"block": "块族", "geometry": "几何", "arc": "弧线", "hatch": "填充",
                "opening": "洞口边", "wall_gap": "墙缺口", "text": "文字",
                "label": "房名", "contour": "轮廓", "line_support": "线支撑"}


def evidence_sources(rec: dict) -> set:
    """一条候选记录能追到的**独立来源**（去重后按来源类别）。"""
    out: set = set()
    if not isinstance(rec, dict):
        return out
    if rec.get("block") or rec.get("block_name") or rec.get("family"):
        out.add("block")
    if rec.get("hinges") or rec.get("swing_angle") is not None or rec.get("arc"):
        out.add("arc")
    if rec.get("hatch") or rec.get("fill"):
        out.add("hatch")
    if rec.get("outline") or rec.get("contour"):
        out.add("contour")
    if rec.get("host_wall") or rec.get("wall_id"):
        out.add("wall_gap")
    for h in (rec.get("handles") or ()):
        out.add("line_support")
    for src in (rec.get("srcs") or ()):
        if isinstance(src, dict):
            out.add(str(src.get("kind") or src.get("role") or "line_support"))
    if rec.get("text") or rec.get("label"):
        out.add("text")
    return {s for s in out if s}


def independent_support(*, before: dict, after: dict, support=None,
                        patch: dict | None = None) -> dict:
    """本次新增候选的独立证据计数：**不同来源**才算不同证据。

    "同一束线出了两条评分"、"模型说了两次像窗"都不是独立证据。升级 ``confirmed``
    至少需要 1 项独立支持（配置 ``require_independent_support_for_confirmed``）。
    """
    added: list[dict] = []
    for bucket in ("walls", "columns", "doors", "windows", "openings"):
        prev = {_sig(r) for r in ((before or {}).get(bucket) or ())
                if isinstance(r, dict)}
        for rec in ((after or {}).get(bucket) or ()):
            if isinstance(rec, dict) and _sig(rec) not in prev:
                added.append(rec)
    for rec in ((patch or {}).get("walls") or ()) + ((patch or {}).get("openings") or ()):
        if isinstance(rec, dict):
            added.append(rec)
    rows = []
    for rec in added:
        srcs = evidence_sources(rec)
        rows.append({"id": rec.get("id") or rec.get("handle"),
                     "sources": sorted(srcs), "independent": len(srcs)})
    covered = set(str(s) for s in (support or ()))
    if covered:
        hit = sum(1 for rec in added
                  for h in (rec.get("handles") or ()) if str(h) in covered)
        if hit and not rows:
            rows.append({"id": "coverage", "sources": ["line_support"],
                         "independent": 1})
    best = max((r["independent"] for r in rows), default=0)
    return {"count": len(rows), "independent": best, "rows": rows[:12]}


def confirm_level(*, after: dict, independent: int, require_independent: bool = True,
                  has_model_text_only: bool = False) -> dict:
    """``confirmed`` / ``uncertain`` 的判定，连同**理由**一起返回。

    规则：
    * 只有模型文字判断、没有几何来源 -> ``uncertain``；
    * 需要独立支持时，独立来源 < 1 -> ``uncertain``；
    * 其余情况才允许 ``confirmed``（几何合法性由 ``global_consistency`` 另判）。
    """
    reasons: list[str] = []
    if has_model_text_only:
        reasons.append("MODEL_TEXT_ONLY")
    if require_independent and int(independent) < 1:
        reasons.append("INSUFFICIENT_INDEPENDENT_EVIDENCE")
    inferred_only = any(isinstance(w, dict) and w.get("geometry_state") == "inferred"
                        for w in ((after or {}).get("walls") or ()))
    if inferred_only and int(independent) < 1:
        reasons.append("INFERRED_ONLY")
    return {"status": "confirmed" if not reasons else "uncertain",
            "reasons": reasons}


# ------------------------------------------------------------------ 全局一致性
def global_consistency(*, before: dict, after: dict, region=None,
                       protected=()) -> dict:
    """跨 ROI 的全局检查：局部过、全局崩的补丁不许提交。

    五项（任一项失败即回滚）：

    ``protected_outside``  区域外被保护的已确认构件不得发生几何变化
    ``duplicate_ids``      同一状态里 id 不得重复（重复计数是最常见的假增长）
    ``host_valid``         洞口声称的宿主墙必须存在，且区间能被墙覆盖
    ``face_sane``          面域不得零面积/巨面积/退化多边形
    ``id_collision``       新构件 id 不得与既有构件撞号
    """
    problems: list[dict] = []
    before = before or {}
    after = after or {}

    outside = _changed_outside(before, after, region)
    if outside:
        problems.append({"code": "PROTECTED_OUTSIDE_CHANGED", "count": int(outside)})

    ids: dict[str, int] = {}
    for bucket in ("walls", "columns", "doors", "windows", "faces"):
        for rec in (after.get(bucket) or ()):
            if not isinstance(rec, dict):
                continue
            rid = rec.get("id") or rec.get("handle")
            if rid is None:
                continue
            key = f"{bucket}:{rid}"
            ids[key] = ids.get(key, 0) + 1
    dups = sorted(k for k, n in ids.items() if n > 1)
    if dups:
        problems.append({"code": "DUPLICATE_IDS", "ids": dups[:8]})

    wall_ids = {str(w.get("id") or w.get("handle"))
                for w in (after.get("walls") or ()) if isinstance(w, dict)}
    bad_hosts = []
    for bucket in ("doors", "windows", "openings"):
        for rec in (after.get(bucket) or ()):
            if not isinstance(rec, dict):
                continue
            host = rec.get("wall_id") or rec.get("host_wall")
            if host in (None, ""):
                continue
            if str(host) not in wall_ids and not str(host).startswith("gap:"):
                bad_hosts.append({"id": rec.get("id") or rec.get("handle"),
                                  "host": str(host)})
    if bad_hosts:
        problems.append({"code": "OPENING_HOST_MISSING", "rows": bad_hosts[:8]})

    bad_faces = []
    for face in (after.get("faces") or ()):
        if not isinstance(face, dict):
            continue
        area = float(face.get("area") or face.get("poly_area") or 0.0)
        poly = face.get("polygon") or ()
        if area <= 0 or len(poly) < 3:
            bad_faces.append({"id": face.get("id"), "why": "degenerate"})
        elif area > MAX_FACE_AREA:
            bad_faces.append({"id": face.get("id"), "why": "too_large"})
    if bad_faces:
        problems.append({"code": "FACE_UNSANITARY", "rows": bad_faces[:8]})

    collide = []
    for bucket in ("walls", "columns", "doors", "windows"):
        prev = {str(r.get("id") or r.get("handle")) for r in (before.get(bucket) or ())
                if isinstance(r, dict)}
        now = [str(r.get("id") or r.get("handle")) for r in (after.get(bucket) or ())
               if isinstance(r, dict)]
        for rid in now:
            if now.count(rid) > 1 and rid in prev:
                collide.append(f"{bucket}:{rid}")
    if collide:
        problems.append({"code": "ID_COLLISION", "ids": sorted(set(collide))[:8]})

    return {"ok": not problems, "problems": problems,
            "checks": {"protected_outside": outside, "duplicates": len(dups),
                       "bad_hosts": len(bad_hosts), "bad_faces": len(bad_faces),
                       "id_collisions": len(set(collide))}}
