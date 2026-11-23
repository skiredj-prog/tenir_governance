"""P8 of the review of 2026-11-23, decided by the author the same day, option A
(candidate 5.7.7, not released; draft RFC-2 v0.2 §1.4, not ratified).

The kernel declares its admissible domain for every measure it reads
(ScoringKernelAdapter.declared_domain(), [0, 1] for each axis). The standard
prescribes no domain. Any finite value outside the declared domain is rejected
explicitly, at the API boundary, as INVALID_ARGUMENT: nothing is registered,
reserved, held or vetoed, and the ledger keeps the refusal under kernel "api".
Between 19 and 23 November such a value was read as an unknown measure and the
request was closed by a HOLD, a disposition, instead of being refused.

An unknown measure is still passed as None (absent) or NaN (non-finite): rule A,
HOLD unless a conclusive veto governs. A boolean or a non-number is not a value
of the axis either and stays an unknown measure (declared limit of this change,
decision 5 of 19 November unchanged on that point).
"""
import math

import pytest

from tenir_governance.composition import (
    DualKernelGovernor, InvalidArgumentError, ScoringKernelAdapter,
)
from tenir_governance.composition.decisions import Decision
from tenir_governance.composition.policy import CompositionPolicy
from tenir_governance.membrane import MembraneState, TransitionMembrane
from tenir_governance.objects import Agent, Passage, Resource
from tenir_governance.operations import MockProvider, OperationState
from tenir_kernel.kernel import TenirKernel

M = dict(irreversibility_materiality=0.0, pressure=0.2, velocity=0.2, capacity=0.9,
         option_space=0.9, authority=0.9, context_fit=0.9, evidence_quality=0.9)
AXES = tuple(M)


def _governor():
    state = MembraneState()
    state.register_resource(Resource(resource_id="budget", capacity=100.0))
    state.register_agent(Agent(agent_id="alice", authority_score=0.9))
    gov = DualKernelGovernor(ScoringKernelAdapter(TenirKernel()), TransitionMembrane(state),
                             policy=CompositionPolicy(flag_conditions=("ops supervision",)),
                             provider=MockProvider())
    return gov, state.resources["budget"]


def _entries(gov):
    return [(e.kernel, e.decision) for e in gov.ledger.entries]


# --- the declaration ---------------------------------------------------------

def test_the_kernel_declares_a_domain_for_every_measured_axis():
    domain = ScoringKernelAdapter.declared_domain()
    assert set(domain) == set(AXES)
    for name, (low, high) in domain.items():
        assert math.isfinite(low) and math.isfinite(high) and low < high, name
        assert (low, high) == (0.0, 1.0)


def test_edges_of_the_domain_are_values():
    adapter = ScoringKernelAdapter(TenirKernel())
    assert adapter.out_of_domain({**M, "authority": 0.0}) == []
    assert adapter.out_of_domain({**M, "authority": 1.0}) == []
    r = adapter.adjudicate(**{**M, "authority": 1.0})
    assert r["decision"] in {Decision.PASS.value, Decision.FLAG.value}


@pytest.mark.parametrize("value", [None, math.nan, math.inf, -math.inf, True, "0.5"])
def test_none_nan_and_non_numbers_are_not_out_of_domain(value):
    """They are unknown measures (rule A), not values outside the domain."""
    assert ScoringKernelAdapter.out_of_domain({**M, "authority": value}) == []


# --- the rejection, at the kernel adapter ------------------------------------

@pytest.mark.parametrize("axis", AXES)
@pytest.mark.parametrize("value", [1.5, -0.5, 1.0000001, -1e-9, 2, 100.0])
def test_a_finite_value_outside_the_domain_is_rejected_explicitly(axis, value):
    adapter = ScoringKernelAdapter(TenirKernel())
    with pytest.raises(InvalidArgumentError) as exc:
        adapter.adjudicate(**{**M, axis: value})
    err = exc.value
    assert isinstance(err, ValueError) and err.code == "INVALID_ARGUMENT"
    assert err.field == axis
    assert f"{axis}={value!r}" in err.reason and "declared domain [0, 1]" in err.reason


def test_several_out_of_domain_measures_are_all_named():
    adapter = ScoringKernelAdapter(TenirKernel())
    with pytest.raises(InvalidArgumentError) as exc:
        adapter.adjudicate(**{**M, "pressure": 1.5, "capacity": -0.5})
    assert exc.value.field == "pressure,capacity"
    assert "pressure=1.5" in exc.value.reason and "capacity=-0.5" in exc.value.reason


def test_the_refusal_precedes_every_evaluation_even_a_conclusive_veto():
    """authority=0.0 is a conclusive veto; with an out-of-domain capacity the
    request is malformed and refused, no veto is drawn from it."""
    adapter = ScoringKernelAdapter(TenirKernel())
    vetoed = adapter.adjudicate(**{**M, "authority": 0.0})
    assert vetoed["decision"] == Decision.HARD_VETO.value
    with pytest.raises(InvalidArgumentError):
        adapter.adjudicate(**{**M, "authority": 0.0, "capacity": 1.5})


@pytest.mark.parametrize("unknown", [None, math.nan])
def test_nan_and_none_still_take_the_insufficient_evidence_path(unknown):
    r = ScoringKernelAdapter(TenirKernel()).adjudicate(**{**M, "authority": unknown})
    assert r["decision"] == Decision.HOLD.value
    assert r["insufficient_evidence"] is True
    assert "authority" in r["rationale"]


# --- the rejection, at the governor's API boundary ---------------------------

@pytest.mark.parametrize("axis,value", [("pressure", 1.5), ("authority", -0.5),
                                        ("irreversibility_materiality", 1.5)])
def test_governor_refuses_before_registration_and_reserves_nothing(axis, value):
    gov, budget = _governor()
    before = len(gov.ledger.entries)
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    with pytest.raises(InvalidArgumentError) as exc:
        gov.adjudicate(p, **{**M, axis: value}, payload={"action_type": "payment"},
                       resource_id="budget", amount=10.0)
    err = exc.value
    assert err.code == "INVALID_ARGUMENT" and err.field == axis
    assert err.idempotency_key == p.idempotency_key and err.passage_id == p.passage_id
    assert gov.registry.get(p.idempotency_key) is None, "nothing registered"
    assert budget.reserved == 0.0 and budget.consumed == 0.0
    assert _entries(gov)[before:] == [("api", "INVALID_ARGUMENT")]
    assert "HARD_VETO" not in str(err.to_dict()) and "HOLD" not in str(err.to_dict())
    assert gov.verify_ledger()


def test_governor_refusal_is_not_a_fail_closed_hold():
    """Before P8 the adapter's raise would have been caught by the governor's
    fail-closed path and reported as a HOLD ('Scoring kernel error')."""
    gov, _ = _governor()
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    with pytest.raises(InvalidArgumentError):
        gov.adjudicate(p, **{**M, "velocity": 5.0}, payload={"action_type": "payment"},
                       resource_id="budget", amount=10.0)
    assert not [e for e in gov.ledger.entries if e.composed_decision]


def test_resolve_hold_refuses_an_out_of_domain_measure_and_keeps_the_hold():
    gov, budget = _governor()
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    held = gov.adjudicate(p, **{**M, "authority": None}, payload={"action_type": "payment"},
                          resource_id="budget", amount=10.0)
    assert held["composed_decision"] == Decision.HOLD.value
    assert gov.registry.get(p.idempotency_key).state == OperationState.HOLD
    with pytest.raises(InvalidArgumentError) as exc:
        gov.resolve_hold(p.idempotency_key, p, **{**M, "authority": 1.5},
                         resource_id="budget", amount=10.0, payload={"action_type": "payment"})
    assert exc.value.field == "authority"
    assert gov.registry.get(p.idempotency_key).state == OperationState.HOLD, "HOLD untouched"
    assert budget.reserved == 0.0
    # With a measure, the same HOLD resolves.
    done = gov.resolve_hold(p.idempotency_key, p, **M, resource_id="budget", amount=10.0,
                            payload={"action_type": "payment"})
    assert done["hold_resolved"] is True


def test_a_scoring_adapter_without_a_declared_domain_is_not_checked_by_the_governor():
    """The governor prescribes no domain: it only enforces the kernel's own
    declaration. An adapter that declares none decides at its own entry."""
    class _NoDomain:
        def adjudicate(self, **kw):
            return {"decision": Decision.HOLD.value, "rationale": "no domain declared",
                    "unmeasured_axes": [], "declared_unmeasured": [], "insufficient_evidence": False}
    state = MembraneState()
    state.register_agent(Agent(agent_id="alice", authority_score=0.9))
    gov = DualKernelGovernor(_NoDomain(), TransitionMembrane(state),
                             policy=CompositionPolicy(flag_conditions=("ops supervision",)))
    r = gov.adjudicate(Passage(agent_id="alice", action_type="read"), **{**M, "pressure": 1.5})
    assert r["composed_decision"] == Decision.HOLD.value
