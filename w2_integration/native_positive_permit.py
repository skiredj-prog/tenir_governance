"""W2.2-AC-04 — positive permit on the native G3.1/G4-A SQLite store.

Uses consensus.sqlite3. Does not rename PositiveG4A into native authority.
Does not change ExperimentalG4AAuthority: HOLD and execution_authorized=false stay.
B3 remains outside the SQLite transaction. The independent Witness uses a
separate database; its checkpoint is verified before the native claim commits.
A failed native commit can therefore leave an unused Witness checkpoint, but
cannot create a dispatchable outbox claim.
"""
from __future__ import annotations

import hashlib
import base64
import json
import os
import sqlite3
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from independent_witness import IndependentWitness
from w2_2b_canonical import request_hash as canon_request_hash
from w2_integration.native_dispatch_command import COMMAND_SCHEMA, snapshot_command, snapshot_payload

PERMIT_SCHEMA = "w2-positive-permit/v1"
TOPOLOGY_ID = "lab-s1-native-sqlite-single-host-2026-10-28"


def _canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


class NativePositivePermitAuthority:
    def __init__(self, backend: SQLiteConsensusBackend, *, witness=None,
                 trusted_provider_public_key: bytes | None = None):
        if not isinstance(backend, SQLiteConsensusBackend):
            raise TypeError("SQLiteConsensusBackend required")
        self.backend = backend
        self.db_path = backend._database_path
        self.topology_id = TOPOLOGY_ID
        self.productive = False
        self.witness = (
            witness if witness is not None
            else IndependentWitness(self.db_path + ".witness.sqlite3")
        )
        if trusted_provider_public_key is None:
            self.trusted_provider_public_key = None
            self.trusted_provider_key_id = None
        else:
            if not isinstance(trusted_provider_public_key, bytes) or len(trusted_provider_public_key) != 32:
                raise ValueError("trusted provider public key must be 32 raw Ed25519 bytes")
            self.trusted_provider_public_key = Ed25519PublicKey.from_public_bytes(
                trusted_provider_public_key
            )
            self.trusted_provider_key_id = hashlib.sha256(
                trusted_provider_public_key
            ).hexdigest()
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
                  settled INTEGER NOT NULL DEFAULT 0,
                  reservation_id TEXT,
                  resource_id TEXT
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
                  created_at TEXT NOT NULL,
                  witness_evidence_json TEXT,
                  witness_hash TEXT,
                  provider_receipt_json TEXT
                );
            """)
            self._add_column(db, "w2_positive_v1_capacity", "reservation_id", "TEXT")
            self._add_column(db, "w2_positive_v1_capacity", "resource_id", "TEXT")
            self._add_column(db, "w2_positive_v1_permits", "nonce_hash", "TEXT")
            self._add_column(db, "w2_positive_v1_permits", "nonce_status", "TEXT NOT NULL DEFAULT 'ISSUED'")
            self._add_column(db, "w2_positive_v1_permits", "execution_id", "TEXT")
            self._add_column(db, "w2_positive_v1_permits", "audience", "TEXT NOT NULL DEFAULT 'kernel-dispatch'")
            self._add_column(db, "w2_positive_v1_outbox", "witness_evidence_json", "TEXT")
            self._add_column(db, "w2_positive_v1_outbox", "witness_hash", "TEXT")
            self._add_column(db, "w2_positive_v1_outbox", "provider_receipt_json", "TEXT")
            db.executescript("""
                DROP TRIGGER IF EXISTS w2_block_historical_after_positive;
                CREATE TRIGGER w2_block_historical_after_positive
                BEFORE INSERT ON reservations
                WHEN EXISTS (
                  SELECT 1 FROM w2_positive_v1_capacity
                  WHERE operation_key = NEW.idempotency_key
                    AND (reservation_id IS NULL OR reservation_id != NEW.reservation_id)
                )
                BEGIN
                  SELECT RAISE(ABORT, 'positive capacity owns this key');
                END;
                DROP TRIGGER IF EXISTS w2_block_positive_after_historical;
                CREATE TRIGGER w2_block_positive_after_historical
                BEFORE INSERT ON w2_positive_v1_capacity
                WHEN EXISTS (
                  SELECT 1 FROM reservations
                  WHERE idempotency_key = NEW.operation_key
                )
                BEGIN
                  SELECT RAISE(ABORT, 'historical reservation owns this key');
                END;
                CREATE TRIGGER IF NOT EXISTS w2_positive_v1_events_no_update
                BEFORE UPDATE ON w2_positive_v1_events
                BEGIN
                  SELECT RAISE(ABORT, 'positive authority events are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS w2_positive_v1_events_no_delete
                BEFORE DELETE ON w2_positive_v1_events
                BEGIN
                  SELECT RAISE(ABORT, 'positive authority events are append-only');
                END;
            """)
        self._load_key()

    def _tx(self):
        """Autocommit connection for schema and reads. Not a write transaction."""
        return self.backend._connection()

    @staticmethod
    def _add_column(db, table, column, definition):
        cols = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _write(self, operation: str):
        """BEGIN IMMEDIATE … COMMIT, or ROLLBACK if the body raises."""
        return self.backend._transaction(operation)

    def _require_native_i8(self, db, operation_key: str, payload: dict, *, allow_committed: bool = False) -> None:
        """Refuse a claim or settlement when the durable I8 row no longer matches."""
        cap = db.execute(
            "SELECT amount, reserved, settled, reservation_id, resource_id "
            "FROM w2_positive_v1_capacity WHERE operation_key=?",
            (operation_key,),
        ).fetchone()
        if not cap or not cap["reservation_id"] or not cap["resource_id"]:
            raise RuntimeError("native I8 reservation missing")
        reservation = db.execute(
            "SELECT * FROM reservations WHERE reservation_id=?",
            (cap["reservation_id"],),
        ).fetchone()
        if not reservation:
            raise RuntimeError("native I8 reservation missing")
        if reservation["idempotency_key"] != operation_key:
            raise RuntimeError("native I8 reservation key mismatch")
        if reservation["resource_id"] != cap["resource_id"]:
            raise RuntimeError("native I8 resource mismatch")
        cap_amount = self.backend._amount(cap["amount"])
        res_amount = self.backend._amount(reservation["amount"])
        payload_amount = self.backend._amount(payload.get("amount")) if isinstance(payload, dict) else None
        if cap_amount is None or res_amount is None or cap_amount != res_amount:
            raise RuntimeError("native I8 amount mismatch")
        if payload_amount is not None and payload_amount != res_amount:
            raise RuntimeError("native I8 amount mismatch")
        if isinstance(payload, dict) and payload.get("resource_id") not in (None, reservation["resource_id"]):
            raise RuntimeError("native I8 resource mismatch")
        allowed = ("RESERVED", "COMMITTED") if allow_committed else ("RESERVED",)
        if reservation["state"] not in allowed:
            raise RuntimeError("native I8 reservation state invalid")
        resource = db.execute(
            "SELECT reserved, committed FROM resources WHERE resource_id=?",
            (reservation["resource_id"],),
        ).fetchone()
        if not resource:
            raise RuntimeError("native I8 budget missing")
        if reservation["state"] == "RESERVED":
            reserved = self.backend._amount(resource["reserved"])
            if reserved is None or reserved < res_amount or not cap["reserved"] or cap["settled"]:
                raise RuntimeError("native I8 budget invariant broken")
        else:
            committed = self.backend._amount(resource["committed"])
            if committed is None or committed < res_amount or cap["reserved"] or not cap["settled"]:
                raise RuntimeError("native I8 budget invariant broken")

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

    def _body(self, permit_id, op, evaluation_id, request_hash, reservation_id, issued_at, expires_at,
              nonce_hash=None, audience="kernel-dispatch") -> dict:
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

    def admit_reserve_issue(
        self, operation_key: str, evaluation_id: str, payload: dict,
        amount=None, *, resource_id: str,
    ) -> dict:
        """Atomically reserve native I8 capacity and issue its bound positive permit.

        The amount defaults to the canonical payload's ``amount`` field. The I8
        resource must already exist in SQLiteConsensusBackend.
        """
        payload = snapshot_payload(payload)
        if payload.get("schema") == COMMAND_SCHEMA:
            payload = snapshot_command(payload)
        if amount is None:
            if not isinstance(payload, dict) or "amount" not in payload:
                raise RuntimeError("native I8 amount must be explicit or present in payload")
            amount = payload["amount"]
        normalized_amount = self.backend._amount(amount)
        if normalized_amount is None:
            raise RuntimeError("invalid native I8 amount")
        if (isinstance(payload, dict) and "amount" in payload
                and self.backend._amount(payload["amount"]) != normalized_amount):
            raise RuntimeError("native I8 amount does not match payload amount")
        if not isinstance(resource_id, str) or not resource_id:
            raise RuntimeError("native I8 resource_id required")
        if (isinstance(payload, dict) and payload.get("resource_id") is not None
                and payload.get("resource_id") != resource_id):
            raise RuntimeError("payload resource_id does not match native I8 resource")
        request_hash = canon_request_hash(payload)
        issued_at = datetime.now(timezone.utc).isoformat()
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        nonce_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        permit_id = f"permit-{operation_key}"
        reservation_id = f"res-{operation_key}"
        body = self._body(permit_id, operation_key, evaluation_id, request_hash, reservation_id, issued_at,
                          expires_at, nonce_hash)
        signature = self.private_key.sign(_canonical(body))
        try:
            with self._write("positive_admit") as db:
                existing = db.execute(
                    "SELECT * FROM w2_positive_v1_permits WHERE operation_key=?", (operation_key,)
                ).fetchone()
                if existing:
                    capacity_row = db.execute(
                        "SELECT amount, reservation_id, resource_id FROM w2_positive_v1_capacity "
                        "WHERE operation_key=?", (operation_key,),
                    ).fetchone()
                    reservation = db.execute(
                        "SELECT reservation_id, resource_id, amount, state FROM reservations "
                        "WHERE reservation_id=? AND idempotency_key=?",
                        (existing["reservation_id"], operation_key),
                    ).fetchone()
                    if (existing["request_hash"] != request_hash
                            or existing["evaluation_id"] != evaluation_id
                            or not capacity_row or not reservation
                            or capacity_row["reservation_id"] != reservation["reservation_id"]
                            or capacity_row["resource_id"] != resource_id
                            or self.backend._amount(capacity_row["amount"]) != normalized_amount
                            or reservation["resource_id"] != resource_id
                            or self.backend._amount(reservation["amount"]) != normalized_amount
                            or reservation["state"] not in ("RESERVED", "COMMITTED")):
                        raise RuntimeError("contradictory or incomplete native I8 permit")
                    return dict(existing)
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
                db.execute(
                    "INSERT INTO w2_positive_v1_capacity "
                    "(operation_key, amount, reservation_id, resource_id) VALUES (?, ?, ?, ?)",
                    (operation_key, str(normalized_amount), reservation_id, resource_id),
                )
                if os.environ.get("W2_FAIL_AFTER_CAPACITY") == "1":
                    raise RuntimeError("injected failure after capacity")
                reservation_result = self.backend._reserve_in_connection(
                    db, operation_key, normalized_amount, resource_id,
                    reservation_id=reservation_id, positive_authority_owner=True,
                )
                if reservation_result.status != "RESERVED":
                    raise RuntimeError(
                        f"native I8 reservation refused: {reservation_result.status}"
                    )
                db.execute(
                    """INSERT INTO w2_positive_v1_permits
                       (permit_id, operation_key, evaluation_id, request_hash, reservation_id,
                       issued_at, expires_at, nonce_hash, execution_id, audience, signature, key_id, schema_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (permit_id, operation_key, evaluation_id, request_hash, reservation_id,
                     issued_at, expires_at, nonce_hash, permit_id, "kernel-dispatch",
                     signature, self.key_id, PERMIT_SCHEMA),
                )
                db.execute(
                    "INSERT INTO w2_positive_v1_events (operation_key, event, at, detail) VALUES (?, ?, ?, ?)",
                    (operation_key, "permit_issued_after_reservation", issued_at, reservation_id),
                )
        except sqlite3.IntegrityError as exc:
            raise RuntimeError("exclusive authority refused") from exc
        return {"permit_id": permit_id, "operation_key": operation_key, "request_hash": request_hash,
                "reservation_id": reservation_id, "resource_id": resource_id,
                "amount": str(normalized_amount), "schema": PERMIT_SCHEMA,
                "execution_authorized_historical": False}

    def consume_and_claim(self, operation_key: str, payload: dict, idempotency_key: str) -> dict:
        request_hash = canon_request_hash(payload)
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        witness_subject = f"{operation_key}:{idempotency_key}"
        try:
            with self._write("positive_claim") as db:
                now = datetime.now(timezone.utc).isoformat()
                replay = db.execute(
                    "SELECT execution_id, operation_key, idempotency_key, request_hash, claim_token, status, "
                    "witness_evidence_json, witness_hash, provider_receipt_json "
                    "FROM w2_positive_v1_outbox WHERE operation_key=? OR idempotency_key=?",
                    (operation_key, idempotency_key),
                ).fetchone()
                review_latched = db.execute(
                    "SELECT 1 FROM w2_positive_v1_events WHERE operation_key=? "
                    "AND event='dispatch_review' LIMIT 1", (operation_key,),
                ).fetchone()
                if replay:
                    if replay["operation_key"] != operation_key or replay["idempotency_key"] != idempotency_key or replay["request_hash"] != request_hash:
                        raise RuntimeError("idempotency key conflict")
                    self._require_native_i8(
                        db, operation_key, payload, allow_committed=replay["status"] == "CONFIRMED",
                    )
                    evidence_json = replay["witness_evidence_json"]
                    witness_hash = replay["witness_hash"]
                    if not evidence_json or not witness_hash:
                        raise RuntimeError("claim witness evidence missing")
                    evidence = json.loads(evidence_json)
                    if evidence.get("record", {}).get("subject") != witness_subject:
                        raise RuntimeError("witness subject mismatch")
                    expected_witness_hash = hashlib.sha256(
                        b"TENIR-W2-CLAIM-WITNESS-v1\0" + _canonical(evidence)
                    ).hexdigest()
                    if (expected_witness_hash != witness_hash
                            or not self.witness.verify(evidence, subject=witness_subject)):
                        raise RuntimeError("claim witness evidence invalid")
                    return {"operation_key": operation_key, "idempotency_key": idempotency_key,
                            "execution_id": replay["execution_id"], "claim_token": replay["claim_token"],
                            "token": replay["claim_token"], "nonce": replay["claim_token"],
                            "operation": operation_key, "audience": "kernel-dispatch",
                            "status": replay["status"], "claimed": True, "replayed": True,
                            "witness": evidence, "witness_hash": witness_hash,
                            "receipt": json.loads(replay["provider_receipt_json"])
                            if replay["provider_receipt_json"] else None}
                if review_latched:
                    raise RuntimeError("manual review precedes send")
                row = db.execute(
                    "SELECT * FROM w2_positive_v1_permits WHERE operation_key=?", (operation_key,)
                ).fetchone()
                if not row or row["request_hash"] != request_hash:
                    raise RuntimeError("no valid unconsumed permit")
                if row["consumed"] or row["nonce_status"] != "ISSUED":
                    raise RuntimeError("nonce already consumed")
                if row["audience"] != "kernel-dispatch":
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
                self._require_native_i8(db, operation_key, payload, allow_committed=False)
                witness_fingerprint = hashlib.sha256(
                    b"TENIR-W2-CLAIM-FINGERPRINT-v1\0" + _canonical({
                        "operation_key": operation_key,
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "permit_id": row["permit_id"],
                        "reservation_id": row["reservation_id"],
                        "audience": row["audience"],
                    })
                ).hexdigest()
                try:
                    proposed_execution = f"exec-{uuid.uuid4().hex}"
                    proposed_token = secrets.token_urlsafe(32)
                    witness_evidence = self.witness.issue(
                        witness_subject, witness_fingerprint,
                        identity={"execution_id": proposed_execution, "claim_token": proposed_token},
                    )
                    if not self.witness.verify(witness_evidence, subject=witness_subject):
                        return {
                            "operation_key": operation_key, "idempotency_key": idempotency_key,
                            "operation": operation_key, "audience": "kernel-dispatch",
                            "execution_id": None, "claim_token": None, "token": None, "nonce": None,
                            "status": "REFUSED", "claimed": False, "witness": witness_evidence,
                            "witness_hash": None,
                        }
                except Exception as exc:
                    raise RuntimeError("independent witness refused claim") from exc
                record = witness_evidence["record"]
                execution_id = record.get("execution_id") or proposed_execution
                claim_token = record.get("claim_token") or proposed_token
                witness_evidence_json = json.dumps(
                    witness_evidence, sort_keys=True, separators=(",", ":")
                )
                witness_hash = hashlib.sha256(
                    b"TENIR-W2-CLAIM-WITNESS-v1\0" + _canonical(witness_evidence)
                ).hexdigest()
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
                    "(execution_id, operation_key, idempotency_key, request_hash, payload_json, claim_token, status, created_at, witness_evidence_json, witness_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)",
                    (execution_id, operation_key, idempotency_key, request_hash, payload_json, claim_token,
                     now, witness_evidence_json, witness_hash),
                )
                db.execute(
                    "INSERT INTO w2_positive_v1_events (operation_key, event, at, detail) VALUES (?, ?, ?, ?)",
                    (operation_key, "dispatch_claimed", now, idempotency_key),
                )
                if os.environ.get("W2_CRASH_BEFORE_NATIVE_CLAIM_COMMIT") == "1":
                    os.kill(os.getpid(), __import__("signal").SIGKILL)
        except sqlite3.IntegrityError as exc:
            raise RuntimeError("exclusive authority refused") from exc
        if os.environ.get("W2_CRASH_AFTER_NATIVE_CLAIM") == "1":
            os.kill(os.getpid(), __import__("signal").SIGKILL)
        return {"operation_key": operation_key, "idempotency_key": idempotency_key,
                "operation": operation_key, "audience": "kernel-dispatch",
                "execution_id": execution_id, "claim_token": claim_token,
                "token": claim_token, "nonce": claim_token,
                "status": "PENDING", "claimed": True,
                "witness": witness_evidence, "witness_hash": witness_hash}

    def latch_review_before_send(self, operation_key: str, reason: str) -> str:
        """Freeze an operation before any send right is granted.

        A review that arrives after the right was granted is still recorded,
        but it cannot erase that grant.
        """
        if not isinstance(operation_key, str) or not operation_key:
            raise RuntimeError("operation required")
        if not isinstance(reason, str) or not reason.strip():
            raise RuntimeError("manual review reason required")
        now = datetime.now(timezone.utc).isoformat()
        with self._write("latch_review_before_send") as db:
            granted = db.execute(
                "SELECT 1 FROM w2_positive_v1_events WHERE operation_key=? "
                "AND event='dispatch_send_granted' LIMIT 1", (operation_key,),
            ).fetchone()
            prior = db.execute(
                "SELECT 1 FROM w2_positive_v1_events WHERE operation_key=? "
                "AND event='dispatch_review' LIMIT 1", (operation_key,),
            ).fetchone()
            if not prior:
                detail = json.dumps(
                    {"reason": reason.strip(), "before_send": granted is None},
                    sort_keys=True, separators=(",", ":"),
                )
                db.execute(
                    "INSERT INTO w2_positive_v1_events (operation_key, event, at, detail) "
                    "VALUES (?, 'dispatch_review', ?, ?)",
                    (operation_key, now, detail),
                )
            return "REVIEW_AFTER_GRANT" if granted else "HOLD"

    def grant_send_right(self, claim: dict) -> str:
        """Give exactly one caller the right to send this claim.

        The decision is the append-only ``dispatch_send_granted`` event, taken
        in the same transaction that observes a manual-review latch. Replay and
        crash do not insert a second event and therefore must not send again.
        """
        if not isinstance(claim, dict):
            raise RuntimeError("dispatch claim required")
        execution_id = claim.get("execution_id")
        token = claim.get("claim_token")
        if not execution_id or not token:
            raise RuntimeError("dispatch claim identity missing")
        now = datetime.now(timezone.utc).isoformat()
        with self._write("grant_send_right") as db:
            row = db.execute(
                "SELECT operation_key, status, claim_token FROM w2_positive_v1_outbox "
                "WHERE execution_id=?", (execution_id,),
            ).fetchone()
            if not row or row["claim_token"] != token:
                raise RuntimeError("dispatch claim does not own outbox row")
            if db.execute(
                "SELECT 1 FROM w2_positive_v1_events WHERE operation_key=? "
                "AND event='dispatch_review' LIMIT 1", (row["operation_key"],),
            ).fetchone():
                return "REVIEW"
            granted = db.execute(
                "SELECT 1 FROM w2_positive_v1_events WHERE operation_key=? "
                "AND event='dispatch_send_granted' LIMIT 1", (row["operation_key"],),
            ).fetchone()
            if granted or row["status"] != "PENDING":
                return "ALREADY"
            permit = db.execute(
                "SELECT expires_at FROM w2_positive_v1_permits WHERE operation_key=?",
                (row["operation_key"],),
            ).fetchone()
            if not permit or permit["expires_at"] <= datetime.now(timezone.utc).isoformat():
                return "EXPIRED"
            db.execute(
                "INSERT INTO w2_positive_v1_events (operation_key, event, at, detail) "
                "VALUES (?, 'dispatch_send_granted', ?, ?)",
                (row["operation_key"], now, execution_id),
            )
            return "GRANTED"

    def load_dispatch_payload(self, claim: dict) -> dict:
        """Read the committed outbox command for this exact native capability.

        This authenticates the payload, not a right to send repeatedly. Dispatch
        election, review interleaving and public settlement fencing are separate
        gates and are not supplied by this read.
        """
        if not isinstance(claim, dict):
            raise RuntimeError("native dispatch claim required")
        with self._tx() as db:
            row = db.execute(
                "SELECT o.payload_json, o.request_hash, o.operation_key, o.idempotency_key, "
                "p.request_hash AS permit_hash FROM w2_positive_v1_outbox o "
                "JOIN w2_positive_v1_permits p ON p.operation_key=o.operation_key "
                "AND p.execution_id=o.execution_id "
                "WHERE o.execution_id=? AND o.claim_token=?",
                (claim.get("execution_id"), claim.get("claim_token")),
            ).fetchone()
            if (not row or claim.get("operation_key") != row["operation_key"]
                    or claim.get("idempotency_key") != row["idempotency_key"]):
                raise RuntimeError("native outbox claim identity mismatch")
            try:
                payload = json.loads(row["payload_json"])
                actual_hash = canon_request_hash(payload)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("persisted command payload invalid") from exc
            if actual_hash != row["request_hash"] or actual_hash != row["permit_hash"]:
                raise RuntimeError("persisted command payload hash mismatch")
            self._require_native_i8(db, row["operation_key"], payload, allow_committed=True)
            return payload

    def mark_dispatch_unknown(self, claim: dict) -> str:
        """Retain a possibly executed dispatch for reconciliation; never clear its key."""
        if not isinstance(claim, dict):
            raise RuntimeError("dispatch claim required")
        execution_id = claim.get("execution_id")
        token = claim.get("claim_token")
        if not execution_id or not token:
            raise RuntimeError("dispatch claim identity missing")
        with self._write("mark_dispatch_unknown") as db:
            row = db.execute(
                "SELECT status, claim_token FROM w2_positive_v1_outbox WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
            if not row or row["claim_token"] != token:
                raise RuntimeError("dispatch claim does not own outbox row")
            if row["status"] == "CONFIRMED":
                return "CONFIRMED"
            if row["status"] == "UNKNOWN":
                return "UNKNOWN"
            if row["status"] != "PENDING":
                raise RuntimeError("dispatch is not eligible for UNKNOWN")
            updated = db.execute(
                "UPDATE w2_positive_v1_outbox SET status='UNKNOWN' "
                "WHERE execution_id=? AND claim_token=? AND status='PENDING'",
                (execution_id, token),
            )
            if updated.rowcount != 1:
                raise RuntimeError("dispatch status race")
            db.execute(
                "INSERT INTO w2_positive_v1_events (operation_key, event, at, detail) "
                "VALUES (?, 'dispatch_unknown', ?, ?)",
                (claim["operation_key"], datetime.now(timezone.utc).isoformat(), execution_id),
            )
            return "UNKNOWN"

    def mark_dispatch_review(self, claim: dict, reason: str) -> str:
        """Durably freeze an ambiguous/contradictory provider result for humans."""
        if not isinstance(claim, dict) or not claim.get("execution_id"):
            raise RuntimeError("dispatch claim identity missing")
        if not isinstance(reason, str) or not reason.strip():
            raise RuntimeError("manual review reason required")
        execution_id = claim["execution_id"]
        token = claim.get("claim_token")
        with self._write("mark_dispatch_review") as db:
            row = db.execute(
                "SELECT operation_key, status, claim_token FROM w2_positive_v1_outbox "
                "WHERE execution_id=?", (execution_id,),
            ).fetchone()
            if not row or row["claim_token"] != token:
                raise RuntimeError("dispatch claim does not own outbox row")
            if row["status"] not in ("PENDING", "UNKNOWN", "CONFIRMED"):
                raise RuntimeError("dispatch cannot enter manual review")
            if row["status"] == "PENDING":
                updated = db.execute(
                    "UPDATE w2_positive_v1_outbox SET status='UNKNOWN' "
                    "WHERE execution_id=? AND claim_token=? AND status='PENDING'",
                    (execution_id, token),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("dispatch review status race")
            prior = db.execute(
                "SELECT 1 FROM w2_positive_v1_events WHERE operation_key=? "
                "AND event='dispatch_review' LIMIT 1", (row["operation_key"],),
            ).fetchone()
            if not prior:
                detail = json.dumps(
                    {"execution_id": execution_id, "reason": reason.strip()},
                    sort_keys=True, separators=(",", ":"),
                )
                db.execute(
                    "INSERT INTO w2_positive_v1_events (operation_key, event, at, detail) "
                    "VALUES (?, 'dispatch_review', ?, ?)",
                    (row["operation_key"], datetime.now(timezone.utc).isoformat(), detail),
                )
            return "CONFIRMED_REVIEW" if row["status"] == "CONFIRMED" else "UNKNOWN"

    def requires_manual_review(self, operation_key: str) -> bool:
        with self._tx() as db:
            return db.execute(
                "SELECT 1 FROM w2_positive_v1_events WHERE operation_key=? "
                "AND event='dispatch_review' LIMIT 1", (operation_key,),
            ).fetchone() is not None

    def _verify_provider_receipt(self, receipt: dict) -> bool:
        if not isinstance(receipt, dict) or self.trusted_provider_public_key is None:
            return False
        try:
            if receipt.get("key_id") != self.trusted_provider_key_id:
                return False
            signature = base64.b64decode(receipt["provider_signature"], validate=True)
            unsigned = {key: value for key, value in receipt.items()
                        if key != "provider_signature"}
            self.trusted_provider_public_key.verify(signature, _canonical(unsigned))
            return True
        except Exception:
            return False

    def mark_dispatch_confirmed(self, claim: dict, receipt: dict) -> str:
        """Persist only a receipt matching the exact pending/unknown native claim."""
        if not isinstance(claim, dict) or not isinstance(receipt, dict):
            raise RuntimeError("claim and provider receipt required")
        if not self._verify_provider_receipt(receipt):
            raise RuntimeError("provider receipt signature invalid")
        execution_id = claim.get("execution_id")
        token = claim.get("claim_token")
        if not execution_id or not token:
            raise RuntimeError("dispatch claim identity missing")
        receipt_json = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
        with self._write("mark_dispatch_confirmed") as db:
            row = db.execute(
                "SELECT execution_id, operation_key, idempotency_key, request_hash, claim_token, status, "
                "provider_receipt_json, witness_evidence_json, witness_hash "
                "FROM w2_positive_v1_outbox WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
            if not row or row["claim_token"] != token:
                raise RuntimeError("dispatch claim does not own outbox row")
            if (claim.get("operation") != row["operation_key"]
                    or claim.get("operation_key") != row["operation_key"]
                    or claim.get("idempotency_key") != row["idempotency_key"]
                    or claim.get("audience") != "kernel-dispatch"):
                raise RuntimeError("dispatch claim scope mismatch")
            if (receipt.get("operation_id") != row["operation_key"]
                    or receipt.get("idempotency_key") != row["idempotency_key"]
                    or receipt.get("request_hash") != row["request_hash"]
                    or receipt.get("effect_status") != "EXECUTED"):
                raise RuntimeError("provider receipt does not match claim")
            evidence_json = row["witness_evidence_json"]
            evidence = json.loads(evidence_json) if evidence_json else None
            permit = db.execute(
                "SELECT permit_id, reservation_id FROM w2_positive_v1_permits WHERE operation_key=?",
                (row["operation_key"],),
            ).fetchone()
            witness_epoch = evidence.get("record", {}).get("revision") if isinstance(evidence, dict) else None
            if (not permit
                    or receipt.get("execution_id") != row["execution_id"]
                    or receipt.get("reservation_id") != permit["reservation_id"]
                    or receipt.get("permit_id") != permit["permit_id"]
                    or receipt.get("witness_hash") != row["witness_hash"]
                    or receipt.get("witness_epoch") != witness_epoch):
                raise RuntimeError("provider receipt binding mismatch")
            if (not evidence or not row["witness_hash"]
                    or claim.get("witness_hash") != row["witness_hash"]
                    or claim.get("witness") != evidence
                    or evidence.get("record", {}).get("subject") != f"{row['operation_key']}:{row['idempotency_key']}"
                    or not self.witness.verify(
                        evidence, subject=f"{row['operation_key']}:{row['idempotency_key']}",
                    )):
                raise RuntimeError("claim witness is invalid")
            if db.execute(
                "SELECT 1 FROM w2_positive_v1_events WHERE operation_key=? "
                "AND event='dispatch_review' LIMIT 1", (row["operation_key"],),
            ).fetchone():
                raise RuntimeError("manual review latch blocks automatic confirmation")
            if row["status"] == "CONFIRMED":
                if row["provider_receipt_json"] != receipt_json:
                    raise RuntimeError("conflicting confirmed receipt")
                reservation = db.execute(
                    "SELECT p.reservation_id, r.state FROM w2_positive_v1_permits p "
                    "JOIN reservations r ON r.reservation_id=p.reservation_id "
                    "WHERE p.operation_key=?", (row["operation_key"],),
                ).fetchone()
                cap = db.execute(
                    "SELECT reserved, settled FROM w2_positive_v1_capacity WHERE operation_key=?",
                    (row["operation_key"],),
                ).fetchone()
                if not reservation or reservation["state"] != "COMMITTED" or not cap or cap["settled"] != 1:
                    raise RuntimeError("confirmed outbox lacks committed native I8 reservation")
                return "CONFIRMED"
            if row["status"] not in ("PENDING", "UNKNOWN"):
                raise RuntimeError("dispatch is not eligible for confirmation")
            stored_payload = json.loads(db.execute(
                "SELECT payload_json FROM w2_positive_v1_outbox WHERE execution_id=?",
                (execution_id,),
            ).fetchone()["payload_json"])
            self._require_native_i8(db, row["operation_key"], stored_payload, allow_committed=False)
            permit = db.execute(
                "SELECT reservation_id FROM w2_positive_v1_permits WHERE operation_key=?",
                (row["operation_key"],),
            ).fetchone()
            if not permit:
                raise RuntimeError("native permit missing for I8 settlement")
            commit_result = self.backend._commit_in_connection(db, permit["reservation_id"])
            if commit_result.status not in ("COMMITTED", "ALREADY_COMMITTED"):
                raise RuntimeError(f"native I8 commit refused: {commit_result.status}")
            settled = db.execute(
                "UPDATE w2_positive_v1_capacity SET reserved=0, settled=1 "
                "WHERE operation_key=? AND reservation_id=? AND reserved=1 AND settled=0",
                (row["operation_key"], permit["reservation_id"]),
            )
            if settled.rowcount != 1:
                raise RuntimeError("positive I8 settlement metadata conflict")
            updated = db.execute(
                "UPDATE w2_positive_v1_outbox SET status='CONFIRMED', provider_receipt_json=? "
                "WHERE execution_id=? AND claim_token=? AND status IN ('PENDING','UNKNOWN')",
                (receipt_json, execution_id, token),
            )
            if updated.rowcount != 1:
                raise RuntimeError("dispatch confirmation race")
            db.execute(
                "INSERT INTO w2_positive_v1_events (operation_key, event, at, detail) "
                "VALUES (?, 'dispatch_confirmed', ?, ?)",
                (row["operation_key"], datetime.now(timezone.utc).isoformat(), execution_id),
            )
            return "CONFIRMED"

    def receipt_context(self, claim: dict) -> dict:
        """Durable identities a connector receipt must sign. Not a send right."""
        if not isinstance(claim, dict):
            raise RuntimeError("dispatch claim required")
        execution_id = claim.get("execution_id")
        token = claim.get("claim_token")
        if not execution_id or not token:
            raise RuntimeError("dispatch claim identity missing")
        with self._tx() as db:
            row = db.execute(
                "SELECT o.execution_id, o.operation_key, o.witness_hash, o.witness_evidence_json, "
                "p.permit_id, p.reservation_id "
                "FROM w2_positive_v1_outbox o "
                "JOIN w2_positive_v1_permits p ON p.operation_key=o.operation_key "
                "WHERE o.execution_id=? AND o.claim_token=?",
                (execution_id, token),
            ).fetchone()
        if not row or not row["witness_hash"] or not row["witness_evidence_json"]:
            raise RuntimeError("receipt context unavailable")
        evidence = json.loads(row["witness_evidence_json"])
        epoch = evidence.get("record", {}).get("revision")
        if not isinstance(epoch, int):
            raise RuntimeError("witness epoch unavailable")
        return {
            "execution_id": row["execution_id"],
            "reservation_id": row["reservation_id"],
            "permit_id": row["permit_id"],
            "witness_hash": row["witness_hash"],
            "witness_epoch": epoch,
        }

    def writers(self) -> list[dict]:
        return [{
            "component": "NativePositivePermitAuthority",
            "database": self.db_path,
            "tables": [
                "w2_positive_v1_capacity", "w2_positive_v1_permits",
                "w2_positive_v1_outbox", "w2_positive_v1_events",
                "resources", "reservations",
            ],
            "i8_backend": "SQLiteConsensusBackend",
            "i8_reserve_in_transaction": "_reserve_in_connection",
            "i8_commit_in_transaction": "_commit_in_connection",
            "witness_database": getattr(self.witness, "db_path", None),
            "witness_key_custody": getattr(self.witness, "key_custody", "INJECTED"),
            "historical_g4a_modified": False,
            "key_custody": self.key_custody,
            "productive": False,
        }]

    def verify_dispatch_claim(
        self, operation_key: str, idempotency_key: str, payload: dict, claim
    ) -> bool:
        """Verify a presented dispatch claim against native outbox custody."""
        if not isinstance(claim, dict):
            return False
        try:
            if (claim.get("operation") != operation_key
                    or claim.get("idempotency_key") != idempotency_key
                    or claim.get("audience") != "kernel-dispatch"):
                return False
            with self._tx() as db:
                row = db.execute(
                    "SELECT execution_id, request_hash, claim_token, status, "
                    "witness_evidence_json, witness_hash FROM w2_positive_v1_outbox "
                    "WHERE operation_key=? AND idempotency_key=?",
                    (operation_key, idempotency_key),
                ).fetchone()
                review = db.execute(
                    "SELECT 1 FROM w2_positive_v1_events WHERE operation_key=? "
                    "AND event='dispatch_review' LIMIT 1", (operation_key,),
                ).fetchone()
                if review or not row or row["status"] not in ("PENDING", "UNKNOWN"):
                    return False
                if row["request_hash"] != canon_request_hash(payload):
                    return False
                self._require_native_i8(db, operation_key, payload, allow_committed=False)
            token = row["claim_token"]
            if (claim.get("token") != token or claim.get("nonce") != token
                    or claim.get("claim_token") != token
                    or claim.get("execution_id") != row["execution_id"]):
                return False
            evidence_json = row["witness_evidence_json"]
            witness_hash = row["witness_hash"]
            if not evidence_json or not witness_hash:
                return False
            evidence = json.loads(evidence_json)
            expected_hash = hashlib.sha256(
                b"TENIR-W2-CLAIM-WITNESS-v1\0" + _canonical(evidence)
            ).hexdigest()
            if (expected_hash != witness_hash
                    or claim.get("witness_hash") != witness_hash
                    or claim.get("witness") != evidence
                    or evidence.get("record", {}).get("subject") != f"{operation_key}:{idempotency_key}"):
                return False
            return bool(self.witness.verify(
                evidence, subject=f"{operation_key}:{idempotency_key}",
            ))
        except Exception:
            return False
