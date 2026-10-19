"""Tests for the dispatch provider abstraction and governor integration.

Covers:
- MockProvider behaviors (confirmed, failed, timeout, pending)
- Governor.dispatch_and_execute() with provider
- Timeout → UNKNOWN (not FAILED)
- Reconciliation of UNKNOWN state
- Idempotency key passthrough
- Full lifecycle: adjudicate → dispatch_and_execute → CONFIRMED
- Provider exception → UNKNOWN (fail-closed)
"""
import pytest
import tempfile
import os
import threading
from types import SimpleNamespace

from tenir_governance.operations.provider import (
    DispatchProvider, DispatchResult, ProviderOutcome,
    ReconciliationResult, MockProvider, StripeProvider,
)
from tenir_governance.composition.governor import (
    DualKernelGovernor, ScoringKernelAdapter,
)
from tenir_governance.composition.policy import CompositionPolicy
from tenir_governance.membrane import TransitionMembrane, MembraneState, Agent, Resource
from tenir_governance.operations.registry import (
    DurableOperationRegistry, OperationRecord, OperationState,
)
from tenir_governance.objects import Passage
from tenir_kernel.kernel import TenirKernel


def test_indeterminate_reconciliation_normalizes_dispatching_to_unknown():
    gov, _state = _setup_governor()
    key = "interrupted-dispatch-indeterminate"
    record = OperationRecord(
        idempotency_key=key, passage_id="p-interrupted",
        agent_id="alice", action_type="payment", amount=0.0,
    )
    assert gov.registry.register(record)
    assert gov.registry.transition_to(key, OperationState.RESERVED)[0]
    assert gov.registry.transition_to(key, OperationState.DISPATCHING)[0]

    class IndeterminateProvider:
        def reconcile(self, **kwargs):
            return ReconciliationResult(resolved=False, outcome=ProviderOutcome.UNKNOWN)

    gov.provider = IndeterminateProvider()
    result = gov.reconcile_unknown(key)

    assert result["reconciled"] is False
    assert result["operation_state"] == OperationState.UNKNOWN.value
    assert gov.registry.get(key).state == OperationState.UNKNOWN


def test_reconcile_can_recover_interrupted_dispatching_operation():
    gov, state = _setup_governor(provider=MockProvider(behavior="timeout"))
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    result = gov.adjudicate(
        p, pressure=0.2, velocity=0.2, capacity=0.9,
        resource_id="budget", amount=10.0,
    )
    assert result["operation_state"] == OperationState.RESERVED.value
    assert state.resources["budget"].commit(10.0)
    assert gov.registry.set_budget_flags(
        p.idempotency_key, budget_reserved=False, budget_committed=True,
    )
    success, _ = gov.registry.transition_to(p.idempotency_key, OperationState.DISPATCHING)
    assert success

    class ConfirmedNoEffectProvider:
        def reconcile(self, **kwargs):
            return ReconciliationResult(resolved=True, outcome=ProviderOutcome.FAILED)

    gov.provider = ConfirmedNoEffectProvider()
    reconciled = gov.reconcile_unknown(p.idempotency_key)

    assert reconciled["reconciled"] is True
    assert reconciled["operation_state"] == OperationState.FAILED.value
    assert state.resources["budget"].consumed == 0.0
    assert state.resources["budget"].reserved == 0.0


def test_concurrent_failed_reconciliation_refunds_only_once():
    initial_provider = MockProvider(behavior="timeout")
    gov, state = _setup_governor(provider=initial_provider)
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    gov.adjudicate(
        p, pressure=0.2, velocity=0.2, capacity=0.9,
        resource_id="budget", amount=10.0,
    )
    dispatched = gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0)
    assert dispatched["operation_state"] == OperationState.UNKNOWN.value
    assert state.resources["budget"].consumed == 10.0

    barrier = threading.Barrier(2)

    class ConcurrentFailedProvider:
        def reconcile(self, **kwargs):
            barrier.wait(timeout=3.0)
            return ReconciliationResult(resolved=True, outcome=ProviderOutcome.FAILED)

    gov.provider = ConcurrentFailedProvider()
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(gov.reconcile_unknown(p.idempotency_key)))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)

    assert all(not thread.is_alive() for thread in threads)
    assert sum(result["reconciled"] for result in results) == 1
    assert gov.registry.get(p.idempotency_key).state == OperationState.FAILED
    assert state.resources["budget"].consumed == 0.0
    assert state.resources["budget"].reserved == 0.0


def test_stripe_reconciliation_empty_search_remains_unknown(monkeypatch):
    provider = StripeProvider(api_key="sk_test_placeholder")
    payment_intent = SimpleNamespace(search=lambda **kwargs: SimpleNamespace(data=[]))
    provider._get_stripe = lambda: SimpleNamespace(PaymentIntent=payment_intent)

    result = provider.reconcile("operation-key")

    assert result.resolved is False
    assert result.outcome == ProviderOutcome.UNKNOWN
    assert "does not prove non-execution" in result.error


def _setup_governor(provider=None, budget_capacity=100.0):
    """Create a governor with an optional provider for testing."""
    kernel = TenirKernel()
    scoring = ScoringKernelAdapter(kernel)
    state = MembraneState()
    budget = Resource(resource_id="budget", capacity=budget_capacity)
    state.register_resource(budget)
    alice = Agent(agent_id="alice", authority_score=0.9, trusted_delegates=frozenset({"bob"}))
    state.register_agent(alice)
    membrane = TransitionMembrane(state)
    registry = DurableOperationRegistry()
    gov = DualKernelGovernor(scoring, membrane, registry=registry, provider=provider)
    return gov, state


# ── MockProvider Tests ──────────────────────────────────────────────

class TestMockProvider:
    """Test the MockProvider directly."""

    def test_confirmed_behavior(self):
        provider = MockProvider(behavior="confirmed")
        result = provider.execute("key-1", "payment", 10.0, "alice")
        assert result.outcome == ProviderOutcome.CONFIRMED
        assert result.provider_reference == "mock_ref_key-1"
        assert result.error == ""
        assert result.is_terminal

    def test_failed_behavior(self):
        provider = MockProvider(behavior="failed")
        result = provider.execute("key-1", "payment", 10.0, "alice")
        assert result.outcome == ProviderOutcome.FAILED
        assert "Simulated provider failure" in result.error
        assert result.is_terminal

    def test_timeout_behavior(self):
        provider = MockProvider(behavior="timeout")
        result = provider.execute("key-1", "payment", 10.0, "alice")
        assert result.outcome == ProviderOutcome.UNKNOWN
        assert "Simulated timeout" in result.error
        assert not result.is_terminal

    def test_pending_behavior(self):
        provider = MockProvider(behavior="pending")
        result = provider.execute("key-1", "payment", 10.0, "alice")
        assert result.outcome == ProviderOutcome.PENDING
        assert not result.is_terminal

    def test_reconcile_resolves(self):
        provider = MockProvider(behavior="timeout", reconcile_outcome=ProviderOutcome.CONFIRMED)
        # Execute gets UNKNOWN
        result = provider.execute("key-1", "payment", 10.0, "alice")
        assert result.outcome == ProviderOutcome.UNKNOWN
        # Reconcile resolves to CONFIRMED
        recon = provider.reconcile("key-1")
        assert recon.resolved
        assert recon.outcome == ProviderOutcome.CONFIRMED

    def test_reconcile_failed(self):
        provider = MockProvider(behavior="timeout", reconcile_outcome=ProviderOutcome.FAILED)
        provider.execute("key-1", "payment", 10.0, "alice")
        recon = provider.reconcile("key-1")
        assert recon.resolved
        assert recon.outcome == ProviderOutcome.FAILED

    def test_execute_after_n(self):
        provider = MockProvider()
        provider.set_execute_after_n(3)
        r1 = provider.execute("key-1", "payment", 10.0, "alice")
        r2 = provider.execute("key-1", "payment", 10.0, "alice")
        r3 = provider.execute("key-1", "payment", 10.0, "alice")
        assert r1.outcome == ProviderOutcome.UNKNOWN
        assert r2.outcome == ProviderOutcome.UNKNOWN
        assert r3.outcome == ProviderOutcome.CONFIRMED

    def test_calls_recorded(self):
        provider = MockProvider(behavior="confirmed")
        provider.execute("key-1", "payment", 10.0, "alice", metadata={"order": "123"})
        provider.execute("key-2", "transfer", 20.0, "bob")
        assert len(provider.calls) == 2
        assert provider.calls[0]["idempotency_key"] == "key-1"
        assert provider.calls[0]["action_type"] == "payment"
        assert provider.calls[0]["amount"] == 10.0
        assert provider.calls[0]["agent_id"] == "alice"
        assert provider.calls[0]["metadata"] == {"order": "123"}

    def test_custom_handler(self):
        provider = MockProvider()
        provider.set_custom_handler(
            lambda k, a, amt, ag, m: DispatchResult(
                outcome=ProviderOutcome.CONFIRMED,
                provider_reference=f"custom_{k}",
            )
        )
        result = provider.execute("key-1", "payment", 10.0, "alice")
        assert result.outcome == ProviderOutcome.CONFIRMED
        assert result.provider_reference == "custom_key-1"

    def test_protocol_compliance(self):
        provider = MockProvider()
        assert isinstance(provider, DispatchProvider)


# ── Governor.dispatch_and_execute Tests ──────────────────────────────

class TestDispatchAndExecute:
    """Test the governor's dispatch_and_execute method with a provider."""

    def test_confirmed_result(self):
        """Provider returns CONFIRMED → state transitions to CONFIRMED."""
        provider = MockProvider(behavior="confirmed")
        gov, state = _setup_governor(provider=provider)
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        gov.adjudicate(p, pressure=0.2, velocity=0.2, capacity=0.9,
                       resource_id="budget", amount=10.0)
        result = gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0)
        assert result["dispatched"] is True
        assert result["executed"] is True
        assert result["operation_state"] == OperationState.CONFIRMED.value
        assert result["provider_reference"] == f"mock_ref_{p.idempotency_key}"

    def test_failed_result(self):
        """Provider returns FAILED → state transitions to FAILED."""
        provider = MockProvider(behavior="failed")
        gov, state = _setup_governor(provider=provider)
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        gov.adjudicate(p, pressure=0.2, velocity=0.2, capacity=0.9,
                       resource_id="budget", amount=10.0)
        result = gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0)
        assert result["dispatched"] is True
        assert result["executed"] is False
        assert result["operation_state"] == OperationState.FAILED.value
        assert "Simulated provider failure" in result["provider_error"]

    def test_timeout_to_unknown(self):
        """Provider timeout → UNKNOWN, NOT FAILED."""
        provider = MockProvider(behavior="timeout")
        gov, state = _setup_governor(provider=provider)
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        gov.adjudicate(p, pressure=0.2, velocity=0.2, capacity=0.9,
                       resource_id="budget", amount=10.0)
        result = gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0)
        assert result["dispatched"] is True
        assert result["executed"] is False
        assert result["operation_state"] == OperationState.UNKNOWN.value
        assert "Simulated timeout" in result["provider_error"]

    def test_pending_stays_dispatching(self):
        """Provider returns PENDING → stays in DISPATCHING."""
        provider = MockProvider(behavior="pending")
        gov, state = _setup_governor(provider=provider)
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        gov.adjudicate(p, pressure=0.2, velocity=0.2, capacity=0.9,
                       resource_id="budget", amount=10.0)
        result = gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0)
        assert result["dispatched"] is True
        assert result["operation_state"] == OperationState.DISPATCHING.value
        assert "PENDING" in result["reason"]

    def test_no_provider_falls_back_to_manual(self):
        """Without a provider, dispatch_and_execute falls back to dispatch()."""
        gov, state = _setup_governor(provider=None)
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        gov.adjudicate(p, pressure=0.2, velocity=0.2, capacity=0.9,
                       resource_id="budget", amount=10.0)
        result = gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0)
        assert result["dispatched"] is True
        assert result["operation_state"] == OperationState.DISPATCHING.value

    def test_provider_exception_to_unknown(self):
        """Provider throws exception → UNKNOWN (fail-closed)."""
        provider = MockProvider()
        provider.set_custom_handler(
            lambda k, a, amt, ag, m: (_ for _ in ()).throw(RuntimeError("Connection refused"))
        )
        gov, state = _setup_governor(provider=provider)
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        gov.adjudicate(p, pressure=0.2, velocity=0.2, capacity=0.9,
                       resource_id="budget", amount=10.0)
        result = gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0)
        assert result["dispatched"] is True
        assert result["operation_state"] == OperationState.UNKNOWN.value
        assert "Connection refused" in result["provider_error"]

    def test_idempotency_key_passed_to_provider(self):
        """The governance idempotency key is passed to the provider."""
        provider = MockProvider(behavior="confirmed")
        gov, state = _setup_governor(provider=provider)
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        gov.adjudicate(p, pressure=0.2, velocity=0.2, capacity=0.9,
                       resource_id="budget", amount=10.0)
        gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0)
        assert len(provider.calls) == 1
        assert provider.calls[0]["idempotency_key"] == p.idempotency_key

    def test_metadata_passed_to_provider(self):
        """Metadata is passed through to the provider."""
        provider = MockProvider(behavior="confirmed")
        gov, state = _setup_governor(provider=provider)
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        gov.adjudicate(p, pressure=0.2, velocity=0.2, capacity=0.9,
                       resource_id="budget", amount=10.0)
        gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0,
                                metadata={"order_id": "ORD-123"})
        assert provider.calls[0]["metadata"] == {"order_id": "ORD-123"}


# ── Reconciliation Tests ────────────────────────────────────────────

class TestReconciliation:
    """Test reconciliation of UNKNOWN operations."""

    def test_reconcile_confirmed(self):
        """UNKNOWN → reconcile → CONFIRMED."""
        provider = MockProvider(behavior="timeout", reconcile_outcome=ProviderOutcome.CONFIRMED)
        gov, state = _setup_governor(provider=provider)
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        gov.adjudicate(p, pressure=0.2, velocity=0.2, capacity=0.9,
                       resource_id="budget", amount=10.0)
        # Dispatch → UNKNOWN
        gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0)
        record = gov.registry.get(p.idempotency_key)
        assert record.state == OperationState.UNKNOWN
        # Reconcile → CONFIRMED
        result = gov.reconcile_unknown(p.idempotency_key)
        assert result["reconciled"] is True
        assert result["operation_state"] == OperationState.CONFIRMED.value

    def test_reconcile_failed(self):
        """UNKNOWN → reconcile → FAILED."""
        provider = MockProvider(behavior="timeout", reconcile_outcome=ProviderOutcome.FAILED)
        gov, state = _setup_governor(provider=provider)
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        gov.adjudicate(p, pressure=0.2, velocity=0.2, capacity=0.9,
                       resource_id="budget", amount=10.0)
        gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0)
        result = gov.reconcile_unknown(p.idempotency_key)
        assert result["reconciled"] is True
        assert result["operation_state"] == OperationState.FAILED.value

    def test_reconcile_still_unknown(self):
        """UNKNOWN → reconcile → still UNKNOWN (human review)."""
        provider = MockProvider(behavior="timeout")
        provider.reconcile_outcome = ProviderOutcome.UNKNOWN
        gov, state = _setup_governor(provider=provider)
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        gov.adjudicate(p, pressure=0.2, velocity=0.2, capacity=0.9,
                       resource_id="budget", amount=10.0)
        gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0)
        result = gov.reconcile_unknown(p.idempotency_key)
        assert result["reconciled"] is False
        assert result["operation_state"] == OperationState.UNKNOWN.value
        assert result["recommendation"] == "human_review"

    def test_reconcile_no_provider(self):
        """Without a provider, reconciliation recommends human review."""
        gov, state = _setup_governor(provider=None)
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        gov.adjudicate(p, pressure=0.2, velocity=0.2, capacity=0.9,
                       resource_id="budget", amount=10.0)
        # Manually transition to UNKNOWN
        gov.registry.transition_to(p.idempotency_key, OperationState.DISPATCHING)
        gov.registry.transition_to(p.idempotency_key, OperationState.UNKNOWN)
        result = gov.reconcile_unknown(p.idempotency_key)
        assert result["reconciled"] is False
        assert result["recommendation"] == "human_review"

    def test_reconcile_wrong_state(self):
        """Reconciling an operation that's not UNKNOWN returns error."""
        provider = MockProvider(behavior="confirmed")
        gov, state = _setup_governor(provider=provider)
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        gov.adjudicate(p, pressure=0.2, velocity=0.2, capacity=0.9,
                       resource_id="budget", amount=10.0)
        # Operation is in RESERVED, not UNKNOWN
        result = gov.reconcile_unknown(p.idempotency_key)
        assert result["reconciled"] is False
        assert "not DISPATCHING or UNKNOWN" in result["reason"]

    def test_reconcile_not_found(self):
        """Reconciling a non-existent operation returns error."""
        gov, state = _setup_governor(provider=MockProvider())
        result = gov.reconcile_unknown("nonexistent-key")
        assert result["reconciled"] is False
        assert "not registered" in result["reason"]


# ── Full Lifecycle Tests ────────────────────────────────────────────

class TestFullLifecycle:
    """Test the full lifecycle: adjudicate → dispatch_and_execute → terminal."""

    def test_full_lifecycle_confirmed(self):
        """Adjudicate PASS/FLAG → dispatch_and_execute → CONFIRMED."""
        provider = MockProvider(behavior="confirmed")
        gov, state = _setup_governor(provider=provider)
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        # Adjudicate
        adj = gov.adjudicate(p, pressure=0.2, velocity=0.2, capacity=0.9,
                             resource_id="budget", amount=10.0)
        assert adj["composed_decision"] in ("PASS", "FLAG")
        assert adj["operation_state"] == OperationState.RESERVED.value
        # Dispatch + execute
        result = gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0)
        assert result["operation_state"] == OperationState.CONFIRMED.value
        # Verify final state
        record = gov.registry.get(p.idempotency_key)
        assert record.state == OperationState.CONFIRMED
        assert record.state.is_terminal

    def test_full_lifecycle_failed(self):
        """Adjudicate → dispatch_and_execute → FAILED."""
        provider = MockProvider(behavior="failed")
        gov, state = _setup_governor(provider=provider)
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        gov.adjudicate(p, pressure=0.2, velocity=0.2, capacity=0.9,
                       resource_id="budget", amount=10.0)
        result = gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0)
        assert result["operation_state"] == OperationState.FAILED.value
        record = gov.registry.get(p.idempotency_key)
        assert record.state == OperationState.FAILED
        assert record.state.is_terminal

    def test_full_lifecycle_timeout_then_reconcile(self):
        """Adjudicate → dispatch → UNKNOWN → reconcile → CONFIRMED."""
        provider = MockProvider(behavior="timeout", reconcile_outcome=ProviderOutcome.CONFIRMED)
        gov, state = _setup_governor(provider=provider)
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        gov.adjudicate(p, pressure=0.2, velocity=0.2, capacity=0.9,
                       resource_id="budget", amount=10.0)
        # Dispatch → UNKNOWN
        dispatch_result = gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0)
        assert dispatch_result["operation_state"] == OperationState.UNKNOWN.value
        # Reconcile → CONFIRMED
        recon_result = gov.reconcile_unknown(p.idempotency_key)
        assert recon_result["operation_state"] == OperationState.CONFIRMED.value
        # Final state
        record = gov.registry.get(p.idempotency_key)
        assert record.state == OperationState.CONFIRMED

    def test_budget_committed_on_confirmed(self):
        """Budget is committed when dispatch transitions to DISPATCHING."""
        provider = MockProvider(behavior="confirmed")
        gov, state = _setup_governor(provider=provider)
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        gov.adjudicate(p, pressure=0.2, velocity=0.2, capacity=0.9,
                       resource_id="budget", amount=10.0)
        gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0)
        resource = state.resources["budget"]
        assert resource.consumed == 10.0
        assert resource.reserved == 0.0

    def test_budget_released_on_failed(self):
        """Budget is released/refunded when dispatch fails."""
        provider = MockProvider(behavior="failed")
        gov, state = _setup_governor(provider=provider)
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        gov.adjudicate(p, pressure=0.2, velocity=0.2, capacity=0.9,
                       resource_id="budget", amount=10.0)
        gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0)
        resource = state.resources["budget"]
        # After failed dispatch, budget should be refunded
        assert resource.consumed == 0.0
        assert resource.reserved == 0.0


# ── ProviderOutcome Enum Tests ──────────────────────────────────────

class TestProviderOutcome:
    """Test the ProviderOutcome enum."""

    def test_outcomes(self):
        assert ProviderOutcome.CONFIRMED.value == "confirmed"
        assert ProviderOutcome.FAILED.value == "failed"
        assert ProviderOutcome.PENDING.value == "pending"
        assert ProviderOutcome.UNKNOWN.value == "unknown"

    def test_terminal_outcomes(self):
        confirmed = DispatchResult(outcome=ProviderOutcome.CONFIRMED)
        failed = DispatchResult(outcome=ProviderOutcome.FAILED)
        pending = DispatchResult(outcome=ProviderOutcome.PENDING)
        unknown = DispatchResult(outcome=ProviderOutcome.UNKNOWN)
        assert confirmed.is_terminal
        assert failed.is_terminal
        assert not pending.is_terminal
        assert not unknown.is_terminal


# ── Recovery Tests ──────────────────────────────────────────────────

class TestRecoveryWithProvider:
    """Test recovery includes UNKNOWN operations from provider timeouts."""

    def test_recover_includes_unknown(self):
        """recover() includes UNKNOWN operations from provider timeouts."""
        provider = MockProvider(behavior="timeout")
        gov, state = _setup_governor(provider=provider)
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        gov.adjudicate(p, pressure=0.2, velocity=0.2, capacity=0.9,
                       resource_id="budget", amount=10.0)
        gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0)
        recovery = gov.recover()
        assert recovery["unresolved_count"] >= 1
        unknown_entries = [r for r in recovery["unresolved"] if r["state"] == "unknown"]
        assert len(unknown_entries) >= 1
        assert unknown_entries[0]["recommendation"] == "reconcile"