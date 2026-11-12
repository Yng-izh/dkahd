"""识别结果基线回归：同一张图，改代码只能"变好"，不能悄悄变样。

基线由 ``scripts/baseline.py --write`` 生成（放在 ``tests/baseline/``）。没有基线
文件时本测试直接跳过——它保护的是"已确认的行为"，不是"还没有人确认的行为"。

为什么要有这条测试：识别器的阈值彼此耦合，改一处常常顺手动了另一处，而这类
副作用不会报错、只会静默改变构件数。靠人翻图翻不完，靠这条测试可以一眼看出
"这次改动让哪一类动了几个、动在哪"。
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests" / "baseline"
sys.path.insert(0, str(ROOT / "src"))

sys.path.insert(0, str(ROOT / "scripts"))
import baseline as BASELINE_TOOL  # noqa: E402


def _cases():
    if not BASELINE.exists():
        return []
    return sorted(p for p in BASELINE.glob("*.json") if p.name != BASELINE_TOOL.LIST_NAME)


@pytest.mark.parametrize("path", _cases(), ids=lambda p: p.stem)
def test_recognized_components_match_baseline(path):
    old = json.loads(path.read_text(encoding="utf-8"))
    drawing = pathlib.Path(old.get("source") or "")
    if not drawing.exists():
        pytest.skip(f"基线对应的图纸不在工作区：{drawing}")
    new = BASELINE_TOOL._snapshot(drawing)
    diff = BASELINE_TOOL._diff(old, new)
    assert not diff["counts"], f"类别计数变了：{diff['counts']}"
    assert not diff["added"], (
        f"多出 {len(diff['added'])} 个构件（前 5 个）：{diff['added'][:5]}")
    assert not diff["removed"], (
        f"少了 {len(diff['removed'])} 个构件（前 5 个）：{diff['removed'][:5]}")
