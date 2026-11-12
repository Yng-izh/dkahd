"""残余图元发现：把"识别器没解释的几何"整理成可推理的 :class:`PatternCase`。

这一步**没有模型**，也**不改任何构件**。它只回答四个问题：

1. 画面上还剩哪些图元，是基础识别既没用、又没明确否决的？（账本 ``unused`` /
   ``weak`` / ``rejected``）
2. 这些残余彼此之间有没有**重复结构**？（同族块、同间距平行线束、形状归一后
   的哈希）——重复是唯一不依赖图纸语义的强线索。
3. 它们贴着什么？（墙、洞口、房名）——上下文要单独登记成 ``context_handles``，
   权限与 ``source_handles`` 不同：上下文只能读，**不能改**。
4. 哪个案例先看？分数只用于排队，**不等于识别可信度**（见 ``§7.2``）。

聚类不是"按图层名分组"——图层名是图纸的私有约定，换一张图就废。这里只用量：
角度、间距、邻接、重复、闭合性，再加一层块族几何指纹。

输出是 ``pattern-case/1.0`` 的 JSON 列表，任何字段都可由几何程序复算。
"""
from __future__ import annotations

import hashlib
import json
import math

from . import blockfamily as BLOCKFAM

SCHEMA = "pattern-case/1.0"

CASE_KINDS = ("line_bundle", "repeated_block", "residual_cluster", "open_contour",
              "unexplained_layer")

#: 逻辑预算（条/个/张），不是几何阈值
BUDGET = {
    "cases": 12,                 # 一次最多出多少个案例进队列
    "case_entities": 400,        # 单个 ROI 最多带多少图元
    "case_hypotheses": 6,        # 单案例最多几个候选假设
    "case_views": 3,             # 单案例最多几张诊断视图
    "halo_thickness": 2.0,       # ROI 外扩（墙厚的倍数），接缝处必须有 halo
    "min_cluster": 3,            # 残余聚类的最小图元数
    "family_min_instances": 3,   # 重复块族的最小实例数
    "bundle_min_lines": 3,       # 线束最小条数
    "spacing_rel_tol": 0.18,     # "等间距"的相对容差
    "bundle_overlap": 0.60,      # 线束沿向重叠占较短者的比例
    "angle_bucket_deg": 1.0,     # 角度分桶（只用于聚类，不用于判定）
    "neighbor_radius": 6.0,      # 上下文吸附半径（墙厚的倍数）
    "priority_floor": 0.05,
}

#: 图层名里出现这些词就当作"图例/标注污染"的风险信号（只降低队列优先级，
#: 不作为任何构件的判据——真正的判据永远是几何）
ANNOTATION_HINTS = ("图例", "legend", "标注", "dim", "说明", "注释", "表格",
                    "index", "详图", "大样", "材料", "做法", "填充", "hatch")


# --------------------------------------------------------------------- 小工具
def _sha(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _r(v, nd: int = 1) -> float:
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return 0.0


def _bbox_of(rec: dict):
    from . import evidence as EVID
    return EVID.bbox_of(rec)


def _union_bbox(boxes):
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    return [_r(min(b[0] for b in boxes)), _r(min(b[1] for b in boxes)),
            _r(max(b[2] for b in boxes)), _r(max(b[3] for b in boxes))]


def _expand(box, pad: float):
    return [_r(box[0] - pad), _r(box[1] - pad), _r(box[2] + pad), _r(box[3] + pad)]


def _inside(box, region, pad: float = 0.0) -> bool:
    if not box or not region:
        return False
    return not (box[2] < region[0] - pad or box[0] > region[2] + pad
                or box[3] < region[1] - pad or box[1] > region[3] + pad)


def _dist(box_a, box_b) -> float:
    """两个包围盒的间隙（相交为 0）。"""
    dx = max(0.0, max(box_a[0], box_b[0]) - min(box_a[2], box_b[2]))
    dy = max(0.0, max(box_a[1], box_b[1]) - min(box_a[3], box_b[3]))
    return math.hypot(dx, dy)


def _median(values, default=0.0) -> float:
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return default
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _basis(scale) -> dict:
    from . import scale as SC
    sc = scale or {}
    return {"thickness": float(sc.get("thickness") or SC.DEFAULT_THICKNESS),
            "door_w": float(sc.get("door_w") or SC.DEFAULT_DOOR_W),
            "noise": float(sc.get("noise") or SC.DEFAULT_NOISE),
            "text_h": float(sc.get("text_h") or SC.DEFAULT_TEXT_H)}


# --------------------------------------------------------------------- 证据池
def _pool(snapshot: dict) -> list[dict]:
    """图元池：模型空间 + 块展开几何，带 bbox 与原始 handle。"""
    out: list[dict] = []
    for rec in (snapshot.get("entities") or ()):
        out.append({"handle": str(rec.get("handle") or ""), "type": rec.get("type"),
                    "layer": rec.get("layer"), "bbox": _bbox_of(rec),
                    "block": rec.get("name") if rec.get("type") == "INSERT" else None,
                    "insert": rec.get("insert"), "text": rec.get("text"),
                    "derived": False})
    for rec in (snapshot.get("derived") or ()):
        out.append({"handle": str(rec.get("handle") or ""), "type": rec.get("type"),
                    "layer": rec.get("layer"), "bbox": _bbox_of(rec),
                    "block": None, "insert": None, "text": rec.get("text"),
                    "derived": True, "via": (rec.get("via") or {}).get("insert")})
    return [r for r in out if r["handle"]]


def _status_of(ledger, handle: str) -> str:
    rec = (ledger or {}).get(str(handle)) or {}
    return str(rec.get("status") or "unused")


def _explained_handles(columns, doors, windows, walls) -> set:
    """已经被基础识别确认/采用的 handle：算"已解释"，不进残余池。"""
    out: set = set()
    for rec in list(columns or ()) + list(doors or ()) + list(windows or ()):
        if not isinstance(rec, dict):
            continue
        for h in (rec.get("handles") or ()):
            out.add(str(h))
        if rec.get("handle"):
            out.add(str(rec["handle"]))
    for rec in (walls or ()):
        if not isinstance(rec, dict):
            continue
        for h in (rec.get("handles") or ()):
            out.add(str(h))
        for src in (rec.get("srcs") or ()):
            if isinstance(src, dict) and src.get("handle"):
                out.add(str(src["handle"]))
    return out


#: 这些角色说明"这一族大概不是结构构件"——只用来**压后续队列**，不作判定
NON_STRUCTURAL_ROLES = frozenset({"furniture", "ignore"})

#: "不打印层"的同义词。只在这里用（压 AI 队列），**不进** taxonomy：
#: taxonomy 会影响识别结论，这里只影响"要不要花一次模型调用"。
NO_PLOT_HINTS = ("不打印", "no-plot", "noplot", "no plot", "nonplot", "defpoints")

#: 计算/统计类图层（面积计算、建筑面积…）：它们画的不是构件，是统计用的多边形
NON_STRUCTURAL_HINTS = ("面积计算", "面积标注", "建筑面积", "area calc", "area-calc")

#: 结构性角色：出现任何一个就仍然值得看
STRUCTURAL_ROLES = frozenset({"wall", "column", "door", "window", "stair", "step",
                              "apron", "frame", "axis", "north", "room_name"})


def _roles_of(layers) -> set:
    """图层 -> 语义角色（用现成的名字规则，仅作风险提示）。"""
    from . import taxonomy as TAX
    out: set = set()
    for layer in layers or ():
        name = str(layer or "")
        hit = TAX.match_layer(name)
        if hit:
            out |= set(hit)
        low = name.lower()
        if any(h in low for h in NO_PLOT_HINTS):
            out.add("ignore")
        if any(h in low for h in NON_STRUCTURAL_HINTS):
            out.add("ignore")
    return out


def _non_structural(layers) -> bool:
    """整族图层都指向"家具/图例/不打印"这类非结构语义时为 True。

    这条**不是判定**：真正决定"是不是构件"的永远是几何与验收。它只回答
    "值不值得花一次模型调用"——家具块重复 17 次也会拿高分，但方案要的 11 类
    构件里没有家具。
    """
    roles = _roles_of(layers)
    if not roles:
        return False
    return not (roles & STRUCTURAL_ROLES) and bool(roles & NON_STRUCTURAL_ROLES)


def _representative(insts, *, basis):
    """同族多实例里挑一个"最典型"的实例做截图（按面积中位数最接近者）。"""
    rows = []
    for inst in insts or ():
        box = _bbox_of(inst)
        if not box:
            continue
        area = max(1.0, (box[2] - box[0]) * (box[3] - box[1]))
        rows.append((area, box, inst))
    if not rows:
        return None
    areas = sorted(r[0] for r in rows)
    mid = areas[len(areas) // 2]
    areas.sort(key=lambda a: abs(a - mid))
    target = areas[0]
    box = next(b for a, b, _i in rows if a == target)
    pad = float(BUDGET["halo_thickness"]) * float(basis["thickness"])
    return _expand(box, pad)


def _annotation_risk(layers) -> float:
    """图例/标注污染风险（0~1）：只影响排队顺序，从不参与构件判定。"""
    names = [str(l or "").lower() for l in (layers or ()) if l]
    if not names:
        return 0.0
    hits = sum(1 for n in names if any(h in n for h in ANNOTATION_HINTS))
    return round(hits / float(len(names)), 3)


# --------------------------------------------------------------------- 线束聚类
def _line_bundles(lines, *, basis, residual: set) -> list[dict]:
    """平行等距线束：窗族/柱族/洞口框最常见、也最容易被规则漏掉的画法。

    只做聚类：把同层、同角度桶、沿向重叠、间距近似相等的线归成一组，记下
    "间距/墙厚"的无量纲签名。是不是窗、是不是柱，这一步**不判**。
    """
    min_lines = int(BUDGET["bundle_min_lines"])
    overlap_min = float(BUDGET["bundle_overlap"])
    bucket = float(BUDGET["angle_bucket_deg"])
    rel_tol = float(BUDGET["spacing_rel_tol"])
    thick = max(1e-6, float(basis["thickness"]))

    groups: dict[tuple, list] = {}
    for line in lines:
        if line.get("pos") is None:
            continue
        ang = float(line.get("angle") or 0.0) % 180.0
        key = (line.get("layer"),
               round(ang / max(1e-6, bucket)) if bucket else 0)
        groups.setdefault(key, []).append(line)

    bundles: list[dict] = []
    for (_layer, _bucket), members in groups.items():
        if len(members) < min_lines:
            continue
        members.sort(key=lambda l: float(l["pos"]))
        for i in range(len(members) - min_lines + 1):
            run = [members[i]]
            for nxt in members[i + 1:]:
                prev = run[-1]
                overlap = (min(float(prev["hi"]), float(nxt["hi"]))
                           - max(float(prev["lo"]), float(nxt["lo"])))
                short = max(1e-6, min(float(prev["hi"]) - float(prev["lo"]),
                                      float(nxt["hi"]) - float(nxt["lo"])))
                if overlap / short < overlap_min:
                    break
                run.append(nxt)
                if len(run) >= int(BUDGET["case_entities"]):
                    break
            if len(run) < min_lines:
                continue
            spacings = [abs(float(b["pos"]) - float(a["pos"]))
                        for a, b in zip(run, run[1:])]
            if not spacings:
                continue
            med = _median(spacings)
            if med <= 0:
                continue
            if any(abs(s - med) > rel_tol * med for s in spacings):
                continue
            handles = [str(l.get("handle")) for l in run if l.get("handle")]
            if not handles:
                continue
            bundles.append({
                "handles": sorted(set(handles)),
                "layers": sorted({str(l.get("layer")) for l in run}),
                "angle_deg": _r(float(run[0].get("angle") or 0.0) % 180.0, 1),
                "lines": len(run),
                "spacing": _r(med, 1),
                "bbox": _union_bbox([_bbox_of(l) for l in run]),
                "residual": sum(1 for h in handles if h in residual),
            })

    # 同签名（间距/墙厚 + 条数 + 角度）的在图上出现多次 = 重复结构，同族一次推理
    for b in bundles:
        b["signature"] = "bundle|%d|%.2f|%.0f" % (b["lines"],
                                                  b["spacing"] / thick,
                                                  b["angle_deg"])
    return bundles


# --------------------------------------------------------------------- 重复块族
def _repeated_blocks(snapshot, *, residual: set, explained: set) -> list[dict]:
    """同族块多次出现，且至少有一个实例没被任何识别器解释。"""
    try:
        fams = BLOCKFAM.families(snapshot)
    except Exception:
        return []
    out: list[dict] = []
    for name, fam in fams.items():
        insts = [e for e in (fam.get("instances") or ()) if e.get("handle")]
        if len(insts) < int(BUDGET["family_min_instances"]):
            continue
        handles = sorted({str(e.get("handle")) for e in insts})
        if not handles:
            continue
        if all(h in explained for h in handles):
            continue
        loose = [h for h in handles if h in residual or h not in explained]
        if not loose:
            continue
        try:
            fp = BLOCKFAM.fingerprint(fam)
        except Exception:
            fp = ""
        sizes = fam.get("sizes") or ()
        out.append({
            "family": str(name), "fingerprint": fp,
            "handles": handles, "unexplained": sorted(loose),
            "layers": list(fam.get("layers") or ()),
            "instances": len(insts),
            "size": list(sizes[len(sizes) // 2]) if sizes else None,
            "bbox": _union_bbox([_bbox_of(e) for e in insts]),
            "signature": "block|" + (fp or "unknown"),
        })
    return out


# --------------------------------------------------------------------- 残余聚类
def _residual_clusters(pool, *, residual: set, explained: set, basis) -> list[dict]:
    """空间邻近的残余图元聚成一块（连通域），贴墙的额外加分。"""
    items = [p for p in pool if p["handle"] in residual and p["handle"] not in explained
             and p["bbox"]]
    if len(items) < int(BUDGET["min_cluster"]):
        return []
    radius = float(BUDGET["neighbor_radius"]) * float(basis["thickness"])
    seen: set = set()
    clusters: list[dict] = []
    for i, item in enumerate(items):
        if i in seen:
            continue
        group = [i]
        seen.add(i)
        stack = [i]
        while stack:
            cur = stack.pop()
            for j, other in enumerate(items):
                if j in seen:
                    continue
                if _dist(items[cur]["bbox"], other["bbox"]) <= radius:
                    seen.add(j)
                    group.append(j)
                    stack.append(j)
        if len(group) < int(BUDGET["min_cluster"]):
            continue
        members = [items[k] for k in group][:int(BUDGET["case_entities"])]
        bbox = _union_bbox([m["bbox"] for m in members])
        widths = [m["bbox"][2] - m["bbox"][0] for m in members]
        heights = [m["bbox"][3] - m["bbox"][1] for m in members]
        clusters.append({
            "handles": sorted({m["handle"] for m in members}),
            "layers": sorted({str(m["layer"]) for m in members}),
            "types": sorted({str(m["type"]) for m in members}),
            "bbox": bbox, "size": len(members),
            "median_size": [_r(_median(widths)), _r(_median(heights))],
            "signature": "cluster|%s|%d" % ("+".join(sorted({str(m['type'])
                                                             for m in members})),
                                            len(members)),
        })
    return clusters


# --------------------------------------------------------------------- 开放轮廓
def _open_contours(ledger, *, explained: set) -> list[dict]:
    """账本里记着"轮廓不闭合/缺边"的图元：柱、墩这类实心形体最常这样。"""
    out: list[dict] = []
    for handle, rec in (ledger or {}).items():
        if not isinstance(rec, dict) or str(handle) in explained:
            continue
        reasons = " ".join(str(r) for r in (rec.get("reasons") or ()))
        if "OPEN_CONTOUR" not in reasons.upper():
            continue
        out.append({"handles": [str(handle)], "layers": [str(rec.get("layer") or "")],
                    "bbox": rec.get("bbox"), "types": [str(rec.get("type") or "")],
                    "reason": "OPEN_CONTOUR", "signature": "contour|open"})
    return out


# --------------------------------------------------------------------- 特征与案例
def _text_hints(pool, region, limit: int = 6) -> list[dict]:
    """ROI 里的文字（**不可信数据**：只作为线索，指令性质的内容不会被采信）。"""
    out: list[dict] = []
    for rec in pool:
        if str(rec.get("type")) not in ("TEXT", "MTEXT"):
            continue
        if region and not _inside(rec.get("bbox"), region):
            continue
        text = (rec.get("text") or "").strip()
        if not text:
            continue
        out.append({"handle": rec["handle"], "text": text[:60]})
        if len(out) >= limit:
            break
    return out


def _near_walls(region, walls, pad: float) -> list[str]:
    out: list[str] = []
    for w in walls or ():
        if not isinstance(w, dict):
            continue
        box = _bbox_of({"start": w.get("start"), "end": w.get("end"),
                        "thickness": w.get("thickness")})
        if not box or not region:
            continue
        if _inside(box, region, pad):
            wid = str(w.get("id") or w.get("handle") or "")
            if wid:
                out.append(wid)
    return out[:24]


def _wall_proximity(bbox, walls, *, basis) -> float:
    """目标几何离最近的已确认墙有多近（0~1，越近越大）。只作特征，不作判据。"""
    if not bbox:
        return 0.0
    near = min((_dist(bbox, _bbox_of({"start": w.get("start"), "end": w.get("end"),
                                      "thickness": w.get("thickness")}))
                for w in (walls or ()) if isinstance(w, dict) and w.get("start")),
               default=None)
    if near is None:
        return 0.0
    scale = max(1e-6, float(basis["thickness"]) * 4.0)
    return _r(max(0.0, 1.0 - near / scale), 3)


def _priority(*, repetition: int, residual_ratio: float, wall_prox: float,
              coverage: float, risk: float) -> float:
    """队列排序分（0~1）。**不是**识别可信度：只决定"先看哪个"。

    四项收益（重复度 / 残余量 / 拓扑影响 / 可执行算子覆盖）减去图例污染风险，
    权重是首版统一配置，不按图纸调。
    """
    rep = min(1.0, math.log1p(max(0, repetition)) / math.log(8.0))
    score = (0.35 * rep + 0.25 * max(0.0, min(1.0, residual_ratio))
             + 0.25 * max(0.0, min(1.0, wall_prox))
             + 0.15 * max(0.0, min(1.0, coverage))
             - 0.45 * max(0.0, min(1.0, risk)))
    return _r(max(float(BUDGET["priority_floor"]), min(1.0, score)), 3)


def _baseline(candidate_ids, status: str, reasons) -> dict:
    return {"candidate_ids": list(candidate_ids or ()),
            "status": status, "reasons": [str(r) for r in (reasons or ())][:8]}


def _case(*, kind: str, cluster: dict, region, features: dict, baseline: dict,
          priority: float, context: dict, instances=None, limits: dict) -> dict:
    handles = list(cluster.get("handles") or ())
    digest = _sha("|".join([kind, cluster.get("signature") or "",
                            ",".join(handles)]))[:12]
    return {
        "schema": SCHEMA,
        "id": f"pattern:{digest}",
        "kind": kind,
        "region_id": "",
        "bbox": cluster.get("bbox"),
        "region": region,
        "source_handles": handles[:int(limits["case_entities"])],
        "target_handles": handles[:int(limits["case_entities"])],
        "context_handles": (context.get("handles") or [])[:int(limits["case_entities"])],
        "wall_handles": context.get("walls") or [],
        "features": features,
        "baseline": baseline,
        "instances": list(instances or ()),
        "limitations": list(limits.get("notes") or ()),
        "image_ref": None,
        "image_bbox": cluster.get("image_bbox"),
        "priority": priority,
        "source_digest": "sha256:" + _sha(handles and ",".join(handles) or kind)[:32],
        "trace": {"layers": cluster.get("layers") or [],
                  "why": [kind, cluster.get("signature") or ""]},
    }


def discover(*, snapshot: dict, ledger=None, walls_seg=(), columns=(), doors=(),
             windows=(), spaces=None, prims=None, scale=None, plan=None,
             config=None, render=None, limit: int | None = None) -> dict:
    """主入口：返回 ``{"cases": [...], "stats": {...}, "budget": {...}}``。

    ``render`` 是可选回调 ``fn(case) -> {"path": str, "b64": str}``：**由调用方**
    决定要不要落盘/怎么画（这层保持纯函数，PURE 节点不允许写文件）。
    """
    walls = list(walls_seg or ())
    if prims is None:
        try:
            from . import primitives as PRIM
            prims = PRIM.extract(snapshot)
        except Exception:
            prims = {}
    basis = _basis(scale)
    limits = dict(BUDGET)
    if config is not None and hasattr(config, "get"):
        try:
            if config.has("line_bundle_min_count"):
                limits["bundle_min_lines"] = int(config.take("line_bundle_min_count"))
        except Exception:
            pass
    max_cases = int(limits["cases"] if limit is None else limit)

    pool = _pool(snapshot)
    explained = _explained_handles(columns, doors, windows, walls)
    # 残余池统一从证据账本里取（:func:`raw_evidence.residual`），避免两处口径分叉；
    # 没有账本时（单测/局部调用）保守地认为"除了已解释的，其余都还没解释"。
    if ledger:
        from . import raw_evidence as RAW
        split = RAW.residual(ledger, explain_handles=explained)
        residual = set(split["unused"]) | set(split["weak"]) | set(split["rejected"])
        residual &= {p["handle"] for p in pool}
    else:
        residual = {p["handle"] for p in pool if p["handle"] not in explained}
    lines = [l for l in ((prims or {}).get("lines") or ()) if l.get("pos") is not None]

    candidates: list[dict] = []
    suppressed = 0          # 因"图层语义是家具/图例/不打印"而主动压掉的案例数

    # 1) 平行线束（按签名合并同族实例）
    bundles = _line_bundles(lines, basis=basis, residual=residual)
    by_sig: dict[str, list] = {}
    for b in bundles:
        by_sig.setdefault(b["signature"], []).append(b)
    for sig, group in by_sig.items():
        group.sort(key=lambda b: (-b["residual"], -b["lines"]))
        head = group[0]
        handles = sorted({h for g in group for h in g["handles"]})
        loose = sum(g["residual"] for g in group)
        features = {
            "parallel_line_count": head["lines"],
            "dominant_angles_deg": [head["angle_deg"]],
            "spacing_ratios_to_wall_thickness": [
                round(head["spacing"] / max(1e-6, basis["thickness"]), 3)],
            "block_family_hash": None,
            "repetition_count": len(group),
            "wall_proximity_ratio": _wall_proximity(head["bbox"], walls, basis=basis),
            "text_hints": [h["text"] for h in _text_hints(pool, head["bbox"])],
        }
        if _non_structural(head["layers"]):
            suppressed += 1
            continue
        risk = _annotation_risk(head["layers"])
        prio = _priority(repetition=len(group),
                         residual_ratio=loose / max(1, len(handles)),
                         wall_prox=features["wall_proximity_ratio"],
                         coverage=min(1.0, head["lines"] / 6.0), risk=risk)
        region = _expand(head["bbox"], float(BUDGET["halo_thickness"])
                         * float(basis["thickness"])) if head["bbox"] else None
        candidates.append(_case(
            kind="line_bundle",
            cluster={**head, "handles": handles},
            region=region, features=features,
            baseline=_baseline([], "unrecognized",
                               ["NO_MATCHING_OPENING_PATTERN"] if loose else
                               ["WEAK_LINE_BUNDLE"]),
            priority=prio,
            context={"handles": _near_walls(region, walls,
                                            float(basis["thickness"]) * 2.0),
                     "walls": _near_walls(region, walls, 0.0)},
            instances=[{"handles": g["handles"], "bbox": g["bbox"],
                        "spacing": g["spacing"]} for g in group[:int(
                            BUDGET["case_hypotheses"])]],
            limits=limits))

    # 2) 重复块族
    for fam in _repeated_blocks(snapshot, residual=residual, explained=explained):
        if _non_structural(fam["layers"]):
            # 家具/洁具/不打印这一族不是方案要的 11 类构件：留在账本里，不花模型调用
            suppressed += 1
            continue
        features = {
            "parallel_line_count": None,
            "dominant_angles_deg": [],
            "spacing_ratios_to_wall_thickness": [],
            "block_family_hash": fam["fingerprint"] or None,
            "repetition_count": fam["instances"],
            "wall_proximity_ratio": _wall_proximity(fam["bbox"], walls, basis=basis),
            "text_hints": [h["text"] for h in _text_hints(pool, fam["bbox"])],
        }
        risk = _annotation_risk(fam["layers"])
        prio = _priority(repetition=fam["instances"],
                         residual_ratio=len(fam["unexplained"]) / max(1, len(fam["handles"])),
                         wall_prox=features["wall_proximity_ratio"],
                         coverage=0.5, risk=risk)
        region = _expand(fam["bbox"], float(BUDGET["halo_thickness"])
                         * float(basis["thickness"])) if fam["bbox"] else None
        # 截图只画一个**代表实例**：同族 17 个实例铺满整栋楼时，整图截图既看不清
        # 也白花 token；实例清单仍在 features/instances 里，验收按实例逐个过
        fam["image_bbox"] = (_representative(
            [e for e in ((BLOCKFAM.families(snapshot).get(fam["family"]) or {})
                         .get("instances") or ())], basis=basis) or fam["bbox"])
        candidates.append(_case(
            kind="repeated_block", cluster=fam, region=region, features=features,
            baseline=_baseline([], "unrecognized", ["UNMATCHED_BLOCK_FAMILY"]),
            priority=prio,
            context={"handles": _near_walls(region, walls,
                                            float(basis["thickness"]) * 2.0),
                     "walls": _near_walls(region, walls, 0.0)},
            instances=[{"handles": [h]} for h in fam["handles"][:int(
                BUDGET["case_hypotheses"])]],
            limits=limits))

    # 3) 残余聚类
    for cluster in _residual_clusters(pool, residual=residual, explained=explained,
                                      basis=basis):
        if _non_structural(cluster["layers"]):
            suppressed += 1
            continue
        features = {
            "parallel_line_count": None,
            "dominant_angles_deg": [],
            "spacing_ratios_to_wall_thickness": [],
            "block_family_hash": None,
            "repetition_count": 1,
            "wall_proximity_ratio": _wall_proximity(cluster["bbox"], walls, basis=basis),
            "text_hints": [h["text"] for h in _text_hints(pool, cluster["bbox"])],
            "types": cluster["types"],
            "median_size": cluster["median_size"],
        }
        prio = _priority(repetition=1,
                         residual_ratio=1.0,
                         wall_prox=features["wall_proximity_ratio"],
                         coverage=0.35,
                         risk=_annotation_risk(cluster["layers"]))
        region = _expand(cluster["bbox"], float(BUDGET["halo_thickness"])
                         * float(basis["thickness"])) if cluster["bbox"] else None
        candidates.append(_case(
            kind="residual_cluster", cluster=cluster, region=region,
            features=features,
            baseline=_baseline([], "unrecognized", ["UNEXPLAINED_GEOMETRY"]),
            priority=prio,
            context={"handles": _near_walls(region, walls,
                                            float(basis["thickness"]) * 2.0),
                     "walls": _near_walls(region, walls, 0.0)},
            instances=[],
            limits=limits))

    # 4) 开放轮廓（缺边柱/墩）
    for contour in _open_contours(ledger, explained=explained):
        features = {
            "parallel_line_count": None, "dominant_angles_deg": [],
            "spacing_ratios_to_wall_thickness": [], "block_family_hash": None,
            "repetition_count": 1,
            "wall_proximity_ratio": _wall_proximity(contour["bbox"], walls, basis=basis),
            "text_hints": [h["text"] for h in _text_hints(pool, contour["bbox"])],
        }
        region = _expand(contour["bbox"], float(BUDGET["halo_thickness"])
                         * float(basis["thickness"])) if contour["bbox"] else None
        candidates.append(_case(
            kind="open_contour", cluster=contour, region=region, features=features,
            baseline=_baseline([], "unrecognized", ["OPEN_CONTOUR"]),
            priority=_priority(repetition=1, residual_ratio=1.0,
                               wall_prox=features["wall_proximity_ratio"],
                               coverage=0.4,
                               risk=_annotation_risk(contour["layers"])),
            context={"handles": [], "walls": _near_walls(region, walls, 0.0)},
            instances=[], limits=limits))

    # ---- 排队、编号、可选渲染
    candidates.sort(key=lambda c: (-c["priority"], c["id"]))
    kept = candidates[:max_cases]
    for i, case in enumerate(kept):
        case["region_id"] = f"region:{i:03d}"
        if render is not None:
            try:
                shot = render(case) or {}
            except Exception:
                shot = {}
            if shot.get("path"):
                case["image_ref"] = f"local://{shot['path']}"
            if isinstance(shot.get("b64"), str):
                case["image_b64"] = shot["b64"]
                case["image_stats"] = shot.get("stats") or {}
    dropped = max(0, len(candidates) - len(kept))
    return {
        "schema": "pattern-cases/1.0",
        "cases": kept,
        "stats": {"candidates": len(candidates), "kept": len(kept),
                  "dropped_by_budget": dropped,
                  "residual_handles": len(residual),
                  "explained_handles": len(explained),
                  "kinds": {k: sum(1 for c in kept if c["kind"] == k)
                            for k in CASE_KINDS},
                  "bundle_signatures": len(by_sig),
                  "suppressed_non_structural": suppressed},
        "budget": dict(limits, cases=max_cases),
        "basis": basis,
    }


def families(cases) -> dict:
    """按"形态族"重新分组：同族多实例只推理一次（100 个同族柱 ≠ 100 次调用）。"""
    out: dict[str, list] = {}
    for case in cases or ():
        if not isinstance(case, dict):
            continue
        key = (case.get("features") or {}).get("block_family_hash") or \
            str((case.get("trace") or {}).get("why") or [""])[1] or case.get("kind")
        out.setdefault(str(key), []).append(case)
    return out


def summary(result) -> dict:
    """进 ``meta.ai_recognition`` 的摘要（不含提示词、不含坐标列表）。"""
    result = result or {}
    cases = result.get("cases") or ()
    return {"schema": result.get("schema") or "pattern-cases/1.0",
            "cases": len(cases),
            "kinds": {k: sum(1 for c in cases if c.get("kind") == k)
                      for k in CASE_KINDS},
            "stats": result.get("stats") or {},
            "top": [{"id": c.get("id"), "kind": c.get("kind"),
                     "priority": c.get("priority"),
                     "handles": len(c.get("source_handles") or ()),
                     "reasons": (c.get("baseline") or {}).get("reasons")}
                    for c in cases[:6]]}


def case_facts(case: dict) -> dict:
    """喂给模型的结构事实（**只有特征，没有坐标阈值**）。"""
    feats = dict((case or {}).get("features") or {})
    return {"case_id": case.get("id"), "kind": case.get("kind"),
            "features": feats,
            "baseline": case.get("baseline") or {},
            "counts": {"source_handles": len(case.get("source_handles") or ()),
                       "context_handles": len(case.get("context_handles") or ()),
                       "instances": len(case.get("instances") or ())}}


def digest_of(case: dict) -> str:
    raw = json.dumps({"id": case.get("id"), "handles": case.get("source_handles"),
                      "features": case.get("features")},
                     sort_keys=True, ensure_ascii=False, default=str)
    return _sha(raw)[:16]
