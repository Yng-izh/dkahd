"""AI 增强识别的配置文件：一个 JSON 说清"跑不跑、跑多大、怎么兜底"。

模式（``mode``）是**唯一**的行为开关，且只有三档：

视觉链路（``provider.vision`` / ``allow_remote_images``）
    本地把 ROI 画成一张小图（:mod:`vision`），只发这一块，不发整张图。``vision=false``
    时连渲染都不做，只用结构特征——模型仍然可用，只是"看不见"。

``off``
    纯几何基线。不实例化模型网关、不发任何请求、不加载规则记忆——与改造前
    逐条一致（这是兼容性红线，见 ``tests/test_ai_enhance.py``）。
``propose``
    AI 只出**提议**：候选假设、算子组合、参数区间、失败原因解释。提议绝不合并
    进候选状态，输出里只多一段 ``meta.ai_recognition.proposals``。
``enhance``
    提议 → 受约束编译 → 局部试验 → 影子+全局验收 → 通过才提交。模型仍然不写
    坐标、不写阈值、不执行代码。

预算与安全默认值都偏保守：真跑不动就停，不阻断基础识别（没有 AI 的图仍然是
一张完整的图）。
"""
from __future__ import annotations

import hashlib
import json
import os

from . import llm_credentials as CRED
from . import param_registry as PARAMS

SCHEMA = "ai-recognition-config/1.0"
MODES = ("off", "propose", "enhance")

#: 配置默认值（全部是逻辑预算，不是几何阈值）
DEFAULT_CONFIG: dict = {
    "schema": SCHEMA,
    "mode": "off",
    "provider": {
        "type": "openai-compatible",
        # 端点/模型/密钥全部由显式配置提供：默认**不上传**任何图纸内容，
        # 截图上传默认**直接授权**：本地渲染的 ROI 小图会随请求发给配置好的模型端点。
        # 想关就设 allow_remote_images=false（或在 CLI 用 --no-ai-vision）。
        "base_url_env": "DWG_AI_BASE_URL",
        "api_key_env": "DWG_AI_API_KEY",
        "model_env": "DWG_AI_MODEL",
        "vision_model_env": "DWG_AI_VISION_MODEL",
        # 视觉链路：本地矢量渲染 -> 只把**这一块 ROI 的小图**发给多模态模型。
        # 远端图片必须显式授权；视觉渲染可默认开启，但默认不上传任何图片。
        "vision": True,
        "allow_remote_images": True,
        "timeout_seconds": 60.0,
        "retries": 1,
    },
    "budget": {
        "max_regions": 12,
        "max_model_calls": 12,
        "max_model_rounds_per_case": 2,
        "max_hypotheses_per_case": 3,
        "max_trials_per_case": 12,
        "max_total_trials": 72,
        "max_walltime_seconds": 180,
        "max_entities_per_region": 400,
        "max_views_per_region": 3,
        "max_diagnostic_images": 6,
        "max_images_per_case": 1,
    },
    "safety": {
        "protect_confirmed": True,
        "require_independent_support_for_confirmed": True,
        "global_consistency_check": True,
        "allow_arbitrary_code": False,
    },
    "memory": {"enabled": True, "scope": "project", "directory": ".dkahd/rule-memory"},
}

#: 闭集：出现别的键就是配置写错了（早报错，别静默忽略）
_TOP_KEYS = frozenset(DEFAULT_CONFIG)
_BUDGET_KEYS = frozenset(DEFAULT_CONFIG["budget"])
_SAFETY_KEYS = frozenset(DEFAULT_CONFIG["safety"])
_PROVIDER_KEYS = frozenset(DEFAULT_CONFIG["provider"])
_MEMORY_KEYS = frozenset(DEFAULT_CONFIG["memory"])

#: 写进提示词/日志的版本标记（配置或提示词一改，缓存键与经验键都变）
CONFIG_VERSION = "1.0"


class ConfigError(ValueError):
    """配置非法：带问题清单，不做"尽力而为"的解析。"""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def _deep_merge(base: dict, extra: dict) -> dict:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in (base or {}).items()}
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load(path=None, *, mode: str | None = None, overrides: dict | None = None) -> dict:
    """读配置：默认值 <- 文件 <- 显式覆盖 <- ``mode`` 参数。

    ``path`` 为 ``None`` 时只用默认值（等价于 ``off``）。文件不存在就报错，
    不静默退默认（否则"我明明配了"会变成最难查的那类问题）。
    """
    cfg = _deep_merge(DEFAULT_CONFIG, {})
    if path:
        raw = json.loads(open(path, encoding="utf-8").read())
        if not isinstance(raw, dict):
            raise ConfigError([f"配置文件顶层必须是对象: {path}"])
        cfg = _deep_merge(cfg, raw)
    if overrides:
        cfg = _deep_merge(cfg, dict(overrides))
    if mode:
        cfg["mode"] = str(mode)
    problems = validate(cfg)
    if problems:
        raise ConfigError(problems)
    cfg["digest"] = digest(cfg)
    return cfg


def validate(cfg: dict) -> list[str]:
    """结构 + 闭集 + 数值范围校验。返回问题清单（空表示合法）。"""
    problems: list[str] = []
    if not isinstance(cfg, dict):
        return ["配置必须是 JSON 对象"]
    for key in cfg:
        if key in ("digest", "schema"):
            continue
        if key not in _TOP_KEYS:
            problems.append(f"未知配置项: {key}")
    if cfg.get("mode") not in MODES:
        problems.append(f"mode 必须是 {list(MODES)} 之一，当前 {cfg.get('mode')!r}")
    for group, allowed in (("budget", _BUDGET_KEYS), ("safety", _SAFETY_KEYS),
                           ("provider", _PROVIDER_KEYS), ("memory", _MEMORY_KEYS)):
        sec = cfg.get(group)
        if sec is None:
            continue
        if not isinstance(sec, dict):
            problems.append(f"{group} 必须是对象")
            continue
        for key in sec:
            if key not in allowed:
                problems.append(f"未知{group}项: {key}")
    budget = cfg.get("budget") or {}
    for key, value in budget.items():
        if key not in _BUDGET_KEYS:
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            problems.append(f"budget.{key} 必须是非负数")
    if float(budget.get("max_total_trials", 0) or 0) < float(
            budget.get("max_trials_per_case", 0) or 0):
        problems.append("budget.max_total_trials 不能小于 max_trials_per_case")
    safety = cfg.get("safety") or {}
    if safety.get("allow_arbitrary_code"):
        # 这是硬红线：模型永远不能执行任意代码
        problems.append("safety.allow_arbitrary_code 必须为 false")
    if str((cfg.get("provider") or {}).get("type") or "") != "openai-compatible":
        problems.append("目前只支持 provider.type = openai-compatible")
    mem = cfg.get("memory") or {}
    if mem.get("scope") not in (None, "project", "drawing", "user"):
        problems.append("memory.scope 只能是 project / drawing / user")
    return problems


def digest(cfg: dict) -> str:
    """配置指纹：只取会改变行为的部分（不含 digest 自身）。"""
    body = {k: v for k, v in (cfg or {}).items() if k != "digest"}
    body["param_registry"] = PARAMS.summary()["count"]
    raw = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def credentials(cfg: dict) -> dict:
    """端点 / 密钥 / 模型名：按配置里点名的环境变量取，再退到统一凭据层。"""
    prov = (cfg or {}).get("provider") or {}
    key = CRED.api_key(os.environ.get(prov.get("api_key_env") or "DWG_AI_API_KEY"))
    base = CRED.base(os.environ.get(prov.get("base_url_env") or "DWG_AI_BASE_URL"))
    model = CRED.model(os.environ.get(prov.get("model_env") or "DWG_AI_MODEL"))
    vision = CRED.vision_model(
        os.environ.get(prov.get("vision_model_env") or "DWG_AI_VISION_MODEL"))
    return {"api_key": key, "base_url": base, "model": model,
            "vision_model": vision,
            "allow_remote_images": bool(prov.get("allow_remote_images"))}


def enabled(cfg: dict) -> bool:
    """是否真的需要模型（``off`` 一律不需要，哪怕配了 key）。"""
    return bool(cfg) and str(cfg.get("mode") or "off") != "off"


def summary(cfg: dict) -> dict:
    """进 ``meta.ai_recognition`` 的配置摘要：**不含密钥**。"""
    if not cfg:
        return {"schema": SCHEMA, "mode": "off"}
    cred = credentials(cfg)
    prov = dict(cfg.get("provider") or {})
    return {"schema": SCHEMA, "mode": cfg.get("mode") or "off",
            "config_version": CONFIG_VERSION, "digest": cfg.get("digest") or digest(cfg),
            "budget": dict(cfg.get("budget") or {}),
            "safety": dict(cfg.get("safety") or {}),
            "memory": {"enabled": bool((cfg.get("memory") or {}).get("enabled")),
                       "scope": (cfg.get("memory") or {}).get("scope")},
            "provider": {"type": (cfg.get("provider") or {}).get("type"),
                         "model": cred["model"] or "",
                         "vision_model": cred["vision_model"] or "",
                         "vision": bool(prov.get("vision", True)),
                         "allow_remote_images": cred["allow_remote_images"],
                         # 只说"有没有配"，绝不输出密钥内容
                         "credentials": "configured" if cred["api_key"] else "missing"}}
