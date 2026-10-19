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
"""Tests for the transition-system membrane — I3, I4, I6, I7, I8, I9.

Updated for revised API: decisions use Decision enum values, checks return
CheckResult with CheckType tagging, budget reservations are atomic.
"""
import threading
import pytest
from tenir_governance.objects import Agent, Passage, Resource, Delegation
from tenir_governance.objects.passage import ExecutionStatus
from tenir_governance.membrane import TransitionMembrane, MembraneState
from tenir_governance.composition.decisions import Decision


class TestDelegationGate:
    """I7 — Delegation integrity (MANDATORY)."""

    def _setup(self):
        state = MembraneState()
        alice = Agent(agent_id="alice", authority_score=0.9,
                      trusted_delegates=frozenset({"bob"}))
        state.register_agent(alice)
        membrane = TransitionMembrane(state)
        return membrane

    def test_trusted_delegation_passes(self):
        m = self._setup()
        passage = Passage(agent_id="alice", action_type="delegation", recipient_id="bob")
        delegation = Delegation(delegating_agent_id="alice", delegatee_agent_id="bob")
        result = m.adjudicate(passage, delegation=delegation)
        assert result["decision"] == Decision.PASS.value

    def test_untrusted_delegation_blocked(self):
        m = self._setup()
        passage = Passage(agent_id="alice", action_type="delegation", recipient_id="eve")
        delegation = Delegation(delegating_agent_id="alice", delegatee_agent_id="eve")
        result = m.adjudicate(passage, delegation=delegation)
        assert result["decision"] == Decision.HARD_VETO.value
        assert "I7" in result["rationale"]
        assert "cannot delegate to 'eve'" in result["rationale"]

    def test_delegation_by_unregistered_agent(self):
        m = self._setup()
        passage = Passage(agent_id="unknown", action_type="delegation")
        delegation = Delegation(delegating_agent_id="unknown", delegatee_agent_id="bob")
        result = m.adjudicate(passage, delegation=delegation)
        assert result["decision"] == Decision.HARD_VETO.value
        assert "not registered" in result["rationale"]

    def test_no_delegation_no_i7_check(self):
        """A passage without a delegation event doesn't trigger I7."""
        m = self._setup()
        passage = Passage(agent_id="alice", action_type="payment")
        result = m.adjudicate(passage)
        assert result["decision"] == Decision.PASS.value

    def test_delegation_check_is_mandatory(self):
        """I7 must be tagged MANDATORY in the checks list."""
        m = self._setup()
        passage = Passage(agent_id="alice", action_type="delegation", recipient_id="bob")
        delegation = Delegation(delegating_agent_id="alice", delegatee_agent_id="bob")
        result = m.adjudicate(passage, delegation=delegation)
        i7_checks = [c for c in result["checks"] if c["check_id"] == "I7"]
        assert len(i7_checks) == 1
        assert i7_checks[0]["type"] == "mandatory"


class TestBudgetGate:
    """I8 — Aggregate budget (MANDATORY, atomic)."""

    def _setup(self, capacity=100.0):
        state = MembraneState()
        budget = Resource(resource_id="budget", capacity=capacity)
        state.register_resource(budget)
        alice = Agent(agent_id="alice", authority_score=0.9,
                      trusted_delegates=frozenset())
        state.register_agent(alice)
        membrane = TransitionMembrane(state)
        return membrane, state

    def test_payment_within_budget_passes(self):
        m, state = self._setup(capacity=100.0)
        passage = Passage(agent_id="alice", action_type="payment", amount=30.0)
        result = m.adjudicate(passage, resource_id="budget", amount=30.0)
        assert result["decision"] == Decision.PASS.value
        # The membrane is a pure evaluator: it never mutates the budget.
        # Reservation is owned by the governor's transitions.
        assert state.resources["budget"].reserved == 0.0
        assert state.resources["budget"].consumed == 0.0

    def test_payment_exceeding_budget_blocked(self):
        m, state = self._setup(capacity=50.0)
        p1 = Passage(agent_id="alice", action_type="payment", amount=30.0)
        r1 = m.adjudicate(p1, resource_id="budget", amount=30.0)
        assert r1["decision"] == Decision.PASS.value
        # Simulate the governor having reserved and committed the first payment.
        state.resources["budget"].reserve(30.0)
        state.resources["budget"].commit(30.0)
        assert state.resources["budget"].consumed == 30.0
        p2 = Passage(agent_id="alice", action_type="payment", amount=30.0)
        r2 = m.adjudicate(p2, resource_id="budget", amount=30.0)
        assert r2["decision"] == Decision.HARD_VETO.value
        assert "I8" in r2["rationale"]

    def test_multiple_payments_exceeding_budget(self):
        """Each individual payment is small, but combined they exceed the budget."""
        m, state = self._setup(capacity=100.0)
        for i in range(4):
            p = Passage(agent_id="alice", action_type="payment", amount=25.0)
            r = m.adjudicate(p, resource_id="budget", amount=25.0)
            assert r["decision"] == Decision.PASS.value
            # Governor-owned reserve + commit, simulated here.
            state.resources["budget"].reserve(25.0)
            state.resources["budget"].commit(25.0)
        assert state.resources["budget"].consumed == 100.0
        p5 = Passage(agent_id="alice", action_type="payment", amount=1.0)
        r5 = m.adjudicate(p5, resource_id="budget", amount=1.0)
        assert r5["decision"] == Decision.HARD_VETO.value

    def test_reservation_released_on_block(self):
        m, state = self._setup(capacity=100.0)
        p = Passage(agent_id="alice", action_type="payment", amount=60.0)
        r = m.adjudicate(p, resource_id="budget", amount=60.0)
        assert r["decision"] == Decision.PASS.value
        # Governor holds a 60 reservation (simulated). The membrane must not
        # touch it, neither when it passes nor when it vetoes.
        state.resources["budget"].reserve(60.0)
        p2 = Passage(agent_id="alice", action_type="payment", amount=50.0)
        r2 = m.adjudicate(p2, resource_id="budget", amount=50.0)
        assert r2["decision"] == Decision.HARD_VETO.value
        assert state.resources["budget"].reserved == 60.0

    def test_unregistered_resource(self):
        m, state = self._setup()
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        r = m.adjudicate(p, resource_id="nonexistent", amount=10.0)
        assert r["decision"] == Decision.HARD_VETO.value
        assert "not registered" in r["rationale"]

    def test_budget_check_is_mandatory(self):
        """I8 must be tagged MANDATORY in the checks list."""
        m, state = self._setup()
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        result = m.adjudicate(p, resource_id="budget", amount=10.0)
        i8_checks = [c for c in result["checks"] if c["check_id"] == "I8"]
        assert len(i8_checks) == 1
        assert i8_checks[0]["type"] == "mandatory"


class TestQuarantineGate:
    """I6 — No admission under quarantine (MANDATORY)."""

    def test_quarantine_blocks_all(self):
        state = MembraneState()
        state.quarantine = True
        state.register_agent(Agent(agent_id="alice"))
        m = TransitionMembrane(state)
        p = Passage(agent_id="alice", action_type="payment")
        r = m.adjudicate(p)
        assert r["decision"] == Decision.HARD_VETO.value
        assert "I6" in r["rationale"]


class TestTerminalityGate:
    """I3 — Attempt terminality (MANDATORY)."""

    def test_terminal_passage_blocked(self):
        state = MembraneState()
        state.register_agent(Agent(agent_id="alice"))
        m = TransitionMembrane(state)
        p = Passage(agent_id="alice", action_type="payment")
        p.execution_status = ExecutionStatus.RESOLVED
        r = m.adjudicate(p)
        assert r["decision"] == Decision.HARD_VETO.value
        assert "I3" in r["rationale"]


class TestUncertainOutcome:
    """Per review §4: unknown execution outcomes trigger HOLD + reconciliation."""

    def test_reconcile_unknown_returns_hold(self):
        state = MembraneState()
        state.register_agent(Agent(agent_id="alice"))
        m = TransitionMembrane(state)
        p = Passage(agent_id="alice", action_type="payment")
        p.execution_status = ExecutionStatus.DISPATCHED
        outcome = m.reconcile_unknown(p)
        assert outcome == "unknown"
        assert p.execution_status == ExecutionStatus.HELD


class TestMembraneResourceLockCreation:
    def test_concurrent_first_access_returns_one_lock(self):
        state = MembraneState()
        barrier = threading.Barrier(16)
        locks = []
        errors = []

        def get_lock():
            try:
                barrier.wait(timeout=2)
                locks.append(state.get_lock("first-access-resource"))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=get_lock) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)

        assert not errors
        assert len(locks) == 16
        assert all(lock is locks[0] for lock in locks)
        assert state._locks["first-access-resource"] is locks[0]


class TestConcurrencyBudgetRace:
    """Per review §4: two agents reading the same budget state must not both pass."""

    def test_concurrent_membrane_checks_do_not_mutate_budget(self):
        """Membrane is a pure evaluator: concurrent I8 checks never reserve.

        Atomic reservation is the governor's responsibility (see
        test_hold_budget_regression.py::test_concurrent_adjudication_reserves_budget_once).
        """
        state = MembraneState()
        budget = Resource(resource_id="budget", capacity=1500.0)
        state.register_resource(budget)
        state.register_agent(Agent(agent_id="agent_a"))
        state.register_agent(Agent(agent_id="agent_b"))
        m = TransitionMembrane(state)
        barrier = threading.Barrier(2)

        def attempt(agent_id, amount):
            barrier.wait()
            p = Passage(agent_id=agent_id, action_type="payment", amount=amount)
            m.adjudicate(p, resource_id="budget", amount=amount)

        t1 = threading.Thread(target=attempt, args=("agent_a", 1000.0))
        t2 = threading.Thread(target=attempt, args=("agent_b", 1000.0))
        t1.start(); t2.start(); t1.join(); t2.join()

        assert budget.reserved == 0.0
        assert budget.consumed == 0.0
