"""Adversarial regressions ported from #14; #17 races already live on main.

These tests exercise aggregate budget ownership independently of successful
provider execution. A foreign reservation is deliberately held in the same
resource to expose accidental amount-based release.
"""
from tenir_governance.operations import (
    DurableOperationRegistry, OperationRecord, OperationState, HoldManager,
)
from tenir_governance.operations.provider import ReconciliationResult, ProviderOutcome
from tenir_governance.composition import DualKernelGovernor, ScoringKernelAdapter
from tenir_governance.membrane import TransitionMembrane, MembraneState
from tenir_governance.objects import Agent, Resource
from tenir_kernel.kernel import TenirKernel


def _governor():
    state = MembraneState()
    resource = Resource(resource_id="budget", capacity=100.0)
    state.register_resource(resource)
    state.register_agent(Agent(agent_id="alice", authority_score=0.9))
    registry = DurableOperationRegistry()
    gov = DualKernelGovernor(
        ScoringKernelAdapter(TenirKernel()),
        TransitionMembrane(state),
        registry=registry,
    )
    return gov, resource


def test_expired_unowned_hold_cannot_release_foreign_reservation(monkeypatch):
    """#14: expiration can fail the operation, but cannot consume foreign ownership."""
    gov, resource = _governor()
    assert resource.reserve(10.0)
    key = "hold-without-budget-ownership"
    assert gov.registry.register(OperationRecord(
        idempotency_key=key, passage_id="p-unowned-hold",
        agent_id="alice", action_type="payment",
        amount=10.0, resource_id="budget",
        budget_reserved=False, budget_committed=False,
    ))
    assert gov.hold_manager.enter_hold(key, tau_k_seconds=3600)["entered"]
    # Exercise the terminal expiry path deterministically without waiting.
    monkeypatch.setattr(
        gov.hold_manager, "check_tau_k_expiry",
        lambda _key: {"expired": True, "reason": "test deadline reached"},
    )
    result = gov.expire_hold(key)
    assert result["expired"] is True
    assert gov.registry.get(key).state == OperationState.FAILED
    assert resource.reserved == 10.0
    assert resource.consumed == 0.0


def test_hold_cannot_expire_before_tau_k():
    """#14: live deadline is not bypassed in the real check_tau_k_expiry path."""
    gov, resource = _governor()
    key = "hold-before-deadline"
    assert gov.registry.register(OperationRecord(
        idempotency_key=key, passage_id="p-before-deadline",
        agent_id="alice", action_type="payment",
    ))
    assert gov.hold_manager.enter_hold(key, tau_k_seconds=3600)["entered"]
    result = gov.expire_hold(key)
    assert result["expired"] is False
    assert gov.registry.get(key).state == OperationState.HOLD
    assert resource.reserved == 0.0


def test_failed_provider_reconciliation_does_not_release_foreign_budget():
    """#14: confirmed no-effect cannot free a reservation it does not own."""
    gov, resource = _governor()
    assert resource.reserve(10.0)
    key = "unknown-without-budget-ownership"
    assert gov.registry.register(OperationRecord(
        idempotency_key=key, passage_id="p-unowned-reconcile",
        agent_id="alice", action_type="payment",
        state=OperationState.UNKNOWN,
        amount=10.0, resource_id="budget",
        budget_reserved=False, budget_committed=False,
    ))

    class ConfirmedNoEffect:
        def reconcile(self, **kwargs):
            return ReconciliationResult(
                resolved=True, outcome=ProviderOutcome.FAILED,
            )

    gov.provider = ConfirmedNoEffect()
    result = gov.reconcile_unknown(key)
    assert result["reconciled"] is True
    assert result["operation_state"] == OperationState.FAILED.value
    assert gov.registry.get(key).state == OperationState.FAILED
    assert resource.reserved == 10.0
    assert resource.consumed == 0.0
