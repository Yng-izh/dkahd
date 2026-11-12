"""LLM 离线图纸适配：把任意图纸的图层/画法约定映射成识别方案。

只在离线生成 profile.json 时调用模型；识别全程离线、可复现、运行期零 LLM。

模型允许输出三类内容（都是"解释"，不是"识别结果"）：

* ``layers``：图层 -> 语义（闭集），
* ``symbols`` / ``operator_hints``：画法与算子优先级（提示，非开关），
* ``unit_hypothesis``：单位假设（必须由几何证据确认，见 :mod:`geometry`）。

**不允许**输出构件坐标、墙厚猜测值、洞口宽度阈值或任何图纸专属数值容差；
``tolerances`` 已从提示词中删除，校验时也会被丢弃。
"""
from __future__ import annotations

import json
import os
import urllib.request
from collections import Counter
from pathlib import Path

from . import llm_credentials as CRED
from .operators import OPERATORS

SEMANTICS = ("wall", "column", "door", "window", "room_name", "axis",
             "stair", "step", "apron", "north", "frame", "furniture", "ignore")
SYMBOLS = ("block", "closed_rect", "collinear_multi", "arc_hinge",
           "paired_ticks", "text_suffix")

#: 端点与模型名也走凭据模块：本地 llm.local.json / 环境变量可以覆盖，
#: 代码里这份只是"默认值"，不再是被 shell export 决定的东西。
DEFAULT_BASE = CRED.base(default="http://101.43.0.41:6010/v1")
DEFAULT_MODEL = CRED.model(default="cn:deepseek-v4.1-flash")
DEFAULT_VISION_MODEL = CRED.vision_model(default="cn:glm-4.6v")

KEYWORDS = {
    "wall": ("wall", "墙"), "column": ("column", "柱"), "door": ("door", "门"),
    "window": ("window", "窗", "curtwall"), "room_name": ("text", "文字", "name", "房间"),
    "axis": ("axis", "轴", "dote"), "stair": ("stair", "梯"), "step": ("step", "台阶"),
    "apron": ("apron", "散水"), "north": ("north", "北"), "frame": ("title", "图框", "tk"),
}

PROMPT = """你是 CAD 图纸识别助手。下面是一张 DXF 的图层清单：每层给出实体类型计数、
样例文字、块名、线角度分布，以及 geom（画法指纹，含义见下）。请判断每个图层的语义，
并给出"画法"层面的适配建议。

geom 各字段含义（都是比例 0~1，除 thickness_mm）：
- line_ratio 直线占比；pair_rate 成对平行线占比；long_pair_rate 长平行对（≥1500mm）占比；
- short_pair_rate 短平行对占比；on_wall_ratio 这些短平行对贴着**别的层**长直线的比例；
- thickness_mm / thickness_concentration 平行对间距的众数及其集中度（墙厚证据）；
- arc_ratio 弧线占比（门的开启弧）；closed_ratio 闭合轮廓占比；
- text_ratio 文字占比；block_ratio 块参照占比；cross_span 该层在图幅内的跨度占比。

判断规则（先看 geom，再看层名；两者冲突时以 geom 为准并在 reason 里说明）：
1. 双线墙：long_pair_rate 或 short_pair_rate 高，且 thickness_mm 有值、concentration 高 → wall；
2. 洞口/留洞/开洞/孔洞（名字含 洞/开洞/留洞/open/hole）：只是**洞口的边框**，
   不构成墙实体（closed_ratio 高、pair_rate 低、无厚度集中）→ 一律 ignore，
   绝不能判 wall；同族层（开洞 / D-开洞 / k开洞）必须同一语义；
3. 门：arc_ratio 明显大于 0（有开启弧），或 block_ratio 高且块名/样例含 door、门、M → door；
4. 窗：长平行线对贴在墙线上（on_wall_ratio 高）或 thickness 与墙厚一致 → window；
   幕墙/玻璃类可同时给 ["wall","window"]；
5. 柱：closed_ratio 高、轮廓自成一对方块、cross_span 小 → column；
6. 文字层：text_ratio 高（>0.5）→ 只能给 room_name 或 ignore，
   **不许**判 furniture/column/wall/door/window；样例是三维标高（±0.000、3.600）
   或轴号、编号 → ignore；是房间名（如"办公室""电梯厅"）→ room_name；
7. 家具/洁具/设备：block_ratio 高、无厚度、arc_ratio 低 → furniture；
   卫生间隔断（LVTRY/lavatory/隔断）若无门弧也按 furniture，有门弧再加 door；
8. 同族层（前缀相同、后缀不同）除非 geom 明显不同，否则必须给同一语义；
9. 吊轨/轨道/灯带/窗帘盒/天花造型这类**线性配件**：即使有长平行线对，
   也不是墙（混进墙集合会打断"弧线—墙"配对），一律 ignore；
10. reason 必须写到具体依据（如"closed_ratio 0.94、无厚度、仅轮廓"），不超过 20 字。

语义取值：{semantics}

要求：
1. 只输出 JSON，不要解释；
2. 每个图层给出 semantics（数组）、confidence(0-1)、reason(不超过20字)；
   一个图层可有多个语义，例如幕墙/玻璃：["wall","window"]；门窗表/图例/填充：["ignore"]；
3. 不确定就用 ["ignore"]，confidence 低于 0.6 不要猜；
4. symbols：{"column": [...], "door": [...]}，说明该构件在本图上的**画法**（几何表达，
   不是阈值）：block / block:块名 / closed_rect / collinear_multi / arc_hinge /
   paired_ticks / single_line / parallel_lines；
5. operator_hints：从 {operators} 里选，按优先级排列；漏选不影响识别；
6. unit_hypothesis：只能给 mm/cm/m/inch 之一，且必须能被几何证据证实；拿不准留空串；
7. 不要给任何数值容差/阈值（snap、gap、min_wall 之类），阈值属于确定性代码。

输出格式：
{"layers":{"<层名>":{"semantics":["wall"],"confidence":0.9,"reason":"不超过20字"}},
 "symbols":{"column":["closed_rect"],"door":["arc_hinge"]},
 "operator_hints":{"door":["arc_feature"],"window":["line_bundle"]},
 "unit_hypothesis":"mm","scope":{"frame_layer":"<层名>"}}

示例（取值示范，不要照抄）：
{"layers":{"CURTWALL":{"semantics":["wall","window"],"confidence":0.9,"reason":"幕墙玻璃"},
           "PUB_TEXT":{"semantics":["room_name"],"confidence":0.85,"reason":"公共文字"},
           "AE-EQPM-TOLT":{"semantics":["furniture"],"confidence":0.7,"reason":"设备重复布置"},
           "DOOR_FIRE":{"semantics":["door"],"confidence":0.92,"reason":"防火门图层"},
           "WD-图例":{"semantics":["ignore"],"confidence":0.8,"reason":"图例非实体"}},
 "symbols":{"column":["closed_rect","block"],"door":["arc_hinge","block:块名前缀"],
            "window":["parallel_lines"]},
 "operator_hints":{"door":["arc_feature"],"window":["line_bundle"],
                   "column":["closed_contour"]},
 "unit_hypothesis":"mm","scope":{"frame_layer":"PUB_TITLE"}}
"""


#: 随层一起送模型的**画法指纹**：这些量能区分"双线墙 / 洞口轮廓 / 文字层 / 家具"，
#: 只给实体类型计数是不够的——`开洞` 层和 `WALL` 层都是 LINE，模型只能靠名字猜。
GEOM_KEYS = ("line_ratio", "pair_rate", "long_pair_rate", "short_pair_rate",
             "on_wall_ratio", "thickness_mm", "thickness_concentration",
             "arc_ratio", "closed_ratio", "text_ratio", "block_ratio",
             "cross_span")


def layer_signature(dxf_path: str, sample_texts: int = 4) -> dict:
    """统计每个图层的实体构成、画法指纹与样例文字，供模型判断语义与画法。"""
    import ezdxf

    from . import layer_roles_infer as LRI

    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()
    fps = LRI.fingerprints_from_msp(doc, msp)
    return signature_from_msp(msp, doc=doc, sample_texts=sample_texts, fps=fps)


def signature_from_msp(msp, *, doc=None, sample_texts: int = 4, fps=None) -> dict:
    """同 :func:`layer_signature`，但直接吃**已加载**的 modelspace。

    运行期自动适配必须复用 pipeline 已经解析过的文档：172MB 的图再解析一遍要多花
    几十秒，而这里只是统计实体，不需要新的解析结果。
    """
    stats: dict[str, dict] = {}
    heights = []
    for e in msp:
        layer = e.dxf.get("layer", "0")
        rec = stats.setdefault(layer, {"types": Counter(), "texts": [], "blocks": Counter(),
                                       "angles": Counter(), "lengths": []})
        t = e.dxftype()
        rec["types"][t] += 1
        if t == "INSERT":
            rec["blocks"][e.dxf.name] += 1
        elif t in ("TEXT", "MTEXT"):
            if len(rec["texts"]) < sample_texts:
                txt = (e.dxf.text if t == "TEXT" else e.plain_text()).strip()
                if txt:
                    rec["texts"].append(txt[:24])
            h = (e.dxf.height if t == "TEXT" else e.dxf.char_height)
            if h:
                heights.append(float(h))
        elif t == "LINE":
            dx = float(e.dxf.end.x - e.dxf.start.x)
            dy = float(e.dxf.end.y - e.dxf.start.y)
            length = (dx * dx + dy * dy) ** 0.5
            if length > 1e-6:
                ang = round((__import__("math").degrees(__import__("math").atan2(dy, dx))) % 180.0)
                rec["angles"][int(ang)] += 1
                rec["lengths"].append(length)
    out = {}
    for layer, rec in stats.items():
        lens = sorted(rec["lengths"])
        out[layer] = {
            "total": sum(rec["types"].values()),
            "entities": dict(rec["types"].most_common(6)),
            "sample_texts": rec["texts"],
            "blocks": dict(rec["blocks"].most_common(4)),
            "line_angles": {str(k): v for k, v in rec["angles"].most_common(4)},
            "line_length": ([round(lens[0], 1), round(lens[len(lens) // 2], 1),
                             round(lens[-1], 1)] if lens else None),
        }
    for layer, rec in out.items():
        fp = (fps or {}).get(layer)
        if not fp:
            continue
        rec["geom"] = {k: fp[k] for k in GEOM_KEYS if fp.get(k) is not None}
    out["_drawing"] = {"text_height_median":
                       (sorted(heights)[len(heights) // 2] if heights else None),
                       "insunits": (doc.header.get("$INSUNITS")
                                    if doc is not None else None)}
    return out


def _post(url: str, body: dict, api_key: str, timeout: int = 120) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def call_llm(prompt: str, *, api_key: str, base: str = DEFAULT_BASE,
             model: str = DEFAULT_MODEL, timeout: int = 120) -> dict:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是 CAD 图纸识别助手，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    data = _post(base.rstrip("/") + "/chat/completions", body, api_key, timeout)
    text = data["choices"][0]["message"]["content"].strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
    return json.loads(text)


def heuristic_profile(sig: dict) -> dict:
    """关键词兜底：模型不可用时的最小可用映射。"""
    layers = {}
    for name in sig:
        low = name.lower()
        for sem, words in KEYWORDS.items():
            if any(w in low for w in words):
                layers[name] = {"semantics": [sem], "confidence": 0.5, "reason": "关键词命中"}
                break
    return {"layers": layers, "symbols": {}, "scope": {}}


def validate(raw: dict, layers: set[str]) -> dict:
    """丢弃模型编造的图层/取值，保留可执行部分。"""
    out = {"layers": {}, "symbols": {}, "scope": {}, "errors": []}
    for name, item in (raw.get("layers") or {}).items():
        if name not in layers:
            out["errors"].append(f"未知图层 {name} 已丢弃")
            continue
        sems = item.get("semantics", item.get("semantic", "ignore"))
        if isinstance(sems, str):
            sems = [sems]
        sems = [x for x in sems if x in SEMANTICS] or ["ignore"]
        if "ignore" in sems and len(sems) > 1:
            sems = [x for x in sems if x != "ignore"]
        out["layers"][name] = {"semantics": sems,
                               "confidence": float(item.get("confidence", 0)),
                               "reason": str(item.get("reason", ""))[:40]}
    for key, kinds in (raw.get("symbols") or {}).items():
        if key not in SEMANTICS:
            continue
        out["symbols"][key] = [k for k in kinds
                               if k == "block" or k.startswith("block:") or k in SYMBOLS]
    scope = raw.get("scope") or {}
    if scope.get("frame_layer"):
        out["scope"]["frame_layer"] = scope["frame_layer"]
    from .operators import OPERATORS

    hints = {}
    for sem, names in (raw.get("operator_hints") or raw.get("operators") or {}).items():
        if sem not in SEMANTICS or not isinstance(names, (list, tuple)):
            continue
        keep = [n for n in names if n in OPERATORS]
        if keep:
            hints[sem] = keep
    out["operator_hints"] = hints
    units = str(raw.get("unit_hypothesis") or "").lower() or None
    # 只是假设：真正的缩放由 geometry.detect_units 的几何证据决定
    out["unit_hypothesis"] = units if units in ("mm", "cm", "m", "inch", "in", "ft") else None
    # 明确丢弃图纸专属数值容差（阈值属于确定性代码）
    out["discarded_keys"] = [k for k in ("tolerances", "thresholds") if k in raw]
    return out


def infer_symbols(sig: dict, layers: dict) -> dict:
    """模型没给 symbols 时，按图层实体构成推断画法（确定性兜底）。"""
    out: dict[str, list[str]] = {}

    def kinds(layer):
        return (sig.get(layer) or {}).get("entities") or {}

    for layer, item in layers.items():
        k = kinds(layer)
        for sem in item.get("semantics") or [item.get("semantic")]:
            if sem == "column" and ("LINE" in k or "LWPOLYLINE" in k):
                out.setdefault("column", []).append("closed_rect")
            elif sem == "door" and "ARC" in k:
                out.setdefault("door", []).append("arc_hinge")
            elif sem == "window" and "LINE" in k:
                out.setdefault("window", []).append("collinear_multi")
            elif sem == "wall" and "LINE" in k:
                out.setdefault("wall", []).append("double_line")
            elif sem == "axis" and "LINE" in k:
                out.setdefault("axis", []).append("paired_ticks")
    arcs = sum((rec.get("entities") or {}).get("ARC", 0) for rec in sig.values())
    if arcs >= 20:
        out.setdefault("door", []).append("arc_hinge")
    return {k: sorted(set(v)) for k, v in out.items()}


def build_profile(dxf_path: str, *, api_key: str | None = None,
                  base: str = DEFAULT_BASE, model: str = DEFAULT_MODEL,
                  out: str | None = None, timeout: int = 180) -> dict:
    from .operators import OPERATORS
    from .taxonomy import SEMANTICS as TAX, is_never_role, match_layer

    sig_all = layer_signature(dxf_path)
    drawing = sig_all.get("_drawing") or {}
    # 结构性排除先于一切：0 / Defpoints 既不值得送模型，也不许进方案
    sig = {k: v for k, v in sig_all.items()
           if not k.startswith("_") and not is_never_role(k)}
    layers: dict[str, dict] = {}
    unmatched: list[str] = []
    for name in sig:
        sems = match_layer(name)
        if sems:
            layers[name] = {"semantics": list(sems), "confidence": 1.0,
                            "reason": "人工别名规则", "source": "alias"}
        else:
            unmatched.append(name)

    api_key = CRED.api_key(api_key)
    llm_error = None
    llm_raw = None
    #: 模型到底答没答成，必须显式记下来：以前 403 被 except 吞掉，产出的其实是
    #: "只有别名规则"的残缺方案，但文件看起来是正常的，谁也没发现。
    llm_status = "skipped"
    if not unmatched:
        llm_status = "not_needed"
    elif not api_key:
        llm_status = "no_key"
    elif unmatched and api_key:
        from .operators import OPERATORS as _OPS
        prompt = (PROMPT.replace("{semantics}", str(TAX))
                  .replace("{operators}", str(sorted(_OPS))))
        prompt += ("\n只能从上面的语义枚举与算子名中选择，不得新增类别；不确定选 [\"ignore\"]。\n"
                   "整图统计：\n" + json.dumps(drawing, ensure_ascii=False) +
                   "\n图层清单：\n" + json.dumps({k: sig[k] for k in unmatched},
                                             ensure_ascii=False, indent=1))
        try:
            raw = call_llm(prompt, api_key=api_key, base=base, model=model,
                           timeout=timeout)
            llm_raw = raw
            llm_status = "ok"
            for name, item in (raw.get("layers") or {}).items():
                if name not in unmatched:
                    continue
                sems = item.get("semantics", item.get("semantic", "ignore"))
                if isinstance(sems, str):
                    sems = [sems]
                sems = [x for x in sems if x in TAX] or ["ignore"]
                layers[name] = {"semantics": sems,
                                "confidence": float(item.get("confidence", 0)),
                                "reason": str(item.get("reason", ""))[:40],
                                "source": "llm"}
        except Exception as exc:
            llm_error = str(exc)[:200]
            llm_status = "llm_error"

    rest = [n for n in unmatched if n not in layers]
    if rest:
        fb = heuristic_profile({n: sig[n] for n in rest})
        for name, item in fb["layers"].items():
            layers[name] = {"semantics": item["semantics"],
                            "confidence": item["confidence"],
                            "reason": item["reason"], "source": "heuristic"}

    hints, unit_hypothesis, scope, errors = {}, None, {}, []
    if llm_raw:
        checked = validate(llm_raw, set(unmatched))
        hints = checked.get("operator_hints") or {}
        unit_hypothesis = checked.get("unit_hypothesis")
        scope = checked.get("scope") or {}
        errors = checked.get("errors") or []
    # 图框层必须是真实图层；模型给 "0" 之类的场景层一律丢弃，
    # 否则区域裁剪会按图层 0 的辅助线切图。
    frame_layer = (scope or {}).get("frame_layer")
    if frame_layer and (is_never_role(frame_layer) or frame_layer not in sig):
        errors = list(errors) + [f"图框层 {frame_layer} 不是有效图层，已丢弃"]
        scope = {k: v for k, v in (scope or {}).items() if k != "frame_layer"}
    judged = sorted(n for n, v in layers.items() if v.get("source") == "llm")
    profile = {"profile_version": "2.0",
               "layers": layers, "symbols": infer_symbols(sig, layers),
               "operator_hints": hints, "unit_hypothesis": unit_hypothesis,
               "scope": scope, "errors": errors,
               "drawing": drawing,
               "reviewed": False,
               "llm_status": llm_status,
               "model_judged": judged,
               # unreviewed = 模型没判、只靠关键词兜底的层；这些层是方案里最可疑的部分
               "unreviewed": sorted(n for n, v in layers.items()
                                    if v.get("source") not in ("alias", "llm")),
               "source": str(dxf_path), "model": model}
    if llm_error:
        profile["llm_error"] = llm_error
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    return profile


def trial(profile: dict, dxf_path: str, **kwargs) -> dict:
    """离线试运行：用这份 profile 跑一遍识别，返回计数与诊断摘要。

    "适配方案验证"的确定性部分：跑不通 / 计数异常就说明方案不可用，不写注册表。
    """
    from .pipeline import run_recognition
    run = run_recognition(dxf_path, profile=profile, targets=("assemble",), **kwargs)
    result = run.output
    meta = result.get("meta") or {}
    return {"counts": meta.get("counts") or {},
            "total": len(result.get("components") or []),
            "unrecognized": len(result.get("unrecognized") or []),
            "units": meta.get("units"), "units_resolved": meta.get("units_resolved"),
            "openings": meta.get("openings") or {},
            "spaces": meta.get("spaces") or {},
            "stats": {k: v.get("status") for k, v in (run.stats or {}).items()}
            if isinstance(run.stats, dict) else {}}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="用 LLM 生成图纸 profile")
    ap.add_argument("dxf")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    a = ap.parse_args()
    p = build_profile(a.dxf, base=a.base, model=a.model, out=a.out)
    print(json.dumps({"layers": len(p["layers"]), "symbols": p["symbols"],
                      "scope": p["scope"], "errors": p["errors"]}, ensure_ascii=False))
