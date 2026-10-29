"""
PROOF-01 B3 Proof Provider Fixture — RECONSTRUCTED
Based on documented spec (PR #84). Not the original ZIP.
SHA of this reconstruction will differ from historical 72aa1dd6...
"""
import sqlite3
import json
import hashlib
import base64
import os
from datetime import datetime, timezone
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from w2_2b_canonical import request_hash as canon_request_hash

class AckLost(Exception):
    pass

class ProofProvider:
    """Reconstructed lab provider; unclaimed effects are disabled by default."""
    def __init__(self, db_path: str = ":memory:", *, claim_verifier=None,
                 allow_unclaimed_execution: bool = False):
        if allow_unclaimed_execution:
            raise RuntimeError("unclaimed provider execution is disabled")
        self.claim_verifier = claim_verifier
        self.allow_unclaimed_execution = False
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self._init_schema()
        private_key_hex = os.environ.get("TENIR_B3_PRIVATE_KEY_HEX")
        if private_key_hex:
            try:
                private_key_raw = bytes.fromhex(private_key_hex.strip())
            except ValueError as exc:
                raise RuntimeError("TENIR_B3_PRIVATE_KEY_HEX is not hex") from exc
            if len(private_key_raw) != 32:
                raise RuntimeError("TENIR_B3_PRIVATE_KEY_HEX must be 32 bytes")
            self.private_key = Ed25519PrivateKey.from_private_bytes(private_key_raw)
            self.key_custody = "EXTERNAL_ENV"
        else:
            self.private_key = Ed25519PrivateKey.generate()
            self.key_custody = "LAB_EPHEMERAL_MEMORY"
        self.public_key = self.private_key.public_key()
        self.provider_id = "lab-provider-reconstructed"
        self.sequence = 0

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS effects (
                operation_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                effect_status TEXT NOT NULL,
                receipt_json TEXT NOT NULL,
                signature BLOB NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tombstones (
                idempotency_key TEXT PRIMARY KEY,
                receipt_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS authenticated_claim_uses (
                nonce TEXT PRIMARY KEY,
                operation_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_hash TEXT NOT NULL
            );
        """)
        # Migration: enforce UNIQUE on the correct column. Fail closed on duplicates or wrong index.
        try:
            self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_effects_idempotency ON effects(idempotency_key)")
        except sqlite3.IntegrityError as e:
            raise RuntimeError("invalid migration: duplicate idempotency_key present, refusing to open") from e
        # Verify the index exists, is UNIQUE, is not partial, and is on the right column
        cur = self.conn.execute("PRAGMA index_list(effects)")
        indexes = {row[1]: row for row in cur.fetchall()}
        if "idx_effects_idempotency" not in indexes:
            raise RuntimeError("migration failed: unique index not created")
        # row = (seq, name, unique, origin, partial)
        idx = indexes["idx_effects_idempotency"]
        if idx[2] != 1:
            raise RuntimeError("incompatible index: existing index is not UNIQUE")
        if len(idx) > 4 and idx[4] != 0:
            raise RuntimeError("incompatible index: existing index is partial")
        # Confirm column
        cur = self.conn.execute("PRAGMA index_info(idx_effects_idempotency)")
        cols = [row[2] for row in cur.fetchall()]
        if cols != ["idempotency_key"]:
            raise RuntimeError(f"incompatible index: expected idempotency_key, got {cols}")
        self.conn.commit()

    def _canonical(self, obj: dict) -> bytes:
        return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()

    def _sign(self, body: dict) -> bytes:
        return self.private_key.sign(self._canonical(body))

    def execute(self, operation_id: str, idempotency_key: str, payload: dict,
                lose_ack: bool = False, *, claim=None, binding=None):
        protected = self.claim_verifier is not None
        native = getattr(self.claim_verifier, "_native_bound", False) or getattr(
            getattr(self.claim_verifier, "__func__", None), "_native_bound", False
        )
        if protected and not native:
            raise RuntimeError("caller-supplied claim verifier refused")
        if protected:
            try:
                valid_claim = self.claim_verifier(
                    operation_id, idempotency_key, payload, claim
                )
            except Exception as exc:
                raise RuntimeError("claim verification failed") from exc
            if not valid_claim:
                raise RuntimeError("authenticated kernel claim required")
        elif not self.allow_unclaimed_execution:
            raise RuntimeError("authenticated kernel claim required")
        nonce = claim.get("nonce") if protected and isinstance(claim, dict) else None
        if protected and (not isinstance(nonce, str) or not nonce):
            raise RuntimeError("claim nonce required")
        request_hash = canon_request_hash(payload)
        if protected:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self.conn.execute(
                "SELECT operation_id, idempotency_key, request_hash, receipt_json "
                "FROM effects WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing:
                if (existing[0] != operation_id or existing[2] != request_hash):
                    raise ValueError("idempotency key conflict: different request")
                if protected:
                    use = self.conn.execute(
                        "SELECT operation_id, idempotency_key, request_hash "
                        "FROM authenticated_claim_uses WHERE nonce=?", (nonce,)
                    ).fetchone()
                    if not use or tuple(use) != (operation_id, idempotency_key, request_hash):
                        raise RuntimeError("claim replay does not match committed effect")
                    self.conn.commit()
                return json.loads(existing[3])

            row = self.conn.execute(
                "SELECT operation_id, idempotency_key, request_hash, receipt_json "
                "FROM effects WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if row:
                if protected:
                    raise RuntimeError("operation already has a different provider effect")
                return json.loads(row[3])

            if protected:
                self.conn.execute(
                    "INSERT INTO authenticated_claim_uses "
                    "(nonce, operation_id, idempotency_key, request_hash) VALUES (?, ?, ?, ?)",
                    (nonce, operation_id, idempotency_key, request_hash),
                )
            self.sequence += 1
            body = {
                "schema": "proof-provider-receipt/candidate-v0.1",
                "provider_id": self.provider_id,
                "key_id": hashlib.sha256(self.public_key.public_bytes(
                    serialization.Encoding.Raw, serialization.PublicFormat.Raw
                )).hexdigest(),
                "sequence": self.sequence,
                "operation_id": operation_id,
                "idempotency_key": idempotency_key,
                "request_hash": request_hash,
                "scope": payload.get("scope", {"account": "test", "amount_minor": 100, "currency": "EUR"}),
                "action_type": payload.get("action_type", "transfer"),
                "effect_status": "EXECUTED",
                "effect_digest": hashlib.sha256(b"effect").hexdigest(),
                "finality": "PROVIDER_LEDGER_TERMINAL",
                "issued_at": datetime.now(timezone.utc).isoformat(),
                "reason": "executed"
            }
            if binding is not None:
                for name in ("execution_id", "reservation_id", "permit_id", "witness_hash", "witness_epoch"):
                    if name not in binding:
                        raise RuntimeError("receipt binding incomplete")
                    body[name] = binding[name]
            sig = self._sign(body)
            body["provider_signature"] = base64.b64encode(sig).decode("ascii")
            self.conn.execute(
                "INSERT INTO effects VALUES (?, ?, ?, ?, ?, ?, ?)",
                (operation_id, idempotency_key, request_hash, "EXECUTED",
                 json.dumps(body), sig, body["issued_at"])
            )
            self.conn.commit()
            if lose_ack:
                raise AckLost("ACK lost after durable commit")
            return body
        except BaseException:
            if self.conn.in_transaction:
                self.conn.rollback()
            raise

    def reconcile(self, operation_id: str):
        cur = self.conn.execute(
            "SELECT receipt_json FROM effects WHERE operation_id = ?", (operation_id,)
        )
        row = cur.fetchone()
        return json.loads(row[0]) if row else None

    def execute_count(self, operation_id: str) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM effects WHERE operation_id = ?", (operation_id,)
        )
        return cur.fetchone()[0]

    def verify_receipt(self, receipt: dict) -> bool:
        """Verify a receipt against the provider's durable row and signing key."""
        if not isinstance(receipt, dict):
            return False
        row = self.conn.execute(
            "SELECT receipt_json, signature FROM effects "
            "WHERE operation_id=? AND idempotency_key=?",
            (receipt.get("operation_id"), receipt.get("idempotency_key")),
        ).fetchone()
        if not row or json.loads(row[0]) != receipt:
            return False
        try:
            signature = base64.b64decode(receipt["provider_signature"], validate=True)
            unsigned = {key: value for key, value in receipt.items()
                        if key != "provider_signature"}
            if signature != row[1]:
                return False
            self.public_key.verify(signature, self._canonical(unsigned))
            return True
        except Exception:
            return False
