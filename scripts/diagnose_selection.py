import os,sys,collections,json
sys.path.insert(0,'src')
from dwg_reader.recognition.pipeline import run_recognition
os.environ['DWG_CONTROLLER_MAX_MS']='500'
r=run_recognition('/mnt/data/测试-二维DWG.dxf',jobs=4,targets=('openings_select',),collect_stats=False)
a=r.outputs
res={}
for k in ('doors','windows','openings_select','openings_unify','spaces_seed','room_labels_seed','texts'):
 v=a.get(k)
 if isinstance(v,list):
  res[k]={'n':len(v)}
  if k in ('doors','windows'):
   res[k]['types']=dict(collections.Counter((x.get('block') or 'arc') for x in v))
   res[k]['layers']=dict(collections.Counter(x.get('layer') for x in v))
 elif isinstance(v,dict):
  res[k]={x:len(y) for x,y in v.items() if isinstance(y,list)}
  if k=='openings_select':
   res[k]['door_types']=dict(collections.Counter(x.get('block') or 'arc' for x in v.get('doors') or []))
   res[k]['door_layers']=dict(collections.Counter(x.get('layer') or 'other' for x in v.get('doors') or []))
print(json.dumps(res,ensure_ascii=False,indent=2));open('reports/generalization_large_selection.json','w').write(json.dumps(res,ensure_ascii=False,indent=2))
