from pathlib import Path

import pytest

from dwg_reader.fonts import DEFAULT_FONT_DIR, ShxFont, font_directories, get_font


@pytest.mark.skipif(not (DEFAULT_FONT_DIR / "gbcbig.shx").exists(), reason="font assets absent")
def test_bundled_chinese_font():
    font = get_font("GBCBIG")
    assert isinstance(font, ShxFont)
    assert font.get_char(0xBAA3) == "海"
    assert len(font) == 7019


def test_rejects_non_shx(tmp_path: Path):
    path = tmp_path / "invalid.shx"
    path.write_bytes(b"not a font")
    with pytest.raises(ValueError):
        ShxFont(path)


def test_explicit_font_directory_has_priority(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("DWG_READER_FONT_DIR", str(tmp_path))
    assert font_directories()[0] == tmp_path.resolve()
