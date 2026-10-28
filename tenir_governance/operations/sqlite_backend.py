"""Single-host ConsensusBackend with one SQLite WAL transaction per mutation.

SQLite is the authoritative store for resources, reservations, and late-effect
compensation records. The budget counters and reservation lifecycle change in
the same `BEGIN IMMEDIATE` transaction, so a crash cannot persist one without
the other. SQLite coordinates processes on one host; it is not a multi-host
consensus system.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterator

from .budget import _decimal_add, _decimal_subtract
from .consensus_backend import (
    CommitResult,
    ConsensusState,
    ReconciliationResult,
    RefundResult,
    ReleaseResult,
    ReservationResult,
)


_RESERVATION_STATES = ("RESERVED", "UNKNOWN", "COMMITTED", "RELEASED", "REFUNDED")


def _decimal(value, *, allow_zero: bool = False) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError, ArithmeticError):
        return None
    if not parsed.is_finite() or parsed < 0 or (parsed == 0 and not allow_zero):
        return None
    parts = parsed.as_tuple()
    if len(parts.digits) > 128 or abs(parts.exponent) > 128 or parsed.adjusted() > 128:
        return None
    return parsed


class SQLiteConsensusBackend:
    """Atomic, cross-process single-host implementation of ConsensusBackend.

    `store_dir` contains `consensus.sqlite3`. Existing `budget.json` and
    `reservations.json` files are imported once only if their accounting
    totals agree exactly and no pending `transaction.json` journal exists.
    Legacy files are preserved as migration evidence.
    """

    def __init__(
        self,
        store_dir: str,
        provider_reconcile: Callable[[str], bool | None] | None = None,
    ):
        self._store_dir = os.path.abspath(store_dir)
        os.makedirs(self._store_dir, exist_ok=True)
        self._database_path = os.path.join(self._store_dir, "consensus.sqlite3")
        self._provider_reconcile = provider_reconcile
        self._initialize()
        self._migrate_legacy_json()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise RuntimeError(f"SQLite WAL mode unavailable (got {mode!r}).")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS resources (
                    resource_id TEXT PRIMARY KEY,
                    capacity TEXT NOT NULL,
                    reserved TEXT NOT NULL,
                    committed TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reservations (
                    reservation_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    resource_id TEXT NOT NULL REFERENCES resources(resource_id),
                    amount TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN
                        ('RESERVED','UNKNOWN','COMMITTED','RELEASED','REFUNDED')),
                    created_at REAL NOT NULL,
                    committed_at REAL,
                    released_at REAL,
                    refunded_at REAL
                );
                CREATE INDEX IF NOT EXISTS reservations_by_resource
                    ON reservations(resource_id, state);
                CREATE TABLE IF NOT EXISTS compensations (
                    compensation_id TEXT PRIMARY KEY,
                    original_reservation_id TEXT NOT NULL UNIQUE
                        REFERENCES reservations(reservation_id),
                    resource_id TEXT NOT NULL REFERENCES resources(resource_id),
                    amount TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('REQUIRED','RESOLVED')),
                    reason TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                PRAGMA user_version = 1;
                """
            )

    @contextmanager
    def _transaction(self, operation: str) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            self._before_commit(operation)
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _before_commit(self, _operation: str) -> None:
        """Fault-injection seam used to verify SQLite rollback behavior."""

    def _migrate_legacy_json(self) -> None:
        budget_path = os.path.join(self._store_dir, "budget.json")
        reservations_path = os.path.join(self._store_dir, "reservations.json")
        pending_path = os.path.join(self._store_dir, "transaction.json")
        if not os.path.exists(budget_path) and not os.path.exists(reservations_path):
            return
        if os.path.exists(pending_path):
            raise RuntimeError(
                "Legacy budget has a pending transaction journal; recover it with "
                "the legacy backend before SQLite migration."
            )
        try:
            with open(budget_path, "r", encoding="utf-8") as stream:
                budgets = json.load(stream)
            with open(reservations_path, "r", encoding="utf-8") as stream:
                reservations = json.load(stream)
            if not isinstance(budgets, dict) or not isinstance(reservations, dict):
                raise ValueError("legacy stores must have object roots")
            normalized_budgets = {}
            for resource_id, record in budgets.items():
                if not isinstance(resource_id, str) or not isinstance(record, dict):
                    raise ValueError("invalid legacy resource record")
                values = {
                    name: _decimal(record.get(name), allow_zero=True)
                    for name in ("capacity", "reserved", "consumed")
                }
                if any(value is None for value in values.values()):
                    raise ValueError(f"invalid budget counters for {resource_id!r}")
                if _decimal_add(values["reserved"], values["consumed"]) > values["capacity"]:
                    raise ValueError(f"I8 invariant violated for {resource_id!r}")
                normalized_budgets[resource_id] = values

            totals = {
                rid: {"reserved": Decimal(0), "committed": Decimal(0)}
                for rid in normalized_budgets
            }
            normalized_reservations = []
            for reservation_id, record in reservations.items():
                if not isinstance(reservation_id, str) or not isinstance(record, dict):
                    raise ValueError("invalid legacy reservation record")
                key = record.get("key")
                resource_id = record.get("resource_id")
                amount = _decimal(record.get("amount"))
                state = record.get("state")
                if (
                    not isinstance(key, str) or not key
                    or resource_id not in totals
                    or amount is None
                    or state not in _RESERVATION_STATES
                ):
                    raise ValueError(f"invalid legacy reservation {reservation_id!r}")
                if state in ("RESERVED", "UNKNOWN"):
                    totals[resource_id]["reserved"] = _decimal_add(
                        totals[resource_id]["reserved"], amount,
                    )
                elif state == "COMMITTED":
                    totals[resource_id]["committed"] = _decimal_add(
                        totals[resource_id]["committed"], amount,
                    )
                normalized_reservations.append((reservation_id, key, resource_id, amount, state, record))

            for resource_id, totals_for_resource in totals.items():
                stored = normalized_budgets[resource_id]
                if (
                    totals_for_resource["reserved"] != stored["reserved"]
                    or totals_for_resource["committed"] != stored["consumed"]
                ):
                    raise ValueError(
                        f"legacy budget and reservation totals disagree for {resource_id!r}"
                    )
        except (OSError, json.JSONDecodeError, TypeError, ValueError, ArithmeticError) as exc:
            raise RuntimeError("Legacy budget stores are invalid; refusing SQLite migration.") from exc

        try:
            with self._transaction("legacy_migration") as connection:
                if connection.execute("SELECT 1 FROM resources LIMIT 1").fetchone():
                    return
                for resource_id, values in normalized_budgets.items():
                    connection.execute(
                        "INSERT INTO resources VALUES (?, ?, ?, ?)",
                        (resource_id, str(values["capacity"]), str(values["reserved"]),
                         str(values["consumed"])),
                    )
                for reservation_id, key, resource_id, amount, state, record in normalized_reservations:
                    connection.execute(
                        """INSERT INTO reservations
                           (reservation_id, idempotency_key, resource_id, amount, state,
                            created_at, committed_at, released_at, refunded_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (reservation_id, key, resource_id, str(amount), state,
                         float(record.get("created_at", 0.0)), record.get("committed_at"),
                         record.get("released_at"), record.get("refunded_at")),
                    )
        except sqlite3.IntegrityError as exc:
            raise RuntimeError("Legacy budget migration conflicted with SQLite state.") from exc

    @staticmethod
    def _amount(value, *, allow_zero: bool = False) -> Decimal | None:
        return _decimal(value, allow_zero=allow_zero)

    def register_resource(self, resource_id: str, capacity) -> str:
        normalized = self._amount(capacity, allow_zero=True)
        if not isinstance(resource_id, str) or not resource_id or normalized is None:
            return "INVALID_CAPACITY"
        with self._transaction("register_resource") as connection:
            existing = connection.execute(
                "SELECT capacity FROM resources WHERE resource_id = ?", (resource_id,),
            ).fetchone()
            if existing:
                return "ALREADY_REGISTERED" if Decimal(existing["capacity"]) == normalized else "RESOURCE_CONFLICT"
            connection.execute(
                "INSERT INTO resources VALUES (?, ?, '0', '0')",
                (resource_id, str(normalized)),
            )
        return "REGISTERED"

    def reserve(self, key: str, amount, resource_id: str) -> ReservationResult:
        normalized = self._amount(amount)
        if normalized is None or not isinstance(key, str) or not key or not isinstance(resource_id, str) or not resource_id:
            return ReservationResult(status="INVALID_AMOUNT" if normalized is None else "INVALID_REQUEST")
        reservation_id = str(uuid.uuid4())
        with self._transaction("reserve") as connection:
            existing = connection.execute(
                "SELECT * FROM reservations WHERE idempotency_key = ?", (key,),
            ).fetchone()
            if existing:
                if Decimal(existing["amount"]) == normalized and existing["resource_id"] == resource_id:
                    return ReservationResult(existing["state"], existing["reservation_id"], normalized)
                return ReservationResult("DUPLICATE_KEY")
            positive_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='w2_positive_v1_capacity'"
            ).fetchone()
            if positive_table and connection.execute(
                "SELECT 1 FROM w2_positive_v1_capacity WHERE operation_key = ?", (key,),
            ).fetchone():
                return ReservationResult("POSITIVE_AUTHORITY_OWNS_KEY")
            resource = connection.execute(
                "SELECT * FROM resources WHERE resource_id = ?", (resource_id,),
            ).fetchone()
            if resource is None:
                return ReservationResult("UNKNOWN_RESOURCE")
            capacity = Decimal(resource["capacity"])
            reserved = Decimal(resource["reserved"])
            committed = Decimal(resource["committed"])
            if _decimal_add(_decimal_add(reserved, committed), normalized) > capacity:
                return ReservationResult("INSUFFICIENT_BUDGET")
            connection.execute(
                "UPDATE resources SET reserved = ? WHERE resource_id = ?",
                (str(_decimal_add(reserved, normalized)), resource_id),
            )
            connection.execute(
                "INSERT INTO reservations (reservation_id,idempotency_key,resource_id,amount,state,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (reservation_id, key, resource_id, str(normalized), "RESERVED", time.time()),
            )
        return ReservationResult("RESERVED", reservation_id, normalized)

    def commit(self, reservation_id: str) -> CommitResult:
        with self._transaction("commit") as connection:
            record = connection.execute(
                "SELECT * FROM reservations WHERE reservation_id = ?", (reservation_id,),
            ).fetchone()
            if record is None:
                return CommitResult("NOT_FOUND")
            if record["state"] == "COMMITTED":
                return CommitResult("ALREADY_COMMITTED")
            if record["state"] != "RESERVED":
                return CommitResult("ERROR")
            resource = connection.execute(
                "SELECT * FROM resources WHERE resource_id = ?", (record["resource_id"],),
            ).fetchone()
            amount = Decimal(record["amount"])
            reserved = Decimal(resource["reserved"])
            if reserved < amount:
                return CommitResult("ERROR")
            connection.execute(
                "UPDATE resources SET reserved = ?, committed = ? WHERE resource_id = ?",
                (str(_decimal_subtract(reserved, amount)),
                 str(_decimal_add(Decimal(resource["committed"]), amount)), record["resource_id"]),
            )
            connection.execute(
                "UPDATE reservations SET state='COMMITTED', committed_at=? WHERE reservation_id=?",
                (time.time(), reservation_id),
            )
        return CommitResult("COMMITTED")

    def release(self, reservation_id: str) -> ReleaseResult:
        with self._transaction("release") as connection:
            record = connection.execute(
                "SELECT * FROM reservations WHERE reservation_id = ?", (reservation_id,),
            ).fetchone()
            if record is None:
                return ReleaseResult("NOT_FOUND")
            if record["state"] == "COMMITTED":
                return ReleaseResult("ALREADY_COMMITTED")
            if record["state"] == "RELEASED":
                return ReleaseResult("ALREADY_RELEASED")
            if record["state"] != "RESERVED":
                return ReleaseResult("ERROR")
            resource = connection.execute(
                "SELECT reserved FROM resources WHERE resource_id = ?", (record["resource_id"],),
            ).fetchone()
            amount, reserved = Decimal(record["amount"]), Decimal(resource["reserved"])
            if reserved < amount:
                return ReleaseResult("ERROR")
            connection.execute(
                "UPDATE resources SET reserved=? WHERE resource_id=?",
                (str(_decimal_subtract(reserved, amount)), record["resource_id"]),
            )
            connection.execute(
                "UPDATE reservations SET state='RELEASED', released_at=? WHERE reservation_id=?",
                (time.time(), reservation_id),
            )
        return ReleaseResult("RELEASED")

    def refund(self, reservation_id: str) -> RefundResult:
        with self._transaction("refund") as connection:
            record = connection.execute(
                "SELECT * FROM reservations WHERE reservation_id = ?", (reservation_id,),
            ).fetchone()
            if record is None:
                return RefundResult("NOT_FOUND")
            if record["state"] == "REFUNDED":
                return RefundResult("ALREADY_REFUNDED")
            if record["state"] != "COMMITTED":
                return RefundResult("NOT_COMMITTED")
            resource = connection.execute(
                "SELECT committed FROM resources WHERE resource_id = ?", (record["resource_id"],),
            ).fetchone()
            amount, committed = Decimal(record["amount"]), Decimal(resource["committed"])
            if committed < amount:
                return RefundResult("ERROR")
            connection.execute(
                "UPDATE resources SET committed=? WHERE resource_id=?",
                (str(_decimal_subtract(committed, amount)), record["resource_id"]),
            )
            connection.execute(
                "UPDATE reservations SET state='REFUNDED', refunded_at=? WHERE reservation_id=?",
                (time.time(), reservation_id),
            )
        return RefundResult("REFUNDED")

    def reconcile(self, key: str) -> ReconciliationResult:
        with self._connection() as connection:
            record = connection.execute(
                "SELECT * FROM reservations WHERE idempotency_key = ?", (key,),
            ).fetchone()
        if record is None:
            return ReconciliationResult("NOT_FOUND")
        if record["state"] == "COMMITTED":
            return ReconciliationResult("CONFIRMED", True)
        if record["state"] == "RELEASED" and self._provider_reconcile is None:
            return ReconciliationResult("FAILED", False)
        if self._provider_reconcile is None:
            if record["state"] in ("RESERVED", "UNKNOWN"):
                with self._transaction("mark_unknown") as connection:
                    connection.execute(
                        "UPDATE reservations SET state='UNKNOWN' WHERE reservation_id=? AND state='RESERVED'",
                        (record["reservation_id"],),
                    )
            return ReconciliationResult("UNRESOLVED")
        try:
            effect_observed = self._provider_reconcile(key)
        except Exception:
            return ReconciliationResult("ERROR")

        with self._transaction("reconcile") as connection:
            current = connection.execute(
                "SELECT * FROM reservations WHERE reservation_id=?", (record["reservation_id"],),
            ).fetchone()
            state = current["state"]
            rid = current["reservation_id"]
            amount = Decimal(current["amount"])
            resource_id = current["resource_id"]
            resource = connection.execute(
                "SELECT * FROM resources WHERE resource_id=?", (resource_id,),
            ).fetchone()
            if effect_observed is True and state == "RELEASED":
                compensation_id = f"compensation:{rid}"
                connection.execute(
                    "INSERT OR IGNORE INTO compensations VALUES (?,?,?,?,?,?,?)",
                    (compensation_id, rid, resource_id, str(amount), "REQUIRED",
                     "External effect confirmed after the reservation was released.", time.time()),
                )
                return ReconciliationResult("RECONCILE_CONFLICT", True, compensation_id)
            if effect_observed is True:
                if state == "COMMITTED":
                    return ReconciliationResult("CONFIRMED", True)
                if state not in ("RESERVED", "UNKNOWN"):
                    return ReconciliationResult("RECONCILE_CONFLICT", True)
                reserved = Decimal(resource["reserved"])
                if reserved < amount:
                    return ReconciliationResult("ERROR", True)
                connection.execute(
                    "UPDATE resources SET reserved=?, committed=? WHERE resource_id=?",
                    (str(_decimal_subtract(reserved, amount)),
                     str(_decimal_add(Decimal(resource["committed"]), amount)), resource_id),
                )
                connection.execute(
                    "UPDATE reservations SET state='COMMITTED', committed_at=? WHERE reservation_id=?",
                    (time.time(), rid),
                )
                return ReconciliationResult("CONFIRMED", True)
            if effect_observed is False:
                if state in ("RESERVED", "UNKNOWN"):
                    reserved = Decimal(resource["reserved"])
                    if reserved < amount:
                        return ReconciliationResult("ERROR", False)
                    connection.execute(
                        "UPDATE resources SET reserved=? WHERE resource_id=?",
                        (str(_decimal_subtract(reserved, amount)), resource_id),
                    )
                    connection.execute(
                        "UPDATE reservations SET state='RELEASED', released_at=? WHERE reservation_id=?",
                        (time.time(), rid),
                    )
                    return ReconciliationResult("FAILED", False)
                if state == "RELEASED":
                    return ReconciliationResult("FAILED", False)
                if state == "COMMITTED":
                    committed = Decimal(resource["committed"])
                    if committed < amount:
                        return ReconciliationResult("ERROR", False)
                    connection.execute(
                        "UPDATE resources SET committed=? WHERE resource_id=?",
                        (str(_decimal_subtract(committed, amount)), resource_id),
                    )
                    connection.execute(
                        "UPDATE reservations SET state='REFUNDED', refunded_at=? WHERE reservation_id=?",
                        (time.time(), rid),
                    )
                    return ReconciliationResult("FAILED", False)
                return ReconciliationResult("RECONCILE_CONFLICT", False)
            return ReconciliationResult("UNRESOLVED")

    def get_state(self, resource_id: str) -> ConsensusState:
        with self._connection() as connection:
            resource = connection.execute(
                "SELECT * FROM resources WHERE resource_id=?", (resource_id,),
            ).fetchone()
            if resource is None:
                return ConsensusState(0, 0, 0, 0)
            reservations = [
                {
                    "reservation_id": row["reservation_id"],
                    "key": row["idempotency_key"],
                    "resource_id": row["resource_id"],
                    "amount": Decimal(row["amount"]),
                    "state": row["state"],
                }
                for row in connection.execute(
                    "SELECT * FROM reservations WHERE resource_id=? ORDER BY created_at, reservation_id",
                    (resource_id,),
                )
            ]
            compensations = [
                {
                    "compensation_id": row["compensation_id"],
                    "original_reservation_id": row["original_reservation_id"],
                    "resource_id": row["resource_id"],
                    "amount": Decimal(row["amount"]),
                    "status": row["status"],
                    "reason": row["reason"],
                }
                for row in connection.execute(
                    "SELECT * FROM compensations WHERE resource_id=? ORDER BY created_at, compensation_id",
                    (resource_id,),
                )
            ]
            capacity = Decimal(resource["capacity"])
            reserved = Decimal(resource["reserved"])
            committed = Decimal(resource["committed"])
            if not all(value.is_finite() and value >= 0 for value in (capacity, reserved, committed)):
                raise RuntimeError("Corrupt SQLite budget accounting state.")
            if _decimal_add(reserved, committed) > capacity:
                raise RuntimeError("SQLite I8 invariant violated; refusing to report state.")
            return ConsensusState(
                available=_decimal_subtract(_decimal_subtract(capacity, reserved), committed),
                reserved=reserved,
                committed=committed,
                capacity=capacity,
                reservations=reservations,
                compensations=compensations,
            )
