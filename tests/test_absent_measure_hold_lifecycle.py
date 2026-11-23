"""Rule A (candidate 5.7.7, not released): the HOLD an absent measure gives
keeps every guarantee of a HOLD.

Author's decision of 23 November 2026, pending ratification of the amended
RFC-2 v0.2: an absent mandatory measure (P, V, C, authority, context_fit,
evidence_quality, or an explicitly absent irreversibility_materiality) gives
HOLD unless a conclusive veto governs. That HOLD must keep its guarantees: no
execution, no automatic approval, a complete re-adjudication before any
resumption, and an explicit treatment of the τ_K bound. Checked here, on the
in-memory registry and budget of one process:

- no dispatch and no provider call while the measure is missing;
- nothing is reserved while the measure is missing (the caller who withholds
  a measure cannot lock a shared budget); the reservation is taken once, at
  the re-adjudication that completes the evidence, under I8 at that moment,
  and consumed only by the single dispatch after resolution;
- a re-adjudication with the measure still missing keeps the HOLD;
- a duplicate idempotency key neither reserves twice nor admits;
- an authorized exit from ESCALATED returns to HOLD, never to admission;
- a membrane veto (revoked delegation) governs whatever the absent measure;
- a structurally malformed request is rejected at entry, never held.
"""
from datetime import datetime, timezone
import json

import pytest

from tenir_governance.composition import DualKernelGovernor, InvalidArgumentError, ScoringKernelAdapter
from tenir_governance.composition import governor as governor_mod
from tenir_governance.composition.decisions import Decision
from tenir_governance.composition.policy import CompositionPolicy
from tenir_governance.membrane import MembraneState, TransitionMembrane
from tenir_governance.objects import Agent, Delegation, Passage, Resource
from tenir_governance.operations import MockProvider, OperationState
from tenir_governance.operations import hold as hold_mod
from tenir_governance.sdk import DualKernelCompositionClient
from tenir_kernel.kernel import TenirKernel

CONDITIONS = ("ops supervision",)
ACK = {"ops supervision": "acknowledged by the test"}
M = dict(pressure=0.2, velocity=0.2, capacity=0.9, option_space=0.9,
         authority=0.9, context_fit=0.9, evidence_quality=0.9)
MANDATORY = ("pressure", "velocity", "capacity", "authority", "context_fit",
             "evidence_quality", "irreversibility_materiality")
PAYLOAD = {"action_type": "payment"}
TAU = 60.0


def _governor(policy=None):
    state = MembraneState()
    state.register_resource(Resource(resource_id="budget", capacity=100.0))
    state.register_agent(Agent(agent_id="alice", authority_score=0.9,
                               trusted_delegates=frozenset({"bob"})))
    state.register_agent(Agent(agent_id="bob", authority_score=0.8))
    gov = DualKernelGovernor(ScoringKernelAdapter(TenirKernel()), TransitionMembrane(state),
                             policy=policy or CompositionPolicy(flag_conditions=CONDITIONS),
                             provider=MockProvider())
    return gov, state, state.resources["budget"]


def _held(gov, missing="authority", amount=10.0, **kw):
    p = kw.pop("passage", None) or Passage(agent_id="alice", action_type="payment", amount=amount)
    r = gov.adjudicate(p, payload=PAYLOAD, **{**M, missing: None},
                       resource_id="budget", amount=amount, **kw)
    assert r["scoring_decision"] == Decision.HOLD.value, r
    assert r["composed_decision"] == Decision.HOLD.value, r
    assert r["operation_state"] == OperationState.HOLD.value
    assert r["budget_reserved"] is False  # rule A: an evidence HOLD reserves nothing
    return p


def _resolve(gov, p, **measures):
    return gov.resolve_hold(p.idempotency_key, passage=p, payload=PAYLOAD,
                            resource_id="budget", amount=10.0, **{**M, **measures})


def _dispatch(gov, p):
    return gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0, payload=PAYLOAD,
                                    flag_conditions_met=ACK)


@pytest.fixture
def clock(monkeypatch):
    """Advance the governor's and the HOLD manager's clocks by `offset` seconds
    (no HoldRecord edit: the HOLD is the one adjudicate created)."""
    state = {"offset": 0.0}
    real = governor_mod._utc_now_epoch
    monkeypatch.setattr(governor_mod, "_utc_now_epoch", lambda: real() + state["offset"])
    real_hold = hold_mod._utc_now_seconds
    monkeypatch.setattr(hold_mod, "_utc_now_seconds", lambda: real_hold() + state["offset"])
    monkeypatch.setattr(
        governor_mod, "_utc_now_iso",
        lambda: datetime.fromtimestamp(real() + state["offset"], timezone.utc).isoformat(),
    )

    def advance(seconds):
        state["offset"] += seconds
    return advance


@pytest.mark.parametrize("missing", MANDATORY)
def test_absent_measure_holds_reserves_nothing_and_never_dispatches(missing):
    gov, _state, budget = _governor()
    p = _held(gov, missing)
    assert budget.reserved == 0.0 and budget.consumed == 0.0
    assert gov.registry.get(p.idempotency_key).budget_reserved is False
    out = _dispatch(gov, p)
    assert out["dispatched"] is False
    assert gov.provider.calls == []
    assert gov.registry.get(p.idempotency_key).state == OperationState.HOLD
    assert budget.reserved == 0.0 and budget.consumed == 0.0


@pytest.mark.parametrize("missing", ["authority", "context_fit", "evidence_quality"])
def test_still_absent_at_re_adjudication_keeps_the_hold(missing):
    gov, _state, budget = _governor()
    p = _held(gov, missing)
    again = _resolve(gov, p, **{missing: None})
    assert again["hold_resolved"] is False
    assert again["composed_decision"] == Decision.HOLD.value
    assert again["operation_state"] == OperationState.HOLD.value
    assert again["budget_reserved"] is False and budget.reserved == 0.0  # still nothing reserved
    assert gov.provider.calls == []


def test_measure_arrives_then_one_dispatch_after_full_re_adjudication():
    gov, _state, budget = _governor()
    p = _held(gov, "authority")
    resolved = _resolve(gov, p)
    assert resolved["hold_resolved"] is True
    assert resolved["composed_decision"] == Decision.FLAG.value
    assert resolved["operation_state"] == OperationState.RESERVED.value
    # The reservation is taken once, here, when the evidence is complete.
    assert resolved["budget_reserved"] is True and budget.reserved == 10.0
    first = _dispatch(gov, p)
    assert first["dispatched"] is True
    assert len(gov.provider.calls) == 1
    assert budget.consumed == 10.0 and budget.reserved == 0.0
    second = _dispatch(gov, p)
    assert second["dispatched"] is False
    assert len(gov.provider.calls) == 1 and budget.consumed == 10.0


def test_measured_zero_authority_at_re_adjudication_vetoes_and_releases():
    """authority=0 is a measure: the policy's authority floor vetoes."""
    gov, _state, budget = _governor()
    p = _held(gov, "authority")
    vetoed = _resolve(gov, p, authority=0.0)
    assert vetoed["composed_decision"] == Decision.HARD_VETO.value
    assert vetoed["hold_resolved"] is True
    assert vetoed["operation_state"] == OperationState.FAILED.value
    assert budget.reserved == 0.0 and budget.consumed == 0.0
    assert gov.provider.calls == []


def test_duplicate_key_while_held_neither_reserves_twice_nor_admits():
    gov, _state, budget = _governor()
    p = _held(gov, "authority")
    dup = gov.adjudicate(p, payload=PAYLOAD, **M, resource_id="budget", amount=10.0)
    assert "Duplicate operation blocked" in dup["composed_rationale"]
    assert dup["operation_state"] == OperationState.HOLD.value
    assert gov.registry.get(p.idempotency_key).state == OperationState.HOLD
    assert budget.reserved == 0.0  # the duplicate reserved nothing either
    assert _dispatch(gov, p)["dispatched"] is False
    assert gov.provider.calls == []


def test_tau_k_exhausted_fails_closed_even_when_the_measure_arrives(clock):
    policy = CompositionPolicy(hold_tau_k_seconds=TAU, hold_tau_k_source="test SLA",
                               flag_conditions=CONDITIONS)
    gov, _state, budget = _governor(policy)
    p = _held(gov, "authority")
    assert gov.registry.get(p.idempotency_key).hold_record.tau_k_seconds == TAU
    clock(TAU + 1)
    late = _resolve(gov, p)
    assert late["hold_resolved"] is False
    assert late["tau_k_expired"] is True
    assert late["execution_record"] == "CONFIRMED_NONEXECUTION"
    assert late["operation_state"] == OperationState.FAILED.value
    assert budget.reserved == 0.0 and budget.consumed == 0.0
    assert _dispatch(gov, p)["dispatched"] is False
    assert gov.provider.calls == []


def test_expire_hold_at_tau_k_fails_closed_with_nothing_to_release(clock):
    policy = CompositionPolicy(hold_tau_k_seconds=TAU, hold_tau_k_source="test SLA",
                               flag_conditions=CONDITIONS)
    gov, _state, budget = _governor(policy)
    p = _held(gov, "evidence_quality")
    assert gov.expire_hold(p.idempotency_key)["expired"] is False  # inside the bound
    clock(TAU + 1)
    assert gov.expire_hold(p.idempotency_key)["expired"] is True
    assert gov.registry.get(p.idempotency_key).state == OperationState.FAILED
    assert budget.reserved == 0.0 and budget.consumed == 0.0


def test_without_a_declared_tau_k_the_hold_stays_open_but_holds_no_budget(clock):
    """Declared limit, not a guarantee: the generic governor invents no bound
    (RN-001 §2a). Without a policy τ_K, a HOLD on an absent measure stays open
    until resolved, escalated or exited. It holds no reservation (rule A), so
    the open HOLD is an exposure on the registry, not on the budget. 5.7.6
    vetoed these requests at once."""
    gov, _state, budget = _governor()
    p = _held(gov, "authority")
    clock(30 * 24 * 3600)
    assert gov.check_hold_expiry(p.idempotency_key)["expired"] is False
    assert gov.registry.get(p.idempotency_key).state == OperationState.HOLD
    assert budget.reserved == 0.0


def test_incomplete_requests_cannot_lock_the_budget():
    """R1 of the review of 2026-11-23: ten requests of 10.0 without authority
    on a budget of 100.0, no τ_K declared. On the first 5.7.7 candidate they
    reserved the whole budget and a complete request was then vetoed by I8.
    Now they reserve nothing and the complete request is admitted."""
    gov, _state, budget = _governor()
    held = [_held(gov, "authority", passage=Passage(agent_id="alice", action_type="payment",
                                                     amount=10.0, idempotency_key=f"incomplete-{i}"))
            for i in range(10)]
    assert len(held) == 10 and budget.reserved == 0.0
    complete = Passage(agent_id="alice", action_type="payment", amount=10.0)
    r = gov.adjudicate(complete, payload=PAYLOAD, **M, resource_id="budget", amount=10.0)
    assert r["membrane_decision"] == Decision.PASS.value, r["membrane_rationale"]
    assert r["composed_decision"] == Decision.FLAG.value
    assert r["operation_state"] == OperationState.RESERVED.value
    assert r["budget_reserved"] is True and budget.reserved == 10.0
    for p in held:
        assert gov.registry.get(p.idempotency_key).state == OperationState.HOLD


def test_budget_consumed_while_held_fails_closed_at_re_adjudication():
    """What rule A costs the held request: it has no reservation, so a later
    complete request may take the budget. When its measure arrives, I8 bounds
    it by what remains: HARD_VETO, FAILED, nothing dispatched."""
    gov, _state, budget = _governor()
    p = _held(gov, "authority", passage=Passage(agent_id="alice", action_type="payment", amount=60.0),
              amount=60.0)
    other = Passage(agent_id="alice", action_type="payment", amount=60.0)
    r = gov.adjudicate(other, payload=PAYLOAD, **M, resource_id="budget", amount=60.0)
    assert r["composed_decision"] == Decision.FLAG.value and budget.reserved == 60.0
    late = gov.resolve_hold(p.idempotency_key, passage=p, payload=PAYLOAD,
                            resource_id="budget", amount=60.0, **M)
    assert late["membrane_decision"] == Decision.HARD_VETO.value
    assert "I8" in late["membrane_rationale"] or any(
        c.get("check_id") == "I8" for c in late["membrane_checks"])
    assert late["composed_decision"] == Decision.HARD_VETO.value
    assert late["hold_resolved"] is True
    assert late["operation_state"] == OperationState.FAILED.value
    assert budget.reserved == 60.0 and budget.consumed == 0.0  # the other's, untouched
    assert gov.provider.calls == []


def test_re_adjudication_to_a_policy_hold_on_complete_evidence_reserves():
    """Once the evidence is complete, a HOLD the policy keeps (budget
    escalation at 85 % utilization) is a HOLD on complete evidence: it takes
    its reservation, as every such HOLD did in 5.7.6."""
    gov, _state, budget = _governor()
    p = _held(gov, "authority", passage=Passage(agent_id="alice", action_type="payment", amount=90.0),
              amount=90.0)
    assert budget.reserved == 0.0
    res = gov.resolve_hold(p.idempotency_key, passage=p, payload=PAYLOAD,
                           resource_id="budget", amount=90.0, **M)
    assert res["membrane_decision"] == Decision.PASS.value
    assert res["composed_decision"] == Decision.HOLD.value and res["hold_resolved"] is False
    assert "escalated to HOLD" in res["composed_rationale"]
    assert res["budget_reserved"] is True and budget.reserved == 90.0
    assert gov.registry.get(p.idempotency_key).state == OperationState.HOLD


def test_unmeasured_option_space_and_invalid_measure_hold_reserve_nothing():
    """The rule covers every HOLD for insufficient evidence, not only an
    absent mandatory measure: an unmeasured option space (5.7.6, decision 2)
    and an invalid measure (NaN) reserve nothing either."""
    gov, _state, budget = _governor()
    p1 = Passage(agent_id="alice", action_type="payment", amount=10.0)
    r1 = gov.adjudicate(p1, payload=PAYLOAD, **{**M, "option_space": None},
                        resource_id="budget", amount=10.0)
    assert r1["composed_decision"] == Decision.HOLD.value and r1["budget_reserved"] is False
    p2 = Passage(agent_id="alice", action_type="payment", amount=10.0)
    r2 = gov.adjudicate(p2, payload=PAYLOAD, **{**M, "evidence_quality": float("nan")},
                        resource_id="budget", amount=10.0)
    assert r2["composed_decision"] == Decision.HOLD.value and r2["budget_reserved"] is False
    assert budget.reserved == 0.0


def test_a_hold_on_complete_evidence_still_reserves():
    """Unchanged from 5.7.6: a HOLD on complete evidence (here a FLAG under a
    policy that declares no condition) keeps its reservation. Only the HOLD
    that waits for the caller's own measure reserves nothing."""
    gov, _state, budget = _governor(CompositionPolicy())
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    r = gov.adjudicate(p, payload=PAYLOAD, **M, resource_id="budget", amount=10.0)
    assert r["scoring_decision"] == Decision.FLAG.value
    assert r["composed_decision"] == Decision.HOLD.value
    assert r["budget_reserved"] is True and budget.reserved == 10.0


def test_authorized_exit_from_escalation_returns_to_hold_not_to_admission():
    gov, _state, budget = _governor()
    p = _held(gov, "authority")
    assert gov.escalate_hold(p.idempotency_key, escalated_by="risk-officer")["escalated"]
    exited = gov.authorize_exit(p.idempotency_key, authorized_by="ciso", scope="payment:10",
                                authorization="ceremony-G-R5-001", reason="reviewed",
                                approve=True)
    assert exited["exited"] is True
    assert exited["operation_state"] == OperationState.HOLD.value
    assert _dispatch(gov, p)["dispatched"] is False
    assert _resolve(gov, p, authority=None)["operation_state"] == OperationState.HOLD.value
    assert _resolve(gov, p)["operation_state"] == OperationState.RESERVED.value
    assert gov.provider.calls == [] and budget.reserved == 10.0


def test_revoked_delegation_vetoes_whatever_the_absent_measure():
    gov, state, budget = _governor()
    state.register_agent(Agent(agent_id="alice", authority_score=0.9,
                               trusted_delegates=frozenset()))
    p = Passage(agent_id="alice", action_type="delegation", amount=10.0, recipient_id="bob")
    r = gov.adjudicate(p, payload=PAYLOAD, **{**M, "authority": None},
                       delegation=Delegation(delegating_agent_id="alice", delegatee_agent_id="bob"),
                       resource_id="budget", amount=10.0)
    assert r["scoring_decision"] == Decision.HOLD.value
    assert r["membrane_decision"] == Decision.HARD_VETO.value
    assert r["composed_decision"] == Decision.HARD_VETO.value
    assert r["operation_state"] == OperationState.FAILED.value
    assert budget.reserved == 0.0


def test_delegation_revoked_while_held_vetoes_at_re_adjudication():
    gov, state, budget = _governor()
    delegation = Delegation(delegating_agent_id="alice", delegatee_agent_id="bob")
    p = Passage(agent_id="alice", action_type="delegation", amount=10.0, recipient_id="bob")
    _held(gov, "authority", passage=p, delegation=delegation)
    state.register_agent(Agent(agent_id="alice", authority_score=0.9,
                               trusted_delegates=frozenset()))
    r = gov.resolve_hold(p.idempotency_key, passage=p, payload=PAYLOAD, resource_id="budget",
                         amount=10.0, delegation=delegation, **M)
    assert r["composed_decision"] == Decision.HARD_VETO.value
    assert r["operation_state"] == OperationState.FAILED.value
    assert budget.reserved == 0.0 and gov.provider.calls == []


@pytest.mark.parametrize("malformed", [
    dict(payload={"note": float("nan")}),
    dict(payload=["not", "an", "object"]),
    dict(amount=float("nan")),
])
def test_malformed_request_is_rejected_at_entry_not_held(malformed):
    """A request that is not a well-formed request of the interface is
    rejected before any claim: nothing registered, nothing reserved, no HOLD.
    R8 (2026-11-23): the refusal is an API-level INVALID_ARGUMENT error, not
    a governance decision, so it cannot be confused with an unknown measure
    (HOLD) or with a veto on the action (HARD_VETO)."""
    gov, _state, budget = _governor()
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    kw = dict(payload=PAYLOAD, resource_id="budget", amount=10.0)
    kw.update(malformed)
    with pytest.raises(InvalidArgumentError) as exc:
        gov.adjudicate(p, **{**M, "authority": None}, **kw)
    assert exc.value.code == "INVALID_ARGUMENT"
    assert exc.value.to_dict()["operation_state"] == "not_registered"
    assert gov.registry.get(p.idempotency_key) is None
    assert budget.reserved == 0.0


def test_json_null_authority_on_the_sdk_path_holds():
    """Decision 6: null on the wire is an absent measure, not a zero."""
    client = DualKernelCompositionClient(policy=CompositionPolicy(flag_conditions=CONDITIONS))
    client.register_resource(Resource(resource_id="budget", capacity=100.0))
    measured = json.loads(json.dumps({**M, "authority": None}))
    r = client.adjudicate(Passage(agent_id="a", action_type="x"), **measured)
    assert r["composed_decision"] == Decision.HOLD.value
    assert "absent measure(s) authority" in str(r.get("scoring_rationale"))
