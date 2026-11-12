"""低置信候选的模型复核（opt-in，运行期默认关闭）。

几何先算分：:func:`openings.score` 已在 0~1 尺度上量出证据强度。本模块只做三件事：

1. :func:`route` —— 挑出"分数低 / 关键证据缺失 / 证据互相矛盾 / 本来就是 pending"
   的候选；
2. :func:`verify` —— 把这些候选的**结构化证据包**（不含坐标）并发丢给模型，拿回
   ``accept / reject / uncertain`` + 一句理由；决策按证据包哈希缓存，同一张图重复
   识别零调用；
3. :func:`apply` —— 把决策回填成候选状态，只能在
   ``pending ↔ accept ↔ reject`` 之间改，**不新增候选、不改几何**。

运行期默认不走这里：``run_recognition(..., llm_verify=True)`` 才启用；
``LLM_API_KEY`` 缺失时整体降级为 ``unavailable``，识别结果与原来完全一致。
"""
from __future__ import annotations

import concurrent.futures as futures
import hashlib
import json
import os
import urllib.request

from . import llm_credentials as CRED
from . import blockmodel as BLK
from .llm_profile import DEFAULT_BASE, DEFAULT_MODEL, call_llm
from .openings import NEG, POS, UNAVAILABLE

#: 低于该分数就交给模型复核
LOW_CONF = 0.5
#: 模型决策的最低采纳置信度（低于它按 uncertain 处理）
MIN_LLM_CONF = 0.6
CATEGORIES = ("door", "window", "column", "wall", "room", "ignore")
DECISIONS = ("accept", "reject", "uncertain")
#: 并发复核线程数（模型请求是 IO 密集，写死在模块里，避免和 DAG 并行度混淆）
MAX_WORKERS = 6

PROMPT = """你是 CAD 构件识别复核员。下面是一个**候选构件**的结构化证据（单位 mm，无坐标）。
请判断它到底是什么，只输出 JSON，不要解释，不要给任何数值阈值或坐标。

类别闭集：{cats}
decision 取值：accept（确认是 guess 里的类别）/ reject（不是该类别，也不是别的构件）/
uncertain（证据不足，保持待核）

判断要点（只作为判据，不要照抄）：
- 门：洞口应落在墙缺口里，两侧通常是两个不同空间；无弧门块也可能是推拉门/折叠门。
- 窗：洞口在墙缺口里、与墙同向，两侧一般一侧室内一侧室外。
- partition.near_mm 很小（<=300）说明附近有轻质隔断块：此时"没墙洞 + 两侧同一空间"
  **不能**作为否定证据，无弧门块也应 accept。
- block 无弧且墙归属对不上时，注意可能是非门符号（检修口、传递窗等）。

输出格式：
{{"decision":"accept","category":"door","subtype":"推拉门","confidence":0.7,
 "reason":"不超过20字"}}

示例 A｜隔断门（accept）
输入：{"guess":{"category":"door"},"block":{"family":"dorlib2d","has_arc":false,
      "width_mm":600},"wall":{"in_gap":false,"proximity":"negative"},
      "space":{"state":"positive"},"partition":{"near_mm":85}}
输出：{"decision":"accept","category":"door","subtype":"隔断门","confidence":0.72,
      "reason":"隔断块邻近，无弧门块符合隔断门"}

示例 B｜假窗（reject）
输入：{"guess":{"category":"window"},"block":{"family":"dorlib2d","has_arc":false},
      "wall":{"in_gap":false},"space":{"state":"negative"},"partition":{"near_mm":null}}
输出：{"decision":"reject","category":null,"subtype":null,"confidence":0.8,
      "reason":"无墙洞且同一空间，非窗"}

示例 C｜证据不足（uncertain）
输入：{"guess":{"category":"door"},"wall":{"in_gap":false,"proximity":"negative"},
      "space":{"state":"unavailable"},"partition":{"near_mm":1800}}
输出：{"decision":"uncertain","category":"door","subtype":null,"confidence":0.3,
      "reason":"墙洞与空间证据均缺失"}

证据：
{evidence}
"""


# --------------------------------------------------------------- 裁剪图（视觉证据）
CROP_HALF_MM = 2500.0      # 候选周围 ±2.5m
CROP_PX = 320


def _basemap_path() -> str | None:
    return os.environ.get("LLM_VERIFY_BASEMAP") or None


_BASEMAP_CACHE: dict[str, dict] = {}


def load_basemap(path: str | None = None) -> dict | None:
    """底图 JSON（tools/make_basemap.py 的产物），进程内缓存。"""
    path = path or _basemap_path()
    if not path or not os.path.exists(path):
        return None
    if path not in _BASEMAP_CACHE:
        try:
            with open(path, encoding="utf-8") as fh:
                _BASEMAP_CACHE[path] = json.load(fh)
        except (OSError, ValueError):
            return None
    return _BASEMAP_CACHE[path]


def render_crop(entry: dict, base: dict, half: float = CROP_HALF_MM,
                px: int = CROP_PX) -> str | None:
    """候选周围 ±half 的裁剪图 -> PNG 路径（浅底、灰线、红框标候选）。"""
    item = entry.get("item") or {}
    seg = item.get("opening") or item.get("axis")
    pts = []
    for key in ("insert", "center"):
        p = item.get(key)
        if p:
            pts.append([float(p[0]), float(p[1])])
    if seg and len(seg) == 2:
        pts += [[float(seg[0][0]), float(seg[0][1])], [float(seg[1][0]), float(seg[1][1])]]
    if not pts:
        return None
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    x0, y0, x1, y1 = cx - half, cy - half, cx + half, cy + half
    scale = px / (2 * half)

    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    img = Image.new("RGB", (px, px), (255, 255, 255))
    d = ImageDraw.Draw(img)

    def to_px(x, y):
        return ((x - x0) * scale, px - (y - y0) * scale)

    for s in base.get("segments") or ():
        if x1 < min(s[0], s[2]) or x0 > max(s[0], s[2]) or \
           y1 < min(s[1], s[3]) or y0 > max(s[1], s[3]):
            continue
        d.line([to_px(s[0], s[1]), to_px(s[2], s[3])], fill=(120, 128, 140), width=1)
    for t in (base.get("texts") or ())[:400]:
        if x0 <= t[0] <= x1 and y0 <= t[1] <= y1:
            d.text(to_px(t[0], t[1]), str(t[2])[:12], fill=(60, 70, 90))

    if seg and len(seg) == 2:
        a, b = to_px(seg[0][0], seg[0][1]), to_px(seg[1][0], seg[1][1])
        d.line([a, b], fill=(220, 40, 40), width=3)
    anchor = item.get("insert") or item.get("center")
    if anchor:
        ax, ay = to_px(anchor[0], anchor[1])
        d.ellipse([ax - 5, ay - 5, ax + 5, ay + 5], outline=(255, 170, 0), width=2)
    d.rectangle([2, 2, px - 3, px - 3], outline=(150, 150, 150))
    d.text((6, 6), f"{item.get('handle')} {item.get('block') or ''}"[:34], fill=(180, 30, 30))

    out = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"crop_{item.get('handle')}.png")
    img.save(out)
    return out


def _b64(path: str) -> str:
    import base64
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode()


VISION_NOTE = """
下面附一张该候选周围的局部裁剪图（白底；灰线=图纸原生线；红线段=该构件的洞口轴线；
黄圈=锚点；上方红字=handle 与块名）。

看图判断示例：
- 红线段正好落在两条墙线的缺口之间、两侧各有一个房间 → accept / door；
- 红线段压在门洞上、两侧同一房间、附近有隔断块 → accept / 隔断门；
- 红线段悬空、附近没有墙线 → uncertain。
仍然只输出 JSON。
"""


def _key(package: dict) -> str:
    blob = json.dumps(package, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _family(block_name) -> tuple[str, str]:
    name = str(block_name or "")
    fam = BLK.family_of(name)
    variant = name.split("$")[-1] if "$" in name else name
    return fam, variant


def route(entries, low: float = LOW_CONF) -> list[dict]:
    """低置信候选 -> [{entry, reasons}]。entry 由 :func:`openings.select` 产出。"""
    out = []
    for entry in entries or ():
        ev = entry.get("evidence") or {}
        score = entry.get("score")
        reasons = []
        if score is None or score < low:
            reasons.append("low_score")
        if entry.get("status") == "pending":
            reasons.append("pending")
        if ev.get("wall_proximity") == UNAVAILABLE or ev.get("span_overlap") == UNAVAILABLE:
            reasons.append("wall_unknown")
        if ev.get("space_connection") == UNAVAILABLE:
            reasons.append("space_unknown")
        if ev.get("block") == POS and ev.get("wall_proximity") == NEG:
            reasons.append("block_vs_wall")
        if reasons:
            out.append({"entry": entry, "reasons": reasons})
    return out


def package(entry: dict) -> dict:
    """候选 -> 给模型的证据包（只放判断用得上的量，不放坐标）。"""
    item = entry.get("item") or {}
    ev = entry.get("evidence") or {}
    fam, variant = _family(item.get("block"))
    return {
        "candidate": item.get("handle"),
        "guess": {"category": entry.get("kind") or item.get("category"),
                  "subtype": item.get("subtype")},
        "block": {"family": fam, "variant": variant,
                  "has_arc": bool(ev.get("hinge_jamb") not in (None, UNAVAILABLE)),
                  "layer": item.get("layer"), "width_mm": item.get("width")},
        "wall": {"in_gap": ev.get("opening_geometry") == POS,
                 "proximity": ev.get("wall_proximity"),
                 "thickness_mm": item.get("thickness")},
        "space": {"state": ev.get("space_connection"), "detail": ev.get("space_detail")},
        "partition": {"near_mm": (entry.get("context") or {}).get("partition_mm")},
        "evidence": {k: v for k, v in ev.items() if v != UNAVAILABLE},
        "geometry_confidence": None if entry.get("score") is None else round(entry["score"], 2),
    }


def _load_cache(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_cache(path: str, cache: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False)
    except OSError:
        pass


def _sanitize(raw: dict) -> dict:
    decision = str(raw.get("decision") or "uncertain").lower()
    if decision not in DECISIONS:
        decision = "uncertain"
    category = raw.get("category")
    if category not in CATEGORIES:
        category = None
    try:
        conf = float(raw.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf < MIN_LLM_CONF:
        decision = "uncertain"
    return {"decision": decision, "category": category,
            "subtype": raw.get("subtype"),
            "confidence": round(conf, 2),
            "reason": str(raw.get("reason") or "")[:40]}


def _parse_json(text: str) -> dict:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        t = t[i:j + 1]
    return json.loads(t)


def _ask(package_json: str, api_key: str, base: str, model: str,
         image_b64: str | None = None) -> dict:
    prompt = PROMPT.replace("{cats}", str(CATEGORIES)).replace("{evidence}", package_json)
    if image_b64:
        prompt = VISION_NOTE + prompt
        req = urllib.request.Request(
            base.rstrip("/") + "/chat/completions",
            data=json.dumps({"model": model, "temperature": 0, "messages": [
                {"role": "system", "content": "你是 CAD 构件识别复核员，只输出 JSON。"},
                {"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url",
                     "image_url": {"url": "data:image/png;base64," + image_b64}}]}]},
                ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + api_key})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
        return _sanitize(_parse_json(data["choices"][0]["message"]["content"]))
    return _sanitize(call_llm(prompt, api_key=api_key, base=base, model=model))


def verify(packages, *, api_key: str, base: str | None = None,
           model: str | None = None, max_workers: int = MAX_WORKERS,
           cache_path: str | None = None, images: dict | None = None) -> dict:
    """并发复核一批证据包，返回 {key: decision}。带磁盘缓存。"""
    try:
        max_workers = max(1, int(os.environ.get("LLM_VERIFY_WORKERS", max_workers)))
    except ValueError:
        pass
    base = base or os.environ.get("LLM_BASE") or DEFAULT_BASE
    model = model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
    cache_path = cache_path or os.environ.get(
        "DWG_LLM_VERIFY_CACHE", os.path.join(os.getcwd(), ".dwg-verify-cache.json"))
    cache = _load_cache(cache_path)
    todo, out = [], {}
    for pkg in packages:
        k = _key(pkg)
        if k in cache:
            out[k] = cache[k]
        else:
            todo.append((k, pkg))

    if todo:
        with futures.ThreadPoolExecutor(max_workers=min(max_workers, len(todo))) as pool:
            fut = {pool.submit(_ask, json.dumps(pkg, ensure_ascii=False),
                               api_key, base, model,
                               (images or {}).get(k)): k for k, pkg in todo}
            for f in futures.as_completed(fut):
                k = fut[f]
                try:
                    out[k] = f.result()
                except Exception as exc:                     # 网络/解析失败不阻塞识别
                    out[k] = {"decision": "uncertain", "category": None, "subtype": None,
                              "confidence": 0.0, "reason": f"error:{type(exc).__name__}"}
                if not str((out[k] or {}).get("reason", "")).startswith("error:"):
                    cache[k] = out[k]
        _save_cache(cache_path, cache)
    return out


def apply(pairs) -> dict:
    """把决策回填到候选：只改状态，不新增候选、不改几何。

    ``pairs`` 是 ``[(entry, decision), ...]``——决策按发起时的证据包哈希取，
    回填时不再重算哈希（视觉模式下证据包多一个 ``_vision`` 字段，重算会错位）。
    """
    stats = {"accept": 0, "reject": 0, "uncertain": 0, "revived": []}
    for entry, dec in pairs or ():
        if not dec:
            continue
        entry["llm"] = dec
        entry["llm_reason"] = dec.get("reason")
        verdict = dec["decision"]
        stats[verdict] = stats.get(verdict, 0) + 1
        if verdict == "accept":
            entry["status"] = "accept"
            entry["reason"] = "llm_verified:" + (dec.get("subtype") or "")
            if entry.get("kind") and not entry["item"].get("_llm_accept"):
                entry["item"]["_llm_accept"] = True
                stats["revived"].append(entry)
        elif verdict == "reject":
            entry["status"] = "reject"
            entry["reason"] = "llm_rejected:" + (dec.get("reason") or "")
            entry["item"]["_llm_reject"] = True
        else:
            entry["status"] = "pending"
    return stats


def run(entries, *, api_key: str | None = None, base: str | None = None,
        model: str | None = None, low: float = LOW_CONF) -> dict:
    """路由 -> 并发复核 -> 回填。返回可放进 report 的摘要。"""
    routed = route(entries, low)
    if not routed:
        return {"routed": 0, "called": 0, "llm": "skipped"}
    api_key = CRED.api_key(api_key)
    if not api_key:
        return {"routed": len(routed),
                "reasons": {r: sum(1 for x in routed if r in x["reasons"])
                            for r in ("low_score", "pending", "wall_unknown",
                                      "space_unknown", "block_vs_wall")},
                "called": 0, "llm": "unavailable"}
    packages = [package(x["entry"]) for x in routed]
    images = {}
    vision = bool(os.environ.get("LLM_VERIFY_VISION"))
    if vision:
        base_map = load_basemap()
        if base_map:
            for pkg, x in zip(packages, routed):
                path = render_crop(x["entry"], base_map)
                if path:
                    images[_key(pkg)] = _b64(path)
                    pkg["_vision"] = True
            model = model or CRED.vision_model(default="cn:glm-4.6v")
    decisions = verify(packages, api_key=api_key, base=base, model=model, images=images)
    applied = apply([(x["entry"], decisions.get(_key(pkg)))
                     for pkg, x in zip(packages, routed)])
    return {"routed": len(routed), "called": len(packages),
            "llm": model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL,
            "decision_counts": {k: applied[k] for k in ("accept", "reject", "uncertain")},
            "revived": [r["item"].get("handle") for r in applied["revived"]],
            "details": [{"handle": x["entry"]["item"].get("handle"),
                         "reasons": x["reasons"],
                         "llm": (x["entry"].get("llm") or {}).get("deal",
                                (x["entry"].get("llm") or {}).get("decision")),
                         "why": (x["entry"].get("llm") or {}).get("reason")}
                        for x in routed]}
