# 识别 DAG

`src/dwg_reader/recognition/` 是一个 48 节点的构件识别 DAG（有向无环图）：
从 DXF 解析、图层角色推导、墙/柱/门窗/房间等识别，到缺陷诊断、修复建议、
导出，全部按节点声明依赖，可并行、可按目标裁剪、结果按内容哈希缓存。

## 看点

| 文件 | 作用 |
|---|---|
| `dag.py` | 执行引擎：拓扑排序、并发、缓存、统计 |
| `nodes.py` | 全部节点实现（MAIN 与 PURE 两类） |
| `scale.py` | 图内自校准尺度：把绝对毫米阈值换成"图内基准量 × 无量纲倍率" |
| `walls.py` | 墙：两阶段识别（高召回候选 → 几何⊗拓扑验证）、跨洞口合并、共线接面 |
| `arcwalls.py` / `curtain.py` | 弧墙（同心圆弧配对）、幕墙（共面三线归并） |
| `rooms.py` / `spaces.py` | 房间：面域优先 + 房名挂载 |
| `layer_roles_infer.py` | 图层角色：几何指纹 ⊗ 图层名先验 |
| `llm_credentials.py` | 大模型凭据入口（key 不入库，全链路一处配置） |
| `param_registry.py` | 运行时参数表：绝对授权区间 + 单位 + 归一化基准 + 真实消费者 |
| `pattern_discovery.py` | 第 47 节点：残余图元聚类 -> PatternCase（无模型、不改构件） |
| `ai_planner.py` | 第 48 节点：两段式推理，只出假设与计划，坐标一律拒收 |
| `model_gateway.py` | 唯一网络出口：预算 / 脱敏 / 图像授权 / 结构化解析 / 审计 |
| `vision.py` | ROI 局部矢量渲染：灰=背景、橙=目标、绿=墙、蓝=文字 |
| `experiment_runner.py` | 同一 baseline 试多组参数、去重、闭集失败码、预算刹车 |
| `rule_memory.py` | 三层规则记忆（candidate -> project -> cross_project），召回只产生提议 |

## AI 自适应增强（默认关闭）

`ai_mode` 三档：`off`（默认，纯几何）/ `propose`（只出提议，不改结果）/
`enhance`（提议 → 受约束编译 → 同一 baseline 上试多组参数 → 影子+全局验收 →
通过才合并）。没配 key、模型故障、超预算、验收不过，全部回滚到"没有 AI"的结果。

```text
raw_evidence -> defect_detect -> pattern_discovery[新]
evidence_graph/quality_gate（登记 pattern:<hash>）
quality_gate -> ai_hypotheses[新] -> plan_compile -> adaptive_controller
```

新增三条硬边界（有单测守着）：提议里出现坐标/代码/路径整条拒收；被编译进来
却没被任何算子读取的参数判失败；需要升级 `confirmed` 的候选必须有独立来源证据。

## 三条设计约束

1. **判据不许写死毫米数**：除 `scale.py` 的 `STRUCTURE` 表，其它模块不准新增
   数值阈值常量，有单测强制（`tests/test_generalization.py`）。
2. **大模型只做判断，不做几何**：它只能从闭集里挑角色或否决候选、给参数区间，
   坐标、尺寸、阈值一律由程序算；`meta.llm` 与 `meta.ai_recognition` 会写明
   到底有没有用到模型、发了几张图、花了多少次调用。
3. **参数必须真被读**：可调参数只能来自 `param_registry`，运行时以不可变
   `RecognitionConfig` 显式传给算子；没被 `take()` 过的参数会进 `params_unused`。

## v2 编排层（新架构，与上图并存）

旧 DAG 是"基础规则 → 找残余 → AI 补救"，AI 的范围被第一轮识别决定。v2 把
三条路并列：规则引擎 / 图形模式引擎 / 多模态 AI，各自往同一个**假设池**投候选，
再由几何拟合与全局联合求解裁决：

```text
cad_ir（统一 uid/几何/多尺度区域）+ geometry_graph（平行/共线/相接/包含/同族）
   -> proposal_engines（rule / pattern / ai 三引擎）
   -> geometry_fitter（component_grammar 的封闭语法在原始图元上兑现）
   -> global_resolver（冲突图 + 分阶段联合选择，保留多状态）
   -> agent_controller（选区域/要语法/失败反馈）+ experiment_store（实验与规则复用）
```

| 文件 | 职责 |
|---|---|
| `v2/cad_ir.py` | 统一几何表示：稳定 uid、封闭 kind、块族、多尺度区域 |
| `v2/geometry_graph.py` | 关系图 + 网格空间索引 |
| `v2/component_grammar.py` | 构件几何语法：封闭原语/关系/锚点 + 内置模板 |
| `v2/geometry_fitter.py` | 按语法在原始图元上拟合精确几何（模型不碰坐标） |
| `v2/proposal_engines.py` | 三引擎并行出候选、统一 schema、去重 |
| `v2/global_resolver.py` | 冲突图 + 联合选择（墙→洞口→空间，多状态） |
| `v2/agent_controller.py` | 观察/提问/反馈循环 |
| `v2/space_solver.py` | 房间：选中墙网 → 面域（分块栅格）→ 房名挂靠 |
| `v2/experiment_store.py` | 实验记录、比较、规则复用 |

覆盖 **11 类**：墙/柱/门/窗/楼梯/台阶/散水/图框/轴网/指北针/房间。房间必须由
选中的墙网重算（单向链），目前只做"面域+挂靠"，不含多标签剖分与空间路由 ——
所以房间数低于 v1，这是已知缺口。

```bash
dwg-reader v2 图纸.dxf -o v2.json --store .dkahd/experiments
```

## 运行

```bash
pip install -e .[dxf]

# AI 增强（默认关闭；不给 --ai-mode 就是纯几何）
dwg-reader components 图纸.dxf --ai-mode propose --ai-config ai_recognition.json -o proposals.json
dwg-reader components 图纸.dxf --ai-mode enhance --ai-config ai_recognition.json -o enhanced.json
dwg-reader components 图纸.dxf --ai-mode enhance --no-ai-vision   # 不发截图，只用结构特征

python scripts/identify_components.py 图纸.dxf --format json -o 结果.json
```

`fonts/` 目录未随仓库提供（第三方 CAD 字体，另装），只影响渲染出图，不影响识别。
