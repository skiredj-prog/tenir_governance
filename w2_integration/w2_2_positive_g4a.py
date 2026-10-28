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
import os
import signal
import sqlite3
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat, PublicFormat,
)
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
                signature BLOB,
                key_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'intent',
                dispatch_claim INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS authority_keys (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                private_key BLOB NOT NULL,
                key_id TEXT NOT NULL
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS recovery_holds (
                operation_id TEXT PRIMARY KEY,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        self.conn.commit()
        # Lab only: the private key is reloaded from this database in clear.
        # This is not independent custody and not a privilege boundary.
        self.key_custody = "LAB_CLEARTEXT_SQLITE"
        self._load_or_create_key()
        self.recover()

    def _load_or_create_key(self):
        row = self.conn.execute("SELECT private_key, key_id FROM authority_keys WHERE id = 1").fetchone()
        if row:
            self.private_key = Ed25519PrivateKey.from_private_bytes(row["private_key"])
            self.public_key = self.private_key.public_key()
            pub = self.public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
            self.public_key_hex = pub.hex()
            self.key_id = hashlib.sha256(pub).hexdigest()
            if self.key_id != row["key_id"]:
                raise RuntimeError("persisted authority key_id mismatch")
            return
        self.private_key = Ed25519PrivateKey.generate()
        self.public_key = self.private_key.public_key()
        raw = self.private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        pub = self.public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.public_key_hex = pub.hex()
        self.key_id = hashlib.sha256(pub).hexdigest()
        self.conn.execute(
            "INSERT INTO authority_keys (id, private_key, key_id) VALUES (1, ?, ?)",
            (raw, self.key_id),
        )
        self.conn.commit()

    def recover(self):
        """Complete our own intents. Never delete a reservation that has no permit."""
        intents = self.conn.execute("SELECT * FROM permits WHERE status = 'intent'").fetchall()
        for row in intents:
            op_id = row["operation_id"]
            if self.i8.reservation_count(op_id):
                self._issue_from_intent(row)
            else:
                self.conn.execute(
                    "DELETE FROM permits WHERE permit_id = ? AND status = 'intent'",
                    (row["permit_id"],),
                )
                self.conn.commit()
        reserved = self.i8.conn.execute(
            "SELECT operation_id FROM ledger WHERE reserved > 0 AND settled = 0"
        ).fetchall()
        for (op_id,) in reserved:
            permit = self.conn.execute(
                "SELECT status FROM permits WHERE operation_id = ?", (op_id,)
            ).fetchone()
            if permit is None:
                self._hold(op_id, "reservation_without_permit_not_abandoned")
        claimed = self.conn.execute(
            "SELECT * FROM permits WHERE consumed = 1 AND dispatch_claim = 1"
        ).fetchall()
        for row in claimed:
            self._repair_dispatch_claim(row)

    def _hold(self, op_id: str, reason: str):
        self.conn.execute(
            """INSERT OR IGNORE INTO recovery_holds (operation_id, reason, created_at)
               VALUES (?, ?, ?)""",
            (op_id, reason, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def _project_dispatching(self, op_id: str):
        state = self.rc01.get_state(op_id)
        if state in (None, "RESERVED", "PRE_ADMITTED"):
            self.rc01.transition(op_id, "DISPATCHING", "dispatch_claim_projected")

    def _repair_dispatch_claim(self, row):
        """Replay only an authenticated claim. Expiry after consumption does not block it."""
        op_id = row["operation_id"]
        if not self._claim_authenticated(row):
            return
        if self.provider.execute_count(op_id) > 0:
            self._project_dispatching(op_id)
            return
        if not row["payload_json"] or not row["idempotency_key"]:
            self._hold(op_id, "dispatch_claim_without_replay_material")
            return
        payload = json.loads(row["payload_json"])
        if canon_request_hash(payload) != row["request_hash"]:
            self._hold(op_id, "dispatch_claim_payload_hash_mismatch")
            return
        self._project_dispatching(op_id)
        self.provider.execute(op_id, row["idempotency_key"], payload)

    def _claim_authenticated(self, row) -> bool:
        op_id = row["operation_id"]
        if row["status"] != "issued" or not row["signature"] or row["key_id"] != self.key_id:
            self._hold(op_id, "dispatch_claim_unauthenticated")
            return False
        body = self._body(
            row["permit_id"], row["operation_id"], row["evaluation_id"],
            row["request_hash"], row["reservation_id"], row["issued_at"], row["expires_at"],
        )
        try:
            self._verify(body, row["signature"])
        except (InvalidSignature, ValueError):
            self._hold(op_id, "dispatch_claim_signature_invalid")
            return False
        if self.i8.reservation_count(op_id) == 0:
            self._hold(op_id, "dispatch_claim_without_reservation")
            return False
        return True

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

    def _issue_from_intent(self, row):
        issued_at = datetime.now(timezone.utc).isoformat()
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        body = self._body(
            row["permit_id"], row["operation_id"], row["evaluation_id"],
            row["request_hash"], row["reservation_id"], issued_at, expires_at,
        )
        signature = self._sign(body)
        self.conn.execute(
            """UPDATE permits
               SET status = 'issued', signature = ?, issued_at = ?, expires_at = ?, key_id = ?
               WHERE permit_id = ? AND status = 'intent'""",
            (signature, issued_at, expires_at, self.key_id, row["permit_id"]),
        )
        self.conn.commit()

    def admit_and_reserve(self, op_id: str, evaluation_id: str, payload: dict, amount: int = 100) -> PositivePermit:
        rh = canon_request_hash(payload)
        existing = self.conn.execute(
            "SELECT * FROM permits WHERE operation_id = ?", (op_id,)
        ).fetchone()
        if existing and existing["status"] == "issued":
            if existing["consumed"]:
                raise RuntimeError("permit already consumed; contradictory reissue refused")
            if existing["request_hash"] != rh:
                raise RuntimeError("contradictory permit reissue refused")
            return PositivePermit(existing)
        if existing and existing["request_hash"] != rh:
            raise RuntimeError("contradictory permit reissue refused")

        reservation_id = f"res-{op_id}"
        permit_id = f"permit-{op_id}"
        if not existing:
            # Durable intent BEFORE reservation so a crash cannot lose the admission.
            now = datetime.now(timezone.utc).isoformat()
            self.conn.execute(
                """INSERT INTO permits
                   (permit_id, operation_id, evaluation_id, request_hash, reservation_id,
                    issued_at, expires_at, consumed, signature, key_id, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, 'intent')""",
                (permit_id, op_id, evaluation_id, rh, reservation_id, now, now, self.key_id),
            )
            self.conn.commit()
            existing = self.conn.execute(
                "SELECT * FROM permits WHERE operation_id = ?", (op_id,)
            ).fetchone()

        if self.i8.reservation_count(op_id) == 0:
            self.i8.reserve(op_id, amount)
        if self.rc01.get_state(op_id) != "RESERVED":
            self.rc01.transition(op_id, "RESERVED", "positive_admission")
        self._issue_from_intent(existing)
        row = self.conn.execute("SELECT * FROM permits WHERE operation_id = ?", (op_id,)).fetchone()
        return PositivePermit(row)

    def consume_and_dispatch(self, op_id: str, payload: dict, idempotency_key: str):
        rh = canon_request_hash(payload)
        now = datetime.now(timezone.utc).isoformat()
        with self.conn:
            row = self.conn.execute(
                "SELECT * FROM permits WHERE operation_id = ?", (op_id,)
            ).fetchone()
            if not row or row["status"] != "issued" or not row["signature"]:
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
            payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            updated = self.conn.execute(
                """UPDATE permits
                   SET consumed = 1, idempotency_key = ?, dispatch_claim = 1, payload_json = ?
                   WHERE operation_id = ? AND status = 'issued' AND consumed = 0
                     AND request_hash = ? AND expires_at > ?""",
                (idempotency_key, payload_json, op_id, rh, now),
            )
            if updated.rowcount != 1:
                raise RuntimeError("durable permit consumption failed")
        self._project_dispatching(op_id)
        # Test seam only: kill this process after the durable claim, before B3.
        if os.environ.get("W2_CRASH_AFTER_CLAIM") == "1":
            os.kill(os.getpid(), signal.SIGKILL)
        return self.provider.execute(op_id, idempotency_key, payload)
