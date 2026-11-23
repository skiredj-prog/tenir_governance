"""R7 and R8 of the review of 2026-11-23, decided by the author the same day
(candidate 5.7.7, not released).

R7, unmeasured axes are unknown. The adapter's interface never measures four
geometry axes (cooperative_control_score, trajectory_risk_score,
manipulation_risk, drift). The kernel needs a complete point, so the main
path fills them with a neutral placeholder; no facet may read that
placeholder: a geometry whose facet does is rejected when the adapter is
built. An unknown option_space takes the insufficient-evidence path, where a
facet is conclusive only whatever value the unknown axis would take.

R8, a malformed request is not a decision. A payload outside canonical JSON,
an invalid amount, a missing Profile S digest or identity: INVALID_ARGUMENT at
the API boundary (InvalidArgumentError, a ValueError), nothing registered,
reserved, held or vetoed, and the ledger keeps the refusal under kernel "api".
5.7.6 returned these as composed_decision HARD_VETO, operation_state
not_registered.
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
from tenir_kernel.geometry import Facet, GeometryPolicy, REFERENCE_GEOMETRY
from tenir_kernel.kernel import KernelPolicy, TenirKernel

CONDITIONS = ("ops supervision",)
M = dict(pressure=0.2, velocity=0.2, capacity=0.9, option_space=0.9,
         authority=0.9, context_fit=0.9, evidence_quality=0.9)
NEVER_MEASURED = {"cooperative_control_score", "trajectory_risk_score",
                  "manipulation_risk", "drift"}
MEASURED_AXES = {"pressure", "velocity", "capacity", "option_space", "authority",
                 "context_fit", "evidence_quality", "irreversible_pathway_materiality"}


def _kernel_with(*facets):
    return TenirKernel(KernelPolicy(geometry=GeometryPolicy(
        REFERENCE_GEOMETRY.geometry_id, "test-geometry",
        REFERENCE_GEOMETRY.facets + tuple(facets))))


def _governor(kernel=None, policy=None):
    state = MembraneState()
    state.register_resource(Resource(resource_id="budget", capacity=100.0))
    state.register_agent(Agent(agent_id="alice", authority_score=0.9))
    gov = DualKernelGovernor(ScoringKernelAdapter(kernel or TenirKernel()),
                             TransitionMembrane(state),
                             policy=policy or CompositionPolicy(flag_conditions=CONDITIONS),
                             provider=MockProvider())
    return gov, state.resources["budget"]


# --- R7: the never-measured axes --------------------------------------------

def test_reference_geometry_reads_no_never_measured_axis():
    for facet in REFERENCE_GEOMETRY.facets:
        assert not (set(facet.normal) & NEVER_MEASURED), facet.facet_id
        assert set(facet.normal) <= MEASURED_AXES, facet.facet_id
    ScoringKernelAdapter(TenirKernel())  # accepted


@pytest.mark.parametrize("severity", ["hard", "soft"])
@pytest.mark.parametrize("axis", sorted(NEVER_MEASURED))
def test_a_facet_reading_a_never_measured_axis_is_rejected_at_construction(axis, severity):
    facet = Facet(f"test-{axis}", {axis: -1.0}, -0.6, severity, "test facet on a placeholder")
    with pytest.raises(ValueError) as exc:
        ScoringKernelAdapter(_kernel_with(facet))
    message = str(exc.value)
    assert "Geometry rejected" in message and f"test-{axis}" in message and axis in message


def test_a_facet_mixing_measured_and_never_measured_axes_is_rejected_too():
    facet = Facet("mixed", {"authority": -1.0, "drift": 1.0}, -0.3, "hard", "mixed")
    with pytest.raises(ValueError, match="mixed .hard. reads drift"):
        ScoringKernelAdapter(_kernel_with(facet))


def test_placeholders_never_reach_a_facet_on_the_reference_geometry():
    """The main path still hands the kernel a complete point with 0.5 on the
    never-measured axes; every facet of the accepted geometry reads measured
    axes only, so no verdict can depend on those values."""
    adapter = ScoringKernelAdapter(TenirKernel())
    r = adapter.adjudicate(**M)
    assert r["decision"] in (Decision.PASS.value, Decision.FLAG.value)
    assert set(r["declared_unmeasured"]) == NEVER_MEASURED
    assert r["insufficient_evidence"] is False
    facets_read = set().union(*(set(f.normal) for f in REFERENCE_GEOMETRY.facets))
    assert not (facets_read & set(r["declared_unmeasured"]))


# --- R7: option_space is unknown like every other measure --------------------

@pytest.mark.parametrize("value", [None, float("nan"), 1.5])
def test_unknown_option_space_takes_the_insufficient_evidence_path(value):
    adapter = ScoringKernelAdapter(TenirKernel())
    r = adapter.adjudicate(**{**M, "option_space": value})
    assert r["decision"] == Decision.HOLD.value
    assert r["insufficient_evidence"] is True
    assert r["constraint_geometry"] is None and r["operating_mode"] is None
    kind = "absent" if value is None else "invalid"
    assert f"{kind} measure(s)" in r["rationale"] and "option_space" in r["rationale"]
    assert r["declared_unmeasured"][0] == "option_space"
    assert NEVER_MEASURED <= set(r["declared_unmeasured"])


def test_no_veto_on_the_option_space_placeholder():
    """A hard facet that needs option_space >= 0.6. 5.7.6 evaluated an absent
    option space at 0.5 and vetoed on that placeholder; the unknown axis may
    be 1.0, so nothing is conclusive: HOLD."""
    facet = Facet("option-space-floor", {"option_space": -1.0}, -0.6, "hard", "needs option space")
    adapter = ScoringKernelAdapter(_kernel_with(facet))
    held = adapter.adjudicate(**{**M, "option_space": None})
    assert held["decision"] == Decision.HOLD.value
    assert "needs option space" not in held["rationale"]
    vetoed = adapter.adjudicate(**{**M, "option_space": 0.5})
    assert vetoed["decision"] == Decision.HARD_VETO.value
    assert "needs option space" in vetoed["rationale"]


def test_a_facet_violated_whatever_option_space_is_stays_conclusive():
    """authority + option_space must reach 1.5. With authority 0.4 even an
    option space of 1.0 cannot satisfy it: HARD_VETO whatever the unknown is.
    With authority 0.9 it can: HOLD."""
    facet = Facet("joint-floor", {"authority": -1.0, "option_space": -1.0}, -1.5, "hard",
                  "authority and option space too low together")
    adapter = ScoringKernelAdapter(_kernel_with(facet))
    vetoed = adapter.adjudicate(**{**M, "authority": 0.4, "option_space": None})
    assert vetoed["decision"] == Decision.HARD_VETO.value
    assert "Conclusive whatever the unknown measure(s) are" in vetoed["rationale"]
    held = adapter.adjudicate(**{**M, "option_space": None})
    assert held["decision"] == Decision.HOLD.value


def test_unknown_option_space_holds_without_reservation_in_the_governor():
    gov, budget = _governor()
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    r = gov.adjudicate(p, payload={"a": 1}, **{**M, "option_space": None},
                       resource_id="budget", amount=10.0)
    assert r["composed_decision"] == Decision.HOLD.value
    assert r["operation_state"] == OperationState.HOLD.value
    assert r["budget_reserved"] is False and budget.reserved == 0.0
    res = gov.resolve_hold(p.idempotency_key, passage=p, payload={"a": 1}, **M,
                           resource_id="budget", amount=10.0)
    assert res["hold_resolved"] is True and res["budget_reserved"] is True
    assert budget.reserved == 10.0


# --- R8: INVALID_ARGUMENT is not a decision -----------------------------------

def _entries(gov):
    return [(e.kernel, e.decision) for e in gov.ledger.entries]


@pytest.mark.parametrize("malformed, field", [
    (dict(payload={"note": float("nan")}), "payload"),
    (dict(payload=["not", "an", "object"]), "payload"),
    (dict(payload={1: "non-string key"}), "payload"),
    (dict(amount=float("nan")), "amount"),
    (dict(amount=-1.0), "amount"),
    (dict(amount=True), "amount"),
    (dict(amount=0.0), "amount"),  # zero with a resource named
])
def test_malformed_request_raises_invalid_argument_and_registers_nothing(malformed, field):
    gov, budget = _governor()
    before = len(gov.ledger.entries)
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    kw = dict(payload={"action_type": "payment"}, resource_id="budget", amount=10.0)
    kw.update(malformed)
    with pytest.raises(InvalidArgumentError) as exc:
        gov.adjudicate(p, **M, **kw)
    err = exc.value
    assert isinstance(err, ValueError)
    assert err.code == "INVALID_ARGUMENT" and err.field == field
    assert err.idempotency_key == p.idempotency_key and err.passage_id == p.passage_id
    assert gov.registry.get(p.idempotency_key) is None
    assert budget.reserved == 0.0 and budget.consumed == 0.0
    # The ledger keeps the refusal under "api", never as a composed decision.
    new = _entries(gov)[before:]
    assert new == [("api", "INVALID_ARGUMENT")]
    assert gov.ledger.verify_chain()


def test_invalid_argument_to_dict_carries_no_decision_field():
    gov, _budget = _governor()
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    with pytest.raises(InvalidArgumentError) as exc:
        gov.adjudicate(p, **M, payload={"x": float("inf")}, resource_id="budget", amount=10.0)
    d = exc.value.to_dict()
    assert d["error"] == "INVALID_ARGUMENT" and d["operation_state"] == "not_registered"
    assert "composed_decision" not in d and "HARD_VETO" not in str(d)
    assert str(exc.value).startswith("INVALID_ARGUMENT (payload): ")


def test_a_governance_veto_is_still_a_decision_not_an_error():
    """The distinction R8 asks for: a well-formed request the kernels refuse
    is a HARD_VETO decision with a registered, FAILED operation."""
    gov, budget = _governor()
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    r = gov.adjudicate(p, payload={"action_type": "payment"},
                       **{**M, "pressure": 1.0, "velocity": 1.0, "capacity": 0.1},  # TAU breach
                       resource_id="budget", amount=10.0)
    assert r["composed_decision"] == Decision.HARD_VETO.value
    assert r["operation_state"] == OperationState.FAILED.value
    assert gov.registry.get(p.idempotency_key) is not None


def test_an_unknown_measure_is_a_hold_not_an_error():
    gov, _budget = _governor()
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    r = gov.adjudicate(p, payload={"action_type": "payment"}, **{**M, "authority": None},
                       resource_id="budget", amount=10.0)
    assert r["composed_decision"] == Decision.HOLD.value


def test_profile_s_gate_refuses_a_missing_digest_as_invalid_argument():
    gov, _budget = _governor()
    gov.require_commit_authorization = True
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    with pytest.raises(InvalidArgumentError) as exc:
        gov.adjudicate(p, **M, payload={"action_type": "payment"})
    assert exc.value.field == "payload_digest"
    assert gov.registry.get(p.idempotency_key) is None


def test_resolve_hold_with_a_malformed_amount_raises_and_keeps_the_hold():
    gov, budget = _governor()
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    r = gov.adjudicate(p, payload={"a": 1}, **{**M, "authority": None},
                       resource_id="budget", amount=10.0)
    assert r["composed_decision"] == Decision.HOLD.value
    with pytest.raises(InvalidArgumentError) as exc:
        gov.resolve_hold(p.idempotency_key, passage=p, payload={"a": 1}, **M,
                         resource_id="budget", amount="ten")
    assert exc.value.field == "amount"
    assert gov.registry.get(p.idempotency_key).state == OperationState.HOLD
    assert budget.reserved == 0.0
