"""图内自校准尺度：把"图纸专属的绝对毫米阈值"换成"图内分布的倍数"。

为什么需要这一层
----------------
识别器里原本写死了一批绝对毫米数（柱截面 150~2500、洞口 250~3000、最小面域
1 m²、短碎线 600、端头容差 300…）。这些数只对"1:100、墙厚 200、门宽 900"的
常规平面成立；换一张 1:50 详图、一张米制图、一张厚墙工业厂房，同一套阈值就会
整体失效——而失效方式是静默的（候选直接消失，不报错）。

本模块只做一件事：**从图里量出基准尺度，再把阈值表达成基准尺度的倍数**。
倍率来自建筑构件的物理关系（柱边长≈墙厚的若干倍、洞口宽≈门宽的若干倍、
房间最小面积≈若干个字的方格），这类关系跨图纸不变；绝对毫米数则不行。

三条不变量
----------
1. 量不出基准量时**退回建筑常识默认值**，绝不让下游拿到 None 去做算术；
2. 所有比值都可在环境变量里覆盖（诊断期调参不必改代码）；
3. 本模块只读快照里已有的数字，不碰 ezdxf、不判断构件身份、不产生坐标。
"""
from __future__ import annotations

import math
import os

from . import geometry as GEO
from .snapshot import geometry_records

#: 建筑常识默认值（mm）：只在图里量不出来时使用。
#: 这些不是"阈值"，是"一张常规建筑平面通常长什么样"的量级参考。
DEFAULT_THICKNESS = 200.0      # 常规墙厚
DEFAULT_DOOR_W = 900.0         # 常规单扇门宽
DEFAULT_TEXT_H = 350.0         # 常规房间名字高
DEFAULT_BAY = 6000.0           # 常规柱网间距
DEFAULT_COL = 500.0            # 常规柱截面
DEFAULT_NOISE = 2.0            # 常规 CAD 图纸的自噪声（mm）：线头对齐误差量级

#: 量值合法区间（mm）：超出的观测值视为异常样本，不参与中位数
_PLAUSIBLE = {
    "thickness": (60.0, 2000.0),
    "door_w": (300.0, 6000.0),
    "text_h": (40.0, 5000.0),
    "bay": (800.0, 60000.0),
    "col": (100.0, 5000.0),
    "noise": (0.01, 500.0),
}

#: **全系统唯一的一张常数表**。这里的每个数都是无量纲的，表达的是建筑构件之间
#: 的物理关系，与图纸比例、单位、绘制习惯无关——换任何一张图，这张表一个字都不
#: 改。除此之外识别器里**不允许**再出现裸的毫米阈值（有单测强制）。
#:
#: 每一项都必须写清"为什么跨图纸成立"，否则它就只是一个被藏起来的魔数。
STRUCTURE = {
    # ---- 相对"图内基准量"的倍率
    # 柱短边下界：一根柱的截面不会比墙厚还薄（再薄是线脚/面层），取 0.75 倍墙厚
    "col_short_min_thickness": float(os.environ.get("DWG_K_COL_SHORT_MIN", "0.75")),
    # 柱短边上界：一张图里最大的柱不超过 2 个门宽（门宽是这张图的尺度标尺）
    "col_short_max_door": float(os.environ.get("DWG_K_COL_SHORT_MAX", "2.0")),
    # 柱长边上界：最长的构造柱/墙垛不超过 3 个门宽
    "col_long_max_door": float(os.environ.get("DWG_K_COL_LONG_MAX", "3.0")),
    # 洞口宽下界：窄于 0.3 个门宽的是家具缝，不是墙上的洞
    "gap_min_door": float(os.environ.get("DWG_K_GAP_MIN", "0.30")),
    # 洞口宽上界：宽于 4 个门宽的是通道/开间，不是单樘洞口
    "gap_max_door": float(os.environ.get("DWG_K_GAP_MAX", "4.0")),
    # 最小面域：约 (4 个字高)²，再小是墙缝或柱缝
    "face_min_text": float(os.environ.get("DWG_K_FACE_MIN", "4.0")),
    # 观察面域允许补的最大缺口：2 个门宽（门/窗本身就是墙上的正常缺口）
    "seal_max_door": float(os.environ.get("DWG_K_SEAL_MAX", "2.0")),

    # ---- 相对"图纸自噪声"的倍率（容差类，统一来源）
    # 共线/轴向判等：图上两条线"算不算同一条"，容差 = 1 倍自噪声
    "tol_align_noise": float(os.environ.get("DWG_K_TOL_ALIGN", "1.0")),
    # 端点相接：两个线头"算不算接上了"，容差 = 2 倍自噪声
    "tol_join_noise": float(os.environ.get("DWG_K_TOL_JOIN", "2.0")),
    # 重复线去重：同一条线画了两遍，中心距在 3 倍自噪声内视为重复
    "tol_dedupe_noise": float(os.environ.get("DWG_K_TOL_DEDUPE", "3.0")),
    # 房名压在墙线上仍算"在这间房里"：容差 = 3 倍自噪声 + 1 个字高
    "label_inside_text": float(os.environ.get("DWG_K_LABEL_INSIDE", "1.5")),
    # 同名房间相邻合并：2 个字高
    "same_name_text": float(os.environ.get("DWG_K_SAME_NAME", "2.5")),
    # 面域剖分栅格：1/4 个字高（字高是这张图上"最小可辨房间特征"的量级）
    "split_res_text": float(os.environ.get("DWG_K_SPLIT_RES", "0.25")),
    # 伪缺口判定：宽度落在"墙厚"的同一条带里（±该比例）的墙缺口不是洞口，
    # 而是 T/L 形墙接头处的断线。依据是构造而非尺寸：接头处让出的那段长度
    # 恒等于**与之相接那道墙的厚度**，所以这个带必然贴着墙厚、与图纸比例无关。
    "gap_artifact_thickness": float(os.environ.get("DWG_K_GAP_ARTIFACT", "0.15")),
    # 自噪声级碎缝：短于 3 倍自噪声的"缺口"是线头没对齐，不是任何构件
    "gap_noise_join": float(os.environ.get("DWG_K_GAP_NOISE", "3.0")),
    # 能"把两个房间隔开"的洞口宽度上界：2 个门宽。更宽的是通道/开敞门洞，
    # 房间本来就通着，把这种开口补成墙等于凭空造房间。
    "barrier_max_gap_door": float(os.environ.get("DWG_K_BARRIER_GAP", "2.0")),
    # ---- 楼梯（相对门宽）。踏步宽 250~320mm ≈ 0.3 个门宽；踏面线的长度就是
    # 梯段宽 1.2~2.0m ≈ 1.3~2.2 个门宽。这两个比例来自"人要能走"这个物理事实。
    "stair_tread_min_door": float(os.environ.get("DWG_K_STAIR_TREAD_MIN", "0.18")),
    "stair_tread_max_door": float(os.environ.get("DWG_K_STAIR_TREAD_MAX", "0.55")),
    "stair_width_min_door": float(os.environ.get("DWG_K_STAIR_W_MIN", "0.60")),
    "stair_width_max_door": float(os.environ.get("DWG_K_STAIR_W_MAX", "2.60")),
    # 同一梯段的踏面线首尾对齐容差：0.1 个门宽（画图误差，不是几何尺度）
    "stair_align_door": float(os.environ.get("DWG_K_STAIR_ALIGN", "0.10")),
    # ---- 弧墙（相对墙厚）。弧墙的两个面是同圆心圆弧，"厚度"等于半径差，
    # 所以这里的判据全用墙厚的倍率表达，与图纸比例无关。
    # 圆心要"同一个"：两条面线的圆心差不超过 0.05 个墙厚（画图误差量级）
    "arc_center_tol_thickness": float(os.environ.get("DWG_K_ARC_CENTER_TOL", "0.05")),
    # 半径差 = 墙厚。下界 0.25 倍：再薄的是玻璃/面层；上界 2 倍：再厚的是两道墙
    "arc_thickness_min_thickness": float(os.environ.get("DWG_K_ARC_T_MIN", "0.25")),
    "arc_thickness_max_thickness": float(os.environ.get("DWG_K_ARC_T_MAX", "2.0")),
    # 半径小于 0.8 个墙厚的是门扇开启弧/装饰小弧，不是墙
    "arc_radius_min_thickness": float(os.environ.get("DWG_K_ARC_R_MIN", "0.8")),
    # 两面弧的角度重叠要占较短那条弧的 60%（缺的部分是洞口）
    "arc_span_overlap": float(os.environ.get("DWG_K_ARC_SPAN_OVERLAP", "0.60")),
    # 重叠至少要 15°：太短的弧段配不出可靠的墙线
    "arc_min_sweep_deg": float(os.environ.get("DWG_K_ARC_MIN_SWEEP", "15.0")),
    # 中间夹的第三条同心弧两侧各要留 0.3 个厚度，才算把厚度劈成两段真墙
    "arc_mid_block_ratio": float(os.environ.get("DWG_K_ARC_MID_RATIO", "0.30")),
    # 弧带折线化的角度步长（度）：5° 时 r=2000 的弓高约 2mm，远小于墙厚
    "arc_tess_deg": float(os.environ.get("DWG_K_ARC_TESS", "5.0")),
    # ---- 幕墙（相对墙厚）。幕墙在平面上画成共面三线：外皮/内皮 + 中间一条分格
    # 基准线。"带"的厚度 = 外皮到内皮，带内线不是第二道墙。
    # 相邻面线间距要"相等"：这是"同一条带"的判据，容差 10%
    "curtain_gap_tol": float(os.environ.get("DWG_K_CURTAIN_GAP_TOL", "0.10")),
    # 带内每段间距上界：0.8 倍墙厚。再宽就是两道独立的墙，不是一条带的分格线
    "curtain_band_max_thickness": float(os.environ.get("DWG_K_CURTAIN_MAX_GAP", "0.8")),
    # 带内线与两端的覆盖比例：至少 80% 共线才算同一条带
    "curtain_cover_ratio": float(os.environ.get("DWG_K_CURTAIN_COVER", "0.8")),
    # 带的最短长度：15 倍墙厚（本图 3m）。再短的"三线"多半是门窗套线脚
    "curtain_min_len_thickness": float(os.environ.get("DWG_K_CURTAIN_MIN_LEN", "15.0")),
    # 面线位置聚类容差：0.01 倍墙厚（画图误差量级）
    "curtain_pos_tol_thickness": float(os.environ.get("DWG_K_CURTAIN_POS_TOL", "0.01")),
    # 幕墙带被结构柱切成一片一片，每片的最短长度：1.5 倍墙厚（300mm）。
    # 比它还短的是转角处的碎片，交给合并环节也拼不出墙。
    "curtain_piece_min_thickness": float(os.environ.get("DWG_K_CURTAIN_PIECE", "1.5")),

    # ---- 图层角色的"这批线像不像墙"判据（layer_roles_infer 用）。
    # 这里**没有任何毫米数**：墙 = 两条面线，重叠长度 ≥ 6 倍面线间距。这个比例
    # 无量纲、跨图纸成立，不需要先知道这张图的墙厚（图里可能有 100、200、300
    # 好几种墙，锁死一个基准厚度反而会把另一种墙整层漏掉）。
    # 细长比（重叠 / 间距）：墙 8~27、楼梯踏步≈3、柱≈1、家具围合 1~3
    "layer_strip_ratio": float(os.environ.get("DWG_K_LAYER_STRIP", "6.0")),
    "layer_aspect_lo": float(os.environ.get("DWG_K_LAYER_ASPECT_LO", "4.0")),
    "layer_aspect_hi": float(os.environ.get("DWG_K_LAYER_ASPECT_HI", "10.0")),
    # 细长双线占本层线长的比例：低于下界是散线/装饰，高于上界基本整层是墙
    "layer_share_lo": float(os.environ.get("DWG_K_LAYER_SHARE_LO", "0.06")),
    "layer_share_hi": float(os.environ.get("DWG_K_LAYER_SHARE_HI", "0.20")),
    # 密度（细长双线长度 / 本层包围盒对角线）：零散几段凑不出这个量级
    "layer_density_lo": float(os.environ.get("DWG_K_LAYER_DENSITY_LO", "0.10")),
    "layer_density_hi": float(os.environ.get("DWG_K_LAYER_DENSITY_HI", "0.45")),
    # 墙层的规模下限：少于这个数量的面线，统计量不可信（只剩 17 条线的楼梯层
    # 会靠几条长线冒充墙层）。40 条以下线性打折，以上不打折。
    "layer_min_lines": float(os.environ.get("DWG_K_LAYER_MIN_LINES", "20.0")),
    # 标注占比否决线：尺寸标注+文字占本层图元的比例超过它就绝不判成墙层——
    # 这些层的平行双线在几何上与墙一模一样，只有实体构成能区分。
    "layer_annotation_veto": float(os.environ.get("DWG_K_LAYER_ANNOT", "0.05")),
    # 墙厚下限（相对常规墙厚 DEFAULT_THICKNESS）：比常规墙厚一半还薄的平行线对
    # 不是墙——本图实测踏步 30、梯段边梁 70、窗框 70、管道 70 都在这条线以下。
    # 这是建筑常识，不是这张图的统计量。
    "wall_min_thickness_ratio": float(os.environ.get("DWG_K_WALL_MIN_T", "0.5")),
    # 候选墙厚的"窄带"与"宽带"（相对该墙厚）：窄带=同一条墙的两个面，
    # 宽带=两道相邻的墙/墙+面层。细长比只在窄带内量。
    "layer_consensus_lo": float(os.environ.get("DWG_K_LAYER_CONS_LO", "0.80")),
    "layer_consensus_hi": float(os.environ.get("DWG_K_LAYER_CONS_HI", "1.25")),
    "layer_band_lo": float(os.environ.get("DWG_K_LAYER_BAND_LO", "0.50")),
    "layer_band_hi": float(os.environ.get("DWG_K_LAYER_BAND_HI", "2.00")),
    # 候选墙厚峰值的相对门槛（与最强峰比）：一张图可以有几种墙厚（砌体 200、
    # 轻钢隔墙 100、幕墙带），但不会七八种。本图实测踏步 310、线脚 410 都到不了。
    "layer_peak_share": float(os.environ.get("DWG_K_LAYER_PEAK", "0.25")),
    # 墙的几何证据强到这个程度时，图层名不能一票否决：实体被搬到"家具"层、
    # 而那一层其实全是墙线时，名字先验是错的，几何才是直接证据。
    # 只对墙生效——门/窗/柱的"长相"很容易被同名层骗，那里仍以名字为准。
    "layer_geom_force": float(os.environ.get("DWG_K_LAYER_GEOM_FORCE", "0.85")),
    "layer_geom_margin": float(os.environ.get("DWG_K_LAYER_GEOM_MARGIN", "0.25")),
}

#: 柱候选的重叠判据：一个矩形被另一个基本包住（交叠面积 / 较小面积 ≥ 此值）
#: 就认为是**同一组线枚举出来的两个解**，不是两根柱。无量纲，与图纸比例无关。
#: 单图层图实测：一组线枚举出 1810×300 / 1810×600 / 1810×1200 三个互相包含的
#: 矩形，中心距去重（0.5×短边）挡不住，必须按面积重叠压掉。
COL_NMS_CONTAIN = 0.85

#: 后向兼容别名（老调用点还在用 RATIO 的下标）
RATIO = STRUCTURE


# ------------------------------------------------------------------ 通用估计子
def quantile(values, q, default=None):
    vals = sorted(float(v) for v in (values or ()) if v is not None and v > 0)
    if not vals:
        return default
    k = max(0, min(len(vals) - 1, int(round(q * (len(vals) - 1)))))
    return vals[k]


def median(values, default=None):
    """中位数：偶数个样本取中间两个的均值。"""
    vals = sorted(float(v) for v in (values or ()) if v is not None and v > 0)
    if not vals:
        return default
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0


def mode_of(values, *, rel_bw=0.06, default=None, min_n=6, max_n=200_000):
    """**众数**（排序 + 滑窗最大密度），O(n log n)，可处理几十万样本。

    为什么不用中位数：图纸里"主流尺寸"和"平均值"不是一回事。200 厚墙占 90%、
    100 厚隔墙占 10% 时中位数还在 200；但 60/40 分布时中位数会落在两簇之间的
    **空档**上——那正好是最不该当"典型值"的位置。众数直接落在最大那一簇的峰上。

    性能：曾经用"对每个样本统计邻域权重"的写法，是 O(n²)——块全部炸开的图纸
    线对数暴涨，直接把整条链路卡死（explode 扰动跑不完）。现在改成在**排序后
    的数组上滑窗**：每个样本只需两次二分，整体 O(n log n)。
    """
    vals = sorted(float(v) for v in (values or ()) if v is not None and v > 0)
    if not vals:
        return default
    if len(vals) < min_n:
        return median(vals, default)
    if len(vals) > max_n:                      # 极端样本量下等距抽样，保分布形状
        step = len(vals) // max_n + 1
        vals = vals[::step]
    lo_v, hi_v = vals[0], vals[-1]
    span = hi_v - lo_v
    if span <= 1e-9:
        return vals[0]
    bw = max(span / 64.0, abs(median(vals, 1.0)) * rel_bw, 1e-6)

    import bisect
    best_i, best_w = 0, -1
    for i, v in enumerate(vals):
        left = bisect.bisect_left(vals, v - bw)
        right = bisect.bisect_right(vals, v + bw)
        w = right - left
        if w > best_w:
            best_i, best_w = i, w
    return vals[best_i]


def gap_split(values, *, min_n=8, rel_min=0.08):
    """**最大相对空档**处切一刀，返回分界值。

    写死的判别边界（"墙厚 < 60 是面层、> 60 是墙"）在别的图上就不成立。分布自己
    会告诉你该在哪切：把样本排序，找相邻两个数之间**相对间隙最大**的那一处，
    它两侧就是两簇。间隙不够大（相对 ``rel_min``）说明只有一簇，返回 ``None``。
    """
    vals = sorted(set(float(v) for v in (values or ()) if v is not None and v > 0))
    if len(vals) < min_n:
        return None
    best, best_rel = None, 0.0
    for a, b in zip(vals, vals[1:]):
        if a <= 0:
            continue
        rel = (b - a) / a
        if rel > best_rel:
            best, best_rel = (a + b) / 2.0, rel
    if best_rel < rel_min:
        return None
    return best


def endpoint_noise(snapshot, *, rel_min=0.5, head_frac=0.5, max_points=5000):
    """**图纸自噪声**：线头"本该重合却差了多少"的量级（mm）。

    这不是"任意两个端点之间的距离"——大部分端点本来就离得很远（墙头、引线端），
    取 95 分位只会量到图的尺度（实测拿到 200mm，纯属胡说）。真正要找的是那条
    **近零的簇**：同一条折线的相邻段共用端点（距离 0），而本该接到一起却没接准
    的线头会形成一小撮"小而非零"的距离。取这一簇的上沿，就是这张图的绘图误差。

    做法：去掉精确重合（0），在分布的前半段里找**最大相对间隙**，间隙下沿即噪声。
    找不到明显间隙（说明图很干净、或本来就没有要接的线头）就取中位数。
    """
    pts = []
    for rec in geometry_records(snapshot or {}):
        for (x1, y1), (x2, y2) in _iter_segments(rec):
            pts.append((x1, y1))
            pts.append((x2, y2))
            if len(pts) >= max_points * 2:
                break
        if len(pts) >= max_points * 2:
            break
    if len(pts) < 64:
        return None

    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    span = max(max(xs) - min(xs), max(ys) - min(ys))
    if span <= 0:
        return None
    cell = max(span / 512.0, 1e-3)
    grid = {}
    for idx, (x, y) in enumerate(pts):
        grid.setdefault((int(x // cell), int(y // cell)), []).append(idx)

    near = []
    for idx, (x, y) in enumerate(pts):
        cx, cy = int(x // cell), int(y // cell)
        best = None
        for ring in (1, 2, 3):
            for gx in range(cx - ring, cx + ring + 1):
                for gy in range(cy - ring, cy + ring + 1):
                    # Extremely dense annotation/furniture cells can contain
                    # thousands of endpoints. Fixed-size deterministic samples
                    # prevent O(n^2) nearest-endpoint scans on large drawings.
                    ids = grid.get((gx, gy), ())
                    step = max(1, len(ids) // 32)
                    for j in ids[::step]:
                        if j == idx:
                            continue
                        d = (pts[j][0] - x) ** 2 + (pts[j][1] - y) ** 2
                        if best is None or d < best:
                            best = d
            if best is not None:
                break
        if best is not None:
            near.append(best ** 0.5)
    if len(near) < 64:
        return None

    vals = sorted(d for d in near if d > 1e-6)      # 去掉精确重合的共用端点
    if len(vals) < 32:
        return None
    head = vals[:max(8, int(head_frac * len(vals)))]
    best, best_rel = head[len(head) // 2], 0.0
    for a, b in zip(head, head[1:]):
        if a <= 1e-9:
            continue
        rel = (b - a) / a
        if rel > best_rel:
            best, best_rel = a, rel
    return best if best_rel >= rel_min else head[len(head) // 2]


def _iter_segments(rec):
    kind = rec.get("type")
    if kind == "LINE":
        s, e = rec.get("start"), rec.get("end")
        if s and e:
            yield (float(s[0]), float(s[1])), (float(e[0]), float(e[1]))
    elif kind in ("LWPOLYLINE", "POLYLINE"):
        pts = [(float(p[0]), float(p[1])) for p in (rec.get("points") or ())]
        for a, b in zip(pts, pts[1:]):
            yield a, b
        if rec.get("closed") and len(pts) > 2:
            yield pts[-1], pts[0]


def _plausible(name, value):
    lo, hi = _PLAUSIBLE.get(name, (0.0, float("inf")))
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if lo <= v <= hi else None


# ------------------------------------------------------------------ 测量
def _rotated_thickness_samples(snapshot, wall_layers=()):
    """Direction-invariant fallback for parallel wall faces drawn at any angle.

    Lines are binned by direction modulo 180 degrees, then projected onto the
    bin's unit tangent/normal. Distances are compared in local coordinates,
    rather than relying on world X/Y being aligned to the drawing.
    """
    wanted = set(wall_layers or ())
    groups = {}
    for rec in geometry_records(snapshot or {}):
        if rec.get("type") != "LINE" or (wanted and rec.get("layer") not in wanted):
            continue
        start, end = rec.get("start"), rec.get("end")
        if not start or not end:
            continue
        dx, dy = float(end[0]) - float(start[0]), float(end[1]) - float(start[1])
        length = math.hypot(dx, dy)
        if length < 200.0:
            continue
        deg = (math.degrees(math.atan2(dy, dx)) % 180.0)
        bin_id = round(deg * 4) % 720   # quarter-degree bins
        angle = math.radians(bin_id / 4.0)
        ux, uy = math.cos(angle), math.sin(angle)
        nx, ny = -uy, ux
        normal = ((float(start[0]) + float(end[0])) * nx
                  + (float(start[1]) + float(end[1])) * ny) / 2.0
        along = sorted((float(start[0]) * ux + float(start[1]) * uy,
                        float(end[0]) * ux + float(end[1]) * uy))
        groups.setdefault(bin_id, {}).setdefault(round(normal, 1), []).append(tuple(along))
    samples = []
    for parallel in groups.values():
        keys = sorted(parallel)
        for i, k in enumerate(keys):
            for k2 in keys[i + 1:]:
                dist = k2 - k
                if dist > 1200.0:
                    break
                if dist < 40.0:
                    continue
                overlap = max((min(b1, b2) - max(a1, a2)
                               for a1, b1 in parallel[k]
                               for a2, b2 in parallel[k2]), default=0.0)
                if overlap < 200.0:
                    continue
                samples.extend([dist] * max(1, min(int(overlap / 200.0), 40)))
                if len(samples) > 100_000:
                    return samples
    return samples


def _thickness_mode(snapshot, wall_layers=(), *, with_count=False):
    """墙厚 = 图里**平行线对垂距**分布的众数（不需要先认出墙）。

    以前这里没有墙结果就直接退到"默认 200"，等于放弃了这张图自己的信息。其实
    墙厚就写在图上：同一方向的平行线，垂距成簇，主峰就是这张图的主流墙厚。
    只取有明显沿向重叠的线对，避免把两条不相干的线算成一个厚度。
    """
    lines = {"H": {}, "V": {}}
    wanted = set(wall_layers or ())
    for rec in geometry_records(snapshot or {}):
        if rec.get("type") != "LINE":
            continue
        if wanted and rec.get("layer") not in wanted:
            continue
        (x1, y1), (x2, y2) = (rec["start"], rec["end"])
        if abs(y1 - y2) <= 1.5 and abs(x2 - x1) > 50:
            lines["H"].setdefault(round(y1, 1), []).append(
                (min(x1, x2), max(x1, x2)))
        elif abs(x1 - x2) <= 1.5 and abs(y2 - y1) > 50:
            lines["V"].setdefault(round(x1, 1), []).append(
                (min(y1, y2), max(y1, y2)))
    dists = []
    for orient, groups in lines.items():
        keys = sorted(groups)
        for i, k in enumerate(keys):
            for k2 in keys[i + 1:]:
                d = k2 - k
                if d > 1200.0:
                    break
                if d < 40.0:
                    continue
                best_ov = 0.0
                for (a1, b1) in groups[k]:
                    for (a2, b2) in groups[k2]:
                        ov = min(b1, b2) - max(a1, a2)
                        if ov > best_ov:
                            best_ov = ov
                if best_ov < 200.0:
                    continue
                # 按沿向重叠长度加权：一堵 20m 的墙比一条 300mm 的线脚更能代表
                # 这张图的墙厚，纯粹按"线对个数"投票会被大量短线脚淹没。
                dists.extend([d] * max(1, min(int(best_ov / 200.0), 40)))
                if len(dists) > 400_000:
                    guess = mode_of(dists)
                    return (guess, len(dists)) if with_count else guess
    if not dists:
        dists = _rotated_thickness_samples(snapshot, wall_layers)
    guess = mode_of(dists) if dists else None
    return (guess, len(dists)) if with_count else guess


#: 门族块名的特征词（与 elements.DOOR_BLOCKS 同一族的保守匹配，避免循环导入）
#: 门族块的命名线索。**``_DBLK`` 一类通用名必须在内**：实战里大量图纸不用厂商名，
#: 只用 ``_DBLK`` / ``_DBLK1`` / ``_DBLK2`` 画门；漏掉它们会让"门宽"量成 397mm
#: （实测艺术楼），下游所有"几倍门宽"的阈值跟着全线失真。
_DOOR_NAME_HINTS = ("door", "dorlib", "门", "dblk")


def _door_like(name) -> bool:
    low = str(name or "").lower()
    return any(h in low for h in _DOOR_NAME_HINTS)


def _door_width_mode(snapshot, *, with_count=False):
    """门宽 = 开启弧半径 / 门块 xscale 的众数（只认"像门"的那些）。"""
    radii, scal = [], []
    for rec in geometry_records(snapshot or {}):
        if rec.get("type") == "ARC":
            r = float(rec.get("radius") or 0.0)
            sweep = (float(rec.get("end_angle") or 0.0)
                     - float(rec.get("start_angle") or 0.0)) % 360.0
            if 300.0 <= r <= 2500.0 and 40.0 <= sweep <= 150.0:
                radii.append(r)
        elif rec.get("type") == "INSERT":
            # 只认**门族**块：柱块（截面 650）和窗块（宽 1500）混进来会把众数带偏，
            # 办公楼就是这么量出 350 的"门宽"。
            if not _door_like(rec.get("name")):
                continue
            v = abs(float(rec.get("xscale") or 0.0))
            if 300.0 <= v <= 2500.0:
                scal.append(v)
    if scal:
        guess = mode_of(scal)
        return (guess, len(scal)) if with_count else guess
    if not radii:
        return (None, 0) if with_count else None
    # 没有门族块时，弧半径是一堆混合：卫生器具的弧半径小（400 上下）、门的弧半径
    # 大（900 上下）。写死一个下限不行，用**最大间隙**把它们分开，取门那一簇。
    split = gap_split(radii, min_n=12, rel_min=0.15)
    if split:
        big = [r for r in radii if r >= split]
        if len(big) >= 6:
            guess = mode_of(big)
            return (guess, len(big)) if with_count else guess
    guess = mode_of(radii)
    return (guess, len(radii)) if with_count else guess


def measure(snapshot, *, walls=(), doors=(), plan=None) -> dict:
    """量出图内基准尺度（mm）。每个量都带出处，量不出来才退到常识默认值。"""
    ents = snapshot or {}
    prov = {}

    def put(key, value, source, estimator, n=0):
        samples = int(n)
        prov[key] = {"value": None if value is None else round(float(value), 3),
                     "source": source, "estimator": estimator, "n": samples,
                     "sample_count": samples,
                     "sample_status": ("default" if source == "default"
                                       else "sufficient" if samples >= 6
                                       else "insufficient") }
        return value

    # 字高要量**房间名那一群人**：全图还有大量标注/图签小字（225 高），
    # 拿全图众数会把"房间名字高"量成标注字高，后面所有相对字高的阈值全跟着偏。
    name_layers = set(((plan or {}).get("layers") or {}).get("room_name") or ())
    name_heights = [e.get("height") for e in (ents.get("entities") or ())
                    if e.get("type") in ("TEXT", "MTEXT")
                    and (not name_layers or e.get("layer") in name_layers)]
    if not name_heights:
        name_heights = [e.get("height") for e in (ents.get("entities") or ())
                        if e.get("type") in ("TEXT", "MTEXT")]
    text_h = _plausible("text_h", mode_of(name_heights))
    text_h = put("text_h", text_h, "measured" if text_h else "default",
                 "mode", len(name_heights)) \
        or put("text_h", DEFAULT_TEXT_H, "default", "constant")

    lengths, xs, ys = [], [], []
    for rec in geometry_records(ents):
        for (x1, y1), (x2, y2) in _iter_segments(rec):
            L = math.hypot(x2 - x1, y2 - y1)
            if L > 0:
                lengths.append(L)
                xs.extend((x1, x2))
                ys.extend((y1, y2))
    line_len = median(lengths) or 1000.0
    put("line_len", line_len, "measured", "median", len(lengths))

    # 墙厚：优先用真实墙识别结果；没有就用"平行线对垂距众数"自己量
    measured = median([float(w.get("thickness") or 0) for w in walls
                       if w.get("thickness")])
    measured = _plausible("thickness", measured)
    if measured:
        thickness = put("thickness", measured, "measured", "median", len(walls))
    else:
        guess, n_thick = _thickness_mode(
            ents, ((plan or {}).get("layers") or {}).get("wall") or (), with_count=True)
        guess = _plausible("thickness", guess)
        thickness = (put("thickness", guess, "measured", "mode", n_thick)
                     if guess else put("thickness", DEFAULT_THICKNESS,
                                       "default", "constant"))

    door_w = median([float(d.get("width") or d.get("code_width") or 0)
                     for d in doors if (d.get("width") or d.get("code_width"))])
    door_w = _plausible("door_w", door_w)
    if door_w:
        put("door_w", door_w, "measured", "median", len(doors))
    else:
        guess, n_door = _door_width_mode(ents, with_count=True)
        guess = _plausible("door_w", guess)
        door_w = (put("door_w", guess, "measured", "mode", n_door)
                  if guess else put("door_w", DEFAULT_DOOR_W, "default", "constant"))

    # 柱距：轴线位置差的众数；没有轴线就用墙线间距的众数
    axis_layers = set(((plan or {}).get("layers") or {}).get("axis") or ())
    # "是不是通长轴/墙线"按**图面跨度**的比例判，不按某条线长：全线长度中位数
    # 会被块内碎线拉到几十毫米，4 倍于它等于"任何线都算长线"。
    span_x = (max(xs) - min(xs)) if xs else 0.0
    span_y = (max(ys) - min(ys)) if ys else 0.0
    long_axis = 0.05 * max(span_x, span_y, 1.0)
    axis_pos = set()
    for rec in geometry_records(ents):
        if rec.get("type") != "LINE":
            continue
        if axis_layers and rec.get("layer") not in axis_layers:
            continue
        for (x1, y1), (x2, y2) in _iter_segments(rec):
            if abs(y1 - y2) <= 1.5 and abs(x2 - x1) >= long_axis:
                axis_pos.add(round(y1, 1))
            elif abs(x1 - x2) <= 1.5 and abs(y2 - y1) >= long_axis:
                axis_pos.add(round(x1, 1))
    ordered = sorted(axis_pos)
    bays = [b - a for a, b in zip(ordered, ordered[1:]) if b - a > 0]
    bay = _plausible("bay", mode_of(bays)) or _plausible("bay", median(bays))
    bay = (put("bay", bay, "measured", "mode", len(bays))
           if bay else put("bay", DEFAULT_BAY, "default", "constant"))

    # 图纸自噪声：所有容差的统一来源。放在最后量——它要用门宽/字高兜上界，
    # 否则一旦量偏（比如把图的尺度当成误差），整套容差会一起放大。
    noise = endpoint_noise(ents)
    cap = 0.20 * min(float(door_w), float(text_h) * 2.0)
    if noise is not None:
        noise = min(float(noise), cap)
    noise = (put("noise", noise, "measured", "first_gap")
             if noise else put("noise", DEFAULT_NOISE, "default", "constant"))

    col = median([min(float(c.get("w") or 0), float(c.get("h") or 0))
                  for c in (ents.get("columns") or ())])
    col = _plausible("col", col)
    put("col", col or max(DEFAULT_COL, 2.0 * thickness),
        "measured" if col else "default", "median")

    return {
        "units": "mm",
        "thickness_source": prov["thickness"]["source"],
        "thickness": round(thickness, 1),
        "door_w": round(door_w, 1),
        "text_h": round(text_h, 1),
        "line_len": round(line_len, 1),
        "noise": round(float(noise), 3),
        "bay": round(bay, 1),
        "col": round(float(prov["col"]["value"] or 0.0), 1),
        "provenance": prov,
        "samples": {"segments": len(lengths), "axis_positions": len(axis_pos),
                    "walls": len(walls or ()), "doors": len(doors or ())},
    }


def thresholds(scale, keys=None) -> dict:
    """基准量 -> 具体阈值（mm）。**倍率来自 STRUCTURE，绝对值随图变化**。

    每个返回值都能追到出处：要么是"量出来的基准量 × STRUCTURE 里的无量纲倍率"，
    要么是"量出来的基准量本身"。没有一项是拍出来的。
    """
    sc = scale or {}
    t = float(sc.get("thickness") or DEFAULT_THICKNESS)
    w = float(sc.get("door_w") or DEFAULT_DOOR_W)
    h = float(sc.get("text_h") or DEFAULT_TEXT_H)
    bay = float(sc.get("bay") or DEFAULT_BAY)
    n = float(sc.get("noise") or DEFAULT_NOISE)

    def clamp(v, lo, hi):
        return max(lo, min(hi, v))

    out = {
        # 基准量本身
        "thickness": t, "door_w": w, "text_h": h, "bay": bay, "noise": n,
        # 柱（相对墙厚 / 门宽）
        "col_short_min": max(k("col_short_min_thickness") * t, 6.0 * n),
        "col_short_max": k("col_short_max_door") * w,
        "col_long_max": k("col_long_max_door") * w,
        "col_dedupe": max(k("tol_dedupe_noise") * n, 0.10 * w),
        "pier_short_min": 0.75 * t,
        # 洞口下界：优先用"图里实际洞口宽度分布的低簇上沿"；没有墙信息才用比率。
        # 上界**必须**留比率（4 倍门宽）：它的作用只是排掉"整间房/整条走廊"那种
        # 跨度，用分布分位当上界会把真实窗（1500）判成超宽洞口整批砍掉。
        "gap_min": float(sc.get("open_min") or k("gap_min_door") * w),
        "gap_max": k("gap_max_door") * w,
        # 面域（相对字高）
        "face_min_area": (k("face_min_text") * h) ** 2,
        "seal_max": k("seal_max_door") * w,
        # 容差（相对图纸自噪声）—— 全系统统一来源
        "tol_align": clamp(k("tol_align_noise") * n, 0.5, 1.0 * w),
        "tol_join": clamp(k("tol_join_noise") * n, 1.0, 0.5 * w),
        "tol_dedupe": clamp(k("tol_dedupe_noise") * n, 1.0, 0.5 * w),
        "end_tol": clamp(k("tol_join_noise") * n, 1.0, 0.5 * w),
        # 房间（相对字高 / 自噪声）
        "label_inside_tol": k("label_inside_text") * h + 2.0 * n,
        "label_near_tol": 8.0 * h,
        "same_name_adj": k("same_name_text") * h,
        "split_res": clamp(k("split_res_text") * h, 20.0, 200.0),
        "split_min_cell": (h * h) / 4.0,
        # 计算分辨率（量化精度，不参与判别；由字高决定）
        "room_res": clamp(h / 40.0, 5.0, 20.0),
        "short_seg": 3.0 * t,
    }
    if keys:
        return {k: out[k] for k in keys if k in out}
    return out


def drop_artifact_gaps(widths, *, thickness=None, noise=None) -> list:
    """把墙缺口分布里**不是洞口**的成分剔掉，剩下的才当"门宽证据"。

    两类杂质，来源完全不同、判据也不同：

    1. **接头断线**（宽度 ≈ 墙厚）：T/L 形墙接头处，画图时把相交那道墙的端头
       让出来，让出的长度恒等于那道墙的厚度。所以"宽度贴着墙厚的缺口"是画法
       产物，不是洞口。实测艺术楼 22 个墙缺口里有 12 个正好 180mm（= 墙厚），
       它们把"典型门宽"压到 397mm、"洞口下限"压到 2.4mm，再顺着倍率把洞口、
       柱、面域的全部阈值拖偏。
    2. **线头没对齐**（宽度在自噪声量级）：CAD 精度残余，不代表任何构件。

    这两类都只能从**分布**里认（图上没有任何标记说"我不是洞口"），所以剔除
    放在测量阶段，而不是放行到构件层再逐个判——一个 2.4mm 的"洞口"永远不该
    参与"门有多宽"的统计。
    """
    t = float(thickness or 0.0)
    n = float(noise or DEFAULT_NOISE)
    band = k("gap_artifact_thickness") * t if t > 0 else 0.0
    floor = k("gap_noise_join") * n
    out = []
    for w in widths or ():
        w = float(w)
        if w <= 0 or w <= floor:
            continue
        if band and abs(w - t) <= band:
            continue
        out.append(w)
    return out


def refine(scale, *, walls=(), gaps=()) -> dict:
    """**第二阶段测量**：墙网算出来之后，用它把基准量再收一次。

    load 阶段的测量只是初值——那时还没有墙。墙网一旦有了，两件事可以直接量：
    墙厚（真实墙结果的中位数）、门宽（**墙缺口宽度**的众数——门和窗本身就是墙上
    的缺口，这是与画法无关的门宽证据，比拿弧半径猜可靠得多）。

    为什么必须分两阶段：没有门族块的图（大图）只能从弧半径猜门宽，而图上的弧
    大半是卫生器具和幕墙装饰，实测会量出 1616mm；用墙缺口一量就回到 900~1000。
    误差会顺着"门宽 × 倍率"污染洞口、柱、面域全部阈值，所以这个精化不能省。
    """
    out = dict(scale or {})
    prov = dict(out.get("provenance") or {})

    thick = median([float(w.get("thickness") or 0) for w in walls or ()
                    if w.get("thickness")])
    thick = _plausible("thickness", thick)
    if thick:
        out["thickness"] = round(thick, 1)
        out["thickness_source"] = "walls"
        prov["thickness"] = {"value": round(thick, 3), "source": "measured",
                             "estimator": "median", "n": len(walls or ())}

    widths = []
    for g in gaps or ():
        w = float(g.get("width") if g.get("width") is not None
                  else (float(g.get("hi") or 0.0) - float(g.get("lo") or 0.0)))
        if w > 0:
            widths.append(w)
    raw_n = len(widths)
    widths = drop_artifact_gaps(widths, thickness=thick,
                                noise=float(out.get("noise") or DEFAULT_NOISE))
    if raw_n != len(widths):
        prov["gap_artifacts_dropped"] = {"value": raw_n - len(widths),
                                         "source": "rule",
                                         "estimator": "thickness_band+noise_floor",
                                         "n": raw_n}
    if widths:
        split = gap_split(widths, min_n=12, rel_min=0.15)
        cand = [w for w in widths if w >= split] if split else list(widths)
        # "典型单扇门宽"取洞口尺寸分布的 **25 分位**，不取众数：
        # 众数会被数量最多的那一类抓住——大图上双扇门/幕墙洞口很多，众数落在
        # 1616，于是"门宽 × 倍率"全部放大，连"同一洞口中心容差"都涨到 646mm，
        # 把相邻的两樘门并成一樘（门数 64 → 46）。单扇门是分布的下半部分，
        # 25 分位才是"一张图里典型一樘门有多宽"。
        guess = _plausible("door_w", quantile(cand, 0.25) if len(cand) >= 12
                           else mode_of(cand))
        if guess:
            out["door_w"] = round(guess, 1)
            prov["door_w"] = {"value": round(guess, 3), "source": "measured",
                              "estimator": "q25_of_wall_gaps", "n": len(widths)}
        # 洞口宽度**下限**同样从分布里长出来：取"小缝那一簇的上沿"。
        # 不能用 0.3 × 门宽——门宽一旦量到 1616，下限就抬到 485，把 450 宽的
        # 真实单扇门整批砍掉（大图门数从 64 掉到 39 就是这么来的）。
        # 取低簇上沿是**保守**的一侧：宁可多留候选，让后面的结构判据去否。
        split2 = gap_split(widths, min_n=12, rel_min=0.10)
        if split2:
            low_top = max((w for w in widths if w < split2), default=None)
            if low_top:
                out["open_min"] = round(float(low_top), 1)
                prov["open_min"] = {"value": round(float(low_top), 3),
                                    "source": "measured",
                                    "estimator": "lower_cluster_top",
                                    "n": len(widths)}


    out["provenance"] = prov
    return out


def summary(scale) -> dict:
    """进 meta 的摘要（审计用：这张图的尺度是怎么量出来的）。"""
    if not scale:
        return {}
    th = thresholds(scale)
    return {"basis": {k: scale.get(k) for k in
                      ("thickness", "door_w", "text_h", "bay", "col",
                       "noise", "line_len")},
            "provenance": scale.get("provenance") or {},
            "samples": scale.get("samples"),
            "thresholds": {k: round(v, 1) for k, v in th.items()}}


# ------------------------------------------------------------------ 单位一致性
def plausible_span(scale, span_mm) -> bool:
    """图面跨度是否像一个建筑平面（用于单位自检）。"""
    try:
        s = float(span_mm)
    except (TypeError, ValueError):
        return False
    return GEO.PLAN_SPAN_MM[0] <= s <= GEO.PLAN_SPAN_MM[1]


# ------------------------------------------------------------------ 运行时上下文
# 上面那张 ``STRUCTURE`` 表是**默认值**，在 import 时从环境变量读一次。它的用途
# 是"给一次运行一个起点"，不是"运行时全局旋钮"：并发跑两个区域、或者一次实验
# 要试十几组参数时，改模块级常量会让两次试验互相污染（还会让缓存键失真）。
#
# 所以这里给一层显式的运行时上下文：
#
#   * :func:`k` 是唯一读取倍率的入口，先看当前上下文的覆盖值，再看默认表；
#   * :class:`ScaleContext` 是不可变对象，装在 ``contextvars`` 里，线程/任务隔离；
#   * 不进入上下文时行为与以前**逐字节一致**（``k`` 等同于 ``STRUCTURE[...]``）。
import contextvars as _ctx
from types import MappingProxyType as _MP

#: import 时的默认表快照（只读）：运行时覆盖永远只影响当前上下文
STRUCTURE_DEFAULTS = _MP(dict(STRUCTURE))

_RUNTIME: "_ctx.ContextVar[dict | None]" = _ctx.ContextVar("dwg_scale_runtime",
                                                           default=None)


def k(name: str, default=None):
    """读一个倍率：运行时覆盖 > 默认表 > ``default``。"""
    over = _RUNTIME.get()
    if over and name in over:
        return over[name]
    return STRUCTURE_DEFAULTS.get(name, default)


class ScaleContext:
    """一张图的尺度 + 一组可覆盖倍率（不可变、可审计）。

    ``basis`` 是 ``measure()`` 量出来的基准量（thickness / door_w / noise …）；
    ``overrides`` 只允许覆盖 STRUCTURE 里**已存在**的键，避免运行时偷偷长出新阈值。
    """

    __slots__ = ("basis", "overrides", "digest")

    def __init__(self, basis=None, overrides=None):
        self.basis = _MP(dict(basis or {}))
        clean = {}
        for name, value in dict(overrides or {}).items():
            if name not in STRUCTURE_DEFAULTS:
                raise KeyError(f"未知倍率: {name}（不得在运行时新增阈值）")
            clean[str(name)] = float(value)
        self.overrides = _MP(clean)
        raw = json_dumps([dict(self.basis), dict(self.overrides)])
        self.digest = _sha12(raw)

    # ---- 读
    def get(self, name: str, default=None):
        return self.overrides.get(name, STRUCTURE_DEFAULTS.get(name, default))

    def thresholds(self, keys=None) -> dict:
        """在当前覆盖下算阈值（与 :func:`thresholds` 同一套公式）。"""
        token = _RUNTIME.set(dict(self.overrides))
        try:
            return thresholds(dict(self.basis), keys)
        finally:
            _RUNTIME.reset(token)

    def as_dict(self) -> dict:
        return {"basis": dict(self.basis), "overrides": dict(self.overrides),
                "digest": self.digest}

    def __enter__(self):
        self._token = _RUNTIME.set(dict(self.overrides))
        return self

    def __exit__(self, *exc):
        _RUNTIME.reset(self._token)
        return False


def runtime(context: ScaleContext | None):
    """上下文管理器：在这段代码里让 ``k()`` 用 ``context`` 的覆盖值。"""
    return context if isinstance(context, ScaleContext) else _NullContext()


class _NullContext:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def json_dumps(obj) -> str:
    import json as _json
    return _json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


def _sha12(text: str) -> str:
    import hashlib as _hashlib
    return _hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
