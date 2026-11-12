"""profile 注册表：按 DXF 内容哈希自动复用离线适配方案。

``recognize(path)`` 的签名保持不变：执行前先按文件内容哈希查注册表，命中就用
已验证的 profile，未命中就用确定性默认方案。这样"离线 LLM 适配"的结果会自动
生效，不需要每次手工传 ``profile=``。

存放位置（依次尝试）：

1. ``$DWG_PROFILE_REGISTRY``；
2. 图纸同目录下的 ``.dwg-profiles/``；
3. 当前工作目录下的 ``.dwg-profiles/``。

只存**已验证**的 profile：``register()`` 写入前会再做一次闭集校验，校验失败
直接抛错，不会把不可执行的方案写进注册表。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .operators import OPERATORS
from .plan import SEMANTICS

PROFILE_VERSION = "2.0"
DIR_NAME = ".dwg-profiles"
ENV_DIR = "DWG_PROFILE_REGISTRY"
#: 置信度低于该值的图层映射视为"模型没把握"，注册时丢弃
MIN_CONFIDENCE = 0.5


def digest_of(dxf_path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(dxf_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def default_dir(dxf_path: str | None = None) -> Path | None:
    env = os.environ.get(ENV_DIR)
    if env:
        return Path(env).expanduser()
    if dxf_path:
        return Path(dxf_path).resolve().parent / DIR_NAME
    return Path.cwd() / DIR_NAME


def profile_path(digest: str, root: Path) -> Path:
    return Path(root) / digest[:2] / f"{digest}.json"


def validate_profile(profile: dict) -> dict:
    """闭集校验：语义枚举、置信度、算子名、单位假设。"""
    if not isinstance(profile, dict):
        raise ValueError("profile 必须是 JSON 对象")
    layers: dict[str, dict] = {}
    dropped = []
    for name, item in (profile.get("layers") or {}).items():
        if not isinstance(item, dict):
            continue
        sems = item.get("semantics", item.get("semantic", "ignore"))
        if isinstance(sems, str):
            sems = [sems]
        sems = [s for s in sems if s in SEMANTICS] or ["ignore"]
        conf = float(item.get("confidence", 0.0))
        if conf < MIN_CONFIDENCE:
            dropped.append(name)
            continue
        layers[name] = {"semantics": sems, "confidence": conf,
                        "reason": str(item.get("reason", ""))[:40],
                        "source": item.get("source")}
    hints = {}
    bad_ops = []
    for sem, names in (profile.get("operator_hints") or {}).items():
        if sem not in SEMANTICS:
            continue
        keep = []
        for name in names or ():
            if name in OPERATORS:
                keep.append(name)
            else:
                bad_ops.append(name)
        if keep:
            hints[sem] = keep
    out = dict(profile)
    out["profile_version"] = profile.get("profile_version") or PROFILE_VERSION
    out["layers"] = layers
    out["operator_hints"] = hints
    out["symbols"] = profile.get("symbols") or {}
    out["scope"] = profile.get("scope") or {}
    units = (profile.get("unit_hypothesis") or profile.get("units") or "").lower()
    out["unit_hypothesis"] = units or None
    out["dropped_layers"] = dropped
    out["dropped_operators"] = sorted(set(bad_ops))
    return out


def register(profile: dict, *, dxf_path: str | None = None,
             digest: str | None = None, root: str | os.PathLike[str] | None = None,
             validate: bool = True) -> Path:
    """把一份已验证的 profile 按内容哈希写入注册表，返回写入路径。"""
    if digest is None:
        if not dxf_path:
            raise ValueError("register() 需要 dxf_path 或 digest")
        digest = digest_of(dxf_path)
    prof = validate_profile(profile) if validate else dict(profile)
    prof.setdefault("profile_version", PROFILE_VERSION)
    prof["digest"] = digest
    prof["source"] = prof.get("source") or dxf_path
    target_dir = Path(root) if root else default_dir(dxf_path)
    path = profile_path(digest, target_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prof, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def lookup(dxf_path: str | None = None, *, digest: str | None = None,
           root: str | os.PathLike[str] | None = None) -> dict | None:
    """按内容哈希查 profile；没有就返回 None（调用方用确定性默认方案）。"""
    if digest is None:
        if not dxf_path:
            return None
        try:
            digest = digest_of(dxf_path)
        except OSError:
            return None
    roots = [Path(root)] if root else [d for d in (default_dir(dxf_path),) if d]
    for base in roots:
        path = profile_path(digest, base)
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        data.setdefault("source", str(path))
        data["registry_file"] = str(path)
        return data
    return None


def list_profiles(root: str | os.PathLike[str] | None = None) -> list[dict]:
    base = Path(root) if root else default_dir()
    if not base or not base.exists():
        return []
    out = []
    for path in sorted(base.glob("*/*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"digest": data.get("digest") or path.stem,
                    "layers": len(data.get("layers") or {}),
                    "source": data.get("source"),
                    "file": str(path)})
    return out
