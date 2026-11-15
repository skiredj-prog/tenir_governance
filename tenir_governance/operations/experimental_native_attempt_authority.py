"""P0 #67 slice 2: Ed25519 evidence attached to a real SQLite OperationRecord.

Opt-in single-host lab lane only. Source OperationRecord and I8 budget remain
RESERVED after a proof; an evidence result is NOT permission to dispatch,
commit, release, refund, or admit a new attempt (T8).
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Mapping

from .experimental_native_sqlite_authority import (
    ExperimentalSqliteOperationAuthority, SqliteAuthorityHold,
)
from .native_attempt_attestation import Ed25519AttemptVerifier
from .native_attempt_evidence_types import NativeVerifiedEvidence
from .registry import OperationRecord, OperationState

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class NativeAttemptReceipt:
    attempt_id: str
    operation_key: str
    lei: str
    operation_digest: str
    attempt_state: str
    evidence_id: str | None = None
    verification_receipt: str | None = None
    decision: str = "HOLD"
    execution_authorized: bool = False
    budget_settlement_authorized: bool = False
    is_production_authority: bool = False


class ExperimentalNativeAttemptAuthority(ExperimentalSqliteOperationAuthority):
    """One SQLite file for real native operation snapshot + signed attempt.

    Ed25519 verifier owns independently configured trust roots, revocation
    and clock. This class never fabricates facts about external provider effects.
    """

    def __init__(self, backend, verifier: Ed25519AttemptVerifier, *,
                 experimental_only: bool = False):
        if not isinstance(verifier, Ed25519AttemptVerifier):
            raise SqliteAuthorityHold("independent Ed25519 verifier required")
        super().__init__(backend, experimental_only=experimental_only)
        self.verifier=verifier
        with self.backend._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS experimental_native_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    operation_key TEXT NOT NULL UNIQUE
                        REFERENCES experimental_native_operations(operation_key),
                    lei TEXT NOT NULL UNIQUE,
                    nonce TEXT NOT NULL,
                    operation_digest TEXT NOT NULL,
                    deadline_ms INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'UNKNOWN'
                        CHECK(state IN ('UNKNOWN','RESOLVED','FAILED')),
                    evidence_id TEXT,
                    authority_id TEXT,
                    verification_receipt TEXT,
                    outcome TEXT
                );
                CREATE TABLE IF NOT EXISTS experimental_native_attempt_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    attempt_id TEXT NOT NULL
                        REFERENCES experimental_native_attempts(attempt_id),
                    transition TEXT NOT NULL,
                    evidence_id TEXT,
                    observed_ms INTEGER NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS native_attempt_immutable_identity
                BEFORE UPDATE OF attempt_id,operation_key,lei,nonce,operation_digest,deadline_ms
                ON experimental_native_attempts
                BEGIN SELECT RAISE(ABORT,'native attempt binding immutable'); END;
                CREATE TRIGGER IF NOT EXISTS native_attempt_terminal_immutable
                BEFORE UPDATE ON experimental_native_attempts
                WHEN OLD.state != 'UNKNOWN'
                BEGIN SELECT RAISE(ABORT,'native terminal state immutable'); END;
                CREATE TRIGGER IF NOT EXISTS native_attempt_no_delete
                BEFORE DELETE ON experimental_native_attempts
                BEGIN SELECT RAISE(ABORT,'native attempt deletion prohibited'); END;
                CREATE TRIGGER IF NOT EXISTS native_event_no_update
                BEFORE UPDATE ON experimental_native_attempt_events
                BEGIN SELECT RAISE(ABORT,'native event immutable'); END;
                CREATE TRIGGER IF NOT EXISTS native_event_no_delete
                BEFORE DELETE ON experimental_native_attempt_events
                BEGIN SELECT RAISE(ABORT,'native event deletion prohibited'); END;
            """)

    def _operation_binding_locked(self, db, operation_key: str) -> str:
        if not isinstance(operation_key, str) or not operation_key.startswith("lab:"):
            raise SqliteAuthorityHold("lab: operation key required")
        entry=db.execute(
            "SELECT * FROM experimental_native_operations WHERE operation_key=?",
            (operation_key,),
        ).fetchone()
        if entry is None:
            raise SqliteAuthorityHold("native operation not admitted")
        try:
            import json
            operation=OperationRecord.from_dict(json.loads(entry["record_json"]))
        except (TypeError, ValueError, KeyError) as exc:
            raise SqliteAuthorityHold("corrupt native operation source") from exc
        amount=self.backend._amount(operation.amount)
        if amount is None:
            raise SqliteAuthorityHold("invalid native operation amount")
        # Reuse slice 1's full I8 and record-checksum integrity checks. This
        # comparison is self-consistency, not a trust claim about legacy JSON.
        self._validate_locked(
            db,operation_key,entry["input_sha256"],operation.resource_id,amount
        )
        if not _DIGEST.fullmatch("sha256:"+entry["record_sha256"]):
            raise SqliteAuthorityHold("invalid native operation fingerprint")
        return "sha256:"+entry["record_sha256"]

    @staticmethod
    def _receipt(row) -> NativeAttemptReceipt:
        return NativeAttemptReceipt(
            attempt_id=row["attempt_id"],
            operation_key=row["operation_key"],
            lei=row["lei"],
            operation_digest=row["operation_digest"],
            attempt_state=row["state"],
            evidence_id=row["evidence_id"],
            verification_receipt=row["verification_receipt"],
        )

    def _lookup_locked(self, db, attempt_id: str):
        row=db.execute(
            "SELECT * FROM experimental_native_attempts WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise SqliteAuthorityHold("attempt does not exist")
        binding=self._operation_binding_locked(db,row["operation_key"])
        if binding != row["operation_digest"]:
            raise SqliteAuthorityHold("attempt vs operation digest drift")
        return row

    def begin_attempt(self, *, operation_key: str, attempt_id: str,
                      lei: str, nonce: str, deadline_ms: int) -> NativeAttemptReceipt:
        if (not isinstance(attempt_id,str) or not attempt_id.strip()
                or len(attempt_id)>256 or not isinstance(lei,str)
                or not lei.startswith("lab:") or len(lei)>256
                or not isinstance(nonce,str) or not 16<=len(nonce)<=256
                or type(deadline_ms) is not int or deadline_ms<0):
            raise SqliteAuthorityHold("invalid native attempt binding")
        with self.backend._transaction("experimental_native_attempt_begin") as db:
            operation_digest=self._operation_binding_locked(db,operation_key)
            existing=db.execute(
                "SELECT * FROM experimental_native_attempts WHERE "
                "operation_key=? OR attempt_id=? OR lei=?",
                (operation_key,attempt_id,lei),
            ).fetchall()
            if existing:
                if (len(existing)==1 and existing[0]["attempt_id"]==attempt_id
                        and existing[0]["operation_key"]==operation_key
                        and existing[0]["lei"]==lei
                        and existing[0]["nonce"]==nonce
                        and existing[0]["deadline_ms"]==deadline_ms
                        and existing[0]["operation_digest"]==operation_digest):
                    return self._receipt(existing[0])
                raise SqliteAuthorityHold("attempt/LEI already bound; T8 not implemented")
            db.execute(
                "INSERT INTO experimental_native_attempts "
                "(attempt_id,operation_key,lei,nonce,operation_digest,deadline_ms) "
                "VALUES(?,?,?,?,?,?)",
                (attempt_id,operation_key,lei,nonce,operation_digest,deadline_ms),
            )
            db.execute(
                "INSERT INTO experimental_native_attempt_events "
                "(attempt_id,transition,observed_ms) VALUES(?,'BEGIN',?)",
                (attempt_id,deadline_ms),
            )
            return self._receipt(self._lookup_locked(db,attempt_id))

    def inspect_attempt(self, attempt_id: str) -> NativeAttemptReceipt:
        if not isinstance(attempt_id,str) or not attempt_id.strip():
            raise SqliteAuthorityHold("attempt ID required")
        with self.backend._connection() as db:
            return self._receipt(self._lookup_locked(db,attempt_id))

    def timeout(self, attempt_id: str, *, observed_ms: int) -> NativeAttemptReceipt:
        if type(observed_ms) is not int or observed_ms<0:
            raise SqliteAuthorityHold("valid observation timestamp required")
        with self.backend._transaction("experimental_native_attempt_timeout") as db:
            row=self._lookup_locked(db,attempt_id)
            if (row["state"]=="UNKNOWN" and observed_ms>=row["deadline_ms"]
                    and not db.execute(
                        "SELECT 1 FROM experimental_native_attempt_events WHERE "
                        "attempt_id=? AND transition='TIMEOUT_UNKNOWN'",
                        (attempt_id,),
                    ).fetchone()):
                db.execute(
                    "INSERT INTO experimental_native_attempt_events "
                    "(attempt_id,transition,observed_ms) "
                    "VALUES(?,'TIMEOUT_UNKNOWN',?)",
                    (attempt_id,observed_ms),
                )
            return self._receipt(row)

    def qualify(self, attempt_id: str, raw_evidence: Mapping, *,
                observed_ms: int) -> NativeAttemptReceipt:
        if type(observed_ms) is not int or observed_ms<0:
            raise SqliteAuthorityHold("valid observation timestamp required")
        try:
            verified=self.verifier(raw_evidence)
        except Exception as exc:
            raise SqliteAuthorityHold("signed native evidence rejected") from exc
        if not isinstance(verified,NativeVerifiedEvidence):
            raise SqliteAuthorityHold("typed verified Ed25519 evidence required")
        target={
            "EFFECT_CONFIRMED":"RESOLVED",
            "NON_EXECUTION_CONFIRMED":"FAILED",
        }.get(verified.outcome)
        if target is None:
            raise SqliteAuthorityHold("unknown signed outcome")
        with self.backend._transaction("experimental_native_attempt_qualify") as db:
            row=self._lookup_locked(db,attempt_id)
            if (verified.attempt_id!=row["attempt_id"]
                    or verified.lei!=row["lei"]
                    or verified.nonce!=row["nonce"]
                    or verified.operation_digest!=row["operation_digest"]):
                raise SqliteAuthorityHold("signed evidence/native operation binding mismatch")
            if row["state"]!="UNKNOWN":
                if (row["state"]==target
                        and row["evidence_id"]==verified.evidence_id
                        and row["authority_id"]==verified.authority_id
                        and row["verification_receipt"]==verified.verification_receipt
                        and row["outcome"]==verified.outcome):
                    return self._receipt(row)
                raise SqliteAuthorityHold("terminal attestation cannot be overwritten")
            updated=db.execute(
                "UPDATE experimental_native_attempts SET "
                "state=?,evidence_id=?,authority_id=?,verification_receipt=?,outcome=? "
                "WHERE attempt_id=? AND state='UNKNOWN'",
                (target,verified.evidence_id,verified.authority_id,
                 verified.verification_receipt,verified.outcome,attempt_id),
            )
            if updated.rowcount!=1:
                raise SqliteAuthorityHold("lost atomic attempt claim")
            db.execute(
                "INSERT INTO experimental_native_attempt_events "
                "(attempt_id,transition,evidence_id,observed_ms) VALUES(?,?,?,?)",
                (attempt_id,target,verified.evidence_id,observed_ms),
            )
            # The true native OperationRecord and I8 reservation never change.
            return self._receipt(self._lookup_locked(db,attempt_id))

    def events(self, attempt_id: str) -> list[tuple[str,str|None,int]]:
        with self.backend._connection() as db:
            self._lookup_locked(db,attempt_id)
            rows=db.execute(
                "SELECT transition,evidence_id,observed_ms "
                "FROM experimental_native_attempt_events WHERE attempt_id=? ORDER BY seq",
                (attempt_id,),
            ).fetchall()
            return [(r["transition"],r["evidence_id"],r["observed_ms"]) for r in rows]
