"""Deterministic evaluation of TENIR 2.0 constraint-space facets.

Client inputs carry a point and a geometry ID. Facets are server-owned policy,
so a caller cannot weaken a geometry by submitting its own constraint planes.
All coordinates are normalized to [0, 1]. A facet is the half-space
``normal · point <= bound``; its signed margin is the Euclidean distance to
the boundary (positive means inside).
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping


GEOMETRY_ID = "tenir-2.0-reference"
GEOMETRY_POLICY_VERSION = "tenir-geometry-1.2.0"
ALLOWED_AXES = frozenset(
    {
        "pressure",
        "velocity",
        "capacity",
        "option_space",
        "cooperative_control_score",
        "trajectory_risk_score",
        "irreversible_pathway_materiality",
        "evidence_quality",
        "manipulation_risk",
        "drift",
        "authority",
        "context_fit",
    }
)


@dataclass(frozen=True)
class Facet:
    facet_id: str
    normal: Mapping[str, float]
    bound: float
    severity: str
    rationale: str


@dataclass(frozen=True)
class GeometryPolicy:
    geometry_id: str
    version: str
    facets: tuple[Facet, ...]


REFERENCE_GEOMETRY = GeometryPolicy(
    geometry_id=GEOMETRY_ID,
    version=GEOMETRY_POLICY_VERSION,
    facets=(
        Facet(
            "authority-floor",
            {"authority": -1.0},
            -0.50,
            "hard",
            "The proposed transition lacks the reference minimum authority score.",
        ),
        Facet(
            "context-fit-floor",
            {"context_fit": -1.0},
            -0.50,
            "hard",
            "The proposed transition does not sufficiently fit its declared context.",
        ),
        Facet(
            "irreversibility-authority-boundary",
            {"irreversible_pathway_materiality": 1.0, "authority": -1.0},
            0.25,
            "hard",
            "Irreversible-pathway materiality exceeds the authority margin.",
        ),
        Facet(
            "evidence-quality-floor",
            {"evidence_quality": -1.0},
            -0.40,
            "soft",
            "Evidence quality is below the reference review threshold.",
        ),
        Facet(
            "load-capacity-envelope",
            {"pressure": 1.0, "velocity": 1.0, "capacity": -1.0},
            0.75,
            "soft",
            "Combined pressure and velocity approach the declared capacity boundary.",
        ),
    ),
)


def coordinates_from_runtime_object(runtime_object: Mapping[str, Any]) -> dict[str, float]:
    """Project the TENIR 2.0 object onto geometry axes without LLM inference."""
    scores = runtime_object["scores"]
    epistemic = runtime_object["epistemic"]
    geometry = runtime_object["constraint_geometry"]
    coordinates = geometry["coordinates"]
    point = {
        "pressure": scores["pressure"],
        "velocity": scores["velocity"],
        "capacity": scores["capacity"],
        "option_space": scores["option_space"],
        "cooperative_control_score": scores["cooperative_control_score"],
        "trajectory_risk_score": scores["trajectory_risk_score"],
        "irreversible_pathway_materiality": scores["irreversible_pathway_materiality"],
        "evidence_quality": epistemic["evidence_quality"],
        "manipulation_risk": epistemic["manipulation_risk"],
        "drift": epistemic["drift"],
        "authority": coordinates["authority"],
        "context_fit": coordinates["context_fit"],
    }
    return _validate_point(point)


def evaluate_geometry(
    point: Mapping[str, Any],
    geometry_id: str = GEOMETRY_ID,
    policy: GeometryPolicy = REFERENCE_GEOMETRY,
) -> dict[str, Any]:
    """Return a stable, auditable report for every facet in a trusted policy."""
    if geometry_id != policy.geometry_id:
        return {
            "geometry_id": geometry_id,
            "policy_version": policy.version,
            "feasible": False,
            "minimum_signed_margin": None,
            "facet_results": [],
            "hard_violations": [
                {
                    "facet_id": "unknown-geometry",
                    "severity": "hard",
                    "signed_margin": None,
                    "rationale": "Geometry ID is not configured by the active server policy.",
                }
            ],
            "soft_violations": [],
        }

    normalized = _validate_point(point)
    facet_results: list[dict[str, Any]] = []
    hard_violations: list[dict[str, Any]] = []
    soft_violations: list[dict[str, Any]] = []
    margins: list[float] = []

    for facet in policy.facets:
        missing = sorted(set(facet.normal) - normalized.keys())
        if missing:
            result = {
                "facet_id": facet.facet_id,
                "severity": "hard",
                "signed_margin": None,
                "rationale": f"Missing required geometry coordinate(s): {', '.join(missing)}.",
            }
            facet_results.append(result)
            hard_violations.append(result)
            continue

        value = sum(coefficient * normalized[axis] for axis, coefficient in facet.normal.items())
        norm = math.sqrt(sum(coefficient * coefficient for coefficient in facet.normal.values()))
        margin = (facet.bound - value) / norm
        margins.append(margin)
        result = {
            "facet_id": facet.facet_id,
            "severity": facet.severity,
            "signed_margin": round(margin, 8),
            "rationale": facet.rationale,
        }
        facet_results.append(result)
        if margin < -1e-9:
            (soft_violations if facet.severity == "soft" else hard_violations).append(result)

    return {
        "geometry_id": geometry_id,
        "policy_version": policy.version,
        "feasible": not hard_violations,
        "minimum_signed_margin": round(min(margins), 8) if margins else None,
        "facet_results": facet_results,
        "hard_violations": hard_violations,
        "soft_violations": soft_violations,
    }


def _validate_point(point: Mapping[str, Any]) -> dict[str, float]:
    if not isinstance(point, Mapping):
        raise ValueError("constraint geometry coordinates must be an object")
    unknown = set(point) - ALLOWED_AXES
    if unknown:
        raise ValueError(f"unknown constraint geometry axis/axes: {', '.join(sorted(unknown))}")
    normalized: dict[str, float] = {}
    for axis, raw_value in point.items():
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise ValueError(f"constraint coordinate {axis!r} must be a number")
        value = float(raw_value)
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError(f"constraint coordinate {axis!r} must be finite and in [0, 1]")
        normalized[axis] = value
    return normalized
