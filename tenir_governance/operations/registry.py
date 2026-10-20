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
import tempfile
import threading
import hashlib
from datetime import datetime, timezone
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict, replace
from enum import Enum
from typing import Any, Mapping, Optional
from uuid import uuid4

from .payload_binding import EMPTY_PAYLOAD_HASH
from .file_lock import acquire_file_lock



def _verify_quarantine_release(
    verifier, *, authorized_by: str, scope: str, authorization: str,
    reason: str, lock_id: str,
) -> dict | None:
    """Accept only a trusted verifier's scoped, lock-bound attestation.

    The input strings and the ceremony label are NOT authentication.
    A caller cannot supply the attestation directly. Integrations must
    configure a verifier that authenticates the claimant and validates
    the token/signature, scope, nonce and replay rules.
    """
    if not callable(verifier) or not lock_id:
        return None
    try:
        proof = verifier(
            authorized_by=authorized_by, scope=scope, authorization=authorization,
            reason=reason, lock_id=lock_id,
        )
    except Exception:
        return None
    if not isinstance(proof, dict):
        return None
    if (proof.get("authorized") is not True
            or proof.get("principal") != authorized_by
            or proof.get("scope") != scope
            or proof.get("lock_id") != lock_id
            or not isinstance(proof.get("authorization_id"), str)
            or not proof["authorization_id"].strip()):
        return None
    return {
        "principal": authorized_by,
        "scope": scope,
        "lock_id": lock_id,
        "authorization_id": proof["authorization_id"],
    }


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
    # Appended to preserve positional-constructor compatibility for legacy fields.
    # Durable SingleHostLockBackend reservation owning this operation budget.
    # Empty only for legacy records or operations without a budget reservation.
    reservation_id: str = ""
    # SHA-256 of the canonical JSON action payload approved at adjudication.
    # The empty-object digest is the backwards-compatible default.
    payload_hash: str = EMPTY_PAYLOAD_HASH

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
    operating-system file-level locking. In production, this would be:
        - A transactional database with SELECT ... FOR UPDATE
        - A write-ahead log with crash recovery
        - A distributed consensus system (Raft, Paxos)

    DURABILITY MODEL:
        - Process crash (kill -9): The last successfully persisted state
          survives. Atomic rename ensures the store file is never partially
          written. The OS file lock is released automatically when the process
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

    def __init__(self, store_path: str | None = None, *, quarantine_release_verifier=None):
        self._lock = threading.Lock()
        self._records: dict[str, OperationRecord] = {}
        self._store_path = store_path
        # In-memory registries need only process-local serialization. Durable
        # backend settlement explicitly requires the file-backed lock below.
        self._local_settlement_locks: dict[str, threading.Lock] = {}
        self._local_settlement_locks_guard = threading.Lock()
        self._corrupted = False
        self._quarantine_memory = {"active": False}
        self._quarantine_release_verifier = quarantine_release_verifier
        self._quarantine_gate_local = threading.RLock()
        self._quarantine_gate_depth = threading.local()
        if store_path and os.path.exists(store_path):
            self._load()

    def _load(self):
        """Load records from the durable store.

        Distinguishes absent file (normal — first start) from corrupt file
        (fail-closed — refuse to proceed with stale data). A corrupt store
        sets the _corrupted flag so register() and transition_to() can refuse
        new operations, preventing silent duplicate adjudication.

        The durable store is authoritative. In particular, do not carry over a
        HoldRecord from memory: another registry instance may have committed a
        newer state and matching metadata since this instance last loaded it.
        """
        if not self._store_path or not os.path.exists(self._store_path):
            return
        try:
            with open(self._store_path, "r") as f:
                data = json.load(f)
            self._records = {}
            for key, rec in data.items():
                record = OperationRecord.from_dict(rec)
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
        - Power loss: on POSIX systems, survives IF fsync of the parent
          directory completes. Python's standard library does not provide
          portable directory fsync on Windows, so this implementation does
          not claim power-loss durability there.

        In production, replace this with a database transaction (which
        handles all of this internally).
        """
        if self._store_path:
            data = {k: v.to_dict() for k, v in self._records.items()}
            directory = os.path.dirname(os.path.abspath(self._store_path))
            prefix = os.path.basename(self._store_path) + "."
            fd, tmp = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(data, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp, self._store_path)
                # Windows does not support opening directories for fsync.
                if os.name != "nt":
                    dir_fd = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)

    def _lock_file(self):
        """Acquire a cross-process exclusive lock on the store file.

        The caller must close the returned file handle to release the lock.
        Returns None for an in-memory registry.
        """
        if not self._store_path:
            return None
        lock_path = self._store_path + ".lock"
        return acquire_file_lock(lock_path)

    @property
    def is_corrupted(self) -> bool:
        """True if the persistent store was corrupted on load."""
        return self._corrupted

    @property
    def is_persistent(self) -> bool:
        """True when registry records are backed by a durable file store."""
        return bool(self._store_path)

    @contextmanager
    def quarantine_guard(self):
        """Shared single-host admission/dispatch/quarantine linearization gate.

        A separate lock file is used: registry transition APIs also acquire
        their own lock and must not re-enter the same OS flock descriptor.
        Nested use from the same registry/thread is supported explicitly.
        """
        with self._quarantine_gate_local:
            depth = getattr(self._quarantine_gate_depth, "depth", 0)
            lease = None
            if depth == 0 and self._store_path:
                lease = acquire_file_lock(self._store_path + ".quarantine.gate.lock")
            self._quarantine_gate_depth.depth = depth + 1
            try:
                yield
            finally:
                self._quarantine_gate_depth.depth -= 1
                if lease is not None:
                    lease.close()

    def _quarantine_path(self) -> str | None:
        return self._store_path + ".quarantine.json" if self._store_path else None

    def _quarantine_initialized_path(self) -> str | None:
        return self._store_path + ".quarantine.initialized" if self._store_path else None

    def _quarantine_ensure_initialized_locked(self) -> None:
        """Durably remember that a quarantine mark has existed.

        Write the sentinel BEFORE the first active mark. If a crash occurs
        between those writes, the next worker sees a missing mark as corrupt
        rather than silently treating it as no quarantine.
        """
        path = self._quarantine_initialized_path()
        if path is None or os.path.exists(path):
            return
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        if os.name != "nt":
            directory = os.path.dirname(os.path.abspath(path))
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

    def _quarantine_read_locked(self) -> dict:
        """Call only while the shared quarantine_guard is held."""
        path = self._quarantine_path()
        if path is None:
            return dict(self._quarantine_memory)
        if not os.path.exists(path):
            # An untouched store may have no quarantine mark. Once any mark
            # was written, its disappearance is corruption, not an unlock.
            initialized_path = self._quarantine_initialized_path()
            if initialized_path and os.path.exists(initialized_path):
                return {"active": True, "corrupted": True}
            return {"active": False}
        # Migrate existing marks written before the sentinel was introduced.
        # A failed migration must not turn a known mark into a permissive read.
        try:
            self._quarantine_ensure_initialized_locked()
        except OSError:
            return {"active": True, "corrupted": True}
        try:
            with open(path, "r", encoding="utf-8") as stream:
                item = json.load(stream)
        except (OSError, ValueError, TypeError):
            return {"active": True, "corrupted": True}
        if not isinstance(item, dict) or type(item.get("active")) is not bool:
            return {"active": True, "corrupted": True}
        if not isinstance(item.get("lock_id"), str) or not item["lock_id"].strip():
            return {"active": True, "corrupted": True}
        if not item["active"]:
            # A durable inactive mark can exist ONLY after an authorized
            # release. A partial JSON object like {"active": false} must not
            # silently grant admission after data corruption.
            receipt = item.get("release_receipt")
            if (
                not isinstance(receipt, dict)
                or receipt.get("lock_id") != item["lock_id"]
                or not isinstance(receipt.get("authorization_id"), str)
                or not receipt["authorization_id"].strip()
                or not isinstance(receipt.get("authorized_by"), str)
                or not receipt["authorized_by"].strip()
                or not isinstance(receipt.get("scope"), str)
                or not receipt["scope"].strip()
            ):
                return {"active": True, "corrupted": True}
        return item

    def _quarantine_write_locked(self, mark: dict) -> None:
        """Atomic durable replace under quarantine_guard; never writes unlocked."""
        path = self._quarantine_path()
        if path is None:
            self._quarantine_memory = dict(mark)
            return
        # Persist this marker first so a crash/delete never looks like a
        # fresh, never-locked store to independent workers.
        self._quarantine_ensure_initialized_locked()
        directory = os.path.dirname(os.path.abspath(path))
        fd, tmp = tempfile.mkstemp(prefix="quarantine.", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(mark, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
            if os.name != "nt":
                directory_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        self._quarantine_memory = dict(mark)

    def set_quarantine(
        self, active: bool, *, locked_by: str = "",
        incident_id: str | None = None,
    ) -> None:
        """Activate or reassert a lock; never allow unauthenticated release.

        A nonempty incident_id distinguishes a genuinely new incident from
        an idempotent retry of the existing one. A *different* incident ID
        renews the lock identity even when quarantine is already active,
        invalidating release proofs obtained for an earlier incident.
        Without an incident ID, legacy repeated activation stays idempotent.
        """
        if active is not True:
            raise PermissionError(
                "QUARANTINE_LOCK cannot be cleared through set_quarantine; "
                "a trusted, lock-bound authorization verifier is mandatory."
            )
        if incident_id is not None and (
            not isinstance(incident_id, str) or not incident_id.strip()
        ):
            raise ValueError("incident_id must be a nonempty string when provided.")
        with self.quarantine_guard():
            with self._lock:
                old = self._quarantine_read_locked()
                if old.get("corrupted"):
                    raise RuntimeError("QUARANTINE_LOCK mark is corrupt; recovery required.")
                if old["active"] and (
                    incident_id is None or old.get("incident_id") == incident_id
                ):
                    return  # Exact retry or legacy idempotent reassertion.
                mark = {
                    "active": True,
                    "lock_id": uuid4().hex,
                    "locked_by": locked_by,
                }
                if incident_id is not None:
                    mark["incident_id"] = incident_id
                self._quarantine_write_locked(mark)

    def quarantine_is_active(self) -> bool:
        """Fail closed on malformed durable state; reflects all local workers."""
        with self.quarantine_guard():
            with self._lock:
                return bool(self._quarantine_read_locked()["active"])

    def authorized_release_quarantine(
        self, *, authorized_by: str, scope: str,
        authorization: str, reason: str = "",
    ) -> dict[str, Any]:
        """Verify outside the shared gate, then atomically check-and-release.

        Trusted verification may involve slow external I/O. Never hold the
        quarantine gate or registry mutex while calling it: other workers
        must retain access to emergency locking and guarded dispatch checks.
        A lock-id comparison at commit rejects stale or replayed proofs.
        """
        with self.quarantine_guard():
            with self._lock:
                initial = self._quarantine_read_locked()
                if initial.get("corrupted"):
                    return {
                        "released": False,
                        "reason": "QUARANTINE_LOCK mark corrupt; manual recovery required.",
                        "receipt": None,
                    }
                if not initial["active"]:
                    return {
                        "released": False,
                        "reason": "No quarantine lock active.",
                        "receipt": None,
                    }
                expected_lock_id = initial["lock_id"]

        # Never call user-supplied integration code under either critical lock.
        proof = _verify_quarantine_release(
            self._quarantine_release_verifier,
            authorized_by=authorized_by, scope=scope,
            authorization=authorization, reason=reason,
            lock_id=expected_lock_id,
        )
        if proof is None:
            return {
                "released": False,
                "reason": "QUARANTINE_LOCK release denied: authorization not verified.",
                "receipt": None,
            }

        with self.quarantine_guard():
            with self._lock:
                mark = self._quarantine_read_locked()
                if mark.get("corrupted"):
                    return {
                        "released": False,
                        "reason": "QUARANTINE_LOCK mark corrupt; manual recovery required.",
                        "receipt": None,
                    }
                if not mark["active"] or mark.get("lock_id") != expected_lock_id:
                    return {
                        "released": False,
                        "reason": "QUARANTINE_LOCK changed during authorization; fresh verification required.",
                        "receipt": None,
                    }
                receipt = {
                    "authorized_by": proof["principal"],
                    "scope": proof["scope"],
                    # Never put the original credential/bearer token in a
                    # durable receipt. The verifier supplies a safe reference.
                    "authorization": "[redacted]",
                    "authorization_id": proof["authorization_id"],
                    "lock_id": proof["lock_id"],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "reason": reason,
                    "locked_by": mark.get("locked_by", ""),
                    "incident_id": mark.get("incident_id"),
                }
                # Persist unlocked state BEFORE returning a success receipt.
                self._quarantine_write_locked({
                    "active": False,
                    "lock_id": mark["lock_id"],
                    "release_receipt": receipt,
                })
                return {
                    "released": True,
                    "reason": "QUARANTINE_LOCK release verified and persisted.",
                    "receipt": receipt,
                }

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

    @contextmanager
    def settlement_lock(
        self, idempotency_key: str, *, require_persistent: bool = True,
    ):
        """Serialize settlement; require cross-process durability when requested.

        File-backed registries use a per-operation OS lock spanning the full
        read/mutate/persist/transition sequence. An in-memory registry may use
        a per-key process-local lock only for legacy in-memory budget mode;
        callers settling a durable backend must set require_persistent=True.
        """
        if not self._store_path:
            if require_persistent:
                raise RuntimeError(
                    "Durable budget settlement requires a persistent operation registry."
                )
            with self._local_settlement_locks_guard:
                lock = self._local_settlement_locks.setdefault(
                    idempotency_key, threading.Lock(),
                )
            with lock:
                yield
            return
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        lock_path = f"{self._store_path}.{digest}.settlement.lock"
        lock_file = acquire_file_lock(lock_path)
        try:
            yield
        finally:
            lock_file.close()

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
        hold_record: HoldRecord | None = None,
        hold_record_changes: Mapping[str, Any] | None = None,
    ) -> tuple[bool, OperationState | None]:
        """Enforce shared quarantine even for direct registry admission calls.

        Every path to RESERVED or DISPATCHING, including a HoldManager exit
        or a direct registry caller, must linearize against quarantine
        activation. Other transitions remain available for fail-closed
        settlement and recovery. A previously DISPATCHING claim may finish.
        """
        if new_state in (OperationState.RESERVED, OperationState.DISPATCHING):
            with self.quarantine_guard():
                if self.quarantine_is_active():
                    current = self.get(idempotency_key)
                    return False, current.state if current is not None else None
                return self._transition_to_unchecked_quarantine(
                    idempotency_key,
                    new_state,
                    claimed_by=claimed_by,
                    hold_record=hold_record,
                    hold_record_changes=hold_record_changes,
                )
        return self._transition_to_unchecked_quarantine(
            idempotency_key,
            new_state,
            claimed_by=claimed_by,
            hold_record=hold_record,
            hold_record_changes=hold_record_changes,
        )

    def _transition_to_unchecked_quarantine(
        self,
        idempotency_key: str,
        new_state: OperationState,
        claimed_by: str = "",
        *,
        hold_record: HoldRecord | None = None,
        hold_record_changes: Mapping[str, Any] | None = None,
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

        HOLD lifecycle metadata associated with the transition can be passed
        as a new ``hold_record`` or as ``hold_record_changes``. The metadata
        is applied only after the transition is legal and persisted in the
        same registry write as the state change.
        """
        if hold_record is not None and hold_record_changes:
            raise ValueError("provide hold_record or hold_record_changes, not both")
        if hold_record is not None and not isinstance(hold_record, HoldRecord):
            raise TypeError("hold_record must be a HoldRecord")
        changes = dict(hold_record_changes or {})
        allowed_hold_fields = {
            "escalated_at", "escalated_by", "exit_reason", "exit_receipt",
        }
        unknown_hold_fields = set(changes) - allowed_hold_fields
        if unknown_hold_fields:
            raise ValueError(f"unsupported hold record field(s): {sorted(unknown_hold_fields)}")
        for name in ("escalated_at", "escalated_by", "exit_reason"):
            if name in changes and not isinstance(changes[name], str):
                raise TypeError(f"{name} must be a string")
        if "exit_receipt" in changes and changes["exit_receipt"] is not None and not isinstance(changes["exit_receipt"], ExitReceipt):
            raise TypeError("exit_receipt must be an ExitReceipt or None")

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

                if hold_record is not None and record.hold_record is not None:
                    return False, prev_state
                if changes and record.hold_record is None:
                    return False, prev_state

                previous_hold_record = record.hold_record
                previous_claimed_by = record.dispatch_claimed_by
                record.state = new_state
                if hold_record is not None:
                    record.hold_record = hold_record
                elif changes:
                    record.hold_record = replace(record.hold_record, **changes)
                if claimed_by:
                    record.dispatch_claimed_by = claimed_by
                try:
                    self._persist()
                except BaseException:
                    # The rename may or may not have completed before a
                    # persistence error (for example, a directory fsync
                    # failure). Reload the durable record so state and metadata
                    # stay paired with whichever side of the atomic replace won.
                    if self._store_path and os.path.exists(self._store_path):
                        self._load()
                    else:
                        record.state = prev_state
                        record.hold_record = previous_hold_record
                        record.dispatch_claimed_by = previous_claimed_by
                    raise
                return True, prev_state
            finally:
                if flock:
                    flock.close()

    def set_budget_flags(self, idempotency_key: str, **flags) -> bool:
        """Atomically persist budget flags and the durable backend reservation identity."""
        allowed = {"budget_reserved", "budget_committed", "reservation_id"}
        unknown = set(flags) - allowed
        if unknown:
            raise ValueError(f"unknown budget flag(s): {sorted(unknown)}")
        if "reservation_id" in flags and not isinstance(flags["reservation_id"], str):
            raise ValueError("reservation_id must be a string")
        for name in ("budget_reserved", "budget_committed"):
            if name in flags and not isinstance(flags[name], bool):
                raise ValueError(f"{name} must be a bool")

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
                    setattr(record, name, bool(value) if name in ("budget_reserved", "budget_committed") else value)
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
