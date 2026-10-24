"""EP-02 local CAS, including real POSIX process termination boundaries.

Process termination leaves the host kernel and filesystem running. These tests
do not simulate power loss, disk failure, checkpoint restore or multiple hosts.
"""
import multiprocessing as mp
import os
from unittest.mock import patch
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)

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


_CRASH_EXIT_CODE = 73


def _public_bytes(root):
    return root.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _competing_cas(path, expected, successor, public_bytes, barrier, results):
    """Spawned worker; each opens its own store and competes after the barrier."""
    roots = {"root": Ed25519PublicKey.from_public_bytes(public_bytes)}
    store = LocalAnchorCAS(path)
    barrier.wait(timeout=20)
    response = store.compare_and_swap(expected, successor, roots=roots)
    results.put(response)


def _crash_cas(path, expected, successor, public_bytes, boundary):
    """Execute actual writes/flushes, then terminate without Python cleanup."""
    roots = {"root": Ed25519PublicKey.from_public_bytes(public_bytes)}
    real_fsync, real_replace = os.fsync, os.replace
    calls = 0

    def fsync_and_exit(fd):
        nonlocal calls
        calls += 1
        if boundary == "before_file_fsync" and calls == 1:
            os._exit(_CRASH_EXIT_CODE)
        real_fsync(fd)
        if boundary == "after_file_fsync" and calls == 1:
            os._exit(_CRASH_EXIT_CODE)
        if boundary == "after_directory_fsync" and calls == 2:
            os._exit(_CRASH_EXIT_CODE)

    def replace_and_exit(source, destination):
        real_replace(source, destination)
        if boundary == "after_replace":
            os._exit(_CRASH_EXIT_CODE)

    with patch("tenir_governance.experimental.ep02_checkpoint_store.os.fsync",
               side_effect=fsync_and_exit), patch(
                   "tenir_governance.experimental.ep02_checkpoint_store.os.replace",
                   side_effect=replace_and_exit):
        LocalAnchorCAS(path).compare_and_swap(expected, successor, roots=roots)
    raise AssertionError(f"crash boundary was not reached: {boundary}")


def _terminate_at_boundary(path, expected, successor, root, boundary):
    process = mp.get_context("spawn").Process(
        target=_crash_cas,
        args=(str(path), expected, successor, _public_bytes(root), boundary),
    )
    try:
        process.start()
        process.join(timeout=20)
        assert process.exitcode == _CRASH_EXIT_CODE, (
            f"worker did not reach {boundary}: exitcode={process.exitcode}"
        )
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=10)
        assert not process.is_alive(), "crash worker did not terminate"
        process.close()


@pytest.mark.parametrize("iteration", range(3))
def test_two_processes_cas_exactly_one_winner(tmp_path, iteration):
    from hashlib import sha256
    from tenir_governance.experimental.ep02_rotation import TrustCheckpoint

    root = Ed25519PrivateKey.generate()
    roots = {"root": root.public_key()}
    path = tmp_path / f"checkpoint-{iteration}.json"
    store = LocalAnchorCAS(path)
    assert store.advance(make_signed(root, 1), roots=roots, bootstrap=True)[0]
    expected = store.get()
    successors = [make_signed(root, 2), make_signed(root, 2)]
    possible_winners = {
        TrustCheckpoint(2, sha256(bound_snapshot_bytes(successor)).hexdigest())
        for successor in successors
    }
    context = mp.get_context("spawn")
    barrier, results = context.Barrier(3), context.Queue()
    workers = [
        context.Process(
            target=_competing_cas,
            args=(str(path), expected, successor, _public_bytes(root),
                  barrier, results),
        )
        for successor in successors
    ]
    try:
        for worker in workers:
            worker.start()
        barrier.wait(timeout=20)
        for worker in workers:
            worker.join(timeout=20)
            assert worker.exitcode == 0
        responses = [results.get(timeout=5) for _ in workers]
        assert sorted(response.result.value for response in responses) == [
            "APPLIED", "CONFLICT",
        ]
        winner = next(response for response in responses
                      if response.result == CASResult.APPLIED)
        assert winner.checkpoint in possible_winners
        assert all(response.checkpoint == winner.checkpoint
                   for response in responses)
        assert LocalAnchorCAS(path).get() == winner.checkpoint
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=10)
            assert not worker.is_alive(), "competing worker did not terminate"
            worker.close()
        results.close()
        results.join_thread()


@pytest.mark.parametrize("boundary,successor_visible", [
    ("before_file_fsync", False),
    ("after_file_fsync", False),
    ("after_replace", True),
    ("after_directory_fsync", True),
])
def test_cas_process_crash_readback_and_retry(tmp_path, boundary, successor_visible):
    from hashlib import sha256
    from tenir_governance.experimental.ep02_rotation import TrustCheckpoint

    root = Ed25519PrivateKey.generate()
    roots = {"root": root.public_key()}
    path = tmp_path / "checkpoint.json"
    store = LocalAnchorCAS(path)
    assert store.advance(make_signed(root, 1), roots=roots, bootstrap=True)[0]
    expected = store.get()
    successor = make_signed(root, 2)
    target = TrustCheckpoint(2, sha256(bound_snapshot_bytes(successor)).hexdigest())

    _terminate_at_boundary(path, expected, successor, root, boundary)

    reopened = LocalAnchorCAS(path)
    assert reopened.get() == (target if successor_visible else expected)
    response = reopened.compare_and_swap(expected, successor, roots=roots)
    assert response.result == (
        CASResult.CONFLICT if successor_visible else CASResult.APPLIED
    )
    assert response.checkpoint == target
    assert LocalAnchorCAS(path).get() == target


def test_lost_ack_then_later_writer_preserves_latest_checkpoint(tmp_path):
    """An old retry reports current conflict; it cannot identify its lost ACK."""
    root = Ed25519PrivateKey.generate()
    roots = {"root": root.public_key()}
    path = tmp_path / "checkpoint.json"
    store = LocalAnchorCAS(path)
    assert store.advance(make_signed(root, 1), roots=roots, bootstrap=True)[0]
    expected = store.get()
    successor = make_signed(root, 2)
    _terminate_at_boundary(path, expected, successor, root, "after_directory_fsync")
    reopened = LocalAnchorCAS(path)
    two = reopened.get()
    assert two.revision == 2
    later = reopened.compare_and_swap(two, make_signed(root, 3), roots=roots)
    assert later.result == CASResult.APPLIED
    retry = LocalAnchorCAS(path).compare_and_swap(expected, successor, roots=roots)
    assert retry.result == CASResult.CONFLICT
    assert retry.checkpoint == later.checkpoint
    assert LocalAnchorCAS(path).get() == later.checkpoint
