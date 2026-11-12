#!/usr/bin/env python3
"""Geometric equivariance audit, comparing a rotated drawing with the source.

This is a metamorphic test, NOT a ground-truth precision/recall study.  The
same recognizer may be wrong on both drawings. Requires optional shapely.
"""
import argparse, collections, json, math, os, time
from pathlib import Path
from dwg_reader.recognition.pipeline import run_recognition
from shapely.affinity import rotate
from shapely.geometry import Polygon, Point, LineString
from shapely.ops import unary_union


def geo(c):
    g=c.get('geometry') or {}
    if c['category'] in {'wall','room','column'}:
        arr=g.get('outline')
        return Polygon(arr).buffer(0) if arr and len(arr)>2 else None
    if c['category']=='window':
        arr=g.get('axis') or g.get('opening')
        if arr and len(arr)>1: return LineString(arr)
        p=g.get('center') or g.get('insert')
        return Point(p) if p else None
    if c['category']=='door':
        arr=g.get('opening') or g.get('axis')
        if arr and len(arr)>1: return LineString(arr)
        p=g.get('insert') or g.get('center')
        return Point(p) if p else None
    return None


def score(a,b,category,degrees):
    left=[geo(x) for x in a if x['category']==category]
    right=[rotate(geo(x),-degrees,origin=(0,0),use_radians=False) for x in b if x['category']==category and geo(x)]
    left=[x for x in left if x and not x.is_empty]
    right=[x for x in right if x and not x.is_empty]
    # 150mm tolerance for outline differences; proximity of different objects
    # is not identity. Count only one-to-one overlap matches.
    if category in ('column','room','wall'):
        reference=unary_union(left)
        observed=unary_union(right)
        common=reference.intersection(observed).area
        precision=common/observed.area if observed.area else 0
        recall=common/reference.area if reference.area else 0
    else:
        reference=unary_union([x.buffer(150) for x in left]); observed=unary_union([x.buffer(150) for x in right]);common=reference.intersection(observed).area
        precision=common/observed.area if observed.area else 0
        recall=common/reference.area if reference.area else 0
    return {'original_count':len(left),'rotated_count':len(right),
            'geometry_overlap_precision':round(precision,4),
            'geometry_overlap_recall':round(recall,4),
            'iou':round(common/(reference.area+observed.area-common),4) if reference.area+observed.area-common else 0}


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('original');ap.add_argument('rotated');ap.add_argument('--out',required=True);ap.add_argument('--degrees',type=float,default=30.0)
    args=ap.parse_args()
    os.environ['DWG_CONTROLLER_MAX_MS']='1200'
    t=time.monotonic()
    a=run_recognition(args.original,jobs=4,targets=('assemble',)).outputs['assemble']['components']
    b=run_recognition(args.rotated,jobs=4,targets=('assemble',)).outputs['assemble']['components']
    result={'original':str(Path(args.original).name),'rotated':str(Path(args.rotated).name),
            'runtime_sec':round(time.monotonic()-t,2),
            'note':'Geometric consistency relative to baseline predictions, not accuracy against GT. Rotated inverse by the --degrees argument about origin.',
            'comparison':{category:score(a,b,category,args.degrees) for category in ('wall','column','door','window','room')}}
    Path(args.out).parent.mkdir(parents=True,exist_ok=True)
    Path(args.out).write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps(result,indent=2,ensure_ascii=False))

if __name__=='__main__':main()
