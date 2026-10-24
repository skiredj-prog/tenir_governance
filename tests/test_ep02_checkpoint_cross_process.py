"""EP-02 POSIX multiprocess checkpoint characterizations."""
import multiprocessing as mp
import shutil
from concurrent.futures import ProcessPoolExecutor

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tenir_governance.experimental.ep02_binding import TrustState
from tenir_governance.experimental.ep02_signed_trust import SignedTrustState, bound_snapshot_bytes
from tenir_governance.experimental.ep02_checkpoint_store import CheckpointStore


def make_snapshot(root, revision):
    issuer = Ed25519PrivateKey.generate()
    raw = issuer.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    state = TrustState(revision, "v1", (("issuer", "contracts"),))
    template = SignedTrustState(state, b"", "root", (("issuer", raw),))
    return SignedTrustState(state, root.sign(bound_snapshot_bytes(template)),
                            "root", template.issuer_keys)


def advance(path, candidate, pub):
    return CheckpointStore(path).advance(candidate, roots={"root": pub})


def test_two_processes_same_revision_different_content_never_both_advance(tmp_path):
    root = Ed25519PrivateKey.generate()
    pub = root.public_key()
    path = tmp_path / "checkpoint.json"
    base = make_snapshot(root, 1)
    assert CheckpointStore(path).advance(base, roots={"root": pub}, bootstrap=True)[0]
    a = make_snapshot(root, 2)
    b = make_snapshot(root, 2)
    with ProcessPoolExecutor(max_workers=2, mp_context=mp.get_context("spawn")) as pool:
        x = pool.submit(advance, str(path), a, pub)
        y = pool.submit(advance, str(path), b, pub)
        results = [x.result(timeout=30), y.result(timeout=30)]
    assert sum(ok for ok, _ in results) == 1
    assert CheckpointStore(path).get().revision == 2


def test_restored_old_checkpoint_is_not_detectable_without_external_anchor(tmp_path):
    root = Ed25519PrivateKey.generate()
    pub = root.public_key()
    path = tmp_path / "checkpoint.json"
    store = CheckpointStore(path)
    one, two = make_snapshot(root, 1), make_snapshot(root, 2)
    assert store.advance(one, roots={"root": pub}, bootstrap=True)[0]
    old_backup = tmp_path / "old-copy.json"
    shutil.copy2(path, old_backup)
    assert store.advance(two, roots={"root": pub})[0]
    shutil.copy2(old_backup, path)
    assert CheckpointStore(path).get().revision == 1
    # Expected characterization: restore rollback is undetectable if checkpoint
    # and all of its local trust assumptions are rolled back together.
