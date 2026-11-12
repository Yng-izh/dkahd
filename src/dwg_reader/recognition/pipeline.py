"""构件识别主流程：原生图元规则识别（无 AI / 无图像）。

识别流程已 DAG 化（见 ``dag.py`` / ``nodes.py`` / ``snapshot.py``）：

    load -> texts/walls_seg/columns/partitions/grid/doors/windows/frame/north/stairs
         -> wall_model / spaces_seed -> openings_select -> walls_merge
         -> envelope/steps/entrance_steps/apron/rooms
         -> room_labels -> rooms -> assemble -> export

``recognize()`` 是兼容门面：串行、无缓存、无统计、只跑到 ``assemble``，返回
``{"meta", "components", "unrecognized"}``；编排能力见 ``run_recognition()``。
渲染与导出见 ``export.py``。
"""

import collections
import hashlib
import json
import os
from typing import Sequence

from . import registry
from . import registry as PROFILE_REGISTRY
from .base import load
from .dag import DagExecutionError, DagRun, LiveDocument, NodeContext, run_dag
from .nodes import build_recognition_dag


def _file_digest(path: str) -> str:
    """DXF 文件字节的 SHA-256（缓存键按内容寻址，不按路径）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_recognition(dxf_path: str, *, jobs: int = 1,
                    targets: Sequence[str] | None = ("assemble",),
                    cache_dir: str | os.PathLike[str] | None = None,
                    collect_stats: bool = True,
                    fail_fast: bool = True,
                    raise_on_error: bool = True,
                    basemap_file: str | None = None,
                    profile: str | dict | None = None,
                    use_registry: bool = True,
                    llm_verify: bool = False,
                    llm_review: bool = True,
                    llm_review_vision: bool = True,
                    llm_api_key: str | None = None,
                    ai_mode: str = "off",
                    ai_config: str | dict | None = None,
                    ai_memory_dir: str | None = None,
                    ai_diagnostics: str | None = None) -> DagRun:
    """编排 API：按 DAG 执行识别，可并行 / 部分执行 / 缓存 / 收集统计。

    默认行为与 ``recognize()`` 一致（串行、无缓存、目标 ``assemble``）；
    显式 ``targets=("export",)`` 才生成 schema 文档。

    ``profile`` 未显式给出时，按 DXF 内容哈希查 profile 注册表（离线 LLM 适配
    的结果会自动生效）；``use_registry=False`` 可强制使用确定性默认方案。

    ``ai_mode`` 是 AI 增强的唯一开关（``off`` / ``propose`` / ``enhance``，
    见 :mod:`ai_config`）：``off`` 时不实例化网关、不发请求、不加任何判定，
    输出与改造前逐条一致。
    """
    from . import ai_config as AIC
    ai_cfg = _ai_config(ai_config, ai_mode)
    ai_mode = str(ai_mode or (ai_cfg or {}).get("mode") or "off")
    digest = _file_digest(dxf_path)
    if profile is None and use_registry:
        profile = registry.lookup(digest=digest)
    live = LiveDocument(lambda: load(dxf_path))
    params = {"basemap_file": basemap_file, "profile": profile,
              "llm_verify": bool(llm_verify), "llm_review": bool(llm_review),
              "llm_review_vision": bool(llm_review_vision),
              "llm_api_key": llm_api_key,
              "ai_mode": ai_mode,
              "ai_config": ai_cfg if ai_mode != "off" else None,
              "memory_dir": ai_memory_dir,
              "diagnostics_path": ai_diagnostics,
              "cache_dir": str(cache_dir) if cache_dir else None}
    context = NodeContext(dxf_path=dxf_path, file_digest=digest,
                          params=params, live=live)
    dag = build_recognition_dag()
    return run_dag(dag, context=context, params=params,
                   targets=targets, jobs=jobs, cache_dir=cache_dir,
                   collect_stats=collect_stats, fail_fast=fail_fast,
                   raise_on_error=raise_on_error)


def _ai_config(ai_config, ai_mode: str) -> dict:
    """解析 ``ai_config``：可以是 dict、JSON 路径，或 None（用默认值 + 该模式）。"""
    from . import ai_config as AIC
    if isinstance(ai_config, dict):
        return AIC.load(mode=ai_mode or ai_config.get("mode"), overrides=ai_config)
    if ai_config:
        return AIC.load(str(ai_config), mode=ai_mode or None)
    return AIC.load(mode=ai_mode or "off")


def recognize(dxf_path: str):
    """对一张 DXF 做全量构件识别，返回 {meta, components, unrecognized}。

    签名与返回结构与 DAG 化之前完全一致；节点异常会还原为原始异常类型/消息。
    profile 由 :mod:`registry` 按内容哈希自动匹配，找不到就用确定性默认方案。
    """
    try:
        run = run_recognition(dxf_path, jobs=1, targets=("assemble",),
                              cache_dir=None, collect_stats=False)
    except DagExecutionError as exc:
        raise exc.cause
    return run.output


CATEGORY_LABEL = {
    "frame": "图框", "grid_axis": "轴线", "grid_intersection": "轴网交点",
    "north_arrow": "指北针", "column": "柱", "wall": "墙", "door": "门",
    "window": "窗", "door_window_combo": "门联窗", "room": "房间",
    "stair": "楼梯", "step": "台阶", "apron": "散水",
}


def render_markdown(result: dict) -> str:
    """把识别结果渲染成 Markdown 报告（纯函数，只读 result）。"""
    meta = result["meta"]
    comps = result["components"]
    unrec = result["unrecognized"]
    by = collections.Counter(c["category"] for c in comps)
    pick = lambda cat: [c for c in comps if c["category"] == cat]

    L = []
    A = L.append
    A(f"# {meta['source']} 构件识别报告\n")
    A(f"- 源文件：`{meta['source']}`（{meta['dxf_version']}），单位 **{meta['units']}**")
    A(f"- 图形范围：{meta['extents']}")
    A(f"- 方法：{meta['method']}")
    A("- 说明：本报告为**规则匹配结果**；无人工标注真值，因此不给出“识别准确率”，只给出各构件的判据与置信度。\n")

    A("## 一、按类别统计\n")
    A("| 类别 | 数量 |")
    A("| --- | --- |")
    for k, n in sorted(by.items(), key=lambda kv: -kv[1]):
        A(f"| {CATEGORY_LABEL.get(k, k)} | {n} |")
    A(f"| **合计** | **{len(comps)}** |")
    A(f"\n未识别图元 {len(unrec)} 条。\n")

    frame = (pick("frame") or [None])[0]
    if frame:
        g, at = frame["geometry"], frame["attributes"]
        A("## 二、图框\n")
        A(f"- 外框：{g['outer']}，尺寸 {g['width']:.0f}×{g['height']:.0f} mm")
        A(f"- 内框：{g['inner']}")
        A(f"- 图名：{at.get('drawing_name')}；图号：{at.get('drawing_number')}；项目：{at.get('project')}")
        A(f"- 比例：{at.get('scale')}\n")

    A("## 三、轴网\n")
    gd = meta.get("grid", {})
    A(f"- 纵轴：{'、'.join(gd.get('v_labels', []))}；轴距 {gd.get('v_spans')} mm")
    A(f"- 横轴：{'、'.join(gd.get('h_labels', []))}；轴距 {gd.get('h_spans')} mm")
    A(f"- 交点 {len(pick('grid_intersection'))} 个\n")

    north = (pick("north_arrow") or [None])[0]
    if north:
        at = north["attributes"]
        A("## 四、指北针\n")
        A(f"- 圆心 {north['geometry']['center']}，半径 {north['geometry']['radius']:.0f}")
        A(f"- {at.get('meaning')}（方位角 {at.get('azimuth_deg')}°）\n")

    walls = pick("wall")
    A("## 五、墙\n")
    A(f"- 共 {len(walls)} 段，总长 {sum(w['attributes']['length_mm'] for w in walls)/1000:.1f} m")
    th = collections.Counter(w["attributes"]["thickness_mm"] for w in walls)
    A(f"- 厚度分布：{dict(th)} mm")
    A(f"- 剔除候选 {meta.get('wall_diagnostics', {}).get('dropped', 0)} 个（贴墙窗套/线脚、非众数厚度）")
    for w in sorted(walls, key=lambda w: -w["attributes"]["length_mm"])[:10]:
        A(f"  - {w['id']}: {w['geometry']['start']} → {w['geometry']['end']}，"
          f"厚 {w['attributes']['thickness_mm']:.0f}，长 {w['attributes']['length_mm']:.0f}")
    A("")

    cols = pick("column")
    A("## 六、柱\n")
    A(f"- 共 {len(cols)} 根，截面 {dict(collections.Counter(c['attributes']['section'] for c in cols))}\n")

    doors, wins = pick("door"), pick("window")
    # block 可能是 None（用弧线画的门没有块名），必须防 None
    part = [d for d in doors
            if str(d["attributes"].get("block") or "").startswith("$TCHSYS$APDOOR")]
    A("## 七、门窗\n")
    A(f"- **按樘计**：门 {len(doors)} 樘（其中卫生间隔断门 {len(part)} 樘）、窗 {len(wins)} 樘")
    A(f"- 门宽分布：{dict(sorted(collections.Counter(d['attributes']['width_mm'] for d in doors).items()))} mm")
    A(f"- 窗宽分布：{dict(sorted(collections.Counter(w['attributes']['width_mm'] for w in wins).items()))} mm\n")

    rooms = pick("room")
    A("## 八、房间\n")
    A("| # | 房间 | 标注面积 m² | 几何面积 m² | 偏差 | 状态 |")
    A("| --- | --- | --- | --- | --- | --- |")
    for i, r in enumerate(rooms, 1):
        at = r["attributes"]
        al, ag = at.get("area_label_m2"), at.get("area_geom_m2")
        d = f"{(ag - al) / al * 100:+.1f}%" if al else "—"
        A(f"| {i} | {r['name']} | {al if al else '—'} | {ag:.2f} | {d} | "
          f"{'OK' if r['status'] == 'confirmed' else '待核'} |")
    A("")

    A("## 九、楼梯 / 台阶 / 散水\n")
    for s in pick("stair"):
        at = s["attributes"]
        A(f"- 楼梯：bbox {s['geometry']['bbox']}，踏步 {at.get('tread_count')} 道，"
          f"间距 {at.get('tread_spacing_mm')} mm，编号 {s['name']}")
    for s in pick("step"):
        at = s["attributes"]
        A(f"- 台阶：bbox {s['geometry']['bbox']}，{at.get('steps')} 级，标注 {at.get('label')}")
    for s in pick("apron"):
        A(f"- 散水：bbox {s['geometry']['bbox']}，宽度 {s['attributes'].get('width_mm')} mm")
    A("")

    A("## 十、未识别图元\n")
    for reason, n in collections.Counter(u["reason"] for u in unrec).most_common():
        A(f"- {reason}：{n} 条")
    A("")

    A("## 十一、已知限制\n")
    A("1. **房间面积**：以 10mm 栅格求连通域，求面积前把同一墙线上相邻墙段之间的洞口"
      "（≤9m）临时闭合，因此得到的是“各房间净面积”，门洞/通道处按墙线截断；"
      "卫生间等被隔断块细分的房间标 uncertain。")
    A("2. **墙体**：仅识别 WALL 图层的“双线墙”画法（平行面线 + 端部封口），"
      "单线墙、块内墙、以及未配对的 WALL 图元未覆盖；厚度非众数的候选按“贴墙窗套/线脚”剔除。")
    A("3. **台阶**：仅识别散水图层上“同中心嵌套 U 形轮廓”的画法。")
    A("4. **门**：宽度取块参照 |xscale| 并以门窗编号校核；开启方向由块内弧线几何推定，"
      "双扇门与隔断门不给左右开。")
    A("5. **图框**：仅检出闭合多段线；图签区若没有表格线，只能取到文字。")
    A("6. **置信度 ≠ 准确率**：无人工标注真值时，confidence 只反映判据强弱。")
    return "\n".join(L)


if __name__ == "__main__":
    import sys as _sys

    if len(_sys.argv) < 2:
        raise SystemExit("用法: python -m dwg_reader.recognition.pipeline <图.dxf>")
    print(json.dumps(recognize(_sys.argv[1])["meta"]["counts"], ensure_ascii=False))
