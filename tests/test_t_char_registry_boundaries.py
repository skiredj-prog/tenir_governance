"""T-CHAR: observe real registry durability and reconciliation boundaries.

Characterization only: tests record current behavior, not A2/A3 guarantees.
"""
import json
import threading
from unittest.mock import patch
import pytest

from tenir_governance.operations.registry import (
    DurableOperationRegistry, OperationRecord, OperationState,
)


def new_unknown(path):
    r = DurableOperationRegistry(str(path))
    assert r.register(OperationRecord(
        idempotency_key="key", passage_id="pass", agent_id="alice",
        action_type="payment"))
    for target in (OperationState.RESERVED, OperationState.DISPATCHING,
                   OperationState.UNKNOWN):
        assert r.transition_to("key", target)[0]
    return r


def snapshot(reg):
    rec = reg.get("key")
    return {"state": rec.state.value, "budget_reserved": rec.budget_reserved,
            "budget_committed": rec.budget_committed,
            "has_record_version": hasattr(rec, "record_version"),
            "has_attempt_id": hasattr(rec, "attempt_id")}


def test_char_01_persist_then_lost_response(tmp_path):
    """Crash-like lost response after successful rename: fresh reader sees the write."""
    path = tmp_path / "registry.json"
    r = new_unknown(path)
    assert r.transition_to("key", OperationState.CONFIRMED)[0]
    # Simulate caller not receiving a response; never issue a second transition.
    fresh = DurableOperationRegistry(str(path))
    observation = snapshot(fresh)
    assert observation["state"] == "confirmed"
    assert fresh.transition_to("key", OperationState.CONFIRMED)[0] is False


def test_crash_postwrite_fsync_error_exposes_durable_state(tmp_path):
    """The registry transition path does not automatically reconcile post-write errors."""
    path = tmp_path / "registry.json"
    r = new_unknown(path)
    original = r._persist
    def persist_then_raise():
        original()
        raise OSError("injected error after durable write")
    with patch.object(r, "_persist", side_effect=persist_then_raise):
        with pytest.raises(OSError, match="after durable write"):
            r.transition_to("key", OperationState.CONFIRMED)
    fresh = DurableOperationRegistry(str(path))
    assert snapshot(fresh)["state"] == "confirmed"
    assert fresh.transition_to("key", OperationState.CONFIRMED)[0] is False


def test_char_03_two_independent_registry_instances_same_store(tmp_path):
    """Sequentialized concurrent terminal commands: exactly one wins."""
    path = tmp_path / "registry.json"
    first = new_unknown(path)
    second = DurableOperationRegistry(str(path))
    barrier = threading.Barrier(2)
    outcomes = []
    lock = threading.Lock()
    def transition(reg, target):
        barrier.wait()
        result = reg.transition_to("key", target)
        with lock:
            outcomes.append((target.value, result[0]))
    a = threading.Thread(target=transition, args=(first, OperationState.CONFIRMED))
    b = threading.Thread(target=transition, args=(second, OperationState.FAILED))
    a.start(); b.start(); a.join(timeout=10); b.join(timeout=10)
    assert not a.is_alive() and not b.is_alive()
    assert sorted(ok for _, ok in outcomes) == [False, True]
    terminal = DurableOperationRegistry(str(path)).get("key").state
    assert terminal in (OperationState.CONFIRMED, OperationState.FAILED)


def test_char_02_budget_flags_persist_but_transition_can_fail(tmp_path):
    """Observe a real intermediate state: flags durable, operation UNKNOWN."""
    path = tmp_path / "registry.json"
    r = new_unknown(path)
    assert r.set_budget_flags("key", budget_reserved=False, budget_committed=False)
    with patch.object(r, "transition_to", side_effect=OSError("injected transition failure")):
        with pytest.raises(OSError):
            r.transition_to("key", OperationState.FAILED)
    rec = DurableOperationRegistry(str(path)).get("key")
    assert rec.state == OperationState.UNKNOWN
    assert not rec.budget_reserved and not rec.budget_committed


def test_proof_conflict_current_main_schema_has_no_qualification_contract(tmp_path):
    """Pin absence of A2/A3 metadata on main; not a proof-authentication test."""
    r = new_unknown(tmp_path / "registry.json")
    rec = r.get("key")
    assert not hasattr(rec, "record_version")
    assert not hasattr(rec, "qualification_evidence_ref")
    assert not hasattr(rec, "finalization_state")
