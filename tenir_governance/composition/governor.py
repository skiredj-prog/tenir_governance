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
from hashlib import sha256
import json
import math
import re
import threading
from typing import Any, Optional
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
)
from ..operations.hold import HoldManager
from ..operations.payload_binding import canonical_payload_hash
from ..assessment.assessor import PostExecutionAssessor, AssessmentReport


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

    def __init__(self, tenir_kernel, *, tau_floor: float = 0.42,
                 option_space_alert_floor: float = 0.35,
                 option_space_block_floor: float = 0.20,
                 reaction_budget_events: int = 5):
        self._kernel = tenir_kernel
        self._tau_floor = tau_floor
        self._option_space_alert_floor = option_space_alert_floor
        self._option_space_block_floor = option_space_block_floor
        self._reaction_budget_events = reaction_budget_events

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
        # A neutral placeholder keeps the geometry structurally complete; the
        # missing measurement is separately converted into a hard gate below.
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
        irreversibility_materiality: float = 0.0,
    ) -> dict[str, Any]:
        """Delegate to TenirKernel with injected gate reasons.

        Returns a dict with the same shape as the prototype's ScoringKernel:
            kernel, s_score, decision, rationale, authority, evidence_quality
        plus TenirKernel's constraint_geometry.
        """
        hard_reasons: list[str] = []
        soft_reasons: list[str] = []

        # Compute the extra signals the prototype's ScoringKernel checked
        # but TenirKernel does not natively evaluate. These become gate reasons
        # injected into TenirKernel.adjudicate().
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

        # Missing mandatory epistemic inputs are a hard gate. A measured
        # value of zero is not "missing": it is real evidence for the geometry
        # to evaluate. Only None denotes absence.
        if authority is None:
            hard_reasons.append("authority missing: cannot adjudicate without authority assessment")
        if context_fit is None:
            hard_reasons.append("context_fit missing: cannot adjudicate without context assessment")
        if evidence_quality is None:
            hard_reasons.append("evidence_quality missing: cannot adjudicate without evidence")

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

        return {
            "kernel": "scoring",
            "s_score": tk_result.get("s_score"),
            "decision": decision,
            "rationale": tk_result.get("rationale", ""),
            "authority": authority,
            "context_fit": context_fit,
            "evidence_quality": evidence_quality,
            "constraint_geometry": tk_result.get("constraint_geometry"),
            "operating_mode": tk_result.get("operating_mode"),
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
    ):
        # scoring_kernel is a ScoringKernelAdapter wrapping TenirKernel,
        # or any object with a compatible adjudicate() interface.
        self.scoring = scoring_kernel
        self.membrane = membrane_kernel
        self.ledger = ledger or HashChainedLedger()
        self.policy = policy or CompositionPolicy()
        self.policy.validate()
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
        pressure: float = 0.5,
        velocity: float = 0.5,
        capacity: float = 1.0,
        option_space: float | None = None,
        authority: float | None = None,
        context_fit: float | None = None,
        evidence_quality: float | None = None,
        irreversibility_materiality: float = 0.0,
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

        try:
            authorized_payload_hash = canonical_payload_hash(payload)
        except (TypeError, ValueError) as exc:
            rationale = f"Invalid action payload; authorization refused: {exc}."
            self.ledger.append(
                kernel="composed", decision=Decision.HARD_VETO.value,
                rationale=rationale, passage_id=passage.passage_id,
                idempotency_key=idem_key,
            )
            return {
                "passage_id": passage.passage_id,
                "idempotency_key": idem_key,
                "composed_decision": Decision.HARD_VETO.value,
                "composed_rationale": rationale,
                "execution_status": "blocked",
                "operation_state": "not_registered",
                "ledger_entries": self.ledger.entry_count,
                "ledger_valid": self.ledger.verify_chain(),
            }

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
            rationale = "Invalid execution amount: must be finite and non-negative, and strictly positive when a resource is specified."
            self.ledger.append(
                kernel="composed", decision=Decision.HARD_VETO.value,
                rationale=rationale, passage_id=passage.passage_id,
                idempotency_key=idem_key,
            )
            return {
                "passage_id": passage.passage_id,
                "idempotency_key": idem_key,
                "scoring_decision": None,
                "membrane_decision": None,
                "composed_decision": Decision.HARD_VETO.value,
                "composed_rationale": rationale,
                "execution_status": "blocked",
                "operation_state": "not_registered",
                "ledger_entries": self.ledger.entry_count,
                "ledger_valid": self.ledger.verify_chain(),
            }
        amount = normalized_amount

        # Profile S binds dispatch permission to the complete immutable
        # operation.  Existing deployments remain unchanged because the gate
        # is disabled by default.
        if self.require_commit_authorization:
            if (
                not isinstance(payload_digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", payload_digest)
                or (payload is not None and authorized_payload_hash != payload_digest)
            ):
                rationale = "Profile S commit authorization requires a lowercase SHA-256 payload digest."
                self.ledger.append(
                    kernel="composed", decision=Decision.HARD_VETO.value,
                    rationale=rationale, passage_id=passage.passage_id,
                    idempotency_key=idem_key,
                )
                return {
                    "passage_id": passage.passage_id,
                    "idempotency_key": idem_key,
                    "composed_decision": Decision.HARD_VETO.value,
                    "composed_rationale": rationale,
                    "execution_status": "blocked",
                    "operation_state": "not_registered",
                    "ledger_entries": self.ledger.entry_count,
                    "ledger_valid": self.ledger.verify_chain(),
                }
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
                rationale = (
                    "Profile S commit authorization requires valid operation and institutional identities."
                )
                self.ledger.append(
                    kernel="composed", decision=Decision.HARD_VETO.value,
                    rationale=rationale, passage_id=passage.passage_id,
                    idempotency_key=idem_key,
                )
                return {
                    "passage_id": passage.passage_id,
                    "idempotency_key": idem_key,
                    "composed_decision": Decision.HARD_VETO.value,
                    "composed_rationale": rationale,
                    "execution_status": "blocked",
                    "operation_state": "not_registered",
                    "ledger_entries": self.ledger.entry_count,
                    "ledger_valid": self.ledger.verify_chain(),
                }

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
                "ledger_valid": self.ledger.verify_chain(),
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
        if (retains_operation and budget_in_scope and self.consensus_backend is not None):
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
                    success, _prev = self.registry.transition_to(idem_key, OperationState.RESERVED)
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
                    # organization-owned SLA. Scenario callers pass their bound explicitly.
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
                    "operation_state": self.registry.get(idem_key).state.value if self.registry.get(idem_key) else "unknown",
                    "ledger_entries": self.ledger.entry_count,
                    "ledger_valid": self.ledger.verify_chain(),
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
        return response

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
        _active_provider_call: bool = False,
    ) -> dict[str, Any]:
        # Never hold the quarantine lock across SQLite or another external
        # authority call.  A quick first check avoids consuming an
        # authorization while an already-active quarantine blocks dispatch.
        actual_payload = metadata if payload is None else payload
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
                if quarantine_active:
                    return {
                        "dispatched": False,
                        "reason": "QUARANTINE_LOCK is active; dispatch is blocked.",
                        "operation_state": record.state.value,
                    }
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
    ) -> dict[str, Any]:
        # dispatch() linearizes the claim against QUARANTINE_LOCK. Do not hold
        # the global quarantine lock while performing external I/O: a hung
        # provider must not prevent quarantine activation or unrelated dispatches.
        # A claim that already returned dispatched=True is not cancelled if
        # quarantine activates before provider.execute(). New claims are blocked.
        return self._dispatch_and_execute_under_quarantine_lock(
            idempotency_key, resource_id, amount, metadata, payload=payload,
        )

    def _dispatch_and_execute_under_quarantine_lock(
        self,
        idempotency_key: str,
        resource_id: str | None = None,
        amount: float = 0.0,
        metadata: dict[str, Any] | None = None,
        *,
        payload: dict[str, Any] | None = None,
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
                failed = self.fail(
                    idempotency_key, resource_id, amount,
                    authoritative_non_execution=True,
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

    def fail(self, idempotency_key: str, resource_id: str | None = None, amount: float = 0.0, *, authoritative_non_execution: bool = False) -> bool:
        """Serialize failure compensation with confirmation for this governor instance."""
        with self._settlement_lock:
            try:
                with self.registry.settlement_lock(
                    idempotency_key,
                    require_persistent=self.consensus_backend is not None,
                ):
                    return self._fail_under_settlement_lock(
                        idempotency_key, resource_id, amount,
                        authoritative_non_execution=authoritative_non_execution,
                    )
            except Exception:
                # Do not mutate budget if cross-process coordination is uncertain.
                return False

    def _fail_under_settlement_lock(self, idempotency_key: str, resource_id: str | None = None, amount: float = 0.0, *, authoritative_non_execution: bool = False) -> bool:
        """Transition to FAILED and mutate only this operation's budget.

        authoritative_non_execution must be true only when the provider or
        reconciliation protocol explicitly confirms that no external effect occurred.
        """
        record = self.registry.get(idempotency_key)
        if record is None:
            return False
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
        """Transition DISPATCHING → UNKNOWN. Outcome is uncertain."""
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
        pressure: float = 0.5,
        velocity: float = 0.5,
        capacity: float = 1.0,
        option_space: float | None = None,
        authority: float | None = None,
        context_fit: float | None = None,
        evidence_quality: float | None = None,
        irreversibility_materiality: float = 0.0,
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
            return {
                "hold_resolved": False,
                "operation_state": record.state.value,
                "reason": "Amount is invalid for HOLD re-adjudication.",
            }
        if supplied_amount == 0 and recorded_amount > 0:
            supplied_amount = recorded_amount
        if not math.isfinite(supplied_amount) or supplied_amount != recorded_amount:
            return {
                "hold_resolved": False,
                "operation_state": record.state.value,
                "reason": "Amount differs from the held operation.",
            }
        amount = supplied_amount

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
        evidence_quality: float | None, irreversibility_materiality: float,
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
            membrane_result = self.membrane.adjudicate(
                passage=passage, delegation=delegation,
                resource_id=resource_id, amount=amount,
                trajectory_id=trajectory_id,
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
        if observed_i6_veto or self._quarantine_blocks():
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
        if composed_decision in (Decision.PASS.value, Decision.FLAG.value):
            # No new reservation here: the HOLD already holds it. Serialize
            # the final admission, not the potentially slow kernel evaluation.
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
                    else:
                        success, _prev = self.registry.transition_to(
                            idem_key, OperationState.RESERVED,
                            hold_record_changes={"exit_reason": "resolved"},
                        )
                        if success:
                            hold_resolved = True
                        else:
                            composed_decision = Decision.HOLD.value
                            composed_rationale = (
                                "Re-adjudication produced PASS/FLAG but RESERVED "
                                "transition failed — staying in HOLD."
                            )
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
            "ledger_entries": self.ledger.entry_count,
            "ledger_valid": self.ledger.verify_chain(),
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
        """Expire an ESCALATED operation when τ_K expires → UNKNOWN.

        Per v7.1 §19: a time-based exit from ESCALATED is rejected.
        Expiry produces UNKNOWN, not resolution.
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
