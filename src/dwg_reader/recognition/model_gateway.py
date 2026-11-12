"""模型网关：一处出口，负责"能不能发、发多大、发出去的是什么"。

识别链路里已经有十来个 ``llm_*.py`` 各自 ``urllib.request``，它们都是"闭集分类"
的小问题。AI 增强不一样：它要带**局部矢量渲染**、要记账、要在预算耗尽时干净地
停下，所以统一走这里。

四条职责（每条都是安全边界，不是封装洁癖）：

1. **显式端点**：没有 ``api_key`` 就是 ``UNAVAILABLE``，不猜、不静默改用别的服务；
2. **脱敏**：审计里只留提示词哈希与长度，绝不记密钥；CAD 的 TEXT/图层名/块名
   是不可信输入，一律经 :func:`untrusted` 包成数据块，不参与指令解析；
3. **图像授权**：``allow_remote_images=False``（默认）时截图**不上传**，改为退化成
   结构特征输入——不是"悄悄传上去再说"；
4. **预算**：调用次数、总时长、图像张数都有上限；超了返回 ``BUDGET_EXHAUSTED``，
   而不是把一个跑了十分钟的图变成半成品。

返回结构永远是 :class:`GatewayResult`（JSON-native），调用方不需要处理异常。
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request

from . import llm_credentials as CRED

SCHEMA = "model-gateway/1.0"

#: 失败原因闭集（会原样进实验记录，供模型修订时阅读）
ERROR_CODES = ("UNAVAILABLE", "BUDGET_EXHAUSTED", "TIMEOUT", "HTTP_ERROR",
               "PARSE_ERROR", "EMPTY_RESPONSE", "TOO_MANY_IMAGES")

#: 请求配额（逻辑预算，不是几何阈值）：图像字节数 / 不可信文本截断长度
LIMITS = {"image_bytes": 6_000_000, "untrusted_chars": 400}


def untrusted(text, limit: int = None) -> dict:
    """把来自图纸的文本包成**数据**：控制字符清掉、超长截断、标明不可信。"""
    limit = int(LIMITS["untrusted_chars"] if limit is None else limit)
    raw = "" if text is None else str(text)
    clean = "".join(ch for ch in raw if ch >= " " or ch in "\t")
    return {"kind": "drawing_data", "trusted": False,
            "text": clean[:limit], "truncated": len(clean) > limit}


class GatewayResult:
    """一次调用的结果（JSON-native，可直接进审计）。"""

    __slots__ = ("ok", "code", "data", "text", "model", "duration_ms",
                 "prompt_hash", "images", "detail")

    def __init__(self, *, ok: bool, code: str = "", data=None, text: str = "",
                 model: str = "", duration_ms: float = 0.0, prompt_hash: str = "",
                 images: int = 0, detail: str = ""):
        self.ok = ok
        self.code = code
        self.data = data
        self.text = text
        self.model = model
        self.duration_ms = duration_ms
        self.prompt_hash = prompt_hash
        self.images = images
        self.detail = detail

    def as_dict(self) -> dict:
        return {"ok": self.ok, "code": self.code, "model": self.model,
                "duration_ms": round(self.duration_ms, 1),
                "prompt_hash": self.prompt_hash, "images": self.images,
                "detail": self.detail, "parsed": self.data is not None}


def _sha12(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:12]


def _extract_json(text: str):
    """从模型输出里抠出 JSON（容忍 ```json 围栏与前后废话）。"""
    t = (text or "").strip()
    if not t:
        return None
    if t.startswith("```"):
        body = t.split("\n", 1)[-1]
        t = body.rsplit("```", 1)[0]
    try:
        return json.loads(t)
    except (TypeError, ValueError):
        pass
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(t[i:j + 1])
        except (TypeError, ValueError):
            return None
    return None


class ModelGateway:
    """OpenAI 兼容端点 + 预算 + 审计。**唯一**的对外网络出口。"""

    def __init__(self, *, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None, vision_model: str | None = None,
                 allow_remote_images: bool = False, timeout: float = 60.0,
                 retries: int = 1, budget: dict | None = None,
                 prompt_version: str = ""):
        self.api_key = CRED.api_key(api_key)
        self.base_url = CRED.base(base_url)
        self.model = CRED.model(model)
        self.vision_model = CRED.vision_model(vision_model) or self.model
        self.allow_remote_images = bool(allow_remote_images)
        self.timeout = float(timeout or 60.0)
        self.retries = max(0, int(retries or 0))
        self.prompt_version = str(prompt_version or "")
        budget = dict(budget or {})
        self.max_calls = int(budget.get("max_model_calls") or 0) or 10 ** 6
        self.max_ms = float(budget.get("max_walltime_seconds") or 0) * 1000.0 or 1e12
        self.calls = 0
        self.spent_ms = 0.0
        self.images_sent = 0
        self.images_dropped = 0
        self.tokens = 0
        self.audit: list[dict] = []
        self._started = time.perf_counter()

    # ---------------------------------------------------------------- 状态
    @property
    def available(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def budget_left(self) -> dict:
        return {"calls": max(0, self.max_calls - self.calls),
                "ms": max(0.0, self.max_ms - self._elapsed_ms()),
                "calls_used": self.calls, "ms_used": round(self._elapsed_ms(), 1)}

    def _elapsed_ms(self) -> float:
        return (time.perf_counter() - self._started) * 1000.0

    def describe(self) -> dict:
        """"这次到底有没有用模型"的可查事实（**不含密钥**）。"""
        return {"schema": SCHEMA, "available": self.available,
                "endpoint": self.base_url, "model": self.model,
                "vision_model": self.vision_model,
                "allow_remote_images": self.allow_remote_images,
                "prompt_version": self.prompt_version,
                "calls": self.calls, "tokens_estimate": self.tokens,
                "images_sent": self.images_sent,
                "images_dropped": self.images_dropped,
                "budget": self.budget_left()}

    # ---------------------------------------------------------------- 调用
    def ask(self, *, system: str, prompt: str, images=(),
            temperature: float = 0.0, vision: bool = False) -> GatewayResult:
        """发一次结构化请求。任何失败都返回 ``ok=False``，不抛异常。"""
        started = time.perf_counter()
        prompt_hash = _sha12(system + "\n" + prompt)
        if not self.available:
            return GatewayResult(ok=False, code="UNAVAILABLE", prompt_hash=prompt_hash,
                                 detail="no credentials")
        left = self.budget_left()
        if left["calls"] <= 0 or left["ms"] <= 0:
            return GatewayResult(ok=False, code="BUDGET_EXHAUSTED",
                                 prompt_hash=prompt_hash, detail=json.dumps(left))

        used_images = 0
        content: list = [{"type": "text", "text": prompt}]
        if images:
            if not self.allow_remote_images:
                # 授权没开：图不上传，只记一笔"想传但没传"，调用照发（纯结构输入）
                self.images_dropped += len(images)
            else:
                if len(images) > 3:
                    return GatewayResult(ok=False, code="TOO_MANY_IMAGES",
                                         prompt_hash=prompt_hash,
                                         detail=str(len(images)))
                for image in images:
                    uri = _image_uri(image)
                    if not uri:
                        self.images_dropped += 1
                        continue
                    content.append({"type": "image_url",
                                    "image_url": {"url": uri}})
                    used_images += 1

        model = self.vision_model if (vision or used_images) else self.model
        body = {"model": model, "temperature": float(temperature),
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": content}]}
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")

        self.calls += 1
        self.images_sent += used_images
        self.tokens += max(1, (len(system) + len(prompt)) // 4)
        last_error = ""
        for attempt in range(self.retries + 1):
            try:
                request = urllib.request.Request(
                    self.base_url.rstrip("/") + "/chat/completions", data=payload,
                    headers={"Content-Type": "application/json",
                             "Authorization": "Bearer " + self.api_key})
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
                text = ((raw.get("choices") or [{}])[0].get("message") or {}).get(
                    "content") or ""
                duration = (time.perf_counter() - started) * 1000.0
                self.spent_ms += duration
                parsed = _extract_json(text)
                self._log(prompt_hash, model, duration, used_images, parsed is not None)
                if not str(text).strip():
                    return GatewayResult(ok=False, code="EMPTY_RESPONSE", text=text,
                                         model=model, duration_ms=duration,
                                         prompt_hash=prompt_hash,
                                         images=used_images)
                if parsed is None:
                    return GatewayResult(ok=False, code="PARSE_ERROR", text=text[:2000],
                                         model=model, duration_ms=duration,
                                         prompt_hash=prompt_hash, images=used_images,
                                         detail="response is not JSON")
                return GatewayResult(ok=True, data=parsed, text=text[:4000],
                                     model=model, duration_ms=duration,
                                     prompt_hash=prompt_hash, images=used_images)
            except urllib.error.HTTPError as exc:
                # 403 的原因（"model not allowed for this key" 之类）必须能查到，
                # 否则只能看到"HTTP 403"，排查全靠猜。
                try:
                    body = exc.read().decode("utf-8", "replace")[:300]
                except Exception:
                    body = ""
                last_error = f"HTTP {exc.code} {body}".strip()
                if attempt >= self.retries:
                    break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = f"{type(exc).__name__}"
                if attempt >= self.retries:
                    break
        duration = (time.perf_counter() - started) * 1000.0
        self.spent_ms += duration
        code = "TIMEOUT" if "timeout" in last_error.lower() else "HTTP_ERROR"
        self._log(prompt_hash, model, duration, used_images, False, code)
        return GatewayResult(ok=False, code=code, model=model, duration_ms=duration,
                             prompt_hash=prompt_hash, images=used_images,
                             detail=last_error)

    # ---------------------------------------------------------------- 审计
    def _log(self, prompt_hash: str, model: str, duration: float, images: int,
             parsed: bool, code: str = "") -> None:
        self.audit.append({"schema": SCHEMA, "call": len(self.audit) + 1,
                           "prompt_hash": prompt_hash, "prompt_version": self.prompt_version,
                           "model": model, "duration_ms": round(duration, 1),
                           "images": images, "parsed": bool(parsed),
                           "code": code or ""})

    def audit_summary(self, limit: int = 24) -> dict:
        return {"schema": SCHEMA, "calls": self.calls, "tokens_estimate": self.tokens,
                "images_sent": self.images_sent,
                "images_dropped": self.images_dropped,
                "allow_remote_images": self.allow_remote_images,
                "model": self.model, "prompt_version": self.prompt_version,
                "entries": self.audit[-limit:]}


def _image_uri(image) -> str:
    """把 ``bytes`` / ``{"b64": ...}`` 变成 data URI；超过上限返回空串。"""
    import base64
    b64 = ""
    if isinstance(image, (bytes, bytearray)):
        if len(image) > int(LIMITS["image_bytes"]):
            return ""
        b64 = base64.b64encode(bytes(image)).decode("ascii")
    elif isinstance(image, dict):
        b64 = str(image.get("b64") or image.get("data_uri") or "")
        if b64.startswith("data:image"):
            return b64
    elif isinstance(image, str):
        b64 = image
    if not b64 or len(b64) > int(LIMITS["image_bytes"]) * 2:
        return ""
    return "data:image/png;base64," + b64


def gateway_from_config(cfg: dict) -> ModelGateway:
    """按 :mod:`ai_config` 的配置建网关（关闭时就返回一个不可用的空网关）。"""
    from . import ai_config as AIC
    if not AIC.enabled(cfg):
        return ModelGateway(api_key="", base_url="", model="")
    cred = AIC.credentials(cfg)
    prov = (cfg or {}).get("provider") or {}
    return ModelGateway(api_key=cred["api_key"], base_url=cred["base_url"],
                        model=cred["model"], vision_model=cred["vision_model"],
                        allow_remote_images=cred["allow_remote_images"],
                        timeout=float(prov.get("timeout_seconds") or 60.0),
                        retries=int(prov.get("retries") or 1),
                        budget=dict((cfg or {}).get("budget") or {}))
