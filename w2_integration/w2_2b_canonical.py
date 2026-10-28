"""
W2.2b — Common versioned canonicalization and causal bindings
Same request bytes → same request_hash at every boundary.
"""
import json, hashlib

CANONICAL_VERSION = "canon-v1"

def canonical_bytes(obj: dict) -> bytes:
    """Versioned canonical encoding. Shared by evaluation, grant, B3, I8, RC-01."""
    return json.dumps(
        {"_canon": CANONICAL_VERSION, **obj},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

def request_hash(obj: dict) -> str:
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()

def bind_causal(
    operation_id: str,
    evaluation_id: str,
    grant_id: str,
    idempotency_key: str,
    request_hash_value: str,
    receipt_digest: str = None,
) -> dict:
    return {
        "operation_id": operation_id,
        "evaluation_id": evaluation_id,
        "grant_id": grant_id,
        "idempotency_key": idempotency_key,
        "request_hash": request_hash_value,
        "receipt_digest": receipt_digest,
        "canon_version": CANONICAL_VERSION,
    }
