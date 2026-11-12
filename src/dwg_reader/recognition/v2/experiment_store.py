"""实验存储：记录、比较、复用——**没有它，就分不清"改好了"和"看起来改好了"**。

存三样东西，都在本地目录（不进仓库）：

* ``experiments.jsonl``：每次运行一行（输入指纹、语法键、权重、目标函数值、
  各类计数、耗时、模型调用数），append-only，坏了也不影响识别；
* ``index.json``：按"语法键 + 输入指纹"索引最近一次的指标，用于快速比较；
* 规则库：复用 :mod:`rule_memory` 的三层状态（candidate / project / cross_project），
  这里只负责"把成功的一次写成候选规则"和"召回它当提议"。

三条纪律：

1. **只追加**，不就地改写历史（否则"之前那次更好"就查不到了）；
2. 复用只产生**提议**，仍要过当前图纸的联合求解与验收；
3. 比较时比的是**同一批图纸、同一套口径**的指标，不跨口径吹牛。
"""
from __future__ import annotations

import json
import os
import time

SCHEMA = "experiment-store/1.0"

#: 逻辑预算：保留多少行、比较最多看多少行
BUDGET = {"keep": 2000, "compare_rows": 50}

FILES = {"log": "experiments.jsonl", "index": "index.json", "meta": "meta.json"}


def _path(directory: str, kind: str) -> str:
    return os.path.join(str(directory), FILES[kind])


def _ensure(directory: str) -> None:
    if directory:
        os.makedirs(str(directory), exist_ok=True)


def _dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


def key_for(*, ir_digest: str = "", grammar_hashes=(), config_digest: str = "",
            weights_digest: str = "") -> str:
    """实验键：输入指纹 + 语法集合 + 配置 + 权重（任一变化就是另一个实验）。"""
    import hashlib
    body = {"ir": str(ir_digest), "grammars": sorted(str(g) for g in grammar_hashes or ()),
            "config": str(config_digest), "weights": str(weights_digest)}
    return hashlib.sha256(_dumps(body).encode("utf-8")).hexdigest()[:16]


def record(entry: dict, *, directory: str) -> dict:
    """追加一条实验记录，并刷新索引。返回 ``{"ok", "path", "key"}``。"""
    if not directory:
        return {"ok": False, "reason": "NO_DIRECTORY"}
    _ensure(directory)
    row = {"schema": SCHEMA, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
           **(entry or {})}
    row.setdefault("key", key_for(ir_digest=row.get("ir_digest") or "",
                                  grammar_hashes=row.get("grammar_hashes") or (),
                                  config_digest=row.get("config_digest") or ""))
    log = _path(directory, "log")
    try:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(_dumps(row) + "\n")
    except OSError as exc:
        return {"ok": False, "reason": f"WRITE_FAILED:{type(exc).__name__}"}
    index_path = _path(directory, "index")
    index = _load_index(index_path)
    prev = index.get(row["key"]) or {}
    index[row["key"]] = {
        "at": row["at"], "objective": row.get("objective"),
        "counts": row.get("counts") or {}, "winner": row.get("winner"),
        "runs": int(prev.get("runs") or 0) + 1,
        "best_objective": max(float(prev.get("best_objective") or -1e9),
                              float(row.get("objective") or 0.0)),
    }
    try:
        with open(index_path, "w", encoding="utf-8") as handle:
            json.dump(index, handle, ensure_ascii=False, indent=1, sort_keys=True)
    except OSError:
        pass
    return {"ok": True, "path": log, "key": row["key"]}


def _load_index(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load(directory: str, *, limit: int | None = None) -> list[dict]:
    """读回实验历史（最后 N 行）。文件不存在就返回空列表。"""
    path = _path(directory, "log")
    limit = int(BUDGET["keep"] if limit is None else limit)
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return rows[-limit:]


def best(*, directory: str, key: str | None = None,
         metric: str = "objective") -> dict | None:
    """历史最优（同键优先，其次全局）：给"这次比上次好吗"一个可比对象。"""
    rows = load(directory, limit=int(BUDGET["keep"]))
    if key:
        rows = [r for r in rows if r.get("key") == key]
    if not rows:
        return None
    return max(rows, key=lambda r: float(r.get(metric) or 0.0))


def compare(rows, *, metric: str = "objective", top: int | None = None) -> dict:
    """比较多次实验：每个键的最好/最差/中位，以及相对上一次的增减。"""
    rows = [r for r in (rows or ()) if isinstance(r, dict)]
    top = int(BUDGET["compare_rows"] if top is None else top)
    rows = rows[-top:]
    by_key: dict[str, list] = {}
    for row in rows:
        by_key.setdefault(str(row.get("key") or "?"), []).append(row)
    out: dict = {"schema": SCHEMA, "rows": len(rows), "keys": {}, "delta": None}
    for key, group in by_key.items():
        values = sorted(float(r.get(metric) or 0.0) for r in group)
        out["keys"][key] = {"runs": len(group), "best": round(values[-1], 4),
                            "worst": round(values[0], 4),
                            "median": round(values[len(values) // 2], 4)}
    if len(rows) >= 2:
        out["delta"] = round(float(rows[-1].get(metric) or 0.0)
                             - float(rows[-2].get(metric) or 0.0), 4)
    return out


def remember_rule(entry: dict, *, directory: str, memory_dir: str) -> dict:
    """一次成功 -> 规则库候选（仍是 candidate 层，不许跳级）。"""
    if not memory_dir:
        return {"ok": False, "reason": "NO_MEMORY_DIR"}
    from .. import rule_memory as MEMORY
    grammar_hash = str(entry.get("grammar_hash") or "")
    key = MEMORY.template_key(
        drawing_unit_context=str(entry.get("units") or ""),
        geometry_fingerprint=grammar_hash,
        block_family=str(entry.get("family_hash") or ""),
        operator_versions={"resolver": str(entry.get("resolver_version") or "1.0")})
    res = MEMORY.remember(
        {"key": key, "tier": "candidate", "scope": "project",
         "drawing": str(entry.get("ir_digest") or ""),
         "region": entry.get("region"),
         "category": str(entry.get("category") or ""),
         "strategy_id": str(entry.get("strategy") or "component_grammar"),
         "parameters": dict(entry.get("weights") or {}),
         "evidence": {"objective": entry.get("objective"),
                      "counts": entry.get("counts") or {},
                      "grammar": grammar_hash}},
        directory=memory_dir)
    return dict(res, key=key)


def reuse(*, directory: str, memory_dir: str, entry: dict) -> dict:
    """召回历史经验（语法/权重）当**提议**：不跳过联合求解与验收。"""
    if not memory_dir:
        return {"ok": False, "reason": "NO_MEMORY_DIR"}
    from .. import rule_memory as MEMORY
    key = MEMORY.template_key(drawing_unit_context=str(entry.get("units") or ""),
                              geometry_fingerprint=str(entry.get("grammar_hash") or ""))
    hits = MEMORY.recall(directory=memory_dir, key=key)
    historic = best(directory=directory, key=key_for(
        ir_digest=entry.get("ir_digest") or "",
        grammar_hashes=entry.get("grammar_hashes") or (),
        config_digest=entry.get("config_digest") or ""))
    return {"ok": bool(hits or historic), "key": key, "memory": hits,
            "best_objective": (historic or {}).get("objective"),
            "advisory": True}


def summary(directory: str) -> dict:
    """进审计的摘要（条数、最近一次、最好一次）。"""
    rows = load(directory, limit=int(BUDGET["compare_rows"]))
    latest = rows[-1] if rows else None
    top = max(rows, key=lambda r: float(r.get("objective") or 0.0)) if rows else None
    return {"schema": SCHEMA, "directory": str(directory), "entries": len(rows),
            "latest": {"key": (latest or {}).get("key"),
                       "objective": (latest or {}).get("objective"),
                       "at": (latest or {}).get("at")} if latest else None,
            "best": {"key": (top or {}).get("key"),
                     "objective": (top or {}).get("objective")} if top else None,
            "compare": compare(rows)}
