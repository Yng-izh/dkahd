"""DrawingSnapshot：把 DXF 模型空间一次性提取成纯 Python（JSON-native）快照。

识别节点（``texts`` … ``rooms``）只读快照，不跨线程共享 live ezdxf 对象；
只有 ``load`` / ``assemble`` / ``export`` 三个 MAIN 节点访问 ``doc/msp``。

快照有三部分几何来源，全部带 ``handle``，下游原语层一视同仁：

* ``entities``：模型空间直接图元；
* ``derived``：块参照**展开后**的几何（``via`` 记录父块参照 handle 与嵌套深度），
  使"墙画在块里 / 柱画在块里 / 图元被炸开成块"不再依赖炸开操作；
* ``blocks``：门块定义的 ARC/LINE 参数（门扇几何判据用）。

字段清单见 ``work/packaging/requirements.md`` 附录 B。未知实体类型只保留
``handle/type/layer``，识别器按类型忽略，``unrecognized`` 仍走 live msp。
"""
from __future__ import annotations

from typing import Any, Iterable

#: 块展开时不进入的块名关键字（门窗块已由块参照本身代表，展开只会重复计数）
NON_EXPAND_BLOCK_HINTS = ("DOOR", "WIN", "WINDOW", "GATE", "LVTRY", "$TCHSYS$", "AXISO")
#: **纯标注类**块：尺寸标注、标高、索引符号。它们展开出来的只有标注线、箭头、
#: 引出线和"数字"，而标注线与墙线长得一模一样——一旦进了候选池就是假墙／假窗。
#: 实测艺术楼有 568 个 ``_DIMX`` / ``*D`` 尺寸块，展开后塞满了 ``derived`` 名额，
#: 真实几何反而被 ``MAX_DERIVED`` 截断（截断是静默的，等于悄悄漏识别）。
NON_EXPAND_ANNOTATION_HINTS = ("_DIM", "DIMX", "DIMENSION", "ELEV", "INDEX",
                               "SYMBOL_TAG")
#: 展开深度上限（块套块）
MAX_EXPAND_DEPTH = 3
#: 展开产生的记录上限（防止极端图纸把快照撑爆）
MAX_DERIVED = 20_000


def _xy(value: Any) -> list[float]:
    return [float(value[0]), float(value[1])]


def _entity_record(entity: Any) -> dict:
    kind = entity.dxftype()
    rec: dict[str, Any] = {
        "handle": entity.dxf.handle,
        "type": kind,
        "layer": entity.dxf.layer,
    }
    if kind == "LINE":
        rec["start"] = _xy(entity.dxf.start)
        rec["end"] = _xy(entity.dxf.end)
    elif kind == "LWPOLYLINE":
        pts = list(entity.get_points("xyseb"))
        rec["points"] = [[float(p[0]), float(p[1])] for p in pts]
        rec["bulge"] = [float(p[4]) for p in pts]
        rec["has_bulge"] = any(abs(p[4]) > 1e-9 for p in pts)
        rec["closed"] = bool(entity.closed)
    elif kind == "POLYLINE":
        verts = list(entity.vertices)
        rec["points"] = [[float(v.dxf.location[0]), float(v.dxf.location[1])]
                         for v in verts]
        rec["bulge"] = [float(getattr(v.dxf, "bulge", 0.0) or 0.0) for v in verts]
        rec["has_bulge"] = any(abs(b) > 1e-9 for b in rec["bulge"])
        rec["closed"] = bool(entity.is_closed)
    elif kind == "ARC":
        rec["center"] = _xy(entity.dxf.center)
        rec["radius"] = float(entity.dxf.radius)
        rec["start_angle"] = float(entity.dxf.start_angle)
        rec["end_angle"] = float(entity.dxf.end_angle)
    elif kind == "CIRCLE":
        rec["center"] = _xy(entity.dxf.center)
        rec["radius"] = float(entity.dxf.radius)
    elif kind == "ELLIPSE":
        rec["center"] = _xy(entity.dxf.center)
        rec["major_axis"] = _xy(entity.dxf.major_axis)
        rec["ratio"] = float(entity.dxf.ratio)
    elif kind in ("TEXT", "MTEXT"):
        if kind == "TEXT":
            rec["text"] = entity.dxf.text
            rec["height"] = float(entity.dxf.height)
        else:
            rec["text"] = entity.text
            rec["height"] = float(entity.dxf.char_height)
        rec["insert"] = _xy(entity.dxf.insert)
        try:
            rec["rotation"] = float(entity.dxf.rotation)
        except Exception:
            rec["rotation"] = 0.0
    elif kind == "HATCH":
        # 填充边界 = 画出来的实体轮廓（构造柱/实心墙/柱墩），保留外环+内环与来源
        loops = []
        try:
            for path in entity.paths:
                try:
                    if hasattr(path, "vertices"):
                        pts = list(path.vertices)
                    else:
                        pts = list(path.flattening(1.0))
                except Exception:
                    continue
                loop = [[float(p[0]), float(p[1])] for p in pts]
                if len(loop) >= 3:
                    loops.append(loop)
        except Exception:
            loops = []
        rec["loops"] = loops
        rec["solid"] = bool(getattr(entity.dxf, "solid_fill", False))
    elif kind == "INSERT":
        rec["name"] = entity.dxf.name
        rec["insert"] = _xy(entity.dxf.insert)
        rec["rotation"] = float(entity.dxf.rotation)
        rec["xscale"] = float(entity.dxf.xscale)
        rec["yscale"] = float(entity.dxf.yscale)
        rec["attribs"] = {a.dxf.tag: a.dxf.text for a in entity.attribs}
        try:
            rec["matrix"] = [float(v) for v in entity.matrix44()]
        except Exception:
            rec["matrix"] = None
    return rec


#: 块定义展开的嵌套层数（子块、孙块）
BLOCK_NEST_DEPTH = 2
#: 单个块定义最多收录多少条几何（病态块的保护上限）
MAX_BLOCK_ENTS = 4000


def _poly_closed(entity: Any, kind: str) -> bool:
    """多段线是否闭合（LWPOLYLINE 用 .closed，POLYLINE 用 .is_closed）。"""
    try:
        return bool(entity.is_closed) if kind == "POLYLINE" else bool(entity.closed)
    except Exception:
        return False


def _poly_points(entity: Any, kind: str) -> list[tuple[float, float]]:
    """多段线顶点（LWPOLYLINE 走 get_points，POLYLINE 走 vertices）。"""
    try:
        if kind == "POLYLINE":
            return [(float(v.dxf.location.x), float(v.dxf.location.y))
                    for v in entity.vertices]
        return [(float(p[0]), float(p[1])) for p in entity.get_points("xy")]
    except Exception:
        return []


def _block_definition(block: Any) -> dict:
    """块定义几何：LINE / ARC / LWPOLYLINE / POLYLINE，**含嵌套块（最多两层）**。

    历史版本只收 ARC / LINE，于是"内容画在子块里"的符号（例如结构柱块
    ``M_S0BKA``）签名是空的——实测 50 根柱只认出 7 根，就是因为块长相
    ``lines: 0``、长相分类一律 None。这里补三件事：

    * 多段线按边拆成线段，闭合的另存一份 ``loops``（柱/洞口轮廓要用）；
    * 嵌套 INSERT 用 ``virtual_entities()`` 就地展开（坐标已在块坐标系里）；
    * 展开总量受 ``MAX_BLOCK_ENTS`` 限制，避免病态块拖垮快照。
    """
    arcs: list[list[float]] = []
    lines: list[list[float]] = []
    loops: list[list[list[float]]] = []
    solids: list[list[list[float]]] = []
    attdefs = [0]
    budget = [0]

    def _add(entity: Any) -> None:
        if budget[0] >= MAX_BLOCK_ENTS:
            return
        kind = entity.dxftype()
        if kind == "ARC":
            center = entity.dxf.center
            arcs.append([float(center.x), float(center.y), float(entity.dxf.radius),
                         float(entity.dxf.start_angle), float(entity.dxf.end_angle)])
            budget[0] += 1
        elif kind == "LINE":
            start, end = entity.dxf.start, entity.dxf.end
            lines.append([float(start.x), float(start.y),
                          float(end.x), float(end.y)])
            budget[0] += 1
        elif kind in ("SOLID", "TRACE"):
            # 实心填充的四边形/三角形：天正、探索者一类软件的柱就是这样画的
            # （``M_S0BKA`` = 单位方块的 SOLID，靠插入缩放变成 400×400 的柱截面）。
            # 以前这里不收 SOLID，柱块的"长相"是空的，于是被判成"不认识"。
            pts = []
            for attr in ("vtx0", "vtx1", "vtx2", "vtx3"):
                if entity.dxf.hasattr(attr):
                    v = entity.dxf.get(attr)
                    pts.append([float(v.x), float(v.y)])
            if len(pts) >= 3:
                if pts[2] == pts[3]:          # 三角形：第 3、4 点重合
                    pts = pts[:3]
                solids.append(pts)
                budget[0] += 1
        elif kind == "ATTDEF":
            attdefs[0] += 1
        elif kind in ("LWPOLYLINE", "POLYLINE"):
            pts = _poly_points(entity, kind)
            if len(pts) < 2:
                return
            closed = _poly_closed(entity, kind)
            seq = pts + pts[:1] if closed else pts
            for (x1, y1), (x2, y2) in zip(seq, seq[1:]):
                lines.append([x1, y1, x2, y2])
                budget[0] += 1
            if closed:
                loops.append([[float(x), float(y)] for x, y in pts])
                budget[0] += 1

    entities = list(block)
    for entity in entities:
        _add(entity)

    # 嵌套块：逐层用 virtual_entities() 展开（返回的实体已带子块变换）
    frontier = [e for e in entities if e.dxftype() == "INSERT"]
    for _depth in range(BLOCK_NEST_DEPTH):
        if not frontier or budget[0] >= MAX_BLOCK_ENTS:
            break
        next_frontier = []
        for insert in frontier:
            try:
                children = list(insert.virtual_entities())
            except Exception:
                continue
            for child in children:
                if child.dxftype() == "INSERT":
                    next_frontier.append(child)
                else:
                    _add(child)
        frontier = next_frontier

    # 块自身的尺寸（块坐标系，未乘插入缩放）：柱块要靠它换算真实截面
    xs: list[float] = []
    ys: list[float] = []
    for x1, y1, x2, y2 in lines:
        xs += [x1, x2]
        ys += [y1, y2]
    for cx, cy, r, _a0, _a1 in arcs:
        xs += [cx - r, cx + r]
        ys += [cy - r, cy + r]
    for poly in loops + solids:
        xs += [float(p[0]) for p in poly]
        ys += [float(p[1]) for p in poly]
    extent = ([round(max(xs) - min(xs), 3), round(max(ys) - min(ys), 3)]
              if xs and ys else None)
    return {"arcs": arcs, "lines": lines, "loops": loops, "solids": solids,
            "attdefs": attdefs[0], "extent": extent,
            "nest_depth": BLOCK_NEST_DEPTH}


def _partition_rects(msp: Any) -> list[list[list[float]]]:
    """LVTRY 图层隔断块：``bbox.extents(virtual_entities)`` 的四个角点。"""
    rects: list[list[list[float]]] = []
    for entity in msp.query("INSERT"):
        if entity.dxf.layer != "LVTRY":
            continue
        try:
            from ezdxf import bbox as _bbox
            box = _bbox.extents(list(entity.virtual_entities()))
            if box.has_data:
                rects.append([
                    [float(box.extmin.x), float(box.extmin.y)],
                    [float(box.extmax.x), float(box.extmin.y)],
                    [float(box.extmax.x), float(box.extmax.y)],
                    [float(box.extmin.x), float(box.extmax.y)],
                ])
        except Exception:
            continue
    return rects


def _expandable(insert: Any, skip_layers: frozenset, skip_blocks: frozenset) -> bool:
    """块参照是否值得展开：不展开门窗块 / 轴号块 / 标注块 / 显式排除的图层。

    标注块（尺寸、标高、索引号）是**噪声源**而不是构件：它们的标注线、箭头、
    引出线展开后与墙线同形，落进墙/窗候选池就是假墙假窗；而且数量极大（艺术楼
    568 个），会把 ``MAX_DERIVED`` 名额吃光，让真实几何被静默截断。
    """
    if insert.dxf.layer in skip_layers:
        return False
    name = str(insert.dxf.name)
    if name in skip_blocks:
        return False
    upper = name.upper()
    # AutoCAD 匿名尺寸块统一是 *D 开头
    if upper.startswith("*D"):
        return False
    if any(hint in upper for hint in NON_EXPAND_BLOCK_HINTS):
        return False
    return not any(hint in upper for hint in NON_EXPAND_ANNOTATION_HINTS)


def _expand_insert(entity: Any, out: list, *, skip_layers, skip_blocks,
                   limit: int, depth: int = 0) -> None:
    """把一个块参照的几何展开成 ``derived`` 记录（含嵌套块与 0 图层继承）。"""
    if depth >= MAX_EXPAND_DEPTH or len(out) >= limit:
        return
    parent = entity.dxf.handle
    parent_layer = entity.dxf.layer
    try:
        children = list(entity.virtual_entities())
    except Exception:
        return
    for child in children:
        if len(out) >= limit:
            return
        kind = child.dxftype()
        if kind == "INSERT":
            if _expandable(child, skip_layers, skip_blocks):
                _expand_insert(child, out, skip_layers=skip_layers,
                               skip_blocks=skip_blocks, limit=limit, depth=depth + 1)
            continue
        if kind not in ("LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "ELLIPSE"):
            continue
        try:
            rec = _entity_record(child)
        except Exception:
            continue
        own_layer = rec.get("layer") or "0"
        # AutoCAD 规则：块内 0 图层图元继承块参照所在图层
        rec["own_layer"] = own_layer
        rec["layer"] = parent_layer if own_layer in ("0", "Defpoints") else own_layer
        rec["handle"] = f"{parent}/{rec.get('handle')}"
        rec["derived"] = True
        rec["via"] = {"insert": parent, "layer": parent_layer, "depth": depth}
        out.append(rec)


def _insert_priority(insert: Any, priority_layers: frozenset) -> tuple:
    """块展开顺序：结构层上的块优先，其余按 handle 稳定排序。

    展开上限是硬预算，顺序决定"谁被静默丢掉"。以前按 msp 遍历顺序展开，
    结构层上的墙/柱块可能排在家具块之后而被截断——漏识别从此处产生，
    而且没有任何报告。现在至少保证结构层块一定先展开，并把截断写成显式统计。
    """
    layer = str(insert.dxf.layer or "")
    try:
        handle = str(insert.dxf.handle or "")
    except Exception:
        handle = ""
    return (0 if layer in priority_layers else 1, layer, handle)


def build_snapshot(doc: Any, msp: Any, dxf_path: str | None = None,
                   block_names: Iterable[str] = (), *, expand: bool = True,
                   expand_skip_layers: Iterable[str] = (),
                   expand_skip_blocks: Iterable[str] = (),
                   max_derived: int = MAX_DERIVED,
                   priority_layers: Iterable[str] = ()) -> dict:
    """构建 JSON-native 快照。

    ``block_names`` 是需要保留块定义的块名（识别层传门块名）。``load`` 节点在
    MAIN 线程调用本函数，之后所有 PURE 节点只读返回值。快照不记录文件路径，
    只由文件内容决定，保证内容寻址缓存可在同一文件的不同路径间复用。

    ``expand``：把块参照的几何展开进 ``derived``，实现"墙/柱画在块里也能识别"，
    不要求用户先炸开图纸。
    """
    entities = [_entity_record(entity) for entity in msp]

    derived: list[dict] = []
    derived_stats: dict[str, Any] = {"limit": max_derived, "expanded": 0,
                                     "inserts": 0, "skipped_inserts": 0,
                                     "truncated": False, "skipped_by_layer": {}}
    if expand:
        skip_layers = frozenset(expand_skip_layers)
        skip_blocks = frozenset(expand_skip_blocks)
        prio = frozenset(priority_layers)
        try:
            inserts = list(msp.query("INSERT"))
        except Exception:
            inserts = []
        inserts.sort(key=lambda ins: _insert_priority(ins, prio))
        derived_stats["inserts"] = len(inserts)
        for insert in inserts:
            if len(derived) >= max_derived:
                derived_stats["truncated"] = True
                layer = str(getattr(insert.dxf, "layer", "") or "")
                skipped = derived_stats["skipped_by_layer"]
                skipped[layer] = skipped.get(layer, 0) + 1
                derived_stats["skipped_inserts"] += 1
                continue
            if not _expandable(insert, skip_layers, skip_blocks):
                continue
            before = len(derived)
            _expand_insert(insert, derived, skip_layers=skip_layers,
                           skip_blocks=skip_blocks, limit=max_derived)
            if len(derived) >= max_derived and len(derived) == before and before:
                # 预算在本次展开中途用尽：记一次截断，后面的块继续被计入 skipped
                derived_stats["truncated"] = True
        derived_stats["expanded"] = len(derived)

    header = doc.header
    extmin, extmax = header.get("$EXTMIN"), header.get("$EXTMAX")
    snapshot: dict[str, Any] = {
        "header": {
            "dxfversion": doc.dxfversion,
            "insunits": header.get("$INSUNITS"),
            "measurement": header.get("$MEASUREMENT"),
            "extmin": _xy(extmin) if extmin is not None else None,
            "extmax": _xy(extmax) if extmax is not None else None,
        },
        "entities": entities,
        "derived": derived,
        "blocks": {},
        "partitions": _partition_rects(msp),
        "derived_stats": derived_stats,
    }
    # 块定义不只服务于门块白名单：按长相判类别需要所有被引用块的几何，
    # 否则同族的变体块（$DorLib2D$00000131 之类）无从继承族语义。
    wanted = set(block_names or ())
    wanted.update(e.get("name") for e in entities
                  if e.get("type") == "INSERT" and e.get("name"))
    for name in sorted(wanted):
        try:
            block = doc.blocks[name]
        except Exception:
            continue
        snapshot["blocks"][name] = _block_definition(block)
    return snapshot


def entity_types(snapshot: dict) -> dict[str, int]:
    """按类型统计快照实体（供摘要/诊断使用）。"""
    counts: dict[str, int] = {}
    for entity in snapshot.get("entities", ()):
        kind = entity.get("type", "?")
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def geometry_records(snapshot: dict):
    """快照里的全部几何记录：模型空间图元 + 展开得到的块几何。"""
    for rec in snapshot.get("entities", ()):
        yield rec
    for rec in snapshot.get("derived", ()):
        yield rec
