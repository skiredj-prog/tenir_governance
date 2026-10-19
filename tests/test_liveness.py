"""Tests for liveness (C2) — termination under quorum.

These tests verify that operations on the ConsensusBackend adapter terminate
within a bounded time T_max when the system is healthy (single-host, no
partition, quorum always available).

For the SingleHostLockBackend (single-host), the quorum is always available
since there is no network partition possible within a single host. The tests
verify that:

1. All invoked operations complete (none hang indefinitely)
2. Operations complete within T_max
3. Operations blocked by HOLD/HARD_VETO are correctly excluded from liveness
4. Reconciliation terminates (CONFIRMED, FAILED, or UNRESOLVED) — not infinite wait

C2 properties tested (consensus-contract-v1.md §3):
  - C2.1: Termination under quorum
  - C2.2: Liveness is conditional (only under quorum)
  - C2.3: Reconciliation liveness
"""
import os
import tempfile
import threading
import time

import pytest
from decimal import Decimal, localcontext

from tenir_governance.operations.consensus_backend import (
    SingleHostLockBackend,
    ReservationResult,
    CommitResult,
    ReleaseResult,
    ReconciliationResult,
    ConsensusState,
)


class TestLivenessTermination:
    """Verify that operations terminate within T_max under quorum."""

    T_MAX = 5.0  # 5 seconds — generous for single-host file-lock

    def test_decimal_reservation_is_json_safe_and_idempotent(self, tmp_path):
        backend = SingleHostLockBackend(str(tmp_path))
        backend.register_resource("decimal-budget", 1)
        first = backend.reserve("decimal-key", Decimal("0.1"), "decimal-budget")
        assert first.status == "RESERVED"
        replay = backend.reserve("decimal-key", Decimal("0.1"), "decimal-budget")
        assert replay.status == "RESERVED"
        assert replay.reservation_id == first.reservation_id
        state = backend.get_state("decimal-budget")
        assert state.reserved == Decimal("0.1")
        assert state.available == Decimal("0.9")

    def test_decimal_budget_accepts_exact_capacity_without_float_drift(self, tmp_path):
        """Decimal reservations may consume an exact decimal capacity fully."""
        backend = SingleHostLockBackend(str(tmp_path))
        backend.register_resource("exact-decimal-budget", Decimal("0.3"))

        first = backend.reserve("exact-decimal-key-1", Decimal("0.1"), "exact-decimal-budget")
        second = backend.reserve("exact-decimal-key-2", Decimal("0.2"), "exact-decimal-budget")
        over = backend.reserve("exact-decimal-key-3", Decimal("0.0000001"), "exact-decimal-budget")

        assert first.status == "RESERVED"
        assert second.status == "RESERVED"
        assert over.status == "INSUFFICIENT_BUDGET"
        state = backend.get_state("exact-decimal-budget")
        assert state.reserved == Decimal("0.3")
        assert state.available == Decimal("0.0")
        assert state.reserved + state.committed <= state.capacity

    def test_high_precision_decimal_reservation_survives_restart(self, tmp_path):
        amount = Decimal("0.123456789012345678901")
        backend = SingleHostLockBackend(str(tmp_path))
        backend.register_resource("precision-budget", amount)
        first = backend.reserve("precision-key", amount, "precision-budget")
        assert first.status == "RESERVED"

        # The durable reservation record must preserve the decimal spelling,
        # not round it through a JSON float.
        import json
        record = json.loads((tmp_path / "reservations.json").read_text())
        persisted = next(iter(record.values()))
        assert persisted["amount"] == str(amount)

        reopened = SingleHostLockBackend(str(tmp_path))
        replay = reopened.reserve("precision-key", amount, "precision-budget")
        assert replay.status == "RESERVED"
        assert replay.reservation_id == first.reservation_id
        assert reopened.commit(first.reservation_id).status == "COMMITTED"
        assert reopened.reserve(
            "precision-overrun", Decimal("0.000000000000000000001"), "precision-budget"
        ).status == "INSUFFICIENT_BUDGET"

    def test_decimal_accounting_exceeds_default_context_precision(self, tmp_path):
        """I8 accounting must not round exact values at Decimal's default precision."""
        amount = Decimal("1.234567890123456789012345678901234567")
        backend = SingleHostLockBackend(str(tmp_path))
        backend.register_resource("long-decimal-budget", amount)
        result = backend.reserve("long-decimal-key", amount, "long-decimal-budget")
        assert result.status == "RESERVED"

        # Reopening validates the durable I8 invariant using the exact counters.
        reopened = SingleHostLockBackend(str(tmp_path))
        exact = reopened._coordinator.get_state_exact("long-decimal-budget")
        assert exact["reserved"] == amount
        assert exact["capacity"] == amount
        assert exact["available"] == Decimal("0")
        assert reopened.reserve(
            "long-decimal-overrun", Decimal("0.000000000000000000000000000000000001"),
            "long-decimal-budget",
        ).status == "INSUFFICIENT_BUDGET"

    def test_corrupted_budget_store_fails_closed(self, tmp_path):
        (tmp_path / "budget.json").write_text("{not-json")
        with pytest.raises(RuntimeError, match="Corrupted durable budget store"):
            SingleHostLockBackend(str(tmp_path))

    def test_corrupted_reservation_registry_fails_closed(self, tmp_path):
        backend = SingleHostLockBackend(str(tmp_path))
        backend.register_resource("corrupt-registry-budget", 1)
        budget_path = tmp_path / "budget.json"
        budget_before = budget_path.read_bytes()
        (tmp_path / "reservations.json").write_text("{not-json")

        with pytest.raises(RuntimeError, match="Corrupted durable reservation registry"):
            backend.reserve("must-not-duplicate", 0.1, "corrupt-registry-budget")
        # The budget file is untouched, and reads also fail closed while the
        # registry cannot prove the operation state.
        assert budget_path.read_bytes() == budget_before
        with pytest.raises(RuntimeError, match="Corrupted durable reservation registry"):
            backend.get_state("corrupt-registry-budget")

    def test_resource_reregistration_preserves_accounting(self, tmp_path):
        backend = SingleHostLockBackend(str(tmp_path))
        backend.register_resource("reregister-budget", 1)
        first = backend.reserve("reregister-key", Decimal("0.4"), "reregister-budget")
        assert first.status == "RESERVED"
        assert backend.commit(first.reservation_id).status == "COMMITTED"

        # Same-capacity registration is idempotent and must not reset counters.
        backend.register_resource("reregister-budget", 1)
        state = backend.get_state("reregister-budget")
        assert state.reserved == 0
        assert state.committed == Decimal("0.4")
        assert state.available == Decimal("0.6")

        with pytest.raises(ValueError, match="different capacity"):
            backend.register_resource("reregister-budget", 2)

    def test_resource_registration_rejects_invalid_capacity(self, tmp_path):
        backend = SingleHostLockBackend(str(tmp_path))
        for capacity in (-1, float("nan"), float("inf"), True):
            with pytest.raises(ValueError, match="finite and non-negative"):
                backend.register_resource("invalid-capacity", capacity)

    def test_decimal_capacity_at_adjusted_boundary_remains_usable(self, tmp_path):
        """A bounded 1E+128 capacity can still serve a small exact reservation."""
        backend = SingleHostLockBackend(str(tmp_path))
        capacity = Decimal("1e128")
        backend.register_resource("boundary-budget", capacity)

        result = backend.reserve("boundary-key", Decimal("1"), "boundary-budget")
        assert result.status == "RESERVED"

        state = backend.get_state("boundary-budget")
        assert state.capacity == capacity
        assert state.reserved == Decimal("1")
        # The default Decimal context (precision 28) rounds this expected
        # value; compute the oracle at sufficient precision for 1E+128 - 1.
        with localcontext() as context:
            context.prec = 130
            expected_available = capacity - Decimal("1")
        assert state.available == expected_available


    def test_zero_decimal_exponent_does_not_poison_internal_precision(self):
        """A zero's scale is irrelevant to exact accounting arithmetic."""
        from tenir_governance.operations.budget import _decimal_subtract

        assert _decimal_subtract(Decimal("1e128"), Decimal("0e-129")) == Decimal("1e128")


    @pytest.mark.parametrize("value", ["1e-129", "1e999999", "0e-999999", "1" + "2" * 128])
    def test_budget_rejects_pathological_decimal_precision(self, tmp_path, value):
        backend = SingleHostLockBackend(str(tmp_path))
        with pytest.raises(ValueError, match="finite and non-negative"):
            backend.register_resource("pathological-capacity", Decimal(value))


    def test_replay_preserves_terminal_state_and_resource_identity(self, tmp_path):
        """An idempotency key cannot revive a released or cross-resource reservation."""
        backend = SingleHostLockBackend(str(tmp_path))
        backend.register_resource("identity-budget-a", 10)
        backend.register_resource("identity-budget-b", 10)

        first = backend.reserve("identity-key", 4, "identity-budget-a")
        assert first.status == "RESERVED"
        assert backend.release(first.reservation_id).status == "RELEASED"

        replay = backend.reserve("identity-key", 4, "identity-budget-a")
        assert replay.status == "RELEASED"
        assert replay.reservation_id == first.reservation_id

        cross_resource_replay = backend.reserve(
            "identity-key", 4, "identity-budget-b"
        )
        assert cross_resource_replay.status == "DUPLICATE_KEY"
        assert backend.get_state("identity-budget-a").reserved == 0
        assert backend.get_state("identity-budget-b").reserved == 0

    class SimulatedProcessCrash(BaseException):
        """Bypass ordinary exception compensation to model abrupt process death."""

    def test_crash_after_budget_reserve_is_recoverable(self, tmp_path, monkeypatch):
        """A process crash after budget mutation must not orphan reserved capacity."""
        backend = SingleHostLockBackend(str(tmp_path))
        backend.register_resource("crash-reserve-budget", Decimal("1"))

        def crash_before_registry_durability(_reservations):
            raise self.SimulatedProcessCrash("crash after budget write")

        monkeypatch.setattr(backend, "_persist_reservations", crash_before_registry_durability)
        with pytest.raises(self.SimulatedProcessCrash):
            backend.reserve("crash-reserve-key", Decimal("0.1"), "crash-reserve-budget")

        # The journal completes the operation when the budget mutation is
        # durable, then restores the matching registry record on restart.
        reopened = SingleHostLockBackend(str(tmp_path))
        state = reopened.get_state("crash-reserve-budget")
        registry = reopened._load_reservations()
        assert state.reserved == Decimal("0.1")
        assert state.available == Decimal("0.9")
        assert len(registry) == 1
        recovered_id, recovered_record = next(iter(registry.items()))
        assert recovered_record["state"] == "RESERVED"
        replay = reopened.reserve(
            "crash-reserve-key", Decimal("0.1"), "crash-reserve-budget"
        )
        assert replay.status == "RESERVED"
        assert replay.reservation_id == recovered_id

    def test_crash_after_budget_commit_is_recoverable(self, tmp_path, monkeypatch):
        """A crash between budget commit and registry write must reconcile on restart."""
        backend = SingleHostLockBackend(str(tmp_path))
        backend.register_resource("crash-commit-budget", Decimal("1"))
        reservation = backend.reserve(
            "crash-commit-key", Decimal("0.1"), "crash-commit-budget"
        )
        assert reservation.status == "RESERVED"

        def crash_after_budget_commit(_reservations):
            raise self.SimulatedProcessCrash("crash after budget commit")

        monkeypatch.setattr(backend, "_persist_reservations", crash_after_budget_commit)
        with pytest.raises(self.SimulatedProcessCrash):
            backend.commit(reservation.reservation_id)

        reopened = SingleHostLockBackend(str(tmp_path))
        state = reopened.get_state("crash-commit-budget")
        registry = reopened._load_reservations()
        assert registry[reservation.reservation_id]["state"] == "COMMITTED"
        assert state.reserved == 0
        assert Decimal(str(state.committed)) == Decimal("0.1")

    def test_crash_after_budget_release_is_recoverable(self, tmp_path, monkeypatch):
        """A crash between budget release and registry write must be idempotently recoverable."""
        backend = SingleHostLockBackend(str(tmp_path))
        backend.register_resource("crash-release-budget", Decimal("1"))
        reservation = backend.reserve(
            "crash-release-key", Decimal("0.1"), "crash-release-budget"
        )
        assert reservation.status == "RESERVED"
        other = backend.reserve(
            "crash-release-other-key", Decimal("0.2"), "crash-release-budget"
        )
        assert other.status == "RESERVED"

        def crash_after_budget_release(_reservations):
            raise self.SimulatedProcessCrash("crash after budget release")

        monkeypatch.setattr(backend, "_persist_reservations", crash_after_budget_release)
        with pytest.raises(self.SimulatedProcessCrash):
            backend.release(reservation.reservation_id)

        reopened = SingleHostLockBackend(str(tmp_path))
        state = reopened.get_state("crash-release-budget")
        registry = reopened._load_reservations()
        assert registry[reservation.reservation_id]["state"] == "RELEASED"
        assert registry[other.reservation_id]["state"] == "RESERVED"
        # Recovery must not apply the release twice or consume another
        # reservation's aggregate budget on the same resource.
        assert Decimal(str(state.reserved)) == Decimal("0.2")
        assert state.committed == 0

    def test_reserve_persistence_failure_compensates_budget(self, tmp_path, monkeypatch):
        """A failed reservation-registry write must not leak reserved capacity."""
        backend = SingleHostLockBackend(str(tmp_path))
        backend.register_resource("persist-failure-budget", 1)

        def fail_persist(_reservations):
            raise OSError("simulated disk write failure")

        monkeypatch.setattr(backend, "_persist_reservations", fail_persist)
        failed = backend.reserve(
            "persist-failure-key", Decimal("0.1"), "persist-failure-budget"
        )
        assert failed.status == "ERROR"

        state = backend.get_state("persist-failure-budget")
        assert state.reserved == 0
        assert state.committed == 0
        assert state.available == 1

        # Once persistence recovers, the same key can reserve exactly once.
        monkeypatch.undo()
        retried = backend.reserve(
            "persist-failure-key", Decimal("0.1"), "persist-failure-budget"
        )
        assert retried.status == "RESERVED"
        state = backend.get_state("persist-failure-budget")
        assert state.reserved == Decimal("0.1")
        assert state.reserved + state.committed <= state.capacity

    def test_reserve_terminates_within_T_max(self, tmp_path):
        """reserve() must complete within T_max."""
        backend = SingleHostLockBackend(str(tmp_path))
        backend.register_resource("res1", 100)

        start = time.monotonic()
        result = backend.reserve("key1", 30, "res1")
        elapsed = time.monotonic() - start

        assert result.status == "RESERVED"
        assert elapsed < self.T_MAX, f"reserve took {elapsed:.3f}s, expected < {self.T_MAX}s"

    def test_commit_terminates_within_T_max(self, tmp_path):
        """commit() must complete within T_max."""
        backend = SingleHostLockBackend(str(tmp_path))
        backend.register_resource("res1", 100)
        r = backend.reserve("key1", 30, "res1")

        start = time.monotonic()
        result = backend.commit(r.reservation_id)
        elapsed = time.monotonic() - start

        assert result.status == "COMMITTED"
        assert elapsed < self.T_MAX, f"commit took {elapsed:.3f}s, expected < {self.T_MAX}s"

    def test_release_terminates_within_T_max(self, tmp_path):
        """release() must complete within T_max."""
        backend = SingleHostLockBackend(str(tmp_path))
        backend.register_resource("res1", 100)
        r = backend.reserve("key1", 30, "res1")

        start = time.monotonic()
        result = backend.release(r.reservation_id)
        elapsed = time.monotonic() - start

        assert result.status == "RELEASED"
        assert elapsed < self.T_MAX, f"release took {elapsed:.3f}s, expected < {self.T_MAX}s"

    def test_get_state_terminates_within_T_max(self, tmp_path):
        """get_state() must complete within T_max (it's a pure read)."""
        backend = SingleHostLockBackend(str(tmp_path))
        backend.register_resource("res1", 100)

        start = time.monotonic()
        state = backend.get_state("res1")
        elapsed = time.monotonic() - start

        assert state is not None
        assert elapsed < self.T_MAX, f"get_state took {elapsed:.3f}s, expected < {self.T_MAX}s"

    def test_concurrent_operations_all_terminate(self, tmp_path):
        """Multiple concurrent operations must all terminate within T_max.

        This is the core liveness test: under quorum (single-host, always
        available), no operation should hang indefinitely.
        """
        backend = SingleHostLockBackend(str(tmp_path))
        backend.register_resource("res1", 1000)

        results = []
        errors = []
        barrier = threading.Barrier(10)

        def worker(i):
            try:
                barrier.wait(timeout=2.0)
                start = time.monotonic()
                r = backend.reserve(f"key-{i}", 10, "res1")
                elapsed = time.monotonic() - start
                results.append((r.status, elapsed))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        start_all = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        elapsed_all = time.monotonic() - start_all

        assert not errors, f"Errors in workers: {errors}"
        assert len(results) == 10, f"Only {len(results)}/10 operations completed"

        # All must terminate within T_max
        for status, elapsed in results:
            assert status in ("RESERVED", "INSUFFICIENT_BUDGET"), f"Unexpected status: {status}"
            assert elapsed < self.T_MAX, f"Operation took {elapsed:.3f}s, expected < {self.T_MAX}s"

        # Total wall time should be reasonable (not 10 * T_max)
        assert elapsed_all < self.T_MAX * 2, f"Total time {elapsed_all:.3f}s too long"

    def test_max_latency_under_load(self, tmp_path):
        """Under concurrent load, max latency must stay under T_max."""
        backend = SingleHostLockBackend(str(tmp_path))
        backend.register_resource("res1", 10000)

        latencies = []
        barrier = threading.Barrier(20)

        def worker(i):
            barrier.wait(timeout=2.0)
            start = time.monotonic()
            r = backend.reserve(f"key-{i}", 10, "res1")
            latencies.append(time.monotonic() - start)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(latencies) == 20
        max_latency = max(latencies)
        assert max_latency < self.T_MAX, f"Max latency {max_latency:.3f}s exceeds T_max={self.T_MAX}s"


class TestReconciliationLiveness:
    """Verify that reconciliation terminates (C2.3)."""

    def test_reconcile_commit_crash_recovers_budget_and_registry(self, tmp_path, monkeypatch):
        class SimulatedCrash(BaseException):
            pass

        backend = SingleHostLockBackend(
            str(tmp_path), provider_reconcile=lambda key: True
        )
        backend.register_resource("reconcile-commit-budget", Decimal("1"))
        reservation = backend.reserve(
            "reconcile-commit-key", Decimal("0.1"), "reconcile-commit-budget"
        )
        assert reservation.status == "RESERVED"

        def crash_before_registry_write(_reservations):
            raise SimulatedCrash("crash after reconciliation budget commit")

        monkeypatch.setattr(backend, "_persist_reservations", crash_before_registry_write)
        with pytest.raises(SimulatedCrash):
            backend.reconcile("reconcile-commit-key")

        reopened = SingleHostLockBackend(str(tmp_path))
        state = reopened.get_state("reconcile-commit-budget")
        record = reopened._load_reservations()[reservation.reservation_id]
        assert record["state"] == "COMMITTED"
        assert state.reserved == 0
        assert Decimal(str(state.committed)) == Decimal("0.1")
        assert not (tmp_path / "transaction.json").exists()

    def test_reconcile_release_crash_recovers_without_double_release(self, tmp_path, monkeypatch):
        class SimulatedCrash(BaseException):
            pass

        backend = SingleHostLockBackend(
            str(tmp_path), provider_reconcile=lambda key: False
        )
        backend.register_resource("reconcile-release-budget", Decimal("1"))
        reservation = backend.reserve(
            "reconcile-release-key", Decimal("0.1"), "reconcile-release-budget"
        )
        other = backend.reserve(
            "reconcile-release-other-key", Decimal("0.2"), "reconcile-release-budget"
        )
        assert reservation.status == other.status == "RESERVED"

        def crash_before_registry_write(_reservations):
            raise SimulatedCrash("crash after reconciliation budget release")

        monkeypatch.setattr(backend, "_persist_reservations", crash_before_registry_write)
        with pytest.raises(SimulatedCrash):
            backend.reconcile("reconcile-release-key")

        reopened = SingleHostLockBackend(str(tmp_path))
        state = reopened.get_state("reconcile-release-budget")
        registry = reopened._load_reservations()
        assert registry[reservation.reservation_id]["state"] == "RELEASED"
        assert registry[other.reservation_id]["state"] == "RESERVED"
        assert Decimal(str(state.reserved)) == Decimal("0.2")
        assert state.committed == 0
        assert not (tmp_path / "transaction.json").exists()

    def test_reconcile_confirmed_terminates(self, tmp_path):
        """reconcile() with provider returning True must terminate."""
        def provider(key):
            return True

        backend = SingleHostLockBackend(str(tmp_path), provider_reconcile=provider)
        backend.register_resource("res1", 100)
        backend.reserve("key1", 30, "res1")

        start = time.monotonic()
        result = backend.reconcile("key1")
        elapsed = time.monotonic() - start

        assert result.status == "CONFIRMED"
        assert elapsed < 5.0, f"reconcile took {elapsed:.3f}s"

    def test_reconcile_failed_terminates(self, tmp_path):
        """reconcile() with provider returning False must terminate."""
        def provider(key):
            return False

        backend = SingleHostLockBackend(str(tmp_path), provider_reconcile=provider)
        backend.register_resource("res1", 100)
        backend.reserve("key1", 30, "res1")

        start = time.monotonic()
        result = backend.reconcile("key1")
        elapsed = time.monotonic() - start

        assert result.status == "FAILED"
        assert elapsed < 5.0

    def test_reconcile_unresolved_terminates(self, tmp_path):
        """reconcile() with no provider must terminate (returns UNRESOLVED)."""
        backend = SingleHostLockBackend(str(tmp_path))
        backend.register_resource("res1", 100)
        backend.reserve("key1", 30, "res1")

        start = time.monotonic()
        result = backend.reconcile("key1")
        elapsed = time.monotonic() - start

        assert result.status == "UNRESOLVED"
        assert elapsed < 5.0

    def test_reconcile_not_found_terminates(self, tmp_path):
        """reconcile() for non-existent key must terminate immediately."""
        backend = SingleHostLockBackend(str(tmp_path))
        backend.register_resource("res1", 100)

        start = time.monotonic()
        result = backend.reconcile("nonexistent")
        elapsed = time.monotonic() - start

        assert result.status == "NOT_FOUND"
        assert elapsed < 1.0, f"NOT_FOUND took {elapsed:.3f}s, expected < 1s"

    def test_reconcile_provider_timeout_returns_unresolved(self, tmp_path):
        """If provider hangs, reconcile should not block indefinitely.

        Note: In the current implementation, if the provider callable blocks,
        reconcile() will also block. This test documents that limitation.
        A production implementation should use a timeout on the provider call.
        """
        def slow_provider(key):
            time.sleep(0.1)  # Simulate slow provider
            return None  # Indeterminate

        backend = SingleHostLockBackend(str(tmp_path), provider_reconcile=slow_provider)
        backend.register_resource("res1", 100)
        backend.reserve("key1", 30, "res1")

        start = time.monotonic()
        result = backend.reconcile("key1")
        elapsed = time.monotonic() - start

        assert result.status == "UNRESOLVED"
        assert elapsed < 2.0, f"Slow provider reconcile took {elapsed:.3f}s"


class TestLivenessConditional:
    """Verify that liveness is correctly conditional (C2.2)."""

    def test_operations_block_on_insufficient_budget(self, tmp_path):
        """When budget is exhausted, reserve() returns immediately (not hang).

        This is safety, not liveness violation: the operation terminates
        with INSUFFICIENT_BUDGET, it doesn't block.
        """
        backend = SingleHostLockBackend(str(tmp_path))
        backend.register_resource("res1", 100)
        backend.reserve("key1", 100, "res1")  # Exhaust budget

        start = time.monotonic()
        result = backend.reserve("key2", 1, "res1")
        elapsed = time.monotonic() - start

        assert result.status == "INSUFFICIENT_BUDGET"
        assert elapsed < 1.0, f"Insufficient budget check took {elapsed:.3f}s"

    def test_commit_nonexistent_returns_immediately(self, tmp_path):
        """commit() on non-existent reservation returns immediately."""
        backend = SingleHostLockBackend(str(tmp_path))
        backend.register_resource("res1", 100)

        start = time.monotonic()
        result = backend.commit("nonexistent-id")
        elapsed = time.monotonic() - start

        assert result.status == "NOT_FOUND"
        assert elapsed < 1.0

    def test_double_commit_returns_immediately(self, tmp_path):
        """Already-committed reservation returns ALREADY_COMMITTED immediately."""
        backend = SingleHostLockBackend(str(tmp_path))
        backend.register_resource("res1", 100)
        r = backend.reserve("key1", 30, "res1")
        backend.commit(r.reservation_id)

        start = time.monotonic()
        result = backend.commit(r.reservation_id)
        elapsed = time.monotonic() - start

        assert result.status == "ALREADY_COMMITTED"
        assert elapsed < 1.0


class TestLinearizabilityWithLiveness:
    """Verify that linearizability holds under concurrent load (C1 + C2)."""

    def test_concurrent_reserve_commit_linearizable(self, tmp_path):
        """Concurrent reserve+commit operations should be linearizable.

        All operations should terminate (liveness) and the final state
        should be consistent (safety/linearizability).
        """
        backend = SingleHostLockBackend(str(tmp_path))
        backend.register_resource("res1", 100)

        num_ops = 20
        barrier = threading.Barrier(num_ops)
        results = []

        def worker(i):
            barrier.wait(timeout=2.0)
            r = backend.reserve(f"key-{i}", 10, "res1")
            if r.status == "RESERVED":
                backend.commit(r.reservation_id)
                results.append(("committed", i))
            else:
                results.append(("blocked", i))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_ops)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # All operations must terminate (liveness)
        assert len(results) == num_ops, f"Only {len(results)}/{num_ops} completed"

        # At most 10 should commit (capacity=100, each reserves 10)
        committed = [r for r in results if r[0] == "committed"]
        assert len(committed) <= 10, f"Too many commits: {len(committed)}"

        # Final state must be consistent (safety)
        state = backend.get_state("res1")
        assert state.committed <= state.capacity, "I8 violated"
        assert state.committed == len(committed) * 10, \
            f"committed={state.committed}, expected {len(committed) * 10}"
        assert state.reserved == 0, f"reserved should be 0 after all commits, got {state.reserved}"

class SimulatedRefundCrash(BaseException):
    """Model abrupt process termination without ordinary exception cleanup."""


def _committed_refund_reservation(tmp_path):
    backend = SingleHostLockBackend(str(tmp_path))
    backend.register_resource("refund-budget", Decimal("100"))
    reservation = backend.reserve("refund-key", Decimal("10"), "refund-budget")
    assert reservation.status == "RESERVED"
    assert backend.commit(reservation.reservation_id).status == "COMMITTED"
    return backend, reservation.reservation_id


def test_refund_is_durable_and_idempotent(tmp_path):
    backend, reservation_id = _committed_refund_reservation(tmp_path)

    first = backend.refund(reservation_id)
    replay = backend.refund(reservation_id)

    assert first.status == "REFUNDED"
    assert replay.status == "ALREADY_REFUNDED"
    state = backend.get_state("refund-budget")
    assert state.reserved == Decimal("0")
    assert state.committed == Decimal("0")
    reservation = next(
        item for item in state.reservations
        if item["reservation_id"] == reservation_id
    )
    assert reservation["state"] == "REFUNDED"


def test_crash_after_refund_budget_write_recovers_without_double_refund(
    tmp_path, monkeypatch,
):
    backend, reservation_id = _committed_refund_reservation(tmp_path)

    def crash_before_reservation_registry_write(_reservations):
        raise SimulatedRefundCrash("crash after durable budget refund")

    monkeypatch.setattr(
        backend, "_persist_reservations", crash_before_reservation_registry_write,
    )
    with pytest.raises(SimulatedRefundCrash):
        backend.refund(reservation_id)

    reopened = SingleHostLockBackend(str(tmp_path))
    state = reopened.get_state("refund-budget")
    assert state.reserved == Decimal("0")
    assert state.committed == Decimal("0")
    reservation = next(
        item for item in state.reservations
        if item["reservation_id"] == reservation_id
    )
    assert reservation["state"] == "REFUNDED"
    assert reopened.refund(reservation_id).status == "ALREADY_REFUNDED"
    final_state = reopened.get_state("refund-budget")
    assert final_state.reserved == Decimal("0")
    assert final_state.committed == Decimal("0")


def test_operation_record_persists_backend_reservation_identity(tmp_path):
    """Recovery must bind registry ownership to one durable backend reservation."""
    from tenir_governance.operations.registry import (
        DurableOperationRegistry,
        OperationRecord,
        OperationState,
    )

    registry = DurableOperationRegistry(str(tmp_path / "operations.json"))
    record = OperationRecord(
        idempotency_key="op-ownership-1",
        passage_id="passage-1",
        agent_id="agent-1",
        action_type="transfer",
        amount=10.0,
        resource_id="refund-budget",
        state=OperationState.RESERVED,
        budget_reserved=True,
        reservation_id="backend-reservation-123",
    )
    assert registry.register(record)

    reopened = DurableOperationRegistry(str(tmp_path / "operations.json"))
    recovered = reopened.get("op-ownership-1")
    assert recovered is not None
    assert recovered.reservation_id == "backend-reservation-123"
    assert recovered.budget_reserved is True
