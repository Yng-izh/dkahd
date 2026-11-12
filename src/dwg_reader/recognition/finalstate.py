"""final_candidate_state：全链路唯一的结果状态，下游只读它。

    {"walls", "columns", "doors", "windows", "virtual_edges", "unresolved", "provenance"}

三条硬规则（PR-0 修的就是这三条被破坏的地方）：

1. **补丁不可变**：``commit()`` 只读 ``patch``，所有标记（``accept_state``）都写在
   新对象上，调用方手里的补丁一字节不变；
2. **接受状态独立**：哪些候选被采纳记在 ``accept_state`` 字段里，不再靠往记录
   里塞 ``_llm_accept`` 这种副作用标记；
3. **原子提交**：先在副本上拼装、跑不变量检查，全部通过才整体替换；任何一项
   不通过就整段丢弃并把原因记进 ``unresolved``（不存在"提交了一半"）。
"""
from __future__ import annotations

import os

from . import recompute as RC

MAX_FACE_AREA = 5.0e9

#: 四种**来源语义**（方案 §8）：可观测 / 可复算 / 推断 / 未决
PROVENANCE_STATES = ("observed", "derived", "inferred", "unresolved")
INVARIANT_REASONS = ("WALL_WITHOUT_INTERVAL", "INFERRED_IN_EXPORT", "FACE_TOO_LARGE",
                     "PATCH_NOT_ACCEPTED")


def base_state(*, walls_seg=(), columns=(), doors=(), windows=(), openings_select=None,
               spaces=None) -> dict:
    sel = openings_select or {}
    doors_out = list(sel.get("doors") or doors or ())
    windows_out = list(sel.get("windows") or windows or ())
    return {
        "schema": "final-candidate-state/1.1",
        "walls": list(walls_seg or ()),
        "columns": list(columns or ()),
        "doors": doors_out,
        "windows": windows_out,
        "virtual_edges": ((spaces or {}).get("stats") or {}).get("virtual_edges") or [],
        "faces": list((spaces or {}).get("faces") or ()),
        "faces_inferred": False,
        "unresolved": [],
        "accept_state": {},
        "provenance": {"walls": len(walls_seg or ()),
                       "columns": len(columns or ()),
                       "doors": len(doors_out), "windows": len(windows_out),
                       "patch": "none"},
    }


def build(*, walls_seg=(), columns=(), doors=(), windows=(), openings_select=None,
          spaces=None, patch=None) -> dict:
    """基线状态 + 补丁 -> **新的**最终状态（原子提交 / 原子回滚）。"""
    base = base_state(walls_seg=walls_seg, columns=columns, doors=doors,
                      windows=windows, openings_select=openings_select, spaces=spaces)
    return commit(base, patch)


def commit(base: dict, patch: dict) -> dict:
    """原子提交：副本上拼装 -> 不变量检查 -> 通过才替换，否则整段回滚。"""
    state = _copy_state(base)
    status = (patch or {}).get("status")
    if status != "accepted":
        state["provenance"]["patch"] = str(status or "none")
        return state                     # 没被接受的补丁：只记状态，不动几何
    trial, accept = _apply(state, patch)
    problems = invariants(trial)
    if problems:
        state["unresolved"] = list(state.get("unresolved") or ()) + [
            {"reason": "COMMIT_REJECTED", "problems": problems}]
        state["provenance"]["patch"] = "rejected"
        state["provenance"]["commit_problems"] = problems
        return state
    trial["provenance"]["patch"] = "accepted"
    trial["provenance"]["patch_added"] = len((patch or {}).get("added") or ())
    trial["provenance"]["patch_updated"] = len((patch or {}).get("updated") or ())
    trial["provenance"]["patch_removed"] = len((patch or {}).get("removed") or ())
    trial["provenance"]["accepted"] = len(accept)
    trial["accept_state"] = accept
    return trial


def provenance_of(rec: dict) -> str:
    """一条记录的来源语义：观察到的、可复算的、推断的，还是还没定论。

    区分这四种不是学术洁癖：工程量只能算 ``observed`` / ``derived``；
    ``inferred`` 是"为了让拓扑闭合而假设的几何"，不能当真实构件计价。
    """
    if not isinstance(rec, dict):
        return "unresolved"
    if rec.get("geometry_state") == "inferred" or rec.get("inferred"):
        return "inferred"
    status = str(rec.get("status") or "")
    if status == "rejected":
        return "unresolved"
    if status in ("uncertain", "pending") or rec.get("evidence_state") in ("weak",
                                                                           "pending"):
        return "unresolved"
    if rec.get("geometry_state") == "derived" or rec.get("derived"):
        return "derived"
    if rec.get("handles") or rec.get("srcs") or rec.get("block"):
        return "observed"
    return "unresolved"


def annotate_provenance(state: dict) -> dict:
    """给每条记录打上 ``provenance_state``，并给出四类计数（只加字段，不改几何）。"""
    out = _copy_state(state)
    tally = {k: 0 for k in PROVENANCE_STATES}
    for bucket in ("walls", "columns", "doors", "windows"):
        recs = []
        for rec in (out.get(bucket) or ()):
            if not isinstance(rec, dict):
                recs.append(rec)
                continue
            row = dict(rec)
            row["provenance_state"] = provenance_of(row)
            tally[row["provenance_state"]] += 1
            recs.append(row)
        out[bucket] = recs
    for face in (out.get("faces") or ()):
        if isinstance(face, dict):
            tally["derived" if face.get("seal_state") != "hypothesis" else "inferred"] += 1
    out.setdefault("provenance", {})["states"] = tally
    return out


def invariants(state: dict) -> list[str]:
    """提交前不变量（闭集）。任何一条不满足 => 回滚。"""
    problems: list[str] = []
    apply_inferred = os.environ.get("DWG_APPLY_INFERRED") == "1"
    for w in state.get("walls") or ():
        if not isinstance(w, dict):
            continue
        if not (w.get("start") and w.get("end")) and not w.get("bbox"):
            problems.append("WALL_WITHOUT_INTERVAL")
        if w.get("geometry_state") == "inferred" and not apply_inferred:
            problems.append("INFERRED_IN_EXPORT")
    # An accepted patch may not create two records with the same ID.
    for bucket in ("walls", "columns", "doors", "windows", "faces"):
        ids = [RC._key(r) for r in (state.get(bucket) or ()) if RC._key(r) is not None]
        if len(ids) != len(set(ids)):
            problems.append("DUPLICATE_ID:" + bucket)
    for f in state.get("faces") or ():
        if isinstance(f, dict) and float(f.get("area") or 0.0) > MAX_FACE_AREA:
            problems.append("FACE_TOO_LARGE")
    return sorted(set(problems))


# ------------------------------------------------------------------ 补丁应用
def _apply(state: dict, patch: dict) -> tuple[dict, dict]:
    """把补丁写进副本；返回 (新状态, 接受表)。**不修改入参 patch。**"""
    out = _copy_state(state)
    accept: dict[str, str] = dict(out.get("accept_state") or {})
    region = patch.get("region")
    apply_inferred = os.environ.get("DWG_APPLY_INFERRED") == "1"
    # v2 patches are calculated from one baseline, so they express removals and
    # updates explicitly. Never apply them through region replacement/append.
    if patch.get("schema") == "candidate-patch/2":
        for bucket in ("walls", "columns", "doors", "windows"):
            delta = (patch.get("changes") or {}).get(bucket) or {}
            upserts = [dict(r) for r in (delta.get("upsert") or ())
                       if isinstance(r, dict)]
            if bucket == "walls":
                for r in upserts:
                    if r.get("geometry_state") == "inferred":
                        r["accept_state"] = "inferred"
                if not apply_inferred:
                    upserts = [r for r in upserts if r.get("geometry_state") != "inferred"]
            for r in upserts:
                r.setdefault("accept_state", "accepted")
                accept[str(r.get("id") or r.get("handle"))] = r["accept_state"]
            for rid in delta.get("remove_ids") or ():
                accept.pop(str(rid), None)
            out[bucket] = RC.apply_record_changes(
                out.get(bucket), upsert=upserts,
                remove_ids=delta.get("remove_ids") or ())
        if patch.get("faces_changed"):
            out["faces"] = [dict(r) for r in (patch.get("faces") or ())]
        if "faces_inferred" in patch:
            out["faces_inferred"] = bool(patch["faces_inferred"])
        if patch.get("inferred"):
            out["inferred_edges"] = list(patch.get("inferred") or ())
            out["inferred_edges_used"] = patch.get("inferred_edges_used") or 0
        if patch.get("unresolved"):
            out["unresolved"].extend(patch["unresolved"])
        return out, accept

    walls = [dict(w) for w in (patch.get("walls") or ()) if isinstance(w, dict)]
    inferred = [w for w in walls if w.get("geometry_state") == "inferred"]
    for w in walls:
        w.setdefault("accept_state", "accepted")
        if w.get("geometry_state") == "inferred":
            w["accept_state"] = "inferred"
    if inferred and not apply_inferred:                 # 推断墙默认不进导出
        walls = [w for w in walls if w.get("geometry_state") != "inferred"]
    out["inferred_walls"] = len(inferred)
    out["inferred_edges"] = list(patch.get("inferred") or ())
    out["inferred_applied"] = bool(inferred) and apply_inferred

    if walls:
        out["walls"] = RC.apply_to_walls(out["walls"], walls, region)
        for w in walls:
            accept[str(w.get("id"))] = w["accept_state"]

    columns = [dict(c, accept_state="accepted")
               for c in (patch.get("columns") or ()) if isinstance(c, dict)]
    if columns:
        out["columns"] = RC.merge_region(out["columns"], columns, region)
        for c in columns:
            accept[str(c.get("id") or c.get("handle"))] = "accepted"

    openings = [dict(o, accept_state="accepted")
                for o in (patch.get("openings") or ()) if isinstance(o, dict)]
    if openings:
        d, w = RC.apply_to_openings(out["doors"], out["windows"], openings, region)
        out["doors"], out["windows"] = d, w
        for o in openings:
            accept[str(o.get("id") or o.get("handle"))] = "accepted"

    if patch.get("faces"):
        # rebuild_space_faces 是整图重建结果，直接替换（按区域拼接会丢面域）
        out["faces"] = [dict(f) for f in patch["faces"] if isinstance(f, dict)]
    if patch.get("faces_inferred") is not None and (patch.get("faces")
                                                    or patch.get("faces_inferred")):
        out["faces_inferred"] = bool(patch.get("faces_inferred"))
        out["inferred_edges_used"] = patch.get("inferred_edges_used") or 0
    if patch.get("unresolved"):
        out["unresolved"] = list(out.get("unresolved") or ()) + [
            dict(u) if isinstance(u, dict) else {"reason": str(u)}
            for u in patch["unresolved"]]
    return out, accept


def _copy_state(state: dict) -> dict:
    out: dict = {}
    for k, v in (state or {}).items():
        if isinstance(v, list):
            out[k] = [dict(r) if isinstance(r, dict) else r for r in v]
        elif isinstance(v, dict):
            out[k] = dict(v)
        else:
            out[k] = v
    out.setdefault("unresolved", [])
    out.setdefault("accept_state", {})
    out.setdefault("provenance", {})
    return out


def accepted(state: dict, rid: str) -> bool:
    """某候选是否已被采纳（读独立接受表，不回写记录）。"""
    return (state.get("accept_state") or {}).get(str(rid)) in ("accepted", "inferred")


# --------------------------------------------------------------------------- 校准
#: 校准判据：独立证据方法数下限（1 条来源只能"待定"，不能 confirmed）
CALIBRATION_MIN_METHODS = 2
#: 低于该置信度的记录一律进 undecided（人工复核队列）
UNDECIDED_CONF = 0.70
#: 弱/待验证证据的置信度上限
WEAK_CAP = 0.65


def _methods(rec, category) -> list:
    """一条记录身上"彼此独立"的证据方法。"""
    out = []
    srcs = [s for s in (rec.get("srcs") or ()) if isinstance(s, dict)]
    handles = {str(s.get("handle")) for s in srcs if s.get("handle")}
    if len(handles) >= 2:
        out.append("multi_source")
    elif handles:
        out.append("single_source")
    for m in rec.get("methods") or ():
        out.append(f"method:{m}")
    if rec.get("faces") or rec.get("caps_n"):
        out.append("double_face")
    if rec.get("judged_role"):
        out.append("judged")
    if rec.get("hinges"):
        out.append("leaf")
    if category in ("door", "window") and rec.get("host_ok") is not None:
        out.append("host")
    if rec.get("geom_score") is not None and float(rec.get("geom_score") or 0) >= 0.6:
        out.append("geom_strong")
    return sorted(set(out))


def calibrate(state: dict, *, weak_cap: float = WEAK_CAP) -> dict:
    """置信度校准：把"看得见"与"证据够"分开，并显式产出未决队列。

    规则（全部可审计）：
    1) 独立方法数 < ``CALIBRATION_MIN_METHODS`` 的记录，状态最多 ``uncertain``；
    2) ``evidence_state`` 为 ``weak`` / ``pending`` 的记录，置信度封顶
       ``weak_cap``（弱厚度簇、靠拓扑保留的墙都属于这一类）；
    3) 拒收状态（``rejected``）不进未决队列，避免噪声；
    4) 置信度 < ``UNDECIDED_CONF`` 或标了 ``needs_review`` 的进 ``undecided``——
       这是"未决/人工复核"的唯一出口，导出时不会被当成确认构件。
    """
    out = _copy_state(state)
    undecided: list[dict] = []
    stats = {"confirmed": 0, "uncertain": 0, "pending": 0, "rejected": 0}
    mapping = (("walls", "wall"), ("columns", "column"), ("doors", "door"),
               ("windows", "window"))
    for bucket, category in mapping:
        recs = []
        for rec in out.get(bucket) or ():
            if not isinstance(rec, dict):
                recs.append(rec)
                continue
            r = dict(rec)
            methods = _methods(r, category)
            r["evidence_methods"] = methods
            r["evidence_n"] = len(methods)
            conf = r.get("confidence")
            try:
                conf = float(conf) if conf is not None else None
            except (TypeError, ValueError):
                conf = None
            if conf is None:
                conf = 0.9 if len(methods) >= CALIBRATION_MIN_METHODS else 0.5
            if r.get("evidence_state") in ("weak", "pending"):
                conf = min(conf, weak_cap)
            if len(methods) < CALIBRATION_MIN_METHODS:
                conf = min(conf, weak_cap)
            r["confidence"] = round(conf, 3)
            status = str(r.get("status") or "")
            if status != "rejected":
                if (r["evidence_n"] >= CALIBRATION_MIN_METHODS
                        and r.get("evidence_state") not in ("weak", "pending")
                        and conf >= UNDECIDED_CONF):
                    status = "confirmed"
                elif conf < UNDECIDED_CONF or r.get("evidence_state") in ("weak", "pending"):
                    status = "uncertain"
                else:
                    status = status or "uncertain"
            # 模型按截图否决、但与位置证据冲突的记录：无论置信度多少都要人工看
            if r.get("review_state") == "conflict":
                status = "uncertain"
                r["needs_review"] = True
            r["status"] = status
            r["needs_review"] = bool(r.get("needs_review") or status in ("uncertain", "pending"))
            stats[status if status in stats else "uncertain"] += 1
            if r["needs_review"]:
                undecided.append({"id": r.get("id") or r.get("handle"),
                                  "category": category,
                                  "confidence": r["confidence"],
                                  "evidence_n": r["evidence_n"],
                                  "evidence_state": r.get("evidence_state"),
                                  "reason": ("模型按截图否决，但与位置证据冲突"
                                             if r.get("review_state") == "conflict"
                                             else "证据方法不足"
                                             if r["evidence_n"] < CALIBRATION_MIN_METHODS
                                             else "弱/待验证证据" if r.get("evidence_state")
                                             in ("weak", "pending") else "置信度不足")})
            recs.append(r)
        out[bucket] = recs
    out["undecided"] = undecided
    out["calibration"] = {"stats": stats,
                          "undecided": len(undecided),
                          "min_methods": CALIBRATION_MIN_METHODS,
                          "undecided_conf": UNDECIDED_CONF,
                          "weak_cap": weak_cap}
    out.setdefault("provenance", {})["calibrated"] = len(undecided)
    # 来源语义（observed/derived/inferred/unresolved）：只加字段，不动几何
    out = annotate_provenance(out)
    return out
