"""修复算子（闭集）：只产出"推断几何"，坐标由确定性算法算，LLM 只能选 option。

反循环自证：一个补边/补墙方案，除"房名需要闭合"外必须至少有一条独立支持证据
（共线墙端、对侧墙面线、填充边界、尺寸标注、门块位置），否则只能作为房间假设，
不能升级成墙体或房间实体。
"""
from __future__ import annotations

import math

INFERRED = "inferred"

REPAIR_OPTIONS = {
    "option:close_gap": "try_virtual_boundary",
    "option:opening": "treat_as_opening",
    "option:leave_open": "no_action",
    "option:change_closure": "rebuild_space_faces",
    "option:retry_pair": "retry_wall_pairing",
    "option:single_line_wall": "single_line_wall",
    "option:reject": "reject_candidate",
    "option:override_layer": "override_layer_role",
    "option:ignore_layer": "ignore_layer",
    "option:rebuild_contour": "rebuild_contour",
    "option:treat_as_fill": "treat_as_fill",
    "option:merge": "merge_duplicate",
    "option:keep_thicker": "keep_thicker",
    "option:llm_analyze": "llm_reason",
    "option:leave_unresolved": "no_action",
}


def virtual_edge_between(wa, wb, *, supports, reason="BROKEN_WALL") -> dict | None:
    """两段共线墙面线的最近端点之间生成虚拟边（只作推断候选）。"""
    if not wa or not wb:
        return None
    a = [(float(wa["start"][0]), float(wa["start"][1])),
         (float(wa["end"][0]), float(wa["end"][1]))]
    b = [(float(wb["start"][0]), float(wb["start"][1])),
         (float(wb["end"][0]), float(wb["end"][1]))]
    pa, pb, best = a[0], b[0], None
    for p in a:
        for q in b:
            d = math.dist(p, q)
            if best is None or d < best:
                best, pa, pb = d, p, q
    if best is None or best <= 1.0:
        return None
    return {"kind": "virtual_edge", "geometry_state": INFERRED,
            "start": [pa[0], pa[1]], "end": [pb[0], pb[1]],
            "length": round(best, 1),
            "supports": list(supports), "reason": reason,
            "confidence": round(min(0.9, 0.4 + 0.15 * len(supports)), 2),
            "status": "candidate"}


def try_virtual_boundary(*, defect: dict, walls_by_id=None, supports_extra=()) -> dict:
    """option:close_gap -> 一条虚拟边（是否采用由求解器按证据决定）。"""
    detail = defect.get("detail") or {}
    walls_by_id = walls_by_id or {}
    wa = walls_by_id.get(detail.get("wall_a"))
    wb = walls_by_id.get(detail.get("wall_b"))
    supports = ["collinear_wall_ends"]
    if detail.get("gap_mm"):
        supports.append(f"gap:{detail['gap_mm']}mm")
    supports += list(supports_extra)
    edge = virtual_edge_between(wa, wb, supports=supports)
    if edge is None:
        return {"ok": False, "reason": "NO_WALL_PAIR", "inferred": [],
                "independent": 0}
    edge["id"] = f"inferred:{defect['defect_id']}"
    # 独立支持 = 除"房名需要闭合"之外的证据条数
    return {"ok": True, "inferred": [edge], "independent": len(supports) - 1,
            "evidence": [f"ev:{h}" for h in ((wa or {}).get("handles") or ())][:4]}


def rebuild_contour(*, hatch: dict) -> dict:
    """option:rebuild_contour -> 推断轮廓（不再用长宽比硬门槛）。"""
    loops = (hatch or {}).get("loops") or ()
    if not loops:
        return {"ok": False, "reason": "NO_LOOPS", "inferred": []}
    outer = max(loops, key=len)
    xs = [float(p[0]) for p in outer]
    ys = [float(p[1]) for p in outer]
    return {"ok": True, "inferred": [{
        "kind": "inferred_contour", "geometry_state": INFERRED,
        "outline": [[float(p[0]), float(p[1])] for p in outer],
        "bbox": [min(xs), min(ys), max(xs), max(ys)],
        "supports": [f"ev:{(hatch or {}).get('handle')}"],
        "reason": "OPEN_CONTOUR", "status": "candidate"}],
        "independent": 0}


def open_contour_verdict(*, has_closed_sibling: bool, has_wall_junction: bool,
                         is_repeated: bool) -> str:
    """填充该算柱/算装饰/留待定——组合判据，不用单形态阈值。"""
    if has_closed_sibling:
        return "merge_into_sibling"
    if has_wall_junction and is_repeated:
        return "column_candidate"
    return "leave_open"
