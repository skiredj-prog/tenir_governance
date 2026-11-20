"""Issue #141 (fixed in 5.7.6): a budget HOLD above half the remaining capacity
could not be resolved, because `resolve_hold` let I8 count the operation's own
reservation a second time. The re-adjudication now declares that reservation to
the membrane, as `adjudicate` already did.

Observed on 5.7.5 (capacity 100): held amounts 50.5, 60 and 90 were refused
with HARD_VETO ("Aggregate budget violation") and reached FAILED; 10 and 50
resolved. Now every amount the budget covers resolves on the budget axis; 90
still composes to HOLD because the policy escalates a FLAG at 85 % utilization
(budget_escalation_threshold), which is the policy's rule, not the #141 defect.
"""
import pytest

from tenir_governance.composition import DualKernelGovernor, ScoringKernelAdapter
from tenir_governance.composition.decisions import Decision
from tenir_governance.composition.policy import CompositionPolicy
from tenir_governance.membrane import MembraneState, TransitionMembrane
from tenir_governance.objects import Agent, Passage, Resource
from tenir_governance.operations import MockProvider, OperationState
from tenir_kernel.kernel import TenirKernel

CONDITIONS = ("ops supervision",)
ACK = {"ops supervision": "ops rota 2026-11-20"}
BEST = dict(pressure=0.2, velocity=0.2, capacity=0.9, authority=0.9, context_fit=1.0,
            evidence_quality=1.0, option_space=0.9)
HELD = {**BEST, "evidence_quality": float("nan")}  # insufficient evidence: HOLD, budget reserved


def _gov(policy=None):
    state = MembraneState()
    state.register_resource(Resource(resource_id="budget", capacity=100.0))
    state.register_agent(Agent(agent_id="alice", authority_score=0.9))
    gov = DualKernelGovernor(ScoringKernelAdapter(TenirKernel()), TransitionMembrane(state),
                             policy=policy or CompositionPolicy(flag_conditions=CONDITIONS),
                             provider=MockProvider())
    return gov, state.resources["budget"]


def _hold(gov, amount):
    p = Passage(agent_id="alice", action_type="payment", amount=amount)
    r = gov.adjudicate(p, **HELD, resource_id="budget", amount=amount)
    assert r["composed_decision"] == Decision.HOLD.value and r["operation_state"] == OperationState.HOLD.value
    return p


@pytest.mark.parametrize("amount", [10.0, 50.0, 50.5, 60.0, 84.0])
def test_budget_hold_resolves_whatever_its_share_of_the_capacity(amount):
    gov, budget = _gov()
    p = _hold(gov, amount)
    assert budget.reserved == amount
    res = gov.resolve_hold(p.idempotency_key, passage=p, **BEST, resource_id="budget", amount=amount)
    assert res["membrane_decision"] == Decision.PASS.value, res["membrane_rationale"]
    assert res["composed_decision"] == Decision.FLAG.value
    assert res["hold_resolved"] is True and res["operation_state"] == OperationState.RESERVED.value
    assert budget.reserved == amount  # one reservation, not two
    out = gov.dispatch_and_execute(p.idempotency_key, "budget", amount, flag_conditions_met=ACK)
    assert out["dispatched"] is True and out["operation_state"] == OperationState.CONFIRMED.value
    assert budget.consumed == amount and budget.reserved == 0.0
    assert len(gov.provider.calls) == 1


def test_above_the_escalation_threshold_the_hold_is_the_policy_rule_not_i8():
    gov, budget = _gov()
    p = _hold(gov, 90.0)
    res = gov.resolve_hold(p.idempotency_key, passage=p, **BEST, resource_id="budget", amount=90.0)
    assert res["membrane_decision"] == Decision.PASS.value  # I8 no longer vetoes
    assert res["composed_decision"] == Decision.HOLD.value and res["hold_resolved"] is False
    assert "escalated to HOLD" in res["composed_rationale"]
    assert gov.registry.get(p.idempotency_key).state == OperationState.HOLD
    assert budget.reserved == 90.0


def test_with_a_higher_threshold_90_of_100_resolves():
    gov, budget = _gov(CompositionPolicy(flag_conditions=CONDITIONS, budget_escalation_threshold=0.95))
    p = _hold(gov, 90.0)
    res = gov.resolve_hold(p.idempotency_key, passage=p, **BEST, resource_id="budget", amount=90.0)
    assert res["hold_resolved"] is True and res["composed_decision"] == Decision.FLAG.value
    assert gov.dispatch_and_execute(p.idempotency_key, "budget", 90.0, flag_conditions_met=ACK)["dispatched"]
    assert budget.consumed == 90.0


def test_a_second_operation_still_counts_the_first_reservation():
    """The fix excludes only the operation's own reservation: another
    operation's reservation still bounds the budget (I8)."""
    gov, budget = _gov()
    first = _hold(gov, 60.0)
    second = _hold(gov, 30.0)
    assert budget.reserved == 90.0
    # 60 + 30 = 90 ≤ 100: both resolve on the budget axis ...
    res = gov.resolve_hold(second.idempotency_key, passage=second, **BEST, resource_id="budget", amount=30.0)
    assert res["membrane_decision"] == Decision.PASS.value
    # ... but a third 20 would exceed the capacity and I8 must say so.
    third = Passage(agent_id="alice", action_type="payment", amount=20.0)
    r = gov.adjudicate(third, **BEST, resource_id="budget", amount=20.0)
    assert r["membrane_decision"] == Decision.HARD_VETO.value
    assert r["composed_decision"] == Decision.HARD_VETO.value
    assert gov.resolve_hold(first.idempotency_key, passage=first, **BEST, resource_id="budget",
                            amount=60.0)["membrane_decision"] == Decision.PASS.value
