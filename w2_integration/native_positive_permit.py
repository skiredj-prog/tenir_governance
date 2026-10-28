"""W2.2-AC-04 — positive permit on the native G3.1/G4-A SQLite store.

Uses consensus.sqlite3. Does not rename PositiveG4A into native authority.
Does not change ExperimentalG4AAuthority: HOLD and execution_authorized=false stay.
B3 remains outside the SQLite transaction.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from w2_2b_canonical import request_hash as canon_request_hash

PERMIT_SCHEMA = "w2-positive-permit/v1"
TOPOLOGY_ID = "lab-s1-native-sqlite-single-host-2026-10-28"


def _canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


class NativePositivePermitAuthority:
    def __init__(self, backend: SQLiteConsensusBackend):
        if not isinstance(backend, SQLiteConsensusBackend):
            raise TypeError("SQLiteConsensusBackend required")
        self.backend = backend
        self.db_path = backend._database_path
        self.topology_id = TOPOLOGY_ID
        self.productive = False
        with self._tx() as db:
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='w2_positive_v1_keys'"
            ).fetchone():
                raise RuntimeError("refusing store that contains w2_positive_v1_keys")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS w2_positive_v1_capacity (
                  operation_key TEXT PRIMARY KEY,
                  amount INTEGER NOT NULL,
                  reserved INTEGER NOT NULL DEFAULT 1,
                  settled INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS w2_positive_v1_permits (
                  permit_id TEXT PRIMARY KEY,
                  operation_key TEXT NOT NULL UNIQUE,
                  evaluation_id TEXT NOT NULL,
                  request_hash TEXT NOT NULL,
                  reservation_id TEXT NOT NULL,
                  issued_at TEXT NOT NULL,
                  expires_at TEXT NOT NULL,
                  consumed INTEGER NOT NULL DEFAULT 0,
                  dispatch_claim INTEGER NOT NULL DEFAULT 0,
                  idempotency_key TEXT,
                  payload_json TEXT,
                  signature BLOB NOT NULL,
                  key_id TEXT NOT NULL,
                  schema_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS w2_positive_v1_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  operation_key TEXT NOT NULL,
                  event TEXT NOT NULL,
                  at TEXT NOT NULL,
                  detail TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS w2_block_historical_after_positive
                BEFORE INSERT ON reservations
                WHEN EXISTS (
                  SELECT 1 FROM w2_positive_v1_capacity
                  WHERE operation_key = NEW.idempotency_key
                )
                BEGIN
                  SELECT RAISE(ABORT, 'positive capacity owns this key');
                END;
                CREATE TRIGGER IF NOT EXISTS w2_block_positive_after_historical
                BEFORE INSERT ON w2_positive_v1_capacity
                WHEN EXISTS (
                  SELECT 1 FROM reservations
                  WHERE idempotency_key = NEW.operation_key
                )
                BEGIN
                  SELECT RAISE(ABORT, 'historical reservation owns this key');
                END;
            """)
        self._load_key()

    def _tx(self):
        """Autocommit connection for schema and reads. Not a write transaction."""
        return self.backend._connection()

    def _write(self, operation: str):
        """BEGIN IMMEDIATE … COMMIT, or ROLLBACK if the body raises."""
        return self.backend._transaction(operation)

    def _load_key(self):
        raw_hex = os.environ.get("TENIR_W2_PRIVATE_KEY_HEX")
        if raw_hex:
            try:
                raw = bytes.fromhex(raw_hex.strip())
            except ValueError as exc:
                raise RuntimeError("TENIR_W2_PRIVATE_KEY_HEX is not hex") from exc
            if len(raw) != 32:
                raise RuntimeError("TENIR_W2_PRIVATE_KEY_HEX must be 32 bytes")
            self.private_key = Ed25519PrivateKey.from_private_bytes(raw)
            self.key_custody = "EXTERNAL_HEX"
        else:
            self.private_key = Ed25519PrivateKey.generate()
            self.key_custody = "LAB_EPHEMERAL_MEMORY"
        self.productive = False
        pub = self.private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.key_id = hashlib.sha256(pub).hexdigest()

    def _body(self, permit_id, op, evaluation_id, request_hash, reservation_id, issued_at, expires_at) -> dict:
        return {
            "schema": PERMIT_SCHEMA,
            "permit_id": permit_id,
            "operation_key": op,
            "evaluation_id": evaluation_id,
            "request_hash": request_hash,
            "reservation_id": reservation_id,
            "issued_at": issued_at,
            "expires_at": expires_at,
            "key_id": self.key_id,
            "decision": "ALLOW",
            "scope": "experimental-positive-v1",
        }

    def admit_reserve_issue(self, operation_key: str, evaluation_id: str, payload: dict, amount: int = 100) -> dict:
        request_hash = canon_request_hash(payload)
        issued_at = datetime.now(timezone.utc).isoformat()
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        permit_id = f"permit-{operation_key}"
        reservation_id = f"res-{operation_key}"
        body = self._body(permit_id, operation_key, evaluation_id, request_hash, reservation_id, issued_at, expires_at)
        signature = self.private_key.sign(_canonical(body))
        try:
            with self._write("positive_admit") as db:
                historical = db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('reservations','experimental_native_operations')"
                ).fetchall()
                names = {row["name"] for row in historical}
                if "reservations" in names:
                    prior = db.execute(
                        "SELECT reservation_id FROM reservations WHERE idempotency_key=?",
                        (operation_key,),
                    ).fetchone()
                    if prior:
                        raise RuntimeError("historical I8 reservation exists; positive authority refused")
                if "experimental_native_operations" in names:
                    prior = db.execute(
                        "SELECT operation_key FROM experimental_native_operations WHERE operation_key=?",
                        (operation_key,),
                    ).fetchone()
                    if prior:
                        raise RuntimeError("historical native operation exists; positive authority refused")
                existing = db.execute(
                    "SELECT * FROM w2_positive_v1_permits WHERE operation_key=?", (operation_key,)
                ).fetchone()
                if existing:
                    if existing["request_hash"] != request_hash or existing["consumed"]:
                        raise RuntimeError("contradictory permit")
                    return dict(existing)
                db.execute(
                    "INSERT INTO w2_positive_v1_capacity (operation_key, amount) VALUES (?, ?)",
                    (operation_key, amount),
                )
                if os.environ.get("W2_FAIL_AFTER_CAPACITY") == "1":
                    raise RuntimeError("injected failure after capacity")
                db.execute(
                    """INSERT INTO w2_positive_v1_permits
                       (permit_id, operation_key, evaluation_id, request_hash, reservation_id,
                        issued_at, expires_at, signature, key_id, schema_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (permit_id, operation_key, evaluation_id, request_hash, reservation_id,
                     issued_at, expires_at, signature, self.key_id, PERMIT_SCHEMA),
                )
                db.execute(
                    "INSERT INTO w2_positive_v1_events (operation_key, event, at, detail) VALUES (?, ?, ?, ?)",
                    (operation_key, "permit_issued_after_reservation", issued_at, reservation_id),
                )
        except sqlite3.IntegrityError as exc:
            raise RuntimeError("exclusive authority refused") from exc
        return {"permit_id": permit_id, "operation_key": operation_key, "request_hash": request_hash,
                "reservation_id": reservation_id, "schema": PERMIT_SCHEMA, "execution_authorized_historical": False}

    def consume_and_claim(self, operation_key: str, payload: dict, idempotency_key: str) -> dict:
        request_hash = canon_request_hash(payload)
        now = datetime.now(timezone.utc).isoformat()
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        try:
            with self._write("positive_claim") as db:
                row = db.execute(
                    "SELECT * FROM w2_positive_v1_permits WHERE operation_key=?", (operation_key,)
                ).fetchone()
                if not row or row["consumed"] or row["request_hash"] != request_hash:
                    raise RuntimeError("no valid unconsumed permit")
                if row["expires_at"] <= now:
                    raise RuntimeError("permit expired")
                if row["key_id"] != self.key_id:
                    raise RuntimeError("key_id mismatch")
                body = self._body(row["permit_id"], row["operation_key"], row["evaluation_id"],
                                  row["request_hash"], row["reservation_id"], row["issued_at"], row["expires_at"])
                try:
                    self.private_key.public_key().verify(row["signature"], _canonical(body))
                except (InvalidSignature, ValueError) as exc:
                    raise RuntimeError("permit signature invalid") from exc
                cap = db.execute(
                    "SELECT reserved, settled FROM w2_positive_v1_capacity WHERE operation_key=?",
                    (operation_key,),
                ).fetchone()
                if not cap or not cap["reserved"]:
                    raise RuntimeError("no native reservation")
                updated = db.execute(
                    """UPDATE w2_positive_v1_permits
                       SET consumed=1, dispatch_claim=1, idempotency_key=?, payload_json=?
                       WHERE operation_key=? AND consumed=0 AND request_hash=?""",
                    (idempotency_key, payload_json, operation_key, request_hash),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("atomic claim failed")
                db.execute(
                    "INSERT INTO w2_positive_v1_events (operation_key, event, at, detail) VALUES (?, ?, ?, ?)",
                    (operation_key, "dispatch_claimed", now, idempotency_key),
                )
        except sqlite3.IntegrityError as exc:
            raise RuntimeError("exclusive authority refused") from exc
        if os.environ.get("W2_CRASH_AFTER_NATIVE_CLAIM") == "1":
            os.kill(os.getpid(), __import__("signal").SIGKILL)
        return {"operation_key": operation_key, "idempotency_key": idempotency_key, "claimed": True}

    def writers(self) -> list[dict]:
        return [{
            "component": "NativePositivePermitAuthority",
            "database": self.db_path,
            "tables": ["w2_positive_v1_capacity", "w2_positive_v1_permits", "w2_positive_v1_events"],
            "historical_g4a_modified": False,
            "key_custody": self.key_custody,
            "productive": False,
        }]
