# 构件识别 DAG：几何一致性与跨图纸修复（2026-11-09）

本版本基于 `dwg-reader-generalization-20261109` 修改。没有重建 DAG，保留已有候选、证据、影子修复和数据契约。此次修复对象是**源码能定位并通过实验复现**的几何链路问题。该版本仍有明确未通过的泛化验收项，见下文。

## 实际修改

- `recognition/linegroups.py`（新增）：方向容差与法向距离容差的共线分组，统一局部基准；原始区间分开保留，避免把门洞合并成实墙。
- `recognition/geometry.py`：无向线支撑归一化；即使原始 CAD LINE 的起终点反向，也形成相同法向坐标。
- `recognition/walls.py`：斜向面线按真实区间配对；引入第三面线遮挡判据抑制虚假配对；墙段归并使用共用的几何分组；不再强制用 H/V 表达已匹配的斜向洞口。
- `recognition/wallmodel.py`：统一墙支撑与模型线之间的分组方法；不再比较浮点方向向量是否精确相等。
- `recognition/spaces.py`：空间封缝沿共线墙段工作，保留推断边界证据；面域轮廓改用游程级边界提取。
- `recognition/rooms.py`：`component_contour()` 在扫描线游程上追踪轮廓，避免大型连通域构建数百万个 Python 单元对象。
- `recognition/labels.py`、`recognition/nodes.py`：无 LLM 时利用房名形态、图层密度和几何位置审核 pending 文字，进入房间匹配前仍保留证据；门窗沿真实宿主墙局部方向投影。
- `recognition/elements.py`：LWPOLYLINE 柱截面使用**实际四条边**判定而非旋转后的轴对齐包围盒；不同来源融合时保留高可信块的精确旋转轮廓，防止把 650×650 变成 888×888。
- `tests/test_geometry_graph_hardening.py`（新增）：倒向线段、近共线、保留门洞、第三面线、游程轮廓面积、旋转柱轮廓/融合等回归测试。
- `scripts/evaluate_rotation.py`（新增）：把旋转图识别构件逆转回原图，对比几何重叠（需要可选 `shapely`），仅表示**旋转一致性**，并非真实识别准确率。

## 环境与复现

在仓库根目录执行（建议 Python 3.11+；依赖见 `pyproject.toml`）：

```sh
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python scripts/verify_user_drawings.py /path/to/办公楼.dxf --out reports/office.json --budget-ms 1200
PYTHONPATH=src python scripts/verify_user_drawings.py /path/to/测试-二维DWG.dxf --out reports/large.json --budget-ms 1200
PYTHONPATH=src python scripts/evaluate_rotation.py /path/to/办公楼.dxf /path/to/office_rotated30.dxf --degrees 30 --out reports/rotation.json
```

旋转测试图来自上一版压缩包的 `reports/perturbed/office_rotated30.dxf`；不复制到本包，避免冗余，原始 DXF 为用户上传文件。

## 本次执行结果

- 单元和集成测试：**269 通过，5 跳过，0 失败**（跳过项为需外部 profile / 字体 / DWG 转换器等）。
- 办公楼原图：墙 31、柱 38、门 30、窗 32、房间 21；**5.33 s**。
- 办公楼旋转 30°：墙 42、柱 38、门 30、窗 30、房间 20；**14.93 s**。
- 大图：墙 309、柱 67、门 65、窗 2、房间 36；**30.97 s**。
- 新版原图/旋转图的几何并集 IoU（原图预测结果作为参照）：墙 **0.9187**、柱 **0.9999**、门 **0.7274**、窗 **0.9356**、房间 **0.8197**。详细见 `reports/hardened/rotation_geometry_final.json`。
- 相较上一版：旋转图房间 6 → 20、窗 15 → 30、柱 42 → 38；大图房间 26 → 36。办公楼原图主要数量保持不变。

## 仍未通过的验收项（重要）

1. **旋转墙** 42 ≠ 原图 31。几何交集覆盖率很高但仍存在重复、拆分与部分伪候选；不应仅用实例数量判断精度。
2. **大型图房间** 36，仍低于仓库的 45–60 数量参考。尤其存在真实房间缺少隔断面域、文字同面域重叠和某些孤立/多段文字。`reports/hardened/large_room_unmatched_review.json` 列出 14 个需要进一步核查的房名线索。
3. **大型图墙** 309，高于仓库参考上限 303；其余两种构件计数落在参考范围内，但不能证明实例正确。
4. 大型图的窗仍只有 2 个最终构件，**没有独立的窗真值**，不能断言其准确率。
5. 原图与旋转图的门/房间几何仍有偏差；相关修复不等于全链路已经严格旋转不变。
6. 缺少按**真实构件实例及轮廓**人工标注的真值集，不能发布 Precision/Recall/F1 的改善声明。

## 设计原则与后续工作

- 不将“修复后计数接近参考值”作为无条件接受补丁的依据。
- 真实观测墙、独立支持的补洞和拓扑假设必须持续区分；只有独立证据足够的候选才能升级 confirmed。
- 下一轮优先处理墙线多候选非极大抑制、真实隔墙/房间的约束匹配，并以多项目人工真值而非单图纸数量为目标训练轻量排序器。
- 由于本包明确仍有未达标项，**这不是跨图纸效果完全解决的生产验收版**，而是经过完整真实 DXF 回归、可复现的修复开发版。
