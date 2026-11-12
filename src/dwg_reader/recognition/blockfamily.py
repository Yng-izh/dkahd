"""块族：**一次定角色，全族实例继承**。

为什么要有这一层
----------------
块参照是这张图上唯一"自带身份"的东西：同一块名的所有实例，本来就是同一个构件
（只是被旋转、缩放、放在不同层上）。而识别器以前是**逐实例判定**的——艺术楼
100 根柱来自 2 个块族，却等于把同一件事判了 100 遍：既慢，又必然出现"这个认出来
了、隔壁那个没认出来"的不一致（实测 100 根柱只认出 7 根，就是族里 92 个
``M_S0BKA`` 一个都没进候选）。

所以这里先聚族，再给每族一个角色，最后让全族实例继承。实测规模：

    艺术楼-图层混乱  22 个块族 / 293 个实例（平均每族 13 个）
    会所-图层混乱    21 个块族 / 169 个实例
    办公楼           29 个块族 / 251 个实例

判角色的三种证据（成本从低到高）
------------------------------
1. **命名先验**：天正/探索者一类软件的块名自带语义（``$DorLib2D$``=门库、
   ``$WinLib2D$``=窗库、``_FZH``=柱、``_AXISO``=轴号、``_DIMX$``=尺寸）。
2. **几何指纹**：块里只有一块实心四边形 + 尺寸像柱 -> 柱；有弧又有编号属性 ->
   门（门弧 + 门编号）；细长 + 编号属性 -> 窗。这条与块名无关，换图库照样成立。
3. **未知**：都判不了就留 ``unknown``，交给原来的逐实例识别去处理——**本模块
   只做加法，不删任何已有候选**。

三个必须守住的纪律（都是实测踩出来的）
------------------------------------
* **同族多尺寸是常态，不是异常**：``_WBLK`` 有 600/800/1500 三种窗宽、``_FZH``
  有 450/650 两种柱；族键只能是块名，尺寸作为属性记录。
* **同族可能画在多个图层上**（艺术楼 4 个族、会所 5 个族）：角色由块本身决定，
  图层只当旁证。
* **匿名块名（``A$C68D277C8``）是每次保存重新生成的哈希**，只在单张图里稳定。
  跨图迁移必须靠 :func:`fingerprint` 给出的几何指纹，不能靠名字。
"""
from __future__ import annotations

import re

#: 命名先验：(正则, 角色, 说明)。只收"厂商/图集固定用法"，不猜语义。
NAME_PRIORS = (
    (r"\$dorlib2d\$", "door", "天正门库块"),
    (r"^_?d(?:oor)?blk\d*$", "door", "门块命名(_DBLK)"),
    (r"门", "door", "块名含「门」"),
    (r"\$winlib2d\$", "window", "天正窗库块"),
    (r"\$tchsys\$win", "window", "TCHSYS 窗块"),
    (r"^_?w(?:in(?:dow)?)?blk\d*$", "window", "窗块命名(_WBLK)"),
    (r"^_?gcblk\d*$", "window", "窗块命名(_GCBLK)"),
    (r"窗", "window", "块名含「窗」"),
    (r"^_?fzh\d*$", "column", "柱块命名(_FZH)"),
    (r"^_?axiso?\d*$", "axis", "轴号块"),
    (r"轴", "axis", "块名含「轴」"),
    (r"^_?dimx?\d*$", "dimension", "尺寸块"),
    (r"^_?arrow\d*$", "annotation", "箭头/符号"),
)

#: 角色 -> 中文名（诊断输出用）
#: 角色确认门槛：低于它只算"假设"，不参与识别
CONFIRM = 0.75

ROLE_CN = {"door": "门", "window": "窗", "column": "柱", "axis": "轴号",
           "dimension": "尺寸", "annotation": "符号", "unknown": "未判"}


def _section(insert, extent):
    """块参照的真实截面（宽,高）：块尺寸 × 插入缩放，不受旋转影响。"""
    sx = abs(float(insert.get("xscale") or 0.0))
    sy = abs(float(insert.get("yscale") or 0.0))
    if extent and len(extent) == 2:
        if extent[0] and abs(extent[0]) > 1e-6:
            sx *= abs(extent[0])
        if extent[1] and abs(extent[1]) > 1e-6:
            sy *= abs(extent[1])
    return sx, sy


def families(snapshot) -> dict:
    """快照 -> {块名: 族记录}。族记录含实例、图层、尺寸档、块内几何摘要。"""
    blocks = snapshot.get("blocks") or {}
    groups: dict[str, list] = {}
    for e in snapshot.get("entities") or ():
        if e.get("type") == "INSERT":
            groups.setdefault(str(e.get("name")), []).append(e)
    out = {}
    for name, insts in groups.items():
        d = blocks.get(name) or {}
        extent = d.get("extent")
        sizes = []
        for e in insts:
            w, h = _section(e, extent)
            if w > 0 and h > 0:
                sizes.append((round(w, 1), round(h, 1)))
        sizes.sort(key=lambda s: (min(s), max(s)))
        xs = sorted(abs(float(e.get("xscale") or 0.0)) for e in insts) or [0.0]
        out[name] = {
            "name": name,
            "xscale_med": xs[len(xs) // 2],
            "n": len(insts),
            "instances": insts,
            "layers": sorted({str(e.get("layer")) for e in insts}),
            "sizes": sizes,
            "n_sizes": len(set(sizes)),
            "def": d,
            "arcs": len(d.get("arcs") or ()),
            "attdefs": int(d.get("attdefs") or 0),
            "solids": len(d.get("solids") or ()),
            "loops": len(d.get("loops") or ()),
            "extent": extent,
        }
    return out


def _quad_solids(defn) -> int:
    n = 0
    for poly in defn.get("solids") or ():
        uniq = {(round(float(p[0]), 3), round(float(p[1]), 3)) for p in poly}
        if len(uniq) >= 4:
            n += 1
    return n


def _name_role(name):
    """块名 -> 提名角色（只是假设，必须由几何确认）。"""
    for pat, role, why in NAME_PRIORS:
        if re.search(pat, name, re.I):
            return role, why
    return None, None


def _strokes(lines):
    """块内线段 -> [(长度, 单位方向, 法向偏移)]（块局部坐标）。"""
    import math
    segs = []
    for raw in lines or ():
        if len(raw) < 4:
            continue
        dx, dy = float(raw[2]) - float(raw[0]), float(raw[3]) - float(raw[1])
        ln = math.hypot(dx, dy)
        if ln <= 1e-5:
            continue
        segs.append((ln, dx / ln, dy / ln, raw))
    return segs


def _window_strokes(lines) -> bool:
    """窗符号：**≥2 条同向、彼此分开的平行长线**，且块内线段总数 ≥4。

    两类常见画法都覆盖：四条等长平行线（``_WBLK``）、两条长外框 + 两条短内线
    （``_GCBLK``，以前只有前一种判据，这类窗整族漏掉）。
    「几条一模一样的线叠在一处」不算（图框/图例）——法向偏移必须互不相同。
    """
    segs = _strokes(lines)
    if len(segs) < 4:
        return False
    segs.sort(key=lambda t: -t[0])
    ref_l, ux, uy, _raw = segs[0]
    offs = set()
    long_ones = 0
    for ln, vx, vy, raw in segs:
        if ln < 0.75 * ref_l:
            continue
        if abs(ux * vy - uy * vx) > 0.04:
            continue
        long_ones += 1
        offs.add(round(-uy * float(raw[0]) + ux * float(raw[1]), 3))
    return long_ones >= 2 and len(offs) >= 2


def role_of(fam, scale=None) -> dict:
    """单族 -> {role, why, confidence, name_prior}。

    纪律：**块名只是提名，几何必须确认**。``_DBLK2`` 这种"名字像门、块里没有门弧"
    的块以前就被名字带偏过；反过来 ``$WinLib2D$`` 也要验过笔画才认。
    """
    from . import scale as SC
    th = SC.thresholds(scale) if scale else {}
    short_min = float(th.get("col_short_min") or SC.DEFAULT_THICKNESS * 0.75)
    short_max = float(th.get("col_short_max") or SC.DEFAULT_DOOR_W * 2.0)
    door = float(th.get("door_w") or SC.DEFAULT_DOOR_W)
    sizes = fam["sizes"]
    name_role, name_why = _name_role(fam["name"])
    result = {"role": "unknown", "why": "无可用证据", "confidence": 0.0,
              "name_prior": name_role}

    # ---- 几何指纹 1：实心四边形 + 尺寸像柱 = 柱
    if _quad_solids(fam["def"]) and not fam["arcs"] and not fam["attdefs"]:
        good = [s for s in sizes
                if short_min <= min(s) <= short_max and max(s) <= 3.0 * door
                and max(s) / max(min(s), 1e-6) <= 3.0]
        if len(good) >= 2 and len(good) >= 0.5 * max(1, len(sizes)):
            mid = sorted(min(s) for s in good)[len(good) // 2]
            return {"role": "column",
                    "why": f"几何指纹:实心四边形柱脚≈{mid:.0f}mm（{len(good)}/{len(sizes)} 个像柱）",
                    "confidence": 0.85, "name_prior": name_role}

    # ---- 几何指纹 2：门弧 = 门
    xsc = float(fam.get("xscale_med") or 1.0) or 1.0
    leaf = [2.0 * float(a[2]) * xsc for a in (fam["def"].get("arcs") or ())]
    # 一樘门最多两扇（1~2 条门弧）、线条不多；电梯/门窗大样是 6~7 条弧、7~8 条线，
    # 以前一律被当成"有弧的门"（实测电梯族 _LV6 的 31 个实例全部误判为门）。
    door_like = 1 <= int(fam["arcs"]) <= 2 and int(len(fam["def"].get("lines") or ())) <= 6
    # 扇宽的合理带用两把尺子取宽：门宽标尺（可能被"小缺口"带偏，实测艺术楼标成
    # 397mm）与墙厚（门扇不可能比墙薄）。只按 3×门宽 卡，会把 1500mm 的双扇
    # 防火门（M_A0GGR）判成"太大，不是门"，实测漏掉 2 樘。
    wt = float((SC.thresholds(scale) if scale else {}).get("thickness")
               or SC.DEFAULT_THICKNESS)
    leaf_lo = max(0.25 * door, 0.5 * wt)
    leaf_hi = max(3.0 * door, 10.0 * wt)
    if door_like and leaf and any(leaf_lo <= r <= leaf_hi for r in leaf):
        return {"role": "door", "why": "geometric:door_block_arc",
                "confidence": 0.8, "name_prior": name_role}

    # ---- 几何指纹 3：多笔画窗 = 窗（笔画 + 细长）
    if not fam["arcs"] and sizes:
        long_side = max(max(s) for s in sizes)
        short_side = min(min(s) for s in sizes)
        # 长度的上限不能只看门宽标尺：标尺可能偏小（实测某图标成 397mm，
        # 12 倍=4764，把 5538mm 的通长窗判成"不是窗"）。再给一个墙厚尺子兜底。
        long_cap = max(12.0 * door, 40.0 * float(th.get("thickness") or 0.0))
        elongated = short_side > 0 and long_side / short_side >= 3.0 \
            and 300.0 <= long_side <= long_cap
        if _window_strokes(fam["def"].get("lines")) and elongated:
            return {"role": "window",
                    "why": "geometric:window_block_parallel_strokes",
                    "confidence": 0.8, "name_prior": name_role}

    if name_role:
        # 名字像、几何不像 -> 只记录假设，不参与识别（confidence 低于门槛）
        result.update({"role": name_role,
                       "why": f"命名先验(未经几何确认):{name_why}",
                       "confidence": 0.35})
    return result


def swing_arcs(fam) -> list:
    """块内**摆动弧**：60~120°、且半径像一扇门的那些弧。

    为什么必须这么严：洁具、家具、圆形符号都是用"弧"画轮廓的。实测艺术楼的
    马桶块 ``M_E24TTP``（一个椭圆盆 + 圆角矩形底座，7 条弧）被当成门，就是
    因为"有弧"被当成了"有门扇"。门的弧和轮廓弧的区别是硬的：
    门的弧只转一个直角多一点（90°±30°），而且半径就是门扇长度。
    """
    out = []
    for a in (fam.get("def") or {}).get("arcs") or ():
        try:
            cx, cy, r, a1, a2 = (float(a[0]), float(a[1]), float(a[2]),
                                 float(a[3]), float(a[4]))
        except (IndexError, TypeError, ValueError):
            continue
        sweep = (a2 - a1) % 360.0
        if 60.0 <= sweep <= 120.0:
            out.append({"r": r, "sweep": sweep})
    return out


def signature_ok(fam, kind, scale=None) -> tuple:
    """块几何是否**独立**支持这个角色（不看块名、不看插入尺寸档）。

    这是"长相判定"进构件池前的最后一道门：``blockmodel`` 只按宽高比和弧线数量
    分类，够用来看图库，但会把洁具和电梯厅门一起收成"门"。三条纪律：

    * 门：1~2 条**摆动弧**（60~120°，单扇/双扇），弧半径落在"像一扇门"的宽度带；
    * 门：**没有 >180° 的弧**——那是画轮廓用的（盆沿、圆形家具），一樘门的弧
      永远不会转过大半圈；
    * 窗：无弧 + 至少两道长而分离的平行笔画（窗框）。
    """
    from . import scale as SC
    th = SC.thresholds(scale) if scale else {}
    door_w = float(th.get("door_w") or SC.DEFAULT_DOOR_W)
    thick = float(th.get("thickness") or SC.DEFAULT_THICKNESS)
    def_ = fam.get("def") or {}
    arcs = def_.get("arcs") or ()
    sweeps = []
    for a in arcs:
        try:
            sweeps.append((float(a[4]) - float(a[3])) % 360.0)
        except (IndexError, TypeError, ValueError):
            continue
    if kind == "door":
        if not arcs:
            return False, "无弧：门扇必然带一条摆动弧"
        if any(sw > 180.0 for sw in sweeps):
            return False, "弧超过 180°：那是画轮廓（洁具/家具），不是门扇"
        swings = swing_arcs(fam)
        if not (1 <= len(swings) <= 2):
            return False, f"摆动弧 {len(swings)} 条：门的弧是 1~2 条（单扇/双扇）"
        xsc = abs(float(fam.get("xscale_med") or 1.0)) or 1.0
        leaf_lo = max(0.25 * door_w, 0.5 * thick)
        leaf_hi = max(3.0 * door_w, 10.0 * thick)
        if not any(leaf_lo <= s["r"] * xsc <= leaf_hi for s in swings):
            return False, "摆动弧半径不像门扇（不在门宽/墙厚的合理带里）"
        return True, "摆动弧 1~2 条且半径像门扇"
    if kind == "window":
        if arcs:
            return False, "有弧：窗块不带开启弧"
        if not _window_strokes(def_.get("lines")):
            return False, "缺少两道长而分离的平行笔画：不像窗框"
        return True, "平行笔画成窗框"
    return True, ""


def series_of(name: str) -> str:
    """块名 -> 图库系列键。

    同一角色在图上往往有好几个块名（会所的「门」有 5 个族：`$DorLib2D$00000001`
    / `00000013` / `00000002` / `$TCHSYS$DOOR2D` / `$TCHSYS$APDOOR2D`），
    它们同属一个图库系列，只是编号不同。系列全下就是"同一类构件的不同规格"。
    """
    n = str(name or "")
    m = re.match(r"^(\$[^$]+\$)", n)
    if m:
        base = m.group(1)
        rest = n[len(base):]
        # TCHSYS 一类把类别写在名字里（DOOR2D / WIN2D / APDOOR2D），要保留
        word = re.match(r"([A-Za-z]+)", rest)
        return (base + (word.group(1).lower() if word else "")).lower()
    return re.sub(r"\d+$", "", n).lower()


def _kinds(rec) -> set:
    """块内几何种类（弧/线/实心/编号属性），用于判断"同系列是否同画法"。"""
    k = set()
    if rec.get("lines") or rec.get("n_lines"):
        k.add("line")
    if rec.get("arcs"):
        k.add("arc")
    if rec.get("solids"):
        k.add("solid")
    if rec.get("loops"):
        k.add("loop")
    if rec.get("attdefs"):
        k.add("attrib")
    return k


def _shape_ok(rec, role) -> bool:
    """继承角色前，候选族必须**自己过一遍该角色的形状关**（尺寸/编号可以缺）。"""
    sh = rec.get("shape") or {}
    if role == "door":
        return int(sh.get("arcs") or 0) >= 1
    if role == "window":
        return bool(sh.get("window_strokes"))
    if role == "column":
        return int(sh.get("quad_solids") or 0) >= 1
    return False


def _propagate(rows) -> dict:
    """族间互证：同系列 或 同几何指纹的族，继承已确认族的角色。

    只对**尚未确认**的族生效（已确认的族永不被改写），且要求画法相容
    （都有弧，或都没有弧）——否则"门库里的门联窗"会被硬塞成门。
    """
    confirmed = {n: r for n, r in rows.items()
                 if float(r.get("confidence") or 0) >= CONFIRM
                 and r.get("role") not in (None, "unknown")}
    if not confirmed:
        return rows
    by_series = {}
    by_fp = {}
    for n, r in confirmed.items():
        by_series.setdefault(series_of(n), []).append(r)
        by_fp.setdefault(r.get("fingerprint"), []).append(r)
    for name, rec in rows.items():
        if float(rec.get("confidence") or 0) >= CONFIRM:
            continue
        mine = _kinds(rec)
        src = None
        for cand in by_series.get(series_of(name), ()):
            if mine and _kinds(cand) == mine and _shape_ok(rec, cand["role"]):
                src = cand
                break
        how = "同系列互证"
        if src is None:
            for cand in by_fp.get(rec.get("fingerprint"), ()):
                if _kinds(cand) == mine and _shape_ok(rec, cand["role"]):
                    src = cand
                    break
            how = "同指纹互证"
        if src is None:
            continue
        # 命名先验与继承角色一致时提高置信度
        agree = rec.get("name_prior") == src["role"]
        rec.update({"role": src["role"], "role_cn": ROLE_CN.get(src["role"], "?"),
                    "why": f"{how}:{src['name']}（{src['role_cn']}）",
                    "confidence": 0.85 if agree else 0.78})
    return rows


def by_role(rows) -> dict:
    """角色 -> {块名: 记录}。一个角色下可以有任意多个块族（这是常态，不是例外）。"""
    out = {}
    for name, rec in (rows or {}).items():
        out.setdefault(rec.get("role") or "unknown", {})[name] = rec
    return out


def table(snapshot, scale=None) -> dict:
    """快照 -> {块名: 族记录 + role/why/confidence}（JSON 原生，可直接进报告）。"""
    out = {}
    for name, fam in families(snapshot).items():
        role = role_of(fam, scale=scale)
        out[name] = {
            "name": name,
            "n": int(fam["n"]),
            "role": role["role"],
            "role_cn": ROLE_CN.get(role["role"], "?"),
            "why": role["why"],
            "name_prior": role.get("name_prior"),
            "confidence": round(float(role["confidence"]), 2),
            "layers": list(fam["layers"]),
            "sizes": [[float(s[0]), float(s[1])] for s in fam["sizes"][:20]],
            "n_sizes": int(fam["n_sizes"]),
            "arcs": int(fam["arcs"]),
            "attdefs": int(fam["attdefs"]),
            "solids": int(fam["solids"]),
            "loops": int(fam["loops"]),
            "n_lines": int(len((fam.get("def") or {}).get("lines") or ())),
            "shape": {"arcs": int(fam["arcs"]),
                      "quad_solids": _quad_solids(fam["def"]),
                      "window_strokes": _window_strokes(
                          (fam.get("def") or {}).get("lines")),
                      "n_lines": int(len((fam.get("def") or {}).get("lines") or ()))},
            "extent": ([float(fam["extent"][0]), float(fam["extent"][1])]
                       if fam.get("extent") else None),
            "fingerprint": fingerprint(fam),
        }
    return _propagate(out)


def names_for(snapshot, role, scale=None, plan=None) -> dict:
    """某一角色的块名 -> 依据。给识别器当"族名单"用。

    ``plan`` 给了声明图层时，**只靠几何指纹**认定的族必须在声明图层上出现过：
    否则办公楼层 0 上那些杂块会被长相判定收成门窗（实测门 30→34、窗 32→34）。
    命名先验不受此限——块名本身就是强证据。
    """
    declared = set()
    if plan:
        declared = set(plan.get("layers", {}).get(role) or ())
    out = {}
    for name, rec in table(snapshot, scale=scale).items():
        if rec["role"] != role:
            continue
        if float(rec.get("confidence") or 0.0) < CONFIRM:
            continue          # 仅命名提名、几何未确认 -> 不进名单
        if declared and not (set(rec["layers"]) & declared):
            continue
        out[name] = rec["why"]
    return out


def fingerprint(fam) -> str:
    """跨图可迁移的几何指纹：不看名字，只看"块里是什么形状"。

    用于把"实心四边形 -> 柱"这类结论搬到陌生图纸上（匿名块名 A$C... 换张图就变）。
    """
    defn = fam["def"] or {}
    sizes = fam["sizes"] or [(0.0, 0.0)]
    long_side = max(max(s) for s in sizes)
    short_side = max(min(s) for s in sizes) or 1.0
    aspect = round(long_side / short_side, 1)
    kinds = []
    if _quad_solids(defn):
        kinds.append("solidquad")
    elif defn.get("solids"):
        kinds.append("solid")
    if defn.get("arcs"):
        kinds.append("arc")
    if defn.get("loops"):
        kinds.append("loop")
    if defn.get("lines"):
        kinds.append("line")
    if fam.get("attdefs"):
        kinds.append("attrib")
    return "|".join(kinds) + f"|aspect{aspect}"


def summary(rows) -> list:
    """族表 -> 可打印的行（诊断输出）。"""
    out = []
    for name, rec in sorted((rows or {}).items(), key=lambda kv: -kv[1].get("n", 0)):
        out.append({"block": name, "n": rec.get("n"), "role": rec.get("role"),
                    "role_cn": ROLE_CN.get(rec.get("role") or "", "?"),
                    "why": rec.get("why"),
                    "sizes": rec.get("sizes", [])[:3], "n_sizes": rec.get("n_sizes"),
                    "layers": rec.get("layers", [])[:4]})
    return out
