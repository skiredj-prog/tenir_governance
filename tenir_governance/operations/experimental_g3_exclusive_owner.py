"""P0 G3 adversarial double-owner gate — *single-host experimental*.

Cooperating JSON writers must cease before the SQLite candidate may reserve
the SAME native operation key. This is a conservative, irreversible lab
ceremony, NOT a production cutover, provider grant, or distributed fence.
Older unpatched binaries/direct filesystem writers can bypass the sentinel;
production remains HOLD until deployment-wide enforcement is independently
demonstrated.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from .experimental_native_sqlite_authority import (
    ExperimentalSqliteOperationAuthority, SqliteAuthorityHold,
    SqliteAdmissionReceipt,
)
from .registry import DurableOperationRegistry, OperationRecord, OperationState
from .sqlite_backend import SQLiteConsensusBackend


@dataclass(frozen=True)
class G3OwnerReceipt:
    epoch: str
    source_sha256: str
    state: str = "SQLITE_LAB_OWNER_HOLD"
    json_writes_fenced: bool = True
    execution_authorized: bool = False
    production_cutover: bool = False


class G3ExperimentalExclusiveOwner:
    """Cooperative single-host G3 owner fencing and lab SQLite admission.

    PREPARED is committed first, sentinel/marker fsynced under the *same*
    cross-process JSON writer lock; ACTIVE is committed last. SQLite may
    stage the reserved operation only after all three agree. A JSON writer
    that acquired its lock first may transition; in that case activation
    must fail on the changed JSON snapshot, and SQLite stays HOLD.
    """

    def __init__(self, json_registry: DurableOperationRegistry,
                 backend: SQLiteConsensusBackend, *,
                 experimental_only: bool = False):
        if experimental_only is not True or not isinstance(json_registry, DurableOperationRegistry):
            raise SqliteAuthorityHold("explicit experimental single-host JSON registry required")
        if not json_registry.is_persistent or not isinstance(backend, SQLiteConsensusBackend):
            raise SqliteAuthorityHold("persistent JSON and SQLite backends required")
        if os.name != "posix":
            raise SqliteAuthorityHold("G3 laboratory file-lock gate is POSIX-only")
        self.registry=json_registry
        self.backend=backend
        self.sqlite=ExperimentalSqliteOperationAuthority(backend,experimental_only=True)
        self.sentinel=json_registry._store_path+".g3-sqlite-owner.initialized"
        self.marker=json_registry._store_path+".g3-sqlite-owner.json"
        with backend._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS experimental_g3_exclusive_epoch(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    epoch TEXT NOT NULL UNIQUE,
                    source_sha256 TEXT NOT NULL,
                    phase TEXT NOT NULL CHECK(
                        phase IN ('PREPARED','ACTIVE_SQLITE_HOLD'))
                );
                CREATE TRIGGER IF NOT EXISTS g3_exclusive_no_delete
                BEFORE DELETE ON experimental_g3_exclusive_epoch
                BEGIN SELECT RAISE(ABORT,'G3 epoch cannot be deleted'); END;
                CREATE TRIGGER IF NOT EXISTS g3_exclusive_monotonic
                BEFORE UPDATE ON experimental_g3_exclusive_epoch
                WHEN OLD.epoch!=NEW.epoch OR OLD.source_sha256!=NEW.source_sha256
                    OR OLD.phase!='PREPARED' OR NEW.phase!='ACTIVE_SQLITE_HOLD'
                BEGIN SELECT RAISE(ABORT,'G3 epoch cannot regress'); END;
            """)

    def _before_sentinel(self):
        """Inject real process crashes after durable PREPARED commit."""

    def _after_sentinel(self):
        """Inject crashes after old JSON writers are blocked, before ACTIVE."""

    @staticmethod
    def _sync_parent(path: str):
        fd=os.open(os.path.dirname(os.path.abspath(path)),os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _source_hash_locked(self):
        if self.registry._corrupted or not os.path.isfile(self.registry._store_path):
            raise SqliteAuthorityHold("missing or corrupt JSON source: HOLD")
        self.registry._load()
        if self.registry._corrupted or not self.registry._records:
            raise SqliteAuthorityHold("source record not trustworthy")
        for key,record in self.registry._records.items():
            if (not key.startswith("lab:") or record.idempotency_key!=key
                    or record.state is not OperationState.CREATED
                    or record.budget_reserved or record.budget_committed
                    or record.reservation_id or record.dispatch_claimed_by
                    or record.commit_dispatch_claim_id):
                raise SqliteAuthorityHold("JSON source already transitioned or not drained")
        data={k:v.to_dict() for k,v in sorted(self.registry._records.items())}
        try:
            raw=json.dumps(data,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
        except (ValueError,TypeError) as exc:
            raise SqliteAuthorityHold("invalid JSON source") from exc
        return hashlib.sha256(raw).hexdigest()

    def _mark_locked(self,epoch,source_hash):
        expected={"schema":"tenir.experimental.g3-exclusive.v1",
                  "epoch":epoch,"source_sha256":source_hash,
                  "execution_authorized":False}
        payload=json.dumps(expected,sort_keys=True,separators=(",",":")).encode()
        if os.path.lexists(self.marker):
            if (not os.path.isfile(self.marker)
                    or Path(self.marker).read_bytes()!=payload):
                raise SqliteAuthorityHold("G3 owner marker drift or corruption")
            return
        directory=os.path.dirname(os.path.abspath(self.marker))
        fd,tmp=tempfile.mkstemp(prefix="g3-owner.",suffix=".tmp",dir=directory)
        try:
            with os.fdopen(fd,"wb") as fp:
                fp.write(payload)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp,self.marker)
            self._sync_parent(self.marker)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _epoch_locked(self):
        with self.backend._connection() as db:
            return db.execute(
                "SELECT epoch,source_sha256,phase "
                "FROM experimental_g3_exclusive_epoch WHERE singleton=1"
            ).fetchone()

    def activate(self) -> G3OwnerReceipt:
        """Strict offline handoff: return HOLD, never an external execution grant."""
        with self.registry.quarantine_guard():
            if self.registry.quarantine_is_active():
                raise SqliteAuthorityHold("source quarantine: HOLD")
            with self.registry._lock:
                lock=self.registry._lock_file()
                try:
                    source_hash=self._source_hash_locked()
                    with self.backend._transaction("experimental_g3_prepare") as db:
                        existing=db.execute(
                            "SELECT epoch,source_sha256,phase "
                            "FROM experimental_g3_exclusive_epoch WHERE singleton=1"
                        ).fetchone()
                        if existing is None:
                            if os.path.lexists(self.sentinel) or os.path.lexists(self.marker):
                                raise SqliteAuthorityHold("orphan G3 marker: HOLD")
                            if db.execute(
                                "SELECT 1 FROM experimental_native_operations LIMIT 1"
                            ).fetchone() or db.execute(
                                "SELECT 1 FROM reservations LIMIT 1"
                            ).fetchone():
                                raise SqliteAuthorityHold("SQLite source already owns a reservation")
                            epoch=uuid.uuid4().hex
                            db.execute(
                                "INSERT INTO experimental_g3_exclusive_epoch "
                                "(singleton,epoch,source_sha256,phase) "
                                "VALUES(1,?,?,'PREPARED')",(epoch,source_hash)
                            )
                        else:
                            epoch,old_hash,phase=existing
                            if old_hash!=source_hash:
                                raise SqliteAuthorityHold("JSON source drift since G3 prepare")
                            if phase=="ACTIVE_SQLITE_HOLD" and (
                                not os.path.isfile(self.sentinel) or
                                not os.path.isfile(self.marker)):
                                raise SqliteAuthorityHold("finalized G3 marker missing: manual recovery")
                    self._before_sentinel()
                    if not os.path.lexists(self.sentinel):
                        fd=os.open(self.sentinel,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                        with os.fdopen(fd,"wb") as fp:
                            fp.write(b"TENIR-G3-SQLITE-LAB-FENCE-v1\n")
                            fp.flush()
                            os.fsync(fp.fileno())
                        self._sync_parent(self.sentinel)
                    self._after_sentinel()
                    self._mark_locked(epoch,source_hash)
                    with self.backend._transaction("experimental_g3_activate") as db:
                        entry=db.execute(
                            "SELECT epoch,source_sha256,phase "
                            "FROM experimental_g3_exclusive_epoch WHERE singleton=1"
                        ).fetchone()
                        if (entry is None or entry["epoch"]!=epoch
                                or entry["source_sha256"]!=source_hash):
                            raise SqliteAuthorityHold("G3 epoch mismatch")
                        if entry["phase"]=="PREPARED":
                            db.execute(
                                "UPDATE experimental_g3_exclusive_epoch "
                                "SET phase='ACTIVE_SQLITE_HOLD' WHERE singleton=1"
                            )
                        elif entry["phase"]!="ACTIVE_SQLITE_HOLD":
                            raise SqliteAuthorityHold("invalid G3 owner phase")
                    return G3OwnerReceipt(epoch=epoch,source_sha256=source_hash)
                finally:
                    if lock:
                        lock.close()

    def candidate_transition(self, record: OperationRecord) -> SqliteAdmissionReceipt:
        """Guarded SQLITE RESERVED claim. NOT provider execution authorization.

        Re-validates marker, source and epoch under the JSON writer lock.
        The SQLite budget/operation claim remains atomic in its own DB TX.
        """
        with self.registry.quarantine_guard():
            if self.registry.quarantine_is_active():
                raise SqliteAuthorityHold("source quarantine: HOLD")
            with self.registry._lock:
                lock=self.registry._lock_file()
                try:
                    source_hash=self._source_hash_locked()
                    epoch=self._epoch_locked()
                    if epoch is None or epoch[2]!="ACTIVE_SQLITE_HOLD":
                        raise SqliteAuthorityHold("G3 exclusive owner not active")
                    if source_hash!=epoch[1]:
                        raise SqliteAuthorityHold("G3 source changed after fencing")
                    if not os.path.isfile(self.sentinel) or not os.path.isfile(self.marker):
                        raise SqliteAuthorityHold("G3 durable fence missing")
                    expect={"schema":"tenir.experimental.g3-exclusive.v1",
                            "epoch":epoch[0],"source_sha256":epoch[1],
                            "execution_authorized":False}
                    try:
                        actual=json.loads(Path(self.marker).read_text(encoding="utf-8"))
                    except (OSError,ValueError) as exc:
                        raise SqliteAuthorityHold("G3 marker corrupt") from exc
                    if actual!=expect:
                        raise SqliteAuthorityHold("G3 marker differs from epoch")
                    if record.idempotency_key not in self.registry._records:
                        raise SqliteAuthorityHold("operation not in frozen legacy inventory")
                    legacy=self.registry._records[record.idempotency_key]
                    if legacy.state is not OperationState.CREATED:
                        raise SqliteAuthorityHold("legacy operation not drained")
                    if legacy.to_dict()!=record.to_dict():
                        raise SqliteAuthorityHold("source identity diverged")
                    return self.sqlite.admit_created(record)
                finally:
                    if lock:
                        lock.close()
