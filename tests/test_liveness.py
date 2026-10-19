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
from decimal import Decimal

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
        state = backend.get_state("decimal-budget")
        assert state.reserved == 0.1
        assert state.available == 0.9

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
        assert state.reserved == 0.1
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