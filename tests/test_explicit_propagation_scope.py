import pytest
from dwg_reader.recognition.v2 import instance_propagator as P
from test_column_family_semantics import _fixture


@pytest.mark.parametrize('category', ['column', 'door', 'window'])
def test_explicit_scope_limits_all_categories(monkeypatch, category):
    ir, family, _ = _fixture()
    family.update(category=category, authorization_scope='representative_only',
                  model={'category': category, 'confidence': 'high'})
    # Use real discovery/signature matching; isolate category fitter from semantics.
    monkeypatch.setattr(P, 'fit_one', lambda cat, inst, **kw: {
        'category': cat, 'instance': inst, 'geometry': {'bbox': inst['bbox']},
        'score': .9, 'status': 'candidate', 'evidence': {}})
    result = P.propagate(family, ir=ir, category=category)
    assert len(result['hypotheses']) == 2
    rep = set(family['representative']['uids'])
    for hyp in result['hypotheses']:
        if set(hyp['uids']) != rep:
            assert hyp['status'] == 'pending'
            assert hyp['evidence'].get('category_confirmed') is not True
