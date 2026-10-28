"""
PROOF-01 B3 Proof Provider Fixture — RECONSTRUCTED
Based on documented spec (PR #84). Not the original ZIP.
SHA of this reconstruction will differ from historical 72aa1dd6...
"""
import sqlite3
import json
import hashlib
import base64
from datetime import datetime, timezone
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

class AckLost(Exception):
    pass

class ProofProvider:
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self._init_schema()
        self.private_key = Ed25519PrivateKey.generate()
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

    def execute(self, operation_id: str, idempotency_key: str, payload: dict, lose_ack: bool = False):
        request_hash = hashlib.sha256(self._canonical(payload)).hexdigest()
        # Idempotence: same key must not produce different effect
        cur = self.conn.execute(
            "SELECT operation_id, request_hash, receipt_json FROM effects WHERE idempotency_key = ?",
            (idempotency_key,)
        )
        existing = cur.fetchone()
        if existing:
            if existing[1] != request_hash:
                raise ValueError("idempotency key conflict: different request_hash")
            return json.loads(existing[2])

        cur = self.conn.execute(
            "SELECT receipt_json FROM effects WHERE operation_id = ?", (operation_id,)
        )
        row = cur.fetchone()
        if row:
            return json.loads(row[0])

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
        sig = self._sign(body)
        self.conn.execute(
            "INSERT INTO effects VALUES (?, ?, ?, ?, ?, ?, ?)",
            (operation_id, idempotency_key, request_hash, "EXECUTED",
             json.dumps(body), sig, body["issued_at"])
        )
        self.conn.commit()
        if lose_ack:
            raise AckLost("ACK lost after durable commit")
        return body

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
