"""Dependency-light TENIR execution kernel, compatible with schema 1.2."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping

from .geometry import (
    GEOMETRY_ID,
    REFERENCE_GEOMETRY,
    GeometryPolicy,
    coordinates_from_runtime_object,
    evaluate_geometry,
)


SCHEMA_VERSION = "1.2"
ENGINE_SPEC_VERSION = "TENIR-2.0"
VALID_MODES = frozenset({"SHADOW_OFF", "SHADOW_PASSIVE", "SHADOW_CRITICAL", "ENFORCE"})


class KernelInputError(ValueError):
    """An input is not admissible under the TENIR runtime contract."""


@dataclass(frozen=True)
class KernelPolicy:
    version: str = "tenir-kernel-1.2.0"
    epsilon: float = 1e-6
    hard_veto_below: float = 0.75
    flag_below: float = 0.90
    geometry: GeometryPolicy = REFERENCE_GEOMETRY

    def __post_init__(self) -> None:
        values = (self.epsilon, self.hard_veto_below, self.flag_below)
        if any(not math.isfinite(v) for v in values):
            raise KernelInputError("policy values must be finite")
        if self.epsilon <= 0 or self.hard_veto_below < 0 or self.flag_below <= self.hard_veto_below:
            raise KernelInputError("invalid kernel threshold ordering")


class TenirKernel:
    """Evaluates score thresholds and a server-owned TENIR 2.0 feasible region."""

    def __init__(self, policy: KernelPolicy | None = None) -> None:
        self.policy = policy or KernelPolicy()

    def adjudicate(
        self,
        *,
        pressure: float,
        velocity: float,
        capacity: float,
        coordinates: Mapping[str, float] | None = None,
        geometry_id: str = GEOMETRY_ID,
        mode: str = "SHADOW_PASSIVE",
        hard_gate_reasons: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        mode = mode.upper()
        if mode not in VALID_MODES:
            raise KernelInputError(f"unsupported operating mode: {mode}")
        if mode == "SHADOW_OFF":
            return {
                "schema_version": SCHEMA_VERSION,
                "engine_spec_version": ENGINE_SPEC_VERSION,
                "policy_version": self.policy.version,
                "operating_mode": mode,
                "s_score": None,
                "decision": "NOT_EVALUATED",
                "execution_disposition": "allow_unobserved",
                "execution_allowed": True,
                "rationale": "Governance evaluation is explicitly disabled in SHADOW_OFF.",
                "constraint_geometry": None,
            }

        p = _unit_interval("pressure", pressure)
        v = _unit_interval("velocity", velocity)
        k = _unit_interval("capacity", capacity)
        score = k / (p * v + self.policy.epsilon)

        geometry_result = None
        if coordinates is None:
            geometry_result = {
                "geometry_id": geometry_id,
                "policy_version": self.policy.geometry.version,
                "feasible": False,
                "minimum_signed_margin": None,
                "facet_results": [],
                "hard_violations": [{
                    "facet_id": "missing-geometry-point",
                    "severity": "hard",
                    "signed_margin": None,
                    "rationale": "Constraint geometry coordinates are required for adjudication.",
                }],
                "soft_violations": [],
            }
        else:
            geometry_result = evaluate_geometry(
                coordinates,
                geometry_id=geometry_id,
                policy=self.policy.geometry,
            )

        hard_reasons = list(hard_gate_reasons)
        if score <= self.policy.hard_veto_below:
            hard_reasons.append(f"S={score:.6f} is at or below hard-veto floor {self.policy.hard_veto_below}")
        hard_reasons.extend(v["rationale"] for v in geometry_result["hard_violations"])

        soft_reasons: list[str] = []
        if score < self.policy.flag_below:
            soft_reasons.append(f"S={score:.6f} is below flag floor {self.policy.flag_below}")
        soft_reasons.extend(v["rationale"] for v in geometry_result["soft_violations"])

        if hard_reasons:
            decision = "HARD_VETO"
            disposition = "block" if mode == "ENFORCE" else "allow_with_intended_block"
            allowed = mode != "ENFORCE"
            reasons = hard_reasons
        elif soft_reasons:
            decision = "FLAG"
            disposition = "allow_with_alert"
            allowed = True
            reasons = soft_reasons
        else:
            decision = "PASS"
            disposition = "allow"
            allowed = True
            reasons = ["Score and configured constraint geometry are within policy bounds."]

        return {
            "schema_version": SCHEMA_VERSION,
            "engine_spec_version": ENGINE_SPEC_VERSION,
            "policy_version": self.policy.version,
            "operating_mode": mode,
            "s_score": round(score, 8),
            "decision": decision,
            "execution_disposition": disposition,
            "execution_allowed": allowed,
            "rationale": " ".join(reasons),
            "constraint_geometry": geometry_result,
        }

    def adjudicate_runtime_object(
        self, runtime_object: Mapping[str, Any], *, mode: str = "SHADOW_PASSIVE"
    ) -> dict[str, Any]:
        """Validate and adjudicate a complete TENIR 2.0 schema 1.2 object."""
        if isinstance(mode, str) and mode.upper() == "SHADOW_OFF":
            return self.adjudicate(
                pressure=0.0,
                velocity=0.0,
                capacity=0.0,
                mode=mode,
            )
        from .schema import validate_runtime_object

        validated = validate_runtime_object(runtime_object)
        scores = validated["scores"]
        hard_reasons: list[str] = []
        if validated["tau"]["tau_integrity"] == "breached":
            hard_reasons.append("TENIR invariant tau is breached.")
        if validated["epistemic"]["status"] == "invalid":
            hard_reasons.append("Epistemic inputs are marked invalid.")
        if validated["posture"] == "HOLDING-FIRST":
            hard_reasons.append("The TENIR 2.0 posture is HOLDING-FIRST.")
        if validated.get("collapse_flags"):
            hard_reasons.append("TENIR 2.0 collapse flags are present.")
        pathway = validated.get("irreversible_pathway_assessment", {})
        if (
            pathway.get("materially_affects_irreversible_pathway")
            and pathway.get("raw_data_human_verification_required")
            and not pathway.get("human_verified_raw_data")
        ):
            hard_reasons.append("Material irreversible-pathway evidence lacks required human verification.")

        coordinates = coordinates_from_runtime_object(validated)
        return self.adjudicate(
            pressure=scores["pressure"],
            velocity=scores["velocity"],
            capacity=scores["capacity"],
            coordinates=coordinates,
            geometry_id=validated["constraint_geometry"]["geometry_id"],
            mode=mode,
            hard_gate_reasons=tuple(hard_reasons),
        )


def _unit_interval(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise KernelInputError(f"{name} must be a number")
    value = float(value)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise KernelInputError(f"{name} must be finite and in [0, 1]")
    return value
