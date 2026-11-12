# AGENT.md — 面向 AI Agent 的使用说明

本文件供 **AI Agent**（而非人类终端用户）调用本 skill 时参考。它定义了触发条件、
唯一入口命令、输入输出契约、决策树与已知陷阱。面向人类的完整说明见 `SKILL.md` /
`README.md`，本文件是其"操作手册"精简版。

---

## When to Use

当用户意图满足以下之一时，调用本 skill：

- 提供 `.dwg` / `.dxf` 文件路径，并要求"读取 / 看 / 理解 / 提取 / 分析"图纸。
- 要求提取图纸的**文字标注、尺寸、图层、表格**（门窗表 / 材料表）、**构件**（门/窗/梁/柱）。
- 要求**批量处理**多个 DWG/DXF、或**按图层筛选**某一专业内容。
- 要求把 DWG 转为可读文本 / DXF。
- 关键词：DWG、DXF、CAD 图纸、工程图、图纸标注、图层、看图、算量、构件。

## Do Not Use

- **不要**用于"画一张图 / 生成 CAD / 修改几何"——本 skill 是只读解析器，不回写文件。
- **不要**把 OpenSCAD 拉伸当作完整 DWG 渲染；只有用户明确要求闭合 DXF 轮廓的
  3D 网格导出时才使用 `dwg-reader openscad`。
- **不要**用于非 CAD 的 PDF/图片图纸——那是 OCR 任务，不在本 skill 范围。
- **不要**用于 3D 实体 CAD→仿真资产——那是 NVIDIA `usd-convert-cad` 的定位，本 skill 专注 2D 工程图。

---

## 唯一推荐入口

```bash
python3 <skill目录>/scripts/dwg_read.py <图纸绝对路径> [选项]
```

- `<skill目录>` 即本 AGENT.md 所在目录（即 `dwg-drawing-reader/`）。
- DWG 会自动经 ODA 转为同目录 DXF 后再解析；DXF 直接解析。
- 默认把结构化 Markdown 报告打印到 **stdout**；用 `--out 报告.md` 写入文件（进度在 stderr）。

### 选项速查

| 选项                      | 作用                      | 何时用                             |
| ------------------------- | ------------------------- | ---------------------------------- | ------------------------------ |
| `--out 报告.md`           | 写文件而非 stdout         | 报告较长、需留档时                 |
| `--table`                 | Markdown 表格还原对齐网格 | 提取门窗表 / 材料表 / 明细表时     |
| `--by-layer`              | 按图层分组输出文字        | 想按专业（建筑/结构/水电）分读时   |
| `--no-convert`            | 跳过 DWG→DXF              | 已知源为 DXF、想加速时             |
| `--font 字体.shx`         | 显式指定大字体            | 自动匹配明显出错时（罕见）         |
| `--dimensions`            | 追加「尺寸标注精读」章节  | 要按类型分类尺寸、关联被标注几何时 |
| `--components`            | 追加「构件识别」章节      | 要清点门/窗/梁/柱等构件时          |
| `--layer-filter "A        | B"`                       | 按图层名关键词/正则过滤            | 只关注某图层（如 WALL/BEAM）时 |
| `--layer-alias 别名.json` | 中文词→实际图层映射       | 用户用中文说"墙/梁"时              |
| `--batch <目录>`          | 批量模式                  | 一次处理目录下所有 .dwg/.dxf       |
| `--out-dir <目录>`        | 批量输出目录              | 配合 `--batch` 必填                |

---

## Decision Tree（Agent 应如何选择）

```
收到图纸路径 + 读取意图
   │
   ├─ 整图概览（文字+图层+实体+尺寸）          → read（默认）
   ├─ 表格类内容（门窗表/材料表）              → read --table
   ├─ 按专业/图层分读                          → read --by-layer
   │                                            或 --layer-filter "WALL|BEAM"
   ├─ 尺寸标注精读（分类+关联几何）            → read --dimensions
   ├─ 构件清点（门/窗/梁/柱…）                 → read --components
   ├─ 算量清单（导出表格）                    → quantity -o 算量.csv
   ├─ 多张图纸                                → read --batch <目录> --out-dir <目录>
   ├─ 程序化消费                              → read --format json
   └─ 纯文字列表/超低内存/超大文件             → scripts/extract_texts_stream.py
```

> 经验法则：**先 `dwg-reader read` 默认跑一次**，看输出是否够用；不够再叠加选项，或换底层脚本。
> **不要**一上来手动调 ODA 或 ezdxf。

---

## Prerequisites

| 组件               | 状态          | 备注                                                                                                       |
| ------------------ | ------------- | ---------------------------------------------------------------------------------------------------------- |
| Python 3.10+       | 已安装        | 系统自带                                                                                                   |
| ezdxf              | 可选          | 仅 `inspect` / `text --expand-blocks` 需要；`read`/`quantity` 零依赖                                       |
| ODA File Converter | 处理 DWG 需装 | Windows `C:\Program Files\ODA\...`、macOS `.app`、Linux `/usr/bin`；自动探测，也可设 `DWG_READER_ODA_PATH` |
| `fonts/` 字体库    | 已内置        | 322 个 SHX + `index.json`，自动匹配                                                                        |

> `read` / `quantity` 仅依赖标准库；ezdxf 与 ODA（仅 DWG 时）为可选增强。

## Supported Formats

| 格式                     | 输入 | 备注                                    |
| ------------------------ | ---- | --------------------------------------- |
| `.dxf`                   | ✅   | 直接解析                                |
| `.dwg`                   | ✅   | 自动 ODA 转 DXF 后解析（同目录落 .dxf） |
| 3D 实体（.step/.dgn 等） | ❌   | 超出本 skill 范围                       |

## Platform Support

| 能力            | macOS arm64 | Linux x86_64   | Windows x86_64 |
| --------------- | ----------- | -------------- | -------------- |
| 解析 DXF        | ✅          | ✅             | ✅             |
| DWG→DXF（ODA）  | ✅          | ✅（需装 ODA） | ✅（需装 ODA） |
| 字体解码（SHX） | ✅          | ✅             | ✅             |

## Implementation Contract

- **返回码**：`dwg-reader` 与 `dwg_read.py` 返回 `0` 成功，非 0 失败；外部调用应分支于返回码。
- **输出**：默认 Markdown 到 stdout；`-o` / `--out` 写文件，进度到 stderr。
- **降级**：流式解析器容忍缺 EOF / 多余 SECTION / 空值；只有完全无实体时才报错。
  （`inspect` 等 ezdxf 路径仍可能在损坏文件上报错，此时改用 `read`。）
- **DWG 副作用**：处理 `.dwg` 会在同目录落同名 `.dxf`（预期行为，非报错）。
- **JSON 模式**：`dwg-reader read --format json` 输出机器可读 JSON（`drawing`/`texts`/`dimensions`/`components`）；
  `extract_dimensions.py --json` / `identify_components.py --json` 同样可用。
- **零依赖**：`read` / `quantity` 不需要 ezdxf；只有 `inspect` 与 `text --expand-blocks` 需要。

---

## 输出契约（Agent 如何消费结果）

`dwg-reader read` 的 stdout 是一份 Markdown 报告，含以下小节：

1. **图纸概览**：DXF 版本、实体数、实体类型统计、图层表（颜色/线型/状态）、块定义表。
2. **文字标注提取**：按几何阅读顺序（上→下、左→右、分栏）排列的全部 TEXT/MTEXT/ATTRIB；
   已自动解码 BigFont/Unicode/`%%`/MTEXT 堆叠。
3. **尺寸标注精读**（仅 `--dimensions`）：按类型分类 + 关联几何的表格。
4. **构件识别**（仅 `--components`）：按类型聚合的构件清单（数量/图层/示例块名/位置范围）。

JSON 模式字段：`drawing`（结构摘要）、`texts`（text/x/y/layer/kind）、
`text_count`、`dimensions`（count/by_type/dimensions[]）、`components`（total_components/by_type[]）。

Agent 应直接使用报告内容。若仍残留 `\` 开头的 MTEXT 控制码，用正则
`re.sub(r'\\[A-Za-z][^\\;]*;|\\[A-Za-z]', '', s)` 清理。

---

## 已知陷阱与注意事项

1. **绝对路径**：`<file>` 传绝对路径，相对路径可能因 cwd 出错。
2. **DWG 生成同目录 DXF**：预期行为，非报错；转换约数秒，需等待进程退出。
3. **ODA 命令行模式**：带完整参数时不弹窗；手动调 ODA 易出错——统一用 `dwg_read.py`。
4. **字体自动匹配**：默认无需 `--font`；中文全乱码时试 `--font fonts/gbcbig.shx`。
5. **超大文件（>100MB）**：优先流式提取或 `dwg_read.py`（内部降级），避免全量加载。
6. **不规范 DXF**：ezdxf 失败自动降级流式，且不丢未引用块内文字；一般结果仍可用。
7. **坐标无单位**：输出原始坐标值，比例关系需结合尺寸标注判断，勿擅自假设 mm/m。
8. **默认不渲染**：默认只输出文本。OpenSCAD 是可选的显式导出后端，仅处理适合
   拉伸的二维闭合轮廓。
9. **图层过滤对块内文字**：块内文字继承 BLOCK 定义的图层；若 BLOCK 无图层则不过滤该块。
10. **天正（TCH）图纸**：若报告顶部出现「⚠️ 天正图纸警告」，说明存在 `ACAD_PROXY_ENTITY`
    代理实体，天正文字/轴号/门窗编号**无法从 DXF 恢复**。必须告知用户在 CAD 中做
    「图形导出（T3）」或 `EXPLODE` 后重新导出，不要把这部分缺失当作图纸本身没有内容。
11. **`.t3` / `.dwt` 输入**：本质是 DWG 容器，按 DWG 自动转换；转换仍失败时请用户另存 DWG。

---

## 批量处理

```bash
dwg-reader read --batch /path/drawings --out-dir /path/reports \
        [--table] [--by-layer] [--dimensions] [--components] [--recursive]
# 或免安装：
python3 <skill目录>/scripts/dwg_read.py --batch /path/drawings --out-dir /path/reports
```

- 每张图生成 `<图名>.md`；额外生成 `index.md` 总览（成功/失败数、实体数、文字条数、链接）。
- 进度到 stderr，总览到 stdout。

---

## 进阶（仅当默认入口不够用时）

可作为库直接调用：

```python
from dwg_reader import ReadOptions, read_drawing, build_quantity

result = read_drawing("图纸.dxf", ReadOptions(components=True, dimensions=True))
result.markdown, result.payload, result.text_count

build_quantity("图纸.dxf").sheets()   # {"components": [...], "layers": [...], "dimensions": [...]}
```

底层脚本（均在 `scripts/`）：`parse_dxf.py`、`extract_texts_stream.py`、`extract_texts.py`、
`extract_dimensions.py`、`identify_components.py`、`shxfont.py`、`shx_decompile.py`。
一般 **不需要** Agent 直接调用，`read` 已聚合它们的能力。

---

## 最小可用示例（Agent 可直接复制）

```bash
# 已安装（推荐）
dwg-reader read "/绝对/路径/图纸.dwg"

# 未安装，用 skill 目录
SKILL=~/.workbuddy/skills/dwg-drawing-reader
python3 "$SKILL/scripts/dwg_read.py" "/绝对/路径/图纸.dwg"
```

拿到 stdout 的 Markdown 报告后，按用户问题从中摘取/归纳答案。表格类加 `--table`，
按图层分读加 `--by-layer`，尺寸精读加 `--dimensions`，构件清点加 `--components`，
算量导出用 `dwg-reader quantity -o 算量.csv`，多图加 `--batch`。
