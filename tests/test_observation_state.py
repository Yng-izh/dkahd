"""Offline coverage continuation: no semantic replay or network requests."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from dwg_reader.recognition import v2
from dwg_reader.recognition.v2 import agent_controller as AG
from dwg_reader.recognition.v2 import multimodal_detector as DET
from dwg_reader.recognition.v2 import multimodal_bootstrap as BOOT
from dwg_reader.recognition.v2 import observation_state as OBS


@pytest.fixture
def setup(monkeypatch):
    asked, generated, bootstrap = [], [], []
    gateway = SimpleNamespace(available=True, ask=lambda **kw: asked.append(kw))

    def read(family, **kwargs):
        gateway.ask(family=family['id'])
        return dict(status='ok', calls=1, category='door', confidence='high')

    def generate(*args, **kwargs):
        generated.append(deepcopy(kwargs['families']))
        return {'hypotheses': []}

    def boot(**kwargs):
        bootstrap.append(kwargs['budget']['max_calls'])
        return {'calls': 0, 'items': [], 'images_seen': 0}

    monkeypatch.setattr(DET, 'read_family', read)
    monkeypatch.setattr(AG.ENGINES, 'generate', generate)
    monkeypatch.setattr(BOOT, 'run', boot)
    kwargs = dict(ir={}, snapshot={'entities': [], 'text': 'full snapshot'},
                  families=[dict(id=f'f{i}', kind='block', category_guess='door',
                                 n=1, priority=1, instances=[], bbox=[i, 0, i+1, 1])
                            for i in range(30)],
                  gateway=gateway, ai={'mode': 'propose'},
                  profile={'profile': 'openings_columns', 'arbitration': False,
                           'bootstrap_calls': 0, 'detector_families': 1,
                           'detector_max_families': 30, 'family_closeup': 0},
                  budget={'max_model_calls': 12, 'max_recheck': 0})
    return kwargs, asked, generated, bootstrap


def test_disjoint_roundtrip_cap_no_semantic_reuse(setup):
    kwargs, asked, generated, bootstrap = setup
    first = AG.run(**kwargs)
    saved = json.loads(json.dumps(first['observation_state']))
    second = AG.run(**kwargs, observation_state=saved)
    a = set(saved['attempted_ids'])
    b = set(second['observation_state']['attempted_ids']) - a
    assert len(a) == len(b) == 12
    assert len(asked) == 24
    assert first['ai']['calls'] == second['ai']['calls'] == 12
    assert all(not f['confirmed'] for f in generated[1] if f['id'] in a)
    assert bootstrap == [0, 0]
    coverage = second['detector']['observation']['coverage']
    assert coverage['round']['attempted']['count'] == 12
    assert coverage['total']['successful']['count'] == 24
    assert coverage['unseen']['count'] == 6
    assert coverage['total']['attempted']['by_category'] == {'door': 24}


@pytest.mark.parametrize('change', ['snapshot', 'family', 'profile', 'budget', 'identity'])
def test_mismatch_before_any_model_call(setup, change):
    kwargs, asked, _, bootstrap = setup
    state = AG.run(**kwargs)['observation_state']
    asked.clear(); bootstrap.clear()
    kwargs = deepcopy(kwargs)
    if change == 'snapshot':
        kwargs['snapshot']['text'] += ' changed'
    elif change == 'family':
        kwargs['families'][-1]['instances'].append({'id': 'hidden', 'uids': ['x']})
    elif change == 'profile':
        kwargs['profile']['family_samples'] = 7
    elif change == 'budget':
        kwargs['budget']['max_families'] = 1
    else:
        kwargs['drawing_identity'] = 'other drawing'
    with pytest.raises(ValueError, match='mismatch'):
        AG.run(**kwargs, observation_state=state)
    assert not asked and not bootstrap


@pytest.mark.parametrize('key', ['confirmed', 'category', 'verdict', 'final_counts'])
def test_injected_semantics_rejected(setup, key):
    kwargs, asked, _, bootstrap = setup
    state = AG.run(**kwargs)['observation_state']
    state[key] = True
    asked.clear(); bootstrap.clear()
    with pytest.raises(ValueError, match='coverage only'):
        AG.run(**kwargs, observation_state=state)
    assert not asked and not bootstrap


@pytest.mark.parametrize('calls', [0, 1])
def test_failed_call_separate_from_success(setup, monkeypatch, calls):
    kwargs, _, _, _ = setup
    monkeypatch.setattr(DET, 'read_family', lambda *a, **kw: {
        'status': 'error', 'calls': calls, 'reason': 'OFFLINE_FAILURE'})
    result = AG.run(**kwargs)
    state = result['observation_state']
    assert len(state['attempted_ids']) == (12 if calls else 0)
    assert not state['successful_ids']
    assert result['detector']['observation']['coverage']['unseen']['count'] == (18 if calls else 30)


def test_analyze_public_state_threading(setup, monkeypatch):
    kwargs, _, _, _ = setup
    kwargs.pop('ir')
    monkeypatch.setattr(v2.IR, 'build', lambda *a, **kw: {})
    first = v2.analyze(**kwargs)
    second = v2.analyze(**kwargs, observation_state=first['observation_state'])
    assert len(second['observation_state']['attempted_ids']) == 24


def test_fingerprint_full_snapshot_and_order_and_no_credentials():
    kwargs = dict(snapshot={'entities': [{'id': str(i)} for i in range(1000)]},
                  ir={}, families=[{'id': 'b'}, {'id': 'a'}], profile={}, budget={},
                  ai={'provider': {'api_key': 'secret'}})
    digest = OBS.fingerprint(**kwargs)
    kwargs['ai']['provider']['api_key'] = 'rotated'
    kwargs['families'].reverse()
    assert OBS.fingerprint(**kwargs) == digest
    kwargs['snapshot']['entities'][-1]['geometry'] = [1, 2, 3]
    assert OBS.fingerprint(**kwargs) != digest


def test_gateway_preflight_failure_is_not_observed(setup, monkeypatch):
    kwargs, _, _, _ = setup
    kwargs['gateway'].calls = 0
    monkeypatch.setattr(DET, 'read_family', lambda *a, **kw: {
        'status': 'error', 'calls': 1, 'reason': 'BUDGET_EXHAUSTED'})
    result = AG.run(**kwargs)
    assert result['observation_state']['attempted_ids'] == []


def test_run_drawing_threads_identity_and_saved_state(tmp_path, monkeypatch):
    import hashlib
    from dwg_reader.recognition import pipeline
    drawing = tmp_path / 'offline.dxf'
    drawing.write_bytes(b'full DXF bytes')
    monkeypatch.setattr(pipeline, 'run_recognition', lambda *a, **kw:
                        SimpleNamespace(outputs={'load': {'snapshot': {}, 'scale': {}}}))
    captured = []
    def analyze(**kw):
        captured.append(kw)
        return {'observation_state': kw['observation_state'], 'ms': 0}
    monkeypatch.setattr(v2, 'analyze', analyze)
    saved = {'saved': 'coverage'}
    result = v2.run_drawing(str(drawing), ai_config={'mode': 'off'},
                            observation_state=saved)
    assert result['observation_state'] == saved
    assert captured[0]['observation_state'] == saved
    assert captured[0]['drawing_identity'] == hashlib.sha256(drawing.read_bytes()).hexdigest()
