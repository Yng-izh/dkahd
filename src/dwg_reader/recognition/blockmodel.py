"""块语义归一：不靠块名白名单，靠"块族 + 块长相"。

块名里的变体号（``$DorLib2D$00000131`` 的 ``00000131``）是实例序号，不含语义；
语义来自块定义里的几何构成，因此换图纸、换图库也能迁移。

职责边界（重要）：

* 本模块只回答"这一族/这块**像**什么"，输出 ``(类别, 置信度)``；
* 一樘门窗到底成不成立，由洞口、墙网、两侧空间这些**实例证据**裁决
  （见 :mod:`openings` / :mod:`walls`），这里不做最终判定。

因此长相看错时，最坏结果是"没认出来 → 标 uncertain"，而不是"错构件"。
"""
from __future__ import annotations

import math
import re

_SEQ = re.compile(r"\d{3,}")
#: 长相判定的最低置信度（低于它不作为候选来源）
MIN_CONF = 0.7
#: 门摆动弧的张角区间（度）
SWING_RANGE = (70.0, 110.0)


def family_of(name: str) -> str:
    """块名 -> 族名：去掉变体号与装饰符号（$DorLib2D$00000131 -> dorlib2d）。"""
    n = (name or "").lower().replace("$", "_")
    n = _SEQ.sub("", n)
    n = re.sub(r"[_\-:]+", "_", n).strip("_")
    return n or "unknown"


def signature(block: dict, insert: dict | None = None) -> dict:
    """块定义 -> 形状签名（只描述形状，不含语义）。

    给出 ``insert``（任一实例）时按 |xscale|/|yscale| 还原**真实长宽比**；
    不给则退化为块定义自身尺寸（块被画成 1×1 时 aspect 恒为 1）。
    """
    arcs = block.get("arcs") or []
    lines = block.get("lines") or []
    loops = block.get("loops") or []

    sweeps = []
    for a in arcs:
        try:
            sweeps.append(abs((float(a[4]) - float(a[3])) % 360.0))
        except (IndexError, TypeError, ValueError):
            continue

    lengths = []
    xs, ys = [], []
    for ln in lines:
        try:
            x1, y1, x2, y2 = (float(v) for v in ln[:4])
        except (TypeError, ValueError):
            continue
        lengths.append(math.hypot(x2 - x1, y2 - y1))
        xs += [x1, x2]
        ys += [y1, y2]
    for a in arcs:
        try:
            cx, cy, r = float(a[0]), float(a[1]), float(a[2])
        except (IndexError, TypeError, ValueError):
            continue
        xs += [cx - r, cx + r]
        ys += [cy - r, cy + r]
    for loop in loops:
        for p in loop or ():
            xs.append(float(p[0]))
            ys.append(float(p[1]))

    # 真实尺寸：块定义常被画成 1×1 单位方框，真实长宽比在 INSERT 的
    # xscale/yscale 上。把宽高归一化掉，会把"1500×200 的窗"和"500×500 的柱"
    # 判成一模一样——实测 $WinLib2D$ / _WBLK / $TCHSYS$WIN2D 全被判成 column。
    sx = sy = 1.0
    if insert:
        try:
            sx = abs(float(insert.get("xscale") or 1.0)) or 1.0
            sy = abs(float(insert.get("yscale") or 1.0)) or 1.0
        except (AttributeError, TypeError, ValueError):
            sx = sy = 1.0
    w = ((max(xs) - min(xs)) * sx) if xs else 0.0
    h = ((max(ys) - min(ys)) * sy) if ys else 0.0
    short, long_ = min(w, h), max(w, h)

    parallel = 0
    for i in range(len(lines)):
        for j in range(i + 1, len(lines)):
            try:
                ax = math.atan2(lines[i][3] - lines[i][1], lines[i][2] - lines[i][0])
                bx = math.atan2(lines[j][3] - lines[j][1], lines[j][2] - lines[j][0])
            except (IndexError, TypeError, ValueError):
                continue
            d = abs(ax - bx) % math.pi
            if min(d, math.pi - d) < 0.05:
                parallel += 1

    return {"arcs": len(arcs), "sweeps": sweeps, "lines": len(lines),
            "parallel": parallel, "loops": len(loops), "lengths": lengths,
            "w": round(w, 1), "h": round(h, 1),
            "aspect": round(long_ / short, 2) if short else 0.0,
            "scaled": bool(insert)}


def classify(sig: dict):
    """签名 -> (类别, 置信度)；判不出返回 ``(None, 0.0)``。

    只覆盖"长得有辨识度"的几类；其余一律返回 None，交由实例证据处理。
    """
    arcs, lines = sig["arcs"], sig["lines"]
    sweeps, aspect = sig["sweeps"], sig["aspect"]

    # 门：带 70~110° 摆动弧 + 至少一条门扇线（天正/中望/自绘门块都符合）
    if arcs and any(SWING_RANGE[0] <= s <= SWING_RANGE[1] for s in sweeps) and lines >= 1:
        return "door", 0.85
    # 窗：无弧、>=2 条平行线、细长（窗绘制成 2~4 条长线）
    if not arcs and lines >= 2 and sig["parallel"] >= 1 and aspect >= 2.0:
        return "window", 0.75
    # 柱：无弧、闭合近方形（>=3 条线或一条闭合轮廓）
    if not arcs and (sig["loops"] >= 1 or lines >= 3) and 0.6 <= aspect <= 1.7:
        return "column", 0.80
    # 轴号：整圆 + 几乎无线
    if arcs and sweeps and all(s >= 350.0 for s in sweeps) and lines <= 1:
        return "grid_axis", 0.60
    return None, 0.0


def classify_block(block: dict, family: str = "", families: dict | None = None):
    """块定义 -> (类别, 置信度, 依据)。profile 的族表优先于长相。"""
    hint = (families or {}).get(family_of(family or ""))
    if hint:
        return hint, 1.0, f"family:{family_of(family)}"
    kind, conf = classify(signature(block))
    return kind, conf, ("shape" if kind else "none")


def kinds_by_block(snapshot: dict, families: dict | None = None,
                   min_conf: float = MIN_CONF) -> dict:
    """块名 -> (类别, 置信度, 依据)。

    两遍：先按块自己的长相判；判不出的块**继承同族**的结论——同一族是同一个
    符号反复复制（``$DorLib2D$00000131`` 与 ``$DorLib2D$00000001`` 同族），
    族内任一成员的长相就足以代表整族，这一层的误判风险最低。
    """
    blocks = snapshot.get("blocks") or {}
    out: dict[str, tuple] = {}
    fam: dict[str, tuple] = {}
    for name, block in blocks.items():
        k, conf, why = classify_block(block, name, families)
        if k and conf >= min_conf:
            out[name] = (k, conf, why)
            fam.setdefault(family_of(name), (k, conf, why))
    for name in blocks:
        if name in out:
            continue
        f = family_of(name)
        if f in fam:
            k, conf, why = fam[f]
            out[name] = (k, max(min_conf, conf * 0.8), f"inherit:{f}")
    return out


def instance_scales(snapshot: dict) -> dict[str, tuple[float, float, int]]:
    """块名 -> (|xscale| 中位数, |yscale| 中位数, 实例数)。

    同一族块常被不同尺寸复用（950×200 的窗、1200×200 的窗），取中位数代表
    "这一族在图上被怎么用"，实例数用于"只出现一次的不立族"这条纪律。
    """
    acc: dict[str, list[tuple[float, float]]] = {}
    for e in snapshot.get("entities") or ():
        if e.get("type") != "INSERT":
            continue
        try:
            sx = abs(float(e.get("xscale") or 1.0))
            sy = abs(float(e.get("yscale") or 1.0))
        except (TypeError, ValueError):
            continue
        acc.setdefault(str(e.get("name")), []).append((sx, sy))
    out: dict[str, tuple[float, float, int]] = {}
    for name, vals in acc.items():
        xs = sorted(v[0] for v in vals)
        ys = sorted(v[1] for v in vals)
        out[name] = (xs[len(xs) // 2], ys[len(ys) // 2], len(vals))
    return out


def kinds_by_insert(snapshot: dict, families: dict | None = None,
                    min_conf: float = MIN_CONF) -> dict[str, tuple]:
    """块名 -> (类别, 置信度, 依据)：用**真实插入尺寸**算长宽比。

    与 :func:`kinds_by_block` 只差一处，但很关键：块定义常被画成 1×1 单位方框，
    真实长宽比在 INSERT 的 xscale/yscale 上。归一化签名会让窗块命中"闭合近方形"
    分支，全部被判成柱（连当年唯一能认的 ``$TCHSYS$WIN2D`` 也一样）。

    块名/族表仍优先（那是可信先验），这里只补"名字不认识但长得像"的那部分。
    """
    blocks = snapshot.get("blocks") or {}
    scales = instance_scales(snapshot)
    out: dict[str, tuple] = {}
    for name, block in blocks.items():
        hint = (families or {}).get(family_of(name))
        if hint:
            out[name] = (hint, 1.0, f"family:{family_of(name)}")
            continue
        sx, sy, n = scales.get(name, (0.0, 0.0, 0))
        if not n:
            continue          # 没被插入过的块定义不产生任何判定（也避免白算签名）
        kind, conf = classify(signature(block, {"xscale": sx, "yscale": sy}))
        if kind and conf >= min_conf:
            out[name] = (kind, conf, f"shape:insert_scale|n={n}")
    return out


def families_by_kind(snapshot: dict, seeds=(), families: dict | None = None,
                     min_conf: float = MIN_CONF) -> dict:
    """族 -> 类别：**只由已知块**（白名单成员 / profile 族表）给族定性。

    孤立的"形状像门"的块不自己立族——家具、索引符号里也有 90° 弧，若让它们
    自立门户就会过检（实测办公楼门 30 -> 44）。族语义只从可信成员往外扩散。
    """
    out: dict[str, str] = {}
    blocks = snapshot.get("blocks") or {}
    for name in seeds or ():
        block = blocks.get(name)
        if block is None:
            continue
        k, conf, _why = classify_block(block, name, families)
        if k and conf >= min_conf:
            out.setdefault(family_of(name), k)
    for name in blocks:
        f = family_of(name)
        hint = (families or {}).get(f)
        if hint and f not in out:
            out[f] = hint
    return out


def names_like(snapshot: dict, kind: str, families: dict | None = None,
               seeds=(), min_conf: float = MIN_CONF) -> dict:
    """找出与"已知 kind 块"**同族**的全部块名 -> 依据串（扩候选来源用）。

    只做同族扩散，不做跨族猜测：块名仍只作溯源标签，逐樘由洞口/空间证据裁决。
    """
    fk = families_by_kind(snapshot, seeds, families, min_conf)
    return {n: f"family:{family_of(n)}" for n in (snapshot.get("blocks") or {})
            if fk.get(family_of(n)) == kind}
