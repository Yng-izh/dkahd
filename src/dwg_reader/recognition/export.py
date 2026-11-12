"""统一输出：把识别结果组装成 dwg-components-records/1.1 单一 JSON 文档。

一个文件装下全部结果（meta / basemap / components / unrecognized /
unmatched_blocks / scope / notes），组装后按 schema.validate() 自检，
不合法直接抛错，避免产出半成品。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import ezdxf

from .pipeline import render_markdown
from .schema import CATEGORIES, SCHEMA_ID, validate

#: 内部英文类别 -> 文档中文类别
CAT_CN = {
    "frame": "图框", "grid_axis": "轴线", "grid_intersection": "轴网交点",
    "north_arrow": "指北针", "column": "柱", "wall": "墙", "door": "门",
    "window": "窗", "door_window_combo": "门联窗", "room": "房间",
    "stair": "楼梯", "step": "台阶", "apron": "散水",
}
PREFIX = {
    "frame": "FRAME", "grid_axis": "AXIS", "grid_intersection": "AXISX",
    "north_arrow": "NORTH", "column": "COL", "wall": "WALL", "door": "DOOR",
    "window": "WIN", "door_window_combo": "DWC", "room": "ROOM",
    "stair": "STAIR", "step": "STEP", "apron": "APRON",
}

BASEMAP_W = 6000
PADDING = 0.02


def _bbox_of(geom: dict):
    """从各种几何表示里取世界坐标轴对齐包围盒 [l, b, r, t]。"""
    def flat(pts):
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return [min(xs), min(ys), max(xs), max(ys)]

    def norm(v):
        if v is None:
            return None
        v = list(v)
        if len(v) == 4 and all(isinstance(x, (int, float)) for x in v):
            return v
        if len(v) == 2 and all(isinstance(x, (int, float)) for x in v):
            return [v[0], v[1], v[0], v[1]]
        if v and isinstance(v[0], (list, tuple)):
            return flat(v)
        return None

    for key in ("bbox", "outline", "outer"):
        v = norm(geom.get(key))
        if v:
            return v
    if geom.get("axis"):
        return flat(geom["axis"])
    if geom.get("start") and geom.get("end"):
        return flat([geom["start"], geom["end"]])
    if geom.get("opening"):
        return flat(geom["opening"])
    if geom.get("needle"):
        return flat(geom["needle"])
    if geom.get("center"):
        c = geom["center"]
        r = geom.get("radius") or 0
        return [c[0] - r, c[1] - r, c[0] + r, c[1] + r]
    if geom.get("position"):
        p = geom["position"]
        return [p[0], p[1], p[0], p[1]]
    if geom.get("insert"):
        p = geom["insert"]
        w = geom.get("width") or 0
        return [p[0] - w / 2, p[1] - 50, p[0] + w / 2, p[1] + 50]
    return None


def basemap_transform(doc, width: int = BASEMAP_W, padding: float = PADDING) -> dict:
    """由 $EXTMIN/$EXTMAX 复算「世界坐标 -> 底图像素坐标」的变换。"""
    emin, emax = doc.header.get("$EXTMIN"), doc.header.get("$EXTMAX")
    dx = (emax[0] - emin[0]) if emin and emax else 0.0
    dy = (emax[1] - emin[1]) if emin and emax else 0.0
    # 未设置/无效的 $EXTMIN/$EXTMAX（ezdxf 新建图写 ±1e20 哨兵）时回退到实体范围
    if not (0 < dx < 1e15 and 0 < dy < 1e15):
        from ezdxf import bbox as _bb
        b = _bb.extents(doc.modelspace())
        if not b.has_data:
            raise ValueError("图纸没有可用的坐标范围（$EXTMIN/$EXTMAX 与实体范围均为空）")
        emin = (b.extmin.x, b.extmin.y)
        emax = (b.extmax.x, b.extmax.y)
        dx, dy = emax[0] - emin[0], emax[1] - emin[1]
    if dx <= 0 or dy <= 0:
        raise ValueError("图纸坐标范围退化，无法建立底图变换")
    scale = width / (dx * (1 + 2 * padding))
    return {
        "basemap_width": width,
        "basemap_height": int(round(dy * (1 + 2 * padding) * scale)),
        "world": {"xmin": emin[0] - padding * dx, "ymin": emin[1] - padding * dy,
                  "xmax": emax[0] + padding * dx, "ymax": emax[1] + padding * dy},
        "scale_px_per_unit": scale,
        "padding_ratio": padding,
        "formula": "left=(x-xmin)*scale ; right=(x1-xmin)*scale ; "
                   "top=(ymax-y1)*scale ; bottom=(ymax-y0)*scale",
        "note": "像素坐标以图像左上角为原点；模型 Y 轴向上，图像 Y 轴向下。",
    }


def _export_attributes(component: dict) -> dict:
    """schema 文档的 attributes：沿用原有「过滤 list 值」口径，但保留墙洞口列表。

    ``attributes.openings`` 是墙体跨门窗洞口合并后的结构化证据（category/id/
    width/span/type_code），schema 的 attributes 是自由对象，允许该扩展。
    """
    attrs = {key: value for key, value in component.get("attributes", {}).items()
             if not isinstance(value, list)}
    attributes = component.get("attributes", {})
    if "openings" in attributes:                 # 保留墙的空 openings 列表，字段稳定
        # 复制条目：后续 schema 记录会重写 openings[].id，不能污染上游 result
        attrs["openings"] = [dict(opening) for opening in attributes["openings"]]
    return attrs


def build_document(result: dict, dxf_path: str, basemap_file: str | None = None) -> dict:
    """组装统一文档并自检。"""
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()
    idx = {e.dxf.handle: e for e in msp}
    tf = basemap_transform(doc)
    xmin, ymax = tf["world"]["xmin"], tf["world"]["ymax"]
    scale = tf["scale_px_per_unit"]
    bw, bh = tf["basemap_width"], tf["basemap_height"]

    def to_px(bb):
        return [max(0, min(bw, round((bb[0] - xmin) * scale))),
                max(0, min(bh, round((ymax - bb[3]) * scale))),
                max(0, min(bw, round((bb[2] - xmin) * scale))),
                max(0, min(bh, round((ymax - bb[1]) * scale)))]

    records, counter = [], {}
    # 组件 id（WALL-H-001 / DOOR-01 / WIN-01） -> schema 记录 id（WALL-0001 / DOOR-0001 / WIN-0001）
    component_ids: dict[tuple[str, str], str] = {}
    for c in result["components"]:
        cat = c["category"]
        cn = CAT_CN.get(cat, cat)
        if cn not in CATEGORIES:
            continue
        counter[cn] = counter.get(cn, 0) + 1
        geom = c.get("geometry", {})
        bb = _bbox_of(geom)
        if not bb:
            continue

        srcs = c.get("source_entities", [])
        handles = [s["handle"] for s in srcs if s.get("handle")]
        first = srcs[0] if srcs else {}
        ent = idx.get(first.get("handle")) if first.get("handle") else None
        is_insert = bool(ent is not None and ent.dxftype() == "INSERT")

        if is_insert:
            from ezdxf import bbox as _bb
            ext = None
            for h in handles:
                e2 = idx.get(h)
                if e2 is None or e2.dxftype() != "INSERT":
                    continue
                b2 = _bb.extents(list(e2.virtual_entities()))
                if not b2.has_data:
                    continue
                ext = ([b2.extmin.x, b2.extmin.y, b2.extmax.x, b2.extmax.y] if ext is None
                       else [min(ext[0], b2.extmin.x), min(ext[1], b2.extmin.y),
                             max(ext[2], b2.extmax.x), max(ext[3], b2.extmax.y)])
            if ext:
                bb = ext
            block, layer = ent.dxf.name, ent.dxf.layer
            anchor, depth = "insert", 1
            insert_pt = [round(float(ent.dxf.insert[0]), 1), round(float(ent.dxf.insert[1]), 1)]
            rot = round(float(ent.dxf.rotation), 2)
            scl = [round(float(ent.dxf.xscale), 4), round(float(ent.dxf.yscale), 4)]
        else:
            block, layer = None, (first.get("layer") or "")
            anchor, depth = "geometry", 0
            insert_pt = [round((bb[0] + bb[2]) / 2, 1), round((bb[1] + bb[3]) / 2, 1)]
            rot, scl = 0.0, [1.0, 1.0]

        bpx = to_px(bb)
        rec = {
            "id": f"{PREFIX.get(cat, 'C')}-{counter[cn]:04d}",
            "category": cn,
            "name": c.get("name"),
            "block": block,
            "layer": layer,
            "anchor": anchor,
            "depth": depth,
            "insert_pt": insert_pt,
            "rotation_deg": rot,
            "scale": scl,
            "bbox_model": [round(bb[0], 1), round(bb[1], 1), round(bb[2], 1), round(bb[3], 1)],
            "bbox_px": bpx,
            "bbox_center_px": [round((bpx[0] + bpx[2]) / 2), round((bpx[1] + bpx[3]) / 2)],
            "dxf_handle": first.get("handle"),
            "source_handles": handles,
            "source_entities": srcs,
            "geometry": geom,
            "attributes": _export_attributes(c),
            "evidence": c.get("evidence") or {},
            "confidence": c.get("confidence"),
            "status": c.get("status"),
        }
        if geom.get("opening"):
            rec["opening"] = geom["opening"]
        if geom.get("axis"):
            rec["axis"] = geom["axis"]
        component_ids[(cn, c["id"])] = rec["id"]
        records.append(rec)

    # F1：schema 记录 id 已按类别重编号，attributes.wall_id / openings[].id 必须跟着重映射；
    # handle 保持 DXF 句柄不变。映射按类别隔离，避免 WALL-0001 与 DOOR-0001 混淆。
    wall_ids = {component_id: record_id
                for (category, component_id), record_id in component_ids.items()
                if category == "墙"}
    door_ids = {component_id: record_id
                for (category, component_id), record_id in component_ids.items()
                if category == "门"}
    window_ids = {component_id: record_id
                  for (category, component_id), record_id in component_ids.items()
                  if category == "窗"}
    for rec in records:
        attrs = rec["attributes"]
        if rec["category"] in ("门", "窗"):
            wall_id = attrs.get("wall_id")
            if wall_id is not None:
                attrs["wall_id"] = wall_ids.get(wall_id, wall_id)
        elif rec["category"] == "墙":
            for opening in attrs.get("openings", ()):
                mapping = door_ids if opening.get("category") == "door" else window_ids
                opening["id"] = mapping.get(opening.get("id"), opening.get("id"))

    used = {h for r in records for h in r["source_handles"]}
    un = {}
    for e in msp.query("INSERT"):
        if e.dxf.handle in used:
            continue
        un[e.dxf.name] = un.get(e.dxf.name, 0) + 1

    by_type: dict[str, int] = {}
    for r in records:
        by_type[r["category"]] = by_type.get(r["category"], 0) + 1

    meta = result["meta"]
    document = {
        "schema": SCHEMA_ID,
        "meta": {
            "source_file": os.path.abspath(dxf_path),
            "parsed_file": os.path.abspath(dxf_path),
            "dxf_version": doc.dxfversion,
            "created_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
            "tool": "dwg-reader-skill 0.5.0 recognition 引擎（ezdxf）",
            "method": meta.get("method", ""),
            "units": meta.get("units", "mm"),
            "extents": meta.get("extents"),
            "counting_rule": "按构件实例计：块类构件 1 个 INSERT = 1 条；"
                             "墙/房间/轴线等由几何推导，anchor=geometry",
            "total_records": len(records),
            "by_type": by_type,
            "counts": meta.get("counts", {}),
            "grid": meta.get("grid", {}),
            "wall_diagnostics": meta.get("wall_diagnostics", {}),
            "unrecognized_count": len(result["unrecognized"]),
        },
        "basemap": {
            "id": "basemap-v1", "version": "basemap-v1", "file": basemap_file,
            "width": bw, "height": bh, "sha256": None,
            "style": "白底黑线线稿，不含文字注记与填充图案",
            "generated_from": os.path.abspath(dxf_path),
            "transform": tf,
        },
        "components": records,
        "unrecognized": result["unrecognized"],
        "unmatched_blocks": [{"block": k, "count": v,
                              "note": "未归入 12 类构件，需扩规则或判定为非构件"}
                             for k, v in sorted(un.items(), key=lambda kv: -kv[1])],
        "unmatched_total": sum(un.values()),
        "scope": {
            "summary": {"mapped_categories": len(by_type),
                        "mapped_instances": len(records),
                        "unmatched_blocks": sum(un.values())},
            "mapped": [{"category": k, "count": v}
                       for k, v in sorted(by_type.items(), key=lambda kv: -kv[1])],
        },
        "notes": [
            "bbox_model 为世界坐标轴对齐包围盒 [左, 下, 右, 上]。",
            "bbox_px 对应底图，[左, 上, 右, 下]，原点在图像左上角，已裁剪到底图边界内。",
            "anchor=insert 的记录来自块参照；anchor=geometry 的记录由几何推导，insert_pt 取包围盒中心。",
            "source_handles 为该构件引用的全部原始图元 Handle；dxf_handle 取其中第一条。",
            "门/门联窗的 bbox 取自块几何外接框，含门扇与开启弧线；洞口实际范围见 opening 字段。",
            "status=uncertain 的构件建议人工复核；confidence 只反映判据强弱，不是准确率。",
        ],
    }

    check = validate(document)
    if not check["ok"]:
        raise ValueError("输出文档不符合 schema：" + "；".join(check["errors"][:5]))
    return document


def to_schema(result: dict, dxf_path: str, basemap_file: str | None = None) -> dict:
    """兼容旧名，等价于 build_document()。"""
    return build_document(result, dxf_path, basemap_file)


def write_outputs(result: dict, dxf_path: str, out_dir: str,
                  basemap_file: str | None = None) -> dict:
    """写出统一 JSON 与 Markdown 报告。"""
    os.makedirs(out_dir, exist_ok=True)
    document = build_document(result, dxf_path, basemap_file)

    p = os.path.join(out_dir, "components.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(document, f, ensure_ascii=False, indent=1)

    r = os.path.join(out_dir, "report.md")
    with open(r, "w", encoding="utf-8") as f:
        f.write(render_markdown(result))

    return {"components": p, "report": r}
