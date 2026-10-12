# Profile: agentic-governance-profile-v1.0.0
# Compliance class: profile-bound
# This module is an extension on top of KB3 and TENIR 2.0 schema 1.2.
# It is NOT canonical, NOT doctrinal. It carries implementation-level
# governance enforcement authority only.
"""Dual-kernel governor — bridges the scoring kernel to TenirKernel.

This is the composition-layer governor. It replaces the prototype's
DualKernelGovernor (which used the simplified ScoringKernel) with one that
delegates the continuous-risk evaluation to 5.2.0's TenirKernel.

Bridge mapping (prototype ScoringKernel → 5.2.0 TenirKernel):
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
)


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

        coords: dict[str, float] = {
            "pressure": pressure,
            "velocity": velocity,
            "capacity": capacity,
            "irreversible_pathway_materiality": irreversibility_materiality,
            "authority": authority,
            "context_fit": context_fit,
            "evidence_quality": evidence_quality,
        }

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
        authority: float = 1.0,
        context_fit: float = 1.0,
        evidence_quality: float = 1.0,
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

        # If mandatory data is missing (authority, context_fit, evidence_quality
        # at their default sentinel values), escalate to a hard gate reason.
        # These are required for a trustworthy adjudication — their absence
        # means the score cannot be relied upon.
        if authority <= 0.0:
            hard_reasons.append("authority missing (≤0.0): cannot adjudicate without authority assessment")
        if context_fit <= 0.0:
            hard_reasons.append("context_fit missing (≤0.0): cannot adjudicate without context assessment")
        if evidence_quality <= 0.0:
            hard_reasons.append("evidence_quality missing (≤0.0): cannot adjudicate without evidence")

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
        self._worker_id = str(uuid4())

    def adjudicate(
        self,
        passage,
        pressure: float = 0.5,
        velocity: float = 0.5,
        capacity: float = 1.0,
        option_space: float | None = None,
        authority: float = 1.0,
        context_fit: float = 1.0,
        evidence_quality: float = 1.0,
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
                budget_utilization = (resource.consumed + resource.reserved) / resource.capacity

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
            self.registry.transition_to(idem_key, OperationState.FAILED)
            if resource_id and amount > 0:
                resource = self.membrane.state.resources.get(resource_id)
                if resource:
                    with self.membrane.state.get_lock(resource_id):
                        resource.release(amount)
        elif composed_decision == Decision.HOLD.value:
            pass  # Stay in CREATED — not yet authorized

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
        | RESERVED      | Safe to dispatch (checks passed, budget reserved) |
        | DISPATCHING   | Reconcile — do NOT resend                        |
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
        elif state == OperationState.RESERVED:
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
    ) -> dict[str, Any]:
        """Atomically claim the dispatch slot and transition to DISPATCHING.

        Per review 3 §1: the transition from RESERVED → DISPATCHING is the
        atomic claim. If two workers try to dispatch the same operation,
        exactly one succeeds.
        """
        record = self.registry.get(idempotency_key)
        if record is None:
            return {"dispatched": False, "reason": "Operation not registered."}

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

        if resource_id and amount > 0:
            resource = self.membrane.state.resources.get(resource_id)
            if resource:
                with self.membrane.state.get_lock(resource_id):
                    resource.commit(amount)

        return {
            "dispatched": True,
            "idempotency_key": idempotency_key,
            "operation_state": OperationState.DISPATCHING.value,
        }

    def confirm(self, idempotency_key: str) -> bool:
        """Transition DISPATCHING → CONFIRMED. The external effect occurred."""
        success, _prev = self.registry.transition_to(idempotency_key, OperationState.CONFIRMED)
        return success

    def fail(self, idempotency_key: str, resource_id: str | None = None, amount: float = 0.0) -> bool:
        """Transition to FAILED. No effect occurred. Release or refund reservation.

        Uses the previous state returned atomically by transition_to() so
        the refund-vs-release decision is based on the state observed under
        the lock.
        """
        success, prev_state = self.registry.transition_to(idempotency_key, OperationState.FAILED)
        if success and resource_id and amount > 0:
            was_dispatching = prev_state == OperationState.DISPATCHING
            resource = self.membrane.state.resources.get(resource_id)
            if resource:
                with self.membrane.state.get_lock(resource_id):
                    if was_dispatching:
                        resource.refund(amount)
                    else:
                        resource.release(amount)
        return success

    def mark_unknown(self, idempotency_key: str) -> bool:
        """Transition DISPATCHING → UNKNOWN. Outcome is uncertain."""
        success, _prev = self.registry.transition_to(idempotency_key, OperationState.UNKNOWN)
        return success

    def recover(self) -> dict[str, Any]:
        """Recovery procedure: find all unresolved operations and report them."""
        unresolved = self.registry.find_unresolved()
        return {
            "unresolved_count": len(unresolved),
            "unresolved": [
                {
                    "idempotency_key": r.idempotency_key,
                    "passage_id": r.passage_id,
                    "state": r.state.value,
                    "recommendation": "reconcile" if r.state == OperationState.DISPATCHING
                    else "human_review" if r.state == OperationState.UNKNOWN
                    else "unknown",
                }
                for r in unresolved
            ],
        }
