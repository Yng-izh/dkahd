# 识别 DAG · 代码说明

`src/dwg_reader/recognition/` 是这套图纸识别的全部实现。**它不是一串顺序调用的函数，
而是一张 46 个节点、带依赖、可并行、可部分执行、可缓存、可审计的有向无环图。**

一句话概括设计意图：

> 把"识别一张图纸"拆成 46 个各自可单独运行、单独验证、单独替换的纯数据阶段；
> 阶段之间只传 JSON 原生数据；哪个阶段出问题，就只重跑哪个阶段，并且能查到它吃了什么、吐了什么。

---

## 1. 目录职责

| 文件 | 行数 | 职责 |
|---|---|---|
| `dag.py` | 787 | **执行器**：`Node` / `DAG` / `NodeContext` / `run_dag` / 内容寻址缓存 / 统计。不认识任何具体识别逻辑 |
| `nodes.py` | 2259 | **节点函数 + 注册表** `build_recognition_dag()`：46 个节点的实现与依赖声明 |
| `pipeline.py` | 209 | **编排门面**：`run_recognition()`（可编排）与 `recognize()`（兼容旧签名） |
| `snapshot.py` | 305 | `DrawingSnapshot`：DXF 模型空间一次性提取成 JSON 原生快照 |
| `base.py` | 457 | 图元层基础：加载、索引、文字合并、几何聚类 |
| `geometry.py` | 526 | 单位解释 + 方向无关几何基元 + 轮廓度量 |
| `scale.py` | 592 | **图内自校准尺度**：三个通用估计子 + 唯一一张无量纲常数表 + 出处可查 |
| `walls.py` | 1237 | 墙体：双线面线配对 → 中心线 + 厚度 + 轮廓 |
| `wallmodel.py` | 309 | `WallSpan` / `WallSolid` / `Gap`：墙网与缺口证据（方向无关表达） |
| `linegroups.py` | 96 | 共线分组：方向容差 + 法向距离容差统一局部基准，保留原始区间（避免把门洞并成实墙） |
| `unlayered.py` | 50 | 单图层图纸检测：全图 96% 图元集中在同一层且图元数 ≥100 时判定为「无图层语义」，仅在此条件下把该层放开给各识别器，并禁止按伪图框裁剪 |
| `unified.py` | 478 | **统一洞口**：先定"洞在哪、多宽"，再定"它是什么" |
| `openings.py` / `opening_cases.py` | 320 / 490 | 洞口候选枚举、证据分组、双路径接受、模型裁决落成几何 |
| `rooms.py` | 911 | **面域优先的房间**：房间 = 闭合面域 + 挂靠房名；多标签面域按栅格最近标签剖分 |
| `spaces.py` / `openzone.py` | 343 / 101 | 空间面域种子（给洞口判定提供"两侧是不是不同空间"）；开放分区 |
| `elements.py` | 1143 | 图框 / 轴网 / 指北针 / 柱 / 门 / 窗 / 楼梯 / 台阶 / 散水 |
| `blockmodel.py` | 181 | 块语义归一：靠"块族 + 块长相"，不靠块名白名单 |
| `layer_names.py` / `layer_roles_infer.py` / `layer_roles.py` | 161 / 384 / 151 | 图层名归一化 → 几何指纹融合 → 本图语义覆盖（不写全局 profile） |
| `plan.py` / `plan_compile.py` / `registry.py` / `auto_profile.py` | 120 / 407 / 158 / 236 | 图纸 → 执行方案（plan / profile）及其注册与自动推导 |
| `primitives.py` / `operators.py` / `operator_specs.py` | 174 / 288 / 353 | 几何证据原语、通用算子、封闭算子/策略库（版本化 + 权限 + 预算） |
| `evidence.py` / `raw_evidence.py` | 201 / 195 | 统一证据索引（`ev:` / `pat:` / `wall:` / `col:` / `op:` …）与原始证据账本 |
| `defects.py` / `repair.py` / `repair_planner.py` | 155 / 100 / 308 | 缺陷分类、确定性修复算子、受约束的修复选型 |
| `quality_gate.py` | 434 | 质量闸门：只把"确定性算法已无法消歧且可修"的问题交出去 |
| `local_solver.py` / `recompute.py` / `adaptive_controller.py` | 585 / 299 / 382 | 局部联合求解（影子试算 + 事务式提交）、增量重算、自适应控制器 |
| `finalstate.py` | 273 | `final_candidate_state`：全链路唯一结果状态，下游只读它 |
| `export.py` / `schema.py` | 329 / 264 | 统一输出文档 `dwg-components-records/1.1` 与校验器 |
| `evaluation.py` / `verify.py` / `diagnose.py` / `perturb.py` | 554 / 253 / 112 / 235 | 跨图纸评估、质量指标、失败诊断、受控扰动 |
| `llm_*.py`（8 个） | 1900+ | **模型只在闭集里做选择**：`opening_judge` / `label_filter` / `space_judge` / `repair_planner` / `llm_reason` / profile / verify |
| `config.py` / `taxonomy.py` | 27 / 55 | 算法级配置（不含图纸语义阈值）与人工维护的语义闭集 |

> 全部约 **28.5k 行**（含 `scripts/`）。运行时只依赖 `ezdxf`；模型调用是可选的。

---

## 2. 执行器（`dag.py`）

### 2.1 一个节点是什么

```python
Node(name, fn, deps=(), kind="pure", cacheable=True, version=None, summarize=None, params=())
```

| 字段 | 含义 |
|---|---|
| `fn(ctx) -> Any` | 节点实现。输入是 `NodeContext`（只能读 `ctx.inputs[<依赖名>]`、`ctx.params`、`ctx.dxf_path`、`ctx.file_digest`），输出必须是 **JSON 原生**（`None/bool/int/float/str/list/dict`，不能有 tuple/set/bytes/NaN） |
| `deps` | 依赖的节点名。依赖的输出就是这个节点的 `ctx.inputs[dep]` |
| `kind` | `"pure"`：可被提交到线程池并行，**访问 `ctx.doc` / `ctx.msp` 立即抛 `RuntimeError`**；`"main"`：固定在编排线程执行，可访问 live ezdxf 文档 |
| `cacheable` | 是否参与内容寻址缓存。含模型调用或与外部状态相关的节点显式关掉 |
| `version` | 默认取 `fn` 源码的 SHA-256 前 16 位，**改代码自动让缓存失效**，不需要手工 bump 版本 |
| `summarize` | 自定义输出摘要（统计面板与缓存元数据用），不用把大对象打进日志 |
| `params` | 本节点消费的 `NodeContext.params` 键名，只影响缓存键 |

**当前 43 个 PURE + 3 个 MAIN**：只有 `load`、`assemble`、`export` 是 MAIN，其余全部 PURE。
这是并行安全的硬边界：PURE 节点拿不到 ezdxf 对象，就不可能在线程间共享可变文档。

### 2.2 一次执行

```python
run = run_dag(dag, context=ctx, params=..., targets=("assemble",), jobs=4,
              cache_dir=None, collect_stats=True, fail_fast=True, raise_on_error=True)
run.outputs["load"], run.stats, run.summary(), run.duration_ms
```

| 参数 | 行为 |
|---|---|
| `targets` | 只执行这些节点的**上游闭包**（Kahn 拓扑序裁剪）。`("assemble",)≈ 全部构件`；`("export",)` 才产出 schema 文档；也可以只跑 `("columns_verify",)` 做单点调试 |
| `jobs` | PURE 节点的线程池大小；MAIN 节点永远串行。`jobs=1` 等价串行 |
| `cache_dir` | 内容寻址缓存根目录；`None` 关闭。缓存键 = `sha256(canonical_json({cache, engine, node, version, file, params, deps:{dep: output_digest}}))`，落盘 `<cache_dir>/dwg-dag-cache-v1/<node>/<key>.json`。`clear_cache()` 可清 |
| `collect_stats` | 收集 `NodeStat`（耗时、线程、输入摘要、输出摘要/摘要哈希、cache 命中、错误） |
| `fail_fast` | 首个节点失败后把其余节点标 `skipped` |
| `raise_on_error` | 失败时抛 `DagExecutionError`（带每个失败节点的原始异常 `cause`）；关掉则拿残缺 `DagRun` 继续 |

结构校验在 `DAG.__init__` 里就做完：重复名、未知依赖、自依赖、环（DFS 三色，报闭合路径而不是只说"有环"）。
拓扑序用 Kahn + 就绪集按名字排序，**同一张 DAG 多次调用顺序完全一致**——这是可复现的前提。

### 2.3 错误类型

`DagError` → `DagValidationError`（含 `DagCycleError`）/ `DagNodeError`（单节点失败，保留 `cause`）/ `DagExecutionError`（一次执行有失败节点，`failures` + `skipped` + 首个 `cause`）。

---

## 3. 46 个节点

分 8 组。`*` = 含模型调用（`cacheable=False`，无 key 时自动降级为确定性策略，链路仍可跑通）。

### A. 载入与快照

| 节点 | 依赖 | 作用 |
|---|---|---|
| `load` (MAIN) | — | 打开 DXF、单位解释、块展开、区域切分、plan/profile、**第一遍尺度测量**；产出 `DrawingSnapshot` |
| `texts` | load | 文字合并：碎字拼词、轴号/门窗编号/尺寸剔除 |
| `raw_evidence` | load | 原始证据账本：任何图元都不丢弃，登记 `unused/weak/used/rejected/inferred` |

### B. 版式与轴线

| 节点 | 依赖 | 作用 |
|---|---|---|
| `partitions` | load | 隔断/轻质墙候选 |
| `grid` | load | 轴网：轴线编号、轴距、交点 |
| `frame` | load, texts | 图框、图名、图号（多图框时选"有内容"的那个） |
| `north` | load, texts | 指北针与方位角 |

### C. 构件初筛（都是候选，不判最终身份）

| 节点 | 依赖 | 作用 |
|---|---|---|
| `columns` | load | 柱形态候选：4 线轴对齐矩形 / 缺边矩形 / 填充+轮廓 / 块 |
| `walls_seg` | load, columns | 双线面线配对 → 墙段（中心线 + 厚度 + 轮廓） |
| `doors` | load, texts, walls_seg | 门候选：门族块、开启弧、铰点+门扇 |
| `windows` | load, texts, walls_seg | 窗候选：窗族块、双线束、贴墙短跨 |
| `stairs` | load, texts | 楼梯（踏步线密度 + 箭头 + 文字） |
| `room_labels_seed` | texts, load | 房名种子：只读文字与 profile，**不依赖墙网**（避免与闸门成环） |

### D. 尺度与墙网（第二遍测量）

| 节点 | 依赖 | 作用 |
|---|---|---|
| `wall_model` | walls_seg, load | 墙网：`WallSpan`（跨洞口连续）/ `WallSolid` / `Gap`；**用真实墙厚与墙缺口宽度精化尺度** |
| `columns_verify` | columns, walls_seg, load | 柱两阶段复核：缺边/异常候选必须有墙网拓扑支持，否则留 `pending` 不入构件 |

> 柱的画法有四条路：块名白名单（`_FZH`）、**实心四边形柱块**（`M_S0BKA` 这类
> 「单位方块 + 插入缩放」）、闭合多段线、线段组合。第三条以前没有，导致实测
> 100 根柱只认出 7 根——见 `elements._footprint_blocks`。
| `spaces_seed` | load, walls_seg, columns, partitions | 空间面域种子：观察面域 + 假设面域 + 缺口桥接 |

### D-0. 判定优先级（`priority.py`）：按证据强度定类，不按谁先跑完

多条判据同时命中同一个图元时，必须有一层裁决，否则就是"谁先算完谁占坑"。实测：
5.5m 长、180mm 厚（=墙厚）、写着窗号 C15、画在 WINDOW 层的窗，被门的形状判据抢先
命中，输出成"5538mm 宽的门"。

| 级 | 证据 | 说明 |
|---|---|---|
| L1 | 显式标注 | 门窗编号 / 块属性（C=窗、M=门） |
| L2 | 块族语义 | 块名先验 + 几何指纹（`blockfamily`） |
| L3 | 互斥形态 | 窗：厚度≈墙厚且长宽比≥3 **且无门扇几何**；门：有开启弧 **且扇宽在 3~8 倍墙厚内** |
| L4 | 图层角色 | WINDOW / 门层声明 |
| L5 | 上下文 | 墙缺口、两侧空间、贴柱（只佐证不定类） |
| L6 | 统计拓扑 | 重复次数、轴网交点、连通性 |

两条纪律：**高优先级覆盖低优先级必须留痕**（写 `category_override`，含级别与理由）；
**同级冲突不定案**（标 `needs_review`）。模型不在这一表里——它只允许否决已生成的候选。

注意 L3 里两个"必须"都是踩出来的：只看"有弧就是门"会让 5538mm 的窗变成门；只看
"厚度≈墙厚且细长"会把带门扇的门（门深度也等于墙厚）误判成窗。

### D-1. 块族：一次定角色、全族实例继承（`blockfamily.py`）

块参照是图上**唯一自带身份**的东西；同一块名的所有实例本来就是同一个构件。所以
先聚族、再定角色，最后全族继承——而不是逐个实例判一遍（艺术楼 100 根柱只来自
2 个块族，逐实例判定等于把同一件事判了 100 遍，实测只认出 7 根）。

**角色是多对一的**：一个角色下可以有任意多个块族，这是常态：

| 图 | 门 | 窗 | 柱 |
|---|---|---|---|
| 艺术楼-图层混乱 | 2 族 / 15 个 | 2 族 / 30 个 | 2 族 / 100 个 |
| 办公楼 | 4 族 / 30 个 | 1 族 / 31 个 | 1 族 / 38 个 |
| 会所-图层混乱 | 5 族 / 58 个 | 5 族 / 63 个 | — |

判角色三种证据：**命名先验**（`$DorLib2D$`/`$WinLib2D$`/`_DBLK`/`_FZH` 一类厂商
用法）、**几何指纹**（实心四边形=柱、门弧=门、多笔画=窗）、**族间互证**（同图库
系列 或 同几何指纹的族继承已确认的角色）。

三条纪律（都是实测踩出来的）：

1. **块名只是提名，几何必须确认**——`_DBLK2` 这种"名字像门、块里没门弧"的块
   不许进名单；`_WBLK2`（四条线叠在一处）也不算窗；
2. **已确认的角色永不被互证改写**——门库里的门联窗被判成窗就保持窗；
3. **匿名块名不可迁移**：`A$C68D277C8` 是每次保存重新生成的哈希，跨图只能用
   `fingerprint()` 的几何指纹。

实测块类构件覆盖率（族实例 -> 进构件）：艺术楼 柱 100/100、门 15/15、窗 28/30；
办公楼 柱 38/38、门 30/30、窗 31/31；会所 门 58/58、窗 58/63。

### D-2. 墙网分隔检验（`walls_net.py`，默认关闭）

"一条线只有把空间分成两半，才有资格成为墙"——把它做成**生成侧**判据：
把全部墙候选画进粗栅格（同一墙线上的缺口临时补合），求连通空间，再对每条候选
在两侧各打三个点；两侧是同一片空间 = 悬空线（标注/家具/引线），两侧是不同空间 = 墙。

| 档位 | 环境变量 | 行为 |
|---|---|---|
| off（默认） | `DWG_WALL_NET=0` | 完全不跑，识别路径与之前一致 |
| evidence | `DWG_WALL_NET=1` | 只给候选写 `net_state` / `net_split` 证据，一条不删 |
| strict | `DWG_WALL_NET=strict` | 剔除"不分隔空间"的候选 |

**实测结论：不能当硬门，默认关闭。** 用"同栋楼的图层版"当代理真值
（`艺术楼-全在一个图层.dxf` 与 `艺术楼-图层混乱.dxf` 的墙线逐条坐标重合 319/319）：

| 图纸 | 候选池真墙占比 | 判为"分隔空间"的精度 | 召回 |
|---|---|---|---|
| 办公楼（图层干净） | 93% | 100% | 19% |
| 艺术楼-图层混乱 | 83% | 98% | 64% |
| 艺术楼-全在一个图层 | 19% | 25% | 41% |

判据本身是准的（干净图上 98~100%），但**召回不足**：一半以上的真墙两侧并不构成
两块**闭合**空间（图纸画成片段、大开间、围合不严），于是被判"不分隔"。安全阀
（通过率 < 50% 或空间数 < 3 时整体退回）就是为了不让这种误杀落地。

### D-3. 尺寸只有一个入口（`sizing.py`）

同一块图形"多宽、多厚"只能有一个答案。历史上洞口宽度有三条互不知情的来源，
每条只在一种画法上成立：

| 画法 | 正确来源 | 另一档读出来是 |
|---|---|---|
| 单位块 + 插入放大（天正门块，`xscale=900`） | `|xscale|` | 块净尺寸×缩放（同一值，无冲突） |
| 按真实尺寸画、插入缩放 1（`M_E24TTP`，块内 500×650） | 块内几何 / 属性 `XSIZE` | `|xscale|` = **1mm** |
| 参数化图库（`XSIZE`/`YSIZE` 属性） | 属性声明值 | 几何可能只画示意，偏小 |

`SIZING.measure()` 按固定顺序取，先到先得，并把用了哪一档写进记录的
`width_source` / `width_candidates`（可审计）：

```
code（门窗编号 M1021 -> 1000×2100）
  > geometry（块内几何量出的洞口长）
  > declared（块属性 XSIZE/YSIZE/WIDTH/HEIGHT）
  > scaled（|插入缩放| × 块定义净尺寸）
  > family（同族中位数）
  > insert（|xscale|，老口径兜底）
```

两条纪律：

* **几何退化的洞口不救**：几何量出 0mm ≠ 这一档不可用（电梯厅门 `_LV6` 块内
  没有门扇，几何 0mm），必须让 0 继续往下走、由结构判据筛掉，而不是换成缩放
  系数把它救活；
* **一个 1mm 会污染全局**：门宽中位数 → 洞口上下限 → 柱的高宽上限 → 房间封缝
  上限，全部按"门宽 × 倍率"派生。实测艺术楼门宽一度被标成 397mm，源头就是
  这类读数混进了中位数。

### D-4. 噪声剔除：先把"不是构件的东西"挡在图元池外

阈值只解决"该不该信"，噪声必须先解决"该不该进候选池"。三类噪声各有各的闸：

| 噪声 | 判据 | 挡在哪 |
|---|---|---|
| 尺寸/标高/索引**标注块** | 块名含 `_DIM`/`DIMX`/`*D`/`ELEV`/`INDEX` | `snapshot._expandable` 不展开（标注线、箭头、引出线与墙线同形，且艺术楼有 568 个，会把 `MAX_DERIVED` 名额吃光导致真实几何被**静默截断**） |
| **纯标注/填充图层** | 图层名含 `DIM`/`标注`/`尺寸`/`HATCH`/`填充`… | `base.is_noise_layer`，只在"没声明图层角色"的兜底路径上生效 |
| **伪缺口**（接头断线 / 自噪声碎缝） | 宽度≈墙厚（±15%）；或 ≤3 倍自噪声 | `scale.drop_artifact_gaps`，进分布统计前先剔（艺术楼 22 个缺口里 12 个正好 180mm=墙厚，把门宽压到 397、洞口下限压到 2.4mm） |

### D-5. 门洞当隔断：房间数靠"洞口闭合"而不是"墙够不够多"

`spaces_seed` 现在接收 `doors` 当 `barriers`：门洞是墙上真实的缺口，不补的话
两间房经门洞连成一个连通域，房间数会系统性偏少（实测艺术楼 56 道墙只围出 11 个
面域，14 间房只落到 8 间）。

准入只用两个与画法无关的物理量：**能给出轴线**（门块/开启弧/墙缺口都行）、
**宽度在 (墙厚, 2 个门宽] 之间**。更宽的开口是通道/开敞门洞，房间本来就通着，
补上它等于凭空造墙；比墙厚还短的是接头断线。

这是**唯一允许进观测面域的人工补墙**——它补的是已观测到的洞口，不是猜的墙；
走廊级 9000mm 的虚拟闭合仍然只进推断面域（`hypothesis`）。修复分支
（`executors._op_rebuild_space_faces`）补的是同一批 `barriers`，两个阶段口径一致。

### D-6. 楼梯：认"踏步串"，不认"图层上的线"

老做法把楼梯层上的线按 x 聚类、取簇的外接框，于是该层只要有一根贯穿全图的线
（剖断线/扶手/引线），"楼梯"就变成横贯图面的长条（实测艺术楼 3 部全中招）。

梯段的形态特征本身很强，且与画法无关：**平行**的短线（长度≈梯段宽）+ **等分**
间距（踏步宽，波动 ≤1/4）+ 至少 4 条。两把尺子都用门宽换算
（`STRUCTURE["stair_tread_*"]` / `stair_width_*`），外接框只取踏步本身。

### D-7. 弧墙：同心圆弧配对（`arcwalls.py`）

墙的面线配对以"直线 + 常量法向偏移"为前提：轴向路径按一个坐标值聚类，斜向路径
按角度分桶。圆弧的每一小段法向都不同，"厚度"不是常量偏移而是**半径差**——
把阈值放宽多少都配不出来，而且 `segments_from` 只认 LINE/LWPOLYLINE，ARC 连
候选池都进不去。一张住院综合楼实测：模型空间 4 处曲墙 8 条 ARC（弧长 29.8m）、
幕墙弧形 9 条（44.9m），识别结果里**一条都没有**，房间在圆角处直接漏出去。

圆弧有自己的不变量，跨图纸不变，所以单开一条通路（不进直线配对/召回/验证链）：

1. 两道弧**同圆心**（圆心差 ≤ 0.05 墙厚，`arc_center_tol_thickness`）；
2. 两弧**半径差 = 墙厚**（0.25~2 倍，`arc_thickness_*`）；
3. 两弧**角度区间重叠**（≥ 较短弧的 60%、且 ≥15°，`arc_span_overlap` / `arc_min_sweep_deg`）；
4. 与直线墙同构的"跨中线"否决：两弧之间夹着第三条同心弧且两侧各留 ≥0.3 墙厚时，
   外层那对不是一道墙（三线幕墙画法，`arc_mid_block_ratio`）。

输出与直线墙同构：`orient="A"`、`arc=true`、`curve{center,radius,inner/outer_radius,
start/end_angle}`、`thickness`、`length`（中线弧长）、`outline`（折线化闭合带，
步长 5°）。证据是纯几何，`evidence_state=strong` 当且仅当半径差等于本图墙厚。

下游四处必须知道"弧不是直线"：

| 位置 | 处理 | 不改会怎样 |
|---|---|---|
| `spaces.build` / `walls_net._fill` | 铺 `outline` 折线带 | 按 start→end 画弦，弧内侧空间被切掉一角 |
| `merge_runs` / `merge_across_openings` | 原样直通（`_arc_runs`） | 按 orient/center 分组重建轮廓会把弧抹成弦 |
| `rooms.build_and_fill` 的 `seal_gaps` | 跳过非 H/V | 用 H/V 区间去补相邻段，补出横穿房间的假墙 |
| `nearest_wall` / `_oriented_wall_runs` / `wall_model` | 跳过弧墙 | 把"中线半径"当法向偏移量，洞口宿主与墙带统计全错 |

实测（06住院综合楼平面施工图，165MB DXF）：墙 255 → **261**（+6：4 道 200 厚
实体弧墙 + 2 道 100 厚弧形幕墙），房间 41 → **42**，其中圆角处被切出的 16 m²
办公室从"住院大堂"里分离出来、大堂房名归位。会所/办公楼/艺术楼三张对照图
逐条指纹比对**零变化**（它们墙层上没有成对圆弧）。

### D-8. 幕墙：共面三线归并成一条带（`curtain.py`）

幕墙算**墙的一种**（不新增类别），但口径必须是"带"而不是"半带"。

一道幕墙在平面上常画成三条共面线：外皮、内皮，加中间一条玻璃/分格基准线。
实测 06住院综合楼：外两条与 WALL 层墙皮**完全重合**，中间那条只出现在 CURTWALL
层，三层间距恒为 100，带总厚 200。而面线配对只认"两道面线配一对"，于是同一条带
被拆成两道各 100 的候选、再被"共用面线"去重删掉一道——**厚度口径从 200 掉到 100**。

判据（全部是图内倍率）：

1. 同方向、共覆盖的平行线 >=3 条，相邻间距**相等**（±`curtain_gap_tol`=10%）；
2. 每段间距 <= `curtain_band_max_thickness` x 墙厚（0.8 倍）；
3. 共覆盖总长 >= `curtain_min_len_thickness` x 墙厚（15 倍）；
4. **中间那条线的图层必须是幕墙类，或不在墙层里**。

第 4 条是安全阀：一道 300 厚、居中画了中线的实体墙，三条线等距也共覆盖，前三条
全中——只有第 4 条能把它挡在外面（三条线都在 WALL 层 -> 不是幕墙）。

三条工程细节，都是实测逼出来的：

| 细节 | 为什么会踩到 |
|---|---|
| 先剪掉本身过短的面线 | 0.2mm 图例/线脚同样可能与邻居"等距"，混进链条后把共覆盖压到 0.2m，一条带都出不来 |
| 长���由**带内线**划定，不是外皮 | 外皮常比幕墙长（同一条墙线上还有别的墙段），那是普通墙的领地 |
| 一片一片输出，交给 `merge_runs` 缝回连续墙 | 幕墙被结构柱切成 6~10 片（柱宽约 600），按"同中心线 + 缺口<=1m"缝合，与实体墙同口径 |

合并时同一条墙线上的"幕墙段 + 实体墙段"会成一道墙（位置相同，本来就该是一道）：
`_role_of()` 按长度占比决定标不标 `role="curtain"`（>=50% 才标），并**始终**记录
`curtain_share`，既不吞掉幕墙，也不让一小段幕墙把整道墙染色。

实测（06住院综合楼）：墙 261 -> 258，其中 **10 道幕墙、合计 155.3m、厚 200**
（含 1 道弧形幕墙 9.0m）；原来那些 100 厚的假半带墙 9 道消失；房间 42 不变，
大堂 365.3 -> 366.1 m²（带比半带厚，吃掉的面积）；门 120 / 窗 27 / 柱 67 / 楼梯 7
全不变。会所/办公楼/艺术楼三张对照图逐条指纹**零变化**（没有幕墙层）。

### E. 洞口统一（"是什么"晚于"在哪"）

| 节点 | 依赖 | 作用 |
|---|---|---|
| `opening_judge`\* | load, walls_seg, wall_model, spaces_seed, doors, windows | 洞口性质裁决：程序枚举候选并算好几何，模型只回闭集 `{id, role, reason}` |
| `openings_unify` | load, doors, windows, walls_seg, wall_model, opening_judge | **把门窗两条并行流合并成"一个洞只出现一次"**：投影到（墙线 + 区间）上按重叠合并，双扇靠结构规则配对（同线、相邻区间、铰点在两端、叶宽相近） |
| `openings_select` | wall_model, spaces_seed, openings_unify, opening_judge, doors, windows | 互斥选择：一个洞口只能是一个构件；冲突记证据 |

### F. 证据、缺陷与闸门

| 节点 | 依赖 | 作用 |
|---|---|---|
| `defect_detect` | load, raw_evidence, walls_seg, spaces_seed, room_labels_seed, openings_select, columns | 图纸缺陷分类：表达不完整（可推断）/ 识别器不支持（补算子）/ 真画错（去重告警） |
| `repair_options` | defect_detect, walls_seg | 确定性修复候选（补边、补墙、共线墙端…），每个方案必须有一条独立支持证据 |
| `evidence_graph` | load, walls_seg, wall_model, spaces_seed, doors, windows, columns, room_labels_seed | 统一证据索引：候选/原语/构件/面域进同一张表，带来源与相互关系 |
| `label_filter`\* | room_labels_seed | 房名语义过滤：确定性规则拿不准的文字交模型判 `room/annotation/uncertain`，闭集、无坐标 |
| `space_cases` | spaces_seed, label_filter, defect_detect | 把"房名没闭合"的每一处整理成可判断的案例 |
| `space_judge`\* | space_cases | 判"该补墙 / 本就是开放空间 / 不确定"，目标必须是已算出的 `defect_id` |
| `space_routing` | space_cases, space_judge | 裁决结果回灌确定性分支：`open_space`→建开放分区；`try_repair`→待修复；`uncertain`→待核 |
| `quality_gate` | 12 个依赖 | 只挑"确定性算法已无法可靠消歧**且存在可执行修复**"的问题出门 |

### G. 规划与控制（模型只选策略，不碰几何）

| 节点 | 依赖 | 作用 |
|---|---|---|
| `repair_planner`\* | quality_gate, evidence_graph, defect_detect | 受约束选型：`{case_id, decision, strategy_id, target_ids, hypothesis, evidence_ids}`，几何参数一律不许带 |
| `llm_reason`\* | quality_gate, evidence_graph | 疑难分析：只收闭环 CaseBundle，只回闭集动作或 `NO_ACTION` |
| `validate_proposal` | quality_gate, llm_reason, evidence_graph | 提议校验：闭集动作 + 已存在的 target + 类型/区域/轮次预算 + 越权拦截，拒绝原因可审计 |
| `plan_compile` | quality_gate, repair_planner, validate_proposal, evidence_graph, defect_detect | 把"策略 + 证据 id"编译成**有依赖的局部子 DAG** |
| `adaptive_controller` | 16 个依赖 | 决定"先修哪个、用哪个策略、试几次、何时停"；**不决定**坐标/阈值/算子实现/是否提交。内部跑局部子 DAG + 影子验收 + 事务式提交 |
| `affected_recompute` | quality_gate, adaptive_controller | 只重算受影响区域（区域切片/回填是纯函数） |
| `finalize_patch` | adaptive_controller | 把被采纳的补丁落地 |
| `final_candidate_state` | finalize_patch, openings_select, walls_seg, columns, columns_verify, spaces_seed, adaptive_controller | **全链路唯一结果状态**（`walls/columns/doors/windows/virtual_edges/unresolved/provenance`），下游只读它 |
| `repair_audit` | final_candidate_state | 修复审计：每条推断几何的来源与独立证据 |
| `final_audit` | quality_gate, repair_planner, plan_compile, adaptive_controller, affected_recompute, repair_audit, defect_detect, space_routing | 终审：与闸门、规划、实际执行对账 |

### H. 构件成型与输出

| 节点 | 依赖 | 作用 |
|---|---|---|
| `walls_merge` | walls_seg, wall_model, doors, windows, openings_select, final_candidate_state | 跨洞口合并成连续墙实例（洞口不切墙） |
| `envelope` | walls_merge, load | 外墙轮廓 / 包围范围 |
| `steps` / `entrance_steps` / `apron` | load, texts, walls_merge | 台阶、出入口台阶、散水 |
| `room_labels` | texts, envelope | 房名候选挂靠（图层清单只给优先级，不当准入条件） |
| `rooms` | load, wall_model, spaces_seed, final_candidate_state, room_labels, label_filter, stairs, defect_detect, space_routing | **房间 = 闭合面域 + 挂靠标签**；一个面域多个标签按"栅格最近标签"剖分；复合名（兼/含/带/及…）不拆；退化时走共享面等分兜底；孤儿标签带最近面域距离上报，不静默丢弃 |
| `assemble` (MAIN) | 19 个依赖 | 汇总成构件清单 `components` + 未识别 + 未匹配块 |
| `export` (MAIN) | assemble, final_audit, adaptive_controller | 产出 `dwg-components-records/1.1` 单一 JSON 文档 |

### 拓扑序（实跑结果，确定性）

```
load → columns → grid → partitions → raw_evidence → texts → frame → north
→ room_labels_seed → label_filter → stairs → walls_seg → columns_verify → doors
→ spaces_seed → wall_model → windows → evidence_graph → opening_judge
→ openings_unify → openings_select → defect_detect → repair_options → quality_gate
→ llm_reason → repair_planner → space_cases → space_judge → space_routing
→ validate_proposal → plan_compile → adaptive_controller → affected_recompute
→ finalize_patch → final_candidate_state → repair_audit → final_audit
→ walls_merge → apron → entrance_steps → envelope → room_labels → rooms → steps
→ assemble → export
```

---

## 4. 数据契约

| 边界 | 形状 | 谁写 | 谁读 |
|---|---|---|---|
| `DrawingSnapshot` | 单位、范围、图元（含块展开后的几何与 handle）、文字、块表、图层统计 | `load` | 除 `load` 外全部节点 |
| `wall_model` | `{lines, spans, gaps, thickness, scale}` | `wall_model` | 洞口、面域、房间 |
| `openings_unify` | `{openings[], doors[], windows[], dropped, unplaced, shadowed, stats}` | `openings_unify` | `openings_select`、`walls_merge` |
| `final_candidate_state` | `{walls, columns, doors, windows, virtual_edges, faces, unresolved, provenance, calibration…}` | `final_candidate_state` | `walls_merge`、`rooms`、`assemble` |
| `components[]` | `{category, geometry, attributes, provenance(handle 列表)}` | `assemble` | `export`、`acceptance.py` |
| 输出文档 | `dwg-components-records/1.1`（`meta/basemap/components/unrecognized/unmatched_blocks/scope/notes`） | `export` + `schema.py` | 下游算量、`SKILL.md` 契约 |

所有边界都是 JSON 原生，`is_json_native()` 会在统计/缓存路径上校验——**tuple 混进输出会立刻暴露**，
这也是"节点可单独缓存、可单独回放"的技术前提。

---

## 5. 参数从哪来（`scale.py`）

识别器里**不允许**出现裸的毫米阈值（有单测强制，基线只能缩小）。

- **三个通用估计子**：`endpoint_noise`（图纸自噪声，近零簇上沿）、`mode_of`（众数，排序滑窗 O(n log n)）、`gap_split`（分布分两簇时取簇间切分点）。
- **五个基准量**：`thickness`（墙厚）、`door_w`（门宽）、`text_h`（字高）、`bay`（柱距/轴距）、`noise`（自噪声）。
- **唯一一张无量纲常数表 `STRUCTURE`**（12 项，每项都写明"为什么跨图纸成立"）：如 `gap_min_door=0.30`（窄于 0.3 个门宽的是家具缝）、`face_min_text=4.0`（约 (4 字高)²）、`tol_join_noise=2.0`。
- **两阶段测量**：`load` 用图元分布量初值；`wall_model` 用真实墙厚 + 墙缺口宽度精化（缺口宽度是**与画法无关**的门宽证据，比拿弧半径猜可靠）。
- **每个值带出处**：`provenance[k] = {value, source: measured|ratio|prior|default, estimator, n}`。

所以换一张图：基准量变、阈值跟着变，常数表一个字不改。

---

## 6. 模型介入边界

三条硬规则：

1. **模型没有几何**：所有坐标/区间/跨度/候选编号都是程序算好再给模型；模型只回 `{id, role, reason}` 或 `{strategy_id, target_ids}`。坐标、尺寸、容差、阈值字段一旦出现即整条提议被拒。
2. **模型不决定依赖与提交**：依赖顺序在 `dag.py` 里静态声明；是否提交由 `local_solver` 的影子验收与事务式提交决定。
3. **没有 key 也能跑完**：每个 `llm_*` 节点在无 key / 输出不合规 / 超预算时降级为确定性策略，节点状态如实记录（`method` 字段会写出"模型介入到哪一步"）。

模型缓存一律**按图内容哈希 + 模型版本 + 算子版本**做键（`layer_roles.py` 注释写明旧版只按 layer 做键导致跨图污染）。识别结果本身不落盘缓存。

---

## 7. 怎么跑

```bash
# 1) 单张图，全量识别（默认目标 assemble）
.dsh/venv/bin/python3 .dsh/skills/dwg-drawing-reader/scripts/identify_components.py 办公楼.dxf --format markdown

# 2) 编排 API：并行 4、带统计、缓存到目录
.dsh/venv/bin/python3 - <<'PY'
import sys; sys.path.insert(0, ".dsh/skills/dwg-drawing-reader/src")
from dwg_reader.recognition.pipeline import run_recognition
run = run_recognition("办公楼.dxf", jobs=4, targets=("assemble",),
                      cache_dir="work/dag-cache", collect_stats=True)
print(run.summary())
for s in sorted(run.stats, key=lambda s: -s.duration_ms)[:5]:
    print(s.name, round(s.duration_ms, 1), s.status)
PY

# 3) 只跑一段（单点调试）：只要柱的复核结果
run_recognition("办公楼.dxf", targets=("columns_verify",)).outputs["columns_verify"]

# 4) 回归基线：改代码前后逐构件对比（图清单在 tests/baseline/drawings.json）
python3 scripts/baseline.py --write      # 改代码**之前**跑，记录当前行为
python3 scripts/baseline.py --check      # 改完跑，有差异即 exit 1
python3 scripts/baseline.py --check --category door

# 5) 评估 / 验收 / 扰动
python3 scripts/recognize_eval.py run --drawing 办公楼.dxf --gt gt/office.json
python3 scripts/acceptance.py --truth gt/office.json gt/tdw.json     # exit 1 = 有类别未达标
python3 scripts/recognize_eval.py invariance --drawing 办公楼.dxf --ops rename_layers scale
```

缓存目录建议放在工作区（`work/dag-cache`），清空用 `dag.clear_cache(cache_dir)`。

---

## 8. 评估与验收

| 手段 | 回答的问题 | 为什么不能用别的替代 |
|---|---|---|
| `evaluation.py` + `gt/*.json` | 准确率（候选召回 / 最终精度 / 几何拓扑三层） | 自身口径的统计（"confirmed 61.9%"）无法回答"这条规则提升了准确率，还是只把数字改大了" |
| `acceptance.py` | 交付口径达标 | 真值带**口径说明**（数的是什么），区间目标与精确目标分开 |
| `perturb.py` + 不变性 | **泛化** | 准确率可以靠对单张图调参刷上去；"改图层名/换单位/旋转/切碎后结果不变"刷不上去 |
| `verify.py` | 识别质量（覆盖率、孤儿率、分布离群） | 全部容差取自本图（墙厚中位数），不写死绝对阈值 |

当前基线（两台真图，确定性模式）：办公楼 187 个构件（墙31/柱38/门30/窗32/房21），
测试-二维DWG 491–519 个构件（墙298/柱67/门62–66/窗27/房26–50，房间与门数依赖是否启用模型标签过滤）。

---

## 9. 测试

```bash
cd .dsh/skills/dwg-drawing-reader && ../../venv/bin/python3 -m pytest tests -q
# 279 passed, 3 skipped
```

`tests/test_generalization.py` 是这套架构的"宪法"，30 项左右，分四类：

- **尺度不变**：阈值随图缩放（基准量 ×2 → 阈值 ×2、面积 ×4）、每项测量都有出处、`STRUCTURE` 全无量纲、容差跟自噪声走。
- **不许新增裸毫米阈值**：正则扫全包大写常量，对照冻结基线 `bare_thresholds_baseline.json`，**只许变小**。
- **结构谓词而非调参距离**：双扇弧合成一个洞、两个不同块仍是两樘门、窄洞被丢、无墙可锚的块留下不删。
- **房间是面域不是洪水**：一个面域两个标签 → 一房多别名；两个真房间在一个面域 → 按标签剖分；复合名不拆；同名相邻合并；孤儿标签上报不丢弃。

`tests/test_dag.py` 钉住节点数、依赖完整性、PURE 节点不得访问 live 文档、拓扑确定性。

---

## 10. 已知限制（诚实清单）

| 限制 | 现象 | 性质 |
|---|---|---|
| 块全部炸开跑不完 | 进程被系统 `Killed: 9`（内存/资源爆炸，不是死循环） | 真实缺陷：缺规模预算与降级路径 |
| 旋转图纸漂移 | 旋转 90° 后墙 31→38（+22.6%）、房 21→20、窗 32→35 | 真实缺陷：`walls.py` 面线配对仍按 H/V 分桶；斜向配对已修，90° 翻转未修 |
| 图层改名后图框/楼梯消失 | 47 个图层加前缀后 图框 2→0、楼梯 2→0、房间 21→23 | 真实缺陷：这两个识别器仍依赖图层名 |
| 大图墙数偏多 | 309（参考 297–303），面板线与墙线区分不彻底 | 口径/缺口：墙按"段"计数，遇洞口/转角就断 |
| 大图房间仍偏少 | 41（参考 45–60） | 部分解决：26→36（房名层识别）→41（房名取并集），仍差面域剖分 |
| 单图层图纸墙数无法收敛 | 687 段候选 → 194 墙；候选池 757 条里真墙只占 19% | 真实缺陷：标注链/轴网/家具与墙厚同量级，"分隔空间"检验召回仅 41%，见 D-2 |
| 单图层图纸只能给出候选 | 194 墙 / 57 柱全部标 `needs_review`，窗 0（无可独立验证的窗块或宿主墙证据） | 已知保守：同一层里家具、尺寸、装饰线混在一起，不做几何上下文验证不敢升 confirmed |
| 陌生窗块靠块内几何验证 | 会所 `$WinLib2D$` 58 樘窗中 45 樘 `needs_review`（宿主墙未确认） | 部分解决：窗 1→58，可用性取决于宿主墙复核 |
| 小面积房间只标疑点 | 会所 8 间房里 4 间 <1 m²，带 `area_warning` | 已知：面域剖分未修，先标记不装准 |
| 房名与面积标注对不上 | 测试图2：13 间带面积的房只有 5 间误差 ≤10%（中位数 77.5%） | 真实缺陷：面域切分/标签挂靠错，房间几何不可信 |
| 洞口落位率 ~90% | 办公楼 12 / 大图 21 个候选找不到可放置墙段 | 缺口：跨两段墙、无墙可锚的处理 |
| 弧墙只认模型空间 ARC | 用 LWPOLYLINE **凸度(bulge)** 画的弧形墙/块内弧墙不认（`segments_from` 不读 code 42，ARC 也不进块展开） | 真实缺陷：下一步应把 bulge 拆成真弧 |
| 幕墙分格（竖挺/格数）未统计 | 只给出带的位置、厚度与长度，不给"几格、每格多宽" | 已知缺口：分格的画法各图差异大，尚无稳定判据 |
| 幕墙上的开启扇不单独成窗 | 幕墙带上的可开启部分不拆成"窗" | 已知保守：拆扇需要幕墙分格，见上一条 |
| 弧墙不给洞口 wall_id | 曲线墙上的门窗按"无宿主"处理，不建立归属 | 已知保守：弧上投影需要参数化角度坐标，暂不做 |
| 模型缓存落在当前工作目录 | 换目录跑可能得到不同结果（同一张会所：工作区根跑柱 88/房 4，技能目录跑柱 59/房 8） | 工程缺陷：应显式指定缓存根 |

---

## 11. 扩展须知

**加一个节点**：在 `nodes.py` 写 `def node_xxx(ctx): ...` → 在 `build_recognition_dag()` 里 `Node("xxx", node_xxx, deps=(...))` → 若下游需要就加进它的 `deps` → 跑 `tests/test_dag.py`（节点数、依赖、PURE 守卫都会帮你检查）。

**加一个阈值**：不许写毫米数。要么在 `scale.py` 的 `STRUCTURE` 里加一条无量纲倍率并写明"为什么跨图纸成立"，要么用已测基准量相乘；否则 `test_no_new_bare_thresholds` 会失败（这是刻意的）。

**加一种构件**：先在 `taxonomy.py` 的闭集里登记，再在 `elements.py` 产候选，最后在 `assemble`/`export` 里给出 geometry 与 provenance。**不要**在识别里直接产出最终实体——先候选、再证据、再接受。

**改模型提示词**：只改"选项集合的表述"，不要让它返回几何；`validate_proposal.py` 会拒绝任何几何字段。

---

## 12. 版本

`dwg-reader-skill 0.7.0`、缓存协议 `dwg-dag-cache/1`、输出文档 `dwg-components-records/1.1`、结果状态 `final-candidate-state/1.1`。

近期架构提交：

```
2ac6c99 扰动工具自检 + 单位换算三处漏项 + 众数 O(n²) 卡死
5d5f1c3 参数统一推导 —— 三个通用估计子 + 唯一结构常量表 + 出处可查 + lint 强制
288ffe2 真值文件入库 + 口径说明，验收可脱离工作区复现
7976529 块展开几何参与识别 + 图层名归一化对齐（不变性检验驱动）
873f62d 泛化架构改造 —— 图内自校准尺度、统一洞口流水、面域优先房间、柱两阶段
517452f 两阶段墙验证、面域双轨、构件融合、跨图评估与扰动工具
```
