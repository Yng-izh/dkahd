"""LLM 凭据：一处配置，全链路可用。

取值优先级（先命中先用）：

1. 调用方显式传的 ``api_key`` / ``base`` / ``model``；
2. 环境变量 ``LLM_API_KEY`` / ``LLM_BASE_URL`` / ``LLM_MODEL``（CI、容器、网页版）；
3. 本地配置文件 ``llm.local.json``（技能根目录或当前工作目录）——**不入库**，
   由 :data:`LOCAL_FILE` 描述结构：:

       {"api_key": "sk-...", "base": "http://host:port/v1",
        "model": "cn:deepseek-v4.1-flash", "vision_model": "cn:glm-4.6v"}

4. 都没有 -> 空字符串，各模块按 ``unavailable`` 降级，识别结果与不接模型一致。

为什么要有本地文件这一层
------------------------
识别 DAG 里十个模块各自去读 ``os.environ``，靠 shell ``export`` 传 key 会漏：
桌面版、网页版、批处理脚本各跑各的环境，一漏就静默降级成"纯几何"，而且从
结果上看不出来。凭据收成一个入口之后，"这次到底有没有用模型"就是一个可查
的事实（见 :func:`describe`）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

#: 技能根目录（src/dwg_reader/recognition/ 往上三层）
SKILL_ROOT = Path(__file__).resolve().parents[3]
LOCAL_FILE = "llm.local.json"

_CACHE: dict = {}
_LOADED = False


def _local() -> dict:
    global _LOADED, _CACHE
    if _LOADED:
        return _CACHE
    _LOADED = True
    for folder in (SKILL_ROOT, Path.cwd()):
        path = folder / LOCAL_FILE
        try:
            if path.is_file():
                _CACHE = json.loads(path.read_text(encoding="utf-8")) or {}
                _CACHE["_source"] = str(path)
                break
        except Exception:
            continue
    return _CACHE


def reload() -> None:
    """测试用：丢掉已加载的本地配置。"""
    global _LOADED, _CACHE
    _LOADED, _CACHE = False, {}


def _pick(name: str, env: str, explicit=None) -> str:
    if explicit:
        return str(explicit)
    got = os.environ.get(env) or os.environ.get(env.replace("LLM_", "DWG_LLM_"))
    if got:
        return str(got)
    return str(_local().get(name) or "")


def api_key(explicit: str | None = None) -> str:
    """取 key。``DWG_LLM_DISABLE=1`` 时一律返回空串——用于测试与基线回归，
    保证结果可复现、不依赖网络（基线图不该每次去问模型）。"""
    if os.environ.get("DWG_LLM_DISABLE", "").lower() in ("1", "true", "yes"):
        return ""
    key = _pick("api_key", "LLM_API_KEY", explicit)
    return key


def base(explicit: str | None = None, default: str = "") -> str:
    return _pick("base", "LLM_BASE_URL", explicit) or default


def model(explicit: str | None = None, default: str = "") -> str:
    return _pick("model", "LLM_MODEL", explicit) or default


def vision_model(explicit: str | None = None, default: str = "") -> str:
    return _pick("vision_model", "LLM_VISION_MODEL", explicit) or default


def enabled() -> bool:
    """这次运行到底有没有模型可用（有没有 key）。"""
    return bool(api_key())


def describe() -> dict:
    """可写进报告的一行摘要：有没有 key、base 是哪、配置从哪来。"""
    key = api_key()
    return {"enabled": bool(key),
            "key_tail": ("***" + key[-4:]) if len(key) > 8 else "",
            "base": base(),
            "model": model(),
            "vision_model": vision_model(),
            "source": _local().get("_source") or
                      ("env" if os.environ.get("LLM_API_KEY") else "none")}
