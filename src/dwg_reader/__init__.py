"""Read DWG/DXF drawings: text, geometry, dimensions, components and quantities.

包门面（懒加载）
----------------
本包只依赖标准库 + ``ezdxf``（可选）。**导入本包本身不应该要求装了 ezdxf**：
以前 ``__init__`` 一上来就 ``from .recognition import ...``，而 recognition 又会
``import ezdxf``——结果是"只想用文字解码 / 只想看版本号"的人在没装 ezdxf 的环境
里连 ``import dwg_reader`` 都会炸，而且报错位置离真正的原因很远。

所以这里改成 PEP 562 的懒加载：``from dwg_reader import recognize`` 照旧可用，
但只有真正取用某个名字时才去导入对应子模块。
"""

from __future__ import annotations

import importlib

__version__ = "0.7.1"

#: 公开名字 -> (子模块, 模块内属性名)
_LAZY: dict[str, tuple[str, str]] = {
    "ComponentReport": (".components", "ComponentReport"),
    "identify_components": (".components", "identify_components"),
    "load_rules": (".components", "load_rules"),
    "recognize": (".recognition", "recognize"),
    "run_recognition": (".recognition", "run_recognition"),
    "build_document": (".recognition", "build_document"),
    "render_markdown": (".recognition", "render_markdown"),
    "schema_json": (".recognition", "schema_json"),
    "to_schema": (".recognition", "to_schema"),
    "validate": (".recognition", "validate"),
    "write_outputs": (".recognition", "write_outputs"),
    "DimensionReport": (".dimensions", "DimensionReport"),
    "read_dimensions": (".dimensions", "read_dimensions"),
    "Drawing": (".dxf", "Drawing"),
    "PlacedEntity": (".dxf", "PlacedEntity"),
    "iter_placed": (".dxf", "iter_placed"),
    "parse_dxf": (".dxf", "parse_dxf"),
    "summarize": (".dxf", "summarize"),
    "ShxFont": (".fonts", "ShxFont"),
    "get_font": (".fonts", "get_font"),
    "TextItem": (".layout", "TextItem"),
    "reorder_by_columns": (".layout", "reorder_by_columns"),
    "to_markdown_table": (".layout", "to_markdown_table"),
    "ReadOptions": (".pipeline", "ReadOptions"),
    "ReadResult": (".pipeline", "ReadResult"),
    "read_batch": (".pipeline", "read_batch"),
    "read_drawing": (".pipeline", "read_drawing"),
    "resolve_dxf": (".pipeline", "resolve_dxf"),
    "QuantityReport": (".quantity", "QuantityReport"),
    "build_quantity": (".quantity", "build_quantity"),
    "export": (".quantity", "export"),
    "decode_cad_text": (".text", "decode_cad_text"),
    "decode_text": (".text", "decode_text"),
    "TianzhengReport": (".tianzheng", "TianzhengReport"),
    "detect_tianzheng": (".tianzheng", "detect_tianzheng"),
}

__all__ = sorted(list(_LAZY) + ["__version__"])


def __getattr__(name: str):
    """按需导入（PEP 562）：取到什么才 import 什么。"""
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = target
    try:
        module = importlib.import_module(module_name, __name__)
    except ModuleNotFoundError as exc:                 # 缺 ezdxf 这类可选依赖
        raise ImportError(
            f"使用 dwg_reader.{name} 需要可选依赖（{exc.name}）："
            f"pip install 'dwg-reader-skill[dxf]'") from exc
    value = getattr(module, attr)
    globals()[name] = value                            # 缓存，下次直接命中
    return value


def __dir__():
    return sorted(set(list(globals()) + list(_LAZY)))
