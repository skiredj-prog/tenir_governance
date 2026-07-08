from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Mapping


SCHEMA_VERSION = "partner_b-sovereignty-pilot-v1"

DIMENSION_WEIGHTS: dict[str, dict[str, int]] = {
    "sovereignty": {
        "boundary_integrity": 30,
        "authority_legibility": 25,
        "forensic_verifiability": 25,
        "runtime_resilience": 20,
    },
    "adjudication": {
        "tension_clarity": 20,
        "tau_alignment": 25,
        "option_space_preservation": 20,
        "reversibility_containment": 15,
        "industrial_consequence_fit": 20,
    },
    "systemic_loop": {
        "incoming_pressure_legibility": 20,
        "response_fitness": 20,
        "outgoing_imprint_visibility": 15,
        "feedback_return_visibility": 20,
        "loop_lag_awareness": 10,
        "tau_exposure_control": 15,
    },
    "evidence_coverage": {
        "provenance": 30,
        "timeliness": 20,
        "completeness": 25,
        "cross_signal_consistency": 25,
    },
}

LOOP_STAGES = (
    "incoming_pressure",
    "system_response",
    "outgoing_imprint",
    "feedback_return",
    "loop_lag",
    "tau_exposure",
)

FAMILY_SCORE_LABELS = {
    "sovereignty": "sovereignty_score",
    "adjudication": "adjudication_score",
    "systemic_loop": "systemic_loop_score",
    "evidence_coverage": "evidence_coverage_score",
}

AUTHORITY_STATUSES = {"CLEAR", "REVIEW_REQUIRED", "CONTRADICTED"}
VERDICTS = {"INTEGRATE", "HOLD", "REJECT"}
NEXT_ACTIONS = {"OBSERVE", "LEARN", "TRANSMIT", "HOLD", "ENFORCE"}
EVIDENCE_GATES = {"LEARN_ONLY", "HOLD_OR_TRANSMIT", "FULL_ACTION"}


class PilotValidationError(ValueError):
    """Raised when the pilot payload does not satisfy the canonical shape."""


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_mapping(name: str, value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PilotValidationError(f"{name} must be an object")
    return value


def _require_string(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PilotValidationError(f"{name} must be a non-empty string")
    return value.strip()


def _require_string_list(name: str, value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise PilotValidationError(f"{name} must be a non-empty list of strings")
    items: list[str] = []
    for idx, item in enumerate(value):
        items.append(_require_string(f"{name}[{idx}]", item))
    return items


def _require_score(name: str, value: Any, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PilotValidationError(f"{name} must be a real number")
    numeric = float(value)
    if numeric < minimum or numeric > maximum:
        raise PilotValidationError(f"{name} must be between {minimum} and {maximum}")
    return numeric


def normalize_raw_score(raw: float) -> int:
    validated = _require_score("raw score", raw, minimum=0.0, maximum=5.0)
    return round((validated / 5.0) * 100)


def evidence_gate_for(score: float) -> str:
    if score < 50:
        return "LEARN_ONLY"
    if score < 70:
        return "HOLD_OR_TRANSMIT"
    return "FULL_ACTION"


def _validate_template(payload: Mapping[str, Any]) -> None:
    pilot = _require_mapping("pilot", payload.get("pilot"))
    tau_profile = _require_mapping("tau_profile", payload.get("tau_profile"))
    loop = _require_mapping("loop", payload.get("loop"))
    scores = _require_mapping("scores", payload.get("scores"))
    raw_dimensions = _require_mapping("scores.raw_dimensions", scores.get("raw_dimensions"))
    adjudication = _require_mapping("adjudication", payload.get("adjudication", {}))
    trace = _require_mapping("trace", payload.get("trace"))

    for field in ("pilot_id", "title", "institution", "workflow"):
        _require_string(f"pilot.{field}", pilot.get(field))
    reporting_mode = _require_string("pilot.reporting_mode", pilot.get("reporting_mode"))
    if reporting_mode != "dual":
        raise PilotValidationError("pilot.reporting_mode must be 'dual'")

    _require_string("tau_profile.tau_id", tau_profile.get("tau_id"))
    _require_string(
        "tau_profile.protected_identity_statement",
        tau_profile.get("protected_identity_statement"),
    )
    _require_string_list("tau_profile.invariants", tau_profile.get("invariants"))
    _require_mapping("tau_profile.thresholds", tau_profile.get("thresholds"))

    for stage in LOOP_STAGES:
        stage_payload = _require_mapping(f"loop.{stage}", loop.get(stage))
        _require_string(f"loop.{stage}.summary", stage_payload.get("summary"))
        _require_string(f"loop.{stage}.realm", stage_payload.get("realm"))
        _require_string_list(f"loop.{stage}.organ_tags", stage_payload.get("organ_tags"))
        _require_string_list(f"loop.{stage}.system_tags", stage_payload.get("system_tags"))
        _require_score(
            f"loop.{stage}.magnitude_raw",
            stage_payload.get("magnitude_raw"),
            minimum=0.0,
            maximum=5.0,
        )
        _require_score(
            f"loop.{stage}.confidence_raw",
            stage_payload.get("confidence_raw"),
            minimum=0.0,
            maximum=5.0,
        )
        evidence_refs = stage_payload.get("evidence_refs")
        if not isinstance(evidence_refs, list):
            raise PilotValidationError(f"loop.{stage}.evidence_refs must be a list")
        for idx, item in enumerate(evidence_refs):
            _require_string(f"loop.{stage}.evidence_refs[{idx}]", item)

    for family, dimensions in DIMENSION_WEIGHTS.items():
        family_payload = _require_mapping(
            f"scores.raw_dimensions.{family}",
            raw_dimensions.get(family),
        )
        for dimension in dimensions:
            _require_score(
                f"scores.raw_dimensions.{family}.{dimension}",
                family_payload.get(dimension),
                minimum=0.0,
                maximum=5.0,
            )

    authority_status = adjudication.get("authority_status", "REVIEW_REQUIRED")
    normalized_authority = _require_string(
        "adjudication.authority_status",
        authority_status,
    ).upper()
    if normalized_authority not in AUTHORITY_STATUSES:
        allowed = ", ".join(sorted(AUTHORITY_STATUSES))
        raise PilotValidationError(
            f"adjudication.authority_status must be one of {allowed}"
        )
    contradictions = adjudication.get("contradictions", [])
    if contradictions:
        _require_string_list("adjudication.contradictions", contradictions)
    protected = adjudication.get("protected_invariants", tau_profile.get("invariants", []))
    if protected:
        _require_string_list("adjudication.protected_invariants", protected)

    _require_string("trace.event_id", trace.get("event_id"))
    _require_string("trace.ledger_ref", trace.get("ledger_ref"))
    attachments = trace.get("attachments")
    if not isinstance(attachments, list):
        raise PilotValidationError("trace.attachments must be a list")
    for idx, item in enumerate(attachments):
        _require_string(f"trace.attachments[{idx}]", item)
    _require_string("trace.created_at", trace.get("created_at"))
    _require_string("trace.authoritative_state", trace.get("authoritative_state"))


def _normalize_dimensions(
    raw_dimensions: Mapping[str, Any],
) -> tuple[dict[str, dict[str, dict[str, float | int]]], dict[str, int]]:
    normalized_dimensions: dict[str, dict[str, dict[str, float | int]]] = {}
    family_totals: dict[str, int] = {}

    for family, dimensions in DIMENSION_WEIGHTS.items():
        family_payload = _require_mapping(
            f"scores.raw_dimensions.{family}",
            raw_dimensions.get(family),
        )
        weighted_total = 0.0
        normalized_family: dict[str, dict[str, float | int]] = {}
        for dimension, weight in dimensions.items():
            raw_value = _require_score(
                f"scores.raw_dimensions.{family}.{dimension}",
                family_payload.get(dimension),
                minimum=0.0,
                maximum=5.0,
            )
            normalized_value = normalize_raw_score(raw_value)
            normalized_family[dimension] = {
                "raw": round(raw_value, 2),
                "normalized": normalized_value,
            }
            weighted_total += normalized_value * weight
        normalized_dimensions[family] = normalized_family
        family_totals[FAMILY_SCORE_LABELS[family]] = round(weighted_total / 100)
    return normalized_dimensions, family_totals


def _top_contradictions(
    payload: Mapping[str, Any],
    *,
    normalized_dimensions: Mapping[str, Any],
    family_totals: Mapping[str, int],
    evidence_gate: str,
) -> list[str]:
    adjudication = _require_mapping("adjudication", payload.get("adjudication", {}))
    explicit = adjudication.get("contradictions", [])
    contradictions = [str(item).strip() for item in explicit if str(item).strip()]

    if not contradictions and evidence_gate != "FULL_ACTION":
        contradictions.append(
            "Evidence coverage constrains action before confidence can widen safely."
        )

    authority_status = str(adjudication.get("authority_status", "REVIEW_REQUIRED")).upper()
    if authority_status == "CONTRADICTED":
        contradictions.append(
            "Authority posture and operational recommendation are not yet aligned."
        )

    tau_control = normalized_dimensions["systemic_loop"]["tau_exposure_control"][
        "normalized"
    ]
    if tau_control <= 40:
        contradictions.append(
            "TAU exposure remains elevated across the active feedback loop."
        )

    if family_totals["sovereignty_score"] < 60:
        contradictions.append(
            "Sovereignty posture trails the level of consequence carried by the workflow."
        )

    if normalized_dimensions["adjudication"]["option_space_preservation"]["normalized"] < 50:
        contradictions.append(
            "Option space is narrowing faster than the current response can justify."
        )

    if not contradictions:
        contradictions.append(
            "No material contradiction dominates the current loop at review depth."
        )

    seen: set[str] = set()
    ordered: list[str] = []
    for item in contradictions:
        if item not in seen:
            ordered.append(item)
            seen.add(item)
    return ordered[:3]


def _derive_verdict(
    *,
    family_totals: Mapping[str, int],
    normalized_dimensions: Mapping[str, Any],
) -> str:
    sovereignty_score = family_totals["sovereignty_score"]
    tau_alignment = normalized_dimensions["adjudication"]["tau_alignment"]["normalized"]
    option_space = normalized_dimensions["adjudication"]["option_space_preservation"][
        "normalized"
    ]
    tension_clarity = normalized_dimensions["adjudication"]["tension_clarity"]["normalized"]
    tau_exposure_control = normalized_dimensions["systemic_loop"]["tau_exposure_control"][
        "normalized"
    ]

    if (
        sovereignty_score >= 65
        and tau_alignment >= 75
        and option_space >= 60
        and tension_clarity >= 60
        and tau_exposure_control >= 55
    ):
        return "INTEGRATE"
    if sovereignty_score < 45 or tau_alignment < 45 or tau_exposure_control <= 40:
        return "REJECT"
    return "HOLD"


def _derive_next_action(
    *,
    verdict: str,
    evidence_gate: str,
    authority_status: str,
    normalized_dimensions: Mapping[str, Any],
) -> str:
    tau_exposure_control = normalized_dimensions["systemic_loop"]["tau_exposure_control"][
        "normalized"
    ]
    industrial_consequence = normalized_dimensions["adjudication"][
        "industrial_consequence_fit"
    ]["normalized"]

    if evidence_gate == "LEARN_ONLY":
        return "LEARN"

    if authority_status == "CONTRADICTED":
        return "TRANSMIT"

    if verdict == "INTEGRATE":
        if evidence_gate == "FULL_ACTION":
            return "OBSERVE"
        return "HOLD"

    if verdict == "HOLD":
        if evidence_gate == "HOLD_OR_TRANSMIT":
            return "HOLD"
        return "HOLD"

    if (
        evidence_gate == "FULL_ACTION"
        and tau_exposure_control <= 40
        and industrial_consequence >= 60
    ):
        return "ENFORCE"

    if evidence_gate == "HOLD_OR_TRANSMIT":
        return "TRANSMIT"
    return "HOLD"


def _executive_summary(
    *,
    family_totals: Mapping[str, int],
    evidence_gate: str,
    verdict: str,
    next_action: str,
) -> str:
    return (
        f"partner_b sovereignty posture reads at {family_totals['sovereignty_score']}/100, "
        f"adjudication at {family_totals['adjudication_score']}/100, "
        f"systemic loop visibility at {family_totals['systemic_loop_score']}/100, "
        f"and evidence coverage at {family_totals['evidence_coverage_score']}/100. "
        f"The current verdict is {verdict} with a recommended next action of "
        f"{next_action}. Evidence gate: {evidence_gate}."
    )


def _operator_summary(
    *,
    loop: Mapping[str, Any],
    verdict: str,
    next_action: str,
) -> str:
    return (
        f"Loop reading: incoming pressure in {loop['incoming_pressure']['realm']} "
        f"crosses into system response through {', '.join(loop['system_response']['organ_tags'])}, "
        f"creates an outgoing imprint in {loop['outgoing_imprint']['realm']}, "
        f"then returns through {loop['feedback_return']['realm']} with lag visible in "
        f"{loop['loop_lag']['summary'].lower()}. Current posture: {verdict} / {next_action}."
    )


def _institutional_readout(
    *,
    pilot: Mapping[str, Any],
    verdict: str,
    next_action: str,
    contradictions: list[str],
) -> str:
    contradiction_clause = (
        f"Primary contradiction: {contradictions[0]}." if contradictions else ""
    )
    return (
        f"{pilot['institution']} can review this payload as a dual report rather than a "
        f"single dashboard. The institutional reading remains bounded, auditable, and "
        f"human-legible. Recommended posture: {verdict} with {next_action}. "
        f"{contradiction_clause}".strip()
    )


def _adjudication_readout(
    *,
    tau_profile: Mapping[str, Any],
    contradictions: list[str],
    verdict: str,
    next_action: str,
) -> str:
    protected = ", ".join(tau_profile["invariants"][:3])
    contradiction_clause = (
        f"Key contradiction: {contradictions[0]}." if contradictions else ""
    )
    return (
        f"Adjudication is being performed around TAU `{tau_profile['tau_id']}` as reference, "
        f"not as deciding subject. Protected invariants in view: {protected}. "
        f"Outcome: {verdict}. Next viable action: {next_action}. {contradiction_clause}"
    ).strip()


def build_pilot_payload(template: Mapping[str, Any]) -> dict[str, Any]:
    _validate_template(template)

    payload = deepcopy(dict(template))
    raw_dimensions = payload["scores"]["raw_dimensions"]
    normalized_dimensions, family_totals = _normalize_dimensions(raw_dimensions)

    evidence_score = family_totals["evidence_coverage_score"]
    evidence_gate = evidence_gate_for(evidence_score)
    authority_status = str(
        payload.get("adjudication", {}).get("authority_status", "REVIEW_REQUIRED")
    ).upper()
    verdict = _derive_verdict(
        family_totals=family_totals,
        normalized_dimensions=normalized_dimensions,
    )
    next_action = _derive_next_action(
        verdict=verdict,
        evidence_gate=evidence_gate,
        authority_status=authority_status,
        normalized_dimensions=normalized_dimensions,
    )

    contradictions = _top_contradictions(
        payload,
        normalized_dimensions=normalized_dimensions,
        family_totals=family_totals,
        evidence_gate=evidence_gate,
    )
    protected_invariants = payload.get("adjudication", {}).get(
        "protected_invariants",
        payload["tau_profile"]["invariants"][:3],
    )

    pilot_admissibility_score = round(
        (0.35 * family_totals["sovereignty_score"])
        + (0.30 * family_totals["adjudication_score"])
        + (0.20 * family_totals["systemic_loop_score"])
        + (0.15 * family_totals["evidence_coverage_score"])
    )

    payload["schema_version"] = payload.get("schema_version", SCHEMA_VERSION)
    payload["scores"] = {
        "raw_dimensions": raw_dimensions,
        "normalized_dimensions": normalized_dimensions,
        "family_totals": family_totals,
        "pilot_admissibility_score": pilot_admissibility_score,
        "evidence_gate": evidence_gate,
    }
    payload["adjudication"] = {
        "verdict": verdict,
        "next_action": next_action,
        "rationale": (
            f"{verdict} was selected from the relationship between sovereignty posture, "
            f"adjudication quality, loop visibility, and evidence gate {evidence_gate}."
        ),
        "contradictions": contradictions,
        "protected_invariants": protected_invariants,
        "authority_status": authority_status,
    }
    payload["report_layers"] = {
        "executive_summary": _executive_summary(
            family_totals=family_totals,
            evidence_gate=evidence_gate,
            verdict=verdict,
            next_action=next_action,
        ),
        "operator_summary": _operator_summary(
            loop=payload["loop"],
            verdict=verdict,
            next_action=next_action,
        ),
        "institutional_readout": _institutional_readout(
            pilot=payload["pilot"],
            verdict=verdict,
            next_action=next_action,
            contradictions=contradictions,
        ),
        "adjudication_readout": _adjudication_readout(
            tau_profile=payload["tau_profile"],
            contradictions=contradictions,
            verdict=verdict,
            next_action=next_action,
        ),
    }
    validate_pilot_payload(payload)
    return payload


def validate_pilot_payload(payload: Mapping[str, Any]) -> None:
    _validate_template(payload)

    scores = _require_mapping("scores", payload.get("scores"))
    normalized_dimensions = _require_mapping(
        "scores.normalized_dimensions",
        scores.get("normalized_dimensions"),
    )
    family_totals = _require_mapping("scores.family_totals", scores.get("family_totals"))
    for family, dimensions in DIMENSION_WEIGHTS.items():
        family_payload = _require_mapping(
            f"scores.normalized_dimensions.{family}",
            normalized_dimensions.get(family),
        )
        for dimension in dimensions:
            dimension_payload = _require_mapping(
                f"scores.normalized_dimensions.{family}.{dimension}",
                family_payload.get(dimension),
            )
            _require_score(
                f"scores.normalized_dimensions.{family}.{dimension}.raw",
                dimension_payload.get("raw"),
                minimum=0.0,
                maximum=5.0,
            )
            _require_score(
                f"scores.normalized_dimensions.{family}.{dimension}.normalized",
                dimension_payload.get("normalized"),
                minimum=0.0,
                maximum=100.0,
            )
    for label in FAMILY_SCORE_LABELS.values():
        _require_score(
            f"scores.family_totals.{label}",
            family_totals.get(label),
            minimum=0.0,
            maximum=100.0,
        )
    _require_score(
        "scores.pilot_admissibility_score",
        scores.get("pilot_admissibility_score"),
        minimum=0.0,
        maximum=100.0,
    )
    evidence_gate = _require_string("scores.evidence_gate", scores.get("evidence_gate"))
    if evidence_gate not in EVIDENCE_GATES:
        allowed = ", ".join(sorted(EVIDENCE_GATES))
        raise PilotValidationError(f"scores.evidence_gate must be one of {allowed}")

    adjudication = _require_mapping("adjudication", payload.get("adjudication"))
    verdict = _require_string("adjudication.verdict", adjudication.get("verdict"))
    if verdict not in VERDICTS:
        allowed = ", ".join(sorted(VERDICTS))
        raise PilotValidationError(f"adjudication.verdict must be one of {allowed}")
    next_action = _require_string(
        "adjudication.next_action",
        adjudication.get("next_action"),
    )
    if next_action not in NEXT_ACTIONS:
        allowed = ", ".join(sorted(NEXT_ACTIONS))
        raise PilotValidationError(
            f"adjudication.next_action must be one of {allowed}"
        )
    _require_string("adjudication.rationale", adjudication.get("rationale"))
    _require_string_list(
        "adjudication.contradictions",
        adjudication.get("contradictions"),
    )
    _require_string_list(
        "adjudication.protected_invariants",
        adjudication.get("protected_invariants"),
    )
    authority_status = _require_string(
        "adjudication.authority_status",
        adjudication.get("authority_status"),
    )
    if authority_status not in AUTHORITY_STATUSES:
        allowed = ", ".join(sorted(AUTHORITY_STATUSES))
        raise PilotValidationError(
            f"adjudication.authority_status must be one of {allowed}"
        )

    report_layers = _require_mapping("report_layers", payload.get("report_layers"))
    for field in (
        "executive_summary",
        "operator_summary",
        "institutional_readout",
        "adjudication_readout",
    ):
        _require_string(f"report_layers.{field}", report_layers.get(field))


def sample_pilot_template(case: str = "amplified_loop") -> dict[str, Any]:
    cases: dict[str, dict[str, Any]] = {
        "stabilizing_loop": {
            "schema_version": SCHEMA_VERSION,
            "pilot": {
                "pilot_id": "partner_b-sovereignty-stabilizing-loop",
                "title": "partner_b Sovereignty Pilot - Stabilizing Procurement Loop",
                "institution": "partner_b Sovereignty",
                "workflow": "sense-adjudicate-decide-act",
                "reporting_mode": "dual",
            },
            "tau_profile": {
                "tau_id": "TAU-partner_b-SOV-01",
                "protected_identity_statement": (
                    "Protect sovereign decision continuity without sacrificing "
                    "human legibility or bounded intervention."
                ),
                "invariants": [
                    "Bounded authority",
                    "Verifiable truth path",
                    "Continuity under consequence",
                    "Human answerability",
                ],
                "thresholds": {
                    "evidence_gate_enforce_min": 70,
                    "tau_exposure_control_enforce_max": 40,
                },
            },
            "loop": {
                "incoming_pressure": {
                    "summary": "Industrial disruption creates a procurement acceleration request.",
                    "realm": "industrial-operations",
                    "organ_tags": ["perception", "membrane"],
                    "system_tags": ["governance", "supply-chain"],
                    "magnitude_raw": 3.6,
                    "confidence_raw": 4.3,
                    "evidence_refs": ["event:e-001", "memo:ops-brief-7"],
                },
                "system_response": {
                    "summary": "The system routes the request into shadow review with explicit authority check.",
                    "realm": "authority",
                    "organ_tags": ["membrane", "transmission"],
                    "system_tags": ["governance", "review"],
                    "magnitude_raw": 3.0,
                    "confidence_raw": 4.0,
                    "evidence_refs": ["ledger:l-001", "approval:rev-17"],
                },
                "outgoing_imprint": {
                    "summary": "The review slows exception velocity without freezing the workflow.",
                    "realm": "industrial-operations",
                    "organ_tags": ["transmission", "memory"],
                    "system_tags": ["workflow", "governance"],
                    "magnitude_raw": 2.6,
                    "confidence_raw": 3.8,
                    "evidence_refs": ["memo:buyer-note-2"],
                },
                "feedback_return": {
                    "summary": "The environment responds with reduced escalation and cleaner evidence.",
                    "realm": "information",
                    "organ_tags": ["perception", "memory"],
                    "system_tags": ["evidence", "governance"],
                    "magnitude_raw": 2.2,
                    "confidence_raw": 3.9,
                    "evidence_refs": ["event:e-002", "report:signal-cleanup"],
                },
                "loop_lag": {
                    "summary": "Return signal becomes legible within one shift window.",
                    "realm": "time",
                    "organ_tags": ["memory"],
                    "system_tags": ["temporal-governance"],
                    "magnitude_raw": 1.9,
                    "confidence_raw": 3.5,
                    "evidence_refs": ["timeline:t-12h"],
                },
                "tau_exposure": {
                    "summary": "TAU remains exposed but within bounded and recoverable range.",
                    "realm": "identity",
                    "organ_tags": ["membrane", "conscience"],
                    "system_tags": ["governance"],
                    "magnitude_raw": 2.1,
                    "confidence_raw": 4.2,
                    "evidence_refs": ["ledger:l-002"],
                },
            },
            "scores": {
                "raw_dimensions": {
                    "sovereignty": {
                        "boundary_integrity": 4.2,
                        "authority_legibility": 4.0,
                        "forensic_verifiability": 4.1,
                        "runtime_resilience": 3.8,
                    },
                    "adjudication": {
                        "tension_clarity": 3.9,
                        "tau_alignment": 4.2,
                        "option_space_preservation": 3.8,
                        "reversibility_containment": 3.6,
                        "industrial_consequence_fit": 3.9,
                    },
                    "systemic_loop": {
                        "incoming_pressure_legibility": 4.0,
                        "response_fitness": 3.8,
                        "outgoing_imprint_visibility": 3.5,
                        "feedback_return_visibility": 3.8,
                        "loop_lag_awareness": 3.2,
                        "tau_exposure_control": 3.7,
                    },
                    "evidence_coverage": {
                        "provenance": 4.1,
                        "timeliness": 3.9,
                        "completeness": 3.7,
                        "cross_signal_consistency": 3.8,
                    },
                }
            },
            "adjudication": {
                "authority_status": "CLEAR",
                "contradictions": [
                    "Industrial urgency still pushes against bounded review latency."
                ],
                "protected_invariants": [
                    "Bounded authority",
                    "Verifiable truth path",
                    "Human answerability",
                ],
            },
            "trace": {
                "event_id": "evt-stabilizing-001",
                "ledger_ref": "audit/governance_ledger.jsonl#l-002",
                "attachments": ["ops-brief-7.pdf", "buyer-note-2.txt"],
                "created_at": _iso_now(),
                "authoritative_state": "SHADOW_PASSIVE",
            },
        },
        "amplified_loop": {
            "schema_version": SCHEMA_VERSION,
            "pilot": {
                "pilot_id": "partner_b-sovereignty-amplified-loop",
                "title": "partner_b Sovereignty Pilot - Amplified Maintenance Override Loop",
                "institution": "partner_b Sovereignty",
                "workflow": "sense-adjudicate-decide-act",
                "reporting_mode": "dual",
            },
            "tau_profile": {
                "tau_id": "TAU-partner_b-SOV-01",
                "protected_identity_statement": (
                    "Protect sovereign decision continuity without sacrificing "
                    "human legibility or bounded intervention."
                ),
                "invariants": [
                    "Bounded authority",
                    "Verifiable truth path",
                    "Continuity under consequence",
                    "Human answerability",
                ],
                "thresholds": {
                    "evidence_gate_enforce_min": 70,
                    "tau_exposure_control_enforce_max": 40,
                },
            },
            "loop": {
                "incoming_pressure": {
                    "summary": "An outage-driven override request enters with plant-wide urgency.",
                    "realm": "industrial-operations",
                    "organ_tags": ["perception", "membrane"],
                    "system_tags": ["maintenance", "governance"],
                    "magnitude_raw": 4.7,
                    "confidence_raw": 4.5,
                    "evidence_refs": ["event:e-017", "telemetry:cluster-9"],
                },
                "system_response": {
                    "summary": "The first response accelerates approval routing before authority is fully settled.",
                    "realm": "authority",
                    "organ_tags": ["transmission", "membrane"],
                    "system_tags": ["workflow", "governance"],
                    "magnitude_raw": 3.9,
                    "confidence_raw": 4.1,
                    "evidence_refs": ["ledger:l-090", "message:chain-12"],
                },
                "outgoing_imprint": {
                    "summary": "The accelerated posture amplifies environmental expectation for more exceptions.",
                    "realm": "institutional",
                    "organ_tags": ["transmission", "memory"],
                    "system_tags": ["authority", "compliance"],
                    "magnitude_raw": 4.2,
                    "confidence_raw": 3.9,
                    "evidence_refs": ["memo:exception-spread"],
                },
                "feedback_return": {
                    "summary": "The environment returns with a second override demand and degraded confidence.",
                    "realm": "information",
                    "organ_tags": ["perception", "memory"],
                    "system_tags": ["evidence", "governance"],
                    "magnitude_raw": 4.4,
                    "confidence_raw": 4.2,
                    "evidence_refs": ["event:e-018", "audit:delta-risk-1"],
                },
                "loop_lag": {
                    "summary": "The loop closes within hours, before review discipline catches up.",
                    "realm": "time",
                    "organ_tags": ["memory"],
                    "system_tags": ["temporal-governance"],
                    "magnitude_raw": 3.7,
                    "confidence_raw": 4.0,
                    "evidence_refs": ["timeline:t-4h"],
                },
                "tau_exposure": {
                    "summary": "TAU exposure rises because the system is teaching the environment to demand bypasses.",
                    "realm": "identity",
                    "organ_tags": ["membrane", "conscience"],
                    "system_tags": ["governance"],
                    "magnitude_raw": 4.5,
                    "confidence_raw": 4.4,
                    "evidence_refs": ["ledger:l-091", "audit:delta-risk-1"],
                },
            },
            "scores": {
                "raw_dimensions": {
                    "sovereignty": {
                        "boundary_integrity": 3.1,
                        "authority_legibility": 3.3,
                        "forensic_verifiability": 4.0,
                        "runtime_resilience": 3.8,
                    },
                    "adjudication": {
                        "tension_clarity": 4.3,
                        "tau_alignment": 2.2,
                        "option_space_preservation": 2.4,
                        "reversibility_containment": 2.8,
                        "industrial_consequence_fit": 4.5,
                    },
                    "systemic_loop": {
                        "incoming_pressure_legibility": 4.2,
                        "response_fitness": 2.0,
                        "outgoing_imprint_visibility": 3.8,
                        "feedback_return_visibility": 4.1,
                        "loop_lag_awareness": 3.6,
                        "tau_exposure_control": 1.4,
                    },
                    "evidence_coverage": {
                        "provenance": 4.0,
                        "timeliness": 4.2,
                        "completeness": 3.8,
                        "cross_signal_consistency": 3.9,
                    },
                }
            },
            "adjudication": {
                "authority_status": "CLEAR",
                "contradictions": [
                    "Short-term industrial relief is increasing long-term authority exposure.",
                    "Evidence is sufficient, but the current response is worsening the loop."
                ],
                "protected_invariants": [
                    "Bounded authority",
                    "Continuity under consequence",
                    "Human answerability",
                ],
            },
            "trace": {
                "event_id": "evt-amplified-017",
                "ledger_ref": "audit/governance_ledger.jsonl#l-091",
                "attachments": ["exception-spread.pdf", "delta-risk-1.json"],
                "created_at": _iso_now(),
                "authoritative_state": "SHADOW_CRITICAL",
            },
        },
        "authority_contradiction": {
            "schema_version": SCHEMA_VERSION,
            "pilot": {
                "pilot_id": "partner_b-sovereignty-authority-contradiction",
                "title": "partner_b Sovereignty Pilot - Authority Contradiction Loop",
                "institution": "partner_b Sovereignty",
                "workflow": "sense-adjudicate-decide-act",
                "reporting_mode": "dual",
            },
            "tau_profile": {
                "tau_id": "TAU-partner_b-SOV-01",
                "protected_identity_statement": (
                    "Protect sovereign decision continuity without sacrificing "
                    "human legibility or bounded intervention."
                ),
                "invariants": [
                    "Bounded authority",
                    "Verifiable truth path",
                    "Continuity under consequence",
                    "Human answerability",
                ],
                "thresholds": {
                    "evidence_gate_enforce_min": 70,
                    "tau_exposure_control_enforce_max": 40,
                },
            },
            "loop": {
                "incoming_pressure": {
                    "summary": "A strategic exception request arrives through parallel informal channels.",
                    "realm": "institutional",
                    "organ_tags": ["perception", "memory"],
                    "system_tags": ["governance", "authority"],
                    "magnitude_raw": 3.8,
                    "confidence_raw": 3.5,
                    "evidence_refs": ["mail:chain-44", "note:meeting-1"],
                },
                "system_response": {
                    "summary": "The system can explain the situation but cannot certify the authority path.",
                    "realm": "authority",
                    "organ_tags": ["membrane", "conscience"],
                    "system_tags": ["governance", "review"],
                    "magnitude_raw": 2.7,
                    "confidence_raw": 3.7,
                    "evidence_refs": ["ledger:l-121", "review:r-31"],
                },
                "outgoing_imprint": {
                    "summary": "The unresolved posture sends mixed signals back into the environment.",
                    "realm": "institutional",
                    "organ_tags": ["transmission"],
                    "system_tags": ["authority", "workflow"],
                    "magnitude_raw": 3.1,
                    "confidence_raw": 3.0,
                    "evidence_refs": ["memo:mixed-signal"],
                },
                "feedback_return": {
                    "summary": "Conflicting expectations return as pressure for immediate exception handling.",
                    "realm": "information",
                    "organ_tags": ["perception", "memory"],
                    "system_tags": ["evidence", "authority"],
                    "magnitude_raw": 3.5,
                    "confidence_raw": 3.3,
                    "evidence_refs": ["event:e-044", "mail:chain-45"],
                },
                "loop_lag": {
                    "summary": "The contradiction persists across multiple review windows.",
                    "realm": "time",
                    "organ_tags": ["memory"],
                    "system_tags": ["temporal-governance"],
                    "magnitude_raw": 3.0,
                    "confidence_raw": 3.4,
                    "evidence_refs": ["timeline:t-3d"],
                },
                "tau_exposure": {
                    "summary": "TAU is exposed less by pressure itself than by ambiguity in who can answer.",
                    "realm": "identity",
                    "organ_tags": ["conscience", "membrane"],
                    "system_tags": ["governance", "authority"],
                    "magnitude_raw": 3.4,
                    "confidence_raw": 3.6,
                    "evidence_refs": ["review:r-31"],
                },
            },
            "scores": {
                "raw_dimensions": {
                    "sovereignty": {
                        "boundary_integrity": 3.7,
                        "authority_legibility": 1.8,
                        "forensic_verifiability": 3.8,
                        "runtime_resilience": 3.5,
                    },
                    "adjudication": {
                        "tension_clarity": 4.1,
                        "tau_alignment": 2.8,
                        "option_space_preservation": 3.1,
                        "reversibility_containment": 3.2,
                        "industrial_consequence_fit": 3.8,
                    },
                    "systemic_loop": {
                        "incoming_pressure_legibility": 3.9,
                        "response_fitness": 2.7,
                        "outgoing_imprint_visibility": 3.1,
                        "feedback_return_visibility": 3.6,
                        "loop_lag_awareness": 3.1,
                        "tau_exposure_control": 2.6,
                    },
                    "evidence_coverage": {
                        "provenance": 3.4,
                        "timeliness": 3.0,
                        "completeness": 3.3,
                        "cross_signal_consistency": 3.1,
                    },
                }
            },
            "adjudication": {
                "authority_status": "CONTRADICTED",
                "contradictions": [
                    "The recommendation is more legible than the authority path allowed to carry it."
                ],
                "protected_invariants": [
                    "Bounded authority",
                    "Verifiable truth path",
                    "Human answerability",
                ],
            },
            "trace": {
                "event_id": "evt-authority-044",
                "ledger_ref": "audit/governance_ledger.jsonl#l-121",
                "attachments": ["meeting-1.txt", "mixed-signal.pdf"],
                "created_at": _iso_now(),
                "authoritative_state": "SHADOW_PASSIVE",
            },
        },
    }

    try:
        template = cases[case]
    except KeyError as exc:
        allowed = ", ".join(sorted(cases))
        raise KeyError(f"unknown sample case '{case}'. Allowed: {allowed}") from exc
    return deepcopy(template)
