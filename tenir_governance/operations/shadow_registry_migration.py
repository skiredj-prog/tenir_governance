"""Strict, read-only G0/G2 shadow migration of the live JSON operation registry.

Stores a bounded snapshot in a SINGLE SQLite transaction, optionally in the
existing SQLiteConsensusBackend file. It neither changes JSON nor makes SQLite
authoritative for governor execution. No cutover, replay/dispatch or settlement.
The shared single-host lock spans capture and SQLite staging; a lock-free
comparison is NEVER presented as an execution grant.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .registry import DurableOperationRegistry, OperationRecord, OperationState


class ShadowMigrationHold(RuntimeError):
    """Fail closed; human-reviewed rebaseline or explicit recovery required."""


@dataclass(frozen=True)
class ShadowMigrationReport:
    status: str
    records: int
    snapshot_sha256: str
    epoch: int
    execution_authorized: bool = False
    authoritative_source: str = "JSON_REGISTRY"


class ShadowOperationMigrator:
    """Experimental immutable shadow inventory with whole-batch rollback.

    Uses the same file lock as DurableOperationRegistry mutation paths.
    Reuses the candidate SQLite file, but does NOT enter the real operation
    or budget transaction path. Snapshots may only be repeated identically:
    incremental changes are a HOLD, not an unreviewed migration.
    """

    def __init__(self, registry: DurableOperationRegistry, sqlite_path: str | Path,
                 *, max_records: int = 10000):
        if not isinstance(registry, DurableOperationRegistry) or not registry.is_persistent:
            raise ShadowMigrationHold("persistent JSON registry required")
        if type(max_records) is not int or max_records < 1:
            raise ShadowMigrationHold("positive snapshot size limit required")
        self.registry = registry
        self.sqlite_path = str(sqlite_path)
        self.max_records = max_records
        with self._db() as db:
            mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode.lower() != "wal":
                raise ShadowMigrationHold("SQLite WAL required")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS shadow_migration_epoch (
                   epoch INTEGER PRIMARY KEY CHECK(epoch=1),
                   snapshot_sha256 TEXT NOT NULL,
                   item_count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shadow_migration_operations (
                   operation_key TEXT PRIMARY KEY,
                   record_sha256 TEXT NOT NULL,
                   record_json TEXT NOT NULL,
                   source_state TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS shadow_epoch_immutable
                BEFORE UPDATE ON shadow_migration_epoch
                BEGIN SELECT RAISE(ABORT,'shadow epoch immutable'); END;
                CREATE TRIGGER IF NOT EXISTS shadow_operations_immutable
                BEFORE UPDATE ON shadow_migration_operations
                BEGIN SELECT RAISE(ABORT,'shadow operations immutable'); END;
                CREATE TRIGGER IF NOT EXISTS shadow_epoch_no_delete
                BEFORE DELETE ON shadow_migration_epoch
                BEGIN SELECT RAISE(ABORT,'shadow epoch cannot be deleted'); END;
                CREATE TRIGGER IF NOT EXISTS shadow_operations_no_delete
                BEFORE DELETE ON shadow_migration_operations
                BEGIN SELECT RAISE(ABORT,'shadow operation cannot be deleted'); END;
            """)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.sqlite_path, timeout=30, isolation_level=None)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("PRAGMA synchronous=FULL")
            yield db
        finally:
            db.close()

    @staticmethod
    def _canonical(value: object) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")

    def _snapshot_locked(self) -> tuple[dict[str, tuple[str, str, str]], str]:
        """Called while both JSON registry writer locks are held."""
        path = self.registry._store_path
        if not path or not os.path.isfile(path):
            raise ShadowMigrationHold("source file missing")
        # Bounded read and parse; reject malformed or non-canonical records.
        if os.path.getsize(path) > 32 * 1024 * 1024:
            raise ShadowMigrationHold("source JSON too large for bounded shadow import")
        try:
            with open(path, "r", encoding="utf-8") as stream:
                raw = json.load(stream)
            if not isinstance(raw, dict) or not raw:
                raise ShadowMigrationHold("source must be a non-empty object")
            if len(raw) > self.max_records:
                raise ShadowMigrationHold("source inventory exceeds configured limit")
            normalized = {}
            for key, payload in raw.items():
                if not isinstance(key, str) or not key or not isinstance(payload, dict):
                    raise ShadowMigrationHold("invalid record key/shape")
                if payload.get("idempotency_key") != key:
                    raise ShadowMigrationHold("source key/idempotency key mismatch")
                # Round-trip against exact OperationRecord schema. Unknown extra
                # fields would otherwise be silently ignored by from_dict.
                allowed = set(OperationRecord.__dataclass_fields__)
                if set(payload) - allowed:
                    raise ShadowMigrationHold("unknown operation record field")
                record = OperationRecord.from_dict(payload)
                if record.idempotency_key != key or not isinstance(record.state, OperationState):
                    raise ShadowMigrationHold("invalid operation source record")
                encoded = self._canonical(record.to_dict())
                original = self._canonical(payload)
                if encoded != original:
                    raise ShadowMigrationHold("non-round-tripping operation record")
                digest = hashlib.sha256(original).hexdigest()
                normalized[key] = (digest, original.decode("utf-8"),record.state.value)
            rows = [[key, normalized[key][0]] for key in sorted(normalized)]
            overall = hashlib.sha256(self._canonical(rows)).hexdigest()
            return normalized, overall
        except (UnicodeError, ValueError, TypeError, OSError, OverflowError) as exc:
            raise ShadowMigrationHold("cannot prove bounded JSON source integrity") from exc

    def _before_commit(self):
        """Fault-injection seam: run after staging and before SQLite COMMIT."""

    def verify_or_stage(self) -> ShadowMigrationReport:
        # The quarantine gate serializes new RESERVED/DISPATCHING transitions;
        # .lock serializes all registry mutations, including other states.
        # Order must match registry write path: quarantine -> _lock -> file lock.
        with self.registry.quarantine_guard():
            if self.registry.quarantine_is_active():
                raise ShadowMigrationHold("source registry in quarantine")
            with self.registry._lock:
                flock = self.registry._lock_file()
                try:
                    normalized, overall = self._snapshot_locked()
                    with self._db() as db:
                        db.execute("BEGIN IMMEDIATE")
                        try:
                            previous = db.execute(
                                "SELECT snapshot_sha256,item_count FROM shadow_migration_epoch WHERE epoch=1"
                            ).fetchone()
                            if previous is None:
                                count = db.execute(
                                    "SELECT COUNT(*) FROM shadow_migration_operations"
                                ).fetchone()[0]
                                if count:
                                    raise ShadowMigrationHold("orphan shadow rows exist")
                                for key in sorted(normalized):
                                    record_sha,record_json,state=normalized[key]
                                    db.execute("""INSERT INTO shadow_migration_operations
                                          (operation_key,record_sha256,record_json,source_state)
                                          VALUES(?,?,?,?)""",
                                               (key,record_sha,record_json,state))
                                db.execute("""INSERT INTO shadow_migration_epoch
                                      (epoch,snapshot_sha256,item_count) VALUES(1,?,?)""",
                                           (overall,len(normalized)))
                            elif previous != (overall,len(normalized)):
                                raise ShadowMigrationHold("source changed since frozen shadow baseline")
                            actual = db.execute("""SELECT operation_key,record_sha256,record_json,source_state
                                                 FROM shadow_migration_operations ORDER BY operation_key""").fetchall()
                            expected = [(key,*normalized[key]) for key in sorted(normalized)]
                            if actual != expected:
                                raise ShadowMigrationHold("shadow rows drifted or corrupted")
                            self._before_commit()
                            db.execute("COMMIT")
                        except BaseException:
                            if db.in_transaction:
                                db.execute("ROLLBACK")
                            raise
                    return ShadowMigrationReport(
                        status="SHADOW_MATCHED_NO_CUTOVER",
                        records=len(normalized),snapshot_sha256=overall,epoch=1,
                    )
                finally:
                    if flock:
                        flock.close()

    def inspect(self) -> ShadowMigrationReport:
        """Re-evaluate the live source; never trust an old shadow receipt."""
        return self.verify_or_stage()
