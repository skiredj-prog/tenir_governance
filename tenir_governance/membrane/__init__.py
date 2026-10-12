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
from dataclasses import dataclass, field
from typing import Any, Optional

from ..objects import Agent, Passage, Trajectory, Delegation, Resource, MissionInvariant
from ..objects.passage import ExecutionStatus as PassageStatus
from ..composition.decisions import Decision, CheckType


@dataclass
class MembraneState:
    """Mutable state of the transition-system membrane."""
    agents: dict[str, Agent] = field(default_factory=dict)
    resources: dict[str, Resource] = field(default_factory=dict)
    trajectories: dict[str, Trajectory] = field(default_factory=dict)
    passages: dict[str, Passage] = field(default_factory=dict)
    quarantine: bool = False
    invariants: list[MissionInvariant] = field(default_factory=list)
    # Per-resource locks for atomic budget reservations
    _locks: dict[str, threading.Lock] = field(default_factory=dict)

    def register_agent(self, agent: Agent) -> None:
        self.agents[agent.agent_id] = agent

    def register_resource(self, resource: Resource) -> None:
        self.resources[resource.resource_id] = resource
        self._locks[resource.resource_id] = threading.Lock()

    def register_trajectory(self, trajectory: Trajectory) -> None:
        self.trajectories[trajectory.trajectory_id] = trajectory

    def add_invariant(self, inv: MissionInvariant) -> None:
        self.invariants.append(inv)

    def get_lock(self, resource_id: str) -> threading.Lock:
        """Get the lock for a resource, creating one if needed."""
        if resource_id not in self._locks:
            self._locks[resource_id] = threading.Lock()
        return self._locks[resource_id]


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
    ) -> dict[str, Any]:
        """Evaluate all applicable guards for a proposed passage.

        Returns a dict with:
            - decision: the policy decision (PASS/FLAG/HOLD/HARD_VETO)
            - checks: list of CheckResult for each invariant checked
            - execution_status: the passage's current execution status
        """
        checks: list[CheckResult] = []

        # I6 — No admission under quarantine (MANDATORY)
        if self.state.quarantine:
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
        _reserved_amount = 0.0
        if resource_id is not None and amount > 0:
            resource = self.state.resources.get(resource_id)
            if resource is None:
                checks.append(CheckResult("I8", CheckType.MANDATORY, Decision.HARD_VETO,
                                           f"I8: Resource '{resource_id}' not registered."))
            else:
                # ATOMIC reservation under lock — prevents race condition
                lock = self.state.get_lock(resource_id)
                with lock:
                    if resource.available >= amount:
                        resource.reserve(amount)
                        _reserved_amount = amount
                        checks.append(CheckResult("I8", CheckType.MANDATORY, Decision.PASS,
                                                   f"I8: Budget OK — reserved {amount}, available now {resource.available:.2f}."))
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
            "quarantine": self.state.quarantine,
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
            # Release any reservation we made
            if _reserved_amount > 0 and resource_id:
                resource = self.state.resources.get(resource_id)
                if resource:
                    with self.state.get_lock(resource_id):
                        resource.release(_reserved_amount)
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
        passage.execution_status = PassageStatus.DISPATCHED
        if resource_id and amount > 0:
            resource = self.state.resources.get(resource_id)
            if resource:
                with self.state.get_lock(resource_id):
                    resource.commit(amount)

    def fail_passage(self, passage: Passage, resource_id: str | None = None, amount: float = 0.0) -> None:
        """Mark a passage as failed — release any reservation."""
        passage.execution_status = PassageStatus.FAILED
        if resource_id and amount > 0:
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


__all__ = [
    "TransitionMembrane",
    "MembraneState",
    "CheckResult",
    "DISCLOSURE",
]