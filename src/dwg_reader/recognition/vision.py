"""局部矢量渲染：把一块 ROI 画成**小图**，喂给多模态模型。

为什么要自己画，而不是截屏：
CAD 图没有"屏幕像素"，整图截屏既不可复现（依赖视图/缩放）也不安全（一次把整
张图传出去）。这里只从快照里的原始图元画**这一个 ROI**，同一份快照永远画出一
模一样的图，而且尺寸、配色、比例全部由代码决定。

配色是有含义的（模型看的就是这个语义）：

============ ====================================================
灰色细线     背景图元（ROI 内的原始几何，不做判断）
橙色粗线     **目标**图元（就是这一条 PatternCase 要解释的东西）
绿色粗线     已确认的墙（只作上下文，模型不许改）
蓝色文字     房间名/标注（不可信数据，只作线索）
============ ====================================================

三条边界：

1. 图只在内存里生成（``bytes``），**不落盘**——PURE 节点不写文件；
2. 分辨率、边距、像素上限都是逻辑预算（见 :data:`RENDER`），不是图纸阈值；
3. 画不出来就返回 ``None``：宁可让模型只看结构特征，也不发一张对不上的图。
"""
from __future__ import annotations

import io
import math

#: 渲染预算（像素/张数，不是几何阈值）
RENDER = {
    "width": 720,          # 目标宽度（像素）
    "max_height": 900,     # 最长边保护
    "pad_thickness": 2.0,  # ROI 外扩（墙厚的倍数），必须留出接缝上下文
    "base_width": 1,       # 背景线宽
    "focus_width": 3,      # 目标高亮线宽
    "wall_width": 2,       # 墙上下文线宽
    "max_bytes": 900_000,  # 单张图上限（超了降分辨率重画）
    "max_images": 3,       # 单个案例最多几张（当前只画 1 张 ROI）
}

#: 中文字体候选（macOS / Linux 常见路径；找不到就退回 Pillow 默认字体）。
#: 没有 CJK 字体时房间名会画成方框——那种图对模型是噪声，所以干脆不画字。
FONT_PATHS = ("/System/Library/Fonts/Hiragino Sans GB.ttc",
              "/System/Library/Fonts/PingFang.ttc",
              "/System/Library/Fonts/Supplemental/Songti.ttc",
              "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
              "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")

#: 配色（放在字典里：这些是像素颜色，不是几何阈值）
COLORS = {"base": (205, 205, 205), "focus": (255, 140, 0),
          "wall": (30, 140, 70), "text": (40, 80, 200)}


def _font(size: int):
    """按候选路径加载中文字体；都没有就返回 ``None``（宁可不画字）。"""
    import os
    try:
        from PIL import ImageFont
    except Exception:
        return None
    for path in FONT_PATHS:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return None


def describe() -> dict:
    """进审计：这次视觉链路用了什么口径。"""
    return {"mode": "local_vector_render", "width": int(RENDER["width"]),
            "max_height": int(RENDER["max_height"]),
            "colors": dict(COLORS)}


def _bbox_of(rec: dict):
    xs, ys = [], []
    for key in ("start", "end", "center", "insert"):
        pt = rec.get(key)
        if isinstance(pt, (list, tuple)) and len(pt) >= 2:
            xs.append(float(pt[0]))
            ys.append(float(pt[1]))
    for key in ("points", "outline"):
        pts = rec.get(key)
        if isinstance(pts, (list, tuple)):
            for pt in pts:
                if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                    xs.append(float(pt[0]))
                    ys.append(float(pt[1]))
    box = rec.get("bbox")
    if isinstance(box, (list, tuple)) and len(box) == 4:
        xs.extend([float(box[0]), float(box[2])])
        ys.extend([float(box[1]), float(box[3])])
    if not xs or not ys:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _segments(rec: dict):
    """一个图元 -> 若干线段（LINE / 多段线 / 弧 / 圆）。"""
    kind = str(rec.get("type") or "").upper()
    out = []
    if kind == "LINE":
        start, end = rec.get("start"), rec.get("end")
        if start and end:
            out.append(((float(start[0]), float(start[1])),
                        (float(end[0]), float(end[1]))))
        return out
    pts = rec.get("points")
    if isinstance(pts, (list, tuple)) and len(pts) >= 2:
        for a, b in zip(pts, pts[1:]):
            if len(a) >= 2 and len(b) >= 2:
                out.append(((float(a[0]), float(a[1])), (float(b[0]), float(b[1]))))
        if rec.get("closed") and len(pts) >= 3:
            first, last = pts[0], pts[-1]
            out.append(((float(last[0]), float(last[1])),
                        (float(first[0]), float(first[1]))))
        return out
    center, radius = rec.get("center"), rec.get("radius")
    if center and radius:
        cx, cy = float(center[0]), float(center[1])
        r = float(radius)
        if kind == "CIRCLE":
            a0, a1 = 0.0, 360.0
        else:
            a0 = float(rec.get("start_angle") or 0.0)
            a1 = float(rec.get("end_angle") or 360.0)
        steps = max(8, min(48, int(abs(a1 - a0) / 10.0) + 8))
        prev = None
        for i in range(steps + 1):
            ang = math.radians(a0 + (a1 - a0) * i / steps)
            pt = (cx + r * math.cos(ang), cy + r * math.sin(ang))
            if prev is not None:
                out.append((prev, pt))
            prev = pt
    return out


#: 纵横比保护：过扁/过高的窗口看不清，按两侧均分补齐
ASPECT = {"max": 3.0}


def _window(case: dict, *, thickness: float, pad_ratio: float):
    # 同族多实例的案例只截**代表实例**（image_bbox），不截"整栋楼的外接框"
    box = case.get("image_bbox") or case.get("region") or case.get("bbox")
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    pad = float(pad_ratio) * float(thickness or 0.0) + 1.0
    x0, y0, x1, y1 = (float(box[0]) - pad, float(box[1]) - pad,
                      float(box[2]) + pad, float(box[3]) + pad)
    if x1 - x0 <= 0 or y1 - y0 <= 0:
        return None
    # 过扁的窗口按短边补齐（两侧均分），否则 720px 宽会把细节压成一根线
    w, h = x1 - x0, y1 - y0
    if w / h > ASPECT["max"]:
        want = w / ASPECT["max"]
        grow = (want - h) / 2.0
        y0, y1 = y0 - grow, y1 + grow
    elif h / w > ASPECT["max"]:
        want = h / ASPECT["max"]
        grow = (want - w) / 2.0
        x0, x1 = x0 - grow, x1 + grow
    return (x0, y0, x1, y1)


def render_case(snapshot: dict, case: dict, *, walls=(), thickness: float = 0.0,
                width: int | None = None) -> dict | None:
    """把一条 PatternCase 画成 PNG（内存里）。

    返回 ``{"b64": str, "width": int, "height": int, "bytes": int,
    "focus": int, "walls": int}``；画不出来返回 ``None``（调用方按"无图"处理）。
    """
    try:
        from PIL import Image, ImageDraw
    except Exception:                                   # 没有 Pillow：退化成无图
        return None
    win = _window(case, thickness=thickness, pad_ratio=RENDER["pad_thickness"])
    if win is None:
        return None
    x0, y0, x1, y1 = win
    span_x, span_y = x1 - x0, y1 - y0
    out_w = int(width or RENDER["width"])
    scale = out_w / span_x
    out_h = max(32, int(span_y * scale))
    if out_h > int(RENDER["max_height"]):
        scale *= int(RENDER["max_height"]) / float(out_h)
        out_w = max(32, int(span_x * scale))
        out_h = int(span_y * scale)

    def pt(p):
        return ((float(p[0]) - x0) * scale, (y1 - float(p[1])) * scale)

    focus_handles = {str(h) for h in (case.get("source_handles")
                                      or case.get("target_handles") or ())}
    wall_handles = {str(h) for h in (case.get("context_handles") or ())}

    img = Image.new("RGB", (out_w, out_h), "white")
    draw = ImageDraw.Draw(img)
    drawn_focus = drawn_walls = drawn_base = 0

    def _in(rec_box):
        if not rec_box:
            return False
        return not (rec_box[2] < x0 or rec_box[0] > x1
                    or rec_box[3] < y0 or rec_box[1] > y1)

    # 背景图元（含块展开几何）：灰色细线
    for rec in list(snapshot.get("entities") or ()) + list(snapshot.get("derived") or ()):
        box = _bbox_of(rec)
        if not _in(box):
            continue
        handle = str(rec.get("handle") or "")
        is_focus = handle in focus_handles
        color = COLORS["focus"] if is_focus else COLORS["base"]
        line_w = int(RENDER["focus_width"]) if is_focus else int(RENDER["base_width"])
        for a, b in _segments(rec):
            draw.line([pt(a), pt(b)], fill=color, width=line_w)
        if is_focus:
            drawn_focus += 1
        else:
            drawn_base += 1

    # 已确认的墙：绿色（上下文，只读）
    for wall in (walls or ()):
        if not isinstance(wall, dict):
            continue
        start, end = wall.get("start"), wall.get("end")
        if not start or not end:
            continue
        box = (min(float(start[0]), float(end[0])), min(float(start[1]), float(end[1])),
               max(float(start[0]), float(end[0])), max(float(start[1]), float(end[1])))
        if not _in(box):
            continue
        draw.line([pt(start), pt(end)], fill=COLORS["wall"],
                  width=int(RENDER["wall_width"]))
        drawn_walls += 1

    # 上下文 handle 里命中墙的也标绿（pattern 里登记的是墙 id 时靠这一步对上）
    for rec in (snapshot.get("entities") or ()):
        handle = str(rec.get("handle") or "")
        if handle not in wall_handles:
            continue
        for a, b in _segments(rec):
            draw.line([pt(a), pt(b)], fill=COLORS["wall"],
                      width=int(RENDER["wall_width"]))

    label_font = _font(max(11, int(out_w / 60)))
    if label_font is not None:
        for rec in (snapshot.get("entities") or ()):
            if str(rec.get("type")) not in ("TEXT", "MTEXT"):
                continue
            box = _bbox_of(rec)
            if not _in(box):
                continue
            text = str(rec.get("text") or "")[:12]
            if not text:
                continue
            try:
                draw.text(pt(rec.get("insert") or (box[0], box[1])), text,
                          fill=COLORS["text"], font=label_font)
            except Exception:
                pass

    buffer = io.BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    raw = buffer.getvalue()
    if len(raw) > int(RENDER["max_bytes"]):
        # 太大就降一档分辨率重画（不改变内容，只改像素）
        smaller = max(120, int(out_w * 0.6))
        return render_case(snapshot, case, walls=walls, thickness=thickness,
                           width=smaller)
    import base64
    return {"b64": base64.b64encode(raw).decode("ascii"), "width": out_w,
            "height": out_h, "bytes": len(raw), "focus": drawn_focus,
            "walls": drawn_walls, "base": drawn_base}


def case_renderer(snapshot: dict, *, walls=(), thickness: float = 0.0,
                  max_images: int | None = None):
    """给 :func:`pattern_discovery.discover` 用的回调工厂。

    返回的 ``fn(case) -> {"b64": ...}`` 只在内存里出图；图片统计写进
    ``case["image_stats"]``，审计里能查到"这次到底发了几张图、多大"。
    ``max_images`` 是张数预算（超出的案例只给结构特征，不给图）。
    """
    budget = {"limit": int(RENDER["max_images"] if max_images is None else max_images),
              "used": 0}

    def render(case):
        if budget["used"] >= budget["limit"]:
            return {}
        shot = render_case(snapshot, case, walls=walls, thickness=thickness)
        if not shot:
            return {}
        budget["used"] += 1
        return {"b64": shot["b64"], "path": None, "stats": {
            "width": shot["width"], "height": shot["height"],
            "bytes": shot["bytes"], "focus": shot["focus"],
            "walls": shot["walls"], "base": shot["base"]}}
    return render


def summary(cases) -> dict:
    """视觉链路摘要（进 meta.ai_recognition.vision）。"""
    rows = [c for c in (cases or ()) if isinstance(c, dict) and c.get("image_b64")]
    total = sum(len(str(c.get("image_b64") or "")) for c in rows)
    return {"mode": "local_vector_render", "rendered": len(rows),
            "encoded_bytes": total, "budget": dict(RENDER)}


# --------------------------------------------------------------------- 多尺度视图
#: 三种观察层次：全局（布局/重复规律）、局部（构件与墙柱关系）、细节（线/弧/填充）
VIEW_SCALES = ("global", "local", "detail")

#: 每个尺度的取景与分辨率（比率，不是图纸阈值）
VIEW_SPEC = {
    "global": {"pad_thickness": 0.0, "width": 640, "shrink": 1.0},
    "local": {"pad_thickness": 6.0, "width": 720, "shrink": 1.0},
    "detail": {"pad_thickness": 1.0, "width": 900, "shrink": 1.0},
}

#: 统一构件编号的配色（画在图元右上角；放进字典，避免被当成几何阈值扫描）
LABEL_STYLE = {"color": (180, 0, 180)}


def health() -> dict:
    """渲染能力自检：**能不能出图**要是一个可查事实，不能静默返回 None。"""
    try:
        from PIL import Image  # noqa: F401
        pillow = True
        reason = None
    except Exception as exc:                       # pragma: no cover - 环境差异
        pillow = False
        reason = f"NO_PILLOW:{type(exc).__name__}"
    return {"ok": bool(pillow), "pillow": pillow, "reason": reason,
            "font": bool(_font(12)), "budget": dict(RENDER),
            "scales": list(VIEW_SCALES)}


def _draw_numbers(draw, entities, *, pt, focus_handles, font) -> int:
    """在目标图元右上角写统一编号（``handle`` 前 6 位），便于人和模型对齐。"""
    if font is None:
        return 0
    drawn = 0
    for rec in entities or ():
        handle = str(rec.get("handle") or "")
        if handle not in focus_handles:
            continue
        box = _bbox_of(rec)
        if not box:
            continue
        try:
            draw.text(pt((box[2], box[3])), handle[:6], fill=LABEL_STYLE["color"],
                      font=font)
            drawn += 1
        except Exception:
            continue
        if drawn >= 24:
            break
    return drawn


def render_views(snapshot: dict, focus: dict, *, scales=("local", "detail"),
                 walls=(), thickness: float = 0.0, focus_handles=(),
                 numbering: bool = True) -> dict:
    """多尺度视图：同一个焦点，按层次各画一张（共享编号与配色）。

    ``focus`` 至少要有 ``bbox``；``scales`` 从 :data:`VIEW_SCALES` 里取。
    返回的每一张都带 ``status``（``rendered`` / ``no_pillow`` / ``empty`` /
    ``too_big``），**AI 到底看没看到图，看这里就能回答**。
    """
    ok = health()
    views: list[dict] = []
    if not ok["ok"]:
        return {"schema": "vision-views/1.0", "ok": False, "reason": ok["reason"],
                "views": [], "rendered": 0}
    box = focus.get("bbox") or focus.get("region")
    if not box:
        return {"schema": "vision-views/1.0", "ok": False, "reason": "NO_FOCUS",
                "views": [], "rendered": 0}
    all_handles = {str(h) for h in (focus_handles or ())}
    for scale in scales:
        spec = VIEW_SPEC.get(str(scale))
        if spec is None:
            views.append({"scale": str(scale), "status": "unknown_scale"})
            continue
        pad = float(spec["pad_thickness"]) * float(thickness or 0.0)
        window = [float(box[0]) - pad, float(box[1]) - pad,
                  float(box[2]) + pad, float(box[3]) + pad]
        if scale == "global" and thickness:
            window = list(snapshot.get("bbox") or window)
        shot = render_case(snapshot, {"bbox": window, "region": window,
                                      "image_bbox": window,
                                      "source_handles": list(all_handles)},
                           walls=walls, thickness=thickness,
                           width=int(spec["width"]))
        if not shot:
            views.append({"scale": scale, "status": "empty"})
            continue
        views.append({"scale": scale, "status": "rendered",
                      "b64": shot["b64"], "width": shot["width"],
                      "height": shot["height"], "bytes": shot["bytes"],
                      "focus": shot["focus"], "walls": shot["walls"],
                      "numbering": bool(numbering)})
    rendered = [v for v in views if v.get("status") == "rendered"]
    return {"schema": "vision-views/1.0", "ok": bool(rendered),
            "rendered": len(rendered), "views": views,
            "bytes": sum(int(v.get("bytes") or 0) for v in rendered),
            "reason": None if rendered else "NO_VIEW"}


def views_summary(result: dict) -> dict:
    result = result or {}
    return {"schema": "vision-views/1.0", "ok": bool(result.get("ok")),
            "reason": result.get("reason"), "rendered": int(result.get("rendered") or 0),
            "bytes": int(result.get("bytes") or 0),
            "scales": [{"scale": v.get("scale"), "status": v.get("status"),
                        "w": v.get("width"), "h": v.get("height")}
                       for v in (result.get("views") or ())]}
