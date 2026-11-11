"""Bounded bridge checks: durable operation UNKNOWN semantics in TENIR-Gov.

These do not claim RFC-4 T6/T7/T8 conformance: OperationRecord has no
attempt_id/evidence_qualified/retry_eligible lifecycle contract.
"""
from tenir_governance.operations.registry import (
    DurableOperationRegistry, OperationRecord, OperationState,
)


def _unknown_registry(path):
    registry = DurableOperationRegistry(str(path))
    record = OperationRecord(
        idempotency_key="lei-1", passage_id="pass-1", agent_id="agent",
        action_type="payment"
    )
    assert registry.register(record)
    for nxt in (OperationState.RESERVED, OperationState.DISPATCHING, OperationState.UNKNOWN):
        assert registry.transition_to("lei-1", nxt)[0]
    return registry


def test_unknown_cannot_be_redispatched_or_re_reserved(tmp_path):
    registry = _unknown_registry(tmp_path / "operations.json")
    for nxt in (OperationState.RESERVED, OperationState.DISPATCHING):
        ok, previous = registry.transition_to("lei-1", nxt)
        assert not ok
        assert previous == OperationState.UNKNOWN
    assert registry.get("lei-1").state == OperationState.UNKNOWN


def test_unknown_survives_new_registry_instance_and_blocks_reclaim(tmp_path):
    path = tmp_path / "operations.json"
    _unknown_registry(path)
    restarted = DurableOperationRegistry(str(path))
    assert restarted.get("lei-1").state == OperationState.UNKNOWN
    assert not restarted.register(OperationRecord(
        idempotency_key="lei-1", passage_id="pass-2", agent_id="agent",
        action_type="payment",
    ))
    assert restarted.transition_to("lei-1", OperationState.DISPATCHING)[0] is False


def test_unknown_can_reconcile_but_terminal_effect_cannot_be_retried(tmp_path):
    registry = _unknown_registry(tmp_path / "operations.json")
    assert registry.transition_to("lei-1", OperationState.CONFIRMED)[0]
    assert registry.transition_to("lei-1", OperationState.DISPATCHING)[0] is False
    assert registry.transition_to("lei-1", OperationState.FAILED)[0] is False


def test_unknown_to_failed_is_state_only_not_qualified_t7_proof(tmp_path):
    registry = _unknown_registry(tmp_path / "operations.json")
    # Bare registry offers a legal UNKNOWN -> FAILED transition without a
    # provenance/evidence parameter. This test documents the existing boundary
    # and MUST NOT be interpreted as an RFC-4 T7 proof.
    assert registry.transition_to("lei-1", OperationState.FAILED)[0]
    assert registry.get("lei-1").state == OperationState.FAILED
