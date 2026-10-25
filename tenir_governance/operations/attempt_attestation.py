"""Opt-in Ed25519 T6/T7 attestation verifier.

Trust roots and time come from independent configuration, not the attestation.
No network, revocation distribution, cross-host agreement or production PKI claim.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from typing import Callable, Mapping

from .attempt_qualification import QualificationError, VerifiedEvidence

_FIELDS = frozenset({
    "schema_version", "evidence_id", "attempt_id", "lei", "nonce", "operation_digest", "outcome",
    "authority_id", "issued_at_ms", "expires_at_ms", "signature_hex",
})
_SIGNED_FIELDS = _FIELDS - {"signature_hex"}
_OUTCOMES = frozenset({"EFFECT_CONFIRMED", "NON_EXECUTION_CONFIRMED"})
_HEX_128 = re.compile(r"^[0-9a-f]{128}$")


def canonical_attestation_payload(value: Mapping) -> bytes:
    """Conservative typed canonical encoding (not a general JCS implementation)."""
    if not isinstance(value, Mapping) or set(value) != _SIGNED_FIELDS:
        raise QualificationError("unexpected or missing attestation fields")
    for field in _SIGNED_FIELDS - {"issued_at_ms", "expires_at_ms"}:
        v = value[field]
        if not isinstance(v, str) or not v or unicodedata.normalize("NFC", v) != v:
            raise QualificationError("attestation strings must be non-empty NFC")
        if len(v) > 512 or any(ord(ch) < 32 for ch in v):
            raise QualificationError("invalid attestation text")
    for field in ("issued_at_ms", "expires_at_ms"):
        if type(value[field]) is not int or value[field] < 0:
            raise QualificationError("invalid attestation timestamps")
    if value["schema_version"] != "tenir.attempt.attestation.v1":
        raise QualificationError("unknown attestation schema version")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", value["operation_digest"]) is None:
        raise QualificationError("invalid bound operation fingerprint")
    if value["outcome"] not in _OUTCOMES:
        raise QualificationError("unsupported attestation outcome")
    if value["issued_at_ms"] > value["expires_at_ms"]:
        raise QualificationError("reversed attestation validity")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


class Ed25519AttemptVerifier:
    """Validate cryptographically authenticated, scoped and fresh attestations."""

    def __init__(self, trust_roots: Mapping[str, bytes], *,
                 revoked_authorities: frozenset[str] = frozenset(),
                 now_ms: Callable[[], int] | None = None,
                 max_validity_ms: int = 60_000):
        if not trust_roots or type(max_validity_ms) is not int or max_validity_ms <= 0:
            raise QualificationError("trusted roots and positive validity bound required")
        if any(not k or not isinstance(k, str) or not isinstance(v, bytes)
               or len(v) != 32 for k, v in trust_roots.items()):
            raise QualificationError("trust roots require authority IDs and 32-byte Ed25519 public keys")
        self._roots = dict(trust_roots)
        self._revoked = frozenset(revoked_authorities)
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._max_validity_ms = max_validity_ms

    def __call__(self, raw: Mapping) -> VerifiedEvidence:
        if not isinstance(raw, Mapping) or set(raw) != _FIELDS:
            raise QualificationError("invalid attestation envelope")
        payload = {k: raw[k] for k in _SIGNED_FIELDS}
        encoded = canonical_attestation_payload(payload)
        signature_hex = raw["signature_hex"]
        if not isinstance(signature_hex, str) or _HEX_128.fullmatch(signature_hex) is None:
            raise QualificationError("invalid signature encoding")
        authority = payload["authority_id"]
        if authority in self._revoked or authority not in self._roots:
            raise QualificationError("authority untrusted or revoked")
        now = self._now_ms()
        if type(now) is not int or now < 0:
            raise QualificationError("untrusted clock reading")
        if (payload["issued_at_ms"] > now or now > payload["expires_at_ms"]
                or payload["expires_at_ms"] - payload["issued_at_ms"] > self._max_validity_ms):
            raise QualificationError("attestation expired, premature or excessive TTL")
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            Ed25519PublicKey.from_public_bytes(self._roots[authority]).verify(
                bytes.fromhex(signature_hex), encoded
            )
        except ImportError as exc:
            raise QualificationError("Ed25519 cryptography extra is required") from exc
        except Exception as exc:
            raise QualificationError("Ed25519 signature verification rejected") from exc
        digest = hashlib.sha256(encoded + bytes.fromhex(signature_hex)).hexdigest()
        from .attempt_qualification import VerifiedEvidence
        return VerifiedEvidence(
            evidence_id=payload["evidence_id"],
            attempt_id=payload["attempt_id"],
            lei=payload["lei"],
            outcome=payload["outcome"],
            authority_id=authority,
            verification_receipt="ed25519-sha256:" + digest,
            nonce=payload["nonce"],
            operation_digest=payload["operation_digest"],
        )
