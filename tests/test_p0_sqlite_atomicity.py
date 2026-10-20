"""P0 regression coverage for atomic SQLite budget and reservation state."""

import json
import os
import subprocess
import sys
import threading
from decimal import Decimal

import pytest

from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend


def _backend(path, **kwargs):
    backend = SQLiteConsensusBackend(str(path), **kwargs)
    assert backend.register_resource("budget", "10") in ("REGISTERED", "ALREADY_REGISTERED")
    return backend


def test_budget_and_reservation_commit_together_and_survive_reopen(tmp_path):
    backend = _backend(tmp_path)
    reservation = backend.reserve("payment-1", "6.25", "budget")
    assert reservation.status == "RESERVED"
    assert backend.get_state("budget").reserved == Decimal("6.25")

    assert backend.commit(reservation.reservation_id).status == "COMMITTED"
    state = SQLiteConsensusBackend(str(tmp_path)).get_state("budget")
    assert state.reserved == Decimal("0")
    assert state.committed == Decimal("6.25")
    assert state.reservations[0]["state"] == "COMMITTED"
    assert state.reserved + state.committed <= state.capacity


@pytest.mark.parametrize("amount", [True, False, 0, -1, float("nan"), float("inf"), float("-inf"), "bad"])
def test_sqlite_backend_rejects_invalid_amount_without_mutation(tmp_path, amount):
    backend = _backend(tmp_path)
    result = backend.reserve("invalid", amount, "budget")
    assert result.status == "INVALID_AMOUNT"
    state = backend.get_state("budget")
    assert state.reserved == 0
    assert state.committed == 0
    assert state.reservations == []


def test_sqlite_atomic_transaction_rolls_back_before_commit(tmp_path, monkeypatch):
    backend = _backend(tmp_path)

    def simulate_crash(_operation):
        raise OSError("simulated process failure before SQLite commit")

    monkeypatch.setattr(backend, "_before_commit", simulate_crash)
    with pytest.raises(OSError, match="before SQLite commit"):
        backend.reserve("payment-crash", "4", "budget")

    reopened = SQLiteConsensusBackend(str(tmp_path))
    state = reopened.get_state("budget")
    assert state.reserved == 0
    assert state.committed == 0
    assert state.reservations == []


def test_process_crash_inside_wal_transaction_recovers_without_partial_reservation(tmp_path):
    _backend(tmp_path)
    script = "\n".join((
        "import os, sys",
        "from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend",
        "backend = SQLiteConsensusBackend(sys.argv[1])",
        "backend._before_commit = lambda _operation: os._exit(73)",
        "backend.reserve('process-crash-before-commit', '4', 'budget')",
        "raise SystemExit('fault injection did not terminate the process')",
    ))
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 73

    state = SQLiteConsensusBackend(str(tmp_path)).get_state("budget")
    assert state.reserved == 0
    assert state.committed == 0
    assert state.reservations == []


def test_process_crash_after_wal_commit_recovers_idempotently(tmp_path):
    _backend(tmp_path)
    script = "\n".join((
        "import os, sys",
        "from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend",
        "backend = SQLiteConsensusBackend(sys.argv[1])",
        "backend.reserve('process-crash-after-commit', '4', 'budget')",
        "os._exit(74)",
    ))
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 74

    recovered = SQLiteConsensusBackend(str(tmp_path))
    state = recovered.get_state("budget")
    replay = recovered.reserve("process-crash-after-commit", "4", "budget")
    assert state.reserved == Decimal("4")
    assert len(state.reservations) == 1
    assert replay.status == "RESERVED"
    assert replay.reservation_id == state.reservations[0]["reservation_id"]
    assert recovered.get_state("budget").reserved == Decimal("4")


def test_same_idempotency_key_cannot_create_second_reservation(tmp_path):
    backend = _backend(tmp_path)
    first = backend.reserve("same-key", "4", "budget")
    replay = backend.reserve("same-key", "4", "budget")
    conflict = backend.reserve("same-key", "5", "budget")

    assert replay.status == "RESERVED"
    assert replay.reservation_id == first.reservation_id
    assert conflict.status == "DUPLICATE_KEY"
    assert backend.get_state("budget").reserved == Decimal("4")


def test_two_backend_instances_serialize_concurrent_capacity_claims(tmp_path):
    backends = [_backend(tmp_path), _backend(tmp_path)]
    barrier = threading.Barrier(2)
    results = []
    result_lock = threading.Lock()

    def reserve(backend, key):
        barrier.wait()
        result = backend.reserve(key, "10", "budget")
        with result_lock:
            results.append(result.status)

    workers = [
        threading.Thread(target=reserve, args=(backends[0], "worker-a")),
        threading.Thread(target=reserve, args=(backends[1], "worker-b")),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)
        assert not worker.is_alive()

    assert sorted(results) == ["INSUFFICIENT_BUDGET", "RESERVED"]
    state = SQLiteConsensusBackend(str(tmp_path)).get_state("budget")
    assert state.reserved == Decimal("10")
    assert state.reserved + state.committed <= state.capacity


def test_late_effect_after_release_creates_idempotent_compensation_record(tmp_path):
    backend = _backend(tmp_path, provider_reconcile=lambda _key: True)
    reservation = backend.reserve("late-effect", "10", "budget")
    assert backend.release(reservation.reservation_id).status == "RELEASED"

    result = backend.reconcile("late-effect")
    replay = backend.reconcile("late-effect")
    state = SQLiteConsensusBackend(str(tmp_path)).get_state("budget")

    assert result.status == "RECONCILE_CONFLICT"
    assert result.compensation_id == f"compensation:{reservation.reservation_id}"
    assert replay.compensation_id == result.compensation_id
    assert state.reserved == 0
    assert state.committed == 0
    assert state.reservations[0]["state"] == "RELEASED"
    assert len(state.compensations) == 1
    assert state.compensations[0]["status"] == "REQUIRED"


def test_legacy_json_migration_requires_exactly_reconciled_totals(tmp_path):
    (tmp_path / "budget.json").write_text(json.dumps({
        "budget": {"capacity": "10", "reserved": "3", "consumed": "2"},
    }), encoding="utf-8")
    (tmp_path / "reservations.json").write_text(json.dumps({
        "held": {"key": "held-key", "amount": "3", "resource_id": "budget", "state": "RESERVED"},
        "spent": {"key": "spent-key", "amount": "2", "resource_id": "budget", "state": "COMMITTED"},
    }), encoding="utf-8")

    backend = SQLiteConsensusBackend(str(tmp_path))
    state = backend.get_state("budget")
    assert state.reserved == Decimal("3")
    assert state.committed == Decimal("2")
    assert len(state.reservations) == 2
    assert (tmp_path / "budget.json").exists()
    assert (tmp_path / "reservations.json").exists()


def test_legacy_json_migration_fails_closed_on_counter_mismatch(tmp_path):
    (tmp_path / "budget.json").write_text(json.dumps({
        "budget": {"capacity": "10", "reserved": "4", "consumed": "0"},
    }), encoding="utf-8")
    (tmp_path / "reservations.json").write_text(json.dumps({}), encoding="utf-8")

    with pytest.raises(RuntimeError, match="refusing SQLite migration"):
        SQLiteConsensusBackend(str(tmp_path))


def test_legacy_json_migration_refuses_pending_transaction_journal(tmp_path):
    (tmp_path / "budget.json").write_text("{}", encoding="utf-8")
    (tmp_path / "reservations.json").write_text("{}", encoding="utf-8")
    (tmp_path / "transaction.json").write_text("{}", encoding="utf-8")

    with pytest.raises(RuntimeError, match="pending transaction journal"):
        SQLiteConsensusBackend(str(tmp_path))
