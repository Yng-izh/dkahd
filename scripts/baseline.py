#!/usr/bin/env python3
"""识别基线：把"改代码之前长什么样"存成文件，改完逐构件对比。

为什么需要这个
--------------
识别器的每一次改进都在**动阈值**，而阈值之间是耦合的：修好"窗户被判成门"，
可能顺手把两樘门并成一樘；修好"房间封缝"，可能顺手把墙数改掉。没有基线，
这些副作用只能靠人去翻图，靠不住也翻不完。

做法
----
对每张基准图跑一遍识别，把每个构件压成**与坐标无关的指纹**
``(类别, 位置/10mm, 宽度/10mm)``。改完代码再跑一遍，两边的指纹集合做差：

* 只允许"新增正确构件"和"删除错误构件"——每一处差异都要在提交说明里写清
  是修了什么、为什么，不能"跑出来不一样但对不上话"；
* 类别计数（墙/柱/门/窗/房间…）单独记一份，一眼看出哪一类动了。

用法
----
    # 记录当前行为（改代码之前跑！）
    python3 scripts/baseline.py --write

    # 改完之后检查
    python3 scripts/baseline.py --check

    # 只看某一类
    python3 scripts/baseline.py --check --category door

图纸清单默认读 ``tests/baseline/drawings.json``（相对工作区根目录的相对路径），
也可以用 ``--drawings a.dxf b.dxf`` 直接给。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dwg_reader.recognition import recognize  # noqa: E402

BASELINE_DIR = ROOT / "tests" / "baseline"
LIST_NAME = "drawings.json"


def _fingerprint(component):
    """构件 -> 可比对的指纹（位置与尺度都量化到 10mm，抗浮点抖动）。"""
    geom = component.get("geometry") or {}
    pt = (component.get("insert_pt") or geom.get("center")
          or geom.get("insert") or geom.get("bbox") or [0.0, 0.0])
    if len(pt) == 4:                      # bbox -> 中心
        pt = [(pt[0] + pt[2]) / 2.0, (pt[1] + pt[3]) / 2.0]
    width = geom.get("width") or geom.get("length") or 0.0
    return {
        "category": component.get("category"),
        "x": round(float(pt[0] or 0.0) / 10.0),
        "y": round(float(pt[1] or 0.0) / 10.0),
        "w": round(float(width or 0.0) / 10.0),
        "block": (component.get("attributes") or {}).get("block") or "",
    }


def _snapshot(path):
    result = recognize(str(path))
    comps = result.get("components") or []
    counts = {}
    for c in comps:
        cat = str(c.get("category"))
        counts[cat] = counts.get(cat, 0) + 1
    return {"file": Path(path).name, "source": str(Path(path).resolve()),
            "counts": dict(sorted(counts.items())),
            "total": len(comps),
            "components": sorted(
                (_fingerprint(c) for c in comps),
                key=lambda f: (str(f["category"]), f["x"], f["y"], f["w"]))}


def _drawings(args):
    if args.drawings:
        return [Path(p) for p in args.drawings]
    list_path = BASELINE_DIR / LIST_NAME
    if not list_path.exists():
        return []
    items = json.loads(list_path.read_text(encoding="utf-8"))
    base = Path(args.root or os.getcwd())
    out = []
    for it in items:
        p = Path(it)
        out.append(p if p.is_absolute() else base / p)
    return [p for p in out if p.exists()]


def _diff(old, new, category=None):
    """两份快照的差异，按"新增 / 消失"给出，带类别与位置，便于逐条核对。"""
    def key(f):
        return (str(f["category"]), f["x"], f["y"], f["w"])

    def filtered(f):
        return category is None or str(f["category"]) == category

    a = {key(f): f for f in old.get("components") or () if filtered(f)}
    b = {key(f): f for f in new.get("components") or () if filtered(f)}
    added = [b[k] for k in b if k not in a]
    removed = [a[k] for k in a if k not in b]
    counts = {}
    for cat in sorted(set(old.get("counts") or {}) | set(new.get("counts") or {})):
        before = int((old.get("counts") or {}).get(cat) or 0)
        after = int((new.get("counts") or {}).get(cat) or 0)
        if before != after:
            counts[cat] = {"before": before, "after": after, "delta": after - before}
    return {"added": added, "removed": removed, "counts": counts,
            "total": {"before": old.get("total"), "after": new.get("total")}}


def main(argv=None):
    ap = argparse.ArgumentParser(description="识别结果基线：记录 / 核对")
    ap.add_argument("--write", action="store_true", help="把当前识别结果写成基线")
    ap.add_argument("--check", action="store_true", help="与基线对比，有差异即返回非 0")
    ap.add_argument("--drawings", nargs="*", default=None, help="图纸路径（默认读清单）")
    ap.add_argument("--root", default=None, help="清单里相对路径的基准目录")
    ap.add_argument("--category", default=None, help="只看某一类构件")
    ap.add_argument("--quiet", action="store_true", help="只输出摘要")
    ap.add_argument("--llm", action="store_true",
                    help="跑基线时也调用大模型（默认关：基线必须可复现、不依赖网络）")
    args = ap.parse_args(argv)
    if not args.llm:
        os.environ["DWG_LLM_DISABLE"] = "1"

    drawings = _drawings(args)
    if not drawings:
        print("没有可用的基准图纸：给 --drawings，或先写 tests/baseline/drawings.json",
              file=sys.stderr)
        return 2

    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    changed = False
    for path in drawings:
        snap = _snapshot(path)
        target = BASELINE_DIR / f"{path.stem}.json"
        if args.write:
            target.write_text(json.dumps(snap, ensure_ascii=False, indent=1),
                              encoding="utf-8")
            print(f"[write] {target.name}  构件 {snap['total']}  "
                  f"{snap['counts']}")
            continue
        if not target.exists():
            print(f"[skip ] {target.name}：还没有基线（先跑 --write）", file=sys.stderr)
            continue
        old = json.loads(target.read_text(encoding="utf-8"))
        d = _diff(old, snap, category=args.category)
        same = not d["added"] and not d["removed"] and not d["counts"]
        print(f"[{'same ' if same else 'DIFF '}] {path.stem}: "
              f"{d['total']['before']} -> {d['total']['after']}  {d['counts'] or ''}")
        if not args.quiet:
            for f in d["added"][:40]:
                print(f"     + {f['category']} @({f['x'] * 10:.0f},{f['y'] * 10:.0f}) "
                      f"w={f['w'] * 10:.0f} {f['block']}")
            for f in d["removed"][:40]:
                print(f"     - {f['category']} @({f['x'] * 10:.0f},{f['y'] * 10:.0f}) "
                      f"w={f['w'] * 10:.0f} {f['block']}")
        changed = changed or not same

    if args.check and changed:
        print("\n识别结果与基线不一致：逐条确认是修正还是回归。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
