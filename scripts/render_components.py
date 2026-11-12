#!/usr/bin/env python3
"""把识别结果渲染成 PNG（仅用于人工核对，不参与识别）。

底图取自 DXF 原生图元；构件按类别着色叠加：
  墙=红 柱=黑 门=蓝 窗=青 门联窗=紫 房间=绿 轴线=灰 楼梯/台阶=橙 散水=棕

用法：
  python3 render_components.py <图.dxf> <components.json> -o out.png
      [--view all|plan] [--width 4200] [--labels] [--cats 墙,柱]
"""
from __future__ import annotations

import argparse
import json
import math
import os

import ezdxf
from PIL import Image, ImageDraw, ImageFont

FONT_PATHS = ["/System/Library/Fonts/Hiragino Sans GB.ttc",
              "/System/Library/Fonts/PingFang.ttc",
              "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"]

COLORS = {
    "墙": (214, 39, 40), "柱": (25, 25, 25), "门": (31, 119, 180),
    "窗": (0, 170, 200), "门联窗": (148, 103, 189), "房间": (40, 150, 40),
    "轴线": (120, 120, 120), "楼梯": (255, 127, 14), "台阶": (240, 170, 90),
    "散水": (140, 86, 75), "图框": (60, 60, 60),
}
HEAD = 132          # 顶部图例带高度（px）


def font(size):
    for p in FONT_PATHS:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


class View:
    def __init__(self, bbox, width, dy=HEAD):
        self.x0, self.y0, self.x1, self.y1 = bbox
        self.scale = width / (self.x1 - self.x0)
        self.w = int(width)
        self.h = int((self.y1 - self.y0) * self.scale)
        self.dy = dy

    def pt(self, x, y):
        return ((x - self.x0) * self.scale, (self.y1 - y) * self.scale + self.dy)


def draw_base(draw, msp, view, color=(198, 198, 198), width=1):
    def seg(a, b):
        draw.line([view.pt(a[0], a[1]), view.pt(b[0], b[1])], fill=color, width=width)

    def arc(c, r, a0, a1, n=20):
        if a1 < a0:
            a1 += 360
        pts = [(c.x + r * math.cos(math.radians(a0 + (a1 - a0) * k / n)),
                c.y + r * math.sin(math.radians(a0 + (a1 - a0) * k / n))) for k in range(n + 1)]
        for i in range(len(pts) - 1):
            seg(pts[i], pts[i + 1])

    def lwp(e):
        pts = [(p[0], p[1]) for p in e.get_points()]
        for i in range(len(pts) - 1):
            seg(pts[i], pts[i + 1])
        if e.closed and len(pts) > 2:
            seg(pts[-1], pts[0])

    for e in msp:
        t = e.dxftype()
        try:
            if t == "LINE":
                seg((e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y))
            elif t == "LWPOLYLINE":
                lwp(e)
            elif t == "CIRCLE":
                c, r = e.dxf.center, e.dxf.radius
                x, y = view.pt(c.x, c.y)
                rr = r * view.scale
                draw.ellipse([x - rr, y - rr, x + rr, y + rr], outline=color, width=width)
            elif t == "ARC":
                arc(e.dxf.center, e.dxf.radius, e.dxf.start_angle, e.dxf.end_angle)
            elif t == "INSERT":
                for v in e.virtual_entities():
                    vt = v.dxftype()
                    if vt == "LINE":
                        seg((v.dxf.start.x, v.dxf.start.y), (v.dxf.end.x, v.dxf.end.y))
                    elif vt == "LWPOLYLINE":
                        lwp(v)
                    elif vt == "ARC":
                        arc(v.dxf.center, v.dxf.radius, v.dxf.start_angle, v.dxf.end_angle, 16)
        except Exception:
            continue


def overlay(doc, view, draw, labels=False, cats=None, idx=None):
    f = font(21)
    fsmall = font(15)
    for c in doc["components"]:
        cat = c["category"]
        if cats and cat not in cats:
            continue
        col = COLORS.get(cat, (120, 120, 120))
        g = c.get("geometry", {})
        t = g.get("type")
        if cat == "房间" and t == "polygon":
            pts = [view.pt(p[0], p[1]) for p in g["outline"]]
            draw.line(pts + [pts[0]], fill=col, width=3)
            if labels:
                pos = None
                if idx:
                    for se in c.get("source_entities", []):
                        e = idx.get(se.get("handle"))
                        if e is not None and e.dxftype() in ("TEXT", "MTEXT"):
                            pos = (e.dxf.insert.x, e.dxf.insert.y)
                            break
                p = view.pt(*pos) if pos else (sum(q[0] for q in pts) / len(pts),
                                               sum(q[1] for q in pts) / len(pts))
                a = c.get("attributes", {}).get("area_geom_m2")
                draw.text(p, f"{c['name']} {a}㎡" if a else c["name"], font=f, fill=col,
                          anchor="mm", stroke_width=3, stroke_fill=(255, 255, 255))
        elif cat == "柱":
            draw.polygon([view.pt(p[0], p[1]) for p in g["outline"]], fill=col)
        elif cat == "墙":
            pts = [view.pt(p[0], p[1]) for p in g["outline"]]
            draw.polygon(pts, fill=col + (95,), outline=col)
        elif cat in ("门", "窗", "门联窗"):
            seg = g.get("opening") or g.get("axis")
            if seg:
                p1, p2 = view.pt(*seg[0]), view.pt(*seg[1])
                draw.line([p1, p2], fill=col, width=9 if cat != "窗" else 7)
                mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
                draw.ellipse([mx - 4, my - 4, mx + 4, my + 4], fill=col)
            if labels:
                # 名字若还带着内部判据串（"shape:...|n=4"），图上不显示，只留编号
                code = c.get("attributes", {}).get("type_code") or c.get("name") or ""
                if ":" in str(code) or "|" in str(code):
                    code = ""
                if seg:
                    p1, p2 = view.pt(*seg[0]), view.pt(*seg[1])
                    tx, ty = (p1[0] + p2[0]) / 2, min(p1[1], p2[1]) - 14
                else:
                    tx, ty = view.pt(*g.get("insert", [0, 0]))
                draw.text((tx, ty), code, font=fsmall, fill=col, anchor="mb",
                          stroke_width=3, stroke_fill=(255, 255, 255))
        elif cat == "轴线":
            ax = g.get("axis_line")
            if ax:
                draw.line([view.pt(*ax[0]), view.pt(*ax[1])], fill=(210, 210, 210), width=1)
            cx, cy = view.pt(*g["center"])
            r = g["radius"] * view.scale
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=col, width=2)
            if labels:
                draw.text((cx, cy), c.get("attributes", {}).get("axis_label", ""),
                          font=font(16), fill=col, anchor="mm")
        else:
            if g.get("outline"):
                pts = [view.pt(p[0], p[1]) for p in g["outline"]]
                draw.line(pts + [pts[0]], fill=col, width=3)
            elif g.get("bbox"):
                b = g["bbox"]
                draw.rectangle([view.pt(b[0], b[3]), view.pt(b[2], b[1])], outline=col, width=3)


def draw_legend(img, doc, view, cats=None):
    d = ImageDraw.Draw(img)
    counts = {}
    for c in doc["components"]:
        if cats and c["category"] not in cats:
            continue
        counts[c["category"]] = counts.get(c["category"], 0) + 1
    d.rectangle([0, 0, view.w, view.dy - 6], fill=(252, 252, 252))
    d.line([0, view.dy - 6, view.w, view.dy - 6], fill=(180, 180, 180))
    _n = sum(counts.values())
    d.text((24, 12), f"{os.path.basename(doc['meta']['source_file'])}  构件识别结果"
                     f"（图示 {_n} 个实例，无 AI / 无图像识别）",
           font=font(26), fill=(20, 20, 20))
    # 两行横排
    x, y = 24, 58
    for i, (k, v) in enumerate(sorted(counts.items(), key=lambda kv: -kv[1])):
        if i == 6:
            x, y = 24, 92
        col = COLORS.get(k, (120, 120, 120))
        d.rectangle([x, y + 2, x + 20, y + 18], fill=col)
        d.text((x + 28, y), f"{k} {v}", font=font(20), fill=(40, 40, 40))
        x += 190


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dxf")
    ap.add_argument("components")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--view", choices=("all", "plan"), default="plan")
    ap.add_argument("--width", type=int, default=4200)
    ap.add_argument("--labels", action="store_true")
    ap.add_argument("--cats")
    ap.add_argument("--bbox", help="世界坐标 x0,y0,x1,y1，覆盖 --view")
    a = ap.parse_args()

    doc = json.load(open(a.components, encoding="utf-8"))
    d = ezdxf.readfile(a.dxf)
    msp = d.modelspace()
    idx = {e.dxf.handle: e for e in msp.query("TEXT MTEXT")}

    if a.bbox:
        bbox = [float(v) for v in a.bbox.split(",")]
    elif a.view == "all":
        fr = [c for c in doc["components"] if c["category"] == "图框" and c["id"].endswith("01")]
        if fr:
            bbox = fr[0]["geometry"]["outer"]
        else:
            # 这张图没认出图框（单图层/无图框的图），退回"全部构件的包络"，
            # 否则 --view all 直接 IndexError，图根本出不来。
            bs = [c["bbox_model"] for c in doc["components"] if c.get("bbox_model")]
            if not bs:
                raise SystemExit("没有任何构件，无法确定出图范围")
            m = 2000
            bbox = [min(b[0] for b in bs) - m, min(b[1] for b in bs) - m,
                    max(b[2] for b in bs) + m, max(b[3] for b in bs) + m]
    else:
        bs = [c["bbox_model"] for c in doc["components"] if c["category"] in ("墙", "柱")]
        m = 2200
        bbox = [min(b[0] for b in bs) - m, min(b[1] for b in bs) - m,
                max(b[2] for b in bs) + m, max(b[3] for b in bs) + m]

    view = View(bbox, a.width)
    base = Image.new("RGB", (view.w, view.h + view.dy), "white")
    draw_base(ImageDraw.Draw(base), msp, view)

    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    overlay(doc, view, ImageDraw.Draw(layer), labels=a.labels,
            cats=set(a.cats.split(",")) if a.cats else None, idx=idx)
    img = Image.alpha_composite(base.convert("RGBA"), layer).convert("RGB")
    draw_legend(img, doc, view, set(a.cats.split(",")) if a.cats else None)
    img.save(a.out, optimize=True)
    print(f"{a.out}  {img.width}×{img.height}")


if __name__ == "__main__":
    main()
