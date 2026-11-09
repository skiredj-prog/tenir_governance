"""Shared canonical fields for the B1 native dual-authorization boundary.

This module deliberately has no dependency on the UID services or the
Governor.  Native SQLite enforcement can therefore validate the durable proof
without accepting a caller callback, thread-local marker, or convenience flag
as authorization.
"""
from __future__ import annotations

import hashlib
import json


APPROVAL_FIELDS = frozenset({
    "schema", "audience", "approval_id", "nonce", "operation_key",
    "evaluation_id", "idempotency_key", "request_hash", "issued_at",
    "expires_at", "key_id",
})

GOVERNOR_RECEIPT_FIELDS = frozenset({
    "schema", "audience", "operation_key", "evaluation_id", "request_hash",
    "decision", "passage_id", "expires_at", "quarantine_epoch",
    "policy_context_hash", "key_id",
})

DUAL_GRANT_SCHEMA = "tenir.b1.native-dual-grant.v1"


def canonical_b1(value: dict) -> bytes:
    """Encode proof material identically to the existing B1 signatures."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def digest_b1(value: dict) -> str:
    return hashlib.sha256(canonical_b1(value)).hexdigest()
