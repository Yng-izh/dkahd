import sys,os,json,collections
sys.path.insert(0,'src')
os.environ['DWG_CONTROLLER_MAX_MS']='500'
from dwg_reader.recognition.pipeline import run_recognition
r=run_recognition('/mnt/data/测试-二维DWG.dxf',jobs=4,targets=('rooms',),collect_stats=False)
o=r.outputs
res={}
for node in ('texts','room_labels_seed','label_filter','room_labels','rooms','spaces_seed','space_routing'):
 v=o.get(node)
 if isinstance(v,list):res[node]={'count':len(v),'sample':str(v[0])[:450] if v else None}
 elif isinstance(v,dict):res[node]={k:len(x) if isinstance(x,(list,tuple)) else str(x)[:90] for k,x in v.items()}
 else:res[node]=str(v)[:200]
print(json.dumps(res,ensure_ascii=False,indent=2)[:5000])
open('reports/generalization_large_rooms.json','w').write(json.dumps(res,ensure_ascii=False,indent=2))
