"""JW-02: only native signed settlement may mutate its I8 reservation.

Raw SQL below deliberately injects storage faults. It does not claim to prevent
a privileged SQLite principal from changing tables, triggers, or the database.
The providers and signing keys are synthetic laboratory fixtures only.
"""
from copy import deepcopy
from decimal import Decimal
import multiprocessing
import os
import queue
import sqlite3
import traceback

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tenir_governance.operations.provider import MockProvider
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from w2_integration.native_dispatch_adapter import (
    Ed25519ConnectorAttestor, NativeClaimedDispatchAdapter,
)
from w2_integration.native_positive_permit import NativePositivePermitAuthority


OPERATION = "lab:jw02"
IDEMPOTENCY = "lab:jw02-idem"
RESOURCE = "lab:budget"
OWNER_STATUS = "POSITIVE_AUTHORITY_OWNS_RESERVATION"


def _command():
    return {
        "schema": "tenir-dispatch-command/v1",
        "resource_id": RESOURCE,
        "amount": 7,
        "action_type": "payment",
        "agent_id": "lab-agent",
        "metadata": {"recipient": "recipient-1", "currency": "EUR"},
    }


def _attestor():
    return Ed25519ConnectorAttestor(
        Ed25519PrivateKey.from_private_bytes(bytes(range(96, 128)))
    )


def _system(tmp_path, monkeypatch):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())
    backend = SQLiteConsensusBackend(str(tmp_path))
    backend.register_resource(RESOURCE, 50)
    backend.register_resource("lab:other-budget", 50)
    attestor = _attestor()
    authority = NativePositivePermitAuthority(
        backend, trusted_provider_public_key=attestor.public_key_bytes,
    )
    provider = MockProvider()
    command = _command()
    issued = authority.admit_reserve_issue(
        OPERATION, "eval-jw02", command, resource_id=RESOURCE,
    )
    adapter = NativeClaimedDispatchAdapter(authority, provider, attestor)
    return backend, authority, provider, attestor, adapter, command, issued


def _snapshot(backend):
    with backend._connection() as db:
        return {
            table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in (
                "resources", "reservations", "compensations",
                "w2_positive_v1_capacity", "w2_positive_v1_permits",
                "w2_positive_v1_outbox", "w2_positive_v1_events",
            )
        }


def _reservation(backend, reservation_id):
    with backend._connection() as db:
        row = db.execute(
            "SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,),
        ).fetchone()
        return dict(row) if row else None


def _mutate(backend, method, reservation_id):
    return (backend.reconcile(OPERATION) if method.startswith("reconcile")
            else getattr(backend, method)(reservation_id))


def _inject_fault(backend, fault):
    # A separate privileged connection injects corrupt durable state; this is
    # explicitly outside the public API's protection boundary.
    statements = {
        "released": "UPDATE reservations SET state='RELEASED' WHERE idempotency_key=?",
        "amount": "UPDATE reservations SET amount='8' WHERE idempotency_key=?",
        "resource": "UPDATE reservations SET resource_id='lab:other-budget' WHERE idempotency_key=?",
        "key": "UPDATE reservations SET idempotency_key='lab:substituted' WHERE idempotency_key=?",
        "missing": "DELETE FROM reservations WHERE idempotency_key=?",
        "aggregate": "UPDATE resources SET reserved='6' WHERE resource_id=?",
        "shadow": "UPDATE w2_positive_v1_capacity SET amount='8' WHERE operation_key=?",
    }
    with sqlite3.connect(backend._database_path) as db:
        db.execute(statements[fault], (RESOURCE if fault == "aggregate" else OPERATION,))


@pytest.mark.parametrize("terminal", [False, True], ids=["reserved", "committed"])
@pytest.mark.parametrize("method", [
    "commit", "release", "refund", "reconcile-confirmed", "reconcile-failed",
    "reconcile-unresolved",
])
def test_reopened_public_backend_cannot_mutate_native_owned_reservation(
    tmp_path, monkeypatch, terminal, method,
):
    backend, _, _, _, adapter, command, issued = _system(tmp_path, monkeypatch)
    if terminal:
        assert adapter.dispatch(OPERATION, IDEMPOTENCY, command)["status"] == "CONFIRMED"
    callback_calls = []

    def reconcile_callback(*args, **kwargs):
        callback_calls.append((args, kwargs))
        return method == "reconcile-confirmed"

    reopened = SQLiteConsensusBackend(
        str(tmp_path),
        provider_reconcile=None if method == "reconcile-unresolved" else reconcile_callback,
    )
    before = _snapshot(backend)
    result = _mutate(reopened, method, issued["reservation_id"])
    assert result.status == OWNER_STATUS
    assert callback_calls == [], "owned reconciliation must refuse before external callback"
    assert _snapshot(backend) == before
    assert _reservation(backend, issued["reservation_id"])["state"] == (
        "COMMITTED" if terminal else "RESERVED"
    )


def test_ordinary_backend_reservations_keep_commit_release_refund_and_reconcile(tmp_path):
    backend = SQLiteConsensusBackend(str(tmp_path), provider_reconcile=lambda *_: True)
    backend.register_resource(RESOURCE, 50)
    first = backend.reserve("ordinary-commit", 7, RESOURCE)
    assert backend.commit(first.reservation_id).status == "COMMITTED"
    assert backend.refund(first.reservation_id).status == "REFUNDED"
    second = backend.reserve("ordinary-release", 7, RESOURCE)
    assert backend.release(second.reservation_id).status == "RELEASED"
    third = backend.reserve("ordinary-reconcile", 7, RESOURCE)
    assert backend.reconcile("ordinary-reconcile").status == "CONFIRMED"
    assert _reservation(backend, third.reservation_id)["state"] == "COMMITTED"
    failed = SQLiteConsensusBackend(str(tmp_path), provider_reconcile=lambda *_: False)
    fourth = failed.reserve("ordinary-failed", 7, RESOURCE)
    assert failed.reconcile("ordinary-failed").status == "FAILED"
    assert _reservation(backend, fourth.reservation_id)["state"] == "RELEASED"
    unknown = SQLiteConsensusBackend(str(tmp_path))
    fifth = unknown.reserve("ordinary-unknown", 7, RESOURCE)
    assert unknown.reconcile("ordinary-unknown").status == "UNRESOLVED"
    assert _reservation(backend, fifth.reservation_id)["state"] == "UNKNOWN"
    state = backend.get_state(RESOURCE)
    assert (state.reserved, state.committed, state.available) == (
        Decimal("7"), Decimal("7"), Decimal("36"),
    )


@pytest.mark.parametrize("fault", [
    "released", "amount", "resource", "key", "missing", "aggregate", "shadow",
])
def test_first_claim_revalidates_i8_and_shadow_before_consuming_nonce(
    tmp_path, monkeypatch, fault,
):
    backend, authority, provider, _, _, command, _ = _system(tmp_path, monkeypatch)
    _inject_fault(backend, fault)
    before = _snapshot(backend)
    with pytest.raises(RuntimeError, match="(?i)reservation|I8|budget"):
        authority.consume_and_claim(OPERATION, command, IDEMPOTENCY)
    assert provider.calls == []
    assert _snapshot(backend) == before
    with backend._connection() as db:
        assert db.execute("SELECT consumed FROM w2_positive_v1_permits").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_outbox").fetchone()[0] == 0


def test_pending_replay_revalidates_missing_i8_reservation(tmp_path, monkeypatch):
    backend, authority, _, _, _, command, _ = _system(tmp_path, monkeypatch)
    authority.consume_and_claim(OPERATION, command, IDEMPOTENCY)
    _inject_fault(backend, "missing")
    before = _snapshot(backend)
    with pytest.raises(RuntimeError, match="(?i)reservation|I8|budget"):
        authority.consume_and_claim(OPERATION, deepcopy(command), IDEMPOTENCY)
    assert _snapshot(backend) == before


def test_load_payload_revalidates_i8_after_claim_before_provider(tmp_path, monkeypatch):
    backend, authority, provider, _, adapter, command, _ = _system(tmp_path, monkeypatch)
    consume = authority.consume_and_claim

    def corrupt_after_claim(*args):
        claim = consume(*args)
        _inject_fault(backend, "amount")
        return claim

    monkeypatch.setattr(authority, "consume_and_claim", corrupt_after_claim)
    with pytest.raises(RuntimeError, match="(?i)reservation|I8|budget"):
        adapter.dispatch(OPERATION, IDEMPOTENCY, command)
    assert provider.calls == [], "invalid I8 must be rejected before execute()"
    assert provider.reconcile_calls == []


@pytest.mark.parametrize("fault", ["released", "aggregate"])
def test_claim_verification_rejects_incoherent_i8(tmp_path, monkeypatch, fault):
    backend, authority, _, _, _, command, _ = _system(tmp_path, monkeypatch)
    claim = authority.consume_and_claim(OPERATION, command, IDEMPOTENCY)
    assert authority.verify_dispatch_claim(OPERATION, IDEMPOTENCY, command, claim)
    _inject_fault(backend, fault)
    before = _snapshot(backend)
    assert not authority.verify_dispatch_claim(OPERATION, IDEMPOTENCY, command, claim)
    assert _snapshot(backend) == before


@pytest.mark.parametrize("fault", ["amount", "aggregate"])
def test_valid_connector_signature_cannot_settle_incoherent_i8(
    tmp_path, monkeypatch, fault,
):
    backend, authority, provider, attestor, _, command, _ = _system(tmp_path, monkeypatch)
    claim = authority.consume_and_claim(OPERATION, command, IDEMPOTENCY)
    receipt = attestor.attest_confirmed(
        operation_key=OPERATION, idempotency_key=IDEMPOTENCY, payload=command,
        reconciliation=provider.reconcile(IDEMPOTENCY),
        binding=authority.receipt_context(claim),
    )
    _inject_fault(backend, fault)
    before = _snapshot(backend)
    with pytest.raises(RuntimeError, match="(?i)reservation|I8|budget"):
        authority.mark_dispatch_confirmed(claim, receipt)
    assert _snapshot(backend) == before
    assert _reservation(backend, "res-" + OPERATION)["state"] == "RESERVED"


def test_confirmed_terminal_replay_and_same_receipt_remain_legal_after_restart(
    tmp_path, monkeypatch,
):
    backend, _, provider, attestor, adapter, command, issued = _system(tmp_path, monkeypatch)
    first = adapter.dispatch(OPERATION, IDEMPOTENCY, command)
    assert first["status"] == "CONFIRMED"
    reopened = SQLiteConsensusBackend(str(tmp_path))
    authority = NativePositivePermitAuthority(
        reopened, trusted_provider_public_key=attestor.public_key_bytes,
    )
    replay_adapter = NativeClaimedDispatchAdapter(authority, provider, attestor)
    before = _snapshot(backend)
    replay = replay_adapter.dispatch(OPERATION, IDEMPOTENCY, deepcopy(command))
    assert replay["status"] == "CONFIRMED" and replay["replayed"]
    assert replay["claim"]["execution_id"] == first["claim"]["execution_id"]
    assert replay["receipt"] == first["receipt"]
    assert authority.mark_dispatch_confirmed(replay["claim"], replay["receipt"]) == "CONFIRMED"
    assert len(provider.calls) == 1
    assert _snapshot(backend) == before
    assert _reservation(backend, issued["reservation_id"])["state"] == "COMMITTED"
    state = backend.get_state(RESOURCE)
    assert (state.reserved, state.committed, state.available) == (
        Decimal("0"), Decimal("7"), Decimal("43"),
    )


def _set_worker_keys():
    os.environ["TENIR_W2_PRIVATE_KEY_HEX"] = bytes(range(32)).hex()
    os.environ["TENIR_WITNESS_PRIVATE_KEY_HEX"] = bytes(range(32, 64)).hex()


def _dispatch_worker(store, start, entered_provider, mutations_done, settled, results):
    """Top-level spawn target; the provider blocks at the vulnerable boundary."""
    try:
        _set_worker_keys()
        backend = SQLiteConsensusBackend(store)
        attestor = _attestor()
        authority = NativePositivePermitAuthority(
            backend, trusted_provider_public_key=attestor.public_key_bytes,
        )
        provider = MockProvider()
        execute = provider.execute

        def blocked_execute(**kwargs):
            entered_provider.set()
            if not mutations_done.wait(30):
                raise RuntimeError("mutator did not reach the provider boundary")
            return execute(**kwargs)

        provider.execute = blocked_execute
        adapter = NativeClaimedDispatchAdapter(authority, provider, attestor)
        results.put(("dispatch-ready",))
        if not start.wait(30):
            raise RuntimeError("start not signaled")
        outcome = adapter.dispatch(OPERATION, IDEMPOTENCY, _command())
        results.put(("dispatch", outcome["status"], len(provider.calls)))
    except BaseException:
        results.put(("error", "dispatch", traceback.format_exc()))
    finally:
        settled.set()


def _mutator_worker(store, method, start, entered_provider, mutations_done, settled, results):
    """An independent backend is never given the native authority object."""
    try:
        _set_worker_keys()
        callback_calls = []

        def callback(*args, **kwargs):
            callback_calls.append((args, kwargs))
            return False

        backend = SQLiteConsensusBackend(store, provider_reconcile=callback)
        results.put(("mutator-ready",))
        if not start.wait(30) or not entered_provider.wait(30):
            raise RuntimeError("dispatcher did not reach the provider boundary")
        during = [_mutate(backend, method, "res-" + OPERATION).status for _ in range(8)]
        mutations_done.set()
        if not settled.wait(30):
            raise RuntimeError("authorized settlement did not finish")
        after = [_mutate(backend, method, "res-" + OPERATION).status for _ in range(8)]
        results.put(("mutator", during, after, len(callback_calls)))
    except BaseException:
        results.put(("error", "mutator", traceback.format_exc()))
    finally:
        mutations_done.set()


@pytest.mark.parametrize("method", ["commit", "release", "refund", "reconcile-failed"])
def test_spawn_public_mutator_cannot_win_during_or_after_authorized_dispatch(
    tmp_path, monkeypatch, method,
):
    backend, _, _, _, _, _, issued = _system(tmp_path, monkeypatch)
    context = multiprocessing.get_context("spawn")
    start, entered, mutations_done, settled = [context.Event() for _ in range(4)]
    results = context.Queue()
    workers = [
        context.Process(target=_dispatch_worker, args=(
            str(tmp_path), start, entered, mutations_done, settled, results,
        )),
        context.Process(target=_mutator_worker, args=(
            str(tmp_path), method, start, entered, mutations_done, settled, results,
        )),
    ]
    messages = []
    try:
        for worker in workers:
            worker.start()
        messages.extend([results.get(timeout=45), results.get(timeout=45)])
        assert {message[0] for message in messages} == {"dispatch-ready", "mutator-ready"}, messages
        start.set()
        messages.extend([results.get(timeout=45), results.get(timeout=45)])
        for worker in workers:
            worker.join(10)
            assert worker.exitcode == 0, messages
        assert not [message for message in messages if message[0] == "error"], messages
        assert ("dispatch", "CONFIRMED", 1) in messages
        mutation = next(message for message in messages if message[0] == "mutator")
        assert mutation[1] == mutation[2] == [OWNER_STATUS] * 8
        assert mutation[3] == 0
    except queue.Empty:
        pytest.fail(f"spawned workers failed to report progress: {messages}")
    finally:
        start.set()
        mutations_done.set()
        settled.set()
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            worker.join(10)
        results.close()
        results.join_thread()
    state = backend.get_state(RESOURCE)
    assert (state.capacity, state.reserved, state.committed, state.available) == (
        Decimal("50"), Decimal("0"), Decimal("7"), Decimal("43"),
    )
    assert _reservation(backend, issued["reservation_id"])["state"] == "COMMITTED"
    with backend._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 1
        assert tuple(db.execute("SELECT reserved,settled FROM w2_positive_v1_capacity").fetchone()) == (0, 1)
        assert db.execute("SELECT status FROM w2_positive_v1_outbox").fetchone()[0] == "CONFIRMED"
