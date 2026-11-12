"""可编排的识别 DAG 执行器：节点 / 依赖 / 拓扑校验 / 并行 / 部分执行 / 统计 / 缓存。

设计要点（与 ``work/packaging/requirements.md`` §5 对应）：

* ``Node`` 描述一个阶段：``fn(ctx)``、``deps``、``kind``（``pure`` / ``main``）、
  ``cacheable``、``version``、``summarize``。
* ``DAG`` 负责结构校验（重复/未知依赖/自依赖/环）与确定性拓扑序（Kahn + 名字排序）。
* ``run_dag()`` 只把 ``kind="pure"`` 的节点提交到线程池；``kind="main"`` 节点固定
  在编排线程执行。PURE 节点通过 ``NodeContext`` 只能拿到已完成依赖的纯 Python
  输出，访问 ``ctx.doc`` / ``ctx.msp`` 会立即抛 ``RuntimeError``。
* 缓存是内容寻址的：``sha256(canonical_json({cache, engine, node, version, file,
  params, deps:{dep: output_digest}}))``，条目落在
  ``<cache_dir>/dwg-dag-cache-v1/<node>/<key>.json``；``clear_cache()`` 提供清除。

本模块不依赖 ezdxf，也不认识任何具体识别节点；识别层见 ``nodes.py``。
"""
from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import threading
import time
import traceback
from concurrent.futures import ALL_COMPLETED, FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence

CACHE_VERSION = "dwg-dag-cache/1"
CACHE_DIRNAME = "dwg-dag-cache-v1"
ERROR_LIMIT = 2048

NodeKind = Literal["pure", "main"]


# --------------------------------------------------------------------------- 错误
class DagError(Exception):
    """DAG 相关错误基类。"""


class DagValidationError(DagError):
    """DAG 结构或调用参数不合法。"""


class DagCycleError(DagValidationError):
    """DAG 存在环；``cycle`` 为闭合路径（首尾同名）。"""

    def __init__(self, cycle: Sequence[str]):
        self.cycle = list(cycle)
        super().__init__("cycle: " + " -> ".join(self.cycle))


class DagNodeError(DagError):
    """单个节点执行失败；``cause`` 保留原始异常对象。"""

    def __init__(self, node: str, cause: BaseException):
        self.node = node
        self.cause = cause
        super().__init__(f"node {node} failed: {type(cause).__name__}: {cause}")


class DagExecutionError(DagError):
    """一次执行中存在失败节点；``cause`` 是拓扑序第一个失败原因。"""

    def __init__(self, failures: Sequence[DagNodeError], skipped: Sequence[str],
                 cause: BaseException):
        self.failures = list(failures)
        self.skipped = list(skipped)
        self.cause = cause
        detail = "; ".join(f"{f.node}: {type(f.cause).__name__}: {f.cause}" for f in failures)
        super().__init__(f"DAG execution failed ({len(self.failures)} node(s)): {detail}")


# --------------------------------------------------------------------------- 工具
def canonical_json(value: Any) -> str:
    """规范 JSON：缓存键 / 摘要 / 跨线程比较都用它。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def output_digest(value: Any) -> str:
    """输出规范 JSON 的 SHA-256。"""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def is_json_native(value: Any) -> bool:
    """递归判断一个值是否只由 JSON 原生类型组成（tuple/set/bytes 都算否）。"""
    if value is None or isinstance(value, (bool, str, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(is_json_native(v) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and is_json_native(v) for k, v in value.items())
    return False


def _summarize_default(value: Any) -> dict:
    if isinstance(value, dict):
        return {"type": "dict", "len": len(value), "keys": list(value.keys())}
    if isinstance(value, (list, tuple)):
        return {"type": "list", "len": len(value)}
    if isinstance(value, str):
        return {"type": "str", "len": len(value)}
    if isinstance(value, bool):
        return {"type": "bool", "value": value}
    if isinstance(value, int):
        return {"type": "int", "value": value}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if value is None:
        return {"type": "none"}
    return {"type": "unknown", "class": type(value).__name__}


_VERSION_CACHE: dict[tuple[str, str], str | None] = {}


def _node_version(fn: Callable[..., Any]) -> str | None:
    key = (getattr(fn, "__module__", ""), getattr(fn, "__qualname__", repr(fn)))
    if key in _VERSION_CACHE:
        return _VERSION_CACHE[key]
    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError):
        version = None
    else:
        version = hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]
    _VERSION_CACHE[key] = version
    return version


def engine_fingerprint(directory: str | os.PathLike[str] | None = None) -> str:
    """识别实现指纹：目录下 ``*.py`` 按文件名排序后拼接取 SHA-256。

    任何识别实现改动都会自动让缓存失效，不依赖手工 bump 版本。
    """
    path = Path(directory) if directory is not None else Path(__file__).resolve().parent
    h = hashlib.sha256()
    for py in sorted(path.glob("*.py"), key=lambda p: p.name):
        h.update(py.name.encode("utf-8"))
        h.update(b"\0")
        h.update(py.read_bytes())
    return h.hexdigest()


# --------------------------------------------------------------------------- 节点
@dataclass(frozen=True)
class Node:
    """一个可执行识别阶段。

    ``params`` 是该节点消费的 ``NodeContext.params`` 键名（只影响缓存键）；
    ``summarize`` 可覆盖默认输出摘要。
    """

    name: str
    fn: Callable[["NodeContext"], Any]
    deps: tuple[str, ...] = ()
    kind: NodeKind = "pure"
    cacheable: bool = True
    version: str | None = None
    summarize: Callable[[Any], dict] | None = None
    params: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "deps", tuple(self.deps))
        object.__setattr__(self, "params", tuple(self.params))
        if self.version is None:
            object.__setattr__(self, "version", _node_version(self.fn))


class DAG:
    """节点集合 + 结构校验 + 确定性拓扑序。"""

    def __init__(self, nodes: Iterable[Node]):
        table: dict[str, Node] = {}
        for node in nodes:
            if not isinstance(node, Node):
                raise DagValidationError(f"invalid node: {node!r}")
            if not isinstance(node.name, str) or not node.name:
                raise DagValidationError(f"invalid node name: {node.name!r}")
            if node.name in table:
                raise DagValidationError(f"duplicate node: {node.name}")
            if node.kind not in ("pure", "main"):
                raise DagValidationError(f"invalid node kind: {node.name} -> {node.kind!r}")
            table[node.name] = node
        self._nodes = table
        self._topo: tuple[str, ...] | None = None
        self.validate()

    @property
    def nodes(self) -> Mapping[str, Node]:
        return MappingProxyType(self._nodes)

    def deps(self, name: str) -> tuple[str, ...]:
        node = self._nodes.get(name)
        if node is None:
            raise DagValidationError(f"unknown node: {name}")
        return node.deps

    # ------------------------------------------------------------------ 校验
    def validate(self) -> None:
        for name, node in self._nodes.items():
            for dep in node.deps:
                if dep == name:
                    raise DagValidationError(f"self dependency: {name}")
                if dep not in self._nodes:
                    raise DagValidationError(f"unknown dependency: {name} -> {dep}")
        self._detect_cycle()

    def _detect_cycle(self) -> None:
        """DFS 三色标记；返回闭合路径而不是只报“有环”。"""
        color = {name: 0 for name in self._nodes}
        for start in sorted(self._nodes):
            if color[start] != 0:
                continue
            stack: list[str] = [start]
            position = {start: 0}
            color[start] = 1
            work: list[tuple[str, Any]] = [
                (start, iter(sorted(self._nodes[start].deps)))
            ]
            while work:
                name, it = work[-1]
                try:
                    dep = next(it)
                except StopIteration:
                    color[name] = 2
                    stack.pop()
                    position.pop(name, None)
                    work.pop()
                    continue
                if color[dep] == 1:
                    raise DagCycleError(stack[position[dep]:] + [dep])
                if color[dep] == 0:
                    color[dep] = 1
                    position[dep] = len(stack)
                    stack.append(dep)
                    work.append((dep, iter(sorted(self._nodes[dep].deps))))

    # ------------------------------------------------------------------ 拓扑
    def toposort(self) -> list[str]:
        """Kahn 算法；就绪集合按名字排序，保证同一 DAG 多次调用结果一致。"""
        if self._topo is not None:
            return list(self._topo)
        indegree = {name: len(node.deps) for name, node in self._nodes.items()}
        children: dict[str, list[str]] = {name: [] for name in self._nodes}
        for name, node in self._nodes.items():
            for dep in node.deps:
                children[dep].append(name)
        ready = sorted(name for name, n in indegree.items() if n == 0)
        order: list[str] = []
        while ready:
            name = ready.pop(0)
            order.append(name)
            for child in sorted(children[name]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
            ready.sort()
        if len(order) != len(self._nodes):
            # 构造函数已校验过环；这里只防御内部状态被破坏。
            raise DagCycleError(order)
        self._topo = tuple(order)
        return list(order)

    def upstream(self, targets: Sequence[str]) -> set[str]:
        """targets 及其全部上游闭包。"""
        seen: set[str] = set()
        stack = list(targets)
        while stack:
            name = stack.pop()
            if name in seen:
                continue
            if name not in self._nodes:
                raise DagValidationError(f"unknown target: {name}")
            seen.add(name)
            stack.extend(self._nodes[name].deps)
        return seen

    def terminals(self) -> tuple[str, ...]:
        """没有下游的节点（识别 DAG 里是 ``export``）。"""
        has_child = set()
        for node in self._nodes.values():
            has_child.update(node.deps)
        return tuple(sorted(n for n in self._nodes if n not in has_child))


# --------------------------------------------------------------------------- 上下文
class LiveDocument:
    """惰性加载的 live ezdxf 文档；只在 MAIN 节点访问时触发。"""

    def __init__(self, loader: Callable[[], tuple[Any, Any]]):
        self._loader = loader
        self._doc: Any = None
        self._msp: Any = None
        self._loaded = False
        self._lock = threading.Lock()

    def _ensure(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if not self._loaded:
                self._doc, self._msp = self._loader()
                self._loaded = True

    @property
    def doc(self) -> Any:
        self._ensure()
        return self._doc

    @property
    def msp(self) -> Any:
        self._ensure()
        return self._msp


class NodeContext:
    """单个节点看到的上下文。

    ``kind="pure"`` 时访问 ``doc`` / ``msp`` 立即抛 ``RuntimeError``，这是并行安全
    的运行时守卫：PURE 节点只能读 ``inputs`` / ``params`` 里的纯 Python 数据。
    """

    def __init__(self, *, dxf_path: str = "", file_digest: str = "",
                 inputs: Mapping[str, Any] | None = None,
                 params: Mapping[str, Any] | None = None,
                 kind: NodeKind = "main", name: str = "<root>",
                 live: LiveDocument | None = None):
        self.dxf_path = dxf_path
        self.file_digest = file_digest
        self.inputs: Mapping[str, Any] = dict(inputs or {})
        self.params: Mapping[str, Any] = dict(params or {})
        self.kind = kind
        self.name = name
        self._live = live

    def _for_main(self) -> Any:
        if self.kind != "main":
            raise RuntimeError(f"doc access is not allowed for pure node: {self.name}")
        if self._live is None:
            raise RuntimeError(f"no live document loader for node: {self.name}")
        return self._live

    @property
    def doc(self) -> Any:
        return self._for_main().doc

    @property
    def msp(self) -> Any:
        return self._for_main().msp


# --------------------------------------------------------------------------- 统计
@dataclass
class NodeStat:
    name: str
    kind: NodeKind
    status: Literal["ok", "cached", "failed", "skipped"]
    duration_ms: float
    thread: int
    deps: tuple[str, ...]
    input_summaries: dict[str, dict] = field(default_factory=dict)
    input_digests: dict[str, str] = field(default_factory=dict)
    output_summary: dict = field(default_factory=dict)
    output_digest: str | None = None
    cache_key: str | None = None
    cache_hit: bool = False
    error: str | None = None
    reason: str | None = None


@dataclass
class DagRun:
    output: Any
    outputs: dict[str, Any]
    stats: list[NodeStat]
    cache_hits: int
    duration_ms: float
    warnings: list[str]

    def summary(self) -> dict:
        executed = [s for s in self.stats if s.status in ("ok", "cached")]
        slowest = max(executed, key=lambda s: s.duration_ms) if executed else None
        return {
            "total_ms": round(self.duration_ms, 3),
            "nodes": len(self.stats),
            "ok": sum(1 for s in self.stats if s.status == "ok"),
            "cached": sum(1 for s in self.stats if s.status == "cached"),
            "failed": sum(1 for s in self.stats if s.status == "failed"),
            "skipped": sum(1 for s in self.stats if s.status == "skipped"),
            "slowest": ({"name": slowest.name, "duration_ms": round(slowest.duration_ms, 3)}
                        if slowest else None),
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------- 缓存
class _CacheStore:
    def __init__(self, cache_dir: str | os.PathLike[str], warnings: list[str]):
        self.root = Path(cache_dir) / CACHE_DIRNAME
        self.warnings = warnings
        self.disabled = False
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:                       # 不可写 -> 禁用缓存，不影响识别
            self.disabled = True
            self.warnings.append(f"cache disabled (cannot create {self.root}): {exc}")

    def path(self, node: str, key: str) -> Path:
        return self.root / node / f"{key}.json"

    def load(self, node: str, key: str, meta: Mapping[str, Any]) -> tuple[Any, str, float] | None:
        if self.disabled:
            return None
        path = self.path(node, key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception as exc:                     # 损坏条目 -> miss，不崩溃
            self.warnings.append(f"cache miss (corrupt {node}/{key[:8]}): {exc}")
            return None
        try:
            for field_name in ("cache", "node", "version", "engine", "file", "params", "deps"):
                expected = CACHE_VERSION if field_name == "cache" else (
                    node if field_name == "node" else meta[field_name])
                if data.get(field_name) != expected:
                    return None
            output = data["output"]
            digest = output_digest(output)
            if digest != data.get("output_digest"):
                self.warnings.append(f"cache miss (digest mismatch {node}/{key[:8]})")
                return None
            return output, digest, float(data.get("duration_ms", 0.0))
        except Exception as exc:
            self.warnings.append(f"cache miss (invalid entry {node}/{key[:8]}): {exc}")
            return None

    def save(self, node: str, key: str, meta: Mapping[str, Any], output: Any,
             digest: str, duration_ms: float) -> None:
        if self.disabled:
            return
        path = self.path(node, key)
        entry = dict(meta)
        entry.update({"cache": CACHE_VERSION, "node": node, "output": output,
                      "output_digest": digest, "duration_ms": round(duration_ms, 3)})
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(entry, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            self.warnings.append(f"cache write failed ({node}): {exc}")
        except (TypeError, ValueError) as exc:
            self.warnings.append(f"cache skipped ({node}): {exc}")


def clear_cache(cache_dir: str | os.PathLike[str], node: str | None = None) -> int:
    """删除缓存条目，返回删除的 ``*.json`` 数量。"""
    root = Path(cache_dir) / CACHE_DIRNAME
    if node is not None:
        root = root / node
    if not root.exists():
        return 0
    removed = 0
    for path in sorted(root.rglob("*.json")):
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    for path in root.rglob("*.tmp"):
        try:
            path.unlink()
        except OSError:
            pass
    return removed


# --------------------------------------------------------------------------- 执行
def run_dag(dag: DAG, *, context: NodeContext,
            params: Mapping[str, Any] | None = None,
            targets: Sequence[str] | None = None,
            jobs: int = 1,
            cache_dir: str | os.PathLike[str] | None = None,
            collect_stats: bool = True,
            fail_fast: bool = True,
            raise_on_error: bool = True) -> DagRun:
    """执行 DAG 的 target 闭包。

    参数与行为见 ``work/packaging/requirements.md`` §5；本函数不修改 ``dag`` 与
    ``context``，节点输出只写进本次执行的 ``DagRun.outputs``。
    """
    started = time.perf_counter()
    dag.validate()
    if not isinstance(jobs, int) or isinstance(jobs, bool) or jobs < 1:
        raise ValueError("jobs must be >= 1")
    if targets is None:
        target_list = list(dag.terminals())
    else:
        target_list = list(targets)
        if not target_list:
            raise ValueError("targets must not be empty")
        for target in target_list:
            if target not in dag.nodes:
                raise DagValidationError(f"unknown target: {target}")
    closure = dag.upstream(target_list)
    order = [name for name in dag.toposort() if name in closure]

    merged_params: dict[str, Any] = dict(context.params or {})
    if params:
        merged_params.update(params)

    warnings: list[str] = []
    outputs: dict[str, Any] = {}
    stats: dict[str, NodeStat] = {}
    digests: dict[str, str] = {}
    failures: list[DagNodeError] = []
    skipped_reasons: dict[str, str] = {}
    main_ident = threading.get_ident()

    cache = _CacheStore(cache_dir, warnings) if cache_dir is not None else None
    if cache is not None and cache.disabled:
        cache = None
    engine = engine_fingerprint() if cache is not None else None
    compute_digests = cache is not None or collect_stats

    def summary_of(value: Any, node: Node | None = None) -> dict:
        if node is not None and node.summarize is not None:
            try:
                return node.summarize(value)
            except Exception as exc:                 # 摘要失败不影响识别
                warnings.append(f"summarize failed for {node.name}: {exc}")
        return _summarize_default(value)

    def mark_skipped(name: str, reason: str) -> None:
        node = dag.nodes[name]
        skipped_reasons[name] = reason
        stats[name] = NodeStat(
            name=name, kind=node.kind, status="skipped", duration_ms=0.0,
            thread=main_ident, deps=node.deps, reason=reason)

    def status_of(name: str) -> str | None:
        stat = stats.get(name)
        return stat.status if stat is not None else None

    def bad_dep(name: str) -> str | None:
        for dep in dag.nodes[name].deps:
            if status_of(dep) in ("failed", "skipped"):
                return dep
        return None

    def deps_done(name: str) -> bool:
        return all(status_of(dep) in ("ok", "cached", "failed", "skipped")
                   for dep in dag.nodes[name].deps)

    def execute(name: str) -> str:
        node = dag.nodes[name]
        node_started = time.perf_counter()
        input_objects = {dep: outputs[dep] for dep in node.deps}
        input_summaries = {dep: summary_of(outputs[dep], dag.nodes[dep]) for dep in node.deps}
        input_digests = ({dep: digests[dep] for dep in node.deps if dep in digests}
                         if compute_digests else {})

        cache_key: str | None = None
        cache_meta: dict[str, Any] | None = None
        if cache is not None and node.cacheable:
            node_params = {key: merged_params.get(key) for key in node.params}
            dep_digests = {dep: digests.get(dep) for dep in node.deps}
            if all(value is not None for value in dep_digests.values()):
                cache_meta = {
                    "version": node.version,
                    "engine": engine,
                    "file": context.file_digest,
                    "params": node_params,
                    "deps": dep_digests,
                }
                try:
                    cache_key = hashlib.sha256(canonical_json(
                        {"cache": CACHE_VERSION, **cache_meta}).encode("utf-8")).hexdigest()
                except (TypeError, ValueError) as exc:
                    warnings.append(f"cache key failed for {name}: {exc}")
            else:
                warnings.append(f"cache skipped for {name}: missing upstream digest")

        if cache is not None and cache_key is not None and cache_meta is not None:
            hit = cache.load(node.name, cache_key, cache_meta)
            if hit is not None:
                cached_output, cached_digest, cached_ms = hit
                outputs[name] = cached_output
                digests[name] = cached_digest
                stats[name] = NodeStat(
                    name=name, kind=node.kind, status="cached",
                    duration_ms=round((time.perf_counter() - node_started) * 1000.0, 3),
                    thread=threading.get_ident(), deps=node.deps,
                    input_summaries=input_summaries, input_digests=input_digests,
                    output_summary=summary_of(cached_output, node),
                    output_digest=cached_digest, cache_key=cache_key, cache_hit=True)
                return "cached"

        ctx = NodeContext(
            dxf_path=context.dxf_path, file_digest=context.file_digest,
            inputs=input_objects, params=merged_params, kind=node.kind,
            name=name, live=context._live)
        try:
            output = node.fn(ctx)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:                     # 节点异常 -> 标记失败，原样保留
            duration_ms = (time.perf_counter() - node_started) * 1000.0
            error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            stats[name] = NodeStat(
                name=name, kind=node.kind, status="failed", duration_ms=round(duration_ms, 3),
                thread=threading.get_ident(), deps=node.deps,
                input_summaries=input_summaries, input_digests=input_digests,
                cache_key=cache_key, error=error[:ERROR_LIMIT])
            failures.append(DagNodeError(name, exc))
            return "failed"

        duration_ms = (time.perf_counter() - node_started) * 1000.0
        digest: str | None = None
        if compute_digests:
            try:
                digest = output_digest(output)
            except (TypeError, ValueError) as exc:
                warnings.append(f"output digest failed for {name}: {exc}")
        outputs[name] = output
        if digest is not None:
            digests[name] = digest
        if cache is not None and cache_key is not None and cache_meta is not None \
                and digest is not None:
            cache.save(node.name, cache_key, cache_meta, output, digest, duration_ms)
        stats[name] = NodeStat(
            name=name, kind=node.kind, status="ok", duration_ms=round(duration_ms, 3),
            thread=threading.get_ident(), deps=node.deps,
            input_summaries=input_summaries, input_digests=input_digests,
            output_summary=summary_of(output, node), output_digest=digest,
            cache_key=cache_key)
        return "ok"

    def run_serial() -> None:
        stop = False
        for name in order:
            dep = bad_dep(name)
            if dep is not None:
                mark_skipped(name, f"upstream_failed:{dep}")
                continue
            if stop:
                mark_skipped(name, "aborted")
                continue
            if execute(name) == "failed" and fail_fast:
                stop = True

    def drain_pending(pending: list[str], default_reason: str) -> None:
        """按拓扑序把剩余节点标 skipped；有失败上游的记 upstream_failed。"""
        pending_set = set(pending)
        for name in order:
            if name not in pending_set:
                continue
            dep = bad_dep(name)
            mark_skipped(name, f"upstream_failed:{dep}" if dep else default_reason)
            pending_set.discard(name)
        pending.clear()

    if jobs == 1 or len(order) <= 1:
        run_serial()
    else:
        workers = min(jobs, len(order))
        try:
            pool = ThreadPoolExecutor(max_workers=workers)
        except Exception as exc:                     # 线程池不可用 -> 串行降级
            warnings.append(f"thread pool unavailable, falling back to serial: {exc}")
            run_serial()
        else:
            pending = list(order)
            running: dict[Future, str] = {}
            stop = False

            def consume(future: Future) -> None:
                nonlocal stop
                name = running.pop(future)
                try:
                    result = future.result()
                except BaseException as exc:         # 执行器自身异常（非节点异常）
                    duration_ms = 0.0
                    stats[name] = NodeStat(
                        name=name, kind=dag.nodes[name].kind, status="failed",
                        duration_ms=duration_ms, thread=threading.get_ident(),
                        deps=dag.nodes[name].deps,
                        error=f"{type(exc).__name__}: {exc}"[:ERROR_LIMIT])
                    failures.append(DagNodeError(name, exc))
                    if fail_fast:
                        stop = True
                    return
                if result == "failed" and fail_fast:
                    stop = True

            try:
                while pending or running:
                    # 1) 先把“上游已失败/跳过”的节点标记为 skipped
                    changed = True
                    while changed:
                        changed = False
                        for name in list(pending):
                            if not deps_done(name):
                                continue
                            dep = bad_dep(name)
                            if dep is not None:
                                pending.remove(name)
                                mark_skipped(name, f"upstream_failed:{dep}")
                                changed = True

                    if stop:
                        drain_pending(pending, "aborted")
                        if running:
                            done, _ = wait(list(running), return_when=ALL_COMPLETED)
                            for future in done:
                                consume(future)
                        break

                    # 2) MAIN 节点：等所有在跑的 PURE 结束，然后在编排线程串行执行
                    main_ready = [name for name in pending
                                  if deps_done(name) and dag.nodes[name].kind == "main"]
                    if main_ready:
                        if running:
                            done, _ = wait(list(running), return_when=ALL_COMPLETED)
                            for future in done:
                                consume(future)
                            continue
                        name = main_ready[0]
                        pending.remove(name)
                        if execute(name) == "failed" and fail_fast:
                            stop = True
                        continue

                    # 3) 提交就绪的 PURE 节点
                    slots = workers - len(running)
                    pure_ready = [name for name in pending
                                  if deps_done(name) and dag.nodes[name].kind == "pure"]
                    if pure_ready and slots > 0:
                        for name in pure_ready[:slots]:
                            pending.remove(name)
                            running[pool.submit(execute, name)] = name
                        continue

                    # 4) 等一个完成
                    if running:
                        done, _ = wait(list(running), return_when=FIRST_COMPLETED)
                        for future in done:
                            consume(future)
                        continue

                    # 5) 防御：没有可运行节点也没有在跑的节点
                    drain_pending(pending, "unresolved_dependencies")
                    break
            finally:
                pool.shutdown(wait=True)

    stats_list = [stats[name] for name in order if name in stats]
    order_index = {name: index for index, name in enumerate(order)}
    failures.sort(key=lambda item: order_index.get(item.node, len(order)))
    skipped = [name for name in order if name in skipped_reasons]
    duration_ms = (time.perf_counter() - started) * 1000.0

    if failures and raise_on_error:
        raise DagExecutionError(failures, skipped, failures[0].cause)

    if len(target_list) == 1:
        output = outputs.get(target_list[0])
    else:
        output = {target: outputs.get(target) for target in target_list}
    return DagRun(
        output=output,
        outputs=outputs,
        stats=stats_list,
        cache_hits=sum(1 for stat in stats_list if stat.cache_hit),
        duration_ms=duration_ms,
        warnings=warnings,
    )
