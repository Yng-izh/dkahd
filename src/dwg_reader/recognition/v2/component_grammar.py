"""构件几何语法：让模型描述"构件由哪些部分、怎么组合"，而不是报坐标。

为什么需要这一层
----------------
旧版里，模型只能在**已有的算子组合**里挑（``group_line_bundle`` /
``pair_parallel`` / ``extract_contour``…）。遇到算子表达不出来的画法——比如六条
线组成的框架窗、两端带连接件的分格窗——调参再多也没用：**表达力**不够，不是
参数不够。

构件几何语法把"表达力"补上，但补得很窄：

* 部分（parts）只能从**封闭原语**里选：平行线对、重复线束、短连接件、闭合轮廓、
  圆弧串、实心四边形、填充区域、文字锚点、共线串、刻度对；
* 关系（relations）只能从**封闭关系**里选：inside / connect / aligned_with /
  parallel_with / symmetric_about / enclose / adjacent_to / repeats_along；
* 拟合方式（fit_mode）只有两种：约束几何拟合、只给包围盒；
* 实例范围（instances）：单个，或"在整图上找同样的"。

模型能做的**新**事情是"组合"，不能做的是"造几何"。真正的坐标、尺寸、轮廓仍由
:mod:`geometry_fitter` 从原始图元算出来；要新几何运算就得先在
:mod:`operator_specs` 里注册并测过，模型拿不到执行入口。
"""
from __future__ import annotations

import hashlib
import json

SCHEMA = "component-grammar/1.0"

#: 可以出现在 grammar.category 里的构件类别（闭集，与识别器 category 对齐）
CATEGORIES = ("column", "wall", "door", "window", "room", "stair", "step",
              "apron", "frame", "north_arrow", "grid_axis")

#: 封闭几何原语：名字 -> 需要的输入（只用来说明，拟合由 fitter 实现）
PRIMITIVES = {
    "parallel_line_pair": "两条平行线（外框/墙面）",
    "repeated_line_bundle": "≥3 条平行等距线（分格/踏面）",
    "short_connectors": "两端短线段（端头、连接件）",
    "closed_contour": "闭合多段线轮廓",
    "arc_run": "首尾相接的圆弧串（门扇开启弧）",
    "solid_quad": "实心四边形（构造柱/柱墩）",
    "hatch_region": "填充边界（实心墙/柱）",
    "text_anchor": "构件旁的编号/名称文字",
    "collinear_run": "共线且相接的长线串（墙线）",
    "tick_pair": "成对的短刻度线（窗编号/标高符号）",
}

#: 封闭关系：关系名 -> (允许的左值域, 右值域)
RELATIONS = {
    "inside": ("part", "part"),
    "connect": ("part", "part"),
    "aligned_with": ("part", "anchor"),
    "parallel_with": ("part", "part"),
    "symmetric_about": ("part", "part"),
    "enclose": ("part", "part"),
    "adjacent_to": ("part", "anchor"),
    "repeats_along": ("part", "part"),
}

#: 关系里可以指的特殊锚点（不是 part，而是图里的东西）
ANCHORS = ("wall", "space", "text", "column")

FIT_MODES = ("constrained_geometry", "bbox_only")
INSTANCE_MODES = ("single", "find_similar")

GRAMMAR_KEYS = frozenset({"category", "subtype", "parts", "relations", "fit_mode",
                          "instances", "note"})

#: 单份语法的预算：部分数、关系数、part 名长度
LIMITS = {"parts": 6, "relations": 10, "name_chars": 24, "note_chars": 120}

ERROR_CODES = ("NOT_AN_OBJECT", "UNKNOWN_FIELD", "UNKNOWN_CATEGORY", "UNKNOWN_PRIMITIVE",
               "UNKNOWN_RELATION", "BAD_RELATION", "UNKNOWN_ANCHOR", "UNKNOWN_PART",
               "TOO_MANY_PARTS", "TOO_MANY_RELATIONS", "NO_PARTS", "UNKNOWN_FIT_MODE",
               "UNKNOWN_INSTANCE_MODE", "MODEL_GEOMETRY")


class GrammarError(ValueError):
    def __init__(self, code: str, **detail):
        self.code = code if code in ERROR_CODES else "NOT_AN_OBJECT"
        self.detail = dict(detail)
        super().__init__(code + (":" + json.dumps(detail, ensure_ascii=False,
                                                  sort_keys=True) if detail else ""))

    def as_dict(self) -> dict:
        return {"code": self.code, **self.detail}


# --------------------------------------------------------------------- 内置模板
#: 这些不是"识别规则"，是**语法默认值**：没有模型时也能拟合，有模型时可以改
TEMPLATES: dict[str, dict] = {
    "window.parallel_frame": {
        "category": "window", "subtype": "punched_window",
        "parts": {"frame": "parallel_line_pair", "ends": "short_connectors"},
        "relations": [["ends", "connect", "frame"], ["frame", "aligned_with", "wall"]],
        "fit_mode": "constrained_geometry", "instances": "find_similar",
        "note": "墙洞里的双线窗框，两端有封口",
    },
    "window.line_bundle": {
        "category": "window", "subtype": "line_bundle_unclassified",
        "parts": {"outer": "parallel_line_pair", "inner": "repeated_line_bundle"},
        "relations": [["inner", "inside", "outer"], ["outer", "aligned_with", "wall"]],
        "fit_mode": "constrained_geometry", "instances": "find_similar",
        "note": "外框 + 中间分格（幕墙分格窗的典型画法）",
    },
    "door.arc_swing": {
        "category": "door", "subtype": "swing_door",
        "parts": {"arc": "arc_run", "leaf": "collinear_run"},
        "relations": [["arc", "connect", "leaf"], ["leaf", "adjacent_to", "wall"]],
        "fit_mode": "constrained_geometry", "instances": "find_similar",
        "note": "门扇直线 + 开启弧",
    },
    "column.solid_or_contour": {
        "category": "column", "subtype": "rect_column",
        "parts": {"body": "closed_contour"},
        "relations": [["body", "adjacent_to", "wall"]],
        "fit_mode": "constrained_geometry", "instances": "find_similar",
        "note": "闭合轮廓（含缺边的柱），长宽比受限",
    },
    "column.fill_region": {
        "category": "column", "subtype": "irregular_column",
        "parts": {"body": "hatch_region"},
        "relations": [["body", "adjacent_to", "wall"]],
        "fit_mode": "constrained_geometry", "instances": "find_similar",
        "note": "填充边界表达的柱/墩",
    },
    "wall.double_line": {
        "category": "wall", "subtype": "double_line_wall",
        "parts": {"face": "parallel_line_pair", "run": "collinear_run"},
        "relations": [["run", "parallel_with", "face"], ["face", "aligned_with", "wall"]],
        "fit_mode": "constrained_geometry", "instances": "single",
        "note": "双线墙：两面线 + 共线延伸",
    },
    "stair.tread_series": {
        "category": "stair", "subtype": "tread_series",
        "parts": {"treads": "repeated_line_bundle"},
        "relations": [],
        "fit_mode": "bbox_only", "instances": "single",
        "note": "等间距踏面线",
    },
    "step.nested_u": {
        "category": "step", "subtype": "nested_u",
        "parts": {"risers": "repeated_line_bundle"},
        "relations": [],
        "fit_mode": "bbox_only", "instances": "single",
        "note": "嵌套 U 形台阶线",
    },
}

#: 形态案例种类 -> 默认模板（无模型时的兜底）
TEMPLATE_FOR_KIND = {
    "line_bundle": "window.line_bundle",
    "repeated_block": "column.solid_or_contour",
    "open_contour": "column.solid_or_contour",
    "residual_cluster": "window.line_bundle",
}

#: 构件类别 -> 默认模板（模型给了类别但没给语法时用）
TEMPLATE_FOR_CATEGORY = {
    "window": "window.parallel_frame",
    "door": "door.arc_swing",
    "column": "column.solid_or_contour",
    "wall": "wall.double_line",
    "stair": "stair.tread_series",
    "step": "step.nested_u",
}


def template(name: str) -> dict | None:
    row = TEMPLATES.get(str(name))
    return dict(row, template=str(name)) if row else None


def for_kind(kind: str) -> dict | None:
    return template(TEMPLATE_FOR_KIND.get(str(kind), ""))


def for_category(category: str) -> dict | None:
    return template(TEMPLATE_FOR_CATEGORY.get(str(category), ""))


# --------------------------------------------------------------------- 校验
def _as_relation(rel, parts, index: int) -> list:
    if not isinstance(rel, (list, tuple)) or len(rel) != 3:
        raise GrammarError("BAD_RELATION", index=index)
    left, name, right = (str(rel[0]), str(rel[1]), str(rel[2]))
    if name not in RELATIONS:
        raise GrammarError("UNKNOWN_RELATION", index=index, relation=name)
    domains = RELATIONS[name]
    for token, side, domain in ((left, "left", domains[0]), (right, "right", domains[1])):
        if domain == "part":
            if token not in parts:
                raise GrammarError("UNKNOWN_PART", index=index, side=side, part=token)
        elif domain == "anchor":
            if token not in ANCHORS:
                raise GrammarError("UNKNOWN_ANCHOR", index=index, side=side,
                                   anchor=token)
    return [left, name, right]


def validate(grammar: dict) -> dict:
    """校验一份语法；非法直接抛 :class:`GrammarError`（错误码闭集）。"""
    if not isinstance(grammar, dict):
        raise GrammarError("NOT_AN_OBJECT")
    # Reject model-provided coordinates with a stable, auditable error code.
    # Validate this *before* checking generic unknown keys.
    for forbidden in ("bbox", "geometry", "points", "polygon", "outline",
                      "coordinates", "coords", "x", "y", "thickness", "width"):
        if forbidden in grammar:
            raise GrammarError("MODEL_GEOMETRY", field=forbidden)
    # Canonical grammars already validated by the bootstrap/agent have a
    # schema field. They must remain valid when the candidate engine checks
    # them a second time; otherwise every AI candidate is silently rejected.
    if "schema" in grammar and grammar["schema"] != SCHEMA:
        raise GrammarError("UNKNOWN_FIELD", fields=["schema"])
    extra = sorted(set(grammar) - GRAMMAR_KEYS -
                   {"template", "source", "hash", "schema"})
    if extra:
        raise GrammarError("UNKNOWN_FIELD", fields=extra)
    category = str(grammar.get("category") or "")
    if category not in CATEGORIES:
        raise GrammarError("UNKNOWN_CATEGORY", category=category)
    parts = grammar.get("parts")
    if not isinstance(parts, dict) or not parts:
        raise GrammarError("NO_PARTS")
    if len(parts) > int(LIMITS["parts"]):
        raise GrammarError("TOO_MANY_PARTS", count=len(parts))
    clean_parts: dict[str, str] = {}
    for name, prim in parts.items():
        pname = str(name)[:int(LIMITS["name_chars"])]
        if str(prim) not in PRIMITIVES:
            raise GrammarError("UNKNOWN_PRIMITIVE", part=pname, primitive=str(prim))
        clean_parts[pname] = str(prim)
    rels = grammar.get("relations") or []
    if not isinstance(rels, (list, tuple)):
        raise GrammarError("BAD_RELATION", index=0)
    if len(rels) > int(LIMITS["relations"]):
        raise GrammarError("TOO_MANY_RELATIONS", count=len(rels))
    clean_rels = [_as_relation(rel, clean_parts, i) for i, rel in enumerate(rels)]
    fit_mode = str(grammar.get("fit_mode") or "constrained_geometry")
    if fit_mode not in FIT_MODES:
        raise GrammarError("UNKNOWN_FIT_MODE", fit_mode=fit_mode)
    instances = str(grammar.get("instances") or "single")
    if instances not in INSTANCE_MODES:
        raise GrammarError("UNKNOWN_INSTANCE_MODE", instances=instances)
    out = {"schema": SCHEMA, "category": category,
           "subtype": (str(grammar.get("subtype"))[:40]
                       if grammar.get("subtype") else None),
           "parts": clean_parts, "relations": clean_rels,
           "fit_mode": fit_mode, "instances": instances,
           "note": str(grammar.get("note") or "")[:int(LIMITS["note_chars"])]}
    if grammar.get("template"):
        out["template"] = str(grammar["template"])
    out["hash"] = key(out)
    return out


def from_model(obj: dict) -> dict:
    """模型给的语法 -> 校验后的语法（非法抛错，绝不"尽力而为"）。"""
    return validate(obj)


def key(grammar: dict) -> str:
    """语法指纹：同一份语法在任何图上都应得到同一个键（跨图复用/缓存用）。"""
    body = {"category": (grammar or {}).get("category"),
            "subtype": (grammar or {}).get("subtype"),
            "parts": sorted(((grammar or {}).get("parts") or {}).items()),
            "relations": [list(r) for r in ((grammar or {}).get("relations") or ())],
            "fit_mode": (grammar or {}).get("fit_mode"),
            "instances": (grammar or {}).get("instances")}
    raw = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def catalog() -> dict:
    """给模型看的语法目录（只有名字与含义，没有任何阈值）。"""
    return {"schema": SCHEMA, "categories": list(CATEGORIES),
            "primitives": dict(PRIMITIVES), "relations": dict(RELATIONS),
            "anchors": list(ANCHORS), "fit_modes": list(FIT_MODES),
            "instance_modes": list(INSTANCE_MODES),
            "templates": {name: {"category": row["category"],
                                 "subtype": row["subtype"],
                                 "parts": dict(row["parts"]),
                                 "note": row["note"]}
                          for name, row in TEMPLATES.items()}}


def summary(grammar: dict) -> dict:
    grammar = grammar or {}
    return {"schema": SCHEMA, "category": grammar.get("category"),
            "subtype": grammar.get("subtype"),
            "parts": dict(grammar.get("parts") or {}),
            "relations": [list(r) for r in (grammar.get("relations") or ())],
            "fit_mode": grammar.get("fit_mode"),
            "instances": grammar.get("instances"),
            "template": grammar.get("template"), "hash": grammar.get("hash")}
