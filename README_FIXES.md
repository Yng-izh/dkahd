# 构件识别 DAG 修复包（2026-11-09）

基于用户上传的 `dwg-reader-20261108-2328.zip`，保持原有 **46 节点 DAG**，优先修正数据契约、修复事务和几何证据传播，不擅自放宽识别置信度阈值。

## 已修复

1. **质量闸门错误传参** — `recognition/quality_gate.py`：`affected_nodes` 与 `evidence_ids` 的实参顺序恢复正确，增加节点名及引用规范化。对应测试：`test_gate_routes_defect_nodes_and_evidence_in_correct_order`。
2. **局部补丁的增删改事务** — `adaptive_controller.py`、`recompute.py`、`finalstate.py`：新增 `candidate-patch/2`，补丁基于同一原始状态计算 *net diff*，明确表示 `upsert/remove_ids`。按 ID 更新，不再盲目追加；同 ID、不同几何可正确替换；面域数量不变但轮廓变化也会提交。提交前检查重复 ID。对应测试见 `tests/test_dag_fixes.py`。
3. **局部求解正确性** — `local_solver.py`、`recompute.py`：几何变化比较不仅看数量或 bbox；区域外删除视为异常；相同墙网通过几何内容缓存拓扑度量；子 DAG 依赖失效时跳过无意义的下游重建（尤其墙配对无产出和虚拟边失败的情况）。
4. **房间观测/推断证据传递** — `rooms.py`、`room_merge.py`、`nodes.py`：保存 `source_face_ids`、`boundary_state`、`needs_review`；推断边界不因几何闭合就错误升级到 `confirmed`，最终 `room.attributes` 带可审计字段。**状态趋于保守是预期行为**。
5. **方向无关尺度取样** — `scale.py`：新增斜向平行线的法向投影取样，并输出测量样本数量/是否充分，不再仅依赖水平/竖直样本估计主墙厚。这不是对全部墙识别算子的旋转不变性证明。
6. **局部证据真实空间位置** — `evidence.py`、`case_builder.py`：将 `line_support` 的 `(pos, lo, hi, dir, nrm)` 逆投影为 bbox，避免本应是局部的缺陷案例误收到整图线证据；未知空间位置的证据不强行混入局部案例。
7. **易变化参数的缓存安全** — `nodes.py`：关闭 `load`、`openings_select`、`quality_gate`、`adaptive_controller` 和 `final_candidate_state` 的复用缓存，避免跨 profile、环境变量、模型复核策略错误读取旧产物。后续可以改成统一配置哈希后恢复缓存。
8. **可移植的回归测试** — `tests/test_wall_quality.py`：需要外部 `work/profiles/office.json` 的集成测试，在 profile 不存在时明确 skip（不将外部缺依赖算作算法通过）。

## 使用方式

```bash
python -m pip install -e '.[dxf,dev]'
python -m pytest -q -ra
PYTHONPATH=src python scripts/verify_fixed_dag.py /path/to/办公楼.dxf
```

修复控制器仍支持原有环境变量限制尝试预算，例如：

```bash
DWG_CONTROLLER_MAX_MS=5000 PYTHONPATH=src \
  python scripts/verify_fixed_dag.py /path/to/办公楼.dxf
```

注意：时间预算在案例之间检查，已经开始的单次局部试算可能超出上限。快速模式只适合吞吐优先的部署场景；必须按独立标注数据检查是否因此少修复了其他图纸。

## 本环境测试记录

- 自动化：**256 个测试，251 通过，5 跳过，0 失败**（见 `reports/pytest.log`）。跳过：外部字体、ODA DWG 转换器、外部 profile、两项缺少原版 t5 文件的回归测试。
- `办公楼.dxf` 目标 `assemble`：**墙 31、柱 38、门 30、窗 32、房间 21**；46 节点链路完成，总耗时约 **27.74 秒**，动态控制器 20 次影子尝试、0 次采纳，全部被严格回滚。新版房间 `boundary_state`：20 hypothesis、1 observed；21 个房间均为 uncertain（没有因证据不全而假装确认）。报告：`reports/office_full.json`。
- `办公楼.dxf` 限时模式 `DWG_CONTROLLER_MAX_MS=5000`：数量一致，耗时约 **11.29 秒**，6 次试算全部回滚。报告：`reports/office_fast.json`。这只反映**该图纸**在本次硬件/配置下的耗时，不能保证一般性加速。
- `测试-二维DWG.dxf`（约 165MB）：成功跑通 `load + columns + walls_seg` 冒烟测试，耗时约 **28.41 秒**，列出 `column_candidates=84`、`wall_segments=576`。这些是 **中间候选数量**，不是最终柱、连续墙数量，更不是真值准确率。报告：`reports/large_dxf_smoke.json`。此图**未完成全部 46 节点的端到端验收**。

## 仍需进一步工作（未声称已解决）

- 当前 `gt/*.json` 以数量为主要验收口径，缺少逐实例人工校核的几何真值；因此**不能宣称 Precision/Recall/F1 已提升**。原有 `evaluation.py` 和 `scripts/recognize_eval.py` 可接入逐实例 GT。
- 斜线尺度估计已增强，但当前墙体识别的完整**任意旋转不变性**还未完成全链路证明。
- `adaptive_controller` 当前办公楼图无可接受补丁，仍会消耗约 20 秒预算。依赖传播避免了部分无效重建；需结合真实缺陷标注提高修复命中率，不能以放宽验收门槛代替可靠修复。
- 外部资源缺失导致的 5 项 skip 和大型图完整流程，需在实际工作环境补齐后复核。

## 本次新增文件

- `tests/test_dag_fixes.py`：局部数据契约、几何变更、证据位置、旋转尺度与控制器依赖失效回归测试。
- `scripts/verify_fixed_dag.py`：输出真实 DXF 的耗时、构件数、审计与边界状态摘要。
- `reports/*.json`、`reports/pytest.log`：本次验证记录；不含原始 CAD 图纸或任何密钥。
