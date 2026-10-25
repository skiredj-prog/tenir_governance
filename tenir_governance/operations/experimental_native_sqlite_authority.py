"""P0 #67 slice 1: bounded single-host SQLite authority *admission*.

Not wired into DualKernelGovernor. Never calls providers or issues a commit
grant. This experimental vertical slice demonstrates that a real OperationRecord
and an existing SQLiteConsensusBackend reservation can be written atomically.

Requires a disposable backend and explicit opt-in. It is NOT a migration or
a production alternative to DurableOperationRegistry.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, replace
from decimal import Decimal

from .registry import OperationRecord, OperationState
from .sqlite_backend import SQLiteConsensusBackend


class SqliteAuthorityHold(RuntimeError):
    """Inconsistent, ineligible, or non-authoritative operation: HOLD."""


@dataclass(frozen=True)
class SqliteAdmissionReceipt:
    operation_key: str
    reservation_id: str
    resource_id: str
    amount: str
    operation_state: str = "RESERVED"
    budget_state: str = "RESERVED"
    decision: str = "HOLD"
    execution_authorized: bool = False
    is_production_authority: bool = False


class ExperimentalSqliteOperationAuthority:
    """A deliberately narrow G1 operation+I8 reservation transaction.

    Only newly-created lab: operations are admitted, never migrated JSON
    operations. No transition to DISPATCHING, no T6/T7 qualification, no
    settlement, no T8 retry and no cutover epoch are exposed.
    """

    def __init__(self, backend: SQLiteConsensusBackend, *,
                 experimental_only: bool = False):
        if experimental_only is not True:
            raise SqliteAuthorityHold("experimental_only=True required")
        if not isinstance(backend, SQLiteConsensusBackend):
            raise SqliteAuthorityHold("SQLiteConsensusBackend required")
        self.backend = backend
        with backend._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS experimental_native_operations (
                  operation_key TEXT PRIMARY KEY,
                  input_sha256 TEXT NOT NULL,
                  record_sha256 TEXT NOT NULL,
                  record_json TEXT NOT NULL,
                  reservation_id TEXT NOT NULL UNIQUE
                    REFERENCES reservations(reservation_id),
                  state TEXT NOT NULL CHECK(state='RESERVED')
                );
                CREATE TRIGGER IF NOT EXISTS experimental_native_no_update
                BEFORE UPDATE ON experimental_native_operations
                BEGIN SELECT RAISE(ABORT,'admitted record is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS experimental_native_no_delete
                BEFORE DELETE ON experimental_native_operations
                BEGIN SELECT RAISE(ABORT,'admitted record cannot be deleted'); END;
            """)

    @staticmethod
    def _canonical(data: dict) -> bytes:
        try:
            return json.dumps(data, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, UnicodeError) as exc:
            raise SqliteAuthorityHold("operation record is not canonical JSON") from exc

    @staticmethod
    def _sha(blob: bytes) -> str:
        return hashlib.sha256(blob).hexdigest()

    def _input(self, record: OperationRecord) -> tuple[str, str, Decimal]:
        if not isinstance(record, OperationRecord):
            raise SqliteAuthorityHold("native OperationRecord required")
        if record.state is not OperationState.CREATED:
            raise SqliteAuthorityHold("only CREATED operations are admissible")
        for field in ("idempotency_key", "passage_id", "agent_id",
                      "action_type", "resource_id"):
            value = getattr(record, field)
            if not isinstance(value, str) or not value.strip():
                raise SqliteAuthorityHold("incomplete operation identity")
        if (not record.idempotency_key.startswith("lab:")
                or not record.resource_id.startswith("lab:")
                or len(record.idempotency_key)>256):
            raise SqliteAuthorityHold("disposable lab: operation and resource required")
        if (record.reservation_id or record.budget_reserved
                or record.budget_committed or record.dispatch_claimed_by
                or record.commit_dispatch_claim_id):
            raise SqliteAuthorityHold("existing authorization/reservation forbidden")
        if (not isinstance(record.payload_hash, str)
                or not re.fullmatch(r"[0-9a-f]{64}",record.payload_hash)):
            raise SqliteAuthorityHold("canonical payload SHA-256 required")
        amount=self.backend._amount(record.amount)
        if amount is None:
            raise SqliteAuthorityHold("invalid positive amount")
        canonical=self._canonical(record.to_dict())
        return record.idempotency_key,self._sha(canonical),amount

    def _verify_budget_locked(self, db, resource_id: str) -> tuple[Decimal, Decimal, Decimal]:
        """I8 replay-safe check before claiming that a local binding matches."""
        if db.execute(
            "SELECT 1 FROM resources WHERE resource_id NOT LIKE 'lab:%' LIMIT 1"
        ).fetchone():
            raise SqliteAuthorityHold("shared non-laboratory budget backend forbidden")
        if db.execute(
            "SELECT 1 FROM reservations WHERE idempotency_key NOT LIKE 'lab:%' LIMIT 1"
        ).fetchone():
            raise SqliteAuthorityHold("shared non-laboratory reservation forbidden")
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='shadow_authority_fence'"
        ).fetchone() and db.execute(
            "SELECT 1 FROM shadow_authority_fence LIMIT 1"
        ).fetchone():
            raise SqliteAuthorityHold("G3 fenced shadow does not grant authority")
        budget=db.execute(
            "SELECT capacity,reserved,committed FROM resources WHERE resource_id=?",
            (resource_id,),
        ).fetchone()
        if budget is None:
            raise SqliteAuthorityHold("missing lab budget")
        numbers=tuple(
            self.backend._amount(budget[name],allow_zero=True)
            for name in ("capacity","reserved","committed")
        )
        if any(x is None for x in numbers):
            raise SqliteAuthorityHold("invalid budget counters")
        capacity,reserved,committed=numbers
        if reserved+committed>capacity:
            raise SqliteAuthorityHold("I8 counters already inconsistent")
        if db.execute(
            "SELECT 1 FROM compensations WHERE resource_id=? "
            "AND status='REQUIRED' LIMIT 1", (resource_id,)
        ).fetchone():
            raise SqliteAuthorityHold("pending compensation")
        totals={"RESERVED":Decimal(0),"COMMITTED":Decimal(0)}
        for row in db.execute(
            "SELECT amount,state FROM reservations WHERE resource_id=?",
            (resource_id,)
        ):
            value=self.backend._amount(row["amount"])
            if value is None:
                raise SqliteAuthorityHold("invalid reservation amount")
            if row["state"] in totals:
                totals[row["state"]]+=value
            elif row["state"] not in ("RELEASED","REFUNDED"):
                raise SqliteAuthorityHold("unknown effect/reservation lifecycle")
        if totals["RESERVED"]!=reserved or totals["COMMITTED"]!=committed:
            raise SqliteAuthorityHold("I8 counter/ledger divergence")
        return capacity,reserved,committed

    def _validate_locked(self, db, key: str, input_hash: str,
                         resource_id: str, amount: Decimal) -> SqliteAdmissionReceipt:
        entry=db.execute(
            "SELECT * FROM experimental_native_operations WHERE operation_key=?",
            (key,),
        ).fetchone()
        existing=db.execute(
            "SELECT * FROM reservations WHERE idempotency_key=?", (key,)
        ).fetchone()
        if entry is None or existing is None:
            raise SqliteAuthorityHold("unowned or partially committed operation")
        self._verify_budget_locked(db,resource_id)
        if (entry["input_sha256"]!=input_hash
                or entry["reservation_id"]!=existing["reservation_id"]
                or existing["resource_id"]!=resource_id
                or self.backend._amount(existing["amount"])!=amount
                or existing["state"]!="RESERVED"):
            raise SqliteAuthorityHold("operation/budget replay or state mismatch")
        blob=entry["record_json"].encode("utf-8")
        if self._sha(blob)!=entry["record_sha256"]:
            raise SqliteAuthorityHold("operation record checksum mismatch")
        try:
            persisted=json.loads(entry["record_json"])
            loaded=OperationRecord.from_dict(persisted)
        except (ValueError, TypeError, KeyError) as exc:
            raise SqliteAuthorityHold("invalid persisted OperationRecord") from exc
        if (loaded.idempotency_key!=key or loaded.state is not OperationState.RESERVED
                or not loaded.budget_reserved or loaded.budget_committed
                or loaded.reservation_id!=entry["reservation_id"]
                or loaded.resource_id!=resource_id
                or self.backend._amount(loaded.amount)!=amount):
            raise SqliteAuthorityHold("native operation state/budget linkage drift")
        return SqliteAdmissionReceipt(
            operation_key=key,
            reservation_id=entry["reservation_id"],
            resource_id=resource_id,
            amount=str(amount),
        )

    def admit_created(self, record: OperationRecord) -> SqliteAdmissionReceipt:
        key,input_hash,amount=self._input(record)
        with self.backend._transaction("experimental_native_admit") as db:
            entry=db.execute(
                "SELECT 1 FROM experimental_native_operations WHERE operation_key=?",
                (key,),
            ).fetchone()
            existing=db.execute(
                "SELECT 1 FROM reservations WHERE idempotency_key=?", (key,)
            ).fetchone()
            if entry or existing:
                return self._validate_locked(
                    db,key,input_hash,record.resource_id,amount
                )
            capacity,reserved,committed=self._verify_budget_locked(
                db,record.resource_id
            )
            if reserved+committed+amount>capacity:
                raise SqliteAuthorityHold("INSUFFICIENT_BUDGET")
            reservation_id=uuid.uuid4().hex
            new_record=replace(
                record,state=OperationState.RESERVED,
                budget_reserved=True,reservation_id=reservation_id,
            )
            canonical=self._canonical(new_record.to_dict())
            db.execute(
                "INSERT INTO reservations "
                "(reservation_id,idempotency_key,resource_id,amount,state,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (reservation_id,key,record.resource_id,str(amount),
                 "RESERVED",time.time()),
            )
            db.execute(
                "UPDATE resources SET reserved=? WHERE resource_id=?",
                (str(reserved+amount),record.resource_id),
            )
            db.execute(
                "INSERT INTO experimental_native_operations "
                "(operation_key,input_sha256,record_sha256,record_json,"
                "reservation_id,state) VALUES(?,?,?,?,?,'RESERVED')",
                (key,input_hash,self._sha(canonical),
                 canonical.decode("utf-8"),reservation_id),
            )
            return self._validate_locked(
                db,key,input_hash,record.resource_id,amount
            )

    def inspect(self, record: OperationRecord) -> SqliteAdmissionReceipt:
        """Read-only binding verification. Never authorizes dispatch."""
        key,digest,amount=self._input(record)
        with self.backend._connection() as db:
            return self._validate_locked(
                db,key,digest,record.resource_id,amount
            )
