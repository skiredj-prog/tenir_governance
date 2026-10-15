"""Regression contract for I8 operation-scoped budget reservations."""
from concurrent.futures import ThreadPoolExecutor

from tenir_governance.objects import Passage, Resource
from tenir_governance.operations.budget import InProcessBudgetCoordinator
from tenir_governance.composition.governor import DualKernelGovernor, ScoringKernelAdapter
from tenir_governance.membrane import TransitionMembrane, MembraneState
from tenir_governance.operations.provider import MockProvider, ProviderOutcome
from tenir_governance.operations.registry import DurableOperationRegistry, OperationState
from tenir_kernel.kernel import TenirKernel


def coordinator(capacity=100.0):
    budget = InProcessBudgetCoordinator()
    budget.register(Resource(resource_id="budget", capacity=capacity))
    return budget


def test_reserve_is_idempotent_per_operation():
    budget = coordinator()
    assert budget.reserve("budget", 60.0, reservation_id="op-1")
    assert budget.reserve("budget", 60.0, reservation_id="op-1")
    assert budget.get_resource("budget").reserved == 60.0


def test_reservation_id_cannot_be_reused_for_different_amount():
    budget = coordinator()
    assert budget.reserve("budget", 60.0, reservation_id="op-1")
    assert not budget.reserve("budget", 50.0, reservation_id="op-1")
    assert budget.get_resource("budget").reserved == 60.0


def test_release_is_scoped_and_idempotent():
    budget = coordinator()
    assert budget.reserve("budget", 30.0, reservation_id="op-1")
    assert budget.reserve("budget", 40.0, reservation_id="op-2")
    budget.release("budget", 30.0, reservation_id="op-1")
    budget.release("budget", 30.0, reservation_id="op-1")
    assert budget.get_resource("budget").reserved == 40.0


def test_commit_is_scoped_and_idempotent():
    budget = coordinator()
    assert budget.reserve("budget", 30.0, reservation_id="op-1")
    assert budget.reserve("budget", 40.0, reservation_id="op-2")
    assert budget.commit("budget", 30.0, reservation_id="op-1")
    assert not budget.commit("budget", 30.0, reservation_id="op-1")
    resource = budget.get_resource("budget")
    assert resource.reserved == 40.0
    assert resource.consumed == 30.0


def test_concurrent_reservations_respect_capacity():
    budget = coordinator(capacity=100.0)
    def reserve(index):
        return budget.reserve("budget", 10.0, reservation_id=f"op-{index}")
    with ThreadPoolExecutor(max_workers=32) as pool:
        outcomes = list(pool.map(reserve, range(50)))
    assert sum(outcomes) == 10
    assert budget.get_resource("budget").reserved == 100.0

def test_committed_operation_cannot_be_reserved_again():
    budget = coordinator()
    assert budget.reserve("budget", 25.0, reservation_id="op-1")
    assert budget.commit("budget", 25.0, reservation_id="op-1")
    assert not budget.reserve("budget", 25.0, reservation_id="op-1")
    resource = budget.get_resource("budget")
    assert resource.reserved == 0.0
    assert resource.consumed == 25.0


def test_resource_identity_cannot_be_silently_replaced():
    budget = coordinator()
    original = budget.get_resource("budget")
    replacement = Resource(resource_id="budget", capacity=1000.0)
    try:
        budget.register(replacement)
    except ValueError:
        pass
    else:
        raise AssertionError("replacing a registered resource must be rejected")
    assert budget.get_resource("budget") is original


def test_refund_is_operation_scoped_and_idempotent():
    budget = coordinator()
    assert budget.reserve("budget", 20.0, reservation_id="op-1")
    assert budget.reserve("budget", 30.0, reservation_id="op-2")
    assert budget.commit("budget", 20.0, reservation_id="op-1")
    assert budget.commit("budget", 30.0, reservation_id="op-2")
    assert budget.refund("budget", 20.0, reservation_id="op-1")
    assert not budget.refund("budget", 20.0, reservation_id="op-1")
    resource = budget.get_resource("budget")
    assert resource.consumed == 30.0


def _lifecycle_governor(provider=None, scoring=None):
    state = MembraneState()
    state.register_resource(Resource(resource_id="budget", capacity=100.0))
    membrane = TransitionMembrane(state)
    gov = DualKernelGovernor(
        scoring or ScoringKernelAdapter(TenirKernel()),
        membrane,
        registry=DurableOperationRegistry(),
        provider=provider,
    )
    return gov, state


def test_i8_dispatch_uses_durable_amount_when_arguments_are_omitted():
    provider = MockProvider(behavior="confirmed")
    gov, state = _lifecycle_governor(provider)
    passage = Passage(agent_id="alice", action_type="payment", amount=10.0)
    gov.adjudicate(passage, pressure=0.2, velocity=0.2, capacity=0.9,
                   resource_id="budget", amount=10.0)

    result = gov.dispatch_and_execute(passage.idempotency_key)

    assert result["operation_state"] == OperationState.CONFIRMED.value
    assert provider.calls[0]["amount"] == 10.0
    assert state.resources["budget"].reserved == 0.0
    assert state.resources["budget"].consumed == 10.0


def test_i8_dispatch_rejects_amount_mismatch_without_state_change():
    provider = MockProvider(behavior="confirmed")
    gov, state = _lifecycle_governor(provider)
    passage = Passage(agent_id="alice", action_type="payment", amount=10.0)
    gov.adjudicate(passage, pressure=0.2, velocity=0.2, capacity=0.9,
                   resource_id="budget", amount=10.0)

    result = gov.dispatch_and_execute(passage.idempotency_key, "budget", 20.0)

    assert result["dispatched"] is False
    assert "does not match" in result["reason"]
    assert gov.registry.get(passage.idempotency_key).state == OperationState.RESERVED
    assert state.resources["budget"].reserved == 10.0
    assert state.resources["budget"].consumed == 0.0
    assert provider.calls == []


def test_i8_reconcile_failed_releases_uncommitted_reservation():
    provider = MockProvider(
        behavior="timeout",
        reconcile_outcome=ProviderOutcome.FAILED,
    )
    gov, state = _lifecycle_governor(provider)
    passage = Passage(agent_id="alice", action_type="payment", amount=10.0)
    gov.adjudicate(passage, pressure=0.2, velocity=0.2, capacity=0.9,
                   resource_id="budget", amount=10.0)

    # Simulate a crash after persisting DISPATCHING but before budget commit.
    gov.registry.transition_to(passage.idempotency_key, OperationState.DISPATCHING)
    gov.registry.transition_to(passage.idempotency_key, OperationState.UNKNOWN)
    result = gov.reconcile_unknown(passage.idempotency_key)

    assert result["operation_state"] == OperationState.FAILED.value
    assert state.resources["budget"].reserved == 0.0
    assert state.resources["budget"].consumed == 0.0



def test_i8_hold_does_not_retain_budget_when_re_adjudication_transition_fails():
    class SequentialScoring:
        def __init__(self):
            self.calls = 0

        def adjudicate(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("force fail-closed HOLD for lifecycle test")
            return {"decision": "PASS", "rationale": "test PASS", "s_score": 0.8}

    scoring = SequentialScoring()
    gov, state = _lifecycle_governor(scoring=scoring)
    passage = Passage(agent_id="alice", action_type="payment", amount=10.0)
    first = gov.adjudicate(passage, resource_id="budget", amount=10.0)

    assert first["operation_state"] == OperationState.HOLD.value
    assert state.resources["budget"].reserved == 0.0

    original_transition = gov.registry.transition_to

    def reject_reserved_transition(key, new_state, claimed_by=""):
        if key == passage.idempotency_key and new_state == OperationState.RESERVED:
            return False, OperationState.HOLD
        return original_transition(key, new_state, claimed_by=claimed_by)

    gov.registry.transition_to = reject_reserved_transition
    result = gov.resolve_hold(
        passage.idempotency_key,
        passage=passage,
        resource_id="budget",
        amount=10.0,
    )

    assert result["hold_resolved"] is False
    assert result["operation_state"] == OperationState.HOLD.value
    assert state.resources["budget"].reserved == 0.0
    assert state.resources["budget"].consumed == 0.0
