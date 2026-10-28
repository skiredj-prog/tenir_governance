"""
W2.2 — Positive G4-A contract implementation (experimental)
G4-A owns admission, I8, signed permit and dispatch claim.
Permit issued AFTER reservation.
Consumption + dispatch intent persisted BEFORE B3 call.
Historical HOLD / execution_authorized=false and local grant remain refused.

Experimental only. Not a wiring of historical ExperimentalG4AAuthority.
"""
from datetime import datetime, timezone, timedelta
import hashlib
import json
import sqlite3
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from w2_2b_canonical import request_hash as canon_request_hash
from i8_ledger import I8Ledger
from lab_s1_bridge import RC01Lab
from proof_provider import ProofProvider


def _canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


class PositivePermit:
    def __init__(self, row: sqlite3.Row):
        self.permit_id = row["permit_id"]
        self.operation_id = row["operation_id"]
        self.evaluation_id = row["evaluation_id"]
        self.request_hash = row["request_hash"]
        self.reservation_id = row["reservation_id"]
        self.issued_at = row["issued_at"]
        self.expires_at = row["expires_at"]
        self.consumed = bool(row["consumed"])
        self.idempotency_key = row["idempotency_key"]
        self.signature = row["signature"]
        self.execution_authorized = True
        self.decision = "ALLOW"


class PositiveG4A:
    """Experimental positive authority. Distinct from historical G4-A."""

    def __init__(self, i8: I8Ledger, rc01: RC01Lab, provider: ProofProvider, permit_db: str):
        self.i8 = i8
        self.rc01 = rc01
        self.provider = provider
        self.conn = sqlite3.connect(permit_db, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.private_key = Ed25519PrivateKey.generate()
        self.public_key = self.private_key.public_key()
        self.public_key_hex = self.public_key.public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        ).hex()
        self.key_id = hashlib.sha256(bytes.fromhex(self.public_key_hex)).hexdigest()
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS permits (
                permit_id TEXT PRIMARY KEY,
                operation_id TEXT NOT NULL UNIQUE,
                evaluation_id TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                reservation_id TEXT NOT NULL,
                issued_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                consumed INTEGER NOT NULL DEFAULT 0,
                idempotency_key TEXT,
                signature BLOB NOT NULL,
                key_id TEXT NOT NULL
            )
        """)
        self.conn.commit()

    def _sign(self, body: dict) -> bytes:
        return self.private_key.sign(_canonical(body))

    def _verify(self, body: dict, signature: bytes) -> None:
        self.public_key.verify(signature, _canonical(body))

    def _body(self, permit_id, op_id, evaluation_id, request_hash, reservation_id, issued_at, expires_at) -> dict:
        return {
            "permit_id": permit_id,
            "operation_id": op_id,
            "evaluation_id": evaluation_id,
            "request_hash": request_hash,
            "reservation_id": reservation_id,
            "issued_at": issued_at,
            "expires_at": expires_at,
            "key_id": self.key_id,
            "decision": "ALLOW",
        }

    def admit_and_reserve(self, op_id: str, evaluation_id: str, payload: dict, amount: int = 100) -> PositivePermit:
        rh = canon_request_hash(payload)
        existing = self.conn.execute(
            "SELECT * FROM permits WHERE operation_id = ?", (op_id,)
        ).fetchone()
        if existing:
            if existing["consumed"]:
                raise RuntimeError("permit already consumed; contradictory reissue refused")
            if existing["request_hash"] != rh:
                raise RuntimeError("contradictory permit reissue refused")
            return PositivePermit(existing)

        self.i8.reserve(op_id, amount)
        reservation_id = f"res-{op_id}"
        self.rc01.transition(op_id, "RESERVED", "positive_admission")
        issued_at = datetime.now(timezone.utc).isoformat()
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        permit_id = f"permit-{op_id}"
        body = self._body(permit_id, op_id, evaluation_id, rh, reservation_id, issued_at, expires_at)
        signature = self._sign(body)
        self.conn.execute(
            """INSERT INTO permits
               (permit_id, operation_id, evaluation_id, request_hash, reservation_id,
                issued_at, expires_at, consumed, signature, key_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
            (permit_id, op_id, evaluation_id, rh, reservation_id, issued_at, expires_at, signature, self.key_id),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM permits WHERE operation_id = ?", (op_id,)).fetchone()
        return PositivePermit(row)

    def consume_and_dispatch(self, op_id: str, payload: dict, idempotency_key: str):
        rh = canon_request_hash(payload)
        now = datetime.now(timezone.utc).isoformat()
        with self.conn:
            row = self.conn.execute(
                "SELECT * FROM permits WHERE operation_id = ?", (op_id,)
            ).fetchone()
            if not row:
                raise RuntimeError("no valid unconsumed permit")
            if row["consumed"]:
                raise RuntimeError("permit already consumed")
            if row["request_hash"] != rh:
                raise RuntimeError("request_hash mismatch on positive path")
            if row["expires_at"] <= now:
                raise RuntimeError("permit expired")
            body = self._body(
                row["permit_id"], row["operation_id"], row["evaluation_id"],
                row["request_hash"], row["reservation_id"], row["issued_at"], row["expires_at"],
            )
            self._verify(body, row["signature"])
            updated = self.conn.execute(
                """UPDATE permits
                   SET consumed = 1, idempotency_key = ?
                   WHERE operation_id = ? AND consumed = 0 AND request_hash = ? AND expires_at > ?""",
                (idempotency_key, op_id, rh, now),
            )
            if updated.rowcount != 1:
                raise RuntimeError("durable permit consumption failed")
        self.rc01.transition(op_id, "DISPATCHING", "permit_consumed_intent_persisted")
        receipt = self.provider.execute(op_id, idempotency_key, payload)
        return receipt
