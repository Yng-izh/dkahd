from dwg_reader.text import clean_mtext, decode_text


def test_decodes_standard_escapes():
    assert decode_text(r"%%C20 45%%d %%P0.5 \U+00D7") == "Ø20 45° ±0.5 ×"


def test_unknown_escape_is_preserved():
    assert decode_text("%%x") == "%%x"


def test_clean_mtext_preserves_content():
    assert clean_mtext(r"{\H2x;Title\PSecond~line}") == "Title\nSecond line"
