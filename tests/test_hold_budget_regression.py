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
import json
import pytest
from decimal import Decimal

from tenir_governance.operations.budget import DurableBudgetCoordinator
from tenir_governance.operations.consensus_backend import SingleHostLockBackend

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


def test_reconciliation_flag_write_failure_leaves_budget_untouched(monkeypatch):
    provider = MockProvider(
        behavior="timeout",
        reconcile_outcome=ProviderOutcome.FAILED,
    )
    gov, state = _setup_governor(provider=provider, budget_capacity=100.0)
    budget = state.resources["budget"]
    passage = Passage(agent_id="alice", action_type="payment", amount=10.0)
    result = gov.adjudicate(
        passage, pressure=0.2, velocity=0.2, capacity=0.9,
        resource_id="budget", amount=10.0,
    )
    assert result["operation_state"] == OperationState.RESERVED.value

    executed = gov.dispatch_and_execute(passage.idempotency_key, "budget", 10.0)
    assert executed["operation_state"] == OperationState.UNKNOWN.value
    assert budget.reserved == 0.0
    assert budget.consumed == 10.0

    def fail_flags(*_args, **_kwargs):
        raise OSError("simulated registry write failure")

    monkeypatch.setattr(gov.registry, "set_budget_flags", fail_flags)
    reconciled = gov.reconcile_unknown(passage.idempotency_key)
    assert reconciled["reconciled"] is False
    assert "ownership release" in reconciled["reason"]
    assert gov.registry.get(passage.idempotency_key).state == OperationState.UNKNOWN
    assert budget.reserved == 0.0
    assert budget.consumed == 10.0


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


# ─── authorize_exit: budget accounting on both exit paths ──────────────

def test_authorize_exit_deny_releases_reservation():
    """ESCALATED → authorize_exit(deny) → FAILED: reservation released, flags cleared."""
    gov, state = _setup_governor(budget_capacity=100.0)
    budget = state.resources["budget"]

    # Force HOLD via broken scoring.
    gov.scoring = _BrokenScoring()
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    gov.adjudicate(
        p, pressure=0.2, velocity=0.2, capacity=0.9,
        resource_id="budget", amount=10.0,
    )
    assert budget.reserved == 10.0
    assert budget.consumed == 0.0

    # Escalate the HOLD.
    assert gov.escalate_hold(p.idempotency_key, escalated_by="risk-officer")["escalated"]
    assert gov.registry.get(p.idempotency_key).state == OperationState.ESCALATED

    # Deny exit → FAILED, reservation must be released.
    result = gov.authorize_exit(
        p.idempotency_key,
        authorized_by="ciso",
        scope="global",
        authorization="ceremony-G-R5-001",
        reason="Risk too high",
        approve=False,
    )
    assert result["exited"] is True
    assert result["operation_state"] == OperationState.FAILED.value

    assert budget.reserved == 0.0, (
        f"Budget leak on deny: reserved={budget.reserved}, expected 0.0"
    )
    assert budget.consumed == 0.0

    record = gov.registry.get(p.idempotency_key)
    assert record.budget_reserved is False
    assert record.budget_committed is False


def test_authorize_exit_approve_keeps_reservation():
    """ESCALATED → authorize_exit(approve) → RESERVED: reservation kept, no re-reserve."""
    gov, state = _setup_governor(budget_capacity=100.0)
    budget = state.resources["budget"]

    # Force HOLD via broken scoring.
    gov.scoring = _BrokenScoring()
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    gov.adjudicate(
        p, pressure=0.2, velocity=0.2, capacity=0.9,
        resource_id="budget", amount=10.0,
    )
    assert budget.reserved == 10.0

    # Escalate the HOLD.
    assert gov.escalate_hold(p.idempotency_key, escalated_by="risk-officer")["escalated"]

    # Approve exit → RESERVED, reservation must stay (no double-reserve).
    result = gov.authorize_exit(
        p.idempotency_key,
        authorized_by="ciso",
        scope="global",
        authorization="ceremony-G-R5-001",
        reason="Approved with conditions",
        approve=True,
    )
    assert result["exited"] is True
    assert result["operation_state"] == OperationState.RESERVED.value

    assert budget.reserved == 10.0, (
        f"Double reservation on approve: reserved={budget.reserved}, expected 10.0"
    )
    assert budget.consumed == 0.0

    record = gov.registry.get(p.idempotency_key)
    assert record.budget_reserved is True
    assert record.budget_committed is False

    # Clean up: dispatch and confirm to release the reservation.
    gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0)
    gov.confirm(p.idempotency_key)
    assert budget.reserved == 0.0
    assert budget.consumed == 10.0


# ─── mutation test: _reserve_budget lock is load-bearing ──────────────

def test_concurrent_adjudication_fails_without_lock():
    """Mutation test: if the lock in _reserve_budget is removed, the concurrency
    test should fail (both threads reserve, exceeding the budget).

    This test verifies that the lock IS the protection by confirming the
    concurrent test passes WITH the lock (which it does in
    test_concurrent_adjudication_reserves_budget_once). Here we verify
    the invariant directly: two threads on a 1500 budget with 1000 each
    must result in exactly one HARD_VETO and reserved == 1000.
    If the lock were removed, both would reserve and reserved would be 2000.
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

    # With the lock: exactly one veto, one kept, reserved == 1000.
    # Without the lock: both would pass (no veto), reserved == 2000.
    vetoes = [d for d in decisions if d == Decision.HARD_VETO.value]
    kept = [d for d in decisions if d != Decision.HARD_VETO.value]
    assert len(vetoes) == 1, f"expected exactly 1 HARD_VETO, got {decisions}"
    assert len(kept) == 1, f"expected exactly 1 kept operation, got {decisions}"
    assert budget.reserved == 1000.0, f"reserved={budget.reserved}, expected 1000.0"
    assert budget.consumed == 0.0

    # The mutation insight: if we remove the lock from _reserve_budget,
    # both threads would call resource.reserve(1000) without coordination.
    # On a 1500-capacity resource, the first reserve succeeds (reserved=1000,
    # available=500), the second also succeeds because the membrane's I8
    # check ran BEFORE either reservation (both saw available >= 1000).
    # That would leave reserved=2000 > capacity=1500, violating I8.
    # The lock ensures the second _reserve_budget sees reserved=1000 and
    # resource.reserve(1000) fails (1000+1000 > 1500), returning HARD_VETO.



def test_durable_budget_state_preserves_decimal_precision(tmp_path):
    coordinator = DurableBudgetCoordinator(str(tmp_path / "budget.json"))
    resource = type("Resource", (), {
        "resource_id": "budget",
        "capacity": Decimal("0.3000000000000000000001"),
        "reserved": Decimal("0"),
        "consumed": Decimal("0"),
    })()
    coordinator.register(resource)
    assert coordinator.reserve("budget", Decimal("0.1000000000000000000001"))
    state = coordinator.get_state("budget")
    assert state["capacity"] == Decimal("0.3000000000000000000001")
    assert state["reserved"] == Decimal("0.1000000000000000000001")
    assert state["available"] == Decimal("0.2000000000000000000000")


def test_consensus_state_preserves_fractional_reservations(tmp_path):
    backend = SingleHostLockBackend(str(tmp_path))
    backend.register_resource("budget", Decimal("0.3"))

    first = backend.reserve("first", Decimal("0.1"), "budget")
    second = backend.reserve("second", Decimal("0.2"), "budget")
    assert first.status == "RESERVED"
    assert second.status == "RESERVED"
    assert backend.commit(first.reservation_id).status == "COMMITTED"

    state = backend.get_state("budget")
    assert state.capacity == Decimal("0.3")
    assert state.reserved == Decimal("0.2")
    assert state.committed == Decimal("0.1")
    assert state.available == Decimal("0.0")
    assert next(
        r for r in state.reservations
        if r["reservation_id"] == second.reservation_id
    )["amount"] == Decimal("0.2")


def test_recovery_rejects_unknown_journal_operation(tmp_path):
    store_dir = tmp_path / "store"
    backend = SingleHostLockBackend(str(store_dir))
    backend.register_resource("budget", Decimal("10"))
    snapshot = backend._snapshot("budget")
    transaction = {
        "operation": "unexpected-operation",
        "reservation_id": "reservation-1",
        "record": {
            "key": "key-1",
            "amount": "1",
            "resource_id": "budget",
            "state": "COMMITTED",
        },
        "before": snapshot,
        "after": snapshot,
    }
    (store_dir / "transaction.json").write_text(json.dumps(transaction))

    with pytest.raises(RuntimeError, match="Corrupted transaction journal"):
        SingleHostLockBackend(str(store_dir))


def test_fail_rejects_unknown_outcome_without_releasing_or_refunding_budget():
    """UNKNOWN must be reconciled; fail() cannot infer non-execution."""
    gov, state = _setup_governor(budget_capacity=100.0)
    budget = state.resources["budget"]
    passage = Passage(agent_id="alice", action_type="payment", amount=10.0)
    result = gov.adjudicate(
        passage, pressure=0.2, velocity=0.2, capacity=0.9,
        resource_id="budget", amount=10.0,
    )
    assert result["operation_state"] == OperationState.RESERVED.value
    assert budget.reserved == 10.0
    assert gov.dispatch(passage.idempotency_key, "budget", 10.0)["dispatched"] is True
    assert budget.reserved == 0.0
    assert budget.consumed == 10.0
    assert gov.mark_unknown(passage.idempotency_key) is True

    before_reserved = budget.reserved
    before_consumed = budget.consumed
    before_record = gov.registry.get(passage.idempotency_key)

    assert gov.fail(passage.idempotency_key) is False

    after_record = gov.registry.get(passage.idempotency_key)
    assert after_record.state == OperationState.UNKNOWN
    assert after_record.budget_committed == before_record.budget_committed
    assert after_record.budget_reserved == before_record.budget_reserved
    assert budget.reserved == before_reserved == 0.0
    assert budget.consumed == before_consumed == 10.0


def test_fail_rechecks_state_under_budget_lock_before_compensating(monkeypatch):
    """A concurrent UNKNOWN transition must not be overwritten by fail()."""
    gov, state = _setup_governor(budget_capacity=100.0)
    budget = state.resources["budget"]
    passage = Passage(agent_id="alice", action_type="payment", amount=10.0)
    result = gov.adjudicate(
        passage, pressure=0.2, velocity=0.2, capacity=0.9,
        resource_id="budget", amount=10.0,
    )
    assert result["operation_state"] == OperationState.RESERVED.value
    assert gov.dispatch(passage.idempotency_key, "budget", 10.0)["dispatched"] is True
    assert budget.consumed == 10.0

    registry = gov.registry
    original_get = registry.get
    first_read = True

    def get_then_race(key):
        nonlocal first_read
        record = original_get(key)
        if key == passage.idempotency_key and first_read:
            first_read = False
            # Simulate uncertainty arriving after fail()'s initial read but
            # before its locked re-check.
            assert registry.transition_to(key, OperationState.UNKNOWN)[0]
        return record

    monkeypatch.setattr(registry, "get", get_then_race)
    assert gov.fail(passage.idempotency_key) is False
    record = original_get(passage.idempotency_key)
    assert record.state == OperationState.UNKNOWN
    assert record.budget_committed is True
    assert budget.consumed == 10.0
    assert budget.reserved == 0.0


def test_fail_refuses_dispatching_without_confirmed_provider_outcome():
    """DISPATCHING is uncertain unless the provider explicitly reports FAILED."""
    gov, state = _setup_governor(budget_capacity=100.0)
    budget = state.resources["budget"]
    passage = Passage(agent_id="alice", action_type="payment", amount=10.0)
    result = gov.adjudicate(
        passage, pressure=0.2, velocity=0.2, capacity=0.9,
        resource_id="budget", amount=10.0,
    )
    assert result["operation_state"] == OperationState.RESERVED.value
    assert gov.dispatch(passage.idempotency_key, "budget", 10.0)["dispatched"] is True
    assert gov.registry.get(passage.idempotency_key).state == OperationState.DISPATCHING
    assert budget.reserved == 0.0
    assert budget.consumed == 10.0

    assert gov.fail(passage.idempotency_key) is False
    record = gov.registry.get(passage.idempotency_key)
    assert record.state == OperationState.DISPATCHING
    assert record.budget_committed is True
    assert budget.consumed == 10.0

    # The same path is allowed only when the provider explicitly establishes
    # that no external effect occurred (the dispatch implementation passes it).
    assert gov.fail(
        passage.idempotency_key,
        _provider_confirmed_no_effect=True,
    ) is True
    assert gov.registry.get(passage.idempotency_key).state == OperationState.FAILED
    assert budget.consumed == 0.0



def test_fail_uses_atomic_expected_state_for_unbudgeted_operation(monkeypatch):
    """A concurrent dispatch/UNKNOWN cannot be overwritten in the no-budget path."""
    gov, _state = _setup_governor()
    passage = Passage(agent_id="alice", action_type="noop", amount=0.0)
    result = gov.adjudicate(
        passage, pressure=0.2, velocity=0.2, capacity=0.9,
    )
    assert result["operation_state"] == OperationState.RESERVED.value

    registry = gov.registry
    original_transition = registry.transition_to
    injected = False

    def transition_with_race(key, new_state, claimed_by="", **kwargs):
        nonlocal injected
        if key == passage.idempotency_key and new_state == OperationState.FAILED and not injected:
            injected = True
            # Another worker starts dispatch, then cannot establish the outcome.
            assert original_transition(key, OperationState.DISPATCHING)[0]
            assert original_transition(key, OperationState.UNKNOWN)[0]
        return original_transition(key, new_state, claimed_by, **kwargs)

    monkeypatch.setattr(registry, "transition_to", transition_with_race)
    assert gov.fail(passage.idempotency_key) is False
    assert injected is True
    assert registry.get(passage.idempotency_key).state == OperationState.UNKNOWN
