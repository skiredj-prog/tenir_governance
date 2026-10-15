"""I8 budget accounting regressions for the governed HOLD lifecycle.

These tests specify end-to-end accounting invariants at the governor boundary:
a HOLD must not cause a duplicate reservation when it is re-adjudicated, and
a denied ESCALATED operation must not retain reserved budget.
"""
from tenir_governance.composition.governor import (
    DualKernelGovernor,
    ScoringKernelAdapter,
)
from tenir_governance.membrane import MembraneState, TransitionMembrane
from tenir_governance.objects import Agent, Passage, Resource
from tenir_governance.operations.hold import HoldManager
from tenir_governance.operations.provider import MockProvider
from tenir_governance.operations.registry import (
    DurableOperationRegistry,
    OperationState,
)
from tenir_kernel.kernel import TenirKernel


def _setup_governor(*, provider=None, capacity=100.0):
    state = MembraneState()
    state.register_resource(Resource(resource_id="budget", capacity=capacity))
    state.register_agent(
        Agent(
            agent_id="alice",
            authority_score=0.9,
            trusted_delegates=frozenset({"bob"}),
        )
    )
    registry = DurableOperationRegistry()
    scoring = ScoringKernelAdapter(TenirKernel())
    membrane = TransitionMembrane(state)
    governor = DualKernelGovernor(
        scoring,
        membrane,
        registry=registry,
        provider=provider,
    )
    return governor, state


class _BrokenScoring:
    def adjudicate(self, **kwargs):
        raise RuntimeError("test: force initial HOLD")


def _enter_hold_with_budget(governor, *, amount=10.0):
    passage = Passage(agent_id="alice", action_type="payment", amount=amount)
    governor.scoring = _BrokenScoring()
    result = governor.adjudicate(
        passage,
        pressure=0.2,
        velocity=0.2,
        capacity=0.9,
        resource_id="budget",
        amount=amount,
    )
    assert result["operation_state"] == OperationState.HOLD.value
    assert governor.registry.get(passage.idempotency_key).state == OperationState.HOLD
    return passage


def test_hold_resolution_dispatch_accounts_for_exactly_one_reservation():
    """HOLD -> re-adjudication -> RESERVED -> dispatch must not double-reserve."""
    governor, state = _setup_governor()
    amount = 10.0
    passage = _enter_hold_with_budget(governor, amount=amount)

    governor.scoring = ScoringKernelAdapter(TenirKernel())
    resolved = governor.resolve_hold(
        passage.idempotency_key,
        passage=passage,
        pressure=0.2,
        velocity=0.2,
        capacity=0.9,
        resource_id="budget",
        amount=amount,
    )
    assert resolved["hold_resolved"] is True
    assert resolved["operation_state"] == OperationState.RESERVED.value

    dispatched = governor.dispatch(
        passage.idempotency_key,
        resource_id="budget",
        amount=amount,
    )
    assert dispatched["dispatched"] is True
    resource = state.resources["budget"]
    assert resource.consumed == amount
    assert resource.reserved == 0.0


def test_denied_escalated_operation_releases_its_budget_reservation():
    """ESCALATED -> authorized denial must leave no reservation behind."""
    governor, state = _setup_governor()
    amount = 10.0
    passage = _enter_hold_with_budget(governor, amount=amount)

    escalated = governor.escalate_hold(
        passage.idempotency_key,
        escalated_by="risk-officer",
    )
    assert escalated["escalated"] is True
    assert escalated["operation_state"] == OperationState.ESCALATED.value

    denied = governor.authorize_exit(
        passage.idempotency_key,
        authorized_by="risk-officer",
        scope="payment",
        authorization="test-authorization",
        reason="denied by test",
        approve=False,
    )
    assert denied["exited"] is True
    assert denied["operation_state"] == OperationState.FAILED.value

    resource = state.resources["budget"]
    assert resource.reserved == 0.0
    assert resource.consumed == 0.0
