"""A2 v0.2: strict CAS and monotone durable record version."""
from concurrent.futures import ThreadPoolExecutor
import pytest
from unittest.mock import patch

from tenir_governance.operations.registry import (
    DurableOperationRegistry, OperationRecord, OperationState, VersionConflict,
)


def initial(path):
    r = DurableOperationRegistry(str(path))
    assert r.register(OperationRecord(
        idempotency_key="one", passage_id="p", agent_id="a",
        action_type="payment"))
    assert r.get("one").record_version == 1
    assert r.transition_to("one", OperationState.RESERVED)[0]
    assert r.transition_to("one", OperationState.DISPATCHING)[0]
    assert r.transition_to("one", OperationState.UNKNOWN)[0]
    return r


def test_cas_stale_read_rejected_and_current_version_reported(tmp_path):
    r = initial(tmp_path / "ops.json")
    v = r.get("one").record_version
    assert r.transition_cas("one", OperationState.FAILED, expected_version=v)[0]
    with pytest.raises(VersionConflict) as e:
        r.transition_cas("one", OperationState.CONFIRMED, expected_version=v)
    assert e.value.current_version == v + 1
    assert r.get("one").state == OperationState.FAILED


def test_two_instances_only_one_wins(tmp_path):
    path = tmp_path / "ops.json"
    r1 = initial(path)
    r2 = DurableOperationRegistry(str(path))
    version = r1.get("one").record_version
    def run(reg, state):
        try:
            return reg.transition_cas("one", state, expected_version=version)[0]
        except VersionConflict:
            return "STALE"
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(run, r1, OperationState.FAILED),
            pool.submit(run, r2, OperationState.CONFIRMED),
        ]
        results = [f.result(timeout=10) for f in futures]
    assert sorted(map(str, results)) == ["STALE", "True"]
    assert DurableOperationRegistry(str(path)).get("one").record_version == version + 1


def test_budget_flag_mutation_increments_version(tmp_path):
    r = initial(tmp_path / "ops.json")
    v = r.get("one").record_version
    assert r.set_budget_flags("one", budget_committed=True)
    assert r.get("one").record_version == v + 1
    assert r.set_budget_flags("one", budget_committed=True)
    assert r.get("one").record_version == v + 1


def test_legacy_records_without_version_remain_readable(tmp_path):
    old = {"idempotency_key": "old", "passage_id": "p",
           "agent_id": "a", "action_type": "payment", "state": "unknown"}
    path = tmp_path / "ops.json"
    import json
    path.write_text(json.dumps({"old": old}))
    r = DurableOperationRegistry(str(path))
    assert r.get("old").record_version == 0
    assert r.transition_cas("old", OperationState.FAILED, expected_version=0)[0]
    assert r.get("old").record_version == 1


def test_postwrite_failure_requires_reload(tmp_path):
    path = tmp_path / "ops.json"
    r = initial(path)
    v = r.get("one").record_version
    persist = r._persist
    def fail_after_write():
        persist()
        raise OSError("response lost after fsync")
    with patch.object(r, "_persist", side_effect=fail_after_write):
        with pytest.raises(OSError):
            r.transition_cas("one", OperationState.FAILED, expected_version=v)
    fresh = DurableOperationRegistry(str(path))
    assert fresh.get("one").record_version == v + 1
    with pytest.raises(VersionConflict):
        fresh.transition_cas("one", OperationState.CONFIRMED, expected_version=v)


def test_budget_flags_cas_rejects_stale_version(tmp_path):
    path = tmp_path / "ops.json"
    first = initial(path)
    second = DurableOperationRegistry(str(path))
    v = first.get("one").record_version
    assert first.set_budget_flags_cas(
        "one", expected_version=v, budget_reserved=True)
    with pytest.raises(VersionConflict) as conflict:
        second.set_budget_flags_cas(
            "one", expected_version=v, budget_committed=True)
    assert conflict.value.current_version == v + 1
    record = second.get("one")
    assert record.budget_reserved and not record.budget_committed


def test_budget_flags_cas_and_state_transition_share_version(tmp_path):
    r = initial(tmp_path / "ops.json")
    v = r.get("one").record_version
    assert r.transition_cas("one", OperationState.FAILED, expected_version=v)[0]
    with pytest.raises(VersionConflict):
        r.set_budget_flags_cas(
            "one", expected_version=v, budget_reserved=False)


def test_budget_flags_cas_noop_preserves_version(tmp_path):
    r = initial(tmp_path / "ops.json")
    v = r.get("one").record_version
    assert r.set_budget_flags_cas(
        "one", expected_version=v, budget_reserved=False)
    assert r.get("one").record_version == v


def test_budget_flags_cas_postwrite_error_recovers_current_version(tmp_path):
    path = tmp_path / "ops.json"
    r = initial(path)
    v = r.get("one").record_version
    persist = r._persist
    def persist_then_error():
        persist()
        raise OSError("fault after durable budget flag write")
    with patch.object(r, "_persist", side_effect=persist_then_error):
        # Existing set_budget_flags handles post-write errors by inspecting
        # the durable flags, so this succeeds without double accounting.
        assert r.set_budget_flags_cas(
            "one", expected_version=v, budget_committed=True)
    fresh = DurableOperationRegistry(str(path))
    assert fresh.get("one").record_version == v + 1
    with pytest.raises(VersionConflict):
        fresh.set_budget_flags_cas(
            "one", expected_version=v, budget_committed=False)
