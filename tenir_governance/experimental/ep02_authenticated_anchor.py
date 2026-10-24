"""Experimental EP-02 authenticated remote anchor observation.

Root public key is out-of-band trusted. This module verifies a signed
challenge response; it does not persist a remote checkpoint or provide CAS.
"""
from dataclasses import dataclass
import json
from typing import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from tenir_governance.experimental.ep02_rotation import TrustCheckpoint
from tenir_governance.experimental.ep02_external_anchor import (
    AnchorObservation, AnchorStatus, assess_anchor,
)


@dataclass(frozen=True)
class SignedAnchorObservation:
    checkpoint: TrustCheckpoint
    anchor_id: str
    challenge: bytes
    signature: bytes


def anchor_payload(anchor_id: str, checkpoint: TrustCheckpoint,
                   challenge: bytes) -> bytes:
    if not anchor_id or len(challenge) < 16:
        raise ValueError("invalid anchor identity or challenge")
    if checkpoint.revision < 1 or len(checkpoint.snapshot_fingerprint) != 64:
        raise ValueError("invalid checkpoint")
    bytes.fromhex(checkpoint.snapshot_fingerprint)
    return b"TENIR-EP02-ANCHOR-V1\\x00" + json.dumps({
        "anchor_id": anchor_id,
        "revision": checkpoint.revision,
        "snapshot_fingerprint": checkpoint.snapshot_fingerprint,
        "challenge": challenge.hex(),
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_anchor(
    signed: SignedAnchorObservation, *, trusted_keys: Mapping[str, Ed25519PublicKey],
    expected_challenge: bytes,
) -> bool:
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
