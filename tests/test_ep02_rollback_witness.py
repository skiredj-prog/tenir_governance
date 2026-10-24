"""Conditional anti-rollback evidence from an independent high-water witness."""
import shutil
import sqlite3

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tenir_governance.experimental.ep02_binding import TrustState
from tenir_governance.experimental.ep02_durable_cas import (
    DurableAnchorCAS,
    DurableCASResult,
)
from tenir_governance.experimental.ep02_external_anchor import (
    AnchorObservation,
    AnchorStatus,
    assess_anchor,
)
from tenir_governance.experimental.ep02_signed_trust import (
    SignedTrustState,
    bound_snapshot_bytes,
)


def _signed(root, revision):
    state = TrustState(revision, "policy-v1", (("issuer", "contracts"),))
    template = SignedTrustState(state, b"", "root", ())
    return SignedTrustState(
        state, root.sign(bound_snapshot_bytes(template)), "root", (),
    )


def _store(path, root, authority):
    return DurableAnchorCAS.provision(
        path,
        authority_id=authority,
        trust_profile_id="institutional-roots",
        trust_generation=1,
        roots={"root": root.public_key()},
    )


def _backup(path, destination):
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    finally:
        connection.close()
    shutil.copyfile(path, destination)


def _restore(path, source):
    for candidate in (path, type(path)(f"{path}-wal"), type(path)(f"{path}-shm")):
        candidate.unlink(missing_ok=True)
    shutil.copyfile(source, path)


def _initialize_pair(tmp_path):
    root = Ed25519PrivateKey.generate()
    local_path = tmp_path / "local.sqlite3"
    witness_path = tmp_path / "witness.sqlite3"
    local = _store(local_path, root, "local-authority")
    witness = _store(witness_path, root, "independent-witness")
    first = _signed(root, 1)
    assert local.bootstrap("bootstrap-1", first).result == DurableCASResult.APPLIED
    assert witness.bootstrap("bootstrap-1", first).result == DurableCASResult.APPLIED
    return root, local_path, witness_path, local, witness


def test_preserved_independent_witness_detects_restored_local_authority(tmp_path):
    root, local_path, _, local, witness = _initialize_pair(tmp_path)
    local_backup = tmp_path / "local-revision-1.backup"
    _backup(local_path, local_backup)
    successor = _signed(root, 2)
    assert local.compare_and_swap(
        "rotation-2", local.get(), successor,
    ).result == DurableCASResult.APPLIED
    assert witness.compare_and_swap(
        "rotation-2", witness.get(), successor,
    ).result == DurableCASResult.APPLIED

    _restore(local_path, local_backup)
    restored = DurableAnchorCAS.open(local_path).get()
    independent = witness.get()
    assert restored.revision == 1
    assert independent.revision == 2
    assert assess_anchor(
        restored, AnchorObservation(AnchorStatus.CURRENT, independent),
    ) == (False, "LOCAL_CHECKPOINT_STALE")


def test_joint_consistent_restore_is_not_detectable_by_local_comparison(tmp_path):
    root, local_path, witness_path, local, witness = _initialize_pair(tmp_path)
    local_backup = tmp_path / "local-revision-1.backup"
    witness_backup = tmp_path / "witness-revision-1.backup"
    _backup(local_path, local_backup)
    _backup(witness_path, witness_backup)
    successor = _signed(root, 2)
    local.compare_and_swap("rotation-2", local.get(), successor)
    witness.compare_and_swap("rotation-2", witness.get(), successor)

    _restore(local_path, local_backup)
    _restore(witness_path, witness_backup)
    restored_local = DurableAnchorCAS.open(local_path).get()
    restored_witness = DurableAnchorCAS.open(witness_path).get()
    assert assess_anchor(
        restored_local,
        AnchorObservation(AnchorStatus.CURRENT, restored_witness),
    ) == (True, "CHECKPOINTS_AGREE")
