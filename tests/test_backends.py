from __future__ import annotations

import json
from pathlib import Path

import pytest

from dwg_reader.backends import convert_dwg_file, doctor_report, openscad_extrude
from dwg_reader.cli import main


def test_doctor_has_stable_backend_contract(capsys):
    assert set(doctor_report()["backends"]) == {"oda", "libredwg", "openscad", "opencad"}
    assert main(["doctor", "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["python"]["available"] is True


def test_openscad_rejects_missing_input(tmp_path: Path):
    with pytest.raises(ValueError, match="DXF input not found"):
        openscad_extrude(tmp_path / "missing.dxf", tmp_path / "out.stl")


def test_converter_rejects_missing_input(tmp_path: Path):
    with pytest.raises(ValueError, match="DWG/T3 input not found"):
        convert_dwg_file(tmp_path / "missing.dwg", tmp_path / "out")
