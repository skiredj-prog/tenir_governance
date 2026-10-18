# Profile: agentic-governance-profile-v1.0.0
# Compliance class: profile-bound
"""Regression tests — budget accounting along HOLD-derived paths.

Invariant locked here: one reservation per operation, from CREATED to a
terminal state, whatever path the operation takes (HOLD → resolve,
HOLD → ESCALATED → UNKNOWN → FAILED, ...).

Both tests are written to FAIL on the code as it stands and PASS once
the governor's compensation logic is fixed:

  1. resolve_hold() re-runs the membrane, which reserves the budget a
     second time although the HOLD already holds a reservation.
  2. reconcile_unknown() always calls refund() on FAILED, even when the
     operation never reached DISPATCHING (nothing was committed), which
     leaves the reservation in place.
"""
import threading

from tenir_governance.composition import DualKernelGovernor, ScoringKernelAdapter
from tenir_governance.composition.decisions import Decision
from tenir_governance.membrane import TransitionMembrane, MembraneState
from tenir_governance.objects import Agent, Passage, Resource
from tenir_governance.operations import (
    MockProvider, OperationState, ProviderOutcome,
)
from tenir_kernel.kernel import TenirKernel


def _setup_governor(budget_capacity=100.0, provider=None):
    state = MembraneState()
    state.register_resource(Resource(resource_id="budget", capacity=budget_capacity))
    state.register_agent(Agent(agent_id="alice", authority_score=0.9,
                               trusted_delegates=frozenset({"bob"})))
    scoring = ScoringKernelAdapter(TenirKernel())
    membrane = TransitionMembrane(state)
    gov = DualKernelGovernor(scoring, membrane, provider=provider)
    return gov, state


class _BrokenScoring:
    """Scoring kernel that always raises → fail-closed HOLD."""

    def adjudicate(self, **kwargs):
        raise RuntimeError("forced HOLD for regression test")


def test_hold_resolve_then_dispatch_does_not_double_reserve():
    """HOLD → RESERVED → DISPATCHING → CONFIRMED : une seule réservation."""
    gov, state = _setup_governor(budget_capacity=100.0)
    budget = state.resources["budget"]

    # Forcer HOLD : scoring en échec, membrane passe et réserve.
    gov.scoring = _BrokenScoring()
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    adj = gov.adjudicate(
        p, pressure=0.2, velocity=0.2, capacity=0.9,
        resource_id="budget", amount=10.0,
    )

    assert adj["composed_decision"] == Decision.HOLD.value
    assert adj["operation_state"] == OperationState.HOLD.value
    assert budget.reserved == 10.0
    assert budget.consumed == 0.0

    # Ré-adjudication : scoring restauré, membrane passe.
    gov.scoring = ScoringKernelAdapter(TenirKernel())
    res = gov.resolve_hold(
        p.idempotency_key, passage=p,
        pressure=0.2, velocity=0.2, capacity=0.9,
        resource_id="budget", amount=10.0,
    )

    assert res["hold_resolved"] is True
    assert res["operation_state"] == OperationState.RESERVED.value
    assert budget.reserved == 10.0, (
        f"Double réservation détectée : reserved={budget.reserved}, attendu 10.0"
    )

    # Dispatch + confirm.
    gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0)
    assert gov.confirm(p.idempotency_key) is True

    assert gov.registry.get(p.idempotency_key).state == OperationState.CONFIRMED
    assert budget.reserved == 0.0
    assert budget.consumed == 10.0


def test_reconcile_from_escalated_unknown_releases_reservation():
    """ESCALATED → UNKNOWN → FAILED : release (rien n'a été commité), pas refund."""
    provider = MockProvider(
        behavior="timeout",
        reconcile_outcome=ProviderOutcome.FAILED,
    )
    gov, state = _setup_governor(provider=provider, budget_capacity=100.0)
    budget = state.resources["budget"]

    gov.scoring = _BrokenScoring()
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    gov.adjudicate(
        p, pressure=0.2, velocity=0.2, capacity=0.9,
        resource_id="budget", amount=10.0,
    )
    assert gov.registry.get(p.idempotency_key).state == OperationState.HOLD
    assert budget.reserved == 10.0
    assert budget.consumed == 0.0

    assert gov.escalate_hold(p.idempotency_key, escalated_by="risk-officer")["escalated"]
    assert gov.expire_escalated(p.idempotency_key)["expired"]
    assert gov.registry.get(p.idempotency_key).state == OperationState.UNKNOWN

    # Le provider répond : aucun effet.
    result = gov.reconcile_unknown(p.idempotency_key)
    assert result["reconciled"] is True
    assert result["operation_state"] == OperationState.FAILED.value

    assert budget.reserved == 0.0, (
        f"Réservation fantôme après ESCALATED→UNKNOWN→FAILED : reserved={budget.reserved}"
    )
    assert budget.consumed == 0.0


def test_concurrent_adjudication_reserves_budget_once():
    """Two concurrent PASS-able passages on a 1500 budget: exactly one reserves.

    The membrane check runs outside the governor's transition, so both may
    pass I8. The reservation point (_reserve_budget, under lock) is what must
    stay atomic.
    """
    gov, state = _setup_governor(budget_capacity=1500.0)
    budget = state.resources["budget"]
    decisions = []
    barrier = threading.Barrier(2)

    def attempt(amount):
        barrier.wait()
        p = Passage(agent_id="alice", action_type="payment", amount=amount)
        r = gov.adjudicate(
            p, pressure=0.2, velocity=0.2, capacity=0.9,
            resource_id="budget", amount=amount,
        )
        decisions.append(r["composed_decision"])

    ts = [threading.Thread(target=attempt, args=(1000.0,)) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    vetoes = [d for d in decisions if d == Decision.HARD_VETO.value]
    kept = [d for d in decisions if d != Decision.HARD_VETO.value]
    assert len(vetoes) == 1, f"expected exactly 1 HARD_VETO, got {decisions}"
    assert len(kept) == 1, f"expected exactly 1 kept operation, got {decisions}"
    assert budget.reserved == 1000.0, f"reserved={budget.reserved}, expected 1000.0"
    assert budget.consumed == 0.0
