"""EP-02 local CAS: stale writers, monotone persistence and ambiguous ack."""
from unittest.mock import patch
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tenir_governance.experimental.ep02_binding import TrustState
from tenir_governance.experimental.ep02_signed_trust import SignedTrustState, bound_snapshot_bytes
from tenir_governance.experimental.ep02_checkpoint_store import CheckpointStore
from tenir_governance.experimental.ep02_local_cas import LocalAnchorCAS, CASResult


def make_signed(root, rev):
    issuer = Ed25519PrivateKey.generate()
    raw = issuer.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    state = TrustState(rev, "policy-v1", (("issuer", "contracts"),))
    template = SignedTrustState(state, b"", "root", (("issuer", raw),))
    return SignedTrustState(state, root.sign(bound_snapshot_bytes(template)),
                            "root", template.issuer_keys)


def test_stale_expected_state_rejected_without_write(tmp_path):
    root = Ed25519PrivateKey.generate()
    roots = {"root": root.public_key()}
    store = LocalAnchorCAS(tmp_path / "cp.json")
    one, two, alternate = (make_signed(root, x) for x in (1, 2, 2))
    assert store.advance(one, roots=roots, bootstrap=True)[0]
    expected = store.get()
    first = store.compare_and_swap(expected, two, roots=roots)
    assert first.result == CASResult.APPLIED
    rejected = store.compare_and_swap(expected, alternate, roots=roots)
    assert rejected.result == CASResult.CONFLICT
    assert store.get() == first.checkpoint


def test_no_initial_state_cannot_cas(tmp_path):
    root = Ed25519PrivateKey.generate()
    store = LocalAnchorCAS(tmp_path / "missing.json")
    from tenir_governance.experimental.ep02_rotation import TrustCheckpoint
    response = store.compare_and_swap(
        TrustCheckpoint(1, "0" * 64), make_signed(root, 2),
        roots={"root": root.public_key()})
    assert response.result == CASResult.NOT_INITIALIZED


def test_error_after_replace_has_ambiguous_outcome_and_readback(tmp_path):
    root = Ed25519PrivateKey.generate()
    roots = {"root": root.public_key()}
    store = LocalAnchorCAS(tmp_path / "cp.json")
    assert store.advance(make_signed(root, 1), roots=roots, bootstrap=True)[0]
    expected = store.get()
    successor = make_signed(root, 2)
    import os
    original = os.fsync
    calls = [0]

    def post_replace_failure(fd):
        calls[0] += 1
        original(fd)
        if calls[0] == 2:
            raise OSError("injected directory flush failure")

    with patch("tenir_governance.experimental.ep02_checkpoint_store.os.fsync",
               side_effect=post_replace_failure):
        with pytest.raises(OSError, match="directory flush"):
            store.compare_and_swap(expected, successor, roots=roots)
    assert store.get().revision == 2
    assert store.compare_and_swap(expected, successor, roots=roots).result == CASResult.CONFLICT
