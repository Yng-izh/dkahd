"""批量跑识别 DAG：目录里的 DXF 逐张识别，输出统一 JSON + 汇总。

用法：
    python3 scripts/batch_dag.py <DXF目录> -o <输出目录> [--jobs 4] [--cache 缓存目录]

输出：
    <输出目录>/json/<图名>.json      每张图的 dwg-components-records/1.1 文档
    <输出目录>/summary.json          逐图计数 / 耗时 / 节点状态（不覆盖已有 json）
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from dwg_reader.recognition import run_recognition  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="批量 DAG 识别")
    ap.add_argument("dxf_dir", type=pathlib.Path)
    ap.add_argument("-o", "--out", type=pathlib.Path, required=True)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--cache", type=pathlib.Path, default=None)
    ap.add_argument("--pattern", default="*.dxf")
    args = ap.parse_args()

    files = sorted(p for p in args.dxf_dir.glob(args.pattern)
                   if p.is_file() and p.suffix.lower() == ".dxf")
    if not files:
        print(f"目录里没有 DXF：{args.dxf_dir}", file=sys.stderr)
        return 2

    out_json = args.out / "json"
    out_json.mkdir(parents=True, exist_ok=True)
    rows = []
    for path in files:
        t0 = time.time()
        row = {"file": path.name, "dxf_bytes": path.stat().st_size}
        try:
            run = run_recognition(str(path), jobs=args.jobs, targets=("export",),
                                  cache_dir=str(args.cache) if args.cache else None)
            doc = run.output
            (out_json / f"{path.stem}.json").write_text(
                json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            summary = run.summary()
            row.update({
                "ok": True,
                "seconds": round(time.time() - t0, 1),
                "total_records": doc["meta"].get("total_records"),
                "by_type": doc["meta"].get("by_type"),
                "nodes_ok": summary.get("ok"),
                "nodes_cached": summary.get("cached"),
                "nodes_failed": summary.get("failed"),
                "warnings": summary.get("warnings") or [],
            })
        except Exception as exc:                       # noqa: BLE001
            row.update({"ok": False, "seconds": round(time.time() - t0, 1),
                        "error": f"{type(exc).__name__}: {exc}"})
        rows.append(row)
        print(f"[{'ok' if row['ok'] else 'FAIL'}] {row['file']} "
              f"{row.get('total_records', '-')} 构件 {row['seconds']}s",
              file=sys.stderr, flush=True)

    (args.out / "summary.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0 if all(r["ok"] for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
