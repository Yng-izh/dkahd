"""判定优先级：多条判据同时命中同一个图元时，**按证据强度裁决，不按谁先跑完**。

为什么需要这一层
----------------
实测（艺术楼-图层混乱）：一个 5.5 米长、180 毫米厚（= 墙厚）、旁边写着窗号 C15、
画在 WINDOW 层上的窗块，被「门的形状判据」抢先命中，输出成一樘「5538mm 宽的门」。
根因不是某条判据写错了，而是**各条路径各跑各的、先到先得**——没有任何一层在
冲突时按证据强弱做裁决。

六级优先级（高 -> 低）
--------------------
L1 显式标注：图上的门窗编号 / 块属性（``C15`` / ``M3``）。制图人自己写的，不是推断。
L2 块族语义：块名先验 + 几何指纹（见 blockfamily）。块是构件身份证，且同族重复出现可互证。
L3 互斥形态：物理上不可能错的特征。窗=厚度≈墙厚且长宽比≥3；门=有门扇开启弧且半径≥墙厚；
              柱=实心四边形且尺寸在柱截面区间。**一条命中即锁定，别的判据不许抢。**
L4 图层角色：WINDOW / COLUMN / 门层声明。制图人的标注，但会被压平、会画错层。
L5 上下文：落在墙缺口上、两侧是空间、贴着柱。只能佐证，不能定类。
L6 统计拓扑：重复次数、轴网交点、连通性。相关性，不是因果。

模型（LLM）不在这一表里：它只允许**否决**已经生成的候选，不允许新增、不允许改类。

两条裁决纪律
------------
1. **高优先级覆盖低优先级，必须留痕**：覆盖结果写进 ``override`` 字段（谁覆盖了谁、
   依据哪一级、原判据是什么），导出时可审计。
2. **同级冲突不定案**：两个 L3 特征互相矛盾（既像门又像窗）时标 ``needs_review``，
   不猜——猜错的代价是算量错，标出来的代价只是看一眼。
"""
from __future__ import annotations

import math
import re

#: 级别名称（数字越小越强）
LEVELS = {
    1: "显式标注",
    2: "块族语义",
    3: "互斥形态",
    4: "图层角色",
    5: "上下文",
    6: "统计拓扑",
}

#: 门窗编号的前缀约定：C/GC = 窗，M/GM/FHM/MLC = 门
WIN_CODE = re.compile(r"^\s*(GC|C)\s*\d+", re.I)
DOOR_CODE = re.compile(r"^\s*(GM|M|FHM|MLC|FM)\s*\d+", re.I)


def code_category(code):
    """门窗编号 -> ('door'|'window'|None)。C 是窗，M 是门，这是制图通用约定。"""
    if not code:
        return None
    text = str(code).strip()
    if WIN_CODE.match(text):
        return "window"
    if DOOR_CODE.match(text):
        return "door"
    return None


def form_category(rec, wall_thick):
    """L3 互斥形态 -> ('door'|'window'|None, 依据)。

    比的是物理关系，不是某张图的习惯：
      * 有门扇开启弧（hinges + swing）且门扇半径不小于墙厚 -> 门；
      * 厚度约等于墙厚、沿墙向长宽比 >= 3 -> 窗（通长窗/带形窗）。
    """
    if not rec:
        return None, ""
    thick = float(rec.get("thickness") or 0.0)
    width = float(rec.get("width") or 0.0)
    wt = float(wall_thick or 0.0)
    has_swing = bool(rec.get("swing")) and bool(rec.get("hinges"))
    # 门扇宽度必须像"一扇门"：3~8 倍墙厚之间（180 墙 -> 540~1440mm，正是常规门宽）。
    # 5538mm 的"门扇"不存在——那个尺寸靠的是"卷帘门/门洞"这类无扇洞口，
    # 而卷帘门没有开启弧。所以"有弧"还要加上"扇宽像扇"。
    # 上界放到 10 倍墙厚：1500mm 的双扇门在 180 墙上正好是 8.3 倍，卡 8 倍会误杀。
    leaf_lo, leaf_hi = 3.0 * wt, 10.0 * wt
    if has_swing and wt > 0 and leaf_lo <= width <= leaf_hi:
        return "door", f"有门扇开启弧，扇宽 {width:.0f}mm（{leaf_lo:.0f}~{leaf_hi:.0f} 内）"
    # 窗形态：贴在墙厚方向上、沿墙向细长，**且没有门扇几何**（无铰点/无摆弧）。
    # 门也有"深度=墙厚"，靠厚度分不开，只能靠"有没有门扇"分开。
    if (not has_swing and thick > 0 and wt > 0
            and abs(thick - wt) <= 0.35 * wt and width >= 3.0 * thick):
        return "window", (f"厚度 {thick:.0f}mm≈墙厚 {wt:.0f}mm 且长宽比 "
                          f"{width / thick:.1f}≥3（通长窗特征，无门扇几何）")
    return None, ""


def layer_category(rec, plan):
    """L4 图层角色 -> ('door'|'window'|None, 依据)。"""
    layer = str((rec or {}).get("layer") or "")
    if not layer or not plan:
        return None, ""
    win_layers = {str(x) for x in (plan.get("layers", {}).get("window") or ())}
    door_layers = {str(x) for x in (plan.get("layers", {}).get("door") or ())}
    if layer in win_layers:
        return "window", f"图层 {layer} 是窗层"
    if layer in door_layers:
        return "door", f"图层 {layer} 是门层"
    return None, ""


def decide(rec, produced_as, *, plan=None, families=None, wall_thick=None):
    """给一条洞口候选裁决类别。

    返回 ``{"category", "level", "why", "override"}``；``override`` 为真表示
    结论与产生它的那条路径不一致（需要留痕）。
    """
    fam = (families or {}).get(str(rec.get("block") or "")) or {}
    checks = []
    # L1 显式标注
    for code in (rec.get("attr"), rec.get("type_code"), rec.get("label")):
        cat = code_category(code)
        if cat:
            checks.append((1, cat, f"编号 {code}"))
            break
    # L2 块族语义
    role = fam.get("role")
    if role in ("door", "window") and float(fam.get("confidence") or 0) >= 0.75:
        checks.append((2, role, f"块族 {rec.get('block')}：{fam.get('why')}"))
    # L3 互斥形态
    cat, why = form_category(rec, wall_thick)
    if cat:
        checks.append((3, cat, why))
    # L4 图层角色
    cat, why = layer_category(rec, plan)
    if cat:
        checks.append((4, cat, why))

    if not checks:
        return {"category": produced_as, "level": None, "why": "无更高优先级证据",
                "override": False}
    checks.sort(key=lambda t: t[0])
    level, category, why = checks[0]
    # 同级冲突：最强一级里出现两个不同结论 -> 不定案，交人工
    same = {c for lv, c, _ in checks if lv == level}
    return {"category": category, "level": level, "why": why,
            "override": category != produced_as,
            "conflict": len(same) > 1,
            "all": [{"level": lv, "name": LEVELS.get(lv), "category": c, "why": w}
                    for lv, c, w in checks]}


def as_window(rec):
    """把一条门记录改写成窗记录（几何照抄，只换字段口径）。"""
    axis = rec.get("axis") or rec.get("opening") or []
    out = dict(rec)
    out["axis"] = axis
    if axis:
        (x1, y1), (x2, y2) = axis
        out["center"] = [(x1 + x2) / 2.0, (y1 + y2) / 2.0]
        out["rotation"] = round(math.degrees(math.atan2(y2 - y1, x2 - x1)), 1)
        out["width"] = round(math.hypot(x2 - x1, y2 - y1), 1)
    out.setdefault("thickness", 0.0)
    return out


def as_door(rec):
    """把一条窗记录改写成门记录。"""
    axis = rec.get("axis") or []
    out = dict(rec)
    out["opening"] = axis
    if axis:
        (x1, y1), (x2, y2) = axis
        out["insert"] = [(x1 + x2) / 2.0, (y1 + y2) / 2.0]
    out.setdefault("hinges", [])
    return out


def apply(doors, windows, *, plan=None, families=None, wall_thick=None):
    """对洞口候选执行一次优先级裁决，返回 (doors, windows, report)。"""
    doors_out, windows_out, overrides, conflicts = [], [], [], []
    for rec in doors:
        verdict = decide(rec, "door", plan=plan, families=families,
                         wall_thick=wall_thick)
        if verdict["category"] == "window":
            new = as_window(rec)
            new["category_override"] = {
                "from": "door", "to": "window", "level": verdict["level"],
                "level_name": LEVELS.get(verdict["level"]), "why": verdict["why"],
                "all": verdict.get("all")}
            windows_out.append(new)
            overrides.append(new["category_override"] | {"block": rec.get("block")})
        else:
            if verdict.get("conflict"):
                rec = dict(rec)
                rec["needs_review"] = True
                conflicts.append({"block": rec.get("block"), "as": "door",
                                  "all": verdict.get("all")})
            doors_out.append(rec)
    for rec in windows:
        verdict = decide(rec, "window", plan=plan, families=families,
                         wall_thick=wall_thick)
        if verdict["category"] == "door":
            new = as_door(rec)
            new["category_override"] = {
                "from": "window", "to": "door", "level": verdict["level"],
                "level_name": LEVELS.get(verdict["level"]), "why": verdict["why"],
                "all": verdict.get("all")}
            doors_out.append(new)
            overrides.append(new["category_override"] | {"block": rec.get("block")})
        else:
            if verdict.get("conflict"):
                rec = dict(rec)
                rec["needs_review"] = True
                conflicts.append({"block": rec.get("block"), "as": "window",
                                  "all": verdict.get("all")})
            windows_out.append(rec)
    report = {"doors_in": len(doors), "windows_in": len(windows),
              "doors_out": len(doors_out), "windows_out": len(windows_out),
              "overrides": overrides, "conflicts": conflicts,
              "levels": {str(k): v for k, v in LEVELS.items()}}
    return doors_out, windows_out, report
