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

    def __init__(self, db_path: str, *, key_env: str = "TENIR_WITNESS_PRIVATE_KEY_HEX",
                 verifiable_recovery: bool = False):
        self.db_path = os.path.abspath(db_path)
        self.verifiable_recovery = verifiable_recovery
        secret = os.environ.get(key_env)
        if verifiable_recovery and not secret:
            raise RuntimeError("JW-10 verifiable recovery requires injected witness key")
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
            # JW-10: determine persisted mode before any schema mutation.
            # The default legacy constructor must never downgrade a store
            # already pinned for verifiable recovery, even with the SAME key.
            pin_table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='witness_custody_pin'"
            ).fetchone()
            prior_pin = (db.execute(
                "SELECT key_id FROM witness_custody_pin WHERE singleton=1"
            ).fetchone() if pin_table else None)
            if prior_pin is not None and not verifiable_recovery:
                raise RuntimeError("JW-10 witness verifiable custody downgrade prohibited")
            if pin_table and prior_pin is not None and prior_pin["key_id"] != self.key_id:
                raise RuntimeError("JW-10 witness custody key changed on restart")
            # Preexisting unsigned/ephemeral witness checkpoints cannot be
            # silently relabelled as verifiably recoverable on a later boot.
            existing_checkpoints = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='witness_checkpoints'"
            ).fetchone()
            if verifiable_recovery and prior_pin is None and existing_checkpoints:
                if db.execute("SELECT 1 FROM witness_checkpoints LIMIT 1").fetchone():
                    raise RuntimeError("JW-10 cannot upgrade legacy witness checkpoints")
            db.execute("CREATE TABLE IF NOT EXISTS witness_custody_pin "
                       "(singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                       "key_id TEXT NOT NULL)")
            if verifiable_recovery and prior_pin is None:
                db.execute("INSERT INTO witness_custody_pin VALUES(1,?)",
                           (self.key_id,))
            db.execute(
                "CREATE TABLE IF NOT EXISTS witness_checkpoints ("
                "revision INTEGER PRIMARY KEY, record_json TEXT NOT NULL, "
                "signature BLOB NOT NULL, subject TEXT)"
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(witness_checkpoints)")}
            if "subject" not in columns:
                db.execute("ALTER TABLE witness_checkpoints ADD COLUMN subject TEXT")
            for row in db.execute(
                "SELECT revision, record_json FROM witness_checkpoints WHERE subject IS NULL"
            ):
                subject = json.loads(row["record_json"]).get("subject")
                db.execute(
                    "UPDATE witness_checkpoints SET subject=? WHERE revision=?",
                    (subject, row["revision"]),
                )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS witness_subject_unique "
                "ON witness_checkpoints(subject)"
            )

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

    def issue(self, subject: str, snapshot_fingerprint: str, *, expected_revision: int | None = None,
              identity: dict | None = None) -> dict:
        if not subject or not snapshot_fingerprint:
            raise ValueError("subject and fingerprint are required")
        if identity is not None and set(identity) != {"execution_id", "claim_token"}:
            raise ValueError("witness identity must bind execution_id and claim_token")
        with self._tx() as db:
            current_pin=db.execute(
                "SELECT key_id FROM witness_custody_pin WHERE singleton=1"
            ).fetchone()
            if current_pin is not None and (
                not self.verifiable_recovery or current_pin["key_id"]!=self.key_id
            ):
                raise RuntimeError("JW-10 old Witness instance fenced by verifiable custody")
            existing = db.execute(
                "SELECT revision, record_json, signature FROM witness_checkpoints WHERE subject=?",
                (subject,),
            ).fetchone()
            if existing:
                record = json.loads(existing["record_json"])
                if record.get("snapshot_fingerprint") != snapshot_fingerprint:
                    raise RuntimeError("JW-09 witness subject already resolved")
                if identity is not None and (
                    record.get("execution_id") is None
                    or record.get("claim_token") is None
                ):
                    raise RuntimeError("JW-09 witness checkpoint missing immutable identity")
                if record.get("key_id") != self.key_id:
                    raise RuntimeError("JW-10 witness checkpoint custody mismatch")
                return {"record": record, "signature": bytes(existing["signature"]).hex()}
            previous = db.execute(
                "SELECT revision, record_json FROM witness_checkpoints ORDER BY revision DESC LIMIT 1"
            ).fetchone()
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
            if identity is not None:
                record["execution_id"] = identity["execution_id"]
                record["claim_token"] = identity["claim_token"]
            signature = self.private_key.sign(_canonical(record))
            db.execute(
                "INSERT INTO witness_checkpoints(revision, record_json, signature, subject) "
                "VALUES (?, ?, ?, ?)",
                (revision, json.dumps(record, sort_keys=True), signature, subject),
            )
            return {"record": record, "signature": signature.hex()}

    def verify(self, evidence: dict, *, subject: str | None = None) -> bool:
        record = evidence.get("record") if isinstance(evidence, dict) else None
        if not isinstance(record, dict) or record.get("key_id") != self.key_id:
            return False
        if subject is not None and record.get("subject") != subject:
            return False
        try:
            signature = bytes.fromhex(evidence["signature"])
            self.private_key.public_key().verify(signature, _canonical(record))
        except (KeyError, ValueError, InvalidSignature):
            return False
        db = sqlite3.connect(self.db_path)
        try:
            pin=db.execute("SELECT key_id FROM witness_custody_pin WHERE singleton=1").fetchone()
            if pin is not None and (
                not self.verifiable_recovery or pin[0]!=self.key_id
            ):
                return False
            row = db.execute(
                "SELECT record_json, signature, subject FROM witness_checkpoints WHERE revision=?",
                (record.get("revision"),),
            ).fetchone()
        finally:
            db.close()
        if not row or row[0] != json.dumps(record, sort_keys=True) or bytes(row[1]) != signature:
            return False
        if subject is not None and row[2] != subject:
            return False
        return True

