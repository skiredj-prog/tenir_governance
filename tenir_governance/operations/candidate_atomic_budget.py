"""G1 experimental atomic *candidate-namespace* budget admission.

One SQLite transaction inserts candidate source + attempt + event, changes
the existing SQLiteConsensusBackend resource counters, and reserves budget.
The API only accepts operation/resource keys beginning "candidate:", and is
NOT connected to the live JSON registry or external dispatch. No G3 fence
ever grants it execution authority. Never use it for production funds.
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .attempt_atomic_candidate import AtomicAttemptCandidateAuthority
from .attempt_attestation import Ed25519AttemptVerifier
from .attempt_qualification import QualificationError
from .sqlite_backend import SQLiteConsensusBackend


class CandidateBudgetHold(QualificationError):
    """Atomic admission denied; never infer a production authorization."""


@dataclass(frozen=True)
class CandidateBudgetReceipt:
    operation_key: str
    attempt_id: str
    reservation_id: str
    resource_id: str
    amount: str
    status: str = "CANDIDATE_RESERVED_HOLD"
    decision: str = "HOLD"
    execution_authorized: bool = False
    source_is_production: bool = False


class CandidateAtomicBudgetAdmission:
    """One-store admission demonstration with strict candidate-only names."""

    def __init__(self, backend: SQLiteConsensusBackend,
                 verifier: Ed25519AttemptVerifier):
        if not isinstance(backend, SQLiteConsensusBackend):
            raise CandidateBudgetHold("SQLiteConsensusBackend required")
        self.backend = backend
        self.attempts = AtomicAttemptCandidateAuthority(
            backend._database_path, verifier
        )

    def _before_commit(self) -> None:
        """Crash-injection seam after every candidate and budget mutation."""

    @staticmethod
    def _candidate_key(value: str, role: str) -> None:
        if (not isinstance(value, str) or not value.startswith("candidate:")
                or len(value) <= len("candidate:") or len(value) > 256):
            raise CandidateBudgetHold(
                f"{role} must belong to isolated candidate: namespace"
            )

    @staticmethod
    def _safe_decimal(value: Any, *, allow_zero: bool = False) -> Decimal:
        parsed = SQLiteConsensusBackend._amount(value, allow_zero=allow_zero)
        if parsed is None:
            raise CandidateBudgetHold("invalid finite bounded monetary amount")
        return parsed

    def _resource_consistent(self, db, resource_id: str):
        row = db.execute(
            "SELECT capacity,reserved,committed FROM resources WHERE resource_id=?",
            (resource_id,),
        ).fetchone()
        if row is None:
            raise CandidateBudgetHold("unknown candidate resource")
        capacity, reserved, committed = (
            self._safe_decimal(row[col], allow_zero=True)
            for col in ("capacity", "reserved", "committed")
        )
        if capacity < reserved + committed:
            raise CandidateBudgetHold("I8 capacity already violated")
        rows = db.execute(
            "SELECT state,amount FROM reservations WHERE resource_id=?",
            (resource_id,),
        ).fetchall()
        expected_reserved=Decimal(0)
        expected_committed=Decimal(0)
        for item in rows:
            amount=self._safe_decimal(item["amount"])
            if item["state"] == "UNKNOWN":
                raise CandidateBudgetHold("uncertain reservation blocks new admission")
            if item["state"] == "RESERVED":
                expected_reserved += amount
            elif item["state"] == "COMMITTED":
                expected_committed += amount
            elif item["state"] not in ("RELEASED", "REFUNDED"):
                raise CandidateBudgetHold("unrecognized reservation state")
        if expected_reserved != reserved or expected_committed != committed:
            raise CandidateBudgetHold("I8 budget counters diverge from reservations")
        if db.execute(
                """SELECT 1 FROM compensations c
                   WHERE c.resource_id=? AND c.status='REQUIRED' LIMIT 1""",
                (resource_id,)).fetchone():
            raise CandidateBudgetHold("unresolved compensation blocks candidate")
        return capacity,reserved,committed

    @staticmethod
    def _reject_fenced_handoff(db) -> None:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='shadow_authority_fence'"
        ).fetchone()
        if exists and db.execute(
                "SELECT 1 FROM shadow_authority_fence LIMIT 1").fetchone():
            raise CandidateBudgetHold(
                "G3 fenced-shadow is HOLD; never promote it to budget admission"
            )

    def admit(self, *, operation_key: str, operation_digest: str,
              attempt_id: str, lei: str, nonce: str,
              deadline_ms: int, resource_id: str, amount: Any
              ) -> CandidateBudgetReceipt:
        self._candidate_key(operation_key, "operation")
        self._candidate_key(resource_id, "resource")
        self._candidate_key(lei, "LEI")
        normalized=self._safe_decimal(amount)
        # This nested constructor-free validation mirrors the existing
        # strict candidate identity requirements.
        if (not isinstance(attempt_id, str) or not attempt_id.strip()
                or not isinstance(nonce, str) or not 16 <= len(nonce) <= 256
                or type(deadline_ms) is not int or deadline_ms < 0):
            raise CandidateBudgetHold("invalid attempt identity, nonce or deadline")
        import re
        if (not isinstance(operation_digest, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", operation_digest) is None):
            raise CandidateBudgetHold("invalid operation SHA-256 binding")

        with self.attempts._transaction() as db:
            self._reject_fenced_handoff(db)
            capacity,reserved,committed=self._resource_consistent(db,resource_id)
            source=db.execute(
                "SELECT operation_digest,lei FROM candidate_sources "
                "WHERE operation_key=?", (operation_key,),
            ).fetchone()
            attempt=db.execute(
                "SELECT operation_key,lei,nonce,deadline_ms FROM candidate_attempts "
                "WHERE attempt_id=?", (attempt_id,),
            ).fetchone()
            budget=db.execute(
                "SELECT reservation_id,resource_id,amount,state "
                "FROM reservations WHERE idempotency_key=?", (operation_key,),
            ).fetchone()
            if source or attempt or budget:
                if not (source and attempt and budget):
                    raise CandidateBudgetHold("partial source/attempt/budget record")
                expected=(operation_digest,lei)
                if (tuple(source)!=expected
                        or tuple(attempt)!=(operation_key,lei,nonce,deadline_ms)
                        or budget["resource_id"]!=resource_id
                        or self._safe_decimal(budget["amount"])!=normalized
                        or budget["state"]!="RESERVED"):
                    raise CandidateBudgetHold("conflicting candidate replay")
                snapshot=self.attempts._snapshot_locked(db,attempt_id)
                if (snapshot.attempt_state!="UNKNOWN"
                        or snapshot.candidate_source_state!="UNKNOWN"):
                    raise CandidateBudgetHold("qualified candidate is not re-admissible")
                return CandidateBudgetReceipt(
                    operation_key,attempt_id,budget["reservation_id"],
                    resource_id,str(normalized),
                )
            if reserved + committed + normalized > capacity:
                raise CandidateBudgetHold("INSUFFICIENT_BUDGET")
            rid=uuid.uuid4().hex
            try:
                db.execute(
                    "INSERT INTO candidate_sources(operation_key,operation_digest,lei) "
                    "VALUES(?,?,?)", (operation_key,operation_digest,lei),
                )
                db.execute(
                    "INSERT INTO candidate_attempts("
                    "attempt_id,operation_key,lei,nonce,deadline_ms) "
                    "VALUES(?,?,?,?,?)",
                    (attempt_id,operation_key,lei,nonce,deadline_ms),
                )
                db.execute(
                    "INSERT INTO candidate_events(attempt_id,kind,observed_ms) "
                    "VALUES(?,'BEGIN',?)", (attempt_id,deadline_ms),
                )
                db.execute(
                    "INSERT INTO reservations("
                    "reservation_id,idempotency_key,resource_id,amount,state,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (rid,operation_key,resource_id,str(normalized),"RESERVED",time.time()),
                )
                db.execute(
                    "UPDATE resources SET reserved=? WHERE resource_id=?",
                    (str(reserved+normalized),resource_id),
                )
            except sqlite3.IntegrityError as exc:
                raise CandidateBudgetHold("duplicate candidate/budget identity") from exc
            self._resource_consistent(db,resource_id)
            self._before_commit()
            return CandidateBudgetReceipt(
                operation_key,attempt_id,rid,resource_id,str(normalized),
            )
