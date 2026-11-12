# CAD 构件识别 DAG 跨图纸泛化改造与验证（2026-11-09）

本版以 `dwg-reader-DAG-fixed-20261109.zip` 为基础，实际修改源码并在用户提供的 `办公楼.dxf`、`测试-二维DWG.dxf` 上验证。**这是一份有明确未通过项的工程实验版本**：不能声称已解决任意图纸泛化或实现了实例级 F1 提升。

## 已实施的改动

1. `layer_roles_infer.py`：记录各图层全部角色融合分数 `role_scores`，而不是仅保留单一判定。
2. `nodes.py`：自动执行方案携带 `role_hypotheses` 和有上限的 `soft_layers`。候选墙、柱、门窗的候选层集合加入高可信的歧义图层。对墙层增加几何/命名独立证据门槛，防止电气等图层被当墙。
3. `nodes.py`：`node_room_labels` 改用 `load` 运行时的自动执行方案，并显式补充 DAG 依赖；无法在指定图层找到有效房名时，允许按房间名称形态在建筑范围内兜底。
4. `elements.py`：块门与弧门并行产生候选，同位置去重；块窗与线窗并行产生候选，要求合理跨度与墙缺口证据。陌生图层全图扫开启弧时，家具/洁具类语义为负证据。斜向门洞使用任意角度的墙线投影验证。
5. `elements.py`：增加闭合四线旋转矩形柱识别。斜向柱必须有全部 4 条边，避免把普通家具轮廓变成柱。
6. `walls.py`：短线合并、单线墙和桥接候选支持斜向局部坐标；在正常轴向图纸仅有孤立斜线时关闭斜向补充候选，避免额外误检。
7. `regions.py`：区域网格识别依据小/中图元实际覆盖格网而非仅第一端点；仍保留对跨越大量格网的图框和大跨度图元的保护。
8. `nodes.py`：每个区域生成 `region_context`，记录区域单位判定、图层和结构量。**当前用于诊断与后续分区判定，没有全面替换全图尺度参数。**
9. `scale.py`：端点近邻搜索使用确定性采样，避免大型密集图元中的近邻计算爆炸。
10. `tests/test_cross_drawing_generalization.py`：增加 7 个定向测试，涵盖斜墙短线、斜墙单线、并行门候选、旋转洞口、角色分数、旋转柱及动态房名图层。

## 真 DXF 验证结果

基于 `scripts/verify_user_drawings.py`，本次原始图纸采用 `jobs=4`，不调用外部模型，限制控制器预算以隔离泛化识别流程。

| 数据集 | 墙 | 柱 | 门 | 窗 | 房间 | 实际运行耗时 |
|---|---:|---:|---:|---:|---:|---:|
| 原始办公楼 | 31 | 38 | 30 | 32 | 21 | 14.49 秒 |
| 办公楼全部非系统图层加前缀 XYZ_ | 31 | 38 | 30 | 33 | 21 | 12.05 秒 |
| 办公楼旋转 30° | 50 | 42 | 30 | 15 | 6 | 11.74 秒 |
| 大型测试 DXF（172MB） | 298 | 67 | 65 | 2 | 26 | 86.99 秒 |

数量验收的参考值来自仓库 `gt/office.json`、`gt/tdw.json`，**仅为数量回归，不能作为实例级 precision/recall/F1**。大型图参考：墙 297–303、柱 67±2、门 55–76、房间 45–60，窗没有人工核验真值。

### 验收结论

- 办公楼原图：主要构件数量回归通过；窗 32 在仓库参考允许误差内。
- 图层加前缀：墙、柱、门、房间数量与原图相同；窗 33 vs 32，仍有一条不一致。
- 大型图：墙、柱、门数量落入参考区间；**房间 26 低于 45–60，未通过**。这证明空间面域与房名关联还需加强。
- 旋转 30°：墙、窗、房间发生明显漂移，**全流程旋转不变性未通过**。当前仅针对局部几何算子覆盖任意角度。
- 不能用“大图墙 298”推出“识别准确率很高”；几何真值、误检/漏检一对一匹配仍未建立。

## 自动化测试

```text
258 passed, 5 skipped in 24.38s
```

5 项跳过原因：部分字体文件、ODA DWG 转换器、外部工作区 profile 和 t5 原文件未就绪。测试具体详情见 `reports/generalization_pytest.log`。

## 使用和复现

```bash
pip install -e .
PYTHONPATH=src pytest -ra
python scripts/verify_user_drawings.py /path/to/办公楼.dxf --out reports/office.json --budget-ms 3000
python scripts/verify_user_drawings.py /path/to/测试-二维DWG.dxf --out reports/large.json --budget-ms 1000
python scripts/recognize_eval.py perturb --src /path/to/办公楼.dxf --dst /tmp/renamed.dxf --ops rename_layers --prefix XYZ_
python scripts/recognize_eval.py perturb --src /path/to/办公楼.dxf --dst /tmp/rotated.dxf --ops rotate --degrees 30
```

输出报告位于 `reports/generalization_*.json`，变换后的办公楼小图保留在 `reports/perturbed/`。压缩包不包含用户上传的 172MB 原始大型 DXF。

## 下一步必须做的两项工作

1. 对墙/洞口/房间实现**全链路方向无关**的墙网、候选归并、面域生成与空间关联，并在旋转图上做逆变换后的实例级匹配。现有几何局部改造不足以解决这项问题。
2. 给 `测试-二维DWG.dxf` 的房间补齐逐实例真值，并检查房名坐标、区域归属、闭合面域及未匹配房名。不能为了让数量达到 45–60 而强行新增房间。

仅有两张原始图纸不足以训练可靠的候选排序模型；本版没有伪造 LightGBM 模型、训练集或 F1 改善数值。
