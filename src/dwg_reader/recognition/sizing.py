"""构件尺寸的**唯一测量入口**：一块图形"多宽、多厚"只能有一个答案。

问题
----
同一张图上，洞口宽度曾经有三条互不知情的来源：

* ``|xscale|``——只对"单位块 + 插入放大"的画法成立。天正门块把门扇画成 1 个
  图形单位，插入时 ``xscale=900`` 就是 900 宽的门，这一档完全正确；
* **块定义几何**——只对"按真实尺寸画、插入缩放 1"的画法成立。实测艺术楼
  ``M_E24TTP``：块内画着 500×650 的卫生间隔断门，``xscale`` 是 1.0，按
  ``|xscale|`` 读出来就是 **1mm** 的门；
* **块属性里写着的尺寸**——``XSIZE``/``YSIZE`` 这类参数（同一个块写着 500/650）。

只认任何一档，都会在另一类图上静默给出 1mm 或 5538mm 这种"不可能的数"。而这些
数会顺流污染下游，且不报错：门宽中位数（"这张图的门都多宽"）→ 洞口上下限 →
柱的高宽上限 → 房间封缝上限。实测艺术楼的门宽一度被标成 397mm，就是缩小画法的
块和真实尺寸画的块混在同一个中位数里造成的。

做法
----
按证据强度排一个**固定顺序**，先到先得，并把用了哪一档写回记录（可审计、可回溯）：

1. ``code``：门窗编号（``M1021`` -> 1000×2100）。设计者亲手写的，最可信；
2. ``geometry``：块内几何量出来的洞口长（铰点 -> 关闭位）。这是**量**出来的，
   与画法无关，所以排在"乘缩放系数猜"的前面；
3. ``declared``：块属性里声明的尺寸（``XSIZE``/``YSIZE``/``WIDTH``/``HEIGHT``）；
4. ``scaled``：``|插入缩放| × 块定义净尺寸``（单位块画法靠这一档，它与第 1 档
   在单位块上给出同一个数，所以两档并不冲突）；
5. ``family``：同族（同名块）其它实例的中位数——单实例读数异常时用它兜底；
6. ``insert``：``|xscale|`` 本身，老画法的既有口径，最后一档。

注意"第 2 档拿到 0 或退化值"**不等于**"第 2 档不可用就拿第 4 档"：几何退化的
洞口本来就不是洞口（实测电梯厅门 ``_LV6``：块内没有门扇，几何量出 0mm），
这种情况必须让 0 继续往下走、由结构判据把它筛掉，而不是换成缩放系数救活它。
"""
from __future__ import annotations

import math

#: 测量顺序（同时也是"证据强度"顺序）。只改这里就等于改全部构件的口径。
TIERS = ("code", "geometry", "declared", "scaled", "family", "insert")

#: 块属性里可能写着真实尺寸的键名（大小写不敏感）。保守列——写错键名会把
#: "编号/名称"这类文字属性当成尺寸，宁可少认不可错认。
_DECLARED_W = ("XSIZE", "WIDTH", "LENGTH", "LEN", "W")
_DECLARED_H = ("YSIZE", "HEIGHT", "THICKNESS", "THICK", "H")


def _num(value):
    """属性值 -> 正浮点；认不出返回 None（属性里常混着文字和空白）。"""
    if value is None:
        return None
    try:
        out = abs(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    return out if out > 1e-9 else None


def declared(entity, axis="w"):
    """块属性里**直接声明**的尺寸（mm）。

    ``XSIZE``/``YSIZE`` 是天正一类图库把"这个族有多少种规格"暴露出来的方式，
    实例插进去以后它就等于真实尺寸，比任何几何推算都直接。
    """
    attrs = (entity or {}).get("attribs") or (entity or {}).get("attributes") or {}
    if not isinstance(attrs, dict):
        return None
    keys = _DECLARED_W if axis == "w" else _DECLARED_H
    for want in keys:
        for key, value in attrs.items():
            if str(key).strip().upper() == want:
                got = _num(value)
                if got:
                    return got
    return None


def net_extent(name, blocks, axis="w"):
    """块定义在该方向上的净尺寸（未乘插入缩放）。认不出返回 None。"""
    ext = ((blocks or {}).get(name) or {}).get("extent")
    if not ext or len(ext) < 2:
        return None
    try:
        value = abs(float(ext[0] if axis == "w" else ext[1]))
    except (TypeError, ValueError, IndexError):
        return None
    return value if value > 1e-6 else None


def scaled(entity, blocks, axis="w"):
    """``|插入缩放| × 块定义净尺寸``：单位块与真实尺寸块通吃。"""
    key = "xscale" if axis == "w" else "yscale"
    try:
        s = abs(float((entity or {}).get(key) or 0.0))
    except (TypeError, ValueError):
        s = 0.0
    ext = net_extent((entity or {}).get("name"), blocks, axis)
    if s > 1e-9 and ext:
        return s * ext
    return s if s > 1e-9 else None


def span_length(opening):
    """洞口两点 -> 长度（mm）。两点重合返回 0.0（不是 None：0 是有效观测）。"""
    if not opening or len(opening) != 2:
        return None
    try:
        (x1, y1), (x2, y2) = opening
        return math.hypot(float(x2) - float(x1), float(y2) - float(y1))
    except (TypeError, ValueError):
        return None


def measure(entity, blocks, *, opening=None, kind="door", th=None, family=None,
            code_size=None):
    """构件尺寸测量主入口。

    参数
    ----
    entity
        INSERT 记录（``name``/``xscale``/``yscale``/``attribs``）。
    blocks
        快照的块定义表（``snapshot["blocks"]``）。
    opening
        **块内几何算出来的洞口段**（两个世界坐标点）。门由
        :func:`elements._door_geom` 给出；窗没有这一档，传 None 即可。
    kind
        ``"door"`` / ``"window"``，只用来在返回值里标注口径。
    family
        ``(宽, 高)``：同名块其它实例的中位数，单实例读数异常时的兜底。
    code_size
        ``(宽, 高)``：从门窗编号解出来的尺寸（``M1021`` -> 1000×2100）。

    返回
    ----
    ``{"width", "height", "source", "height_source", "candidates"}``。
    ``candidates`` 保留每一档的读数，方便事后追"这个 1mm 是哪来的"。
    """
    cand = {}
    if code_size and code_size[0]:
        cand["code"] = float(code_size[0])
    geom = span_length(opening)
    if geom is not None:
        cand["geometry"] = float(geom)
    dec = declared(entity, "w")
    if dec:
        cand["declared"] = dec
    sc = scaled(entity, blocks, "w")
    if sc:
        cand["scaled"] = sc
    if family and family[0]:
        cand["family"] = float(family[0])
    try:
        ins = abs(float((entity or {}).get("xscale") or 0.0))
    except (TypeError, ValueError):
        ins = 0.0
    if ins > 1e-9:
        cand["insert"] = ins

    width, source = None, None
    for tier in TIERS:
        if tier in cand and (cand[tier] > 1e-9 or tier == "geometry"):
            width, source = cand[tier], tier
            break
    if width is None:                      # 一无所获：0 比 None 更安全（下游在做算术）
        width, source = 0.0, "none"

    # 厚度方向（窗的墙厚口径）只走 declared/scaled/insert——窗块没有开启弧，
    # 没有"几何量出来的厚度"这一档。
    hcand = {}
    dec_h = declared(entity, "h")
    if dec_h:
        hcand["declared"] = dec_h
    sc_h = scaled(entity, blocks, "h")
    if sc_h:
        hcand["scaled"] = sc_h
    if family and len(family) > 1 and family[1]:
        hcand["family"] = float(family[1])
    try:
        ins_h = abs(float((entity or {}).get("yscale") or 0.0))
    except (TypeError, ValueError):
        ins_h = 0.0
    if ins_h > 1e-9:
        hcand["insert"] = ins_h
    height, height_source = None, None
    for tier in ("declared", "scaled", "family", "insert"):
        if tier in hcand:
            height, height_source = hcand[tier], tier
            break

    return {"width": round(float(width), 1), "source": source,
            "height": round(float(height), 1) if height else None,
            "height_source": height_source,
            "kind": kind,
            "candidates": {k: round(v, 1) for k, v in cand.items()},
            "candidates_h": {k: round(v, 1) for k, v in hcand.items()}}


def family_size(snapshot, name, axis="w"):
    """同名块实例的 ``|缩放| × 块净尺寸`` 中位数——"这个族在这张图上多大"。

    单实例读数异常（旋转 45°、镜像、局部缩放）时用它兜底，而不是让那一个实例
    的读数污染整族口径。
    """
    blocks = (snapshot or {}).get("blocks") or {}
    vals = []
    for e in (snapshot or {}).get("entities") or ():
        if e.get("type") != "INSERT" or e.get("name") != name:
            continue
        got = scaled(e, blocks, axis)
        if got:
            vals.append(float(got))
    if not vals:
        return None
    vals.sort()
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0


def summary(items) -> dict:
    """一批记录的测量口径统计：每一档各兜住了多少构件（进 meta 用）。"""
    counts = {}
    for it in items or ():
        src = str((it or {}).get("width_source") or "none")
        counts[src] = counts.get(src, 0) + 1
    return {"tiers": counts, "order": list(TIERS)}
