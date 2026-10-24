"""T-CHAR second tranche: observable budget fault and contradictory signed evidence.

These tests document CURRENT behavior, not compliance with A2/A3.
"""
import json
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tenir_governance.composition import DualKernelGovernor, ScoringKernelAdapter
from tenir_governance.membrane import TransitionMembrane, MembraneState
from tenir_governance.objects import Agent, Passage, Resource
from tenir_governance.operations import MockProvider, ProviderOutcome, OperationState
from tenir_kernel.kernel import TenirKernel


def _governor_with_unknown():
    provider = MockProvider(behavior="timeout", reconcile_outcome=ProviderOutcome.FAILED)
    state = MembraneState()
    state.register_resource(Resource(resource_id="budget", capacity=100.0))
    state.register_agent(Agent(agent_id="alice", authority_score=0.9,
                               trusted_delegates=frozenset({"bob"})))
    gov = DualKernelGovernor(ScoringKernelAdapter(TenirKernel()),
                             TransitionMembrane(state), provider=provider)
    passage = Passage(agent_id="alice", action_type="payment", amount=10.0)
    adj = gov.adjudicate(
        passage, pressure=0.2, velocity=0.2, capacity=0.9,
        resource_id="budget", amount=10.0, authority=0.9,
        context_fit=0.9, evidence_quality=0.9)
    assert adj["operation_state"] == OperationState.RESERVED.value
    dispatch = gov.dispatch_and_execute(passage.idempotency_key, "budget", 10.0)
    assert dispatch["operation_state"] == OperationState.UNKNOWN.value
    assert state.resources["budget"].consumed == 10.0
    return gov, state.resources["budget"], passage.idempotency_key


def test_char_02_real_budget_refund_failure_after_registry_transition():
    """Inject a real Resource.refund exception, AFTER the registry moved to FAILED.

    Explicitly demonstrates the split between terminal state and accounting:
    FAIL here would indicate unexpected behavior change, not safety assurance.
    """
    gov, budget, key = _governor_with_unknown()
    def fail_refund(amount):
        raise OSError("T-CHAR injected refund failure")
    with patch.object(budget, "refund", side_effect=fail_refund):
        with pytest.raises(OSError, match="injected refund failure"):
            gov.reconcile_unknown(key)
    rec = gov.registry.get(key)
    # Characterization of the existing intermediate state (not desired A3 behavior).
    assert rec.state == OperationState.FAILED
    assert rec.budget_committed is False
    assert budget.consumed == 10.0


def test_proof_conflict_two_valid_signatures_not_supported_by_main_registry():
    """Two different authentic statements have opposite conclusions.

    Tests cryptographic verification only; current main has no evidence ingestion
    or conflict transition. This is a capability GAP, not a passed A3 conflict test.
    """
    key = Ed25519PrivateKey.generate()
    public = key.public_key()
    common = {"operation_id":"op-42", "attempt_id":"attempt-1"}
    positive = json.dumps({**common, "effect":"confirmed"}, sort_keys=True).encode()
    negative = json.dumps({**common, "effect":"not_observed"}, sort_keys=True).encode()
    sig_positive = key.sign(positive)
    sig_negative = key.sign(negative)
    public.verify(sig_positive, positive)
    public.verify(sig_negative, negative)
    assert positive != negative
    assert sig_positive != sig_negative
    from tenir_governance.operations.registry import OperationRecord
    assert "finalization_state" not in OperationRecord.__dataclass_fields__
    assert "qualification_evidence_ref" not in OperationRecord.__dataclass_fields__
