from types import SimpleNamespace

from dwg_reader.recognition.v2 import multimodal_detector as D


def test_rendered_family_missing_consistency_cannot_authorize_siblings(monkeypatch):
    family = {'id': 'f', 'instances': [
        {'id': 'a', 'bbox': [0, 0, 10, 10]},
        {'id': 'b', 'bbox': [20, 0, 30, 10]}]}
    monkeypatch.setattr(D, '_sample_views', lambda *a, **kw: (
        ['a', 'b'], [{'instance': 'a'}, {'instance': 'b'}]))
    gateway = SimpleNamespace(available=True, ask=lambda **kw: SimpleNamespace(
        ok=True, data={'component': {'category': 'column', 'confidence': 'high'}}, images=2))
    result = D.read_family(family, ir={}, gateway=gateway)
    assert result['status'] == 'ok'
    assert result['authorization_scope'] == 'representative_only'


def test_holdout_facts_describe_actual_observed_member():
    family = {'id': 'f', 'n': 2, 'representative': {'id': 'a'},
              'instances': [{'id': 'a', 'bbox': [0, 0, 10, 10]},
                            {'id': 'b', 'bbox': [20, 0, 30, 10]}]}
    result = D.read_family(family, ir={}, budget={
        'verification': True, 'primary_sample_ids': ['a']})
    assert result['samples'][0]['instance'] == 'b'
    assert result['facts']['observed_instance'] == 'b'
