"""Regression: evidence-reference binding is atomic, durable and never a verdict."""
import pytest
from tenir_governance.operations.registry import (
    DurableOperationRegistry, OperationRecord, OperationState,
)


def make_unknown(path):
    r = DurableOperationRegistry(str(path))
    assert r.register(OperationRecord(
        idempotency_key="lei-1", passage_id="p1",
        agent_id="alice", action_type="payment"))
    for state in (OperationState.RESERVED, OperationState.DISPATCHING,
                  OperationState.UNKNOWN):
        assert r.transition_to("lei-1", state)[0]
    return r


def bind(r, attempt="a1", evidence="sha256:abc", kind="non_effect_confirmed"):
    return r.bind_unknown_qualification(
        "lei-1", attempt_id=attempt,
        evidence_ref=evidence, qualification_kind=kind)


def test_durable_binding_survives_restart_without_resolution(tmp_path):
    path = tmp_path / "ops.json"
    r = make_unknown(path)
    assert bind(r)
    fresh = DurableOperationRegistry(str(path))
    record = fresh.get("lei-1")
    assert record.state == OperationState.UNKNOWN
    assert (record.attempt_id, record.qualification_evidence_ref,
            record.qualification_kind) == ("a1", "sha256:abc", "non_effect_confirmed")
    assert bind(fresh)
    assert not bind(fresh, evidence="sha256:different")
    assert fresh.get("lei-1").qualification_evidence_ref == "sha256:abc"


def test_non_unknown_cannot_bind(tmp_path):
    r = DurableOperationRegistry(str(tmp_path / "ops.json"))
    assert r.register(OperationRecord(
        idempotency_key="lei-1", passage_id="p1",
        agent_id="alice", action_type="payment"))
    assert not bind(r)
    assert r.get("lei-1").attempt_id == ""


def test_invalid_and_unqualified_inputs_rejected(tmp_path):
    r = make_unknown(tmp_path / "ops.json")
    with pytest.raises(ValueError):
        bind(r, evidence="")
    with pytest.raises(ValueError):
        bind(r, kind="unknown")
    assert r.get("lei-1").qualification_evidence_ref == ""


def test_shared_file_competing_bindings_only_one_wins(tmp_path):
    path = tmp_path / "ops.json"
    r1 = make_unknown(path)
    r2 = DurableOperationRegistry(str(path))
    assert bind(r1, attempt="a1")
    assert not bind(r2, attempt="a2")
    assert r2.get("lei-1").attempt_id == "a1"


def test_old_record_schema_defaults_remain_compatible():
    old = {
        "idempotency_key": "old", "passage_id": "p1",
        "agent_id": "alice", "action_type": "payment",
        "state": "unknown",
    }
    record = OperationRecord.from_dict(old)
    assert record.attempt_id == ""
    assert record.qualification_evidence_ref == ""
    assert record.qualification_kind == ""
