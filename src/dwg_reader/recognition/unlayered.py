"""Detection of genuinely unlayered CAD documents.

Layer 0 normally means a construction/insertion layer, but a drawing where
nearly everything lives on it is different: excluding it removes all evidence.
Detection never changes explicit user profiles and never labels an ordinary
multilayer drawing's layer 0 as a wall layer.
"""
from __future__ import annotations

from collections import Counter

GEOMETRIC = frozenset(("LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "INSERT", "HATCH"))


def detect(msp, *, min_entities=100, dominant_ratio=0.96):
    counts = Counter()
    geom = Counter()
    for e in msp:
        kind = e.dxftype()
        layer = str(e.dxf.get('layer', '0'))
        counts[layer] += 1
        if kind in GEOMETRIC:
            geom[layer] += 1
    total = sum(counts.values())
    if total < min_entities:
        return None
    dominant, size = counts.most_common(1)[0]
    if size / total < dominant_ratio or geom[dominant] < 80:
        return None
    # Only a structural/default or non-semantic layer needs the exception.
    # A named WALL layer covering a whole plan already has meaningful routing.
    from .taxonomy import match_layer
    known = match_layer(dominant)
    if known and any(r in ("wall", "column", "door", "window", "room_name") for r in known):
        return None
    return {'layer': dominant, 'entities': total, 'on_layer': size,
            'ratio': round(size / total, 4), 'geometric': geom[dominant]}


def fallback_profile(info):
    layer = info['layer']
    # Do not use the entire line layer as a window layer. Parallel annotations
    # and furniture would otherwise generate huge numbers of false windows.
    return {
        'source': 'unlayered-geometry',
        'layers': {layer: {'semantics': ['wall', 'column', 'door', 'room_name'],
                           'confidence': 0.5, 'reason': 'dominant unlayered CAD geometry'}},
        'scope': {'crop': False},
        'unlayered': info,
    }
