"""End-to-end observation boundary tests using actual durable operation registry.

No test grants an external dispatch or mutates the governor state from an
attestation. Cross-store races are explicitly downgraded to HOLD.
"""
from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tenir_governance.operations.registry import (
    DurableOperationRegistry, OperationRecord, OperationState,
)
from tenir_governance.operations.attempt_qualification import (
    AttemptQualificationStore, QualificationError,
)
from tenir_governance.operations.attempt_attestation import (
    Ed25519AttemptVerifier, canonical_attestation_payload,
)
from tenir_governance.operations.attempt_operation_bridge import (
    AttemptOperationObservationBridge, OperationLinkError,
    operation_context_digest,
)

NONCE = "0123456789abcdef0123456789abcdef"


def _fixture(tmp_path, *, op_state=OperationState.UNKNOWN):
    registry = DurableOperationRegistry(str(tmp_path / "operations.json"))
    assert registry.register(OperationRecord(
        idempotency_key="op-1", passage_id="passage-1", agent_id="alice",
        action_type="payment", amount=25.0, resource_id="budget",
        state=op_state, operation_id="institutional-operation-1",
        institutional_scope="bank", payload_digest="sha256:" + "ab"*32,
    ))
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    verifier = Ed25519AttemptVerifier({"authority":public},now_ms=lambda:1500)
    attempts = AttemptQualificationStore(tmp_path/"candidate.sqlite",verifier)
    bridge = AttemptOperationObservationBridge(registry,attempts)
    return bridge, registry, attempts, private


def _signed(private, digest, *, outcome="EFFECT_CONFIRMED"):
    fields = {
        "schema_version":"tenir.attempt.attestation.v1",
        "evidence_id":"proof-1", "attempt_id":"attempt-1",
        "lei":"LEI-1", "nonce":NONCE, "operation_digest":digest,
        "outcome":outcome, "authority_id":"authority",
        "issued_at_ms":1000, "expires_at_ms":2000,
    }
    return {**fields, "signature_hex":private.sign(
        canonical_attestation_payload(fields)).hex()}


def _begin(bridge):
    return bridge.begin_observation(
        operation_key="op-1",attempt_id="attempt-1",
        lei="LEI-1",nonce=NONCE,deadline_ms=1400,
    )


def test_real_persistent_unknown_operation_bound_to_signed_positive_evidence(tmp_path):
    bridge, registry, attempts, private = _fixture(tmp_path)
    started = _begin(bridge)
    assert started.status == "MATCHED_OBSERVATION_ONLY"
    assert started.execution_authorized is False
    digest = operation_context_digest(registry.get("op-1"))
    assert attempts.read("attempt-1")["operation_digest"] == digest
    assert attempts.read("attempt-1")["operation_key"] == "op-1"
    result = bridge.qualify_observation(
        operation_key="op-1",attempt_id="attempt-1",
        signed_evidence=_signed(private,digest),observed_ms=1500,
    )
    assert result.status == "EVIDENCE_RECORDED_NO_COMMIT_AUTHORITY"
    assert result.attempt_state == "RESOLVED"
    assert result.execution_authorized is False
    # Independent attempted settlement must never modify governor state.
    assert DurableOperationRegistry(str(tmp_path/"operations.json")).get("op-1").state == OperationState.UNKNOWN


def test_signed_non_execution_does_not_terminalize_operation_or_refund(tmp_path):
    bridge, registry, _attempts, private = _fixture(tmp_path)
    _begin(bridge)
    result = bridge.qualify_observation(
        operation_key="op-1",attempt_id="attempt-1",
        signed_evidence=_signed(private,operation_context_digest(registry.get("op-1")),
                                outcome="NON_EXECUTION_CONFIRMED"),observed_ms=1500)
    assert result.status == "EVIDENCE_RECORDED_NO_COMMIT_AUTHORITY"
    assert result.attempt_state == "FAILED"
    assert registry.get("op-1").state == OperationState.UNKNOWN
    assert result.execution_authorized is False


def test_refuse_unpersisted_registry(tmp_path):
    attempts = AttemptQualificationStore(tmp_path/"attempt.sqlite",lambda _: True)
    with pytest.raises(OperationLinkError,match="persistent"):
        AttemptOperationObservationBridge(DurableOperationRegistry(), attempts)


def test_terminal_operation_never_enrolled(tmp_path):
    bridge,_registry,attempts,_private = _fixture(tmp_path,op_state=OperationState.FAILED)
    with pytest.raises(OperationLinkError,match="UNKNOWN"):
        _begin(bridge)
    with pytest.raises(KeyError):
        attempts.read("attempt-1")


def test_operation_moves_to_terminal_after_begin_then_observation_holds(tmp_path):
    bridge,registry,_attempts,private = _fixture(tmp_path)
    _begin(bridge)
    digest = operation_context_digest(registry.get("op-1"))
    assert registry.transition_to("op-1",OperationState.FAILED)[0]
    result = bridge.qualify_observation(
        operation_key="op-1",attempt_id="attempt-1",
        signed_evidence=_signed(private,digest),observed_ms=1500)
    assert result.status == "HOLD"
    assert result.execution_authorized is False


def test_operation_terminalizes_during_verifier_call_then_hold(tmp_path):
    bridge,registry,attempts,private = _fixture(tmp_path)
    _begin(bridge)
    digest = operation_context_digest(registry.get("op-1"))
    original = attempts.verifier
    def racing_verifier(value):
        proof = original(value)
        assert registry.transition_to("op-1",OperationState.FAILED)[0]
        return proof
    attempts.verifier = racing_verifier
    result = bridge.qualify_observation(
        operation_key="op-1",attempt_id="attempt-1",
        signed_evidence=_signed(private,digest),observed_ms=1500)
    assert result.status == "HOLD"
    assert result.evidence_recorded is True
    assert result.execution_authorized is False
    assert attempts.read("attempt-1")["state"] == "RESOLVED"
    assert registry.get("op-1").state == OperationState.FAILED


def test_wrong_operation_key_never_matches(tmp_path):
    bridge,registry,_attempts,_private = _fixture(tmp_path)
    _begin(bridge)
    registry.register(OperationRecord(
        idempotency_key="op-2",passage_id="passage-2",
        agent_id="bob",action_type="payment",state=OperationState.UNKNOWN))
    result = bridge.observe(operation_key="op-2",attempt_id="attempt-1")
    assert result.status == "HOLD"
    assert result.execution_authorized is False


def test_source_identity_drift_holds(tmp_path):
    bridge,registry,_attempts,_private = _fixture(tmp_path)
    _begin(bridge)
    # Simulate an upstream persistence bug, not a supported mutation API.
    source = registry.get("op-1")
    source.payload_digest = "sha256:" + "cd"*32
    # A test-only direct persist simulates forbidden upstream identity mutation.
    # Never call registry.get() while holding registry._lock (non-reentrant).
    with registry._lock:
        registry._persist()
    observed = bridge.observe(operation_key="op-1",attempt_id="attempt-1")
    assert observed.status == "HOLD"
    assert "drift" in observed.reason


def test_untrusted_registry_corruption_fails_closed(tmp_path):
    bridge,registry,_attempts,_private = _fixture(tmp_path)
    _begin(bridge)
    (tmp_path/"operations.json").write_text("{not-json",encoding="utf-8")
    observed = bridge.observe(operation_key="op-1",attempt_id="attempt-1")
    assert observed.status == "HOLD"


def test_same_operation_cannot_be_rebound_to_new_lei(tmp_path):
    bridge, registry, attempts, _private = _fixture(tmp_path)
    _begin(bridge)
    with pytest.raises(QualificationError):
        attempts.begin(attempt_id="attempt-2",lei="LEI-2",
                       nonce="fedcba9876543210fedcba9876543210",
                       operation_digest=operation_context_digest(registry.get("op-1")),
                       operation_key="op-1",deadline_ms=1900)
    assert bridge.observe(operation_key="op-1",attempt_id="attempt-1").status == "MATCHED_OBSERVATION_ONLY"
