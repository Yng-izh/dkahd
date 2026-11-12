"""Generalization regressions: different drawing conventions without GT leakage."""
import math
from dwg_reader.recognition import elements as E, walls as W, layer_roles_infer as LRI


def line(h, a, b, layer='WALL'):
    return {'handle': h, 'type': 'LINE', 'start': list(a), 'end': list(b), 'layer': layer}


def rotated(p, a):
    u, v = math.cos(a), math.sin(a)
    return [u*p[0] - v*p[1], v*p[0] + u*p[1]]


def test_short_wall_virtual_merging_rotates():
    a = math.radians(30)
    segs = [line(f'h{i}', rotated((x, 0), a), rotated((x+400, 0), a))
            for i, x in enumerate((0, 401, 802))]
    vs, mapping = W.virtual_from_shorts({'entities': segs, 'derived': []}, layers=['WALL'])
    assert len(vs['entities']) == 1
    assert len(next(iter(mapping.values()))) == 3
    p, q = vs['entities'][0]['start'], vs['entities'][0]['end']
    assert math.isclose(math.hypot(q[0]-p[0], q[1]-p[1]), 1202, abs_tol=2)


def test_single_line_candidates_rotates():
    a = math.radians(30)
    snap = {'entities': [line('one', rotated((0, 0), a), rotated((4000, 0), a))], 'derived': []}
    recs = W.single_line_candidates(snap, layers=['WALL'])
    assert len(recs) == 1
    assert recs[0]['orient'].startswith('O')
    assert len(recs[0]['outline']) == 4


def test_mixed_door_candidates_with_existing_block(monkeypatch):
    # An arc-drawn door should still be admitted if a block door exists.
    monkeypatch.setattr(E, '_doors_from_arcs', lambda *a: [{
        'handle': 'arc1', 'insert': [3000, 0], 'width': 900, 'layer': 'DOOR',
        'block': None, 'opening': [[3000,0],[3900,0]]}])
    monkeypatch.setattr(E, '_door_geom', lambda *a: None)
    snap = {'entities': [{'type': 'INSERT', 'name': next(iter(E.DOOR_BLOCKS)),
                          'xscale': 900, 'yscale': 1, 'insert': [0, 0],
                          'rotation': 0, 'layer': 'DOOR', 'handle': 'block1'}],
            'blocks': {}}
    got = E.recognize_doors(snap, [], walls=[{'start':[0,0], 'end':[5000,0]}], layers=['DOOR'])
    assert {d['handle'] for d in got} == {'arc1', 'block1'}


def test_oriented_wall_gap_rotates():
    def transform(p): return rotated(p, math.radians(30))
    walls = [dict(start=transform([0, 0]), end=transform([500, 0])),
             dict(start=transform([1500, 0]), end=transform([2500, 0]))]
    assert E._opening_on_wall_gap(walls, [transform([550, 0]), transform([1450, 0])], 200)
    assert not E._opening_on_wall_gap(walls, [transform([1700, 0]), transform([2100, 0])], 200)


def test_layer_soft_score_is_auditable():
    roles = LRI.infer_roles({'UNKNOWN': {k: 0.0 for k in ('text_ratio', 'line_ratio', 'closed_ratio', 'arc_ratio', 'fill_ratio', 'long_pair_rate', 'thickness_concentration', 'long_ratio', 'cross_span', 'short_pair_rate', 'on_wall_ratio', 'thickness_mm', 'block_ratio', 'n', 'pair_rate')}})
    assert 'role_scores' in roles['UNKNOWN']


def test_rotated_four_line_column():
    angle = math.radians(30)
    pts = [rotated(x, angle) for x in ([0,0],[400,0],[400,500],[0,500])]
    segs = [line('col'+str(i), pts[i], pts[(i+1)%4], 'COLUMN') for i in range(4)]
    candidates = E._rotated_rects_from_lines({'entities': segs, 'derived': []}, {'COLUMN'})
    assert len(candidates) == 1
    assert 395 <= candidates[0]['w'] <= 405


def test_room_labels_follow_resolved_drawing_plan():
    from types import SimpleNamespace
    from dwg_reader.recognition import nodes as N
    ctx = SimpleNamespace(
        inputs={"load": {"plan": {"layers": {"room_name": ["UNKNOWN_ROOM_LAYER"]}}},
                "texts": [{"text": "会议室", "x": 100, "y": 100, "h": 300,
                            "handle": "a", "layer": "UNKNOWN_ROOM_LAYER"}],
                "envelope": [0, 0, 1000, 1000]},
        params={"profile": None})
    got = N.node_room_labels(ctx)
    assert len(got["labels"]) == 1
    assert got["labels"][0]["name"] == "会议室"
