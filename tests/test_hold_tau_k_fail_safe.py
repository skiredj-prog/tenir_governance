# Profile: agentic-governance-profile-v1.0.0
# Compliance class: profile-bound
"""τ_K as a policy-declared fail-safe bound (issue #140, RN-001 §2a).

A bound the versioned policy declares fail-safe refuses alone when it is
exhausted, wherever a held operation could advance: re-adjudication
(resolve_hold), authorized exit (authorize_exit approve=True) and dispatch.
No timer is required. Without a declared bound nothing changes from 5.7.0:
the governor invents none.

Time is moved by replacing the governor's clock (``_utc_now_epoch``), never
by editing a HoldRecord, so the HOLD is the one ``adjudicate`` created.
"""
from datetime import datetime, timezone

import pytest

from tenir_governance.composition import DualKernelGovernor, ScoringKernelAdapter
from tenir_governance.composition import governor as governor_mod
from tenir_governance.composition.decisions import Decision
from tenir_governance.composition.policy import CompositionPolicy
from tenir_governance.membrane import MembraneState, TransitionMembrane
from tenir_governance.objects import Agent, Passage, Resource
from tenir_governance.operations import MockProvider, OperationState
from tenir_kernel.kernel import TenirKernel

# 5.7.6 (RFC-0 §4): a FLAG admits only under conditions the policy declares, and
# their acknowledgement is required at dispatch. The test deployment declares one.
TEST_FLAG_CONDITIONS = ("test supervision",)
TEST_FLAG_ACK = {"test supervision": "acknowledged by the test"}
TEST_POLICY_KW = dict(flag_conditions=TEST_FLAG_CONDITIONS)


TAU = 60.0
GOOD = dict(pressure=0.2, velocity=0.2, capacity=0.9, authority=0.9,
            context_fit=1.0, evidence_quality=1.0, option_space=0.9)


class _BrokenScoring:
    """Scoring kernel that raises: fail-closed HOLD at adjudication."""

    def adjudicate(self, **kwargs):
        raise RuntimeError("forced HOLD")


def _governor(policy=None):
    state = MembraneState()
    state.register_resource(Resource(resource_id="budget", capacity=100.0))
    state.register_agent(Agent(agent_id="alice", authority_score=0.9))
    gov = DualKernelGovernor(ScoringKernelAdapter(TenirKernel()), TransitionMembrane(state),
                             policy=policy or CompositionPolicy(**TEST_POLICY_KW), provider=MockProvider())
    return gov, state.resources["budget"]


def _policy(tau=TAU):
    return CompositionPolicy(hold_tau_k_seconds=tau, hold_tau_k_source="test SLA", **TEST_POLICY_KW)


def _held(gov):
    """A HOLD created by Governor.adjudicate, with 10.0 reserved."""
    gov.scoring = _BrokenScoring()
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    adj = gov.adjudicate(p, payload={"action_type": "payment"},
                         pressure=0.2, velocity=0.2, capacity=0.9,
                         resource_id="budget", amount=10.0)
    assert adj["composed_decision"] == Decision.HOLD.value
    gov.scoring = ScoringKernelAdapter(TenirKernel())
    return p


def _resolve(gov, p):
    return gov.resolve_hold(p.idempotency_key, passage=p, payload={"action_type": "payment"},
                            resource_id="budget", amount=10.0, **GOOD)


@pytest.fixture
def clock(monkeypatch):
    """Advance the governor's clock by `offset` seconds."""
    state = {"offset": 0.0}
    real = governor_mod._utc_now_epoch
    monkeypatch.setattr(governor_mod, "_utc_now_epoch", lambda: real() + state["offset"])
    monkeypatch.setattr(
        governor_mod, "_utc_now_iso",
        lambda: datetime.fromtimestamp(real() + state["offset"], timezone.utc).isoformat(),
    )

    def advance(seconds):
        state["offset"] += seconds
    return advance


# -- policy contract -------------------------------------------------------

def test_policy_declares_no_tau_k_by_default():
    assert CompositionPolicy().hold_tau_k_seconds is None


@pytest.mark.parametrize("tau", [0, -1, float("nan"), float("inf"), True, "60"])
def test_policy_rejects_invalid_tau_k(tau):
    with pytest.raises(ValueError):
        CompositionPolicy(hold_tau_k_seconds=tau, hold_tau_k_source="x").validate()


def test_policy_requires_a_source_with_tau_k():
    with pytest.raises(ValueError):
        CompositionPolicy(hold_tau_k_seconds=60.0).validate()


# -- C2a: the bound reaches the HOLD only when declared --------------------

def test_without_declared_bound_hold_carries_none():
    gov, _ = _governor()
    p = _held(gov)
    hold = gov.registry.get(p.idempotency_key).hold_record
    assert hold.tau_k_seconds == 0.0


def test_declared_bound_reaches_governor_created_hold():
    gov, _ = _governor(_policy())
    p = _held(gov)
    hold = gov.registry.get(p.idempotency_key).hold_record
    assert hold.tau_k_seconds == TAU
    assert hold.tau_k_source == "test SLA"


# -- controls: inside the bound nothing changes ----------------------------

def test_resolution_inside_bound_dispatches_once(clock):
    gov, budget = _governor(_policy())
    p = _held(gov)
    clock(TAU / 2)
    res = _resolve(gov, p)
    assert res["hold_resolved"] is True
    clock(TAU)  # dispatch after the bound: the HOLD was resolved in time
    out = gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0,
                                   payload={"action_type": "payment"}, flag_conditions_met=TEST_FLAG_ACK)
    assert out["dispatched"] is True
    assert budget.consumed == 10.0
    assert len(gov.provider.calls) == 1


def test_no_declared_bound_late_resolution_unchanged(clock):
    """5.7.0 behaviour is kept when the policy declares nothing."""
    gov, budget = _governor()
    p = _held(gov)
    clock(10 * TAU)
    assert _resolve(gov, p)["hold_resolved"] is True
    out = gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0,
                                   payload={"action_type": "payment"}, flag_conditions_met=TEST_FLAG_ACK)
    assert out["dispatched"] is True


# -- C2b: late re-adjudication ---------------------------------------------

def test_late_resolution_fails_closed_and_releases_budget(clock):
    gov, budget = _governor(_policy())
    p = _held(gov)
    assert budget.reserved == 10.0
    clock(TAU + 1)
    res = _resolve(gov, p)
    assert res["hold_resolved"] is False
    assert res["tau_k_expired"] is True
    assert res["composed_decision"] == Decision.HARD_VETO.value
    assert res["operation_state"] == OperationState.FAILED.value
    assert res["execution_record"] == "CONFIRMED_NONEXECUTION"
    assert budget.reserved == 0.0
    out = gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0,
                                   payload={"action_type": "payment"}, flag_conditions_met=TEST_FLAG_ACK)
    assert out["dispatched"] is False
    assert budget.consumed == 0.0
    assert gov.provider.calls == []


def test_late_resolution_is_ledgered_as_hard_veto(clock):
    gov, _ = _governor(_policy())
    p = _held(gov)
    clock(TAU + 1)
    _resolve(gov, p)
    last = [e for e in gov.ledger.entries if e.idempotency_key == p.idempotency_key][-1]
    assert last.kernel == "hold_lifecycle"
    assert last.decision == Decision.HARD_VETO.value
    assert "τ_K exhausted" in last.rationale and "resolve_hold" in last.rationale
    assert "CONFIRMED_NONEXECUTION" in last.rationale
    assert gov.ledger.verify_chain()


def test_bound_is_exhausted_exactly_at_deadline(clock):
    gov, _ = _governor(_policy())
    p = _held(gov)
    clock(TAU + 0.001)
    assert _resolve(gov, p)["tau_k_expired"] is True


# -- C2c: late authorized exit ---------------------------------------------

def test_late_authorized_exit_fails_closed(clock):
    gov, budget = _governor(_policy())
    p = _held(gov)
    assert gov.escalate_hold(p.idempotency_key, "ops")["escalated"] is True
    clock(TAU + 1)
    out = gov.authorize_exit(p.idempotency_key, "risk-officer", "payment",
                             "ceremony-G-R5-001", approve=True)
    assert out["exited"] is False
    assert out["tau_k_expired"] is True
    assert out["operation_state"] == OperationState.FAILED.value
    assert budget.reserved == 0.0
    assert _resolve(gov, p)["hold_resolved"] is False
    assert gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0,
                                    payload={"action_type": "payment"}, flag_conditions_met=TEST_FLAG_ACK)["dispatched"] is False
    assert budget.consumed == 0.0
    assert gov.provider.calls == []


def test_exit_inside_bound_then_late_resolution_fails_closed(clock):
    """The bound counts from HOLD entry; an exit does not restart it."""
    gov, budget = _governor(_policy())
    p = _held(gov)
    gov.escalate_hold(p.idempotency_key, "ops")
    clock(TAU / 2)
    out = gov.authorize_exit(p.idempotency_key, "risk-officer", "payment",
                             "ceremony-G-R5-001", approve=True)
    assert out["exited"] is True
    clock(TAU)
    res = _resolve(gov, p)
    assert res["tau_k_expired"] is True
    assert res["operation_state"] == OperationState.FAILED.value
    assert budget.reserved == 0.0


def test_late_denial_still_denies(clock):
    gov, budget = _governor(_policy())
    p = _held(gov)
    gov.escalate_hold(p.idempotency_key, "ops")
    clock(TAU + 1)
    out = gov.authorize_exit(p.idempotency_key, "risk-officer", "payment",
                             "ceremony-G-R5-001", approve=False)
    assert out["operation_state"] == OperationState.FAILED.value
    assert budget.reserved == 0.0


# -- dispatch as an advance point ------------------------------------------

def test_dispatch_of_unanswered_hold_past_bound_fails_closed(clock):
    gov, budget = _governor(_policy())
    p = _held(gov)
    clock(TAU + 1)
    out = gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0,
                                   payload={"action_type": "payment"}, flag_conditions_met=TEST_FLAG_ACK)
    assert out["dispatched"] is False
    assert out["tau_k_expired"] is True
    assert out["composed_decision"] == Decision.HARD_VETO.value
    assert gov.registry.get(p.idempotency_key).state == OperationState.FAILED
    assert budget.reserved == 0.0 and budget.consumed == 0.0
    assert gov.provider.calls == []


def test_dispatch_of_unanswered_hold_inside_bound_is_refused_and_stays_held(clock):
    gov, budget = _governor(_policy())
    p = _held(gov)
    clock(TAU / 2)
    out = gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0,
                                   payload={"action_type": "payment"}, flag_conditions_met=TEST_FLAG_ACK)
    assert out["dispatched"] is False
    assert not out.get("tau_k_expired")
    assert gov.registry.get(p.idempotency_key).state == OperationState.HOLD
    assert budget.reserved == 10.0


def test_dispatch_refuses_reserved_operation_resolved_after_bound(clock):
    """Defence in depth: a RESERVED record whose resolution time is past the
    bound (or unrecorded, e.g. resolved by an earlier version) fails closed."""
    gov, budget = _governor(_policy())
    p = _held(gov)
    assert _resolve(gov, p)["hold_resolved"] is True
    record = gov.registry.get(p.idempotency_key)
    record.hold_record.resolved_at = ""
    clock(TAU + 1)
    out = gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0,
                                   payload={"action_type": "payment"}, flag_conditions_met=TEST_FLAG_ACK)
    assert out["dispatched"] is False
    assert out["tau_k_expired"] is True
    assert budget.reserved == 0.0 and budget.consumed == 0.0


def test_persistent_registry_keeps_resolution_time(tmp_path, clock):
    from tenir_governance.operations import DurableOperationRegistry

    state = MembraneState()
    state.register_resource(Resource(resource_id="budget", capacity=100.0))
    state.register_agent(Agent(agent_id="alice", authority_score=0.9))
    store = str(tmp_path / "ops.json")
    gov = DualKernelGovernor(ScoringKernelAdapter(TenirKernel()), TransitionMembrane(state),
                             policy=_policy(), provider=MockProvider(),
                             registry=DurableOperationRegistry(store))
    p = _held(gov)
    clock(TAU / 2)
    assert _resolve(gov, p)["hold_resolved"] is True
    reloaded = DurableOperationRegistry(store).get(p.idempotency_key)
    assert reloaded.hold_record.resolved_at
    assert reloaded.hold_record.tau_k_seconds == TAU


# -- review of #142: escalation, structured record, release failure, order ---

def test_escalation_past_bound_fails_closed(clock):
    gov, budget = _governor(_policy())
    p = _held(gov)
    clock(TAU + 1)
    out = gov.escalate_hold(p.idempotency_key, "ops")
    assert out["escalated"] is False
    assert out["tau_k_expired"] is True
    assert out["operation_state"] == OperationState.FAILED.value
    assert budget.reserved == 0.0


def test_escalation_inside_bound_is_unchanged(clock):
    gov, budget = _governor(_policy())
    p = _held(gov)
    clock(TAU / 2)
    assert gov.escalate_hold(p.idempotency_key, "ops")["escalated"] is True
    assert budget.reserved == 10.0


def test_exhaustion_is_recorded_in_structured_fields(clock):
    gov, _ = _governor(_policy())
    p = _held(gov)
    clock(TAU + 1)
    _resolve(gov, p)
    hold = gov.registry.get(p.idempotency_key).hold_record
    assert hold.exit_reason == "expired"
    assert hold.exhaustion_disposition == "fail-closed"
    assert hold.exhausted_at == "resolve_hold"
    assert hold.execution_record == "CONFIRMED_NONEXECUTION"
    assert hold.tau_k_seconds == TAU and hold.tau_k_source == "test SLA"
    last = [e for e in gov.ledger.entries if e.idempotency_key == p.idempotency_key][-1]
    assert last.composed_decision == Decision.HARD_VETO.value


def test_structured_record_survives_persistence(tmp_path, clock):
    from tenir_governance.operations import DurableOperationRegistry

    state = MembraneState()
    state.register_resource(Resource(resource_id="budget", capacity=100.0))
    state.register_agent(Agent(agent_id="alice", authority_score=0.9))
    store = str(tmp_path / "ops.json")
    gov = DualKernelGovernor(ScoringKernelAdapter(TenirKernel()), TransitionMembrane(state),
                             policy=_policy(), provider=MockProvider(),
                             registry=DurableOperationRegistry(store))
    p = _held(gov)
    clock(TAU + 1)
    _resolve(gov, p)
    hold = DurableOperationRegistry(store).get(p.idempotency_key).hold_record
    assert hold.execution_record == "CONFIRMED_NONEXECUTION"
    assert hold.exhausted_at == "resolve_hold"


def test_release_failure_is_ledgered_and_leaves_attempt_open(clock, monkeypatch):
    gov, budget = _governor(_policy())
    p = _held(gov)
    clock(TAU + 1)
    monkeypatch.setattr(gov, "_release_hold_budget_before_terminal_transition",
                        lambda key, record: "simulated release failure")
    res = _resolve(gov, p)
    assert res["tau_k_expired"] is True
    assert res["budget_reconciliation_required"] is True
    assert res["operation_state"] == OperationState.HOLD.value
    last = [e for e in gov.ledger.entries if e.idempotency_key == p.idempotency_key][-1]
    assert last.decision == Decision.HARD_VETO.value
    assert "reconciliation required" in last.rationale


def test_late_resolution_with_wrong_payload_still_closes_the_attempt(clock):
    gov, budget = _governor(_policy())
    p = _held(gov)
    clock(TAU + 1)
    res = gov.resolve_hold(p.idempotency_key, passage=p, payload={"action_type": "other"},
                           resource_id="budget", amount=10.0, **GOOD)
    assert res["tau_k_expired"] is True
    assert res["operation_state"] == OperationState.FAILED.value
    assert budget.reserved == 0.0


def test_explicit_expire_hold_records_confirmed_nonexecution(clock, monkeypatch):
    """Decision B3: an expired HOLD was never dispatched. expire_hold records
    the disposition and CONFIRMED_NONEXECUTION, like the advance points."""
    from tenir_governance.operations import hold as hold_mod
    gov, budget = _governor(_policy())
    p = _held(gov)
    real = hold_mod._utc_now_seconds
    monkeypatch.setattr(hold_mod, "_utc_now_seconds", lambda: real() + TAU + 1)
    clock(TAU + 1)
    result = gov.expire_hold(p.idempotency_key)
    assert result["expired"] is True
    assert result["execution_record"] == "CONFIRMED_NONEXECUTION"
    hold = gov.registry.get(p.idempotency_key).hold_record
    assert gov.registry.get(p.idempotency_key).state == OperationState.FAILED
    assert hold.exit_reason == "expired"
    assert hold.exhaustion_disposition == "fail-closed"
    assert hold.execution_record == "CONFIRMED_NONEXECUTION"
    assert budget.reserved == 0.0
