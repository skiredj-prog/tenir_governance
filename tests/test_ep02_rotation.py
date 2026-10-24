"""EP-02 snapshot monotonicity and key-rotation regression tests."""
from hashlib import sha256

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tenir_governance.experimental.ep02_binding import TrustState
from tenir_governance.experimental.ep02_signed_trust import (
    SignedTrustState, bound_snapshot_bytes,
)
from tenir_governance.experimental.ep02_rotation import (
    TrustCheckpoint, validate_successor,
)


def signed(root, revision, issuer_key, *, policy="p1"):
    raw = issuer_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)
    state = TrustState(revision, policy, (("issuer", "contracts"),))
    template = SignedTrustState(state, b"", "root", (("issuer", raw),))
    return SignedTrustState(
        state, root.sign(bound_snapshot_bytes(template)), "root",
        template.issuer_keys)


def checkpoint(snapshot):
    return TrustCheckpoint(
        snapshot.state.revision, sha256(bound_snapshot_bytes(snapshot)).hexdigest())


def test_key_rotation_advances_revision():
    root = Ed25519PrivateKey.generate()
    old = signed(root, 1, Ed25519PrivateKey.generate())
    rotated = signed(root, 2, Ed25519PrivateKey.generate())
    ok, status, next_checkpoint = validate_successor(
        checkpoint(old), rotated, roots={"root": root.public_key()})
    assert ok and status == "ADVANCED"
    assert next_checkpoint.revision == 2


def test_same_revision_key_substitution_is_rejected():
    root = Ed25519PrivateKey.generate()
    old = signed(root, 1, Ed25519PrivateKey.generate())
    changed = signed(root, 1, Ed25519PrivateKey.generate())
    assert validate_successor(
        checkpoint(old), changed, roots={"root": root.public_key()})[:2] == (
            False, "SAME_REVISION_EQUIVOCATION")


def test_old_snapshot_replay_is_rejected():
    root = Ed25519PrivateKey.generate()
    old = signed(root, 1, Ed25519PrivateKey.generate())
    newer = signed(root, 2, Ed25519PrivateKey.generate())
    assert validate_successor(
        checkpoint(newer), old, roots={"root": root.public_key()})[0] is False


def test_unchanged_snapshot_is_safe_noop():
    root = Ed25519PrivateKey.generate()
    old = signed(root, 1, Ed25519PrivateKey.generate())
    assert validate_successor(
        checkpoint(old), old, roots={"root": root.public_key()})[:2] == (
            True, "UNCHANGED")


def test_wrong_root_signature_cannot_advance():
    root = Ed25519PrivateKey.generate()
    attacker = Ed25519PrivateKey.generate()
    old = signed(root, 1, Ed25519PrivateKey.generate())
    forgery = signed(attacker, 2, Ed25519PrivateKey.generate())
    assert validate_successor(
        checkpoint(old), forgery, roots={"root": root.public_key()})[0] is False
