"""Tests for the governed-object model."""
# Profile: agentic-governance-profile-v1.0.0
# Compliance class: profile-bound
# This module is an extension on top of KB3 and TENIR 2.0 schema 1.2.
# It is NOT canonical, NOT doctrinal. It carries implementation-level
# governance enforcement authority only.
import pytest
from tenir_governance.objects import (
    Agent, Passage, Trajectory, Relationship, Delegation,
    Resource, Budget, MissionInvariant,
)
from tenir_governance.objects.passage import ExecutionStatus


class TestAgent:
    def test_agent_creation(self):
        a = Agent(agent_id="A1", authority_score=0.9,
                  trusted_delegates=frozenset({"A2", "A3"}),
                  capabilities=frozenset({"payment"}))
        assert a.agent_id == "A1"
        assert a.can_delegate_to("A2") is True
        assert a.can_delegate_to("A4") is False
        assert a.has_capability("payment") is True
        assert a.has_capability("read") is False

    def test_default_agent(self):
        a = Agent(agent_id="A1")
        assert a.authority_score == 1.0
        assert a.trusted_delegates == frozenset()
        assert a.can_delegate_to("anyone") is False


class TestPassage:
    def test_passage_creation(self):
        p = Passage(agent_id="A1", action_type="payment",
                    target_realm="finance", amount=100.0)
        assert p.agent_id == "A1"
        assert p.action_type == "payment"
        assert p.amount == 100.0
        assert p.execution_status == ExecutionStatus.PENDING
        assert p.is_terminal is False
        assert p.is_executed is False

    def test_passage_terminal(self):
        p = Passage(agent_id="A1", action_type="payment")
        p.execution_status = ExecutionStatus.RESOLVED
        assert p.is_terminal is True
        assert p.is_executed is False

    def test_passage_dispatched(self):
        p = Passage(agent_id="A1", action_type="payment")
        p.execution_status = ExecutionStatus.DISPATCHED
        assert p.is_executed is True
        assert p.is_terminal is False

    def test_auto_id(self):
        p1 = Passage()
        p2 = Passage()
        assert p1.passage_id != p2.passage_id


class TestTrajectory:
    def test_trajectory_consistent(self):
        t = Trajectory(trajectory_id="T1",
                       temporal_constraints=[(0, 5), (0, 5)])
        t.add_passage(Passage(), timestamp=0.0)
        t.add_passage(Passage(), timestamp=2.0)
        t.add_passage(Passage(), timestamp=4.0)
        ok, reason = t.check_temporal_consistency()
        assert ok is True
        assert reason is None

    def test_trajectory_violation_too_fast(self):
        t = Trajectory(trajectory_id="T1",
                       temporal_constraints=[(3, 10)])
        t.add_passage(Passage(), timestamp=0.0)
        t.add_passage(Passage(), timestamp=1.0)  # gap=1 < min=3
        ok, reason = t.check_temporal_consistency()
        assert ok is False
        assert "too closely" in reason

    def test_trajectory_violation_too_slow(self):
        t = Trajectory(trajectory_id="T1",
                       temporal_constraints=[(0, 5)])
        t.add_passage(Passage(), timestamp=0.0)
        t.add_passage(Passage(), timestamp=10.0)  # gap=10 > max=5
        ok, reason = t.check_temporal_consistency()
        assert ok is False
        assert "too late" in reason


class TestResource:
    def test_resource_reserve_and_commit(self):
        r = Resource(resource_id="R1", capacity=100.0)
        assert r.available == 100.0
        assert r.reserve(30.0) is True
        assert r.available == 70.0
        assert r.reserved == 30.0
        assert r.commit(30.0) is True
        assert r.consumed == 30.0
        assert r.reserved == 0.0
        assert r.available == 70.0

    def test_resource_reserve_insufficient(self):
        r = Resource(resource_id="R1", capacity=50.0)
        assert r.reserve(60.0) is False
        assert r.reserved == 0.0

    def test_resource_release(self):
        r = Resource(resource_id="R1", capacity=100.0)
        r.reserve(30.0)
        r.release(30.0)
        assert r.reserved == 0.0
        assert r.available == 100.0

    def test_budget(self):
        b = Budget(capacity=1000.0)
        assert b.resource_id == "budget"
        assert b.capacity == 1000.0


class TestDelegation:
    def test_delegation_trusted(self):
        d = Delegation(delegating_agent_id="A1", delegatee_agent_id="A2")
        assert d.is_trusted(frozenset({"A2", "A3"})) is True
        assert d.is_trusted(frozenset({"A3"})) is False

    def test_delegation_active(self):
        d = Delegation(delegating_agent_id="A1", delegatee_agent_id="A2")
        assert d.active is True


class TestMissionInvariant:
    def test_invariant_holds(self):
        inv = MissionInvariant(
            invariant_id="I_budget",
            description="Total spending must not exceed budget",
            predicate=lambda state: state.get("total_spent", 0) <= state.get("budget", 0),
        )
        assert inv.evaluate({"total_spent": 80, "budget": 100}) == (True, None)

    def test_invariant_violated(self):
        inv = MissionInvariant(
            invariant_id="I_budget",
            description="Total spending must not exceed budget",
            predicate=lambda state: state.get("total_spent", 0) <= state.get("budget", 0),
        )
        holds, reason = inv.evaluate({"total_spent": 120, "budget": 100})
        assert holds is False
        assert "I_budget" in reason