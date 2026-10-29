"""JW-09 — versioned positive G4-A grant on the native SQLite store.

This is a new laboratory interface. It does not rename NativePositivePermitAuthority
into the historical authority, and it does not change ExperimentalG4AAuthority:
HOLD and execution_authorized=false stay. B3 remains outside the SQLite transaction.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from native_positive_permit import NativePositivePermitAuthority, _canonical
from w2_2b_canonical import bind_causal, request_hash as canon_request_hash
from w2_integration.native_dispatch_command import snapshot_command

GRANT_SCHEMA = "native-positive-g4a-grant/v1"


class NativePositiveG4A:
    """Positive grant on the native consensus store. Not the historical G4-A."""

    schema = GRANT_SCHEMA
    historical_execution_authorized = False

    def __init__(self, authority: NativePositivePermitAuthority):
        if not isinstance(authority, NativePositivePermitAuthority):
            raise TypeError("native positive permit authority required")
        self.authority = authority
        self.backend = authority.backend
        with authority._tx() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS w2_positive_g4a_v1_grants (
                     grant_id TEXT PRIMARY KEY,
                     operation_key TEXT NOT NULL UNIQUE,
                     evaluation_id TEXT NOT NULL,
                     permit_id TEXT NOT NULL,
                     reservation_id TEXT NOT NULL,
                     request_hash TEXT NOT NULL,
                     resource_id TEXT NOT NULL,
                     amount TEXT NOT NULL,
                     issued_at TEXT NOT NULL,
                     expires_at TEXT NOT NULL,
                     consumed INTEGER NOT NULL DEFAULT 0,
                     signature BLOB NOT NULL,
                     key_id TEXT NOT NULL,
                     schema_id TEXT NOT NULL
                   )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS w2_positive_g4a_v1_custody (
                     key_id TEXT PRIMARY KEY,
                     public_key_hex TEXT NOT NULL,
                     noted_at TEXT NOT NULL
                   )"""
            )
            self._require_same_key(db)

    def _require_external_key(self) -> None:
        if self.authority.key_custody != "EXTERNAL_HEX":
            raise RuntimeError("positive G4-A grant requires an injected key")

    def _require_same_key(self, db) -> None:
        row = db.execute(
            "SELECT key_id FROM w2_positive_g4a_v1_custody LIMIT 1"
        ).fetchone()
        if row and row["key_id"] != self.authority.key_id:
            raise RuntimeError("positive G4-A key changed since grant")

    def evaluate_and_grant(
        self, operation_key: str, evaluation_id: str, payload: dict, *, resource_id: str,
    ) -> dict:
        self._require_external_key()
        command = snapshot_command(payload)
        issued = self.authority.admit_reserve_issue(
            operation_key, evaluation_id, command, resource_id=resource_id,
        )
        if issued["request_hash"] != canon_request_hash(command):
            raise RuntimeError("grant request_hash diverges from evaluation")
        with self.authority._tx() as db:
            permit = db.execute(
                "SELECT permit_id, reservation_id, expires_at, evaluation_id, request_hash "
                "FROM w2_positive_v1_permits WHERE operation_key=?",
                (operation_key,),
            ).fetchone()
        if not permit or permit["request_hash"] != issued["request_hash"]:
            raise RuntimeError("permit missing after positive admit")
        grant_id = f"grant-{uuid.uuid4().hex}"
        issued_at = datetime.now(timezone.utc).isoformat()
        body = {
            "schema": GRANT_SCHEMA,
            "grant_id": grant_id,
            "operation_key": operation_key,
            "evaluation_id": evaluation_id,
            "permit_id": permit["permit_id"],
            "reservation_id": permit["reservation_id"],
            "request_hash": issued["request_hash"],
            "resource_id": resource_id,
            "amount": issued["amount"],
            "issued_at": issued_at,
            "expires_at": permit["expires_at"],
            "key_id": self.authority.key_id,
            "historical_execution_authorized": False,
        }
        signature = self.authority.private_key.sign(_canonical(body))
        public_hex = self.public_key_bytes().hex()
        with self.authority._write("positive_g4a_grant") as db:
            self._require_same_key(db)
            prior = db.execute(
                "SELECT key_id FROM w2_positive_g4a_v1_custody LIMIT 1"
            ).fetchone()
            if not prior:
                db.execute(
                    "INSERT INTO w2_positive_g4a_v1_custody (key_id, public_key_hex, noted_at) "
                    "VALUES (?, ?, ?)",
                    (self.authority.key_id, public_hex, issued_at),
                )
            db.execute(
                """INSERT INTO w2_positive_g4a_v1_grants
                   (grant_id, operation_key, evaluation_id, permit_id, reservation_id,
                    request_hash, resource_id, amount, issued_at, expires_at,
                    signature, key_id, schema_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (grant_id, operation_key, evaluation_id, permit["permit_id"],
                 permit["reservation_id"], issued["request_hash"], resource_id,
                 issued["amount"], issued_at, permit["expires_at"], signature,
                 self.authority.key_id, GRANT_SCHEMA),
            )
        return {**body, "signature": signature, "causal": self._causal(body)}

    def dispatch_to_b3(
        self, operation_key: str, idempotency_key: str, payload: dict, grant: dict, provider,
    ) -> dict:
        command = snapshot_command(payload)
        self._require_grant(operation_key, command, grant)
        self.authority._accepted_grant_id = grant["grant_id"]
        try:
            claim = self.authority.consume_and_claim(operation_key, command, idempotency_key)
        finally:
            self.authority._accepted_grant_id = None
        if claim.get("claimed") is not True:
            raise RuntimeError("positive grant did not produce a claim")
        with self.authority._write("positive_g4a_consume") as db:
            updated = db.execute(
                "UPDATE w2_positive_g4a_v1_grants SET consumed=1 "
                "WHERE grant_id=? AND operation_key=? AND consumed=0",
                (grant["grant_id"], operation_key),
            )
            if updated.rowcount != 1:
                raise RuntimeError("positive grant already consumed")
        receipt = provider.execute(
            operation_key, idempotency_key, command, claim=claim,
        )
        if receipt.get("request_hash") != grant["request_hash"]:
            raise RuntimeError("B3 receipt request_hash diverges from grant")
        return {
            "status": "EXECUTED",
            "claim": claim,
            "receipt": receipt,
            "causal": self._causal(grant, receipt_digest=receipt.get("effect_digest")),
        }

    def _require_grant(self, operation_key: str, command: dict, grant: dict) -> None:
        if not isinstance(grant, dict) or not grant.get("grant_id"):
            raise RuntimeError("positive G4-A grant required")
        expected = canon_request_hash(command)
        if grant.get("request_hash") != expected or grant.get("operation_key") != operation_key:
            raise RuntimeError("grant does not match request")
        if grant.get("schema") != GRANT_SCHEMA:
            raise RuntimeError("unsupported positive G4-A grant")
        if grant.get("historical_execution_authorized") is not False:
            raise RuntimeError("historical execution flag must stay false")
        body = {key: grant[key] for key in (
            "schema", "grant_id", "operation_key", "evaluation_id", "permit_id",
            "reservation_id", "request_hash", "resource_id", "amount",
            "issued_at", "expires_at", "key_id", "historical_execution_authorized",
        )}
        public = self.authority.private_key.public_key()
        try:
            public.verify(grant["signature"], _canonical(body))
        except (InvalidSignature, KeyError, TypeError) as exc:
            raise RuntimeError("positive G4-A grant signature invalid") from exc
        with self.authority._tx() as db:
            row = db.execute(
                "SELECT * FROM w2_positive_g4a_v1_grants WHERE grant_id=?",
                (grant["grant_id"],),
            ).fetchone()
        if not row or row["operation_key"] != operation_key or row["request_hash"] != expected:
            raise RuntimeError("positive G4-A grant not issued")
        if row["consumed"]:
            raise RuntimeError("positive G4-A grant already consumed")
        if row["expires_at"] <= datetime.now(timezone.utc).isoformat():
            raise RuntimeError("positive G4-A grant expired")
        if row["evaluation_id"] != grant["evaluation_id"] or row["permit_id"] != grant["permit_id"]:
            raise RuntimeError("grant causal identifiers diverge")

    @staticmethod
    def _causal(grant: dict, receipt_digest: str | None = None) -> dict:
        return bind_causal(
            grant["operation_key"],
            grant["evaluation_id"],
            grant["grant_id"],
            grant.get("idempotency_key", ""),
            grant["request_hash"],
            receipt_digest,
        )

    def public_key_bytes(self) -> bytes:
        return self.authority.private_key.public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw,
        )
