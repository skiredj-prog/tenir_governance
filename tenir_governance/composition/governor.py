# Profile: agentic-governance-profile-v1.0.0
# Compliance class: profile-bound
# This module is an extension on top of KB3 and TENIR 2.0 schema 1.2.
# It is NOT canonical, NOT doctrinal. It carries implementation-level
# governance enforcement authority only.
"""Dual-kernel governor — bridges the scoring kernel to TenirKernel.

This is the composition-layer governor. It replaces the prototype's
DualKernelGovernor (which used the simplified ScoringKernel) with one that
delegates the continuous-risk evaluation to the TenirKernel.

Bridge mapping (prototype ScoringKernel → TenirKernel):
    ScoringKernel.adjudicate(pressure, velocity, capacity, option_space,
                             authority, evidence_quality,
                             irreversibility_materiality)
    → TenirKernel.adjudicate(pressure, velocity, capacity,
                             mode="ENFORCE",
                             hard_gate_reasons=..., soft_gate_reasons=...)

The TenirKernel already computes S = K/(P·V+ε) and evaluates constraint
geometry facets. It returns a dict with "decision" in {PASS, FLAG, HARD_VETO}
which maps directly to the composition layer's Decision enum.

Extra prototype signals (tau floor, option_space, evidence quality,
irreversibility-authority) that TenirKernel does not natively evaluate
are computed here and injected as hard/soft gate reasons. This preserves
the prototype's safety properties while using TenirKernel's richer scoring
and geometry evaluation.

Per KB3: posture and execution mode are separate fields. The governor
returns implementation-level Decision enums; the schema conformance
adapter (schema_adapter.py) maps them to canonical TENIR 2.0 posture
and execution_disposition fields.

Per versioning policy §6: this module holds implementation-level
governance enforcement authority only. It does not hold semantic authority
over TENIR doctrine.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import re
import threading
import time
from typing import Any, Mapping, Optional
from uuid import uuid4

from .decisions import Decision, ExecutionStatus, CheckType
from .policy import CompositionPolicy
from .lattice import compose, HashChainedLedger
from ..operations.budget import BudgetCoordinator, InProcessBudgetCoordinator
from ..operations.registry import (
    DurableOperationRegistry,
    OperationRecord,
    OperationState,
    HoldRecord,
    ExitReceipt,
    NonExecutionAttestation,
)
from ..operations.hold import HoldManager
from ..operations.payload_binding import canonical_payload_hash
from .errors import InvalidArgumentError
from ..assessment.assessor import PostExecutionAssessor, AssessmentReport


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_now_epoch() -> float:
    return datetime.now(timezone.utc).timestamp()


def _has_hypothesis_violated_signal(result) -> bool:
    """Return true only for the explicit S3 contradiction signal."""
    signals = getattr(result, "safety_signals", None) or []
    raw_response = getattr(result, "raw_response", None)
    if isinstance(raw_response, dict):
        direct_signal = raw_response.get("s3_signal")
        if isinstance(direct_signal, str):
            signals = [*signals, direct_signal]
        raw_signals = raw_response.get("safety_signals", [])
        if isinstance(raw_signals, list):
            signals = [*signals, *raw_signals]
    return any(signal == "HYPOTHESIS_VIOLATED" for signal in signals)


# ── Scoring Adapter ─────────────────────────────────────────────────────

class ScoringKernelAdapter:
    """Adapts TenirKernel to the scoring-kernel interface expected by the governor.

    The prototype's ScoringKernel computed extra signals (tau floor, option
    space, evidence quality, irreversibility-authority) that TenirKernel does
    not natively evaluate. This adapter computes those signals and injects
    them as hard/soft gate reasons, then delegates the core S-score and
    constraint-geometry evaluation to TenirKernel.

    The returned dict matches the prototype's ScoringKernel.adjudicate()
    return shape so the governor and composition layer work unchanged.
    """

    # The admissible domain this kernel declares for every measure it reads
    # (draft RFC-2 v0.2 §1.4: the kernel declares its domain, the standard
    # does not prescribe it). Closed interval; a finite value outside it is
    # rejected explicitly (INVALID_ARGUMENT), it is never adjudicated.
    MEASURE_DOMAIN: tuple[float, float] = (0.0, 1.0)
    MEASURED_AXES: tuple[str, ...] = (
        "pressure", "velocity", "capacity", "option_space", "authority",
        "context_fit", "evidence_quality", "irreversibility_materiality",
    )

    @classmethod
    def declared_domain(cls) -> dict[str, tuple[float, float]]:
        """The declared admissible domain of each measured axis, by name."""
        return {name: cls.MEASURE_DOMAIN for name in cls.MEASURED_AXES}

    @classmethod
    def out_of_domain(cls, named: dict[str, Any]) -> list[str]:
        """Names of the measures whose value is a finite number outside the
        declared domain. ``None`` (absent) and non-finite values (NaN, ±inf)
        are not in this list: they are unknown measures, handled by rule A.
        A boolean or a non-number is not a value either."""
        low, high = cls.MEASURE_DOMAIN
        return [
            n for n, v in named.items()
            if v is not None and not isinstance(v, bool) and isinstance(v, (int, float))
            and math.isfinite(v) and not (low <= v <= high)
        ]

    @classmethod
    def malformed_measures(cls, named: dict[str, Any]) -> list[str]:
        """Names of the measures whose value is not a number at all: a
        boolean, a string, a container. Not a measure, not an unknown either
        (author's decision of 23 November 2026, 11:22 UTC: rejected like a
        value outside the declared domain). ``None`` and NaN/±inf are not in
        this list: they are unknown measures (rule A)."""
        return [n for n, v in named.items()
                if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)))]

    @classmethod
    def _malformed_reason(cls, named: dict[str, Any], names: list[str]) -> str:
        values = "; ".join(f"{n}={named[n]!r} ({type(named[n]).__name__})" for n in names)
        return (
            f"Measure(s) that are not numbers: {values}. Request refused explicitly; not a "
            "governance decision. A measure is a finite number in the kernel's declared "
            "domain; an unknown measure is passed as None or NaN."
        )

    @classmethod
    def _out_of_domain_reason(cls, named: dict[str, Any], names: list[str]) -> str:
        low, high = cls.MEASURE_DOMAIN
        values = "; ".join(f"{n}={named[n]!r}" for n in names)
        return (
            f"Measure(s) outside the kernel's declared domain [{low:g}, {high:g}]: {values}. "
            "Request refused explicitly; not a governance decision. The domain is "
            "declared by the kernel (ScoringKernelAdapter.declared_domain()); an "
            "unknown measure is passed as None or NaN, not as an out-of-domain value."
        )

    def __init__(self, tenir_kernel, *, tau_floor: float = 0.42,
                 option_space_alert_floor: float = 0.35,
                 option_space_block_floor: float = 0.20,
                 reaction_budget_events: int = 5):
        self._kernel = tenir_kernel
        self._tau_floor = tau_floor
        self._option_space_alert_floor = option_space_alert_floor
        self._option_space_block_floor = option_space_block_floor
        self._reaction_budget_events = reaction_budget_events
        self._reject_facets_on_never_measured_axes()

    # Axes this interface never measures. The kernel requires a structurally
    # complete geometry point, so the main path fills them with a neutral
    # placeholder (0.5) that no facet may read (R7, author's decision of
    # 2026-11-23: an omitted axis is unknown, no default may trigger a
    # geometric veto). A geometry whose facet reads one of them is rejected
    # when the adapter is built: no veto, flag or reason can then rest on a
    # placeholder. The reference geometry reads none of them.
    _NEVER_MEASURED_AXES = frozenset({
        "cooperative_control_score", "trajectory_risk_score", "manipulation_risk", "drift",
    })

    def _reject_facets_on_never_measured_axes(self) -> None:
        geometry = getattr(getattr(self._kernel, "policy", None), "geometry", None)
        facets = getattr(geometry, "facets", None) or ()
        offending = []
        for facet in facets:
            axes = sorted(set(getattr(facet, "normal", {}) or {}) & self._NEVER_MEASURED_AXES)
            if axes:
                offending.append(
                    f"{getattr(facet, 'facet_id', '?')} ({getattr(facet, 'severity', '?')}) "
                    f"reads {', '.join(axes)}")
        if offending:
            raise ValueError(
                "Geometry rejected: a facet reads an axis this interface never measures, "
                "so it would be evaluated on a placeholder, never on a measure: "
                + "; ".join(offending)
                + ". Measure the axis through the interface, or remove the facet."
            )

    def _build_coordinates(
        self,
        pressure: float,
        velocity: float,
        capacity: float,
        option_space: float | None,
        authority: float,
        context_fit: float,
        evidence_quality: float,
        irreversibility_materiality: float,
    ) -> dict[str, float]:
        """Build constraint geometry coordinates for TenirKernel.

        The reference geometry (tenir-2.0-reference) has facets that reference:
        authority, context_fit, irreversible_pathway_materiality,
        evidence_quality, pressure, velocity, capacity.

        All axes in ALLOWED_AXES are populated. Missing axes would cause
        a hard "missing-geometry-point" violation.

        Per peer review P2: coordinates that are not measured must not be
        filled with favorable defaults. Missing mandatory data (authority,
        context_fit, evidence_quality) is signaled via gate reasons so the
        composition layer can HOLD rather than silently proceeding with a
        favorable score. Optional axes without measurement input are set to
        neutral (not favorable) values and flagged as unmeasured.
        """
        # Track which axes are unmeasured so the caller can add gate reasons
        self._unmeasured_axes: list[str] = []

        # Mandatory epistemic inputs must never acquire favorable defaults.
        # A neutral placeholder keeps the geometry structurally complete. Since
        # 5.7.7 (candidate) adjudicate() holds an absent mandatory input before
        # building coordinates (rule A); the placeholder stays for direct calls.
        mandatory = {
            "authority": authority,
            "context_fit": context_fit,
            "evidence_quality": evidence_quality,
        }
        coords: dict[str, float] = {
            "pressure": pressure,
            "velocity": velocity,
            "capacity": capacity,
            "irreversible_pathway_materiality": irreversibility_materiality,
        }
        for axis, value in mandatory.items():
            if value is None:
                coords[axis] = 0.5
                self._unmeasured_axes.append(axis)
            else:
                coords[axis] = value

        # option_space: if not provided, do NOT default to 1.0 (favorable).
        # Use a neutral 0.5 and flag as unmeasured. The caller will add a
        # soft gate reason for missing option_space.
        if option_space is not None:
            coords["option_space"] = option_space
        else:
            coords["option_space"] = 0.5  # neutral, not favorable
            self._unmeasured_axes.append("option_space")

        # Axes that have no measurement input: set to NEUTRAL (not favorable)
        # and flag as unmeasured. These must not be 1.0 or 0.0 which would
        # make the geometry treat missing data as ideal conditions.
        coords["cooperative_control_score"] = 0.5  # neutral
        self._unmeasured_axes.append("cooperative_control_score")

        coords["trajectory_risk_score"] = 0.5  # neutral
        self._unmeasured_axes.append("trajectory_risk_score")

        coords["manipulation_risk"] = 0.5  # neutral
        self._unmeasured_axes.append("manipulation_risk")

        coords["drift"] = 0.5  # neutral
        self._unmeasured_axes.append("drift")

        return coords

    def adjudicate(
        self,
        pressure: float,
        velocity: float,
        capacity: float,
        option_space: float | None = None,
        authority: float | None = None,
        context_fit: float | None = None,
        evidence_quality: float | None = None,
        irreversibility_materiality: float | None = None,
    ) -> dict[str, Any]:
        """Delegate to TenirKernel with injected gate reasons.

        Returns a dict with the same shape as the prototype's ScoringKernel:
            kernel, s_score, decision, rationale, authority, evidence_quality
        plus TenirKernel's constraint_geometry.
        """
        hard_reasons: list[str] = []
        soft_reasons: list[str] = []

        named = {"pressure": pressure, "velocity": velocity, "capacity": capacity,
                 "option_space": option_space, "authority": authority,
                 "context_fit": context_fit, "evidence_quality": evidence_quality,
                 "irreversibility_materiality": irreversibility_materiality}
        # Declared domain (candidate 5.7.7, author's decision of 23 November
        # 2026, P8 option A; draft RFC-2 v0.2 §1.4, not ratified): a finite
        # value outside the domain this kernel declares for its measures is
        # not a measure of that axis. It is rejected explicitly, before any
        # evaluation, as INVALID_ARGUMENT, never closed by a decision. Between
        # 19 and 23 November it was read as an unknown measure (HOLD), which
        # closed the request by a disposition instead of refusing it.
        malformed = self.malformed_measures(named)
        if malformed:
            raise InvalidArgumentError(self._malformed_reason(named, malformed),
                                       field=",".join(malformed))
        out_of_domain = self.out_of_domain(named)
        if out_of_domain:
            raise InvalidArgumentError(
                self._out_of_domain_reason(named, out_of_domain),
                field=",".join(out_of_domain),
            )
        # Decision 5: finiteness guard at the kernel's entry. A non-finite
        # measure (NaN, ±inf) is insufficient evidence, never PASS: it must not
        # reach a comparison, where NaN compares false and admits. It is
        # reported as unmeasured, and every conclusive veto the other inputs
        # still support is evaluated and governs (RFC-0 §4). NaN is the
        # caller's own way of saying "unknown" (an unmeasured axis has no
        # value, in the domain or out of it); it is not a value. Every value
        # here is None or a number: the rest was refused above.
        def _finite(v):
            return v is None or math.isfinite(v)
        non_finite = [n for n, v in named.items() if not _finite(v)]
        absent = [n for n, v in named.items() if v is None]
        # Rule A (candidate 5.7.7, author's decision of 23 November 2026,
        # pending ratification of the amended RFC-2 v0.2): a measure that S or
        # a geometry facet reads is unknown when it is absent (None, JSON null)
        # exactly as when it is invalid. The absence of a measure is proof
        # neither of a violation nor of admissibility: HOLD, unless a
        # conclusive veto governs, one the valid measures establish whatever
        # value the unknown ones would take. No verdict, and no reason, is
        # drawn from a placeholder (draft RFC-2 v0.2 §3). 5.7.6 hard-gated an
        # absent authority, context_fit or evidence_quality, and vetoed on the
        # authority placeholder (0.5) when authority was invalid.
        unknown = set(non_finite) | set(absent)
        if unknown:
            # option_space included (R7, 2026-11-23): an unknown option space
            # used to reach the main path on a placeholder (0.5) that a custom
            # facet could read. It takes the insufficient-evidence path, where
            # a facet is conclusive only whatever value the unknown axis would
            # take in [0, 1].
            return self._insufficient_evidence(named, non_finite, absent)
        # Every input is a valid measure past this point. The only placeholders
        # the kernel sees are the never-measured axes, which no facet reads
        # (rejected at construction).

        # Compute the extra signals the prototype's ScoringKernel checked
        # but TenirKernel does not natively evaluate. These become gate reasons
        # injected into TenirKernel.adjudicate().
        # Every measure S and the geometry facets read is valid here (an absent
        # or invalid one returned above): no verdict is drawn from a placeholder.
        s_preview = capacity / (pressure * velocity + 1e-6)
        if s_preview <= self._tau_floor:
            hard_reasons.append(
                f"TAU BREACH: S={s_preview:.4f} ≤ τ={self._tau_floor}"
            )

        # Note: irreversibility-authority boundary is already checked by
        # the reference geometry's "irreversibility-authority-boundary" facet.
        # We don't duplicate it here — the geometry handles it.

        # Option space checks (not in the reference geometry facets)
        if option_space is not None:
            if option_space <= self._option_space_block_floor:
                hard_reasons.append(
                    f"option_space={option_space:.3f} ≤ block floor"
                )
            elif option_space <= self._option_space_alert_floor:
                soft_reasons.append(
                    f"option_space={option_space:.3f} ≤ alert floor"
                )

        # Build geometry coordinates from adapter inputs
        coordinates = self._build_coordinates(
            pressure=pressure,
            velocity=velocity,
            capacity=capacity,
            option_space=option_space,
            authority=authority,
            context_fit=context_fit,
            evidence_quality=evidence_quality,
            irreversibility_materiality=irreversibility_materiality,
        )

        # Add soft gate reasons for unmeasured axes so the composition
        # layer is aware that these coordinates are not based on real
        # measurements. This prevents a favorable score from being
        # interpreted as evidence of safety on unmeasured dimensions.
        unmeasured = getattr(self, "_unmeasured_axes", [])
        for axis in unmeasured:
            soft_reasons.append(f"unmeasured axis: {axis} (neutral default, not favorable)")

        # A measured value of zero is not "missing": it is real evidence for
        # the geometry to evaluate (authority=0.0 meets the authority floor).
        # An absent mandatory input no longer reaches this point (rule A).

        # Delegate core scoring to TenirKernel
        tk_result = self._kernel.adjudicate(
            pressure=pressure,
            velocity=velocity,
            capacity=capacity,
            coordinates=coordinates,
            mode="ENFORCE",
            hard_gate_reasons=tuple(hard_reasons),
            soft_gate_reasons=tuple(soft_reasons),
        )

        # Map TenirKernel decision to composition Decision enum
        tk_decision = tk_result["decision"]
        # TenirKernel returns PASS/FLAG/HARD_VETO — same strings as Decision
        # but we normalize through the enum for safety
        if tk_decision == "HARD_VETO":
            decision = Decision.HARD_VETO.value
        elif tk_decision == "FLAG":
            decision = Decision.FLAG.value
        elif tk_decision == "PASS":
            decision = Decision.PASS.value
        else:
            decision = Decision.HOLD.value

        rationale = tk_result.get("rationale", "")
        # An unknown input (absent or invalid, option_space included) never
        # reaches this point: it returned from _insufficient_evidence above.

        return {
            "kernel": "scoring",
            "s_score": tk_result.get("s_score"),
            "decision": decision,
            "rationale": rationale,
            # True only on the insufficient-evidence path (rule A, 5.7.7
            # candidate), where the HOLD waits for a measure the caller did
            # not supply. The governor reserves no budget for such a HOLD.
            "insufficient_evidence": False,
            "authority": authority,
            "context_fit": context_fit,
            "evidence_quality": evidence_quality,
            "constraint_geometry": tk_result.get("constraint_geometry"),
            "operating_mode": tk_result.get("operating_mode"),
            # Axes that carry no measurement: a neutral placeholder keeps the
            # geometry complete, but they are declared, not measured (decision 2).
            "declared_unmeasured": list(unmeasured),
            "unmeasured_axes": list(non_finite),
        }

    # Adapter input name -> geometry axis name, where they differ.
    _GEOMETRY_AXIS = {"irreversibility_materiality": "irreversible_pathway_materiality"}

    # The measures S = C / (P·V + ε) and the reference geometry's facets read.
    # One of them unknown (absent or invalid) means insufficient evidence (rule A).
    _SCORED_MEASURES = ("pressure", "velocity", "capacity", "authority", "context_fit",
                        "evidence_quality", "irreversibility_materiality")

    def _conclusive_geometry_vetoes(self, measured):
        """Hard facets of the kernel's geometry violated whatever value the
        axes without a valid measure take. Coordinates are normalized to
        [0, 1] and a facet is the half-space ``normal · point <= bound``, so
        the most favorable value of an unknown axis contributes min(0, w).
        A facet whose every axis is measured is the common case. Such a
        violation does not depend on an unknown value: it is conclusive and
        must govern (RFC-0 §4). No facet is evaluated on a placeholder."""
        point = {self._GEOMETRY_AXIS.get(n, n): float(v) for n, v in measured.items()}
        policy = getattr(getattr(self._kernel, "policy", None), "geometry", None)
        reasons = []
        for facet in getattr(policy, "facets", ()):
            if facet.severity != "hard":
                continue
            least = sum(w * point[a] if a in point else min(0.0, w)
                        for a, w in facet.normal.items())
            if least > facet.bound:
                reasons.append(facet.rationale)
        return reasons

    def _insufficient_evidence(self, named, non_finite, absent):
        """A measure S or a geometry facet reads is unknown (rule A, candidate
        5.7.7). HOLD for insufficient evidence, unless a conclusive veto
        governs, that is one the valid measures establish whatever value the
        unknown ones would take (RFC-0 §4, draft RFC-2 v0.2 §2):

        - P, V and C valid: S is computed from them, and a TAU BREACH or the
          kernel's hard-veto floor on that S vetoes;
        - P or V unknown, C measured at zero: S = 0 whatever P and V are;
        - a hard geometry facet violated whatever its unknown axes are,
          such as authority or context_fit measured below its floor;
        - option space measured at its block floor.

        An absent capacity is never read as zero, an absent authority never as
        its placeholder: no veto and no reason rest on a value nobody measured
        (draft RFC-2 v0.2 §3). Every conclusive veto of the membrane governs
        in the composition, whatever this result is."""
        unknown = set(non_finite) | set(absent)
        measured = {n: v for n, v in named.items() if n not in unknown}
        hard: list[str] = []
        s_score = None
        if unknown.isdisjoint(("pressure", "velocity", "capacity")):
            p, v, c = (float(measured[n]) for n in ("pressure", "velocity", "capacity"))
            s_preview = c / (p * v + 1e-6)
            if s_preview <= self._tau_floor:
                hard.append(f"TAU BREACH: S={s_preview:.4f} ≤ τ={self._tau_floor}")
            policy = getattr(self._kernel, "policy", None)
            epsilon = getattr(policy, "epsilon", 1e-6)
            score = c / (p * v + epsilon)
            s_score = round(score, 8)
            floor = getattr(policy, "hard_veto_below", None)
            if floor is not None and score <= floor:
                hard.append(f"S={score:.6f} is at or below hard-veto floor {floor}")
        elif "capacity" in measured and measured["capacity"] <= 0:
            hard.append(f"TAU BREACH: capacity={measured['capacity']} gives S=0 ≤ τ={self._tau_floor} "
                        "whatever pressure and velocity are")
        hard.extend(self._conclusive_geometry_vetoes(measured))
        option_space = measured.get("option_space")
        if option_space is not None and option_space <= self._option_space_block_floor:
            hard.append(f"option_space={option_space:.3f} ≤ block floor")
        named_unknown = []
        if non_finite:
            named_unknown.append(
                f"invalid measure(s) (non-finite) {', '.join(non_finite)}")
        if absent:
            named_unknown.append(f"absent measure(s) {', '.join(absent)}")
        detail = "; ".join(named_unknown)
        if hard:
            reason = (f"Conclusive whatever the unknown measure(s) are: {detail}"
                      + ("; S not computed." if s_score is None else "."))
        elif s_score is None:
            reason = f"INSUFFICIENT_EVIDENCE: {detail}; S not computed."
        else:
            reason = (f"INSUFFICIENT_EVIDENCE: {detail}; held, not admitted "
                      "(re-adjudicate with the missing measure).")
        decision = Decision.HARD_VETO.value if hard else Decision.HOLD.value
        return {
            "kernel": "scoring",
            "s_score": s_score,
            "decision": decision,
            "rationale": " ".join(hard + [reason]),
            "insufficient_evidence": not hard,
            # An unknown input is reported as null (decision 6), never as a placeholder.
            "authority": measured.get("authority"),
            "context_fit": measured.get("context_fit"),
            "evidence_quality": measured.get("evidence_quality"),
            # Not evaluated: the geometry would have read placeholders.
            "constraint_geometry": None,
            "operating_mode": None,
            # The unknown inputs, then the axes this interface never measures
            # (declared unmeasured on every result, as in the main path).
            "declared_unmeasured": [n for n in named if n in unknown]
                                   + sorted(self._NEVER_MEASURED_AXES),
            "unmeasured_axes": list(non_finite),
        }


# ── Dual Kernel Governor ────────────────────────────────────────────────

class DualKernelGovernor:
    """The composed two-kernel governor.

    Bridges the continuous risk monitor (TenirKernel via ScoringKernelAdapter)
    with the discrete invariant enforcer (TransitionMembrane).

    Per review 3:
    - §1: Idempotency key is durably claimed BEFORE dispatch via the
      DurableOperationRegistry. State machine:
        CREATED → RESERVED → DISPATCHING → CONFIRMED | FAILED | UNKNOWN
    - §2: Budget coordination abstracted behind BudgetCoordinator protocol.
    - §3: Core safety properties are testable.

    Fail-closed: kernel exceptions produce HOLD, never PASS.

    Per KB3: posture (NORMAL/CAUTION/HOLDING-FIRST) and execution mode
    (shadow-passive/shadow-critical/enforce) are separate output fields.
    The schema conformance adapter maps Decision → posture.
    """

    def __init__(
        self,
        scoring_kernel,
        membrane_kernel,
        ledger: HashChainedLedger | None = None,
        policy: CompositionPolicy | None = None,
        registry: DurableOperationRegistry | None = None,
        budget_coordinator: BudgetCoordinator | None = None,
        provider: Any = None,
        assessor: PostExecutionAssessor | None = None,
        consensus_backend: Any = None,
        commit_authority: Any = None,
        require_commit_authorization: bool = False,
        attestation_verifier: Any = None,
    ):
        # scoring_kernel is a ScoringKernelAdapter wrapping TenirKernel,
        # or any object with a compatible adjudicate() interface.
        self.scoring = scoring_kernel
        self.membrane = membrane_kernel
        self.ledger = ledger or HashChainedLedger()
        self.policy = policy or CompositionPolicy()
        self.policy.validate()
        # Verifies caller-supplied proofs of non-execution against the keys the
        # deployment declares (operations.attestation). None (default): no
        # external attestation is accepted; after dispatch only the runtime's
        # own provider channel can establish non-execution (fail-closed).
        if attestation_verifier is not None and not callable(getattr(attestation_verifier, "verify", None)):
            raise ValueError("attestation_verifier must provide verify(attestation) -> str | None")
        self.attestation_verifier = attestation_verifier
        self.registry = registry or DurableOperationRegistry()
        self.membrane.state.bind_quarantine_registry(self.registry)
        self.budget_coordinator = budget_coordinator
        # Optional durable reservation backend. When supplied, this becomes
        # the budget source of truth for this governor instance.
        self.consensus_backend = consensus_backend
        self.provider = provider
        self.commit_authority = commit_authority
        self.require_commit_authorization = bool(require_commit_authorization)
        if self.require_commit_authorization and self.commit_authority is None:
            raise ValueError(
                "require_commit_authorization=True requires a Profile S commit authority"
            )
        self._worker_id = str(uuid4())
        # Process-local guard for the window where provider.execute() is active.
        # Durable DISPATCHING recovery remains available after process restart.
        self._active_dispatches: set[str] = set()
        self._active_dispatches_lock = threading.RLock()
        # Ledger-integrity incident (project owner's review of 2026-11-23, 17:37
        # UTC): the first dispatch refused on a ledger that does not verify
        # writes one api/HARD_VETO entry and opens the incident; further
        # refusals while it stays open write nothing more (the chain is
        # already known not to verify, and each entry would lengthen it).
        # verify_ledger() closes the incident when the chain verifies again.
        self._ledger_integrity_incident: dict[str, Any] | None = None
        # Serialize terminal budget settlement paths within this governor instance.
        # The registry transition remains the durable terminal-state authority.
        self._settlement_lock = threading.RLock()
        self.hold_manager = HoldManager(self.registry)
        # Post-execution assessor (optional). When set, the governor can
        # run τ⁻¹ and EVP assessments after terminal states. The assessor
        # does NOT feed back into admissibility — it is a pure measurement.
        self.assessor = assessor

    def adjudicate(
        self,
        passage,
        pressure: float | None = None,
        velocity: float | None = None,
        capacity: float | None = None,
        option_space: float | None = None,
        authority: float | None = None,
        context_fit: float | None = None,
        evidence_quality: float | None = None,
        irreversibility_materiality: float | None = None,
        delegation=None,
        resource_id: str | None = None,
        amount: float = 0.0,
        trajectory_id: str | None = None,
        payload: dict[str, Any] | None = None,
        payload_digest: str | None = None,
        operation_id: str | None = None,
        institutional_scope: str | None = None,
    ) -> dict[str, Any]:
        """Run both kernels, compose, ledger, and durably register.

        Both kernels ALWAYS run. Fail-closed: if either kernel throws an
        exception, the result is HOLD, never PASS.

        Per review 3 §1: the idempotency key is durably registered BEFORE
        any kernel evaluation, and the state machine tracks the operation
        through dispatch.
        """
        idem_key = passage.idempotency_key or f"op-{passage.passage_id}"

        # R8 (author's decision of 2026-11-23): a structurally malformed request
        # is refused at the API boundary as INVALID_ARGUMENT, never as a
        # governance decision. Nothing is registered, reserved, held or
        # vetoed; the ledger keeps the refusal under kernel "api" so the trace
        # cannot be read as a HARD_VETO on the action.
        try:
            authorized_payload_hash = canonical_payload_hash(payload)
        except (TypeError, ValueError) as exc:
            self._refuse_malformed(
                f"Invalid action payload; authorization refused: {exc}.",
                field="payload", passage=passage, idem_key=idem_key,
            )

        # Validate the economic parameter before it can bypass the budget
        # gate (NaN compares false to > 0, and negative values also skip it).
        try:
            invalid_amount = isinstance(amount, bool) or not math.isfinite(float(amount)) or float(amount) < 0
            normalized_amount = float(amount)
            if resource_id is not None and normalized_amount <= 0:
                invalid_amount = True
        except (TypeError, ValueError, OverflowError):
            invalid_amount = True
            normalized_amount = 0.0
        if invalid_amount:
            self._refuse_malformed(
                "Invalid execution amount: must be finite and non-negative, and strictly "
                "positive when a resource is specified.",
                field="amount", passage=passage, idem_key=idem_key,
            )
        amount = normalized_amount

        # P8 (author's decision of 2026-11-23, option A): a measure outside the
        # kernel's declared domain is refused here, before registration, as
        # INVALID_ARGUMENT (field = the measure). Nothing is registered or
        # held; the ledger keeps the refusal under kernel "api".
        self._refuse_out_of_domain(
            dict(pressure=pressure, velocity=velocity, capacity=capacity,
                 option_space=option_space, authority=authority,
                 context_fit=context_fit, evidence_quality=evidence_quality,
                 irreversibility_materiality=irreversibility_materiality),
            passage=passage, idem_key=idem_key,
        )

        # Profile S binds dispatch permission to the complete immutable
        # operation.  Existing deployments remain unchanged because the gate
        # is disabled by default.
        if self.require_commit_authorization:
            if (
                not isinstance(payload_digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", payload_digest)
                or (payload is not None and authorized_payload_hash != payload_digest)
            ):
                self._refuse_malformed(
                    "Profile S commit authorization requires a lowercase SHA-256 payload digest.",
                    field="payload_digest", passage=passage, idem_key=idem_key,
                )
            # Profile S's bounded canonical payload digest is the stronger
            # binding. Persist it as the legacy payload hash as well so both
            # gates authorize the same bytes during the integration period.
            authorized_payload_hash = payload_digest
            operation_id = operation_id or idem_key
            commit_id_pattern = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
            if (
                not isinstance(operation_id, str)
                or not re.fullmatch(commit_id_pattern, operation_id)
                or not isinstance(institutional_scope, str)
                or not re.fullmatch(commit_id_pattern, institutional_scope)
            ):
                self._refuse_malformed(
                    "Profile S commit authorization requires valid operation and institutional identities.",
                    field="operation_id/institutional_scope", passage=passage, idem_key=idem_key,
                )

        # ── DURABLE CLAIM: register the operation BEFORE evaluation ──────
        record = OperationRecord(
            idempotency_key=idem_key,
            passage_id=passage.passage_id,
            agent_id=passage.agent_id,
            action_type=passage.action_type,
            operation_id=operation_id or "",
            institutional_scope=institutional_scope or "",
            payload_digest=payload_digest or "",
            amount=amount,
            resource_id=resource_id or "",
            payload_hash=authorized_payload_hash,
            commit_gate_required=self.require_commit_authorization,
            # The mandate as admitted, re-validated before dispatch (5.7.6).
            delegating_agent_id=getattr(delegation, "delegating_agent_id", "") or "" if delegation is not None else "",
            delegatee_agent_id=getattr(delegation, "delegatee_agent_id", "") or "" if delegation is not None else "",
            agent_registered_at_admission=passage.agent_id in self.membrane.state.agents,
            policy_version=str(self.policy.version),
        )
        if not self.registry.register(record):
            existing = self.registry.get(idem_key)
            return {
                "passage_id": passage.passage_id,
                "idempotency_key": idem_key,
                "composed_decision": Decision.HARD_VETO.value,
                "composed_rationale": (
                    f"Duplicate operation blocked — idempotency key '{idem_key}' "
                    f"is already claimed in state {existing.state.value}. "
                    f"Use resume_existing() for recovery."
                ),
                "execution_status": existing.state.value,
                "operation_state": existing.state.value,
                "ledger_entries": self.ledger.entry_count,
                "ledger_valid": self.ledger.verify_new_entries(),
            }

        # 1. Scoring kernel — fail-closed
        scoring_result = None
        scoring_error = ""
        try:
            scoring_result = self.scoring.adjudicate(
                pressure=pressure, velocity=velocity, capacity=capacity,
                option_space=option_space, authority=authority,
                context_fit=context_fit, evidence_quality=evidence_quality,
                irreversibility_materiality=irreversibility_materiality,
            )
        except Exception as e:
            scoring_result = None
            scoring_error = str(e)

        # 2. Membrane kernel — fail-closed
        membrane_result = None
        membrane_error = ""
        try:
            held_record = self.registry.get(passage.idempotency_key)
            already_reserved_amount = (
                amount if held_record is not None and held_record.budget_reserved else 0.0
            )
            membrane_result = self.membrane.adjudicate(
                passage=passage, delegation=delegation,
                resource_id=resource_id, amount=amount,
                trajectory_id=trajectory_id,
                existing_reservation_amount=already_reserved_amount,
            )
        except Exception as e:
            membrane_result = None
            membrane_error = str(e)

        # 3. Compute budget utilization
        budget_utilization = None
        if resource_id and amount > 0:
            resource = self.membrane.state.resources.get(resource_id)
            if resource and resource.capacity > 0:
                budget_utilization = self._prospective_utilization(idem_key, resource, amount)

        # 4. Compose using the decision lattice (fail-closed)
        budget_in_scope = resource_id is not None and amount > 0
        composed_decision, composed_rationale = compose(
            scoring_result["decision"] if scoring_result else None,
            membrane_result["decision"] if membrane_result else None,
            scoring_ces=scoring_result.get("ces_state") if scoring_result else None,
            membrane_checks=membrane_result.get("checks") if membrane_result else None,
            budget_utilization=budget_utilization,
            budget_in_scope=budget_in_scope,
            policy=self.policy,
        )
        composed_decision, composed_rationale = self._apply_flag_condition_rule(
            composed_decision, composed_rationale,
        )

        # 5. Ledger
        self.ledger.append(
            kernel="scoring",
            decision=scoring_result["decision"] if scoring_result else "HOLD",
            rationale=scoring_result["rationale"] if scoring_result else f"Scoring kernel error: {scoring_error}",
            passage_id=passage.passage_id,
            idempotency_key=idem_key,
        )
        self.ledger.append(
            kernel="membrane",
            decision=membrane_result["decision"] if membrane_result else "HOLD",
            rationale=membrane_result["rationale"] if membrane_result else f"Membrane kernel error: {membrane_error}",
            passage_id=passage.passage_id,
            idempotency_key=idem_key,
        )
        self.ledger.append(
            kernel="composed",
            decision=composed_decision,
            rationale=composed_rationale,
            passage_id=passage.passage_id,
            idempotency_key=idem_key,
            scoring_result=scoring_result or {},
            membrane_result=membrane_result or {},
            composed_decision=composed_decision,
        )

        # A network-backed reservation can block indefinitely. Perform that
        # external call before taking the shared quarantine gate, then make
        # the final admission decision and state claim under the gate. If an
        # emergency lock wins while reserve() is in flight, compensate the
        # reservation only after leaving the gate.
        external_reservation_attempted = False
        retains_operation = composed_decision in (
            Decision.PASS.value, Decision.FLAG.value, Decision.HOLD.value,
        )
        # Rule A (5.7.7 candidate): a HOLD for insufficient evidence reserves
        # nothing. Only the caller can lift it, by supplying the measure it
        # withheld, so a reservation would let any caller lock a shared budget
        # by omitting a measure. The reservation is taken at re-adjudication,
        # under I8 at that moment. Every other retained operation (admission,
        # or a HOLD on complete evidence: escalation, FLAG without condition,
        # fail-closed kernel error) reserves as before.
        evidence_hold = (
            composed_decision == Decision.HOLD.value
            and bool((scoring_result or {}).get("insufficient_evidence"))
        )
        if (retains_operation and budget_in_scope and not evidence_hold
                and self.consensus_backend is not None):
            # Avoid creating a reservation if quarantine is already active.
            # This short read is intentionally separate from reserve(): the
            # lock may activate immediately afterward, in which case the
            # final claim below vetoes and compensates the late reservation.
            with self.membrane.state._quarantine_lock:
                with self.registry.quarantine_guard():
                    already_quarantined = self._quarantine_blocks()
            if already_quarantined:
                composed_decision = Decision.HARD_VETO.value
                composed_rationale = (
                    "QUARANTINE_LOCK is active; admission and reservation blocked."
                )
                self.ledger.append(
                    kernel="composed",
                    decision=composed_decision,
                    rationale=composed_rationale,
                    passage_id=passage.passage_id,
                    idempotency_key=idem_key,
                )
            else:
                # Recovery may discover this CREATED operation while reserve
                # is still running. Serialize settlement for this key without
                # holding the shared emergency quarantine gate.
                external_reservation_attempted = True
                try:
                    with self.registry.settlement_lock(idem_key, require_persistent=True):
                        current = self.registry.get(idem_key)
                        reserved = bool(
                            current is not None
                            and current.state == OperationState.CREATED
                            and self._reserve_budget(idem_key, resource_id, amount)
                        )
                except Exception:
                    reserved = False
                if not reserved:
                    composed_decision = Decision.HARD_VETO.value
                    composed_rationale = (
                        "Budget reservation failed at commit point (concurrent consumption) "
                        "— fail-closed HARD_VETO."
                    )
                    self.ledger.append(
                        kernel="composed",
                        decision=composed_decision,
                        rationale=composed_rationale,
                        passage_id=passage.passage_id,
                        idempotency_key=idem_key,
                    )

        # The cross-process gate linearizes quarantine activation against the
        # final admission/HOLD claim. No external budget call runs inside it.
        with self.membrane.state._quarantine_lock:
            with self.registry.quarantine_guard():
                # 6a. Claim final admission and any local budget reservation.
                # External reservations were attempted before taking this gate;
                # quarantine can veto admission even if the backend reserved.
                if composed_decision in (
                    Decision.PASS.value, Decision.FLAG.value, Decision.HOLD.value,
                ):
                    # Linearize final admission against QUARANTINE_LOCK even when no
                    # budget is in scope. If a lock activates after I6's first check,
                    # no outcome that retains the operation may pass this point.
                    with self.membrane.state._quarantine_lock:
                        if self._quarantine_blocks():
                            composed_decision = Decision.HARD_VETO.value
                            composed_rationale = (
                                "QUARANTINE_LOCK activated during adjudication; admission and reservation blocked."
                            )
                    if (budget_in_scope and self.consensus_backend is None
                            and composed_decision != Decision.HARD_VETO.value
                            and not evidence_hold
                            and not self._reserve_budget(
                                idem_key, resource_id, amount,
                    )):
                        composed_decision = Decision.HARD_VETO.value
                        composed_rationale = (
                            "Budget reservation failed at commit point (concurrent consumption) "
                            "— fail-closed HARD_VETO."
                        )
                        self.ledger.append(
                            kernel="composed",
                            decision=composed_decision,
                            rationale=composed_rationale,
                            passage_id=passage.passage_id,
                            idempotency_key=idem_key,
                        )
                    if composed_decision == Decision.HARD_VETO.value:
                        self.ledger.append(
                            kernel="composed",
                            decision=composed_decision,
                            rationale=composed_rationale,
                            passage_id=passage.passage_id,
                            idempotency_key=idem_key,
                        )

                # 6. State machine transitions
                if composed_decision == Decision.PASS.value or composed_decision == Decision.FLAG.value:
                    success, _prev = self.registry.transition_to(
                        idem_key, OperationState.RESERVED,
                        admission_terms=self._admission_terms(composed_decision),
                    )
                    if not success:
                        # A failed RESERVED claim must not leak a backend
                        # reservation acquired before taking the quarantine gate.
                        # Fail closed; compensation runs below after the gate.
                        composed_decision = Decision.HARD_VETO.value
                        composed_rationale = (
                            "RESERVED transition failed; fail-closed HARD_VETO."
                        )
                        if not external_reservation_attempted:
                            self.registry.transition_to(idem_key, OperationState.FAILED)
                        self.ledger.append(
                            kernel="composed",
                            decision=composed_decision,
                            rationale=composed_rationale,
                            passage_id=passage.passage_id,
                            idempotency_key=idem_key,
                        )
                elif composed_decision == Decision.HARD_VETO.value:
                    # Any external reservation rejected by the final check is
                    # compensated below, after leaving the shared gate.
                    if not external_reservation_attempted:
                        self.registry.transition_to(idem_key, OperationState.FAILED)
                elif composed_decision == Decision.HOLD.value:
                    # Enter through HoldManager so the lifecycle record is initialized.
                    # Do not invent a τ_K here: this generic Governor has no
                    # organization-owned SLA. The bound reaches the HOLD only
                    # when the versioned policy declares it (RN-001 §2a).
                    if self.policy.hold_tau_k_seconds is not None:
                        hold_entry = self.hold_manager.enter_hold(
                            idem_key,
                            tau_k_seconds=float(self.policy.hold_tau_k_seconds),
                            tau_k_source=self.policy.hold_tau_k_source,
                        )
                    else:
                        hold_entry = self.hold_manager.enter_hold(idem_key)
                    if not hold_entry["entered"]:
                        composed_rationale = (
                            "HOLD decision could not initialize the lifecycle record: "
                            + hold_entry["reason"]
                        )
                        self.ledger.append(
                            kernel="hold_lifecycle",
                            decision=Decision.HOLD.value,
                            rationale=composed_rationale,
                            passage_id=passage.passage_id,
                            idempotency_key=idem_key,
                        )
                    elif evidence_hold and budget_in_scope:
                        self.ledger.append(
                            kernel="hold_lifecycle",
                            decision=Decision.HOLD.value,
                            rationale=(
                                "Budget reservation deferred: nothing is reserved while the "
                                "evidence is insufficient; the amount is reserved at "
                                "re-adjudication, under I8 at that moment (rule A)."
                            ),
                            passage_id=passage.passage_id,
                            idempotency_key=idem_key,
                        )

                final_record = self.registry.get(idem_key)
                response = {
                    "passage_id": passage.passage_id,
                    "idempotency_key": idem_key,
                    "scoring_decision": scoring_result["decision"] if scoring_result else None,
                    "scoring_rationale": scoring_result["rationale"] if scoring_result else None,
                    "scoring_ces": scoring_result.get("ces_state") if scoring_result else None,
                    "scoring_s": scoring_result.get("s_score") if scoring_result else None,
                    "membrane_decision": membrane_result["decision"] if membrane_result else None,
                    "membrane_rationale": membrane_result["rationale"] if membrane_result else None,
                    "membrane_checks": membrane_result.get("checks", []) if membrane_result else [],
                    "composed_decision": composed_decision,
                    "composed_rationale": composed_rationale,
                    "execution_status": membrane_result.get("execution_status", "pending") if membrane_result else "unknown",
                    "operation_state": final_record.state.value if final_record else "unknown",
                    # False for a HOLD on insufficient evidence (rule A): the
                    # reservation is taken at re-adjudication.
                    "budget_reserved": bool(final_record and final_record.budget_reserved),
                    "ledger_entries": self.ledger.entry_count,
                    "ledger_valid": self.ledger.verify_new_entries(),
                    **self._flag_fields(composed_decision, scoring_result),
                }

        if external_reservation_attempted and composed_decision == Decision.HARD_VETO.value:
            # Keep CREATED until compensation and durable ownership updates
            # complete. fail() also recovers reservations by key if reserve
            # persisted before ownership flags, and accepts ALREADY_RELEASED.
            compensated = self.fail(idem_key)
            if not compensated:
                composed_rationale = (
                    composed_rationale + " External reservation release is uncertain; "
                    "durable ownership is retained for reconciliation."
                )
                response["composed_rationale"] = composed_rationale
            current = self.registry.get(idem_key)
            if current is not None:
                response["operation_state"] = current.state.value
                response["budget_reserved"] = bool(current.budget_reserved)
        return response

    def _refuse_out_of_domain(self, named: dict[str, Any], *, passage, idem_key: str) -> None:
        """Refuse a measure that is not a number, or outside the scoring kernel's declared domain (P8).

        The domain is the kernel's own declaration (``declared_domain()``);
        the governor prescribes none. A scoring adapter without a declared
        domain (no ``out_of_domain``) is not checked here: its own entry
        decides. The refusal is INVALID_ARGUMENT, never a decision.
        """
        malformed = getattr(self.scoring, "malformed_measures", None)
        if malformed is not None:
            names = malformed(named)
            if names:
                self._refuse_malformed(self.scoring._malformed_reason(named, names),
                                       field=",".join(names), passage=passage, idem_key=idem_key)
        probe = getattr(self.scoring, "out_of_domain", None)
        if probe is None:
            return
        names = probe(named)
        if names:
            self._refuse_malformed(
                self.scoring._out_of_domain_reason(named, names),
                field=",".join(names), passage=passage, idem_key=idem_key,
            )

    def verify_ledger(self) -> bool:
        """Full verification of the hash chain, on demand: every entry is
        re-hashed and every link checked (O(N)). ``ledger_valid`` in a decision
        result is the incremental check of the links added since the last
        verification; call this one for an audit."""
        ok = self.ledger.verify_chain()
        if ok and self._ledger_integrity_incident is not None:
            self._ledger_integrity_incident = None
        return ok

    @property
    def ledger_integrity_incident(self) -> dict[str, Any] | None:
        """The open ledger-integrity incident (first refusal, count), or None."""
        return dict(self._ledger_integrity_incident) if self._ledger_integrity_incident else None

    def _refuse_malformed(self, reason: str, *, field: str, passage, idem_key: str) -> None:
        """Refuse a structurally malformed request at the API boundary (R8).

        Raises InvalidArgumentError (code INVALID_ARGUMENT, a ValueError).
        Nothing is registered, reserved, held or vetoed. The refusal is kept
        in the ledger under kernel "api", never as a composed decision, so the
        trace cannot be read as a HARD_VETO on the action. 5.7.6 returned
        these refusals as composed_decision "HARD_VETO" with operation_state
        "not_registered".
        """
        passage_id = getattr(passage, "passage_id", None)
        self.ledger.append(
            kernel="api", decision=InvalidArgumentError.code,
            rationale=f"{reason} Request refused before registration; not a governance decision.",
            passage_id=passage_id or "", idempotency_key=idem_key,
        )
        raise InvalidArgumentError(reason, field=field, passage_id=passage_id,
                                   idempotency_key=idem_key)

    def _flag_fields(self, composed_decision: str, scoring_result: dict | None) -> dict[str, Any]:
        """RFC-0 §4 fields of a result: the FLAG's declared conditions, and the
        axes the score did not measure. Conditions come only from the policy."""
        is_flag = composed_decision == Decision.FLAG.value
        return {
            "flag_conditions": list(self.policy.flag_conditions) if is_flag else [],
            "declared_unmeasured": list((scoring_result or {}).get("declared_unmeasured", [])),
        }

    def _admission_terms(self, composed_decision: str) -> dict[str, Any]:
        """The terms persisted with a RESERVED transition (5.7.6): the admitting
        decision and, for a FLAG, the conditions the policy declares. They are
        what dispatch later enforces."""
        is_flag = composed_decision == Decision.FLAG.value
        return {
            "composed_decision": composed_decision,
            "flag_conditions": list(self.policy.flag_conditions) if is_flag else [],
        }

    def _with_flag_conditions(self, composed_decision: str, rationale: str) -> str:
        if composed_decision == Decision.FLAG.value and self.policy.flag_conditions:
            return rationale + " Conditions: " + "; ".join(self.policy.flag_conditions) + "."
        return rationale

    FLAG_WITHOUT_CONDITION_RATIONALE = (
        "FLAG states no condition: the policy declares no flag_conditions, so the "
        "conditional authorization cannot be stated (RFC-0 §4, decision 3); held, "
        "not admitted. Declare CompositionPolicy.flag_conditions or re-adjudicate."
    )

    def _apply_flag_condition_rule(self, composed_decision: str, rationale: str) -> tuple[str, str]:
        """RFC-0 §4 (5.7.6): a FLAG admits only under declared conditions.

        With conditions declared, the FLAG stands and names them. Without,
        the result is HOLD: the runtime invents no condition and admits
        nothing it cannot condition.
        """
        if composed_decision != Decision.FLAG.value:
            return composed_decision, rationale
        if self.policy.flag_conditions:
            return composed_decision, self._with_flag_conditions(composed_decision, rationale)
        return Decision.HOLD.value, (rationale + " " + self.FLAG_WITHOUT_CONDITION_RATIONALE).strip()

    def _verify_attestation(self, attestation: NonExecutionAttestation) -> str | None:
        """Method string when the deployment's verifier authenticates it, else None."""
        if self.attestation_verifier is None:
            return None
        try:
            method = self.attestation_verifier.verify(attestation)
        except Exception:
            return None
        if not isinstance(method, str) or not method.strip():
            return None
        return method

    def _revalidate_mandate_at_dispatch(self, record: OperationRecord, amount: float) -> list[str]:
        """Re-check, against the current state, the mandate that admitted the
        operation (5.7.6). Returns the list of changes; empty means intact.

        Checked: the acting agent (when it was registered at admission), the
        delegation in scope, the in-memory budget (the resource still exists
        and its limit still covers consumed + reserved, so a limit lowered
        under this reservation refuses), and the composition policy version.
        With a durable budget backend the limit lives in the backend, which
        refuses a re-registration with another capacity and enforces I8 on
        every reserve; no backend I/O runs here, so the check is the same
        before and under the quarantine gate.
        """
        changes: list[str] = []
        state = self.membrane.state
        if record.agent_registered_at_admission and record.agent_id not in state.agents:
            changes.append(f"acting agent '{record.agent_id}' is no longer registered")
        if record.delegating_agent_id:
            delegator = state.agents.get(record.delegating_agent_id)
            if delegator is None:
                changes.append(f"delegating agent '{record.delegating_agent_id}' is no longer registered")
            elif not delegator.can_delegate_to(record.delegatee_agent_id or record.agent_id):
                changes.append(
                    f"delegation from '{record.delegating_agent_id}' to "
                    f"'{record.delegatee_agent_id or record.agent_id}' is withdrawn"
                )
        if record.resource_id and amount > 0 and self.consensus_backend is None:
            resource = state.resources.get(record.resource_id)
            if resource is None:
                changes.append(f"resource '{record.resource_id}' is no longer registered")
            else:
                try:
                    headroom = float(resource.capacity) - float(resource.consumed) - float(resource.reserved)
                except (TypeError, ValueError):
                    headroom = float("nan")
                own = float(amount) if record.budget_reserved else 0.0
                if not math.isfinite(headroom) or headroom + own < float(amount):
                    changes.append(
                        f"budget limit of '{record.resource_id}' no longer covers the amount "
                        f"(capacity {resource.capacity}, consumed {resource.consumed}, "
                        f"reserved {resource.reserved}, amount {amount})"
                    )
        if record.policy_version and record.policy_version != str(self.policy.version):
            changes.append(
                f"composition policy version changed from '{record.policy_version}' "
                f"to '{self.policy.version}' since admission"
            )
        return changes

    def _revalidate_mandate_locked(self, idempotency_key: str) -> dict[str, Any] | None:
        """Before the dispatch gate: take the settlement lock, re-read, and
        fail closed when the admitting mandate changed (5.7.6).

        Returns None when the mandate is intact or the operation is no longer
        RESERVED (the caller proceeds on the current record).
        """
        if self.consensus_backend is not None and not self.registry.is_persistent:
            # Configuration refusal first: the dispatch path refuses a durable
            # backend without a persistent registry, with no state change.
            return None
        try:
            with self._settlement_lock:
                with self.registry.settlement_lock(
                    idempotency_key,
                    require_persistent=self.consensus_backend is not None,
                ):
                    current = self.registry.get(idempotency_key)
                    if current is None or current.state != OperationState.RESERVED:
                        return None
                    changes = self._revalidate_mandate_at_dispatch(current, float(current.amount))
                    if not changes:
                        return None
                    return self._fail_closed_on_mandate_change(idempotency_key, current, changes)
        except Exception:
            current = self.registry.get(idempotency_key)
            return {
                "dispatched": False,
                "mandate_revalidation_failed": True,
                "operation_state": current.state.value if current else None,
                "budget_reconciliation_required": True,
                "reason": "Mandate re-validation could not acquire the durable settlement lock; no dispatch.",
            }

    @staticmethod
    def _flag_conditions_acknowledged(record: OperationRecord) -> bool:
        """True when every condition recorded at admission has a persisted,
        non-empty acknowledgement."""
        declared = list(record.flag_conditions)
        met = record.flag_conditions_met or {}
        return bool(declared) and set(declared) == set(met) and all(
            isinstance(v, str) and v.strip() for v in met.values()
        )

    def _acknowledge_flag_conditions(
        self, idempotency_key: str, record: OperationRecord,
        met: Mapping[str, str] | None,
    ) -> dict[str, Any] | None:
        """Before the dispatch gate (5.7.6, RFC-0 §4): a FLAG-admitted
        operation dispatches only once every condition recorded at admission
        is acknowledged. The acknowledgement is persisted on the record and
        ledgered; a refusal is returned as the dispatch result.

        A PASS admission has no condition and ``met`` is ignored. Returns None
        when dispatch may proceed.
        """
        if record.composed_decision != Decision.FLAG.value and not record.flag_conditions:
            return None
        declared = list(record.flag_conditions)
        base = {"dispatched": False, "operation_state": record.state.value,
                "flag_conditions": declared, "flag_conditions_met": record.flag_conditions_met}
        if not declared:
            return {**base, "reason": (
                "FLAG admission records no condition; dispatch refused (RFC-0 §4). "
                "Re-adjudicate under a policy that declares flag_conditions.")}
        if met is None:
            if self._flag_conditions_acknowledged(record):
                return None
            return {**base, "reason": (
                "FLAG conditions not acknowledged: the dispatch of a FLAG-admitted operation "
                "requires flag_conditions_met covering: " + "; ".join(declared) + ".")}
        try:
            canonical = json.loads(json.dumps(dict(met), sort_keys=True))
        except (TypeError, ValueError) as exc:
            return {**base, "reason": f"FLAG condition acknowledgement is invalid: {exc}."}
        already = record.flag_conditions_met == canonical
        try:
            recorded = self.registry.record_flag_conditions_met(idempotency_key, met)
        except (ValueError, RuntimeError) as exc:
            return {**base, "reason": f"FLAG condition acknowledgement is invalid: {exc}."}
        if not recorded:
            current = self.registry.get(idempotency_key) or record
            return {**base, "operation_state": current.state.value,
                    "flag_conditions_met": current.flag_conditions_met,
                    "reason": (
                        "FLAG condition acknowledgement refused: it must cover exactly the "
                        "declared conditions (" + "; ".join(declared) + ") on a RESERVED "
                        f"operation (state {current.state.value}), and an acknowledgement "
                        "already recorded is never replaced.")}
        if not already:
            self.ledger.append(
                kernel="composed", decision=Decision.FLAG.value,
                rationale="FLAG conditions acknowledged at dispatch: " + "; ".join(
                    f"{c}: {canonical[c]}" for c in declared),
                passage_id=record.passage_id, idempotency_key=idempotency_key,
                composed_decision=Decision.FLAG.value,
            )
        return None

    def _fail_closed_on_mandate_change(
        self, idempotency_key: str, record: OperationRecord, changes: list[str],
    ) -> dict[str, Any]:
        """Refuse the dispatch of an operation whose mandate changed (5.7.6).

        The reservation is released first; the attempt is terminal (FAILED,
        CONFIRMED_NONEXECUTION: the check runs before any DISPATCHING claim).
        If the release cannot be proven, the operation stays open for
        reconciliation and the refusal is ledgered.
        """
        detail = "; ".join(changes)
        base = {"dispatched": False, "mandate_revalidation_failed": True, "mandate_changes": list(changes)}
        release_error = self._release_hold_budget_before_terminal_transition(idempotency_key, record)
        if release_error:
            reason = (f"MANDATE_REVALIDATION_FAILED at dispatch ({detail}): no effect; attempt not "
                      f"closed, budget reconciliation required; {release_error}")
            self.ledger.append(
                kernel="composed", decision=Decision.HARD_VETO.value, rationale=reason,
                passage_id=record.passage_id, idempotency_key=idempotency_key,
                composed_decision=Decision.HARD_VETO.value,
            )
            return {**base, "operation_state": record.state.value,
                    "budget_reconciliation_required": True, "reason": reason}
        success, _prev = self.registry.transition_to(idempotency_key, OperationState.FAILED)
        current = self.registry.get(idempotency_key)
        reason = (
            f"MANDATE_REVALIDATION_FAILED at dispatch ({detail}): FAIL-CLOSED, terminal for this "
            "attempt identity, never dispatched (CONFIRMED_NONEXECUTION), reservation released."
            if success else f"MANDATE_REVALIDATION_FAILED at dispatch ({detail}); transition to FAILED failed."
        )
        self.ledger.append(
            kernel="composed", decision=Decision.HARD_VETO.value, rationale=reason,
            passage_id=record.passage_id, idempotency_key=idempotency_key,
            composed_decision=Decision.HARD_VETO.value,
        )
        return {**base, "operation_state": current.state.value if current else None,
                "composed_decision": Decision.HARD_VETO.value,
                "execution_record": "CONFIRMED_NONEXECUTION" if success else None, "reason": reason}

    def _prospective_utilization(self, idempotency_key: str, resource, amount: float) -> float:
        """Budget utilization as it will be once this operation holds its budget.

        The operation's own amount is counted until it holds a reservation
        (record.budget_reserved), so the policy sees the same load it would
        see after the reservation is taken.
        """
        record = self.registry.get(idempotency_key)
        own = 0.0 if (record is not None and record.budget_reserved) else amount
        if self.consensus_backend is not None and record is not None and record.resource_id:
            try:
                state = self.consensus_backend.get_state(record.resource_id)
                capacity = float(state.capacity)
                if not math.isfinite(capacity) or capacity <= 0:
                    return float("inf")
                return (float(state.committed) + float(state.reserved) + own) / capacity
            except Exception:
                # Missing/stale durable utilization is indeterminate: fail closed.
                return float("inf")
        return (resource.consumed + resource.reserved + own) / resource.capacity

    def _reserve_budget(self, idempotency_key: str, resource_id: str | None, amount: float) -> bool:
        """Reserve budget for an operation, at most once.

        Idempotent through record.budget_reserved: a second call on an
        operation that already holds its reservation is a no-op. Returns
        False if the budget cannot be reserved (fail-closed for the caller).
        """
        if not resource_id or amount <= 0:
            return True
        record = self.registry.get(idempotency_key)
        if record is None:
            return False
        if self.consensus_backend is not None and not self.registry.is_persistent:
            # Even a pre-existing ownership flag is unsafe without the
            # persistent cross-process settlement lock.
            return False
        if record.budget_reserved:
            # A backend-backed record must carry the reservation identity too.
            return self.consensus_backend is None or bool(record.reservation_id)
        if self.consensus_backend is not None:
            # The backend's idempotency key closes the crash window between
            # durable reservation and registry ownership persistence.
            try:
                result = self.consensus_backend.reserve(
                    idempotency_key, amount, resource_id,
                )
            except Exception:
                return False
            if getattr(result, "status", None) != "RESERVED":
                return False
            reservation_id = getattr(result, "reservation_id", None)
            if not reservation_id:
                return False
            try:
                recorded = self.registry.set_budget_flags(
                    idempotency_key,
                    budget_reserved=True,
                    budget_committed=False,
                    reservation_id=reservation_id,
                )
            except Exception:
                recorded = False
            if not recorded:
                # Compensation is idempotent. If the process dies before this
                # call, replaying reserve(key, ...) returns the same reservation.
                try:
                    self.consensus_backend.release(reservation_id)
                except Exception:
                    pass
                return False
            return True

        resource = self.membrane.state.resources.get(resource_id)
        if resource is None:
            return False
        with self.membrane.state.get_lock(resource_id):
            if not resource.reserve(amount):
                return False
        # Legacy in-process path: retain existing behavior when no durable
        # consensus backend has been explicitly configured.
        try:
            recorded = self.registry.set_budget_flags(
                idempotency_key, budget_reserved=True,
            )
        except Exception:
            recorded = False
        if not recorded:
            with self.membrane.state.get_lock(resource_id):
                resource.release(amount)
            return False
        return True

    def _commit_request_matches_record(self, request, record: OperationRecord) -> bool:
        """Check every immutable execution field before authority use."""
        from ..experimental.ep02_commit_authorization import canonical_amount

        try:
            binding = request.binding
            anchor = self.commit_authority.anchor
            return (
                binding.authority_id == anchor.authority_id
                and binding.profile_id == anchor.trust_profile_id
                and binding.profile_generation == anchor.trust_generation
                and binding.institutional_scope == record.institutional_scope
                and binding.operation_id == record.operation_id
                and binding.idempotency_key == record.idempotency_key
                and binding.passage_id == record.passage_id
                and binding.agent_id == record.agent_id
                and binding.action_type == record.action_type
                and binding.resource_id == record.resource_id
                and binding.amount == canonical_amount(record.amount)
                and binding.payload_digest == record.payload_digest
            )
        except (AttributeError, TypeError, ValueError):
            return False

    def _record_commit_gate_status(
        self,
        idempotency_key: str,
        *,
        phase: str,
        status: str,
        reason: str,
        security_event: bool = False,
    ) -> bool:
        """Persist and ledger a bounded authorization substate."""
        persisted = False
        try:
            persisted = self.registry.record_commit_gate_status(
                idempotency_key,
                phase=phase,
                status=status,
                reason=reason,
                security_event=security_event,
            )
        except Exception:
            persisted = False
        record = self.registry.get(idempotency_key)
        self.ledger.append(
            kernel="commit_authorization",
            decision=(
                Decision.HARD_VETO.value if security_event else Decision.HOLD.value
            ),
            rationale=f"{phase}:{status}: {reason}",
            passage_id=record.passage_id if record is not None else "",
            idempotency_key=idempotency_key,
        )
        return persisted

    def authorize_commit(
        self,
        idempotency_key: str,
        request,
        *,
        finding,
        claim,
        current_snapshot,
        now,
    ) -> dict[str, Any]:
        """Durably request the exact Profile S authorization for an operation.

        The request is recorded before the authority call.  If a reply is
        lost after SQLite commits, lookup recovers the signed decision.  An
        ambiguous result leaves the operation RESERVED and never authorizes
        dispatch.
        """
        if not self.require_commit_authorization:
            return {
                "authorized": False,
                "reason": "Profile S commit authorization is not enabled.",
            }
        from ..experimental.ep02_commit_authorization import (
            AuthorizationDecision,
            CommitLookupStatus,
            authorization_receipt_to_dict,
            authorization_request_to_dict,
            verify_authorization_receipt,
        )

        record = self.registry.get(idempotency_key)
        if record is None:
            return {"authorized": False, "reason": "Operation not registered."}
        if record.state != OperationState.RESERVED:
            return {
                "authorized": False,
                "reason": f"Commit authorization requires RESERVED state, got {record.state.value}.",
                "operation_state": record.state.value,
            }
        if not self._commit_request_matches_record(request, record):
            return {
                "authorized": False,
                "reason": "Authorization request does not match the immutable operation binding.",
                "operation_state": record.state.value,
            }
        try:
            request_data = authorization_request_to_dict(request)
        except (TypeError, ValueError, AttributeError) as exc:
            return {"authorized": False, "reason": f"Invalid authorization request: {exc}"}
        if not self.registry.record_commit_authorization_request(
            idempotency_key, request_data,
        ):
            return {
                "authorized": False,
                "reason": "A different authorization request is already bound to this operation.",
            }

        receipt = None
        authority_error = None
        try:
            receipt = self.commit_authority.authorize(
                request,
                finding=finding,
                claim=claim,
                current_snapshot=current_snapshot,
                now=now,
            )
        except Exception as exc:
            authority_error = exc
            try:
                lookup = self.commit_authority.lookup_authorization(request)
                if lookup.status == CommitLookupStatus.RECORDED:
                    receipt = lookup.receipt
            except Exception:
                receipt = None
        if receipt is None:
            reason = "Commit authorization outcome is unknown; dispatch remains blocked."
            self._record_commit_gate_status(
                idempotency_key,
                phase="authorization",
                status="TRUST_UNKNOWN",
                reason=reason,
            )
            return {
                "authorized": False,
                "authorization_status": "UNKNOWN",
                "reason": (
                    reason
                    + (f" Authority error: {authority_error}" if authority_error else "")
                ),
                "operation_state": record.state.value,
            }
        try:
            trusted = self.commit_authority.trusted_receipt_keys()
        except Exception:
            reason = "Commit authorization trust keys are unavailable or invalid."
            self._record_commit_gate_status(
                idempotency_key,
                phase="authorization",
                status="HOLD",
                reason=reason,
                security_event=True,
            )
            return {
                "authorized": False,
                "authorization_status": "INVALID",
                "reason": reason,
            }
        if not verify_authorization_receipt(receipt, request, trusted_keys=trusted):
            reason = "Commit authorization receipt verification failed."
            self._record_commit_gate_status(
                idempotency_key,
                phase="authorization",
                status="HOLD",
                reason=reason,
                security_event=True,
            )
            return {
                "authorized": False,
                "authorization_status": "INVALID",
                "reason": reason,
            }
        if not self.registry.record_commit_authorization_receipt(
            idempotency_key, authorization_receipt_to_dict(receipt),
        ):
            reason = "Signed authorization could not be bound durably to the operation."
            self._record_commit_gate_status(
                idempotency_key,
                phase="authorization",
                status="HOLD",
                reason=reason,
                security_event=True,
            )
            return {
                "authorized": False,
                "authorization_status": "PERSISTENCE_FAILED",
                "reason": reason,
            }
        if receipt.decision != AuthorizationDecision.AUTHORIZED:
            self._record_commit_gate_status(
                idempotency_key,
                phase="authorization",
                status="HOLD",
                reason=receipt.reason,
            )
        return {
            "authorized": receipt.decision == AuthorizationDecision.AUTHORIZED,
            "authorization_status": receipt.decision.value,
            "reason": receipt.reason,
            "request_id": receipt.request_id,
            "authorization_sequence": receipt.sequence,
            "replayed": receipt.replayed,
            "operation_state": record.state.value,
        }

    def _consume_commit_authorization(self, idempotency_key: str) -> dict[str, Any]:
        """Recover or consume one authorization before taking the dispatch lock."""
        from ..experimental.ep02_commit_authorization import (
            AuthorizationDecision,
            CommitLookupStatus,
            ConsumptionDecision,
            authorization_receipt_from_dict,
            authorization_request_from_dict,
            consumption_receipt_to_dict,
            verify_authorization_receipt,
            verify_consumption_receipt,
        )

        record = self.registry.get(idempotency_key)
        if record is None or record.state != OperationState.RESERVED:
            return {"consumed": False, "reason": "Operation is not dispatchable from RESERVED state."}
        if (
            record.commit_authorization_request is None
            or record.commit_authorization_receipt is None
        ):
            return {
                "consumed": False,
                "reason": "No durable Profile S commit authorization is bound to the operation.",
            }
        try:
            request = authorization_request_from_dict(record.commit_authorization_request)
            authorization = authorization_receipt_from_dict(record.commit_authorization_receipt)
        except (TypeError, ValueError, AttributeError) as exc:
            reason = f"Stored commit authorization is invalid: {exc}"
            self._record_commit_gate_status(
                idempotency_key,
                phase="consumption",
                status="HOLD",
                reason=reason,
                security_event=True,
            )
            return {"consumed": False, "reason": reason}
        try:
            trusted = self.commit_authority.trusted_receipt_keys()
        except Exception:
            reason = "Commit authorization trust keys are unavailable or invalid."
            self._record_commit_gate_status(
                idempotency_key,
                phase="consumption",
                status="HOLD",
                reason=reason,
                security_event=True,
            )
            return {
                "consumed": False,
                "consumption_status": "INVALID",
                "reason": reason,
            }
        if (
            not self._commit_request_matches_record(request, record)
            or not verify_authorization_receipt(
                authorization, request, trusted_keys=trusted,
            )
            or authorization.decision != AuthorizationDecision.AUTHORIZED
        ):
            reason = "Stored commit authorization is rejected, invalid, or operation-mismatched."
            self._record_commit_gate_status(
                idempotency_key,
                phase="consumption",
                status="HOLD",
                reason=reason,
                security_event=True,
            )
            return {
                "consumed": False,
                "reason": reason,
            }
        proposed_claim = "dispatch:" + sha256(
            f"{idempotency_key}\0{request.request_id}".encode("utf-8")
        ).hexdigest()
        claimed, claim_id = self.registry.ensure_commit_dispatch_claim(
            idempotency_key, proposed_claim,
        )
        if not claimed:
            return {"consumed": False, "reason": "Could not persist the dispatch claim identity."}

        consumption = None
        consume_error = None
        try:
            lookup = self.commit_authority.lookup_consumption(request)
            if lookup.status == CommitLookupStatus.RECORDED:
                consumption = lookup.receipt
            else:
                consumption = self.commit_authority.consume(
                    request, authorization, dispatch_claim_id=claim_id,
                )
        except Exception as exc:
            consume_error = exc
            try:
                lookup = self.commit_authority.lookup_consumption(request)
                if lookup.status == CommitLookupStatus.RECORDED:
                    consumption = lookup.receipt
            except Exception:
                consumption = None
        if consumption is None:
            reason = "Commit consumption outcome is unknown; dispatch remains blocked."
            self._record_commit_gate_status(
                idempotency_key,
                phase="consumption",
                status="TRUST_UNKNOWN",
                reason=reason,
            )
            return {
                "consumed": False,
                "consumption_status": "UNKNOWN",
                "reason": (
                    reason
                    + (f" Authority error: {consume_error}" if consume_error else "")
                ),
            }
        if (
            consumption.dispatch_claim_id != claim_id
            or not verify_consumption_receipt(
                consumption, request, authorization, trusted_keys=trusted,
            )
        ):
            reason = "Commit consumption receipt verification failed."
            self._record_commit_gate_status(
                idempotency_key,
                phase="consumption",
                status="HOLD",
                reason=reason,
                security_event=True,
            )
            return {
                "consumed": False,
                "consumption_status": "INVALID",
                "reason": reason,
            }
        if not self.registry.record_commit_consumption_receipt(
            idempotency_key, consumption_receipt_to_dict(consumption),
        ):
            reason = "Signed consumption could not be bound durably to the operation."
            self._record_commit_gate_status(
                idempotency_key,
                phase="consumption",
                status="HOLD",
                reason=reason,
                security_event=True,
            )
            return {
                "consumed": False,
                "consumption_status": "PERSISTENCE_FAILED",
                "reason": reason,
            }
        if consumption.decision != ConsumptionDecision.CONSUMED:
            self._record_commit_gate_status(
                idempotency_key,
                phase="consumption",
                status="HOLD",
                reason=consumption.reason,
            )
        return {
            "consumed": consumption.decision == ConsumptionDecision.CONSUMED,
            "consumption_status": consumption.decision.value,
            "reason": consumption.reason,
            "consumption_sequence": consumption.sequence,
            "replayed": consumption.replayed,
        }

    def _valid_commit_consumption(self, record: OperationRecord) -> bool:
        """Verify the durable signed evidence again at the dispatch boundary."""
        if not record.commit_gate_required:
            return True
        from ..experimental.ep02_commit_authorization import (
            AuthorizationDecision,
            ConsumptionDecision,
            authorization_receipt_from_dict,
            authorization_request_from_dict,
            consumption_receipt_from_dict,
            verify_authorization_receipt,
            verify_consumption_receipt,
        )
        try:
            request = authorization_request_from_dict(record.commit_authorization_request)
            authorization = authorization_receipt_from_dict(record.commit_authorization_receipt)
            consumption = consumption_receipt_from_dict(record.commit_consumption_receipt)
            trusted = self.commit_authority.trusted_receipt_keys()
            return (
                self._commit_request_matches_record(request, record)
                and authorization.decision == AuthorizationDecision.AUTHORIZED
                and consumption.decision == ConsumptionDecision.CONSUMED
                and consumption.dispatch_claim_id == record.commit_dispatch_claim_id
                and verify_authorization_receipt(
                    authorization, request, trusted_keys=trusted,
                )
                and verify_consumption_receipt(
                    consumption, request, authorization, trusted_keys=trusted,
                )
            )
        except Exception:
            return False

    def resume_existing(
        self,
        idempotency_key: str,
        resource_id: str | None = None,
        amount: float = 0.0,
    ) -> dict[str, Any]:
        """Resume an existing operation that was already claimed.

        Per snippet: "provide a separate resume_existing() path so legitimate
        recovery is not confused with fresh adjudication."

        | State         | Resume action                                    |
        |---------------|--------------------------------------------------|
        | CREATED       | Reconcile durable budget; re-adjudicate legacy   |
        | HOLD          | Resolve with new evidence or escalate            |
        | RESERVED      | Safe to dispatch (checks passed, budget reserved) |
        | DISPATCHING   | Reconcile — do NOT resend                        |
        | ESCALATED     | Awaiting authorized exit (absorbing state)        |
        | CONFIRMED     | Return existing confirmed result                 |
        | FAILED        | Return existing failed result                    |
        | UNKNOWN       | Human review required                            |
        | No record     | Not found — caller should use adjudicate()       |
        """
        record = self.registry.get(idempotency_key)
        if record is None:
            return {
                "idempotency_key": idempotency_key,
                "action": "not_found",
                "reason": "No existing operation with this key. Use adjudicate() for new operations.",
                "operation_state": None,
            }

        state = record.state
        if state == OperationState.CREATED:
            if self.consensus_backend is not None and record.resource_id and record.amount > 0:
                return {
                    "idempotency_key": idempotency_key,
                    "action": "reconcile_budget",
                    "reason": (
                        "Durable admission did not complete. Use fail() to reconcile and "
                        "release any reservation before closing this operation; do not dispatch."
                    ),
                    "operation_state": state.value,
                    "passage_id": record.passage_id,
                }
            return {
                "idempotency_key": idempotency_key,
                "action": "re_adjudicate",
                "reason": "Operation in CREATED state — no checks were run yet. Safe to re-adjudicate.",
                "operation_state": state.value,
                "passage_id": record.passage_id,
            }
        elif state == OperationState.HOLD:
            tau_status = self.hold_manager.check_tau_k_expiry(idempotency_key)
            return {
                "idempotency_key": idempotency_key,
                "action": "resolve_or_escalate",
                "reason": (
                    f"Operation in HOLD state — evidence or governance question unresolved. "
                    f"Resolve with new evidence, or escalate. {tau_status['reason']}."
                ),
                "operation_state": state.value,
                "passage_id": record.passage_id,
                "tau_k_remaining": tau_status.get("tau_k_remaining", 0.0),
                "tau_k_expired": tau_status.get("expired", False),
            }
        elif state == OperationState.RESERVED:
            if self._quarantine_blocks():
                return {
                    "idempotency_key": idempotency_key,
                    "action": "await_quarantine_release",
                    "reason": "QUARANTINE_LOCK active; reserved operation cannot be dispatched.",
                    "operation_state": state.value,
                    "passage_id": record.passage_id,
                }
            if self.consensus_backend is not None and record.resource_id and record.amount > 0:
                if not record.reservation_id:
                    return {
                        "idempotency_key": idempotency_key,
                        "action": "reconcile",
                        "reason": "Durable reservation identity missing; dispatch blocked pending reconciliation.",
                        "operation_state": state.value,
                        "passage_id": record.passage_id,
                    }
                try:
                    backend_state = self.consensus_backend.get_state(record.resource_id)
                    reservation = next(
                        (item for item in backend_state.reservations
                         if item.get("reservation_id") == record.reservation_id),
                        None,
                    )
                except Exception:
                    reservation = None
                if reservation is None or reservation.get("state") != "RESERVED":
                    return {
                        "idempotency_key": idempotency_key,
                        "action": "reconcile",
                        "reason": "Durable reservation is missing or not RESERVED; dispatch blocked pending reconciliation.",
                        "operation_state": state.value,
                        "passage_id": record.passage_id,
                    }
            if (
                record.commit_gate_required
                and record.commit_authorization_status == "TRUST_UNKNOWN"
            ):
                return {
                    "idempotency_key": idempotency_key,
                    "action": "reconcile_commit_authorization",
                    "reason": "The Profile S authority outcome is unknown; retry lookup with the original request ID.",
                    "operation_state": state.value,
                    "authorization_status": record.commit_authorization_status,
                    "passage_id": record.passage_id,
                }
            if (
                record.commit_gate_required
                and record.commit_authorization_status == "HOLD"
            ):
                return {
                    "idempotency_key": idempotency_key,
                    "action": "return_authorization_result",
                    "reason": record.commit_gate_last_reason or "The Profile S authorization is on HOLD.",
                    "operation_state": state.value,
                    "authorization_status": record.commit_authorization_status,
                    "passage_id": record.passage_id,
                }
            if record.commit_gate_required and record.commit_authorization_receipt is None:
                return {
                    "idempotency_key": idempotency_key,
                    "action": "authorize_commit",
                    "reason": "Operation is RESERVED but Profile S commit authorization is still required.",
                    "operation_state": state.value,
                    "passage_id": record.passage_id,
                    "resource_id": record.resource_id,
                    "amount": record.amount,
                }
            if record.commit_gate_required:
                if record.commit_consumption_status == "TRUST_UNKNOWN":
                    return {
                        "idempotency_key": idempotency_key,
                        "action": "reconcile_commit_consumption",
                        "reason": "The Profile S consumption outcome is unknown; retry lookup with the original request and dispatch claim IDs.",
                        "operation_state": state.value,
                        "consumption_status": record.commit_consumption_status,
                        "passage_id": record.passage_id,
                    }
                if record.commit_consumption_status == "HOLD":
                    return {
                        "idempotency_key": idempotency_key,
                        "action": "return_authorization_result",
                        "reason": record.commit_gate_last_reason or "The Profile S consumption is on HOLD.",
                        "operation_state": state.value,
                        "consumption_status": record.commit_consumption_status,
                        "passage_id": record.passage_id,
                    }
                body = record.commit_authorization_receipt.get("body", {})
                if body.get("decision") != "AUTHORIZED":
                    return {
                        "idempotency_key": idempotency_key,
                        "action": "return_authorization_result",
                        "reason": "The durable Profile S commit authorization was not granted.",
                        "operation_state": state.value,
                        "authorization_decision": body.get("decision", "INVALID"),
                        "passage_id": record.passage_id,
                    }
            if ((record.composed_decision == Decision.FLAG.value or record.flag_conditions)
                    and not self._flag_conditions_acknowledged(record)):
                return {
                    "idempotency_key": idempotency_key,
                    "action": "acknowledge_flag_conditions",
                    "reason": (
                        "Operation is RESERVED under a FLAG; dispatch requires flag_conditions_met "
                        "covering every declared condition (RFC-0 §4)."
                    ),
                    "operation_state": state.value,
                    "passage_id": record.passage_id,
                    "resource_id": record.resource_id,
                    "amount": record.amount,
                    "flag_conditions": list(record.flag_conditions),
                }
            return {
                "idempotency_key": idempotency_key,
                "action": "dispatch",
                "reason": "Operation in RESERVED state — checks passed, budget reserved. Safe to dispatch.",
                "operation_state": state.value,
                "passage_id": record.passage_id,
                "resource_id": record.resource_id,
                "amount": record.amount,
            }
        elif state == OperationState.DISPATCHING:
            return {
                "idempotency_key": idempotency_key,
                "action": "reconcile",
                "reason": "Operation in DISPATCHING state — effect may have occurred. Reconcile, do NOT resend.",
                "operation_state": state.value,
                "passage_id": record.passage_id,
            }
        elif state == OperationState.CONFIRMED:
            return {
                "idempotency_key": idempotency_key,
                "action": "return_result",
                "reason": "Operation already CONFIRMED. Return existing result, do not repeat.",
                "operation_state": state.value,
                "passage_id": record.passage_id,
            }
        elif state == OperationState.FAILED:
            return {
                "idempotency_key": idempotency_key,
                "action": "return_result",
                "reason": "Operation already FAILED. Return existing result, do not retry automatically.",
                "operation_state": state.value,
                "passage_id": record.passage_id,
            }
        elif state == OperationState.UNKNOWN:
            return {
                "idempotency_key": idempotency_key,
                "action": "human_review",
                "reason": "Operation in UNKNOWN state — outcome uncertain. Human review required, do NOT resend.",
                "operation_state": state.value,
                "passage_id": record.passage_id,
            }
        elif state == OperationState.ESCALATED:
            return {
                "idempotency_key": idempotency_key,
                "action": "await_authorized_exit",
                "reason": (
                    "Operation in ESCALATED state — absorbing until authorized exit. "
                    "An authorization model must define who signs, under what scope, "
                    "and with which receipt. Time-based exit is rejected."
                ),
                "operation_state": state.value,
                "passage_id": record.passage_id,
                "hold_record": record.hold_record.to_dict() if record.hold_record else None,
            }
        else:
            return {
                "idempotency_key": idempotency_key,
                "action": "unknown_state",
                "reason": f"Operation in unexpected state {state.value}.",
                "operation_state": state.value,
            }

    def _quarantine_blocks(self) -> bool:
        """True when this process or the shared registry mark forbids admission.

        The in-memory flag excludes other threads on this membrane. The registry
        mark is what another process sharing the durable store observes. A claim
        that already reached DISPATCHING is not rolled back here.
        """
        if self.membrane.state.quarantine_active:
            return True
        checker = getattr(self.registry, "quarantine_is_active", None)
        return bool(checker()) if callable(checker) else False

    def lock_quarantine(
        self, locked_by: str = "", *, incident_id: str | None = None,
    ) -> None:
        """Persist the shared guard; new incidents renew the lock epoch."""
        self.membrane.lock_quarantine(locked_by, incident_id=incident_id)

    def release_quarantine(
        self,
        authorized_by: str,
        scope: str,
        authorization: str,
        reason: str = "",
    ) -> dict[str, Any]:
        """Only a configured trusted verifier can authorize durable release."""
        return self.membrane.release_quarantine(
            authorized_by, scope, authorization, reason,
        )

    def dispatch(
        self,
        idempotency_key: str,
        resource_id: str | None = None,
        amount: float = 0.0,
        metadata: dict[str, Any] | None = None,
        *,
        payload: dict[str, Any] | None = None,
        flag_conditions_met: Mapping[str, str] | None = None,
        _active_provider_call: bool = False,
    ) -> dict[str, Any]:
        """Claim the dispatch slot (RESERVED → DISPATCHING) for one operation.

        Since the 5.7.7 candidate the full verification of the decision ledger
        runs here, under the quarantine gate, immediately before the claim
        (``CompositionPolicy.ledger_full_verification_at_dispatch``): a ledger
        that does not verify claims nothing, whatever the caller
        (``dispatch_and_execute``, a direct call, the SDK client).

        Since 5.7.6 two checks run before the claim, on the default policy:

        - a FLAG-admitted operation dispatches only once every condition the
          admission recorded is acknowledged (``flag_conditions_met``, a
          mapping condition → how it was met, persisted and ledgered);
        - the mandate that admitted the operation is re-validated (agent,
          delegation, budget limit, policy version); a change refuses fail-
          closed: the reservation is released and the attempt ends FAILED
          with ``execution_record`` CONFIRMED_NONEXECUTION.
        """
        # Never hold the quarantine lock across SQLite or another external
        # authority call.  A quick first check avoids consuming an
        # authorization while an already-active quarantine blocks dispatch.
        actual_payload = metadata if payload is None else payload
        record = self.registry.get(idempotency_key)
        if record is not None and self._tau_k_exhausted(record):
            tau_k_refusal = self._fail_closed_on_tau_k_locked(idempotency_key, "dispatch")
            if tau_k_refusal is not None:
                return tau_k_refusal
            record = self.registry.get(idempotency_key)
        if record is not None and record.state == OperationState.RESERVED:
            # 5.7.6 (RFC-0 §4): acknowledge the FLAG conditions, persisted
            # before any claim; a PASS admission has none.
            refusal = self._acknowledge_flag_conditions(idempotency_key, record, flag_conditions_met)
            if refusal is not None:
                return refusal
            # 5.7.6: the mandate that admitted the operation must still hold.
            # Checked here, before any authority call or claim; re-checked
            # without I/O under the gate.
            if self.policy.revalidate_mandate_at_dispatch:
                refusal = self._revalidate_mandate_locked(idempotency_key)
                if refusal is not None:
                    return refusal
            record = self.registry.get(idempotency_key)
        if record is not None and record.commit_gate_required and record.state == OperationState.RESERVED:
            from ..experimental.ep02_commit_authorization import canonical_payload_bytes

            try:
                actual_payload_digest = sha256(
                    canonical_payload_bytes(actual_payload)
                ).hexdigest()
            except (TypeError, ValueError) as exc:
                return {
                    "dispatched": False,
                    "reason": f"Provider payload is invalid: {exc}.",
                    "operation_state": record.state.value,
                }
            if actual_payload_digest != record.payload_digest:
                return {
                    "dispatched": False,
                    "reason": "Provider payload does not match the authorized payload digest.",
                    "operation_state": record.state.value,
                }
            with self.membrane.state._quarantine_lock:
                with self.registry.quarantine_guard():
                    quarantine_active = self._quarantine_blocks()
                    # Single-use Profile S authorization: refuse on a ledger that
                    # does not verify BEFORE consuming it, so a refusal leaves the
                    # authorization intact (project owner's review of 2026-11-23,
                    # 17:37 UTC; the earlier state of this candidate consumed it
                    # first). The claim below re-verifies under the gate: on this
                    # path the full verification runs twice (declared cost).
                    integrity_refusal = None if quarantine_active else \
                        self._refuse_on_ledger_integrity(idempotency_key)
                if quarantine_active:
                    return {
                        "dispatched": False,
                        "reason": "QUARANTINE_LOCK is active; dispatch is blocked.",
                        "operation_state": record.state.value,
                    }
                if integrity_refusal is not None:
                    return integrity_refusal
            consumption = self._consume_commit_authorization(idempotency_key)
            if not consumption.get("consumed"):
                return {
                    "dispatched": False,
                    "reason": consumption.get("reason", "Commit authorization was not consumed."),
                    "operation_state": record.state.value,
                    "commit_consumption_status": consumption.get("consumption_status"),
                }

        # Only validation and the durable DISPATCHING claim need the shared
        # gate. The backend commit is operation-scoped work AFTER that claim:
        # slow network I/O must not prevent emergency quarantine activation.
        with self.membrane.state._quarantine_lock:
            with self.registry.quarantine_guard():
                # Full verification of the ledger at the effect boundary
                # (author's decisions of 2026-11-23, 11:22 and 14:54 UTC):
                # the per-decision check is incremental (verify_new_entries);
                # before the DISPATCHING claim, the last step before any
                # effect, every entry and every link is re-verified from
                # GENESIS, under the gate. The verification holds the ledger
                # lock, so no append lands while it runs; the claim follows
                # under the same gate, so no other claim slips between them.
                # The ledger itself can still be mutated by any code holding
                # a reference to it (``entries`` is the live list), and the
                # registry's ``transition_to`` is public: a caller that
                # bypasses dispatch() bypasses this check (declared limits).
                # Every path through dispatch() passes here:
                # dispatch_and_execute, a direct dispatch() call, the SDK
                # client, in this process (the ledger is in memory: another
                # process or a restart starts from an empty ledger that
                # verifies). Fail-closed: a ledger that does not verify
                # claims nothing. Linear in the ledger, once per dispatch
                # (twice on the Profile S path), never per HOLD; it runs
                # under the quarantine gate, so a quarantine activation waits
                # for it (declared cost: 0.54 s at 100 000 entries, measured
                # 2026-11-23).
                integrity_refusal = self._refuse_on_ledger_integrity(idempotency_key)
                if integrity_refusal is not None:
                    return integrity_refusal
                result = self._dispatch_under_quarantine_lock(
                    idempotency_key, resource_id, amount, payload=actual_payload,
                )
                if result.get("_backend_claimed") or (
                    result.get("dispatched") and _active_provider_call
                ):
                    if _active_provider_call:
                        with self._active_dispatches_lock:
                            self._active_dispatches.add(idempotency_key)

        if not result.get("_backend_claimed"):
            return result

        # A claimed transition is allowed to finish after quarantine becomes
        # active, but it must re-confirm ownership before touching the budget.
        # Other workers settling this operation use this same durable lock.
        try:
            with self._settlement_lock:
                with self.registry.settlement_lock(
                    idempotency_key, require_persistent=True,
                ):
                    current = self.registry.get(idempotency_key)
                    if (current is None
                            or current.state != OperationState.DISPATCHING
                            or current.dispatch_claimed_by != self._worker_id):
                        result = {
                            "dispatched": False,
                            "operation_state": current.state.value if current else "not_found",
                            "reason": "Dispatch claim changed before budget settlement; no provider call.",
                            "recommendation": "human_review",
                        }
                    else:
                        result = self._complete_claimed_backend_dispatch(
                            idempotency_key,
                            current.resource_id or None,
                            float(current.amount),
                        )
        except Exception:
            result = {
                "dispatched": False,
                "operation_state": OperationState.DISPATCHING.value,
                "reason": "Durable dispatch settlement failed; no provider call, manual reconciliation required.",
                "recommendation": "human_review",
            }
        if not result.get("dispatched") and _active_provider_call:
            with self._active_dispatches_lock:
                self._active_dispatches.discard(idempotency_key)
        return result

    def _refuse_on_ledger_integrity(self, idempotency_key: str) -> dict[str, Any] | None:
        """Refusal dict when the policy asks for a full ledger verification before
        a claim and the ledger does not verify; None otherwise.

        The refusal is written to the ledger (kernel ``api``, HARD_VETO) so the
        audit trail says why nothing was claimed. Declared limit: that entry is
        appended to a chain already known not to verify, so the chain itself
        cannot prove it; it is readable and timestamped, not chain-proven. The
        operation stays in its state (RESERVED): no claim, no effect.

        A chain that verifies again closes an open incident here, at the claim
        (owner's decision of 2026-11-23, 21:25 UTC, option A): see the comment
        below for what that changes.
        """
        if not self.policy.ledger_full_verification_at_dispatch:
            return None
        if self.ledger.verify_chain():
            # The incident is closed by a chain that verifies again, at the
            # claim, not only by an explicit audit (verify_ledger()). Before
            # this, a repaired chain left the incident open for ever, so a
            # LATER alteration, distinct from the first, was refused without
            # writing anything (incident_recorded: false): one audit entry
            # covered two alterations and the second left no trace (project
            # owner's review of 2026-11-23, 20:03 UTC, finding N1). A new
            # alteration now opens a NEW incident with its own entry. What
            # closes an incident is still verify_chain(): internal consistency,
            # not authenticity, so a rechained ledger closes it too (declared
            # limit, unchanged).
            self._ledger_integrity_incident = None
            return None
        record = self.registry.get(idempotency_key)
        reason = ("LEDGER_INTEGRITY_FAILED: the hash chain of the decision ledger does not "
                  "verify; no dispatch (fail-closed). Audit the ledger (verify_ledger()) "
                  "before any further effect.")
        incident = self._ledger_integrity_incident
        if incident is None:
            # First refusal: one api/HARD_VETO entry opens the incident.
            self.ledger.append(kernel="api", decision=Decision.HARD_VETO.value, rationale=reason,
                               passage_id=record.passage_id if record else "",
                               idempotency_key=idempotency_key)
            incident = self._ledger_integrity_incident = {
                "opened_at": time.time(), "first_idempotency_key": idempotency_key,
                "refusals": 0, "audit_entry_index": self.ledger.entry_count - 1}
        incident["refusals"] += 1
        return {"dispatched": False, "ledger_integrity_failed": True, "reason": reason,
                "audit_entry_chain_proven": False,
                "incident_recorded": incident["refusals"] == 1,
                "incident_refusals": incident["refusals"],
                "operation_state": record.state.value if record else None}

    def _dispatch_under_quarantine_lock(
        self,
        idempotency_key: str,
        resource_id: str | None = None,
        amount: float = 0.0,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically claim the dispatch slot and transition to DISPATCHING.

        Per review 3 §1: the transition from RESERVED → DISPATCHING is the
        atomic claim. If two workers try to dispatch the same operation,
        exactly one succeeds.
        """
        record = self.registry.get(idempotency_key)
        if record is None:
            return {"dispatched": False, "reason": "Operation not registered."}

        try:
            supplied_payload_hash = canonical_payload_hash(payload)
        except (TypeError, ValueError) as exc:
            return {"dispatched": False, "reason": f"Dispatch payload is invalid: {exc}."}
        expected_payload_hash = (
            record.payload_digest if record.commit_gate_required else record.payload_hash
        )
        if supplied_payload_hash != expected_payload_hash:
            return {
                "dispatched": False,
                "reason": "Dispatch payload does not match the payload authorized at adjudication.",
                "operation_state": record.state.value,
            }

        if record.commit_gate_required:
            from ..experimental.ep02_commit_authorization import canonical_payload_bytes

            try:
                actual_payload_digest = sha256(
                    canonical_payload_bytes(payload)
                ).hexdigest()
            except (TypeError, ValueError) as exc:
                return {
                    "dispatched": False,
                    "reason": f"Provider payload is invalid: {exc}.",
                    "operation_state": record.state.value,
                }
            if actual_payload_digest != record.payload_digest:
                return {
                    "dispatched": False,
                    "reason": "Provider payload does not match the authorized payload digest.",
                    "operation_state": record.state.value,
                }

        commit_gate_verified = self._valid_commit_consumption(record)
        if not commit_gate_verified:
            return {
                "dispatched": False,
                "reason": "A valid consumed Profile S commit authorization is required.",
                "operation_state": record.state.value,
            }

        # Bind dispatch to the immutable identity adjudicated and reserved.
        # Defaults mean "use the recorded values", never "skip budget checks".
        recorded_resource = record.resource_id or None
        if resource_id is None:
            resource_id = recorded_resource
        elif resource_id != recorded_resource:
            return {"dispatched": False, "reason": "Dispatch resource_id does not match the adjudicated operation."}
        try:
            supplied_amount = float(amount)
            recorded_amount = float(record.amount)
        except (TypeError, ValueError, OverflowError):
            return {"dispatched": False, "reason": "Dispatch amount is invalid."}
        if not math.isfinite(supplied_amount) or supplied_amount < 0:
            return {"dispatched": False, "reason": "Dispatch amount must be finite and non-negative."}
        if supplied_amount == 0 and recorded_amount > 0:
            supplied_amount = recorded_amount
        if supplied_amount != recorded_amount:
            return {"dispatched": False, "reason": "Dispatch amount does not match the adjudicated operation."}
        amount = supplied_amount

        if (
            self.consensus_backend is not None
            and resource_id and amount > 0
            and not self.registry.is_persistent
        ):
            return {
                "dispatched": False,
                "reason": "Durable backend dispatch requires a persistent operation registry; provider execution blocked.",
                "operation_state": record.state.value,
                "recommendation": "configure_persistent_registry",
            }

        # 5.7.6: what dispatch() settled before taking the gate is re-read
        # here from the current record, without I/O, so that no claim passes
        # on terms that changed in between.
        if record.state == OperationState.RESERVED:
            if ((record.composed_decision == Decision.FLAG.value or record.flag_conditions)
                    and not self._flag_conditions_acknowledged(record)):
                return {
                    "dispatched": False,
                    "reason": "FLAG conditions not acknowledged; dispatch refused (RFC-0 §4).",
                    "operation_state": record.state.value,
                    "flag_conditions": list(record.flag_conditions),
                }
            if self.policy.revalidate_mandate_at_dispatch:
                changes = self._revalidate_mandate_at_dispatch(record, amount)
                if changes:
                    return {
                        "dispatched": False,
                        "mandate_revalidation_failed": True,
                        "mandate_changes": changes,
                        "reason": (
                            "MANDATE_REVALIDATION_FAILED under the dispatch gate ("
                            + "; ".join(changes)
                            + "); no claim made. Call dispatch() again to settle fail-closed."
                        ),
                        "operation_state": record.state.value,
                    }

        # QUARANTINE_LOCK blocks dispatch, including operations reserved before
        # the lock was activated. Check before the durable dispatch transition.
        if self._quarantine_blocks():
            return {
                "dispatched": False,
                "reason": "QUARANTINE_LOCK is active; dispatch is blocked.",
                "operation_state": record.state.value,
            }

        success, _prev = self.registry.transition_to(
            idempotency_key, OperationState.DISPATCHING,
            claimed_by=self._worker_id,
            commit_gate_verified=commit_gate_verified,
        )
        if not success:
            existing = self.registry.get(idempotency_key)
            return {
                "dispatched": False,
                "reason": f"Cannot dispatch — operation is in state {existing.state.value}.",
                "operation_state": existing.state.value,
            }

        if resource_id and amount > 0 and self.consensus_backend is not None:
            # The durable claim linearizes here. No external backend I/O under
            # the quarantine gate; dispatch() settles this claim afterwards.
            return {
                "_backend_claimed": True,
                "dispatched": False,
                "idempotency_key": idempotency_key,
                "operation_state": OperationState.DISPATCHING.value,
            }

        if resource_id and amount > 0 and self.consensus_backend is None:
            resource = self.membrane.state.resources.get(resource_id)
            if resource:
                with self.membrane.state.get_lock(resource_id):
                    # Revalidate budget at dispatch if policy requires it.
                    # The reservation was taken at adjudication time (step 6a);
                    # check it is still held (not released by a concurrent
                    # fail/expire on this operation).  available already
                    # excludes our own reservation, so we check reserved
                    # directly: if reserved < amount, the reservation is gone.
                    if self.policy.revalidate_budget_at_dispatch:
                        if resource.reserved < amount:
                            self.registry.transition_to(idempotency_key, OperationState.FAILED)
                            return {
                                "dispatched": False,
                                "reason": (
                                    "Budget revalidation at dispatch failed — "
                                    f"reservation lost (reserved {resource.reserved:.2f} < required {amount}). "
                                    "Fail-closed."
                                ),
                                "operation_state": OperationState.FAILED.value,
                            }
                    committed = resource.commit(amount)
                if committed:
                    try:
                        flags_ok = self.registry.set_budget_flags(
                            idempotency_key,
                            budget_reserved=False,
                            budget_committed=True,
                        )
                    except Exception:
                        flags_ok = False
                    if not flags_ok:
                        # The provider has not been called yet. Restore the
                        # accounting to its pre-dispatch reservation and leave
                        # DISPATCHING for explicit reconciliation; do not allow
                        # an external effect when ownership persistence failed.
                        with self.membrane.state.get_lock(resource_id):
                            resource.refund(amount)
                            restored = resource.reserve(amount)
                        if not restored:
                            return {
                                "dispatched": False,
                                "reason": "Budget ownership persistence failed and reservation restoration failed; manual reconciliation required.",
                                "operation_state": OperationState.DISPATCHING.value,
                            }
                        return {
                            "dispatched": False,
                            "reason": "Budget ownership persistence failed; provider execution blocked and reservation restored.",
                            "operation_state": OperationState.DISPATCHING.value,
                        }
                else:
                    # Commit failed — the reservation is no longer held
                    # (e.g. released by a concurrent fail/expire). Fail-closed.
                    self.registry.transition_to(idempotency_key, OperationState.FAILED)
                    return {
                        "dispatched": False,
                        "reason": "Budget commit failed — reservation lost. Fail-closed.",
                        "operation_state": OperationState.FAILED.value,
                    }

        return {
            "dispatched": True,
            "idempotency_key": idempotency_key,
            "operation_state": OperationState.DISPATCHING.value,
        }

    def _complete_claimed_backend_dispatch(
        self, idempotency_key: str, resource_id: str | None, amount: float,
    ) -> dict[str, Any]:
        """Settle a claimed durable dispatch without the global quarantine gate.

        Only dispatch() calls this after it has verified DISPATCHING ownership
        under the per-operation settlement lock. No provider effect may occur
        unless durable budget ownership is committed and persisted.
        """
        if resource_id and amount > 0 and self.consensus_backend is not None:
            current = self.registry.get(idempotency_key)
            reservation_id = current.reservation_id if current else ""
            if not reservation_id:
                return {
                    "dispatched": False,
                    "reason": "Durable reservation identity missing; provider execution blocked.",
                    "operation_state": OperationState.DISPATCHING.value,
                }
            try:
                backend_state = self.consensus_backend.get_state(resource_id)
                if self.policy.revalidate_budget_at_dispatch:
                    reservation = next(
                        (item for item in backend_state.reservations
                         if item.get("reservation_id") == reservation_id),
                        None,
                    )
                    if not reservation or reservation.get("state") != "RESERVED":
                        return {
                            "dispatched": False,
                            "reason": "Durable reservation revalidation failed; provider execution blocked and reconciliation required.",
                            "operation_state": OperationState.DISPATCHING.value,
                            "recommendation": "human_review",
                        }
                commit_result = self.consensus_backend.commit(reservation_id)
            except Exception:
                return {
                    "dispatched": False,
                    "reason": "Durable backend commit failed or is uncertain; reconciliation required.",
                    "operation_state": OperationState.DISPATCHING.value,
                }
            if getattr(commit_result, "status", None) not in ("COMMITTED", "ALREADY_COMMITTED"):
                return {
                    "dispatched": False,
                    "reason": "Durable backend refused commit; provider execution blocked and reconciliation required.",
                    "operation_state": OperationState.DISPATCHING.value,
                    "recommendation": "human_review",
                }
            try:
                flags_ok = self.registry.set_budget_flags(
                    idempotency_key,
                    budget_reserved=False,
                    budget_committed=True,
                    reservation_id=reservation_id,
                )
            except Exception:
                flags_ok = False
            if not flags_ok:
                # Do not call the provider. Leave DISPATCHING and the durable
                # COMMITTED reservation for explicit reconciliation/refund.
                return {
                    "dispatched": False,
                    "reason": "Budget ownership persistence failed after durable commit; provider execution blocked, reconciliation required.",
                    "operation_state": OperationState.DISPATCHING.value,
                }

        return {
            "dispatched": True,
            "idempotency_key": idempotency_key,
            "operation_state": OperationState.DISPATCHING.value,
        }

    def dispatch_and_execute(
        self,
        idempotency_key: str,
        resource_id: str | None = None,
        amount: float = 0.0,
        metadata: dict[str, Any] | None = None,
        *,
        payload: dict[str, Any] | None = None,
        flag_conditions_met: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        # dispatch() linearizes the claim against QUARANTINE_LOCK. Do not hold
        # the global quarantine lock while performing external I/O: a hung
        # provider must not prevent quarantine activation or unrelated dispatches.
        # A claim that already returned dispatched=True is not cancelled if
        # quarantine activates before provider.execute(). New claims are blocked.
        return self._dispatch_and_execute_under_quarantine_lock(
            idempotency_key, resource_id, amount, metadata, payload=payload,
            flag_conditions_met=flag_conditions_met,
        )

    def _dispatch_and_execute_under_quarantine_lock(
        self,
        idempotency_key: str,
        resource_id: str | None = None,
        amount: float = 0.0,
        metadata: dict[str, Any] | None = None,
        *,
        payload: dict[str, Any] | None = None,
        flag_conditions_met: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Atomically claim dispatch, then call the external provider.

        This is the production dispatch path. It:
        1. Atomically transitions RESERVED → DISPATCHING (the durable claim)
        2. Calls the provider's execute() with the idempotency key
        3. Transitions to CONFIRMED, FAILED, or UNKNOWN based on the result

        Per doctrine v7.1: a timeout does NOT prove no effect occurred.
        Provider timeouts and ambiguous responses produce UNKNOWN, not FAILED.

        If no provider is configured, falls back to the manual dispatch()
        method (state transition only, no external call).
        """
        # The full verification of the ledger (R13) runs inside dispatch(),
        # under the quarantine gate, immediately before the DISPATCHING claim:
        # every path to a claim goes through it (this method, a direct
        # dispatch() call, the SDK client). See _refuse_on_ledger_integrity.

        # Freeze the exact JSON payload before either verification or provider
        # use so another thread cannot mutate the caller's mapping between the
        # digest check and the external call.
        actual_payload = metadata if payload is None else payload
        provider_metadata = actual_payload
        record = self.registry.get(idempotency_key)
        if record is not None and record.commit_gate_required:
            from ..experimental.ep02_commit_authorization import canonical_payload_bytes

            try:
                provider_metadata = json.loads(
                    canonical_payload_bytes(actual_payload).decode("utf-8")
                )
            except (TypeError, ValueError) as exc:
                return {
                    "dispatched": False,
                    "reason": f"Provider payload is invalid: {exc}.",
                    "operation_state": record.state.value,
                }

        # 1. Atomic claim
        dispatch_result = self.dispatch(
            idempotency_key, resource_id, amount,
            payload=provider_metadata,
            flag_conditions_met=flag_conditions_met,
            _active_provider_call=self.provider is not None,
        )
        if not dispatch_result.get("dispatched"):
            return dispatch_result

        # 2. If no provider, return the manual dispatch result
        if self.provider is None:
            return dispatch_result

        # If reconciliation was already requested, the durable state claim
        # still governs; active marker only prevents a concurrent live execute.
        # 3. Get the operation record for metadata
        record = self.registry.get(idempotency_key)
        if record is None:
            with self._active_dispatches_lock:
                self._active_dispatches.discard(idempotency_key)
            return dispatch_result
        # The provider receives the exact amount bound to the durable record,
        # not an omitted/default or caller-substituted amount.
        amount = float(record.amount)
        resource_id = record.resource_id or None

        # 4. Call the provider
        from ..operations.provider import ProviderOutcome
        try:
            prov_result = self.provider.execute(
                idempotency_key=idempotency_key,
                action_type=record.action_type or "payment",
                amount=amount,
                agent_id=record.agent_id,
                metadata=provider_metadata,
            )
        except Exception as e:
            # Provider call threw an exception — fail closed to UNKNOWN
            try:
                self.registry.transition_to(idempotency_key, OperationState.UNKNOWN)
            finally:
                with self._active_dispatches_lock:
                    self._active_dispatches.discard(idempotency_key)
            return {
                "dispatched": True,
                "executed": False,
                "idempotency_key": idempotency_key,
                "operation_state": OperationState.UNKNOWN.value,
                "provider_error": str(e),
                "reason": "Provider call failed — outcome UNKNOWN (fail-closed).",
            }

        # 5. Transition based on provider outcome
        if prov_result.outcome == ProviderOutcome.CONFIRMED:
            try:
                confirmed = self.confirm(idempotency_key)
            finally:
                with self._active_dispatches_lock:
                    self._active_dispatches.discard(idempotency_key)
            if not confirmed:
                latest = self.registry.get(idempotency_key)
                return {
                    "dispatched": True,
                    "executed": True,
                    "idempotency_key": idempotency_key,
                    "operation_state": latest.state.value if latest else "not_found",
                    "provider_reference": prov_result.provider_reference,
                    "provider_latency_ms": prov_result.latency_ms,
                    "recommendation": "human_review",
                    "reason": "Provider confirmed the external effect, but durable budget ownership or terminal state could not be confirmed.",
                }
            return {
                "dispatched": True,
                "executed": True,
                "idempotency_key": idempotency_key,
                "operation_state": OperationState.CONFIRMED.value,
                "provider_reference": prov_result.provider_reference,
                "provider_latency_ms": prov_result.latency_ms,
            }
        elif prov_result.outcome == ProviderOutcome.FAILED:
            if _has_hypothesis_violated_signal(prov_result):
                try:
                    self.registry.transition_to(idempotency_key, OperationState.UNKNOWN)
                except Exception:
                    # Contradictory evidence remains HOLD even if the durable
                    # state transition needs operator recovery.
                    pass
                finally:
                    with self._active_dispatches_lock:
                        self._active_dispatches.discard(idempotency_key)
                rationale = (
                    "I5 HOLD: provider reported FAILED while S3 reported "
                    "HYPOTHESIS_VIOLATED; evidence conflicts, so no failure "
                    "settlement or budget refund was applied."
                )
                self.ledger.append(
                    kernel="I5", decision=Decision.HOLD.value,
                    rationale=rationale, passage_id=record.passage_id,
                    idempotency_key=idempotency_key,
                )
                latest = self.registry.get(idempotency_key)
                return {
                    "dispatched": True,
                    "executed": None,
                    "idempotency_key": idempotency_key,
                    "decision": Decision.HOLD.value,
                    "composed_decision": Decision.HOLD.value,
                    "operation_state": latest.state.value if latest else OperationState.UNKNOWN.value,
                    "provider_error": prov_result.error,
                    "provider_latency_ms": prov_result.latency_ms,
                    "recommendation": "human_review",
                    "reason": rationale,
                }
            try:
                # The runtime observed the provider's answer itself: this is
                # the runtime channel, recorded as such (5.7.6).
                failed = self._fail_with_locks(
                    idempotency_key, resource_id, amount,
                    non_execution_attested=NonExecutionAttestation(
                        idempotency_key=idempotency_key,
                        source="provider",
                        attestation=(f"provider outcome FAILED; reference="
                                     f"{prov_result.provider_reference or '-'}; "
                                     f"error={prov_result.error or '-'}"),
                        attested_at=_utc_now_iso(),
                    ).verified("provider-channel"),
                    runtime_channel=True,
                )
            finally:
                with self._active_dispatches_lock:
                    self._active_dispatches.discard(idempotency_key)
            if not failed:
                latest = self.registry.get(idempotency_key)
                return {
                    "dispatched": True,
                    "executed": False,
                    "idempotency_key": idempotency_key,
                    "operation_state": latest.state.value if latest else "not_found",
                    "provider_error": prov_result.error,
                    "provider_latency_ms": prov_result.latency_ms,
                    "recommendation": "human_review",
                    "reason": "Provider confirmed failure, but durable budget compensation or registry transition remains unresolved.",
                }
            return {
                "dispatched": True,
                "executed": False,
                "idempotency_key": idempotency_key,
                "operation_state": OperationState.FAILED.value,
                "provider_error": prov_result.error,
                "provider_latency_ms": prov_result.latency_ms,
            }
        elif prov_result.outcome == ProviderOutcome.PENDING:
            # Pending — stay in DISPATCHING, caller should poll/reconcile
            with self._active_dispatches_lock:
                self._active_dispatches.discard(idempotency_key)
            return {
                "dispatched": True,
                "executed": False,
                "idempotency_key": idempotency_key,
                "operation_state": OperationState.DISPATCHING.value,
                "provider_reference": prov_result.provider_reference,
                "provider_latency_ms": prov_result.latency_ms,
                "reason": "Provider returned PENDING — reconcile later.",
            }
        else:
            # UNKNOWN — timeout or ambiguous response
            self.registry.transition_to(idempotency_key, OperationState.UNKNOWN)
            with self._active_dispatches_lock:
                self._active_dispatches.discard(idempotency_key)
            return {
                "dispatched": True,
                "executed": False,
                "idempotency_key": idempotency_key,
                "operation_state": OperationState.UNKNOWN.value,
                "provider_error": prov_result.error,
                "provider_latency_ms": prov_result.latency_ms,
                "reason": "Provider returned UNKNOWN — reconciliation required.",
            }

    def reconcile_unknown(
        self,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Reconcile UNKNOWN or interrupted DISPATCHING with the provider.

        DISPATCHING is included because a process may crash after claiming
        dispatch but before recording the external outcome. The provider's
        reconcile() determines whether the external effect occurred.

        If the provider resolves the outcome:
        - CONFIRMED → transition to CONFIRMED
        - FAILED → transition to FAILED (with budget release)

        If the provider cannot resolve:
        - Stay in UNKNOWN (human review required)

        If no provider is configured, returns a recommendation for human review.
        """
        record = self.registry.get(idempotency_key)
        if record is None:
            return {"reconciled": False, "reason": "Operation not registered."}

        with self._active_dispatches_lock:
            if idempotency_key in self._active_dispatches:
                return {
                    "reconciled": False,
                    "idempotency_key": idempotency_key,
                    "operation_state": record.state.value,
                    "reason": "Provider execution is still active; reconciliation deferred to avoid racing the external effect.",
                    "recommendation": "retry_reconciliation",
                }

        if record.state not in (OperationState.UNKNOWN, OperationState.DISPATCHING):
            return {
                "reconciled": False,
                "operation_state": record.state.value,
                "reason": f"Operation is in state {record.state.value}, not DISPATCHING or UNKNOWN.",
            }

        if self.provider is None:
            return {
                "reconciled": False,
                "idempotency_key": idempotency_key,
                "operation_state": record.state.value,
                "recommendation": "human_review",
                "reason": "No provider configured — manual reconciliation required.",
            }

        if (
            self.consensus_backend is not None
            and record.resource_id and record.amount > 0
            and not self.registry.is_persistent
        ):
            return {
                "reconciled": False,
                "idempotency_key": idempotency_key,
                "operation_state": record.state.value,
                "recommendation": "configure_persistent_registry",
                "reason": "Durable backend reconciliation requires a persistent operation registry; provider reconciliation was not called.",
            }

        from ..operations.provider import ProviderOutcome
        try:
            recon = self.provider.reconcile(
                idempotency_key=idempotency_key,
            )
        except Exception as e:
            current = self.registry.get(idempotency_key)
            if current is not None and current.state == OperationState.DISPATCHING:
                self.registry.transition_to(idempotency_key, OperationState.UNKNOWN)
                current = self.registry.get(idempotency_key)
            return {
                "reconciled": False,
                "idempotency_key": idempotency_key,
                "operation_state": current.state.value if current else "not_found",
                "provider_error": str(e),
                "recommendation": "human_review",
            }

        # Provider calls may run concurrently (some providers require that for
        # bounded reconciliation); serialize only the settlement phase.
        with self._settlement_lock:
            with self.registry.settlement_lock(
                    idempotency_key,
                    require_persistent=self.consensus_backend is not None,
                ):
                current = self.registry.get(idempotency_key)
                if current is None or current.state not in (
                    OperationState.UNKNOWN, OperationState.DISPATCHING,
                ):
                    return {
                        "reconciled": False,
                        "idempotency_key": idempotency_key,
                        "operation_state": current.state.value if current else "not_found",
                        "reason": "Reconciliation lost the terminal-state race; budget unchanged.",
                    }
                if recon.resolved and recon.outcome == ProviderOutcome.FAILED and _has_hypothesis_violated_signal(recon):
                    if current.state == OperationState.DISPATCHING:
                        try:
                            self.registry.transition_to(idempotency_key, OperationState.UNKNOWN)
                        except Exception:
                            pass
                    rationale = (
                        "I5 HOLD: provider reconciliation reported FAILED while S3 "
                        "reported HYPOTHESIS_VIOLATED; preserve committed budget "
                        "and require human review."
                    )
                    self.ledger.append(
                        kernel="I5", decision=Decision.HOLD.value,
                        rationale=rationale, passage_id=current.passage_id,
                        idempotency_key=idempotency_key,
                    )
                    latest = self.registry.get(idempotency_key)
                    return {
                        "reconciled": False,
                        "idempotency_key": idempotency_key,
                        "decision": Decision.HOLD.value,
                        "operation_state": latest.state.value if latest else OperationState.UNKNOWN.value,
                        "recommendation": "human_review",
                        "reason": rationale,
                    }
                if recon.resolved and recon.outcome == ProviderOutcome.CONFIRMED:
                    if self.consensus_backend is not None and record.amount > 0:
                        try:
                            if not record.resource_id:
                                raise RuntimeError("Budgeted operation has no durable resource identity")
                            backend_state = self.consensus_backend.get_state(record.resource_id)
                            durable_reservation = next(
                                (item for item in backend_state.reservations
                                 if record.reservation_id
                                 and item.get("reservation_id") == record.reservation_id),
                                None,
                            )
                            if durable_reservation is None and not record.reservation_id:
                                # Recover the crash window between backend reservation
                                # and persistence of reservation_id in the operation registry.
                                durable_reservation = next(
                                    (item for item in backend_state.reservations
                                     if item.get("key") == idempotency_key),
                                    None,
                                )
                            if durable_reservation is None:
                                raise RuntimeError("Durable reservation identity cannot be recovered or verified")
                            if float(durable_reservation.get("amount", -1)) != float(record.amount):
                                raise RuntimeError("Durable reservation amount does not match the operation record")
                            reservation_id = durable_reservation.get("reservation_id")
                            if not reservation_id:
                                raise RuntimeError("Recovered durable reservation has no reservation ID")
                            # Persist recovered identity before changing reservation state.
                            if record.reservation_id != reservation_id:
                                identity_ok = self.registry.set_budget_flags(
                                    idempotency_key,
                                    reservation_id=reservation_id,
                                )
                                if not identity_ok:
                                    raise RuntimeError("Could not persist recovered durable reservation identity")
                            if durable_reservation.get("state") == "RESERVED":
                                committed_result = self.consensus_backend.commit(reservation_id)
                                if getattr(committed_result, "status", None) not in ("COMMITTED", "ALREADY_COMMITTED"):
                                    raise RuntimeError("Could not commit reservation for confirmed external effect")
                            elif durable_reservation.get("state") != "COMMITTED":
                                raise RuntimeError(
                                    f"External effect confirmed but durable reservation is {durable_reservation.get('state')}"
                                )
                            flags_ok = self.registry.set_budget_flags(
                                idempotency_key,
                                budget_reserved=False,
                                budget_committed=True,
                                reservation_id=reservation_id,
                            )
                            if not flags_ok:
                                raise RuntimeError("Could not persist committed budget ownership")
                        except Exception as exc:
                            return {
                                "reconciled": False, "idempotency_key": idempotency_key,
                                "operation_state": record.state.value,
                                "recommendation": "human_review",
                                "reason": f"External effect confirmed but budget ownership needs reconciliation: {exc}",
                            }
                    success, _prev = self.registry.transition_to(idempotency_key, OperationState.CONFIRMED)
                    if not success:
                        current = self.registry.get(idempotency_key)
                        return {
                            "reconciled": False,
                            "idempotency_key": idempotency_key,
                            "operation_state": current.state.value if current else "not_found",
                            "reason": "Confirmation lost the terminal-state race.",
                        }
                    return {
                        "reconciled": True,
                        "idempotency_key": idempotency_key,
                        "operation_state": OperationState.CONFIRMED.value,
                        "provider_reference": recon.provider_reference,
                    }
                elif recon.resolved and recon.outcome == ProviderOutcome.FAILED:
                    # T7: persist the proof of non-execution before any settlement.
                    try:
                        attested = self.registry.record_non_execution_attested(
                            idempotency_key,
                            NonExecutionAttestation(
                                idempotency_key=idempotency_key,
                                source="provider-reconciliation",
                                attestation=(f"reconciliation outcome FAILED; reference="
                                             f"{recon.provider_reference or '-'}"),
                                attested_at=_utc_now_iso(),
                            ).verified("provider-reconciliation-channel"),
                        )
                    except (ValueError, RuntimeError):
                        attested = False
                    if not attested:
                        return {
                            "reconciled": False,
                            "idempotency_key": idempotency_key,
                            "operation_state": current.state.value,
                            "reason": "Non-execution attestation could not be persisted; budget unchanged.",
                        }
                    record = self.registry.get(idempotency_key) or record
                    committed = record.budget_committed
                    if self.consensus_backend is not None and record.amount > 0:
                        # The backend is authoritative when a crash occurred between
                        # reservation/commit and registry ownership persistence.
                        try:
                            if not record.resource_id:
                                raise RuntimeError("Budgeted operation has no durable resource identity")
                            backend_state = self.consensus_backend.get_state(record.resource_id)
                            durable_reservation = next(
                                (item for item in backend_state.reservations
                                 if record.reservation_id
                                 and item.get("reservation_id") == record.reservation_id),
                                None,
                            )
                            if durable_reservation is None and not record.reservation_id:
                                durable_reservation = next(
                                    (item for item in backend_state.reservations
                                     if item.get("key") == idempotency_key),
                                    None,
                                )
                            if durable_reservation is None:
                                raise RuntimeError("Durable reservation identity cannot be recovered or verified")
                            if float(durable_reservation.get("amount", -1)) != float(record.amount):
                                raise RuntimeError("Durable reservation amount does not match the operation record")
                            reservation_id = durable_reservation.get("reservation_id")
                            if not reservation_id:
                                raise RuntimeError("Recovered durable reservation has no reservation ID")
                            if record.reservation_id != reservation_id:
                                identity_ok = self.registry.set_budget_flags(
                                    idempotency_key,
                                    reservation_id=reservation_id,
                                )
                                if not identity_ok:
                                    raise RuntimeError("Could not persist recovered durable reservation identity")
                        except Exception:
                            durable_reservation = None
                            reservation_id = None
                        if durable_reservation is None:
                            return {
                                "reconciled": False, "idempotency_key": idempotency_key,
                                "operation_state": record.state.value,
                                "recommendation": "human_review",
                                "reason": "Durable reservation identity cannot be verified.",
                            }
                        if durable_reservation.get("state") == "COMMITTED":
                            committed = True
                        elif durable_reservation.get("state") == "RESERVED":
                            committed = False
                        elif durable_reservation.get("state") in ("REFUNDED", "RELEASED"):
                            # A prior attempt may have compensated before crashing.
                            committed = durable_reservation.get("state") == "REFUNDED"
                        else:
                            return {
                                "reconciled": False, "idempotency_key": idempotency_key,
                                "operation_state": record.state.value,
                                "recommendation": "human_review",
                                "reason": "Durable reservation is in a non-compensable state.",
                            }
                        # Provider reconciliation is authoritative non-execution proof:
                        # compensate the durable reservation before terminalizing the registry.
                        try:
                            compensation = (
                                self.consensus_backend.refund(reservation_id)
                                if committed
                                else self.consensus_backend.release(reservation_id)
                            )
                        except Exception as exc:
                            return {
                                "reconciled": False, "idempotency_key": idempotency_key,
                                "operation_state": record.state.value,
                                "recommendation": "human_review",
                                "reason": f"Durable budget compensation failed: {exc}",
                            }
                        accepted = (
                            ("REFUNDED", "ALREADY_REFUNDED")
                            if committed else ("RELEASED", "ALREADY_RELEASED")
                        )
                        if getattr(compensation, "status", None) not in accepted:
                            return {
                                "reconciled": False, "idempotency_key": idempotency_key,
                                "operation_state": record.state.value,
                                "recommendation": "human_review",
                                "reason": "Durable budget compensation was not confirmed; registry remains unresolved.",
                            }
                        try:
                            flags_ok = self.registry.set_budget_flags(
                                idempotency_key,
                                budget_reserved=False,
                                budget_committed=False,
                                reservation_id=reservation_id,
                            )
                        except Exception:
                            flags_ok = False
                        if not flags_ok:
                            return {
                                "reconciled": False, "idempotency_key": idempotency_key,
                                "operation_state": record.state.value,
                                "recommendation": "human_review",
                                "reason": "Compensation durable but registry ownership update failed; replay reconciliation.",
                            }
                        success, _prev = self.registry.transition_to(
                            idempotency_key, OperationState.FAILED,
                        )
                        if not success:
                            latest = self.registry.get(idempotency_key)
                            if latest and latest.state == OperationState.FAILED:
                                return {
                                    "reconciled": True, "idempotency_key": idempotency_key,
                                    "operation_state": OperationState.FAILED.value,
                                    "budget_compensation": getattr(compensation, "status", None),
                                }
                            return {
                                "reconciled": False, "idempotency_key": idempotency_key,
                                "operation_state": latest.state.value if latest else "not_found",
                                "reason": "Compensation completed but terminal transition needs replay.",
                            }
                        return {
                            "reconciled": True, "idempotency_key": idempotency_key,
                            "operation_state": OperationState.FAILED.value,
                            "budget_compensation": getattr(compensation, "status", None),
                        }
                    if record.resource_id and record.amount > 0:
                        resource = self.membrane.state.resources.get(record.resource_id)
                        if resource is None:
                            return {
                                "reconciled": False, "idempotency_key": idempotency_key,
                                "operation_state": OperationState.UNKNOWN.value,
                                "recommendation": "human_review",
                                "reason": "Budget resource is missing; cannot safely release or refund.",
                            }
                        # Serialize competing reconciliation attempts with the budget
                        # mutation. Only the worker that wins UNKNOWN -> FAILED may
                        # release/refund, and accounting must cover the exact amount.
                        with self.membrane.state.get_lock(record.resource_id):
                            current = self.registry.get(idempotency_key)
                            if current is None or current.state not in (OperationState.UNKNOWN, OperationState.DISPATCHING):
                                return {
                                    "reconciled": False, "idempotency_key": idempotency_key,
                                    "operation_state": current.state.value if current else "not_found",
                                    "reason": "Reconciliation lost the terminal-state race; budget unchanged.",
                                }
                            # A resource-wide balance is not evidence that this operation
                            # owns any part of it. An unowned UNKNOWN operation may
                            # terminalize on authoritative provider no-effect evidence,
                            # but it must not release another operation's reservation.
                            if not current.budget_reserved and not current.budget_committed:
                                success, _ = self.registry.transition_to(
                                    idempotency_key, OperationState.FAILED,
                                )
                                if not success:
                                    latest = self.registry.get(idempotency_key)
                                    return {
                                        "reconciled": False,
                                        "idempotency_key": idempotency_key,
                                        "operation_state": latest.state.value if latest else "not_found",
                                        "reason": "Reconciliation transition lost; foreign budget unchanged.",
                                    }
                                return {
                                    "reconciled": True,
                                    "idempotency_key": idempotency_key,
                                    "operation_state": OperationState.FAILED.value,
                                    "provider_reference": recon.provider_reference,
                                    "reason": "Provider confirmed no effect; operation held no budget reservation.",
                                }
                            committed = current.budget_committed
                            available_accounting = resource.consumed if committed else resource.reserved
                            if available_accounting < record.amount:
                                return {
                                    "reconciled": False, "idempotency_key": idempotency_key,
                                    "operation_state": OperationState.UNKNOWN.value,
                                    "recommendation": "human_review",
                                    "reason": "Budget accounting is inconsistent; refusing automatic refund/release.",
                                }
                            # Persist ownership release before mutating accounting.
                            # If this write fails, leave both the unresolved state and
                            # the budget untouched; never release capacity while the
                            # durable record still claims to own it.
                            flags_changed = committed or record.budget_reserved
                            if flags_changed:
                                try:
                                    flags_ok = self.registry.set_budget_flags(
                                        idempotency_key,
                                        budget_reserved=False,
                                        budget_committed=False,
                                    )
                                except Exception:
                                    flags_ok = False
                                if not flags_ok:
                                    return {
                                        "reconciled": False, "idempotency_key": idempotency_key,
                                        "operation_state": current.state.value,
                                        "recommendation": "human_review",
                                        "reason": "Could not persist budget ownership release; budget unchanged.",
                                    }
                            success, _ = self.registry.transition_to(idempotency_key, OperationState.FAILED)
                            if not success:
                                # Restore the ownership flags if the terminal transition
                                # lost a race. No accounting mutation has happened yet.
                                if flags_changed:
                                    try:
                                        self.registry.set_budget_flags(
                                            idempotency_key,
                                            budget_reserved=not committed,
                                            budget_committed=committed,
                                        )
                                    except Exception:
                                        pass
                                latest = self.registry.get(idempotency_key)
                                return {
                                    "reconciled": False, "idempotency_key": idempotency_key,
                                    "operation_state": latest.state.value if latest else "not_found",
                                    "reason": "Reconciliation transition lost; budget unchanged.",
                                }
                            if committed:
                                resource.refund(record.amount)
                            else:
                                resource.release(record.amount)
                    else:
                        success, _ = self.registry.transition_to(idempotency_key, OperationState.FAILED)
                        if not success:
                            current = self.registry.get(idempotency_key)
                            return {
                                "reconciled": False, "idempotency_key": idempotency_key,
                                "operation_state": current.state.value if current else "not_found",
                                "reason": "Reconciliation transition lost; no budget mutation performed.",
                            }
                    return {
                        "reconciled": True,
                        "idempotency_key": idempotency_key,
                        "operation_state": OperationState.FAILED.value,
                        "provider_reference": recon.provider_reference,
                        "reason": "Provider confirmed no effect occurred.",
                    }
                else:
                    # An interrupted dispatch that remains indeterminate becomes UNKNOWN;
                    # it must not remain mislabeled DISPATCHING after reconciliation.
                    current = self.registry.get(idempotency_key)
                    if current is not None and current.state == OperationState.DISPATCHING:
                        self.registry.transition_to(idempotency_key, OperationState.UNKNOWN)
                        current = self.registry.get(idempotency_key)
                    return {
                        "reconciled": False,
                        "idempotency_key": idempotency_key,
                        "operation_state": current.state.value if current else "not_found",
                        "provider_reference": recon.provider_reference,
                        "provider_error": recon.error,
                        "recommendation": "human_review",
                        "reason": "Provider could not determine outcome.",
                    }
        
    def confirm(self, idempotency_key: str) -> bool:
        """Serialize confirmation with failure compensation across local processes."""
        with self._settlement_lock:
            try:
                with self.registry.settlement_lock(
                    idempotency_key,
                    require_persistent=self.consensus_backend is not None,
                ):
                    return self._confirm_under_settlement_lock(idempotency_key)
            except Exception:
                # An unavailable durable coordination lock is an indeterminate
                # settlement: fail closed without touching budget ownership.
                return False

    def _confirm_under_settlement_lock(self, idempotency_key: str) -> bool:
        """Confirm an external effect only after durable budget ownership agrees."""
        record = self.registry.get(idempotency_key)
        if record is None or record.state not in (
            OperationState.DISPATCHING, OperationState.UNKNOWN,
        ):
            # Validate the lifecycle transition before committing budget.
            # Otherwise an illegal confirm from RESERVED could consume budget
            # even though the registry correctly rejects CONFIRMED.
            return False
        if self.consensus_backend is not None and record.amount > 0:
            if not record.resource_id or not record.reservation_id:
                return False
            try:
                state = self.consensus_backend.get_state(record.resource_id)
                reservation = next(
                    (item for item in state.reservations
                     if item.get("reservation_id") == record.reservation_id),
                    None,
                )
                if reservation is None:
                    return False
                reservation_state = reservation.get("state")
                if reservation_state == "RESERVED":
                    committed = self.consensus_backend.commit(record.reservation_id)
                    if getattr(committed, "status", None) not in ("COMMITTED", "ALREADY_COMMITTED"):
                        return False
                elif reservation_state != "COMMITTED":
                    # A released/refunded reservation cannot back a newly
                    # confirmed external effect.
                    return False
                flags_ok = self.registry.set_budget_flags(
                    idempotency_key,
                    budget_reserved=False,
                    budget_committed=True,
                    reservation_id=record.reservation_id,
                )
                if not flags_ok:
                    return False
            except Exception:
                return False
        success, _prev = self.registry.transition_to(idempotency_key, OperationState.CONFIRMED)
        return success

    def fail(self, idempotency_key: str, resource_id: str | None = None, amount: float = 0.0, *,
             non_execution_attested=None, authoritative_non_execution: bool = False) -> bool:
        """Serialize failure compensation with confirmation for this governor instance.

        Once the attempt was dispatched (DISPATCHING or UNKNOWN), only a
        ``NonExecutionAttestation`` (or its dict form) closes it as FAILED and
        gives the budget back; it is persisted on the operation first. Without
        it, DISPATCHING moves to UNKNOWN, the budget is kept, and False is
        returned. ``authoritative_non_execution`` (a bare boolean) is no longer
        accepted as proof and is kept only so that older callers do not break.

        Since 5.7.6 a caller's attestation is proof only once the governor's
        ``attestation_verifier`` authenticates its signature under a declared
        key. The attestation is then recorded with ``verified_by_runtime=True``
        and the verification method. Without a verifier, or when the signature
        does not verify, or when the caller asserts ``verified_by_runtime``
        itself, the attestation is refused: a DISPATCHING operation moves to
        UNKNOWN with its budget kept, the refusal is ledgered, and False is
        returned. Only the runtime's own observation of the provider
        (``dispatch_and_execute``, ``reconcile_unknown``) bypasses the verifier.
        """
        if authoritative_non_execution and non_execution_attested is None:
            import warnings
            warnings.warn(
                "fail(authoritative_non_execution=True) is not proof of non-execution; "
                "pass non_execution_attested=NonExecutionAttestation(...)",
                DeprecationWarning, stacklevel=2,
            )
        return self._fail_with_locks(
            idempotency_key, resource_id, amount,
            non_execution_attested=non_execution_attested, runtime_channel=False,
        )

    def _fail_with_locks(self, idempotency_key: str, resource_id: str | None, amount: float, *,
                         non_execution_attested, runtime_channel: bool) -> bool:
        """Lock wrapper shared by ``fail()`` and the runtime's provider channel."""
        with self._settlement_lock:
            try:
                with self.registry.settlement_lock(
                    idempotency_key,
                    require_persistent=self.consensus_backend is not None,
                ):
                    return self._fail_under_settlement_lock(
                        idempotency_key, resource_id, amount,
                        non_execution_attested=non_execution_attested,
                        runtime_channel=runtime_channel,
                    )
            except Exception:
                # Do not mutate budget if cross-process coordination is uncertain.
                return False

    def _refuse_attestation_after_dispatch(
        self, idempotency_key: str, record: OperationRecord, why: str,
    ) -> bool:
        """An attestation that does not authenticate is no proof (5.7.6): the
        attempt is left open, UNKNOWN if it was DISPATCHING, budget kept."""
        moved = ""
        if record.state == OperationState.DISPATCHING:
            success, _prev = self.registry.transition_to(idempotency_key, OperationState.UNKNOWN)
            moved = "; DISPATCHING → UNKNOWN pending reconciliation" if success else ""
        self.ledger.append(
            kernel="composed", decision=Decision.HOLD.value,
            rationale=(f"NON_EXECUTION_ATTESTATION_REFUSED ({why}): not proof of non-execution; "
                       f"budget kept{moved}."),
            passage_id=record.passage_id, idempotency_key=idempotency_key,
        )
        return False

    def _fail_under_settlement_lock(self, idempotency_key: str, resource_id: str | None = None, amount: float = 0.0, *, non_execution_attested=None, runtime_channel: bool = False) -> bool:
        """Transition to FAILED and mutate only this operation's budget.

        After dispatch, non_execution_attested is required: the provider or
        reconciliation protocol confirms that no external effect occurred, and
        the attestation is persisted on the operation before any budget change.
        Unless ``runtime_channel`` (the governor's own provider observation),
        the attestation must authenticate under ``attestation_verifier``.
        """
        record = self.registry.get(idempotency_key)
        if record is None:
            return False
        authoritative_non_execution = False
        if non_execution_attested is not None:
            att = non_execution_attested
            if isinstance(att, dict):
                try:
                    att = NonExecutionAttestation.from_dict(att)
                except (TypeError, ValueError):
                    return False
            if not isinstance(att, NonExecutionAttestation):
                return False
            if record.state not in (OperationState.DISPATCHING, OperationState.UNKNOWN):
                return False  # an attestation is for a dispatched attempt only
            if att.idempotency_key != idempotency_key:
                return False
            try:
                att.validate()
            except ValueError:
                return False
            if runtime_channel:
                if not att.verified_by_runtime:
                    return False
            else:
                if att.verified_by_runtime or att.verification:
                    return self._refuse_attestation_after_dispatch(
                        idempotency_key, record,
                        "the caller asserted verified_by_runtime; only the runtime verifies",
                    )
                method = self._verify_attestation(att)
                if method is None:
                    why = ("no attestation_verifier is configured on the governor"
                           if self.attestation_verifier is None else
                           f"signature not verified under key_id '{att.key_id or '-'}'")
                    return self._refuse_attestation_after_dispatch(idempotency_key, record, why)
                att = att.verified(method)
            try:
                authoritative_non_execution = self.registry.record_non_execution_attested(
                    idempotency_key, att,
                )
            except (ValueError, RuntimeError):
                return False
            if not authoritative_non_execution:
                return False
            record = self.registry.get(idempotency_key)
        recorded_resource = record.resource_id or None
        if resource_id is None:
            resource_id = recorded_resource
        elif resource_id != recorded_resource:
            return False
        try:
            supplied_amount = float(amount)
            recorded_amount = float(record.amount)
        except (TypeError, ValueError, OverflowError):
            return False
        if supplied_amount == 0 and recorded_amount > 0:
            supplied_amount = recorded_amount
        if not math.isfinite(supplied_amount) or supplied_amount != recorded_amount:
            return False

        if (
            self.consensus_backend is not None
            and recorded_resource
            and recorded_amount > 0
            and not record.reservation_id
        ):
            # Recover the crash window where backend.reserve() persisted but
            # the operation registry did not yet persist reservation_id/flags.
            try:
                backend_state = self.consensus_backend.get_state(recorded_resource)
                orphan = next(
                    (item for item in backend_state.reservations
                     if item.get("key") == idempotency_key),
                    None,
                )
            except Exception:
                return False
            if orphan is not None:
                orphan_state = orphan.get("state")
                if orphan_state in ("RESERVED", "UNKNOWN"):
                    reserved_flag, committed_flag = True, False
                elif orphan_state == "COMMITTED":
                    reserved_flag, committed_flag = False, True
                elif orphan_state in ("RELEASED", "REFUNDED"):
                    reserved_flag, committed_flag = False, False
                else:
                    return False
                try:
                    repaired = self.registry.set_budget_flags(
                        idempotency_key,
                        budget_reserved=reserved_flag,
                        budget_committed=committed_flag,
                        reservation_id=orphan["reservation_id"],
                    )
                except Exception:
                    repaired = False
                if not repaired:
                    return False
                record = self.registry.get(idempotency_key)
                if record is None:
                    return False

        committed = record.budget_committed
        owns_budget = record.budget_reserved or record.budget_committed
        if self.consensus_backend is not None and owns_budget and not record.reservation_id:
            # Never fall back to the membrane's in-memory counters when the
            # durable backend is configured but reservation ownership is unknown.
            return False
        if self.consensus_backend is not None and record.reservation_id and owns_budget:
            if committed:
                # A committed budget can be refunded only when this call is
                # backed by explicit provider/reconciliation non-execution proof.
                if not authoritative_non_execution or record.state not in (
                    OperationState.DISPATCHING, OperationState.UNKNOWN,
                ):
                    return False
                try:
                    compensated = self.consensus_backend.refund(record.reservation_id)
                except Exception:
                    return False
                if getattr(compensated, "status", None) not in ("REFUNDED", "ALREADY_REFUNDED"):
                    return False
            else:
                if record.state not in (
                    OperationState.CREATED, OperationState.HOLD,
                    OperationState.RESERVED, OperationState.ESCALATED,
                ):
                    return False
                try:
                    compensated = self.consensus_backend.release(record.reservation_id)
                except Exception:
                    return False
                if getattr(compensated, "status", None) not in ("RELEASED", "ALREADY_RELEASED"):
                    return False
            try:
                flags_ok = self.registry.set_budget_flags(
                    idempotency_key,
                    budget_reserved=False,
                    budget_committed=False,
                    reservation_id=record.reservation_id,
                )
            except Exception:
                flags_ok = False
            if not flags_ok:
                return False
            success, _prev_state = self.registry.transition_to(
                idempotency_key, OperationState.FAILED,
            )
            return success
        # Decision B3: once dispatch was possible, only proof of non-execution
        # may close the attempt as FAILED and give its budget back. Without it,
        # a DISPATCHING operation becomes UNKNOWN (reconciliation) and keeps
        # its budget; an UNKNOWN one is left as it is.
        if record.state in (OperationState.DISPATCHING, OperationState.UNKNOWN) \
                and not authoritative_non_execution:
            if record.state == OperationState.DISPATCHING:
                self.registry.transition_to(idempotency_key, OperationState.UNKNOWN)
            return False

        if recorded_resource and recorded_amount > 0 and owns_budget:
            resource = self.membrane.state.resources.get(recorded_resource)
            if resource is None:
                return False
            with self.membrane.state.get_lock(recorded_resource):
                current = self.registry.get(idempotency_key)
                if current is None or current.state in (OperationState.FAILED, OperationState.CONFIRMED):
                    return False
                accounted = resource.consumed if committed else resource.reserved
                if accounted < recorded_amount:
                    return False
                flags_changed = committed or record.budget_reserved
                if flags_changed:
                    try:
                        flags_ok = self.registry.set_budget_flags(
                            idempotency_key,
                            budget_reserved=False,
                            budget_committed=False,
                        )
                    except Exception:
                        flags_ok = False
                    if not flags_ok:
                        return False
                success, _prev_state = self.registry.transition_to(
                    idempotency_key, OperationState.FAILED,
                )
                if not success:
                    if flags_changed:
                        try:
                            self.registry.set_budget_flags(
                                idempotency_key,
                                budget_reserved=not committed,
                                budget_committed=committed,
                            )
                        except Exception:
                            pass
                    return False
                if committed:
                    resource.refund(recorded_amount)
                else:
                    resource.release(recorded_amount)
                return True

        success, _prev_state = self.registry.transition_to(idempotency_key, OperationState.FAILED)
        return success

    def mark_unknown(self, idempotency_key: str) -> bool:
        """Transition DISPATCHING → UNKNOWN. Outcome is uncertain; the budget is kept."""
        success, _prev = self.registry.transition_to(idempotency_key, OperationState.UNKNOWN)
        return success

    def recover(self) -> dict[str, Any]:
        """Recovery procedure: find all unresolved operations and report them.

        Includes operations in DISPATCHING, UNKNOWN (dispatch-related),
        HOLD, ESCALATED, and budgeted CREATED operations whose durable
        reservation may need settlement. Recovery reports; it never redispatches.
        """
        unresolved = self.registry.find_unresolved()
        holds = self.hold_manager.find_holds()
        escalated = self.hold_manager.find_escalated()
        all_unresolved = unresolved + holds + escalated
        if self.consensus_backend is not None:
            all_unresolved += [
                record for record in self.registry.find_by_state(OperationState.CREATED)
                if record.resource_id and record.amount > 0
            ]

        def _recommendation(state: OperationState) -> str:
            if state == OperationState.CREATED:
                return "reconcile_budget"
            elif state == OperationState.DISPATCHING:
                return "reconcile"
            elif state == OperationState.UNKNOWN:
                return "reconcile" if self.provider is not None else "human_review"
            elif state == OperationState.HOLD:
                return "resolve_or_escalate"
            elif state == OperationState.ESCALATED:
                return "await_authorized_exit"
            else:
                return "unknown"

        return {
            "unresolved_count": len(all_unresolved),
            "unresolved": [
                {
                    "idempotency_key": r.idempotency_key,
                    "passage_id": r.passage_id,
                    "state": r.state.value,
                    "recommendation": _recommendation(r.state),
                    "hold_record": r.hold_record.to_dict() if r.hold_record else None,
                }
                for r in all_unresolved
            ],
        }

    # ── HOLD Lifecycle Methods ──────────────────────────────────────────
    # Per doctrine v7.1 and Blueprint v4.4: HOLD is a first-class protocol
    # state — a designed suspension of agency under pressure, not a system
    # failure. These methods manage the governed lifecycle that follows
    # a HOLD decision.

    def resolve_hold(
        self,
        idempotency_key: str,
        passage=None,
        pressure: float | None = None,
        velocity: float | None = None,
        capacity: float | None = None,
        option_space: float | None = None,
        authority: float | None = None,
        context_fit: float | None = None,
        evidence_quality: float | None = None,
        irreversibility_materiality: float | None = None,
        delegation=None,
        resource_id: str | None = None,
        amount: float = 0.0,
        trajectory_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Re-adjudicate a HOLD operation with new evidence.

        Per doctrine: when evidence becomes sufficient, the operator
        re-runs adjudication. If the new decision is PASS or FLAG,
        transition HOLD → RESERVED. If still HOLD, stay in HOLD. If
        HARD_VETO, transition to FAILED.

        The caller MUST provide the original passage identity (including
        idempotency key, passage ID, agent and action type) and updated
        evidence. The canonical payload must also match the durable
        authorization record; no substituted or missing payload is accepted.
        Omitting the passage is rejected; it cannot bypass re-adjudication.

        Returns the adjudication result dict with an additional
        'hold_resolved' field indicating whether the hold was resolved.
        """
        record = self.registry.get(idempotency_key)
        if record is None:
            return {"hold_resolved": False, "reason": f"Operation '{idempotency_key}' not found."}

        if record.state != OperationState.HOLD:
            return {
                "hold_resolved": False,
                "operation_state": record.state.value,
                "reason": f"Operation is in state {record.state.value}, not HOLD.",
            }

        # HOLD re-adjudication must retain the original economic identity.
        recorded_resource = record.resource_id or None
        if resource_id is None:
            resource_id = recorded_resource
        elif resource_id != recorded_resource:
            return {
                "hold_resolved": False,
                "operation_state": record.state.value,
                "reason": "Resource identity differs from the held operation.",
            }
        try:
            supplied_amount = float(amount)
            recorded_amount = float(record.amount)
        except (TypeError, ValueError, OverflowError):
            # Malformed argument, not a governance answer (R8): the HOLD is untouched.
            self._refuse_malformed(
                "Amount is invalid for HOLD re-adjudication.", field="amount",
                passage=passage, idem_key=idempotency_key,
            )
        if supplied_amount == 0 and recorded_amount > 0:
            supplied_amount = recorded_amount
        if not math.isfinite(supplied_amount) or supplied_amount != recorded_amount:
            return {
                "hold_resolved": False,
                "operation_state": record.state.value,
                "reason": "Amount differs from the held operation.",
            }
        amount = supplied_amount

        # P8: an out-of-domain measure is a malformed re-adjudication, refused
        # at entry (INVALID_ARGUMENT); the HOLD is untouched.
        self._refuse_out_of_domain(
            dict(pressure=pressure, velocity=velocity, capacity=capacity,
                 option_space=option_space, authority=authority,
                 context_fit=context_fit, evidence_quality=evidence_quality,
                 irreversibility_materiality=irreversibility_materiality),
            passage=passage, idem_key=idempotency_key,
        )

        if passage is None:
            # A HOLD may only be resolved after fresh adjudication. Never let
            # omission of the passage bypass both kernels and the decision lattice.
            reason = (
                "Re-adjudication required: provide the passage and updated evidence; "
                "HOLD remains unchanged."
            )
            self.ledger.append(
                kernel="hold_resolution",
                decision=Decision.HOLD.value,
                rationale=f"Rejected resolve_hold for '{idempotency_key}': {reason}",
                passage_id=record.passage_id,
                idempotency_key=idempotency_key,
            )
            return {
                "hold_resolved": False,
                "operation_state": record.state.value,
                "reason": reason,
            }

        # Do not mutate the supplied passage: operation identity is
        # immutable. Verify it under the durable settlement lock below,
        # against the current HOLD record, before either kernel is invoked.

        # Serialize re-adjudication with expiry and authorized exit so a
        # terminal transition cannot release a reservation mid-resolution.
        try:
            with self._settlement_lock:
                with self.registry.settlement_lock(
                    idempotency_key,
                    require_persistent=(
                        self.consensus_backend is not None and self.registry.is_persistent
                    ),
                ):
                    current = self.registry.get(idempotency_key)
                    if current is None or current.state != OperationState.HOLD:
                        return {
                            "hold_resolved": False,
                            "operation_state": current.state.value if current else None,
                            "reason": "HOLD changed before re-adjudication could be claimed.",
                        }
                    if (
                        self.consensus_backend is not None
                        and not self.registry.is_persistent
                        and (current.budget_reserved or current.budget_committed)
                    ):
                        return {
                            "hold_resolved": False,
                            "operation_state": current.state.value,
                            "budget_reconciliation_required": True,
                            "reason": "Durable HOLD re-adjudication requires a persistent operation registry.",
                        }

                    # A policy-declared τ_K refuses alone: past it, no fresh
                    # evidence can resolve the HOLD (RN-001 §2a). Checked before
                    # identity: the attempt is closed at its bound, whoever calls.
                    if self._tau_k_exhausted(current):
                        return self._fail_closed_on_tau_k(
                            idempotency_key, current, "resolve_hold",
                        )
                    # Bind HOLD resolution to the exact durable operation.
                    # Recheck under the lock so a stale caller cannot
                    # substitute identity while another worker settles HOLD.
                    expected_identity = {
                        "idempotency_key": idempotency_key,
                        "passage_id": current.passage_id,
                        "agent_id": current.agent_id,
                        "action_type": current.action_type,
                    }
                    mismatch = next((
                        name for name, expected in expected_identity.items()
                        if getattr(passage, name, None) != expected
                    ), None)
                    if mismatch is not None:
                        reason = f"HOLD identity mismatch: {mismatch} differs from the durable operation."
                    else:
                        try:
                            supplied_payload_hash = canonical_payload_hash(payload)
                        except (TypeError, ValueError) as exc:
                            reason = f"HOLD payload invalid: {exc}"
                        else:
                            reason = (
                                "HOLD payload mismatch: the supplied action payload "
                                "differs from the authorized payload."
                                if supplied_payload_hash != current.payload_hash else ""
                            )
                    if reason:
                        self.ledger.append(
                            kernel="hold_resolution",
                            decision=Decision.HOLD.value,
                            rationale=reason,
                            passage_id=current.passage_id,
                            idempotency_key=idempotency_key,
                        )
                        return {
                            "hold_resolved": False,
                            "operation_state": current.state.value,
                            "reason": reason,
                        }
                    # First check avoids needless kernel evaluation under a
                    # known quarantine. Re-check under the shared gate at the
                    # actual HOLD -> RESERVED transition: scoring and membrane
                    # evaluation must not block emergency lock activation.
                    if self._quarantine_blocks():
                        return {
                            "hold_resolved": False,
                            "operation_state": current.state.value,
                            "reason": "QUARANTINE_LOCK is active; HOLD cannot resolve to admission.",
                        }
                    return self._re_adjudicate_hold(
                        passage, pressure, velocity, capacity, option_space,
                        authority, context_fit, evidence_quality, irreversibility_materiality,
                        delegation, resource_id, amount, trajectory_id,
                    )
        except Exception:
            current = self.registry.get(idempotency_key)
            return {
                "hold_resolved": False,
                "operation_state": current.state.value if current else None,
                "reason": "Could not acquire durable HOLD settlement lock; operation unchanged.",
            }

    def _re_adjudicate_hold(
        self,
        passage,
        pressure: float, velocity: float, capacity: float,
        option_space: float | None, authority: float | None, context_fit: float | None,
        evidence_quality: float | None, irreversibility_materiality: float | None,
        delegation, resource_id: str | None, amount: float,
        trajectory_id: str | None,
    ) -> dict[str, Any]:
        """Internal: re-adjudicate a HOLD operation with new evidence.

        Runs both kernels with the new evidence. If the composed decision
        is PASS or FLAG, transitions HOLD → RESERVED. If HARD_VETO,
        transitions HOLD → FAILED. If still HOLD, stays in HOLD.
        """
        idem_key = passage.idempotency_key

        # 1. Scoring kernel — fail-closed
        scoring_result = None
        scoring_error = ""
        try:
            scoring_result = self.scoring.adjudicate(
                pressure=pressure, velocity=velocity, capacity=capacity,
                option_space=option_space, authority=authority,
                context_fit=context_fit, evidence_quality=evidence_quality,
                irreversibility_materiality=irreversibility_materiality,
            )
        except Exception as e:
            scoring_result = None
            scoring_error = str(e)

        # 2. Membrane kernel — fail-closed
        membrane_result = None
        membrane_error = ""
        try:
            # #141 (5.7.6): the HOLD already holds the reservation this
            # re-adjudication would take. Declare it to I8 so the budget check
            # does not count the same amount twice (adjudicate() already does).
            held_record = self.registry.get(idem_key)
            already_reserved_amount = (
                amount if held_record is not None and held_record.budget_reserved else 0.0
            )
            membrane_result = self.membrane.adjudicate(
                passage=passage, delegation=delegation,
                resource_id=resource_id, amount=amount,
                trajectory_id=trajectory_id,
                existing_reservation_amount=already_reserved_amount,
            )
        except Exception as e:
            membrane_result = None
            membrane_error = str(e)

        # 3. Compute budget utilization
        budget_utilization = None
        if resource_id and amount > 0:
            resource = self.membrane.state.resources.get(resource_id)
            if resource and resource.capacity > 0:
                budget_utilization = self._prospective_utilization(idem_key, resource, amount)

        # 4. Compose
        budget_in_scope = resource_id is not None and amount > 0
        composed_decision, composed_rationale = compose(
            scoring_result["decision"] if scoring_result else None,
            membrane_result["decision"] if membrane_result else None,
            scoring_ces=scoring_result.get("ces_state") if scoring_result else None,
            membrane_checks=membrane_result.get("checks") if membrane_result else None,
            budget_utilization=budget_utilization,
            budget_in_scope=budget_in_scope,
            policy=self.policy,
        )
        composed_decision, composed_rationale = self._apply_flag_condition_rule(
            composed_decision, composed_rationale,
        )

        # 5. Ledger
        self.ledger.append(
            kernel="scoring",
            decision=scoring_result["decision"] if scoring_result else "HOLD",
            rationale=scoring_result["rationale"] if scoring_result else f"Scoring kernel error: {scoring_error}",
            passage_id=passage.passage_id,
            idempotency_key=idem_key,
        )
        self.ledger.append(
            kernel="membrane",
            decision=membrane_result["decision"] if membrane_result else "HOLD",
            rationale=membrane_result["rationale"] if membrane_result else f"Membrane kernel error: {membrane_error}",
            passage_id=passage.passage_id,
            idempotency_key=idem_key,
        )
        self.ledger.append(
            kernel="composed",
            decision=composed_decision,
            rationale=composed_rationale,
            passage_id=passage.passage_id,
            idempotency_key=idem_key,
            scoring_result=scoring_result or {},
            membrane_result=membrane_result or {},
            composed_decision=composed_decision,
        )

        # 6. State transitions from HOLD
        hold_resolved = False
        # I6 may have observed quarantine during kernel evaluation even if
        # another worker released it before this point. Such evidence cannot
        # be used to terminally fail an existing HOLD; require fresh review.
        observed_i6_veto = any(
            c.get("check_id") == "I6" and c.get("decision") == Decision.HARD_VETO.value
            for c in (membrane_result.get("checks", []) if membrane_result else [])
        )
        quarantine_retained = False
        if observed_i6_veto or self._quarantine_blocks():
            quarantine_retained = True
            composed_decision = Decision.HOLD.value
            composed_rationale = (
                "QUARANTINE_LOCK observed during HOLD re-adjudication; "
                "HOLD retained pending fresh evidence."
            )
            self.ledger.append(
                kernel="composed", decision=Decision.HOLD.value,
                rationale=composed_rationale, passage_id=passage.passage_id,
                idempotency_key=idem_key,
            )

        # Rule A (5.7.7 candidate): a HOLD on insufficient evidence reserved
        # nothing (see adjudicate). Once the evidence is complete, the retained
        # operation takes its reservation here, under I8 as evaluated above
        # with existing_reservation_amount = 0. A re-adjudication that is
        # still insufficient reserves nothing. A HOLD that already holds its
        # reservation (5.7.6 HOLDs on complete evidence) is unchanged.
        held_record = self.registry.get(idem_key)
        still_evidence_hold = (
            composed_decision == Decision.HOLD.value
            and bool((scoring_result or {}).get("insufficient_evidence"))
        )
        reserved_now = False
        if (budget_in_scope and not quarantine_retained and not still_evidence_hold
                and composed_decision in (
                    Decision.PASS.value, Decision.FLAG.value, Decision.HOLD.value)
                and not (held_record and held_record.budget_reserved)):
            try:
                if self.consensus_backend is not None:
                    with self.registry.settlement_lock(idem_key, require_persistent=True):
                        current = self.registry.get(idem_key)
                        reserved_now = bool(
                            current is not None
                            and current.state == OperationState.HOLD
                            and self._reserve_budget(idem_key, resource_id, amount)
                        )
                else:
                    reserved_now = bool(self._reserve_budget(idem_key, resource_id, amount))
            except Exception:
                reserved_now = False
            if not reserved_now:
                composed_decision = Decision.HARD_VETO.value
                composed_rationale = (
                    "Budget reservation failed at re-adjudication: the amount is no "
                    "longer available (nothing was reserved while the evidence was "
                    "insufficient) — fail-closed HARD_VETO."
                )
                self.ledger.append(
                    kernel="composed", decision=composed_decision,
                    rationale=composed_rationale, passage_id=passage.passage_id,
                    idempotency_key=idem_key,
                )
            else:
                self.ledger.append(
                    kernel="hold_lifecycle", decision=composed_decision,
                    rationale="Budget reserved at re-adjudication: the evidence is complete (rule A).",
                    passage_id=passage.passage_id, idempotency_key=idem_key,
                )

        def _compensate_reservation_taken_now(retained_rationale: str) -> None:
            # The HOLD stays; a reservation taken in this call must not stay
            # with it (an evidence HOLD holds none). Release what was reserved
            # here; if that fails, say so instead of claiming a clean HOLD.
            nonlocal composed_rationale
            if not reserved_now:
                return
            current = self.registry.get(idem_key)
            error = (self._release_hold_budget_before_terminal_transition(idem_key, current)
                     if current is not None else "Operation record missing after reservation.")
            if error:
                composed_rationale = retained_rationale + " Reservation taken at re-adjudication could not be released; reconciliation required: " + error

        if composed_decision in (Decision.PASS.value, Decision.FLAG.value):
            # Serialize the final admission, not the potentially slow kernel
            # evaluation. The reservation is the HOLD's (5.7.6) or the one
            # taken just above (rule A).
            with self.membrane.state._quarantine_lock:
                with self.registry.quarantine_guard():
                    if self._quarantine_blocks():
                        # A lock activated during scoring: retain HOLD even
                        # when both kernels would otherwise permit admission.
                        composed_decision = Decision.HOLD.value
                        composed_rationale = (
                            "QUARANTINE_LOCK activated during HOLD re-adjudication; "
                            "HOLD retained, no RESERVED transition."
                        )
                        self.ledger.append(
                            kernel="composed", decision=Decision.HOLD.value,
                            rationale=composed_rationale, passage_id=passage.passage_id,
                            idempotency_key=idem_key,
                        )
                        _compensate_reservation_taken_now(composed_rationale)
                    else:
                        success, _prev = self.registry.transition_to(
                            idem_key, OperationState.RESERVED,
                            hold_record_changes={
                                "exit_reason": "resolved",
                                "resolved_at": _utc_now_iso(),
                            },
                            admission_terms=self._admission_terms(composed_decision),
                        )
                        if success:
                            hold_resolved = True
                        else:
                            composed_decision = Decision.HOLD.value
                            composed_rationale = (
                                "Re-adjudication produced PASS/FLAG but RESERVED "
                                "transition failed — staying in HOLD."
                            )
                            _compensate_reservation_taken_now(composed_rationale)
        elif composed_decision == Decision.HARD_VETO.value:
            held_record = self.registry.get(idem_key)
            # Only release a reservation that this durable operation actually owns.
            should_release_budget = bool(held_record and held_record.budget_reserved)
            if resource_id and amount > 0 and should_release_budget:
                if self.consensus_backend is not None and not (held_record and held_record.reservation_id):
                    composed_decision = Decision.HOLD.value
                    composed_rationale = "Durable reservation identity missing; budget release blocked pending reconciliation."
                elif self.consensus_backend is not None and held_record and held_record.reservation_id:
                    try:
                        released = self.consensus_backend.release(held_record.reservation_id)
                    except Exception:
                        released = None
                    if getattr(released, "status", None) not in ("RELEASED", "ALREADY_RELEASED"):
                        composed_decision = Decision.HOLD.value
                        composed_rationale = "Durable reservation release unresolved; HOLD retained for reconciliation."
                    else:
                        try:
                            flags_ok = self.registry.set_budget_flags(
                                idem_key, budget_reserved=False,
                                budget_committed=False,
                                reservation_id=held_record.reservation_id,
                            )
                        except Exception:
                            flags_ok = False
                        if flags_ok:
                            success, _prev = self.registry.transition_to(
                                idem_key, OperationState.FAILED,
                                hold_record_changes={"exit_reason": "resolved_veto"},
                            )
                            hold_resolved = bool(success)
                            if not success:
                                composed_decision = Decision.HOLD.value
                                composed_rationale = "Durable release completed but terminal transition lost; replay reconciliation."
                        else:
                            composed_decision = Decision.HOLD.value
                            composed_rationale = "Durable release completed but ownership update failed; reconciliation required."
                else:
                    resource = self.membrane.state.resources.get(resource_id)
                    if resource is None:
                        composed_decision = Decision.HOLD.value
                        composed_rationale = "HOLD resource missing; budget release requires reconciliation."
                    else:
                        with self.membrane.state.get_lock(resource_id):
                            if resource.reserved < amount:
                                composed_decision = Decision.HOLD.value
                                composed_rationale = "HOLD reservation accounting inconsistent; refusing release."
                            else:
                                success, _prev = self.registry.transition_to(
                                    idem_key, OperationState.FAILED,
                                    hold_record_changes={"exit_reason": "resolved_veto"},
                                )
                                if success:
                                    resource.release(amount)
                                    try:
                                        self.registry.set_budget_flags(
                                            idem_key,
                                            budget_reserved=False,
                                            budget_committed=False,
                                        )
                                    except Exception:
                                        # The operation is terminal, but ownership
                                        # persistence failed. Preserve a visible
                                        # reconciliation signal instead of claiming
                                        # a fully resolved HOLD.
                                        composed_decision = Decision.HOLD.value
                                        composed_rationale = (
                                            "HOLD veto applied, but budget ownership flags could not be persisted; reconciliation required."
                                        )
                                        hold_resolved = False
                                    else:
                                        hold_resolved = True
                                else:
                                    composed_decision = Decision.HOLD.value
                                    composed_rationale = "HARD_VETO re-adjudication lost the terminal transition race; budget unchanged."
            else:
                success, _prev = self.registry.transition_to(
                    idem_key, OperationState.FAILED,
                    hold_record_changes={"exit_reason": "resolved_veto"},
                )
                if success:
                    hold_resolved = True
                else:
                    composed_decision = Decision.HOLD.value
                    composed_rationale = "HARD_VETO re-adjudication lost the terminal transition race; budget unchanged."
        elif composed_decision == Decision.HOLD.value:
            # Still HOLD — update the hold record's timestamp
            if record := self.registry.get(idem_key):
                if record.hold_record:
                    # Keep the original hold_start — τ_K continues counting
                    pass

        return {
            "passage_id": passage.passage_id,
            "idempotency_key": idem_key,
            "scoring_decision": scoring_result["decision"] if scoring_result else None,
            "scoring_rationale": scoring_result["rationale"] if scoring_result else None,
            "scoring_s": scoring_result.get("s_score") if scoring_result else None,
            "membrane_decision": membrane_result["decision"] if membrane_result else None,
            "membrane_rationale": membrane_result["rationale"] if membrane_result else None,
            "membrane_checks": membrane_result.get("checks", []) if membrane_result else [],
            "composed_decision": composed_decision,
            "composed_rationale": composed_rationale,
            "execution_status": membrane_result.get("execution_status", "pending") if membrane_result else "unknown",
            "operation_state": self.registry.get(idem_key).state.value if self.registry.get(idem_key) else "unknown",
            "budget_reserved": bool(
                self.registry.get(idem_key) and self.registry.get(idem_key).budget_reserved
            ),
            "ledger_entries": self.ledger.entry_count,
            "ledger_valid": self.ledger.verify_new_entries(),
            **self._flag_fields(composed_decision, scoring_result),
            "hold_resolved": hold_resolved,
        }

    def _release_hold_budget_before_terminal_transition(
        self, idempotency_key: str, record: OperationRecord,
    ) -> str | None:
        """Release only proven reservation ownership before failing a HOLD.

        Returning an error leaves the lifecycle state non-terminal, allowing
        an idempotent backend release to be retried after recovery.
        """
        if record.budget_committed:
            return "Committed budget cannot be released as a HOLD reservation; reconciliation is required."
        if not record.budget_reserved:
            return None
        try:
            amount = float(record.amount)
        except (TypeError, ValueError, OverflowError):
            return "Reservation ownership is recorded with an invalid amount; reconciliation is required."
        if not record.resource_id or not math.isfinite(amount) or amount <= 0:
            return "Reservation ownership is recorded without a valid resource and amount; reconciliation is required."

        if self.consensus_backend is not None:
            if not self.registry.is_persistent:
                return "Durable budget settlement requires a persistent operation registry."
            if not record.reservation_id:
                return "Durable reservation identity missing; budget release blocked."
            try:
                released = self.consensus_backend.release(record.reservation_id)
            except Exception:
                released = None
            if getattr(released, "status", None) not in ("RELEASED", "ALREADY_RELEASED"):
                return "Durable budget release is unresolved; lifecycle state remains open for retry."
            try:
                flags_ok = self.registry.set_budget_flags(
                    idempotency_key,
                    budget_reserved=False,
                    budget_committed=False,
                    reservation_id=record.reservation_id,
                )
            except Exception:
                flags_ok = False
            if not flags_ok:
                return "Budget was released but ownership persistence is unresolved; retry/reconcile before exit."
            return None

        resource = self.membrane.state.resources.get(record.resource_id)
        if resource is None:
            return "Owned in-memory budget resource is unavailable; lifecycle state remains open."
        try:
            with self.membrane.state.get_lock(record.resource_id):
                resource.release(amount)
            flags_ok = self.registry.set_budget_flags(
                idempotency_key,
                budget_reserved=False,
                budget_committed=False,
            )
        except Exception:
            flags_ok = False
        if not flags_ok:
            return "Budget release or ownership persistence failed; lifecycle state remains open for reconciliation."
        return None

    def escalate_hold(
        self,
        idempotency_key: str,
        escalated_by: str = "",
    ) -> dict[str, Any]:
        """Escalate a HOLD operation to ESCALATED state.

        Per v7.1 §19: ESCALATED is absorbing. It remains until an
        authorization model defines who signs, under what scope, and with
        which receipt. No automatic time-based exit.

        Per Blueprint v4.4: Escalate strategy = HOLD global + escalation
        to Risk Officer.
        """
        try:
            with self._settlement_lock:
                with self.registry.settlement_lock(
                    idempotency_key,
                    require_persistent=(
                        self.consensus_backend is not None and self.registry.is_persistent
                    ),
                ):
                    record = self.registry.get(idempotency_key)
                    if (record is not None and record.state == OperationState.HOLD
                            and self._tau_k_exhausted(record)):
                        return self._fail_closed_on_tau_k(
                            idempotency_key, record, "escalate_hold",
                        )
                    return self.hold_manager.escalate_hold(idempotency_key, escalated_by)
        except Exception:
            current = self.registry.get(idempotency_key)
            return {
                "escalated": False,
                "operation_state": current.state.value if current else None,
                "reason": "Could not acquire durable HOLD settlement lock; operation unchanged.",
            }

    def authorize_exit(
        self,
        idempotency_key: str,
        authorized_by: str,
        scope: str,
        authorization: str,
        reason: str = "",
        approve: bool = True,
    ) -> dict[str, Any]:
        """Authorized exit from ESCALATED state.

        Per v7.1 §19: ESCALATED remains absorbing until an authorization
        model defines who signs, under what scope, and with which receipt.

        Per Blueprint v4.4 §5: overrides constitutionnels uniquement via
        Ceremony G-R5-001 (authentification opérateur).

        Args:
            authorized_by: who is authorizing the exit (operator ID)
            scope: scope of the authorization
            authorization: authorization mechanism (e.g., "ceremony-G-R5-001")
            reason: why the exit is being authorized
            approve: True → proceed (RESERVED); False → deny (FAILED)
        """
        try:
            with self._settlement_lock:
                with self.registry.settlement_lock(
                    idempotency_key,
                    require_persistent=(
                        self.consensus_backend is not None and self.registry.is_persistent
                    ),
                ):
                    record = self.registry.get(idempotency_key)
                    if record is None or record.state != OperationState.ESCALATED:
                        return self.hold_manager.authorize_exit(
                            idempotency_key, authorized_by, scope, authorization, reason, approve,
                        )
                    if (
                        self.consensus_backend is not None
                        and not self.registry.is_persistent
                        and (record.budget_reserved or record.budget_committed)
                    ):
                        return {
                            "exited": False,
                            "operation_state": record.state.value,
                            "exit_receipt": None,
                            "budget_reconciliation_required": True,
                            "reason": "Durable budget settlement requires a persistent operation registry.",
                        }
                    if approve and self._tau_k_exhausted(record):
                        return self._fail_closed_on_tau_k(
                            idempotency_key, record, "authorize_exit",
                        )
                    if not approve:
                        release_error = self._release_hold_budget_before_terminal_transition(
                            idempotency_key, record,
                        )
                        if release_error:
                            return {
                                "exited": False,
                                "operation_state": record.state.value,
                                "exit_receipt": None,
                                "budget_reconciliation_required": True,
                                "reason": release_error,
                            }
                    # Approval retains the reservation; denial reaches FAILED
                    # only after reservation ownership is safely released.
                    return self.hold_manager.authorize_exit(
                        idempotency_key, authorized_by, scope, authorization, reason, approve,
                    )
        except Exception:
            current = self.registry.get(idempotency_key)
            return {
                "exited": False,
                "operation_state": current.state.value if current else None,
                "exit_receipt": None,
                "budget_reconciliation_required": True,
                "reason": "Could not acquire durable HOLD settlement lock; operation unchanged.",
            }

    def expire_hold(self, idempotency_key: str) -> dict[str, Any]:
        """Expire a HOLD when τ_K is exhausted — FAIL-CLOSED.

        Per doctrine: when τ_K expires on an unresolved HOLD, the default
        behavior is FAIL-CLOSED: the action is rejected. This is NOT a
        resolution — it is a fail-closed rejection.

        Releases any reserved budget.
        """
        try:
            with self._settlement_lock:
                with self.registry.settlement_lock(
                    idempotency_key,
                    require_persistent=(
                        self.consensus_backend is not None and self.registry.is_persistent
                    ),
                ):
                    record = self.registry.get(idempotency_key)
                    if record is None:
                        return {"expired": False, "reason": f"Operation '{idempotency_key}' not found."}
                    expiry = self.hold_manager.check_tau_k_expiry(idempotency_key)
                    if not expiry["expired"]:
                        return {
                            "expired": False,
                            "operation_state": record.state.value,
                            "reason": expiry["reason"],
                        }
                    release_error = self._release_hold_budget_before_terminal_transition(
                        idempotency_key, record,
                    )
                    if release_error:
                        return {
                            "expired": False,
                            "operation_state": record.state.value,
                            "budget_reconciliation_required": True,
                            "reason": release_error,
                        }
                    return self.hold_manager.expire_hold(idempotency_key)
        except Exception:
            current = self.registry.get(idempotency_key)
            return {
                "expired": False,
                "operation_state": current.state.value if current else None,
                "budget_reconciliation_required": True,
                "reason": "Could not acquire durable HOLD settlement lock; operation unchanged.",
            }

    def expire_escalated(self, idempotency_key: str) -> dict[str, Any]:
        """Expire an ESCALATED operation once τ_K is exhausted → UNKNOWN.

        Refuses before the bound, like ``expire_hold``. Per v7.1 §19 the runtime
        label is UNKNOWN; the operation never reached dispatch, so the record says
        CONFIRMED_NONEXECUTION. The reservation is left to reconciliation, as before.
        An early end goes through ``cancel_escalation``.
        """
        try:
            with self._settlement_lock:
                with self.registry.settlement_lock(
                    idempotency_key,
                    require_persistent=(
                        self.consensus_backend is not None and self.registry.is_persistent
                    ),
                ):
                    return self.hold_manager.expire_escalated(idempotency_key)
        except Exception:
            current = self.registry.get(idempotency_key)
            return {
                "expired": False,
                "operation_state": current.state.value if current else None,
                "reason": "Could not acquire durable HOLD settlement lock; operation unchanged.",
            }

    def cancel_escalation(
        self, idempotency_key: str, cancelled_by: str, reason: str, decision: str,
    ) -> dict[str, Any]:
        """End an escalation before its bound, by an authority → FAILED.

        Releases the reservation first (never dispatched), then records actor,
        reason and decision. If the release cannot be proven, nothing changes.
        """
        try:
            with self._settlement_lock:
                with self.registry.settlement_lock(
                    idempotency_key,
                    require_persistent=(
                        self.consensus_backend is not None and self.registry.is_persistent
                    ),
                ):
                    record = self.registry.get(idempotency_key)
                    if record is None or record.state != OperationState.ESCALATED:
                        return self.hold_manager.cancel_escalation(
                            idempotency_key, cancelled_by, reason, decision)
                    if not all(str(v or "").strip() for v in (cancelled_by, reason, decision)):
                        return self.hold_manager.cancel_escalation(
                            idempotency_key, cancelled_by, reason, decision)
                    release_error = self._release_hold_budget_before_terminal_transition(
                        idempotency_key, record,
                    )
                    if release_error:
                        return {
                            "cancelled": False,
                            "operation_state": record.state.value,
                            "budget_reconciliation_required": True,
                            "reason": release_error,
                        }
                    return self.hold_manager.cancel_escalation(
                        idempotency_key, cancelled_by, reason, decision)
        except Exception:
            current = self.registry.get(idempotency_key)
            return {
                "cancelled": False,
                "operation_state": current.state.value if current else None,
                "budget_reconciliation_required": True,
                "reason": "Could not acquire durable HOLD settlement lock; operation unchanged.",
            }

    # -- τ_K as a fail-safe bound (RN-001 §2a) ------------------------------
    def _tau_k_exhausted(self, record: OperationRecord) -> bool:
        """True when a held operation's τ_K is exhausted at an advance point.

        HOLD / ESCALATED: the bound is exhausted now. RESERVED after a HOLD:
        the resolution came after the bound, or its time is unknown. A record
        without τ_K (none declared) is never exhausted: no bound is invented.
        """
        hold = record.hold_record
        if hold is None or not hold.tau_k_seconds or hold.tau_k_seconds <= 0:
            return False
        try:
            deadline = datetime.fromisoformat(hold.hold_start).timestamp() + float(hold.tau_k_seconds)
        except (TypeError, ValueError):
            # An unreadable HOLD start cannot show the bound was respected.
            return record.state in (
                OperationState.HOLD, OperationState.ESCALATED, OperationState.RESERVED,
            )
        if record.state in (OperationState.HOLD, OperationState.ESCALATED):
            return _utc_now_epoch() >= deadline
        if record.state == OperationState.RESERVED and hold.exit_reason == "resolved":
            if not hold.resolved_at:
                return True
            try:
                return datetime.fromisoformat(hold.resolved_at).timestamp() >= deadline
            except (TypeError, ValueError):
                return True
        return False

    def _fail_closed_on_tau_k(
        self, idempotency_key: str, record: OperationRecord, point: str,
    ) -> dict[str, Any]:
        """Apply the fail-closed exhaustion policy. Caller holds the settlement lock.

        The reservation is released first; the operation reaches FAILED only
        once that is proven, otherwise it stays open for reconciliation.
        """
        hold = record.hold_record
        bound = f"{hold.tau_k_seconds:g} s, source {hold.tau_k_source or 'unstated'}" if hold else "unknown"
        release_error = self._release_hold_budget_before_terminal_transition(
            idempotency_key, record,
        )
        base = {
            "hold_resolved": False,
            "exited": False,
            "escalated": False,
            "dispatched": False,
            "tau_k_expired": True,
            "advance_point": point,
        }
        if release_error:
            reason = (f"τ_K exhausted ({bound}) at {point}: no effect; attempt not closed, "
                      "budget reconciliation required; " + release_error)
            self.ledger.append(
                kernel="hold_lifecycle",
                decision=Decision.HARD_VETO.value,
                rationale=reason,
                passage_id=record.passage_id,
                idempotency_key=idempotency_key,
                composed_decision=Decision.HARD_VETO.value,
            )
            return {
                **base,
                "operation_state": record.state.value,
                "budget_reconciliation_required": True,
                "reason": reason,
            }
        success, _prev = self.registry.transition_to(
            idempotency_key,
            OperationState.FAILED,
            hold_record_changes={
                "exit_reason": "expired",
                "exhaustion_disposition": "fail-closed",
                "exhausted_at": point,
                "execution_record": "CONFIRMED_NONEXECUTION",
            },
        )
        current = self.registry.get(idempotency_key)
        # Every advance point checks before any DISPATCHING claim: the attempt
        # was never dispatched, so its non-execution is established. FAILED is
        # this runtime's label; the canonical record is CONFIRMED_NONEXECUTION.
        reason = (
            f"τ_K exhausted ({bound}) at {point}: FAIL-CLOSED, terminal for this attempt "
            "identity, never dispatched (CONFIRMED_NONEXECUTION), reservation released "
            "(not a resolution)."
            if success else f"τ_K exhausted ({bound}) at {point}; transition to FAILED failed."
        )
        self.ledger.append(
            kernel="hold_lifecycle",
            decision=Decision.HARD_VETO.value,
            rationale=reason,
            passage_id=record.passage_id,
            idempotency_key=idempotency_key,
            composed_decision=Decision.HARD_VETO.value,
        )
        return {
            **base,
            "operation_state": current.state.value if current else None,
            "composed_decision": Decision.HARD_VETO.value,
            "execution_record": "CONFIRMED_NONEXECUTION" if success else None,
            "reason": reason,
        }

    def _fail_closed_on_tau_k_locked(self, idempotency_key: str, point: str) -> dict[str, Any] | None:
        """Take the settlement lock, re-read, and fail closed if still exhausted.

        Returns None when, under the lock, the bound no longer applies (the
        operation moved on); the caller then proceeds with the current record.
        """
        try:
            with self._settlement_lock:
                with self.registry.settlement_lock(
                    idempotency_key,
                    require_persistent=(
                        self.consensus_backend is not None and self.registry.is_persistent
                    ),
                ):
                    current = self.registry.get(idempotency_key)
                    if current is None or not self._tau_k_exhausted(current):
                        return None
                    return self._fail_closed_on_tau_k(idempotency_key, current, point)
        except Exception:
            current = self.registry.get(idempotency_key)
            return {
                "dispatched": False,
                "tau_k_expired": True,
                "operation_state": current.state.value if current else None,
                "budget_reconciliation_required": True,
                "reason": "τ_K exhausted; could not acquire durable settlement lock; no dispatch.",
            }

    def check_hold_expiry(self, idempotency_key: str) -> dict[str, Any]:
        """Check whether τ_K has expired for a HOLD or ESCALATED operation."""
        return self.hold_manager.check_tau_k_expiry(idempotency_key)

    def find_holds(self) -> list[OperationRecord]:
        """Find all operations currently in HOLD state."""
        return self.hold_manager.find_holds()

    def find_escalated(self) -> list[OperationRecord]:
        """Find all operations currently in ESCALATED state."""
        return self.hold_manager.find_escalated()

    # ── Post-Execution Assessment (τ⁻¹ + EVP) ──────────────────────────
    # Per doctrine: both τ⁻¹ (partial tau inversion) and EVP (intended value
    # preservation) are EX-POST assessments. They run AFTER the dispatch
    # state machine reaches a terminal state (CONFIRMED or FAILED), never
    # before. They do NOT feed back into the admissibility function D.
    # The assessor is OPTIONAL — the governor operates correctly without it.

    def assess_post_execution(
        self,
        idempotency_key: str,
        retained_properties: list[str] | None = None,
        return_transition: Any = None,
        intended_value: float = 0.0,
        realized_value: float | None = None,
        confidence: float = 1.0,
        execution_loss: float = 0.0,
        denied_opportunity_value: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> AssessmentReport | None:
        """Run τ⁻¹ and EVP assessment for a completed operation.

        This method runs AFTER the operation reaches a terminal state
        (CONFIRMED or FAILED). It produces an AssessmentReport containing
        both the τ⁻¹ classification and the EVP measurement.

        CRITICAL: This assessment does NOT feed back into admissibility.
        It is a pure post-execution measurement for audit, reporting, and
        continuous improvement. It does NOT affect the operation's state
        or any future admissibility decision.

        Args:
            idempotency_key: The operation's idempotency key.
            retained_properties: Properties Q for τ⁻¹ evaluation.
            return_transition: Return transition function for τ⁻¹.
            intended_value: Value the operation was designed to produce.
            realized_value: Actual value produced.
            confidence: Prior confidence of success (0-1).
            execution_loss: Value lost to execution failures.
            denied_opportunity_value: Value of denied opportunities.
            metadata: Additional context for both assessments.

        Returns:
            AssessmentReport with both τ⁻¹ and EVP results, or None if
            the assessor is not configured or the operation is not in a
            terminal state.
        """
        if self.assessor is None:
            return None

        record = self.registry.get(idempotency_key)
        if record is None:
            return None

        # Only assess terminal states
        if record.state not in (OperationState.CONFIRMED, OperationState.FAILED):
            return None

        # Extract hold/escalation durations from the hold record
        hold_duration_seconds = 0.0
        escalated_duration_seconds = 0.0
        if record.hold_record:
            from datetime import datetime, timezone
            try:
                hold_start = datetime.fromisoformat(record.hold_record.hold_start) if record.hold_record.hold_start else None
                if hold_start:
                    now = datetime.now(timezone.utc)
                    hold_duration = (now - hold_start).total_seconds()
                    if record.hold_record.is_escalated and record.hold_record.escalated_at:
                        esc_start = datetime.fromisoformat(record.hold_record.escalated_at)
                        escalated_duration_seconds = (now - esc_start).total_seconds()
                        hold_duration_seconds = max(0, (esc_start - hold_start).total_seconds())
                    else:
                        hold_duration_seconds = max(0, hold_duration)
            except (ValueError, TypeError):
                pass  # Timestamp parsing failed — durations stay at 0

        return self.assessor.assess(
            idempotency_key=idempotency_key,
            passage_id=record.passage_id,
            operation_state=record.state.value,
            retained_properties=retained_properties,
            return_transition=return_transition,
            intended_value=intended_value,
            realized_value=realized_value,
            confidence=confidence,
            hold_duration_seconds=hold_duration_seconds,
            escalated_duration_seconds=escalated_duration_seconds,
            execution_loss=execution_loss,
            denied_opportunity_value=denied_opportunity_value,
            metadata=metadata,
        )
