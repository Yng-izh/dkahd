"""Regressions for line-support invariance, gaps and RLE room contours."""
import math
import pytest
from dwg_reader.recognition import geometry as GEO, linegroups as LG, walls as W, rooms as RM
from dwg_reader.recognition.base import polygon_area


def _rot(p, degrees):
    t=math.radians(degrees)
    return [p[0]*math.cos(t)-p[1]*math.sin(t), p[0]*math.sin(t)+p[1]*math.cos(t)]


def test_oblique_support_reversed_has_identical_basis():
    a,b=_rot((125,-50),30),_rot((4582,-50),30)
    x,y=GEO.as_support(a,b),GEO.as_support(b,a)
    for key in ('angle','pos','lo','hi','length'):
        assert x[key]==pytest.approx(y[key],abs=1e-8)
    assert x['dir']==pytest.approx(y['dir'])
    assert x['nrm']==pytest.approx(y['nrm'])


def test_collinear_group_stable_to_coordinate_noise_and_keeps_gap():
    seg=[]
    for a,b in [((0,0),(1000,0)),((1900,0.001),(3000,0.001))]:
        seg.append({'start':_rot(a,30),'end':_rot(b,30)})
    groups=LG.grouped(seg)
    assert len(groups)==1
    assert len(groups[0])==2
    assert groups[0][1]['lo']-groups[0][0]['hi']==pytest.approx(900,abs=.1)


def test_oblique_pair_does_not_fill_door_gap():
    raw=[]
    for y in (0,200):
        for a,b in ((0,1000),(1900,3000)):
            s,e=_rot((a,y),30),_rot((b,y),30)
            raw.append((*s,*e,str(len(raw)),'LINE',math.dist(s,e)))
    got=W._oblique_walls(raw,thick_hint=200)
    assert len(got)==2,got
    assert all(w['length']==pytest.approx(l,abs=.1) for w,l in zip(sorted(got,key=lambda x:x['length']),[1000,1100]))


def test_oblique_third_parallel_face_blocks_phantom_pair():
    raw=[]
    for y in (0,100,300):
        s,e=_rot((0,y),30),_rot((3000,y),30)
        raw.append((*s,*e,str(len(raw)),'LINE',math.dist(s,e)))
    got=W._oblique_walls(raw)
    assert not any(abs(w['thickness']-300)<1 for w in got)
    assert any(abs(w['thickness']-100)<1 for w in got)


@pytest.mark.parametrize('cells',[{(i,j) for i in range(3,9) for j in range(2,6)},
     {(i,j) for i in range(2,10) for j in range(2,7) if i<4 or j<4},
     {(i,j) for i in range(2,11) for j in range(2,11) if (i,j) not in {(4,4),(5,4),(4,5),(5,5)}},
    {(i,j) for i in range(3,12) for j in range(3,12) if i<5 or i>9 or j<5 or j>9}])
def test_run_length_contour_preserves_exterior_area(cells):
    g=RM.Grid([0,0,200,200],res=10)
    # Treat exactly the chosen cells as free; all others as barriers.
    g.mask[:]=bytes([1])*len(g.mask)
    for i,j in cells:
        g.mask[g.idx(i,j)]=0
    cc=RM._Components(g)
    roots=[r for r in cc.by_root if r!=cc.outside_root]
    assert roots
    for r in roots:
        old_area=abs(polygon_area(RM.contour(cc.component_cells(r),g)))
        new=RM.component_contour(cc,r,g)
        assert new
        assert abs(polygon_area(new))==pytest.approx(old_area,abs=1e-5)
        assert len(new)<=len(RM.contour(cc.component_cells(r),g))


def test_rotated_column_polyline_preserves_actual_section():
    from dwg_reader.recognition import elements as E
    pts=[_rot(p,30) for p in [(0,0),(650,0),(650,650),(0,650)]]
    snapshot={'entities':[{'type':'LWPOLYLINE','layer':'COLUMN',
                          'closed':True,'points':pts,'handle':'x'}], 'derived':[]}
    found=E._rects_from_lwpolyline(snapshot, {'COLUMN'})
    assert len(found)==1
    assert found[0]['w']==pytest.approx(650)
    assert found[0]['h']==pytest.approx(650)
    assert abs(polygon_area(found[0]['outline']))==pytest.approx(650*650)


def test_rotated_elon_pier_not_mistaken_for_square_column():
    from dwg_reader.recognition import elements as E
    for angle in (0,30,90):
        pts=[_rot(p,angle) for p in [(0,0),(350,0),(350,200),(0,200)]]
        snap={'entities':[{'type':'LWPOLYLINE','layer':'COLUMN',
                          'closed':True,'points':pts,'handle':'pier'}], 'derived':[]}
        assert E._rects_from_lwpolyline(snap, {'COLUMN'})==[]


def test_column_fusion_does_not_replace_exact_rotated_block_outline():
    from dwg_reader.recognition import elements as E
    block={'outline':[_rot(p,30) for p in [(0,0),(650,0),(650,650),(0,650)]],
           'bbox':[-325,0,562.9,887.9],'w':650,'h':650,
           'method':'block','center':[120,444], 'handle':'B1'}
    poly={'outline':[[-325,0],[562.9,0],[562.9,887.9],[-325,887.9]],
          'bbox':[-325,0,562.9,887.9],'w':887.9,'h':887.9,
          'method':'polyline','center':[120,444],'handle':'P1'}
    cols=E.fuse_columns([block,poly])['columns']
    assert len(cols)==1
    assert cols[0]['w']==pytest.approx(650)
    assert abs(polygon_area(cols[0]['outline']))==pytest.approx(650*650)
