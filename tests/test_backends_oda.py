"""Tests for cross-platform backend discovery (no real CAD tools required)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dwg_reader import backends


def test_env_override_is_preferred(tmp_path, monkeypatch):
    fake = tmp_path / "ODAFileConverter.exe"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setenv("DWG_READER_ODA_PATH", str(fake))
    status = backends.discover_backends()["oda"]
    assert status.available is True
    assert Path(status.path) == fake.resolve()


def test_missing_env_override_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("DWG_READER_ODA_PATH", str(tmp_path / "nope.exe"))
    monkeypatch.setattr(backends, "_preferred_oda", lambda: None)
    monkeypatch.setattr(backends, "_find", lambda *a, **k: None)
    # 显式路径不存在 + 静默副本/_find 都被隔离，discovery 报告缺失。
    assert backends.discover_backends()["oda"].available is False


def test_silent_copy_preferred_over_applications(tmp_path, monkeypatch):
    silent = tmp_path / "ODAFileConverter"
    silent.write_text("", encoding="utf-8")
    monkeypatch.delenv("DWG_READER_ODA_PATH", raising=False)
    monkeypatch.setattr(backends, "_preferred_oda", lambda: str(silent))
    monkeypatch.setattr(
        backends, "_find",
        lambda *a, **k: "/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter")
    status = backends.discover_backends()["oda"]
    assert status.available is True
    assert Path(status.path) == silent


def test_scan_known_locations_picks_highest_version(tmp_path):
    older = tmp_path / "ODA 24.0"
    newer = tmp_path / "ODA 26.4"
    for folder in (older, newer):
        folder.mkdir()
        (folder / "ODAFileConverter.exe").write_text("", encoding="utf-8")
    found = backends._scan_known_locations((str(tmp_path / "ODA *" / "ODAFileConverter.exe"),))
    assert found is not None
    assert "26.4" in found


def test_scan_known_locations_returns_none_when_absent(tmp_path):
    assert backends._scan_known_locations((str(tmp_path / "missing" / "*.exe"),)) is None


def test_install_hint_mentions_platform_mechanism():
    hint = backends.oda_install_hint()
    assert hint
    assert "DWG_READER_ODA_PATH" in hint


def test_doctor_report_shape(monkeypatch):
    monkeypatch.setattr(backends, "_preferred_oda", lambda: None)
    monkeypatch.setattr(backends, "_find", lambda *a, **k: None)
    report = backends.doctor_report()
    assert report["platform"]
    assert set(report["backends"]) == {"oda", "libredwg", "openscad", "opencad"}
    assert report["python"]["available"] is True
    assert "hints" in report
    assert "oda" in report["hints"]


def test_doctor_report_omits_hints_when_oda_present(tmp_path, monkeypatch):
    fake = tmp_path / "ODAFileConverter"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setenv("DWG_READER_ODA_PATH", str(fake))
    report = backends.doctor_report()
    assert report["backends"]["oda"]["available"] is True
    assert "hints" not in report


def test_convert_reports_missing_backend(tmp_path, monkeypatch):
    source = tmp_path / "drawing.dwg"
    source.write_bytes(b"AC1032")
    monkeypatch.setattr(backends, "_preferred_oda", lambda: None)
    monkeypatch.setattr(backends, "_find", lambda *a, **k: None)
    with pytest.raises(ValueError, match="ODA File Converter"):
        backends.convert_dwg_file(source, tmp_path / "out")


@pytest.mark.parametrize("suffix", [".dxf", ".DXF"])
def test_lossless_scanner_handles_case_insensitive_glob(tmp_path, suffix):
    folder = tmp_path / f"ODA 26.4{suffix}"
    folder.mkdir()
    exe = folder / "ODAFileConverter.exe"
    exe.write_text("", encoding="utf-8")
    pattern = str(tmp_path / "ODA *" / "ODAFileConverter.exe")
    if os.name == "nt":  # Windows globbing is case-insensitive, POSIX is not.
        assert backends._scan_known_locations((pattern,)) is not None
