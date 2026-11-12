"""构件识别引擎：原生图元规则识别（无 AI、无图像）。

入口::

    from dwg_reader.recognition import recognize, to_schema, write_outputs

    result = recognize("drawing.dxf")        # {meta, components, unrecognized}
    schema = to_schema(result, "drawing.dxf") # dwg-components-records/1.1

编排 API（DAG）::

    from dwg_reader.recognition import run_recognition, build_recognition_dag

    run = run_recognition("drawing.dxf", jobs=4, targets=("rooms",),
                          cache_dir=".cache/dwg")
    run.summary()                            # 逐节点耗时 / 摘要 / 缓存命中
"""

from __future__ import annotations

from .dag import (DAG, DagCycleError, DagError, DagExecutionError, DagNodeError,
                  DagRun, DagValidationError, LiveDocument, Node, NodeContext,
                  NodeStat, canonical_json, clear_cache, engine_fingerprint,
                  is_json_native, output_digest, run_dag)
from .export import (CAT_CN, PREFIX, basemap_transform, build_document, to_schema,
                     write_outputs)
from .nodes import build_recognition_dag
from .registry import lookup as lookup_profile, register as register_profile
from .pipeline import recognize, render_markdown, run_recognition
from .schema import CATEGORIES, DOCUMENT_SCHEMA, SCHEMA_ID, schema_json, validate

__all__ = [
    "recognize",
    "run_recognition",
    "build_recognition_dag",
    "lookup_profile",
    "register_profile",
    "render_markdown",
    "build_document",
    "to_schema",
    "write_outputs",
    "basemap_transform",
    "validate",
    "schema_json",
    "SCHEMA_ID",
    "DOCUMENT_SCHEMA",
    "CATEGORIES",
    "CAT_CN",
    "PREFIX",
    # DAG 执行器
    "DAG",
    "Node",
    "NodeContext",
    "NodeStat",
    "DagRun",
    "run_dag",
    "clear_cache",
    "engine_fingerprint",
    "canonical_json",
    "output_digest",
    "is_json_native",
    "LiveDocument",
    "DagError",
    "DagValidationError",
    "DagCycleError",
    "DagNodeError",
    "DagExecutionError",
]

# ------------------------------------------------------------------ v2 编排层
# v2 是新架构（多路径候选 + 全局联合求解），复用同一套底层算法。
# 用可选导入：缺 Pillow 之类可选依赖时，老路径必须照常可用。
try:                                        # pragma: no cover - 可选依赖路径
    from . import v2  # noqa: F401
except Exception:                           # noqa: BLE001
    v2 = None  # type: ignore
