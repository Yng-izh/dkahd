"""Counting requires semantics independently of shape score and layer metadata."""
from copy import deepcopy
from dwg_reader.recognition import elements as E
from dwg_reader.recognition.v2 import proposal_engines as P, global_resolver as G


def candidate(**extra):
    rec = {'bbox': [0, 0, 600, 600], 'outline': [[0,0],[600,0],[600,600],[0,600]],
           'w':600, 'h':600, 'center':[300,300], 'method':'block',
           'srcs':[{'handle':'A','type':'INSERT'}]}
    return dict(rec, **extra)


def test_generic_block_and_independent_geometry_do_not_confer_category():
    for rec in [candidate(), candidate(method='lines')]:
        result = E.fuse_columns([rec], axis=([300],[300]))['columns'][0]
        assert result['geometry_status'] == 'verified'
        assert result['status'] == 'pending'
        assert result['pending_reason'] == 'CATEGORY_UNVERIFIED'


def test_catalog_semantics_survive_fusion_but_cannot_override_bad_geometry():
    catalog = candidate(category_confirmed=True, category_basis='catalog_block')
    rows = E.fuse_columns([candidate(method='contour'), catalog])['columns']
    assert len(rows) == 1 and rows[0]['status'] != 'pending'
    bad = E.fuse_columns([dict(catalog, w=9000)])['columns'][0]
    assert bad['status'] == 'pending'
    assert bad['pending_reason'] == 'SECTION_OUT_OF_RANGE'


def test_pending_high_score_cannot_hide_classified_instance():
    h = {'id':'verified','category':'column','bbox':[0,0,600,600], 'score':.5,
         'status':'candidate','uids':['A'], 'evidence':{'category_confirmed':True,
         'category_basis':'model_family','geometry_status':'verified'},
         'geometry':{'outline':[[0,0],[600,0],[600,600],[0,600]],'w_mm':600,'h_mm':600}}
    p = deepcopy(h)
    p.update(id='pending', score=1, status='pending', evidence={})
    kept, dropped = P.dedupe([p,h])
    assert [r['id'] for r in kept] == ['verified']
    assert dropped == 1
    assert G.solve({'hypotheses':[p]})['selected'] == []
