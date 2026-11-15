"""Coverage-only continuation. This is never a cache of semantic decisions.

Fingerprints contain full canonical inputs, never truncated IR summaries. Only
hashes, family IDs and derived coverage counts leave this module in saved state.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter

SCHEMA = "family-observation-state/1.1"
_FIELDS = {"schema", "fingerprint", "attempted_ids", "successful_ids", "category_counts"}


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def fingerprint(*, snapshot, ir, families, profile, budget, ai, context=None,
                drawing_identity=None, gateway=None):
    """Bind coverage to full geometry and effective observation settings.

    Provider credentials/URLs and arbitrary AI config are deliberately excluded;
    only observation-affecting provider settings are used, never secret hashes.
    """
    # Only explicit nonsecret settings; never environment values or live counters.
    effective_gateway = (None if gateway is None else
                         {key: getattr(gateway, key, None) for key in
                          ("model", "vision_model", "max_calls", "max_ms",
                           "retries", "timeout")})
    members = []
    for family in families:
        row = dict(family)
        if "instances" in row:
            row["instances"] = sorted(row["instances"], key=_canonical)
        if "uids" in row:
            row["uids"] = sorted(row["uids"])
        members.append(row)
    provider = (ai or {}).get("provider") or {}
    payload = {"schema": SCHEMA, "snapshot": snapshot, "ir": ir,
               "families": sorted(members, key=_canonical),
               "profile": profile, "budget": budget, "context": context,
               "drawing_identity": drawing_identity,
               "gateway": effective_gateway,
               "ai": {"mode": (ai or {}).get("mode", "off"),
                       "budget": (ai or {}).get("budget") or {},
                      "provider": {k: provider.get(k) for k in
                                   ("vision", "allow_remote_images", "model",
                                     "model_env", "vision_model_env")}}}
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def category_counts(ids, families):
    categories = {str(f["id"]): f.get("category_guess") or "unknown"
                  for f in families}
    return dict(sorted(Counter(categories[i] for i in ids).items()))


def make(fingerprint, attempted, successful, families):
    attempted, successful = sorted(set(attempted)), sorted(set(successful))
    return {"schema": SCHEMA, "fingerprint": fingerprint,
            "attempted_ids": attempted, "successful_ids": successful,
            "category_counts": category_counts(attempted, families)}


def validate(state, fingerprint, families):
    """Reject mismatches/injected semantics before any gateway request."""
    if state is None:
        return make(fingerprint, (), (), families)
    if not isinstance(state, dict) or set(state) != _FIELDS:
        raise ValueError("Invalid observation_state fields (coverage only)")
    if state["schema"] != SCHEMA or state["fingerprint"] != fingerprint:
        raise ValueError("observation_state fingerprint/schema mismatch")
    known = {str(f["id"]) for f in families}
    for key in ("attempted_ids", "successful_ids"):
        ids = state[key]
        if (not isinstance(ids, list) or any(not isinstance(i, str) for i in ids)
                or len(ids) != len(set(ids)) or not set(ids) <= known):
            raise ValueError("Invalid observation_state " + key)
    if not set(state["successful_ids"]) <= set(state["attempted_ids"]):
        raise ValueError("Successful observations must have been attempted")
    clean = make(fingerprint, state["attempted_ids"], state["successful_ids"], families)
    if state["category_counts"] != clean["category_counts"]:
        raise ValueError("Invalid observation_state category_counts")
    return clean


def coverage(state, previous, families, eligible_ids):
    """Unique families attempted/succeeded, not request or component counts."""
    attempted, successful = set(state["attempted_ids"]), set(state["successful_ids"])
    def row(ids):
        return {"ids": sorted(ids), "count": len(ids),
                "by_category": category_counts(ids, families)}
    return {"round": {"attempted": row(attempted - set(previous["attempted_ids"])),
                      "successful": row(successful - set(previous["successful_ids"]))},
            "total": {"attempted": row(attempted), "successful": row(successful)},
            "unseen": row(set(eligible_ids) - attempted)}
