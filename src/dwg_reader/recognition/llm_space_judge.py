"""空间性质判定（LLM）：房名没闭合的每一处，判 该补墙 / 本就是开放空间 / 不确定。

模型只做语义裁决，不产出坐标；目标必须是诊断已经算出的 defect_id。
结果回灌确定性分支：open_space -> 建开放分区；try_repair -> 待修复；uncertain -> 待核。
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

PROMPT_VERSION = "space_judge/1.0"
BATCH = 12
DECISIONS = ("try_repair", "open_space", "uncertain")

PROMPT = """你是建筑图纸的语义判别员。下面每一处都满足"房名文字落在未闭合区域里"，
请判断这处更可能是哪种情况：

A. try_repair  —— 图纸把边界画漏了：缺口不大、两侧墙体共线、补上后这个房间能闭合、
                  面积与标注接近。
B. open_space  —— 这里本来就是开放空间/功能分区：走道、开敞办公、门厅、中庭、共享区、
                  相邻空间本来就相通。
C. uncertain   —— 信息不足，不要硬判。

规则：
1. 只能选 A/B/C 之一，不能给坐标、尺寸、阈值；
2. 判 try_repair 时 defect_ids 必须来自 case 里给出的 defect_ids；
3. 判 open_space 时，若房名本身是该功能分区名称（走道/开敞/门厅/中庭/餐厅/共享），
   即使附近有缺口也倾向 open_space；
4. 判 try_repair 时，缺口应当是**该房间边界的唯一缺口**，且面积与标注数量级一致；
5. 每条给一句不超过 20 字的 reason。

输入 cases：
{cases}

输出格式：
{"results":[{"case_id":"<原样返回>","decision":"try_repair",
             "defect_ids":["<id>"],"reason":"不超过20字"}]}

示例输入：
{"cases":[
 {"case_id":"sp_01","room_name":"开敞办公","declared_area":96.0,
  "label_in_face":false,"gap_count":3,"max_gap_mm":5200,
  "collinear_supports":1,"neighbour_names":["走道","前厅"],
  "defect_ids":["defect_004","defect_009"]},
 {"case_id":"sp_02","room_name":"女卫","declared_area":13.1,
  "label_in_face":false,"gap_count":1,"max_gap_mm":600,
  "collinear_supports":2,"neighbour_names":["男卫"],
  "defect_ids":["defect_012"]}]}
示例输出：
{"results":[
 {"case_id":"sp_01","decision":"open_space","defect_ids":[],
  "reason":"开敞办公与走道相通，缺口非边界"},
 {"case_id":"sp_02","decision":"try_repair","defect_ids":["defect_012"],
  "reason":"单处小缺口且面积吻合"}]}
"""


def _post(prompt, api_key, base, model, timeout=90) -> str:
    body = {"model": model, "temperature": 0,
            "messages": [{"role": "system",
                          "content": "你是建筑图纸语义判别员，只输出 JSON。"},
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


def _sanitize(raw: dict, ids: set, defect_ids: set) -> dict:
    out = {}
    for rec in (raw.get("results") or ()):
        if not isinstance(rec, dict):
            continue
        cid = str(rec.get("case_id") or "")
        if cid not in ids:
            continue
        decision = str(rec.get("decision") or "uncertain").lower()
        if decision not in DECISIONS:
            decision = "uncertain"
        targets = [str(t) for t in (rec.get("defect_ids") or ())
                   if str(t) in defect_ids]
        if decision == "try_repair" and not targets:
            decision = "uncertain"
        out[cid] = {"decision": decision, "defect_ids": targets[:4],
                    "reason": str(rec.get("reason") or "")[:24]}
    for cid in ids:
        out.setdefault(cid, {"decision": "uncertain", "defect_ids": [],
                             "reason": "no_answer"})
    return out


def judge(cases, *, api_key: str | None = None, base: str | None = None,
          model: str | None = None, max_workers: int = 3,
          cache_path: str | None = None, log_path: str | None = None) -> dict:
    cases = list(cases or ())
    if not cases:
        return {"status": "skipped", "verdicts": {}, "calls": 0, "counts": {}}
    api_key = CRED.api_key(api_key)
    if not api_key:
        return {"status": "unavailable", "verdicts": {}, "calls": 0, "counts": {}}
    base = base or os.environ.get("LLM_BASE") or DEFAULT_BASE
    model = model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
    cache_path = cache_path or os.path.join(os.getcwd(), ".dwg-space-cache.json")
    log_path = log_path or os.path.join(os.getcwd(), ".dwg-space-log.jsonl")
    cache = _load(cache_path)

    batches = [cases[i:i + BATCH] for i in range(0, len(cases), BATCH)]
    payloads = []
    for b in batches:
        rows = [{k: c.get(k) for k in ("case_id", "room_name", "declared_area",
                                      "label_in_face", "gap_count", "max_gap_mm",
                                      "collinear_supports", "neighbour_names",
                                      "defect_ids")} for c in b]
        payloads.append(({r["case_id"] for r in rows}, rows,
                         {d for r in rows for d in (r.get("defect_ids") or ())}))
    keys = [hashlib.sha256(json.dumps({"p": PROMPT_VERSION, "m": model, "rows": rows},
                                      ensure_ascii=False, sort_keys=True).encode()
                           ).hexdigest() for _i, rows, _d in payloads]

    verdicts: dict[str, dict] = {}
    todo = []
    for k, (ids, rows, dids) in zip(keys, payloads):
        if k in cache:
            verdicts.update(cache[k])
        else:
            todo.append((k, ids, rows, dids))

    def ask(ids, rows, dids):
        prompt = PROMPT.replace("{cases}", json.dumps({"cases": rows}, ensure_ascii=False))
        return _sanitize(_parse(_post(prompt, api_key, base, model)), ids, dids)

    if todo:
        with futures.ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(todo)))) as pool:
            fut = {pool.submit(ask, ids, rows, dids): k for k, ids, rows, dids in todo}
            for f in futures.as_completed(fut):
                k = fut[f]
                try:
                    got = f.result()
                except Exception:
                    got = {}
                verdicts.update(got)
                if got:
                    cache[k] = got
                _log(log_path, {"ts": round(time.time(), 3), "prompt": PROMPT_VERSION,
                                "model": model, "key": k, "n": len(got)})
        _save(cache_path, cache)

    counts = {d: sum(1 for v in verdicts.values() if v["decision"] == d)
              for d in DECISIONS}
    status = "ok" if (counts.get("try_repair") or counts.get("open_space")) else "no_action"
    return {"status": status, "verdicts": verdicts, "calls": len(todo),
            "counts": counts, "prompt_version": PROMPT_VERSION, "model": model}


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
