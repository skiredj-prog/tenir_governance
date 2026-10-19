"""Budget coordination protocol — abstracts atomic reservation.

Per review 2 §1: a per-resource threading.Lock protects threads in one process,
but does NOT coordinate multiple worker processes, containers, hosts, or
external writers. This module abstracts the coordination mechanism behind a
protocol so the in-process lock can be replaced with a transactional database,
serializable transaction, or distributed coordination service without changing
the membrane or composition layer.

The protocol defines linearizability as the correctness concept: concurrent
operations should behave as though they occurred in a valid sequential order.
"""
# Profile: agentic-governance-profile-v1.0.0
# Compliance class: profile-bound
# This module is an extension on top of KB3 and TENIR 2.0 schema 1.2.
# It is NOT canonical, NOT doctrinal. It carries implementation-level
# governance enforcement authority only.
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
from typing import Protocol, runtime_checkable


@runtime_checkable
class BudgetCoordinator(Protocol):
    """Protocol for atomic budget coordination.

    Implementations must guarantee that check-and-reserve is atomic: no two
    concurrent callers can both reserve the same available budget.

    The in-process implementation (InProcessBudgetCoordinator) uses
    threading.Lock and is sufficient for single-process, multi-threaded
    deployments. For multi-process or distributed deployments, replace with a
    transactional database, serializable transaction, or distributed lock.
    """

    def reserve(self, resource_id: str, amount: float) -> bool:
        """Atomically check available capacity and reserve if sufficient.

        Returns True if the reservation succeeded, False if insufficient
        capacity. Must be atomic: no race condition between check and reserve.
        """
        ...

    def commit(self, resource_id: str, amount: float) -> bool:
        """Move a reservation from reserved to consumed. Returns True on success."""
        ...

    def release(self, resource_id: str, amount: float) -> None:
        """Release a reservation (passage blocked, failed, or cancelled)."""
        ...

    def get_utilization(self, resource_id: str) -> float | None:
        """Get current utilization fraction (0.0–1.0), or None if unavailable.

        Per review 2 §3: if the utilization measurement is missing, stale, or
        inconsistent, the composition layer must treat it as indeterminate and
        fail closed (HOLD), not silently proceed.
        """
        ...


class InProcessBudgetCoordinator:
    """In-process budget coordination using threading.Lock.

    LIMITATIONS (per review 2 §1):
        - Only coordinates threads sharing the same lock object in one process.
        - Does NOT coordinate multiple worker processes, containers, or hosts.
        - Does NOT provide durability: budget state is lost on process crash.
        - Does NOT provide linearizability across distributed deployments.

    For production deployments, replace with:
        - A transactional database with SELECT ... FOR UPDATE or equivalent.
        - A serializable transaction spanning the check and reserve.
        - A distributed coordination service (etcd, Zookeeper, Redis with
          atomic operations).
        - A single-writer sequencer process.
    """

    def __init__(self):
        self._resources: dict[str, any] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def register(self, resource) -> None:
        """Register once without replacing live accounting or synchronization."""
        existing = self._resources.get(resource.resource_id)
        if existing is not None:
            if existing is resource:
                return
            if float(existing.capacity) == float(resource.capacity):
                # Keep the original object and lock: its counters may already
                # represent live reservations or committed usage.
                return
            raise ValueError(
                f"Resource {resource.resource_id!r} is already registered "
                "with a different capacity."
            )
        self._resources[resource.resource_id] = resource
        self._locks.setdefault(resource.resource_id, threading.Lock())

    def get_resource(self, resource_id: str):
        return self._resources.get(resource_id)

    def get_lock(self, resource_id: str) -> threading.Lock:
        # Protect the check-and-create sequence; concurrent first access must
        # never hand out distinct locks for the same resource.
        with self._locks_guard:
            if resource_id not in self._locks:
                self._locks[resource_id] = threading.Lock()
            return self._locks[resource_id]

    def reserve(self, resource_id: str, amount: float) -> bool:
        resource = self._resources.get(resource_id)
        if resource is None:
            return False
        with self._locks[resource_id]:
            if resource.available >= amount:
                resource.reserved += amount
                return True
            return False

    def commit(self, resource_id: str, amount: float) -> bool:
        resource = self._resources.get(resource_id)
        if resource is None:
            return False
        with self._locks[resource_id]:
            if resource.reserved >= amount:
                resource.reserved -= amount
                resource.consumed += amount
                return True
            return False

    def release(self, resource_id: str, amount: float) -> None:
        resource = self._resources.get(resource_id)
        if resource is None:
            return
        with self._locks[resource_id]:
            resource.reserved = max(0.0, resource.reserved - amount)

    def get_utilization(self, resource_id: str) -> float | None:
        resource = self._resources.get(resource_id)
        if resource is None:
            return None
        with self._locks[resource_id]:
            if resource.capacity <= 0:
                return None  # indeterminate — fail closed
            return (resource.consumed + resource.reserved) / resource.capacity

import json
import math
import os
import tempfile

try:
    import fcntl
except ImportError:
    fcntl = None  # Windows fallback


MAX_DECIMAL_DIGITS = 128
MAX_DECIMAL_EXPONENT = 128
MAX_DECIMAL_ADJUSTED = 128


def _validate_decimal_bounds(value: Decimal) -> Decimal:
    """Bound accounting inputs to prevent pathological Decimal exponents."""
    parts = value.as_tuple()
    if (
        len(parts.digits) > MAX_DECIMAL_DIGITS
        or abs(parts.exponent) > MAX_DECIMAL_EXPONENT
        or (value and value.adjusted() > MAX_DECIMAL_ADJUSTED)
    ):
        raise ValueError("Decimal amount exceeds supported precision or exponent bounds.")
    return value


def _decimal_precision(*values: Decimal) -> int:
    """Choose enough precision to add/subtract bounded finite Decimals exactly."""
    if not values:
        return 28
    # Inputs are validated at the accounting boundary by _decimal(). Zero
    # values carry no magnitude, so their representation exponent must not
    # inflate precision requirements or make an otherwise valid operation
    # fail (e.g. a persisted Decimal zero with exponent -129).
    nonzero_values = [value for value in values if value]
    for value in nonzero_values:
        _validate_decimal_bounds(value)
    highest_adjusted = max((value.adjusted() for value in nonzero_values), default=0)
    lowest_exponent = min(
        (value.as_tuple().exponent for value in nonzero_values), default=0
    )
    return max(28, highest_adjusted - lowest_exponent + 2)


def _decimal_add(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = _decimal_precision(left, right)
        return left + right


def _decimal_subtract(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = _decimal_precision(left, right)
        return left - right


class DurableBudgetCoordinator:
    """Cross-process budget coordination using fcntl.flock + durable JSON state.

    Extends the InProcessBudgetCoordinator pattern to multiple processes on
    the same host. Budget state (reserved, consumed, capacity per resource_id)
    is persisted to a JSON file, and all read-check-write sequences are
    serialized via an exclusive file lock (fcntl.flock).

    LIMITATIONS:
        - Single-host only: fcntl.flock does not coordinate across hosts.
        - No WAL: a crash during _persist may lose in-flight state.
        - For multi-host, replace with a transactional database or
          distributed coordination service (etcd, Zookeeper).

    I8 invariant: reserved + consumed <= capacity, per resource_id, at all
    times. Enforced atomically under the file lock.

    ConsensusBackend conformance: this coordinator implements the
    reserve/commit/release/get_utilization interface defined in
    consensus-interface-v1.json. It satisfies C1 (safety) and I8 for
    single-host, multi-process deployments. It does NOT satisfy C2
    (liveness under partition) or C3 (fault tolerance across hosts).
    """

    def __init__(self, store_path: str | None = None):
        self._store_path = store_path
        self._lock = threading.Lock()
        self._resources: dict[str, dict] = {}  # Decimal counters: capacity, reserved, consumed
        if store_path:
            self._load()

    def register(self, resource) -> None:
        """Register once; preserve accounting for already-known resources."""
        capacity = getattr(resource, "capacity", None)
        if isinstance(capacity, bool):
            raise ValueError("Resource capacity must be finite and non-negative.")
        try:
            capacity = self._decimal(capacity)
        except (TypeError, ValueError, InvalidOperation) as exc:
            raise ValueError("Resource capacity must be finite and non-negative.") from exc
        if not capacity.is_finite() or capacity < 0:
            raise ValueError("Resource capacity must be finite and non-negative.")

        with self._lock:
            flock = self._lock_file()
            try:
                if self._store_path and os.path.exists(self._store_path):
                    self._load()
                existing = self._resources.get(resource.resource_id)
                if existing is not None:
                    try:
                        old_capacity = self._decimal(existing["capacity"])
                        reserved = self._decimal(existing["reserved"])
                        consumed = self._decimal(existing["consumed"])
                    except (KeyError, TypeError, ValueError, OverflowError, InvalidOperation) as exc:
                        raise RuntimeError("Corrupt persisted budget state; registration refused.") from exc
                    if (
                        not all(v.is_finite() and v >= 0 for v in (old_capacity, reserved, consumed))
                        or _decimal_add(reserved, consumed) > old_capacity
                    ):
                        raise RuntimeError("Corrupt persisted budget invariant; registration refused.")
                    if old_capacity != capacity:
                        raise ValueError(
                            f"Resource {resource.resource_id!r} is already registered "
                            "with a different capacity; explicit migration is required."
                        )
                    return

                reserved = getattr(resource, "reserved", 0.0)
                consumed = getattr(resource, "consumed", 0.0)
                try:
                    reserved, consumed = self._decimal(reserved), self._decimal(consumed)
                except (TypeError, ValueError, InvalidOperation) as exc:
                    raise ValueError("Initial resource counters must be finite and non-negative.") from exc
                if (
                    not reserved.is_finite() or not consumed.is_finite()
                    or reserved < 0 or consumed < 0 or _decimal_add(reserved, consumed) > capacity
                ):
                    raise ValueError("Initial resource counters violate the budget invariant.")
                self._resources[resource.resource_id] = {
                    "capacity": capacity, "reserved": reserved, "consumed": consumed,
                }
                self._persist()
            finally:
                if flock:
                    flock.close()

    @staticmethod
    def _decimal(value) -> Decimal:
        """Parse bounded accounting values without binary-float rounding."""
        if isinstance(value, bool):
            raise InvalidOperation("boolean is not a budget amount")
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
        return _validate_decimal_bounds(parsed)

    def get_resource(self, resource_id: str):
        """Return a lightweight view of the resource state."""
        with self._lock:
            return self._resources.get(resource_id)

    def _lock_file(self):
        """Acquire a cross-process exclusive lock on the budget store file."""
        if not self._store_path or fcntl is None:
            return None
        lock_path = self._store_path + ".lock"
        if not os.path.exists(lock_path):
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
            os.close(fd)
        f = open(lock_path, "r+")
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        return f

    def _load(self):
        """Load budget state from the durable store."""
        if not self._store_path or not os.path.exists(self._store_path):
            return
        try:
            with open(self._store_path, "r") as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                raise ValueError("budget store root must be an object")
            normalized = {}
            for resource_id, record in loaded.items():
                if not isinstance(record, dict):
                    raise ValueError(f"invalid record for resource {resource_id!r}")
                try:
                    capacity = self._decimal(record["capacity"])
                    reserved = self._decimal(record["reserved"])
                    consumed = self._decimal(record["consumed"])
                except (KeyError, TypeError, ValueError, OverflowError) as exc:
                    raise ValueError(f"invalid counters for resource {resource_id!r}") from exc
                if (
                    not all(v.is_finite() and v >= 0 for v in (capacity, reserved, consumed))
                    or _decimal_add(reserved, consumed) > capacity
                ):
                    raise ValueError(f"budget invariant invalid for resource {resource_id!r}")
                normalized[resource_id] = {
                    "capacity": capacity, "reserved": reserved, "consumed": consumed,
                }
            self._resources = normalized
        except (json.JSONDecodeError, ValueError, TypeError, InvalidOperation) as exc:
            # Corrupt durable state is not equivalent to an empty budget.
            # Refuse the operation so registration cannot silently reset it.
            raise RuntimeError("Corrupted durable budget store; refusing operation.") from exc

    def _persist(self):
        """Persist budget state atomically (temp file, fsync, rename)."""
        if not self._store_path:
            return
        tmp = self._store_path + ".tmp"
        serializable = {
            resource_id: {key: str(value) for key, value in record.items()}
            for resource_id, record in self._resources.items()
        }
        with open(tmp, "w") as f:
            json.dump(serializable, f)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, self._store_path)
        dir_fd = os.open(os.path.dirname(self._store_path) or ".", os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _reload_and_check(self, resource_id: str, amount: Decimal) -> bool:
        """Reload from disk under lock and check if amount is available.
        Returns True if available >= amount after reload.
        Does NOT mutate state — caller mutates after check passes.
        """
        if self._store_path and os.path.exists(self._store_path):
            self._load()
        r = self._resources.get(resource_id)
        if r is None:
            return False
        available = _decimal_subtract(_decimal_subtract(r["capacity"], r["reserved"]), r["consumed"])
        return available >= amount

    def reserve(self, resource_id: str, amount: float) -> bool:
        """Atomically reserve budget across processes.

        Acquires fcntl.flock, reloads from disk (another process may have
        changed state), checks availability, mutates, and persists.
        I8: reserved + consumed <= capacity is maintained.
        """
        try:
            amount = self._decimal(amount)
        except (TypeError, ValueError, InvalidOperation):
            return False
        if not amount.is_finite() or amount <= 0:
            return False
        with self._lock:
            flock = self._lock_file()
            try:
                if not self._reload_and_check(resource_id, amount):
                    return False
                r = self._resources[resource_id]
                r["reserved"] = _decimal_add(r["reserved"], amount)
                self._persist()
                return True
            finally:
                if flock:
                    flock.close()

    def commit(self, resource_id: str, amount: float) -> bool:
        """Move amount from reserved to consumed (cross-process atomic)."""
        try:
            amount = self._decimal(amount)
        except (TypeError, ValueError, InvalidOperation):
            return False
        if not amount.is_finite() or amount <= 0:
            return False
        with self._lock:
            flock = self._lock_file()
            try:
                if self._store_path and os.path.exists(self._store_path):
                    self._load()
                r = self._resources.get(resource_id)
                if r is None or r["reserved"] < amount:
                    return False
                r["reserved"] = _decimal_subtract(r["reserved"], amount)
                r["consumed"] = _decimal_add(r["consumed"], amount)
                self._persist()
                return True
            finally:
                if flock:
                    flock.close()

    def release(self, resource_id: str, amount: float) -> None:
        """Release a reservation (cross-process atomic)."""
        try:
            amount = self._decimal(amount)
        except (TypeError, ValueError, InvalidOperation):
            return
        if not amount.is_finite() or amount <= 0:
            return
        with self._lock:
            flock = self._lock_file()
            try:
                if self._store_path and os.path.exists(self._store_path):
                    self._load()
                r = self._resources.get(resource_id)
                if r is None:
                    return
                r["reserved"] = max(Decimal("0"), _decimal_subtract(r["reserved"], amount))
                self._persist()
            finally:
                if flock:
                    flock.close()

    def get_utilization(self, resource_id: str) -> float | None:
        """Get current utilization fraction (0.0–1.0), or None if unavailable."""
        with self._lock:
            flock = self._lock_file()
            try:
                if self._store_path and os.path.exists(self._store_path):
                    self._load()
                r = self._resources.get(resource_id)
                if r is None or r["capacity"] <= 0:
                    return None
                return float(_decimal_add(r["consumed"], r["reserved"]) / r["capacity"])
            finally:
                if flock:
                    flock.close()

    def get_state(self, resource_id: str) -> dict | None:
        """Return the full state for a resource (ConsensusBackend interface)."""
        with self._lock:
            flock = self._lock_file()
            try:
                if self._store_path and os.path.exists(self._store_path):
                    self._load()
                r = self._resources.get(resource_id)
                if r is None:
                    return None
                return {
                    # Preserve exact Decimal values at the state boundary. Callers
                    # that need display floats should convert explicitly; accounting
                    # and invariant checks must not silently round here.
                    "available": _decimal_subtract(_decimal_subtract(r["capacity"], r["reserved"]), r["consumed"]),
                    "reserved": r["reserved"],
                    "consumed": r["consumed"],
                    "capacity": r["capacity"],
                }
            finally:
                if flock:
                    flock.close()

    def get_state_exact(self, resource_id: str) -> dict | None:
        """Return exact Decimal counters for durable transaction recovery."""
        with self._lock:
            flock = self._lock_file()
            try:
                if self._store_path and os.path.exists(self._store_path):
                    self._load()
                r = self._resources.get(resource_id)
                if r is None:
                    return None
                return {
                    "available": _decimal_subtract(_decimal_subtract(r["capacity"], r["reserved"]), r["consumed"]),
                    "reserved": r["reserved"],
                    "consumed": r["consumed"],
                    "capacity": r["capacity"],
                }
            finally:
                if flock:
                    flock.close()
