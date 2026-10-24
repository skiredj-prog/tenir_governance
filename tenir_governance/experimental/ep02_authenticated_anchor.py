"""Experimental EP-02 authenticated remote anchor observation.

Root public key is out-of-band trusted. This module verifies a signed
challenge response; it does not persist a remote checkpoint or provide CAS.
"""
from dataclasses import dataclass
import json
import re
import secrets
from typing import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from tenir_governance.experimental.ep02_rotation import TrustCheckpoint
from tenir_governance.experimental.ep02_external_anchor import (
    AnchorObservation, AnchorStatus, assess_anchor,
)


_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class SignedAnchorObservation:
    checkpoint: TrustCheckpoint
    anchor_id: str
    challenge: bytes
    signature: bytes


def anchor_payload(anchor_id: str, checkpoint: TrustCheckpoint,
                   challenge: bytes) -> bytes:
    if not isinstance(anchor_id, str) or not _IDENTITY.fullmatch(anchor_id):
        raise ValueError("invalid anchor identity or challenge")
    if not isinstance(challenge, bytes) or not 16 <= len(challenge) <= 64:
        raise ValueError("invalid anchor identity or challenge")
    if (
        not isinstance(checkpoint, TrustCheckpoint)
        or type(checkpoint.revision) is not int
        or checkpoint.revision < 1
        or not isinstance(checkpoint.snapshot_fingerprint, str)
        or not _HEX64.fullmatch(checkpoint.snapshot_fingerprint)
    ):
        raise ValueError("invalid checkpoint")
    return b"TENIR-EP02-ANCHOR-V1\\x00" + json.dumps({
        "anchor_id": anchor_id,
        "revision": checkpoint.revision,
        "snapshot_fingerprint": checkpoint.snapshot_fingerprint,
        "challenge": challenge.hex(),
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")


def new_anchor_challenge() -> bytes:
    """Return a fresh 256-bit challenge for one anchor observation."""
    return secrets.token_bytes(32)


def verify_anchor(
    signed: SignedAnchorObservation, *, trusted_keys: Mapping[str, Ed25519PublicKey],
    expected_challenge: bytes,
) -> bool:
    if (
        not isinstance(signed, SignedAnchorObservation)
        or not isinstance(expected_challenge, bytes)
        or not 16 <= len(expected_challenge) <= 64
        or not isinstance(trusted_keys, Mapping)
    ):
        return False
    key = trusted_keys.get(signed.anchor_id)
    if not isinstance(key, Ed25519PublicKey):
        return False
    if signed.challenge != expected_challenge:
        return False
    try:
        key.verify(signed.signature, anchor_payload(
            signed.anchor_id, signed.checkpoint, signed.challenge))
    except (InvalidSignature, TypeError, ValueError):
        return False
    return True


def assess_signed_anchor(
    local: TrustCheckpoint, signed: SignedAnchorObservation | None, *,
    trusted_keys: Mapping[str, Ed25519PublicKey],
    expected_challenge: bytes,
) -> tuple[bool, str]:
    if signed is None or not verify_anchor(
        signed, trusted_keys=trusted_keys,
        expected_challenge=expected_challenge,
    ):
        return False, "ANCHOR_AUTHENTICATION_FAILED"
    return assess_anchor(local, AnchorObservation(
        AnchorStatus.CURRENT, signed.checkpoint))
