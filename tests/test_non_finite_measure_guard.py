"""Decision 5 (19 Nov 2026): a non-finite measure is insufficient evidence,
never PASS, and it does not mask a conclusive veto (RFC-0 §4)."""
import math
import warnings

import pytest

from tenir_governance.composition import DualKernelGovernor, ScoringKernelAdapter
from tenir_governance.composition.decisions import Decision
from tenir_governance.membrane import MembraneState, TransitionMembrane
from tenir_governance.objects import Agent, Passage, Resource
from tenir_kernel.kernel import TenirKernel

BEST = dict(pressure=0.2, velocity=0.2, capacity=0.9, authority=0.9,
            context_fit=1.0, evidence_quality=1.0, option_space=0.9)
TAU_BREACH = dict(pressure=0.95, velocity=0.90, capacity=0.10)


def _adapter():
    return ScoringKernelAdapter(TenirKernel())


def _governor():
    state = MembraneState()
    state.register_agent(Agent(agent_id="a", authority_score=0.9))
    state.register_resource(Resource(resource_id="b", capacity=100.0))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return DualKernelGovernor(_adapter(), TransitionMembrane(state))


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_evidence_is_held_not_admitted(bad):
    r = _adapter().adjudicate(**{**BEST, "evidence_quality": bad})
    assert r["decision"] == Decision.HOLD.value
    assert "INSUFFICIENT_EVIDENCE" in r["rationale"]
    assert r["unmeasured_axes"] == ["evidence_quality"]
    assert "evidence_quality" in r["declared_unmeasured"]
    assert r["evidence_quality"] is None


def test_conclusive_veto_governs_over_non_finite_evidence():
    r = _adapter().adjudicate(**{**BEST, **TAU_BREACH, "evidence_quality": math.nan})
    assert r["decision"] == Decision.HARD_VETO.value
    assert "evidence_quality" in r["unmeasured_axes"]


def test_zero_authority_veto_governs_over_non_finite_evidence():
    r = _adapter().adjudicate(**{**BEST, "authority": 0.0, "evidence_quality": math.nan})
    assert r["decision"] == Decision.HARD_VETO.value


def test_governor_composes_the_veto_not_a_fail_closed_hold():
    r = _governor().adjudicate(Passage(agent_id="a", action_type="wire_transfer"),
                               **{**BEST, **TAU_BREACH, "evidence_quality": math.nan})
    assert r["scoring_decision"] == Decision.HARD_VETO.value
    assert r["composed_decision"] == Decision.HARD_VETO.value


@pytest.mark.parametrize("axis", ["pressure", "velocity", "capacity"])
def test_non_finite_pressure_velocity_or_capacity_holds_without_an_s(axis):
    r = _adapter().adjudicate(**{**BEST, axis: math.nan})
    assert r["decision"] == Decision.HOLD.value
    assert r["s_score"] is None
    assert r["unmeasured_axes"] == [axis]


def test_non_finite_s_input_does_not_hide_a_block_floor_veto():
    r = _adapter().adjudicate(**{**BEST, "pressure": math.nan, "option_space": 0.1})
    assert r["decision"] == Decision.HARD_VETO.value


@pytest.mark.parametrize("bad", ["0.9", True])
def test_non_number_is_not_a_measure(bad):
    r = _adapter().adjudicate(**{**BEST, "authority": bad})
    assert r["decision"] != Decision.PASS.value and r["decision"] != Decision.FLAG.value
    assert "authority" in r["unmeasured_axes"]


def test_zero_capacity_vetoes_even_with_unknown_pressure():
    """S = C / (P·V + ε) is 0 whatever P and V are when C = 0."""
    r = _adapter().adjudicate(**{**BEST, "pressure": math.nan, "capacity": 0.0})
    assert r["decision"] == Decision.HARD_VETO.value
    assert "TAU BREACH" in r["rationale"]


def test_governor_holds_on_non_finite_evidence_never_admits():
    """The scoring HOLD is not dropped by composition (lattice rule 2b)."""
    r = _governor().adjudicate(Passage(agent_id="a", action_type="x"),
                               **{**BEST, "evidence_quality": math.nan})
    assert r["scoring_decision"] == Decision.HOLD.value
    assert r["composed_decision"] == Decision.HOLD.value
    assert r["operation_state"] == "hold"


def test_lattice_scoring_hold_with_membrane_pass_is_hold():
    from tenir_governance.composition.lattice import compose
    assert compose("HOLD", "PASS")[0] == "HOLD"
    assert compose("HOLD", "FLAG")[0] == "HOLD"
