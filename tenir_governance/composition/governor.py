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
import math
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
from ..assessment.assessor import PostExecutionAssessor, AssessmentReport


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
    ):
        # scoring_kernel is a ScoringKernelAdapter wrapping TenirKernel,
        # or any object with a compatible adjudicate() interface.
        self.scoring = scoring_kernel
        self.membrane = membrane_kernel
        self.ledger = ledger or HashChainedLedger()
        self.policy = policy or CompositionPolicy()
        self.policy.validate()
        self.registry = registry or DurableOperationRegistry()
        self.budget_coordinator = budget_coordinator
        # Optional durable reservation backend. When supplied, this becomes
        # the budget source of truth for this governor instance.
        self.consensus_backend = consensus_backend
        self.provider = provider
        self._worker_id = str(uuid4())
        # Process-local guard for the window where provider.execute() is active.
        # Durable DISPATCHING recovery remains available after process restart.
        self._active_dispatches: set[str] = set()
        self._active_dispatches_lock = threading.RLock()
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
    ) -> dict[str, Any]:
        """Run both kernels, compose, ledger, and durably register.

        Both kernels ALWAYS run. Fail-closed: if either kernel throws an
        exception, the result is HOLD, never PASS.

        Per review 3 §1: the idempotency key is durably registered BEFORE
        any kernel evaluation, and the state machine tracks the operation
        through dispatch.
        """
        idem_key = passage.idempotency_key or f"op-{passage.passage_id}"

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

        # ── DURABLE CLAIM: register the operation BEFORE evaluation ──────
        record = OperationRecord(
            idempotency_key=idem_key,
            passage_id=passage.passage_id,
            agent_id=passage.agent_id,
            action_type=passage.action_type,
            amount=amount,
            resource_id=resource_id or "",
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

        # 6a. Budget ownership: a reservation is taken here, exactly once,
        # for every outcome that keeps the operation alive (PASS/FLAG/HOLD).
        # A failed reservation at this point (concurrent consumption since
        # the membrane check) is fail-closed to HARD_VETO.
        if composed_decision in (
            Decision.PASS.value, Decision.FLAG.value, Decision.HOLD.value,
        ):
            # Linearize final admission against QUARANTINE_LOCK even when no
            # budget is in scope. If a lock activates after I6's first check,
            # no outcome that retains the operation may pass this point.
            with self.membrane.state._quarantine_lock:
                if self.membrane.state.quarantine_active:
                    composed_decision = Decision.HARD_VETO.value
                    composed_rationale = (
                        "QUARANTINE_LOCK activated during adjudication; admission and reservation blocked."
                    )
                elif budget_in_scope and not self._reserve_budget(
                    idem_key, resource_id, amount,
                ):
                    composed_decision = Decision.HARD_VETO.value
                    composed_rationale = (
                        "Budget reservation failed at commit point (concurrent consumption) "
                        "— fail-closed HARD_VETO."
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
                # Transition failed — the operation cannot be reserved.
                # This is an inconsistent state: the decision is PASS but
                # the durable state does not confirm it. Return HOLD, not PASS.
                self.ledger.append(
                    kernel="composed",
                    decision=Decision.HOLD.value,
                    rationale=f"PASS decision but RESERVED transition failed — fail-closed HOLD.",
                    passage_id=passage.passage_id,
                    idempotency_key=idem_key,
                )
                return {
                    "passage_id": passage.passage_id,
                    "idempotency_key": idem_key,
                    "scoring_decision": scoring_result["decision"] if scoring_result else None,
                    "scoring_rationale": scoring_result["rationale"] if scoring_result else None,
                    "scoring_ces": scoring_result.get("ces_state") if scoring_result else None,
                    "scoring_s": scoring_result.get("s_score") if scoring_result else None,
                    "membrane_decision": membrane_result["decision"] if membrane_result else None,
                    "membrane_rationale": membrane_result["rationale"] if membrane_result else None,
                    "membrane_checks": membrane_result.get("checks", []) if membrane_result else [],
                    "composed_decision": Decision.HOLD.value,
                    "composed_rationale": "PASS decision but RESERVED transition failed — fail-closed HOLD.",
                    "execution_status": "pending",
                    "operation_state": self.registry.get(idem_key).state.value if self.registry.get(idem_key) else "unknown",
                    "ledger_entries": self.ledger.entry_count,
                    "ledger_valid": self.ledger.verify_chain(),
                }
        elif composed_decision == Decision.HARD_VETO.value:
            # No reservation is taken before the decision point (6a), so a
            # veto has nothing to release.
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

        return {
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
        | CREATED       | Re-adjudicate (no checks were run yet)            |
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

    def dispatch(
        self,
        idempotency_key: str,
        resource_id: str | None = None,
        amount: float = 0.0,
        *,
        _active_provider_call: bool = False,
    ) -> dict[str, Any]:
        # Linearize the quarantine check and dispatch claim against lock/unlock.
        with self.membrane.state._quarantine_lock:
            result = self._dispatch_under_quarantine_lock(idempotency_key, resource_id, amount)
            if result.get("dispatched") and _active_provider_call:
                with self._active_dispatches_lock:
                    self._active_dispatches.add(idempotency_key)
            return result

    def _dispatch_under_quarantine_lock(
        self,
        idempotency_key: str,
        resource_id: str | None = None,
        amount: float = 0.0,
    ) -> dict[str, Any]:
        """Atomically claim the dispatch slot and transition to DISPATCHING.

        Per review 3 §1: the transition from RESERVED → DISPATCHING is the
        atomic claim. If two workers try to dispatch the same operation,
        exactly one succeeds.
        """
        record = self.registry.get(idempotency_key)
        if record is None:
            return {"dispatched": False, "reason": "Operation not registered."}

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

        # QUARANTINE_LOCK blocks dispatch, including operations reserved before
        # the lock was activated. Check before the durable dispatch transition.
        if self.membrane.state.quarantine_active:
            return {
                "dispatched": False,
                "reason": "QUARANTINE_LOCK is active; dispatch is blocked.",
                "operation_state": record.state.value,
            }

        success, _prev = self.registry.transition_to(
            idempotency_key, OperationState.DISPATCHING,
            claimed_by=self._worker_id,
        )
        if not success:
            existing = self.registry.get(idempotency_key)
            return {
                "dispatched": False,
                "reason": f"Cannot dispatch — operation is in state {existing.state.value}.",
                "operation_state": existing.state.value,
            }

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

    def dispatch_and_execute(
        self,
        idempotency_key: str,
        resource_id: str | None = None,
        amount: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # dispatch() linearizes the claim against QUARANTINE_LOCK. Do not hold
        # the global quarantine lock while performing external I/O: a hung
        # provider must not prevent quarantine activation or unrelated dispatches.
        return self._dispatch_and_execute_under_quarantine_lock(
            idempotency_key, resource_id, amount, metadata,
        )

    def _dispatch_and_execute_under_quarantine_lock(
        self,
        idempotency_key: str,
        resource_id: str | None = None,
        amount: float = 0.0,
        metadata: dict[str, Any] | None = None,
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
        # 1. Atomic claim
        dispatch_result = self.dispatch(
            idempotency_key, resource_id, amount,
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
                metadata=metadata,
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
                elif durable_reservation.get("state") in ("RESERVED", "UNKNOWN"):
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
        """Confirm an external effect only after durable budget ownership agrees."""
        record = self.registry.get(idempotency_key)
        if record is None:
            return False
        if self.consensus_backend is not None and record.resource_id and record.amount > 0:
            if not record.reservation_id:
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
        HOLD, and ESCALATED (hold-lifecycle) states.
        """
        unresolved = self.registry.find_unresolved()
        holds = self.hold_manager.find_holds()
        escalated = self.hold_manager.find_escalated()
        all_unresolved = unresolved + holds + escalated

        def _recommendation(state: OperationState) -> str:
            if state == OperationState.DISPATCHING:
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
    ) -> dict[str, Any]:
        """Re-adjudicate a HOLD operation with new evidence.

        Per doctrine: when evidence becomes sufficient, the operator
        re-runs adjudication. If the new decision is PASS or FLAG,
        transition HOLD → RESERVED. If still HOLD, stay in HOLD. If
        HARD_VETO, transition to FAILED.

        The caller MUST provide the same passage (or a new one with the
        same idempotency key) and the updated evidence parameters. Omitting
        passage is rejected: it must never bypass fresh adjudication.

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

        # Re-run adjudication with new evidence
        # The passage must have the same idempotency key
        if passage.idempotency_key != idempotency_key:
            passage.idempotency_key = idempotency_key

        # Temporarily remove the record so re-registration succeeds
        # (the key is already claimed in HOLD state)
        # We bypass the register() check by directly transitioning
        result = self._re_adjudicate_hold(
            passage, pressure, velocity, capacity, option_space,
            authority, context_fit, evidence_quality, irreversibility_materiality,
            delegation, resource_id, amount, trajectory_id,
        )
        return result

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
        if composed_decision in (Decision.PASS.value, Decision.FLAG.value):
            # No new reservation here: the HOLD already holds it (budget_reserved).
            success, _prev = self.registry.transition_to(idem_key, OperationState.RESERVED)
            if success:
                hold_resolved = True
                if record := self.registry.get(idem_key):
                    if record.hold_record:
                        record.hold_record.exit_reason = "resolved"
            else:
                # Transition failed — stay in HOLD
                composed_decision = Decision.HOLD.value
                composed_rationale = "Re-adjudication produced PASS/FLAG but RESERVED transition failed — staying in HOLD."
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
                            success, _prev = self.registry.transition_to(idem_key, OperationState.FAILED)
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
                                success, _prev = self.registry.transition_to(idem_key, OperationState.FAILED)
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
                success, _prev = self.registry.transition_to(idem_key, OperationState.FAILED)
                if success:
                    hold_resolved = True
                else:
                    composed_decision = Decision.HOLD.value
                    composed_rationale = "HARD_VETO re-adjudication lost the terminal transition race; budget unchanged."
            if record := self.registry.get(idem_key):
                if record.hold_record:
                    record.hold_record.exit_reason = "resolved_veto" if hold_resolved else ""
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
        return self.hold_manager.escalate_hold(idempotency_key, escalated_by)

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
        result = self.hold_manager.authorize_exit(
            idempotency_key, authorized_by, scope, authorization, reason, approve,
        )
        # approve → RESERVED keeps the reservation taken at HOLD (no re-reserve).
        # deny → FAILED releases it; otherwise the budget leaks.
        if result.get("exited") and not approve:
            record = self.registry.get(idempotency_key)
            if record and record.budget_reserved and record.resource_id and record.amount > 0:
                if self.consensus_backend is not None and not record.reservation_id:
                    result["budget_reconciliation_required"] = True
                    result["reason"] = "Durable reservation identity missing; budget release blocked."
                    return result
                if self.consensus_backend is not None and record.reservation_id:
                    try:
                        released = self.consensus_backend.release(record.reservation_id)
                    except Exception:
                        released = None
                    if getattr(released, "status", None) not in ("RELEASED", "ALREADY_RELEASED"):
                        result["budget_reconciliation_required"] = True
                        result["reason"] = "Exit recorded, but durable budget release is unresolved."
                        return result
                    self.registry.set_budget_flags(
                        idempotency_key, budget_reserved=False,
                        budget_committed=False, reservation_id=record.reservation_id,
                    )
                else:
                    resource = self.membrane.state.resources.get(record.resource_id)
                    if resource:
                        with self.membrane.state.get_lock(record.resource_id):
                            resource.release(record.amount)
                    self.registry.set_budget_flags(idempotency_key, budget_reserved=False)
        return result

    def expire_hold(self, idempotency_key: str) -> dict[str, Any]:
        """Expire a HOLD when τ_K is exhausted — FAIL-CLOSED.

        Per doctrine: when τ_K expires on an unresolved HOLD, the default
        behavior is FAIL-CLOSED: the action is rejected. This is NOT a
        resolution — it is a fail-closed rejection.

        Releases any reserved budget.
        """
        record = self.registry.get(idempotency_key)
        if record is None:
            return {"expired": False, "reason": f"Operation '{idempotency_key}' not found."}

        result = self.hold_manager.expire_hold(idempotency_key)

        # Release any reserved budget
        if result.get("expired") and record.resource_id and record.amount > 0:
            if self.consensus_backend is not None and not record.reservation_id and (record.budget_reserved or record.budget_committed):
                result["budget_reconciliation_required"] = True
                result["reason"] = "Durable reservation identity missing; budget release blocked."
                return result
            if self.consensus_backend is not None and record.reservation_id:
                if record.budget_committed:
                    result["budget_reconciliation_required"] = True
                    result["reason"] = "HOLD expiry found a committed reservation; refusing release without reconciliation."
                    return result
                try:
                    released = self.consensus_backend.release(record.reservation_id)
                except Exception:
                    released = None
                if getattr(released, "status", None) not in ("RELEASED", "ALREADY_RELEASED"):
                    result["budget_reconciliation_required"] = True
                    result["reason"] = "HOLD expired, but durable budget release is unresolved."
                    return result
                if record.budget_reserved or record.budget_committed:
                    self.registry.set_budget_flags(
                        idempotency_key, budget_reserved=False,
                        budget_committed=False, reservation_id=record.reservation_id,
                    )
            else:
                resource = self.membrane.state.resources.get(record.resource_id)
                if resource:
                    with self.membrane.state.get_lock(record.resource_id):
                        resource.release(record.amount)
                if record.budget_reserved or record.budget_committed:
                    self.registry.set_budget_flags(
                        idempotency_key, budget_reserved=False, budget_committed=False,
                    )

        return result

    def expire_escalated(self, idempotency_key: str) -> dict[str, Any]:
        """Expire an ESCALATED operation when τ_K expires → UNKNOWN.

        Per v7.1 §19: a time-based exit from ESCALATED is rejected.
        Expiry produces UNKNOWN, not resolution.
        """
        return self.hold_manager.expire_escalated(idempotency_key)

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
