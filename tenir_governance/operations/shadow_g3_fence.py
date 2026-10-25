"""G3 experimental *fenced shadow* handoff; never grants execution.

Single-host cooperative writer fencing only. The JSON registry and candidate
SQLite remain distinct; the shadow is not governor dispatch authority.
A durable INITIALIZED sentinel prevents a deleted marker from reopening
legacy writes. Pre-existing binaries not implementing the fence are NOT safe.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .shadow_budget_parity import ShadowBudgetParityAudit
from .shadow_registry_migration import ShadowMigrationHold


@dataclass(frozen=True)
class G3FenceReport:
    status: str
    epoch: int
    source_sha256: str
    budget_sha256: str
    legacy_writes_fenced: bool
    execution_authorized: bool = False
    candidate_dispatch_authorized: bool = False
    is_production_cutover: bool = False


class G3ShadowFenceCoordinator:
    """Monotonic cutover-readiness marker; no active governor transfer.

    Durable ordering under source writer locks:
      1. Persist sqlite PREPARED row.
      2. Create non-removable-in-protocol initialized sentinel, then marker.
      3. Persist sqlite FENCED_SHADOW_HOLD.
    Each crash point is recoverable in a fail-closed mode. This contract
    assumes all relevant JSON writers use the updated registry implementation;
    an earlier binary and direct filesystem modification are not fenced.
    """

    def __init__(self, preflight: ShadowBudgetParityAudit):
        if not isinstance(preflight, ShadowBudgetParityAudit):
            raise ShadowMigrationHold("G1 parity preflight required")
        self.preflight=preflight
        self.shadow=preflight.shadow
        self.registry=self.shadow.registry
        self.marker=self.registry._store_path + ".authority-fence.json"
        self.sentinel=self.registry._store_path + ".authority-fence.initialized"
        with self.shadow._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS shadow_authority_fence (
                    epoch INTEGER PRIMARY KEY CHECK(epoch=1),
                    fence_id TEXT NOT NULL UNIQUE,
                    source_sha256 TEXT NOT NULL,
                    budget_sha256 TEXT NOT NULL,
                    phase TEXT NOT NULL CHECK(
                        phase IN ('PREPARED','FENCED_SHADOW_HOLD'))
                );
                CREATE TRIGGER IF NOT EXISTS shadow_fence_no_delete
                BEFORE DELETE ON shadow_authority_fence
                BEGIN SELECT RAISE(ABORT,'fence row cannot be deleted'); END;
                CREATE TRIGGER IF NOT EXISTS shadow_fence_bound
                BEFORE UPDATE ON shadow_authority_fence
                WHEN OLD.epoch!=NEW.epoch OR OLD.fence_id!=NEW.fence_id
                  OR OLD.source_sha256!=NEW.source_sha256
                  OR OLD.budget_sha256!=NEW.budget_sha256
                  OR OLD.phase!='PREPARED'
                  OR NEW.phase!='FENCED_SHADOW_HOLD'
                BEGIN SELECT RAISE(ABORT,'fence can only advance once'); END;
            """)

    def _after_prepare(self):
        """Crash-injection seam: SQLite PREPARED durable, marker absent."""

    def _after_marker(self):
        """Crash-injection seam: old writes fenced, SQLite not finalized."""

    @staticmethod
    def _fsync_parent(path: str):
        if os.name!="nt":
            fd=os.open(os.path.dirname(os.path.abspath(path)),os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _write_sentinel_locked(self):
        if os.path.lexists(self.sentinel):
            return
        fd=os.open(self.sentinel,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,"wb") as f:
            f.write(b"TENIR-G3-LEGACY-FENCE-INITIALIZED-v1\n")
            f.flush()
            os.fsync(f.fileno())
        self._fsync_parent(self.sentinel)

    def _write_marker_locked(self, fence_id: str, source_hash: str, budget_hash: str):
        marker={"schema":"tenir.g3.fenced-shadow.v1","epoch":1,
                "fence_id":fence_id,"source_sha256":source_hash,
                "budget_sha256":budget_hash,"execution_authorized":False}
        expected=json.dumps(marker,sort_keys=True,separators=(",",":")).encode()
        if os.path.lexists(self.marker):
            if not os.path.isfile(self.marker) or Path(self.marker).read_bytes()!=expected:
                raise ShadowMigrationHold("G3 marker mismatch or corruption")
            return
        parent=os.path.dirname(os.path.abspath(self.marker))
        fd,path=tempfile.mkstemp(prefix="g3-fence.",suffix=".tmp",dir=parent)
        try:
            with os.fdopen(fd,"wb") as f:
                f.write(expected)
                f.flush()
                os.fsync(f.fileno())
            # PREPARED and sentinel already exist; conservative duplicate
            # protection under registry writer file lock.
            os.replace(path,self.marker)
            self._fsync_parent(self.marker)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def _validate_drained(self, db, snapshot_hash: str, budget_hash: str) -> None:
        # This is an OFFLINE FENCE ceremony, not a live-drain algorithm.
        frozen=db.execute(
            "SELECT snapshot_sha256 FROM shadow_migration_epoch WHERE epoch=1"
        ).fetchone()
        parity=db.execute(
            "SELECT source_sha256,budget_sha256 FROM shadow_budget_parity_epoch WHERE epoch=1"
        ).fetchone()
        if frozen!=(snapshot_hash,) or parity!=(snapshot_hash,budget_hash):
            raise ShadowMigrationHold("G0/G1 baseline not coherent")
        rows=db.execute(
            "SELECT source_state FROM shadow_migration_operations"
        ).fetchall()
        if not rows or any(state not in ("created","hold") for (state,) in rows):
            raise ShadowMigrationHold("in-flight or effect-bearing operation requires drain")
        # Conservative: even historical released/committed reservations
        # block this initial G3 prototype. No effect uncertainty allowed.
        if db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0]:
            raise ShadowMigrationHold("reservation history requires explicit settlement migration")
        for resource,cap,reserved,committed in db.execute(
                "SELECT resource_id,capacity,reserved,committed FROM resources"):
            if Decimal(reserved)!=0 or Decimal(committed)!=0:
                raise ShadowMigrationHold("I8 budget is not drained")
        # Recompute budget digest; a previous parity receipt cannot stand
        # in for checking the current budget view under the SQLite writer lock.
        resources=[
            [rid,str(Decimal(cap)),str(Decimal(reserved)),str(Decimal(committed))]
            for rid,cap,reserved,committed in db.execute(
                "SELECT resource_id,capacity,reserved,committed FROM resources ORDER BY resource_id")
        ]
        current=hashlib.sha256(self.shadow._canonical(
            [["resources",resources],["reservations",[]]])).hexdigest()
        if current!=budget_hash:
            raise ShadowMigrationHold("budget changed since G1 snapshot")

    def fence_shadow(self) -> G3FenceReport:
        # No quarantine or unsafe source claims allowed. G1 validation is
        # independent and may create its own frozen parity epoch if absent.
        self.preflight.verify()
        r=self.registry
        with r.quarantine_guard():
            if r.quarantine_is_active():
                raise ShadowMigrationHold("source quarantine active")
            with r._lock:
                lease=r._lock_file()
                try:
                    _,source_hash=self.shadow._snapshot_locked()
                    with self.shadow._db() as db:
                        db.execute("BEGIN IMMEDIATE")
                        try:
                            old=db.execute(
                                "SELECT fence_id,source_sha256,budget_sha256,phase "
                                "FROM shadow_authority_fence WHERE epoch=1").fetchone()
                            parity=db.execute(
                                "SELECT budget_sha256 FROM shadow_budget_parity_epoch "
                                "WHERE epoch=1").fetchone()
                            if parity is None:
                                raise ShadowMigrationHold("missing G1 parity")
                            budget_hash=parity[0]
                            self._validate_drained(db,source_hash,budget_hash)
                            if old is None:
                                if os.path.lexists(self.sentinel) or os.path.lexists(self.marker):
                                    raise ShadowMigrationHold("orphan fence marker requires manual recovery")
                                fence_id=uuid.uuid4().hex
                                db.execute("""INSERT INTO shadow_authority_fence
                                             VALUES(1,?,?,?,'PREPARED')""",
                                           (fence_id,source_hash,budget_hash))
                            else:
                                fence_id=old[0]
                                if old[1:3]!=(source_hash,budget_hash):
                                    raise ShadowMigrationHold("G3 owner epoch drift")
                            db.execute("COMMIT")
                        except BaseException:
                            if db.in_transaction:
                                db.execute("ROLLBACK")
                            raise
                    self._after_prepare()
                    # Sentinel precedes marker: deletion of marker alone
                    # cannot restore JSON write authority.
                    self._write_sentinel_locked()
                    self._write_marker_locked(fence_id,source_hash,budget_hash)
                    self._after_marker()
                    with self.shadow._db() as db:
                        db.execute("BEGIN IMMEDIATE")
                        try:
                            current=db.execute(
                                "SELECT fence_id,source_sha256,budget_sha256,phase "
                                "FROM shadow_authority_fence WHERE epoch=1").fetchone()
                            if current is None or current[:3]!=(fence_id,source_hash,budget_hash):
                                raise ShadowMigrationHold("G3 epoch changed before finalization")
                            if current[3]=="PREPARED":
                                db.execute("""UPDATE shadow_authority_fence
                                              SET phase='FENCED_SHADOW_HOLD' WHERE epoch=1""")
                            elif current[3]!="FENCED_SHADOW_HOLD":
                                raise ShadowMigrationHold("unknown G3 phase")
                            db.execute("COMMIT")
                        except BaseException:
                            if db.in_transaction:
                                db.execute("ROLLBACK")
                            raise
                    return G3FenceReport(
                        status="FENCED_SHADOW_HOLD",
                        epoch=1,source_sha256=source_hash,
                        budget_sha256=budget_hash,legacy_writes_fenced=True)
                finally:
                    if lease:
                        lease.close()

    def inspect(self) -> G3FenceReport:
        # Never use an inspection to issue a grant. Missing marker or epoch
        # results in HOLD; no repair occurs implicitly from a reader.
        with self.shadow._db() as db:
            epoch=db.execute(
                "SELECT fence_id,source_sha256,budget_sha256,phase "
                "FROM shadow_authority_fence WHERE epoch=1").fetchone()
        if epoch is None or epoch[3]!="FENCED_SHADOW_HOLD":
            raise ShadowMigrationHold("G3 fenced shadow not finalized")
        if not os.path.isfile(self.marker) or not os.path.lexists(self.sentinel):
            raise ShadowMigrationHold("G3 fence files incomplete")
        expected={"schema":"tenir.g3.fenced-shadow.v1","epoch":1,
                  "fence_id":epoch[0],"source_sha256":epoch[1],
                  "budget_sha256":epoch[2],"execution_authorized":False}
        try:
            data=json.loads(Path(self.marker).read_text(encoding="utf-8"))
        except (OSError,ValueError) as exc:
            raise ShadowMigrationHold("unreadable G3 fence marker") from exc
        if data!=expected:
            raise ShadowMigrationHold("G3 fence marker mismatch")
        return G3FenceReport("FENCED_SHADOW_HOLD",1,epoch[1],epoch[2],True)
