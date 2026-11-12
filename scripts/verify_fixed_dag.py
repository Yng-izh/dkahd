"""Run real-DXF regression and emit small JSON with counts, timing and audit."""
from __future__ import annotations
import collections
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dwg_reader.recognition.pipeline import run_recognition


def run(path, *, targets=("assemble",), jobs=4, use_registry=False):
    started = time.perf_counter()
    result = run_recognition(str(path), targets=targets, jobs=jobs,
                             use_registry=use_registry, collect_stats=True)
    outputs = result.outputs
    assembled = outputs.get("assemble") or {}
    items = assembled.get("components") or []
    counts = dict(collections.Counter(c.get("category") for c in items))
    rooms = [c for c in items if c.get("category") == "room"]
    nodes = {s.name: {"status": s.status, "ms": round(s.duration_ms, 2)}
             for s in (result.stats or [])}
    ctl = outputs.get("adaptive_controller") or {}
    fs = outputs.get("final_candidate_state") or {}
    partial_counts = {}
    if "walls_seg" in outputs:
        partial_counts["wall_segments"] = len((outputs["walls_seg"] or {}).get("walls_seg") or ())
    if "columns" in outputs:
        partial_counts["column_candidates"] = len(outputs["columns"] or ())
    if "spaces_seed" in outputs:
        partial_counts["space_faces"] = len((outputs["spaces_seed"] or {}).get("faces") or ())
    return {"file": Path(path).name, "targets": list(targets),
            "duration_seconds": round(time.perf_counter() - started, 3),
            "counts": counts, "partial_counts": partial_counts,
            "total": len(items),
            "room_boundary": dict(collections.Counter(
                (r.get("attributes") or {}).get("boundary_state") for r in rooms)),
            "room_status": dict(collections.Counter(r.get("status") for r in rooms)),
            "repair": {"runs": ctl.get("runs"), "applied": len(ctl.get("applied") or []),
                       "rolled_back": len(ctl.get("rolled_back") or []),
                       "rejected": len(ctl.get("rejected") or []),
                       "budget": ctl.get("budget"),
                       "rollback_verdicts": dict(collections.Counter(
                           r.get("verdict") for r in (ctl.get("rolled_back") or ())))},
            "finalstate": {"provenance": fs.get("provenance"),
                           "unresolved": len(fs.get("unresolved") or [])},
            "node_times": nodes}


if __name__ == "__main__":
    report = run(sys.argv[1], targets=tuple(sys.argv[2].split(",")) if len(sys.argv)>2 else ("assemble",))
    print(json.dumps(report, ensure_ascii=False, indent=2))
