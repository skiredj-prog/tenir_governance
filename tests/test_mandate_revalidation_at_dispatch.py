"""5.7.6: the mandate that admitted an operation is re-validated before the
DISPATCHING claim (audit reserve P1 "revalidation avant dispatch", 20 Nov).

On the default policy (`revalidate_mandate_at_dispatch=True`) a change refuses
fail-closed: the reservation is released, the attempt ends FAILED with
`execution_record = CONFIRMED_NONEXECUTION`, the provider is never called and
the refusal is ledgered as HARD_VETO (MANDATE_REVALIDATION_FAILED). Checked:
the acting agent, the delegation in scope, the in-memory budget limit, and the
composition policy version. `False` restores the 5.7.5 behaviour.
"""
from decimal import Decimal

import pytest

from tenir_governance.composition import DualKernelGovernor, ScoringKernelAdapter
from tenir_governance.composition.decisions import Decision
from tenir_governance.composition.policy import CompositionPolicy
from tenir_governance.membrane import MembraneState, TransitionMembrane
from tenir_governance.objects import Agent, Delegation, Passage, Resource
from tenir_governance.operations import MockProvider, OperationState
from tenir_governance.operations.consensus_backend import SingleHostLockBackend
from tenir_governance.operations.registry import DurableOperationRegistry
from tenir_kernel.kernel import TenirKernel

CONDITIONS = ("ops supervision",)
ACK = {"ops supervision": "ops rota 2026-11-20"}
M = dict(pressure=0.2, velocity=0.3, capacity=1.0, authority=0.9, context_fit=0.9,
         evidence_quality=0.9, option_space=0.9)


def _gov(policy=None, *, registry=None, provider=None):
    state = MembraneState()
    state.register_resource(Resource(resource_id="budget", capacity=100.0))
    state.register_agent(Agent(agent_id="alice", authority_score=0.9, trusted_delegates=frozenset({"bob"})))
    state.register_agent(Agent(agent_id="bob", authority_score=0.8))
    gov = DualKernelGovernor(ScoringKernelAdapter(TenirKernel()), TransitionMembrane(state),
                             policy=policy or CompositionPolicy(flag_conditions=CONDITIONS),
                             registry=registry, provider=provider or MockProvider())
    return gov, state


def _admit(gov, amount=10.0, **kw):
    p = Passage(agent_id=kw.pop("agent_id", "alice"), action_type=kw.pop("action_type", "payment"), amount=amount,
                recipient_id=kw.pop("recipient_id", None))
    r = gov.adjudicate(p, **M, resource_id="budget", amount=amount, **kw)
    assert r["operation_state"] == OperationState.RESERVED.value, r
    return p


def _assert_fail_closed(gov, state, key, change_fragment):
    out = gov.dispatch_and_execute(key, "budget", 10.0, flag_conditions_met=ACK)
    assert out["dispatched"] is False
    assert out["mandate_revalidation_failed"] is True
    assert any(change_fragment in c for c in out["mandate_changes"]), out["mandate_changes"]
    assert out["execution_record"] == "CONFIRMED_NONEXECUTION"
    assert out["composed_decision"] == Decision.HARD_VETO.value
    assert out["operation_state"] == OperationState.FAILED.value
    record = gov.registry.get(key)
    assert record.state == OperationState.FAILED and record.budget_reserved is False
    assert state.resources["budget"].reserved == 0.0 and state.resources["budget"].consumed == 0.0
    assert gov.provider.calls == []
    vetoes = [e for e in gov.ledger.entries
              if e.idempotency_key == key and "MANDATE_REVALIDATION_FAILED at dispatch" in e.rationale]
    assert len(vetoes) == 1 and vetoes[0].decision == Decision.HARD_VETO.value
    assert "CONFIRMED_NONEXECUTION" in vetoes[0].rationale
    # Terminal for this attempt identity: a second dispatch finds nothing to claim.
    again = gov.dispatch(key, "budget", 10.0, flag_conditions_met=ACK)
    assert again["dispatched"] is False and again["operation_state"] == OperationState.FAILED.value


def test_intact_mandate_dispatches():
    gov, state = _gov()
    p = _admit(gov)
    record = gov.registry.get(p.idempotency_key)
    assert record.agent_registered_at_admission is True
    assert record.policy_version == gov.policy.version
    out = gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0, flag_conditions_met=ACK)
    assert out["dispatched"] is True and out["operation_state"] == OperationState.CONFIRMED.value
    assert state.resources["budget"].consumed == 10.0


def test_removed_acting_agent_refuses_fail_closed():
    gov, state = _gov()
    p = _admit(gov)
    del state.agents["alice"]
    _assert_fail_closed(gov, state, p.idempotency_key, "acting agent 'alice' is no longer registered")


def test_lowered_budget_limit_refuses_fail_closed():
    gov, state = _gov()
    p = _admit(gov, amount=60.0)
    state.resources["budget"].capacity = 50.0  # lowered under the reservation
    out = gov.dispatch_and_execute(p.idempotency_key, "budget", 60.0, flag_conditions_met=ACK)
    assert out["dispatched"] is False and out["mandate_revalidation_failed"] is True
    assert any("no longer covers the amount" in c for c in out["mandate_changes"])
    assert out["execution_record"] == "CONFIRMED_NONEXECUTION"
    assert gov.registry.get(p.idempotency_key).state == OperationState.FAILED
    assert state.resources["budget"].reserved == 0.0 and state.resources["budget"].consumed == 0.0
    assert gov.provider.calls == []


def test_limit_lowered_but_still_covering_dispatches():
    gov, state = _gov()
    p = _admit(gov, amount=10.0)
    state.resources["budget"].capacity = 10.0  # exactly the reservation: still covered
    assert gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0, flag_conditions_met=ACK)["dispatched"]


def test_removed_resource_refuses_fail_closed_and_reports_reconciliation():
    gov, state = _gov()
    p = _admit(gov)
    del state.resources["budget"]
    out = gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0, flag_conditions_met=ACK)
    assert out["dispatched"] is False and out["mandate_revalidation_failed"] is True
    # The reservation cannot be given back to a resource that is gone: the
    # attempt stays open for reconciliation rather than closing as released.
    assert out["budget_reconciliation_required"] is True
    assert gov.registry.get(p.idempotency_key).state == OperationState.RESERVED
    assert gov.provider.calls == []


def test_withdrawn_delegation_refuses_fail_closed():
    gov, state = _gov()
    p = _admit(gov, action_type="delegation", recipient_id="bob",
               delegation=Delegation(delegating_agent_id="alice", delegatee_agent_id="bob"))
    record = gov.registry.get(p.idempotency_key)
    assert (record.delegating_agent_id, record.delegatee_agent_id) == ("alice", "bob")
    state.register_agent(Agent(agent_id="alice", authority_score=0.9, trusted_delegates=frozenset()))
    _assert_fail_closed(gov, state, p.idempotency_key, "delegation from 'alice' to 'bob' is withdrawn")


def test_removed_delegating_agent_refuses_fail_closed():
    gov, state = _gov()
    p = _admit(gov, action_type="delegation", recipient_id="bob",
               delegation=Delegation(delegating_agent_id="alice", delegatee_agent_id="bob"))
    del state.agents["alice"]
    _assert_fail_closed(gov, state, p.idempotency_key, "no longer registered")


def test_changed_policy_version_refuses_fail_closed():
    gov, state = _gov()
    p = _admit(gov)
    gov.policy = CompositionPolicy(version="composition-v2.0.0", flag_conditions=CONDITIONS)
    _assert_fail_closed(gov, state, p.idempotency_key,
                        "composition policy version changed from 'composition-v1.0.0' to 'composition-v2.0.0'")


def test_several_changes_are_all_reported():
    gov, state = _gov()
    p = _admit(gov)
    del state.agents["alice"]
    gov.policy = CompositionPolicy(version="composition-v2.0.0", flag_conditions=CONDITIONS)
    out = gov.dispatch(p.idempotency_key, "budget", 10.0, flag_conditions_met=ACK)
    assert len(out["mandate_changes"]) == 2


def test_policy_switch_off_restores_the_575_behaviour():
    gov, state = _gov(CompositionPolicy(flag_conditions=CONDITIONS, revalidate_mandate_at_dispatch=False))
    p = _admit(gov)
    del state.agents["alice"]
    state.resources["budget"].capacity = 5.0
    out = gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0, flag_conditions_met=ACK)
    assert out["dispatched"] is True  # documented 5.7.5 gap, now opt-in only
    assert len(gov.provider.calls) == 1


def test_policy_switch_must_be_a_bool():
    with pytest.raises(ValueError):
        CompositionPolicy(revalidate_mandate_at_dispatch="yes").validate()
    assert CompositionPolicy().revalidate_mandate_at_dispatch is True


def test_revalidation_runs_before_the_commit_gate_and_before_any_provider_call():
    """The refusal is settled before any authority or provider I/O: the ledger
    holds the HARD_VETO and no provider call, and the FLAG acknowledgement is
    recorded first (it is part of the mandate the dispatch asks to use)."""
    gov, state = _gov()
    p = _admit(gov)
    del state.agents["alice"]
    out = gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0, flag_conditions_met=ACK)
    assert out["dispatched"] is False and gov.provider.calls == []
    kinds = [e.rationale[:40] for e in gov.ledger.entries if e.idempotency_key == p.idempotency_key][-2:]
    assert kinds[0].startswith("FLAG conditions acknowledged at dispatch")
    assert kinds[1].startswith("MANDATE_REVALIDATION_FAILED at dispatch")


def test_a_hold_resolved_then_withdrawn_mandate_is_refused_at_dispatch():
    class _Broken:
        def adjudicate(self, **kwargs):
            raise RuntimeError("forced HOLD")
    gov, state = _gov()
    gov.scoring = _Broken()
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    assert gov.adjudicate(p, **M, resource_id="budget", amount=10.0)["composed_decision"] == Decision.HOLD.value
    gov.scoring = ScoringKernelAdapter(TenirKernel())
    assert gov.resolve_hold(p.idempotency_key, passage=p, **M, resource_id="budget", amount=10.0)["hold_resolved"]
    del state.agents["alice"]
    _assert_fail_closed(gov, state, p.idempotency_key, "acting agent 'alice' is no longer registered")


def test_durable_backend_releases_the_reservation_on_a_withdrawn_agent(tmp_path):
    registry = DurableOperationRegistry(str(tmp_path / "operations.json"))
    gov, state = _gov(registry=registry)
    backend = SingleHostLockBackend(str(tmp_path))
    backend.register_resource("budget", Decimal("100.0"))
    gov.consensus_backend = backend
    p = _admit(gov)
    assert backend.get_state("budget").reserved == Decimal("10.0")
    del state.agents["alice"]
    out = gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0, flag_conditions_met=ACK)
    assert out["dispatched"] is False and out["mandate_revalidation_failed"] is True
    assert out["execution_record"] == "CONFIRMED_NONEXECUTION"
    record = gov.registry.get(p.idempotency_key)
    assert record.state == OperationState.FAILED and record.budget_reserved is False
    assert backend.get_state("budget").reserved == Decimal("0")
    assert backend.get_state("budget").committed == Decimal("0")
    assert gov.provider.calls == []


def test_durable_backend_limit_is_not_read_at_dispatch(tmp_path):
    """With a durable backend the limit is the backend's (a re-registration with
    another capacity is refused); the in-memory copy is not consulted."""
    registry = DurableOperationRegistry(str(tmp_path / "operations.json"))
    gov, state = _gov(registry=registry)
    backend = SingleHostLockBackend(str(tmp_path))
    backend.register_resource("budget", Decimal("100.0"))
    gov.consensus_backend = backend
    p = _admit(gov)
    with pytest.raises(ValueError, match="different capacity"):
        backend.register_resource("budget", Decimal("5.0"))
    state.resources["budget"].capacity = 5.0  # a stale in-memory copy must not refuse
    out = gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0, flag_conditions_met=ACK)
    assert out["dispatched"] is True
