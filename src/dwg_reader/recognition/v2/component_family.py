"""构件族发现：把"重复出现、画法相同"的几何归成族，供 AI 只理解一次。

为什么以"族"为单位
------------------
逐扇调用模型看窗，成本随窗数线性涨，而且每扇都要重新猜；可是同一张图里绝大多数
窗是**同一个画法**（同一个块、同一套分格、同一种线束）。真正该做的是：

1. 程序先把候选族找出来（重复块、门弧+门扇、平行线束、闭合/近闭合轮廓）；
2. 模型只对**族的代表实例**给一次语义判断；
3. 程序把结论沿族推广到全图所有实例，并逐个用几何验证。

所以族发现只管"哪些实例长得一样"，不管"它们是什么"——类别由模型或拟合器给。

族记录里的每一条都要能回溯：实例的 uid、块名、插入变换、成员图元数、被规则引擎
覆盖了多少。没有这些东西，"推广"就是瞎猜。
"""
from __future__ import annotations

import hashlib
import math

from . import cad_ir as IR
from . import component_grammar as G
from . import geometry_fitter as FIT
from .fitters import _common as SH
from .fitters import guess_category

SCHEMA = "component-family/1.0"

#: 逻辑预算（个）：族数、每族实例数、各类种子的扫描上限
BUDGET = {"families": 40, "instances_per_family": 240, "block_min_instances": 2,
          "bundle_rows": 400, "pair_rows": 400, "contour_rows": 600,
          "arc_rows": 400, "signature_distance": 0.42, "top": 12}

#: 线束扫描参数：都是**相对本图尺度**的倍率，不是毫米数
SCAN = {"angle_bucket_deg": 2.0,      # 方向离散化
        "min_length": 4.0,            # 线长 / 噪声 的下限（太短的当标注）
        "max_length": 25.0,           # 线长 / 墙厚 的上限（再长是墙，不是构件）
        "overlap_ratio": 0.55,        # 沿向重叠占较短者的比例
        "max_members": 6,             # 一组最多几条线（超过就切成两组）
        "spacing_ratio": 0.5,         # 组内相邻线间距 / 跨度 的上限
        "min_spacing_noise": 2.0,     # 间距 / 噪声 的下限（重合线不算一对）
        "lookahead": 8,               # 沿侧向排序后最多跳过几条无关线再放弃
        "max_runs": 400}

#: 观察优先级：窗最高（用户明确要求），柱次之（家具混淆最严重），门再次
CATEGORY_PRIORITY = {"window": 1.00, "column": 0.90, "door": 0.85, "unknown": 0.45}

#: 各来源的"可信度"（只用于排序，不进裁决）
KIND_WEIGHT = {"block": 1.00, "arc": 0.90, "bundle": 0.85, "contour": 0.80,
               "pair": 0.75, "rule": 0.70}


def _sha(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:12]


def _r(v, nd: int = 1) -> float:
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------- 实例种子
def _children_index(ir: dict) -> dict:
    """``块实例 handle -> 展开后的子图元 uid``（IR 里子图元 handle 以 ``H/`` 开头）。"""
    out: dict = {}
    for handle, uids in (((ir.get("index") or {}).get("handle_to_uid")) or {}).items():
        if "/" in str(handle):
            parent = str(handle).split("/", 1)[0]
            out.setdefault(parent, []).extend(uids)
    return out


def _instance(uid_list, bbox, *, handle=None, block=None, transform=None,
              kind="lines") -> dict:
    uids = sorted({str(u) for u in uid_list or ()})
    return {"id": "inst:" + _sha("|".join(uids) + "|" + str(kind)),
            "uids": uids, "bbox": bbox, "handle": handle, "block": block,
            "kind": kind, "transform": transform or {"rotation": 0.0,
                                                     "scale": [1.0, 1.0],
                                                     "mirror": False}}


def block_instances(ir: dict) -> list:
    """重复块族的实例：一个插入点算一个实例，成员是**块内展开后的真实图元**。"""
    children = _children_index(ir)
    by_uid = IR.by_uid(ir)
    out: list = []
    for fam in ir.get("families") or ():
        if int(fam.get("n") or 0) < int(BUDGET["block_min_instances"]):
            continue
        for uid in (fam.get("uids") or ())[:int(BUDGET["instances_per_family"])]:
            insert = by_uid.get(uid)
            if not insert:
                continue
            handle = insert.get("handle")
            members = children.get(str(handle), [])
            uids = [uid] + members
            scale = list((insert.get("geom") or {}).get("scale") or [1.0, 1.0])
            out.append(_instance(uids, insert.get("bbox"), handle=handle,
                                 block=fam.get("name"),
                                 kind="block",
                                 transform={"rotation": _r((insert.get("geom")
                                                            or {}).get("rotation") or 0.0, 2),
                                            "scale": [_r(scale[0], 4), _r(scale[1], 4)],
                                            "mirror": bool(scale[0] < 0 or scale[1] < 0),
                                            "source": "insert"}))
    return out


def _seg_grid(segments, *, cell: float) -> dict:
    grid: dict = {}
    for seg in segments:
        box = seg.get("bbox")
        if not box:
            continue
        for gx in range(int(box[0] // cell), int(box[2] // cell) + 1):
            for gy in range(int(box[1] // cell), int(box[3] // cell) + 1):
                grid.setdefault((gx, gy), []).append(seg)
    return grid


def _near(grid, box, *, cell: float) -> list:
    if not box:
        return []
    seen: dict = {}
    for gx in range(int(box[0] // cell), int(box[2] // cell) + 1):
        for gy in range(int(box[1] // cell), int(box[3] // cell) + 1):
            for seg in grid.get((gx, gy), ()):
                seen[seg["uid"]] = seg
    return list(seen.values())


def arc_instances(ir: dict) -> list:
    """门弧实例：一条开启弧 + 铰链附近的门扇线（弧心附近的短线）。"""
    entities = list(ir.get("entities") or ())
    basis = ir.get("basis") or {}
    thickness = float(basis.get("thickness") or 200.0)
    cell = max(1.0, thickness * 8.0)
    segments = [e for e in entities if e.get("kind") in ("segment", "poly")]
    grid = _seg_grid(segments, cell=cell)
    out: list = []
    for arc in entities:
        if arc.get("kind") != "arc":
            continue
        geom = arc.get("geom") or {}
        center, radius = geom.get("center"), float(geom.get("radius") or 0.0)
        if not center or radius <= 0:
            continue
        members = [arc]
        reach = float(SH.LIMITS["host_gap_ratio"]) * radius * 0.5
        for seg in _near(grid, arc.get("bbox"), cell=cell):
            if seg.get("uid") in {m["uid"] for m in members}:
                continue
            for a, b in SH.segments_of(seg):
                if min(SH._len(a, center), SH._len(b, center)) <= reach and \
                        abs(SH._len(a, b) - radius) <= 0.35 * radius:
                    members.append(seg)
                    break
            if len(members) >= 4:
                break
        out.append(_instance([m["uid"] for m in members],
                             SH.bbox_of(members), handle=arc.get("handle"),
                             kind="arc"))
        if len(out) >= int(BUDGET["arc_rows"]):
            break
    return out


def bundle_instances(ir: dict) -> list:
    """线束实例：≥3 条平行等距线（散线窗、幕墙分格、窗帘盒都长这样）。"""
    basis = ir.get("basis") or {}
    rows = FIT.m_repeated_line_bundle(
        list(ir.get("entities") or ()),
        thickness=float(basis.get("thickness") or 200.0),
        tol=float(basis.get("noise") or 2.0) * 2.0)
    return [_instance(row.get("uids"), row.get("bbox"), kind="bundle")
            for row in rows[:int(BUDGET["bundle_rows"])]]


def line_runs(ir: dict) -> list:
    """平行线"束"：2~6 条平行且沿向重叠的线（窗框、双线墙、门垛都是这个形态）。

    这里**不能**用墙厚做配对门槛——窄窗框的间距可能只有墙厚的几分之一，一卡就
    全没了（这正是当初要改的毛病）。配对只看三件事：方向一致、沿向重叠、间距在
    "噪声之上、跨度一半之下"。
    """
    import math

    basis = ir.get("basis") or {}
    thickness = float(basis.get("thickness") or 200.0)
    noise = float(basis.get("noise") or 2.0)
    lo_len = float(SCAN["min_length"]) * noise
    hi_len = float(SCAN["max_length"]) * thickness
    lines = [e for e in (ir.get("entities") or ())
             if e.get("kind") in ("segment", "poly") and e.get("angle") is not None
             and lo_len <= float(e.get("length") or 0.0) <= hi_len]
    buckets: dict = {}
    for e in lines:
        key = round(float(e["angle"]) / float(SCAN["angle_bucket_deg"]))
        buckets.setdefault(key, []).append(e)

    runs: list = []
    seen: set = set()
    for key, members in buckets.items():
        if len(members) < 2:
            continue
        ang = math.radians(float(members[0]["angle"]))
        ux, uy = math.cos(ang), math.sin(ang)
        nx, ny = -uy, ux
        rows = []
        for e in members:
            seg = [(e.get("geom") or {}).get("a"), (e.get("geom") or {}).get("b")]
            if e.get("kind") == "poly":
                pts = (e.get("geom") or {}).get("pts") or ()
                if len(pts) < 2:
                    continue
                seg = [pts[0], pts[-1]]
            if not seg[0] or not seg[1]:
                continue
            side = (seg[0][0] * nx + seg[0][1] * ny
                    + seg[1][0] * nx + seg[1][1] * ny) / 2.0
            proj = sorted([seg[0][0] * ux + seg[0][1] * uy,
                           seg[1][0] * ux + seg[1][1] * uy])
            rows.append({"e": e, "side": side, "lo": proj[0], "hi": proj[1],
                         "len": proj[1] - proj[0]})
        rows.sort(key=lambda r: r["side"])
        for i, start in enumerate(rows):
            run = [start]
            skipped = 0
            for nxt in rows[i + 1:]:
                prev = run[-1]
                span = min(prev["hi"] - prev["lo"], nxt["hi"] - nxt["lo"])
                overlap = min(prev["hi"], nxt["hi"]) - max(prev["lo"], nxt["lo"])
                if span <= 0:
                    break
                if overlap / span < float(SCAN["overlap_ratio"]):
                    # 相邻行可能是"别的构件"的线：少量跳过，不整段放弃
                    skipped += 1
                    if skipped > int(SCAN["lookahead"]):
                        break
                    continue
                spacing = nxt["side"] - prev["side"]
                if spacing <= float(SCAN["min_spacing_noise"]) * noise:
                    continue
                if spacing > float(SCAN["spacing_ratio"]) * span:
                    break
                run.append(nxt)
                skipped = 0
                if len(run) >= int(SCAN["max_members"]):
                    break
            if len(run) < 2:
                continue
            uids = tuple(sorted(str(r["e"]["uid"]) for r in run))
            if uids in seen:
                continue
            seen.add(uids)
            runs.append({"uids": list(uids),
                         "bbox": SH._union([r["e"].get("bbox") for r in run]),
                         "count": len(run),
                         "spacing": SH._r(abs(run[-1]["side"] - run[0]["side"])
                                          / max(1, len(run) - 1)),
                         "angle": SH._r(float(run[0]["e"].get("angle") or 0.0), 2)})
            if len(runs) >= int(SCAN["max_runs"]):
                return [_instance(run["uids"], run["bbox"], kind="pair")
                        for run in runs]
    return [_instance(run["uids"], run["bbox"], kind="pair") for run in runs]


def pair_instances(ir: dict) -> list:
    """平行线对/线束实例：用**局部间距**配对，不用全图墙厚当门槛。"""
    return line_runs(ir)[:int(BUDGET["pair_rows"])]


def contour_instances(ir: dict) -> list:
    """闭合/填充轮廓实例：柱、墩、异形柱。"""
    basis = ir.get("basis") or {}
    rows = FIT.m_closed_contour(
        list(ir.get("entities") or ()),
        thickness=float(basis.get("thickness") or 200.0),
        tol=float(basis.get("noise") or 2.0) * 2.0)
    return [_instance(row.get("uids"), row.get("bbox"), kind="contour")
            for row in rows[:int(BUDGET["contour_rows"])]]


def _rule_handles(rec: dict) -> list:
    """Normalize rule provenance without dropping composite source entities."""
    handles = list(rec.get("handles") or ())
    handles.extend(src.get("handle") for src in rec.get("srcs") or ()
                   if isinstance(src, dict))
    handles.append(rec.get("handle"))
    return sorted({str(h) for h in handles if h is not None and str(h)})


def rule_instances(ir: dict, *, rules=None) -> list:
    """规则引擎已认出的门窗柱：按实例聚成族（模型只需确认其中一族）。"""
    rules = rules or {}
    by_uid = IR.by_uid(ir)
    index = (ir.get("index") or {}).get("handle_to_uid") or {}
    out: list = []
    for bucket in ("doors", "windows", "columns"):
        for rec in rules.get(bucket) or ():
            if not isinstance(rec, dict):
                continue
            uids: list = []
            handles = _rule_handles(rec)
            for handle in handles:
                uids.extend(index.get(handle, ()))
            if not uids:
                continue
            box = SH.bbox_of([by_uid[u] for u in uids if u in by_uid])
            out.append(_instance(uids, box or rec.get("bbox"), kind="rule",
                                 handle=handles[0] if len(handles) == 1 else None))
    return out


# --------------------------------------------------------------------- 归类
def _signature(by_uid: dict, instance) -> dict | None:
    entities = [by_uid[u] for u in instance.get("uids") or () if u in by_uid]
    return SH.shape_signature(entities)


def _coarse_key(sig: dict) -> tuple:
    """粗分桶：图元种类直方图 + 长宽比（都与旋转无关），先快速排除不可能同族的。"""
    kinds = tuple(sorted((sig.get("kinds") or {}).items()))
    aspect = float(sig.get("aspect") or 1.0)
    return (kinds, round(aspect * 2.0))


def _physical_profile(by_uid, instance, *, walls, noise):
    entities = [by_uid[u] for u in instance.get("uids") or () if u in by_uid]
    rect = SH.min_area_rect(SH.points_of(entities))
    if not rect:
        return None
    drawing = tuple(sorted((e.get("kind"), bool(SH.is_closed(e)))
                           for e in entities if e.get("kind") != "text"))
    return {"drawing": drawing, "size": sorted([rect["w"], rect["h"]]),
            "wall_hit": bool(SH.wall_context(walls, SH.bbox_of(entities),
                                             tol=noise * 4).get("hit"))}


def _same_physical_family(a, b, *, noise):
    if not a or not b or a["drawing"] != b["drawing"] or a["wall_hit"] != b["wall_hit"]:
        return False
    return all(abs(x - y) <= max(noise * 4, min(x, y) * 0.05)
               for x, y in zip(a["size"], b["size"]))


def _group(ir: dict, instances, *, key_of=None, physical=False, walls=()) -> list:
    """按形状签名归类：同族=签名距离够近（旋转/镜像/缩放都已归一）。"""
    families: list = []
    tol = float(BUDGET["signature_distance"])
    by_uid = IR.by_uid(ir)
    index: dict = {}
    for inst in instances:
        sig = _signature(by_uid, inst)
        if not sig:
            continue
        inst["signature"] = sig
        noise = float((ir.get("basis") or {}).get("noise") or 2.0)
        if physical:
            inst["physical"] = _physical_profile(by_uid, inst, walls=walls, noise=noise)
        key = key_of(inst) if key_of else None
        coarse = _coarse_key(sig)
        placed = False
        for fam in index.get((key, coarse), ()):
            if key is not None and fam.get("key") != key:
                continue
            if physical and not _same_physical_family(
                    inst.get("physical"), fam["instances"][0].get("physical"), noise=noise):
                continue
            dist = SH.signature_distance(fam["signature"], sig)
            if dist <= tol:
                fam["instances"].append(inst)
                fam["_dist"].append(dist)
                placed = True
                break
        if not placed:
            fam = {"key": key, "signature": sig, "instances": [inst],
                   "_dist": [0.0]}
            families.append(fam)
            index.setdefault((key, coarse), []).append(fam)
    return families


def _area(inst: dict) -> float:
    box = inst.get("bbox")
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return 0.0
    return max(0.0, float(box[2]) - float(box[0])) * max(0.0, float(box[3])
                                                         - float(box[1]))


def _touches_wall(inst: dict, walls, *, thickness: float) -> bool:
    """实例挨着已识别墙：族里"有宿主关系"的成员比孤立的更有判断价值。"""
    box = inst.get("bbox")
    if not walls or not isinstance(box, (list, tuple)) or len(box) != 4:
        return False
    center = SH._center(box)
    reach = 1.5 * float(thickness or 0.0) + 0.5 * math.hypot(
        float(box[2]) - float(box[0]), float(box[3]) - float(box[1]))
    for wall in walls:
        start, end = (wall or {}).get("start"), (wall or {}).get("end")
        if not start or not end:
            continue
        if SH._pt_seg_dist(center, start, end) <= reach:
            return True
    return False


def _choose_representative(instances, *, ir: dict, walls=(), noise: float = 2.0,
                           thickness: float = None) -> tuple[dict, list]:
    """挑代表实例：**典型 + 有上下文**，而不是"离图纸中心最近"。

    以前取离整图包围盒中心最近的实例——那个位置与"这一族最好判断"毫无关系，
    实测挑中的是一个 16 倍墙厚远、完全没有宿主关系的家具矩形。现在按三件事打分：
    尺寸接近中位、图元数与众数一致（不会挑到族里的异类）、贴墙（有宿主可判）。
    返回 ``(实例, 判据列表)``，判据进审计，能查"为什么是它"。
    """
    thickness = float(thickness if thickness is not None
                      else (ir.get("basis") or {}).get("thickness") or 200.0)
    areas = sorted(_area(inst) for inst in instances)
    median = areas[len(areas) // 2] if areas else 0.0
    counts = [len(inst.get("uids") or ()) for inst in instances]
    modal = max(set(counts), key=counts.count) if counts else 0
    drawing = ir.get("bbox") or [0, 0, 0, 0]
    diag = max(1e-9, math.hypot(float(drawing[2]) - float(drawing[0]),
                                float(drawing[3]) - float(drawing[1])))
    best, best_score, best_why = None, -1.0, []
    for inst in instances:
        why = []
        area = _area(inst)
        size_term = 0.0 if median <= 0 or area <= 0 else min(
            1.0, abs(area - median) / median)
        if size_term == 0.0:
            why.append("size_is_median")
        count = len(inst.get("uids") or ())
        count_term = 0.0 if count == modal else 1.0
        if count_term == 0.0:
            why.append("entity_count_is_modal")
        wall = _touches_wall(inst, walls, thickness=thickness)
        if wall:
            why.append("has_wall_context")
        center = SH._center(inst.get("bbox") or [0, 0, 0, 0])
        spread = min(1.0, SH._len(center, SH._center(drawing)) / diag)
        score = (2.0 * (1.0 - size_term) + 1.0 * (1.0 - count_term)
                 + 1.5 * (1.0 if wall else 0.0) + 0.5 * (1.0 - spread))
        if score > best_score + 1e-9:
            best, best_score, best_why = inst, score, why
    return best or instances[0], best_why


def _family_record(ir: dict, fam: dict, *, kind: str, name: str,
                   known_uids=(), walls=()) -> dict:
    instances = fam["instances"][:int(BUDGET["instances_per_family"])]
    if not instances:
        return {}
    by_uid = IR.by_uid(ir)
    basis = ir.get("basis") or {}
    rep, rep_why = _choose_representative(
        instances, ir=ir, walls=walls,
        noise=float(basis.get("noise") or 2.0),
        thickness=float(basis.get("thickness") or 200.0))
    rep_entities = [by_uid[u] for u in rep.get("uids") or () if u in by_uid]
    category = guess_category(rep_entities)
    covered = 0
    total = 0
    for inst in instances:
        for uid in inst.get("uids") or ():
            total += 1
            if uid in known_uids:
                covered += 1
    explained = (covered / total) if total else 0.0
    # 重复度用连续函数：以前 min(1, n/6) 让所有 ≥6 成员的族同分（实测 6/7/9/20/28
    # 个成员的柱轮廓族全部 0.72，先后顺序等于没排序）
    n = len(instances)
    repetition = 0.5 + 0.5 * (n / (n + 6.0))
    priority = (float(CATEGORY_PRIORITY.get(category, CATEGORY_PRIORITY["unknown"]))
                * repetition
                * float(KIND_WEIGHT.get(kind, 0.6))
                * (1.0 - 0.35 * explained))
    box = SH.bbox_of(rep_entities)
    return {"schema": SCHEMA, "id": "fam:" + _sha(name + "|" + rep["id"]),
            "kind": kind, "name": name, "n": len(instances),
            "category_guess": category,
            "authorization_scope": ("representative_only" if rep_entities and all(
                e.get("kind") in ("poly", "segment", "text") for e in rep_entities)
                else "family"),
            "representative": {"id": rep["id"], "uids": rep.get("uids"),
                               "bbox": box, "handle": rep.get("handle"),
                               "block": rep.get("block"),
                               "transform": rep.get("transform")},
            "representative_reason": rep_why,
            "instances": [{"id": inst["id"], "uids": inst["uids"],
                           "bbox": inst.get("bbox"), "handle": inst.get("handle"),
                           "block": inst.get("block"),
                           "transform": inst.get("transform")}
                          for inst in instances],
            "signature": fam["signature"],
            "bbox": SH._union([inst.get("bbox") for inst in instances]),
            "layers": sorted({str((by_uid[u] or {}).get("layer") or "")
                              for inst in instances for u in inst.get("uids") or ()
                              if u in by_uid})[:8],
            "explained_ratio": _r(explained, 3),
            "priority": _r(priority, 4),
            "evidence": {"instances": len(instances),
                         "max_signature_distance": _r(max(fam["_dist"] or [0.0]), 3),
                         "block": rep.get("block"),
                         "representative_reason": rep_why,
                         "transform_variants": _transform_variants(instances)}}


def _transform_variants(instances) -> dict:
    """族内是否出现旋转/镜像/缩放（这正是"推广"必须处理的三种变换）。"""
    rotations = set()
    mirrored = False
    scales = set()
    for inst in instances or ():
        tr = inst.get("transform") or {}
        rotations.add(round(float(tr.get("rotation") or 0.0) / 15.0) * 15.0)
        mirrored = mirrored or bool(tr.get("mirror"))
        scale = tr.get("scale") or [1.0, 1.0]
        scales.add(round(float(scale[0] or 1.0), 3))
    return {"rotations_deg": sorted(rotations)[:8], "mirror": mirrored,
            "scales": sorted(scales)[:8]}


def discover(ir: dict, *, graph=None, walls=(), rules=None, limit=None,
             known_uids=(), kinds=("pair", "bundle", "block", "arc", "contour",
                                   "rule")) -> dict:
    """找构件族：平行线束 / 线对 / 重复块 / 门弧 / 闭合轮廓 / 规则已认出的实例。"""
    per_kind: dict = {}
    families: list = []
    trace: list = []
    if not known_uids:
        known_uids = set()
        for bucket in ("doors", "windows", "columns"):
            for rec in (rules or {}).get(bucket) or ():
                if not isinstance(rec, dict):
                    continue
                # Geometry candidates still need model observation; they are
                # not already explained merely because a rule found them.
                evidence = rec.get("evidence")
                if bucket == "columns" and not (
                        isinstance(evidence, dict)
                        and evidence.get("category_confirmed") is True):
                    continue
                for handle in _rule_handles(rec):
                    known_uids.update(IR.uids_for_handle(ir, handle))

    sources = {
        "block": (block_instances(ir), lambda inst: inst.get("block") or ""),
        "arc": (arc_instances(ir), None),
        "bundle": (bundle_instances(ir), None),
        "pair": (pair_instances(ir), None),
        "contour": (contour_instances(ir), None),
        "rule": (rule_instances(ir, rules=rules) if rules else [], None),
    }
    # ---- 先按画法各自建族，再**轮流**取：不能因为平行线对先跑，就把 40 个名额
    # 全部占满（实测整张图 40 个族全是 pair，块和闭合轮廓一个都没进观察名单，
    # 模型等于只在看几根线）。
    per_kind_families: dict = {}
    for kind in kinds:
        instances, key_of = sources.get(kind, ([], None))
        per_kind[kind] = len(instances)
        if not instances:
            per_kind_families[kind] = []
            trace.append({"kind": kind, "instances": 0, "families": 0})
            continue
        grouped = _group(ir, instances, key_of=key_of,
                         physical=kind in ("rule", "contour"), walls=walls)
        rows = []
        for index, fam in enumerate(grouped):
            name = "%s:%s" % (kind, (key_of(fam["instances"][0]) if key_of else index)
                              or index)
            record = _family_record(ir, fam, kind=kind, name=name,
                                    known_uids=known_uids, walls=walls)
            if record:
                rows.append(record)
        rows.sort(key=lambda f: -float(f.get("priority") or 0.0))
        per_kind_families[kind] = rows
        trace.append({"kind": kind, "instances": len(instances),
                      "families": len(rows)})

    cap = int(BUDGET["families"])
    #: 每种画法最多占四成名额，剩下的按优先级补
    quota = max(1, int(round(cap * 0.4)))
    taken: dict = {kind: 0 for kind in per_kind_families}
    families: list = []
    while len(families) < cap:
        progressed = False
        for kind in kinds:
            if len(families) >= cap:
                break
            rows = per_kind_families.get(kind) or []
            if taken.get(kind, 0) >= min(len(rows), quota):
                continue
            families.append(rows[taken[kind]])
            taken[kind] = taken.get(kind, 0) + 1
            progressed = True
        if not progressed:            # 各画法的配额都用完了：按剩余优先级补满
            rest = [f for kind, rows in per_kind_families.items()
                    for f in rows[taken.get(kind, 0):]]
            rest.sort(key=lambda f: -float(f.get("priority") or 0.0))
            families.extend(rest[:cap - len(families)])
            break
    trace.append({"kind": "round_robin", "quota": quota,
                  "taken": dict(taken), "families": len(families)})

    families.sort(key=lambda f: -float(f.get("priority") or 0.0))
    if limit:
        families = families[:int(limit)]
    by_cat: dict = {}
    for fam in families:
        key = fam.get("category_guess") or "unknown"
        by_cat[key] = by_cat.get(key, 0) + 1
    return {"schema": SCHEMA, "families": families, "trace": trace,
            "stats": {"families": len(families), "instances": sum(
                int(f.get("n") or 0) for f in families),
                "by_category_guess": by_cat,
                # 每种画法实际有多少个族（以前这里写成"存在与否"0/1，读起来像族数）
                "by_kind": {k: sum(1 for f in families if f["kind"] == k)
                            for k in per_kind},
                "seed_instances": per_kind}}


# --------------------------------------------------------------------- 观察对象
def observation_queue(families, *, categories=None) -> list:
    """可以给模型看的族，按"该先看谁"排好序（不截断）。

    排序键（同分时才看后面的）：类别优先级 → 优先级 → 一个族已经被模型判过没有。
    多个同类别族会被依次排开，**被否决一个不会让同类别其它族失去机会**。
    """
    wanted = tuple(categories or ("window", "column", "door"))
    rows = []
    for fam in families or ():
        cat = fam.get("category_guess")
        if cat not in wanted and cat not in (None, "unknown"):
            continue
        rows.append({"family": fam, "priority": fam.get("priority"),
                     "why": {"category": cat, "instances": fam.get("n"),
                             "kind": fam.get("kind"),
                             "explained_ratio": fam.get("explained_ratio"),
                             "still_ununderstood": fam.get("explained_ratio", 1.0) < 0.95}})
    rows.sort(key=lambda row: -float(row["priority"] or 0.0))
    return rows


def next_observation(families, *, observed=(), category_counts=None,
                     categories=None) -> dict | None:
    """下一族该看谁：**没看过的**里面，优先补看观察次数最少的类别。

    以前是"一次挑 4 个族，看完就结束"：柱族被否决后，同一张图里另外 9 个柱候选族
    再没有机会，明明还有 3 次调用没用。现在由调用方循环取，每次取"最缺观察的类别
    里优先级最高的未看族"。
    """
    seen = {str(f) for f in (observed or ())}
    counts = dict(category_counts or {})
    rows = [row for row in observation_queue(families, categories=categories)
            if str((row["family"] or {}).get("id")) not in seen]
    if not rows:
        return None

    def rank(row):
        cat = row["why"]["category"] or "unknown"
        return (counts.get(cat, 0), -float(row["priority"] or 0.0))

    rows.sort(key=rank)
    return rows[0]


def select_for_observation(families, *, limit: int = 6, categories=None) -> list:
    """按类别优先级 + 覆盖率 + 重复度挑"值得让模型看一眼"的族。"""
    rows = observation_queue(families, categories=categories)
    cap = max(0, int(limit))
    # Reserve one observation per available category, including unknown, when
    # the budget permits. Geometry guesses must not starve unseen categories.
    first_by_category = {}
    for row in rows:
        cat = row["why"]["category"] or "unknown"
        first_by_category.setdefault(cat, row)
    if cap < len(first_by_category):
        return rows[:cap]
    selected = list(first_by_category.values())
    selected_ids = {id(row) for row in selected}
    selected.extend(row for row in rows if id(row) not in selected_ids)
    return selected[:cap]


def wall_relation(family: dict, ir: dict, walls=(), *, tol_deg: float = 5.0) -> dict:
    """候选与已识别墙的关系：这是"墙线对"还是"墙上的洞"，只能靠这个分辨。

    给模型的事实必须是**关系**，不是我猜的类别：

    * ``distance_over_thickness``：候选到最近墙轴线的距离 / 墙厚；
    * ``parallel_to_wall``：候选长轴与墙轴是否同向；
    * ``covers_wall_faces``：候选取的两条线是否正好落在该墙的**两面线**位置
      （是 → 它就是这堵墙自己，不是墙上的洞）；
    * ``wall_continues``：候选两侧还有多少条与该墙共线的线段延续（窗的位置墙应
      该是被打断的）。
    """
    by_uid = IR.by_uid(ir)
    rep = family.get("representative") or {}
    entities = [by_uid[u] for u in rep.get("uids") or () if u in by_uid]
    if not entities:
        return {}
    points = SH.points_of(entities)
    rect = SH.min_area_rect(points)
    box = rep.get("bbox") or SH.bbox_of(entities)
    basis = ir.get("basis") or {}
    thickness = float(basis.get("thickness") or 200.0)
    noise = float(basis.get("noise") or 2.0)
    if not rect or not box:
        return {}
    center = SH._center(box)
    axis_angle = rect["angle"] + (90.0 if rect["h"] > rect["w"] else 0.0)
    span = max(rect["w"], rect["h"])
    best = None
    for wall in walls or ():
        a, b = (wall or {}).get("start"), (wall or {}).get("end")
        if not a or not b:
            continue
        gap = SH._pt_seg_dist(center, a, b)
        if best is None or gap < best["gap"]:
            best = {"gap": gap, "wall": wall, "a": a, "b": b,
                    "thickness": float(wall.get("thickness") or thickness)}
    if best is None:
        return {"available": False, "reason": "NO_WALLS"}
    wall_angle = SH._angle(best["a"], best["b"])
    parallel = SH._angle_diff(axis_angle, wall_angle) <= float(tol_deg)
    # 候选的两条线是否落在墙的两面线上（法向位置比对）
    ang = math.radians(wall_angle)
    ux, uy = math.cos(ang), math.sin(ang)
    nx, ny = -uy, ux
    sides = []
    for entity in entities:
        for a, b in SH.segments_of(entity):
            if SH._angle_diff(SH._angle(a, b), wall_angle) <= float(tol_deg):
                sides.append((a[0] * nx + a[1] * ny + b[0] * nx + b[1] * ny) / 2.0)
    wall_side = (best["a"][0] * nx + best["a"][1] * ny
                 + best["b"][0] * nx + best["b"][1] * ny) / 2.0
    half = best["thickness"] / 2.0
    on_faces = sum(1 for side in sides
                   if abs(abs(side - wall_side) - half) <= 0.30 * best["thickness"])
    # 两侧共线延续：窗的位置墙被打断，墙线对的两侧会一直连下去
    reach = max(3.0 * span, 6.0 * thickness)
    lo = min(pt[0] * ux + pt[1] * uy for pt in
             ([box[0], box[1]], [box[2], box[1]], [box[2], box[3]], [box[0], box[3]]))
    hi = max(pt[0] * ux + pt[1] * uy for pt in
             ([box[0], box[1]], [box[2], box[1]], [box[2], box[3]], [box[0], box[3]]))
    continues = 0
    for entity in ir.get("entities") or ():
        if entity.get("kind") not in ("segment", "poly") or not entity.get("bbox"):
            continue
        if entity.get("uid") in {e.get("uid") for e in entities}:
            continue
        if SH._box_gap(entity["bbox"], box) > reach:
            continue
        for a, b in SH.segments_of(entity):
            if SH._angle_diff(SH._angle(a, b), wall_angle) > float(tol_deg):
                continue
            ta, tb = (a[0] * ux + a[1] * uy, b[0] * ux + b[1] * uy)
            t0, t1 = min(ta, tb), max(ta, tb)
            if t0 > hi + noise or t1 < lo - noise:
                continues += 1
    return {"available": True,
            "distance_over_thickness": SH._r(best["gap"] / max(1e-9, best["thickness"]), 2),
            "parallel_to_wall": bool(parallel),
            "covers_wall_faces": int(on_faces),
            "wall_thickness": SH._r(best["thickness"]),
            "wall_continues": int(continues),
            "depth_over_thickness": SH._r(min(rect["w"], rect["h"])
                                          / max(1e-9, best["thickness"]), 2)}


def width_percentile(width, widths) -> dict:
    """候选宽度在**本图洞口宽度分布**里的分位（只给分位，不给绝对门槛）。"""
    rows = sorted(float(w) for w in (widths or ()) if float(w or 0) > 0)
    if not rows or not width:
        return {"sample": len(rows), "percentile": None}
    below = sum(1 for w in rows if w <= float(width))
    return {"sample": len(rows), "percentile": SH._r(below / float(len(rows)), 3),
            "median": SH._r(rows[len(rows) // 2])}


def family_facts(family: dict, ir: dict, *, walls=(), widths=(),
                 layout: bool = False) -> dict:
    """给模型看的族事实：只给几何关系，**不给程序猜的类别，也不给层名/块名**。

    以前这里带了一个 ``geometry_guess``（按"非弧、非闭合 → 窗"的默认值猜的），
    等于先把答案写进提示词，模型只能顺着说"是窗"；后来还留着 ``name`` /
    ``layers`` / ``block``——那是图层名和块名，既不是几何证据，又足够"顺眼"到
    让模型拿它当理由。现在只给可核对的关系量与形态量：与墙的距离、是否落在墙的
    两面线上、墙在该处是否延续、宽度在图内洞口分布里的分位、成员数与变换。

    ``block_instance`` 只回答"这个成员是不是块参照"（是否展开成重复几何），
    不带块名——块名本身是 CAD 里的自由文本，不能当作类别证据。

    ``layout=True`` 时再加三组**全部成员**的事实（见 :mod:`family_layout`）：
    成员排列（成行成列/间距重复）、墙线在成员边界上的交接方式、填充类型。
    """
    by_uid = IR.by_uid(ir)
    rep = family.get("representative") or {}
    entities = [by_uid[u] for u in rep.get("uids") or () if u in by_uid]
    kinds: dict = {}
    for e in entities:
        kinds[e.get("kind")] = kinds.get(e.get("kind"), 0) + 1
    rect = SH.min_area_rect(SH.points_of(entities))
    facts = {"family": family.get("id"),
             "drawing_method": family.get("kind"),
             "instances": family.get("n"),
             "block_instance": bool(rep.get("block")),
             "entity_kinds": kinds,
             "entity_count": len(entities),
             "arcs": kinds.get("arc", 0),
             "closed_contours": sum(1 for e in entities if SH.is_closed(e)),
             "aspect_ratio": None if not rect else SH._r(
                 rect["w"] / max(1e-9, rect["h"]), 2),
             "size_over_wall_thickness": None if not rect else SH._r(
                 max(rect["w"], rect["h"]) / max(1e-9, float(
                     (ir.get("basis") or {}).get("thickness") or 1.0)), 2),
             "transform_variants": (family.get("evidence") or {}).get("transform_variants"),
             "explained_ratio": family.get("explained_ratio")}
    facts["wall_relation"] = wall_relation(family, ir, walls)
    if rect:
        facts["opening_width_percentile"] = width_percentile(max(rect["w"], rect["h"]),
                                                             widths)
    if layout:
        from . import family_layout as LAYOUT
        facts.update(LAYOUT.facts(family, ir, walls))
    return facts


def summary(result) -> dict:
    result = result or {}
    return {"schema": SCHEMA, "stats": result.get("stats") or {},
            "top": [{"id": f.get("id"), "kind": f.get("kind"), "n": f.get("n"),
                     "category_guess": f.get("category_guess"),
                     "category": f.get("category"),
                     "confirmed": bool(f.get("confirmed")),
                     "priority": f.get("priority")}
                    for f in (result.get("families") or ())[:8]]}


__all__ = ["BUDGET", "CATEGORY_PRIORITY", "SCHEMA", "arc_instances",
           "block_instances", "bundle_instances", "contour_instances", "discover",
           "family_facts", "pair_instances", "rule_instances", "wall_relation",
           "width_percentile", "next_observation", "observation_queue",
           "select_for_observation", "summary"]
