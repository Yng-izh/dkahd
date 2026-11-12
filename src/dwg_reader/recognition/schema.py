"""统一输出文档的 JSON Schema（dwg-components-records/1.1）与轻量校验器。

设计原则：
  - **一个文档装下全部结果**：meta / basemap / components / unrecognized /
    unmatched_blocks / scope / notes，不再拆成多个互相不兼容的 JSON。
  - **构件记录同时带模型坐标与底图像素坐标**，下游既能叠图也能回到 CAD 定位。
  - 只依赖标准库：schema 以 Python 字面量保存，`validate()` 支持本文件用到的
    关键字子集（type / required / properties / items / enum / minimum / maximum /
    minItems / maxItems / additionalProperties），无 jsonschema 依赖。
"""

from __future__ import annotations

import json
from typing import Any

SCHEMA_ID = "dwg-components-records/1.1"

#: 构件类别（中文，与参考交付文件一致）。新增类别时同步这里。
CATEGORIES = [
    "图框", "轴线", "轴网交点", "指北针", "柱", "墙",
    "门", "窗", "房间", "楼梯", "台阶", "散水",
]

_NUM2 = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}
_NUM4 = {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4}
_INT4 = {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4}
_INT2 = {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2}
_PT = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}

COMPONENT_SCHEMA: dict[str, Any] = {
    "title": "ComponentRecord",
    "description": "一个构件实例。块类构件 anchor=insert，几何推导构件 anchor=geometry。",
    "type": "object",
    "required": ["id", "category", "anchor", "bbox_model", "bbox_px", "bbox_center_px"],
    "properties": {
        "id": {"type": "string", "description": "类别内唯一编号，如 WALL-0001"},
        "category": {"type": "string", "enum": CATEGORIES},
        "name": {"type": ["string", "null"]},
        "block": {"type": ["string", "null"], "description": "块名；几何推导构件为 null"},
        "layer": {"type": "string"},
        "anchor": {"type": "string", "enum": ["insert", "geometry"]},
        "depth": {"type": "integer", "minimum": 0, "description": "块嵌套深度，非块为 0"},
        "insert_pt": _PT,
        "rotation_deg": {"type": "number", "description": "DXF 原始值，未归一化"},
        "scale": _NUM2,
        "bbox_model": _NUM4,
        "bbox_px": _INT4,
        "bbox_center_px": _INT2,
        "dxf_handle": {"type": ["string", "null"]},
        "source_handles": {"type": "array", "items": {"type": "string"}},
        "source_entities": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["handle"],
                "properties": {
                    "handle": {"type": "string"},
                    "type": {"type": "string"},
                    "layer": {"type": "string"},
                    "role": {"type": "string"},
                },
            },
        },
        "geometry": {
            "type": "object",
            "description": "几何明细：中心线/轮廓/包围盒/面积等，随类别而异",
        },
        "attributes": {"type": "object", "description": "类别相关的属性键值"},
        "evidence": {
            "type": "object",
            "properties": {
                "rule": {"type": "string"},
                "detail": {"type": "string"},
            },
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "status": {"type": "string", "enum": ["confirmed", "uncertain"]},
        "opening": {"type": "array", "items": _PT, "minItems": 2, "maxItems": 2,
                    "description": "墙上的洞口段（门/窗），两点"},
        "axis": {"type": "array", "items": _PT, "minItems": 2, "maxItems": 2},
    },
    "additionalProperties": True,
}

UNRECOGNIZED_SCHEMA: dict[str, Any] = {
    "title": "UnrecognizedEntity",
    "description": "未归入任何构件的原始图元，附原因。",
    "type": "object",
    "required": ["handle", "type", "reason"],
    "properties": {
        "handle": {"type": "string"},
        "type": {"type": "string"},
        "layer": {"type": "string"},
        "bbox": {"type": ["array", "null"], "items": {"type": "number"}},
        "reason": {"type": "string"},
    },
    "additionalProperties": True,
}

DOCUMENT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": SCHEMA_ID,
    "title": "DWG component recognition result",
    "description": "单张图纸的构件识别统一输出文档。",
    "type": "object",
    "required": ["schema", "meta", "components"],
    "properties": {
        "schema": {"type": "string", "const": SCHEMA_ID},
        "meta": {
            "type": "object",
            "required": ["source_file", "dxf_version", "tool", "method", "units",
                         "total_records", "by_type"],
            "properties": {
                "source_file": {"type": "string"},
                "parsed_file": {"type": "string"},
                "dxf_version": {"type": "string"},
                "created_at": {"type": "string"},
                "tool": {"type": "string"},
                "method": {"type": "string"},
                "units": {"type": "string"},
                "extents": _NUM4,
                "counting_rule": {"type": "string"},
                "total_records": {"type": "integer", "minimum": 0},
                "by_type": {"type": "object", "additionalProperties": {"type": "integer"}},
                "counts": {"type": "object", "additionalProperties": {"type": "integer"}},
                "grid": {"type": "object"},
                "wall_diagnostics": {"type": "object"},
                "unrecognized_count": {"type": "integer", "minimum": 0},
                "crosscheck": {"type": "object"},
            },
            "additionalProperties": True,
        },
        "basemap": {
            "type": "object",
            "required": ["width", "height", "transform"],
            "properties": {
                "id": {"type": "string"},
                "version": {"type": "string"},
                "file": {"type": ["string", "null"]},
                "width": {"type": "integer", "minimum": 1},
                "height": {"type": "integer", "minimum": 1},
                "sha256": {"type": ["string", "null"]},
                "style": {"type": "string"},
                "generated_from": {"type": "string"},
                "transform": {
                    "type": "object",
                    "required": ["basemap_width", "basemap_height", "world",
                                 "scale_px_per_unit", "padding_ratio"],
                    "properties": {
                        "basemap_width": {"type": "integer"},
                        "basemap_height": {"type": "integer"},
                        "world": {
                            "type": "object",
                            "required": ["xmin", "ymin", "xmax", "ymax"],
                            "properties": {
                                "xmin": {"type": "number"}, "ymin": {"type": "number"},
                                "xmax": {"type": "number"}, "ymax": {"type": "number"},
                            },
                        },
                        "scale_px_per_unit": {"type": "number", "minimum": 0},
                        "padding_ratio": {"type": "number", "minimum": 0},
                        "formula": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "additionalProperties": True,
                },
            },
            "additionalProperties": True,
        },
        "components": {"type": "array", "items": COMPONENT_SCHEMA},
        "unrecognized": {"type": "array", "items": UNRECOGNIZED_SCHEMA},
        "unmatched_blocks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["block", "count"],
                "properties": {
                    "block": {"type": "string"},
                    "count": {"type": "integer", "minimum": 0},
                    "note": {"type": "string"},
                },
            },
        },
        "unmatched_total": {"type": "integer", "minimum": 0},
        "scope": {"type": "object"},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": True,
}

_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool,
    "number": (int, float), "integer": int, "null": type(None),
}


def _type_ok(value: Any, expected: Any) -> bool:
    names = expected if isinstance(expected, list) else [expected]
    for name in names:
        py = _TYPES.get(name)
        if py is None:
            continue
        if name == "integer" and isinstance(value, bool):
            continue
        if name == "number" and isinstance(value, bool):
            continue
        if isinstance(value, py):
            return True
    return False


def _validate(value: Any, schema: dict, path: str, errors: list[str]) -> None:
    t = schema.get("type")
    if t and not _type_ok(value, t):
        errors.append(f"{path}: 期望 {t}，实际 {type(value).__name__}")
        return
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: 期望常量 {schema['const']!r}，实际 {value!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} 不在枚举 {schema['enum']} 内")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} < 最小 {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} > 最大 {schema['maximum']}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: 元素数 {len(value)} < {schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: 元素数 {len(value)} > {schema['maxItems']}")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(value):
                _validate(item, item_schema, f"{path}[{i}]", errors)
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: 缺少必填字段 {key!r}")
        props = schema.get("properties", {})
        for key, sub in props.items():
            if key in value:
                _validate(value[key], sub, f"{path}.{key}", errors)
        extra = schema.get("additionalProperties")
        if extra is False:
            for key in value:
                if key not in props:
                    errors.append(f"{path}: 不允许的字段 {key!r}")
        elif isinstance(extra, dict):
            for key, sub in value.items():
                if key not in props:
                    _validate(sub, extra, f"{path}.{key}", errors)


def validate(doc: dict) -> dict:
    """校验统一文档；返回 ``{"ok": bool, "errors": [...]}``。"""
    errors: list[str] = []
    _validate(doc, DOCUMENT_SCHEMA, "$", errors)
    return {"ok": not errors, "errors": errors}


def schema_json() -> str:
    """导出 schema 文件内容（供 `dwg-reader components --emit-schema` 使用）。"""
    return json.dumps(DOCUMENT_SCHEMA, ensure_ascii=False, indent=1)
