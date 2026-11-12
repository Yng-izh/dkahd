"""校验 LLM 提议：闭集 action + 已存在 target + 类型/区域/轮次预算 + 越权拦截。

任何几何字段（坐标、尺寸、容差、阈值）、任何未知字段、未注册算子、``solver_only``
算子一律拒绝，并记录可审计的拒绝原因。
"""
from __future__ import annotations

from .operator_specs import REGISTRY

ALLOWED_TOP_FIELDS = {"case_id", "proposals"}
ALLOWED_PROPOSAL_FIELDS = {"action", "target_ids", "hypothesis", "evidence_ids",
                           "reason", "alternative"}
#: 只有语义修复动作允许携带 roles（闭集角色，由 layer_roles 校验）
CONDITIONAL_FIELDS = {"propose_layer_role": {"roles"}}
FORBIDDEN_KEYS = ("x", "y", "z", "width", "height", "coord", "tolerance", "threshold",
                  "radius", "angle", "code", "script", "python", "dxf")
FORBIDDEN_TEXT = ("def ", "import ", "dxf.", "write", "execute(")


def validate(proposal: dict, *, index: dict, region=None, round_no: int = 1,
             max_rounds: int = 2) -> tuple[bool, str]:
    if not isinstance(proposal, dict):
        return False, "NOT_AN_OBJECT"
    if round_no > max_rounds:
        return False, "ROUND_BUDGET_EXCEEDED"

    action = str(proposal.get("action") or "")
    allow = set(ALLOWED_PROPOSAL_FIELDS) | CONDITIONAL_FIELDS.get(action, set())
    extra = set(proposal) - allow
    if extra:
        return False, "EXTRA_FIELDS:" + ",".join(sorted(extra))
    spec = REGISTRY.get(action)
    if spec is None:
        return False, f"UNKNOWN_OPERATOR:{action}"
    if spec.solver_only or not spec.executable_by_llm:
        return False, f"OPERATOR_NOT_DELEGABLE:{action}"

    targets = proposal.get("target_ids") or []
    if not isinstance(targets, list) or not targets:
        return False, "NO_TARGETS"
    if len(targets) > spec.max_targets:
        return False, f"TOO_MANY_TARGETS:{len(targets)}>{spec.max_targets}"

    items = index.get("items") or {}
    for t in targets:
        rec = items.get(str(t))
        if rec is None:
            return False, f"TARGET_NOT_FOUND:{t}"
        if rec.get("kind") not in spec.input_kinds:
            return False, f"TARGET_KIND_MISMATCH:{t}:{rec.get('kind')}"
        if region and rec.get("bbox") and not _inside(rec["bbox"], region):
            return False, f"TARGET_OUT_OF_REGION:{t}"

    for eid in proposal.get("evidence_ids") or ():
        if str(eid) not in items:
            return False, f"EVIDENCE_NOT_FOUND:{eid}"

    if action == "propose_layer_role":
        from . import layer_roles as ROLES
        layers = [str(t).split(":", 1)[1] for t in targets if str(t).startswith("layer:")]
        if not layers:
            return False, "NO_LAYER_TARGET"
        roles = proposal.get("roles")
        if not isinstance(roles, list) or not roles:
            return False, "NO_ROLES"
        if len(roles) != len(layers):
            return False, f"ROLES_TARGET_MISMATCH:{len(roles)}!={len(layers)}"
        for layer, one in zip(layers, roles):
            ok, why = ROLES.validate(layer, one)
            if not ok:
                return False, f"{why}:{layer}"
    if _looks_geometric(proposal):
        return False, "GEOMETRY_IN_PROPOSAL"
    return True, "OK"


def validate_case(payload: dict, *, index: dict, region=None, round_no: int = 1,
                  max_rounds: int = 2) -> dict:
    if not isinstance(payload, dict):
        return {"ok": False, "reason": "NOT_AN_OBJECT", "accepted": [], "rejected": []}
    extra = set(payload) - ALLOWED_TOP_FIELDS
    if extra:
        return {"ok": False, "reason": "EXTRA_TOP_FIELDS:" + ",".join(sorted(extra)),
                "accepted": [], "rejected": []}
    accepted, rejected = [], []
    for p in payload.get("proposals") or ():
        ok, why = validate(p, index=index, region=region,
                           round_no=round_no, max_rounds=max_rounds)
        src = p if isinstance(p, dict) else {}
        rec = {"action": src.get("action"), "target_ids": src.get("target_ids"),
               "hypothesis": src.get("hypothesis"),
               "case_id": payload.get("case_id"), "reason": why}
        if src.get("roles") is not None:
            rec["roles"] = src.get("roles")
        if src.get("alternative"):
            rec["alternative"] = src.get("alternative")
        (accepted if ok else rejected).append(rec)
    return {"ok": bool(accepted), "accepted": accepted, "rejected": rejected}


def _inside(bbox, region) -> bool:
    x0, y0, x1, y1 = region
    pad = 300.0
    return not (bbox[2] < x0 - pad or bbox[0] > x1 + pad
                or bbox[3] < y0 - pad or bbox[1] > y1 + pad)


def _looks_geometric(proposal) -> bool:
    for k, v in proposal.items():
        if k in ("target_ids", "evidence_ids", "roles"):
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return True
        text = str(v).lower()
        if any(h in text for h in FORBIDDEN_TEXT):
            return True
        if any(text.strip() == h for h in FORBIDDEN_KEYS):
            return True
    return False
