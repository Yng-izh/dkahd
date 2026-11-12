# v2 编排层：多路径候选 + 全局联合求解

旧架构是"基础规则 → 找漏掉的图元 → AI 补救"。它有一个结构性缺陷：

> **AI 的工作范围由第一轮算法决定。** 第一轮把图元错判成墙/装饰线、把图层归错、
> 把空间切错，第二轮再聪明也只能在这个错误前提里补。

v2 把这件事倒过来：三条路各自独立出候选，**统一放进一个假设池**，再由几何拟合
与全局联合求解裁决。旧 48 节点 DAG 一个字没改：v2 把它当"几何底座"用。

```text
CAD 统一几何表示（cad_ir）· 块族 · 关系图 · 文字 · 多尺度区域
        │
        ├── 规则引擎（proposal_engines.rule）     现有识别器结果 → 候选
        ├── 图形模式引擎（proposal_engines.pattern）残余聚类 + 内置语法 → 拟合 → 候选
        └── 多模态 AI（agent_controller + grammar）只提**语法**，不提坐标 → 拟合 → 候选
        ↓
   统一构件假设池（同一图元允许多个竞争解释）
        ↓
   参数化几何拟合（geometry_fitter：语法在原始图元上兑现）
        ↓
   全局联合识别（global_resolver：冲突图 + 分阶段联合选择 + 多状态）
        ↓
   Confirmed / Uncertain / Unresolved + 来源证据 + 可复用规则（experiment_store）
```

## 模块与职责

| 文件 | 职责 | 复用的旧实现 |
|---|---|---|
| `cad_ir.py` | 统一几何表示：稳定 uid、封闭 kind、块族、多尺度区域 | `snapshot.py`、`blockfamily.py`、`scale.py` |
| `geometry_graph.py` | 几何关系图（平行/共线/相接/相交/包含/同族/贴空间）+ 网格索引 | `geometry.py`、`evidence.py` |
| `component_grammar.py` | 构件几何语法：封闭原语/关系/锚点 + 内置模板 + 校验 | `operator_specs.py`（闭集思想） |
| `geometry_fitter.py` | 按语法在原始图元上找部件、检查关系、算精确几何 | `executors.py`、`walls.py`（配对/线束算法思路） |
| `proposal_engines.py` | 规则/模式/AI 三引擎并行出候选，统一 schema + 去重 | `walls.py`、`elements.py`、`pattern_discovery.py` |
| `global_resolver.py` | 冲突图 + 贪心/局部改进联合选择，分阶段（墙→洞口→空间），保留多状态 | `local_solver.py` |
| `agent_controller.py` | 选观察区域、向模型要语法、多尺度视图、失败反馈循环 | `ai_planner.py`、`model_gateway.py`、`vision.py` |
| `experiment_store.py` | 实验记录/比较/规则复用（接三层记忆库） | `experiment_runner.py`、`rule_memory.py` |

## 四条硬边界

1. **模型只能提语法**：原语、关系、锚点全是闭集；输出里出现坐标/包围盒/毫米数
   整份作废（`component_grammar.MODEL_GEOMETRY`）。
2. **几何只能由拟合器算**：每个候选的 `uids` 必须指向真实图元，坐标由
   `geometry_fitter` 从原始几何算出，模型不参与。
3. **裁决先硬后软**：互斥（门/窗同位置）、重复、宿主缺失这类硬冲突直接不允许
   同选；支持关系只加分。权重是首版统一配置，**未经独立标注校准前不宣称准确率**。
4. **不假装算过**：房间面域必须由墙网重算，`staged()` 明确返回
   `spaces.status = needs_geometry`，不会凭空造房间。

## 用法

```bash
# 纯确定性（不调模型）：候选生成 + 联合求解照样跑
dwg-reader v2 图纸.dxf -o v2.json --store .dkahd/experiments

# 允许模型提语法（多尺度视图会一起发过去）
dwg-reader v2 图纸.dxf --ai-mode enhance --ai-config ai_recognition.json \
  --store .dkahd/experiments --memory .dkahd/rule-memory
```

```python
from dwg_reader.recognition import v2

result = v2.run_drawing("图纸.dxf", ai_mode="off")
print(result["final"]["counts"])          # 联合求解后的最终计数
print(result["resolution"]["stages"])     # 分阶段：墙 → 洞口 → 空间
print(result["pool"]["engines"])          # 三条引擎各自出了多少候选
```

## 与旧架构的关系

* 旧 `run_recognition()` / `recognize()` 签名与输出**完全不变**，`ai_mode=off`
  时行为逐条一致；
* v2 是**新增**的编排器，通过 `run_drawing()` 读旧的快照与墙/柱/门窗结果，
  在它们之上做多假设与联合求解；
* v2 的结论目前**不写回**旧导出结构：它先作为"影子层"跑，等独立真值集证明
  确实更好，再决定谁当默认。
