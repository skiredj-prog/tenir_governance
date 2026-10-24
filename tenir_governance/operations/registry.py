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
    fcntl = None

try:
    import msvcrt
except ImportError:
    msvcrt = None


class _WindowsFileLock:
    """One-byte advisory lock whose close operation releases the lock."""

    def __init__(self, path: str):
        self._file = open(path, "a+b")
        self._file.seek(0, os.SEEK_END)
        if self._file.tell() == 0:
            self._file.write(b"\0")
            self._file.flush()
        self._file.seek(0)
        msvcrt.locking(self._file.fileno(), msvcrt.LK_LOCK, 1)

    def close(self):
        if self._file is None:
            return
        try:
            self._file.seek(0)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._file.close()
            self._file = None


class OperationState(str, Enum):
    """States in the dispatch state machine.

    Per review 3 §1: every legal state transition, its durable write, and
    the behavior if the process dies immediately before or after it must be
    defined.

    HOLD lifecycle (per doctrine v7.1 and Blueprint v4.4):
        HOLD is a first-class protocol state — a designed suspension of agency
        under pressure, not a system failure. The operation is not denied; it
        is pending evidence or review. HOLD may resolve to:
          - RESERVED (evidence becomes sufficient → PASS/FLAG)
          - FAILED (τ_K expires → fail-closed, NOT auto-resolve)
          - ESCALATED (escalation to risk officer — absorbing until authorized)

        ESCALATED is an absorbing state: it remains until an authorization
        model defines who signs, under what scope, and with which receipt.
        A time-based exit from ESCALATED is rejected: τ_K expiry produces
        UNKNOWN, not resolution (per v7.1 §19).

    QUARANTINE_LOCK is distinct from HOLD (per v7.1 §6):
        HOLD withholds admission for the specific decision. QUARANTINE_LOCK
        blocks ALL new admission and dispatch for the LEI until an authorized
        release. The two must not be treated as interchangeable.
    """
    CREATED = "created"
    HOLD = "hold"                    # Suspended pending evidence/review
    RESERVED = "reserved"
    DISPATCHING = "dispatching"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    UNKNOWN = "unknown"
    ESCALATED = "escalated"          # Absorbing — awaiting authorized exit

    @property
    def is_terminal(self) -> bool:
        """Terminal states: no further transitions possible.

        CONFIRMED and FAILED are fully terminal. UNKNOWN is not terminal:
        it can be reconciled to CONFIRMED or FAILED via the provider.
        """
        return self in (OperationState.CONFIRMED, OperationState.FAILED)

    @property
    def is_dispatched(self) -> bool:
        """Has this operation been dispatched (or may have been)?"""
        return self in (OperationState.DISPATCHING, OperationState.CONFIRMED, OperationState.UNKNOWN)

    @property
    def may_have_been_dispatched(self) -> bool:
        """Per review 3 §1: recovery must distinguish 'definitely not
        dispatched' from 'may have been dispatched'.

        DISPATCHING and UNKNOWN mean the operation may have been dispatched.
        CONFIRMED means it definitely was. CREATED, HOLD, RESERVED, and
        ESCALATED mean it definitely was not.
        """
        return self in (OperationState.DISPATCHING, OperationState.CONFIRMED, OperationState.UNKNOWN)

    @property
    def is_holding(self) -> bool:
        """Is this operation in a HOLD-related state (not yet authorized)?"""
        return self in (OperationState.HOLD, OperationState.ESCALATED)


# Legal transitions
_LEGAL_TRANSITIONS = {
    OperationState.CREATED: {OperationState.RESERVED, OperationState.HOLD, OperationState.FAILED},
    OperationState.HOLD: {OperationState.RESERVED, OperationState.FAILED, OperationState.ESCALATED},
    OperationState.RESERVED: {OperationState.DISPATCHING, OperationState.FAILED},
    OperationState.DISPATCHING: {OperationState.CONFIRMED, OperationState.FAILED, OperationState.UNKNOWN},
    OperationState.ESCALATED: {OperationState.RESERVED, OperationState.FAILED, OperationState.UNKNOWN},
    OperationState.UNKNOWN: {OperationState.CONFIRMED, OperationState.FAILED},  # reconcilable
    OperationState.CONFIRMED: set(),  # terminal
    OperationState.FAILED: set(),  # terminal
}


@dataclass
class ExitReceipt:
    """Receipt for an authorized exit from HOLD or QUARANTINE_LOCK.

    Per v7.1 §19: ESCALATED remains absorbing until an authorization model
    defines who signs, under what scope, and with which receipt. A time-based
    exit is rejected: expiry produces UNKNOWN, not resolution.

    Per Blueprint v4.4 §5: overrides constitutionnels uniquement via
    Ceremony G-R5-001 (authentification opérateur).
    """
    authorized_by: str = ""
    scope: str = ""
    timestamp: str = ""
    authorization: str = ""  # e.g., "ceremony-G-R5-001", "risk-officer-override"
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ExitReceipt":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class HoldRecord:
    """Metadata for the HOLD lifecycle of an operation.

    Per doctrine v7.1: HOLD is a first-class protocol state — a designed
    suspension of agency under pressure, not a system failure. The operation
    is not denied; it is pending evidence or review.

    Fields:
        hold_start: ISO timestamp when HOLD was entered
        tau_k_seconds: temporal validity bound — the organization's reaction
                       time to understand a situation and exercise its veto
        tau_k_source: where τ_K was derived from (SLA, regulatory, etc.)
        escalated_at: ISO timestamp when escalated to ESCALATED (if applicable)
        escalated_by: who initiated the escalation
        exit_reason: why the hold ended ("resolved", "expired", "escalated",
                     "authorized_exit", "failed")
        exit_receipt: receipt for authorized exits (None for expired/failed)
    """
    hold_start: str = ""
    tau_k_seconds: float = 0.0
    tau_k_source: str = ""
    escalated_at: str = ""
    escalated_by: str = ""
    exit_reason: str = ""
    exit_receipt: ExitReceipt | None = None

    @property
    def is_escalated(self) -> bool:
        return bool(self.escalated_at)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["exit_receipt"] = self.exit_receipt.to_dict() if self.exit_receipt else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "HoldRecord":
        d = dict(d)
        er = d.pop("exit_receipt", None)
        exit_receipt = ExitReceipt.from_dict(er) if er else None
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__},
                   exit_receipt=exit_receipt)


@dataclass
class OperationRecord:
    """A durable record of an operation's lifecycle.

    Per review 3 §1: the key should be durably claimed before dispatch, with
    an explicit state machine. This record is the durable claim.

    HOLD lifecycle fields are populated when the operation enters HOLD state
    and updated through the hold lifecycle (resolve, escalate, expire, exit).
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
    # Budget ownership flags (single source of truth for reserve/commit):
    #   budget_reserved  — a reservation is currently held for this operation
    #   budget_committed — the reservation was committed (consumed) at dispatch
    budget_reserved: bool = False
    budget_committed: bool = False
    # HOLD lifecycle metadata (None unless operation has been in HOLD)
    hold_record: HoldRecord | None = None
    # Immutable execution identity used by the optional Profile S commit gate.
    # These fields are appended after the legacy record fields to preserve the
    # positional constructor contract for existing integrations.
    operation_id: str = ""
    institutional_scope: str = ""
    payload_digest: str = ""
    # Profile S commit authorization is opt-in per operation.  The request and
    # signed receipts are stored as canonical JSON-safe envelopes so recovery
    # can resume after either authority call commits but its reply is lost.
    commit_gate_required: bool = False
    commit_authorization_request: dict | None = None
    commit_authorization_receipt: dict | None = None
    commit_dispatch_claim_id: str = ""
    commit_consumption_receipt: dict | None = None
    # Persisted authorization substate.  This remains separate from the
    # provider lifecycle so TRUST_UNKNOWN/HOLD can block a RESERVED operation
    # without pretending that an external effect may have occurred.
    commit_authorization_status: str = ""
    commit_consumption_status: str = ""
    commit_gate_last_reason: str = ""
    commit_gate_events: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["state"] = self.state.value
        d["hold_record"] = self.hold_record.to_dict() if self.hold_record else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "OperationRecord":
        d = dict(d)
        d["state"] = OperationState(d.get("state", "created"))
        hr = d.pop("hold_record", None)
        hold_record = HoldRecord.from_dict(hr) if hr else None
        # Remove hold_record from d since we'll pass it separately
        d.pop("hold_record", None)
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__},
                   hold_record=hold_record)


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

        Preserves hold_record from existing in-memory records when reloading,
        so that HoldManager modifications are not lost during state transitions
        that trigger a disk reload.
        """
        if not self._store_path or not os.path.exists(self._store_path):
            return
        try:
            with open(self._store_path, "r") as f:
                data = json.load(f)
            # Preserve hold_records from existing in-memory records.
            # In-memory hold_records are always more up-to-date than disk
            # because HoldManager modifies them in memory before calling
            # transition_to(), which triggers this reload. Without this
            # preservation, exit_reason and exit_receipt set by
            # resolve_hold() / authorize_exit() would be lost.
            existing_hold_records = {
                k: v.hold_record for k, v in self._records.items() if v.hold_record
            }
            self._records = {}
            for key, rec in data.items():
                record = OperationRecord.from_dict(rec)
                if key in existing_hold_records:
                    # Always prefer in-memory hold_record (most up-to-date)
                    record.hold_record = existing_hold_records[key]
                self._records[key] = record
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
            # os.replace is the cross-platform overwrite form of atomic
            # rename.  os.rename cannot replace an existing file on Windows.
            os.replace(tmp, self._store_path)
            # fsync the parent directory so the rename survives power loss
            # Windows does not permit opening a directory with os.open().
            # The file itself was already flushed before os.replace; retain
            # the stronger parent-directory fsync on POSIX where supported.
            try:
                dir_fd = os.open(
                    os.path.dirname(self._store_path) or ".", os.O_RDONLY,
                )
            except (PermissionError, OSError):
                if os.name != "nt":
                    raise
            else:
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
        if not self._store_path:
            return None
        # Ensure the lock target exists
        lock_path = self._store_path + ".lock"
        if fcntl is None and msvcrt is not None:
            return _WindowsFileLock(lock_path)
        if fcntl is None:
            return None
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
        *,
        commit_gate_verified: bool = False,
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
                    if record.commit_gate_required:
                        consumption = record.commit_consumption_receipt
                        body = consumption.get("body", {}) if isinstance(consumption, dict) else {}
                        if (
                            body.get("decision") != "CONSUMED"
                            or not record.commit_dispatch_claim_id
                            or body.get("dispatch_claim_id") != record.commit_dispatch_claim_id
                            or not commit_gate_verified
                        ):
                            return False, prev_state

                record.state = new_state
                if claimed_by:
                    record.dispatch_claimed_by = claimed_by
                self._persist()
                return True, prev_state
            finally:
                if flock:
                    flock.close()

    def set_budget_flags(self, idempotency_key: str, **flags: bool) -> bool:
        """Atomically update and persist budget ownership flags."""
        allowed = {"budget_reserved", "budget_committed"}
        unknown = set(flags) - allowed
        if unknown:
            raise ValueError(f"unknown budget flag(s): {sorted(unknown)}")

        with self._lock:
            if self._corrupted:
                raise RuntimeError(
                    "Registry store is corrupted — refuse to update budget flags."
                )
            flock = self._lock_file()
            try:
                if self._store_path and os.path.exists(self._store_path):
                    self._load()
                if self._corrupted:
                    raise RuntimeError(
                        "Registry store became corrupted on reload — refuse to update budget flags."
                    )
                record = self._records.get(idempotency_key)
                if record is None:
                    return False

                previous = {name: getattr(record, name) for name in flags}
                for name, value in flags.items():
                    setattr(record, name, bool(value))
                desired = {name: getattr(record, name) for name in flags}
                try:
                    self._persist()
                except Exception:
                    # Atomic rename may already have installed the new file
                    # before a later directory-fsync error. Determine which
                    # version is visible on disk before telling the caller to
                    # compensate an external budget mutation.
                    if self._store_path and os.path.exists(self._store_path):
                        try:
                            self._load()
                        except Exception:
                            pass
                        durable = self._records.get(idempotency_key)
                        if durable is not None and all(
                            getattr(durable, name) == value
                            for name, value in desired.items()
                        ):
                            # New ownership flags are visible. Treat the update
                            # as committed so callers do not release capacity
                            # while durable state claims to own it.
                            return True
                    current = self._records.get(idempotency_key)
                    if current is not None:
                        for name, value in previous.items():
                            setattr(current, name, value)
                    raise
                return True
            finally:
                if flock:
                    flock.close()

    def _set_immutable_commit_value(
        self,
        idempotency_key: str,
        field_name: str,
        value,
        *,
        require_authorization: bool = False,
        status_field: str | None = None,
        status: str = "",
    ) -> bool:
        """Persist one immutable commit-gate value under the registry lock."""
        with self._lock:
            if self._corrupted:
                raise RuntimeError(
                    "Registry store is corrupted — refuse to update commit authorization."
                )
            flock = self._lock_file()
            try:
                if self._store_path and os.path.exists(self._store_path):
                    self._load()
                record = self._records.get(idempotency_key)
                if record is None or not record.commit_gate_required:
                    return False
                existing = getattr(record, field_name)
                if existing not in (None, ""):
                    if existing != value:
                        return False
                    if status_field and getattr(record, status_field) != status:
                        setattr(record, status_field, status)
                        self._persist()
                    return True
                if record.state != OperationState.RESERVED:
                    return False
                if require_authorization and record.commit_authorization_receipt is None:
                    return False
                setattr(record, field_name, value)
                if status_field:
                    setattr(record, status_field, status)
                self._persist()
                return True
            finally:
                if flock:
                    flock.close()

    def record_commit_authorization_request(
        self, idempotency_key: str, request: dict,
    ) -> bool:
        """Persist the immutable request before contacting the authority."""
        canonical = json.loads(json.dumps(request, sort_keys=True))
        return self._set_immutable_commit_value(
            idempotency_key, "commit_authorization_request", canonical,
            status_field="commit_authorization_status",
            status="TRUST_COMMITTING",
        )

    def record_commit_authorization_receipt(
        self, idempotency_key: str, receipt: dict,
    ) -> bool:
        """Persist the authority's signed decision without allowing replacement."""
        canonical = json.loads(json.dumps(receipt, sort_keys=True))
        body = canonical.get("body", {}) if isinstance(canonical, dict) else {}
        decision = body.get("decision")
        return self._set_immutable_commit_value(
            idempotency_key, "commit_authorization_receipt", canonical,
            status_field="commit_authorization_status",
            status="TRUST_COMMITTED" if decision == "AUTHORIZED" else "HOLD",
        )

    def ensure_commit_dispatch_claim(
        self, idempotency_key: str, claim_id: str,
    ) -> tuple[bool, str]:
        """Claim one stable dispatch identity before consuming authorization."""
        if not isinstance(claim_id, str) or not claim_id:
            raise ValueError("dispatch claim identity must be non-empty")
        with self._lock:
            if self._corrupted:
                raise RuntimeError(
                    "Registry store is corrupted — refuse to claim commit authorization."
                )
            flock = self._lock_file()
            try:
                if self._store_path and os.path.exists(self._store_path):
                    self._load()
                record = self._records.get(idempotency_key)
                if (
                    record is None
                    or not record.commit_gate_required
                    or record.state != OperationState.RESERVED
                    or record.commit_authorization_receipt is None
                ):
                    return False, ""
                if record.commit_dispatch_claim_id:
                    return True, record.commit_dispatch_claim_id
                record.commit_dispatch_claim_id = claim_id
                self._persist()
                return True, claim_id
            finally:
                if flock:
                    flock.close()

    def record_commit_consumption_receipt(
        self, idempotency_key: str, receipt: dict,
    ) -> bool:
        """Persist the signed single-consumption result before dispatch claim."""
        canonical = json.loads(json.dumps(receipt, sort_keys=True))
        body = canonical.get("body", {}) if isinstance(canonical, dict) else {}
        record = self.get(idempotency_key)
        if (
            record is None
            or not record.commit_dispatch_claim_id
            or body.get("dispatch_claim_id") != record.commit_dispatch_claim_id
        ):
            return False
        return self._set_immutable_commit_value(
            idempotency_key,
            "commit_consumption_receipt",
            canonical,
            require_authorization=True,
            status_field="commit_consumption_status",
            status="TRUST_CONSUMED" if body.get("decision") == "CONSUMED" else "HOLD",
        )

    def record_commit_gate_status(
        self,
        idempotency_key: str,
        *,
        phase: str,
        status: str,
        reason: str,
        security_event: bool = False,
    ) -> bool:
        """Persist a bounded trust-gate non-success state and audit event."""
        if phase not in {"authorization", "consumption"}:
            raise ValueError("invalid commit-gate phase")
        if (
            not isinstance(status, str)
            or not status
            or len(status) > 64
            or not status.replace("_", "").isalnum()
        ):
            raise ValueError("invalid commit-gate status")
        if not isinstance(reason, str):
            reason = str(reason)
        reason = reason[:512]
        status_field = (
            "commit_authorization_status"
            if phase == "authorization"
            else "commit_consumption_status"
        )
        with self._lock:
            if self._corrupted:
                raise RuntimeError(
                    "Registry store is corrupted — refuse to update commit-gate status."
                )
            flock = self._lock_file()
            try:
                if self._store_path and os.path.exists(self._store_path):
                    self._load()
                record = self._records.get(idempotency_key)
                if record is None or not record.commit_gate_required:
                    return False
                setattr(record, status_field, status)
                record.commit_gate_last_reason = reason
                next_sequence = (
                    record.commit_gate_events[-1].get("sequence", 0) + 1
                    if record.commit_gate_events
                    else 1
                )
                event = {
                    "sequence": next_sequence,
                    "phase": phase,
                    "status": status,
                    "reason": reason,
                    "security_event": bool(security_event),
                }
                if not record.commit_gate_events or record.commit_gate_events[-1] != event:
                    record.commit_gate_events.append(event)
                    if len(record.commit_gate_events) > 128:
                        record.commit_gate_events = record.commit_gate_events[-128:]
                self._persist()
                return True
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
