"""Decisions 2 and 3 (19 Nov 2026): no favorable default for an unmeasured
axis, and a FLAG states the conditions the deployment declared (RFC-0 §4)."""
import pytest

from tenir_governance.composition import DualKernelGovernor, ScoringKernelAdapter
from tenir_governance.composition.decisions import Decision
from tenir_governance.composition.policy import CompositionPolicy
from tenir_governance.membrane import MembraneState, TransitionMembrane
from tenir_governance.objects import Agent, Passage, Resource
from tenir_governance.sdk import DualKernelCompositionClient
from tenir_kernel.kernel import TenirKernel

FULL = dict(pressure=0.2, velocity=0.3, capacity=1.0, authority=0.9, context_fit=0.9, evidence_quality=0.9, option_space=0.9)
ALWAYS_UNMEASURED = {"cooperative_control_score", "trajectory_risk_score",
                     "manipulation_risk", "drift"}


def _gov(policy=None):
    state = MembraneState()
    state.register_resource(Resource(resource_id="budget", capacity=1000.0))
    state.register_agent(Agent(agent_id="alice", authority_score=0.9))
    return DualKernelGovernor(ScoringKernelAdapter(TenirKernel()), TransitionMembrane(state),
                              policy=policy)


def test_sdk_adjudicate_has_no_favorable_defaults():
    import inspect
    params = inspect.signature(DualKernelCompositionClient.adjudicate).parameters
    for name in ("authority", "context_fit", "evidence_quality", "option_space",
                 "pressure", "velocity", "capacity"):
        assert params[name].default is None, name


def test_missing_mandatory_measure_is_not_admitted():
    r = _gov().adjudicate(Passage(agent_id="alice", action_type="read"),
                          pressure=0.2, velocity=0.3, capacity=1.0,
                          option_space=0.9, context_fit=0.9, evidence_quality=0.9)
    assert r["composed_decision"] not in (Decision.PASS.value, Decision.FLAG.value)
    assert "authority" in r["declared_unmeasured"]


def test_absent_option_space_is_declared_unmeasured_and_held():
    """5.7.6: an unmeasured option space is insufficient evidence (decision 2
    applied in full): HOLD, never PASS or FLAG; the axis stays declared."""
    kw = dict(FULL)
    kw.pop("option_space")
    r = _gov(CompositionPolicy(flag_conditions=("review",))).adjudicate(
        Passage(agent_id="alice", action_type="read"), **kw)
    assert "option_space" in r["declared_unmeasured"]
    assert ALWAYS_UNMEASURED <= set(r["declared_unmeasured"])
    assert r["scoring_decision"] == Decision.HOLD.value
    assert r["composed_decision"] == Decision.HOLD.value
    # 5.7.7 candidate (R7): the unknown option space is named as such.
    assert "absent measure(s) option_space" in r["scoring_rationale"]


def test_flag_without_declared_conditions_is_held_not_admitted():
    """5.7.6 (RFC-0 §4): with the default policy a FLAG states no condition, so
    the runtime holds the operation instead of admitting it (decision 3)."""
    r = _gov().adjudicate(Passage(agent_id="alice", action_type="read"), **FULL)
    assert r["scoring_decision"] == Decision.FLAG.value
    assert r["composed_decision"] == Decision.HOLD.value
    assert r["operation_state"] == "hold"
    assert r["flag_conditions"] == []
    assert "FLAG states no condition" in r["composed_rationale"]
    assert "flag_conditions" in r["composed_rationale"]


def test_flag_carries_the_policy_declared_conditions_and_ledgers_them():
    conds = ("human review of the effect within 24 h", "amount capped at 100")
    gov = _gov(CompositionPolicy(flag_conditions=conds))
    r = gov.adjudicate(Passage(agent_id="alice", action_type="read"), **FULL)
    assert r["composed_decision"] == Decision.FLAG.value
    assert r["flag_conditions"] == list(conds)
    assert all(c in r["composed_rationale"] for c in conds)
    composed = [e for e in gov.ledger.entries if e.kernel == "composed"][-1]
    assert all(c in composed.rationale for c in conds)


def test_non_flag_result_carries_no_conditions():
    gov = _gov(CompositionPolicy(flag_conditions=("review",)))
    r = gov.adjudicate(Passage(agent_id="alice", action_type="read"),
                       pressure=0.2, velocity=0.3, capacity=1.0,
                       context_fit=0.9, evidence_quality=0.9, option_space=0.9)
    assert r["composed_decision"] != Decision.FLAG.value
    assert r["flag_conditions"] == []


@pytest.mark.parametrize("bad", [("",), ("  ",), ["review"], (1,)])
def test_policy_rejects_malformed_flag_conditions(bad):
    with pytest.raises(ValueError):
        CompositionPolicy(flag_conditions=bad).validate()


@pytest.mark.parametrize("missing", ["pressure", "velocity", "capacity"])
def test_missing_pressure_velocity_or_capacity_is_not_admitted(missing):
    kw = dict(FULL)
    kw.pop(missing)
    r = _gov().adjudicate(Passage(agent_id="alice", action_type="read"), **kw)
    assert r["composed_decision"] not in (Decision.PASS.value, Decision.FLAG.value)
    assert missing in r["declared_unmeasured"]


def test_unassessed_trust_is_not_a_favorable_default():
    from tenir_governance.objects.relationship import Relationship
    assert Relationship(from_agent_id="a", to_agent_id="b").trust_score is None
