"""Opt-in qualified reconciliation: no evidence assertion bypasses the verifier."""
from tenir_governance.composition import DualKernelGovernor, ScoringKernelAdapter
from tenir_governance.membrane import TransitionMembrane, MembraneState
from tenir_governance.objects import Agent, Passage, Resource
from tenir_governance.operations import MockProvider, ProviderOutcome, OperationState
from tenir_kernel.kernel import TenirKernel


def setup_unknown():
    provider = MockProvider(behavior="timeout", reconcile_outcome=ProviderOutcome.FAILED)
    state = MembraneState()
    state.register_resource(Resource(resource_id="budget", capacity=100.0))
    state.register_agent(Agent(agent_id="alice", authority_score=0.9,
        trusted_delegates=frozenset({"bob"})))
    gov = DualKernelGovernor(ScoringKernelAdapter(TenirKernel()),
                             TransitionMembrane(state), provider=provider)
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    result = gov.adjudicate(
        p, pressure=0.2, velocity=0.2, capacity=0.9, resource_id="budget",
        amount=10.0, authority=0.9, context_fit=0.9, evidence_quality=0.9)
    assert result["operation_state"] == OperationState.RESERVED.value
    outcome = gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0)
    assert outcome["operation_state"] == OperationState.UNKNOWN.value
    return gov, state, p.idempotency_key


def test_qualification_requires_verifier_and_preserves_budget():
    gov, state, key = setup_unknown()
    before = state.resources["budget"].consumed
    qual = {"attempt_id": "A1", "evidence_ref": "receipt:1"}
    result = gov.reconcile_unknown(key, qualification=qual)
    assert not result["reconciled"]
    assert gov.registry.get(key).state == OperationState.UNKNOWN
    assert state.resources["budget"].consumed == before
    assert not gov.registry.get(key).qualification_evidence_ref


def test_qualification_verifier_rejection_blocks_transition():
    gov, state, key = setup_unknown()
    result = gov.reconcile_unknown(key,
        qualification={"attempt_id":"A1","evidence_ref":"receipt:1"},
        evidence_verifier=lambda *_: False)
    assert not result["reconciled"]
    assert gov.registry.get(key).state == OperationState.UNKNOWN
    assert state.resources["budget"].consumed == 10.0


def test_verified_reference_binds_and_reconciles_existing_budget_path():
    gov, state, key = setup_unknown()
    observed = []
    def verify(q, recon, idem):
        observed.append((q, recon.outcome, idem))
        return True
    result = gov.reconcile_unknown(key,
        qualification={"attempt_id":"A1","evidence_ref":"receipt:1"},
        evidence_verifier=verify)
    assert result["reconciled"]
    rec = gov.registry.get(key)
    assert rec.state == OperationState.FAILED
    assert rec.attempt_id == "A1"
    assert rec.qualification_kind == "non_effect_confirmed"
    assert rec.qualification_evidence_ref == "receipt:1"
    assert state.resources["budget"].consumed == 0.0
    assert observed[0][1] == ProviderOutcome.FAILED


def test_conflicting_existing_reference_rejected_before_budget_mutation():
    gov, state, key = setup_unknown()
    assert gov.registry.bind_unknown_qualification(key,
        attempt_id="A1", evidence_ref="receipt:first",
        qualification_kind="non_effect_confirmed")
    result = gov.reconcile_unknown(key,
        qualification={"attempt_id":"A2","evidence_ref":"receipt:second"},
        evidence_verifier=lambda *_: True)
    assert not result["reconciled"]
    assert gov.registry.get(key).state == OperationState.UNKNOWN
    assert state.resources["budget"].consumed == 10.0
