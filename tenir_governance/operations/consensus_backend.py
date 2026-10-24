"""ConsensusBackend adapter — reference implementation.

This module provides a reference adapter that implements the ConsensusBackend
interface defined in reg-conformance/specs/consensus-interface-v1.json.

The reference adapter wraps DurableBudgetCoordinator (file-lock based) and
adds the reconcile() operation. It is the simplest conforming backend:
single-host, multi-process, crash-stop recovery, no byzantine tolerance.

For multi-host deployments, replace this adapter with one backed by etcd,
PostgreSQL, or a Raft implementation. The interface is the same.

Conformance:
    C1 (safety): ✓ — fcntl.flock serializes all operations linearizably
    I8 (budget): ✓ — enforced atomically under file lock
    C2 (liveness): ✓ — single-host, no partition possible within host
    C3 (fault tolerance): partial — crash-stop recovery via durable state,
        but no byzantine tolerance and no cross-host partition tolerance

Limitations:
    - Single-host only (fcntl.flock does not cross host boundaries)
    - No byzantine tolerance
    - No network partition tolerance (single host has no network partition)
    - reconcile() depends on provider availability
"""
# Profile: agentic-governance-profile-v1.0.0
# Compliance class: profile-bound
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
import uuid
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from tenir_governance.operations.budget import (
    DurableBudgetCoordinator, _decimal_add, _decimal_subtract,
)
from tenir_governance.operations.file_lock import acquire_file_lock


# --- Result types (matching consensus-interface-v1.json) ---

@dataclass
class ReservationResult:
    status: str  # RESERVED | INSUFFICIENT_BUDGET | DUPLICATE_KEY | ERROR
    reservation_id: str | None = None
    amount_reserved: int | float | Decimal | None = None


@dataclass
class CommitResult:
    status: str  # COMMITTED | NOT_FOUND | ALREADY_COMMITTED | ERROR


@dataclass
class ReleaseResult:
    status: str  # RELEASED | ALREADY_RELEASED | NOT_FOUND | ALREADY_COMMITTED | ERROR


@dataclass
class RefundResult:
    status: str  # REFUNDED | NOT_FOUND | ALREADY_REFUNDED | NOT_COMMITTED | ERROR


@dataclass
class ReconciliationResult:
    status: str  # CONFIRMED | FAILED | UNRESOLVED | NOT_FOUND | RECONCILE_CONFLICT | ERROR
    effect_observed: bool | None = None
    compensation_id: str | None = None


@dataclass
class ConsensusState:
    available: int | float | Decimal
    reserved: int | float | Decimal
    committed: int | float | Decimal
    capacity: int | float | Decimal
    reservations: list = field(default_factory=list)
    compensations: list = field(default_factory=list)


# --- ConsensusBackend Protocol (matches consensus-interface-v1.json) ---

@runtime_checkable
class ConsensusBackend(Protocol):
    """Interface de conformance pour tout backend de consensus distribué.

    Tout backend qui implémente cette interface et satisfait les propriétés
    C1/C2/C3/I8 (consensus-contract-v1.md) est conforme.
    """

    def reserve(self, key: str, amount: int | float | Decimal, resource_id: str) -> ReservationResult:
        """Réserver un montant budgétaire pour une clé d'idempotence."""
        ...

    def commit(self, reservation_id: str) -> CommitResult:
        """Commiter une réservation."""
        ...

    def release(self, reservation_id: str) -> ReleaseResult:
        """Libérer une réservation non commitée."""
        ...

    def refund(self, reservation_id: str) -> RefundResult:
        """Refund a committed reservation after authoritative non-execution proof."""
        ...

    def reconcile(self, key: str) -> ReconciliationResult:
        """Réconcilier une opération en état UNKNOWN."""
        ...

    def get_state(self, resource_id: str) -> ConsensusState:
        """Retourner l'état courant du backend (opération pure)."""
        ...


# --- Reference adapter: SingleHostLockBackend ---

class SingleHostLockBackend:
    """Reference ConsensusBackend using file-lock coordination.

    Wraps DurableBudgetCoordinator for reserve/commit/release and adds
    a reservation registry (key → reservation_id → state) persisted to disk.
    reconcile() is delegated to a provider callback.

    This is the simplest conforming backend. It satisfies C1 (safety) and
    I8 (budget conservation) for single-host, multi-process deployments.
    """

    def __init__(self, store_dir: str, provider_reconcile=None):
        """
        Args:
            store_dir: directory for durable state files
            provider_reconcile: callable(key) -> (effect_observed: bool|None)
                If None, reconcile() always returns UNRESOLVED.
        """
        self._store_dir = store_dir
        os.makedirs(store_dir, exist_ok=True)
        self._budget_store = os.path.join(store_dir, "budget.json")
        self._reservations_store = os.path.join(store_dir, "reservations.json")
        self._coordinator = DurableBudgetCoordinator(self._budget_store)
        self._provider_reconcile = provider_reconcile
        self._lock = threading.Lock()
        self._transaction_store = os.path.join(store_dir, "transaction.json")
        self._recover_pending_transaction()

    def register_resource(self, resource_id: str, capacity: int | float | Decimal):
        """Register a resource without resetting any existing budget counters."""
        if isinstance(capacity, bool):
            raise ValueError("Resource capacity must be finite and non-negative.")
        try:
            normalized_capacity = capacity if isinstance(capacity, Decimal) else Decimal(str(capacity))
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise ValueError("Resource capacity must be finite and non-negative.") from exc
        if not normalized_capacity.is_finite() or normalized_capacity < 0:
            raise ValueError("Resource capacity must be finite and non-negative.")

        # Delegate both idempotent re-registration and corruption checks to
        # the coordinator under its own durable lock; never short-circuit a
        # potentially corrupt persisted state here.
        resource = type("Resource", (), {
            "resource_id": resource_id,
            "capacity": normalized_capacity,
            "reserved": 0.0,
            "consumed": 0.0,
        })()
        self._coordinator.register(resource)

    def _load_reservations(self) -> dict:
        """Load reservation registry from disk."""
        if not os.path.exists(self._reservations_store):
            return {}
        try:
            with open(self._reservations_store, "r") as f:
                reservations = json.load(f)
            if not isinstance(reservations, dict):
                raise ValueError("reservation registry root must be an object")
            for reservation_id, record in reservations.items():
                if not isinstance(record, dict):
                    raise ValueError(f"invalid reservation record {reservation_id!r}")
                if not {"key", "amount", "resource_id", "state"}.issubset(record):
                    raise ValueError(f"incomplete reservation record {reservation_id!r}")
            return reservations
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            # A damaged idempotency registry cannot safely be treated as empty:
            # doing so could duplicate an existing reservation after restart.
            raise RuntimeError("Corrupted durable reservation registry; refusing operation.") from exc

    def _persist_reservations(self, reservations: dict):
        """Persist reservation registry atomically."""
        fd, tmp = tempfile.mkstemp(
            prefix="reservations.", suffix=".tmp", dir=self._store_dir,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(reservations, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self._reservations_store)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _persist_transaction(self, transaction: dict) -> None:
        """Durably record a single in-flight budget/registry transaction."""
        fd, tmp = tempfile.mkstemp(
            prefix="transaction.", suffix=".tmp", dir=self._store_dir,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(transaction, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self._transaction_store)
            if os.name != "nt":
                dir_fd = os.open(self._store_dir, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _clear_transaction(self) -> None:
        try:
            os.unlink(self._transaction_store)
        except FileNotFoundError:
            return
        if os.name != "nt":
            dir_fd = os.open(self._store_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    def _snapshot(self, resource_id: str) -> dict:
        state = self._coordinator.get_state_exact(resource_id)
        if state is None:
            raise RuntimeError(f"Cannot transact against unknown resource {resource_id!r}.")
        return {name: str(state[name]) for name in ("reserved", "consumed", "capacity")}

    def _recover_pending_transaction(self) -> None:
        """Replay the sole interrupted operation by comparing exact durable snapshots."""
        if not os.path.exists(self._transaction_store):
            return
        flock = self._lock_file()
        try:
            if not os.path.exists(self._transaction_store):
                return
            try:
                with open(self._transaction_store, "r") as f:
                    tx = json.load(f)
                required = {"operation", "reservation_id", "record", "before", "after"}
                if not isinstance(tx, dict) or not required.issubset(tx):
                    raise ValueError("incomplete transaction journal")
                # Validate the journal's operation and structure before replay.
                # Unknown/corrupted operations must fail closed, never be treated
                # as a generic commit/release mutation.
                if tx["operation"] not in {"reserve", "commit", "release", "refund"}:
                    raise ValueError("unknown transaction operation")
                if not isinstance(tx["reservation_id"], str) or not tx["reservation_id"]:
                    raise ValueError("invalid transaction reservation_id")
                if not isinstance(tx["record"], dict) or not isinstance(tx["record"].get("resource_id"), str):
                    raise ValueError("invalid transaction record/resource_id")
                for snapshot_name in ("before", "after"):
                    snapshot = tx[snapshot_name]
                    if not isinstance(snapshot, dict) or set(snapshot) != {"reserved", "consumed", "capacity"}:
                        raise ValueError(f"invalid {snapshot_name} budget snapshot")
                    for value in snapshot.values():
                        if not isinstance(value, str):
                            raise ValueError(f"invalid {snapshot_name} snapshot value")
                        parsed = Decimal(value)
                        if not parsed.is_finite() or parsed < 0:
                            raise ValueError(f"invalid {snapshot_name} snapshot amount")
                resource_id = tx["record"]["resource_id"]
                current = self._snapshot(resource_id)
            except (OSError, ValueError, TypeError, KeyError, ArithmeticError, json.JSONDecodeError) as exc:
                raise RuntimeError("Corrupted transaction journal; refusing recovery.") from exc

            if current == tx["after"]:
                reservations = self._load_reservations()
                rid = tx["reservation_id"]
                record = dict(tx["record"])
                if tx["operation"] == "reserve":
                    reservations[rid] = record
                else:
                    existing = reservations.get(rid)
                    if existing is None:
                        raise RuntimeError("Transaction journal references a missing reservation.")
                    existing["state"] = record["state"]
                    state_key = {
                        "commit": "committed_at",
                        "release": "released_at",
                        "refund": "refunded_at",
                    }.get(tx["operation"])
                    if state_key:
                        existing[state_key] = record[state_key]
                self._persist_reservations(reservations)
            elif current != tx["before"]:
                raise RuntimeError(
                    "Budget state matches neither side of the pending transaction; manual reconciliation required."
                )
            self._clear_transaction()
        finally:
            if flock:
                flock.close()

    def _lock_file(self):
        """Acquire cross-process exclusive lock on reservations store."""
        lock_path = self._reservations_store + ".lock"
        return acquire_file_lock(lock_path)

    def reserve(self, key: str, amount: int | float | Decimal, resource_id: str) -> ReservationResult:
        """Reserve budget for an idempotency key.

        Amounts remain Decimal internally and are stored as decimal strings
        in JSON so neither persistence nor idempotency checks round through
        binary floating-point.
        """
        if isinstance(amount, bool):
            return ReservationResult(status="INVALID_AMOUNT")
        try:
            amount = amount if isinstance(amount, Decimal) else Decimal(str(amount))
        except (TypeError, ValueError, ArithmeticError):
            return ReservationResult(status="INVALID_AMOUNT")
        if not amount.is_finite() or amount <= 0:
            return ReservationResult(status="INVALID_AMOUNT")
        with self._lock:
            flock = self._lock_file()
            try:
                reservations = self._load_reservations()

                # Check for existing reservation with this key
                for rid, rec in reservations.items():
                    if rec.get("key") == key:
                        # Idempotency binds the key to the full immutable
                        # reservation identity and returns its actual lifecycle
                        # state; replay must never revive a terminal reservation.
                        if (
                            Decimal(str(rec.get("amount"))) == amount
                            and rec.get("resource_id") == resource_id
                        ):
                            return ReservationResult(
                                status=rec.get("state", "ERROR"),
                                reservation_id=rid,
                                amount_reserved=amount,
                            )
                        return ReservationResult(status="DUPLICATE_KEY")

                reservation_id = str(uuid.uuid4())
                record = {
                    "key": key,
                    "amount": str(amount),
                    "resource_id": resource_id,
                    "state": "RESERVED",
                    "created_at": time.time(),
                }
                before = self._snapshot(resource_id)
                after = dict(before)
                after["reserved"] = str(_decimal_add(Decimal(before["reserved"]), amount))
                self._persist_transaction({
                    "operation": "reserve", "reservation_id": reservation_id,
                    "record": record, "before": before, "after": after,
                })
                if not self._coordinator.reserve(resource_id, amount):
                    self._clear_transaction()
                    return ReservationResult(status="INSUFFICIENT_BUDGET")
                reservations[reservation_id] = record
                try:
                    self._persist_reservations(reservations)
                except Exception:
                    reservations.pop(reservation_id, None)
                    try:
                        self._coordinator.release(resource_id, amount)
                        self._clear_transaction()
                    except Exception:
                        return ReservationResult(status="ERROR")
                    return ReservationResult(status="ERROR")
                self._clear_transaction()

                return ReservationResult(
                    status="RESERVED",
                    reservation_id=reservation_id,
                    amount_reserved=amount,
                )
            finally:
                if flock:
                    flock.close()

    def commit(self, reservation_id: str) -> CommitResult:
        """Commit a reservation: transfer from reserved to consumed."""
        with self._lock:
            flock = self._lock_file()
            try:
                reservations = self._load_reservations()

                if reservation_id not in reservations:
                    return CommitResult(status="NOT_FOUND")

                rec = reservations[reservation_id]
                if rec["state"] == "COMMITTED":
                    return CommitResult(status="ALREADY_COMMITTED")
                if rec["state"] != "RESERVED":
                    return CommitResult(status="ERROR")

                amount = Decimal(str(rec["amount"]))
                before = self._snapshot(rec["resource_id"])
                if Decimal(before["reserved"]) < amount:
                    return CommitResult(status="ERROR")
                after = dict(before)
                after["reserved"] = str(_decimal_subtract(Decimal(before["reserved"]), amount))
                after["consumed"] = str(_decimal_add(Decimal(before["consumed"]), amount))
                updated = dict(rec)
                updated["state"] = "COMMITTED"
                updated["committed_at"] = time.time()
                self._persist_transaction({
                    "operation": "commit", "reservation_id": reservation_id,
                    "record": updated, "before": before, "after": after,
                })
                if not self._coordinator.commit(rec["resource_id"], amount):
                    self._clear_transaction()
                    return CommitResult(status="ERROR")
                reservations[reservation_id] = updated
                self._persist_reservations(reservations)
                self._clear_transaction()

                return CommitResult(status="COMMITTED")
            finally:
                if flock:
                    flock.close()

    def release(self, reservation_id: str) -> ReleaseResult:
        """Release a non-committed reservation."""
        with self._lock:
            flock = self._lock_file()
            try:
                reservations = self._load_reservations()

                if reservation_id not in reservations:
                    return ReleaseResult(status="NOT_FOUND")

                rec = reservations[reservation_id]
                if rec["state"] == "COMMITTED":
                    return ReleaseResult(status="ALREADY_COMMITTED")
                if rec["state"] == "RELEASED":
                    return ReleaseResult(status="ALREADY_RELEASED")
                if rec["state"] != "RESERVED":
                    # UNKNOWN is ambiguous, not proof that capacity can be released.
                    return ReleaseResult(status="ERROR")

                amount = Decimal(str(rec["amount"]))
                before = self._snapshot(rec["resource_id"])
                if Decimal(before["reserved"]) < amount:
                    return ReleaseResult(status="ERROR")
                after = dict(before)
                after["reserved"] = str(_decimal_subtract(Decimal(before["reserved"]), amount))
                updated = dict(rec)
                updated["state"] = "RELEASED"
                updated["released_at"] = time.time()
                self._persist_transaction({
                    "operation": "release", "reservation_id": reservation_id,
                    "record": updated, "before": before, "after": after,
                })
                self._coordinator.release(rec["resource_id"], amount)
                reservations[reservation_id] = updated
                self._persist_reservations(reservations)
                self._clear_transaction()

                return ReleaseResult(status="RELEASED")
            finally:
                if flock:
                    flock.close()

    def refund(self, reservation_id: str) -> RefundResult:
        """Refund a committed reservation exactly once using the durable journal.

        Call only after authoritative evidence establishes that no external
        effect occurred. A replay never mutates budget counters twice.
        """
        with self._lock:
            flock = self._lock_file()
            try:
                reservations = self._load_reservations()
                if reservation_id not in reservations:
                    return RefundResult(status="NOT_FOUND")

                rec = reservations[reservation_id]
                if rec["state"] == "REFUNDED":
                    return RefundResult(status="ALREADY_REFUNDED")
                if rec["state"] != "COMMITTED":
                    return RefundResult(status="NOT_COMMITTED")

                amount = Decimal(str(rec["amount"]))
                before = self._snapshot(rec["resource_id"])
                if Decimal(before["consumed"]) < amount:
                    return RefundResult(status="ERROR")
                after = dict(before)
                after["consumed"] = str(
                    _decimal_subtract(Decimal(before["consumed"]), amount)
                )
                updated = dict(rec)
                updated["state"] = "REFUNDED"
                updated["refunded_at"] = time.time()
                self._persist_transaction({
                    "operation": "refund",
                    "reservation_id": reservation_id,
                    "record": updated,
                    "before": before,
                    "after": after,
                })
                if not self._coordinator._refund_committed(rec["resource_id"], amount):
                    self._clear_transaction()
                    return RefundResult(status="ERROR")
                reservations[reservation_id] = updated
                self._persist_reservations(reservations)
                self._clear_transaction()
                return RefundResult(status="REFUNDED")
            finally:
                if flock:
                    flock.close()

    def reconcile(self, key: str) -> ReconciliationResult:
        """Reconcile an UNKNOWN operation by querying the provider.

        If provider_reconcile is None, returns UNRESOLVED.
        If provider returns True (effect observed), commits the reservation.
        If provider returns False (no effect), releases the reservation.
        If provider returns None (indeterminate), returns UNRESOLVED.
        """
        with self._lock:
            flock = self._lock_file()
            try:
                reservations = self._load_reservations()

                # Find reservation by key in UNKNOWN state
                reservation_id = None
                for rid, rec in reservations.items():
                    if rec.get("key") == key and rec["state"] in ("UNKNOWN", "RESERVED"):
                        reservation_id = rid
                        break

                if reservation_id is None:
                    return ReconciliationResult(status="NOT_FOUND")

                if self._provider_reconcile is None:
                    # Mark as UNKNOWN for future reconciliation
                    reservations[reservation_id]["state"] = "UNKNOWN"
                    self._persist_reservations(reservations)
                    return ReconciliationResult(status="UNRESOLVED", effect_observed=None)

                try:
                    effect_observed = self._provider_reconcile(key)
                except Exception:
                    return ReconciliationResult(status="ERROR", effect_observed=None)

                rec = reservations[reservation_id]

                if effect_observed is True:
                    # Effect confirmed — journal the budget/registry transition
                    # so a crash between the two durable writes is recoverable.
                    amount = Decimal(str(rec["amount"]))
                    before = self._snapshot(rec["resource_id"])
                    if Decimal(before["reserved"]) < amount:
                        return ReconciliationResult(status="ERROR", effect_observed=True)
                    after = dict(before)
                    after["reserved"] = str(_decimal_subtract(Decimal(before["reserved"]), amount))
                    after["consumed"] = str(_decimal_add(Decimal(before["consumed"]), amount))
                    updated = dict(rec)
                    updated["state"] = "COMMITTED"
                    updated["committed_at"] = time.time()
                    self._persist_transaction({
                        "operation": "commit", "reservation_id": reservation_id,
                        "record": updated, "before": before, "after": after,
                    })
                    if not self._coordinator.commit(rec["resource_id"], amount):
                        self._clear_transaction()
                        return ReconciliationResult(status="ERROR", effect_observed=True)
                    reservations[reservation_id] = updated
                    self._persist_reservations(reservations)
                    self._clear_transaction()
                    return ReconciliationResult(status="CONFIRMED", effect_observed=True)

                elif effect_observed is False:
                    # No effect — journal the release so restart cannot double-release.
                    amount = Decimal(str(rec["amount"]))
                    before = self._snapshot(rec["resource_id"])
                    if Decimal(before["reserved"]) < amount:
                        return ReconciliationResult(status="ERROR", effect_observed=False)
                    after = dict(before)
                    after["reserved"] = str(_decimal_subtract(Decimal(before["reserved"]), amount))
                    updated = dict(rec)
                    updated["state"] = "RELEASED"
                    updated["released_at"] = time.time()
                    self._persist_transaction({
                        "operation": "release", "reservation_id": reservation_id,
                        "record": updated, "before": before, "after": after,
                    })
                    self._coordinator.release(rec["resource_id"], amount)
                    reservations[reservation_id] = updated
                    self._persist_reservations(reservations)
                    self._clear_transaction()
                    return ReconciliationResult(status="FAILED", effect_observed=False)

                else:
                    # Indeterminate — stay UNKNOWN
                    rec["state"] = "UNKNOWN"
                    self._persist_reservations(reservations)
                    return ReconciliationResult(status="UNRESOLVED", effect_observed=None)
            finally:
                if flock:
                    flock.close()

    def get_state(self, resource_id: str) -> ConsensusState:
        """Return current state (pure, no mutation)."""
        state = self._coordinator.get_state_exact(resource_id)
        if state is None:
            return ConsensusState(available=Decimal("0"), reserved=Decimal("0"),
                                  committed=Decimal("0"), capacity=Decimal("0"))

        reservations = self._load_reservations()
        res_list = [
            {
                "reservation_id": rid,
                "key": rec["key"],
                "amount": Decimal(str(rec["amount"])),
                "state": rec["state"],
            }
            for rid, rec in reservations.items()
            if rec.get("resource_id") == resource_id
        ]

        return ConsensusState(
            available=state["available"],
            reserved=state["reserved"],
            committed=state["consumed"],
            capacity=state["capacity"],
            reservations=res_list,
        )

# --- Backward-compatible alias (deprecated) ---
# FileLockConsensusBackend is the old name. It is kept for backward
# compatibility but is deprecated. Use SingleHostLockBackend instead.
# The name was changed because 'Consensus' in the class name could be
# confused with distributed consensus algorithms (Raft, Paxos, PBFT).
# This backend is a single-host file-lock coordination point, not a
# distributed consensus implementation.
FileLockConsensusBackend = SingleHostLockBackend
