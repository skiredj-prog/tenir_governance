"""Opt-in, durable attempt qualification boundary (T6/T7 candidate).

Distinct from OperationState and dispatch. No authorization of external effects.
A verified attestation callback is required; booleans and provider claims alone
never establish terminal qualification. SQLite guarantees are single-host only.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping


class AttemptState(str, Enum):
    UNKNOWN = "UNKNOWN"
    RESOLVED = "RESOLVED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class VerifiedEvidence:
    evidence_id: str
    attempt_id: str
    lei: str
    outcome: str  # EFFECT_CONFIRMED or NON_EXECUTION_CONFIRMED
    authority_id: str
    verification_receipt: str


class QualificationError(ValueError):
    pass


class AttemptQualificationStore:
    """Single-host SQLite state, transactionally bound to evidence references.

    The verifier must validate signed evidence, source authority, nonce,
    scope, freshness and revocation independently. This class does NOT
    implement signature verification or claim production certification.
    """

    def __init__(self, path: str | Path, verifier: Callable[[Mapping], VerifiedEvidence]):
        if not callable(verifier):
            raise TypeError("independent evidence verifier required")
        self.verifier = verifier
        self.path = str(path)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS attempts (
                attempt_id TEXT PRIMARY KEY, lei TEXT NOT NULL, state TEXT NOT NULL,
                deadline_ms INTEGER NOT NULL, evidence_id TEXT, authority_id TEXT,
                verification_receipt TEXT, outcome TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id TEXT NOT NULL, transition TEXT NOT NULL,
                evidence_id TEXT, observed_ms INTEGER NOT NULL)""")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def begin(self, *, attempt_id: str, lei: str, deadline_ms: int):
        if not attempt_id or not lei or type(deadline_ms) is not int or deadline_ms < 0:
            raise QualificationError("invalid attempt identity or deadline")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute(
                "SELECT attempt_id FROM attempts WHERE lei=? AND state='UNKNOWN'", (lei,)
            ).fetchone()
            if active:
                db.rollback()
                raise QualificationError("unresolved LEI blocks additional attempts")
            try:
                db.execute("INSERT INTO attempts(attempt_id,lei,state,deadline_ms) VALUES(?,?,'UNKNOWN',?)",
                           (attempt_id, lei, deadline_ms))
                db.execute("INSERT INTO events(attempt_id,transition,observed_ms) VALUES(?,'BEGIN',?)",
                           (attempt_id, deadline_ms))
            except sqlite3.IntegrityError as exc:
                db.rollback()
                raise QualificationError("duplicate attempt id") from exc
            db.commit()

    def read(self, attempt_id: str) -> dict:
        with self._connect() as db:
            row = db.execute("SELECT attempt_id,lei,state,deadline_ms,evidence_id,authority_id,verification_receipt,outcome FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row is None:
            raise KeyError(attempt_id)
        return dict(zip(("attempt_id","lei","state","deadline_ms","evidence_id","authority_id","verification_receipt","outcome"),row))

    def timeout(self, attempt_id: str, *, observed_ms: int) -> str:
        if type(observed_ms) is not int or observed_ms < 0:
            raise QualificationError("invalid clock")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,deadline_ms FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None:
                raise KeyError(attempt_id)
            if row[0] == "UNKNOWN" and observed_ms >= row[1]:
                db.execute("INSERT INTO events(attempt_id,transition,observed_ms) VALUES(?,'TIMEOUT_UNKNOWN',?)",
                           (attempt_id, observed_ms))
            db.commit()
        return row[0]  # timeout alone is never FAILED

    def qualify(self, attempt_id: str, raw_evidence: Mapping, *, observed_ms: int) -> str:
        if type(observed_ms) is not int or observed_ms < 0:
            raise QualificationError("invalid clock")
        if not isinstance(raw_evidence, Mapping):
            raise QualificationError("evidence mapping required")
        try:
            proof = self.verifier(raw_evidence)
        except Exception as exc:
            raise QualificationError("evidence verification rejected") from exc
        if not isinstance(proof, VerifiedEvidence):
            raise QualificationError("verifier must return VerifiedEvidence")
        if not all((proof.evidence_id,proof.attempt_id,proof.lei,proof.authority_id,proof.verification_receipt)):
            raise QualificationError("incomplete qualified evidence")
        if proof.outcome not in ("EFFECT_CONFIRMED", "NON_EXECUTION_CONFIRMED"):
            raise QualificationError("unsupported evidence outcome")
        target = AttemptState.RESOLVED if proof.outcome == "EFFECT_CONFIRMED" else AttemptState.FAILED
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT lei,state,evidence_id,outcome FROM attempts WHERE attempt_id=?",
                             (attempt_id,)).fetchone()
            if row is None:
                db.rollback()
                raise KeyError(attempt_id)
            if proof.attempt_id != attempt_id or proof.lei != row[0]:
                db.rollback()
                raise QualificationError("attempt/LEI evidence binding mismatch")
            if row[1] != "UNKNOWN":
                if row[1] == target.value and row[2] == proof.evidence_id and row[3] == proof.outcome:
                    db.commit()
                    return row[1]
                db.rollback()
                raise QualificationError("terminal state cannot be rewritten")
            db.execute("UPDATE attempts SET state=?, evidence_id=?,authority_id=?,verification_receipt=?,outcome=? WHERE attempt_id=?",
                       (target.value,proof.evidence_id,proof.authority_id,proof.verification_receipt,proof.outcome,attempt_id))
            db.execute("INSERT INTO events(attempt_id,transition,evidence_id,observed_ms) VALUES(?,?,?,?)",
                       (attempt_id,target.value,proof.evidence_id,observed_ms))
            db.commit()
        return target.value

    def events(self, attempt_id: str) -> list[tuple]:
        with self._connect() as db:
            return db.execute("SELECT transition,evidence_id,observed_ms FROM events WHERE attempt_id=? ORDER BY seq",
                              (attempt_id,)).fetchall()
