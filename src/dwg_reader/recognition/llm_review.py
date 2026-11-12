"""模式 B · 模型**只能否决**：候选只会减少，永远不会被模型"说成"什么。

角色边界（这是本模块存在的全部理由）
------------------------------------
模型在识别链路里能做的事只有一件：看一堆**程序已经算好几何**的候选，指出
"这个明显不是门/窗/柱"。它不能：

* 新增候选（它拿不到全图几何，没有产生候选的能力）；
* 提升置信（``keep`` 与 ``unsure`` 在代码里是**同一个分支**：不改动）；
* 给几何（回复里出现坐标/尺寸/容差字段 → 整条记录被丢弃）。

判据不进提示词（重要）
----------------------
上一版在提示词里写了"柱截面长宽比 ≤2"这种判据，结果比代码自己的判据
（``COL_ASPECT=3``、贴墙垛到 12）更严，模型照章办事把 49 根合法墙垛全删了。
教训：**判据一旦写成数字，就是在赌所有图纸都这么画**。

所以这里只给模型两样东西：

1. **结构化事实**（尺寸、构成、是否落在墙线上、同族重复次数——都是程序量的，
   不带坐标，不教它怎么判）；
2. **候选周围的局部截图**（白底灰线，红框标候选）。门/窗/柱"长什么样"模型
   见得太多了，一张小图比任何数字判据都可靠。

``reject`` 必须有图上可见的证据或结构化事实里两条独立负面信号；拿不准一律
``unsure``（保留 + 待复核），拿得准的好结果（``keep``）零改动。

审计
----
被否决的候选不删除，连同一句理由进 ``rejected_by_model``（handles + reason），
可在报告里逐条复查；同一证据包哈希 → 同一结论，可回放。
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.request

from .llm_profile import DEFAULT_BASE, DEFAULT_MODEL

PROMPT_VERSION = "llm_review/2.0"   # 2.0 = 事实 + 截图，判据不写进提示词
#: 一次最多送多少条候选（大图上候选成百上千，分批问，别一图元一次调用）
BATCH = 200
#: 每张图最多补多少张截图（截图问法是单条的，贵；只给第一级拿不准的）
MAX_IMAGES = int(os.environ.get("DWG_REVIEW_MAX_IMAGES", "40"))
#: 闭集：**只有 reject 有副作用**
VERDICTS = ("keep", "reject", "unsure")
#: 缓存文件名（与其他 dwg-* 缓存同目录；键里含模型版本与提示词版本）
CACHE_FILE = ".dwg-review-cache.json"

PROMPT = """你是建筑图纸构件复核员。下面是一批**已经由程序算出几何**的候选构件，
请判断每一条"是不是它自称的那种构件"。只输出 JSON。

每条候选附**结构化事实**（都是程序量的，单位 mm）：尺寸、构成（线/弧/闭合/
填充）、是否落在墙线上、两侧空间、同族在图上重复的次数。**不要臆测事实里
没有的信息；事实不足就 unsure。**

规则：
1. 只能给 verdict ∈ {{keep, reject, unsure}}：
   - keep：图样和事实都对得上，或证据不足以否定；
   - reject：明确不像。必须给出一个具体证据（事实里两条独立负面信号，
     例如"不在墙线上 + 长宽比 8:1"；或截图里看得出是家具/装饰/图例）；
   - unsure：拿不准。**宁可 unsure，不要为了"干净"乱 reject**；
2. **只能做减法**：不要提出新增构件，不要给坐标、尺寸、容差、阈值；
3. 每条附一句不超过 15 字的 reason，写明你看到的证据。

输入：
{cases}

输出格式：
{{"results":[{{"id":"<原样返回>","verdict":"reject","reason":"不在墙线上，且同族只出现1次"}}]}}
"""

VISION_NOTE = """
下面附一张该候选周围的局部裁剪图（白底；灰线=图纸原生线；红线段=候选的洞口
轴线；黄圈=锚点；上方红字=handle 与块名）。

结合截图与结构化事实，判断它是不是 {kind}。verdict 闭集不变。
- reject 需要图上可见的证据：红框里看得出是家具/装饰/图例，而不是墙线洞口；
- 截图看不清就 unsure；看得出没问题就 keep。
"""


# --------------------------------------------------------------------------- 事实
def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _aspect(item: dict) -> float:
    """候选的长宽比（窗=长度/厚度，柱=长边/短边）。取不到给 0。"""
    w = _num(item.get("width") or item.get("w"))
    t = _num(item.get("thickness") or item.get("height") or item.get("h"))
    if w <= 0 or t <= 0:
        return 0.0
    return round(max(w, t) / min(w, t), 2)


def _family(name) -> str | None:
    """块名 -> 族名（去掉实例序号，``$WinLib2D$00000001`` -> ``$WinLib2D$``）。"""
    if not name:
        return None
    text = str(name)
    if "$" in text:
        parts = text.split("$")
        if len(parts) >= 2 and parts[1]:
            return "$".join(parts[:2]) + "$"
    head, sep, tail = text.rpartition("_")
    return head + sep if sep and tail.isdigit() else text


def _facts(item: dict, kind: str, repeats: dict | None = None) -> dict:
    """候选 -> 结构化事实（**不含坐标**）。只是事实，不含任何判据。"""
    methods = list(item.get("methods") or ())
    if not methods:
        for key in ("block", "arc", "attr", "hinges", "free_end", "line_bundle"):
            if item.get(key):
                methods.append(key)
    family = _family(item.get("block"))
    facts = {
        "kind": kind,
        "width_mm": round(_num(item.get("width") or item.get("w")), 1),
        "thickness_mm": round(_num(item.get("thickness") or item.get("h")), 1),
        "aspect": _aspect(item),
        "block": item.get("block") or None,
        "block_family": family,
        "family_repeat": (repeats or {}).get(family),
        "methods": methods[:6],
        "on_wall_gap": item.get("on_wall_gap"),
        "wall_distance_mm": _opt(item.get("wall_distance")),
        "has_swing_arc": bool(item.get("hinges") or item.get("swing")),
        "space_both_sides": item.get("space_connection"),
        "touches_wall": _touches(item),
        "section_type": item.get("section_type"),
        "evidence_state": item.get("evidence_state") or item.get("status"),
    }
    return {k: v for k, v in facts.items() if v is not None}


def _touches(item: dict) -> bool | None:
    """候选是否贴墙（有 evidence 时取 evidence，否则看 touches_wall 字段）。"""
    ev = item.get("evidence")
    if isinstance(ev, (list, tuple)) and "touches_wall" in ev:
        return True
    if isinstance(ev, str) and "touches_wall" in ev:
        return True
    v = item.get("touches_wall")
    return bool(v) if v is not None else None


def _opt(v):
    if v is None:
        return None
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return None


def _case_id(item: dict, idx: int, kind: str) -> str:
    handle = item.get("handle")
    return f"{kind}:{handle or idx}"


def _repeat_map(snapshot: dict | None) -> dict:
    """块族 -> 图上实例数（"这个族出现 50 次"比任何单体判据都硬）。"""
    out: dict[str, int] = {}
    for e in (snapshot or {}).get("entities") or ():
        if e.get("type") != "INSERT":
            continue
        fam = _family(e.get("name"))
        if fam:
            out[fam] = out.get(fam, 0) + 1
    return out


# --------------------------------------------------------------------------- 底图与截图
def _basemap_from_snapshot(snapshot: dict | None, max_segments: int = 20000) -> dict:
    """从快照搭一张给 render_crop 用的底图：全部原生线段 + 文字（不带块内展开）。

    块内展开的几何对复核没价值（候选本身就画成红线了），原生线才是上下文。
    """
    segments, texts = [], []
    for rec in (snapshot or {}).get("entities") or ():
        kind = rec.get("type")
        if kind == "LINE":
            s, e = rec.get("start"), rec.get("end")
            if s and e:
                segments.append([float(s[0]), float(s[1]), float(e[0]), float(e[1])])
        elif kind in ("LWPOLYLINE", "POLYLINE"):
            pts = rec.get("points") or ()
            tail = pts[:1] if rec.get("closed") else []
            for a, b in zip(pts, list(pts[1:]) + list(tail)):
                segments.append([float(a[0]), float(a[1]), float(b[0]), float(b[1])])
        elif kind in ("TEXT", "MTEXT"):
            p = rec.get("insert")
            t = rec.get("text")
            if p and t:
                texts.append([float(p[0]), float(p[1]), str(t)])
    return {"segments": segments[:max_segments], "texts": texts[:400]}


def _render_case(item: dict, base_map: dict) -> str | None:
    """复用 llm_verify 的裁剪渲染（白底灰线、红线候选、黄圈锚点）。"""
    from . import llm_verify as _V
    return _V.render_crop({"item": item}, base_map)


# --------------------------------------------------------------------------- 调用
def _post(prompt: str, api_key: str, base: str, model: str,
          image_b64: str | None = None, timeout: int = 90) -> str:
    content = [{"type": "text", "text": prompt}]
    if image_b64:
        content.append({"type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + image_b64}})
    body = {"model": model, "temperature": 0,
            "messages": [{"role": "system", "content": "你是图纸构件复核员，只输出 JSON。"},
                         {"role": "user", "content": content}]}
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


#: 回复里出现这些键 → 视为越权（模型试图给几何），整条丢弃
_FORBIDDEN = ("x", "y", "center", "insert", "start", "end", "axis", "bbox",
              "coord", "coordinates", "size", "tolerance", "threshold",
              "mm", "width", "height", "position")


def _sanitize(raw: dict, ids: set[str]) -> dict:
    """只要 {id, verdict, reason}；任何几何字段或未知 id 都丢弃。"""
    out: dict[str, dict] = {}
    for rec in (raw.get("results") or ()):
        if not isinstance(rec, dict):
            continue
        rid = str(rec.get("id") or "")
        if rid not in ids:
            continue
        if any(k in rec for k in _FORBIDDEN if k != "height"):
            continue
        verdict = str(rec.get("verdict") or "").strip().lower()
        if verdict not in VERDICTS:
            continue
        reason = str(rec.get("reason") or "").strip()[:40]
        out[rid] = {"verdict": verdict, "reason": reason}
    return out


def _cache_key(payload) -> str:
    blob = json.dumps({"v": PROMPT_VERSION, "cases": payload},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _load_cache(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(path: str, cache: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, indent=1)
    except OSError:
        pass


#: 最近一次调用失败的原因（诊断用；成功时清空）
LAST_ERROR: str | None = None


def _vision_one(case: dict, base_map: dict, api_key: str, base: str,
                model: str, cache_path: str) -> dict | None:
    """单条候选的"文字 + 截图"复核。截图渲染失败就当没这条，不影响主流程。"""
    item = case.get("_item")
    if not item:
        return None
    image_path = _render_case(item, base_map)
    if not image_path:
        return None
    try:
        import base64
        with open(image_path, "rb") as fh:
            image_b64 = base64.b64encode(fh.read()).decode()
    except OSError:
        return None
    # 截图缓存：事实 + 图字节前缀一起哈希，图换了结论必须重问
    key = hashlib.sha256((PROMPT_VERSION + "|vision|" + model + "|"
                          + str(case.get("id")) + "|"
                          + json.dumps(case["facts"], ensure_ascii=False, sort_keys=True)
                          + "|" + image_b64[:4096]).encode("utf-8")).hexdigest()
    cache = _load_cache(cache_path)
    hit = cache.get(key)
    if isinstance(hit, dict):
        return hit
    # id 必须出现在提示词里——模型只能原样返回，否则 _sanitize 会把它的结论丢掉
    prompt = (VISION_NOTE.format(kind=case["facts"].get("kind") or "?")
              + f'\n本条候选的 id："{case["id"]}"'
              + "\n结构化事实：\n" + json.dumps(case["facts"], ensure_ascii=False)
              + '\n输出格式：{"results":[{"id":"<本条候选的 id 原样返回>","verdict":"...","reason":"..."}]}')
    global LAST_ERROR
    try:
        text = _post(prompt, api_key, base, model, image_b64=image_b64)
        got = _sanitize(_parse(text), {case["id"]})
    except Exception as exc:
        LAST_ERROR = f"vision: {type(exc).__name__}: {exc}"
        return None
    if got:
        cache[key] = got[case["id"]]
        _save_cache(cache_path, cache)
        return got[case["id"]]
    return None


def review(cases, *, api_key: str | None = None, base: str | None = None,
           model: str | None = None, cache_dir: str | None = None,
           base_map: dict | None = None, vision: bool = True) -> dict:
    """候选 -> {id: {verdict, reason, stage}}。无 key / 任何异常 -> 空 dict。

    两级：
      1. **文字批量**：所有候选一次分批问（便宜，覆盖全）；
      2. **截图单条**：只对"文字级拿不准或要否决"的候选补截图再问（贵，有上限）。
    截图级的结论覆盖文字级——截图是更硬的证据。
    """
    cases = [c for c in (cases or ()) if c]
    if not cases or not api_key:
        return {}
    base = base or os.environ.get("LLM_BASE") or DEFAULT_BASE
    model = model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
    path = os.path.join(cache_dir or os.getcwd(), CACHE_FILE)

    out: dict[str, dict] = {}
    # ---- 第一级：文字批量
    for start in range(0, len(cases), BATCH):
        chunk = cases[start:start + BATCH]
        payload = [{"id": c["id"], **c["facts"]} for c in chunk]
        key = _cache_key(payload)
        cache = _load_cache(path)
        if key in cache and isinstance(cache[key], dict):
            out.update(cache[key])
            continue
        ids = {c["id"] for c in chunk}
        global LAST_ERROR
        try:
            prompt = PROMPT.format(cases=json.dumps({"cases": payload},
                                                    ensure_ascii=False))
        except Exception as exc:
            LAST_ERROR = f"prompt_format: {type(exc).__name__}: {exc}"
            return {}
        try:
            text = _post(prompt, api_key, base, model)
            raw = _parse(text)
        except Exception as exc:
            LAST_ERROR = f"request: {type(exc).__name__}: {exc}"
            continue
        got = _sanitize(raw, ids)
        if got:
            for rid, v in got.items():
                v["stage"] = "text"
            cache[key] = got
            _save_cache(path, cache)
            out.update(got)
        else:
            LAST_ERROR = f"sanitized_empty: {str(text)[:120]}"

    # ---- 第二级：截图。给谁补图按**证据强度**，不按文字结论——
    # 文字级对"事实弱"的候选倾向于 keep（拿不准就不敢否），正是这批需要图来兜底。
    # 硬证据 = 落在墙缺口上 / 有开启弧 / 有确认的截面类型；一样都没有的，截图复核。
    def _weak(case: dict) -> bool:
        """证据弱 = 没有**上下文证据**（位置/拓扑）。

        不能把 section_type（截面类型）算作"证据够"：那是程序按形状自己判的，
        正是模型要看图复核的东西。实测教训：把 section_type 算进去之后，单层图
        的 57 个柱候选一张截图都没送（vision=0），复核等于没做。
        """
        return not _context_of(case.get("_item") or {})

    if vision and base_map is not None:
        # 预算有限就别按顺序花：**证据最弱的先看**（上下文证据越少越值得一张图）
        weak_cases = [c for c in cases if _weak(c)]
        weak_cases.sort(key=lambda c: (len(_context_of(c.get("_item") or {})),
                                       str(c.get("id"))))
        for c in weak_cases[:MAX_IMAGES]:
            more = _vision_one(c, base_map, api_key, base, model, path)
            if more:
                more["stage"] = "vision"
                out[c["id"]] = more
    return out


# --------------------------------------------------------------------------- 应用
#: 模型看的是一张局部截图：**形状**（闭合轮廓/双线/弧线）它判得准，**位置**
#: （贴没贴墙/在不在洞口/两侧是不是两间房）是全局关系，截图可能根本没拍到
#: 那根墙线。所以模型能否决形态证据，不能否决上下文证据——后者冲突时降级
#: 为"待复核"，不删除。
_CONTEXT_KEYS = ("on_wall_gap", "space_both_sides", "touches_wall",
                 "at_grid_intersection")


def _context_of(item: dict) -> list:
    """候选手里有哪些**上下文证据**（位置/拓扑），返回命中的键名。"""
    ev = item.get("evidence")
    touches = item.get("touches_wall")
    if touches is None and isinstance(ev, (list, tuple)):
        touches = "touches_wall" in ev
    # 只有"贴**已确认的**墙"才算上下文证据。贴任意一条线不算——单图层图上那种
    # touches_wall 对 100% 候选都成立（实测 57/57），拿它挡模型的否决等于没有规则。
    state = item.get("touches_wall_state")
    if state is not None and state != "strong":
        touches = False
    out = []
    for key, val in (("on_wall_gap", item.get("on_wall_gap")),
                     ("space_both_sides", item.get("space_connection")),
                     ("touches_wall", touches),
                     ("at_grid_intersection", item.get("at_grid_intersection"))):
        if val:
            out.append(key)
    return out


def _reject(items, verdicts, kind: str):
    """按 verdict 拆成 (保留, 否决, 冲突)；**keep 与 unsure 同等对待**。

    * reject 且无上下文证据 → 否决成立（移出构件，进 rejected 清单）；
    * reject 但有上下文证据 → **冲突**：程序的位置证据和模型的截图判断对不上，
      不删除、不计入确认，标 ``review_state=conflict`` + ``needs_review``；
    * keep / unsure → 零改动。
    """
    kept, rejected, conflicts = [], [], []
    # 同一 handle 可能枚举出多个重叠候选：按 handle 共享结论，报告按 handle 去重
    by_handle: dict[str, dict] = {}
    for rid, entry in (verdicts or {}).items():
        head = rid.split(":", 1)[1] if ":" in rid else rid
        by_handle.setdefault(head, entry)
    seen_report: set[tuple] = set()
    for idx, item in enumerate(items or ()):
        rid = _case_id(item, idx, kind)
        entry = verdicts.get(rid) or by_handle.get(str(item.get("handle"))) or {}
        verdict = entry.get("verdict")
        if verdict == "reject":
            reason = entry.get("reason") or ""
            context = _context_of(item)
            if context:
                item["review_state"] = "conflict"
                item["needs_review"] = True
                # 「不计入确认」必须落到证据强度上：calibrate 只认 evidence_state，
                # 光打 needs_review 标记不会让它从 confirmed 里出来。
                item.setdefault("_evidence_state_before_conflict",
                                item.get("evidence_state"))
                item["evidence_state"] = "weak"
                item["review_note"] = (f"模型按截图判不像{kind}（{reason}），"
                                       f"但持有上下文证据：{'+'.join(context)}")
                key = (kind, str(item.get("handle")), reason)
                if key not in seen_report:
                    seen_report.add(key)
                    conflicts.append({"id": rid, "handle": item.get("handle"),
                                      "kind": kind, "reason": reason,
                                      "stage": entry.get("stage") or "text",
                                      "context": context})
                kept.append(item)
                continue
            key = (kind, str(item.get("handle")), reason)
            if key not in seen_report:
                seen_report.add(key)
                rejected.append({"id": rid, "handle": item.get("handle"),
                                 "kind": kind, "reason": reason,
                                 "stage": entry.get("stage") or "text",
                                 "width_mm": _num(item.get("width") or item.get("w")) or None,
                                 "methods": list(item.get("methods") or ()),
                                 "block": item.get("block")})
            continue
        kept.append(item)
    return kept, rejected, conflicts


def review_openings(doors, windows, *, api_key: str | None = None,
                    cache_dir: str | None = None, base: str | None = None,
                    model: str | None = None, snapshot: dict | None = None,
                    vision: bool = True) -> tuple[list, list, dict]:
    """模式 B 在洞口上的应用：只摘掉"明确不像门/窗"的候选。

    返回 ``(保留的门, 保留的窗, 统计)``；统计里带被否决清单（可审计、可回放）。
    """
    if not api_key:
        return list(doors or ()), list(windows or ()), {"status": "unavailable"}
    repeats = _repeat_map(snapshot)
    base_map = _basemap_from_snapshot(snapshot) if snapshot else None
    cases = []
    for idx, item in enumerate(doors or ()):
        cases.append({"id": _case_id(item, idx, "door"),
                      "facts": _facts(item, "door", repeats), "_item": item})
    for idx, item in enumerate(windows or ()):
        cases.append({"id": _case_id(item, idx, "window"),
                      "facts": _facts(item, "window", repeats), "_item": item})
    verdicts = review(cases, api_key=api_key, cache_dir=cache_dir, base=base,
                      model=model, base_map=base_map, vision=vision)
    if not verdicts:
        return list(doors or ()), list(windows or ()), {"status": "no_verdict"}
    kept_doors, rej_doors, conf_doors = _reject(doors, verdicts, "door")
    kept_windows, rej_windows, conf_windows = _reject(windows, verdicts, "window")
    stats = {"status": "applied", "cases": len(cases),
             "verdicts": len(verdicts),
             "vision": sum(1 for v in verdicts.values() if v.get("stage") == "vision"),
             "kept_doors": len(kept_doors), "kept_windows": len(kept_windows),
             "rejected_doors": len(rej_doors), "rejected_windows": len(rej_windows),
             "rejected": rej_doors + rej_windows,
             "conflicts": conf_doors + conf_windows}
    return kept_doors, kept_windows, stats


def reject_columns(columns, *, api_key: str | None = None,
                   cache_dir: str | None = None, base: str | None = None,
                   model: str | None = None, snapshot: dict | None = None,
                   vision: bool = True) -> tuple[list, dict]:
    """模式 B 在柱上的应用（同上：只做减法）。"""
    if not api_key:
        return list(columns or ()), {"status": "unavailable"}
    repeats = _repeat_map(snapshot)
    base_map = _basemap_from_snapshot(snapshot) if snapshot else None
    cases = []
    for idx, item in enumerate(columns or ()):
        facts = _facts(item, "column", repeats)
        facts["width_mm"] = round(_num(item.get("w")), 1)
        facts["thickness_mm"] = round(_num(item.get("h")), 1)
        facts["aspect"] = _aspect({"width": item.get("w"), "thickness": item.get("h")})
        facts["methods"] = list(item.get("methods") or (item.get("method") or ()))
        cases.append({"id": _case_id(item, idx, "column"),
                      "facts": facts, "_item": item})
    verdicts = review(cases, api_key=api_key, cache_dir=cache_dir, base=base,
                      model=model, base_map=base_map, vision=vision)
    if not verdicts:
        return list(columns or ()), {"status": "no_verdict"}
    kept, rejected, conflicts = _reject(columns, verdicts, "column")
    return kept, {"status": "applied", "cases": len(cases), "verdicts": len(verdicts),
                  "vision": sum(1 for v in verdicts.values() if v.get("stage") == "vision"),
                  "kept": len(kept), "rejected": len(rejected),
                  "rejected_items": rejected, "conflicts": conflicts}
