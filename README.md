# DWG Reader Skill

读取 DWG/DXF 工程图纸，提取图层、实体、文字标注、尺寸信息，支持中英文及多语言编码，输出结构化解析报告供阅读理解。

当前版本 **0.7.0**（变更记录见 [SKILL.md](SKILL.md#版本与变更记录)）。

## 核心能力

- **DWG → DXF 转换**：通过 ODA File Converter 命令行批量转换
- **一站式流水线**：`dwg_read.py` 自动转换 + 结构化报告 + 文字提取，输出 Markdown
- **图纸解析**：实体清单（LINE/CIRCLE/ARC/TEXT/MTEXT/DIMENSION 等）、文字提取、图层信息、尺寸信息
- **块内文字与属性**：INSERT 自动展开（含缩放/旋转/镜像变换）、ATTRIB 属性提取、匿名块主体文字直提
- **表格还原**：`--table` 按几何对齐还原 Markdown 表格（门窗表/材料表）
- **阅读顺序**：几何感知排序（先分栏、栏内上→下左→右），适配多栏图纸
- **按图层聚合**：`--by-layer` 按专业图层分组输出
- **MTEXT 堆叠**：`\S`（分数/公差）解码为 `top/bottom`，剥离 `\H` 高度控制码
- **全字体支持**：内置 322 个 SHX 字体（约 159MB），覆盖中文 GBK、韩文 cp949、日文 shift_jis、西文 unifont
- **自动字体匹配**：解析 DXF 的 STYLE 表，按文本样式自动匹配字体库（支持路径前缀/大小写/后缀容错），无需手动指定
- **符号支持**：AutoCAD `%%` 转义序列（%%C→Ø、%%D→°、%%P→±）、`\U+XXXX` Unicode、`\M+5XXXX` bigfont 符号区
- **SHX 反编译**：大字体 SHX → SHP 文本（等价 DUMPSHX / shx2shp 工具）
- **低内存提取**：大 DXF 逐行流式解析，避免 OOM
- **损坏文件容错**：缺 EOF / 空值 / 嵌套 SECTION / 无 ENTITIES 段等不规范 DXF 均不丢字
- **尺寸标注精读**：`--dimensions` 按类型（线性/对齐/角度/半径/直径/坐标/弧长）分类，关联最近几何，输出表格 + JSON
- **构件识别**：`--components` 按原生图元识别图框/轴网/指北针/柱/墙/门窗/房间/楼梯/台阶/散水等 12 类构件的**逐个实例**，输出 `dwg-components-records/1.1` 统一文档
- **连续墙 run**：同一墙线上被门窗洞口断开的墙段合并为一道连续墙，洞口记 `attributes.openings`（handle/span/type_code）；门窗 `wall_id` 指向连续墙，房间/台阶/散水仍用未合并墙段
- **识别流程 DAG**：构件识别内部为 46 节点有向无环图（43 PURE + 3 MAIN），说明见 [`src/dwg_reader/recognition/README.md`](src/dwg_reader/recognition/README.md)；`run_recognition()` 支持 `jobs` 并行、`targets` 部分执行、内容寻址缓存与逐节点耗时统计，`recognize()` 行为不变
- **图层智能过滤**：`--layer-filter "WALL|BEAM"` + `--layer-alias 别名.json`，仅保留匹配图层的内容
- **批量处理**：`--batch <目录> --out-dir <目录>` 一次处理多张图纸，生成总览 index.md
- **可选 CAD 后端**：`dwg-reader doctor` 探测 ODA、LibreDWG、OpenSCAD、OpenCAD（Windows/macOS/Linux 自动扫描安装路径）
- **OpenSCAD 桥接**：二维 DXF 按高度/图层拉伸并导出 STL、3MF、OFF
- **MCP 服务**：通过 stdio MCP 暴露诊断、文字提取、结构检查、一站式阅读、算量与显式网格导出
- **算量清单导出**：构件数量 + 图层几何量（直线/多段线总长、圆/弧/文字数）→ CSV（Excel 友好）或 XLSX
- **工程化 CLI**：`dwg-reader read` 一站式 Markdown/JSON 报告，`dwg-reader quantity` 算量导出，无需手写脚本
- **零依赖内核**：流式 DXF 解析器仅用标准库，未装 ezdxf 也能出完整报告
- **天正（TCH）图纸检测**：自动识别 `TCH_*` 自定义对象与代理实体，报告顶部告警并给出「图形导出/T3」处理建议；支持 `.t3` / `.dwt` 输入

## 环境依赖

| 组件               | 说明                                                                                                                                                                               |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ODA File Converter | DWG→DXF 转换；Windows 装到 `C:\Program Files\ODA\ODAFileConverter <版本>\ODAFileConverter.exe`，macOS 为 `.app` 包，Linux 一般在 `/usr/bin`；也可用 `DWG_READER_ODA_PATH` 显式指定 |
| Python 3.10+       | 运行脚本与 CLI                                                                                                                                                                     |
| ezdxf（可选）      | `pip install 'dwg-reader-skill[dxf]'`；仅 `inspect`/`text --expand-blocks` 与旧脚本需要                                                                                            |
| openpyxl（可选）   | `pip install 'dwg-reader-skill[xlsx]'`；仅 XLSX 导出需要                                                                                                                           |
| SHX 解析器         | 内置 `src/dwg_reader/fonts.py`（自研，仅依赖标准库）                                                                                                                               |
| OpenSCAD / OpenCAD | 可选本地后端，使用 `dwg-reader doctor` 探测                                                                                                                                        |

## 快速开始

### 0. 一站式阅读（推荐）

```bash
pip install -e ".[dxf]"        # Windows/macOS/Linux 通用

# 工程化 CLI（推荐，等价能力，输出更规整）
dwg-reader read 图纸.dwg                    # Markdown 报告到 stdout
dwg-reader read 图纸.dxf -o 报告.md          # 写入文件
dwg-reader read 图纸.dxf --table             # 表格还原
dwg-reader read 图纸.dxf --by-layer          # 按图层聚合
dwg-reader read 图纸.dxf --dimensions        # 尺寸标注精读
dwg-reader read 图纸.dxf --components        # 构件识别
dwg-reader read 图纸.dxf --layer-filter "WALL|BEAM"
dwg-reader read 图纸.dxf --format json       # 机器可读

dwg-reader read --batch ./drawings --out-dir ./reports   # 批量 + index.md
dwg-reader read --batch ./drawings --out-dir ./reports --recursive

dwg-reader quantity 图纸.dxf -o 算量.csv      # 构件+图层几何量+尺寸
dwg-reader quantity 图纸.dxf -o 算量.xlsx --sections components,layers

dwg-reader doctor                             # 探测 ODA / ezdxf / OpenSCAD
dwg-reader doctor --format json
```

```bash
# 脚本入口（无需 pip 安装，功能等价）
python3 scripts/dwg_read.py 图纸.dwg --out 报告.md
python3 scripts/dwg_read.py 图纸.dxf --table          # 表格还原
python3 scripts/dwg_read.py 图纸.dxf --by-layer       # 按图层聚合
python3 scripts/dwg_read.py 图纸.dxf --dimensions     # 尺寸标注精读
python3 scripts/dwg_read.py 图纸.dxf --components     # 构件识别
python3 scripts/dwg_read.py 图纸.dxf --layer-filter "WALL|BEAM"  # 按图层过滤
python3 scripts/dwg_read.py --batch ./drawings --out-dir ./reports  # 批量
```

### 0.1 作为 Python 库调用

```python
from dwg_reader import ReadOptions, read_drawing, build_quantity

result = read_drawing("图纸.dxf", ReadOptions(dimensions=True, components=True))
print(result.markdown)          # Markdown 报告
print(result.payload["texts"])  # 结构化文字

quantity = build_quantity("图纸.dxf")
quantity.sheets()               # {"components": [...], "layers": [...], "dimensions": [...]}
```

### 1. 转换 DWG → DXF

```bash
# macOS
ODA="/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter"
# Windows
# ODA="C:/Program Files/ODA/ODAFileConverter 26.4.0/ODAFileConverter.exe"
# Linux
# ODA="/usr/bin/ODAFileConverter"

mkdir -p out
"$ODA" input_dir out ACAD2018 DXF 0 0 "*.dwg"

# 或交给 CLI 自动探测并转换
dwg-reader convert 图纸.dwg ./out
```

### 2. 提取文字（自动匹配字体）

```bash
python3 scripts/extract_texts_stream.py 图纸.dxf out.txt
# 输出: 自动匹配 N 个大字体
```

### 3. 解析实体/图层/尺寸

```bash
python3 scripts/parse_dxf.py 图纸.dxf --entities --texts --layers
```

### 4. 显式指定字体

```bash
python3 scripts/extract_texts_stream.py 图纸.dxf out.txt --font fonts/gbcbig.shx
```

### 5. SHX 反编译为 SHP

```bash
python3 scripts/shx_decompile.py fonts/gbcbig.shx /tmp/gbcbig.shp
```

## 识别流程 DAG（编排 API）

构件识别内部是一条 **46 节点有向无环图**（43 个 PURE + 3 个 MAIN），代码在
`src/dwg_reader/recognition/`。`recognize()`、`dwg-reader components`、MCP 与 render
仍走默认路径；需要并行、部分执行、缓存或逐节点耗时时用 `run_recognition()`。

```text
load(MAIN) ─ texts / walls_seg / columns / partitions / grid / frame / north / stairs
           ─ raw_evidence ─ defect_detect ─ repair_options ─ evidence_graph ─ quality_gate
           ─ openings_unify ─ openings_select ─ adaptive_controller
           ─ final_candidate_state ─ final_audit
           ─ walls_merge / envelope / rooms ─ assemble(MAIN) ─ export(MAIN)
```

**完整的 46 节点清单、依赖表、数据契约、参数推导（图内自校准尺度）与已知限制见
[`src/dwg_reader/recognition/README.md`](src/dwg_reader/recognition/README.md)**——
该文件是识别 DAG 的唯一权威说明，本 README 只保留调用面。

```python
from dwg_reader.recognition import run_recognition, clear_cache

run = run_recognition("图纸.dxf", jobs=4)                  # 并行；输出与 recognize() 相同
run = run_recognition("图纸.dxf", targets=("rooms",))       # 部分执行：目标 + 上游闭包
run = run_recognition("图纸.dxf", targets=("export",))      # 生成 dwg-components-records/1.1 文档
run = run_recognition("图纸.dxf", jobs=4, cache_dir=".cache/dwg-dag")  # 内容寻址缓存

run.summary()          # total_ms / nodes / ok / cached / failed / skipped / slowest / warnings
for stat in run.stats: # 逐节点：status、duration_ms、thread、cache_hit、output_summary
    print(stat.name, stat.status, round(stat.duration_ms, 1))

clear_cache(".cache/dwg-dag")                 # 清全部缓存，返回删除条目数
clear_cache(".cache/dwg-dag", node="rooms")   # 只清单个节点
```

| 入口 | 行为 |
| --- | --- |
| `recognize(path)` | 兼容门面：`jobs=1`、无缓存、跑到 `assemble`，返回 `{meta, components, unrecognized}`；签名与输出不变 |
| `run_recognition(path, ...)` | 编排 API：返回 `DagRun`（`output` / `stats` / `cache_hits` / `warnings`），支持 `jobs`、`targets`、`cache_dir`、`collect_stats`、`fail_fast`、`raise_on_error` |

- **缓存**：`<cache_dir>/dwg-dag-cache-v1/<node>/<key>.json`，39 个可缓存节点；键含文件 SHA-256、
  上游输出摘要、节点版本与 `recognition/*.py` 源码指纹（代码改动自动失效）。损坏条目 miss 重算，
  不可写目录降级为无缓存并记 warning。
- **并行安全**：`load` 在主线程用 ezdxf 解析并产出 JSON-native 快照；PURE 节点只读纯数据
  （访问 `ctx.doc` / `ctx.msp` 直接抛 `RuntimeError`），MAIN 节点固定编排线程，故 `jobs=1/4` 输出逐条一致。
- **异常**：失败节点 `failed`，下游 `skipped(upstream_failed:*)`；`fail_fast` 时其余 `skipped(aborted)`；
  默认抛 `DagExecutionError`（`.cause` 为原始异常），`raise_on_error=False` 时返回 `DagRun`。

## 构件识别与墙体语义（0.7.0）

构件识别按原生图元输出 12 类构件的逐个实例（`dwg-components-records/1.1`）。
其中**墙**按连续墙 run 输出，不再被门窗洞口切成多段：

- **合并条件**：同一墙线（`orient` + 中心线，0.1mm 分组）+ 缺口被单樘门/窗洞口覆盖（容差 120mm）
  + 缺口 ≤3000mm + 厚度差 ≤2mm + 合并后 ≥250mm。
- **`geometry`**：`type="wall_run"`，`start`/`end`/`thickness`/`length`/`outline` 为整道连续墙；
  `segments` 保留原子墙段（有序无重叠），`segment_count` 为段数。
- **`attributes.openings`**：`{category, id, width, span, type_code, handle, layer}`；
  `source_entities` 含洞口 Handle，`evidence.detail` 写“跨 N 个门窗洞口连续”。
- **引用**：门窗 `wall_id` 指向所在 wall run；schema 文档重编号（`WALL-0001…`）后
  同步重映射 `wall_id` 与 `openings[].id`，交叉引用 50/50 + 40/40 全部命中。
- **兼容**：房间/台阶/散水仍用未合并墙段（洞口处断开），本图 23/3/1 与基线一致。

```bash
python3 scripts/identify_components.py 图纸.dxf --format json -o out.json
python3 - <<'PY'
import json
doc = json.load(open("out.json", encoding="utf-8"))
walls = [c for c in doc["components"] if c["category"] == "墙"]
print("墙", len(walls), "洞口", sum(len(w["attributes"]["openings"]) for w in walls))
PY
# 办公楼.dxf: 墙 31 洞口 40（71 段原子墙 → 31 个 wall run）
```

- **真值对照**（72 段人工标注）：中心代表点覆盖 **72/72 = 100%**；沿墙长度覆盖 ≥0.9 为
  **66/72 = 91.7%**（基线 71 段时为 63/72 = 87.5%）；6 段低值均为真值框含柱位/转角外缘。
- **已知限制**：洞口 >3000mm、走廊/无洞口缺口、不同墙线或厚度差 >2mm 不合并；
  单线墙/块内墙不识别；无 opening 几何的门不参与合并；非 WALL 中心线门 `wall_id=null`（本图 11 个）；
  `evidence.detail` 用组件层洞口编号，结构化 `openings[].id` 才是 schema 记录 id。

## 天正（TCH）图纸与 T3 格式

天正建筑/结构把墙、门窗、轴号、标注等存为自定义对象（`TCH_*` 类）。
DWG→DXF 转换后这些对象变成 **`ACAD_PROXY_ENTITY` 代理实体**，只保留显示缓存，
**文字内容不可恢复**。本工具会自动检测并告警：

```
> ⚠️ 天正图纸警告：天正对象未展开：3784 个代理实体/对象无法解析（占 87%），
> 天正文字、轴号、门窗编号、尺寸等可能大量缺失。
```

报告末尾的「天正图纸检测」章节会列出受影响对象（`TCH_TEXT`、`TCH_AXIS_LABEL`、
`TCH_DIMENSION` 等）与处理建议。JSON 输出对应 `tianzheng` 字段。

**让天正图纸变得可读**（任选其一）：

1. 天正中打开原图 →「文件布图 → 图形导出」，格式选 **AutoCAD/T3**（会把天正对象炸开为基本实体）；
2. 在 CAD/天正中 `EXPLODE`（分解）天正对象后另存为 DXF；
3. 用天正批量导出为低版本 DWG，再交给本工具。

`.t3` / `.dwt` 文件本质是 Autodesk DWG 容器，`dwg-reader read 图纸.t3` 可直接处理。
若天正对象未炸开，同样会得到上述告警。

## 字体库
`fonts/` 目录内置 322 个 SHX 字体，含 `index.json` 索引（字体名 → 类型/编码/shape 数）。

| 编码      | 数量 | 说明                                |
| --------- | ---- | ----------------------------------- |
| gbk       | 198  | 中文简体（gbcbig/hztxt/tssdchn 等） |
| unicode   | 88   | unifont 西文字体（tssdeng 等）      |
| cp949     | 28   | 韩文（KORdansun 等）                |
| shift_jis | 6    | 日文（hsa 等）                      |
| ascii     | 2    | 纯 ASCII                            |

## 解码原理

DXF 文本中字符的三种存储方式：

| 类型     | 存储方式   | 解码方式             |
| -------- | ---------- | -------------------- |
| 纯 ASCII | 直存       | 原样输出             |
| 西文字体 | `\U+XXXX`  | `chr(int(XXXX, 16))` |
| 中文字体 | `\M+5XXXX` | SHX 字体映射表       |

关键结论：bigfont 索引表 shape number = 该字体的字符编码。DXF 中 `\M+5BAA3` 去掉前导 `5` 后为 `0xBAA3` = GBK 码 = 「海」。

## 目录结构

```
dwg-reader-skill/
├── SKILL.md              # 技能说明（NVIDIA 风格范式）
├── AGENT.md             # 面向 AI Agent 的操作手册
├── README.md             # 本文档
├── src/dwg_reader/       # 可安装包（pip install -e .）
│   ├── dxf.py            # 流式 DXF 模型解析（零依赖，容错损坏文件）
│   ├── text.py           # \U+ / \M+5 / %% / \S 堆叠解码
│   ├── fonts.py          # SHX 字体索引与自动匹配
│   ├── layout.py         # 阅读顺序、分栏、表格还原
│   ├── dimensions.py     # 尺寸标注精读 + 关联几何
│   ├── components.py     # 旧版构件规则匹配（兼容保留）
│   ├── recognition/      # 实例级构件识别（DAG 编排）
│   │   ├── dag.py        # DAG 执行器：拓扑/并行/部分执行/统计/缓存
│   │   ├── nodes.py      # 20 个识别节点与依赖注册
│   │   ├── snapshot.py   # MAIN 线程解析 → JSON-native 快照
│   │   ├── pipeline.py   # recognize() 兼容门面 + run_recognition()
│   │   ├── base.py / walls.py / elements.py / rooms.py  # 识别规则
│   │   ├── export.py     # dwg-components-records/1.1 文档
│   │   └── schema.py     # schema 校验
│   ├── pipeline.py       # 一站式报告 + 批量
│   ├── quantity.py       # 算量清单（CSV/XLSX）
│   ├── backends.py       # 跨平台 ODA/LibreDWG/OpenSCAD 探测
│   ├── cli.py            # dwg-reader 命令
│   └── mcp_server.py     # MCP stdio 服务
├── schema/               # dwg-components-records-1.1.schema.json
├── tests/                # pytest（含 test_dag.py / test_dag_executor.py / test_wall_runs.py）
├── .github/workflows/ci.yml  # CI：lint / test / core / dag / package
├── scripts/              # 免安装脚本入口（与包能力等价）
│   ├── dwg_read.py           # 一站式流水线（推荐入口）
│   ├── parse_dxf.py          # 结构化解析（实体/文字/图层/尺寸/块，支持 --layer-filter）
│   ├── extract_texts_stream.py # 流式文字提取（低内存，自动字体匹配，支持图层过滤）
│   ├── extract_texts.py      # 文字提取（ezdxf，块属性展开）
│   ├── extract_dimensions.py # 尺寸标注精读（按类型分类 + 关联几何，支持 --json）
│   ├── identify_components.py # 构件识别（实例级 12 类，--format json|markdown|files）
│   ├── component_rules.json  # 旧版规则表（兼容保留）
│   ├── shxfont.py            # SHX 大字体解析器（多编码）
│   └── shx_decompile.py      # SHX → SHP 反编译
└── fonts/               # 322 个 SHX 字体 + index.json
```

## License

MIT
