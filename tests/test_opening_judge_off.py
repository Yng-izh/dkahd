from types import SimpleNamespace
from dwg_reader.recognition import nodes as N
from dwg_reader.recognition import llm_interpret as L


def test_opening_judge_disabled_review_never_uses_legacy_model(monkeypatch):
    item = {'id': 'ambiguous'}
    monkeypatch.setattr(N.OPCASES, 'candidates', lambda **kw: [item])
    monkeypatch.setattr(N.OPCASES, 'triage', lambda item: ('pending', None))
    def forbidden(*a, **kw):
        raise AssertionError('legacy model called with review disabled')
    monkeypatch.setattr(L, 'judge', forbidden)
    ctx = SimpleNamespace(inputs={'load': {'snapshot': {}}, 'wall_model': {'wall_model': {}},
                                  'spaces_seed': [], 'doors': [], 'windows': []},
                          params={'ai_mode': 'off', 'llm_review': False}, file_digest='test')
    result = N.node_opening_judge(ctx)
    assert result['decisions'] == {}
    assert result['status'] == 'no_action'
