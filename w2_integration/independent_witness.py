"""B2 independent monotone witness for the W2 laboratory path.

The witness owns a separate SQLite WAL database and signing key.  The Kernel
cannot advance its checkpoint by writing to the Kernel database, and private
key material is never persisted in this store.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class IndependentWitness:
    """Separate monotone checkpoint authority; laboratory mode is not multi-host."""

    def __init__(self, db_path: str, *, key_env: str = "TENIR_WITNESS_PRIVATE_KEY_HEX"):
        self.db_path = os.path.abspath(db_path)
        secret = os.environ.get(key_env)
        if secret:
            try:
                raw = bytes.fromhex(secret)
                if len(raw) != 32:
                    raise ValueError
                self.private_key = Ed25519PrivateKey.from_private_bytes(raw)
                self.key_custody = "EXTERNAL_HEX"
            except ValueError as exc:
                raise RuntimeError(f"invalid {key_env}") from exc
        else:
            self.private_key = Ed25519PrivateKey.generate()
            self.key_custody = "LAB_EPHEMERAL_MEMORY"
        public = self.private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.key_id = hashlib.sha256(public).hexdigest()
        with self._tx() as db:
            db.execute("CREATE TABLE IF NOT EXISTS witness_checkpoints (revision INTEGER PRIMARY KEY, record_json TEXT NOT NULL, signature BLOB NOT NULL)")

    @contextmanager
    def _tx(self):
        db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def issue(self, subject: str, snapshot_fingerprint: str, *, expected_revision: int | None = None) -> dict:
        if not subject or not snapshot_fingerprint:
            raise ValueError("subject and fingerprint are required")
        with self._tx() as db:
            previous = db.execute("SELECT revision, record_json FROM witness_checkpoints ORDER BY revision DESC LIMIT 1").fetchone()
            revision = 1 if previous is None else previous["revision"] + 1
            if expected_revision is not None and expected_revision != revision:
                raise RuntimeError("witness revision conflict")
            previous_fp = None if previous is None else json.loads(previous["record_json"])["snapshot_fingerprint"]
            record = {
                "schema": "tenir-independent-witness/v1",
                "subject": subject,
                "revision": revision,
                "snapshot_fingerprint": snapshot_fingerprint,
                "previous_fingerprint": previous_fp,
                "key_id": self.key_id,
                "issued_at": datetime.now(timezone.utc).isoformat(),
            }
            signature = self.private_key.sign(_canonical(record))
            db.execute("INSERT INTO witness_checkpoints(revision, record_json, signature) VALUES (?, ?, ?)",
                       (revision, json.dumps(record, sort_keys=True), signature))
            return {"record": record, "signature": signature.hex()}

    def verify(self, evidence: dict) -> bool:
        record = evidence.get("record") if isinstance(evidence, dict) else None
        if not isinstance(record, dict) or record.get("key_id") != self.key_id:
            return False
        try:
            signature = bytes.fromhex(evidence["signature"])
            self.private_key.public_key().verify(signature, _canonical(record))
        except (KeyError, ValueError, InvalidSignature):
            return False
        db = sqlite3.connect(self.db_path)
        try:
            row = db.execute("SELECT record_json, signature FROM witness_checkpoints WHERE revision=?", (record.get("revision"),)).fetchone()
        finally:
            db.close()
        return bool(row and row[0] == json.dumps(record, sort_keys=True) and bytes(row[1]) == signature)

