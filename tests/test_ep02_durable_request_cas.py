"""EP-02 durable request outcomes: idempotency and UNKNOWN reconciliation."""
import multiprocessing as mp
import os
from pathlib import Path
import shutil
import sqlite3

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tenir_governance.experimental.ep02_binding import TrustState
from tenir_governance.experimental.ep02_rotation import TrustCheckpoint
from tenir_governance.experimental.ep02_signed_trust import (
    SignedTrustState,
    bound_snapshot_bytes,
)
from tenir_governance.experimental.ep02_durable_cas import (
    DurableAnchorCAS,
    DurableCASResult,
    LookupStatus,
    RequestIdConflict,
    StoreIntegrityError,
    StoreNotProvisioned,
    UnsupportedLegacyWriter,
)


def _raw_public(key):
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _signed(root, revision, policy="policy-v1"):
    issuer = Ed25519PrivateKey.generate()
    state = TrustState(revision, policy, (("issuer", "contracts"),))
    template = SignedTrustState(
        state, b"", "root", (("issuer", _raw_public(issuer)),)
    )
    return SignedTrustState(
        state,
        root.sign(bound_snapshot_bytes(template)),
        "root",
        template.issuer_keys,
    )


def _provision(path, root, authority="anchor-a"):
    return DurableAnchorCAS.provision(
        path,
        authority_id=authority,
        trust_profile_id="institutional-roots",
        trust_generation=1,
        roots={"root": root.public_key()},
    )


def _bootstrapped(path, root):
    store = _provision(path, root)
    first = _signed(root, 1)
    response = store.bootstrap("bootstrap-1", first)
    assert response.result == DurableCASResult.APPLIED
    assert response.reason == "BOOTSTRAPPED"
    return store, first


def _same_request_worker(path, request_id, expected, successor, queue, barrier):
    store = DurableAnchorCAS.open(path)
    barrier.wait(timeout=20)
    response = store.compare_and_swap(request_id, expected, successor)
    queue.put((response.result.value, response.replayed, response.sequence,
               response.result_checkpoint))


def _different_request_worker(path, request_id, expected, successor, queue, barrier):
    _same_request_worker(path, request_id, expected, successor, queue, barrier)


def _bootstrap_worker(path, request_id, snapshot, queue, barrier):
    store = DurableAnchorCAS.open(path)
    barrier.wait(timeout=20)
    response = store.bootstrap(request_id, snapshot)
    queue.put((response.result.value, response.reason, response.result_checkpoint))


def _crash_worker(path, request_id, expected, successor, point):
    class CrashAtPoint(DurableAnchorCAS):
        def _fault(self, current):
            if current == point:
                os._exit(73)

    store = CrashAtPoint.open(path)
    store.compare_and_swap(request_id, expected, successor)


def _observe_worker(path, request_id, request_digest, queue):
    store = DurableAnchorCAS.open(path)
    observation = store.lookup(request_id, request_digest)
    queue.put((
        observation.status.value,
        None if observation.response is None else observation.response.result.value,
        None if observation.current_checkpoint is None
        else observation.current_checkpoint.revision,
        store.request_count(),
    ))


def _blocked_writer(path, request_id, expected, successor, entered, release, queue):
    class BlockAfterLookup(DurableAnchorCAS):
        def _fault(self, point):
            if point == "after_request_lookup":
                entered.set()
                if not release.wait(timeout=20):
                    raise TimeoutError("test writer was not released")

    response = BlockAfterLookup.open(path).compare_and_swap(
        request_id, expected, successor,
    )
    queue.put(response.result.value)


def test_retry_after_restart_returns_original_applied_result(tmp_path):
    root = Ed25519PrivateKey.generate()
    path = tmp_path / "anchor.sqlite3"
    store, _ = _bootstrapped(path, root)
    expected = store.get()
    successor = _signed(root, 2)

    first = store.compare_and_swap("request-r", expected, successor)
    retry = DurableAnchorCAS.open(path).compare_and_swap(
        "request-r", expected, successor
    )

    assert first.result == DurableCASResult.APPLIED
    assert not first.replayed
    assert retry.result == DurableCASResult.APPLIED
    assert retry.replayed
    assert retry.sequence == first.sequence
    assert retry.observed_checkpoint == first.observed_checkpoint
    assert retry.result_checkpoint == first.result_checkpoint
    assert retry.current_checkpoint == first.result_checkpoint
    assert DurableAnchorCAS.open(path).request_count() == 2  # bootstrap + R


def test_old_request_result_survives_later_commit(tmp_path):
    root = Ed25519PrivateKey.generate()
    path = tmp_path / "anchor.sqlite3"
    store, _ = _bootstrapped(path, root)
    one = store.get()
    two_snapshot = _signed(root, 2)
    result_r = store.compare_and_swap("request-r", one, two_snapshot)
    two = store.get()
    result_s = store.compare_and_swap("request-s", two, _signed(root, 3))

    replay = DurableAnchorCAS.open(path).compare_and_swap(
        "request-r", one, two_snapshot
    )
    assert replay.result == DurableCASResult.APPLIED
    assert replay.sequence == result_r.sequence
    assert replay.result_checkpoint == result_r.result_checkpoint
    assert replay.current_checkpoint == result_s.result_checkpoint
    assert replay.current_checkpoint.revision == 3


def test_recorded_conflict_is_not_reinterpreted_after_state_changes(tmp_path):
    root = Ed25519PrivateKey.generate()
    path = tmp_path / "anchor.sqlite3"
    store, _ = _bootstrapped(path, root)
    actual = store.get()
    wrong = TrustCheckpoint(actual.revision, "0" * 64)
    wanted = _signed(root, 2)
    conflict = store.compare_and_swap("request-conflict", wrong, wanted)
    assert conflict.result == DurableCASResult.CONFLICT
    assert conflict.observed_checkpoint == actual
    assert store.compare_and_swap("request-advance", actual, wanted).result == (
        DurableCASResult.APPLIED
    )

    replay = store.compare_and_swap("request-conflict", wrong, wanted)
    assert replay.result == DurableCASResult.CONFLICT
    assert replay.replayed
    assert replay.sequence == conflict.sequence
    assert replay.observed_checkpoint == actual
    assert replay.current_checkpoint.revision == 2


@pytest.mark.parametrize("change", [
    "expected", "successor", "signature", "signer", "issuer-key", "operation",
])
def test_request_id_is_bound_to_immutable_context(tmp_path, change):
    root = Ed25519PrivateKey.generate()
    path = tmp_path / "anchor.sqlite3"
    store, _ = _bootstrapped(path, root)
    expected = store.get()
    successor = _signed(root, 2)
    original = store.compare_and_swap("request-r", expected, successor)
    assert original.result == DurableCASResult.APPLIED
    before = store.get()

    if change == "expected":
        call = lambda: store.compare_and_swap(
            "request-r", TrustCheckpoint(1, "f" * 64), successor
        )
    elif change == "successor":
        call = lambda: store.compare_and_swap(
            "request-r", expected, _signed(root, 3)
        )
    elif change == "signature":
        altered = SignedTrustState(
            successor.state, bytes([successor.signature[0] ^ 1]) + successor.signature[1:],
            successor.signer_id, successor.issuer_keys,
        )
        call = lambda: store.compare_and_swap("request-r", expected, altered)
    elif change == "signer":
        altered = SignedTrustState(
            successor.state, successor.signature, "another-root", successor.issuer_keys,
        )
        call = lambda: store.compare_and_swap("request-r", expected, altered)
    elif change == "issuer-key":
        altered = SignedTrustState(
            successor.state,
            successor.signature,
            successor.signer_id,
            (("issuer", _raw_public(Ed25519PrivateKey.generate())),),
        )
        call = lambda: store.compare_and_swap("request-r", expected, altered)
    else:
        call = lambda: store.bootstrap("request-r", successor)

    with pytest.raises(RequestIdConflict):
        call()
    assert store.get() == before
    assert store.request_count() == 2


def test_lookup_resolves_recorded_and_not_recorded_without_guessing(tmp_path):
    root = Ed25519PrivateKey.generate()
    path = tmp_path / "anchor.sqlite3"
    store, _ = _bootstrapped(path, root)
    expected, successor = store.get(), _signed(root, 2)
    digest = store.cas_request_digest(expected, successor)

    missing = store.lookup("request-r", digest)
    assert missing.status == LookupStatus.NOT_RECORDED
    assert missing.response is None
    assert missing.current_checkpoint == expected

    applied = store.compare_and_swap("request-r", expected, successor)
    found = DurableAnchorCAS.open(path).lookup("request-r", digest)
    assert found.status == LookupStatus.RECORDED
    assert found.response.result == DurableCASResult.APPLIED
    assert found.response.sequence == applied.sequence
    assert found.response.replayed

    with pytest.raises(RequestIdConflict):
        store.lookup("request-r", "0" * 64)


def test_two_processes_same_request_apply_once_and_replay_one_result(tmp_path):
    root = Ed25519PrivateKey.generate()
    path = tmp_path / "anchor.sqlite3"
    store, _ = _bootstrapped(path, root)
    expected, successor = store.get(), _signed(root, 2)
    context = mp.get_context("spawn")
    barrier, queue = context.Barrier(3), context.Queue()
    workers = [
        context.Process(
            target=_same_request_worker,
            args=(str(path), "same-r", expected, successor, queue, barrier),
        )
        for _ in range(2)
    ]
    try:
        for worker in workers:
            worker.start()
        barrier.wait(timeout=20)
        for worker in workers:
            worker.join(timeout=30)
            assert worker.exitcode == 0
        results = [queue.get(timeout=5) for _ in workers]
        assert {item[0] for item in results} == {"APPLIED"}
        assert sorted(item[1] for item in results) == [False, True]
        assert len({item[2] for item in results}) == 1
        assert len({item[3] for item in results}) == 1
        assert store.request_count() == 2
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=10)
            assert not worker.is_alive()
            worker.close()
        queue.close()
        queue.join_thread()


def test_two_request_ids_with_same_expected_record_applied_and_conflict(tmp_path):
    root = Ed25519PrivateKey.generate()
    path = tmp_path / "anchor.sqlite3"
    store, _ = _bootstrapped(path, root)
    expected = store.get()
    successors = [_signed(root, 2), _signed(root, 2)]
    context = mp.get_context("spawn")
    barrier, queue = context.Barrier(3), context.Queue()
    workers = [
        context.Process(
            target=_different_request_worker,
            args=(str(path), f"request-{index}", expected, successor,
                  queue, barrier),
        )
        for index, successor in enumerate(successors)
    ]
    try:
        for worker in workers:
            worker.start()
        barrier.wait(timeout=20)
        for worker in workers:
            worker.join(timeout=30)
            assert worker.exitcode == 0
        results = [queue.get(timeout=5) for _ in workers]
        assert sorted(item[0] for item in results) == ["APPLIED", "CONFLICT"]
        assert store.request_count() == 3
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=10)
            assert not worker.is_alive()
            worker.close()
        queue.close()
        queue.join_thread()


def test_competing_bootstrap_records_one_applied_and_one_conflict(tmp_path):
    root = Ed25519PrivateKey.generate()
    path = tmp_path / "anchor.sqlite3"
    _provision(path, root).close()
    snapshots = [_signed(root, 1), _signed(root, 1)]
    context = mp.get_context("spawn")
    barrier, queue = context.Barrier(3), context.Queue()
    workers = [
        context.Process(
            target=_bootstrap_worker,
            args=(str(path), f"bootstrap-{index}", snapshot, queue, barrier),
        )
        for index, snapshot in enumerate(snapshots)
    ]
    try:
        for worker in workers:
            worker.start()
        barrier.wait(timeout=20)
        for worker in workers:
            worker.join(timeout=30)
            assert worker.exitcode == 0
        results = [queue.get(timeout=5) for _ in workers]
        assert sorted(item[0] for item in results) == ["APPLIED", "CONFLICT"]
        assert DurableAnchorCAS.open(path).get().revision == 1
        assert DurableAnchorCAS.open(path).request_count() == 2
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=10)
            assert not worker.is_alive()
            worker.close()
        queue.close()
        queue.join_thread()


def test_store_lifecycle_fails_closed(tmp_path):
    path = tmp_path / "anchor.sqlite3"
    with pytest.raises(StoreNotProvisioned):
        DurableAnchorCAS.open(path)

    root = Ed25519PrivateKey.generate()
    store = _provision(path, root)
    with pytest.raises(FileExistsError):
        _provision(path, root)
    with pytest.raises(UnsupportedLegacyWriter):
        store.advance(_signed(root, 1), bootstrap=True)


def test_cas_requires_explicit_expected_checkpoint(tmp_path):
    root = Ed25519PrivateKey.generate()
    path = tmp_path / "anchor.sqlite3"
    store = _provision(path, root)
    with pytest.raises(TypeError, match="explicit expected"):
        store.compare_and_swap("request-r", None, _signed(root, 1))
    assert store.request_count() == 0


def test_profile_and_authority_are_bound_into_request_digest(tmp_path):
    root = Ed25519PrivateKey.generate()
    path_a, path_b = tmp_path / "a.sqlite3", tmp_path / "b.sqlite3"
    a, _ = _bootstrapped(path_a, root)
    b = _provision(path_b, root, authority="anchor-b")
    assert b.bootstrap("bootstrap-1", _signed(root, 1)).result == (
        DurableCASResult.APPLIED
    )
    expected, successor = a.get(), _signed(root, 2)
    assert a.cas_request_digest(expected, successor) != (
        b.cas_request_digest(b.get(), successor)
    )


def test_rejected_bootstrap_is_durable_and_does_not_block_valid_bootstrap(tmp_path):
    root = Ed25519PrivateKey.generate()
    path = tmp_path / "anchor.sqlite3"
    store = _provision(path, root)
    valid = _signed(root, 1)
    invalid = SignedTrustState(
        valid.state,
        bytes([valid.signature[0] ^ 1]) + valid.signature[1:],
        valid.signer_id,
        valid.issuer_keys,
    )
    rejected = store.bootstrap("bootstrap-rejected", invalid)
    assert rejected.result == DurableCASResult.REJECTED
    assert rejected.result_checkpoint is None
    replay = DurableAnchorCAS.open(path).bootstrap("bootstrap-rejected", invalid)
    assert replay.result == DurableCASResult.REJECTED
    assert replay.replayed
    accepted = store.bootstrap("bootstrap-valid", valid)
    assert accepted.result == DurableCASResult.APPLIED
    assert DurableAnchorCAS.open(path).request_count() == 2


def test_not_initialized_outcome_remains_immutable_after_bootstrap(tmp_path):
    root = Ed25519PrivateKey.generate()
    path = tmp_path / "anchor.sqlite3"
    store = _provision(path, root)
    expected = TrustCheckpoint(1, "0" * 64)
    successor = _signed(root, 2)
    result = store.compare_and_swap("request-early", expected, successor)
    assert result.result == DurableCASResult.NOT_INITIALIZED
    assert store.bootstrap("bootstrap-valid", _signed(root, 1)).result == (
        DurableCASResult.APPLIED
    )
    replay = DurableAnchorCAS.open(path).compare_and_swap(
        "request-early", expected, successor,
    )
    assert replay.result == DurableCASResult.NOT_INITIALIZED
    assert replay.result_checkpoint is None
    assert replay.current_checkpoint.revision == 1


def test_unchanged_outcome_survives_reopen(tmp_path):
    root = Ed25519PrivateKey.generate()
    path = tmp_path / "anchor.sqlite3"
    store, first = _bootstrapped(path, root)
    expected = store.get()
    result = store.compare_and_swap("request-unchanged", expected, first)
    assert result.result == DurableCASResult.UNCHANGED
    assert result.result_checkpoint == expected
    replay = DurableAnchorCAS.open(path).compare_and_swap(
        "request-unchanged", expected, first,
    )
    assert replay.result == DurableCASResult.UNCHANGED
    assert replay.replayed
    assert replay.sequence == result.sequence


@pytest.mark.parametrize(
    ("point", "recorded", "revision"),
    [
        ("before_transaction", False, 1),
        ("after_begin", False, 1),
        ("after_request_lookup", False, 1),
        ("after_checkpoint_update", False, 1),
        ("after_outcome_insert", False, 1),
        ("before_commit", False, 1),
        ("after_commit_before_reply", True, 2),
    ],
)
def test_real_process_exit_recovers_checkpoint_and_outcome_atomically(
    tmp_path, point, recorded, revision,
):
    root = Ed25519PrivateKey.generate()
    path = tmp_path / "anchor.sqlite3"
    store, _ = _bootstrapped(path, root)
    expected, successor = store.get(), _signed(root, 2)
    digest = store.cas_request_digest(expected, successor)
    context = mp.get_context("spawn")
    worker = context.Process(
        target=_crash_worker,
        args=(str(path), "request-crash", expected, successor, point),
    )
    worker.start()
    worker.join(timeout=30)
    try:
        assert worker.exitcode == 73
    finally:
        if worker.is_alive():
            worker.kill()
            worker.join(timeout=10)
        worker.close()

    queue = context.Queue()
    observer = context.Process(
        target=_observe_worker,
        args=(str(path), "request-crash", digest, queue),
    )
    observer.start()
    observer.join(timeout=30)
    try:
        assert observer.exitcode == 0
        status, result, current_revision, request_count = queue.get(timeout=5)
        assert (status == LookupStatus.RECORDED.value) is recorded
        assert result == (DurableCASResult.APPLIED.value if recorded else None)
        assert current_revision == revision
        assert request_count == (2 if recorded else 1)
    finally:
        if observer.is_alive():
            observer.kill()
            observer.join(timeout=10)
        observer.close()
        queue.close()
        queue.join_thread()


@pytest.mark.parametrize(
    ("point", "recorded"),
    [("before_commit", False), ("after_commit_before_reply", True)],
)
def test_exception_boundary_is_resolved_by_lookup_and_exact_retry(
    tmp_path, point, recorded,
):
    class RaiseAtPoint(DurableAnchorCAS):
        def _fault(self, current):
            if current == point:
                raise OSError(f"injected failure at {point}")

    root = Ed25519PrivateKey.generate()
    path = tmp_path / "anchor.sqlite3"
    store, _ = _bootstrapped(path, root)
    expected, successor = store.get(), _signed(root, 2)
    digest = store.cas_request_digest(expected, successor)

    with pytest.raises(OSError, match="injected failure"):
        RaiseAtPoint.open(path).compare_and_swap(
            "request-unknown", expected, successor,
        )

    observation = DurableAnchorCAS.open(path).lookup("request-unknown", digest)
    assert (observation.status == LookupStatus.RECORDED) is recorded
    retry = DurableAnchorCAS.open(path).compare_and_swap(
        "request-unknown", expected, successor,
    )
    assert retry.result == DurableCASResult.APPLIED
    assert retry.replayed is recorded
    assert DurableAnchorCAS.open(path).request_count() == 2


def test_lookup_can_report_not_recorded_while_live_writer_has_not_committed(tmp_path):
    root = Ed25519PrivateKey.generate()
    path = tmp_path / "anchor.sqlite3"
    store, _ = _bootstrapped(path, root)
    expected, successor = store.get(), _signed(root, 2)
    digest = store.cas_request_digest(expected, successor)
    context = mp.get_context("spawn")
    entered, release, queue = context.Event(), context.Event(), context.Queue()
    worker = context.Process(
        target=_blocked_writer,
        args=(str(path), "request-live", expected, successor,
              entered, release, queue),
    )
    worker.start()
    try:
        assert entered.wait(timeout=20)
        observation = DurableAnchorCAS.open(path).lookup("request-live", digest)
        assert observation.status == LookupStatus.NOT_RECORDED
        assert observation.current_checkpoint == expected
        release.set()
        worker.join(timeout=30)
        assert worker.exitcode == 0
        assert queue.get(timeout=5) == DurableCASResult.APPLIED.value
        final = DurableAnchorCAS.open(path).lookup("request-live", digest)
        assert final.status == LookupStatus.RECORDED
        assert final.response.result == DurableCASResult.APPLIED
    finally:
        release.set()
        if worker.is_alive():
            worker.kill()
            worker.join(timeout=10)
        worker.close()
        queue.close()
        queue.join_thread()


def test_writer_lock_error_never_returns_false_terminal_success(tmp_path):
    root = Ed25519PrivateKey.generate()
    path = tmp_path / "anchor.sqlite3"
    store, _ = _bootstrapped(path, root)
    expected, successor = store.get(), _signed(root, 2)
    digest = store.cas_request_digest(expected, successor)
    lock = sqlite3.connect(path, isolation_level=None)
    lock.execute("PRAGMA journal_mode=WAL")
    lock.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            store.compare_and_swap("request-locked", expected, successor)
    finally:
        lock.rollback()
        lock.close()
    observation = DurableAnchorCAS.open(path).lookup("request-locked", digest)
    assert observation.status == LookupStatus.NOT_RECORDED
    retry = DurableAnchorCAS.open(path).compare_and_swap(
        "request-locked", expected, successor,
    )
    assert retry.result == DurableCASResult.APPLIED


def test_history_survives_wal_checkpoint_and_reopen(tmp_path):
    root = Ed25519PrivateKey.generate()
    path = tmp_path / "anchor.sqlite3"
    store, _ = _bootstrapped(path, root)
    expected, successor = store.get(), _signed(root, 2)
    original = store.compare_and_swap("request-r", expected, successor)
    digest = store.cas_request_digest(expected, successor)
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    finally:
        connection.close()
    recovered = DurableAnchorCAS.open(path).lookup("request-r", digest)
    assert recovered.status == LookupStatus.RECORDED
    assert recovered.response.sequence == original.sequence
    assert recovered.response.result_checkpoint == original.result_checkpoint
    assert recovered.response.request_digest == original.request_digest


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE authority SET schema_version = 999 WHERE singleton = 1",
        "UPDATE authority SET profile_digest = '0' || substr(profile_digest, 2) "
        "WHERE singleton = 1",
        "UPDATE requests SET request_digest = 'f' || substr(request_digest, 2) "
        "WHERE request_id = 'bootstrap-1'",
        "DELETE FROM requests WHERE request_id = 'bootstrap-1'",
    ],
)
def test_authority_and_request_rows_reject_update_and_delete(tmp_path, statement):
    root = Ed25519PrivateKey.generate()
    path = tmp_path / "anchor.sqlite3"
    _bootstrapped(path, root)
    connection = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute(statement)
    finally:
        connection.rollback()
        connection.close()
    assert DurableAnchorCAS.open(path).get().revision == 1


def test_missing_immutability_guard_fails_closed(tmp_path):
    root = Ed25519PrivateKey.generate()
    path = tmp_path / "anchor.sqlite3"
    _bootstrapped(path, root)
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TRIGGER requests_no_delete")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(StoreIntegrityError, match="guards are missing"):
        DurableAnchorCAS.open(path)


def test_missing_state_changing_history_fails_closed(tmp_path):
    root = Ed25519PrivateKey.generate()
    path = tmp_path / "anchor.sqlite3"
    _bootstrapped(path, root)
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TRIGGER requests_no_delete")
        connection.execute(
            "DELETE FROM requests WHERE request_id = 'bootstrap-1'"
        )
        connection.execute("""
            CREATE TRIGGER requests_no_delete
            BEFORE DELETE ON requests
            BEGIN SELECT RAISE(ABORT, 'request outcomes are immutable'); END
        """)
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(StoreIntegrityError, match="not justified"):
        DurableAnchorCAS.open(path)


def test_open_handle_rejects_database_replaced_with_another_authority(tmp_path):
    root = Ed25519PrivateKey.generate()
    path_a, path_b = tmp_path / "a.sqlite3", tmp_path / "b.sqlite3"
    store_a, _ = _bootstrapped(path_a, root)
    store_b = _provision(path_b, root, authority="anchor-b")
    assert store_b.bootstrap("bootstrap-1", _signed(root, 1)).result == (
        DurableCASResult.APPLIED
    )
    connection = sqlite3.connect(path_b, isolation_level=None)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    path_a.unlink()
    shutil.copyfile(path_b, path_a)
    with pytest.raises(StoreIntegrityError, match="identity changed"):
        store_a.get()
