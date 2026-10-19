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
    """Map a governor adjudicate() result to TENIR 2.0 schema 1.2 fields.

    This produces a dict with canonical schema field names. The schema
    already defines these fields — this adapter populates them from the
    governor's implementation-level output.

    Per KB3: posture and execution mode are separate output fields.
    Per versioning policy §6: disclosure metadata is included.
    """
    composed = governor_result.get("composed_decision", Decision.HOLD.value)
    posture = POSTURE_MAP.get(composed, "HOLDING-FIRST")
    disposition = EXECUTION_DISPOSITION_MAP.get(composed, "hold")
    is_degraded = composed in (Decision.HOLD.value, Decision.HARD_VETO.value)

    # Build the schema-conformant output
    result: dict[str, Any] = {
        # ── Canonical schema fields ──
        "schema_version": "1.2",
        "engine_spec_version": "TENIR-2.0",

        # Posture (separate from execution mode per KB3)
        "posture": posture,

        # Execution mode (separate from posture per KB3)
        "execution_mode": EXECUTION_MODE,
        "execution_disposition": disposition,
        "execution_allowed": composed in (Decision.PASS.value, Decision.FLAG.value),

        # Compliance class (visible per KB3)
        "compliance_class": COMPLIANCE_CLASS,

        # Degraded mode (visible per KB3)
        "degraded_mode": is_degraded,

        # Scores
        "scores": {
            "s_score": governor_result.get("scoring_s"),
        },

        # Synthesis (composite)
        "synthesis": {
            "final_posture": posture,
            "structural_core": {
                "scoring_decision": governor_result.get("scoring_decision"),
                "scoring_rationale": governor_result.get("scoring_rationale"),
            },
            "agency_state": {
                "membrane_decision": governor_result.get("membrane_decision"),
                "membrane_rationale": governor_result.get("membrane_rationale"),
                "membrane_checks": governor_result.get("membrane_checks", []),
            },
            "trajectory_state": {
                "operation_state": governor_result.get("operation_state"),
            },
            "principal_failure_mode": _principal_failure_mode(governor_result),
        },

        # Trace
        "trace": {
            "trace_id": governor_result.get("idempotency_key", ""),
            "passage_id": governor_result.get("passage_id", ""),
            "stages": _build_trace_stages(governor_result),
            "ledger_entries": governor_result.get("ledger_entries", 0),
            "ledger_valid": governor_result.get("ledger_valid", False),
        },

        # ── Disclosure metadata (per versioning policy §6) ──
        "disclosure": {
            "doctrinal_version": "TENIR 2.0",
            "platform_version": "5.3.0",
            "contract_version": "1.2",
            "compliance_class": COMPLIANCE_CLASS,
            "active_profiles": [PROFILE_VERSION],
            "semantic_authority_held": [],
            "semantic_authority_not_held": [
                "canonical-doctrine",
                "schema-definition",
                "posture-determination",
            ],
        },
    }

    # Never infer human verification or pathway materiality from the
    # existence of a scalar score. The governor may attach an assessment only
    # when an upstream component actually performed and recorded it.
    pathway_assessment = governor_result.get("irreversible_pathway_assessment")
    if isinstance(pathway_assessment, Mapping):
        result["irreversible_pathway_assessment"] = dict(pathway_assessment)

    # Constraint geometry (if available from scoring)
    # The scoring adapter passes this through from TenirKernel
    if "constraint_geometry" in governor_result:
        result["constraint_geometry"] = governor_result["constraint_geometry"]

    return result


def _build_trace_stages(governor_result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Build trace.stages[] from the governor's operation state and decisions."""
    stages = []

    # Scoring stage
    if governor_result.get("scoring_decision"):
        stages.append({
            "stage": "scoring",
            "decision": governor_result["scoring_decision"],
            "rationale": governor_result.get("scoring_rationale", ""),
        })

    # Membrane stage
    if governor_result.get("membrane_decision"):
        stages.append({
            "stage": "membrane",
            "decision": governor_result["membrane_decision"],
            "rationale": governor_result.get("membrane_rationale", ""),
        })

    # Composition stage
    stages.append({
        "stage": "composition",
        "decision": governor_result.get("composed_decision", ""),
        "rationale": governor_result.get("composed_rationale", ""),
    })

    # Operation state
    op_state = governor_result.get("operation_state")
    if op_state:
        stages.append({
            "stage": "operation",
            "state": op_state,
        })

    return stages


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
