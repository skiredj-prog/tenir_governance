"""Signed anchor challenge-response: authenticity, replay and equivocation."""
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tenir_governance.experimental.ep02_rotation import TrustCheckpoint
from tenir_governance.experimental.ep02_authenticated_anchor import (
    SignedAnchorObservation, anchor_payload, assess_signed_anchor,
)

CP = TrustCheckpoint(4, "a" * 64)
CHALLENGE = bytes(range(32))


def signed(key, checkpoint=CP, challenge=CHALLENGE):
    return SignedAnchorObservation(
        checkpoint, "anchor-1", challenge,
        key.sign(anchor_payload("anchor-1", checkpoint, challenge)))


def test_matching_signed_checkpoint():
    key = Ed25519PrivateKey.generate()
    assert assess_signed_anchor(CP, signed(key),
                                trusted_keys={"anchor-1": key.public_key()},
                                expected_challenge=CHALLENGE) == (
                                    True, "CHECKPOINTS_AGREE")


def test_replayed_response_for_old_challenge_is_rejected():
    key = Ed25519PrivateKey.generate()
    assert assess_signed_anchor(
        CP, signed(key), trusted_keys={"anchor-1": key.public_key()},
        expected_challenge=b"x" * 32)[0] is False


def test_unknown_anchor_and_invalid_signature_rejected():
    key = Ed25519PrivateKey.generate()
    other = Ed25519PrivateKey.generate()
    assert assess_signed_anchor(
        CP, signed(key), trusted_keys={},
        expected_challenge=CHALLENGE)[0] is False
    assert assess_signed_anchor(
        CP, signed(other), trusted_keys={"anchor-1": key.public_key()},
        expected_challenge=CHALLENGE)[0] is False


def test_signed_remote_newer_checkpoint_blocks_local():
    key = Ed25519PrivateKey.generate()
    remote = TrustCheckpoint(5, "b" * 64)
    assert assess_signed_anchor(
        CP, signed(key, remote), trusted_keys={"anchor-1": key.public_key()},
        expected_challenge=CHALLENGE) == (False, "LOCAL_CHECKPOINT_STALE")


def test_signature_cannot_be_reused_for_mutated_checkpoint():
    key = Ed25519PrivateKey.generate()
    original = signed(key)
    tampered = SignedAnchorObservation(
        TrustCheckpoint(5, "b" * 64), original.anchor_id,
        original.challenge, original.signature)
    assert assess_signed_anchor(
        CP, tampered, trusted_keys={"anchor-1": key.public_key()},
        expected_challenge=CHALLENGE)[0] is False
