"""受控扰动：验证"泛化"不是靠图层名碰巧对上的。

真实跨图纸泛化很难批量获得（要不同设计院的新图），但**受控扰动**可以低成本
暴露"一改名字就崩"的脆弱点。五种扰动对应五种真实漂移：

================ ==========================================================
扰动              模拟的真实情况
================ ==========================================================
``rename_layers``  另一家设计院的图层命名习惯（WALL -> PZ_WALL）
``explode``        出图时把块炸开（柱/门不再是 INSERT）
``rotate``         图纸整体旋转（不是所有图都摆正）
``scale``          单位变化（mm -> m / cm），或按不同比例插入
``split_lines``    长墙线被洞口、柱、填充切碎成短线段
================ ==========================================================

识别结果应当在扰动后保持几何一致（位置/数量允许小幅变化，不允许塌掉）。
"""
from __future__ import annotations

import math
from pathlib import Path

import ezdxf
from ezdxf.math import Matrix44


# --------------------------------------------------------------------------- 工具
#: 布局块：模型空间/图纸空间本身也是 ``doc.blocks`` 里的一项。
#: 早期版本先走 ``modelspace()`` 再走 ``doc.blocks``，于是模型空间的每个图元都被
#: 变换了**两次**：rotate 转成 180°、scale 变成百万分之一——扰动出来的图是坏的，
#: 后面所有"不变性"结论跟着失真。
_LAYOUT_BLOCKS = ("*Model_Space", "*Paper_Space", "*Paper_Space0")


def _all_entities(doc):
    """模型空间图元 + 块定义内图元（每个图元**只出现一次**）。"""
    for e in doc.modelspace():
        yield e
    for block in doc.blocks:
        if block.name in _LAYOUT_BLOCKS:
            continue
        for e in block:
            yield e


def rename_layers(doc, prefix: str = "PZ_", skip=("0", "Defpoints")) -> dict:
    """所有图层加前缀（模拟另一套图层命名）。"""
    mapping, created = {}, 0
    for layer in list(doc.layers):
        name = layer.dxf.name
        if name in skip:
            continue
        new = f"{prefix}{name}"
        mapping[name] = new
        try:
            layer.dxf.name = new
        except Exception:
            continue
        if not doc.layers.has_entry(new):
            try:
                doc.layers.add(new)
                created += 1
            except Exception:
                pass
    hit = 0
    for e in _all_entities(doc):
        low = str(e.dxf.get("layer", "") or "")
        if low in mapping:
            e.dxf.layer = mapping[low]
            hit += 1
    return {"op": "rename_layers", "layers": len(mapping), "entities": hit,
            "created": created}


def explode_blocks(doc, max_depth: int = 2) -> dict:
    """把模型空间里的块参照炸成图元（模拟"柱/门不再是 INSERT"）。"""
    msp = doc.modelspace()
    done = 0
    for depth in range(max_depth):
        inserts = [e for e in msp.query("INSERT")]
        if not inserts:
            break
        for ins in inserts:
            try:
                children = ins.explode()
            except Exception:
                continue
            if children:
                done += 1
                try:
                    msp.delete_entity(ins)
                except Exception:
                    pass
    return {"op": "explode", "inserts": done}


def _content_bbox(doc):
    """模型空间内容的包围盒（只读，用于扰动前后的自检）。"""
    xs, ys = [], []
    for e in doc.modelspace():
        for key in ("start", "end", "insert", "center"):
            p = e.dxf.get(key) if e.dxf.is_supported(key) else None
            if p is not None:
                try:
                    xs.append(float(p[0]))
                    ys.append(float(p[1]))
                except (TypeError, ValueError, IndexError):
                    pass
    if not xs:
        return None
    return [min(xs), min(ys), max(xs), max(ys)]


def _span(b):
    return None if not b else max(b[2] - b[0], b[3] - b[1])


def transform_geometry(doc, matrix: Matrix44, op: str, **extra) -> dict:
    """对模型空间 + 块定义内图元统一施加仿射变换，**并自检**。

    自检是这次补的：扰动工具本身出错时，不变性检验会得出完全错误的结论——
    之前 ``_all_entities`` 把模型空间遍历了两遍（模型空间同时也是 ``*Model_Space``
    块），rotate 被施加两次变成 180°、scale 变成百万分之一，而报告只显示"偏差
    很大"，看上去像是识别器的毛病。现在把改前/改后的包围盒一起返回，倍数被
    施加了几次一目了然，harness 也能直接断言。

    未变换成功的图元不再静默跳过：``failed`` 会如实计数（静默跳过同样会把
    "工具失手"伪装成"识别退化"）。
    """
    before = _content_bbox(doc)
    ok, failed = 0, []
    for e in _all_entities(doc):
        try:
            e.transform(matrix)
            ok += 1
        except Exception as exc:                     # 记下来，不再静默
            failed.append({"type": e.dxftype(), "error": type(exc).__name__})
    after = _content_bbox(doc)
    s0, s1 = _span(before), _span(after)
    return dict({"op": op, "entities": ok, "failed": len(failed),
                 "failed_sample": failed[:5],
                 "bbox_before": None if not before else [round(v, 3) for v in before],
                 "bbox_after": None if not after else [round(v, 3) for v in after],
                 "span_ratio": (round(s1 / s0, 6) if s0 and s1 else None)}, **extra)


def rotate(doc, degrees: float, origin=(0.0, 0.0)) -> dict:
    m = (Matrix44.z_rotate(math.radians(degrees))
         @ Matrix44.translate(-origin[0], -origin[1], 0.0))
    m = Matrix44.translate(origin[0], origin[1], 0.0) @ m
    return transform_geometry(doc, m, "rotate", degrees=degrees)


def scale(doc, factor: float, insunits=None) -> dict:
    """单位换算（mm -> m 用 0.001），同时更新 $EXTMIN/$EXTMAX。

    ``insunits`` 可显式给（如 6 = 米）。不给就保留原值——**故意**不自动改，因为
    "几何换了单位、$INSUNITS 却还是旧的"正是真实图纸里最常见的坑，识别器必须
    能靠几何证据把单位纠回来，而不是靠 header 自证。
    """
    out = transform_geometry(doc, Matrix44.scale(factor), "scale", factor=factor)
    header = doc.header
    if insunits is not None:
        try:
            header["$INSUNITS"] = int(insunits)
        except Exception:
            pass
    for key in ("$EXTMIN", "$EXTMAX"):
        try:
            v = header.get(key)
            if v is not None:
                header[key] = (v[0] * factor, v[1] * factor, 0.0)
        except Exception:
            continue
    return out


def split_lines(doc, segments: int = 3, gap: float = 2.0,
                min_len: float = 1200.0) -> dict:
    """把长 LINE 切成 ``segments`` 段（段间留 ``gap``）——模拟墙线被切碎。"""
    msp = doc.modelspace()
    split = 0
    for e in list(msp.query("LINE")):
        s, t = e.dxf.start, e.dxf.end
        length = math.hypot(t.x - s.x, t.y - s.y)
        if length < min_len:
            continue
        layer, color = e.dxf.layer, e.dxf.color
        seg_len = max(1.0, (length - gap * (segments - 1)) / segments)
        ux, uy = (t.x - s.x) / length, (t.y - s.y) / length
        try:
            msp.delete_entity(e)
        except Exception:
            continue
        pos = 0.0
        for _i in range(segments):
            a = (s.x + ux * pos, s.y + uy * pos)
            b = (s.x + ux * (pos + seg_len), s.y + uy * (pos + seg_len))
            line = msp.add_line(a, b)
            line.dxf.layer = layer
            try:
                line.dxf.color = color
            except Exception:
                pass
            pos += seg_len + gap
        split += 1
    return {"op": "split_lines", "lines": split, "segments": segments}


OPS = {"rename_layers": rename_layers, "explode": explode_blocks,
       "rotate": rotate, "scale": scale, "split_lines": split_lines}


def apply(src, dst, ops, *, prefix="PZ_", degrees=90.0, factor=0.001,
          segments=3) -> dict:
    """读 ``src`` -> 依次施加 ``ops`` -> 写 ``dst``，返回每步的改动统计。"""
    doc = ezdxf.readfile(str(src))
    applied = []
    for op in ops or ():
        if op == "rename_layers":
            applied.append(rename_layers(doc, prefix=prefix))
        elif op == "explode":
            applied.append(explode_blocks(doc))
        elif op == "rotate":
            applied.append(rotate(doc, degrees))
        elif op == "scale":
            applied.append(scale(doc, factor))
        elif op == "split_lines":
            applied.append(split_lines(doc, segments=segments))
        else:
            raise ValueError(f"未知扰动: {op}")
    out = Path(dst)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(out))
    return {"src": str(src), "dst": str(out), "ops": list(ops), "applied": applied}
