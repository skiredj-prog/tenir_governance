"""W2.2-AC-04 — positive permit on the native G3.1/G4-A SQLite store.

Uses consensus.sqlite3. Does not rename PositiveG4A into native authority.
Does not change ExperimentalG4AAuthority: HOLD and execution_authorized=false stay.
B3 remains outside the SQLite transaction.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat, PublicFormat,
)

from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from w2_2b_canonical import request_hash as canon_request_hash

PERMIT_SCHEMA = "w2-positive-permit/v1"
TOPOLOGY_ID = "lab-s1-native-sqlite-single-host-2026-10-28"


def _canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


class NativePositivePermitAuthority:
    def __init__(self, backend: SQLiteConsensusBackend, *, private_key_hex: str | None = None):
        if not isinstance(backend, SQLiteConsensusBackend):
            raise TypeError("SQLiteConsensusBackend required")
        self.backend = backend
        self.db_path = backend._database_path
        self._private_key_hex = private_key_hex
        self.key_custody = "ENV_INJECTED" if (private_key_hex or os.getenv("TENIR_W2_PRIVATE_KEY_HEX")) else "LAB_EPHEMERAL_MEMORY"
        self.topology_id = TOPOLOGY_ID
        with self._tx() as db:
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
                CREATE TABLE IF NOT EXISTS w2_positive_v1_outbox (
                  execution_id TEXT PRIMARY KEY,
                  operation_key TEXT NOT NULL UNIQUE,
                  idempotency_key TEXT NOT NULL UNIQUE,
                  request_hash TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  claim_token TEXT NOT NULL UNIQUE,
                  status TEXT NOT NULL CHECK(status IN ('PENDING','UNKNOWN','CONFIRMED','FAILED')),
                  created_at TEXT NOT NULL
                );
            """)
            self._add_column(db, "w2_positive_v1_permits", "nonce_hash", "TEXT")
            self._add_column(db, "w2_positive_v1_permits", "nonce_status", "TEXT NOT NULL DEFAULT 'ISSUED'")
            self._add_column(db, "w2_positive_v1_permits", "execution_id", "TEXT")
            self._add_column(db, "w2_positive_v1_permits", "audience", "TEXT NOT NULL DEFAULT 'proof-provider'")
        self._load_or_create_key()

    def _tx(self):
        return self._transaction()

    @contextmanager
    def _transaction(self):
        """One authoritative SQLite transaction; reads happen after the write lock."""
        with self.backend._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except Exception:
                db.rollback()
                raise
            else:
                db.commit()

    @staticmethod
    def _add_column(db, table, column, definition):
        cols = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _load_or_create_key(self):
        # Private material is never persisted in the authority database. A
        # deployment must inject it through a secret manager/environment; the
        # no-secret mode is deliberately ephemeral laboratory-only custody.
        secret = os.getenv("TENIR_W2_PRIVATE_KEY_HEX")
        if secret is None:
            secret = getattr(self, "_private_key_hex", None)
        if secret:
            try:
                raw = bytes.fromhex(secret)
                self.private_key = Ed25519PrivateKey.from_private_bytes(raw)
            except (ValueError, TypeError) as exc:
                raise RuntimeError("invalid TENIR_W2_PRIVATE_KEY_HEX; refusing to start") from exc
        else:
            with self.backend._connection() as db:
                if db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='w2_positive_v1_keys'").fetchone():
                    raise RuntimeError("legacy cleartext authority key present; migrate custody before starting")
            self.private_key = Ed25519PrivateKey.generate()
        pub = self.private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.key_id = hashlib.sha256(pub).hexdigest()

    def _body(self, permit_id, op, evaluation_id, request_hash, reservation_id, issued_at, expires_at,
              nonce_hash=None, audience="proof-provider") -> dict:
        return {
            "schema": PERMIT_SCHEMA,
            "permit_id": permit_id,
            "operation_key": op,
            "evaluation_id": evaluation_id,
            "request_hash": request_hash,
            "reservation_id": reservation_id,
            "issued_at": issued_at,
            "expires_at": expires_at,
            "nonce_hash": nonce_hash,
            "audience": audience,
            "key_id": self.key_id,
            "decision": "ALLOW",
            "scope": "experimental-positive-v1",
        }

    def admit_reserve_issue(self, operation_key: str, evaluation_id: str, payload: dict, amount: int = 100,
                            *, approval_id: str | None = None, expires_at: str | None = None) -> dict:
        request_hash = canon_request_hash(payload)
        issued_at = datetime.now(timezone.utc).isoformat()
        expires_at = expires_at or (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        nonce_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        permit_id = f"permit-{operation_key}"
        reservation_id = f"res-{operation_key}"
        body = self._body(permit_id, operation_key, evaluation_id, request_hash, reservation_id, issued_at,
                          expires_at, nonce_hash)
        signature = self.private_key.sign(_canonical(body))
        with self._tx() as db:
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
                if existing["request_hash"] != request_hash or existing["evaluation_id"] != evaluation_id:
                    raise RuntimeError("contradictory permit")
                return dict(existing)
            db.execute(
                "INSERT INTO w2_positive_v1_capacity (operation_key, amount) VALUES (?, ?)",
                (operation_key, amount),
            )
            db.execute(
                """INSERT INTO w2_positive_v1_permits
                   (permit_id, operation_key, evaluation_id, request_hash, reservation_id,
                    issued_at, expires_at, nonce_hash, execution_id, audience, signature, key_id, schema_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (permit_id, operation_key, evaluation_id, request_hash, reservation_id,
                 issued_at, expires_at, nonce_hash, approval_id or permit_id, "proof-provider",
                 signature, self.key_id, PERMIT_SCHEMA),
            )
            db.execute(
                "INSERT INTO w2_positive_v1_events (operation_key, event, at, detail) VALUES (?, ?, ?, ?)",
                (operation_key, "permit_issued_after_reservation", issued_at, reservation_id),
            )
        return {"permit_id": permit_id, "operation_key": operation_key, "request_hash": request_hash,
                "reservation_id": reservation_id, "schema": PERMIT_SCHEMA, "execution_authorized_historical": False}

    def consume_and_claim(self, operation_key: str, payload: dict, idempotency_key: str,
                          *, audience: str = "proof-provider") -> dict:
        request_hash = canon_request_hash(payload)
        now = datetime.now(timezone.utc).isoformat()
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self._tx() as db:
            replay = db.execute(
                "SELECT execution_id, operation_key, idempotency_key, request_hash, claim_token, status "
                "FROM w2_positive_v1_outbox WHERE operation_key=? OR idempotency_key=?",
                (operation_key, idempotency_key),
            ).fetchone()
            if replay:
                if replay["operation_key"] != operation_key or replay["idempotency_key"] != idempotency_key or replay["request_hash"] != request_hash:
                    raise RuntimeError("idempotency key conflict")
                return {"operation_key": operation_key, "idempotency_key": idempotency_key,
                        "execution_id": replay["execution_id"], "claim_token": replay["claim_token"],
                        "status": replay["status"], "claimed": True, "replayed": True}
            row = db.execute(
                "SELECT * FROM w2_positive_v1_permits WHERE operation_key=?", (operation_key,)
            ).fetchone()
            if not row or row["request_hash"] != request_hash:
                raise RuntimeError("no valid unconsumed permit")
            if row["consumed"] or row["nonce_status"] != "ISSUED":
                raise RuntimeError("nonce already consumed")
            if row["audience"] != audience:
                raise RuntimeError("claim audience mismatch")
            if row["expires_at"] <= now:
                raise RuntimeError("permit expired")
            if row["key_id"] != self.key_id:
                raise RuntimeError("key_id mismatch")
            body = self._body(row["permit_id"], row["operation_key"], row["evaluation_id"],
                              row["request_hash"], row["reservation_id"], row["issued_at"], row["expires_at"],
                              row["nonce_hash"], row["audience"])
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
            execution_id = f"exec-{uuid.uuid4().hex}"
            claim_token = secrets.token_urlsafe(32)
            updated = db.execute(
                """UPDATE w2_positive_v1_permits
                   SET consumed=1, dispatch_claim=1, idempotency_key=?, payload_json=?,
                       nonce_status='CONSUMED', execution_id=?
                   WHERE operation_key=? AND consumed=0 AND nonce_status='ISSUED' AND request_hash=?""",
                (idempotency_key, payload_json, execution_id, operation_key, request_hash),
            )
            if updated.rowcount != 1:
                raise RuntimeError("atomic claim failed")
            db.execute(
                "INSERT INTO w2_positive_v1_outbox "
                "(execution_id, operation_key, idempotency_key, request_hash, payload_json, claim_token, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?)",
                (execution_id, operation_key, idempotency_key, request_hash, payload_json, claim_token, now),
            )
            db.execute(
                "INSERT INTO w2_positive_v1_events (operation_key, event, at, detail) VALUES (?, ?, ?, ?)",
                (operation_key, "dispatch_claimed", now, idempotency_key),
            )
        if os.environ.get("W2_CRASH_AFTER_NATIVE_CLAIM") == "1":
            os.kill(os.getpid(), __import__("signal").SIGKILL)
        return {"operation_key": operation_key, "idempotency_key": idempotency_key,
                "execution_id": execution_id, "claim_token": claim_token, "audience": audience,
                "status": "PENDING", "claimed": True}

    def verify_claim(self, claim_token: str, execution_id: str, operation_key: str,
                     payload: dict, audience: str = "proof-provider") -> bool:
        request_hash = canon_request_hash(payload)
        with self._tx() as db:
            row = db.execute("SELECT * FROM w2_positive_v1_outbox WHERE claim_token=?", (claim_token,)).fetchone()
            if not row or row["execution_id"] != execution_id or row["operation_key"] != operation_key:
                return False
            if row["request_hash"] != request_hash or row["status"] not in ("PENDING", "UNKNOWN"):
                return False
            permit = db.execute("SELECT audience FROM w2_positive_v1_permits WHERE execution_id=?", (execution_id,)).fetchone()
            return bool(permit and permit["audience"] == audience)

    def writers(self) -> list[dict]:
        return [{
            "component": "NativePositivePermitAuthority",
            "database": self.db_path,
            "tables": ["w2_positive_v1_capacity", "w2_positive_v1_permits", "w2_positive_v1_events"],
            "key_custody": self.key_custody,
            "production_allowed": self.key_custody == "ENV_INJECTED",
            "historical_g4a_modified": False,
        }]
