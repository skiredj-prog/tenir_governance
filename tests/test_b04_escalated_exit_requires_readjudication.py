"""B04: an authorized ESCALATED exit is not a fresh admission decision."""
from tenir_governance.composition import DualKernelGovernor, ScoringKernelAdapter
from tenir_governance.membrane import MembraneState, TransitionMembrane
from tenir_governance.objects import Agent, Passage, Resource
from tenir_governance.operations import OperationState
from tenir_kernel.kernel import TenirKernel


class BrokenScoring:
    def adjudicate(self, **kwargs):
        raise RuntimeError("force initial HOLD")


def _governor():
    state = MembraneState()
    state.register_resource(Resource(resource_id="budget", capacity=100.0))
    state.register_agent(Agent(agent_id="alice", authority_score=0.9))
    return DualKernelGovernor(ScoringKernelAdapter(TenirKernel()), TransitionMembrane(state)), state


def test_approved_escalated_exit_requires_fresh_readjudication_before_admission():
    gov, state = _governor()
    passage = Passage(agent_id="alice", action_type="payment", amount=10.0)
    gov.scoring = BrokenScoring()

    initial = gov.adjudicate(
        passage, pressure=0.2, velocity=0.2, capacity=0.9,
        authority=0.9, context_fit=1.0, evidence_quality=1.0,
        resource_id="budget", amount=10.0,
    )
    assert initial["operation_state"] == OperationState.HOLD.value
    assert state.resources["budget"].reserved == 10.0
    assert gov.escalate_hold(passage.idempotency_key, escalated_by="risk-officer")["escalated"]

    approval = gov.authorize_exit(
        passage.idempotency_key,
        authorized_by="ciso",
        scope="payment:10",
        authorization="ceremony-G-R5-001",
        reason="Risk officer reviewed the escalation",
        approve=True,
    )

    assert approval["exited"] is True
    assert approval["operation_state"] == OperationState.HOLD.value
    assert approval["exit_receipt"]["authorized_by"] == "ciso"
    assert state.resources["budget"].reserved == 10.0

    dispatch = gov.dispatch(passage.idempotency_key, "budget", 10.0)
    assert dispatch["dispatched"] is False
    assert gov.registry.get(passage.idempotency_key).state == OperationState.HOLD

    gov.scoring = ScoringKernelAdapter(TenirKernel())
    resolved = gov.resolve_hold(
        passage.idempotency_key,
        passage=passage,
        pressure=0.2, velocity=0.2, capacity=0.9,
        authority=0.9, context_fit=1.0, evidence_quality=1.0,
        resource_id="budget", amount=10.0,
    )
    assert resolved["hold_resolved"] is True
    assert resolved["operation_state"] == OperationState.RESERVED.value
    assert state.resources["budget"].reserved == 10.0
