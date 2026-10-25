"""Atomic single-store *candidate* qualification projection (P0 / issue #67).

An alternative to the non-atomic JSON-registry × SQLite-attempt pair.
One SQLite transaction owns its candidate source projection, attempt, and
qualification event. It is NOT the active governor's operation registry,
and it NEVER authorizes an external effect, retry, budget settlement or commit.
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping

from .attempt_qualification import QualificationError, VerifiedEvidence
from .attempt_attestation import Ed25519AttemptVerifier


@dataclass(frozen=True)
class CandidateAtomicObservation:
    operation_key: str
    attempt_id: str
    lei: str
    operation_digest: str
    attempt_state: str
    candidate_source_state: str
    evidence_id: str | None
    evidence_receipt: str | None
    decision: str = "HOLD"
    execution_authorized: bool = False
    is_governor_operation_state: bool = False


class AtomicAttemptCandidateAuthority:
    """Single-host SQLite candidate for ONE coherent source+attempt authority.

    All changes to candidate source-state and attempt-state occur together.
    No method changes DurableOperationRegistry, calls providers or publishes
    a commit grant. The injected verifier must use authenticated trust roots.
    """

    def __init__(self, db_path: str | Path, verifier: Ed25519AttemptVerifier):
        if not isinstance(verifier, Ed25519AttemptVerifier):
            raise QualificationError("Ed25519AttemptVerifier required")
        self.path = str(db_path)
        self.verifier = verifier
        with self._connection() as db:
            mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode.lower() != "wal":
                raise QualificationError("SQLite WAL unavailable")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS candidate_sources (
                  operation_key TEXT PRIMARY KEY,
                  operation_digest TEXT NOT NULL,
                  lei TEXT NOT NULL UNIQUE,
                  source_state TEXT NOT NULL DEFAULT 'UNKNOWN'
                    CHECK(source_state IN ('UNKNOWN','EFFECT_ATTESTED','NO_EFFECT_ATTESTED')),
                  evidence_id TEXT, receipt TEXT
                );
                CREATE TABLE IF NOT EXISTS candidate_attempts (
                  attempt_id TEXT PRIMARY KEY,
                  operation_key TEXT NOT NULL UNIQUE
                    REFERENCES candidate_sources(operation_key),
                  lei TEXT NOT NULL UNIQUE,
                  nonce TEXT NOT NULL,
                  deadline_ms INTEGER NOT NULL,
                  state TEXT NOT NULL DEFAULT 'UNKNOWN'
                    CHECK(state IN ('UNKNOWN','RESOLVED','FAILED')),
                  evidence_id TEXT, receipt TEXT
                );
                CREATE TABLE IF NOT EXISTS candidate_events (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT,
                  attempt_id TEXT NOT NULL REFERENCES candidate_attempts(attempt_id),
                  kind TEXT NOT NULL,
                  evidence_id TEXT,
                  observed_ms INTEGER NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS source_binding_immutable
                BEFORE UPDATE OF operation_key,operation_digest,lei
                ON candidate_sources
                BEGIN SELECT RAISE(ABORT,'source identity immutable'); END;
                CREATE TRIGGER IF NOT EXISTS attempt_binding_immutable
                BEFORE UPDATE OF attempt_id,operation_key,lei,nonce,deadline_ms
                ON candidate_attempts
                BEGIN SELECT RAISE(ABORT,'attempt identity immutable'); END;
            """)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("PRAGMA synchronous=FULL")
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                self._before_commit()
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def _before_commit(self):
        """Test seam: simulates kill -9 or an injected failure before commit."""

    def begin(self, *, operation_key: str, operation_digest: str,
              attempt_id: str, lei: str, nonce: str, deadline_ms: int):
        identity = (operation_key, attempt_id, lei)
        if any(not isinstance(x, str) or not x.strip() for x in identity):
            raise QualificationError("source/attempt identity required")
        if not isinstance(nonce, str) or len(nonce) < 16 or len(nonce) > 256:
            raise QualificationError("non-empty independently generated nonce required")
        if (not isinstance(operation_digest, str)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", operation_digest)):
            raise QualificationError("valid canonical source digest required")
        if type(deadline_ms) is not int or deadline_ms < 0:
            raise QualificationError("valid deadline required")
        with self._transaction() as db:
            # Replaying a complete identical begin is safe; anything else is
            # a collision. No automatic new attempt is possible (T8 absent).
            old = db.execute("""
                SELECT s.operation_digest, s.lei, a.attempt_id, a.nonce,
                       a.deadline_ms
                FROM candidate_sources s JOIN candidate_attempts a
                  ON a.operation_key=s.operation_key
                WHERE s.operation_key=?""", (operation_key,)).fetchone()
            if old is not None:
                if old == (operation_digest, lei, attempt_id, nonce, deadline_ms):
                    return self._snapshot_locked(db, attempt_id)
                raise QualificationError("source/attempt binding conflict")
            try:
                db.execute("INSERT INTO candidate_sources(operation_key,operation_digest,lei) VALUES(?,?,?)",
                           (operation_key,operation_digest,lei))
                db.execute("INSERT INTO candidate_attempts(attempt_id,operation_key,lei,nonce,deadline_ms) VALUES(?,?,?,?,?)",
                           (attempt_id,operation_key,lei,nonce,deadline_ms))
                db.execute("INSERT INTO candidate_events(attempt_id,kind,observed_ms) VALUES(?,'BEGIN',?)",
                           (attempt_id,deadline_ms))
            except sqlite3.IntegrityError as exc:
                raise QualificationError("duplicate LEI, attempt or source identity") from exc
            return self._snapshot_locked(db, attempt_id)

    def _snapshot_locked(self, db, attempt_id: str) -> CandidateAtomicObservation:
        row = db.execute("""
            SELECT s.operation_key,a.attempt_id,a.lei,s.operation_digest,
                   a.state,s.source_state,a.evidence_id,a.receipt,
                   s.evidence_id,s.receipt,s.lei
            FROM candidate_attempts a JOIN candidate_sources s
              ON s.operation_key=a.operation_key
            WHERE a.attempt_id=?""", (attempt_id,)).fetchone()
        if row is None:
            raise KeyError(attempt_id)
        if row[2] != row[10] or row[6] != row[8] or row[7] != row[9]:
            raise QualificationError("atomic source/attempt projection diverged")
        if (row[4],row[5]) not in (("UNKNOWN","UNKNOWN"),
                                  ("RESOLVED","EFFECT_ATTESTED"),
                                  ("FAILED","NO_EFFECT_ATTESTED")):
            raise QualificationError("invalid candidate transition pair")
        return CandidateAtomicObservation(*row[:8])

    def inspect(self, attempt_id: str) -> CandidateAtomicObservation:
        with self._connection() as db:
            return self._snapshot_locked(db, attempt_id)

    def qualify(self, attempt_id: str, signed: Mapping, *, observed_ms: int):
        if not isinstance(attempt_id,str) or not attempt_id:
            raise QualificationError("attempt ID required")
        if type(observed_ms) is not int or observed_ms < 0:
            raise QualificationError("valid observation clock required")
        try:
            proof = self.verifier(signed)
        except Exception as exc:
            raise QualificationError("authenticated proof rejected") from exc
        if not isinstance(proof, VerifiedEvidence):
            raise QualificationError("verified result required")
        desired_attempt, desired_source = (
            ("RESOLVED","EFFECT_ATTESTED") if proof.outcome=="EFFECT_CONFIRMED"
            else ("FAILED","NO_EFFECT_ATTESTED") if proof.outcome=="NON_EXECUTION_CONFIRMED"
            else (None,None)
        )
        if desired_attempt is None:
            raise QualificationError("invalid attested outcome")
        with self._transaction() as db:
            record = db.execute("""
                SELECT a.attempt_id,a.lei,a.nonce,s.operation_digest,
                       a.state,a.evidence_id,a.receipt,s.source_state,
                       s.evidence_id,s.receipt
                FROM candidate_attempts a JOIN candidate_sources s
                  ON s.operation_key=a.operation_key
                WHERE a.attempt_id=?""",(attempt_id,)).fetchone()
            if record is None:
                raise KeyError(attempt_id)
            if (proof.attempt_id,proof.lei,proof.nonce,proof.operation_digest) != record[:4]:
                raise QualificationError("signed evidence context mismatch")
            current = self._snapshot_locked(db, attempt_id)
            if current.attempt_state != "UNKNOWN":
                if (current.attempt_state == desired_attempt
                        and current.candidate_source_state == desired_source
                        and record[5] == record[8] == proof.evidence_id
                        and record[6] == record[9] == proof.verification_receipt):
                    return current
                raise QualificationError("terminal outcome cannot be overwritten")
            db.execute("""UPDATE candidate_sources SET source_state=?, evidence_id=?,receipt=?
                         WHERE operation_key=?""",
                       (desired_source,proof.evidence_id,proof.verification_receipt,
                        current.operation_key))
            db.execute("""UPDATE candidate_attempts SET state=?,evidence_id=?,receipt=?
                         WHERE attempt_id=?""",
                       (desired_attempt,proof.evidence_id,proof.verification_receipt,attempt_id))
            db.execute("""INSERT INTO candidate_events(attempt_id,kind,evidence_id,observed_ms)
                         VALUES(?,?,?,?)""",
                       (attempt_id,desired_attempt,proof.evidence_id,observed_ms))
            return self._snapshot_locked(db, attempt_id)

    def timeout(self, attempt_id: str, *, observed_ms: int) -> CandidateAtomicObservation:
        if type(observed_ms) is not int or observed_ms < 0:
            raise QualificationError("valid observation timestamp required")
        with self._transaction() as db:
            current = self._snapshot_locked(db,attempt_id)
            if current.attempt_state == "UNKNOWN" and not db.execute(
                    "SELECT 1 FROM candidate_events WHERE attempt_id=? AND kind='TIMEOUT_UNKNOWN'",
                    (attempt_id,)).fetchone():
                db.execute("""INSERT INTO candidate_events(attempt_id,kind,observed_ms)
                             VALUES(?,'TIMEOUT_UNKNOWN',?)""",(attempt_id,observed_ms))
            return self._snapshot_locked(db,attempt_id)

    def events(self, attempt_id: str) -> list[tuple]:
        with self._connection() as db:
            return db.execute("""SELECT kind,evidence_id,observed_ms
                                 FROM candidate_events WHERE attempt_id=?
                                 ORDER BY seq""",(attempt_id,)).fetchall()
