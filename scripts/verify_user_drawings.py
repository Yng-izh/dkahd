#!/usr/bin/env python3
"""Reproducible real DXF smoke/end-to-end report; quantities are not precision."""
import argparse, collections, json, os, time, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from dwg_reader.recognition.pipeline import run_recognition

p=argparse.ArgumentParser(); p.add_argument('drawing');p.add_argument('--out',required=True);p.add_argument('--review-out')
p.add_argument('--target',default='assemble');p.add_argument('--budget-ms',type=int,default=3000)
a=p.parse_args()
os.environ['DWG_CONTROLLER_MAX_MS']=str(a.budget_ms)
t0=time.monotonic()
try:
    run=run_recognition(a.drawing,jobs=4,targets=(a.target,),collect_stats=True)
    output=run.outputs
    components=output.get('assemble',{}).get('components',[])
    c=collections.Counter(x['category'] for x in components)
    statuses={kind: dict(collections.Counter(x.get('status','unknown')
                         for x in components if x.get('category')==kind))
              for kind in sorted(c)}
    rooms=[x for x in components if x.get('category')=='room']
    room_states=dict(collections.Counter(x.get('attributes',{}).get('boundary_state','unknown')
                                        for x in rooms))
    reviews=dict(collections.Counter(x.get('category') for x in components if x.get('needs_review')))
    if a.review_out:
        review_records = [{
            'id': x.get('id'), 'category': x.get('category'),
            'name': x.get('name'), 'status': x.get('status'),
            'confidence': x.get('confidence'),
            'needs_review':bool(x.get('needs_review')),
            'geometry': x.get('geometry'),
            'boundary_state':x.get('attributes',{}).get('boundary_state'),
            'area_warning':x.get('attributes',{}).get('area_warning'),
            'source_entities':x.get('source_entities'),
            'evidence':x.get('evidence'),
        } for x in components if x.get('needs_review') or x.get('status')!='confirmed']
        Path(a.review_out).parent.mkdir(parents=True,exist_ok=True)
        Path(a.review_out).write_text(json.dumps(review_records,ensure_ascii=False,indent=2),encoding='utf-8')
    result={'drawing':Path(a.drawing).name,'target':a.target,'status':'ok',
            'seconds':round(time.monotonic()-t0,3),'counts':dict(c),
            'status_by_category':statuses,
            'needs_review_by_category':reviews,
            'room_area_warning_count':sum(bool(r.get('attributes',{}).get('area_warning')) for r in rooms),
            'unlayered':(output.get('load') or {}).get('plan',{}).get('unlayered'),
            'wall_segments':len((output.get('walls_seg') or {}).get('walls_seg') or []),
            'column_candidates':len(output.get('columns') or []),
            'door_candidates':len(output.get('doors') or []),
            'window_candidates':len(output.get('windows') or []),
            'region_count':len((output.get('load') or {}).get('regions') or []),
            'soft_layers':(output.get('load') or {}).get('plan',{}).get('soft_layers',{}),
            'room_boundary_states': room_states,
            'note':'Candidate and instance counts are not precision/recall without instance-level GT.'}
except Exception as e:
    result={'drawing':Path(a.drawing).name,'target':a.target,'status':'error',
            'seconds':round(time.monotonic()-t0,3),'exception':repr(e)}
Path(a.out).parent.mkdir(parents=True,exist_ok=True)
Path(a.out).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False,indent=2))
if result['status']!='ok':sys.exit(1)
