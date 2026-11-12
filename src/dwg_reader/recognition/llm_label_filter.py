"""房名语义过滤：确定性规则拿不准的文字，交给模型判 room / annotation / uncertain。

闭集输出、不含坐标、不碰阈值；按批次哈希缓存；模型不可用时整批降级 uncertain，
不影响任何构件（只影响房名候选）。
"""
from __future__ import annotations

import concurrent.futures as futures
import hashlib
import json
import os
import time
import urllib.request

from . import llm_credentials as CRED
from .llm_profile import DEFAULT_BASE, DEFAULT_MODEL

PROMPT_VERSION = "label_filter/1.0"
BATCH = int(os.environ.get("DWG_LABEL_BATCH", "400"))   # 一次全发
ROLES = ("room", "annotation", "uncertain")

PROMPT = """你是建筑图纸标注判别员。下面是一批 DXF 图上的**短文字**，请判断每条是
"房间名"还是"其它标注（说明/尺寸/坡度/编号/材料/索引）"。只输出 JSON。

规则：
1. 只能给 role ∈ {room, annotation, uncertain}（也可用 ok=是房名 / no=不是），
   不能给坐标、尺寸、阈值；
2. 房间名通常是功能空间名称（办公室/卫生间/走道/厨房/阳台/设备间…）；
3. 带"：""注""详见""留洞""洞顶"的是说明文字；纯数字、百分比、X×Y、面积值都不是房名；
4. 拿不准就给 uncertain，不要硬判；
5. 每条附一句不超过 15 字的 reason。

输入 batch：
{batch}

输出格式：
{{"results":[{{"id":"<原样返回>","role":"room","reason":"功能空间名"}}]}}

示例输入：
{{"batch":[
 {{"id":"c1","text":"办公室","layer":"SPACE","height":300,"repeated":4,"in_face":true}},
 {{"id":"c2","text":"1%","layer":"SPACE","height":150,"repeated":3,"in_face":true}},
 {{"id":"c3","text":"留洞：","layer":"PUB_TEXT","height":250,"repeated":1,"in_face":true}}]}}
示例输出：
{{"results":[
 {{"id":"c1","role":"room","reason":"功能空间名"}},
 {{"id":"c2","role":"annotation","reason":"坡度标注"}},
 {{"id":"c3","role":"annotation","reason":"说明文字"}}]}}
"""


def _post(prompt, api_key, base, model, timeout=90) -> str:
    body = {"model": model, "temperature": 0,
            "messages": [{"role": "system", "content": "你是图纸标注判别员，只输出 JSON。"},
                         {"role": "user", "content": prompt}]}
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + api_key})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    return data["choices"][0]["message"].get("content") or ""


def _parse(text: str) -> dict:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
    i, j = t.find("{"), t.rfind("}")
    return json.loads(t[i:j + 1] if i >= 0 and j > i else "{}")


def _sanitize(raw: dict, ids) -> dict:
    out = {}
    for rec in (raw.get("results") or ()):
        if not isinstance(rec, dict):
            continue
        rid = str(rec.get("id") or "")
        if rid not in ids:
            continue
        role = str(rec.get("role") or rec.get("ok") or "uncertain").lower()
        role = {"ok": "room", "yes": "room", "true": "room", "1": "room",
                "no": "annotation", "false": "annotation", "0": "annotation"}.get(role, role)
        out[rid] = {"role": role if role in ROLES else "uncertain",
                    "reason": str(rec.get("reason") or "")[:20]}
    for rid in ids:
        out.setdefault(rid, {"role": "uncertain", "reason": "no_answer"})
    return out


def classify(cands, *, api_key: str | None = None, base: str | None = None,
             model: str | None = None, max_workers: int = 3,
             cache_path: str | None = None, log_path: str | None = None) -> dict:
    cands = list(cands or ())
    if not cands:
        return {"status": "skipped", "roles": {}, "calls": 0}
    api_key = CRED.api_key(api_key)
    if not api_key:
        return {"status": "unavailable", "roles": {}, "calls": 0}
    base = base or os.environ.get("LLM_BASE") or DEFAULT_BASE
    model = model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
    cache_path = cache_path or os.path.join(os.getcwd(), ".dwg-label-cache.json")
    log_path = log_path or os.path.join(os.getcwd(), ".dwg-label-log.jsonl")
    cache = _load(cache_path)

    batches = [cands[i:i + BATCH] for i in range(0, len(cands), BATCH)]
    payloads = []
    for b in batches:
        rows = [{"id": str(c.get("id")), "text": c.get("text"), "layer": c.get("layer"),
                 "height": c.get("height"), "repeated": c.get("repeated"),
                 "in_face": c.get("in_face")} for c in b]
        payloads.append(({r["id"] for r in rows}, rows))
    keys = [hashlib.sha256(json.dumps({"p": PROMPT_VERSION, "m": model, "rows": rows},
                                      ensure_ascii=False, sort_keys=True).encode()
                           ).hexdigest() for _ids, rows in payloads]

    roles: dict[str, dict] = {}
    todo = []
    for k, (ids, rows) in zip(keys, payloads):
        if k in cache:
            roles.update(cache[k])
        else:
            todo.append((k, ids, rows))

    def ask(ids, rows):
        prompt = PROMPT.replace("{batch}", json.dumps({"batch": rows}, ensure_ascii=False))
        return _sanitize(_parse(_post(prompt, api_key, base, model)), ids)

    if todo:
        with futures.ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(todo)))) as pool:
            fut = {pool.submit(ask, ids, rows): k for k, ids, rows in todo}
            for f in futures.as_completed(fut):
                k = fut[f]
                try:
                    got = f.result()
                except Exception:
                    got = {}
                roles.update(got)
                if got:
                    cache[k] = got
                _log(log_path, {"ts": round(time.time(), 3), "prompt": PROMPT_VERSION,
                                "model": model, "key": k, "n": len(got)})
        _save(cache_path, cache)

    counts = {r: sum(1 for v in roles.values() if v["role"] == r) for r in ROLES}
    status = "ok" if counts["room"] or counts["annotation"] else "no_action"
    return {"status": status, "roles": roles, "calls": len(todo),
            "prompt_version": PROMPT_VERSION, "model": model, "counts": counts}


def _load(p):
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save(p, d):
    try:
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False)
    except OSError:
        pass


def _log(p, rec):
    try:
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass
