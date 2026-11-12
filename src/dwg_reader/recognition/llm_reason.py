"""LLM 疑难分析（DAG 条件节点）：只接收闭环结构化 CaseBundle，只回闭集动作或 NO_ACTION。

四类任务共用同一节点，按 ``problem`` 分流：

* ``SEMANTIC_ADAPT``     未知图层/块族含义
* ``PATTERN_INTERPRET``  有原语、无候选（算子组合建议）
* ``CONFLICT_REVIEW``    互斥解释排序
* ``TOPOLOGY_DIAGNOSE``  房间未闭合、墙网异常

不可用时返回 ``status="unavailable"``，绝不影响确定性识别；输入输出、prompt 版本、
模型名与哈希全部追加到 ``.dwg-llm-reason-log.jsonl``，支持重放。
"""
from __future__ import annotations

import base64
import concurrent.futures as futures
import hashlib
import json
import os
import time
import urllib.request

from . import llm_credentials as CRED
from . import case_builder as CB
from .llm_profile import DEFAULT_BASE, DEFAULT_MODEL

PROMPT_VERSION = "llm_reason/2.2"
MAX_PROPOSALS = 4
MAX_CALLS = int(os.environ.get("DWG_LLM_MAX_CALLS", "8"))
VISION_NOTE = """
下面附一张该区域中心的局部裁剪图（白底、灰线为图纸原生线、红字为 case id）。
请结合图形判断，但仍然只能返回闭集动作，不得给出坐标或尺寸。
"""
PROMPT = """你是 CAD 识别疑难分析员。系统已经为该问题算好了若干**合法替代方案**（alternatives），
请从中挑一个，或选 NO_ACTION。只输出 JSON，不要解释。

规则：
1. 不能给出任何坐标、尺寸、容差、阈值，也不能写代码；
2. selected_alternative 必须是 alternatives 里已有的 id，不能自己编；
3. 几何特征（parallel_consistency / wall_alignment / span_overlap）由确定性算法算出，
   据此判断该处更像墙还是更像窗；证据不足就选 NO_ACTION；
4. 若选中的方案 operation 是 `propose_layer_role`，**必须同时输出 roles，且与
   target_ids 一一对应**：`"roles": [["furniture"], ["ignore"]]` 表示第一、二个图层
   分别取这些角色（每层 1~2 个，闭集：wall/column/door/window/room_name/axis/stair/
   step/apron/north/frame/furniture/ignore）；数量对不上会被校验器拒绝；
5. 其他方案的 roles 必须省略，不要写成 null；
6. decision 只能是 TRY 或 NO_ACTION；附一句不超过 20 字的 reason。

输出格式：
{"case_id":"<原样返回>","selected_alternative":"alt:<n>","decision":"TRY",
 "supporting_evidence_ids":["<id>"],"reason":"不超过20字"}
（选中 propose_layer_role 时额外带 "roles":["furniture"]）

示例 A｜普通方案
输入 alternatives：[{"id":"alt:1","operation":"regenerate_opening","hypothesis":"window"},
                  {"id":"alt:2","operation":"regenerate_wall","hypothesis":"wall"},
                  {"id":"alt:3","operation":"NO_ACTION"}]
输入 patterns：[{"line_count":4,"parallel_consistency":0.98,"wall_alignment":0.96,
                "span_overlap":0.91,"layer":"CURTWALL"}]
输出：{"case_id":"case_017","selected_alternative":"alt:1","decision":"TRY",
      "supporting_evidence_ids":["pattern:1"],"reason":"四线平行且与墙同向，判为窗"}

示例 B｜语义修复（必须带 roles）
输入 alternatives：[{"id":"alt:1","operation":"propose_layer_role",
                    "target_ids":["layer:AE-EQPM-TOLT"]},
                  {"id":"alt:2","operation":"NO_ACTION"}]
输入 detail：{"layers":[{"layer":"AE-EQPM-TOLT","count":214}]}
输出：{"case_id":"case_002","selected_alternative":"alt:1","decision":"TRY",
      "supporting_evidence_ids":["layer:AE-EQPM-TOLT","layer:DOTE"],
      "roles":[["furniture"],["ignore"]],
      "reason":"设备层按家具，点注记忽略"}

示例 C｜证据不足
输出：{"case_id":"case_009","selected_alternative":"alt:3","decision":"NO_ACTION",
      "supporting_evidence_ids":[],"reason":"替代方案均无几何支持"}

任务：{task}
证据：
{case}
"""


def task_of(code: str) -> tuple[str, tuple[str, ...]]:
    if code.startswith("SEMANTIC_GAP"):
        return "SEMANTIC_ADAPT", ("extract_contour", "extract_arc")
    if code.endswith("EVIDENCE_WITHOUT_CANDIDATES"):
        return "PATTERN_INTERPRET", ("group_line_bundle", "pair_parallel",
                                     "regenerate_opening", "regenerate_wall",
                                     "regenerate_column", "extract_contour")
    if "CONFLICT" in code:
        return "CONFLICT_REVIEW", ("resolve_conflicts",)
    return "TOPOLOGY_DIAGNOSE", ("rebuild_wall_graph", "rebuild_space_faces",
                                 "regenerate_wall")


def build_case(case: dict, *, index: dict, extra=None) -> dict:
    """把 case 变成"有信息量"的证据包：几何特征 + 有限替代方案。"""
    task, actions = task_of(case.get("reason") or "")
    enriched = case if case.get("alternatives") else CB.build(case, index)
    ids = enriched.get("evidence_ids") or []
    items = index.get("items") or {}
    kinds: dict[str, int] = {}
    for i in ids:
        k = (items.get(i) or {}).get("kind")
        if k:
            kinds[k] = kinds.get(k, 0) + 1
    pats = enriched.get("patterns") or []
    alts = [{"id": a["id"], "operation": a["operation"],
             "hypothesis": a.get("hypothesis"), "target_ids": a.get("target_ids"),
             "expected": a.get("expected")}
            for a in (enriched.get("alternatives") or ())]
    return {"case_id": case["case_id"], "problem": case.get("reason"),
            "region_bbox_mm": [round(float(v)) for v in (case.get("region") or [])] or None,
            "layer_semantics": enriched.get("layer_semantics") or {},
            "available_evidence": ids, "evidence_kinds": kinds,
            "patterns": [{"id": p["id"], "kind": p["kind"], "layer": p.get("layer"),
                          "line_count": p.get("line_count"),
                          "parallel_consistency": p.get("parallel_consistency"),
                          "wall_alignment": p.get("wall_alignment"),
                          "span_overlap": p.get("span_overlap"),
                          "nearby_wall": p.get("nearby_wall")} for p in pats],
            "alternatives": alts,
            "detail": enriched.get("detail") or {},
            "task": task, "allowed_actions": list(actions),
            "_alternatives_full": enriched.get("alternatives") or [],
            **(extra or {})}


def analyze(cases, *, index: dict, api_key: str | None = None,
            base: str | None = None, model: str | None = None,
            max_workers: int = 4, cache_path: str | None = None,
            log_path: str | None = None) -> dict:
    """并发分析多个 case，返回 {status, proposals, cases, prompt_version, model}。"""
    cases = list(cases or ())[:MAX_CALLS]          # 一次识别的调用预算
    if not cases:
        return {"status": "skipped", "proposals": [], "cases": [],
                "prompt_version": PROMPT_VERSION}
    api_key = CRED.api_key(api_key)
    if not api_key:
        return {"status": "unavailable", "proposals": [], "cases": [],
                "prompt_version": PROMPT_VERSION}

    base = base or os.environ.get("LLM_BASE") or DEFAULT_BASE
    model = model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
    cache_path = cache_path or os.path.join(os.getcwd(), ".dwg-llm-reason-cache.json")
    log_path = log_path or os.path.join(os.getcwd(), ".dwg-llm-reason-log.jsonl")
    cache = _load(cache_path)

    vision = os.environ.get("DWG_LLM_REASON_VISION") == "1"
    bundles = [build_case(c, index=index) for c in cases]
    images = {_key(b, model): _crop(c) for b, c in zip(bundles, cases)} if vision else {}
    keys = [_key(b, model) for b in bundles]

    if os.environ.get("DWG_LLM_REPLAY") == "1":     # 只重放：不发新请求
        replayed = {k: cache[k] for k in keys if k in cache}
        proposals = [dict(p, case_id=None) for r in replayed.values()
                     for p in (r.get("proposals") or ())]
        return {"status": "replay" if replayed else "no_action",
                "proposals": proposals, "cases": bundles,
                "prompt_version": PROMPT_VERSION, "model": model,
                "calls": 0, "cached": len(replayed), "errors": 0}

    out: dict[str, dict] = {k: cache[k] for k in keys if k in cache}
    todo = [(k, b) for k, b in zip(keys, bundles) if k not in out]
    if todo:
        with futures.ThreadPoolExecutor(max_workers=min(max_workers, len(todo))) as pool:
            fut = {pool.submit(_ask, b, api_key, base, model, images.get(k)): k
                   for k, b in todo}
            for f in futures.as_completed(fut):
                k = fut[f]
                try:
                    out[k] = f.result()
                except Exception as exc:
                    out[k] = {"case_id": None, "proposals": [],
                              "error": type(exc).__name__}
                if not out[k].get("error"):
                    cache[k] = out[k]
                _log(log_path, {"ts": round(time.time(), 3), "prompt": PROMPT_VERSION,
                                "model": model, "key": k, "result": out[k]})
        _save(cache_path, cache)

    proposals = []
    for case, k in zip(bundles, keys):
        for p in (out.get(k) or {}).get("proposals") or ():
            proposals.append(dict(p, case_id=case["case_id"]))
        for p in (out.get(k) or {}).get("proposals") or ():
            p["reason"] = p.get("reason")
    errors = sum(1 for v in out.values() if v.get("error"))
    status = "ok" if proposals else ("error" if errors and errors == len(out) else "no_action")
    return {"status": status, "proposals": proposals, "cases": bundles,
            "prompt_version": PROMPT_VERSION, "model": model,
            "calls": len(todo), "cached": len(out) - len(todo), "errors": errors}


def _ask(bundle, api_key, base, model, image_b64=None) -> dict:
    payload = {k: v for k, v in bundle.items() if not k.startswith("_")}
    prompt = (PROMPT.replace("{task}", str(bundle["task"]))
              .replace("{case}", json.dumps(payload, ensure_ascii=False, indent=1)))
    text = _post(prompt, api_key, base, model, image_b64)
    return _sanitize(_parse_json(text), bundle["case_id"],
                     bundle.get("_alternatives_full"))


def _post(prompt: str, api_key: str, base: str, model: str,
          image_b64: str | None = None, timeout: int = 120) -> str:
    """文本/视觉统一请求：可视化模式下把裁剪图作为多模态内容一起发送。"""
    if image_b64:
        prompt = VISION_NOTE + prompt
        content = [{"type": "text", "text": prompt},
                   {"type": "image_url",
                    "image_url": {"url": "data:image/png;base64," + image_b64}}]
    else:
        content = prompt
    body = {"model": model, "temperature": 0,
            "messages": [{"role": "system", "content": "你是 CAD 识别疑难分析员，只输出 JSON。"},
                         {"role": "user", "content": content}]}
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + api_key})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    return data["choices"][0]["message"].get("content") or ""


def _parse_json(text: str) -> dict:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        t = t[i:j + 1]
    return json.loads(t or "{}")


def _crop(case: dict) -> str | None:
    """case 区域中心 -> 裁剪图 base64（复用 llm_verify 的渲染，不额外依赖 ezdxf）。"""
    region = case.get("region")
    if not region:
        return None
    path_base = os.environ.get("DWG_LLM_REASON_BASEMAP") or os.environ.get("LLM_VERIFY_BASEMAP")
    try:
        from . import llm_verify as V
        base_map = V.load_basemap(path_base)
        if not base_map:
            return None
        x, y = (region[0] + region[2]) / 2.0, (region[1] + region[3]) / 2.0
        png = V.render_crop({"item": {"handle": case.get("case_id"), "center": [x, y],
                                     "bbox": region}}, base_map)
        if not png:
            return None
        with open(png, "rb") as fh:
            return base64.b64encode(fh.read()).decode()
    except Exception:
        return None


def replay(cases, *, index: dict, model: str | None = None,
           cache_path: str | None = None) -> dict:
    """只重放：用同一输入哈希命中已存响应，不发任何请求。"""
    model = model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
    cache_path = cache_path or os.path.join(os.getcwd(), ".dwg-llm-reason-cache.json")
    cache = _load(cache_path)
    bundles = [build_case(c, index=index) for c in (cases or ())]
    hit = [cache[k] for k in (_key(b, model) for b in bundles) if k in cache]
    return {"status": "replay" if hit else "no_action",
            "proposals": [p for r in hit for p in (r.get("proposals") or ())],
            "cases": bundles, "prompt_version": PROMPT_VERSION, "model": model,
            "cached": len(hit)}


def _roles_per_target(raw_roles, n_targets: int) -> list:
    """把模型的 roles 归一成"每个图层一份"：[["furniture"],["ignore"]]。"""
    roles = raw_roles if isinstance(raw_roles, list) else []
    if roles and all(isinstance(r, str) for r in roles):
        head = [[str(r) for r in roles][:2]]
        return head + [[] for _ in range(max(0, n_targets - 1))]
    out = [[str(x) for x in (r or ())][:2] if isinstance(r, (list, tuple)) else []
           for r in roles]
    out += [[] for _ in range(max(0, n_targets - len(out)))]
    n = max(1, n_targets)
    return out[:n]


def _sanitize(raw: dict, case_id, alternatives=None) -> dict:
    """把"选的替代方案 id"翻回可校验的提议（自带 target/action）。"""
    alts = {a["id"]: a for a in (alternatives or ()) if isinstance(a, dict) and "id" in a}
    props = []
    sel = str(raw.get("selected_alternative") or "").strip()
    decision = str(raw.get("decision") or "NO_ACTION").upper()
    if decision == "TRY" and sel in alts:
        alt = alts[sel]
        prop = {"action": alt.get("operation"),
                "target_ids": [str(t) for t in (alt.get("target_ids") or ())][:8],
                "hypothesis": alt.get("hypothesis"),
                "alternative": sel,
                "evidence_ids": [str(t) for t in
                                 (raw.get("supporting_evidence_ids") or ())][:8],
                "reason": str(raw.get("reason") or "")[:40]}
        if alt.get("operation") == "propose_layer_role":
            prop["roles"] = _roles_per_target(raw.get("roles"), len(prop["target_ids"]))
        props.append(prop)
    elif raw.get("proposals"):                   # 兼容旧格式
        for p in (raw.get("proposals") or ())[:MAX_PROPOSALS]:
            if isinstance(p, dict) and p.get("action"):
                props.append({"action": p.get("action"),
                              "target_ids": [str(t) for t in (p.get("target_ids") or ())][:8],
                              "hypothesis": p.get("hypothesis"),
                              "evidence_ids": [str(t) for t in
                                               (p.get("evidence_ids") or ())][:8],
                              "reason": str(p.get("reason") or "")[:40]})
    return {"case_id": case_id, "decision": decision, "proposals": props}


def _key(bundle: dict, model: str) -> str:
    blob = json.dumps({"p": PROMPT_VERSION, "m": model, "b": bundle},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save(path, data):
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
    except OSError:
        pass


def _log(path, rec):
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass
