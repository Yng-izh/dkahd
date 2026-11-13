"""识别流程的节点函数与 DAG 注册表。

节点只消费 ``load`` 产出的 ``DrawingSnapshot``（纯 Python / JSON-native）或上游
节点的纯数据输出；只有 ``load`` / ``assemble`` / ``export`` 是 MAIN 节点，需要
live ezdxf 文档，固定由编排线程执行。

节点与依赖与 ``work/packaging/requirements.md`` §4.1/§4.2 一致：

    load -> texts, walls_seg, columns, partitions, grid, doors, windows, frame,
            north, stairs
    texts -> doors, windows, frame, north, stairs, room_labels
    walls_seg -> wall_model
    walls_seg + columns + partitions + load -> spaces_seed
    wall_model + spaces_seed + doors + windows -> openings_select
    walls_seg + wall_model + doors + windows + openings_select -> walls_merge
    walls_merge -> envelope, steps, entrance_steps, apron
    envelope + texts -> room_labels
    walls_merge + columns + doors + windows + room_labels + partitions + envelope
        + spaces_seed -> rooms
    {...} + spaces_seed + openings_select -> assemble -> export

拓扑反馈闭环：``spaces_seed``（墙网围出的初步空面）先于洞口判定产出，
``openings_select`` 用它判断"洞口两侧是不是两个空间"，``walls_merge`` 再用
采纳的洞口做跨洞口连续；房间识别最后复用同一批面域，因此墙网、洞口、房间
三者的空间口径一致。
"""
from __future__ import annotations

import collections
import os

from . import elements as R
from . import rooms as RM
from . import walls as wallrec
from .base import _clean_mtext, bbox_of_points, merge_spaced_words, merge_text_fragments
from . import base as _BASE
from .dag import DAG, Node
from . import geometry as GEO
from . import openings as OPN
from . import blockmodel as BLK
from . import llm_verify as LLMVERIFY
from . import llm_review as LLMREVIEW
from . import evidence as EVID
from . import executors as EXEC
from . import defects as DEF
from . import labels as LAB
from . import llm_label_filter as LLMLABEL
from . import openzone as OZ
from . import room_merge as RMERGE
from . import llm_space_judge as SPACEJUDGE
from . import adaptive_controller as ADAPT
from . import experiment_runner as ADAPT_RUNNER
from . import llm_interpret as LINT
from . import opening_cases as OPCASES
from . import layer_names as LNAMES
from . import layer_roles_infer as LRI
from . import auto_profile as AUTO
from . import plan_compiler as COMPILER
from . import repair_planner as PLANNER
from . import finalstate as FS
from . import raw_evidence as RAV
from . import repair as REP
from . import layer_roles as LROLES
from . import local_solver as SOLVER
from . import llm_reason as REASON
from . import operator_specs as OPSPEC
from . import proposal_validator as VALID
from . import quality_gate as GATE
from . import recompute as RC
from . import operators as OPS
from . import primitives as PRIM
from . import regions as REGIONS
from . import scale as SCALE
from . import spaces as SPACES
from . import unified as UNI
from . import wallmodel as WM
from .config import CONFIG
from .layout import crop_snapshot
from .plan import has_symbol, load_plan, resolve_layers
from .snapshot import build_snapshot, entity_types
from . import blockfamily as BLOCKF
from . import priority as PRIO


def _round(x, n=1):
    return round(float(x), n)


#: 柱画法中文名（报告/证据说明用）
_COL_METHOD_CN = {"block": "柱块参照", "contour": "闭合轮廓", "hatch": "填充边界",
                  "polyline": "闭合多段线", "lines": "线段组合"}


def _pts(ps, n=1):
    return [[_round(p[0], n), _round(p[1], n)] for p in ps]


def nearest_wall(walls, seg, maxd=400.0, slack=1200.0):
    """洞口/窗所在墙：取垂直于洞口段、且洞口落在该墙跨度内的最近墙中心线。

    洞口两侧往往是墙段之间的缺口，按"到线段距离"会全部落空，因此用墙中心线
    的法向距离 + 跨度包含判断。法向距离用点积算，斜墙同样适用。
    """
    (x1, y1), (x2, y2) = seg
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    best, bd = None, maxd
    for w in walls:
        # 弧墙的"center/pos"是中线半径，不是法向偏移量：按直线基座投影会得到
        # 毫无意义的距离。弧形墙上的洞口暂不建立 wall_id 归属（宁缺不错）。
        if w.get("arc"):
            continue
        line = {"orient": w["orient"], "pos": w["center"],
                "dir": w.get("dir"), "nrm": w.get("nrm")}
        u, d = WM.local(line, cx, cy)
        d = abs(d)
        lo = min(WM.local(line, w["start"][0], w["start"][1])[0],
                 WM.local(line, w["end"][0], w["end"][1])[0])
        hi = max(WM.local(line, w["start"][0], w["start"][1])[0],
                 WM.local(line, w["end"][0], w["end"][1])[0])
        ok = lo - slack <= u <= hi + slack
        if ok and d < bd:
            bd, best = d, w
    return best, bd


# --------------------------------------------------------------------------- 门窗编号
def _door_entries(doors):
    """按 assemble 的固定排序返回 (component_id, door)，保证 wall_runs 与组件 id 一致。"""
    return [(f"DOOR-{k:02d}", d) for k, d in enumerate(
        sorted(doors, key=lambda d: (d["layer"], -d["insert"][1])), 1)]


def _window_entries(windows):
    """按 assemble 的固定排序返回 (component_id, window)。"""
    return [(f"WIN-{k:02d}", w) for k, w in enumerate(
        sorted(windows, key=lambda w: (-w["center"][1], w["center"][0])), 1)]


def _opening_source_entities(candidate, role):
    """Keep provenance types from unified candidates; never invent INSERTs.

    Some symbol-like openings originate from ARC/LINE geometry, not blocks.
    """
    srcs = [dict(src) for src in (candidate.get("srcs") or ())
            if src.get("handle")]
    handle = candidate.get("handle")
    if handle and not any(src.get("handle") == handle for src in srcs):
        kind = ("INSERT" if candidate.get("block") else
                "ARC" if candidate.get("free_end") else "LINE")
        srcs.insert(0, {"handle": handle, "type": kind,
                        "layer": candidate.get("layer"), "role": role})
    return srcs


def _axis_of(item):
    g = item.get("geometry") or {}
    return g.get("axis") or item.get("axis")


def _axis_center(axis):
    (x1, y1), (x2, y2) = axis
    return (x1 + x2) / 2.0, (y1 + y2) / 2.0


def _same_axis(a, b, tol):
    """平行、法向偏移不超过 tol、沿墙区间有重叠 → 视为同一构件（方向无关）。"""
    if not a or not b:
        return False
    sa = GEO.as_support(a[0], a[1])
    sb = GEO.as_support(b[0], b[1])
    if sa is None or sb is None:
        return False
    if not GEO.is_same_line(sa, sb, angle_tol=2.0, pos_tol=tol):
        return False
    return GEO.overlap_1d(sa["lo"], sa["hi"], sb["lo"], sb["hi"]) > 0


def _bbox_center(b):
    return (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0


def _same_box(a, b, tol):
    if not a or not b:
        return False
    ca, cb = _bbox_center(a), _bbox_center(b)
    return abs(ca[0] - cb[0]) <= tol and abs(ca[1] - cb[1]) <= tol


def _col_bbox(c):
    g = c.get("geometry") or {}
    return c.get("bbox") or g.get("bbox")


def _opening_span(item, category):
    """门/窗 -> (orient, pos, lo, hi)；与 walls.openings_from 同口径。

    没有 opening 几何（或零宽）的门不进入跨洞口合并集合：零宽 span 在 slack
    容差下可能覆盖真实缺口。它仍会通过 nearest_wall 参与 wall_id 查询。
    """
    seg = item.get("opening") if category == "door" else item.get("axis")
    if not seg:
        return None
    (x1, y1), (x2, y2) = seg
    if abs(x2 - x1) < 1e-9 and abs(y2 - y1) < 1e-9:
        return None
    sup = GEO.as_support((x1, y1), (x2, y2))
    if sup is None:
        return None
    orient = sup["orient"] if sup["orient"] in ("H", "V") else f"O{sup['angle']:.0f}"
    return orient, sup["pos"], sup["lo"], sup["hi"]


def _rich_openings(doors, windows, walls=(), thick=200.0):
    """Project openings onto the actual host wall's local line coordinates.

    Geometry that is slightly rotated relative to the wall no longer creates
    a different support key.  The original item remains unchanged; no host
    association means no fabricated wall modification.
    """
    out = []
    for category, records in (("door", _door_entries(doors)),
                              ("window", _window_entries(windows))):
        for cid, item in records:
            seg = item.get('opening') if category == 'door' else item.get('axis')
            if not seg or len(seg) != 2:
                continue
            host, distance = nearest_wall(walls, seg,
                                          maxd=max(400.0, 2.5*thick),
                                          slack=max(1200.0, float(item.get('width') or 0.0)))
            if host is not None:
                base = {'orient':host['orient'], 'pos':host['center'],
                        'dir':host.get('dir'), 'nrm':host.get('nrm')}
                u1 = WM.local(base, *seg[0])[0]
                u2 = WM.local(base, *seg[1])[0]
                orient, pos, lo, hi = host['orient'], host['center'],min(u1,u2),max(u1,u2)
            else:
                span = _opening_span(item, category)
                if span is None:
                    continue
                orient, pos, lo, hi = span
            if hi-lo < 1.0:
                continue
            out.append({'category':category, 'id':cid, 'handle':item.get('handle'),
                        'layer':item.get('layer'), 'block':item.get('block'),
                        'width':float(item.get('width') or hi-lo),
                        'type_code':item.get('attr') if category=='door' else item.get('attr'),
                        'orient':orient, 'pos':pos, 'lo':lo, 'hi':hi,
                        'host_found':host is not None})
    return out


# --------------------------------------------------------------------------- 摘要
def summarize_snapshot(snapshot: dict) -> dict:
    return {
        "type": "snapshot",
        "entities": len(snapshot.get("entities", ())),
        "by_type": entity_types(snapshot),
        "blocks": len(snapshot.get("blocks", {})),
        "partitions": len(snapshot.get("partitions", ())),
    }


def summarize_load(value: dict) -> dict:
    plan = value.get("plan") or {}
    return {
        "type": "load",
        "entities": len(value.get("snapshot", {}).get("entities", ())),
        "plan_source": plan.get("source"),
        "plan_layers": {k: len(v) for k, v in (plan.get("layers") or {}).items() if v},
    }


def summarize_walls_merge(value: dict) -> dict:
    return {"type": "dict", "len": len(value), "keys": list(value.keys()),
            "walls_all": len(value.get("walls_all", ())),
            "walls": len(value.get("walls", ())),
            "wall_runs": len(value.get("wall_runs", ()))}


# --------------------------------------------------------------------------- 节点
#: 结构性排除：块内几何的落脚层，按 CAD 规范永不赋语义角色
_NEVER_ROLE = {"0", "defpoints", "defpoints_", "0-", "0_"}
#: 图框层的结构判据：bbox 覆盖整图（>=88% 两个方向）且以文字为主
_FRAME_COVER = 0.88


def _structural(roles: dict, fps: dict) -> dict:
    """结构特征覆盖：图框层（包住整张图）与落脚层（``0``/``Defpoints``）。"""
    boxes = [f.get("bbox") for f in fps.values() if f.get("bbox")]
    if not boxes:
        return roles
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes)
    y1 = max(b[3] for b in boxes)
    w, h = max(1.0, x1 - x0), max(1.0, y1 - y0)
    for layer, rec in list(roles.items()):
        b = (fps.get(layer) or {}).get("bbox")
        if not b:
            continue
        cover = min((b[2] - b[0]) / w, (b[3] - b[1]) / h)
        if cover >= _FRAME_COVER and (fps.get(layer) or {}).get("text_ratio", 0) > 0:
            rec["roles"] = ["frame"]
            rec["confidence"] = round(max(rec.get("confidence") or 0.0, 0.8), 3)
            rec["why"] = "structural:frame_bbox"
    return roles


def _infer_plan(doc, msp, *, digest: str = "", api_key: str | None = None,
                dxf_path: str | None = None, root=None):
    """图纸自己推导执行方案：**模型给方案，几何指纹做另一半证据**。

    模型产出完整 profile（层语义 + 画法 symbols + 算子提示 + 单位假设 + 图框层），
    程序只做闭集校验、缓存与融合：模型的层判断与几何指纹各占 0.5 参与决策，
    所以模型说错的层会被几何证据顶住，模型没说到的层由几何补上。

    返回 (完整 profile, 角色明细, 方案摘要)。
    """
    # 指纹只算一次：既给模型当判断依据，也参与 0.5/0.5 融合
    fps = LRI.fingerprints_from_msp(doc, msp)
    if not fps:
        return {}, {}, {"status": "skipped"}
    derived = AUTO.derive(msp, doc=doc, dxf_path=dxf_path, digest=digest,
                          api_key=api_key, root=root, fps=fps)
    # 结构性排除（先于一切打分）：0 / Defpoints 永不赋角色
    excluded = sorted(k for k in fps if k.strip().lower() in _NEVER_ROLE)
    fps = {k: v for k, v in fps.items() if k.strip().lower() not in _NEVER_ROLE}
    roles = LRI.infer_roles(fps, prior=derived.get("prior") or {})
    roles = _structural(roles, fps)
    profile = dict(derived.get("profile") or {})
    # Soft routing: ambiguous layers may enter a small number of additional
    # high-evidence recognizers. Never override an explicit user profile, and
    # never route furniture, annotation, title blocks or vetoed roles.
    soft_layers = {}
    for role in ("wall", "column", "door", "window"):
        choices = []
        for layer, rec in roles.items():
            if rec.get("roles") or role in (rec.get("vetoed") or ()):
                continue
            evidence = float((rec.get("role_scores") or {}).get(role, 0))
            geom = float((rec.get("geom") or {}).get(role, 0))
            geometry_ok = geom >= (0.25 if role == "wall" else 0.30)
            # Unknown wall layers need either independent strong geometry or
            # an agreeing naming prior; otherwise e.g. electrical/furniture
            # layers get routed as walls and corrupt topology downstream.
            if role == "wall":
                geometry_ok = geometry_ok and (
                    geom >= 0.45 or float((rec.get("prior") or {}).get(role, 0)) >= 0.45)
            if evidence >= (0.35 if role == "wall" else 0.42) and geometry_ok:
                choices.append((evidence, geom, layer))
        # Cap speculative candidates per role to avoid explosions on large CADs.
        soft_layers[role] = [layer for _, _, layer in sorted(choices, reverse=True)[:4]]
    profile["soft_layers"] = soft_layers
    profile["role_hypotheses"] = {k: {"scores": v.get("role_scores") or {},
                                                "candidates": v.get("candidates") or []}
                                   for k, v in roles.items()}
    # 融合后的层语义才是执行依据；模型给的 symbols/units/scope 原样保留
    profile["layers"] = LRI.to_profile_layers(roles)
    info = dict(derived.get("summary") or {})
    info["excluded_layers"] = excluded
    info["fused"] = LRI.summary(roles, {"status": derived.get("source"),
                                        "priors": derived.get("prior") or {}})
    return profile, roles, info


def node_load(ctx):
    """MAIN：解析 DXF、构建快照，并挂载执行方案（profile 或**自推导**）。

    顺序：profile -> （缺省时自推导图层方案）-> 快照（含块展开）-> 单位判定与
    换算 -> 区域分割 -> 裁剪。裁剪只在"确认是单一平面"时进行；``multi_plan``
    时不裁剪，避免静默丢掉另一个平面。

    自推导的意义：``recognize(path)`` 对任何一张新图都能直接跑通，不需要先有人
    给它写一份 profile。方案来源记在输出里（``plan_source`` / ``auto_layers``），
    仍然可以被显式传入的 profile 覆盖（用户/离线适配优先）。
    """
    profile = ctx.params.get("profile")
    plan = load_plan(profile)
    auto, roles, prior_info = {}, {}, {}
    # A drawing with nearly all geometry on one default layer cannot use the
    # normal semantic-layer veto. Preserve all geometry and route it through
    # evidence-backed recognizers, without changing explicitly supplied plans.
    from . import unlayered as UNLAYERED
    single_layer = UNLAYERED.detect(ctx.msp) if not profile else None
    if single_layer:
        inferred = UNLAYERED.fallback_profile(single_layer)
        plan = load_plan(inferred)
        plan["unlayered"] = single_layer
        plan["plan_source"] = "unlayered-geometry"
        plan["scope"]["crop"] = False
        prior_info = {"status": "unlayered", "layer": single_layer}
    elif not profile:
        derived, roles, prior_info = _infer_plan(
            ctx.doc, ctx.msp, digest=ctx.file_digest,
            api_key=(ctx.params or {}).get("llm_api_key"),
            dxf_path=ctx.dxf_path)
        auto = (derived or {}).get("layers") or {}
        if auto:
            plan = load_plan(derived)
            plan["auto_layers"] = auto
            plan["layer_roles"] = roles
            plan["soft_layers"] = derived.get("soft_layers") or {}
            plan["role_hypotheses"] = derived.get("role_hypotheses") or {}
            plan["plan_source"] = (derived.get("source")
                                   or "auto-fingerprint+model")
    elif plan.get("source") is None:
        plan["plan_source"] = "profile"
    skip_layers = set(plan["layers"].get("door") or ()) | set(plan["layers"].get("window") or ())
    snapshot = build_snapshot(ctx.doc, ctx.msp, ctx.dxf_path,
                              block_names=tuple(R.DOOR_BLOCKS),
                              expand=not plan["scope"].get("no_expand", False),
                              expand_skip_layers=skip_layers,
                              expand_skip_blocks=plan["scope"].get("no_expand_blocks") or ())

    units = GEO.detect_units(snapshot, hypothesis=plan.get("unit_hypothesis"))
    if units["resolved"]:
        snapshot = GEO.apply_units(snapshot, units)

    frame_layer = plan["scope"].get("frame_layer")
    struct_layers = list(plan["layers"]["wall"]) + list(plan["layers"]["column"])
    # 区域上下文：给每条记录打 region 标记（下游按建筑平面独立推理），
    # 并对"跨度明显不可能是 mm 平面"的区域做局部单位换算。
    part = REGIONS.partition(snapshot, [frame_layer] if frame_layer else [],
                             struct_layers=struct_layers, tag=True,
                             resolve_units=True)
    cropped = bool(part["plan"]) and plan["scope"].get("crop", True) and not part["multi_plan"]
    if cropped:
        # 自证条款：裁剪框自己的面积必须占内容包围盒 ≥5%，否则不裁。
        # 单图层图实测过最坏情况：415×427 的小符号被当成图框，5444 个图元
        # 只剩 262 个（4.8%），后面 46 个节点全在空集上跑。
        box_area = max(0.0, (part["plan"][2] - part["plan"][0])) * \
                   max(0.0, (part["plan"][3] - part["plan"][1]))
        regions = part.get("regions") or []
        base = regions[0]["bbox"] if regions else None
        content_area = (max(0.0, base[2] - base[0]) * max(0.0, base[3] - base[1])) if base else 0.0
        if content_area > 0 and box_area / content_area < 0.05:
            cropped = False
        else:
            snapshot = crop_snapshot(snapshot, part["plan"])
    # 图内自校准尺度：所有识别阈值都表达成它的倍数（见 recognition/scale.py）
    drawing_scale = SCALE.measure(snapshot, plan=plan)

    # Per-region context is diagnostic and can guide downstream candidate
    # budgets. No speculative local unit/scale is forced onto global geometry.
    # Only sample source entities, not large expanded block payloads.
    region_context = {}
    for r in part["regions"]:
        role_layers = {role: [it["layer"] for it in r.get("layers") or ()
                              if it.get("layer") in set(plan["layers"].get(role) or ())]
                       for role in ("wall", "column", "door", "window")}
        region_context[r["id"]] = {
            "kind": r.get("kind"), "unit": r.get("unit"),
            "layer_roles": role_layers,
            "struct": r.get("struct", 0), "entities": r.get("entities", 0),
            "scale_origin": "drawing" if r.get("kind") != "plan" else "region_unverified",
        }
    plan["region_context"] = region_context

    scope = {"method": part["method"], "regions": len(part["regions"]),
             "multi_plan": part["multi_plan"], "cropped": cropped,
             "crop": part["plan"] if cropped else None,
             "region_summary": REGIONS.summary(part),
             "region_context": region_context,
             "region_tagged": part.get("tagged", 0),
             "region_rescaled": part.get("rescaled", 0),
             "scale": SCALE.summary(drawing_scale),
             "derived": snapshot.get("derived_stats") or {}}
    return {"snapshot": snapshot, "file_digest": ctx.file_digest,
            "plan": plan, "units": units, "scope": scope,
            "scale": drawing_scale,
            # 块族表：一次定角色、全族实例继承（见 recognition/blockfamily.py）
            "block_families": BLOCKF.table(snapshot, scale=drawing_scale),
            "regions": part["regions"],
            "plan_source": plan.get("plan_source") or ("profile" if profile else "default"),
            "auto_layers": sorted(auto),
            "layer_profile": prior_info if roles else {},
            "name_prior": prior_info.get("fused") or prior_info,
            "layer_roles": {role: list(names) for role, names in
                            (plan.get("layers") or {}).items() if names}}


def node_texts(ctx):
    out = []
    for entity in ctx.inputs["load"]["snapshot"]["entities"]:
        kind = entity.get("type")
        if kind not in ("TEXT", "MTEXT"):
            continue
        raw = entity.get("text", "")
        text = raw if kind == "TEXT" else _clean_mtext(raw)
        out.append({
            "handle": entity["handle"], "type": kind, "layer": entity["layer"],
            "text": text, "raw": raw,
            "x": entity["insert"][0], "y": entity["insert"][1],
            "h": entity["height"], "rot": entity.get("rotation", 0.0) % 360.0,
        })
    # 两遍合并：正常字距的断句 -> 有意拉开字距的散字词组（"病 人 廊"）
    return merge_spaced_words(merge_text_fragments(out))


def node_walls_seg(ctx):
    """PURE：墙体两阶段识别（高召回候选 -> 几何⊗拓扑联合验证）。

    阶段 A 尽量不丢候选（双线配对 / 短碎线合并再配对 / 单线 / 同线短断口），
    阶段 B 才按"独立几何证据 + 端头与柱的拓扑支持"决定接受与否，并按
    ``region`` 独立配对——不同建筑平面的墙线不参与同一次配对。
    """
    plan = ctx.inputs["load"]["plan"]
    declared = list(plan["layers"]["wall"]) + list(plan.get("soft_layers", {}).get("wall") or ())
    if declared:
        wall_layers = list(dict.fromkeys(declared + ["WALL"]))
    else:
        # 没有任何墙层声明（图层乱/全在一层/新图库）：几何是主干，放开全图候选，
        # 由两阶段验证（几何⊗拓扑）决定接受与否——不再因为"没告诉我哪层是墙"
        # 就把整张图判成空。候选降级标注见 :func:`_mark_geometry_only`。
        # 但**纯标注/填充层一律不进**：那上面的线与墙线同形（尺寸线是平行双线、
        # 界线是短垂线、填充是等距平行线），放进来就是成批假墙假窗。
        wall_layers = sorted({str(e.get("layer")) for e in ctx.inputs["load"]["snapshot"].get("entities") or ()
                              if e.get("layer") and not _is_noise_layer(e.get("layer"))})
    columns = ctx.inputs.get("columns") or []
    walls_seg, candidates, wdiag = wallrec.extract_walls_ext(
        ctx.inputs["load"]["snapshot"], layers=wall_layers,
        allow_single_line=has_symbol(plan, "wall", "single_line"),
        columns=columns,
        # scale 进上下文：生成门（walls_net）要拿字高/门宽推出"多大才算一块空间"，
        # 不允许在这里写死毫米阈值。
        context={"layers": wall_layers, "scale": ctx.inputs["load"].get("scale")})
    return {"walls_seg": walls_seg, "unpaired": candidates,
            "wdropped": candidates, "wdiag": wdiag,
            "candidates": candidates,
            "validation": (wdiag or {}).get("stage_details") or {}}


def node_columns(ctx):
    plan = ctx.inputs["load"]["plan"]
    snap = ctx.inputs["load"]["snapshot"]
    col_layers = list(dict.fromkeys(list(plan["layers"]["column"]) +
                                      list(plan.get("soft_layers", {}).get("column") or [])))
    if not col_layers:
        # 没有柱层声明：几何主干，全图找闭合轮廓/线段组合（一律标 geometry-only）
        col_layers = sorted({str(e.get("layer")) for e in snap.get("entities") or ()
                             if e.get("layer")})
    scale = ctx.inputs["load"].get("scale")
    shape_cols = set(_shape_blocks(snap, plan, "column",
                                   scale=ctx.inputs["load"].get("scale"))) | {"_FZH"}
    out = R.recognize_columns(snap, layers=col_layers or None, scale=scale,
                              block_names=shape_cols)
    # 柱的证据来源统一：块语义 / 闭合轮廓 / 填充边界 / 线段组合，再按空间重合去重。
    # 仍然只扫 profile 指明的 column 图层（不恢复"扫全图矩形当柱"的过检策略）。
    cands = (OPS.run(["closed_contour", "hatch_region"], snap, col_layers, 200.0, CONFIG)
             if col_layers else [])
    for r in cands:
        b = r["bbox"]
        half = 0.5 * max(1.0, b[2] - b[0], b[3] - b[1])
        if any(_same_box(b, _col_bbox(c), half) for c in out):
            continue
        outline = [[float(p[0]), float(p[1])] for p in (r.get("outline") or [])]
        if len(outline) < 3:
            outline = [[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]]]
        handles = [h for h in (r.get("handles") or []) if h]
        out.append({"outline": outline, "bbox": b,
                    "w": b[2] - b[0], "h": b[3] - b[1],
                    "rotation": round(float(r.get("angle") or 0.0), 1),
                    "center": [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2],
                    "area": r.get("area"), "evidence": r.get("kind"),
                    "method": "hatch" if r.get("kind") == "hatch" else "contour",
                    "layer": r.get("layer"),
                    "handle": handles[0] if handles else None,
                    "srcs": [{"handle": h, "type": "CONTOUR", "layer": r.get("layer")}
                             for h in handles]})
    # 多画法融合：块 / 线段组合 / 闭合多段线 / 闭合轮廓 / 填充边界统一去重打分
    axis = R.axis_coords(snap, list(plan["layers"].get("axis") or ()))
    return R.fuse_columns(out, walls=(), axis=axis, scale=scale)["columns"]


def node_columns_verify(ctx):
    """PURE：柱候选的**第二阶段验证**——用墙网拓扑再判一次。

    柱候选在 ``columns`` 节点里就已经由形态判据筛过一遍，但那一阶段拿不到墙网
    （``walls_seg`` 依赖 ``columns``，反过来会成环）。缺边的矩形（只画了三条边）
    恰恰最需要"贴不贴墙/在不在轴网交点"这条证据：贴墙垛本来就靠墙界定，而孤零零
    的三条边矩形多半是墙面层或洞口框。

    所以这里按与墙体相同的两阶段思路补一次：有了墙网，缺边候选要么得到拓扑支持
    转正，要么降级到待核（不进构件、留痕可审）。
    """
    cols = list(ctx.inputs.get("columns") or ())
    walls = ctx.inputs["walls_seg"]["walls_seg"]
    axis = R.axis_coords(ctx.inputs["load"]["snapshot"],
                         list(ctx.inputs["load"]["plan"]["layers"].get("axis") or ()))
    fused = R.fuse_columns(cols, walls=walls, axis=axis,
                           scale=_scale_of(ctx))["columns"]
    # 归并后可能少于候选数（同位置不同画法合并），按 handle 回填原始层引用
    by_handle = {c.get("handle"): c for c in cols if c.get("handle")}
    for c in fused:
        src = by_handle.get(c.get("handle"))
        if src and src.get("edges"):
            c["edges"] = src["edges"]
    # ---- 模式 B（柱）：同上，模型只否决。待核（pending）不送模型、也不受影响。
    pending_cols = [c for c in fused if c.get("status") == "pending"]
    accepted = [c for c in fused if c.get("status") != "pending"]
    mode_b = {"status": "disabled"}
    if (ctx.params or {}).get("llm_review", True):
        accepted, mode_b = LLMREVIEW.reject_columns(
            accepted,
            api_key=(ctx.params or {}).get("llm_api_key"),
            cache_dir=ctx.params.get("cache_dir"),
            snapshot=ctx.inputs["load"]["snapshot"],
            vision=(ctx.params or {}).get("llm_review_vision", True))
        kept = {id(c) for c in accepted}
        fused = [c for c in fused
                 if c.get("status") == "pending" or id(c) in kept]
    return {"columns": fused,
            "stats": {"candidates": len(cols),
                      "emitted": len(accepted),
                      "pending": len(pending_cols),
                      "confirmed": sum(1 for c in accepted if c.get("status") == "confirmed"),
                      "mode_b": mode_b}}


def node_partitions(ctx):
    return ctx.inputs["load"]["snapshot"].get("partitions", [])


def node_grid(ctx):
    plan = ctx.inputs["load"]["plan"]
    return R.recognize_grid(ctx.inputs["load"]["snapshot"],
                            layers=list(plan["layers"]["axis"]) + ["DOTE", "AXIS"])



def _parallel_window_block(lines):
    """A multi-stroke window symbol, not just an arbitrary four-line block.

    Geometry is defined in *block-local* coordinates. It must contain at least
    four similarly long, parallel, separately offset strokes.
    """
    import math
    segs = []
    for raw in lines or ():
        if len(raw) < 4:
            continue
        dx, dy = float(raw[2]) - float(raw[0]), float(raw[3]) - float(raw[1])
        ln = math.hypot(dx, dy)
        if ln > 1e-5:
            segs.append((raw, ln, dx / ln, dy / ln))
    if len(segs) < 4:
        return False
    segs.sort(key=lambda it: it[1], reverse=True)
    _, ref_l, ux, uy = segs[0]
    normals = set()
    for raw, ln, vx, vy in segs:
        if ln < 0.75 * ref_l or abs(ux * vy - uy * vx) > 0.04:
            continue
        mid_x = (float(raw[0]) + float(raw[2])) / 2
        mid_y = (float(raw[1]) + float(raw[3])) / 2
        normals.add(round(((-uy * mid_x + ux * mid_y) / ref_l), 3))
    return len(normals) >= 4 and max(normals) - min(normals) >= 0.15

def _frame_window_block(lines):
    """Recognize four parallel strokes in the familiar double-panel window symbol.

    Some libraries (e.g. WinLib2D) store two long outer strokes and two
    shorter inner strokes. A strict four-*equal*-stroke test misses them.
    Require separated supports and two substantial parallel outer strokes
    before admitting the block family. The insert size is checked separately.
    """
    import math
    segs = []
    for raw in lines or ():
        if len(raw) < 4:
            continue
        dx, dy = float(raw[2]) - float(raw[0]), float(raw[3]) - float(raw[1])
        ln = math.hypot(dx, dy)
        if ln > 1e-5:
            segs.append((raw, ln, dx / ln, dy / ln))
    if len(segs) < 4:
        return False
    segs.sort(key=lambda x: x[1], reverse=True)
    longest = segs[0][1]
    ux, uy = segs[0][2:]
    aligned = []
    for raw, ln, vx, vy in segs:
        if ln < longest * 0.22 or abs(ux * vy - uy * vx) > 0.03:
            continue
        mx = (float(raw[0]) + float(raw[2])) / 2
        my = (float(raw[1]) + float(raw[3])) / 2
        pos = -uy * mx + ux * my
        aligned.append((pos, ln))
    if len(aligned) < 4:
        return False
    positions = sorted(set(round(p / longest, 3) for p, _ in aligned))
    if len(positions) < 2 or positions[-1] - positions[0] < 0.2:
        return False
    return sum(ln >= longest * 0.85 for _, ln in aligned) >= 2


def _shape_blocks(snapshot, plan, kind, scale=None):
    """已知块的**同族**成员 -> {块名: 依据}。

    族语义只从白名单/族表里的可信成员扩散，孤立方块不自己立族；
    候选逐樘仍由洞口+空间证据裁决。

    第一路现在是**块族表**（``blockfamily``）：命名先验 + 几何指纹，一次定一整族。
    一个块名在图上往往有十几个实例（艺术楼 100 根柱只来自 2 个族），逐实例判定
    既慢又必然出现"认出来一部分、剩下一部分没认出来"的不一致。
    """
    found = dict(BLOCKF.names_for(snapshot, kind, scale=scale, plan=plan))
    seeds = (tuple(R.DOOR_BLOCKS) if kind == "door"
             else tuple(R.WIN_BLOCKS) if kind == "window" else ())
    if seeds:
        found.update(BLK.names_like(snapshot, kind,
                                    plan.get("block_families") or None,
                                    seeds=seeds))
    # AutoCAD drawings often contain generic _DBLK / _DBLK1 (opening arc)
    # and _WBLK (unit-square frame). Names alone are NOT enough: require
    # matching block geometry and realistic transformed insert dimensions.
    import re
    block_instances = {}
    for e in snapshot.get("entities") or ():
        if e.get("type") == "INSERT":
            block_instances.setdefault(e.get("name"), []).append(e)
    for name, definition in (snapshot.get("blocks") or {}).items():
        if name in found:
            continue
        is_door = bool(kind == "door" and re.fullmatch(r"_?D(?:OOR)?BLK\d*", name, re.I))
        is_window = bool(kind == "window" and (
            re.fullmatch(r"_?W(?:IN(?:DOW)?)?BLK\d*", name, re.I)
            or re.fullmatch(r"\$WinLib2D\$[0-9]+", name, re.I)))
        if not (is_door or is_window):
            continue
        instances = block_instances.get(name) or []
        if not instances:
            continue
        if is_door:
            arcs = definition.get("arcs") or ()
            if not any(0.25 <= float(a[2]) <= 2.0 for a in arcs):
                continue
            if any(400 <= abs(float(e.get("xscale") or 0)) <= 3000 for e in instances):
                found[name] = "geometric:door_block_arc"
        if is_window:
            lines = definition.get("lines") or ()
            if definition.get("arcs") or not BLOCKF._window_strokes(lines):
                continue
            if any(400 <= abs(float(e.get("xscale") or 0)) <= 5000
                   and 40 <= abs(float(e.get("yscale") or 0)) <= 650
                   for e in instances):
                found[name] = "geometric:window_block_parallel_strokes"

    # ---- 第三路：**长相 + 真实插入长宽比**，完全不看块名。
    # 上面两路（同族扩散、名字正则）都要求"名字里有点线索"，图库一换就失效
    # （$WinLib2D$ / _WBLK / M_A23MCU 全是这么漏的）。这里改用块长相判型，
    # 名字只作溯源标签；为避免家具/索引符号误入，加两条纪律：
    #   * 该块在图上至少出现 2 次（孤立符号不立族）；
    #   * 窗必须细长（长宽比 ≥3）且厚度在门窗厚度带内；柱必须近方形。
    shape = BLK.kinds_by_insert(snapshot, plan.get("block_families") or None)
    scales = BLK.instance_scales(snapshot)
    # 陌生块族的准入门槛：**必须在声明过的门窗层上出现过**，或者这张图压根没有
    # 声明层（单图层/图层不可信的图）。否则办公楼那些层 0 上的杂块
    # （YHRGNTFBV、234…）会被长相判定收成门窗——实测门 30→34、窗 32→34，
    # 还会吃掉墙段让房间从 8 掉到 4。
    declared = set(plan.get("layers", {}).get("door" if kind == "door"
                                             else "window") or ())
    layers_of: dict[str, set] = {}
    for e in snapshot.get("entities") or ():
        if e.get("type") == "INSERT":
            layers_of.setdefault(str(e.get("name")), set()).add(str(e.get("layer")))
    fams = BLOCKF.families(snapshot)
    for name, info in shape.items():
        if name in found or name in R.WIN_BLOCKS or name in R.DOOR_BLOCKS:
            continue
        k, conf = info[0], float(info[1] or 0.0)
        if k != kind or conf < 0.75:
            continue
        sx, sy, n = scales.get(name, (0.0, 0.0, 0))
        if n < 2 or min(sx, sy) <= 0:
            continue
        seen_layers = layers_of.get(name) or set()
        if declared and not (seen_layers & declared):
            continue          # 声明了门窗层，而这块从没画在那几层上 -> 不是门窗
        long_side, short_side = max(sx, sy), min(sx, sy)
        if kind == "window":
            if long_side / short_side < 3.0:
                continue
            if not (40.0 <= short_side <= 650.0):
                continue
            if not (400.0 <= long_side <= 9000.0):
                continue
        elif kind == "column":
            if long_side / short_side > 1.8:
                continue
            if not (150.0 <= long_side <= 2500.0):
                continue
        # 长相只是**提名**，几何签名必须独立确认：块内有没有一条 60~120° 的
        # 摆动弧、弧半径是不是门扇长度、有没有"画轮廓用"的 >180° 大弧。
        # 少了这一步，洁具（马桶块 M_E24TTP：椭圆盆 + 圆角底座，7 条弧）和
        # 电梯厅门（_LV6）都会被"长宽比像门"收成门——实测艺术楼就是这样把
        # 一个马桶画成了蓝门。
        if kind in ("door", "window"):
            ok, why = BLOCKF.signature_ok(fams.get(name) or {}, kind, scale=scale)
            if not ok:
                continue
        found[name] = f"shape:{info[2] if len(info) > 2 else 'insert_scale'}"
    return found


def nearest_rect_mm(point, rects):
    """点到矩形集合的最近距离（mm）；无矩形返回 None。"""
    if not point or not rects:
        return None
    x, y = float(point[0]), float(point[1])
    best = None
    for r in rects:
        dx = max(r[0] - x, 0.0, x - r[2])
        dy = max(r[1] - y, 0.0, y - r[3])
        d = (dx * dx + dy * dy) ** 0.5
        if best is None or d < best:
            best = d
    return round(best, 1)


def _tag_methods(items, extra=()):
    """给门窗候选标出**实际用到的画法**（多画法融合的可审计口径）。

    门窗在不同设计院里可能是：块参照 + 属性编号、只有开启弧、只有图层上的
    短边线、靠同墙线首尾相接归并。把"这条洞口到底靠哪几种画法站住"记下来，
    校准（finalstate）才有独立证据可数，而不是一条来源就算实锤。
    """
    for it in items or ():
        methods = []
        if it.get("block"):
            methods.append("block")
        if it.get("attr") or it.get("type_code"):
            methods.append("attr")
        if it.get("hinges"):
            methods.append("arc")
        if it.get("label_handle"):
            methods.append("label")
        if it.get("host_ok") or it.get("wall_id"):
            methods.append("host")
        if it.get("no_leaf"):
            methods.append("judged")
        if it.get("opening") or it.get("axis"):
            methods.append("layer_span")
        for m in extra:
            if it.get(m):
                methods.append(m)
        it["methods"] = sorted(set(methods)) or ["layer"]
    return items


def node_doors(ctx):
    plan = ctx.inputs["load"]["plan"]
    door_layers = list(dict.fromkeys(list(plan["layers"]["door"]) +
                                       list(plan.get("soft_layers", {}).get("door") or [])))
    snapshot = ctx.inputs["load"]["snapshot"]
    # 是否允许"全图扫弧"只看方案声明的画法，**不能**看"某个门层里恰好有没有弧线"。
    # 否则 profile 判得越全（门层越多），越容易被某一层的装饰弧误伤成 scan_all=False，
    # 门候选直接归零——这不是方案的问题，是开关的判据错了。
    # recognize_doors 内部是渐进降级（块 → 按门层扫弧 → 全图扫弧），
    # 前两级有 "not out" 保护，所以放开 scan_all 不会重复计数。
    return _tag_methods(R.recognize_doors(snapshot, ctx.inputs["texts"],
                                          walls=ctx.inputs["walls_seg"]["walls_seg"],
                                          layers=door_layers or None,
                                          extra=_shape_blocks(snapshot, plan, "door",
                                                              scale=ctx.inputs["load"].get("scale")),
                                          scan_all=has_symbol(plan, "door", "arc_hinge")),
                        extra=("free_end",))


def node_windows(ctx):
    plan = ctx.inputs["load"]["plan"]
    snap = ctx.inputs["load"]["snapshot"]
    walls = ctx.inputs["walls_seg"]["walls_seg"]
    thick = sorted(w.get("thickness", 200.0) for w in walls)
    thick = thick[len(thick) // 2] if thick else 200.0
    win_layers = list(dict.fromkeys(list(plan["layers"]["window"]) +
                                      list(plan.get("soft_layers", {}).get("window") or [])))
    if not win_layers:
        # 没有窗层声明：线束/块两路都放开到全图，逐樘仍要过"宿主墙缺口"这一关
        win_layers = sorted({str(e.get("layer")) for e in snap.get("entities") or ()
                             if e.get("layer")})
    out = R.recognize_windows(snap, ctx.inputs["texts"],
                              layers=win_layers or None, walls=walls,
                              extra=_shape_blocks(snap, plan, "window",
                                                  scale=ctx.inputs["load"].get("scale")))
    # 窗同样是"墙上的一个洞"：线束候选必须落在真实墙缺口（或洞口开在墙端）上。
    # 只按"与已采纳的窗不共线"去重，会把贴在实墙上的窗框装饰线束也算成一樘
    # ——办公楼固定 31 樘窗却输出 32，多出来的那樘就是这条线束。
    wlines = walls or ()
    for b in (OPS.run(["line_bundle"], snap, win_layers, thick, CONFIG) if win_layers else []):
        if b["kind"] != "bundle":
            continue
        line = {"dir": b["dir"], "nrm": b["nrm"], "pos": b["pos"]}
        p1, p2 = GEO.point_at(line, b["lo"]), GEO.point_at(line, b["hi"])
        axis = [[p1[0], p1[1]], [p2[0], p2[1]]]
        half = 0.5 * max(1.0, b["hi"] - b["lo"])
        if any(_same_axis(axis, _axis_of(w), half) for w in out):
            continue
        if not _opening_on_wall_gap(wlines, axis, thick):
            continue
        out.append({"block": None, "layer": b["layer"],
                    "center": [(axis[0][0] + axis[1][0]) / 2, (axis[0][1] + axis[1][1]) / 2],
                    "rotation": round(b.get("angle", 0.0), 1),
                    "width": round(b["hi"] - b["lo"], 1),
                    "thickness": round(thick, 1), "axis": axis, "attr": None,
                    "handle": b["handles"][0],
                    "methods": ["line_bundle"],
                    "srcs": [{"handle": h, "type": "LINE"} for h in b["handles"]]})
    return _tag_methods(out, extra=("line_bundle",))


def _opening_on_wall_gap(wall_lines, seg, thick):
    """Legacy wall-line helper, used by line bundles; orientation-independent."""
    return R._opening_on_wall_gap(wall_lines, seg, thick)


def node_frame(ctx):
    plan = ctx.inputs["load"]["plan"]
    frame, _tb_texts = R.recognize_frame(
        ctx.inputs["load"]["snapshot"], ctx.inputs["texts"],
        frame_layer=plan["scope"].get("frame_layer") or "PUB_TITLE")
    return frame


def node_north(ctx):
    return R.recognize_north(ctx.inputs["load"]["snapshot"], ctx.inputs["texts"],
                             layers=_sem_layers(ctx, "north",
                                                ("DIM_SYMB", "0", "PUB_TEXT")))


def node_stairs(ctx):
    return R.recognize_stairs(ctx.inputs["load"]["snapshot"], ctx.inputs["texts"],
                              layers=_sem_layers(ctx, "stair", ("STAIR",)),
                              scale=_scale_of(ctx))


def node_wall_model(ctx):
    """PURE：墙段 -> 三层墙模型（WallSpan / WallSolid / Gap）+ 墙线。

    斜墙没有 ``pos``（法向偏移）语义，进不了 H/V 墙模型：这里显式跳过并计数，
    不再让整条链路因为一堵斜墙 KeyError 崩掉。
    """
    all_walls = list(ctx.inputs["walls_seg"]["walls_seg"])
    # 斜墙/推断墙可能没有 pos（法向偏移）：就地补算，而不是把它们从墙模型里丢掉
    walls_seg = [_merge_ready(w) for w in all_walls]
    # 弧墙的 pos 是中线半径，没有"法向偏移"语义：进 H/V 墙模型会被当成斜墙，
    # 把墙带/缺口统计搅乱。它们自己已经是一道完整的墙（thickness/length/outline
    # 都在记录里），不需要墙带建模。
    walls_seg = [w for w in walls_seg
                 if w.get("pos") is not None and not w.get("arc")]
    ts = sorted(w["thickness"] for w in walls_seg if w.get("thickness"))
    thick = ts[len(ts) // 2] if ts else None
    model = WM.build(walls_seg, thick)
    lines = WM.lines(model["spans"])
    # 第二阶段测量：墙厚用真实墙结果、门宽用墙缺口宽度的众数，收一次尺度
    refined = SCALE.refine(ctx.inputs["load"].get("scale") or {},
                           walls=walls_seg, gaps=model["gaps"])
    return {"wall_model": model, "lines": lines, "thickness": thick,
            "thickness_source": model["thickness_source"],
            "scale": refined,
            "spans": len(model["spans"]), "gaps": len(model["gaps"]),
            "oblique_skipped": len(all_walls) - len(walls_seg)}


def node_spaces_seed(ctx):
    """PURE：墙网 -> 初步空间面域（洞口服务关系 + 房间覆盖的种子）。

    门候选在这里就当"隔断"用（``barriers``）：门洞是墙上真实的缺口，不补的话
    两间房经门洞连成一个连通域，房间数会系统性偏少（实测艺术楼 56 道墙只围出
    11 个面域、14 间房只落到 8 间）。用**已观测洞口**补齐是唯一允许进观测面域的
    人工补墙——位置和宽度都是构件证据，不是猜的。

    这一步同时给下游的洞口判定提供更强的空间证据（门两侧确实是两个空间），
    所以"房间"和"门窗"两个短板是一起被这一步抬起来的。
    """
    walls_seg = ctx.inputs["walls_seg"]["walls_seg"]
    ts = sorted(w["thickness"] for w in walls_seg if w.get("thickness"))
    thick = ts[len(ts) // 2] if ts else None
    # 一张图里贴了多个建筑平面时按平面分区建面域：否则全图栅格被最大的那张平面
    # 撑爆限额，其余平面的面域一个都出不来（房名会全部变成"无面域"）。
    return SPACES.build_regions(walls_seg,
                                regions=ctx.inputs["load"].get("regions") or (),
                                columns=ctx.inputs["columns"],
                                partitions=ctx.inputs["partitions"],
                                thick=thick,
                                barriers=ctx.inputs.get("doors") or (),
                                scale=_scale_of(ctx))


def node_opening_judge(ctx):
    """条件节点：洞口/通道候选 -> 模型判性质（门/窗/无门扇洞口/不是洞口）。

    与老做法的分工差别：程序只枚举候选并算好区间与上下文（宽度、墙厚、两侧
    有无面域、附近有无开启弧），**不判性质**；模型只看这些候选做归类，坐标由
    ``opening_cases.to_records`` 按候选重算。这样抽象画法（推拉门、只画一条斜线）
    也有机会被认出来，而几何仍然可复算、可审计。
    """
    i = ctx.inputs
    # 候选来自**墙模型**（缺口 = 洞口唯一与画法无关的结构证据），不是 walls_seg：
    # 那里没有 pos/dir，且跨洞口合并后断口已被抹平。
    items = OPCASES.candidates(snapshot=i["load"]["snapshot"],
                               wall_model=i["wall_model"]["wall_model"],
                               spaces=i["spaces_seed"], doors=i["doors"],
                               windows=i["windows"])
    if not items:
        return {"status": "skipped", "items": [], "decisions": {},
                "doors": [], "windows": [], "rejected": [],
                "counts": {"items": 0, "doors": 0, "windows": 0}}
    # 事实 → 规则 → 模型：已固化的判定直接复用，规则能定的不问模型，
    # 只有"疑难杂症"（证据冲突/都缺）才送模型，答完立刻写回事实库。
    decisions, plan = OPCASES.decide(
        items, digest=ctx.file_digest,
        api_key=(ctx.params or {}).get("llm_api_key"))
    patch = OPCASES.to_records(decisions, items)
    patch.update({"status": ("ok" if decisions else "no_action"),
                  "items": items[:40],
                  "candidate_counts": dict(collections.Counter(
                      str(it.get("kind")) for it in items)),
                  "triage": dict(collections.Counter(
                      str(it.get("triage")) for it in items)),
                  "decisions": decisions,
                  "plan": plan,
                  "judge": plan})
    return patch


def node_openings_unify(ctx):
    """PURE：门窗候选（含语义裁定）-> **统一洞口对象**。

    这是"一个洞口只能算一次"的唯一入口：门块、门弧、窗块、窗线束、语义裁定
    全部投影到"所在墙线 + 沿墙区间"，区间重叠的归并成一个洞口，证据取并集，
    类型留到 ``openings_select`` 再定。双扇门的两条弧因此自然归一，家具层上的
    装饰弧因为"没有落在洞口上"自然出局。
    """
    i = ctx.inputs
    judged = i.get("opening_judge") or {}
    doors = list(i["doors"]) + list(judged.get("doors") or ())
    windows = list(i["windows"]) + list(judged.get("windows") or ())
    thick = (i["wall_model"].get("thickness") or 200.0)
    res = UNI.cluster(doors=doors, windows=windows,
                      lines=WM.lines(i["wall_model"]["wall_model"]["spans"]),
                      thick=thick,
                      scale=_scale_of(ctx))
    ds, ws = UNI.split(res["openings"])
    for it in ds + ws:
        it["methods"] = sorted(set(it.get("methods") or ()) | set(it.get("sources") or ()))
    return {"openings": res["openings"], "doors": ds, "windows": ws,
            "stats": res["stats"], "dropped": res["dropped"],
            "shadowed": res["shadowed"], "unplaced": res["unplaced"],
            "summary": UNI.summary(res)}


def node_openings_select(ctx):
    """PURE：洞口候选 -> 构造证据 + 墙体归属 + 空间服务 -> 采纳/待核/否决。"""
    model = ctx.inputs["wall_model"]["wall_model"]
    thick = ctx.inputs["wall_model"]["thickness"] or 200.0
    spaces = ctx.inputs["spaces_seed"]["faces"]
    # 已归并的统一洞口（一个洞只在清单里出现一次）
    unified = ctx.inputs.get("openings_unify") or {}
    doors_in = list(unified.get("doors") or ctx.inputs["doors"])
    windows_in = list(unified.get("windows") or ctx.inputs["windows"])
    picked = OPN.select(doors_in, windows_in,
                        WM.lines(model["spans"]), thick, CONFIG, spaces=spaces)
    # 轻质隔断（LVTRY 之类）不在墙网里：把"最近隔断块距离"作为上下文随候选一起给模型
    parts = (ctx.inputs["spaces_seed"].get("stats") or {}).get("partition_rects") or []
    for entry in picked.get("entries") or ():
        item = entry["item"]
        entry["context"] = {"partition_mm":
                            nearest_rect_mm(item.get("insert") or item.get("center"), parts)}
    report = dict(picked["report"])
    report["spaces_available"] = len(spaces)
    report["unified"] = (unified or {}).get("stats") or {}
    doors_out, windows_out = picked["doors"], picked["windows"]
    # ---- 判定优先级裁决：多条判据命中同一洞口时，**按证据强度定类，不按谁先跑完**。
    # 实测：5.5m 长、180mm 厚（=墙厚）、写着窗号 C15、画在 WINDOW 层的窗，被门的
    # 形状判据抢先命中，输出成"5538mm 宽的门"。裁决后回归为窗，并留痕。
    pri = PRIO.apply(doors_out, windows_out,
                     plan=ctx.inputs["load"].get("plan"),
                     families=ctx.inputs["load"].get("block_families"),
                     wall_thick=thick)
    doors_out, windows_out = pri[0], pri[1]
    report["priority"] = pri[2]
    if (ctx.params or {}).get("llm_verify"):
        # 只复核低置信/证据缺失/矛盾的那一小撮；并发请求，结果按证据包哈希缓存
        review = LLMVERIFY.run(picked.get("entries") or (),
                              api_key=(ctx.params or {}).get("llm_api_key"))
        report["llm_review"] = review
        for entry in picked.get("entries") or ():
            item = entry["item"]
            if item.get("_llm_accept") and item not in (doors_out + windows_out):
                (doors_out if entry.get("kind") == "door" else windows_out).append(item)
    # ---- 模式 B：模型**只能否决**（只做减法）
    # 放开候选之后真正的问题是误报（单图层图实测出 109 樘"窗"，其实是长条家具），
    # 这里让模型只说"这不像门/窗"，程序照办把它从采纳清单里摘出来。
    # 硬边界：keep / unsure 同一分支（不改动）；模型不能新增候选、不能提升置信、
    # 不能给几何（回复里带坐标/尺寸字段整条丢弃，见 llm_review._sanitize）。
    if (ctx.params or {}).get("llm_review", True):
        doors_out, windows_out, mode_b = LLMREVIEW.review_openings(
            doors_out, windows_out,
            api_key=(ctx.params or {}).get("llm_api_key"),
            cache_dir=ctx.params.get("cache_dir"),
            snapshot=ctx.inputs["load"]["snapshot"],
            vision=(ctx.params or {}).get("llm_review_vision", True))
        report["mode_b"] = mode_b
    return {"doors": doors_out, "windows": windows_out,
            "report": report,
            "pending_doors": sum(1 for d in doors_out if d.get("_pending"))}


def _merge_ready(w):
    """墙段归一：只补**下游必需但可能缺失**的字段。

    关键：不擅自给"只有 start/end"的墙补 ``dir/nrm/angle``。墙模型对没有
    dir/nrm 的墙走 ``GEO.as_support``（按 orient 归到 0° / 90°），擅自补真实
    角度会把 89.9° 的墙分到另一个角度桶，墙网被切碎（房间数会掉一半）。
    """
    if not isinstance(w, dict):
        return w
    s, e = w.get("start"), w.get("end")
    if not (isinstance(s, (list, tuple)) and isinstance(e, (list, tuple))):
        return w
    dx, dy = float(e[0]) - float(s[0]), float(e[1]) - float(s[1])
    ln = (dx * dx + dy * dy) ** 0.5
    if ln <= 1e-9:
        return w
    out = dict(w)
    horiz = abs(dx) >= abs(dy)
    out.setdefault("orient", "H" if horiz else "V")
    mid = ((float(s[0]) + float(e[0])) / 2.0, (float(s[1]) + float(e[1])) / 2.0)
    if out.get("center") is None:
        out["center"] = round(mid[1] if horiz else mid[0], 2)
    if out.get("pos") is None:
        out["pos"] = out["center"]
    if out.get("lo") is None or out.get("hi") is None:
        out["lo"] = round(min(float(s[0]), float(e[0])) if horiz
                          else min(float(s[1]), float(e[1])), 2)
        out["hi"] = round(max(float(s[0]), float(e[0])) if horiz
                          else max(float(s[1]), float(e[1])), 2)
    if out.get("dir") and out.get("nrm") and out.get("angle") is None:
        out["angle"] = round(__import__("math").degrees(
            __import__("math").atan2(float(out["dir"][1]), float(out["dir"][0]))) % 180.0, 3)
    out.setdefault("length", round(ln, 1))
    out.setdefault("thickness", None)
    out.setdefault("caps_n", 0)          # merge_runs 需要的墙体端点证据计数
    out.setdefault("segments", [])
    out.setdefault("openings", [])
    out.setdefault("srcs", [])
    return out


def node_walls_merge(ctx):
    """PURE：墙段合并为 wall run，并把采纳的洞口记到墙上。"""
    doors_all = ctx.inputs["doors"]
    windows_all = ctx.inputs["windows"]
    walls_seg = ctx.inputs["walls_seg"]["walls_seg"]
    final = ctx.inputs.get("final_candidate_state") or {}
    patch_accepted = (final.get("provenance", {}).get("patch") == "accepted"
                      and bool(final.get("walls")))
    if patch_accepted:
        # 系统已接受补丁：墙段以统一状态为准（区域外原样保留）
        walls_seg = list(final.get("walls") or walls_seg)
    walls_seg = [_merge_ready(w) for w in walls_seg]
    thick = ctx.inputs["wall_model"]["thickness"] or 200.0
    selected = ctx.inputs["openings_select"]
    doors_sel, windows_sel = selected["doors"], selected["windows"]
    if patch_accepted:                       # 洞口也以统一状态为准（含求解新增）
        doors_sel = list(final.get("doors") or doors_sel)
        windows_sel = list(final.get("windows") or windows_sel)

    # 只用 openings_select 采纳的洞口改写实体墙：被否决的装饰弧/错误窗候选
    # 不得改变墙网与房间边界（未决洞口只作软证据，不进墙）。
    # One geometric host projection for both the initial segmentation and the
    # final continuous-wall merge. Never quantize oblique openings to H/V.
    sel_records = _rich_openings(doors_sel, windows_sel, walls_seg, thick)
    sel_openings = [(op['orient'], op['pos'], op['lo'], op['hi'])
                    for op in sel_records]
    walls_all = wallrec.merge_runs(walls_seg, sel_openings, min_length=0)
    walls = [w for w in walls_all if w["length"] >= 250]

    wall_runs = wallrec.merge_across_openings(
        walls, _rich_openings(doors_sel, windows_sel, walls, thick),
        max_gap=3000.0, slack=120.0,
        min_length=250.0)
    # 最后一道口径修正（用户给定）：**一面墙开了门窗仍然是一面墙**，只有被柱子或
    # 另一道墙拦住才断开。merge_across_openings 只认得"已采纳的洞口"，门没识别
    # 出来时同一面墙就会碎成两段；这里补上。
    columns_all = list(ctx.inputs.get("columns") or ())
    runs_collinear = wallrec.merge_collinear(
        wall_runs, blockers=columns_all, max_gap=3000.0, min_length=250.0)
    stats = dict(ctx.inputs["walls_seg"]["wdiag"])
    stats["segments_before_run_merge"] = len(walls_seg)
    stats["runs_after_merge"] = len(walls_all)
    stats["runs_kept"] = len(walls)
    stats["runs_after_opening_merge"] = len(wall_runs)
    stats["runs_after_collinear_merge"] = len(runs_collinear)
    stats["openings_selected"] = len(doors_sel) + len(windows_sel)
    stats["openings_candidates"] = len(doors_all) + len(windows_all)
    return {"walls_all": walls_all, "walls": walls, "wall_runs": runs_collinear,
            "stats": stats, "wall_model": ctx.inputs["wall_model"]["wall_model"],
            "thickness": thick,
            "selected": {"doors": doors_sel, "windows": windows_sel},
            "openings": [[o, p, lo, hi] for (o, p, lo, hi) in
                         wallrec.openings_from(doors_sel, windows_sel)],
            "openings_report": selected["report"]}


def node_envelope(ctx):
    walls = ctx.inputs["walls_merge"]["walls"]
    if not walls:
        header = ctx.inputs["load"]["snapshot"].get("header") or {}
        lo, hi = header.get("extmin"), header.get("extmax")
        if lo and hi:
            return [lo[0] - 800, lo[1] - 800, hi[0] + 800, hi[1] + 800]
        return [0.0, 0.0, 1000.0, 1000.0]
    return [min(w["start"][0] for w in walls) - 800,
            min(w["start"][1] for w in walls) - 800,
            max(w["end"][0] for w in walls) + 800,
            max(w["end"][1] for w in walls) + 800]


def _scale_of(ctx):
    """当前可用的图内尺度：**已经过墙网精化的优先**，否则用 load 阶段的初值。

    阈值必须用同一份尺度，否则同一张图里不同节点会拿不同的门宽去算倍率。
    """
    wm = (ctx.inputs.get("wall_model") or {})
    return wm.get("scale") or (ctx.inputs.get("load") or {}).get("scale") or {}


def _sem_layers(ctx, sem, default=()):
    """profile/自动推导给出的图层清单，并对齐到图里真实存在的图层名。

    对齐这一步是"换套图层名还能认"的关键：声明的是"散水"，图纸里可能是
    "PZ_散水"。``resolve_layers`` 先全等、再按归一化键匹配，匹配不到才保留原名。
    """
    plan = (ctx.inputs["load"].get("plan") or {})
    declared = list((plan.get("layers") or {}).get(sem) or ()) or list(default)
    snap = ctx.inputs["load"].get("snapshot") or {}
    have = {e.get("layer") for e in snap.get("entities") or () if e.get("layer")}
    return resolve_layers(declared, have)


def node_steps(ctx):
    return R.recognize_steps(ctx.inputs["load"]["snapshot"], ctx.inputs["texts"],
                             ctx.inputs["walls_merge"]["walls"],
                             layers=_sem_layers(ctx, "step", ("散水",)))


def node_entrance_steps(ctx):
    return R.recognize_entrance_steps(ctx.inputs["load"]["snapshot"],
                                      ctx.inputs["walls_merge"]["walls"],
                                      layers=_sem_layers(ctx, "step", ("散水",)))


def node_apron(ctx):
    apron, _polys = R.recognize_apron(ctx.inputs["load"]["snapshot"],
                                      ctx.inputs["walls_merge"]["walls"],
                                      layers=_sem_layers(ctx, "apron", ("散水",)))
    return apron


def node_room_labels(ctx):
    # Use the plan resolved by load (including auto-inferred/renamed layers).
    # Reloading the user profile discards drawing-specific room-name layers.
    plan = ctx.inputs["load"]["plan"]
    layers = list(plan["layers"]["room_name"]) + list(RM.ROOM_NAME_LAYERS)
    labels, areas = RM.room_labels(ctx.inputs["texts"], ctx.inputs["envelope"],
                                   layers=layers)
    if not labels:
        # Nonstandard text-layer names (e.g. FF-TEXT) should not suppress
        # names supported by strong Chinese room-suffix morphology. This is a
        # high-precision fallback, bounded by the existing building envelope.
        all_text_layers = {t.get("layer") for t in ctx.inputs["texts"] if t.get("layer")}
        labels, areas = RM.room_labels(ctx.inputs["texts"], ctx.inputs["envelope"],
                                       layers=all_text_layers)
    return {"labels": labels, "areas": areas}


def _attach_drawing_area(labels, ctx):
    """把图纸自带的面积标注补回到房名候选上。

    ``label_filter`` 那批候选只带文字本身（``text/x/y/handle``），图纸原有的
    "房名 + 下方面积标注"关系会丢；房间输出因此没有 ``area_label``，图上唯一
    可以自证的几何真值也随之消失。按 handle 回到 ``room_labels`` 的候选上取面积，
    取不到再按与 ``rooms.room_labels`` 相同的邻近规则（≤1400 mm）配一次。
    只补属性，不改任何候选的采纳/拒绝。
    """
    src = (ctx.inputs.get("room_labels") or {})
    by_handle = {}
    for lab in src.get("labels") or ():
        if lab.get("area_label") is not None:
            by_handle[str(lab.get("handle"))] = lab
    areas = src.get("areas") or ()
    for lab in labels:
        if lab.get("h") is None:
            lab["h"] = lab.get("height")
        if lab.get("area_label") is not None:
            continue
        hit = by_handle.get(str(lab.get("handle")))
        if hit:
            lab["area_label"] = hit["area_label"]
            lab["area_handle"] = hit.get("area_handle")
            lab["area_sup_handle"] = hit.get("area_sup_handle")
            continue
        best, best_d2 = None, 1400.0 ** 2
        lx, ly = float(lab.get("x") or 0.0), float(lab.get("y") or 0.0)
        for a in areas:
            d2 = (a["x"] - lx) ** 2 + (a["y"] - ly) ** 2
            if d2 < best_d2:
                best_d2, best = d2, a
        if best:
            lab["area_label"] = best["value"]
            lab["area_handle"] = best["handle"]
    return labels


def _room_label_source(ctx):
    """房间名来源：优先"模型判为 room"的那批，退回到形态规则那批。

    两批的分工：``room_labels`` 用后缀词表（室/厅/卫/间…）确定性筛，覆盖稳但会
    漏掉"内走廊""男厕""医技街"这类；``label_filter`` 把全部候选送模型判角色，
    覆盖广但依赖模型可用。**由系统决定用哪一批**：模型那批有货就用它，没有就退回
    确定性那批——用户不需要知道这里有没有过 LLM。
    """
    fed = (ctx.inputs.get("label_filter") or {}).get("labels") or ()
    picked = []
    for lab in fed:
        if (lab.get("role") or "room") != "room":
            continue
        name = (lab.get("text") or lab.get("name") or "").strip()
        if not name:
            continue
        picked.append(dict(lab, name=name))
    # Union independent evidence sources. The former early return discarded
    # every legitimate suffix-based room whenever *one* model/geometry label
    # was promoted. Mixed/no-layer drawings often have both kinds.
    seen = {(str(x.get("handle") or ""), round(float(x.get("x") or 0), 1),
             round(float(x.get("y") or 0), 1)) for x in picked}
    added = 0
    for lab in (ctx.inputs.get("room_labels") or {}).get("labels") or ():
        name = (lab.get("text") or lab.get("name") or "").strip()
        if not name:
            continue
        key = (str(lab.get("handle") or ""), round(float(lab.get("x") or 0), 1),
               round(float(lab.get("y") or 0), 1))
        if key not in seen:
            seen.add(key)
            picked.append(dict(lab, name=name, role="room",
                               role_source="lexical_fallback"))
            added += 1
    return _attach_drawing_area(picked, ctx), (
        "label_filter+lexical" if added and len(picked) > added
        else "room_labels" if added else "label_filter")


#: 与"楼梯/电梯"构件重复的房名文字：这些点位已由 stair 类别计数，不再算房间
_STAIR_NAMES = ("楼梯", "楼梯间", "电梯", "电梯井", "自动扶梯", "扶梯", "消防电梯")


def _drop_stair_labels(labels, stairs, radius=6000.0):
    """去掉"就是楼梯本身"的房名文字（名字完全相同且落在楼梯构件附近）。

    只按**完全相同的名字**+**位置邻近**两个条件一起判：一个叫"楼梯间"的房间、
    或图纸另一侧的"楼梯"文字，都不会被误伤。
    """
    if not stairs:
        return labels
    boxes = [s.get("bbox") for s in stairs if s.get("bbox")]
    keep, dropped = [], []
    for lab in labels:
        name = (lab.get("name") or lab.get("text") or "").strip()
        if name not in _STAIR_NAMES:
            keep.append(lab)
            continue
        x, y = float(lab.get("x") or 0.0), float(lab.get("y") or 0.0)
        near = False
        for b in boxes:
            dx = max(b[0] - x, 0.0, x - b[2])
            dy = max(b[1] - y, 0.0, y - b[3])
            if (dx * dx + dy * dy) ** 0.5 <= radius:
                near = True
                break
        (dropped if near else keep).append(lab)
    return keep


def node_rooms(ctx):
    """PURE：**面域优先**的房间识别（拓扑先行，房名挂靠）。

    房间的几何来自墙网独立算出的封闭面域，房名只负责"这块面域叫什么"。旧做法
    反过来用房名当泛洪种子，一个连通域只能出一个房间，于是同一区域挂两个房名就
    丢一个、房名压在墙线上就整个漏掉——大图上 75 个房名只落到 25 个房间，原因
    全在这一段。挂不上任何面域的房名不丢弃：交给开放分区/修复分支，并带上到
    最近面域的距离，用来区分"墙网漏检"与"真的是开放空间"。
    """
    final = ctx.inputs.get("final_candidate_state") or {}
    faces = final.get("faces") or ctx.inputs["spaces_seed"]["faces"]
    faces_inferred = bool(final.get("faces_inferred"))
    labels, source = _room_label_source(ctx)
    labels = _drop_stair_labels(labels, ctx.inputs.get("stairs") or ())

    # 面域最小面积随图内尺度走（4 个字高的方格），不再写死 1 m²
    th = SCALE.thresholds(_scale_of(ctx))
    found = RM.rooms_from_faces(
        faces, labels,
        min_area=float(th["face_min_area"]),
        near_tol=float(th["label_near_tol"]),
        inside_tol=float(th["label_inside_tol"]),
        same_name_adj=float(th["same_name_adj"]),
        split_res=float(th["split_res"]),
        split_min_cell=float(th["split_min_cell"]))
    room_recs = found["rooms"]
    orphans = found["orphans"]
    SPACES.attach_rooms(room_recs, faces)
    if faces_inferred:
        for r in room_recs:
            if r.get("boundary_state") != "observed":
                r["boundary_state"] = "hypothesis"
                r["needs_review"] = True

    room_recs, merge_stats = RMERGE.dedupe(room_recs)

    # 开放分区：房名挂不上任何面域，且附近没有断墙证据 -> 只当"开放空间"处理
    routed = ctx.inputs.get("space_routing") or {}
    if routed:
        loose = routed.get("open_labels") or []      # 只有模型判 open_space 的才建分区
    else:
        loose = []
        for lab in orphans:
            if lab.get("nearest_distance") is not None and lab["nearest_distance"] > 6000.0:
                continue
            if any(ch.isdigit() for ch in (lab.get("name") or "")):
                continue
            loose.append(lab)
    enclosed = [(r.get("label") or {}).get("x") for r in room_recs]
    zones = OZ.build(labels=loose, enclosed_labels=[],
                     defects=(ctx.inputs.get("defect_detect") or {}).get("defects") or ())
    for z in zones:
        head = z["label"]
        room_recs.append({
            "label": dict(head, name=head.get("name") or z["room_name"],
                          x=head.get("x"), y=head.get("y"),
                          handle=head.get("handle"), layer=head.get("layer"),
                          area_label=None, area_handle=None, area_sup_handle=None),
            "polygon": z["polygon"], "area": z["area"], "area_m2": z["area_m2"],
            "bbox": z["bbox"], "leak": False, "seed_inside": False,
            "kind": "open_zone", "status": "open", "boundary": "label_cluster",
            "area_source": "bbox_estimate", "note": z["note"], "open_zone": z,
            "srcs": [{"handle": h, "type": "TEXT"} for h in z["handles"]]})

    out = []
    for room in room_recs:
        item = dict(room)
        item["polygon"] = [[float(p[0]), float(p[1])] for p in (room.get("polygon") or [])]
        out.append(item)
    stats = dict(found["stats"], label_source=source, zones=len(zones),
                 merge=merge_stats)
    for item in out:
        item["_merge_stats"] = merge_stats
        item["_face_stats"] = stats
    return out


#: 详图/大样/图例/说明类图层：这些层画的是"构造做法示意"或图例，不是平面里的构件。
#: 实测（艺术楼）："设置"层上的块被当成门窗、半径 10/40mm 的装饰弧被当成平开门。
#: 注意**不要把"设置"放进来**：实测艺术楼「设置」层 186 个图元里 115 个落在
#: 男生卫生间平面内，是洁具与隔断（含隔断门）的布置，属于平面内容，不是图例。
#: 早先把"设置"当详图层整层排除，结果把两樘卫生间隔断门也删了。
DETAIL_LAYER_HINTS = ("大样", "详图", "图例", "说明", "做法", "构造",
                      "DETAIL", "LEGEND", "NOTE")


def _is_detail_layer(name) -> bool:
    n = str(name or "").upper()
    return any(h.upper() in n for h in DETAIL_LAYER_HINTS)


def _is_noise_layer(name) -> bool:
    """纯标注/填充层：不参与构件候选（判据与理由见 ``base.is_noise_layer``）。"""
    return _BASE.is_noise_layer(name)


def _filter_components(components, scale=None):
    """组装后的三道过滤，返回 (保留, 剔除)，都带 ``drop_reason`` 可追溯。

    1. **详图/图例图层**上的图元不产生平面构件；
    2. **同位置重复**（图上把同一樘画了两遍）只算一次；
    3. 洞口**特别宽**的不删除——大门/卷帘门/门洞确实可以很宽——只在属性里
       标记待核，交人工判断（"宽 5538mm" 这种既可能是大门，也可能是窗块被
       误判成门，光看数字定不了）。
    """
    from . import scale as SC
    th = SC.thresholds(scale) if scale else {}
    door_w = float(th.get("door_w") or SC.DEFAULT_DOOR_W)
    thick = float(th.get("thickness") or SC.DEFAULT_THICKNESS)
    wide = max(door_w * float(SC.k("gap_max_door")), 12.0 * thick)
    keep, dropped, seen = [], [], {}
    for c in components:
        layers = {str(s.get("layer") or "")
                  for s in (c.get("source_entities") or []) if s.get("layer")}
        if layers and all(_is_detail_layer(l) for l in layers):
            c["drop_reason"] = ("详图/图例图层（" + "、".join(sorted(layers))
                                + "）上的图元，不是平面构件")
            dropped.append(c)
            continue
        if c.get("category") in ("door", "window"):
            g = c.get("geometry") or {}
            pt = (c.get("insert_pt") or g.get("insert")
                  or g.get("center") or [0.0, 0.0])
            key = (c["category"], round(float(pt[0]) / 50.0),
                   round(float(pt[1]) / 50.0))
            if key in seen:
                c["drop_reason"] = "同一位置重复实例（图上把同一樘画了两遍）"
                dropped.append(c)
                continue
            seen[key] = c
            w = (c.get("attributes") or {}).get("width_mm")
            if w is not None and float(w) > wide:
                c.setdefault("attributes", {})
                c["attributes"]["wide_opening"] = True
                c.setdefault("needs_review", True)
                c["status"] = "uncertain"
                ev = dict(c.get("evidence") or {})
                ev["wide_opening"] = (
                    f"洞口宽 {float(w):.0f}mm，超出常规门宽（>{wide:.0f}mm）："
                    "可能是大门/卷帘门，也可能是窗块被误判成门，需人工确认")
                c["evidence"] = ev
        keep.append(c)
    return keep, dropped


def node_assemble(ctx):
    """MAIN：组装 components / unrecognized / meta。"""
    inputs = ctx.inputs
    load = inputs["load"]
    units = load.get("units") or {}
    spaces = inputs["spaces_seed"].get("stats") or {}
    open_report = inputs["openings_select"].get("report") or {}
    frame = inputs["frame"]
    grid = inputs["grid"]
    north = inputs["north"]
    columns = (inputs.get("columns_verify") or {}).get("columns") or inputs["columns"]
    # C5：给墙写 id 前必须复制，避免污染 walls_merge 的输出（缓存/上游复用）
    wall_runs = [dict(w) for w in inputs["walls_merge"]["wall_runs"]]
    for k, wall in enumerate(wall_runs, 1):
        wall["id"] = f"WALL-{wall['orient']}-{k:03d}"
    selected = inputs["walls_merge"].get("selected") or {}
    doors = selected.get("doors", inputs["doors"])
    windows = selected.get("windows", inputs["windows"])
    room_recs = inputs["rooms"]
    stairs = inputs["stairs"]
    steps = list(inputs["steps"])
    for es in inputs["entrance_steps"]:
        steps.append({"bbox": es["bbox"], "levels": max(1, es["edge_count"] - 1),
                      "tread_depths": es["tread_depths"], "label": "入口台阶（边线法）",
                      "label_handle": None, "srcs": es["srcs"], "entrance": True})
    apron = inputs["apron"]
    wdiag = inputs["walls_merge"]["stats"]
    wdropped = inputs["walls_seg"]["wdropped"]
    doc, msp = ctx.doc, ctx.msp

    components = []

    # ---------------- 图框
    if frame:
        components.append({
            "id": "FRAME-01", "category": "frame", "name": "图纸图框（含图签）",
            "geometry": {"type": "rectangle", "outer": frame["outer"], "inner": frame["inner"],
                         "width": _round(frame["width"]), "height": _round(frame["height"]),
                         "title_block_zone": frame["title_block"]},
            "attributes": {"drawing_name": frame["drawing_name"],
                           "drawing_number": frame["drawing_number"],
                           "project": frame["project"], "scale": "1:100",
                           "sheet_mm_at_1_100": [_round(frame["width"] / 100), _round(frame["height"] / 100)]},
            "source_entities": frame["srcs"] + frame["title_texts"],
            "evidence": {"rule": "PUB_TITLE 闭合多段线（外框+内框）+ 图签区文字",
                         "detail": "内框右侧 7000mm 竖带内检出项目名/图名/图号"},
            "confidence": 0.95, "status": "confirmed"})

    # 图签栏：内框右侧竖带（图纸范围框之外），人工标注单列
    if frame:
        tb = frame["title_block"]
        components.append({
            "id": "FRAME-02", "category": "frame", "name": "图签栏",
            "geometry": {"type": "rectangle", "bbox": [tb["x0"], tb["y0"], tb["x1"], tb["y1"]],
                         "outline": [[tb["x0"], tb["y0"]], [tb["x1"], tb["y0"]],
                                     [tb["x1"], tb["y1"]], [tb["x0"], tb["y1"]]]},
            "attributes": {"role": "title_block", "width": tb["x1"] - tb["x0"],
                           "height": tb["y1"] - tb["y0"]},
            "source_entities": frame["srcs"][:1],
            "evidence": {"rule": "内框右侧竖带（图纸范围框 x 右边界至内框右边界）",
                         "detail": "图名/图号/项目名三行文字均落在该竖带内"},
            "confidence": 0.9, "status": "confirmed"})

    # ---------------- 轴网（按"轴号气泡"计，与人工标注口径一致）
    for k, b in enumerate(sorted(grid["bubbles"], key=lambda b: (b["y"], b["x"])), 1):
        if not b.get("label"):
            continue
        axis = next((a for a in grid["vaxes"] if abs(a["pos"] - b["x"]) < 5), None) or \
               next((a for a in grid["haxes"] if abs(a["pos"] - b["y"]) < 5), None)
        r = b.get("r") or 0
        components.append({
            "id": f"AXIS-{k:02d}", "category": "grid_axis",
            "name": f"{b['label']} 轴号",
            "geometry": {"type": "circle", "center": [_round(b["x"]), _round(b["y"])],
                         "radius": _round(r),
                         "outline": [[_round(b["x"] - r), _round(b["y"] - r)],
                                     [_round(b["x"] + r), _round(b["y"] - r)],
                                     [_round(b["x"] + r), _round(b["y"] + r)],
                                     [_round(b["x"] - r), _round(b["y"] + r)]],
                         "axis_line": ([_round(axis["pos"]), _round(axis["extent"][0])],
                                       [_round(axis["pos"]), _round(axis["extent"][1])])
                         if axis and axis.get("extent") else None},
            "attributes": {"axis_label": b["label"],
                           "orientation": "vertical" if axis in grid["vaxes"] else "horizontal",
                           "bubble_radius": _round(r),
                           "axis_position": _round(axis["pos"]) if axis else None},
            "source_entities": [{"handle": b["handle"], "type": "INSERT", "layer": "AXIS",
                                 "role": "_AXISO 轴号气泡"}]
                                + [{"handle": h, "type": "LINE", "layer": "DOTE", "role": "轴线"}
                                   for h in (axis or {}).get("line_handles", [])],
            "evidence": {"rule": "_AXISO 块属性 A 给出轴号；两端同号气泡定出一条轴线",
                         "detail": f"轴号 {b['label']}，气泡半径 {r:.0f}mm"},
            "confidence": 0.95, "status": "confirmed"})

    # ---------------- 指北针
    if north:
        components.append({
            "id": "NORTH-01", "category": "north_arrow", "name": "指北针",
            "geometry": {"type": "symbol", "center": _pts([north["center"]])[0],
                         "radius": _round(north["radius"]), "needle": _pts(north["needle"]),
                         "north_tip": _pts([north["north_tip"]])[0],
                         "bbox": [_round(min(north["center"][0] - north["radius"],
                                            north["needle"][0][0], north["needle"][1][0])),
                                  _round(min(north["center"][1] - north["radius"],
                                            north["needle"][0][1], north["needle"][1][1])),
                                  _round(max(north["center"][0] + north["radius"],
                                            north["needle"][0][0], north["needle"][1][0])),
                                  _round(max(north["center"][1] + north["radius"],
                                            north["needle"][0][1], north["needle"][1][1]))]},
            "attributes": {"azimuth_deg": north["azimuth_deg"],
                           "rotation_from_sheet_up_deg": north["rotation_from_sheet_up_deg"],
                           "label": north["label"],
                           "meaning": ("正北方向相对图纸正上方"
                                       + ("逆时针偏 " if north["rotation_from_sheet_up_deg"] < 0 else "顺时针偏 ")
                                       + f"{abs(north['rotation_from_sheet_up_deg']):.2f}°")},
            "source_entities": north["srcs"],
            "evidence": {"rule": "DIM_SYMB 图层圆 + 过圆心指针线 + '北' 字定位指向端",
                         "detail": "指针线与圆直径等长且过圆心"},
            "confidence": 0.9, "status": "confirmed"})

    # ---------------- 柱
    pending_cols = []
    for k, c in enumerate(sorted(columns, key=lambda c: (-c["center"][1], c["center"][0])), 1):
        # 来源一律取候选自己的 srcs（块 / 轮廓 / 填充 / 线段组合），不再固定写
        # INSERT+COLUMN+_FZH——那样闭合轮廓柱会被记成"块参照"，证据审计直接失真。
        srcs = [dict(x) for x in (c.get("srcs") or ()) if x.get("handle")]
        if not srcs and c.get("handle"):
            srcs = [{"handle": c["handle"], "type": "CONTOUR",
                     "layer": c.get("layer"), "role": "柱轮廓"}]
        methods = list(c.get("methods") or (["block"] if c.get("handle") else []))
        layer_txt = "/".join(sorted({str(x.get("layer") or "") for x in srcs
                                     if x.get("layer")})) or "?"
        rule = ("柱多画法融合：" + "、".join(_COL_METHOD_CN.get(m, m) for m in methods)
                + f"（图层 {layer_txt}），截面尺寸 + 端头/轴网连接校验")
        if (c.get("status") or "pending") == "pending":
            # 证据不足的柱候选不导出为构件（只在未决/未识别里留痕），
            # 这是"提升召回必须同步防过检"的出口。
            pending_cols.append(c)
            continue
        components.append({
            "id": f"COL-{k:02d}", "category": "column", "name": f"柱 {_round(c['w'])}×{_round(c['h'])}",
            "geometry": {"type": "rectangle", "outline": _pts(c["outline"]), "bbox": [_round(v) for v in c["bbox"]],
                         "center": [_round(c["center"][0]), _round(c["center"][1])],
                         "width": _round(c["w"]), "depth": _round(c["h"])},
            "attributes": {"section": f"{_round(c['w'])}×{_round(c['h'])}",
                           "rotation": c["rotation"], "material": "未标注",
                           "methods": methods, "score": c.get("score"),
                           "evidence": c.get("evidence")},
            "source_entities": srcs,
            "evidence": {"rule": rule,
                         "detail": f"截面 {c['w']:.0f}×{c['h']:.0f}，"
                                   f"证据 {'+'.join(c.get('evidence') or []) or '单一来源'}，"
                                   f"得分 {float(c.get('score') or 0):.2f}"},
            "confidence": round(float(c.get("confidence") or 0.5), 3),
            "status": c.get("status") or "uncertain",
            "needs_review": bool(c.get("needs_review", True))})

    # ---------------- 墙（跨门窗洞口合并后的连续墙实例）
    pending_walls = []
    for w in wall_runs:
        arc_curve = w.get("curve") if w.get("arc") else None
        if w.get("arc"):
            # 弧墙没有端头封口线（弧的"端头"是扇形端面），证据是同心 / 半径差 =
            # 墙厚 / 角度区间重叠这三条纯几何不变量，不靠封口计数也不靠拓扑兜底。
            conf = 0.90 if w.get("evidence_state") == "strong" else 0.65
        elif w["caps_n"] >= 2:
            conf = 0.95
        elif w["caps_n"] == 1:
            conf = 0.85
        else:
            conf = 0.65
        if not arc_curve and w.get("caps_n", 0) == 0 and float(w.get("topo_score") or 0.0) >= 0.5:
            # 没有端部封口线、但两端真的接进墙网/柱：证据来自拓扑，不是自证
            conf = 0.85
        if w.get("evidence_state") in ("weak", "pending"):
            # 弱厚度簇 / 靠拓扑保留的墙可以进墙网、可以被局部求解采用，
            # 但不得凭"看得见"或"补上就闭合了"就 confirmed
            conf = min(conf, 0.65)
        if load["plan"].get("unlayered"):
            # A geometric pair on an annotation-rich single layer is a candidate,
            # not proof that this entity is a structural wall.
            conf = min(conf, 0.70)
        opening_attrs = []
        opening_srcs = []
        for op in w.get("openings", []):
            width = op.get("width")
            opening_attrs.append({
                "category": op["category"], "id": op["id"],
                "width": _round(width) if width is not None else None,
                "span": [_round(v) for v in op.get("span", [])],
                "type_code": op.get("type_code"),
                "handle": op.get("handle"), "layer": op.get("layer"),
            })
            if op.get("handle"):
                opening_srcs.append({
                    "handle": op["handle"], "type": "INSERT",
                    "layer": op.get("layer", ""),
                    "role": f"{op['category']} {op['id']} 洞口"})
        if arc_curve:
            detail = (("弧形幕墙带：" if w.get("role") == "curtain" else "同心圆弧配对：")
                      + f"内 R{arc_curve['inner_radius']:.0f} / "
                      f"外 R{arc_curve['outer_radius']:.0f}（半径差 {w['thickness']:.0f}mm），"
                      f"张角 {w.get('arc_sweep_deg') or 0:.0f}°，弧长 {w['length']:.0f}mm")
        else:
            if w.get("role") == "curtain":
                detail = (f"共面 {w.get('band_lines') or 3} 条线等距归并成一条幕墙带："
                          f"带宽 {w['thickness']:.0f}mm（带内线间距 "
                          f"{w.get('band_pitch') or 0:.0f}mm），长 {w['length']:.0f}mm")
            else:
                detail = (f"两条面线间距 {w['thickness']:.0f}mm，"
                          f"沿墙向重叠 {w['length']:.0f}mm，端部封口 {w['caps_n']} 处")
        if opening_attrs:
            detail += (f"；跨 {len(opening_attrs)} 个门窗洞口连续"
                       f"（{'、'.join(op['id'] for op in opening_attrs)}）")
        axis_label = {"H": "横墙", "V": "纵墙", "A": "弧墙"}.get(w["orient"], "斜墙")
        if w.get("role") == "curtain":
            axis_label = "幕墙"
        if w.get("evidence_state") == "pending":
            # 只靠拓扑（端头/柱连接）保留的墙：可以进墙网帮房间闭合，
            # 但**没有独立几何证据**，不能作为构件输出——进未决队列等人工复核。
            pending_walls.append(w)
            continue
        wall_geom = {"type": "wall_arc" if arc_curve else "wall_run",
                     "start": _pts([w["start"]])[0],
                     "end": _pts([w["end"]])[0], "thickness": _round(w["thickness"]),
                     "length": _round(w["length"]), "outline": _pts(w["outline"]),
                     "segments": w.get("segments", []),
                     "segment_count": len(w.get("segments", []))}
        if arc_curve:
            wall_geom["curve"] = dict(arc_curve)
        components.append({
            "id": w["id"], "category": "wall",
            "name": f"{axis_label} {_round(w['thickness'])}mm",
            "region": w.get("region"),
            "geometry": wall_geom,
            "attributes": {"thickness_mm": _round(w["thickness"]), "length_mm": _round(w["length"]),
                           "orientation": {"H": "horizontal", "V": "vertical",
                                           "A": "arc"}.get(w["orient"], "oblique"),
                           "caps": w["caps_n"], "openings": opening_attrs},
            "source_entities": list(w["srcs"]) + opening_srcs,
            "evidence": {"rule": (
                ("弧形幕墙：三层同心弧等距归并，取内外两条为面" if arc_curve
                 else "幕墙：共面三线等距归并成一条带，取内外两条为面")
                if w.get("role") == "curtain" else
                ("墙图层同心圆弧配对：同圆心 + 半径差=墙厚 + 角度区间重叠"
                 if arc_curve else
                 "WALL 图层双线墙：平行面线配对 + 端部封口校验 + 跨门窗洞口连续")),
                         "detail": detail},
            "confidence": conf, "status": "confirmed" if conf >= 0.85 else "uncertain"})
        if load["plan"].get("unlayered"):
            components[-1]["needs_review"] = True
            components[-1]["attributes"]["routing_mode"] = "unlayered_geometry"
            components[-1]["evidence"]["rule"] = (
                "单图层几何候选：双线/端头/拓扑证据；无图层独立支持，须复核")
        if w.get("role") == "curtain":
            # 幕墙算墙的一种：类别仍是"墙"，但把"这是外围护玻璃带"写清楚，
            # 算量时能一眼分出来（幕墙按 m² 玻璃、砌体墙按 m³/m²，口径不同）。
            components[-1]["attributes"]["role"] = "curtain"
            components[-1]["attributes"]["band_lines"] = w.get("band_lines")
            components[-1]["attributes"]["band_pitch_mm"] = _round(w["band_pitch"]) \
                if w.get("band_pitch") else None
            components[-1]["attributes"]["curtain_share"] = w.get("curtain_share")
        if w.get("evidence_state"):
            components[-1]["attributes"]["evidence_state"] = w["evidence_state"]
        if w.get("verify"):
            components[-1]["attributes"]["verify"] = w["verify"]
        components[-1]["attributes"].update(
            {k: w[k] for k in ("geom_score", "topo_score", "score", "recall_source")
             if w.get(k) is not None})

    # ---------------- 门
    for cid, d in _door_entries(doors):
        wall, wd = nearest_wall(wall_runs, d["opening"] if d.get("opening") else [d["insert"], d["insert"]])
        comp = {
            "id": cid, "category": "door",
            "name": f"{d['subtype']} {d['attr'] or ''}".strip(),
            "geometry": {"type": "opening", "insert": [_round(d["insert"][0]), _round(d["insert"][1])],
                         "width": _round(d["width"]),
                         "opening": _pts(d.get("opening", [d["insert"], d["insert"]])),
                         "hinges": _pts(d.get("hinges", [])),
                         "rotation": d["rotation"]},
            "attributes": {"block": d["block"], "type_code": d["attr"],
                           "width_mm": _round(d["width"]),
                           "height_mm": d.get("code_height"),
                           "swing": d.get("swing"),
                           "wall_id": wall["id"] if wall else None,
                           "wall_distance": _round(wd) if wall else None},
            "source_entities": _opening_source_entities(d, "门候选")
                                + ([{"handle": d["label_handle"], "type": "TEXT", "layer": "PUB_TEXT",
                                     "role": "门编号"}] if d.get("label_handle") else []),
            "evidence": {"rule": "门块参照（$TCHSYS$DOOR2D/$DorLib2D/$APDOOR2D）+ 块属性 A + 开启弧线几何",
                         "detail": f"属性 {d['attr']}；开启弧半径 {d['width']:.0f}mm；块插入点落在墙洞口"},
            "confidence": 0.7 if d.get("_pending") else (0.95 if d["attr"] else 0.8),
            "status": "uncertain" if d.get("_pending") else "confirmed"}
        if d.get("judged_role"):
            comp["evidence"] = {
                "rule": "洞口语义裁决（模型判定为门）+ 墙线缺口几何复算",
                "detail": f"{d['judge']['case_kind']} 候选，宽 {_round(d['width'])}mm；"
                          f"两侧面域 {d['judge'].get('faces_both_sides')}；"
                          f"附近弧线 {d['judge'].get('arc_nearby')}；"
                          f"模型理由：{d['judge'].get('reason') or '—'}"}
            comp["confidence"] = 0.8
            comp["status"] = "uncertain"
        if d.get("no_leaf"):
            comp["attributes"]["note"] = "模型判为无门扇洞口（洞口计入，门扇不计）"
        if d.get("_pending"):
            comp["attributes"]["note"] = "可信门块但墙体归属未确认，按待核输出"
        if d["layer"] == "LVTRY":
            comp["attributes"]["note"] = "位于 LVTRY（卫生间隔断）图层，为隔断门"
        components.append(comp)

    # ---------------- 窗
    for cid, w in _window_entries(windows):
        wall, wd = nearest_wall(wall_runs, w["axis"])
        comp = {
            "id": cid, "category": "window",
            "name": f"窗 {w['attr'] or ''}".strip(),
            "geometry": {"type": "opening", "center": [_round(w["center"][0]), _round(w["center"][1])],
                         "axis": _pts(w["axis"]), "width": _round(w["width"]),
                         "thickness": _round(w["thickness"]), "rotation": w["rotation"]},
            "attributes": {"block": w["block"], "type_code": w["attr"], "width_mm": _round(w["width"]),
                           "wall_thickness_mm": _round(w["thickness"]),
                           "wall_id": wall["id"] if wall else None,
                           "wall_distance": _round(wd) if wall else None,
                           "label": w.get("label")},
            "source_entities": _opening_source_entities(w, "窗候选")
                                + ([{"handle": w["label_handle"], "type": "TEXT", "layer": "WINDOW_TEXT",
                                     "role": "窗编号"}] if w.get("label_handle") else []),
            "evidence": {"rule": "窗块 $TCHSYS$WIN2D + 块属性 A + 与墙中心线共线",
                         "detail": f"属性 {w['attr']}；xscale={w['width']:.0f} 即窗宽；"
                                   f"yscale={w['thickness']:.0f} 与墙厚一致"},
            "confidence": 0.6 if w.get("_pending") else (0.95 if w["attr"] else 0.8),
            "status": "uncertain" if w.get("_pending") else "confirmed"}
        if w.get("block_evidence"):
            comp["evidence"]["block_evidence"] = w["block_evidence"]
        if w.get("_pending"):
            comp["needs_review"] = True
            comp["attributes"]["note"] = (
                "窗块自身几何可信，但墙体宿主尚未确认；按待核输出")
        if w.get("judged_role"):
            comp["evidence"] = {
                "rule": "洞口语义裁决（模型判定为窗）+ 墙线缺口几何复算",
                "detail": f"{w['judge']['case_kind']} 候选，宽 {_round(w['width'])}mm；"
                          f"两侧面域 {w['judge'].get('faces_both_sides')}；"
                          f"模型理由：{w['judge'].get('reason') or '—'}"}
            comp["confidence"] = 0.8
            comp["status"] = "uncertain"
        components.append(comp)

    # ---------------- 房间
    for k, r in enumerate(sorted(room_recs, key=lambda r: (-r["label"]["y"], r["label"]["x"])), 1):
        if not r.get("polygon"):
            continue
        lab = r["label"]
        alab = lab.get("area_label")
        area_m2 = r["area"] / 1e6
        diff = abs(area_m2 - alab) / alab if alab else None
        kind = r.get("kind") or "enclosed"
        merged_parts = int(r.get("part_count") or 1)
        # 两个维度分开算：几何可信度 vs 标注一致性。
        # 以前缺面积文字就直接 0.5、永远进不了 confirmed——把"没有标注"当成了
        # "几何不可靠"，几何闭合得再好也无法确认。
        # A room closed only by speculative/virtual edges stays uncertain even
        # when its label lies inside the mathematically closed polygon.
        boundary_state = r.get("boundary_state") or "unknown"
        geo_ok = (not r["leak"]) and bool(r.get("seed_inside")) \
            and kind == "enclosed" and merged_parts == 1 \
            and boundary_state == "observed" and not r.get("needs_review")
        geo_conf = 0.95 if geo_ok else (0.6 if (not r["leak"] and r.get("seed_inside")) else 0.45)
        if diff is None:
            label_state, label_conf = "no_area_label", None
        elif diff < 0.05:
            label_state, label_conf = "consistent", 0.95
        elif diff < 0.15:
            label_state, label_conf = "minor_deviation", 0.8
        else:
            label_state, label_conf = "conflict", 0.5
        conf = geo_conf if label_conf is None else round(0.5 * (geo_conf + label_conf), 3)
        status = ("confirmed" if (geo_ok and label_state != "conflict") else "uncertain")
        comp = {
            "id": f"ROOM-{k:02d}", "category": "room", "name": lab["name"],
            "geometry": {"type": "polygon", "outline": _pts(r["polygon"]),
                         "bbox": [_round(v) for v in bbox_of_points(r["polygon"])],
                         "area_m2": _round(area_m2, 2)},
            "attributes": {"room_name": lab["name"], "area_label_m2": alab,
                           "area_label_state": label_state,
                           "geometry_confirmed": geo_ok,
                           "boundary_state": boundary_state,
                           "source_face_ids": r.get("source_face_ids") or [],
                           "boundary_review_required": bool(r.get("needs_review")
                                                            or boundary_state != "observed"),
                           "area_geom_m2": _round(area_m2, 2),
                           "area_deviation": _round(diff, 4) if diff is not None else None,
                           "leak_to_outside": r["leak"],
                           "label_inside_outline": r.get("seed_inside"),
                           "kind": kind, "part_count": merged_parts,
                           "boundary": r.get("boundary") or "walls",
                           "area_source": r.get("area_source") or "grid_contour",
                           "note": r.get("note")},
            "source_entities": [{"handle": lab["handle"], "type": "TEXT", "layer": lab["layer"],
                                 "role": "房间名"}]
                                + ([{"handle": lab["area_handle"], "type": "TEXT", "layer": "SPACE",
                                     "role": "面积标注"}] if lab.get("area_handle") else [])
                                + ([{"handle": lab["area_sup_handle"], "type": "TEXT", "layer": "SPACE",
                                     "role": "面积标注上标 2"}] if lab.get("area_sup_handle") else []),
            "evidence": {"rule": "房间名文字作种子 + 墙/柱/洞口围合做障碍的连通域 + 边界追踪",
                         "detail": f"栅格 10mm；面积标注 {alab} m²，几何 {area_m2:.2f} m²"},
            "confidence": conf, "status": status}
        if area_m2 < 1.0:
            comp["attributes"]["area_warning"] = (
                "房间几何面积不足 1 m²，疑似边界截断或仅识别到局部；需人工复核")
            comp["needs_review"] = True
            comp["status"] = "uncertain"
        if diff is not None and diff > 0.15:
            comp["attributes"]["deviation_reason"] = (
                "该房间被 LVTRY 图层卫生间隔断块细分，或与其他标注房间共用同一连通域，"
                "几何面积不可靠，按 uncertain 处理")
        components.append(comp)

    # ---------------- 楼梯
    for k, s in enumerate(stairs, 1):
        bb = s["bbox"]
        components.append({
            "id": f"STAIR-{k:02d}", "category": "stair",
            "name": s.get("label") or f"楼梯 {k}",
            "geometry": {"type": "polygon", "bbox": [_round(v) for v in bb],
                         "outline": [[_round(bb[0]), _round(bb[1])], [_round(bb[2]), _round(bb[1])],
                                     [_round(bb[2]), _round(bb[3])], [_round(bb[0]), _round(bb[3])]],
                         "width": _round(bb[2] - bb[0]), "depth": _round(bb[3] - bb[1])},
            "attributes": {"tread_count": s["tread_count"],
                           "tread_spacing_mm": _round(s["tread_spacing"]) if s["tread_spacing"] else None,
                           "up_label": s["up_label"],
                           "direction": "向上（'上' 字标注于梯段下方）" if s["up_label"] else "未标注",
                           # 梯段宽 = 踏面线的长度，不是外接框的某一侧：踏步竖着画时
                           # 外接框宽度是梯段长度方向，直接取宽度会差一个量级。
                           "flight_width_mm": _round(s.get("tread_width") or (bb[2] - bb[0]))},
            "source_entities": s["srcs"] + ([{"handle": s["label_handle"], "type": "TEXT", "layer": "PUB_TEXT",
                                              "role": "楼梯编号"}] if s.get("label_handle") else []),
            "evidence": {"rule": "踏步串：平行 + 等距 + 至少 4 道，外接框只取踏步本身",
                         "detail": f"踏步线 {s['tread_count']} 道，间距 {s['tread_spacing'] or 0:.0f}mm"},
            "confidence": 0.9 if s.get("label") else 0.7,
            "status": "confirmed" if s.get("label") else "uncertain"})

    # ---------------- 台阶
    for k, s in enumerate(steps, 1):
        bb = s["bbox"]
        components.append({
            "id": f"STEP-{k:02d}", "category": "step",
            "name": f"台阶（{s['label'] or '未标注'}）",
            "geometry": {"type": "polygon", "bbox": [_round(v) for v in bb],
                         "outline": [[_round(bb[0]), _round(bb[1])], [_round(bb[2]), _round(bb[1])],
                                     [_round(bb[2]), _round(bb[3])], [_round(bb[0]), _round(bb[3])]],
                         "width": _round(bb[2] - bb[0]), "depth": _round(bb[3] - bb[1])},
            "attributes": {"steps": s["levels"], "tread_depths_mm": s["tread_depths"],
                           "label": s["label"]},
            "source_entities": s["srcs"] + ([{"handle": s["label_handle"], "type": "TEXT", "layer": "PUB_TEXT",
                                              "role": "做法标注"}] if s.get("label_handle") else []),
            "evidence": {"rule": "散水图层上的嵌套 U 形轮廓（同中心、逐级内缩 ~300mm）",
                         "detail": f"{s['levels']} 级轮廓 -> {s['levels']} 级踏步"},
            "confidence": 0.75 if s.get("entrance") else (0.85 if s["label"] else 0.6),
            "status": "uncertain" if s.get("entrance") else
                      ("confirmed" if s["label"] else "uncertain")})

    # ---------------- 散水
    if apron:
        bb = apron["bbox"]
        components.append({
            "id": "APRON-01", "category": "apron", "name": "建筑散水",
            "geometry": {"type": "ring", "bbox": [_round(v) for v in bb],
                         "outline": [[_round(bb[0]), _round(bb[1])], [_round(bb[2]), _round(bb[1])],
                                     [_round(bb[2]), _round(bb[3])], [_round(bb[0]), _round(bb[3])]],
                         "segments": [{"p1": _pts([s["p1"]])[0], "p2": _pts([s["p2"]])[0]}
                                      for s in apron["segments"]]},
            "attributes": {"width_mm": _round(apron["width"]) if apron["width"] else None,
                           "width_candidates": apron["width_candidates"],
                           "segment_count": len(apron["segments"])},
            "source_entities": [{"handle": s["handle"], "type": s["type"], "layer": "散水"}
                                for s in apron["segments"]],
            "evidence": {"rule": "散水图层轮廓线 + 到外墙面的法向距离",
                         "detail": f"外边线到外墙面距离众数 {apron['width']}mm"},
            "confidence": 0.85, "status": "confirmed"})

    # ---------------- 组装后过滤（详图图层 / 重复实例 / 超宽洞口标记）
    components, filtered_out = _filter_components(components, load.get("scale"))

    # ---------------- 未识别图元
    used = set()
    for c in components:
        for s in c["source_entities"]:
            if s.get("handle"):
                used.add(s["handle"])
    unrecognized = []
    for e in msp:
        h = e.dxf.handle
        if h in used:
            continue
        t = e.dxftype()
        lay = e.dxf.layer
        if t in ("DIMENSION",):
            reason = "尺寸标注：用于校核轴距/构件尺寸，本身不作为构件实例"
        elif lay in ("DIM_LEAD", "DIM_SYMB", "DIM_ELEV", "DIM_IDEN"):
            reason = "引线/标高/索引符号：注释图元，非构件"
        elif lay in ("PUB_TEXT", "WINDOW_TEXT", "DOOR_FIRE_TEXT"):
            reason = "说明文字：已作为构件属性/证据引用或为构造做法说明"
        elif lay in ("LVTRY", "01-平面-活动家具", "EQUIP_灭火器", "EQUIP_地漏", "EQUIP_消火栓"):
            reason = "设备/家具/卫生间隔断：不在本次 11 类识别目标内"
        elif lay in ("空调", "DUCT-排风风口", "DUCT-排风管设备", "通风道", "@防烟", "屋面", "屋面排水线"):
            reason = "暖通/屋面附件：不在识别目标内"
        elif lay == "WINDOW" and t == "LINE":
            reason = "窗框线（WINDOW 图层 LINE），已由窗块实例代表"
        elif lay in ("Hole(JZ留洞)", "建筑洞口"):
            reason = "结构留洞/建筑洞口：已作为洞口参考，未单列构件"
        elif lay == "WALL":
            reason = "墙体图元：未配对成墙（贴墙窗套/线脚、单线或超短段）"
        elif lay == "散水":
            reason = "散水/台阶附属线：已归入散水或台阶轮廓"
        elif lay == "0":
            reason = "0 图层杂项（图签文字/无意义块参照/图面辅助线）"
        else:
            reason = f"未纳入规则的图层 {lay}"
        bb = None
        try:
            from ezdxf import bbox as bbmod
            b = bbmod.extents([e])
            if b.has_data:
                bb = [_round(b.extmin.x), _round(b.extmin.y), _round(b.extmax.x), _round(b.extmax.y)]
        except Exception:
            pass
        unrecognized.append({"handle": h, "type": t, "layer": lay, "bbox": bb, "reason": reason})

    # 墙体未采纳项（三态候选：未配对 / 弱证据），保留在 unrecognized 里可追溯
    for w in wdropped:
        srcs = (w.get("face1") or {}).get("srcs") or w.get("srcs") or ()
        first = srcs[0] if srcs else {}
        handle = first.get("handle") or w.get("handle")
        if not handle:
            continue
        why = w.get("drop_reason") or w.get("why") or "未配对"
        thick = float(w.get("thickness") or 0.0)
        pos = w.get("center")
        pos_txt = f"，位置 {pos:.0f}" if isinstance(pos, (int, float)) else ""
        unrecognized.append({
            "handle": handle, "type": first.get("type") or "LINE",
            "layer": first.get("layer") or "WALL", "bbox": w.get("bbox"),
            "reason": f"墙候选未采纳：{why}（厚度 {thick:.0f}mm{pos_txt}）"})

    for w in pending_walls:
        unrecognized.append({
            "handle": ((w.get("srcs") or [{}])[0] or {}).get("handle"),
            "type": "LINE",
            "layer": ((w.get("srcs") or [{}])[0] or {}).get("layer") or "WALL",
            "bbox": w.get("bbox"),
            "reason": ("墙候选未采纳：靠端头/柱连接保留，缺独立几何证据"
                       f"（几何分 {float(w.get('geom_score') or 0):.2f} / "
                       f"拓扑分 {float(w.get('topo_score') or 0):.2f}），待人工复核")})
    for c in pending_cols:
        unrecognized.append({
            "handle": (c.get("srcs") or [{}])[0].get("handle"),
            "type": "CONTOUR", "layer": c.get("layer") or "COLUMN",
            "bbox": c.get("bbox"),
            "reason": (f"柱候选未采纳：证据不足（得分 {float(c.get('score') or 0):.2f}，"
                       f"画法 {'+'.join(c.get('methods') or [])}），待人工复核")})

    meta = {
        "source": os.path.basename(ctx.dxf_path),
        "dxf_version": doc.dxfversion,
        "insunits_header": doc.header.get("$INSUNITS"),
        "units": units.get("units") or "mm",
        "units_resolved": bool(units.get("resolved")),
        "units_scale": units.get("scale"),
        "units_evidence": "; ".join(units.get("notes") or [])
                          or "$INSUNITS 与几何证据一致",
        "unit_candidates": units.get("candidates") or [],
        "extents": _drawing_extents(doc, load.get("snapshot")),
        "method": _method_note(inputs),
        "grid": {"v_spans": grid["v_spans"], "h_spans": grid["h_spans"],
                 "v_labels": [a["label"] for a in grid["vaxes"]],
                 "h_labels": [a["label"] for a in grid["haxes"]]},
        "wall_diagnostics": {k: (v if not isinstance(v, list) else [_round(x) for x in v])
                             for k, v in wdiag.items()},
        "counts": dict(collections.Counter(c["category"] for c in components)),
        "filtered": {
            "n": len(filtered_out),
            "by_reason": dict(collections.Counter(
                (c.get("drop_reason") or "").split("（")[0] for c in filtered_out)),
            "items": [{"id": c.get("id"), "category": c.get("category"),
                       "layer": c.get("layer"), "reason": c.get("drop_reason")}
                      for c in filtered_out[:50]]},
        "wide_openings": [c.get("id") for c in components
                          if (c.get("attributes") or {}).get("wide_opening")],
        "unrecognized_count": len(unrecognized),
        "spaces": spaces,
        "openings": {
            "funnel": open_report.get("funnel"),
            "space_evidence": open_report.get("space_evidence"),
            "pending": open_report.get("pending"),
            "rejected": open_report.get("rejected"),
            "spaces_available": open_report.get("spaces_available"),
        },
        "regions": load.get("regions") or [],
        "scope": load.get("scope") or {},
        "undecided": (inputs.get("final_candidate_state") or {}).get("undecided") or [],
        "pending": {"walls": len(pending_walls), "columns": len(pending_cols)},
        "calibration": (inputs.get("final_candidate_state") or {}).get("calibration") or {},
        "wall_validation": {
            "stage_a": (wdiag or {}).get("stage_a"),
            "stage_b": (wdiag or {}).get("stage_b"),
            "recall_added": (wdiag or {}).get("recall_added"),
            "pending": (wdiag or {}).get("pending"),
            "candidates": (wdiag or {}).get("candidates")},
        "snapshot": {"entities": len(load["snapshot"].get("entities", ())),
                     "derived": len(load["snapshot"].get("derived", ()))},
    }

    # 自适应链路的结论只进 meta（审计/诊断），默认不改动导出构件
    meta["audit"] = inputs.get("final_audit") or {}
    return {"meta": meta, "components": components, "unrecognized": unrecognized}


def node_export(ctx):
    """MAIN：包装 ``export.build_document``，并把自适应链路的审计摘要挂到文档上。"""
    from .export import build_document
    doc = build_document(ctx.inputs["assemble"], ctx.dxf_path,
                         basemap_file=ctx.params.get("basemap_file"))
    audit = (ctx.inputs.get("final_audit") or {})
    if isinstance(doc, dict) and audit:
        doc["diagnostics"] = audit
    # "这次到底有没有用大模型"要能一眼查到：key 有没有、端点在哪、模型是哪个。
    # 没配 key 时这里是 {"enabled": false}，结果就是纯几何——报告里不许含糊。
    if isinstance(doc, dict):
        from . import llm_credentials as CRED
        doc.setdefault("meta", {})["llm"] = CRED.describe()
        # AI 增强：模式、预算、提议、实验、记忆库，一个字段都不含糊
        ctrl = (ctx.inputs.get("adaptive_controller") or {})
        ai_node = (ctx.inputs.get("ai_hypotheses") or {})
        if ctrl.get("ai_mode") and ctrl.get("ai_mode") != "off":
            doc["meta"]["ai_recognition"] = {
                "schema": "ai-recognition/1.0",
                "mode": ctrl.get("ai_mode"),
                "config": ai_node.get("config") or {},
                "gateway": ai_node.get("gateway") or {},
                "patterns": ai_node.get("pattern_summary") or {},
                "vision": ai_node.get("vision") or {},
                "proposals": ai_node.get("proposals") or {},
                "plans_compiled": ai_node.get("plans_compiled") or 0,
                "experiments": ctrl.get("ai") or {},
            }
        else:
            doc["meta"]["ai_recognition"] = {"schema": "ai-recognition/1.0",
                                             "mode": ctrl.get("ai_mode") or "off"}
    # 语义覆盖缓存只在 MAIN 节点落盘（PURE 节点不写文件），且原子替换
    staged = (ctx.inputs.get("adaptive_controller") or {}).get("layer_role_staged") or []
    if staged:
        LROLES.commit_staged(staged)
    return doc


def node_pattern_discovery(ctx):
    """PURE：残余图元发现 —— 把"没解释的几何"整理成 PatternCase 队列。

    这一步没有模型、不改任何构件：只做聚类与特征计算。它的输出在
    ``ai_mode=off`` 时只是审计材料（谁没被解释、重复了几次），不影响识别结果。
    """
    from . import pattern_discovery as PD
    i = ctx.inputs
    ai = dict((ctx.params or {}).get("ai_config") or {})
    mode = str((ctx.params or {}).get("ai_mode") or ai.get("mode") or "off")
    limit = None
    render = None
    if mode != "off":
        limit = int((ai.get("budget") or {}).get("max_regions") or 12)
        if bool((ai.get("provider") or {}).get("vision", True)):
            # 视觉链路：本地把整块 ROI 画成小图（内存里，不落盘、不联网）
            from . import vision as VISION
            scale = (i.get("load") or {}).get("scale") or {}
            render = VISION.case_renderer(
                i["load"]["snapshot"],
                walls=(i.get("walls_seg") or {}).get("walls_seg") or (),
                thickness=float(scale.get("thickness") or 0.0),
                max_images=int((ai.get("budget") or {}).get(
                    "max_diagnostic_images") or VISION.RENDER["max_images"]))
    out = PD.discover(snapshot=i["load"]["snapshot"],
                      ledger=(i.get("defect_detect") or {}).get("ledger") or {},
                      walls_seg=(i.get("walls_seg") or {}).get("walls_seg") or (),
                      columns=i.get("columns") or (),
                      doors=(i.get("openings_select") or {}).get("doors") or (),
                      windows=(i.get("openings_select") or {}).get("windows") or (),
                      spaces=i.get("spaces_seed") or {},
                      scale=(i.get("load") or {}).get("scale") or {},
                      render=render, limit=limit)
    if render is not None:
        from . import vision as VISION
        out["vision"] = VISION.summary(out.get("cases"))
    return out


def _controller_state(i):
    """候选状态的初始快照（与 adaptive_controller 节点用的是同一份口径）。"""
    sel = i.get("openings_select") or {}
    return {"walls": list((i.get("walls_seg") or {}).get("walls_seg") or ()),
            "columns": list(i.get("columns") or ()),
            "doors": list(sel.get("doors") or ()),
            "windows": list(sel.get("windows") or ()),
            "faces": list((i.get("spaces_seed") or {}).get("faces") or ()),
            "inferred_edges": [], "unresolved": []}


def node_ai_hypotheses(ctx):
    """含模型调用：为未识别形态产出**假设 + 计划草案**（不执行、不提交）。

    ``ai_mode`` 为 ``off`` 时直接返回空草案（一个请求都不发）。``propose`` 模式
    到此为止：结果只进 ``meta.ai_recognition``，绝不合并进候选状态。
    """
    from . import ai_config as AIC
    from . import ai_planner as PLANNER
    from . import model_gateway as GATEWAY
    from . import pattern_discovery as PD

    i = ctx.inputs
    cfg = (ctx.params or {}).get("ai_config") or AIC.load()
    mode = str((ctx.params or {}).get("ai_mode") or cfg.get("mode") or "off")
    cases = list((i.get("quality_gate") or {}).get("patterns") or ())
    if mode == "off":
        return {"schema": PLANNER.SCHEMA, "status": "off", "mode": "off",
                "proposals": {}, "plans": {}, "cases": len(cases),
                "gateway": {"available": False, "calls": 0,
                            "images_sent": 0, "images_dropped": 0},
                "config": AIC.summary(cfg)}
    gateway = GATEWAY.gateway_from_config(cfg)
    budget = dict(cfg.get("budget") or {})
    max_cases = int(budget.get("max_regions") or 12)
    proposals: dict = {}
    plans: dict = {}
    for case in cases[:max_cases]:
        prop = PLANNER.draft(case, gateway=gateway, budget=budget,
                             tools=ADAPT._readonly_tools(i.get("evidence_graph") or {},
                                                         _controller_state(i)))
        proposals[str(case.get("id"))] = PLANNER.summary(prop)
        compiled = ADAPT.plans_from_proposal(case, prop,
                                            index=i.get("evidence_graph") or {},
                                            ledger=(i.get("defect_detect")
                                                    or {}).get("ledger") or {},
                                            budget=budget)
        plans[str(case.get("id"))] = [p.as_dict() for p in compiled
                                      if p.ok and not p.empty]
    compiled_count = sum(len(v) for v in plans.values())
    return {"schema": PLANNER.SCHEMA,
            "status": "ok" if gateway.available else "unavailable",
            "mode": mode, "proposals": proposals, "plans": plans,
            "cases": len(cases), "plans_compiled": compiled_count,
            "pattern_summary": PD.summary(i.get("pattern_discovery") or {}),
            "gateway": gateway.describe(), "config": AIC.summary(cfg),
            "vision": dict((i.get("pattern_discovery") or {}).get("vision") or {},
                           images_sent=int((gateway.describe() or {}).get("images_sent")
                                           or 0)),
            "operator_catalog": OPSPEC.summary()}


# --------------------------------------------------------------------------- 注册
def node_raw_evidence(ctx):
    """PURE：建证据账本（每个图元一条，永不丢弃）。"""
    return RAV.build(ctx.inputs["load"]["snapshot"])


def node_defect_detect(ctx):
    """PURE：图纸缺陷诊断 + 在账本上登记利用状态。"""
    i = ctx.inputs
    snap = i["load"]["snapshot"]
    seg = i["walls_seg"]
    ledger = RAV.copy(i["raw_evidence"])
    for w in seg["walls_seg"]:
        for src in (w.get("srcs") or ()):
            RAV.mark(ledger, src.get("handle"), RAV.USED, role="wall",
                     candidate=w.get("id") or "wall")
    for u in seg.get("unpaired") or ():
        RAV.mark_many(ledger, u.get("handles") or (), RAV.WEAK,
                      role="unpaired_face", reason=u.get("why"))
    prims = PRIM.extract(snap)
    RAV.annotate_prims(ledger, prims)
    cases = DEF.detect(snapshot=snap, prims=prims, ledger=ledger,
                       walls_seg=seg["walls_seg"], unpaired=seg.get("unpaired") or (),
                       spaces=i["spaces_seed"],
                       room_labels=(i["room_labels_seed"] or {}).get("labels") or (),
                       openings=(i["openings_select"].get("doors") or [])
                                + (i["openings_select"].get("windows") or []),
                       columns=i["columns"], plan=i["load"]["plan"])
    return {"defects": cases, "ledger": ledger,
            "counts": {t: sum(1 for c in cases if c["type"] == t)
                       for t in sorted({c["type"] for c in cases})}}


def node_repair_options(ctx):
    """PURE：为缺陷生成确定性修复候选（虚拟边坐标由算法算，LLM 只能选 option）。"""
    walls_by_id = {(w.get("id") or ""): w for w in ctx.inputs["walls_seg"]["walls_seg"]}
    out = []
    for d in ctx.inputs["defect_detect"].get("defects") or ():
        if d["type"] == "BROKEN_WALL":
            r = REP.try_virtual_boundary(defect=d, walls_by_id=walls_by_id)
            out.append({"defect_id": d["defect_id"], "option": "option:close_gap",
                        "result": r})
        elif d["type"] == "OPEN_CONTOUR":
            hatch = (d.get("detail") or {}).get("hatch") or {}
            out.append({"defect_id": d["defect_id"], "option": "option:rebuild_contour",
                        "result": REP.rebuild_contour(hatch=hatch)})
    return out


def node_room_labels_seed(ctx):
    """PURE：房间名候选（图层清单只排优先级，形态证据兜底）。"""
    plan = ctx.inputs["load"]["plan"]
    layer_list = list(plan["layers"]["room_name"] or ())
    return LAB.collect(ctx.inputs["texts"], layers=layer_list,
                       rank={name: i for i, name in enumerate(layer_list)})


def node_label_filter(ctx):
    """条件节点：只把"确定性拿不准"的房名交给模型；无 key / 无需则跳过。"""
    seed = ctx.inputs["room_labels_seed"] or {}
    split = LAB.prefilter(seed.get("labels") or ())
    keep, drop = split["room_candidates"], split["annotations"]
    # 所有候选房名一次性全部送模型判定（不再只送"短/带符号/缺字高"）
    uncertain = []
    for i, lab in enumerate(keep):
        lab["id"] = lab.get("handle") or f"L{i}"
        uncertain.append(dict(lab, in_face=True))
    if not uncertain:
        return {"status": "skipped", "labels": keep, "annotations": drop,
                "stats": {"candidates": len(keep), "to_llm": 0}}
    res = LLMLABEL.classify(uncertain, api_key=(ctx.params or {}).get("llm_api_key"))
    roles = res.get("roles") or {}
    final, extra = [], list(drop)
    for lab in keep:
        verdict = roles.get(str(lab.get("id")) ) or roles.get(str(lab.get("handle")))
        if verdict and verdict["role"] == "annotation":
            extra.append(dict(lab, role="annotation", reason="llm:" + verdict["reason"]))
            continue
        if verdict:
            lab["role"] = "room" if verdict["role"] == "room" else "uncertain"
            lab["llm_reason"] = verdict["reason"]
        final.append(lab)
    final, geom_stat = LAB.promote_with_faces(
        final, (ctx.inputs.get('spaces_seed') or {}).get('faces') or ())
    return {"status": res.get("status") or "ok", "labels": final,
            "annotations": extra,
            "stats": {"candidates": len(final), "to_llm": len(uncertain),
                      "geometry": geom_stat,
                      "llm": res.get("counts") or {}}}


def node_space_cases(ctx):
    """PURE：为"房名未闭合"的每一处生成 case，几何量由确定性算法给出。"""
    from .quality_gate import associate_labels
    i = ctx.inputs
    faces = (i["spaces_seed"] or {}).get("faces") or []
    labels = (i["label_filter"] or {}).get("labels") or ()
    defects = (i["defect_detect"] or {}).get("defects") or ()
    assoc = associate_labels(labels, faces)
    bound_names = [a["label"]["text"] for a in assoc if a["status"] == "bound"]

    cases = []
    for a in assoc:
        if a["status"] not in ("leak", "distant"):
            continue
        lab = a["label"]
        if lab.get("role") != "room":
            continue
        pt = (lab.get("x"), lab.get("y"))
        gaps, max_gap, collinear, dids = 0, 0.0, 0, []
        for d in defects:
            if d["type"] not in ("BROKEN_WALL", "ROOM_LEAK") or not d.get("region"):
                continue
            b = d["region"]
            dx = max(b[0] - pt[0], 0.0, pt[0] - b[2])
            dy = max(b[1] - pt[1], 0.0, pt[1] - b[3])
            if (dx * dx + dy * dy) ** 0.5 > 3000.0:
                continue
            gaps += 1
            max_gap = max(max_gap, float((d.get("detail") or {}).get("gap_mm") or 0.0))
            collinear += 1
            dids.append(d["defect_id"])
        cases.append({"case_id": f"sp_{len(cases) + 1:02d}",
                      "room_name": lab["text"],
                      "declared_area": lab.get("area"),
                      "label_in_face": a["status"] == "offset",
                      "gap_count": gaps, "max_gap_mm": round(max_gap, 1),
                      "collinear_supports": collinear,
                      "neighbour_names": bound_names[:3],
                      "defect_ids": dids[:4],
                      "label": lab, "status": a["status"]})
    return cases


def node_space_judge(ctx):
    """条件节点：把"未闭合房名"的语义裁决交给模型；无 key / 无 case 则跳过。"""
    cases = ctx.inputs["space_cases"] or ()
    if not cases:
        return {"status": "skipped", "verdicts": {}, "counts": {}}
    return SPACEJUDGE.judge(cases, api_key=(ctx.params or {}).get("llm_api_key"))


def node_space_routing(ctx):
    """PURE：把裁决回灌成确定性分支（不产出任何几何）。"""
    cases = {c["case_id"]: c for c in (ctx.inputs["space_cases"] or ())}
    verdicts = (ctx.inputs["space_judge"] or {}).get("verdicts") or {}
    open_labels, repair_cases, uncertain = [], [], []
    for cid, c in cases.items():
        v = verdicts.get(cid) or {"decision": "uncertain", "defect_ids": []}
        if v["decision"] == "open_space":
            open_labels.append(c["label"])
        elif v["decision"] == "try_repair":
            repair_cases.append({"case_id": cid, "room_name": c["room_name"],
                                 "label": c["label"],
                                 "defect_ids": v.get("defect_ids") or [],
                                 "reason": v.get("reason")})
        else:
            uncertain.append({"case_id": cid, "room_name": c["room_name"],
                              "why": v.get("reason") or "unjudged"})
    return {"open_labels": open_labels, "repair_cases": repair_cases,
            "uncertain": uncertain,
            "counts": {"open_space": len(open_labels),
                       "try_repair": len(repair_cases),
                       "uncertain": len(uncertain)}}


def node_evidence_graph(ctx):
    """PURE：统一证据索引（候选 / 原语 / 构件 / 面域 + 来源关系）。"""
    i = ctx.inputs
    return EVID.build(snapshot=i["load"]["snapshot"],
                      walls_seg=i["walls_seg"]["walls_seg"],
                      wall_model=i["wall_model"]["wall_model"],
                      spaces=i["spaces_seed"],
                      doors=i["doors"], windows=i["windows"], columns=i["columns"],
                      room_labels=(i["room_labels_seed"] or {}).get("labels") or (),
                      patterns=(i.get("pattern_discovery") or {}).get("cases") or ())


def node_quality_gate(ctx):
    """PURE：识别质量闸门（候选/空间/冲突/语义四维）-> 是否需要 LLM 分析。"""
    i = ctx.inputs
    return GATE.check(snapshot=i["load"]["snapshot"], plan=i["load"]["plan"],
                      walls_seg=i["walls_seg"]["walls_seg"],
                      wall_model=i["wall_model"]["wall_model"],
                      spaces=i["spaces_seed"], doors=i["doors"], windows=i["windows"],
                      columns=i["columns"],
                      room_labels=(i["label_filter"] or {}).get("labels") or (),
                      evidence=i["evidence_graph"],
                      openings_selected=i["openings_select"],
                      ledger=i["defect_detect"].get("ledger") or {},
                      defects=i["defect_detect"].get("defects") or (),
                      regions=i["load"].get("regions") or (),
                      patterns=(i.get("pattern_discovery") or {}).get("cases") or ())


def node_llm_reason(ctx):
    """条件节点：只在 quality_gate 判定需要时执行；否则 status=skipped。"""
    gate = ctx.inputs["quality_gate"]
    if not gate.get("need_llm"):
        return {"status": "skipped", "proposals": [], "cases": [],
                "prompt_version": REASON.PROMPT_VERSION}
    # 有 strategies 的 case 由 repair_planner 负责；这里只处理语义/冲突类
    cases = [c for c in (gate.get("cases") or ()) if not c.get("strategies")]
    if not cases:
        return {"status": "skipped", "proposals": [], "cases": [],
                "prompt_version": REASON.PROMPT_VERSION}
    return REASON.analyze(cases, index=ctx.inputs["evidence_graph"],
                          api_key=(ctx.params or {}).get("llm_api_key"))


def node_validate_proposal(ctx):
    """PURE：闭集校验 LLM 提议（越权/几何字段/未知算子一律拒绝）。"""
    gate = ctx.inputs["quality_gate"]
    llm = ctx.inputs["llm_reason"]
    index = ctx.inputs["evidence_graph"]
    if (llm.get("status") in ("skipped", "unavailable", "error")
            or not llm.get("proposals")):
        return {"accepted": [], "rejected": [],
                "status": llm.get("status") or "no_action"}
    by_case = {}
    for p in llm["proposals"]:
        by_case.setdefault(p.get("case_id"), []).append(p)
    accepted, rejected = [], []
    for case in gate.get("cases") or ():
        props = []
        for p in (by_case.get(case.get("case_id")) or ()):
            allow = set(VALID.ALLOWED_PROPOSAL_FIELDS) | set(
                VALID.CONDITIONAL_FIELDS.get(p.get("action"), set()))
            props.append({k: v for k, v in p.items() if k in allow})
        if not props:
            continue
        res = VALID.validate_case({"case_id": case["case_id"], "proposals": props},
                                  index=index, region=case.get("region"),
                                  max_rounds=(gate.get("budget") or {}).get("max_rounds", 2))
        accepted += res["accepted"]
        rejected += res["rejected"]
    return {"accepted": accepted, "rejected": rejected,
            "status": "ok" if accepted else "no_action"}


# --------------------------------------------------------------------------- 规划与执行
def _layer_role_hook(inputs):
    """语义重算回调：给了"图层 -> 角色"，几何由确定性识别重算，模型不碰坐标。"""
    snap = inputs["load"]["snapshot"]
    texts = inputs.get("texts") or []
    walls_seg = (inputs.get("walls_seg") or {}).get("walls_seg") or []

    def hook(roles_by_layer: dict) -> dict:
        clean = {}
        for layer, roles in (roles_by_layer or {}).items():
            ok, _why = LROLES.validate(layer, roles)
            if ok:
                clean[str(layer)] = list(roles)
        if not clean:
            return {}
        return EXEC.execute_layer_role(clean, snapshot=snap, texts=texts,
                                       walls_seg=walls_seg,
                                       columns=inputs.get("columns") or ())
    return hook


def node_repair_planner(ctx):
    """条件节点：把"可修 case"交给 Repair Planner 选策略（不是选坐标）。"""
    gate = ctx.inputs["quality_gate"]
    cases = [c for c in (gate.get("cases") or ()) if c.get("strategies")]
    cases = [c for c in cases if PLANNER.needs_llm(c)]
    if not cases:
        return {"status": "skipped", "proposals": {}, "counts": {"cases": 0}}
    ledger = (ctx.inputs.get("defect_detect") or {}).get("ledger") or {}
    out = PLANNER.analyze(cases, index=ctx.inputs["evidence_graph"], ledger=ledger,
                          api_key=(ctx.params or {}).get("llm_api_key"))
    out["counts"] = {"cases": len(cases),
                     "proposed": len(out.get("proposals") or {})}
    return out


def node_plan_compile(ctx):
    """PURE：把"策略提议 / 旧版单动作 / 确定性兜底"统一编译成局部计划。

    编译失败的计划不会往下走（``rejected`` 里留错误码），因此控制器只会看到
    "策略存在、目标存在、类型匹配、预算之内"的计划。
    """
    i = ctx.inputs
    gate = i["quality_gate"]
    index = i.get("evidence_graph") or {}
    ledger = (i.get("defect_detect") or {}).get("ledger") or {}
    budget = dict(gate.get("budget") or {})
    allowed = [c.get("region") for c in (gate.get("cases") or ()) if c.get("region")]
    plans: dict[str, list] = {}
    errors: list[dict] = []

    def _add(plan) :
        plans.setdefault(plan.case_id or "", []).append(plan)
        if not plan.ok:
            errors.append({"case_id": plan.case_id, "strategy": plan.strategy_id,
                           "source": plan.source, "errors": plan.errors})

    # 1) 策略级提议（Repair Planner）
    for cid, prop in ((i.get("repair_planner") or {}).get("proposals") or {}).items():
        _add(COMPILER.compile_plan(dict(prop, case_id=cid), index=index, ledger=ledger,
                                  budget=budget, allowed_regions=allowed, source="llm"))
    # 2) 旧版单动作提议（llm_reason -> validate_proposal）
    for act in (i.get("validate_proposal") or {}).get("accepted") or ():
        cid = str(act.get("case_id") or "")
        for plan in COMPILER.compile_actions([act], index=index, case_id=cid,
                                            ledger=ledger,
                                            allowed_regions=allowed,
                                            source="llm_action"):
            _add(plan)
    # 2.5) AI 增强通道编译好的计划（来自 ai_hypotheses，key 是 PatternCase id）
    for cid, lst in ((i.get("ai_hypotheses") or {}).get("plans") or {}).items():
        for data in lst:
            plan = COMPILER.plan_from_dict(data)
            if plan.case_id:
                plans.setdefault(plan.case_id, []).append(plan)
            else:
                errors.append({"case_id": cid, "strategy": plan.strategy_id,
                               "source": plan.source, "errors": plan.errors})

    # 3) 确定性兜底：模型没给出可编译策略时，按"先便宜后激进"编译策略
    auto = ADAPT.deterministic_plans(gate.get("cases"), index=index, ledger=ledger,
                                     budget=budget)
    for cid, lst in auto.items():
        if any(p.ok and not p.empty for p in (plans.get(cid) or ())):
            continue
        for plan in lst:
            _add(plan)

    allp = [p for lst in plans.values() for p in lst]
    return {"plans": {cid: [p.as_dict() for p in lst] for cid, lst in plans.items()},
            "summary": COMPILER.summary(allp), "errors": errors,
            "cases": {cid: len(lst) for cid, lst in plans.items()}}


def node_adaptive_controller(ctx):
    """PURE：LLM 规划 + 动态局部子 DAG 执行 + 影子验收 + 事务式提交。

    这是本次架构改造的控制层：先修什么、用哪个策略、要不要问模型、试几次、
    什么时候停，都在这里决定；几何与依赖顺序由算子库/编译器负责。
    """
    i = ctx.inputs
    sel = i["openings_select"]
    gate = i["quality_gate"]
    compiled = (i.get("plan_compile") or {}).get("plans") or {}
    plans = {cid: [COMPILER.plan_from_dict(d) for d in lst]
             for cid, lst in compiled.items()}
    state = {"walls": list(i["walls_seg"]["walls_seg"]),
             "columns": list(i.get("columns") or ()),
             "doors": list(sel.get("doors") or ()),
             "windows": list(sel.get("windows") or ()),
             "faces": list((i.get("spaces_seed") or {}).get("faces") or ()),
             "inferred_edges": [], "unresolved": []}
    result = ADAPT.run(
        state=state, index=i.get("evidence_graph") or {}, cases=gate.get("cases") or (),
        ledger=(i.get("defect_detect") or {}).get("ledger") or {},
        columns=i.get("columns") or (),
        partitions=i.get("partitions") or (),
        thick=(i.get("wall_model") or {}).get("thickness"),
        labels=(i.get("label_filter") or {}).get("labels") or (),
        repairs=i.get("repair_options") or (),
        plans=plans,
        hooks={"layer_role": _layer_role_hook(i)},
        budget={"max_ms": float(os.environ.get("DWG_CONTROLLER_MAX_MS", "20000")),
                "max_cases": int(os.environ.get("DWG_CONTROLLER_MAX_CASES", "12")),
                "max_runs": int(os.environ.get("DWG_CONTROLLER_MAX_RUNS", "24"))})
    # ---- AI 增强通道（``ai_mode=enhance``）：在老通道的结果状态上继续做局部试验
    ai_cfg = dict((ctx.params or {}).get("ai_config") or {})
    ai_mode = str((ctx.params or {}).get("ai_mode") or ai_cfg.get("mode") or "off")
    ai_node = i.get("ai_hypotheses") or {}
    ai_result = None
    if ai_mode == "enhance" and (gate.get("patterns") or ()):
        from . import model_gateway as GATEWAY
        compiled = {cid: [COMPILER.plan_from_dict(d) for d in lst]
                    for cid, lst in (ai_node.get("plans") or {}).items() if lst}
        base = result.get("state") or state
        ai_result = ADAPT.enhance(
            state=base, index=i.get("evidence_graph") or {},
            patterns=gate.get("patterns") or (),
            ledger=(i.get("defect_detect") or {}).get("ledger") or {},
            columns=i.get("columns") or (), partitions=i.get("partitions") or (),
            thick=(i.get("wall_model") or {}).get("thickness"),
            labels=(i.get("label_filter") or {}).get("labels") or (),
            repairs=i.get("repair_options") or (),
            hooks={"layer_role": _layer_role_hook(i)},
            gateway=GATEWAY.gateway_from_config(ai_cfg),
            ai=ai_cfg,
            memory_dir=str((ai_cfg.get("memory") or {}).get("directory") or
                           (ctx.params or {}).get("memory_dir") or ""),
            diagnostics_path=(ctx.params or {}).get("diagnostics_path"),
            budget={"max_ms": float(os.environ.get("DWG_CONTROLLER_MAX_MS", "20000"))},
            plans_by_case=compiled)
        result["ai"] = ADAPT.ai_audit(ai_result)
        result["ai_state"] = ai_result.get("state")
        if ai_result.get("status") == "applied":
            result["state"] = ai_result.get("state")
            result["patch"] = ADAPT._diff(state, ai_result.get("state") or base)
            # finalize_patch only commits controllers with status=applied.
            # Keep the net patch and status consistent when legacy did nothing.
            result["status"] = "applied"
            result["applied"] = list(result.get("applied") or ()) + list(
                ai_result.get("applied") or ())
            result["rejected"] = list(result.get("rejected") or ()) + list(
                ai_result.get("rejected") or ())
    result["ai_mode"] = ai_mode
    result["layer_role_staged"] = LROLES.stage(
        {r["layer"]: r["roles"] for r in result.get("layer_role_staged") or ()
         if r.get("roles")},
        accepted=bool(result.get("status") == "applied"),
        stats={"applied": len(result.get("applied") or ())},
        file_digest=ctx.file_digest,
        model=str((ctx.params or {}).get("llm_model") or os.environ.get("LLM_MODEL") or ""))
    return result


def node_finalize_patch(ctx):
    """Commit the controller's net ID-based delta (including updates/deletions)."""
    ctrl = ctx.inputs.get("adaptive_controller") or {}
    patch = ctrl.get("patch") or {}
    if ctrl.get("status") != "applied":
        return {"status": "skipped", "schema": "candidate-patch/2",
                "reason": ctrl.get("status") or "no_action"}
    has_changes = any((d.get("upsert") or d.get("remove_ids"))
                      for d in (patch.get("changes") or {}).values())
    if not has_changes and not patch.get("faces_changed"):
        return {"status": "skipped", "schema": "candidate-patch/2",
                "reason": "NO_GEOMETRY_CHANGE",
                "applied_cases": [a.get("case_id") for a in ctrl.get("applied") or ()]}
    # AI 通道的每个新增构件都要能一键追到"规则 / 案例 / 实验 / 参数"
    ai = (ctrl.get("ai") or {})
    ai_rows = {str(a.get("case_id")): a for a in (ai.get("applied") or ())}
    changes = {}
    for bucket, delta in (patch.get("changes") or {}).items():
        upserts = []
        for rec in delta.get("upsert") or ():
            if not isinstance(rec, dict):
                upserts.append(rec)
                continue
            rec = dict(rec)
            prov = dict(rec.get("provenance") or {})
            prov.setdefault("origin", "ai" if ai_rows else "deterministic")
            for row in ai_rows.values():
                if str(rec.get("case_id") or "") == str(row.get("case_id")) \
                        or rec.get("action") in (row.get("strategy") or ()):
                    prov.update({"case_id": row.get("case_id"),
                                 "experiment_id": row.get("experiment_id"),
                                 "rule_id": ",".join(row.get("strategy") or ()) or None,
                                 "parameter_hash": ADAPT_RUNNER.digest_params(
                                     row.get("params") or {}) if row.get("params") else None})
                    break
            if ai_rows:
                rec["provenance"] = prov
                if prov.get("origin") == "ai":
                    rec["source_handles"] = list(rec.get("handles") or
                                                 rec.get("source_handles") or ())
            upserts.append(rec)
        if upserts or delta.get("remove_ids"):
            changes[bucket] = {"upsert": upserts,
                               "remove_ids": list(delta.get("remove_ids") or ())}
    if changes:
        patch = dict(patch, changes=changes)
    inferred = list((ctrl.get("state") or {}).get("inferred_edges") or ())
    out = dict(patch, status="accepted", schema="candidate-patch/2",
               faces_inferred=bool(inferred and patch.get("faces_changed")),
               inferred_edges_used=len(inferred),
               applied_cases=[a.get("case_id") for a in (ctrl.get("applied") or ())])
    return out


def node_affected_recompute(ctx):
    """PURE：局部重算的**真实执行结果** + 仍未做的重跑计划。"""
    ctrl = ctx.inputs.get("adaptive_controller") or {}
    planned = RC.plan(ctx.inputs["quality_gate"].get("affected_nodes") or ())
    executed = list(ctrl.get("executed_nodes") or ())
    return {"executed": executed,
            "executed_stages": sorted({s for a in (ctrl.get("applied") or ())
                                       for s in (a.get("implicit") or ())}),
            "runs": ctrl.get("runs"),
            "pending": [n for n in (planned.get("nodes") or ()) if n not in executed],
            "nodes": list(planned.get("nodes") or []), "count": planned.get("count"),
            "cost_hint": planned.get("cost_hint"),
            "mode": "executed" if executed else "planned_only"}


def node_final_candidate_state(ctx):
    """PURE：全链路唯一结果状态（下游只读它；补丁由 finalize_patch 提供）。"""
    i = ctx.inputs
    sel = i["openings_select"] or {}
    state = FS.build(walls_seg=i["walls_seg"]["walls_seg"],
                     columns=i.get("columns_verify", {}).get("columns") or i["columns"],
                     doors=sel.get("doors") or (), windows=sel.get("windows") or (),
                     openings_select=sel, spaces=i["spaces_seed"],
                     patch=i.get("finalize_patch") or {})
    # 置信度校准 + 未决队列：证据方法不足/弱证据的最高只能 uncertain
    return FS.calibrate(state)


def node_repair_audit(ctx):
    """PURE：观察 / 推断 / 未决三分（算量隔离用）。"""
    fs = ctx.inputs["final_candidate_state"]
    inferred_edges = fs.get("virtual_edges") or []
    return {"schema": "repair-audit/1.1",
            "observed": {"walls": len([w for w in (fs.get("walls") or ())
                                       if w.get("geometry_state") != "inferred"]),
                         "openings": len(fs.get("doors") or ())
                                     + len(fs.get("windows") or ())},
            "derived": {"faces": len(fs.get("faces") or ())},
            "inferred": {"virtual_edges": len(inferred_edges),
                         "inferred_walls": len([w for w in (fs.get("walls") or ())
                                                if w.get("geometry_state") == "inferred"]),
                         "applied": bool(fs.get("inferred_applied"))},
            "undecided": len(fs.get("undecided") or ()),
            "undecided_items": (fs.get("undecided") or [])[:40],
            "calibration": fs.get("calibration") or {},
            "unresolved": fs.get("unresolved") or []}


def node_final_audit(ctx):
    """PURE：把闸门/规划/编译/执行/验收的全过程压成可审计摘要。"""
    gate = ctx.inputs["quality_gate"]
    planner = ctx.inputs.get("repair_planner") or {}
    compile_ = ctx.inputs.get("plan_compile") or {}
    ctrl = ctx.inputs.get("adaptive_controller") or {}
    val = ctx.inputs.get("validate_proposal") or {}
    llm = ctx.inputs.get("llm_reason") or {}
    return {"schema": "recognize-audit/2.0",
            "operators": OPSPEC.summary(),
            "gate": {"need_llm": gate.get("need_llm"),
                     "reason_codes": gate.get("reason_codes"),
                     "cases": len(gate.get("cases") or ()),
                     "groups": gate.get("groups"),
                     "budget": gate.get("budget"),
                     "stats": gate.get("stats")},
            "opening_judge": LINT.summary(ctx.inputs.get("opening_judge") or {}),
            "planner": PLANNER.summary(planner),
            "plan_compile": compile_.get("summary") or {},
            "controller": ADAPT.audit(ctrl),
            "llm": {"status": llm.get("status"), "model": llm.get("model"),
                    "prompt_version": llm.get("prompt_version"),
                    "proposals": len(llm.get("proposals") or ()),
                    "errors": llm.get("errors")},
            "validation": {"accepted": len(val.get("accepted") or ()),
                           "rejected": len(val.get("rejected") or ()),
                           "reasons": [r.get("reason") for r in (val.get("rejected") or ())][:8]},
            "recompute": ctx.inputs["affected_recompute"],
            "evidence": RAV.summary((ctx.inputs.get("defect_detect") or {}).get("ledger") or {}),
            "defects": (ctx.inputs.get("defect_detect") or {}).get("counts") or {},
            "repairs": ctx.inputs.get("repair_audit") or {},
            "space_routing": (ctx.inputs.get("space_routing") or {}).get("counts") or {},
            # AI 增强通道的过程摘要（``ai_mode=off`` 时是空壳，不假装跑过）
            "ai": ctrl.get("ai") or {"mode": ctrl.get("ai_mode") or "off"},
            "patterns": (ctx.inputs.get("pattern_discovery") or {}).get("stats") or {},
            "unresolved": (ctrl.get("state") or {}).get("unresolved") or []}


def build_recognition_dag() -> DAG:
    """构建 46 节点识别 DAG。

    外层 DAG 永远无环；中间是一段**动态局部子 DAG**（由 ``plan_compile`` 编译、
    ``adaptive_controller`` 执行）：

        raw_evidence -> defect_detect -> repair_options
        -> evidence_graph -> quality_gate
        -> repair_planner(条件) / llm_reason(条件) -> validate_proposal
        -> plan_compile(PURE) -> adaptive_controller(局部子 DAG + 影子验收)
        -> finalize_patch -> final_candidate_state -> walls_merge/rooms
        -> final_audit -> assemble -> export

    LLM 只决定"选哪个策略"，不决定几何、依赖顺序和是否提交；没有 key 或输出不
    合规时自动降级成确定性策略，整条链路仍然可跑通。
    """
    return DAG([
        Node("load", node_load, deps=(), kind="main", cacheable=False,
             summarize=summarize_load, params=("profile",)),
        Node("texts", node_texts, deps=("load",)),
        Node("walls_seg", node_walls_seg, deps=("load", "columns")),
        # 房间名种子：只读文字 + profile，不依赖最终墙网（避免与闸门形成环）
        Node("room_labels_seed", node_room_labels_seed, deps=("texts", "load")),
        Node("raw_evidence", node_raw_evidence, deps=("load",)),
        Node("columns", node_columns, deps=("load",)),
        # 柱两阶段：形态候选(code) -> 墙网拓扑复核，缺边候选必须有拓扑支持
        Node("columns_verify", node_columns_verify,
             deps=("columns", "walls_seg", "load")),
        Node("partitions", node_partitions, deps=("load",)),
        Node("grid", node_grid, deps=("load",)),
        Node("doors", node_doors, deps=("load", "texts", "walls_seg")),
        Node("windows", node_windows, deps=("load", "texts", "walls_seg")),
        Node("frame", node_frame, deps=("load", "texts")),
        Node("north", node_north, deps=("load", "texts")),
        Node("stairs", node_stairs, deps=("load", "texts")),
        # wall_model 用墙网精化图内尺度（墙厚/门宽），所以要多读 load 的初值
        Node("wall_model", node_wall_model, deps=("walls_seg", "load")),
        # 门候选在面域阶段就当"隔断"用：墙上的门洞不补，房间会经门洞连成一片。
        Node("spaces_seed", node_spaces_seed,
             deps=("load", "walls_seg", "columns", "partitions", "doors")),
        # 洞口候选的语义裁决：候选程序枚举，性质由模型判断（可跳过、可重放）
        Node("opening_judge", node_opening_judge, cacheable=False,
             deps=("load", "walls_seg", "wall_model", "spaces_seed", "doors",
                   "windows")),
        # 门窗候选先归并成**统一洞口**（一个洞只出现一次），再判性质
        Node("openings_unify", node_openings_unify,
             deps=("load", "doors", "windows", "walls_seg", "wall_model",
                   "opening_judge")),
        Node("openings_select", node_openings_select, cacheable=False,
             # load：模式 B 复核需要快照（截图底图 + 同族重复次数）
             deps=("load", "wall_model", "spaces_seed", "openings_unify",
                   "opening_judge", "doors", "windows")),
        # ---- 证据层：账本 / 缺陷 / 确定性修复候选 / 语义裁决
        Node("defect_detect", node_defect_detect,
             deps=("load", "raw_evidence", "walls_seg", "spaces_seed",
                   "room_labels_seed", "openings_select", "columns")),
        Node("repair_options", node_repair_options,
             deps=("defect_detect", "walls_seg")),
        Node("label_filter", node_label_filter, cacheable=False,
             deps=("room_labels_seed", "spaces_seed")),
        Node("space_cases", node_space_cases,
             deps=("spaces_seed", "label_filter", "defect_detect")),
        Node("space_judge", node_space_judge, cacheable=False, deps=("space_cases",)),
        Node("space_routing", node_space_routing,
             deps=("space_cases", "space_judge")),
        # 残余图元发现（无模型、不改构件）：给证据图与闸门提供"未识别形态"
        # params=("ai_mode",)：``off`` 与 ``enhance`` 的输出不同（后者带 ROI 小图），
        # 缓存键必须跟着模式走，否则改开关会拿到旧的、没有图的缓存
        Node("pattern_discovery", node_pattern_discovery,
             deps=("load", "defect_detect", "walls_seg", "columns",
                   "openings_select", "spaces_seed"),
             params=("ai_mode", "ai_config"),
             summarize=lambda out: {"cases": len(out.get("cases") or ()),
                                    "stats": out.get("stats") or {}}),
        Node("evidence_graph", node_evidence_graph,
             deps=("load", "walls_seg", "wall_model", "spaces_seed", "doors",
                   "windows", "columns", "room_labels_seed", "pattern_discovery")),
        Node("quality_gate", node_quality_gate, cacheable=False,
             deps=("load", "evidence_graph", "defect_detect", "repair_options",
                   "label_filter", "walls_seg", "wall_model", "spaces_seed",
                   "doors", "windows", "columns", "openings_select",
                   "pattern_discovery")),
        # ---- 规划：Repair Planner（条件）+ 旧版疑难分析（条件）+ 闭集校验
        Node("repair_planner", node_repair_planner, cacheable=False,
             deps=("quality_gate", "evidence_graph", "defect_detect")),
        Node("llm_reason", node_llm_reason, cacheable=False,
             deps=("quality_gate", "evidence_graph")),
        Node("validate_proposal", node_validate_proposal,
             deps=("quality_gate", "llm_reason", "evidence_graph")),
        # AI 假设：只在 ai_mode != off 时真正调用模型（PURE 但含外部调用 -> 不缓存）
        Node("ai_hypotheses", node_ai_hypotheses, cacheable=False,
             deps=("quality_gate", "evidence_graph", "pattern_discovery", "load",
                   "walls_seg", "columns", "openings_select", "spaces_seed",
                   "defect_detect")),
        Node("plan_compile", node_plan_compile,
             deps=("quality_gate", "repair_planner", "ai_hypotheses",
                   "validate_proposal", "evidence_graph", "defect_detect")),
        # ---- 控制层：动态局部子 DAG + 影子验收 + 事务式提交
        Node("adaptive_controller", node_adaptive_controller, cacheable=False,
             deps=("quality_gate", "plan_compile", "walls_seg", "columns",
                   "doors", "windows", "spaces_seed", "openings_select",
                   "partitions", "wall_model", "evidence_graph", "texts", "load",
                   "repair_options", "defect_detect", "label_filter",
                   "ai_hypotheses", "pattern_discovery")),
        Node("affected_recompute", node_affected_recompute,
             deps=("quality_gate", "adaptive_controller")),
        Node("finalize_patch", node_finalize_patch,
             deps=("adaptive_controller",)),
        Node("final_candidate_state", node_final_candidate_state, cacheable=False,
             deps=("finalize_patch", "openings_select", "walls_seg", "columns",
                   "columns_verify", "spaces_seed", "adaptive_controller")),
        Node("repair_audit", node_repair_audit, deps=("final_candidate_state",)),
        Node("final_audit", node_final_audit,
             deps=("quality_gate", "repair_planner", "plan_compile",
                   "adaptive_controller", "affected_recompute", "repair_audit",
                   "defect_detect", "space_routing")),
        Node("walls_merge", node_walls_merge,
             deps=("walls_seg", "wall_model", "doors", "windows", "openings_select",
                   "final_candidate_state"),
             summarize=summarize_walls_merge),
        Node("envelope", node_envelope, deps=("walls_merge", "load")),
        Node("steps", node_steps, deps=("load", "texts", "walls_merge")),
        Node("entrance_steps", node_entrance_steps, deps=("load", "walls_merge")),
        Node("apron", node_apron, deps=("load", "walls_merge")),
        Node("room_labels", node_room_labels, deps=("texts", "envelope", "load")),
        # 面域优先：房间几何来自墙网面域，房名只做挂靠，因此不再依赖洞口/柱/隔断
        Node("rooms", node_rooms, deps=("load", "wall_model", "spaces_seed",
                                        "final_candidate_state", "room_labels",
                                        "label_filter", "stairs",
                                        "defect_detect", "space_routing")),
        Node("assemble", node_assemble, kind="main", cacheable=False, deps=(
            "load", "walls_seg", "walls_merge", "columns", "columns_verify",
            "doors", "windows", "grid",
            "frame", "north", "stairs", "steps", "entrance_steps", "apron", "rooms",
            "spaces_seed", "openings_select", "final_audit",
            "final_candidate_state")),
        Node("export", node_export, kind="main", cacheable=False,
             deps=("assemble", "final_audit", "adaptive_controller", "ai_hypotheses"),
             params=("basemap_file", "ai_mode", "ai_config")),
    ])


# --------------------------------------------------------------------------- 元信息
def _drawing_extents(doc, snapshot=None):
    """图纸范围：优先 $EXTMIN/$EXTMAX，缺失/无效时回退到图元包围盒。

    DXF 里这两个 Header 字段不是必需的（ezdxf 新建图、部分导出器、裁剪后的
    片段都可能没有），以前直接 ``doc.header["$EXTMIN"]`` 会抛 DXFKeyError，
    让整条管线在图元之外的地方崩掉。
    """
    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    try:
        emin, emax = doc.header["$EXTMIN"], doc.header["$EXTMAX"]
        a, b = _num(emin[0]), _num(emin[1])
        c, d = _num(emax[0]), _num(emax[1])
        if None not in (a, b, c, d) and max(abs(a), abs(c)) < 1e19:
            return [_round(a), _round(b), _round(c), _round(d)]
    except (KeyError, TypeError, IndexError, ValueError, AttributeError):
        pass

    xs, ys = [], []
    for e in (snapshot or {}).get("entities") or ():
        for key in ("bbox", "points", "outline"):
            v = e.get(key)
            if isinstance(v, (list, tuple)) and v and isinstance(v[0], (list, tuple)):
                xs.extend(float(p[0]) for p in v)
                ys.extend(float(p[1]) for p in v)
            elif isinstance(v, (list, tuple)) and len(v) == 4 and not isinstance(v[0], (list, tuple)):
                xs.extend([float(v[0]), float(v[2])])
                ys.extend([float(v[1]), float(v[3])])
        for key in ("start", "end", "center", "insert"):
            p = e.get(key)
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                xs.append(float(p[0]))
                ys.append(float(p[1]))
    if not xs and snapshot and snapshot.get("bbox"):
        b = snapshot["bbox"]
        return [_round(b[0]), _round(b[1]), _round(b[2]), _round(b[3])]
    if not xs:
        return [0.0, 0.0, 0.0, 0.0]
    return [_round(min(xs)), _round(min(ys)), _round(max(xs)), _round(max(ys))]


def _method_note(inputs):
    """识别方法如实描述：规则识别打底，模型介入到哪一步就写哪一步。"""
    parts = ["原生 CAD 图元规则识别（图层/块名/块属性/文字/几何/拓扑）"]
    lp = ((inputs.get("load") or {}).get("layer_profile") or {})
    src = str(lp.get("source") or "")
    if src.startswith("auto"):
        parts.append(f"图层方案自动推导（{src}）")
    st = ((inputs.get("opening_judge") or {}).get("judge") or {}).get("status")
    if st in ("ok", "cached"):
        parts.append("洞口性质由模型裁决")
    parts.append("无 SVG/图像识别")
    return "；".join(parts)
