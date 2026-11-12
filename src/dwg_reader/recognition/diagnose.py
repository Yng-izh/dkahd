"""失败样本诊断（离线）：把"没被解释的图元"整理成算子扩展的输入。

用途（``architecture.md`` §4.5）：当新图纸出现现有算子覆盖不了的画法时，

1. 确定性算子已产出未决候选与诊断（本模块整理它们）；
2. 按图元类型 / 图层语义 / 几何统计给出"可能由哪个已有算子解释"的假设；
3. 假设拿不准时，把结构化上下文交给离线 LLM 提新 profile / 新算子组合；
4. 仍然无法表达时，由人起草新算子代码，跑回归测试后再进代码库。

本模块**运行期不参与识别**，不产出坐标，不写文件；LLM 也不会把代码直接投进
生产管线。
"""
from __future__ import annotations

import collections
import json

from . import primitives as PRIM
from .operators import OPERATORS

#: 一个图层至少这么多图元才值得进"未解释"清单
MIN_GROUP = 12
#: 按图元类型给"可能解释得通"的算子（只是假设，需验证）
HYPOTHESIS_BY_TYPE = {
    "LINE": ("line_bundle", "parallel_pair"),
    "LWPOLYLINE": ("closed_contour", "line_bundle"),
    "POLYLINE": ("closed_contour", "line_bundle"),
    "ARC": ("arc_feature",),
    "CIRCLE": ("arc_feature",),
    "INSERT": ("arc_feature", "closed_contour"),
}


def _bucket(unrecognized):
    """按 (图层, 类型) 归并未识别图元。"""
    out = collections.defaultdict(list)
    for item in unrecognized:
        out[(item.get("layer"), item.get("type"))].append(item)
    return out


def _prims(snapshot):
    prims = PRIM.extract(snapshot)
    return prims, PRIM.summary(prims)


def analyze(result: dict, *, snapshot: dict | None = None,
            profile: dict | None = None, top: int = 15) -> dict:
    """整理一次识别结果的失败面（纯函数，输入是 ``recognize()`` 的输出）。"""
    meta = result.get("meta") or {}
    comps = result.get("components") or []
    unrec = result.get("unrecognized") or []
    buckets = _bucket(unrec)

    groups = []
    for (layer, kind), items in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        if len(items) < MIN_GROUP:
            continue
        sem = None
        if profile:
            sem = ((profile.get("layers") or {}).get(layer) or {}).get("semantics")
        groups.append({
            "layer": layer, "type": kind, "count": len(items),
            "semantics": sem,
            "reasons": dict(collections.Counter(i.get("reason") for i in items).most_common(3)),
            "operator_hypotheses": [h for h in HYPOTHESIS_BY_TYPE.get(kind, ())
                                    if h in OPERATORS],
            "sample_handles": [i.get("handle") for i in items[:5]],
        })

    openings = meta.get("openings") or {}
    report = {
        "source": meta.get("source"),
        "counts": meta.get("counts") or dict(collections.Counter(
            c.get("category") for c in comps)),
        "unrecognized": len(unrec),
        "unrecognized_groups": groups[:top],
        "openings": openings,
        "spaces": meta.get("spaces") or {},
        "units": {"units": meta.get("units"), "resolved": meta.get("units_resolved"),
                  "evidence": meta.get("units_evidence")},
        "scope": meta.get("scope") or {},
        "unreviewed_layers": (profile or {}).get("unreviewed") or [],
        "next_actions": [],
    }

    if groups:
        report["next_actions"].append(
            "对未解释分组先用现有算子组合试探（operator_hypotheses），"
            "能解释就写进 profile.operator_hints，不改代码")
    pending = (openings.get("pending") or {})
    if pending.get("doors"):
        report["next_actions"].append(
            f"{pending['doors']} 樘可信块门缺墙体归属：先修墙网/空间面域，不要放宽门的判定")
    if (openings.get("space_evidence") or {}).get("unavailable"):
        report["next_actions"].append("空间面域不可用：检查 walls_seg 是否为空或区域裁剪是否过大")
    if groups and all(not g["operator_hypotheses"] for g in groups):
        report["next_actions"].append(
            "现有算子都无法解释：进入人工起草新算子 + 回归测试流程（LLM 只起草，不入生产）")

    if snapshot is not None:
        _pr, summary = _prims(snapshot)
        report["primitives"] = summary
    return report


def prompt_context(report: dict) -> str:
    """把诊断结果压成给离线 LLM 的上下文（不含坐标、不含阈值）。"""
    keep = {k: report.get(k) for k in
            ("source", "counts", "unrecognized", "unrecognized_groups",
             "openings", "spaces", "units", "unreviewed_layers")}
    return json.dumps(keep, ensure_ascii=False, indent=1)
