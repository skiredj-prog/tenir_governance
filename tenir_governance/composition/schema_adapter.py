# Profile: agentic-governance-profile-v1.0.0
# Compliance class: profile-bound
# This module is an extension on top of KB3 and TENIR 2.0 schema 1.2.
# It is NOT canonical, NOT doctrinal. It carries implementation-level
# governance enforcement authority only.
"""Schema conformance adapter — maps governor outputs to TENIR 2.0 schema 1.2.

Per the governing principle: "TENIR 2.0 schema (1.2) is the canonical contract.
Every module's output must conform to it. The prototype conforms to the schema;
it does not extend it."

This adapter takes the governor's implementation-level output dict and produces
a dict that conforms to the TENIR 2.0 schema 1.2 field names. The schema already
contains everything needed — this adapter maps, it does not extend.

Schema conformance mapping (prototype → canonical):

  composed_decision → posture (NORMAL/CAUTION/HOLDING-FIRST)
                      + synthesis.final_posture
  operation_state   → trace.stages[]
  scoring_result    → scores + synthesis.structural_core
  membrane_result   → synthesis.agency_state + synthesis.trajectory_state
  fail-closed HOLD  → degraded_mode
  guarantee tiers   → compliance_class
  UNKNOWN           → degraded_mode + synthesis.principal_failure_mode
  idempotency_key   → trace.trace_id

KB3 posture mapping:
  PASS      → NORMAL
  FLAG      → CAUTION
  HOLD      → HOLDING-FIRST
  HARD_VETO → HOLDING-FIRST (with block disposition)

Per KB3: posture and execution mode are separate fields, never collapsed.
"""
from __future__ import annotations

from typing import Any, Mapping

from .decisions import Decision
from .._profile_meta import DISCLOSURE


# ── Posture Mapping ─────────────────────────────────────────────────────

POSTURE_MAP = {
    Decision.PASS.value: "NORMAL",
    Decision.FLAG.value: "CAUTION",
    Decision.HOLD.value: "HOLDING-FIRST",
    Decision.HARD_VETO.value: "HOLDING-FIRST",
}

EXECUTION_DISPOSITION_MAP = {
    Decision.PASS.value: "allow",
    Decision.FLAG.value: "allow_with_alert",
    Decision.HOLD.value: "hold",
    Decision.HARD_VETO.value: "block",
}

# Per KB3: execution mode is separate from posture.
# The governor runs in ENFORCE mode (not shadow).
EXECUTION_MODE = "enforce"

# Compliance class for this profile
COMPLIANCE_CLASS = "profile-bound"

# Profile identifier
PROFILE_VERSION = "agentic-governance-profile-v1.0.0"


def conform_to_schema(governor_result: Mapping[str, Any]) -> dict[str, Any]:
    """Build and validate a canonical TENIR 2.0 schema 1.2 runtime object.

    The governor result alone is not a complete canonical runtime object.
    Callers must supply an explicit canonical_context mapping containing the
    canonical input/evidence fields. Missing context is rejected; this adapter
    never fabricates identity, epistemic evidence, or human verification.
    """
    from tenir_kernel.schema import validate_runtime_object

    if not isinstance(governor_result, Mapping):
        raise ValueError("governor_result must be a mapping")
    context = governor_result.get("canonical_context")
    if not isinstance(context, Mapping):
        raise ValueError(
            "canonical_context is required: governor decisions do not contain "
            "all evidence and identity fields required by TENIR schema 1.2"
        )

    result: dict[str, Any] = dict(context)
    for key in ("scores", "identity", "tau", "epistemic", "trace", "synthesis",
                "constraint_geometry", "irreversible_pathway_assessment"):
        if isinstance(result.get(key), Mapping):
            result[key] = dict(result[key])

    composed = governor_result.get("composed_decision", Decision.HOLD.value)
    posture = POSTURE_MAP.get(composed, "HOLDING-FIRST")
    is_degraded = composed in (Decision.HOLD.value, Decision.HARD_VETO.value)
    result.update({
        "schema_version": "1.2",
        "engine_spec_version": "TENIR-2.0",
        "posture": posture,
        "compliance_class": COMPLIANCE_CLASS,
        "degraded_mode": _degraded_modes(context, is_degraded),
        "active_profiles": list(context.get("active_profiles", [PROFILE_VERSION])),
        "trace": {
            **dict(context.get("trace", {})),
            "trace_id": str(governor_result.get("idempotency_key")
                            or context.get("trace", {}).get("trace_id", "")),
            "stages": _build_trace_stages(governor_result),
            "ledger_entries": governor_result.get("ledger_entries", 0),
            "ledger_valid": governor_result.get("ledger_valid", False),
            "passage_id": governor_result.get("passage_id", ""),
        },
        "synthesis": {
            **dict(context.get("synthesis", {})),
            "final_posture": posture,
            "structural_core": dict(context.get("synthesis", {}).get("structural_core", {})),
            "epistemic_state": dict(context.get("synthesis", {}).get("epistemic_state", {})),
            "rationale": _rationale_items(governor_result),
            "degraded_inputs": _degraded_modes(context, is_degraded),
            "agency_state": {
                "membrane_decision": governor_result.get("membrane_decision"),
                "membrane_rationale": governor_result.get("membrane_rationale"),
                "membrane_checks": governor_result.get("membrane_checks", []),
            },
            "trajectory_state": {"operation_state": governor_result.get("operation_state")},
            "principal_failure_mode": _principal_failure_mode(governor_result),
        },
        "disclosure": {
            "doctrinal_version": "TENIR 2.0",
            "platform_version": DISCLOSURE.platform_version,
            "contract_version": "1.2",
            "compliance_class": COMPLIANCE_CLASS,
            "active_profiles": list(context.get("active_profiles", [PROFILE_VERSION])),
            "semantic_authority_held": [],
            "semantic_authority_not_held": [
                "canonical-doctrine", "schema-definition", "posture-determination",
            ],
        },
    })

    assessment = governor_result.get("irreversible_pathway_assessment")
    if isinstance(assessment, Mapping):
        result["irreversible_pathway_assessment"] = dict(assessment)
    geometry = governor_result.get("constraint_geometry")
    if isinstance(geometry, Mapping):
        result["constraint_geometry"] = dict(geometry)

    return validate_runtime_object(result)


def _degraded_modes(context: Mapping[str, Any], is_degraded: bool) -> list[str]:
    """Return schema-defined degradation labels without inventing causes."""
    supplied = context.get("degraded_mode")
    modes = list(supplied) if isinstance(supplied, (list, tuple)) else []
    if is_degraded and not modes:
        modes = ["sparse_ingress"]
    if not is_degraded and not modes:
        modes = ["none"]
    allowed = {"none", "missing_agency", "missing_trajectory",
               "missing_epistemic", "sparse_ingress", "legacy_shape"}
    if any(mode not in allowed for mode in modes):
        raise ValueError("canonical_context.degraded_mode contains an unknown schema 1.2 label")
    if "none" in modes and len(modes) > 1:
        raise ValueError("degraded_mode cannot combine 'none' with degradation labels")
    return modes


def _rationale_items(governor_result: Mapping[str, Any]) -> list[str]:
    """Normalize decision rationale to the canonical string-array contract."""
    items = []
    for key in ("composed_rationale", "scoring_rationale", "membrane_rationale"):
        value = governor_result.get(key)
        if isinstance(value, str) and value.strip() and value not in items:
            items.append(value)
    return items or ["Decision recorded; no rationale supplied by the governor."]



def _build_trace_stages(governor_result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Build schema-valid trace stages without losing decision details."""
    stages: list[dict[str, Any]] = []
    for label, decision_key, rationale_key in (
        ("scoring", "scoring_decision", "scoring_rationale"),
        ("membrane", "membrane_decision", "membrane_rationale"),
        ("composition", "composed_decision", "composed_rationale"),
    ):
        decision = governor_result.get(decision_key)
        rationale = governor_result.get(rationale_key)
        if decision is None and label != "composition":
            continue
        stage = {
            "stage": label,
            "status": _trace_status(decision),
            "outputs_emitted": [str(decision)] if decision is not None else [],
        }
        if isinstance(rationale, str) and rationale:
            stage["summary"] = rationale
        if decision is not None:
            stage["decision"] = decision
        stages.append(stage)

    operation_state = governor_result.get("operation_state")
    if operation_state:
        stages.append({
            "stage": "operation",
            "status": "warning" if str(operation_state).upper() in {"UNKNOWN", "HOLD"} else "ok",
            "summary": str(operation_state),
            "outputs_emitted": [str(operation_state)],
        })
    return stages


def _trace_status(decision: Any) -> str:
    if decision == Decision.PASS.value:
        return "ok"
    if decision in (Decision.FLAG.value, Decision.HOLD.value):
        return "warning"
    return "failed"



def _principal_failure_mode(governor_result: Mapping[str, Any]) -> str | None:
    """Determine the principal failure mode for synthesis.

    Per schema: principal_failure_mode describes why the system is in
    a degraded state. Returns None if not degraded.
    """
    composed = governor_result.get("composed_decision", "")

    if composed == Decision.HARD_VETO.value:
        if governor_result.get("scoring_decision") == Decision.HARD_VETO.value:
            return "scoring-conclusive-risk"
        if governor_result.get("membrane_decision") == Decision.HARD_VETO.value:
            return "membrane-invariant-violation"
        return "composed-hard-veto"

    if composed == Decision.HOLD.value:
        if governor_result.get("scoring_decision") is None:
            return "scoring-kernel-unavailable"
        if governor_result.get("membrane_decision") is None:
            return "membrane-kernel-unavailable"
        if governor_result.get("membrane_decision") == Decision.HOLD.value:
            return "membrane-pending-reconciliation"
        return "budget-escalation"

    return None
