"""RaftConsensusBackend — multi-host consensus via Raft (pysyncobj).

This module provides a multi-host ConsensusBackend adapter that uses the Raft
consensus algorithm (via pysyncobj) for replicated state. It implements the
same ConsensusBackend interface as SingleHostLockBackend but provides true
cross-host fault tolerance (C3).

Architecture:
    - Budget state (capacity, reserved, committed) is replicated across all
      Raft nodes via @replicated commands.
    - Reservations registry (key → reservation_id → state) is replicated.
    - I8 is enforced on the leader before the command is committed. If the
      leader crashes mid-replication, the command may be in UNKNOWN state —
      reconcile() handles this.
    - get_state() reads local replicated state (eventually consistent on
      followers, linearizable on leader).

Conformance:
    C1 (safety): ✓ — Raft provides linearizability for committed commands
    I8 (budget): ✓ — enforced on leader before replication
    C2 (liveness): ✓ — operations terminate under healthy quorum
    C3 (fault tolerance): ✓ — tolerates f crash-stops with n ≥ 2f+1

Requirements:
    pip install pysyncobj

Limitations:
    - No byzantine fault tolerance (Raft is crash-only)
    - Network partitions: safety maintained, liveness requires quorum
    - reconcile() depends on provider availability
    - pysyncobj uses pickle for serialization (not cryptographically signed)
"""
# Profile: agentic-governance-profile-v1.0.0
# Compliance class: profile-bound
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from decimal import Decimal, InvalidOperation, localcontext
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

try:
    from pysyncobj import SyncObj, SyncObjConf, replicated, FAIL_REASON
except ImportError:
    SyncObj = None
    SyncObjConf = None
    replicated = None
    FAIL_REASON = None

# Re-use the result types from consensus_backend for interface compatibility
from tenir_governance.operations.consensus_backend import (
    ReservationResult,
    CommitResult,
    ReleaseResult,
    ReconciliationResult,
    ConsensusState,
)


def _decimal_precision(*values: Decimal) -> int:
    """Choose enough precision to preserve exact finite Decimal arithmetic."""
    highest_adjusted = max((value.adjusted() for value in values if value), default=0)
    lowest_exponent = min((value.as_tuple().exponent for value in values), default=0)
    return max(28, highest_adjusted - lowest_exponent + 2)


def _decimal_add(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = _decimal_precision(left, right)
        return left + right


def _decimal_subtract(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = _decimal_precision(left, right)
        return left - right


MAX_AMOUNT = Decimal("1000000000000000000")


def _validated_amount(value, *, allow_zero=False):
    """Return a finite, bounded Decimal or None; reject bool and malformed input."""
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not amount.is_finite() or amount < 0 or (amount == 0 and not allow_zero):
        return None
    parts = amount.as_tuple()
    # Keep replicated arithmetic bounded; tiny scientific-notation values can
    # otherwise force extreme Decimal precision despite being below MAX_AMOUNT.
    if len(parts.digits) > 128 or abs(parts.exponent) > 128:
        return None
    if amount > MAX_AMOUNT:
        return None
    return amount


class _RaftStateMachine(SyncObj):
    """Replicated state machine for budget + reservations.

    All state-modifying methods are @replicated — they execute on the leader
    and are replicated to followers via Raft. I8 is enforced before the
    command is committed.

    The state machine maintains:
    - _resources: {resource_id: {capacity, reserved, committed}}
    - _reservations: {reservation_id: {key, amount, resource_id, state, logical timestamps, ...}}
    - _key_index: {key: reservation_id} for fast idempotency lookup
    """

    def __init__(self, selfAddr, otherAddrs, conf=None):
        super().__init__(selfAddr, otherAddrs, conf=conf)
        self._resources = {}
        self._reservations = {}
        self._key_index = {}
        self._logical_clock = 0
        self._incidents = []

    def _tick_logical(self):
        """Advance the replicated logical clock deterministically."""
        self._logical_clock += 1
        return self._logical_clock

    def _record_incident(self, code, reservation_id, previous_state):
        self._incidents.append({
            "code": code, "reservation_id": reservation_id,
            "previous_state": previous_state, "logical_time": self._tick_logical(),
        })

    def _normalize_resource_values(self, resource):
        """Normalize accounting values restored from pre-Decimal journals."""
        values = {
            name: _validated_amount(resource.get(name), allow_zero=True)
            for name in ("capacity", "reserved", "committed")
        }
        if any(value is None for value in values.values()):
            return False
        if _decimal_add(values["reserved"], values["committed"]) > values["capacity"]:
            return False
        resource.update(values)
        return True

    def _normalize_reservation_amount(self, record):
        amount = _validated_amount(record.get("amount"), allow_zero=True)
        if amount is None:
            return None
        record["amount"] = amount
        return amount

    @replicated
    def register_resource(self, resource_id, capacity):
        """Register once; never reset an existing resource's accounting."""
        normalized = _validated_amount(capacity, allow_zero=True)
        if normalized is None:
            return "INVALID_CAPACITY"
        existing = self._resources.get(resource_id)
        if existing is not None:
            return "ALREADY_REGISTERED" if existing["capacity"] == normalized else "RESOURCE_CONFLICT"
        self._resources[resource_id] = {"capacity": normalized, "reserved": Decimal("0"), "committed": Decimal("0")}
        return "REGISTERED"

    @replicated
    def reserve(self, key, amount, resource_id, reservation_id, _legacy_timestamp=None):
        """Reserve budget for an idempotency key.

        The optional legacy timestamp preserves replay compatibility with
        journals written by the previous five-argument command. New calls do
        not use wall-clock timestamps; logical time is assigned on replication.

        Returns: (status, reservation_id, amount_reserved)
        - RESERVED: reservation created or existing returned (idempotent)
        - INSUFFICIENT_BUDGET: not enough budget
        - DUPLICATE_KEY: key exists with different amount
        """
        normalized = _validated_amount(amount)
        if normalized is None:
            return ("INVALID_AMOUNT", None, None)
        if key in self._key_index:
            rid = self._key_index[key]
            rec = self._reservations.get(rid)
            if rec is None:
                return ("IDEMPOTENCY_INDEX_CORRUPTED", None, None)
            # Old journals may contain float accounting values. Normalize via
            # str() to avoid importing binary float artifacts into Decimal math.
            old_amount = _validated_amount(rec.get("amount"), allow_zero=True)
            if old_amount is None:
                return ("CORRUPT_RESERVATION_STATE", None, None)
            rec["amount"] = old_amount
            if old_amount == normalized and rec["resource_id"] == resource_id:
                return (rec["state"], rid, normalized)
            return ("DUPLICATE_KEY", None, None)
        res = self._resources.get(resource_id)
        if res is None:
            return ("UNKNOWN_RESOURCE", None, None)
        # Normalize persisted pre-Decimal state before arithmetic after upgrade.
        # Fail closed if any stored accounting value is invalid.
        if not self._normalize_resource_values(res):
            return ("CORRUPT_RESOURCE_STATE", None, None)
        if _decimal_add(_decimal_add(res["reserved"], res["committed"]), normalized) > res["capacity"]:
            return ("INSUFFICIENT_BUDGET", None, None)
        if reservation_id in self._reservations:
            return ("RESERVATION_ID_CONFLICT", None, None)
        self._reservations[reservation_id] = {
            "key": key, "amount": normalized, "resource_id": resource_id,
            "state": "RESERVED", "created_at_logical": self._tick_logical(),
        }
        self._key_index[key] = reservation_id
        res["reserved"] = _decimal_add(res["reserved"], normalized)
        return ("RESERVED", reservation_id, normalized)

    @replicated
    def commit(self, reservation_id):
        if reservation_id not in self._reservations:
            return "NOT_FOUND"
        rec = self._reservations[reservation_id]
        amount = self._normalize_reservation_amount(rec)
        if amount is None:
            return "CORRUPT_RESERVATION_STATE"
        if rec["state"] == "COMMITTED":
            return "ALREADY_COMMITTED"
        if rec["state"] != "RESERVED":
            return "INVALID_STATE"
        res = self._resources.get(rec["resource_id"])
        if res is None:
            return "ERROR"
        if not self._normalize_resource_values(res):
            return "CORRUPT_RESOURCE_STATE"
        if res["reserved"] < amount:
            return "INCONSISTENT_RESERVED_AMOUNT"
        res["reserved"] = _decimal_subtract(res["reserved"], amount)
        res["committed"] = _decimal_add(res["committed"], amount)
        rec["state"] = "COMMITTED"
        rec["committed_at_logical"] = self._tick_logical()
        return "COMMITTED"

    @replicated
    def release(self, reservation_id):
        if reservation_id not in self._reservations:
            return "NOT_FOUND"
        rec = self._reservations[reservation_id]
        amount = self._normalize_reservation_amount(rec)
        if amount is None:
            return "CORRUPT_RESERVATION_STATE"
        if rec["state"] == "COMMITTED":
            return "ALREADY_COMMITTED"
        if rec["state"] not in ("RESERVED", "UNKNOWN"):
            return "INVALID_STATE"
        res = self._resources.get(rec["resource_id"])
        if res is None:
            return "ERROR"
        if not self._normalize_resource_values(res):
            return "CORRUPT_RESOURCE_STATE"
        if rec["state"] in ("RESERVED", "UNKNOWN"):
            if res["reserved"] < amount:
                return "INCONSISTENT_RESERVED_AMOUNT"
            res["reserved"] = _decimal_subtract(res["reserved"], amount)
        rec["state"] = "RELEASED"
        rec["released_at_logical"] = self._tick_logical()
        return "RELEASED"

    @replicated
    def reconcile_commit(self, reservation_id):
        if reservation_id not in self._reservations:
            return "NOT_FOUND"
        rec = self._reservations[reservation_id]
        amount = self._normalize_reservation_amount(rec)
        if amount is None:
            return "CORRUPT_RESERVATION_STATE"
        if rec["state"] == "COMMITTED":
            return "ALREADY_COMMITTED"
        if rec["state"] != "RESERVED":
            if rec["state"] == "RELEASED":
                self._record_incident("LATE_EFFECT_AFTER_RELEASE", reservation_id, "RELEASED")
                return "RECONCILE_CONFLICT"
            return "INVALID_STATE"
        res = self._resources.get(rec["resource_id"])
        if res is None:
            return "ERROR"
        if not self._normalize_resource_values(res):
            return "CORRUPT_RESOURCE_STATE"
        if res["reserved"] < amount:
            self._record_incident("RESERVED_AMOUNT_MISSING", reservation_id, rec["state"])
            return "INCONSISTENT_RESERVED_AMOUNT"
        res["reserved"] = _decimal_subtract(res["reserved"], amount)
        res["committed"] = _decimal_add(res["committed"], amount)
        rec["state"] = "COMMITTED"
        rec["committed_at_logical"] = self._tick_logical()
        return "COMMITTED"

    @replicated
    def reconcile_release(self, reservation_id):
        if reservation_id not in self._reservations:
            return "NOT_FOUND"
        rec = self._reservations[reservation_id]
        amount = self._normalize_reservation_amount(rec)
        if amount is None:
            return "CORRUPT_RESERVATION_STATE"
        if rec["state"] == "COMMITTED":
            return "ALREADY_COMMITTED"
        if rec["state"] == "RELEASED":
            return "RELEASED"
        if rec["state"] not in ("RESERVED", "UNKNOWN"):
            return "INVALID_STATE"
        res = self._resources.get(rec["resource_id"])
        if res is None:
            return "ERROR"
        if not self._normalize_resource_values(res):
            return "CORRUPT_RESOURCE_STATE"
        if rec["state"] in ("RESERVED", "UNKNOWN"):
            if res["reserved"] < amount:
                return "INCONSISTENT_RESERVED_AMOUNT"
            res["reserved"] = _decimal_subtract(res["reserved"], amount)
        rec["state"] = "RELEASED"
        rec["released_at_logical"] = self._tick_logical()
        return "RELEASED"

    # --- Read-only methods (local, not replicated) ---

    def get_state_local(self, resource_id):
        """Return local state for a resource (no replication needed)."""
        res = self._resources.get(resource_id)
        if res is None:
            return {"available": 0, "reserved": 0, "committed": 0, "capacity": 0, "reservations": []}

        reservations = [
            {
                "reservation_id": rid,
                "key": rec["key"],
                "amount": _validated_amount(rec["amount"], allow_zero=True),
                "state": rec["state"],
            }
            for rid, rec in self._reservations.items()
            if rec.get("resource_id") == resource_id
        ]

        # Expose exact decimal values; float conversion loses precision for
        # large capacities (e.g. 1e18 with a 0.1 reservation).
        try:
            capacity = Decimal(str(res["capacity"]))
            reserved = Decimal(str(res["reserved"]))
            committed = Decimal(str(res["committed"]))
            if not all(v.is_finite() and v >= 0 for v in (capacity, reserved, committed)):
                raise InvalidOperation
            if _decimal_add(reserved, committed) > capacity:
                raise InvalidOperation
            normalized_reservations = [
                {**record, "amount": Decimal(str(record["amount"]))}
                for record in reservations
            ]
        except (InvalidOperation, ValueError, TypeError, KeyError) as exc:
            raise RuntimeError("Corrupt persisted Raft accounting state") from exc
        return {
            "available": _decimal_subtract(_decimal_subtract(capacity, reserved), committed),
            "reserved": reserved,
            "committed": committed,
            "capacity": capacity,
            "reservations": normalized_reservations,
        }

    def find_by_key(self, key):
        """Find a reservation by idempotency key (local read)."""
        rid = self._key_index.get(key)
        if rid is None:
            return None, None
        return rid, self._reservations.get(rid)


class RaftConsensusBackend:
    """Multi-host ConsensusBackend using Raft (pysyncobj).

    Implements the ConsensusBackend interface with true cross-host fault
    tolerance via Raft consensus. State is replicated across all nodes.

    This backend satisfies C1 (safety/linearizability), C2 (liveness),
    C3 (fault tolerance for crash-stop), and I8 (budget conservation)
    for multi-host deployments.

    Args:
        self_addr: address of this node (e.g., "localhost:5000")
        other_addrs: list of other node addresses
        store_dir: directory for durable state (Raft log)
        provider_reconcile: callable(key) -> (effect_observed: bool|None)
    """

    def __init__(self, self_addr, other_addrs, store_dir=None, provider_reconcile=None):
        if SyncObj is None:
            raise ImportError(
                "pysyncobj is required for RaftConsensusBackend. "
                "Install with: pip install pysyncobj"
            )

        self._self_addr = self_addr
        self._provider_reconcile = provider_reconcile
        self._lock = threading.Lock()

        # Configure pysyncobj
        conf = SyncObjConf(
            autoTick=True,
            autoTickPeriod=0.05,
            commandsQueueSize=100000,
        )

        # Set up journal file if store_dir provided
        if store_dir:
            os.makedirs(store_dir, exist_ok=True)
            journal_file = os.path.join(store_dir, "raft_journal.json")
            conf.journalFile = journal_file
            conf.fullDumpFile = os.path.join(store_dir, "raft_dump.json")

        self._raft = _RaftStateMachine(self_addr, list(other_addrs), conf=conf)

    def _get_leader_node(self):
        """Get the leader node address from Raft status."""
        status = self._raft.getStatus()
        leader = status.get("leader")
        if leader is None:
            return None
        return str(leader)

    def _is_my_state_current(self, reservation_id, timeout=5.0):
        """Wait until a reservation_id is visible in local replicated state."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if reservation_id in self._raft._reservations:
                return True
            time.sleep(0.1)
        return False

    def wait_ready(self, timeout=10.0):
        """Wait until the Raft cluster is ready (leader elected)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._raft.isReady():
                return True
            time.sleep(0.1)
        return False

    def is_leader(self):
        """Check if this node is the Raft leader."""
        status = self._raft.getStatus()
        leader = status.get("leader")
        if leader is None:
            return False
        # leader may be a TCPNode object or a string
        leader_str = str(leader)
        return leader_str == self._self_addr

    def get_status(self):
        """Return Raft status dict."""
        return self._raft.getStatus()

    def register_resource(self, resource_id, capacity):
        """Register without overwriting existing accounting state."""
        normalized = _validated_amount(capacity, allow_zero=True)
        if normalized is None:
            return "INVALID_CAPACITY"
        return self._raft.register_resource(resource_id, normalized, sync=True, timeout=5.0)

    def reserve(self, key, amount, resource_id) -> ReservationResult:
        """Reserve budget for an idempotency key (replicated via Raft).

        The reservation_id and timestamp are generated client-side and passed
        as arguments to the replicated command. This ensures deterministic
        replication: all nodes store the same reservation_id, so commit/release
        from any node can find the reservation.

        I8 is enforced on the leader before the command is committed.
        """
        normalized = _validated_amount(amount)
        if normalized is None:
            return ReservationResult(status="INVALID_AMOUNT")
        reservation_id = str(uuid.uuid4())
        try:
            result = self._raft.reserve(
                key, normalized, resource_id, reservation_id,
                sync=True, timeout=10.0
            )
            status, rid, amt = result
            return ReservationResult(status=status, reservation_id=rid, amount_reserved=amt)
        except Exception:
            return ReservationResult(status="ERROR")

    def commit(self, reservation_id) -> CommitResult:
        """Commit a reservation (replicated via Raft).

        On followers, sync=True may return NOT_FOUND if the reservation
        hasn't replicated to local state yet. We retry after waiting for
        replication to catch up.
        """
        for attempt in range(5):
            try:
                status = self._raft.commit(reservation_id, sync=True, timeout=10.0)
                if status == "NOT_FOUND" and attempt < 4:
                    # Reservation may not have replicated to this node yet.
                    # Wait for it, then retry.
                    self._is_my_state_current(reservation_id, timeout=3.0)
                    continue
                return CommitResult(status=status)
            except Exception:
                if attempt < 4:
                    time.sleep(0.2)
                else:
                    return CommitResult(status="ERROR")

    def release(self, reservation_id) -> ReleaseResult:
        """Release a non-committed reservation (replicated via Raft).

        On followers, sync=True may return NOT_FOUND if the reservation
        hasn't replicated to local state yet. We retry after waiting for
        replication to catch up.
        """
        for attempt in range(5):
            try:
                status = self._raft.release(reservation_id, sync=True, timeout=10.0)
                if status == "NOT_FOUND" and attempt < 4:
                    self._is_my_state_current(reservation_id, timeout=3.0)
                    continue
                return ReleaseResult(status=status)
            except Exception:
                if attempt < 4:
                    time.sleep(0.2)
                else:
                    return ReleaseResult(status="ERROR")

    def reconcile(self, key) -> ReconciliationResult:
        """Reconcile an UNKNOWN operation by querying the provider.

        If provider_reconcile is None, returns UNRESOLVED.
        If provider returns True (effect observed), commits the reservation.
        If provider returns False (no effect), releases the reservation.
        If provider returns None (indeterminate), returns UNRESOLVED.
        """
        with self._lock:
            rid, rec = self._raft.find_by_key(key)
            if rid is None or rec is None:
                return ReconciliationResult(status="NOT_FOUND")

            if rec["state"] == "COMMITTED":
                return ReconciliationResult(status="CONFIRMED", effect_observed=True)
            if rec["state"] == "RELEASED" and self._provider_reconcile is None:
                return ReconciliationResult(status="FAILED", effect_observed=False)
            if self._provider_reconcile is None:
                return ReconciliationResult(status="UNRESOLVED", effect_observed=None)

            try:
                effect_observed = self._provider_reconcile(key)
            except Exception:
                return ReconciliationResult(status="ERROR", effect_observed=None)

            if effect_observed is True:
                try:
                    status = self._raft.reconcile_commit(rid, sync=True, timeout=10.0)
                    if status in ("COMMITTED", "ALREADY_COMMITTED"):
                        return ReconciliationResult(status="CONFIRMED", effect_observed=True)
                    if status == "RECONCILE_CONFLICT":
                        return ReconciliationResult(status="RECONCILE_CONFLICT", effect_observed=True)
                    return ReconciliationResult(status="ERROR", effect_observed=True)
                except Exception:
                    return ReconciliationResult(status="ERROR", effect_observed=True)

            elif effect_observed is False:
                try:
                    status = self._raft.reconcile_release(rid, sync=True, timeout=10.0)
                    if status in ("RELEASED", "ALREADY_COMMITTED"):
                        if status == "ALREADY_COMMITTED":
                            return ReconciliationResult(status="CONFIRMED", effect_observed=True)
                        return ReconciliationResult(status="FAILED", effect_observed=False)
                    else:
                        return ReconciliationResult(status="ERROR", effect_observed=False)
                except Exception:
                    return ReconciliationResult(status="ERROR", effect_observed=False)

            else:
                return ReconciliationResult(status="UNRESOLVED", effect_observed=None)

    def get_state(self, resource_id) -> ConsensusState:
        """Return current state (local read from replicated state).

        On the leader, this is linearizable. On followers, it is
        eventually consistent (may lag behind the leader).
        """
        state = self._raft.get_state_local(resource_id)
        return ConsensusState(
            available=state["available"],
            reserved=state["reserved"],
            committed=state["committed"],
            capacity=state["capacity"],
            reservations=state["reservations"],
        )

    def destroy(self):
        """Clean up the Raft node."""
        self._raft.destroy()