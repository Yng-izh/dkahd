"""识别档位（``recognition_profile``）：这一趟到底要识别什么、愿意付多少代价。

为什么要有档位
--------------
旧链路把"识别一张图"当成一件事：48 个节点从头跑到尾，房间是主流程的一部分。
可下游真正要的是**门、窗、柱**——它们有明确实例、能算量、能核对。房间是派生
信息（墙网围出来的面域），墙是上下文（门窗的宿主、柱的结构位置）。

所以档位只做一件事：把"主输出 / 上下文 / 可选派生"划清，并据此决定

1. 旧 DAG 要跑到哪几个节点（:data:`DAG_TARGETS`）——focused 档不跑房间面域、
   房间路由、装配与导出；
2. v2 里房间要不要算（``rooms``）；
3. 走哪条求解器（``resolver``：``component_focus`` 门窗柱专用 / ``staged`` 通用）；
4. 首轮视觉理解看多少个构件族（``detector_families``）。

档位不是"新参数越多越好"：它只有三档，且每一档都必须能说清"少算了什么"。
"""
from __future__ import annotations

SCHEMA = "recognition-profile/1.0"

#: 主输出构件：这三类的实例级结果就是"这次识别交付了什么"
TARGET_CATEGORIES = ("door", "window", "column")

#: 上下文构件：只用来定位与约束（宿主墙、轴网位），不算主输出的召回
CONTEXT_CATEGORIES = ("wall", "grid_axis")

#: 可选派生：默认关，开着就要付代价（面域栅格化 / 楼梯台阶成组扫描）
OPTIONAL_CATEGORIES = ("room", "stair", "step", "apron", "frame", "north_arrow")

#: 每档跑哪些旧 DAG 节点。不是"多跑几个"，而是"**只**跑这几类需要的"：
#: focused 档里没有 assemble / export / rooms / space_routing / room_labels。
DAG_TARGETS = {
    # 门窗柱专用：装载 + 墙体上下文 + 柱 + 门窗候选 + 残余扫描（给族发现用种子）
    "openings_columns": ("texts", "walls_seg", "wall_model", "columns_verify",
                         "doors", "windows", "openings_select", "walls_merge",
                         "grid", "pattern_discovery"),
    # 更精简的一档：连洞口语义裁决（openings_select）都不跑，直接用候选节点。
    # 计数口径会与导出口径不同，只在"只想看候选"时用。
    "openings_columns_lean": ("texts", "walls_seg", "walls_merge",
                              "columns_verify", "doors", "windows"),
    # 旧行为：一条链跑到 assemble（房间在链里）
    "full": ("assemble",),
}

PROFILES = {
    "openings_columns": {
        "schema": SCHEMA, "profile": "openings_columns",
        "targets": TARGET_CATEGORIES,
        "context": CONTEXT_CATEGORIES,
        "optional": (),
        "rooms": False,
        "residual_scan": True,
        "families": True,
        "detector": True,
        "propagate": True,
        "resolver": "component_focus",
        # ``detector_families``：**至少**看几个族（要覆盖各类别）；
        # ``detector_max_families``：最多看几个族（真正的上限，受调用预算约束）。
        # 以前只用一个数当循环终点，第 4 个族看完就收工，剩下 9 个柱候选族和
        # 3 次未用调用一起浪费掉。
        "detector_families": 4,
        "detector_max_families": 8,
        "bootstrap_calls": 1,          # 只看全图概览；族语义交给 detector
        "walltime_seconds": 600,
        "propagate_confidence": "high",   # 模型达不到这个把握就不许推广
        "arbitration": True,              # 逻辑冲突交给模型按位置裁决
        "arbitration_points": 4,          # 每次最多裁几个冲突点
        "images_per_family": 4,           # 代表紧视图+细节图，再加两个有差异的成员
        "family_samples": 3,              # 每族看几个实例（代表 + 2 个不同成员）
        "family_closeup": 1,              # 模型说看不清时允许补看几次
        "family_overview": True,          # 另加一张全部成员的分布总览图（同一次调用）
        # 成员数达到这个规模的族，推广影响大：必须两次独立判读一致才允许整族推广
        "corroborate_instances": 8,
        "note": "门窗柱为主输出；墙/轴网只做宿主与几何约束；房间退出主流程",
    },
    "openings_columns_lean": {
        "schema": SCHEMA, "profile": "openings_columns_lean",
        "targets": TARGET_CATEGORIES,
        "context": CONTEXT_CATEGORIES,
        "optional": (),
        "rooms": False,
        "residual_scan": False,
        "families": True,
        "detector": True,
        "propagate": True,
        "resolver": "component_focus",
        "detector_families": 3,
        "detector_max_families": 6,
        "bootstrap_calls": 1,
        "walltime_seconds": 480,
        "propagate_confidence": "high",
        "arbitration": True,
        "arbitration_points": 3,
        "images_per_family": 3,
        "family_samples": 2,
        "family_closeup": 1,
        "family_overview": True,
        "corroborate_instances": 8,
        "note": "同上，但不跑洞口语义裁决与残余扫描：更快，计数口径是候选口径",
    },
    "full": {
        "schema": SCHEMA, "profile": "full",
        "targets": TARGET_CATEGORIES + OPTIONAL_CATEGORIES,
        "context": CONTEXT_CATEGORIES,
        "optional": OPTIONAL_CATEGORIES,
        "rooms": True,
        "residual_scan": True,
        "propagate_confidence": "high",
        "images_per_family": 2,
        "families": False,
        "detector": False,
        "propagate": False,
        "resolver": "staged",
        "detector_families": 0,
        "bootstrap_calls": 5,
        "note": "旧 v2 行为：11 类都算，房间计入口径",
    },
}

DEFAULT = "full"


def resolve(name=None, *, targets=None, context=None, rooms=None,
            profile=None) -> dict:
    """拿到一份**完整**档位配置：未给的就用档位默认值，显式给的覆盖它。"""
    if isinstance(name, dict) and not isinstance(profile, dict):
        profile, name = name, name.get("profile")
    base = dict(PROFILES.get(str(name or (profile or {}).get("profile") or DEFAULT))
                or PROFILES[DEFAULT])
    if isinstance(profile, dict):
        for key, value in profile.items():
            if value is not None:
                base[key] = value
    if targets is not None:
        base["targets"] = tuple(targets)
    if context is not None:
        base["context"] = tuple(context)
    if rooms is not None:
        base["rooms"] = bool(rooms)
    base["dag_targets"] = tuple(DAG_TARGETS.get(str(base["profile"]),
                                                DAG_TARGETS[DEFAULT]))
    base["schema"] = SCHEMA
    return base


def focused(profile) -> bool:
    """是否走"门窗柱专用"执行路径（决定跳过房间、改走专用求解器）。"""
    profile = profile if isinstance(profile, dict) else resolve(profile)
    return str(profile.get("profile") or DEFAULT) != "full"


def categories(profile) -> tuple:
    """这一档最终会有哪些类别的候选进池子（主输出 + 上下文 + 可选）。"""
    profile = profile if isinstance(profile, dict) else resolve(profile)
    return tuple(profile.get("targets") or ()) + tuple(profile.get("context") or ()) \
        + tuple(profile.get("optional") or ())


def summary(profile) -> dict:
    profile = profile if isinstance(profile, dict) else resolve(profile)
    return {"schema": SCHEMA, "profile": profile.get("profile"),
            "targets": list(profile.get("targets") or ()),
            "context": list(profile.get("context") or ()),
            "optional": list(profile.get("optional") or ()),
            "rooms": bool(profile.get("rooms")),
            "resolver": profile.get("resolver"),
            "dag_targets": list(profile.get("dag_targets") or ()),
            "families": bool(profile.get("families")),
            "propagate": bool(profile.get("propagate")),
            "detector": bool(profile.get("detector")),
            "observation": {key: profile.get(key) for key in (
                "detector_families", "detector_max_families", "bootstrap_calls",
                "images_per_family", "family_samples", "family_closeup", "family_overview",
                "corroborate_instances", "propagate_confidence", "arbitration",
                "arbitration_points", "walltime_seconds")},
            "note": profile.get("note")}


__all__ = ["DEFAULT", "DAG_TARGETS", "PROFILES", "SCHEMA", "categories", "focused",
           "resolve", "summary"]
