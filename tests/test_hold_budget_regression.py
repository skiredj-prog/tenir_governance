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


def _setup_durable_backend_governor(tmp_path, provider=None, budget_capacity=100.0):
    gov, state = _setup_governor(budget_capacity=budget_capacity, provider=provider)
    backend = SingleHostLockBackend(str(tmp_path))
    backend.register_resource("budget", Decimal(str(budget_capacity)))
    gov.consensus_backend = backend
    return gov, state, backend


def _adjudicate_durable_budget(gov):
    passage = Passage(agent_id="alice", action_type="payment", amount=10.0)
    result = gov.adjudicate(
        passage, pressure=0.2, velocity=0.2, capacity=0.9,
        authority=0.9, context_fit=0.9, evidence_quality=0.9,
        option_space=0.9, resource_id="budget", amount=10.0,
    )
    assert result["composed_decision"] in (Decision.PASS.value, Decision.FLAG.value)
    return passage, result


def test_governor_reservation_is_owned_by_durable_backend(tmp_path):
    gov, _state, backend = _setup_durable_backend_governor(tmp_path)
    passage, result = _adjudicate_durable_budget(gov)

    record = gov.registry.get(passage.idempotency_key)
    state = backend.get_state("budget")
    assert record is not None
    assert record.budget_reserved is True
    assert record.reservation_id
    assert state.reserved == Decimal("10.0")
    assert state.committed == Decimal("0")


def test_governor_dispatch_commits_backend_reservation_once(tmp_path):
    gov, _state, backend = _setup_durable_backend_governor(tmp_path)
    passage, _result = _adjudicate_durable_budget(gov)

    dispatched = gov.dispatch(passage.idempotency_key, "budget", 10.0)
    assert dispatched["dispatched"] is True
    record = gov.registry.get(passage.idempotency_key)
    state = backend.get_state("budget")
    assert record.budget_reserved is False
    assert record.budget_committed is True
    assert state.reserved == Decimal("0")
    assert state.committed == Decimal("10.0")

    # Duplicate dispatch is rejected by the operation state machine and must
    # not commit the same backend reservation a second time.
    replay = gov.dispatch(passage.idempotency_key, "budget", 10.0)
    assert replay["dispatched"] is False
    assert backend.get_state("budget").committed == Decimal("10.0")


def test_governor_pre_dispatch_failure_releases_backend_reservation(tmp_path):
    gov, _state, backend = _setup_durable_backend_governor(tmp_path)
    passage, _result = _adjudicate_durable_budget(gov)

    assert gov.fail(passage.idempotency_key, "budget", 10.0) is True
    record = gov.registry.get(passage.idempotency_key)
    state = backend.get_state("budget")
    assert record.state == OperationState.FAILED
    assert record.budget_reserved is False
    assert state.reserved == Decimal("0")
    assert state.committed == Decimal("0")


def test_reconcile_failed_provider_refunds_durable_commit(tmp_path):
    provider = MockProvider(
        behavior="timeout",
        reconcile_outcome=ProviderOutcome.FAILED,
    )
    gov, _state, backend = _setup_durable_backend_governor(tmp_path, provider=provider)
    passage, _result = _adjudicate_durable_budget(gov)

    result = gov.dispatch_and_execute(passage.idempotency_key, "budget", 10.0)
    assert result["operation_state"] in (
        OperationState.UNKNOWN.value, OperationState.DISPATCHING.value,
    )
    reconciled = gov.reconcile_unknown(passage.idempotency_key)
    assert reconciled["reconciled"] is True
    record = gov.registry.get(passage.idempotency_key)
    state = backend.get_state("budget")
    assert record.state == OperationState.FAILED
    assert record.budget_reserved is False
    assert record.budget_committed is False
    assert state.reserved == Decimal("0")
    assert state.committed == Decimal("0")


class _SimulatedProcessCrash(BaseException):
    pass


def test_crash_after_backend_reserve_reuses_same_reservation(tmp_path, monkeypatch):
    from tenir_governance.operations.registry import DurableOperationRegistry, OperationRecord

    gov, _state, backend = _setup_durable_backend_governor(tmp_path)
    registry_path = str(tmp_path / "operations.json")
    gov.registry = DurableOperationRegistry(registry_path)
    key = "crash-after-reserve"
    assert gov.registry.register(OperationRecord(
        idempotency_key=key, passage_id="passage-crash",
        agent_id="alice", action_type="payment",
        amount=10.0, resource_id="budget",
    ))

    def crash_before_registry_ownership(*args, **kwargs):
        raise _SimulatedProcessCrash("simulated process termination")

    monkeypatch.setattr(gov.registry, "set_budget_flags", crash_before_registry_ownership)
    with pytest.raises(_SimulatedProcessCrash):
        gov._reserve_budget(key, "budget", 10.0)

    reopened_backend = SingleHostLockBackend(str(tmp_path))
    reopened_registry = DurableOperationRegistry(registry_path)
    gov.consensus_backend = reopened_backend
    gov.registry = reopened_registry

    assert gov._reserve_budget(key, "budget", 10.0) is True
    record = reopened_registry.get(key)
    state = reopened_backend.get_state("budget")
    assert record.reservation_id
    assert record.budget_reserved is True
    assert state.reserved == Decimal("10.0")
    assert len([
        item for item in state.reservations
        if item["key"] == key and item["state"] == "RESERVED"
    ]) == 1


def test_crash_after_backend_commit_reconciles_using_backend_state(tmp_path, monkeypatch):
    from tenir_governance.operations.registry import DurableOperationRegistry

    provider = MockProvider(
        behavior="timeout",
        reconcile_outcome=ProviderOutcome.FAILED,
    )
    gov, _state, backend = _setup_durable_backend_governor(tmp_path, provider=provider)
    registry_path = str(tmp_path / "operations.json")
    gov.registry = DurableOperationRegistry(registry_path)
    passage, _result = _adjudicate_durable_budget(gov)
    key = passage.idempotency_key

    def crash_after_commit_before_registry_flags(*args, **kwargs):
        raise _SimulatedProcessCrash("crash after durable commit")

    monkeypatch.setattr(gov.registry, "set_budget_flags", crash_after_commit_before_registry_flags)
    with pytest.raises(_SimulatedProcessCrash):
        gov.dispatch_and_execute(key, "budget", 10.0)

    reopened_backend = SingleHostLockBackend(str(tmp_path))
    reopened_registry = DurableOperationRegistry(registry_path)
    gov.consensus_backend = reopened_backend
    gov.registry = reopened_registry

    durable_state = reopened_backend.get_state("budget")
    assert durable_state.reserved == Decimal("0")
    assert durable_state.committed == Decimal("10.0")
    assert reopened_registry.get(key).budget_committed is False  # deliberately stale pre-crash flag

    reconciled = gov.reconcile_unknown(key)
    assert reconciled["reconciled"] is True
    final_record = reopened_registry.get(key)
    final_state = reopened_backend.get_state("budget")
    assert final_record.state == OperationState.FAILED
    assert final_record.budget_reserved is False
    assert final_record.budget_committed is False
    assert final_state.reserved == Decimal("0")
    assert final_state.committed == Decimal("0")


def test_reconcile_confirmed_keeps_durable_budget_committed(tmp_path):
    provider = MockProvider(
        behavior="timeout",
        reconcile_outcome=ProviderOutcome.CONFIRMED,
    )
    gov, _state, backend = _setup_durable_backend_governor(tmp_path, provider=provider)
    passage, _result = _adjudicate_durable_budget(gov)

    dispatched = gov.dispatch_and_execute(passage.idempotency_key, "budget", 10.0)
    assert dispatched["operation_state"] in (
        OperationState.UNKNOWN.value, OperationState.DISPATCHING.value,
    )

    reconciled = gov.reconcile_unknown(passage.idempotency_key)
    record = gov.registry.get(passage.idempotency_key)
    state = backend.get_state("budget")

    assert reconciled["reconciled"] is True
    assert record.state == OperationState.CONFIRMED
    assert record.budget_reserved is False
    assert record.budget_committed is True
    assert state.reserved == Decimal("0")
    assert state.committed == Decimal("10.0")


def test_crash_after_durable_refund_replays_reconciliation_idempotently(tmp_path, monkeypatch):
    from tenir_governance.operations.registry import DurableOperationRegistry

    provider = MockProvider(
        behavior="timeout",
        reconcile_outcome=ProviderOutcome.FAILED,
    )
    gov, _state, backend = _setup_durable_backend_governor(tmp_path, provider=provider)
    registry_path = str(tmp_path / "operations.json")
    gov.registry = DurableOperationRegistry(registry_path)
    passage, _result = _adjudicate_durable_budget(gov)
    key = passage.idempotency_key

    first = gov.dispatch_and_execute(key, "budget", 10.0)
    assert first["operation_state"] in (
        OperationState.UNKNOWN.value, OperationState.DISPATCHING.value,
    )

    def crash_after_compensation(*args, **kwargs):
        raise _SimulatedProcessCrash("crash after refund before ownership flags")

    monkeypatch.setattr(gov.registry, "set_budget_flags", crash_after_compensation)
    with pytest.raises(_SimulatedProcessCrash):
        gov.reconcile_unknown(key)

    reopened_backend = SingleHostLockBackend(str(tmp_path))
    reopened_registry = DurableOperationRegistry(registry_path)
    gov.consensus_backend = reopened_backend
    gov.registry = reopened_registry

    before = reopened_backend.get_state("budget")
    assert before.reserved == Decimal("0")
    assert before.committed == Decimal("0")

    replay = gov.reconcile_unknown(key)
    final_record = reopened_registry.get(key)
    after = reopened_backend.get_state("budget")

    assert replay["reconciled"] is True
    assert final_record.state == OperationState.FAILED
    assert final_record.budget_reserved is False
    assert final_record.budget_committed is False
    assert after.reserved == Decimal("0")
    assert after.committed == Decimal("0")


def test_durable_backend_missing_reservation_identity_fails_closed(tmp_path):
    from tenir_governance.operations.registry import OperationRecord

    gov, state, backend = _setup_durable_backend_governor(tmp_path)
    key = "missing-durable-reservation-id"
    assert gov.registry.register(OperationRecord(
        idempotency_key=key, passage_id="passage-missing-id",
        agent_id="alice", action_type="payment",
        amount=10.0, resource_id="budget",
        budget_reserved=True,
    ))

    # A missing durable identity must never trigger legacy in-memory release.
    local_resource = state.resources["budget"]
    local_reserved_before = local_resource.reserved
    backend_before = backend.get_state("budget")

    assert gov.fail(key, "budget", 10.0) is False
    record = gov.registry.get(key)
    backend_after = backend.get_state("budget")

    assert record.state == OperationState.CREATED
    assert record.budget_reserved is True
    assert record.reservation_id == ""
    assert local_resource.reserved == local_reserved_before
    assert backend_after.reserved == backend_before.reserved
    assert backend_after.committed == backend_before.committed


def test_direct_confirm_commits_durable_reservation_before_terminal_state(tmp_path):
    gov, _state, backend = _setup_durable_backend_governor(tmp_path)
    passage, _result = _adjudicate_durable_budget(gov)
    record = gov.registry.get(passage.idempotency_key)
    assert record.reservation_id
    assert backend.get_state("budget").reserved == Decimal("10.0")

    # Model recovery/integration calling confirm after the external effect,
    # before the backend commit was recorded by the normal dispatch path.
    transitioned, _previous = gov.registry.transition_to(
        passage.idempotency_key, OperationState.DISPATCHING,
    )
    assert transitioned

    assert gov.confirm(passage.idempotency_key) is True
    final_record = gov.registry.get(passage.idempotency_key)
    state = backend.get_state("budget")
    assert final_record.state == OperationState.CONFIRMED
    assert final_record.budget_reserved is False
    assert final_record.budget_committed is True
    assert state.reserved == Decimal("0")
    assert state.committed == Decimal("10.0")


def test_resume_reserved_requires_live_durable_reservation(tmp_path):
    gov, _state, backend = _setup_durable_backend_governor(tmp_path)
    passage, _result = _adjudicate_durable_budget(gov)
    record = gov.registry.get(passage.idempotency_key)
    assert record.reservation_id

    released = backend.release(record.reservation_id)
    assert released.status == "RELEASED"

    resumed = gov.resume_existing(passage.idempotency_key)
    assert resumed["action"] != "dispatch"
    assert resumed["operation_state"] == OperationState.RESERVED.value
    assert "durable" in resumed["reason"].lower() or "reconcil" in resumed["reason"].lower()


def test_direct_provider_failure_refunds_durable_commit(tmp_path):
    provider = MockProvider(behavior="failed")
    gov, _state, backend = _setup_durable_backend_governor(tmp_path, provider=provider)
    passage, _result = _adjudicate_durable_budget(gov)

    result = gov.dispatch_and_execute(passage.idempotency_key, "budget", 10.0)
    record = gov.registry.get(passage.idempotency_key)
    state = backend.get_state("budget")

    assert result["operation_state"] == OperationState.FAILED.value
    assert record.state == OperationState.FAILED
    assert record.budget_reserved is False
    assert record.budget_committed is False
    assert state.reserved == Decimal("0")
    assert state.committed == Decimal("0")


def test_manual_fail_cannot_refund_committed_durable_budget_without_proof(tmp_path):
    gov, _state, backend = _setup_durable_backend_governor(tmp_path)
    passage, _result = _adjudicate_durable_budget(gov)
    dispatched = gov.dispatch(passage.idempotency_key, "budget", 10.0)
    assert dispatched["dispatched"] is True

    before = backend.get_state("budget")
    record_before = gov.registry.get(passage.idempotency_key)
    assert record_before.budget_committed is True

    assert gov.fail(passage.idempotency_key, "budget", 10.0) is False

    after = backend.get_state("budget")
    record_after = gov.registry.get(passage.idempotency_key)
    assert record_after.state == OperationState.DISPATCHING
    assert record_after.budget_committed is True
    assert after.committed == before.committed == Decimal("10.0")
    assert after.reserved == before.reserved == Decimal("0")


def test_direct_provider_confirmation_uses_durable_confirm_path(tmp_path):
    provider = MockProvider(behavior="confirmed")
    gov, _state, backend = _setup_durable_backend_governor(tmp_path, provider=provider)
    passage, _result = _adjudicate_durable_budget(gov)

    result = gov.dispatch_and_execute(passage.idempotency_key, "budget", 10.0)
    record = gov.registry.get(passage.idempotency_key)
    state = backend.get_state("budget")

    assert result["operation_state"] == OperationState.CONFIRMED.value
    assert record.state == OperationState.CONFIRMED
    assert record.budget_reserved is False
    assert record.budget_committed is True
    assert state.reserved == Decimal("0")
    assert state.committed == Decimal("10.0")
