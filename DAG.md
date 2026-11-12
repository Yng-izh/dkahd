# 识别 DAG

`src/dwg_reader/recognition/` 是一个 46 节点的构件识别 DAG（有向无环图）：
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

## 两条设计约束

1. **判据不许写死毫米数**：除 `scale.py` 的 `STRUCTURE` 表，其它模块不准新增
   数值阈值常量，有单测强制（`tests/test_generalization.py`）。
2. **大模型只做判断，不做几何**：它只能从闭集里挑角色或否决候选，坐标、尺寸、
   阈值一律由程序算；每次运行的 `meta.llm` 会写明到底有没有用到模型。

## 运行

```bash
pip install -e .[dxf]
python scripts/identify_components.py 图纸.dxf --format json -o 结果.json
```

`fonts/` 目录未随仓库提供（第三方 CAD 字体，另装），只影响渲染出图，不影响识别。
