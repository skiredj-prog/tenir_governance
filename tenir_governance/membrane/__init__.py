"""Transition-System Membrane — enforces discrete invariants across governed objects.

Revised per review feedback:
- Budget reservations are now atomic (threading.Lock) to prevent race conditions
- Checks are tagged MANDATORY or RISK_BASED
- Uncertain outcomes (UNKNOWN) trigger HOLD + reconciliation, not HARD_VETO
- Execution status is separate from policy decision
"""
# Profile: agentic-governance-profile-v1.0.0
# Compliance class: profile-bound
# This module is an extension on top of KB3 and TENIR 2.0 schema 1.2.
# It is NOT canonical, NOT doctrinal. It carries implementation-level
# governance enforcement authority only.
#
# NOTE: This is the discrete invariant enforcer (I3–I9: delegation trust,
# budget limits, quarantine, terminality, trajectory, temporal constraints).
# It is distinct from PolicyEngine.evaluate_membrane() which is the
# score-threshold membrane. Both coexist in the composition layer.
from __future__ import annotations

from .._profile_meta import DISCLOSURE

import threading
from uuid import uuid4
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from ..objects import Agent, Passage, Trajectory, Delegation, Resource, MissionInvariant
from ..objects.passage import ExecutionStatus as PassageStatus
from ..composition.decisions import Decision, CheckType
from ..operations.budget import _positive_finite_float
from ..operations.registry import _verify_quarantine_release


def _utc_now() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class MembraneState:
    """Mutable state of the transition-system membrane."""
    agents: dict[str, Agent] = field(default_factory=dict)
    resources: dict[str, Resource] = field(default_factory=dict)
    trajectories: dict[str, Trajectory] = field(default_factory=dict)
    passages: dict[str, Passage] = field(default_factory=dict)
    quarantine: bool = False
    quarantine_locked_at: str = ""
    quarantine_locked_by: str = ""
    quarantine_release_receipt: dict | None = None
    invariants: list[MissionInvariant] = field(default_factory=list)
    # Per-resource locks for atomic budget reservations
    _locks: dict[str, threading.Lock] = field(default_factory=dict)
    _quarantine_lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False, compare=False)
    quarantine_release_verifier: Any = field(default=None, repr=False, compare=False)
    _quarantine_registry: Any = field(default=None, init=False, repr=False, compare=False)
    _quarantine_lock_id: str = field(default="", init=False, repr=False, compare=False)

    def bind_quarantine_registry(self, registry) -> None:
        """All public membrane entry points use the governor's durable store."""
        with self._quarantine_lock:
            if self._quarantine_registry is not None and self._quarantine_registry is not registry:
                raise RuntimeError("MembraneState already bound to a different operation registry.")
            self._quarantine_registry = registry
            if self.quarantine:
                registry.set_quarantine(True, locked_by=self.quarantine_locked_by)

    def register_agent(self, agent: Agent) -> None:
        self.agents[agent.agent_id] = agent

    def register_resource(self, resource: Resource) -> None:
        """Register once; replacing a live resource would orphan its budget and lock."""
        # Serialize check-and-insert so concurrent first registrations cannot
        # replace the resource object or the lock protecting its accounting.
        with self._quarantine_lock:
            existing = self.resources.get(resource.resource_id)
            if existing is not None:
                if existing is resource:
                    return
                raise ValueError(
                    f"Resource {resource.resource_id!r} is already registered; "
                    "resource replacement is prohibited because it can reset accounting."
                )
            self.resources[resource.resource_id] = resource
            self._locks.setdefault(resource.resource_id, threading.Lock())

    def register_trajectory(self, trajectory: Trajectory) -> None:
        self.trajectories[trajectory.trajectory_id] = trajectory

    def add_invariant(self, inv: MissionInvariant) -> None:
        self.invariants.append(inv)

    def get_lock(self, resource_id: str) -> threading.Lock:
        """Get the unique lock for a resource, creating it atomically."""
        with self._quarantine_lock:
            lock = self._locks.get(resource_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[resource_id] = lock
            return lock

    def lock_quarantine(self, locked_by: str = "") -> None:
        with self._quarantine_lock:
            if self._quarantine_registry is not None:
                # Durable mark must exist before acknowledging the local lock.
                self._quarantine_registry.set_quarantine(True, locked_by=locked_by)
            self._lock_quarantine_under_lock(locked_by)

    def _lock_quarantine_under_lock(self, locked_by: str = "") -> None:
        """Activate QUARANTINE_LOCK — blocks all new admission and dispatch.

        Per v7.1 §6: QUARANTINE_LOCK blocks new attempt admission and
        execution dispatch for the LEI until an authorized release or
        resolution transition succeeds.

        This is distinct from a HOLD decision — QUARANTINE_LOCK is a
        persistent LEI-level guard, not a per-operation disposition.
        """
        if not self.quarantine:
            self._quarantine_lock_id = uuid4().hex
        self.quarantine = True
        self.quarantine_locked_at = _utc_now()
        self.quarantine_locked_by = locked_by
        self.quarantine_release_receipt = None

    def release_quarantine(
        self,
        authorized_by: str,
        scope: str,
        authorization: str,
        reason: str = "",
    ) -> dict[str, Any]:
        with self._quarantine_lock:
            if self._quarantine_registry is not None:
                # The durable registry verifies the authorization and commits
                # the shared release before the local flag can be cleared.
                result = self._quarantine_registry.authorized_release_quarantine(
                    authorized_by=authorized_by, scope=scope,
                    authorization=authorization, reason=reason,
                )
                if result.get("released"):
                    self.quarantine = False
                    self.quarantine_release_receipt = result["receipt"]
                    self.quarantine_locked_at = ""
                    self.quarantine_locked_by = ""
                    self._quarantine_lock_id = ""
                return result
            return self._release_quarantine_under_lock(authorized_by, scope, authorization, reason)

    def _release_quarantine_under_lock(
        self,
        authorized_by: str,
        scope: str,
        authorization: str,
        reason: str = "",
    ) -> dict[str, Any]:
        """Release QUARANTINE_LOCK — requires authenticated authorization.

        Per v7.1 §6: Release must be authenticated and authorized under
        the applicable governance contract.

        Per Blueprint v4.4 §5: overrides constitutionnels uniquement via
        Ceremony G-R5-001 (authentification opérateur).

        Returns:
            dict with released (bool), receipt, and reason
        """
        if not self.quarantine:
            return {
                "released": False,
                "reason": "No quarantine lock active.",
                "receipt": None,
            }

        proof = _verify_quarantine_release(
            self.quarantine_release_verifier,
            authorized_by=authorized_by, scope=scope,
            authorization=authorization, reason=reason,
            lock_id=self._quarantine_lock_id,
        )
        if proof is None:
            return {
                "released": False,
                "receipt": None,
                "reason": "QUARANTINE_LOCK release denied: authorization not verified.",
            }
        receipt = {
            "authorization_id": proof["authorization_id"],
            "lock_id": proof["lock_id"],
            "authorized_by": authorized_by,
            "scope": scope,
            "timestamp": _utc_now(),
            "authorization": authorization,
            "reason": reason,
            "locked_at": self.quarantine_locked_at,
            "locked_by": self.quarantine_locked_by,
        }

        self.quarantine = False
        self.quarantine_release_receipt = receipt
        self.quarantine_locked_at = ""
        self.quarantine_locked_by = ""
        self._quarantine_lock_id = ""

        return {
            "released": True,
            "receipt": receipt,
            "reason": f"Quarantine lock released by {authorized_by} via {authorization}.",
        }

    @property
    def quarantine_active(self) -> bool:
        """Check if quarantine lock is currently active."""
        if self._quarantine_registry is not None:
            # Shared durable state is authoritative. A different worker can
            # release an earlier lock while this worker's local flag remains
            # set; stale memory must not permanently block valid admissions.
            return self._quarantine_registry.quarantine_is_active()
        return self.quarantine


@dataclass
class CheckResult:
    """Result of a single invariant check."""
    check_id: str
    check_type: CheckType
    decision: Decision
    rationale: str


class TransitionMembrane:
    """Evaluates discrete invariants over governed objects.

    Each guard checks a specific invariant. Guards are tagged as MANDATORY
    (must run for every action in scope, cannot be skipped) or RISK_BASED
    (may be selectively invoked based on routing policy).

    The membrane separates policy decisions from execution status.
    """

    def __init__(self, state: MembraneState | None = None):
        self.state = state or MembraneState()

    def adjudicate(
        self,
        passage: Passage,
        delegation: Delegation | None = None,
        resource_id: str | None = None,
        amount: float = 0.0,
        trajectory_id: str | None = None,
        existing_reservation_amount: float = 0.0,
    ) -> dict[str, Any]:
        """Evaluate all applicable guards for a proposed passage.

        Returns a dict with:
            - decision: the policy decision (PASS/FLAG/HOLD/HARD_VETO)
            - checks: list of CheckResult for each invariant checked
            - execution_status: the passage's current execution status
        """
        checks: list[CheckResult] = []

        # I6 — No admission under quarantine (MANDATORY)
        if self.state.quarantine_active:
            checks.append(CheckResult("I6", CheckType.MANDATORY, Decision.HARD_VETO,
                                       "I6: System is under quarantine lock — no admissions permitted."))
        else:
            checks.append(CheckResult("I6", CheckType.MANDATORY, Decision.PASS,
                                       "I6: No quarantine active."))

        # I3 — Attempt terminality (MANDATORY)
        if passage.is_terminal:
            checks.append(CheckResult("I3", CheckType.MANDATORY, Decision.HARD_VETO,
                                       f"I3: Passage {passage.passage_id} is already terminal."))
        else:
            checks.append(CheckResult("I3", CheckType.MANDATORY, Decision.PASS,
                                       "I3: Passage is not terminal."))

        # I4 — Only dispatch produces effects (MANDATORY)
        if passage.execution_status not in (PassageStatus.PENDING, PassageStatus.HELD):
            checks.append(CheckResult("I4", CheckType.MANDATORY, Decision.HARD_VETO,
                                       f"I4: Passage is in state {passage.execution_status.value} — cannot dispatch."))
        else:
            checks.append(CheckResult("I4", CheckType.MANDATORY, Decision.PASS,
                                       "I4: Passage is in a dispatchable state."))

        # I7 — Delegation integrity (MANDATORY when delegation is in scope)
        if delegation is not None:
            delegating_agent = self.state.agents.get(delegation.delegating_agent_id)
            if delegating_agent is None:
                checks.append(CheckResult("I7", CheckType.MANDATORY, Decision.HARD_VETO,
                                           f"I7: Delegating agent {delegation.delegating_agent_id} not registered."))
            elif not delegating_agent.can_delegate_to(delegation.delegatee_agent_id):
                checks.append(CheckResult("I7", CheckType.MANDATORY, Decision.HARD_VETO,
                                           f"I7: Delegation integrity violation — agent '{delegation.delegating_agent_id}' "
                                           f"cannot delegate to '{delegation.delegatee_agent_id}'."))
            else:
                checks.append(CheckResult("I7", CheckType.MANDATORY, Decision.PASS,
                                           "I7: Delegation to trusted agent."))
        # If no delegation in scope, I7 is not applicable — don't check

        # I8 — Aggregate budget (MANDATORY when resource is in scope)
        # Pure check: the membrane never mutates the budget. Reservation is
        # owned by the governor's state transitions (see DualKernelGovernor).
        if resource_id is not None:
            normalized_amount = _positive_finite_float(amount)
            if normalized_amount is None:
                checks.append(CheckResult(
                    "I8", CheckType.MANDATORY, Decision.HARD_VETO,
                    "I8: Budget amount must be finite and strictly positive.",
                ))
            else:
                amount = normalized_amount
                resource = self.state.resources.get(resource_id)
                if resource is None:
                    checks.append(CheckResult("I8", CheckType.MANDATORY, Decision.HARD_VETO,
                                               f"I8: Resource '{resource_id}' not registered."))
                else:
                    lock = self.state.get_lock(resource_id)
                    with lock:
                        existing = _positive_finite_float(existing_reservation_amount)
                        if not isinstance(existing_reservation_amount, bool) and existing_reservation_amount == 0:
                            existing = 0.0
                        if existing is None:
                            checks.append(CheckResult(
                                "I8", CheckType.MANDATORY, Decision.HARD_VETO,
                                "I8: Existing reservation amount is invalid.",
                            ))
                        elif resource.available + existing >= amount:
                            checks.append(CheckResult("I8", CheckType.MANDATORY, Decision.PASS,
                                                       f"I8: Budget OK — {amount} within available {resource.available + existing:.2f}."))
                        else:
                            checks.append(CheckResult("I8", CheckType.MANDATORY, Decision.HARD_VETO,
                                                       f"I8: Aggregate budget violation — requested {amount} but only "
                                                       f"{resource.available:.2f} available."))

        # I9 — Trajectory temporal consistency (MANDATORY when trajectory is in scope)
        if trajectory_id is not None:
            trajectory = self.state.trajectories.get(trajectory_id)
            if trajectory is not None:
                ok, reason = trajectory.check_temporal_consistency()
                if not ok:
                    checks.append(CheckResult("I9", CheckType.MANDATORY, Decision.HARD_VETO,
                                               f"I9: Temporal constraint violated — {reason}"))
                else:
                    checks.append(CheckResult("I9", CheckType.MANDATORY, Decision.PASS,
                                               "I9: Temporal constraints satisfied."))

        # Mission invariants
        system_state = {
            "agents": len(self.state.agents),
            "resources": {rid: r.consumed for rid, r in self.state.resources.items()},
            "quarantine": self.state.quarantine_active,
        }
        for inv in self.state.invariants:
            holds, reason = inv.evaluate(system_state)
            if not holds:
                severity = Decision.HARD_VETO if inv.severity == "hard" else Decision.FLAG
                checks.append(CheckResult(inv.invariant_id, CheckType.MANDATORY, severity, reason))

        # Compose checks into a single membrane decision
        # HARD_VETO from any check → HARD_VETO
        # HOLD from any check → HOLD (unless HARD_VETO)
        # FLAG from any check → FLAG (unless HOLD or HARD_VETO)
        # All PASS → PASS
        hard_reasons = [c for c in checks if c.decision == Decision.HARD_VETO]
        if hard_reasons:
            decision = Decision.HARD_VETO
            rationale = "; ".join(c.rationale for c in hard_reasons)
        else:
            hold_reasons = [c for c in checks if c.decision == Decision.HOLD]
            if hold_reasons:
                decision = Decision.HOLD
                rationale = "; ".join(c.rationale for c in hold_reasons)
            else:
                flag_reasons = [c for c in checks if c.decision == Decision.FLAG]
                if flag_reasons:
                    decision = Decision.FLAG
                    rationale = "; ".join(c.rationale for c in flag_reasons)
                else:
                    decision = Decision.PASS
                    rationale = "All invariants hold."

        return {
            "kernel": "membrane",
            "decision": decision.value,
            "rationale": rationale,
            "passage_id": passage.passage_id,
            "checks": [{"check_id": c.check_id, "type": c.check_type.value,
                        "decision": c.decision.value, "rationale": c.rationale} for c in checks],
            "execution_status": passage.execution_status.value,
        }

    def commit_passage(self, passage: Passage, resource_id: str | None = None, amount: float = 0.0) -> None:
        """Commit a passage that was allowed — update execution status and resource state."""
        if resource_id is not None:
            amount = _positive_finite_float(amount)
            if amount is None:
                raise ValueError("Budget amount must be finite and strictly positive.")
        passage.execution_status = PassageStatus.DISPATCHED
        if resource_id is not None:
            resource = self.state.resources.get(resource_id)
            if resource:
                with self.state.get_lock(resource_id):
                    resource.commit(amount)

    def fail_passage(self, passage: Passage, resource_id: str | None = None, amount: float = 0.0) -> None:
        """Mark a passage as failed — release any reservation."""
        if resource_id is not None:
            amount = _positive_finite_float(amount)
            if amount is None:
                raise ValueError("Budget amount must be finite and strictly positive.")
        passage.execution_status = PassageStatus.FAILED
        if resource_id is not None:
            resource = self.state.resources.get(resource_id)
            if resource:
                with self.state.get_lock(resource_id):
                    resource.release(amount)

    def reconcile_unknown(self, passage: Passage, resource_id: str | None = None, amount: float = 0.0) -> str:
        """Reconcile an uncertain outcome (dispatch followed by timeout/lost response).

        Per review §4: an unknown execution outcome should NOT automatically be
        called a hard violation. The correct response is to hold further
        conflicting activity and reconcile the outcome.

        Returns:
            "confirmed" — the effect was confirmed to have occurred.
            "failed" — the effect was confirmed to have NOT occurred.
            "unknown" — still uncertain; remain in HOLD.
        """
        # In a real system, this would query the external provider.
        # For the prototype, we return "unknown" and leave the passage in HOLD.
        passage.execution_status = PassageStatus.HELD
        return "unknown"

    def lock_quarantine(self, locked_by: str = "") -> None:
        """Activate QUARANTINE_LOCK — blocks all new admission and dispatch.

        Per v7.1 §6: QUARANTINE_LOCK blocks new attempt admission and
        execution dispatch for the LEI until an authorized release or
        resolution transition succeeds.

        This is distinct from a HOLD decision — QUARANTINE_LOCK is a
        persistent LEI-level guard, not a per-operation disposition.
        """
        self.state.lock_quarantine(locked_by)

    def release_quarantine(
        self,
        authorized_by: str,
        scope: str,
        authorization: str,
        reason: str = "",
    ) -> dict[str, Any]:
        """Release QUARANTINE_LOCK — requires authenticated authorization.

        Per v7.1 §6: Release must be authenticated and authorized under
        the applicable governance contract.

        Per Blueprint v4.4 §5: overrides constitutionnels uniquement via
        Ceremony G-R5-001 (authentification opérateur).

        Args:
            authorized_by: who is authorizing the release (operator ID)
            scope: scope of the authorization
            authorization: authorization mechanism (e.g., "ceremony-G-R5-001")
            reason: why the release is being authorized

        Returns:
            dict with released (bool), receipt, and reason
        """
        return self.state.release_quarantine(
            authorized_by, scope, authorization, reason,
        )

    @property
    def quarantine_active(self) -> bool:
        """Check if quarantine lock is currently active."""
        return self.state.quarantine_active

    @property
    def quarantine_locked_by(self) -> str:
        return self.state.quarantine_locked_by

    @property
    def quarantine_locked_at(self) -> str:
        return self.state.quarantine_locked_at

    @property
    def quarantine_release_receipt(self) -> dict | None:
        return self.state.quarantine_release_receipt


__all__ = [
    "TransitionMembrane",
    "MembraneState",
    "CheckResult",
    "DISCLOSURE",
]
