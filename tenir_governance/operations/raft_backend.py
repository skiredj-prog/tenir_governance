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


class _RaftStateMachine(SyncObj):
    """Replicated state machine for budget + reservations.

    All state-modifying methods are @replicated — they execute on the leader
    and are replicated to followers via Raft. I8 is enforced before the
    command is committed.

    The state machine maintains:
    - _resources: {resource_id: {capacity, reserved, committed}}
    - _reservations: {reservation_id: {key, amount, resource_id, state, created_at, ...}}
    - _key_index: {key: reservation_id} for fast idempotency lookup
    """

    def __init__(self, selfAddr, otherAddrs, conf=None):
        super().__init__(selfAddr, otherAddrs, conf=conf)
        self._resources = {}
        self._reservations = {}
        self._key_index = {}

    @replicated
    def register_resource(self, resource_id, capacity):
        """Register a resource with a capacity ceiling."""
        if resource_id not in self._resources:
            self._resources[resource_id] = {
                "capacity": float(capacity),
                "reserved": 0.0,
                "committed": 0.0,
            }
        return True

    @replicated
    def reserve(self, key, amount, resource_id, reservation_id, timestamp):
        """Reserve budget for an idempotency key.

        reservation_id and timestamp are generated client-side and passed as
        arguments to ensure deterministic replication (Raft requires all
        state machine commands to be deterministic).

        Returns: (status, reservation_id, amount_reserved)
        - RESERVED: reservation created or existing returned (idempotent)
        - INSUFFICIENT_BUDGET: not enough budget
        - DUPLICATE_KEY: key exists with different amount
        """
        # Check for existing reservation with this key
        if key in self._key_index:
            rid = self._key_index[key]
            rec = self._reservations[rid]
            if rec["amount"] == amount:
                return ("RESERVED", rid, amount)
            else:
                return ("DUPLICATE_KEY", None, None)

        # Check I8: reserved + committed + amount <= capacity
        res = self._resources.get(resource_id)
        if res is None:
            return ("ERROR", None, None)

        if res["reserved"] + res["committed"] + float(amount) > res["capacity"]:
            return ("INSUFFICIENT_BUDGET", None, None)

        # Create reservation with client-provided deterministic ID
        self._reservations[reservation_id] = {
            "key": key,
            "amount": amount,
            "resource_id": resource_id,
            "state": "RESERVED",
            "created_at": timestamp,
        }
        self._key_index[key] = reservation_id
        res["reserved"] += float(amount)

        return ("RESERVED", reservation_id, amount)

    @replicated
    def commit(self, reservation_id):
        """Commit a reservation: transfer from reserved to committed.

        Returns: status string
        - COMMITTED: successfully committed
        - NOT_FOUND: reservation doesn't exist
        - ALREADY_COMMITTED: already committed
        - ERROR: invalid state
        """
        if reservation_id not in self._reservations:
            return "NOT_FOUND"

        rec = self._reservations[reservation_id]
        if rec["state"] == "COMMITTED":
            return "ALREADY_COMMITTED"
        if rec["state"] != "RESERVED":
            return "ERROR"

        res = self._resources.get(rec["resource_id"])
        if res is None:
            return "ERROR"

        # Transfer from reserved to committed
        res["reserved"] -= float(rec["amount"])
        res["committed"] += float(rec["amount"])
        rec["state"] = "COMMITTED"
        rec["committed_at"] = time.time()

        return "COMMITTED"

    @replicated
    def release(self, reservation_id):
        """Release a non-committed reservation.

        Returns: status string
        """
        if reservation_id not in self._reservations:
            return "NOT_FOUND"

        rec = self._reservations[reservation_id]
        if rec["state"] == "COMMITTED":
            return "ALREADY_COMMITTED"
        if rec["state"] not in ("RESERVED", "UNKNOWN"):
            return "ERROR"

        res = self._resources.get(rec["resource_id"])
        if res is None:
            return "ERROR"

        res["reserved"] -= float(rec["amount"])
        rec["state"] = "RELEASED"
        rec["released_at"] = time.time()

        return "RELEASED"

    @replicated
    def reconcile_commit(self, reservation_id):
        """Reconcile by committing (provider confirmed effect)."""
        if reservation_id not in self._reservations:
            return "NOT_FOUND"

        rec = self._reservations[reservation_id]
        if rec["state"] == "COMMITTED":
            return "ALREADY_COMMITTED"

        res = self._resources.get(rec["resource_id"])
        if res is None:
            return "ERROR"

        if rec["state"] == "RESERVED":
            res["reserved"] -= float(rec["amount"])
        rec["state"] = "COMMITTED"
        rec["committed_at"] = time.time()
        res["committed"] += float(rec["amount"])

        return "COMMITTED"

    @replicated
    def reconcile_release(self, reservation_id):
        """Reconcile by releasing (provider confirmed no effect)."""
        if reservation_id not in self._reservations:
            return "NOT_FOUND"

        rec = self._reservations[reservation_id]
        if rec["state"] == "COMMITTED":
            return "ALREADY_COMMITTED"
        if rec["state"] == "RELEASED":
            return "RELEASED"

        res = self._resources.get(rec["resource_id"])
        if res is None:
            return "ERROR"

        if rec["state"] == "RESERVED":
            res["reserved"] -= float(rec["amount"])
        rec["state"] = "RELEASED"
        rec["released_at"] = time.time()

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
                "amount": rec["amount"],
                "state": rec["state"],
            }
            for rid, rec in self._reservations.items()
            if rec.get("resource_id") == resource_id
        ]

        return {
            "available": int(res["capacity"] - res["reserved"] - res["committed"]),
            "reserved": int(res["reserved"]),
            "committed": int(res["committed"]),
            "capacity": int(res["capacity"]),
            "reservations": reservations,
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
        """Register a resource with the backend (replicated)."""
        self._raft.register_resource(resource_id, capacity, sync=True, timeout=5.0)

    def reserve(self, key, amount, resource_id) -> ReservationResult:
        """Reserve budget for an idempotency key (replicated via Raft).

        The reservation_id and timestamp are generated client-side and passed
        as arguments to the replicated command. This ensures deterministic
        replication: all nodes store the same reservation_id, so commit/release
        from any node can find the reservation.

        I8 is enforced on the leader before the command is committed.
        """
        reservation_id = str(uuid.uuid4())
        timestamp = time.time()
        try:
            result = self._raft.reserve(
                key, amount, resource_id, reservation_id, timestamp,
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

            if rec["state"] in ("COMMITTED", "RELEASED"):
                # Already resolved
                if rec["state"] == "COMMITTED":
                    return ReconciliationResult(status="CONFIRMED", effect_observed=True)
                else:
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
                    else:
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