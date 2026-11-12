"""Stable, rotation-independent normalization and grouping of CAD wall lines.

Do not use tuple(dir) or a rounded *world* offset as line identity.  A
normalized local basis is chosen for nearby parallel segments; original
intervals remain separate so openings are not silently filled.
"""
from __future__ import annotations

import math
from . import geometry as GEO


def _angle_dist(a, b):
    d = abs(float(a) - float(b)) % 180.0
    return min(d, 180.0 - d)


def grouped(walls, *, angle_tol=0.3, pos_tol=2.5):
    """Return groups of geometrically co-linear wall records in common basis.

    Each original record is copied with normalized dir/nrm/center/pos/start/end.
    Crucially: never union their occupied [lo,hi] intervals here.  Large
    drawings and rotated images can have substantial world-coordinate offsets,
    hence compare actual midpoints against the representative support instead
    of comparing offsets calculated in slightly different bases.
    """
    supports = []
    for w in walls:
        if not w.get('start') or not w.get('end'):
            continue
        s = GEO.as_support(w['start'], w['end'])
        if s:
            supports.append((w, s))
    supports.sort(key=lambda z: (-z[1]['length'], z[1]['angle'], z[1]['pos']))
    families = []
    for w, sup in supports:
        chosen = None
        for family in families:
            if _angle_dist(sup['angle'], family['angle']) <= angle_tol:
                chosen = family
                break
        if chosen is None:
            chosen = {'angle': sup['angle'], 'ref': sup, 'items': []}
            families.append(chosen)
        chosen['items'].append((w, sup))
    result = []
    for family in families:
        ref = family['ref']
        ux, uy = ref['dir']; nx, ny = ref['nrm']
        parallel = []
        for w, s in family['items']:
            start, end = w['start'], w['end']
            p = (float(start[0]) + float(end[0])) / 2.0
            q = (float(start[1]) + float(end[1])) / 2.0
            pos = nx * p + ny * q
            a = ux * start[0] + uy * start[1]
            b = ux * end[0] + uy * end[1]
            parallel.append((pos, min(a,b), max(a,b), w))
        parallel.sort(key=lambda t: t[0])
        cluster = []
        def flush(items):
            if not items:
                return
            # Midpoint keeps the group representative independent of the
            # original line direction/order while avoiding angle-bucket drift.
            center = sum(x[0] for x in items) / len(items)
            rows = []
            for _, lo, hi, original in items:
                obj = dict(original)
                obj['angle'] = ref['angle']
                obj['dir'] = [ux, uy]
                obj['nrm'] = [nx, ny]
                obj['center'] = center
                obj['pos'] = center
                obj['lo'], obj['hi'] = lo, hi
                obj['start'] = [ux*lo+nx*center, uy*lo+ny*center]
                obj['end'] = [ux*hi+nx*center, uy*hi+ny*center]
                obj['orient'] = ('H' if ref['orient'] == 'H' else
                                 'V' if ref['orient'] == 'V' else
                                 f"O{ref['angle']:.0f}")
                rows.append(obj)
            result.append(rows)
        for rec in parallel:
            if cluster and rec[0] - cluster[-1][0] > pos_tol:
                flush(cluster); cluster = []
            cluster.append(rec)
        flush(cluster)
    # Determinism: stable groups and within-group order.
    for g in result:
        g.sort(key=lambda w: (w['lo'], w['hi']))
    result.sort(key=lambda g: (g[0]['angle'], g[0]['center'], g[0]['lo']))
    return result
