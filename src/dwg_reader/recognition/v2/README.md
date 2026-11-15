# v2 编排层：以构件族为中心的多模态识别（v2-component-focus）

> 这一版（`2.1.0-component-focus`）把识别的**核心单位**从"区域 → 语法"改成
> "构件族 → 语义 → 全图传播"。门、窗、柱是主输出；墙和轴网只做宿主与几何约束；
> 房间退出主流程，作为可选派生。

旧架构的缺陷不只是"AI 的工作范围由第一轮算法决定"，还有一层更贵的：**逐实例
调用模型**。一张图里几十扇窗是同一种画法，模型只看一次就够，剩下的事程序干。

```text
构件族发现（component_family）
   重复块族 / 门弧+门扇 / 平行线束 / 闭合轮廓 / 规则已认出的实例
        │  按"窗 > 柱 > 门 × 实例数 × 尚未理解比例"排队
        ↓
多模态构件理解（multimodal_detector）
   只回答：是什么构件、什么子类型、由哪些部分组成、有没有反面特征
   不看整块区域，不出坐标，不能造词
        ↓
专用几何拟合（fitters/door · window · column）
   各自按本实例自身的尺度量（窄窗框不再被"全图墙厚"卡死）
   各自带类别特有的反面证据（家具弧、图例轮廓、墙上的一段线）
        ↓
全图实例传播（instance_propagator）
   旋转 / 镜像 / 缩放不变的形状签名 + 逐实例几何复核 + 跨图层
        ↓
门窗柱专用求解（component_resolver）
   分类别权重、分类别来源可信度、分类别冲突；不确定的保留为 uncertain
```

规则 / 图形模式 / AI 语法三条引擎继续并行供候选，只是现在多了一条
`propagation`；旧 48 节点 DAG 仍然原样当"几何底座"，但 focused 档**只跑它需要
的那几个节点**（不跑房间面域、房间路由、装配与导出）。

---

## 旧版说明（v2.0，仍可用 `--profile full`）


旧架构是"基础规则 → 找漏掉的图元 → AI 补救"。它有一个结构性缺陷：

> **AI 的工作范围由第一轮算法决定。** 第一轮把图元错判成墙/装饰线、把图层归错、
> 把空间切错，第二轮再聪明也只能在这个错误前提里补。

v2 把这件事倒过来：三条路各自独立出候选，**统一放进一个假设池**，再由几何拟合
与全局联合求解裁决。旧 48 节点 DAG 一个字没改：v2 把它当"几何底座"用。

```text
CAD 统一几何表示（cad_ir）· 块族 · 关系图 · 文字 · 多尺度区域
        │
        ├── 多模态首轮理解（multimodal_bootstrap）
        │      全图概览 → 重复块族和空间区域 → 构件语法提案
        │      不依赖残余图元，也不依赖图层和块的名字
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
| `multimodal_bootstrap.py` | **第一次 AI 调用就读全图**；对代表块族和空间区域做图像与几何联合理解 | `vision.py`、`model_gateway.py` |
| `agent_controller.py` | 共享模型调用预算，接收初轮语法，再处理冲突和失败反馈 | `ai_planner.py`、`model_gateway.py`、`vision.py` |
| `space_solver.py` | 房间：选中墙网 → 面域（分块栅格）→ 房名挂靠 | `spaces.py`、`rooms.py`、`labels.py` |
| `experiment_store.py` | 实验记录/比较/规则复用（接三层记忆库） | `experiment_runner.py`、`rule_memory.py` |

## 覆盖的构件类别（11 类）

| 类别 | 来源 | 说明 |
|---|---|---|
| 墙 / 柱 / 门 / 窗 | 规则引擎 | v1 的墙 run / 柱 / 统一洞口结果 |
| 楼梯 | 规则引擎 | 踏步串（`stair.tread_series` 语法也能拟合） |
| 台阶 | 规则引擎 | 含入口台阶（两个 v1 节点合并成一类，来源记在 `evidence.rule`） |
| 散水 | 规则引擎 | 外圈闭合轮廓 |
| 图框 | 规则引擎 | 闭合多段线，一张图一个"有内容"的框 |
| 轴网 | 规则引擎 | 轴线 + 交点两个 subtype |
| 指北针 | 规则引擎 | 圆 + 箭头符号 |
| 房间 | `space_solver` | **必须**由选中的墙网重算面域再挂房名（单向链，绝不反推墙） |

房间口径要说清楚：v2 目前是"面域 + 房名挂靠"这一步（`rooms.rooms_from_faces`），
**不含** v1 房间节点里的多标签剖分与空间路由，所以同一张图上房间数会比 v1 少
（住院楼实测 14 vs 44）。这是已知缺口，不是"合并简化"的胜利。

## 四条硬边界

1. **模型只能提语法**：原语、关系、锚点全是闭集；输出里出现坐标/包围盒/毫米数
   整份作废（`component_grammar.MODEL_GEOMETRY`）。
2. **几何只能由拟合器算**：每个候选的 `uids` 必须指向真实图元，坐标由
   `geometry_fitter` 从原始几何算出，模型不参与。
3. **裁决先硬后软**：互斥（门/窗同位置）、重复、宿主缺失这类硬冲突直接不允许
   同选；支持关系只加分。权重是首版统一配置，**未经独立标注校准前不宣称准确率**。
4. **不假装算过**：房间面域必须由墙网重算，`staged()` 明确返回
   `spaces.status = needs_geometry`，不会凭空造房间。

## 多模态首次识别（建议使用）

```bash
pip install -e '.[dxf,vision]'
export DWG_AI_BASE_URL='https://YOUR-OPENAI-COMPATIBLE-ENDPOINT/v1'
export DWG_AI_API_KEY='YOUR_KEY'
export DWG_AI_MODEL='YOUR_MULTIMODAL_MODEL'
export DWG_AI_VISION_MODEL='YOUR_MULTIMODAL_MODEL'

dwg-reader v2 '图纸.dxf' --no-convert --ai-mode enhance \
  --ai-config examples/ai_v2_multimodal.json -o v2-result.json

# 原有 components 命令也可以显式切换到 v2（JSON 格式）：
dwg-reader components '图纸.dxf' --no-convert --engine v2 \
  --ai-mode enhance --ai-config examples/ai_v2_multimodal.json \
  -o v2-result.json
```

`enhance` 在 v2 中严格要求 **图片渲染正常、API 密钥有效、允许上传图片**：
缺任何一项都会报错而非悄悄退化到文本模式。配置中的
`provider.allow_remote_images=true` 是显式授权，**会上传全图概览和局部
CAD 渲染图片到你指定的端点**；不传原始 DXF 文件。默认配置不授权上传。
如果图纸敏感，请配置经过许可的本地兼容多模态模型服务。

`result.final.instances` 是可直接消费的 v2 实例输出，包含拟合出的
`geometry` / `bbox` / `uids` / `evidence` / `source`。
`result.bootstrap`、`result.gateway`、`result.vision` 证明首轮是否实际调用
模型及上传图片。此格式与 v1 `components` 导出 schema 不同，请勿混用。

首轮 VLM 的语义判断并不等于正确的构件实例。当前拟合器和规则仍可能漏识别
幕墙、复杂楼梯和开敞房间；模型提案必须通过原始图元拟合与全局约束。
`propose` 模式只给提案、不向候选池提交 AI 结果；`off` 不调用模型。

## 旧版用法

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
* v2 CLI 的 `final.instances` 是正式的 v2 结构化几何结果，
  不再仅限影子计数；旧 `components` 的导出 schema 仍保持独立，
  在未经跨项目人工真值验证之前，不自动替换老引擎。


---

# v2-component-focus：新增模块

| 文件 | 职责 | 关键约束 |
|---|---|---|
| `profiles.py` | 识别档位：主输出 / 上下文 / 可选派生 + 每档跑哪些 DAG 节点 | 档位只减少工作，不新增判定 |
| `component_family.py` | 构件族发现：重复块、门弧、线束、线对、闭合轮廓、规则实例 | 只归"长得一样"，不判"是什么"；配对用**局部间距**，不用全图墙厚 |
| `multimodal_detector.py` | 构件级语义：类别 / 子类型 / 组成 / 反面特征 / 竞争解释 | 闭集；出现坐标或尺寸即整条作废 |
| `fitters/door.py` | 平开（含双扇）、推拉、无弧洞口、门联窗 | 弧心=铰链、半径=门宽、弧要落在墙上；家具弧记 uncertain 不删 |
| `fitters/window.py` | 块窗、四线窗、线束窗、推拉窗、转角窗、幕墙 | 框间距按实例自身量；端部封口 + 外侧共线延续用来区分窗与墙段 |
| `fitters/column.py` | 矩形/旋转/异形/填充/散线柱 | 轮廓重建要逐边验证；结构位置（贴墙/轴线/柱网）是主要证据 |
| `instance_propagator.py` | 族 → 全图实例（旋转/镜像/缩放/跨图层） | 每个命中都要过拟合器，拟合不过就不算 |
| `component_resolver.py` | 门窗柱专用打分与冲突裁决 | 分类别权重与来源可信度；结构性否证（"这是墙上的一段线"）不参选 |

## 用法

```bash
# 门窗柱专用（默认）
dwg-reader v2 图纸.dxf --profile openings_columns -o out.json

# 只要候选、更精简（不跑洞口语义裁决与残余扫描）
dwg-reader v2 图纸.dxf --profile openings_columns_lean

# 旧行为（11 类 + 房间）
dwg-reader v2 图纸.dxf --profile full
```

输出里必须看三处，别只看总数：

* `profile`：这一档少算了什么（`dag_targets`、`rooms`）；
* `base.caliber`：底数是"导出口径"还是"候选口径"——两者不能直接比；
* `families` / `detector` / `pool.engines.propagation`：族发现、模型读懂几个、
  传播出多少个实例、各自失败在哪。

## 已知局限（必须一起看）

* 传播是"形状签名 + 拟合复核"，**没有真值**时无法证明多出来的实例是真构件；
  多出来的窗要人工抽查，别当成提升；
* 门联窗、转角窗这类组合形态只在族内相邻实例之间判定，跨族合并还没做；
* `component_resolver` 的权重是首版先验，需要按类别用独立标注校准。

## 实测（住院楼-图层混乱.dxf，2026-11-13）

同一张图、同一台机器，四种口径并列（**数字不可互相加减**）：

| 跑法 | 墙 | 柱 | 门 | 窗 | 楼梯 | 房间 | 耗时 |
|---|---|---|---|---|---|---|---|
| v1（导出/assemble 口径） | 236 | 31 | 120 | 21 | 7 | 44 | — |
| v2 `full`（房间在链里） | 248 | 33 | 120 | 21 | **7** | 14 | 132 s |
| v2 `openings_columns`（纯几何） | 248 | 42 | 120 | 47 | — | 不算 | 180 s |
| 同上 + 模型（propose，9 次调用 / 9 张图） | 248 | 38 | 120 | 47 | — | 不算 | 215 s |

怎么读这张表：

* `full` 档的楼梯从 0 变 7 是**修 bug**（以前它们进池子但不属于任何求解阶段）；
* 门窗柱专用档里柱 +9、窗 +26 是**传播出来的新实例**，没有真值，不能当提升；
  多出来的窗要人工抽查（混进双线墙的风险靠"外侧共线延续"挡住了一部分，没挡住全部）；
* 房间 14 vs 44 是老缺口（v2 的面域阶段不含 v1 的多标签剖分与空间路由），
  这也是房间退出主流程的理由之一；
* 模型那份是 `propose`（只提判断、不直接提交几何）；两次运行里模型对同一族的判断
  并不稳定（一次把某族判成家具、另一次判成窗），所以"模型结论"目前只能当候选证据。
