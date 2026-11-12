"""墙网分隔检验（stage A 生成门）：一条线只有把空间分成两半，才有资格成为墙。

为什么必须在生成侧做
--------------------
墙的定义属性不是"两根平行线、间距合适"，而是"把一块空间和另一块空间隔开"。
只按前者配对，图纸上的标注链、轴网线、家具轮廓、引线都能凑出"墙"。单图层图纸
实测：687 段候选里大半是这类线，而且它们在厚度、端头、双线、封口这些**事后**
判据上和真墙完全同构（墙厚 180 与家具间距 180 同量级），事后调规则怎么调都分
不开——所以问题不在验证，在生成。

本模块把全部候选画进一张粗栅格（同一墙线上的缺口临时补合：门窗洞本来就是墙上
的缺口），求出连通空间，再对每条候选在两侧各打三个点：

    两侧是**同一片**空间 -> 它不是任何空间的边界（悬空线）：标注/家具/引线
    两侧是**两片**空间   -> 它确实分隔空间：墙

判定为悬空线的候选不进入阶段 B（联合验证），直接记为候选并写清理由。

判别口径只有一条、且与图纸无关
------------------------------
"两侧各是不是一块够大的空间"——够大的下界由 :mod:`scale` 的字高基准推出
（最小房间 ≈ 4 个字高的方格），不写死毫米数。

安全阀（宁可退回旧行为，不许整体变差）
--------------------------------------
1. 图里没有形成起码的空间划分（够大的空间少于 3 块）：检验不生效；
2. 通过率低于 ``KEEP_RATIO``：说明这张图的墙网本来就不闭合，检验结论不可信，
   整个检验不生效（全部保留，仍走原来的两阶段）。
两条都会写进 ``diag``，可追溯，不是静默降级。
"""
from __future__ import annotations

import os

from . import rooms as RM
from . import scale as SC

#: 栅格单元数上限：只防极端图纸把内存吃穿，不参与任何几何判定（计数，非长度）
MAX_CELLS = 4_000_000
#: 探测点落在挡格上时，向外找空格的搜索半径（格）；栅格离散化的必要补偿
PROBE_REACH = 3
#: 通过率下限：低于它认定"本图墙网不闭合"，检验整体不生效
KEEP_RATIO = float(os.environ.get("DWG_NET_KEEP_RATIO", "0.5"))
#: "空间"的下界 = 最小房间面积（4 个字高见方）× 该比例。储物井/管井也小于它。
SPACE_RATIO = float(os.environ.get("DWG_NET_SPACE_RATIO", "0.4"))
#: 开关与档位：
#:   off      ——（默认）完全不跑，识别路径与之前逐字节一致；
#:   evidence ——只给候选打"是否分隔空间"的证据，一条都不删；
#:   strict   ——把不分隔空间的候选剔除（**实测会误杀真墙，见下方"实测结论"**）。
MODE = os.environ.get("DWG_WALL_NET", "off").strip().lower()
#: 三条探测线里至少几条"两侧是不同空间"才算墙：中间一条落进洞口时不至于误杀
MIN_SPLIT = 2
#: 栅格步长 = 墙厚 / 该值。分得太粗，薄墙压不住空缝；分得太细，栅格单元数爆炸。
#: 无量纲（墙厚的几分之一），与图纸比例无关。
_RES_SLICE = 3.0


#: 实测结论（2026-11，四张真实图纸 + 代理真值）
#: ------------------------------------------------------------------
#: 代理真值：``艺术楼-全在一个图层.dxf``（5444 个图元全在 0 层）与
#: ``艺术楼-图层混乱.dxf`` 是同一栋楼，图层版 WALL/PUB_WALL 上的 319 条墙线与
#: 单图层版**逐条坐标精确重合**（319/319 命中），因此可以当"这栋楼的墙在哪"。
#:
#: 结果：
#:   * 判据本身是准的：图层清洁的图上，"判定为分隔空间"的候选 98~100% 是真墙；
#:   * 但召回只有 45~50%：一半真墙两侧不构成两块**闭合**空间（图纸画成片段、
#:     大开间、围合不严），于是被判"不分隔"；
#:   * 单图层那张更是判不开：池 757 条候选里判为分隔的 234 条，只有 59 条真墙
#:     （25%），而被判"不分隔"的 277+198 条里仍有 79 条真墙。
#:
#: 结论：**不能当硬门**。默认 off；strict 档只作为"图纸明显干净时"的可选项，
#: 或在证据侧使用（``evidence`` 档），不许默认开启。
def mode() -> str:
    if MODE in ("0", "false", "off", "", "no"):
        return "off"
    if MODE in ("strict", "cut", "filter"):
        return "strict"
    return "evidence"


def enabled() -> bool:
    return mode() != "off"


# --------------------------------------------------------------------------- 栅格
#: 补合缺口的倍数（环境变量可覆盖）：以"门宽"为单位。
#: 只补**同一条墙线上**两块已存在墙段之间的缺口，不向两端延伸，
#: 所以放大这个倍数不会在空地上凭空造墙。
SEAL_DOOR = float(os.environ.get("DWG_NET_SEAL_DOOR", "3.0"))


def _seal_gap_for(thickness, seal_gap=None, door_w=None) -> float:
    """补合缺口的尺度：门窗洞本身就是墙上的缺口，房间因此必须补起来才成形。"""
    if seal_gap:
        return float(seal_gap)
    unit = float(door_w) if door_w else 6.0 * float(thickness)
    return max(SEAL_DOOR * unit, 6.0 * float(thickness))


def _bbox_of(walls, columns=()):
    xs, ys = [], []
    for w in walls:
        xs += [float(w["start"][0]), float(w["end"][0])]
        ys += [float(w["start"][1]), float(w["end"][1])]
    for c in columns or ():
        outline = c.get("outline") if isinstance(c, dict) else c
        for p in outline or ():
            xs.append(float(p[0]))
            ys.append(float(p[1]))
    if not xs:
        return None
    pad = 0.0
    return [min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad]


def _fill(g, walls, thickness, columns=()):
    """把候选画成挡格：轴向墙用真实墙带，斜墙沿墙轴画带宽（不撑大外接框）。

    挡格比真实墙厚多一个栅格步长：栅格化必然把薄墙压成 1~2 格，不补这一格的话
    相邻墙段之间会出现"格与格之间"的假缝，连通域直接从缝里漏出去，房间永远不闭合。
    这只影响栅格，不改任何真实几何。
    """
    from . import spaces as SP
    for w in walls:
        if not (w.get("start") and w.get("end")):
            continue
        t = float(w.get("thickness") or thickness) + 2.0 * g.res
        if w.get("arc") and w.get("outline"):
            # 弧墙：直接铺折线带。按 start→end 画弦会把弧内侧的空间切掉一角。
            g.fill_poly(w["outline"])
        elif w.get("orient") in ("H", "V") and w.get("center") is not None:
            g.fill_rect(SP._wall_outline(w, t))
        else:
            g.fill_seg(w["start"], w["end"], half=max(t / 2.0, g.res))
    for c in columns or ():
        outline = c.get("outline") if isinstance(c, dict) else c
        if outline:
            g.fill_poly(outline)


def _build_grid(walls, thickness, columns=(), res=None, seal_gap=None,
                door_w=None):
    """候选 -> (栅格, 连通域, 已补合缺口数)。栅格步长与图纸尺度挂钩。"""
    from . import spaces as SP
    bbox = _bbox_of(walls, columns)
    if bbox is None:
        return None, None, 0
    step = float(res or max(thickness / _RES_SLICE, 1e-6))
    while True:
        nx = (bbox[2] - bbox[0]) / step + 4
        ny = (bbox[3] - bbox[1]) / step + 4
        if nx * ny <= MAX_CELLS or step > 1e6:
            break
        step *= 1.5
    g = RM.Grid(bbox, res=step)
    _fill(g, walls, thickness, columns)
    sealed = SP._seal(g, walls,
                      max_gap=_seal_gap_for(thickness, seal_gap, door_w),
                      virtual=None)
    if CLOSE_CELLS:
        _close_mask(g, CLOSE_CELLS)
    return g, RM._Components(g), sealed


#: 形态学闭运算半径（格）。**默认 0：不用**。
#: 实测它会把"家具线离墙 200~300mm"这种真缺口一并补上，于是室内的悬空线被接成
#: 墙、房间被切两半——判据的误检就是这么来的。栅格化的假缝改由"挡格多画一格"
#: （见 :func:`_fill`）解决，那一条不会跨接真实缺口。
CLOSE_CELLS = 0


def _rows_to_bits(mask, nx, ny):
    """bytearray 掩码 -> 每行一个整数（bit 8*i = 第 i 格）。"""
    return [int.from_bytes(bytes(mask[j * nx:(j + 1) * nx]), "little")
            for j in range(ny)]


def _bits_to_mask(rows, nx, ny, mask):
    for j, v in enumerate(rows):
        mask[j * nx:(j + 1) * nx] = v.to_bytes(nx, "little")[:nx]


def _dilate_bits(rows, radius):
    out = []
    for v in rows:
        w = v
        for d in range(1, radius + 1):
            w |= (v << (8 * d)) | (v >> (8 * d))
        out.append(w)
    dil = list(out)
    n = len(out)
    for j in range(n):
        w = out[j]
        for d in range(1, radius + 1):
            if j - d >= 0:
                w |= out[j - d]
            if j + d < n:
                w |= out[j + d]
        dil[j] = w
    return dil


def _close_mask(g, radius=CLOSE_CELLS):
    """闭运算：先膨胀再腐蚀。目的只有一个——把"相邻挡格之间差 1~2 格"的假缝补上。

    不做这一步，10m 长的外墙只要有一处 66mm 的格缝，整层房间就会从这道缝漏成一片，
    分隔检验直接失效（实测办公楼 21 个房间会退化成 3 个）。
    """
    if radius <= 0:
        return 0
    nx, ny = g.nx, g.ny
    rows = _rows_to_bits(g.mask, nx, ny)
    full = (1 << (8 * nx)) - 1
    grown = [v & full for v in _dilate_bits(rows, radius)]
    inv = [full ^ v for v in grown]
    closed = [full ^ (v & full) for v in _dilate_bits(inv, radius)]
    changed = sum(1 for a, b in zip(rows, closed) if a != b)
    _bits_to_mask(closed, nx, ny, g.mask)
    return changed


# --------------------------------------------------------------------------- 探测
def _cell_root(comp, g, x, y):
    i, j = g.to_cell(float(x), float(y))
    if 0 <= i < g.nx and 0 <= j < g.ny:
        return comp.root_of(i, j)
    return None


def _probe_dir(comp, g, x, y, dx, dy, off):
    """沿法向在偏移 ``off`` 处取连通域；落在挡格上时**只继续外推**。

    早期版本在挡格上向四个方向找空格，结果从墙的一侧会跨到另一侧，把两侧读成
    同一片空间——几乎每条真墙都被判成"不分隔"，判据形同虚设。方向必须单向。
    """
    for k in range(0, PROBE_REACH + 1):
        d = off + k * g.res
        root = _cell_root(comp, g, x + dx * d, y + dy * d)
        if root is not None:
            return root
    return None


def _is_space(comp, root, min_cells) -> bool:
    """够大的一块空间，或室外（室外是无限面，任何墙都可以拿它当另一侧）。"""
    if root is None:
        return False
    if root == comp.outside_root:
        return True
    return comp.size.get(root, 0) >= min_cells


def _split_positions(wall, comp, g, thickness, min_cells):
    """沿墙取三处，量两侧是不是两块不同的空间。返回 (命中数, 采样数)。"""
    from . import walls as WALLS
    if not (wall.get("start") and wall.get("end")):
        return 0, 0
    a = WALLS._along(wall, wall["start"])
    b = WALLS._along(wall, wall["end"])
    if a > b:
        a, b = b, a
    if b - a <= 0:
        return 0, 0
    (ux, uy), (nx, ny) = WALLS._basis(wall)
    t = float(wall.get("thickness") or thickness)
    # 挡格比真实墙厚多一格（见 _fill），探测点必须让开这一格再留一格余量
    off = t / 2.0 + 2.0 * g.res
    split = 0
    counted = 0
    for frac in (0.25, 0.5, 0.75):
        u = a + (b - a) * frac
        px, py = WALLS._world(wall, u, wall.get("center") or 0.0)
        rl = _probe_dir(comp, g, px, py, nx, ny, off)
        rr = _probe_dir(comp, g, px, py, -nx, -ny, off)
        counted += 1
        if rl is None or rr is None or rl == rr:
            continue                      # 同一片空间（悬空线）或打不到空格
        if _is_space(comp, rl, min_cells) and _is_space(comp, rr, min_cells):
            split += 1
    return split, counted


# --------------------------------------------------------------------------- 生成门
def separation_gate(walls, *, columns=(), min_space=None, seal_gap=None,
                    keep_ratio=None, res=None, door_w=None
                    ) -> tuple[list, list, dict]:
    """按"是否分隔空间"给候选分流。返回 ``(保留, 剔除, diag)``。

    ``min_space``：判定"一块空间"的面积下界（mm²）。缺省按墙厚兜底；生产路径由
    ``scale`` 的字高基准推出并传入。
    """
    items = [w for w in walls if w.get("start") and w.get("end")]
    diag = {"net": mode(), "candidates": len(walls)}
    if not enabled():
        diag["reason"] = "disabled"
        return list(walls), [], diag
    if len(items) < 4:
        diag["reason"] = "too_few_candidates"
        return list(walls), [], diag

    thick = SC.median([float(w["thickness"]) for w in items
                       if w.get("thickness")], default=SC.DEFAULT_THICKNESS)
    if min_space is None:
        # 兜底口径：8 个墙厚见方的方格 × 比例。生产路径用字高基准，不走这里。
        min_space = (8.0 * thick) ** 2 * SPACE_RATIO
    min_space = float(min_space)

    g, comp, sealed = _build_grid(items, thick, columns, res=res,
                                  seal_gap=seal_gap, door_w=door_w)
    if g is None or comp is None:
        diag["reason"] = "no_grid"
        return list(walls), [], diag

    min_cells = max(4, int(min_space / (g.res * g.res)))
    spaces = [r for r, size in comp.size.items()
              if r != comp.outside_root and size >= min_cells]
    diag.update({"res": round(g.res, 1), "grid": [g.nx, g.ny],
                 "components": len(comp.size), "spaces": len(spaces),
                 "min_space_m2": round(min_space / 1e6, 3), "sealed": sealed})

    # 安全阀 1：这张图根本没形成空间划分（墙网不闭合 / 只有标注线）
    if len(spaces) < 3:
        diag["reason"] = "no_space_structure"
        diag["fallback"] = "no_space_structure"
        return list(walls), [], diag

    kept, cut = [], []
    for w in items:
        split, _counted = _split_positions(w, comp, g, thick, min_cells)
        w["net_split"] = split
        if split >= MIN_SPLIT:
            w["net_state"] = "separates_space"
            kept.append(w)
        else:
            w["net_state"] = "same_space"
            w["net_reason"] = (f"三处探测只有 {split} 处两侧是不同空间"
                               f"（同一片空间 = 悬空线：标注/家具/引线）")
            cut.append(w)

    # 安全阀 2：绝大多数候选都不分隔空间 -> 这张图的墙网本来就不闭合，结论不可信
    ratio = KEEP_RATIO if keep_ratio is None else float(keep_ratio)
    if kept and len(kept) < ratio * len(items):
        diag["fallback"] = "too_aggressive"
        diag["would_keep"] = len(kept)
        diag["would_cut"] = len(cut)
        for w in items:
            w["net_state"] = "fallback"
        diag["kept"] = len(items)
        diag["dropped"] = 0
        return list(walls), [], diag
    if not kept:
        diag["fallback"] = "nothing_kept"
        diag["kept"] = len(items)
        diag["dropped"] = 0
        return list(walls), [], diag

    diag["kept"] = len(kept)
    diag["dropped"] = len(cut)
    return kept, cut, diag


def summary(diag) -> str:
    """一行人类可读摘要（进诊断输出，不参与判定）。"""
    d = diag or {}
    if d.get("net") in ("off", None):
        return "墙网分隔检验：关闭"
    if d.get("fallback"):
        return (f"墙网分隔检验：本图不适用（{d['fallback']}），"
                f"候选全部保留")
    if "kept" not in d:
        return f"墙网分隔检验：未执行（{d.get('reason')}）"
    return (f"墙网分隔检验：{d['kept']}/{d['candidates']} 条候选分隔空间"
            f"（{d.get('spaces')} 块空间，剔除 {d['dropped']} 条悬空线）")
