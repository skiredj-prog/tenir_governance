"""Durable operation registry and dispatch state machine.

Per review 3 §1: the idempotency key must be durably claimed BEFORE dispatch,
not checked in-memory after. The state machine is:

    CREATED → RESERVED → DISPATCHING → CONFIRMED | FAILED | UNKNOWN

Every transition must be durably recorded before proceeding to the next step.
If the process crashes at any point, recovery can determine the exact state:

    CREATED     — operation registered, not yet authorized
    RESERVED    — budget reserved, authorization granted, not yet dispatching
    DISPATCHING — dispatch in progress (external call sent, response not received)
                  Recovery: RECONCILE — do not blindly resend
    CONFIRMED   — external system confirmed the effect occurred (terminal)
    FAILED      — external system confirmed no effect occurred (terminal)
    UNKNOWN     — outcome uncertain after reconciliation (terminal-ish: needs human)

The critical correctness property: the transition from RESERVED → DISPATCHING
must be atomic with claiming the dispatch. If two workers try to dispatch the
same operation, exactly one succeeds.

For the prototype, durability is simulated via a file-backed store. In
production, this would be a transactional database with write-ahead logging.
"""
# Profile: agentic-governance-profile-v1.0.0
# Compliance class: profile-bound
# This module is an extension on top of KB3 and TENIR 2.0 schema 1.2.
# It is NOT canonical, NOT doctrinal. It carries implementation-level
# governance enforcement authority only.
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional
from uuid import uuid4

try:
    import fcntl
except ImportError:
    fcntl = None  # Windows fallback (not used in tests)


class OperationState(str, Enum):
    """States in the dispatch state machine.

    Per review 3 §1: every legal state transition, its durable write, and
    the behavior if the process dies immediately before or after it must be
    defined.
    """
    CREATED = "created"
    RESERVED = "reserved"
    DISPATCHING = "dispatching"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    UNKNOWN = "unknown"

    @property
    def is_terminal(self) -> bool:
        """Terminal states: no further transitions possible.

        CONFIRMED and FAILED are fully terminal. UNKNOWN is terminal in the
        sense that the system cannot resolve it automatically — it requires
        human intervention or external reconciliation.
        """
        return self in (OperationState.CONFIRMED, OperationState.FAILED, OperationState.UNKNOWN)

    @property
    def is_dispatched(self) -> bool:
        """Has this operation been dispatched (or may have been)?"""
        return self in (OperationState.DISPATCHING, OperationState.CONFIRMED, OperationState.UNKNOWN)

    @property
    def may_have_been_dispatched(self) -> bool:
        """Per review 3 §1: recovery must distinguish 'definitely not
        dispatched' from 'may have been dispatched'.

        DISPATCHING and UNKNOWN mean the operation may have been dispatched.
        CONFIRMED means it definitely was. CREATED and RESERVED mean it
        definitely was not.
        """
        return self in (OperationState.DISPATCHING, OperationState.CONFIRMED, OperationState.UNKNOWN)


# Legal transitions
_LEGAL_TRANSITIONS = {
    OperationState.CREATED: {OperationState.RESERVED, OperationState.FAILED},
    OperationState.RESERVED: {OperationState.DISPATCHING, OperationState.FAILED},
    OperationState.DISPATCHING: {OperationState.CONFIRMED, OperationState.FAILED, OperationState.UNKNOWN},
    OperationState.CONFIRMED: set(),  # terminal
    OperationState.FAILED: set(),  # terminal
    OperationState.UNKNOWN: set(),  # terminal (needs human)
}


@dataclass
class OperationRecord:
    """A durable record of an operation's lifecycle.

    Per review 3 §1: the key should be durably claimed before dispatch, with
    an explicit state machine. This record is the durable claim.
    """
    idempotency_key: str
    passage_id: str
    agent_id: str
    action_type: str
    amount: float = 0.0
    resource_id: str = ""
    state: OperationState = OperationState.CREATED
    created_at: str = ""
    updated_at: str = ""
    # The composed decision that authorized this operation
    composed_decision: str = ""
    # Which worker/process claimed the dispatch (for cross-process coordination)
    dispatch_claimed_by: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["state"] = self.state.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "OperationRecord":
        d = dict(d)
        d["state"] = OperationState(d.get("state", "created"))
        return cls(**d)


class DurableOperationRegistry:
    """Durable registry of operation records with atomic state transitions.

    For the prototype, durability is provided via a file-backed JSON store
    with atomic rename + fsync. Cross-process atomicity is provided via
    fcntl.flock file-level locking. In production, this would be:
        - A transactional database with SELECT ... FOR UPDATE
        - A write-ahead log with crash recovery
        - A distributed consensus system (Raft, Paxos)

    DURABILITY MODEL:
        - Process crash (kill -9): The last successfully persisted state
          survives. Atomic rename ensures the store file is never partially
          written. The fcntl.flock is released automatically when the process
          dies, so a crashed process does not deadlock the registry.
        - Power loss: Survives IF the directory fsync in _persist() completes.
          The temp file is fsync'd before rename, and the parent directory is
          fsync'd after rename, so the rename is durable. Without the directory
          fsync, power loss could revert to the old file.

    LIMITATIONS:
        - Single-host only: fcntl.flock does not coordinate across hosts.
          For cross-host coordination, replace with a database-backed store.
        - No WAL: if a crash occurs during _persist(), the in-memory state
          since the last successful _persist() is lost. A WAL would preserve
          uncommitted operations. In practice, the state machine's resume
          path handles this: a lost transition is detected as a stale state
          and recovered via resume_existing().
        - Budget coordinator is in-process only: the InProcessBudgetCoordinator
          uses threading.Lock, which does not coordinate across processes.
          The file-backed registry provides cross-process atomicity for the
          dispatch state machine, but budget reservations remain in-process.

    The critical property: transition_to(DISPATCHING) is the atomic claim.
    If two workers try to transition the same operation to DISPATCHING,
    exactly one succeeds. The other gets (False, current_state).
    """

    def __init__(self, store_path: str | None = None):
        self._lock = threading.Lock()
        self._records: dict[str, OperationRecord] = {}
        self._store_path = store_path
        self._corrupted = False
        if store_path and os.path.exists(store_path):
            self._load()

    def _load(self):
        """Load records from the durable store.

        Distinguishes absent file (normal — first start) from corrupt file
        (fail-closed — refuse to proceed with stale data). A corrupt store
        sets the _corrupted flag so register() and transition_to() can refuse
        new operations, preventing silent duplicate adjudication.
        """
        if not self._store_path or not os.path.exists(self._store_path):
            return
        try:
            with open(self._store_path, "r") as f:
                data = json.load(f)
            for key, rec in data.items():
                self._records[key] = OperationRecord.from_dict(rec)
            self._corrupted = False
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            # Corrupted store — fail-closed. Do NOT start fresh silently.
            # The _corrupted flag prevents new adjudication, which would
            # risk duplicate operations if previously-claimed keys are
            # invisible due to corruption.
            self._corrupted = True

    def _persist(self):
        """Persist records to the durable store.

        Uses atomic-rename: write to temp file, fsync the temp file, rename
        over the target, then fsync the parent directory. This ensures:

        - Process crash (kill -9): the last successfully persisted state
          survives. A crash during _persist leaves either the old file
          (rename not yet done) or the new file (rename done). The temp
          file may be orphaned but is harmless.
        - Power loss: survives IF fsync of the parent directory completes.
          Without the directory fsync, the rename may not be flushed to
          disk and power loss could revert to the old file. With it, the
          rename is durable.

        In production, replace this with a database transaction (which
        handles all of this internally).
        """
        if self._store_path:
            data = {k: v.to_dict() for k, v in self._records.items()}
            tmp = self._store_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.rename(tmp, self._store_path)
            # fsync the parent directory so the rename survives power loss
            dir_fd = os.open(os.path.dirname(self._store_path) or ".", os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    def _lock_file(self):
        """Acquire a cross-process exclusive lock on the store file.

        Uses fcntl.flock for cross-process atomicity. The caller must
        close the returned file handle to release the lock.
        Returns None if no store path or fcntl is unavailable.
        """
        if not self._store_path or fcntl is None:
            return None
        # Ensure the lock target exists
        lock_path = self._store_path + ".lock"
        if not os.path.exists(lock_path):
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
            os.close(fd)
        f = open(lock_path, "r+")
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        return f

    @property
    def is_corrupted(self) -> bool:
        """True if the persistent store was corrupted on load."""
        return self._corrupted

    def register(self, record: OperationRecord) -> bool:
        """Register a new operation. Returns False if key already exists.

        Fail-closed: if the persistent store is corrupted, refuses to register
        new operations. A corrupted store may have lost previously-claimed
        keys; allowing new operations would risk duplicate adjudication.

        Cross-process safe: acquires a file lock, reloads from disk to check
        for concurrent registrations, then persists atomically.
        """
        with self._lock:
            if self._corrupted:
                raise RuntimeError(
                    "Registry store is corrupted — refuse to register new "
                    "operations. Preserve the corrupt file for investigation "
                    "and recover explicitly before proceeding."
                )
            flock = self._lock_file()
            try:
                # Reload from disk to see if another process registered first
                if self._store_path and os.path.exists(self._store_path):
                    self._load()
                if self._corrupted:
                    raise RuntimeError(
                        "Registry store became corrupted on reload — refuse "
                        "to register new operations."
                    )
                if record.idempotency_key in self._records:
                    return False
                self._records[record.idempotency_key] = record
                self._persist()
                return True
            finally:
                if flock:
                    flock.close()

    def get(self, idempotency_key: str) -> OperationRecord | None:
        with self._lock:
            # Reload from disk to see the latest state from other processes
            if self._store_path and os.path.exists(self._store_path):
                self._load()
            return self._records.get(idempotency_key)

    def transition_to(
        self,
        idempotency_key: str,
        new_state: OperationState,
        claimed_by: str = "",
    ) -> tuple[bool, OperationState | None]:
        """Atomically transition an operation to a new state.

        This is the critical atomic operation. It checks:
        1. The operation exists.
        2. The transition is legal.
        3. For DISPATCHING: the operation is not already dispatched.

        Returns (True, previous_state) if the transition succeeded,
        (False, current_state_or_None) otherwise.

        The previous state is returned so callers can make post-transition
        decisions (e.g., refund vs release) based on the state that was
        atomically observed under the lock, not from a separate get() call
        that could race.

        Per review 3 §1: the transition from RESERVED → DISPATCHING is the
        atomic claim. If two workers try to dispatch the same operation,
        exactly one succeeds.

        Cross-process safe: acquires a file lock, reloads from disk to see
        the latest state from other processes, then atomically checks and
        persists.
        """
        with self._lock:
            if self._corrupted:
                raise RuntimeError(
                    "Registry store is corrupted — refuse to transition "
                    "operations. Preserve the corrupt file and recover."
                )
            flock = self._lock_file()
            try:
                # Reload from disk to see the latest state from other processes
                if self._store_path and os.path.exists(self._store_path):
                    self._load()
                if self._corrupted:
                    raise RuntimeError(
                        "Registry store became corrupted on reload — refuse "
                        "to transition operations."
                    )

                record = self._records.get(idempotency_key)
                if record is None:
                    return False, None

                prev_state = record.state

                # Check legal transition
                if new_state not in _LEGAL_TRANSITIONS.get(record.state, set()):
                    return False, prev_state

                # For DISPATCHING: check not already dispatched
                if new_state == OperationState.DISPATCHING:
                    if record.state == OperationState.DISPATCHING:
                        return False, prev_state  # already dispatching
                    if record.state.is_dispatched:
                        return False, prev_state  # already dispatched

                record.state = new_state
                if claimed_by:
                    record.dispatch_claimed_by = claimed_by
                self._persist()
                return True, prev_state
            finally:
                if flock:
                    flock.close()

    def find_by_state(self, state: OperationState) -> list[OperationRecord]:
        """Find all operations in a given state. Used for recovery.

        Cross-process safe: reloads from disk under the file lock so that
        operations registered by other processes are visible.
        """
        with self._lock:
            flock = self._lock_file()
            try:
                if self._store_path and os.path.exists(self._store_path):
                    self._load()
                return [r for r in self._records.values() if r.state == state]
            finally:
                if flock:
                    flock.close()

    def find_unresolved(self) -> list[OperationRecord]:
        """Find all operations that may have been dispatched but are not confirmed.

        Per review 3 §1: recovery must find DISPATCHING and UNKNOWN operations
        and reconcile them, not blindly resend.

        Cross-process safe: reloads from disk under the file lock so that
        operations left DISPATCHING by a crashed process are visible.
        """
        with self._lock:
            flock = self._lock_file()
            try:
                if self._store_path and os.path.exists(self._store_path):
                    self._load()
                return [r for r in self._records.values()
                        if r.state in (OperationState.DISPATCHING, OperationState.UNKNOWN)]
            finally:
                if flock:
                    flock.close()

    @property
    def record_count(self) -> int:
        with self._lock:
            if self._store_path and os.path.exists(self._store_path):
                self._load()
            return len(self._records)