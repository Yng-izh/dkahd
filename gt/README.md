# 真实图纸真值

真值**独立于识别器**：用 ezdxf 直接量（块参照计数 / 4 线矩形配对 / 开启弧 /
房名文字），不经过 `src/dwg_reader/` 任何识别代码。每个文件逐类别写明口径与容差。

| 文件 | 图纸 | 通过条件 |
|---|---|---|
| `office.json` | 办公楼.dxf | 墙31 / 柱38 / 门30 / 窗31(±1) / 房21 |
| `tdw.json` | 测试-二维DWG.dxf | 墙297–303 / 柱67(±2) / 门55–76 / 房45–60 |

用法（在图纸所在目录执行，`--drawing` 按文件名解析）：

```bash
python3 scripts/acceptance.py --truth gt/office.json gt/tdw.json \
    --out work/reports/acceptance.json --md work/reports/acceptance.md
```

注意 `--truth` 是 `nargs="+"`：多个文件要**在同一个 `--truth` 后面**列，写成
`--truth a.json --truth b.json` 只会取最后一个。

## 实例级真值（`*.instances.json`）

数量口径（本文上半部分）只能回答"数对不对"。要回答"这一根对不对"，需要逐构件
位置真值。文件名约定：`gt/<图纸名>.instances.json`，与 `gt/<图纸>.json` 并存，
互不替代。

```json
{
  "schema": "instances/1.0",
  "drawing": "会所-图层混乱.dxf",
  "annotator": "姓名",
  "annotated_at": "2026-11-11",
  "instances": {
    "wall":   [[x0, y0, x1, y1]],
    "column": [[x0, y0, x1, y1]],
    "door":   [[x0, y0, x1, y1]],
    "window": [[x0, y0, x1, y1]],
    "room":   [[x0, y0, x1, y1]]
  },
  "note": "只标主体平面；图签、详图、重复图框不标"
}
```

规则：

* **必须人工核改**。`scripts/instance_eval.py --bootstrap` 只导出草稿（坐标来自
  一次识别），直接当基线等于自己给自己打分。
* 只标**这张图主体平面**里的构件；图签、详图、索引符号不标 —— 否则真值本身
  就和验收口径矛盾。
* 包围盒取"该构件的可见外轮廓"，墙取中心线外扩半个墙厚；斜墙允许用轴对齐
  包围盒（对斜墙偏严格，见脚本里的 caveat）。
* 每张图先抽 20 个（墙/柱/门/窗/房各 4）就够看出方向，不必一次标全。

跑法：`python3 scripts/instance_eval.py --drawing <图>.dxf --gt gt/<图>.instances.json --iou 0.5`
