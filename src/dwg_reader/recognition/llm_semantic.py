"""可选运行时 LLM 节点：房间名归一化（N3）与复核说明（N4）。

默认不进入识别 DAG，由调用方显式启用；只发送文字清单，不发送几何坐标。
"""
from __future__ import annotations

import json
import os

from . import llm_credentials as CRED
from .llm_profile import DEFAULT_BASE, DEFAULT_MODEL, call_llm

_ROOM_PROMPT = """下面是建筑图纸里的房间名清单。请给出归一化映射：
- 中文保留，英文/缩写翻译成中文（如 OFFICE→办公室、WC→卫生间）
- 明显不是房间名的（做法说明、材料、编号）标记为 null
只输出 JSON：{"原名": "标准名或 null"}。

房间名清单：
%s
"""

_REVIEW_PROMPT = """下面是构件识别中标记为 uncertain 的记录摘要。请给出复核建议：
每条给出原因判断与复核优先级（high/medium/low），不改动识别结果。
只输出 JSON：[{"id":"...","why":"...","priority":"high|medium|low"}]

记录：
%s
"""


def _key(api_key: str | None) -> str:
    return CRED.api_key(api_key)


def normalize_room_names(names, *, api_key: str | None = None,
                         base: str = DEFAULT_BASE, model: str = DEFAULT_MODEL) -> dict:
    """N3：房间名归一化。无 key 时原样返回空映射（调用方保持原名）。"""
    key = _key(api_key)
    if not key or not names:
        return {}
    raw = call_llm(_ROOM_PROMPT % json.dumps(list(names), ensure_ascii=False),
                   api_key=key, base=base, model=model)
    out = {}
    for src, dst in (raw or {}).items():
        if src in names and (dst is None or isinstance(dst, str)):
            out[src] = dst
    return out


def review_notes(records, *, api_key: str | None = None,
                 base: str = DEFAULT_BASE, model: str = DEFAULT_MODEL) -> list[dict]:
    """N4：uncertain 记录复核说明（旁路，不改识别输出）。"""
    key = _key(api_key)
    if not key or not records:
        return []
    payload = [{"id": r.get("id"), "category": r.get("category"),
                "name": r.get("name"), "evidence": (r.get("evidence") or {}).get("detail"),
                "confidence": r.get("confidence")} for r in records[:60]]
    raw = call_llm(_REVIEW_PROMPT % json.dumps(payload, ensure_ascii=False),
                   api_key=key, base=base, model=model)
    return raw if isinstance(raw, list) else []
