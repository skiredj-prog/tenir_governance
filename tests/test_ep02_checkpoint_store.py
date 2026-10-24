"""EP-02 durable checkpoint tests: restart, rollback, fail-closed corruption."""
from hashlib import sha256
from unittest.mock import patch
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tenir_governance.experimental.ep02_binding import TrustState
from tenir_governance.experimental.ep02_signed_trust import SignedTrustState, bound_snapshot_bytes
from tenir_governance.experimental.ep02_checkpoint_store import CheckpointStore, CheckpointCorrupted


def signed(root, revision):
    issuer = Ed25519PrivateKey.generate()
    raw = issuer.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    state = TrustState(revision, "policy-v1", (("issuer", "contracts"),))
    blank = SignedTrustState(state, b"", "root", (("issuer", raw),))
    return SignedTrustState(state, root.sign(bound_snapshot_bytes(blank)),
                            "root", blank.issuer_keys)


def test_bootstrap_requires_explicit_authorization(tmp_path):
    root = Ed25519PrivateKey.generate()
    store = CheckpointStore(tmp_path / "check.json")
    assert store.advance(signed(root, 1), roots={"root": root.public_key()}) == (
        False, "TRUST_CHECKPOINT_NOT_INITIALIZED")


def test_restart_preserves_high_water_and_blocks_rollback(tmp_path):
    root = Ed25519PrivateKey.generate()
    path = tmp_path / "check.json"
    store = CheckpointStore(path)
    one = signed(root, 1)
    two = signed(root, 2)
    assert store.advance(one, roots={"root": root.public_key()}, bootstrap=True)[0]
    assert CheckpointStore(path).advance(two, roots={"root": root.public_key()}) == (
        True, "ADVANCED")
    fresh = CheckpointStore(path)
    assert fresh.get().revision == 2
    assert not fresh.advance(one, roots={"root": root.public_key()})[0]


def test_corrupted_store_fails_closed(tmp_path):
    root = Ed25519PrivateKey.generate()
    path = tmp_path / "check.json"
    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(CheckpointCorrupted):
        CheckpointStore(path).advance(signed(root, 1),
                                      roots={"root": root.public_key()}, bootstrap=True)


def test_postwrite_error_requires_readback(tmp_path):
    root = Ed25519PrivateKey.generate()
    path = tmp_path / "check.json"
    store = CheckpointStore(path)
    one = signed(root, 1)
    import os
    original = os.fsync
    calls = [0]
    def fsync_then_error(fd):
        calls[0] += 1
        original(fd)
        if calls[0] == 2:
            raise OSError("injected directory fsync failure")
    with patch("tenir_governance.experimental.ep02_checkpoint_store.os.fsync",
               side_effect=fsync_then_error):
        with pytest.raises(OSError, match="directory fsync"):
            store.advance(one, roots={"root": root.public_key()}, bootstrap=True)
    assert CheckpointStore(path).get().revision == 1
    assert CheckpointStore(path).advance(one, roots={"root": root.public_key()}) == (
        True, "UNCHANGED")
