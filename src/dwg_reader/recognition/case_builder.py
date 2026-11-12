"""CaseBundle 构建：把"一串 id"升级成"可判断的局部建筑关系 + 有限替代方案"。

所有数值都由确定性几何算出，LLM 只在这些**已算好的选项**里挑：

* 方向一律用**单位方向向量**算（``LinearSupport.angle`` 是**角度制**，旧版当成
  弧度做比较，1° 和 2° 被算成"差 0.03 rad 以内"，几乎全部判为平行）；
* 墙方向用 ``start -> end`` 的真实向量，不再用 bbox 长宽比猜 H/V（斜墙因此不再
  被判成水平墙）；
* 线束分组不再用固定的 600mm 阈值，而是按本区域**实测间距分布**（中位数的 3 倍
  或 2 倍实测板厚，取小者）切分，切分依据一并输出，便于审计；
* 原始证据账本里 weak / unused 的线也参与统计（``pool_support``），使"厚度簇证据
  弱"的墙不再从模型视野里消失。
"""
from __future__ import annotations

import math

MAX_PATTERNS = 8
MAX_ALTERNATIVES = 5
#: 方向聚类的角容差（度）——只影响"同一束"的分组粒度，不是几何判据
DIR_TOL_DEG = 1.5

#: 假设 -> 策略（与 plan_compiler.ACTION_STRATEGY 同源，这里给 LLM 看）
HYPOTHESIS_STRATEGY = {
    "window": "recover_openings", "door": "recover_openings",
    "opening_misassigned": "recover_openings",
    "wall": "recover_wall_network", "fragmented_wall": "recover_wall_network",
    "missing_wall_face": "recover_wall_network",
    "weak_thickness_cluster": "recover_wall_network",
    "single_line_wall": "recover_wall_network",
    "column": "recover_column_geometry", "decoration_fill": "recover_column_geometry",
    "duplicate_column": "recover_column_geometry",
    "broken_boundary": "recover_space_boundary", "room_needs_closure":
        "recover_space_boundary",
    "wrong_layer": "repair_semantics", "unknown_pattern": "repair_semantics",
}


# ------------------------------------------------------------------ 几何工具
def _in_region(b, region, pad=300.0) -> bool:
    if not region:
        return True
    if not b:
        return False  # no spatial evidence -> never qualify for a local case
    return not (b[2] < region[0] - pad or b[0] > region[2] + pad
                or b[3] < region[1] - pad or b[1] > region[3] + pad)


def _unit(d) -> tuple[float, float]:
    if isinstance(d, (list, tuple)) and len(d) >= 2:
        x, y = float(d[0]), float(d[1])
        n = math.hypot(x, y)
        if n > 1e-9:
            return (x / n, y / n)
    return (1.0, 0.0)


def _dir_of(rec) -> tuple[float, float]:
    d = rec.get("dir")
    if isinstance(d, (list, tuple)) and len(d) >= 2:
        return _unit(d)
    ang = rec.get("angle")
    if ang is None:
        return (1.0, 0.0)
    rad = math.radians(float(ang))          # angle 是**角度制**
    return (round(math.cos(rad), 9), round(math.sin(rad), 9))


def _nrm_of(d) -> tuple[float, float]:
    return (-d[1], d[0])


def _dot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1]


def _angle_between_deg(a, b) -> float:
    """两个方向向量之间的夹角（度，0~90）。"""
    c = min(1.0, max(-1.0, abs(_dot(a, b))))
    return math.degrees(math.acos(c))


def _projection_world(rec) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """线支撑 -> 世界坐标两端点（pos*n + u*d）。"""
    pos = rec.get("pos")
    lo, hi = rec.get("lo"), rec.get("hi")
    if pos is None or lo is None or hi is None:
        return None
    d = _dir_of(rec)
    n = _nrm_of(d)
    p = (float(pos) * n[0], float(pos) * n[1])
    return ((p[0] + float(lo) * d[0], p[1] + float(lo) * d[1]),
            (p[0] + float(hi) * d[0], p[1] + float(hi) * d[1]))


def _wall_vector(wall) -> tuple[tuple[float, float], tuple[float, float]] | None:
    s, e = wall.get("start"), wall.get("end")
    if isinstance(s, (list, tuple)) and isinstance(e, (list, tuple)):
        return ((float(s[0]), float(s[1])), (float(e[0]), float(e[1])))
    return None


def _interval(p0, p1, d) -> tuple[float, float]:
    u0, u1 = _dot(p0, d), _dot(p1, d)
    return (u0, u1) if u0 <= u1 else (u1, u0)


def _overlap_len(a0, a1, b0, b1) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _median(vals, default=0.0) -> float:
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return default
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _cluster_rows(supports, *, cut=None) -> tuple[list[list], dict]:
    """按法向位置把同向线分"行"，切分阈值由本区域实测间距分布决定。"""
    ordered = sorted(supports, key=lambda s: float(s.get("pos") or 0.0))
    gaps = [float(b.get("pos") or 0.0) - float(a.get("pos") or 0.0)
            for a, b in zip(ordered, ordered[1:])]
    positive = [g for g in gaps if g > 1.0]
    med = _median(positive, 0.0)
    if cut is None:
        cut = max(3.0 * med, 120.0) if positive else float("inf")
    rows: list[list] = []
    for rec in ordered:
        pos = float(rec.get("pos") or 0.0)
        if rows and pos - float(rows[-1][-1].get("pos") or 0.0) <= cut:
            rows[-1].append(rec)
        else:
            rows.append([rec])
    evidence = {"gap_median": round(med, 1), "cut": (round(cut, 1)
                                                     if cut != float("inf") else None),
                "rows": len(rows)}
    return rows, evidence


def _cluster_along(recs) -> list[list]:
    """同一"行"内按沿向区间切分：沿向空档超过实测中位间距 3 倍就算两段。

    少了这一步，图上 6 段互不相连但法向位置相同的墙会被并成一个长条，
    ``along_overlap`` 直接变 0，模型看到的就是一组假特征。
    """
    ordered = sorted(recs, key=lambda r: float(r.get("lo") or 0.0))
    if len(ordered) <= 1:
        return [ordered]
    gaps = []
    for a, b in zip(ordered, ordered[1:]):
        gaps.append(max(0.0, float(b.get("lo") or 0.0) - float(a.get("hi") or 0.0)))
    positive = [g for g in gaps if g > 1.0]
    cut = 3.0 * _median(positive, 0.0) if positive else float("inf")
    out: list[list] = []
    for rec in ordered:
        if out and float(rec.get("lo") or 0.0) - max(float(r.get("hi") or 0.0)
                                                     for r in out[-1]) <= max(cut, 150.0):
            out[-1].append(rec)
        else:
            out.append([rec])
    return out


# ------------------------------------------------------------------ 线束特征
def patterns(index: dict, region=None, *, limit: int = MAX_PATTERNS, walls=(),
             pool=None) -> list[dict]:
    """把区域内的线支撑按 (图层, 方向向量) 聚成"线束"，并给出**真实几何**特征。"""
    items = index.get("items") or {}
    weak_handles = {str(h) for h, rec in (pool or {}).items()
                    if rec.get("status") in ("weak", "unused")}
    groups: dict[tuple, list] = {}
    for rid, rec in items.items():
        if rec.get("kind") != "line_support" or rec.get("pos") is None:
            continue
        if not _in_region(rec.get("bbox"), region):
            continue
        d = _dir_of(rec)
        key = (rec.get("layer"),
               round(math.degrees(math.atan2(d[1], d[0])) % 180.0 / DIR_TOL_DEG))
        groups.setdefault(key, []).append(dict(rec, _rid=rid, _dir=d))

    wall_vecs = [(wid, _wall_vector(w)) for wid, w in
                 ((wid, rec) for wid, rec in items.items()
                  if rec.get("kind") == "wall_candidate")]
    wall_vecs = [(wid, v) for wid, v in wall_vecs if v]
    for w in walls or ():
        vec = _wall_vector(w)
        if vec:
            wall_vecs.append((w.get("id") or "wall", vec))

    out: list[dict] = []
    for (layer, _bucket), members in sorted(groups.items(),
                                            key=lambda kv: str(kv[0][0] or "")):
        if len(out) >= limit:
            break
        rows, row_evidence = _cluster_rows(members)
        spans_all: list[list] = []
        for members_row in rows:
            spans_all.extend(_cluster_along(members_row))
        for members_row in spans_all:
            if len(out) >= limit:
                break
            recs = list(members_row)
            d = recs[0]["_dir"]
            n = _nrm_of(d)
            pos_vals = [float(r.get("pos") or 0.0) for r in recs]
            pair_gaps = [b - a for a, b in zip(sorted(pos_vals), sorted(pos_vals)[1:])]
            thickness = _median([g for g in pair_gaps if 20.0 <= g <= 1000.0], None)

            # 平行一致性：两两夹角 < DIR_TOL_DEG 的比例
            comparisons = [_angle_between_deg(a["_dir"], b["_dir"]) <= DIR_TOL_DEG
                           for i, a in enumerate(recs) for b in recs[i + 1:]]
            parallel = round(sum(comparisons) / len(comparisons), 3) if comparisons else 1.0

            # 沿向真实区间重叠（不是 bbox 重叠）
            spans = [(float(r.get("lo") or 0.0), float(r.get("hi") or 0.0)) for r in recs]
            lo = max(s0 for s0, _s1 in spans)
            hi = min(s1 for _s0, s1 in spans)
            overlap_len = max(0.0, hi - lo)
            union = max(1e-9, max(s1 for _s0, s1 in spans) - min(s0 for s0, _s1 in spans))
            overlap_ratio = round(overlap_len / union, 3)

            p0 = (pos_vals[0] * n[0] + lo * d[0], pos_vals[0] * n[1] + lo * d[1])
            p1 = (pos_vals[0] * n[0] + hi * d[0], pos_vals[0] * n[1] + hi * d[1])

            align, span_overlap, nearest = 0.0, 0.0, None
            for wid, (ws, we) in wall_vecs:
                wd = _unit((we[0] - ws[0], we[1] - ws[1]))
                a = math.cos(math.radians(_angle_between_deg(d, wd)))
                w0, w1 = _interval(ws, we, d)
                ov = _overlap_len(lo, hi, w0, w1) / max(1e-9, hi - lo)
                # 最近墙按"与线束同向 + 沿向重叠"的综合分数挑，重叠为 0 也要给出
                if nearest is None or ov > span_overlap:
                    nearest, align, span_overlap = wid, round(a, 3), round(ov, 3)

            handles = [r.get("handle") for r in recs if r.get("handle")]
            pool_hits = [h for h in handles if str(h) in weak_handles]
            out.append({
                "id": recs[0]["_rid"],
                "group": f"pattern:{len(out) + 1}",
                "kind": "short_parallel_bundle" if len(recs) >= 2 else "single_support",
                "layer": layer, "line_count": len(recs),
                "dir": [round(d[0], 6), round(d[1], 6)],
                "angle_deg": round(math.degrees(math.atan2(d[1], d[0])) % 180.0, 2),
                "source_handles": handles[:8],
                "target_ids": [r["_rid"] for r in recs][:24],
                "parallel_consistency": parallel,
                "pair_gap_mm": (round(thickness, 1) if thickness else None),
                "along_overlap_mm": round(overlap_len, 1),
                "span_overlap_ratio": overlap_ratio,
                "wall_alignment": align, "span_overlap": span_overlap,
                "nearby_wall": nearest,
                "pool_support": len(pool_hits),
                "bbox": [round(min(p0[0], p1[0]), 1), round(min(p0[1], p1[1]), 1),
                         round(max(p0[0], p1[0]), 1), round(max(p0[1], p1[1]), 1)],
                "row_evidence": row_evidence,
            })
    return out


# ------------------------------------------------------------------ 替代方案
def alternatives(case: dict, index: dict, *, pool=None) -> list[dict]:
    """按问题类型给出 2~5 个**合法**替代方案（策略 + 兼容旧版 operation 字段）。"""
    code = str(case.get("reason") or "")
    pats = case.get("patterns") or []
    targets = [t for p in pats for t in (p.get("target_ids") or ())][:24] or \
              [i for i in (case.get("evidence_ids") or ()) if i.startswith(("pat:", "pattern:"))]
    alts: list[dict] = []

    def add(op, hyp, ids, why):
        if not ids:
            return
        alts.append({"id": f"alt:{len(alts) + 1}", "operation": op,
                     "strategy_id": HYPOTHESIS_STRATEGY.get(hyp or "", None)
                     or _strategy_of_operation(op),
                     "hypothesis": hyp, "target_ids": list(ids)[:24],
                     "expected": why})

    if code.endswith("EVIDENCE_WITHOUT_CANDIDATES"):
        add("regenerate_opening", "window", targets, "线束重组为洞口候选")
        add("retry_wall_pairing", "fragmented_wall", targets, "线束重试配对为墙候选")
        add("extract_contour", "column", targets, "闭合轮廓提取为柱候选")
    elif code.startswith("SEMANTIC_GAP"):
        layers = [f"layer:{d['layer']}" for d in (case.get("detail") or {}).get("layers") or ()]
        add("propose_layer_role", None, layers, "为该图层补语义并重算")
        add("extract_contour", "column", targets, "先按几何提取候选")
    elif "CONFLICT" in code:
        ids = [i for i in (case.get("evidence_ids") or ()) if i.startswith(("op:", "wall:"))]
        add("resolve_conflicts", None, ids, "按互斥规则裁决")
        add("merge_duplicate", "duplicate_column", ids, "同名/同位置去重")
    elif "BROKEN_WALL" in code or "UNPAIRED" in code:
        add("retry_wall_pairing", "fragmented_wall", targets, "未配对墙面重试配对")
        add("single_line_wall", "single_line_wall", targets, "只有一面线时按单线墙试算")
        add("try_virtual_boundary", "broken_boundary", targets, "只补虚拟边做闭合试算")
    else:
        ids = [i for i in (case.get("evidence_ids") or ())
               if i.startswith(("face:", "label:", "wall:"))]
        add("rebuild_space_faces", "room_needs_closure", ids or targets, "重建区域空间面域")
        add("retry_wall_pairing", "missing_wall_face", targets, "重建区域墙网")

    alts.append({"id": f"alt:{len(alts) + 1}", "operation": "NO_ACTION",
                 "strategy_id": None, "hypothesis": None, "target_ids": [],
                 "expected": "证据不足，保持未决"})
    return alts[:MAX_ALTERNATIVES]


def _strategy_of_operation(op: str) -> str | None:
    from .plan_compiler import ACTION_STRATEGY
    return ACTION_STRATEGY.get(str(op))


def build(case: dict, index: dict, *, walls=(), pool=None) -> dict:
    """把闸门的裸 case 变成有信息量的证据包（几何特征 + 替代方案 + 可选策略）。"""
    region = case.get("region")
    pats = patterns(index, region, walls=walls, pool=pool)
    detail = dict(case.get("detail") or {})
    enriched = dict(case)
    enriched["patterns"] = pats
    enriched["layer_semantics"] = detail.get("layer_semantics") or {}
    enriched["alternatives"] = alternatives(enriched, index, pool=pool)
    enriched["strategies"] = _strategies_for(enriched)
    enriched["pool_evidence"] = _pool_evidence(index, region, pool)
    return enriched


def _strategies_for(case: dict) -> list[dict]:
    """本 case 允许尝试的策略（闭集，供 LLM 直接选 strategy_id）。"""
    from . import operator_specs as OPSPEC
    out: list[dict] = []
    seen = set()
    for alt in case.get("alternatives") or ():
        sid = alt.get("strategy_id")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        st = OPSPEC.strategy(sid)
        if st is None:
            continue
        out.append({"strategy_id": sid, "goal": st.goal,
                    "hypotheses": list(st.hypotheses),
                    "steps": list(st.steps),
                    "target_ids": list(alt.get("target_ids") or ())[:24]})
    return out


def _pool_evidence(index: dict, region, pool) -> dict:
    """未利用图元的可复用情况（weak / unused 分图层计数）。"""
    if not pool:
        return {}
    by_layer: dict[str, dict] = {}
    for h, rec in pool.items():
        if rec.get("status") not in ("weak", "unused"):
            continue
        item = (index.get("items") or {}).get(f"ev:{h}") or {}
        if region and not _in_region(item.get("bbox"), region):
            continue
        lay = rec.get("layer") or "(无图层)"
        slot = by_layer.setdefault(lay, {"weak": 0, "unused": 0})
        slot[rec["status"]] = slot.get(rec["status"], 0) + 1
    return {"layers": dict(sorted(by_layer.items(),
                                  key=lambda kv: -(kv[1]["weak"] + kv[1]["unused"]))[:8]),
            "reusable": sum(v["weak"] + v["unused"] for v in by_layer.values())}


def _overlap_1d(a0, a1, b0, b1) -> float:
    lo, hi = max(a0, b0), min(a1, b1)
    if hi <= lo:
        return 0.0
    return (hi - lo) / max(1e-9, min(a1 - a0, b1 - b0))


# ------------------------------------------------------------------ 多模态案例
def case_payload(case: dict, index: dict, *, catalog=None, roi=None) -> dict:
    """PatternCase -> **可重放**的案例包：结构事实 + 图元上下文 + 可用算子/参数。

    关键点是"可重放"：给同一份 payload（同样的几何与同样的算子目录），换任何人
    重跑都应该得到同一组候选。所以这里只做取数，不做任何判断：

    * ``targets``    —— 允许被改动/新增构件的图元（``source_handles``）；
    * ``context``    —— 只读上下文（相邻墙、房名），模型可以看、不能改；
    * ``highlights`` —— 渲染时要高亮的图元（就是 targets）；
    * ``catalog``    —— 可用算子与可调参数（口径，不含图纸专属数值）。
    """
    items = (index or {}).get("items") or {}
    by_handle: dict = {}
    for rid, rec in items.items():
        handle = rec.get("handle")
        if handle:
            by_handle.setdefault(str(handle), []).append(rid)

    def _pick(handles):
        rows: list[dict] = []
        for handle in handles or ():
            for rid in by_handle.get(str(handle), ()):
                rec = items.get(rid) or {}
                rows.append({"evidence_id": rid, "handle": str(handle),
                             "kind": rec.get("kind"), "layer": rec.get("layer"),
                             "bbox": rec.get("bbox")})
        return rows

    targets = _pick(case.get("source_handles") or case.get("target_handles"))
    context = _pick(case.get("context_handles"))
    from . import operator_specs as OPSPEC
    from . import param_registry as PARAMS
    return {
        "schema": "pattern-case-payload/1.0",
        "case_id": case.get("id"),
        "kind": case.get("kind"),
        "region": case.get("region"),
        "bbox": case.get("bbox"),
        "features": case.get("features") or {},
        "baseline": case.get("baseline") or {},
        "targets": targets,
        "context": context,
        "highlights": [row["evidence_id"] for row in targets],
        "instances": case.get("instances") or [],
        "image_ref": case.get("image_ref"),
        "roi": roi or {},
        "catalog": catalog if catalog is not None else {
            "operators": OPSPEC.llm_selectable_operators(),
            "params": PARAMS.main_public_catalog(),
            "strategies": sorted(OPSPEC.STRATEGIES),
        },
        "permissions": {"targets": "read_write", "context": "read_only"},
    }
