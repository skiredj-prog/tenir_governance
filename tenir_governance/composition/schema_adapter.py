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
    active_profiles = context.get("active_profiles")
    if not isinstance(active_profiles, (list, tuple)) or any(
        not isinstance(profile, str) or not profile.strip()
        for profile in active_profiles
    ):
        raise ValueError(
            "canonical_context.active_profiles must explicitly list active profiles; "
            "the adapter will not infer profile activation"
        )
    active_profiles = list(active_profiles)
    if len(set(active_profiles)) != len(active_profiles):
        raise ValueError(
            "canonical_context.active_profiles must not contain duplicate profile identifiers"
        )
    trace_value = context.get("trace")
    if not isinstance(trace_value, Mapping):
        raise ValueError("canonical_context.trace must be an explicit mapping")
    trace_context = dict(trace_value)
    trace_id = governor_result.get("idempotency_key") or trace_context.get("trace_id")
    if not isinstance(trace_id, str) or not trace_id.strip():
        raise ValueError(
            "canonical_context.trace.trace_id or idempotency_key is required; "
            "the adapter will not fabricate a trace identifier"
        )
    trace_context["trace_id"] = trace_id
    canonical_stages = trace_context.get("stages")
    if canonical_stages is None or canonical_stages == []:
        trace_context["stages"] = _build_trace_stages(governor_result)
    elif not isinstance(canonical_stages, list):
        raise ValueError("canonical_context.trace.stages must be a list when supplied")
    for trace_key in ("ledger_entries", "ledger_valid", "passage_id"):
        if trace_key in governor_result:
            trace_context[trace_key] = governor_result[trace_key]
    degraded_modes = _degraded_modes(context, is_degraded)
    result.update({
        "schema_version": "1.2",
        "engine_spec_version": "TENIR-2.0",
        "posture": posture,
        # Profile-level execution fields remain additive; the canonical schema
        # has no closed-root additionalProperties restriction.
        "execution_mode": EXECUTION_MODE,
        "execution_disposition": EXECUTION_DISPOSITION_MAP.get(composed, "hold"),
        "execution_allowed": composed in (Decision.PASS.value, Decision.FLAG.value),
        "compliance_class": COMPLIANCE_CLASS,
        "degraded_mode": degraded_modes,
        "active_profiles": active_profiles,
        "trace": trace_context,
        "synthesis": {
            **dict(context.get("synthesis", {})),
            "final_posture": posture,
            "structural_core": dict(context.get("synthesis", {}).get("structural_core", {})),
            "epistemic_state": dict(context.get("synthesis", {}).get("epistemic_state", {})),
            "rationale": _rationale_items(governor_result),
            "degraded_inputs": [] if degraded_modes == ["none"] else degraded_modes,
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
            "active_profiles": active_profiles,
            "semantic_authority_held": [],
            "semantic_authority_not_held": [
                "canonical-doctrine", "schema-definition", "posture-determination",
            ],
        },
    })

    if "irreversible_pathway_assessment" in governor_result:
        assessment = governor_result["irreversible_pathway_assessment"]
        if not isinstance(assessment, Mapping):
            raise ValueError(
                "governor_result.irreversible_pathway_assessment must be a mapping when supplied"
            )
        result["irreversible_pathway_assessment"] = dict(assessment)
    if "constraint_geometry" in governor_result:
        geometry = governor_result["constraint_geometry"]
        if not isinstance(geometry, Mapping):
            raise ValueError(
                "governor_result.constraint_geometry must be a mapping when supplied"
            )
        result["constraint_geometry"] = dict(geometry)

    return validate_runtime_object(result)


def _degraded_modes(context: Mapping[str, Any], is_degraded: bool) -> list[str]:
    """Return schema-defined degradation labels without inventing causes."""
    supplied = context.get("degraded_mode")
    modes = list(supplied) if isinstance(supplied, (list, tuple)) else []
    allowed = {"none", "missing_agency", "missing_trajectory",
               "missing_epistemic", "sparse_ingress", "legacy_shape"}
    if not modes:
        raise ValueError("canonical_context.degraded_mode must explicitly describe degradation state")
    if any(not isinstance(mode, str) or mode not in allowed for mode in modes):
        raise ValueError("canonical_context.degraded_mode contains a non-string or unknown schema 1.2 label")
    if len(set(modes)) != len(modes):
        raise ValueError("canonical_context.degraded_mode must not contain duplicate labels")
    if "none" in modes and len(modes) > 1:
        raise ValueError("degraded_mode cannot combine 'none' with degradation labels")
    if is_degraded and modes == ["none"]:
        raise ValueError(
            "HOLD/HARD_VETO requires an explicit degraded_mode cause; "
            "the adapter will not infer one from the decision alone"
        )
    return modes


def _rationale_items(governor_result: Mapping[str, Any]) -> list[str]:
    """Normalize decision rationale to the canonical string-array contract."""
    items = []
    for key in ("composed_rationale", "scoring_rationale", "membrane_rationale"):
        value = governor_result.get(key)
        if isinstance(value, str) and value.strip() and value not in items:
            items.append(value)
    return items



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
            "status": (
                "failed" if str(operation_state).upper() in {"FAILED", "ERROR", "HARD_VETO", "REJECTED"}
                else "warning" if str(operation_state).upper() in {"UNKNOWN", "HOLD", "HELD"}
                else "ok"
            ),
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
    """Preserve an explicitly evidenced failure mode; never infer its cause.

    A posture or kernel decision alone is insufficient evidence to attribute
    a causal failure mode. The canonical context may provide the field directly,
    or the governor result may provide an explicit principal_failure_mode.
    """
    if "principal_failure_mode" in governor_result:
        supplied = governor_result["principal_failure_mode"]
    else:
        context = governor_result.get("canonical_context")
        synthesis = context.get("synthesis") if isinstance(context, Mapping) else None
        supplied = (
            synthesis["principal_failure_mode"]
            if isinstance(synthesis, Mapping) and "principal_failure_mode" in synthesis
            else None
        )

    if supplied is not None and (
        not isinstance(supplied, str) or not supplied.strip()
    ):
        raise ValueError(
            "principal_failure_mode must be a non-empty string or null when supplied"
        )
    return supplied
