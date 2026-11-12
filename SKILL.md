---
name: dwg-drawing-reader
description: "读取、解析和分析 DWG/DXF 工程图纸，提取图层、实体、文字、尺寸、表格和构件，并可导出算量清单（CSV/XLSX）、按需通过 OpenSCAD 导出闭合二维轮廓或通过 MCP 提供结构化只读工具。适用于建筑、机械、电气、结构及给排水 CAD 内容理解；不用于直接编辑 DWG/DXF。"
---

# DWG 图纸阅读

## When to Use

使用本 skill 当且仅当用户意图满足以下之一：

- 提供 `.dwg` / `.dxf` 文件路径，并要求"读取 / 看 / 理解 / 提取 / 分析"图纸内容。
- 要求从 CAD 图纸提取文字标注、尺寸、图层、表格（门窗表 / 材料表）。
- 要求把 DWG 转为可读文本 / DXF。
- 要求批量处理多个 DWG/DXF、按图层筛选、识别构件（门/窗/梁/柱等）、精读尺寸标注。

关键词：DWG、DXF、CAD 图纸、工程图、图纸标注、图层、看图、算量。

## Do Not Use

- **不要**用于"画一张图 / 生成 CAD / 修改图纸几何"——本 skill 是只读解析器，不写回 DXF/DWG。
- **不要**把 OpenSCAD 拉伸当成完整 DWG 渲染；仅在用户明确要求从闭合二维 DXF
  生成 3D 网格时使用可选后端。
- **不要**用于非 CAD 的 PDF/图片图纸——那是 OCR/光栅识别任务，不在本 skill 范围。

## Prerequisites

| 组件               | 版本 / 路径                                                                                                                                                                                | 状态          | 安装                                                                                            |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------- | ----------------------------------------------------------------------------------------------- |
| Python             | 3.10+                                                                                                                                                                                      | 已安装        | 系统自带                                                                                        |
| ezdxf              | v1.4+                                                                                                                                                                                      | 可选          | `pip install 'dwg-reader-skill[dxf]'`（仅 `inspect` 需要）                                      |
| openpyxl           | 3.1+                                                                                                                                                                                       | 可选          | `pip install 'dwg-reader-skill[xlsx]'`（仅 XLSX 算量导出）                                      |
| ODA File Converter | Windows `C:\Program Files\ODA\ODAFileConverter <版本>\ODAFileConverter.exe`；macOS `/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter`；Linux `/usr/bin/ODAFileConverter` | 仅 DWG 需安装 | https://www.opendesign.com/guestfiles/oda_file_converter ；也支持环境变量 `DWG_READER_ODA_PATH` |
| 中文字体库         | `fonts/`（322 个 SHX + `index.json`）                                                                                                                                                      | 已内置        | 无需操作                                                                                        |
| 大字体解析器       | `src/dwg_reader/fonts.py`、`scripts/shxfont.py`                                                                                                                                            | 已内置        | 仅依赖标准库                                                                                    |

> `read` / `quantity` 仅依赖标准库；ezdxf 与 ODA（处理 DWG 时）为可选增强。

## Supported Formats

| 格式                       | 输入支持         | 备注                                                                    |
| -------------------------- | ---------------- | ----------------------------------------------------------------------- |
| `.dxf`                     | ✅ 直接解析      | 文本格式，ezdxf 原生支持                                                |
| `.dwg`                     | ✅ 经 ODA 转 DXF | 自动调用 ODA 转为同目录 DXF 后解析                                      |
| `.shx`                     | ✅ 字体解析      | 作为字体库被引用，不直接作为图纸输入                                    |
| `.dgn` / `.step` / 3D 实体 | ❌               | 本 skill 面向 2D 工程图，不做 3D CAD 转换（见下方 Platform Support 注） |

## Platform Support

| 能力            | macOS arm64 | macOS x86_64 | Linux x86_64              | Windows x86_64          |
| --------------- | ----------- | ------------ | ------------------------- | ----------------------- |
| 解析 DXF        | ✅          | ✅           | ✅                        | ✅                      |
| DWG→DXF（ODA）  | ✅          | ✅           | ✅（需安装 ODA Linux 版） | ✅（需安装 ODA Win 版） |
| 字体解码（SHX） | ✅          | ✅           | ✅                        | ✅                      |

> 注：本 skill 与 NVIDIA `usd-convert-cad`（用于 3D CAD→USD 仿真）定位不同——本 skill 专注 2D 工程图（DWG/DXF）的文本/几何解析，不处理 3D 实体建模或仿真就绪资产。二者互补而非替代。

## Implementation Contract

- **入口**：装好后用 `dwg-reader read <文件> [选项]`；免安装用 `scripts/dwg_read.py <文件> [选项]`。
  两者返回码 `0` 表示成功，非 0 表示失败。
- **输出契约**：默认打印 Markdown 报告到 stdout；`-o` / `--out` 写入文件并打印进度到 stderr；
  `--format json` 输出机器可读 JSON。
- **降级行为**：内置流式解析器容忍缺 EOF / 多余 SECTION / 空值等不规范文件，一般无需降级分支；
  ezdxf 路径（`inspect`）失败时改用 `read`。
- **DWG 副作用**：处理 `.dwg` 会在源文件同目录落一个同名 `.dxf`（预期行为，非报错）。
- **算量导出**：`dwg-reader quantity <文件> -o 算量.csv|xlsx`，含构件清单、图层几何量与尺寸清单。
- **底层脚本**：`parse_dxf.py` / `extract_texts_stream.py` / `extract_dimensions.py` / `identify_components.py` 均为返回码驱动的 CLI，可被外部调用。
- **识别编排（可选）**：`run_recognition()` 支持 `jobs` 并行、`targets` 部分执行、内容寻址缓存与逐节点统计；
  默认入口 `recognize()` / CLI / MCP / render 行为不变。详见「识别流程 DAG（编排 API）」。
- **可选集成**：需要 OpenSCAD、OpenCAD 或 MCP 时先读取
  [references/integrations.md](references/integrations.md)，再运行 `dwg-reader doctor`。

## Troubleshooting

| 现象                              | 原因                 | 处理                                                             |
| --------------------------------- | -------------------- | ---------------------------------------------------------------- |
| `未找到 ODA File Converter`       | ODA 未安装或路径不符 | 安装 ODA（见 Prerequisites），或传 `.dxf` 并加 `--no-convert`    |
| `DXF 解析失败（missing EOF tag）` | 文件缺 EOF           | 自动降级流式提取；结果见 `⚠️` 提示下文字章节                     |
| 中文全变 `?` / 乱码               | 字体未匹配           | 加 `--font fonts/gbcbig.shx` 强制指定；或检查 `fonts/index.json` |
| 报告全空（实体数 0）              | 内容在匿名块定义内   | 已自动补提取未引用块文字；若仍空，文件可能无文字实体             |
| DWG 转换卡住 / 弹窗               | ODA 参数不完整       | 不应手动调 ODA；统一用 `dwg_read.py` 入口                        |
| `未安装 ezdxf`                    | 环境缺包             | `pip3 install ezdxf`                                             |

## 概述

| 项目         | 内容                                                                                |
| ------------ | ----------------------------------------------------------------------------------- |
| **能力名称** | DWG 图纸阅读与解析                                                                  |
| **核心功能** | 读取 DWG/DXF 图纸，提取图层、实体、文字标注、尺寸信息，输出结构化解析报告供阅读理解 |
| **输入**     | DWG 或 DXF 文件路径                                                                 |
| **输出**     | 结构化解析报告（实体清单、文字提取、图层列表、尺寸信息）                            |
| **依赖**     | ODA File Converter（DWG→DXF）+ ezdxf（解析）                                        |

## 工作原理

```
DWG (二进制) → [ODA File Converter] → DXF (纯文本) → [ezdxf] → 解析提取
```

- DWG 是 Autodesk 专有二进制格式，无法直接读取，必须先转换为 DXF。
- DXF 是纯文本格式，ezdxf 库可完整解析。
- 转换是无损的，实体、图层、文字、尺寸、块引用全部保留。
- **不渲染图片**：只做解析提取，通过文字、实体、图层信息阅读理解图纸内容。

## 环境依赖

| 组件               | 路径/版本                                                             | 状态   |
| ------------------ | --------------------------------------------------------------------- | ------ |
| ODA File Converter | `/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter`  | 已安装 |
| Python             | python3 (3.13+)                                                       | 已安装 |
| ezdxf              | pip 包，v1.4+                                                         | 已安装 |
| SHX 大字体解析器   | 内置 `scripts/shxfont.py`（自研，仅依赖 Python 标准库，无需第三方包） | 已内置 |
| 中文字体库         | `fonts/`（322 个 SHX，含 index.json 编码索引）                        | 已内置 |

> 如果 ezdxf 未安装，执行：`pip3 install ezdxf`
> 如果 ODA 未安装，从 https://www.opendesign.com/guestfiles/oda_file_converter 下载 macOS arm64 dmg，拖入 /Applications，并执行 `xattr -dr com.apple.quarantine "/Applications/ODAFileConverter.app"`。

## 转换命令（DWG→DXF）

### 方式一：命令行调用 ODA（推荐用于脚本）

```bash
ODA="/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter"
mkdir -p <输出目录>
"$ODA" <输入目录> <输出目录> ACAD2018 DXF 0 0 "*.dwg"
```

参数说明（位置参数，无命名参数）：
| 位置 | 参数 | 说明 |
|------|------|------|
| 1 | 输入目录 | 含 DWG/DXF 的源目录 |
| 2 | 输出目录 | 转换结果存放目录（需已存在） |
| 3 | 输出版本 | 如 `ACAD2018`、`ACAD2013`、`ACAD2007`、`ACAD2000`、`ACAD12` |
| 4 | 输出类型 | `DXF` 或 `DWG` |
| 5 | 递归标志 | `0`=不递归，`1`=递归处理子目录 |
| 6 | 审计标志 | `0`=关闭，`1`=加载时审计修复 |
| 7 | 文件过滤器 | 如 `"*.dwg"`、`"*.dxf"` |

> 注意：ODA 是 Qt GUI 程序，无参数启动会打开窗口；必须带完整 7 个位置参数以命令行模式运行。转换完成后进程自动退出。

### 方式二：ezdxf 插件（ODA 装好后可用）

```python
from ezdxf.addons.dwg import dwg2dxf
dwg2dxf("input.dwg", "output.dxf")
```

## 解析脚本

项目内置脚本位于 `scripts/` 目录：

- `dwg_read.py` — **一站式流水线**（推荐）：自动 DWG→DXF 转换 + 结构化报告 + 文字提取 + 可选尺寸精读 / 构件识别 / 批量处理，输出 Markdown 报告
- `parse_dxf.py` — 解析 DXF 并输出结构化报告（实体清单、文字、图层、块、尺寸），支持 `--layer-filter`
- `extract_texts_stream.py` — 流式提取全部文字（低内存），支持 BigFont 解码、块展开、表格/图层聚合、`--layer-filter` / `--layer-alias`
- `extract_texts.py` — 基于 ezdxf 的全量文字提取（含块属性 virtual_entities 展开）
- `extract_dimensions.py` — **尺寸标注精读**：按类型（线性/对齐/角度/半径/直径/坐标/弧长）分类，关联最近几何，输出表格 + 可选 JSON
- `identify_components.py` — **构件识别（实例级）**：图框/轴网/指北针/柱/墙/门窗/房间/楼梯/台阶/散水，输出构件实例 + `dwg-components-records/1.1` 统一文档
- `render_components.py` — **识别结果可视化**：把构件按类别着色叠到 DXF 线稿上出 PNG（仅供人工核对，不参与识别）
- `shxfont.py` — SHX 大字体解析器（建立 shape number → 字符映射）
- `shx_decompile.py` — SHX 反编译为 SHP 文本（等价 DUMPSHX / shx2shp）

> **解码一致性**：BigFont（`\M+`）/ Unicode（`\U+`）/ `%%` 转义 / MTEXT 堆叠（`\S`）解码已接入全部提取脚本
> （`dwg_read.py`、`parse_dxf.py`、`extract_texts.py`、`extract_texts_stream.py`），
> 均会自动按文本样式匹配 `fonts/` 字体库；无需手动指定字体或预处理。

### 一站式流水线（推荐）

```bash
# 工程化 CLI（需 pip install -e .）
dwg-reader read <图纸.dwg|图纸.dxf> [-o 报告.md] [--format json]
                [--table] [--by-layer] [--no-convert] [--font 字体.shx]
                [--dimensions] [--components]
                [--layer-filter "WALL|BEAM"] [--layer-alias 别名.json]

# 批量
dwg-reader read --batch <目录> --out-dir <输出目录> [--recursive]

# 算量导出（构件清单 + 图层几何量 + 尺寸清单）
dwg-reader components <图纸> [--format json|markdown|files] [--emit-schema]
dwg-reader quantity <图纸> -o 算量.csv
dwg-reader quantity <图纸> -o 算量.xlsx --sections components,layers
```

```bash
# 免安装脚本入口（能力等价）
# 单文件（DWG 或 DXF 均可，DWG 自动转 DXF）
python3 scripts/dwg_read.py <图纸.dwg|图纸.dxf> [--out 报告.md]
                      [--table] [--by-layer] [--no-convert] [--font 字体.shx]
                      [--dimensions] [--components]
                      [--layer-filter "WALL|BEAM"] [--layer-alias 别名.json]

# 批量目录：遍历目录下所有 .dwg/.dxf，每张图一份报告 + 总览 index.md
python3 scripts/dwg_read.py --batch <目录> --out-dir <输出目录>
                      [--table] [--by-layer] [--dimensions] [--components]

# 选项
#   --out 报告.md        写入 Markdown 报告文件（默认打印到 stdout）
#   --table              以 Markdown 表格还原对齐网格（如门窗表）
#   --by-layer           按图层聚合输出文字
#   --no-convert         跳过 DWG→DXF 转换（输入已是 DXF 时加速）
#   --font               显式指定大字体文件（默认按 STYLE 表自动匹配 fonts/）
#   --dimensions         追加「尺寸标注精读」章节（按类型分类 + 关联几何）
#   --components         追加「构件识别」章节（图框/轴网/墙/门窗/房间等实例统计）
#   --layer-filter       按图层名关键词/正则过滤（仅保留匹配图层；逗号分隔多词）
#   --layer-alias        图层别名表 JSON（{用户词: 实际图层片段}），中文词映射到实际图层
#   --batch              批量模式：扫描目录下所有 .dwg/.dxf
#   --out-dir            批量模式的输出目录（每张图一个 .md + 总览 index.md）
```

### 用法（单脚本）

```bash
# 结构化报告
python3 <skill目录>/scripts/parse_dxf.py <文件.dxf> [--entities] [--texts] [--layers] [--blocks] [--limits] [--layer-filter WALL]

# 尺寸标注精读
python3 <skill目录>/scripts/extract_dimensions.py <文件.dxf> [--json] [--out 尺寸.md]

# 构件识别
python3 <skill目录>/scripts/identify_components.py <文件.dxf> [--format json|markdown|files] [-o 输出] [--emit-schema]
```

### 快速阅读理解（推荐）

对于整张图纸的快速理解，用一段内联 Python 按位置排序提取全部文字（TEXT + MTEXT），还原图纸的阅读顺序（左上到右下）：

```python
import ezdxf
doc = ezdxf.readfile('图纸.dxf')
msp = doc.modelspace()
texts = []
for e in msp.query('TEXT MTEXT'):
    pos = e.dxf.insert
    content = e.text if e.dxftype() == 'MTEXT' else e.dxf.text
    texts.append((pos.y, pos.x, content))
texts.sort(key=lambda t: (-t[0], t[1]))  # y 从大到小（上到下），x 从小到大（左到右）
for y, x, t in texts:
    print(t)
```

> 注：MTEXT 内容含内嵌格式代码（`\P` 换行、`\f` 字体、`\H` 高度、`\W` 宽度等），输出时可先用正则 `re.sub(r'\\[A-Za-z][^\\;]*;|\\[A-Za-z]', '', content)` 清理，再阅读理解。

### 大字体编码解码（BigFont）

天正/探索者（TSSD）图纸的中文文字常以 CAD 大字体编码存储，需解码：

- `\M+5XXXX` → 去掉前导 `5`，后 4 位 `XXXX` 是 SHX 大字体 shape number（即该字体的字符编码，中文字体为 GBK 两字节码）：`bytes([hi, lo]).decode('gbk')`
- `\U+XXXX` → Unicode 码点：`chr(int(XXXX, 16))`

示例：`\M+5BAA3` = 海、`\M+5C4CF` = 南、`\M+5BDE1` = 结、`\M+5B9B9` = 构 → "海南结构"

> 注意：`\M+5XXXX` 与相邻数字文本（如 `\M+5A1DD800` = "≥800"）之间不要贪婪匹配，用 `\\M\+5([0-9A-Fa-f]{4})` 精确匹配 5 位。

### 英文与西文支持

英文/数字/符号完整支持，分三种情况：

| 文本类型                | 存储方式   | 解码方式                 |
| ----------------------- | ---------- | ------------------------ |
| 纯英文/数字（ASCII）    | 直存       | 原样输出，无需解码       |
| 西文字体字符（unifont） | `\U+XXXX`  | `chr(int(XXXX, 16))`     |
| 中文字符（bigfont）     | `\M+5XXXX` | SHX 字体映射表（gbk 等） |

- **ASCII 直存**：`HELLO WORLD 123`、`GB50017-2017`、`kN/m`、`MPa` 等原样提取
- **Unicode 码点**：`\U+0041`→A、`\U+00D7`→×（乘号）、`\U+006D2`→m²、`\U+00A0`→不换行空格
- **西文 unifont 字体**：tssdeng.shx 等（unifont 类型，shape number 即 Unicode 码点）直接 `chr()` 解码
- **中英混排**：一条文本中英文、Unicode 符号、中文同时正确解码（如 `C30混凝土 \U+00D7 250` → `C30混凝土 × 250`）
- **中英文共用字体**：bigfont 中 ASCII 区（0x20-0x7F）字符直接按 `chr()` 映射

### 符号支持

图纸中的专业符号完整支持，分三类：

**1. AutoCAD `%%` 转义序列**（自动解码，大小写不敏感）

| 转义        | 符号         | 含义                         |
| ----------- | ------------ | ---------------------------- |
| `%%C`/`%%c` | `Ø`          | 直径（如 `%%C20` → `Ø20`）   |
| `%%D`/`%%d` | `°`          | 度（如 `45%%d` → `45°`）     |
| `%%P`/`%%p` | `±`          | 正负（如 `%%P0.5` → `±0.5`） |
| `%%%`       | `%`          | 百分号                       |
| `%%u`/`%%o` | （移除标记） | 下划线/上划线开-关           |
| `%%数字`    | 单字节字符   | 如 `%%130` → `\x82`          |

**2. `\U+XXXX` Unicode 符号**：`\U+00B1`→±、`\U+00B0`→°、`\U+00D7`→×、`\U+00B2`→²、`\U+00B3`→³、`\U+00B5`→µ

**3. `\M+5XXXX` bigfont 符号区**（GBK A1 区）：`\M+5A1DD`→≥、`\M+5A1E3`→°、`\M+5A1A2`→、等

实测：结构图纸中 `栓钉 Ø19 间距@200`、`吊索夹角45°`、`±0.000`、`±3mm` 等全部正确解码。

### SHX 大字体解析（全字体支持）

内置 SHX 大字体解析器（`scripts/shxfont.py`），从 SHX 字体文件直接建立
shape number → 字符映射，支持多语言编码自动识别：

```python
from shxfont import ShxBigFont
font = ShxBigFont("fonts/gbcbig.shx")   # 7019 shapes
font.get_char(0xBAA3)   # → '海'
```

**关键结论**：bigfont 索引表 shape number = 该字体的字符编码。gbcbig.shx /
hztxt.shx / tssdchn.shx（中文 GBK）、KORdansun.shx（韩文 cp949）、hsa.shx
（日文 shift_jis）等均验证成立。DXF 中 `\M+5XXXX` 的 `XXXX` 就是 shape
number，解码时会按字体编码自动匹配（gbk/cp949/shift_jis/big5），解码失败
时多编码回退。

### 自动字体匹配（默认模式）

`extract_texts_stream.py` 无 `--font` 参数时会**自动解析 DXF 的 STYLE 表**，
按每个文本实体的样式名匹配 `fonts/` 字体库中的大字体文件，逐样式正确解码：

```bash
python3 scripts/extract_texts_stream.py 图纸.dxf out.txt
# 输出: 自动匹配 N 个大字体
```

### 反编译 SHX → SHP

`scripts/shx_decompile.py` 将大字体 SHX 反编译为 SHP 文本（等价于
AutoCAD DUMPSHX / shx2shp 工具）：

```bash
python3 scripts/shx_decompile.py fonts/gbcbig.shx /tmp/gbcbig.shp
# 输出格式: *BIGFONT 7019,1,0A1,0FE / *BAA3,124,海 / 数据行...
```

### 低内存提取

大 DXF（>15MB）用 ezdxf 全量加载可能 OOM，改用 `scripts/extract_texts_stream.py`
逐行解析 DXF tag 对，仅收集 TEXT/MTEXT 的 code 1/3 内容，支持 BigFont 解码
（自动匹配或 `--font` 显式指定）：

```bash
# 自动模式（推荐）：按 STYLE 表匹配字体
python3 scripts/extract_texts_stream.py 图纸.dxf out.txt
# 显式指定字体
python3 scripts/extract_texts_stream.py 图纸.dxf out.txt --font fonts/gbcbig.shx
```

### 字体库（全量）

`fonts/` 目录内置 322 个 SHX 字体（约 159MB，来自 AutoCAD SHX 字体集合），
含 `index.json` 索引（字体名 → 类型/编码/shape 数）：

- bigfont 234 个：中文 GBK（gbcbig/hztxt/tssdchn 等）、韩文 cp949、日文 shift_jis
- unifont 88 个：Unicode 编码（tssdeng 等）
- 支持常见别名：gbcbig/hztxt/tssdchn/tssdeng 等，大小写不敏感，自动补 `.shx`
- STYLE 表字体名带路径前缀（如 `fonts/tssdchn.shx`、`C:\...\x.shx`）会自动取 basename 匹配

### 表格结构化还原（`--table`）

对对齐工整的网格文字（如门窗表、材料表），`extract_texts_stream.py` / `dwg_read.py`
可用几何聚类（`--table`）还原为 Markdown 表格：

```bash
python3 scripts/extract_texts_stream.py 图纸.dxf --table
# 或
python3 scripts/dwg_read.py 图纸.dxf --table
```

算法：按 Y 相近聚类出行、X 相近聚类出列，顶部行在上；不足 2×2 时自动回退平铺输出。

### 阅读顺序还原（默认）

文字默认按**几何感知排序**（`reorder_by_columns`）：先按栏（X 聚类）分组，栏内按
Y 降序（上→下）、X 升序（左→右）。比纯 `(-Y, X)` 更适合多栏图纸，避免左栏与
右栏同 Y 行交错。

### 按图层聚合（`--by-layer`）

`--by-layer` 按 DXF LAYER 分组输出文字，便于按专业（建筑/结构/水电）分读：

```bash
python3 scripts/extract_texts_stream.py 图纸.dxf --by-layer
```

### 块内文字与属性（INSERT / ATTRIB）

- INSERT 块参照自动展开：块定义内 TEXT/MTEXT 按块基点 + 缩放/旋转/镜像变换到世界坐标
- ATTRIB 块属性随块展开提取
- 未被 INSERT 引用的块（图纸主体常置于 `*Model_Space` 等匿名块）其内部文字也直接提取，
  保证无独立 ENTITIES 段或不规范文件不丢字

### MTEXT 堆叠文字（`\S`）

MTEXT 的堆叠分数/公差（`\S top^bottom`、`\S top/bottom`、`\S top#bottom`）解码为
可读形式（`top/bottom`、`top#bottom`），`{...}` 内的高度控制码（`\H`）自动剥离。

### 损坏 / 不规范 DXF 容错

- 缺 EOF 标签、`0` 后空值（空字符串合法值）等导致 ezdxf 严格解析失败时，
  `dwg_read.py` 自动降级为流式提取（结果仍可用）
- BLOCKS 段内游离实体、嵌套 SECTION 均按实体提取，不丢字
- 文本编码：优先 UTF-8（现代 CAD 通用），失败回退 `$DWGCODEPAGE` 指示的编解码器

### 尺寸标注精读（`--dimensions`）

针对 DIMENSION 实体，按 AutoCAD 类型分类并关联被标注几何：

- **类型分类**：线性（0）、对齐（1）、角度（2/6）、直径（3）、半径（4）、坐标（5）、弧长（8），来自 `dimtype` 低 4 位。
- **标注值**：优先覆盖文字（code 1），否则用 `get_measurement()` 测量值；已自动解码 `%%` / `\U+` / BigFont。
- **几何关联**：对每条尺寸取插入点，匹配最近的 k 个 LINE/ARC/CIRCLE/LWPOLYLINE，给出类型/图层/距离/描述，便于核对"尺寸标在谁身上"。
- **输出**：Markdown 明细表（类型/标注值/图层/位置/关联几何）+ 可选 `--json` 机器可读格式。
- **降级**：ezdxf 严格解析失败时，流式提取 DIMENSION 的 code 1/10/70 关键字段（类型/位置），关联几何不可用，报告顶部标 `⚠️`。

```bash
python3 scripts/extract_dimensions.py 图.dxf --json
# 或经一站式入口
python3 scripts/dwg_read.py 图.dxf --dimensions
```

### 构件识别（`components` 子命令 / `--components`）

按**原生图元**识别 12 类建筑构件，输出**逐个构件实例**（不是类型计数）：
图框、轴线、轴网交点、指北针、柱、墙、门、窗、房间、楼梯、台阶、散水。

- **识别链**：
  - 墙 — WALL 图层双线面线配对（平行面线间距 + 端部封口校验 + 厚度≤350 + 剔除共用面线的面层线）→ 中心线/厚度/轮廓；跨柱位与面线错位合并；组件层再把同一墙线上被门窗洞口断开的墙段合并为**连续墙 run**（洞口记 `attributes.openings`），房间/台阶/散水仍用未合并墙段（洞口处断开）
  - 柱 — `_FZH` 单位方块块参照 × 缩放
  - 门窗 — 块名 + 块属性（`M1021`/`C1828`）+ 弧线几何；门、窗各自按块计，不合并
  - 轴网 — `_AXISO` 块属性（轴号）+ DOTE 图层通长轴线
  - 图框 — PUB_TITLE 闭合多段线 + 图签区文字（图名/图号/项目）
  - 指北针 — 圆 + 过圆心指针 + “北”字方位 → 方位角
  - 楼梯 — STAIR 图层踏步线聚类 + 梯段编号；台阶 — 散水图层两种画法：嵌套 U 形轮廓（北侧）+ 平行台阶边线（南入口）
  - 散水 — 散水图层轮廓 + 到外墙面法向距离
  - 房间 — 房间名文字作种子，墙/柱/隔断/洞口作障碍，10mm 栅格连通域 + 边界追踪
- **每实例输出**：`id` / `category` / 几何（中心线、轮廓、包围盒）/ 属性（厚度、宽、面积、开启方向）/ `source_entities`（含 Handle）/ `evidence`（判据）/ `confidence` / `status`（`uncertain` 需人工复核）。
- **统一输出**：只有**一种 JSON**——`dwg-components-records/1.1` 单一文档，同时装下
  `meta` / `basemap` / `components` / `unrecognized` / `unmatched_blocks` / `scope` / `notes`，
  不再拆成多个互相不兼容的 JSON。
  - schema 定义：`schema/dwg-components-records-1.1.schema.json`（JSON Schema 2020-12），
    `dwg-reader components --emit-schema` 可直接打印。
  - 每条构件同时带 `bbox_model`（世界坐标，可回 CAD 定位）与 `bbox_px`（底图像素，可叠图）。
  - 生成时用内置校验器按 schema 自检，不合法直接报错，不会产出半成品。
- **输出格式**：`json`（统一文档，默认）、`markdown`（分类报告）、`files`（写出 `components.json` + `report.md`）。
- **面积口径**：房间面积为“净面积”，求连通域前沿同一墙线闭合洞口（≤9m），门洞/通道处按墙线截断。
- **已知缺口**：楼梯只给梯段轮廓（不含梯间周边）；卫生间被 LVTRY 成品隔断块细分，面积偏差大；图例框本图未绘制；墙体的连续合并边界见下「墙体：连续墙 run」的已知限制。
- **不做图像识别**：全程原生图元，不渲染、不做 SVG/位图识别。

```bash
# 识别并打印统一 JSON
python3 scripts/identify_components.py 图.dxf --format json -o out.json
# 写出 components.json（统一文档）+ report.md
python3 scripts/identify_components.py 图.dxf --format files --out-dir out/
# 打印 JSON Schema 定义
python3 scripts/identify_components.py 图.dxf --emit-schema

# 或经 CLI
dwg-reader components 图.dxf --format json -o out.json
dwg-reader components --emit-schema
# 或经一站式入口（在报告里追加 Markdown 章节）
python3 scripts/dwg_read.py 图.dxf --components

# 可视化核对（需要 Pillow）
python3 scripts/render_components.py 图.dxf out/components.json -o 平面.png --view plan --labels
python3 scripts/render_components.py 图.dxf out/components.json -o 全图.png --view all
python3 scripts/render_components.py 图.dxf out/components.json -o 墙.png --cats 墙,柱,门,窗
```

统一文档的顶层结构：

```json
{
  "schema": "dwg-components-records/1.1",
  "meta":    { "source_file", "dxf_version", "units", "total_records", "by_type", "grid", ... },
  "basemap": { "width", "height", "transform": { "world", "scale_px_per_unit", ... } },
  "components": [ { "id", "category", "anchor", "bbox_model", "bbox_px", "dxf_handle",
                    "source_handles", "geometry", "attributes", "evidence",
                    "confidence", "status", ... } ],
  "unrecognized":     [ { "handle", "type", "layer", "reason" } ],
  "unmatched_blocks": [ { "block", "count" } ],
  "scope": { "summary", "mapped" },
  "notes": [ ... ]
}
```

#### 墙体：连续墙 run（0.7.0）

组件层的墙按**连续墙实例（wall run）**输出，不再被门窗洞口切成多段：

- **合并条件**：同一墙线（`orient` + 中心线坐标，按 0.1mm 分组）；相邻墙段缺口被**单樘门/窗洞口**覆盖（容差 120mm）；缺口 ≤3000mm；厚度差 ≤2mm；合并后长度 ≥250mm。
- **`geometry`**：`type="wall_run"`；`start` / `end` / `thickness` / `length` / `outline` 描述整道连续墙；`segments` 保留合并前的原子墙段（按轴向有序、无重叠），`segment_count` 为段数。
- **`attributes.openings`**：洞口数组，每项 `{category, id, width, span, type_code, handle, layer}`；`source_entities` 额外包含洞口门/窗的 Handle；`evidence.detail` 写“跨 N 个门窗洞口连续（…）”。
- **门窗关联**：门/窗 `attributes.wall_id` 指向所在 wall run（组件层 id 形如 `WALL-H-001` / `WALL-V-002`）。
- **schema 文档**：`build_document()` 把记录重编号为 `WALL-0001…` / `DOOR-0001…` / `WIN-0001…`，并**同步重映射**门窗 `wall_id` 与 `openings[].id`，保证交叉引用有效（只改文档副本，不回写上游）。
- **兼容**：房间/台阶/散水仍使用未跨洞口合并的墙段（洞口处断开），面积/连通性不变；`walls_merge` 同时保留旧 `walls_all` / `walls` 供这些节点使用。

办公楼.dxf 实测：墙 **71 段 → 31 个 wall run**（0 个 <500mm 碎片），10 个墙含 **40 条**洞口记录（如 `WALL-0009` 跨 13 个洞口、长 49.3m）。真值 72 段：中心代表点覆盖 **72/72 = 100%**，沿墙长度覆盖 ≥0.9 为 **66/72 = 91.7%**。

已知限制：

- 洞口宽度 >3000mm、或缺口未被门/窗洞口覆盖（走廊/通道口）时**不合并**；
- 不同墙线（中心线不同）或同中心线厚度差 >2mm 不合并；单线墙/块内墙不识别；
- 门缺少 opening 几何或零宽时不作为合并依据（仍参与 `wall_id` 就近查询）；
- 未落在 WALL 中心线上的门/隔断门 `wall_id` 为 `null`（本图 11 个，与基线一致）；
- `evidence.detail` 中的洞口编号是组件层 id（如 `WIN-01`），与 schema 文档记录 id（`WIN-0001`）不同；结构化 `attributes.openings[].id` 已重映射，可直接引用。

示例（实跑）：

```bash
python3 scripts/identify_components.py 图.dxf --format json -o out.json
python3 - <<'PY'
import json
doc = json.load(open("out.json", encoding="utf-8"))
walls = [c for c in doc["components"] if c["category"] == "墙"]
print("墙", len(walls), "洞口", sum(len(w["attributes"]["openings"]) for w in walls))
w = max(walls, key=lambda c: len(c["attributes"]["openings"]))
print(w["id"], w["geometry"]["type"], w["geometry"]["length"],
      w["geometry"]["segment_count"], [o["id"] for o in w["attributes"]["openings"]])
PY
# 墙 31 洞口 40
# WALL-0009 wall_run 49300.0 24 ['WIN-0001', 'WIN-0002', 'DOOR-0009', 'WIN-0003']
```

### 图层规则智能过滤（`--layer-filter` / `--layer-alias`）

仅保留匹配图层的实体与文字，便于聚焦单一专业：

- `--layer-filter "WALL|BEAM|DIMS"`：逗号分隔，支持正则片段，不区分大小写。
- `--layer-alias 别名.json`：`{"墙":"WALL","梁":"BEAM"}`，把中文词映射为实际图层片段后并入过滤。
- 作用于全链路：`parse_dxf.py`（实体/文字）与 `extract_texts_stream.py`（文字，含块内文字继承块图层）同时过滤。
- 块内文字继承其所属 BLOCK 定义的图层（实测部分图纸 BLOCK 不带图层，则降级为不过滤该块）。

### 批量处理目录（`--batch`）

一次处理一个目录下的所有 `.dwg` / `.dxf`：

```bash
python3 scripts/dwg_read.py --batch /path/to/drawings --out-dir /path/to/reports \
        [--table] [--by-layer] [--dimensions] [--components]
```

- 每张图生成 `<图名>.md` 报告到 `--out-dir`；
- 额外生成 `index.md` 总览（图数/成功失败数/每张图的实体数、文字条数、报告链接）；
- 处理进度打印到 stderr，总览打印到 stdout。

## 识别流程 DAG（编排 API）

构件识别内部是一条 **46 节点有向无环图**（43 个 PURE + 3 个 MAIN），代码在
`src/dwg_reader/recognition/`。默认入口 `recognize()`、`dwg-reader components`、
`dwg-reader read --components`、MCP 与 render 的行为全部不变；只有显式调用
`run_recognition()` 才启用并行、部分执行、缓存与逐节点统计。

**完整的节点清单、依赖表、数据契约、参数推导与已知限制见
[`src/dwg_reader/recognition/README.md`](src/dwg_reader/recognition/README.md)**
（该文件是识别 DAG 的唯一权威说明，本节只保留调用面）。

```text
load(MAIN·快照)
  ├─ texts ─ rooms_labels_seed ─ label_filter ─┐
  ├─ walls_seg / columns ─ columns_verify ─ wall_model ─ spaces_seed
  │     └─ doors / windows ─ opening_judge ─ openings_unify ─ openings_select
  ├─ partitions / grid / frame / north / stairs
  └─ raw_evidence ─ defect_detect ─ repair_options ─ evidence_graph ─ quality_gate
        └─ llm_reason / repair_planner ─ validate_proposal ─ plan_compile
             └─ adaptive_controller ─ affected_recompute ─ finalize_patch
                  └─ final_candidate_state ─ repair_audit ─ final_audit
                       └─ walls_merge ─ envelope / steps / apron / room_labels
                            └─ rooms ─ assemble(MAIN) ─ export(MAIN)
```

### 调用

```python
from dwg_reader.recognition import run_recognition, clear_cache

run = run_recognition("图纸.dxf", jobs=4)                    # 并行；targets 默认 ("assemble",)
run = run_recognition("图纸.dxf", targets=("rooms",))         # 部分执行：目标 + 上游闭包
run = run_recognition("图纸.dxf", targets=("export",))        # 生成 dwg-components-records/1.1
run = run_recognition("图纸.dxf", cache_dir=".cache/dwg-dag") # 内容寻址缓存
run.summary()   # total_ms / nodes / ok / cached / failed / skipped / slowest / warnings
clear_cache(".cache/dwg-dag")                                 # 或 clear_cache(dir, node="rooms")
```

- `NodeStat` 字段：`name` / `kind` / `status`（`ok`|`cached`|`failed`|`skipped`）/ `duration_ms` /
  `thread` / `deps` / `input_summaries` / `input_digests` / `output_summary` /
  `output_digest` / `cache_key` / `cache_hit` / `error` / `reason`。

### 缓存与并行安全

- 位置：`<cache_dir>/dwg-dag-cache-v1/<node>/<key>.json`；39 个可缓存节点
  （46 − 7：`opening_judge` / `label_filter` / `space_judge` / `repair_planner` / `llm_reason` /
  `assemble` / `export`）。
- 键为内容寻址：`sha256(canonical_json({cache, version, engine, file, params, deps}))`，
  `file` 是 DXF 字节 SHA-256、`deps` 是上游输出摘要、`engine` 是 `recognition/*.py` 源码指纹
  ——**识别代码改动会自动失效缓存**。条目损坏/摘要不符 → miss 并记 `warnings`；
  目录不可写 → 降级为无缓存，识别照常完成。
- `load`（MAIN）在主线程解析并构建 JSON-native 快照；PURE 节点访问 `ctx.doc` / `ctx.msp`
  立即抛 `RuntimeError`，ezdxf 对象不跨线程，因此 `jobs=1` 与 `jobs=4` 输出逐条一致。
- 节点异常：失败节点 `failed`，下游 `skipped(upstream_failed:<dep>)`；`fail_fast` 时其余
  `skipped(aborted)`；默认抛 `DagExecutionError`（`.cause` 为拓扑序第一个原始异常），
  `raise_on_error=False` 时返回 `DagRun`。

## 解析输出内容

### 1. 实体清单（Entities）

模型空间中所有实体，按类型统计并列出关键属性：

- `LINE`: 起点/终点坐标
- `CIRCLE`: 圆心/半径
- `ARC`: 圆心/半径/起始角度/终止角度
- `TEXT`/`MTEXT`: 文字内容/插入点/高度/旋转
- `DIMENSION`: 尺寸标注（测量值/文字/位置）
- `LWPOLYLINE`: 顶点序列/闭合状态
- `INSERT`(块引用): 块名/插入点/缩放/旋转

### 2. 文字提取（Texts）

提取图纸中所有文字标注（TEXT + MTEXT），用于理解图纸含义。MTEXT 需要解析内嵌格式代码（`\A`、`\P`、`\f` 等）。

### 3. 图层信息（Layers）

图层名、颜色、线型、开关/冻结状态、可见性。

### 4. 尺寸信息

DIMENSION 实体的测量值，对应图纸的尺寸标注。

## 注意事项

1. **版本兼容性**：ODA 输出 ACAD2018 DXF 时，ezdxf 可读所有版本（含 R12）。若源文件是 2018+ 版本，仍可无损转换。
2. **中文字体**：DXF 文字优先按 UTF-8 解码，失败回退到 `$DWGCODEPAGE` 编码；含中文可正确输出。
3. **块引用**：INSERT 默认自动展开块内文字与属性；`parse_dxf.py` 另有 `--explode-blocks` 展开实体清单。
4. **大文件**：超大图纸（>100MB）建议用 `extract_texts_stream.py` / `dwg_read.py`（逐行流式，低内存）。
5. **批量转换**：ODA 支持整个目录批量转换，把多个 DWG 放进一个目录即可一次转换。
6. **坐标单位**：DWG 无强制单位约定，解析时输出原始坐标值，比例关系需结合图纸尺寸标注判断。
7. **不渲染图片**：按用户偏好，本 skill 只做解析提取（实体/文字/图层/尺寸），不输出 SVG/PNG 渲染图。
8. **一站式入口**：日常使用直接 `python3 scripts/dwg_read.py 图纸.dwg`，自动转换+报告，无需分步操作。

## 版本与变更记录

当前版本：**0.7.1**（与 `pyproject.toml`、`src/dwg_reader/__init__.py::__version__` 一致）。

| 版本 | 日期 | 变更 |
| --- | --- | --- |
| 0.7.1 | 2026-11-12 | **AI 自适应增强（默认关闭）**：新增 `ai_mode=off｜propose｜enhance`、运行时参数表 `param_registry`（绝对授权区间 + 真实消费者校验）、残余图元发现 `pattern_discovery`（48 节点）、受约束提议 `ai_planner`、模型网关 `model_gateway`、多试验执行 `experiment_runner`、全局一致性验收、三层规则记忆 `rule_memory`、`dwg-reader ai-eval`。`ai_mode=off` 输出与 0.7.0 逐条一致；模型不写坐标/阈值/不执行代码；未授权不上传图纸。另修：`import dwg_reader` 不再强制要求 ezdxf（改为 PEP 562 懒加载），版本号统一 0.7.1。 |
| 0.7.0 | 2026-11-07 | 墙体语义：同一墙线上被门窗洞口断开的墙段合并为**连续墙 run**（办公楼.dxf 71 段 → 31 个实例），洞口记入 `attributes.openings`（含 handle/span/type_code），门窗 `wall_id` 指向连续墙；schema 文档重编号后同步重映射 `wall_id` 与 `openings[].id`；房间/台阶/散水仍用未合并墙段，结果不变。 |
| 0.6.0 | 2026-11-06 | 识别流程 DAG 化：20 节点、`jobs` 并行、`targets` 部分执行、逐节点耗时/摘要、内容寻址缓存与 `clear_cache()`；新增 `run_recognition()` / `build_recognition_dag()`；`recognize()` 改为兼容门面，输出与 CLI/MCP/render 行为不变。 |
| 0.5.0 | 2026-11-06 | 构件识别升级为实例级 12 类（图框/轴网/指北针/柱/墙/门窗/房间/楼梯/台阶/散水），统一 `dwg-components-records/1.1` 文档；新增 `dwg-reader components`、`render_components.py`。 |
| 0.4.0 | 2026-11-01 | 工程化读取流水线、算量导出（CSV/XLSX）、天正（TCH）图纸检测。 |
