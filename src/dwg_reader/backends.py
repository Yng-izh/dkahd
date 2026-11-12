"""Discovery and safe subprocess adapters for optional CAD backends."""

from __future__ import annotations

import glob
import importlib.util
import os
import platform
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# Install locations scanned when the executable is not on PATH.
# Glob patterns are expanded with environment variables, newest version wins.
ODA_GLOBS = (
    # Windows: per-version folders such as "C:\Program Files\ODA\ODAFileConverter 26.4.0"
    r"%ProgramFiles%\ODA\*\ODAFileConverter.exe",
    r"%ProgramFiles%\ODA\*\ODAFileConverter\ODAFileConverter.exe",
    r"%ProgramFiles%\ODA\*\ODAFileConverter\ODAFileConverterApp.exe",
    r"%ProgramFiles%\ODAFileConverter*\ODAFileConverter.exe",
    r"%ProgramFiles(x86)%\ODA\*\ODAFileConverter.exe",
    r"%LOCALAPPDATA%\Programs\ODA\*\ODAFileConverter.exe",
    r"%LOCALAPPDATA%\ODAFileConverter\ODAFileConverter.exe",
    r"%LOCALAPPDATA%\ODA\*\ODAFileConverter.exe",
    r"%ProgramW6432%\ODA\*\ODAFileConverter.exe",
    # Linux packages
    "/opt/oda/*/ODAFileConverter",
    "/opt/*/ODAFileConverter",
    # macOS bundle
    "/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter",
)

#: Files handled as Autodesk DWG containers (Tianzheng's ``.t3`` export is a DWG).
DWG_SUFFIXES = (".dwg", ".t3", ".dwt")

INSTALL_HINTS = {    "Windows": (
        "下载 ODA File Converter Windows x64 安装包并安装；"
        "默认会装到 C:\\Program Files\\ODA\\ODAFileConverter <版本>\\ODAFileConverter.exe。"
        "安装后重启终端，或设置环境变量 DWG_READER_ODA_PATH 指向该 exe。"
    ),
    "Darwin": (
        "下载 macOS 安装包，拖入 /Applications 后执行 "
        'xattr -dr com.apple.quarantine "/Applications/ODAFileConverter.app"；'
        "或设置 DWG_READER_ODA_PATH 指向 app 内的 ODAFileConverter。"
    ),
    "Linux": (
        "安装 ODA File Converter deb/rpm 包（通常位于 /usr/bin/ODAFileConverter）；"
        "或设置 DWG_READER_ODA_PATH 指向可执行文件。"
    ),
}


@dataclass(frozen=True)
class BackendStatus:
    name: str
    available: bool
    path: str | None
    purpose: str
    version: str | None = None


def _expand_pattern(pattern: str) -> str:
    return os.path.expandvars(os.path.expanduser(pattern))


def _version_key(path: str) -> tuple:
    """Sort key preferring higher version numbers found in the path."""
    numbers = tuple(int(part) for part in re.findall(r"\d+", path) if len(part) <= 3)
    return numbers


def _scan_known_locations(patterns: tuple[str, ...] = ODA_GLOBS) -> str | None:
    matches: list[str] = []
    for pattern in patterns:
        matches.extend(glob.glob(_expand_pattern(pattern)))
    candidates = [item for item in matches if Path(item).is_file()]
    if not candidates:
        return None
    return str(Path(max(candidates, key=_version_key)).resolve())


def _find(
    env_name: str,
    *commands: str,
    mac_path: str | None = None,
    glob_patterns: tuple[str, ...] = (),
) -> str | None:
    configured = os.environ.get(env_name)
    if configured and Path(configured).is_file():
        return str(Path(configured).resolve())
    for command in commands:
        if found := shutil.which(command):
            return found
    if glob_patterns and (scanned := _scan_known_locations(glob_patterns)):
        return scanned
    if mac_path and Path(mac_path).is_file():
        return mac_path
    return None


def oda_install_hint() -> str:
    return INSTALL_HINTS.get(platform.system()) or (
        "安装 ODA File Converter 后重启终端，或设置 DWG_READER_ODA_PATH 指向可执行文件；"
        "也可直接传入 DXF 文件跳过 DWG 转换。"
    )


def _version(path: str | None, *args: str) -> str | None:
    if not path:
        return None
    try:
        result = subprocess.run(
            [path, *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return (result.stdout or result.stderr).strip().splitlines()[0][:200] or None
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return None


_SILENT_ODA = os.path.expanduser(
    "~/Library/Application Support/dsh-oda/ODAFileConverter.app/Contents/MacOS/ODAFileConverter"
)


def _env_oda() -> str | None:
    """显式环境变量且路径存在时优先；用于覆盖静默副本/系统安装。"""
    configured = os.environ.get("DWG_READER_ODA_PATH")
    if not configured:
        return None
    path = Path(os.path.expanduser(configured))
    if path.is_file():
        return str(path.resolve())
    return None


def _preferred_oda() -> str | None:
    """本地静默副本（LSUIElement）优先于 /Applications 原版，避免 Dock 弹窗。"""
    return _SILENT_ODA if Path(_SILENT_ODA).is_file() else None


def discover_backends() -> dict[str, BackendStatus]:
    # 优先级：DWG_READER_ODA_PATH（路径存在）-> 静默副本 -> 常规 _find
    # （shutil.which / 已知安装位置 / /Applications 原版）。
    oda = _env_oda() or _preferred_oda() or _find(
        "DWG_READER_ODA_PATH",
        "ODAFileConverter",
        glob_patterns=ODA_GLOBS,
        mac_path="/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter",
    )
    libredwg = _find("DWG_READER_LIBREDWG_PATH", "dwgread")
    openscad = _find(
        "OPENSCAD_PATH",
        "openscad",
        glob_patterns=(
            r"%ProgramFiles%\OpenSCAD\openscad.exe",
            r"%ProgramFiles%\OpenSCAD\*.exe",
            "/usr/bin/openscad",
            "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD",
        ),
        mac_path="/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD",
    )
    opencad = _find("OPENCAD_PATH", "opencad")
    return {
        "oda": BackendStatus("oda", bool(oda), oda, "DWG to canonical DXF"),
        "libredwg": BackendStatus(
            "libredwg",
            bool(libredwg),
            libredwg,
            "open-source DWG fallback",
            _version(libredwg, "--version"),
        ),
        "openscad": BackendStatus(
            "openscad",
            bool(openscad),
            openscad,
            "DXF extrusion, render, STL/3MF export",
            _version(openscad, "--version"),
        ),
        "opencad": BackendStatus(
            "opencad",
            bool(opencad),
            opencad,
            "OCCT parametric STEP/STL modeling",
            _version(opencad, "--version"),
        ),
    }


def doctor_report() -> dict[str, Any]:
    try:
        import ezdxf

        ezdxf_version = ezdxf.__version__
    except ImportError:
        ezdxf_version = None
    mcp_available = importlib.util.find_spec("mcp") is not None
    backends = discover_backends()
    report = {
        "backends": {name: asdict(status) for name, status in backends.items()},
        "python": {"available": True, "version": os.sys.version.split()[0]},
        "platform": platform.system(),
        "ezdxf": {"available": ezdxf_version is not None, "version": ezdxf_version},
        "mcp": {"available": mcp_available},
    }
    if not backends["oda"].available:
        report["hints"] = {"oda": oda_install_hint()}
    return report


def openscad_extrude(
    dxf: str | Path,
    output: str | Path,
    *,
    height: float = 10.0,
    layer: str | None = None,
    convexity: int = 10,
    timeout: int = 300,
) -> dict[str, Any]:
    """Extrude a 2D DXF with OpenSCAD and export by output extension."""
    source, target = Path(dxf).resolve(), Path(output).resolve()
    if not source.is_file() or source.suffix.lower() != ".dxf":
        raise ValueError(f"DXF input not found: {source}")
    if height <= 0:
        raise ValueError("height must be greater than zero")
    backend = discover_backends()["openscad"]
    if not backend.path:
        raise ValueError("OpenSCAD not found; set OPENSCAD_PATH or install OpenSCAD")
    target.parent.mkdir(parents=True, exist_ok=True)
    layer_arg = f", layer={_scad_string(layer)}" if layer else ""
    source_code = (
        f"linear_extrude(height={height:g}, convexity={int(convexity)})\n"
        f"    import(file={_scad_string(str(source))}{layer_arg});\n"
    )
    with tempfile.TemporaryDirectory(prefix="dwg-reader-openscad-") as temp_dir:
        scad = Path(temp_dir) / "model.scad"
        scad.write_text(source_code, encoding="utf-8")
        result = subprocess.run(
            [backend.path, "-o", str(target), str(scad)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    if result.returncode != 0 or not target.is_file():
        raise RuntimeError((result.stderr or result.stdout or "OpenSCAD export failed").strip())
    return {
        "input": str(source),
        "output": str(target),
        "height": height,
        "layer": layer,
        "backend": backend.path,
        "bytes": target.stat().st_size,
    }


def convert_dwg_file(
    source: str | Path,
    output_dir: str | Path,
    *,
    backend: str = "auto",
    oda_path: str | Path | None = None,
) -> dict[str, Any]:
    """Convert one DWG/T3 drawing to DXF using ODA first or the LibreDWG fallback."""
    source_path, target_dir = Path(source).resolve(), Path(output_dir).resolve()
    if not source_path.is_file() or source_path.suffix.lower() not in DWG_SUFFIXES:
        raise ValueError(f"DWG/T3 input not found: {source_path}")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{source_path.stem}.dxf"
    statuses = discover_backends()
    if oda_path and Path(oda_path).is_file():
        statuses["oda"] = BackendStatus(
            "oda", True, str(Path(oda_path).resolve()), "DWG to canonical DXF"
        )
    selected = backend
    if backend == "auto":
        if statuses["oda"].available:
            selected = "oda"
        elif statuses["libredwg"].available:
            selected = "libredwg"
        else:
            raise ValueError("DWG→DXF 需要 ODA File Converter 或 LibreDWG。" + oda_install_hint())
    if selected == "oda":
        executable = statuses["oda"].path
        if not executable:
            raise ValueError(f"ODA File Converter not found. {oda_install_hint()}")
        return _convert_with_oda(executable, source_path, target_dir, target)
    if selected == "libredwg":
        executable = statuses["libredwg"].path
        if not executable:
            raise ValueError(
                "LibreDWG dwgread not found; install LibreDWG or set DWG_READER_LIBREDWG_PATH"
            )
        command = [executable, "-O", "DXF", "-o", str(target), str(source_path)]
    else:
        raise ValueError(f"unsupported converter backend: {backend}")
    result = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
    if result.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        raise RuntimeError((result.stderr or result.stdout or "DWG conversion failed").strip())
    return {
        "input": str(source_path),
        "output": str(target),
        "backend": selected,
        "bytes": target.stat().st_size,
    }


def _oda_error_file(target: Path) -> Path:
    """ODA writes ``<name>.dxf.err`` beside the output when it hits a problem."""
    return target.parent / f"{target.name}.err"


def has_entities(path: str | Path) -> bool:
    """Cheaply check whether a DXF document contains an ENTITIES section."""
    with Path(path).open(encoding="utf-8", errors="replace") as stream:
        while code := stream.readline():
            value = stream.readline()
            if not value:
                return False
            if code.strip() == "0" and value.strip() == "SECTION":
                next_code, next_value = stream.readline(), stream.readline()
                if next_code.strip() == "2" and next_value.strip() == "ENTITIES":
                    return True
    return False


def _convert_with_oda(
    executable: str, source: Path, target_dir: Path, target: Path
) -> dict[str, Any]:
    """Convert one DWG, retrying older DXF versions and surfacing ODA errors.

    The conversion runs in a scratch directory so a failed or partial result can
    never overwrite an existing DXF next to the drawing.  Non-``.dwg`` containers
    (Tianzheng ``.t3``/``.dwt``) are copied to a ``.dwg`` name first, because ODA
    filters its input directory by file name pattern.
    """
    detail = ""
    for version in ("ACAD2018", "ACAD2013", "ACAD2007"):
        with tempfile.TemporaryDirectory(prefix="dwg-reader-oda-") as temp_dir:
            scratch = Path(temp_dir)
            out_dir = scratch / "out"
            out_dir.mkdir()
            if source.suffix.lower() == ".dwg":
                input_dir = source.parent
                input_name = source.name
            else:
                input_dir = scratch
                input_name = f"{source.stem}.dwg"
                shutil.copy2(source, input_dir / input_name)
            command = [
                executable,
                str(input_dir),
                str(out_dir),
                version,
                "DXF",
                "0",
                "0",
                input_name,
            ]
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=600, check=False
            )
            produced = out_dir / f"{Path(input_name).stem}.dxf"
            error_file = _oda_error_file(produced)
            if error_file.exists():
                detail = error_file.read_text(encoding="utf-8", errors="replace").strip() or detail
            if (
                result.returncode == 0
                and produced.is_file()
                and produced.stat().st_size > 0
                and has_entities(produced)
            ):
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(produced, target)
                return {
                    "input": str(source),
                    "output": str(target),
                    "backend": "oda",
                    "version": version,
                    "bytes": target.stat().st_size,
                }
            if result.returncode != 0:
                detail = detail or (result.stderr or result.stdout or "").strip()
    reason = detail or "ODA 未能生成包含实体的 DXF"
    raise RuntimeError(
        f"DWG 转换失败（{source.name}）：{reason}。"
        "常见原因：源 DWG 含非法符号表名、文件已损坏或加密。"
        "可在 CAD 软件中打开另存为 DXF 后再用本工具解析。"
    )


def _scad_string(value: str | None) -> str:
    escaped = (value or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
