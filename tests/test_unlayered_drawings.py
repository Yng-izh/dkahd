"""Regressions for drawings in which layers provide no usable semantics."""
from dataclasses import dataclass

from dwg_reader.recognition import unlayered
from dwg_reader.recognition.nodes import _shape_blocks, _room_label_source
from dwg_reader.recognition.rooms import room_labels

@dataclass
class DXFStub:
    layer: str
    def get(self, name, fallback=None):
        return getattr(self,name,fallback)

class Entity:
    def __init__(self, layer, type_):
        self.dxf = DXFStub(layer)
        self.kind = type_
    def dxftype(self):
        return self.kind

class Ctx:
    def __init__(self, inputs):
        self.inputs = inputs


def test_all_entities_on_layer_zero_supported_without_mutating_cad():
    msp = [Entity('0','LINE') for _ in range(150)] + [Entity('0','TEXT') for _ in range(20)]
    info = unlayered.detect(msp)
    assert info['ratio'] == 1.0
    plan = unlayered.fallback_profile(info)
    assert plan['scope']['crop'] is False
    assert {'wall','column','door','room_name'} <= set(plan['layers']['0']['semantics'])
    assert 'window' not in plan['layers']['0']['semantics']


def test_a_normal_multilayer_drawing_does_not_enable_unlayered_route():
    msp = [Entity('0','LINE') for _ in range(200)] + [Entity('WALL','LINE') for _ in range(50)]
    assert unlayered.detect(msp) is None
    msp = [Entity('WALL','LINE') for _ in range(300)]
    assert unlayered.detect(msp) is None


def test_lexical_room_sources_union_without_duplicate():
    label={'handle':'A','x':2,'y':3,'text':'办公室','role':'room'}
    ctx=Ctx({'label_filter':{'labels':[label]}, 'room_labels': {'labels':[
        {'handle':'A','x':2,'y':3,'name':'办公室'},
        {'handle':'B','x':4,'y':5,'name':'配电所'}]}})
    picked,src=_room_label_source(ctx)
    assert len(picked)==2 and {x['name'] for x in picked}=={'办公室','配电所'}
    assert src=='label_filter+lexical'


def test_spaced_chinese_room_names_and_explanations():
    bbox=[0,0,1000,1000]
    samples=[('书    库','A'),('配  电  所','B'),('女生卫生间','C'),
             ('外墙变形缝详','D'),('首层平面图','E'),('花池','F')]
    texts=[{'text':s,'x':30+i*25,'y':25+i*20,'h':300,'handle':handle,'layer':'0'}
           for i,(s,handle) in enumerate(samples)]
    names,_=room_labels(texts,bbox,layers=['0'])
    assert {x['name'] for x in names}=={'书库','配电所','女生卫生间'}


def test_unfamiliar_blocks_require_both_name_and_geometry():
    snap={'entities':[
        {'type':'INSERT','name':'_DBLK1','xscale':1200,'yscale':1200},
        {'type':'INSERT','name':'_WBLK','xscale':2700,'yscale':180},
        {'type':'INSERT','name':'_WBLK2','xscale':2700,'yscale':180},
        {'type':'INSERT','name':'_DBLK2','xscale':1200,'yscale':1200}],
        'blocks':{
            '_DBLK1':{'arcs':[[0,0,0.5,0,90]],'lines':[[0,0,1,0]]},
            '_WBLK':{'arcs':[],'lines':[[0,-0.5,1,-0.5], [0,-0.2,1,-0.2], [0,0.2,1,0.2],[0,0.5,1,0.5]]},
            '_WBLK2':{'arcs':[],'lines':[[0,0,1,0]]*4},
            '_DBLK2':{'arcs':[],'lines':[[0,0,1,0]]}}}
    doors=_shape_blocks(snap, {'block_families':{}}, 'door')
    windows=_shape_blocks(snap, {'block_families':{}}, 'window')
    assert '_DBLK1' in doors and '_DBLK2' not in doors
    assert '_WBLK' in windows and '_WBLK2' not in windows


def test_window_block_uses_window_width_budget_not_narrow_door_width(monkeypatch):
    from dwg_reader.recognition import unified as UNI
    old = UNI.SC.thresholds({})
    thresholds = dict(old, gap_min=80.0, gap_max=1590.0,
                      door_w=400.0, thickness=180.0)
    monkeypatch.setattr(UNI.SC, 'thresholds', lambda _: thresholds)
    valid = {
        'block': '_WBLK', 'block_evidence': 'geometric:window_block_parallel_strokes',
        'axis': [[0, 0], [2700, 0]], 'layer':'WINDOW', 'handle': 'win1',
        'width':2700.0, 'thickness':180.0,
    }
    result = UNI.cluster(windows=[valid], thick=180.0, lines=[])
    doors, windows = UNI.split(result['openings'])
    assert len(windows) == 1 and not doors
    assert windows[0]['block_evidence'] == valid['block_evidence']
    # A block with no verified internal structure is NOT exempt from limits.
    dubious = dict(valid, block_evidence=None, handle='unknown')
    result = UNI.cluster(windows=[dubious], thick=180.0, lines=[])
    assert not result['openings']
    assert result['stats']['dropped']['too_wide'] == 1


def test_winlib2d_window_library_recognized_by_block_geometry():
    # In the club DXF this family appears 56 times; the internal 4 strokes
    # have two full and two 1/3-height edges, unlike four-equal strokes.
    lines = [[-0.5,0.5,-0.5,-0.5], [0.5,0.5,0.5,-0.5],
             [-0.16667,0.16667,-0.16667,-0.16667],
             [0.16667,0.16667,0.16667,-0.16667]]
    snap={'entities':[{'type':'INSERT','name':'$WinLib2D$00000001',
                       'xscale':1550,'yscale':200}],
          'blocks':{'$WinLib2D$00000001':{'lines':lines,'arcs':[]}}}
    result=_shape_blocks(snap,{'block_families':{}},'window')
    assert result['$WinLib2D$00000001']=='geometric:window_block_parallel_strokes'
    snap['blocks']['$WinLib2D$00000001']['lines']=[[0,0,1,0]]*4
    assert '$WinLib2D$00000001' not in _shape_blocks(snap,{'block_families':{}},'window')


def test_opening_source_provenance_keeps_arc_line_and_insert():
    from dwg_reader.recognition.nodes import _opening_source_entities
    assert _opening_source_entities({'handle':'A','layer':'0','free_end':[1,2]},'door')[0]['type']=='ARC'
    assert _opening_source_entities({'handle':'L','layer':'0'},'window')[0]['type']=='LINE'
    assert _opening_source_entities({'handle':'B','layer':'0','block':'_WBLK'},'window')[0]['type']=='INSERT'
    sources=_opening_source_entities({'handle':'L','layer':'0','srcs':[
        {'handle':'L','type':'LWPOLYLINE','layer':'0'}]},'window')
    assert len(sources)==1 and sources[0]['type']=='LWPOLYLINE'
