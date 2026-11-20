"""Authentication of non-execution attestations (5.7.6).

A :class:`~tenir_governance.operations.registry.NonExecutionAttestation`
supplied by a caller of ``governor.fail()`` is proof only once the runtime has
verified that a declared attester signed it. This module provides:

- :func:`sign_non_execution_attestation`: what an attester (a reconciliation
  job, an operator console) does with its key before calling ``fail()``;
- :class:`HmacAttestationVerifier`: HMAC-SHA256 over the attestation's
  canonical signing payload, with the keys the deployment declares to the
  governor (standard library only);
- :class:`Ed25519AttestationVerifier`: public-key verification when the
  ``cryptography`` package is installed (the ``ep02``/``raft`` extras);
- :class:`AttestationVerifier`: the protocol a deployment may implement.

The verifier binds an attestation to a key the deployment trusts. It does not
observe the external world: whether the dispatched effect truly did not occur
remains assumption A of the manuscript.

Profile: agentic-governance-profile-v1.0.0
Compliance class: profile-bound
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import replace
from typing import Mapping, Protocol, runtime_checkable

from .registry import NonExecutionAttestation

HMAC_METHOD = "hmac-sha256"
ED25519_METHOD = "ed25519"


@runtime_checkable
class AttestationVerifier(Protocol):
    """What the governor asks of an attestation verifier."""

    def verify(self, attestation: NonExecutionAttestation) -> str | None:
        """Return the verification method string when the attestation is
        authentic under a declared key (e.g. ``"hmac-sha256:ops-key"``),
        ``None`` otherwise. Must never raise on malformed input."""
        ...


def _normalize_keys(keys: Mapping[str, bytes | str]) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for key_id, secret in keys.items():
        if not isinstance(key_id, str) or not key_id.strip():
            raise ValueError("attestation key ids must be non-empty strings")
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        if not isinstance(secret, (bytes, bytearray)) or len(secret) < 16:
            raise ValueError(f"attestation key {key_id!r} must be at least 16 bytes")
        out[key_id] = bytes(secret)
    return out


def hmac_signature(attestation: NonExecutionAttestation, secret: bytes | str) -> str:
    """Hex HMAC-SHA256 of the attestation's signing payload under ``secret``."""
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    return hmac.new(bytes(secret), attestation.signing_payload(), hashlib.sha256).hexdigest()


def sign_non_execution_attestation(
    attestation: NonExecutionAttestation, key_id: str, secret: bytes | str,
) -> NonExecutionAttestation:
    """Return ``attestation`` signed with the HMAC key ``key_id``.

    The attester keeps the secret; the governor is configured with the same
    secret under the same ``key_id`` (:class:`HmacAttestationVerifier`).
    """
    if not isinstance(key_id, str) or not key_id.strip():
        raise ValueError("key_id is required to sign an attestation")
    unsigned = replace(attestation, key_id=key_id, signature="",
                       verified_by_runtime=False, verification="")
    unsigned.validate()
    return replace(unsigned, signature=hmac_signature(unsigned, secret))


class HmacAttestationVerifier:
    """Verify attestations signed with :func:`sign_non_execution_attestation`."""

    method = HMAC_METHOD

    def __init__(self, keys: Mapping[str, bytes | str]):
        self._keys = _normalize_keys(keys)
        if not self._keys:
            raise ValueError("at least one attestation key is required")

    @property
    def key_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._keys))

    def verify(self, attestation: NonExecutionAttestation) -> str | None:
        try:
            attestation.validate()
        except ValueError:
            return None
        secret = self._keys.get(attestation.key_id)
        if secret is None or not attestation.signature:
            return None
        expected = hmac_signature(replace(attestation, signature=""), secret)
        if not hmac.compare_digest(expected, attestation.signature.lower()):
            return None
        return f"{self.method}:{attestation.key_id}"


class Ed25519AttestationVerifier:
    """Verify Ed25519 signatures (hex) over the signing payload.

    ``public_keys`` maps a key id to a raw 32-byte Ed25519 public key (bytes
    or hex string). Requires the ``cryptography`` package.
    """

    method = ED25519_METHOD

    def __init__(self, public_keys: Mapping[str, bytes | str]):
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        except ImportError as e:  # pragma: no cover - depends on the extra
            raise RuntimeError("Ed25519AttestationVerifier requires the 'cryptography' package") from e
        self._keys = {}
        for key_id, raw in public_keys.items():
            if not isinstance(key_id, str) or not key_id.strip():
                raise ValueError("attestation key ids must be non-empty strings")
            if isinstance(raw, str):
                raw = bytes.fromhex(raw)
            self._keys[key_id] = Ed25519PublicKey.from_public_bytes(bytes(raw))
        if not self._keys:
            raise ValueError("at least one attestation public key is required")

    @property
    def key_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._keys))

    def verify(self, attestation: NonExecutionAttestation) -> str | None:
        from cryptography.exceptions import InvalidSignature

        try:
            attestation.validate()
        except ValueError:
            return None
        public_key = self._keys.get(attestation.key_id)
        if public_key is None or not attestation.signature:
            return None
        try:
            public_key.verify(bytes.fromhex(attestation.signature),
                              replace(attestation, signature="").signing_payload())
        except (InvalidSignature, ValueError):
            return None
        return f"{self.method}:{attestation.key_id}"


def sign_non_execution_attestation_ed25519(
    attestation: NonExecutionAttestation, key_id: str, private_key,
) -> NonExecutionAttestation:
    """Sign with a ``cryptography`` ``Ed25519PrivateKey`` (hex signature)."""
    if not isinstance(key_id, str) or not key_id.strip():
        raise ValueError("key_id is required to sign an attestation")
    unsigned = replace(attestation, key_id=key_id, signature="",
                       verified_by_runtime=False, verification="")
    unsigned.validate()
    return replace(unsigned, signature=private_key.sign(unsigned.signing_payload()).hex())


__all__ = [
    "AttestationVerifier", "HmacAttestationVerifier", "Ed25519AttestationVerifier",
    "sign_non_execution_attestation", "sign_non_execution_attestation_ed25519",
    "hmac_signature", "HMAC_METHOD", "ED25519_METHOD",
]
