# Profile: agentic-governance-profile-v1.0.0
# Compliance class: profile-bound
# This module is an extension on top of KB3 and TENIR 2.0 schema 1.2.
# It is NOT canonical, NOT doctrinal. It carries implementation-level
# governance enforcement authority only.
"""Fault-injection tests for the dispatch state machine.

Per review 3 §1: crash after every pair of durable state transitions, not
just one selected crash point. These tests simulate crashes at each
transition in the state machine:

    CREATED → RESERVED → DISPATCHING → CONFIRMED | FAILED | UNKNOWN

Each test verifies that recovery produces the correct state and that no
duplicate effect or budget overrun occurs.
"""
import threading
import pytest
from tenir_governance.operations import (
    DurableOperationRegistry, OperationRecord, OperationState,
)

# These imports require modules not yet built in the kernel repo
# (ScoringKernel, TransitionMembrane, MembraneState, DualKernelGovernor).
# Governor-dependent tests are skipped; registry-specific tests remain active.
try:
    from tenir_governance import (
        Agent, Passage, Resource, Delegation,
        TransitionMembrane, MembraneState,
        DualKernelGovernor, CompositionPolicy,
    )
    from tenir_governance.composition.decisions import Decision
    from tenir_governance.composition.governor import ScoringKernelAdapter
    from tenir_kernel.kernel import TenirKernel
    _GOVERNOR_AVAILABLE = True
except ImportError:
    _GOVERNOR_AVAILABLE = False
    # Provide stubs so module import succeeds; tests using them are skipped.
    Agent = Passage = Resource = Delegation = None
    ScoringKernelAdapter = TenirKernel = None
    TransitionMembrane = MembraneState = None
    DualKernelGovernor = CompositionPolicy = None
    Decision = None


_SKIP = pytest.mark.skipif(
    not _GOVERNOR_AVAILABLE,
    reason="DualKernelGovernor / membrane / scoring not yet built in the kernel repo",
)


def _setup_governor(budget_capacity=100.0, registry=None):
    state = MembraneState()
    budget = Resource(resource_id="budget", capacity=budget_capacity)
    state.register_resource(budget)
    alice = Agent(agent_id="alice", authority_score=0.9,
                  trusted_delegates=frozenset({"bob"}))
    state.register_agent(alice)
    # Bridge: TenirKernel via ScoringKernelAdapter (replaces prototype's ScoringKernel)
    tenir_kernel = TenirKernel()
    scoring = ScoringKernelAdapter(tenir_kernel)
    membrane = TransitionMembrane(state)
    return DualKernelGovernor(scoring, membrane, registry=registry), state


class TestStateMachineTransitions:
    """Verify legal and illegal state transitions."""

    def test_legal_transitions(self):
        reg = DurableOperationRegistry()
        rec = OperationRecord(idempotency_key="op-1", passage_id="p1",
                              agent_id="alice", action_type="payment")
        assert reg.register(rec)
        assert reg.transition_to("op-1", OperationState.RESERVED)[0]
        assert reg.transition_to("op-1", OperationState.DISPATCHING)[0]
        assert reg.transition_to("op-1", OperationState.CONFIRMED)[0]

    def test_terminal_state_no_transition(self):
        reg = DurableOperationRegistry()
        rec = OperationRecord(idempotency_key="op-1", passage_id="p1",
                              agent_id="alice", action_type="payment")
        reg.register(rec)
        reg.transition_to("op-1", OperationState.RESERVED)
        reg.transition_to("op-1", OperationState.DISPATCHING)
        reg.transition_to("op-1", OperationState.CONFIRMED)
        # CONFIRMED is terminal — no further transitions
        assert not reg.transition_to("op-1", OperationState.FAILED)[0]
        assert not reg.transition_to("op-1", OperationState.DISPATCHING)[0]

    def test_illegal_transition_created_to_dispatching(self):
        reg = DurableOperationRegistry()
        rec = OperationRecord(idempotency_key="op-1", passage_id="p1",
                              agent_id="alice", action_type="payment")
        reg.register(rec)
        # Cannot skip RESERVED
        assert not reg.transition_to("op-1", OperationState.DISPATCHING)[0]

    def test_illegal_transition_reserved_to_confirmed(self):
        reg = DurableOperationRegistry()
        rec = OperationRecord(idempotency_key="op-1", passage_id="p1",
                              agent_id="alice", action_type="payment")
        reg.register(rec)
        reg.transition_to("op-1", OperationState.RESERVED)
        # Cannot skip DISPATCHING
        assert not reg.transition_to("op-1", OperationState.CONFIRMED)[0]


@_SKIP
class TestCrashAfterReserve:
    """Crash after RESERVED but before DISPATCHING.

    Recovery: the operation is in RESERVED state. It was authorized but not
    dispatched. Recovery can safely re-dispatch (transition to DISPATCHING)
    because no external effect occurred.
    """

    def test_crash_after_reserve_recovery(self):
        gov, state = _setup_governor()
        p = Passage(agent_id="alice", action_type="payment", amount=25.0)
        r = gov.adjudicate(p, pressure=0.2, velocity=0.3, capacity=1.0,
                           resource_id="budget", amount=25.0)
        assert r["composed_decision"] in (Decision.PASS.value, Decision.FLAG.value)
        assert r["operation_state"] == OperationState.RESERVED.value

        # Simulate crash: the process dies here.
        # On restart, we inspect the registry.
        record = gov.registry.get(p.idempotency_key)
        assert record.state == OperationState.RESERVED

        # Recovery: this operation was authorized but not dispatched.
        # It's safe to dispatch.
        dispatch_result = gov.dispatch(p.idempotency_key,
                                        resource_id="budget", amount=25.0)
        assert dispatch_result["dispatched"] is True
        assert dispatch_result["operation_state"] == OperationState.DISPATCHING.value

        # Budget was reserved, then committed on dispatch
        assert state.resources["budget"].consumed == 25.0
        assert state.resources["budget"].reserved == 0.0


@_SKIP
class TestCrashAfterDispatchClaim:
    """Crash after DISPATCHING but before external response.

    This is the most dangerous crash point. The external operation was sent
    but the response was lost. The operation is in DISPATCHING state.

    Recovery: the operation MAY have been dispatched. We must NOT blindly
    resend. We must reconcile.
    """

    def test_crash_after_dispatch_recovery_reconciles_not_resends(self):
        gov, state = _setup_governor()
        p = Passage(agent_id="alice", action_type="payment", amount=25.0)
        r = gov.adjudicate(p, pressure=0.2, velocity=0.3, capacity=1.0,
                           resource_id="budget", amount=25.0)
        assert r["composed_decision"] in (Decision.PASS.value, Decision.FLAG.value)

        dispatch_result = gov.dispatch(p.idempotency_key,
                                        resource_id="budget", amount=25.0)
        assert dispatch_result["dispatched"] is True

        # Simulate crash: process dies here. External response is lost.
        # On restart, recovery finds DISPATCHING operations.
        recovery = gov.recover()
        assert recovery["unresolved_count"] == 1
        assert recovery["unresolved"][0]["state"] == OperationState.DISPATCHING.value
        assert recovery["unresolved"][0]["recommendation"] == "reconcile"

        # A retry with the same key must be blocked
        p2 = Passage(agent_id="alice", action_type="payment", amount=25.0,
                     idempotency_key=p.idempotency_key)
        r2 = gov.adjudicate(p2, pressure=0.2, velocity=0.3, capacity=1.0,
                            resource_id="budget", amount=25.0)
        assert r2["composed_decision"] == Decision.HARD_VETO.value
        assert "Duplicate" in r2["composed_rationale"]

        # Budget must not be double-consumed
        assert state.resources["budget"].consumed == 25.0

    def test_two_workers_race_only_one_dispatches(self):
        """Two workers try to dispatch the same operation simultaneously.
        Exactly one should succeed."""
        gov, state = _setup_governor()
        p = Passage(agent_id="alice", action_type="payment", amount=25.0)
        r = gov.adjudicate(p, pressure=0.2, velocity=0.3, capacity=1.0,
                           resource_id="budget", amount=25.0)
        assert r["composed_decision"] in (Decision.PASS.value, Decision.FLAG.value)

        results = []
        barrier = threading.Barrier(2)

        def try_dispatch():
            barrier.wait()
            result = gov.dispatch(p.idempotency_key,
                                  resource_id="budget", amount=25.0)
            results.append(result["dispatched"])

        t1 = threading.Thread(target=try_dispatch)
        t2 = threading.Thread(target=try_dispatch)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly one should succeed
        assert sum(results) == 1, f"Expected 1 dispatch, got {sum(results)}"
        assert state.resources["budget"].consumed == 25.0  # not doubled


@_SKIP
class TestCrashAfterExternalAcceptance:
    """Crash after external system accepted the operation but before
    local confirmation was recorded.

    Recovery: operation is in DISPATCHING. Reconciliation should discover
    the external effect and transition to CONFIRMED.
    """

    def test_reconciliation_confirms_effect(self):
        gov, state = _setup_governor()
        p = Passage(agent_id="alice", action_type="payment", amount=25.0)
        gov.adjudicate(p, pressure=0.2, velocity=0.3, capacity=1.0,
                       resource_id="budget", amount=25.0)
        gov.dispatch(p.idempotency_key, resource_id="budget", amount=25.0)

        # Simulate: external system accepted, but response lost.
        # Reconciliation (in a real system) would query the provider.
        # Here we simulate successful reconciliation:
        assert gov.confirm(p.idempotency_key)
        record = gov.registry.get(p.idempotency_key)
        assert record.state == OperationState.CONFIRMED
        assert record.state.is_terminal

        # A retry after confirmation is blocked
        p2 = Passage(agent_id="alice", action_type="payment", amount=25.0,
                     idempotency_key=p.idempotency_key)
        r2 = gov.adjudicate(p2, pressure=0.2, velocity=0.3, capacity=1.0,
                            resource_id="budget", amount=25.0)
        assert r2["composed_decision"] == Decision.HARD_VETO.value

    def test_reconciliation_finds_no_effect(self):
        """Reconciliation discovers the external system did NOT process
        the operation. Transition to FAILED, release reservation."""
        gov, state = _setup_governor()
        p = Passage(agent_id="alice", action_type="payment", amount=25.0)
        gov.adjudicate(p, pressure=0.2, velocity=0.3, capacity=1.0,
                       resource_id="budget", amount=25.0)
        gov.dispatch(p.idempotency_key, resource_id="budget", amount=25.0)
        assert state.resources["budget"].consumed == 25.0

        # Reconciliation finds no effect → FAILED
        assert gov.fail(p.idempotency_key, resource_id="budget", amount=25.0)
        record = gov.registry.get(p.idempotency_key)
        assert record.state == OperationState.FAILED

        # Budget is released
        assert state.resources["budget"].consumed == 0.0

    def test_reconciliation_uncertain_marks_unknown(self):
        """Reconciliation cannot determine the outcome. Mark UNKNOWN.
        Per review 3 §1: UNKNOWN requires human review, not resend."""
        gov, state = _setup_governor()
        p = Passage(agent_id="alice", action_type="payment", amount=25.0)
        gov.adjudicate(p, pressure=0.2, velocity=0.3, capacity=1.0,
                       resource_id="budget", amount=25.0)
        gov.dispatch(p.idempotency_key, resource_id="budget", amount=25.0)

        # Reconciliation is uncertain → UNKNOWN
        assert gov.mark_unknown(p.idempotency_key)
        record = gov.registry.get(p.idempotency_key)
        assert record.state == OperationState.UNKNOWN

        # Recovery recommends human review
        recovery = gov.recover()
        assert recovery["unresolved_count"] == 1
        assert recovery["unresolved"][0]["recommendation"] == "human_review"

        # Retry is blocked
        p2 = Passage(agent_id="alice", action_type="payment", amount=25.0,
                     idempotency_key=p.idempotency_key)
        r2 = gov.adjudicate(p2, pressure=0.2, velocity=0.3, capacity=1.0,
                            resource_id="budget", amount=25.0)
        assert r2["composed_decision"] == Decision.HARD_VETO.value


@_SKIP
class TestCrashBeforeConfirmation:
    """Crash between external acceptance and recording CONFIRMED.

    The external effect occurred, but the local state was never updated.
    Recovery must find the DISPATCHING operation and reconcile.
    """

    def test_crash_before_confirmation_then_reconcile(self):
        gov, state = _setup_governor()
        p = Passage(agent_id="alice", action_type="payment", amount=25.0)
        gov.adjudicate(p, pressure=0.2, velocity=0.3, capacity=1.0,
                       resource_id="budget", amount=25.0)
        gov.dispatch(p.idempotency_key, resource_id="budget", amount=25.0)

        # Crash: external effect occurred but not recorded.
        # Recovery finds DISPATCHING.
        recovery = gov.recover()
        assert recovery["unresolved"][0]["state"] == OperationState.DISPATCHING.value

        # Reconciliation confirms the effect
        gov.confirm(p.idempotency_key)
        record = gov.registry.get(p.idempotency_key)
        assert record.state == OperationState.CONFIRMED

        # No duplicate
        p2 = Passage(agent_id="alice", action_type="payment", amount=25.0,
                     idempotency_key=p.idempotency_key)
        r2 = gov.adjudicate(p2, pressure=0.2, velocity=0.3, capacity=1.0,
                            resource_id="budget", amount=25.0)
        assert r2["composed_decision"] == Decision.HARD_VETO.value
        assert state.resources["budget"].consumed == 25.0  # not doubled


class TestDurableRegistryPersistence:
    """Test that the registry persists across simulated restarts."""

    def test_registry_survives_restart(self, tmp_path):
        store = str(tmp_path / "operations.json")
        reg1 = DurableOperationRegistry(store_path=store)
        rec = OperationRecord(idempotency_key="op-1", passage_id="p1",
                              agent_id="alice", action_type="payment", amount=25.0)
        reg1.register(rec)
        reg1.transition_to("op-1", OperationState.RESERVED)
        reg1.transition_to("op-1", OperationState.DISPATCHING)

        # Simulate restart: create a new registry pointing to the same store
        reg2 = DurableOperationRegistry(store_path=store)
        loaded = reg2.get("op-1")
        assert loaded is not None
        assert loaded.state == OperationState.DISPATCHING
        assert loaded.amount == 25.0

    def test_registry_blocks_duplicate_after_restart(self, tmp_path):
        store = str(tmp_path / "operations.json")
        reg1 = DurableOperationRegistry(store_path=store)
        rec = OperationRecord(idempotency_key="op-1", passage_id="p1",
                              agent_id="alice", action_type="payment", amount=25.0)
        reg1.register(rec)
        reg1.transition_to("op-1", OperationState.RESERVED)

        # Restart with new registry
        reg2 = DurableOperationRegistry(store_path=store)

        # Duplicate registration is blocked
        rec2 = OperationRecord(idempotency_key="op-1", passage_id="p1",
                               agent_id="alice", action_type="payment", amount=25.0)
        assert not reg2.register(rec2)

        # The operation is still RESERVED
        existing = reg2.get("op-1")
        assert existing.state == OperationState.RESERVED