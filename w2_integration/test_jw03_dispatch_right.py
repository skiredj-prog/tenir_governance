"""JW-03: one durable send right, no second send after replay, crash, or review."""
import multiprocessing
import os
import queue
import traceback

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tenir_governance.operations.provider import MockProvider
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from w2_integration.native_dispatch_adapter import (
    Ed25519ConnectorAttestor, NativeClaimedDispatchAdapter,
)
from w2_integration.native_positive_permit import NativePositivePermitAuthority


OPERATION = "lab:jw03"
IDEMPOTENCY = "lab:jw03-idem"
RESOURCE = "lab:budget"


def _command():
    return {
        "schema": "tenir-dispatch-command/v1",
        "resource_id": RESOURCE,
        "amount": 7,
        "action_type": "payment",
        "agent_id": "lab-agent",
        "metadata": {"recipient": "recipient-1", "currency": "EUR"},
    }


def _keys():
    os.environ["TENIR_W2_PRIVATE_KEY_HEX"] = bytes(range(32)).hex()
    os.environ["TENIR_WITNESS_PRIVATE_KEY_HEX"] = bytes(range(32, 64)).hex()


def _system(tmp_path, monkeypatch):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())
    backend = SQLiteConsensusBackend(str(tmp_path))
    backend.register_resource(RESOURCE, 50)
    attestor = Ed25519ConnectorAttestor(
        Ed25519PrivateKey.from_private_bytes(bytes(range(96, 128)))
    )
    authority = NativePositivePermitAuthority(
        backend, trusted_provider_public_key=attestor.public_key_bytes,
    )
    command = _command()
    authority.admit_reserve_issue(OPERATION, "eval-jw03", command, resource_id=RESOURCE)
    provider = MockProvider()
    adapter = NativeClaimedDispatchAdapter(authority, provider, attestor)
    return backend, authority, provider, adapter, command


def _permit_consumed(backend):
    with backend._connection() as db:
        return db.execute(
            "SELECT consumed FROM w2_positive_v1_permits WHERE operation_key=?",
            (OPERATION,),
        ).fetchone()[0]


def test_review_before_grant_blocks_send_and_keeps_nonce(tmp_path, monkeypatch):
    backend, authority, provider, adapter, command = _system(tmp_path, monkeypatch)
    assert authority.latch_review_before_send(OPERATION, "operator hold") == "HOLD"
    result = adapter.dispatch(OPERATION, IDEMPOTENCY, command)
    assert result["status"] == "HOLD"
    assert provider.calls == []
    assert _permit_consumed(backend) == 0
    with backend._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_outbox").fetchone()[0] == 0


def test_review_after_claim_and_before_grant_does_not_send(tmp_path, monkeypatch):
    backend, authority, provider, adapter, command = _system(tmp_path, monkeypatch)
    claim = authority.consume_and_claim(OPERATION, command, IDEMPOTENCY)
    assert authority.latch_review_before_send(OPERATION, "hold before grant") == "HOLD"
    assert authority.grant_send_right(claim) == "REVIEW"
    result = adapter.dispatch(OPERATION, IDEMPOTENCY, command)
    assert result["status"] == "HOLD"
    assert provider.calls == []
    with backend._connection() as db:
        assert db.execute(
            "SELECT 1 FROM w2_positive_v1_events WHERE event='dispatch_send_granted'"
        ).fetchone() is None


def test_crash_after_grant_does_not_send_again(tmp_path, monkeypatch):
    backend, authority, provider, adapter, command = _system(tmp_path, monkeypatch)
    claim = authority.consume_and_claim(OPERATION, command, IDEMPOTENCY)
    assert authority.grant_send_right(claim) == "GRANTED"
    assert authority.grant_send_right(claim) == "ALREADY"
    replay = adapter.dispatch(OPERATION, IDEMPOTENCY, command)
    assert replay["status"] == "UNKNOWN"
    assert replay["replayed"] is True
    assert provider.calls == []
    assert provider.reconcile_calls == []
    with backend._connection() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM w2_positive_v1_events WHERE event='dispatch_send_granted'"
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT status FROM w2_positive_v1_outbox WHERE operation_key=?",
            (OPERATION,),
        ).fetchone()[0] == "UNKNOWN"


def test_granted_dispatch_still_sends_once_and_replays_without_a_second_send(tmp_path, monkeypatch):
    _, authority, provider, adapter, command = _system(tmp_path, monkeypatch)
    first = adapter.dispatch(OPERATION, IDEMPOTENCY, command)
    second = adapter.dispatch(OPERATION, IDEMPOTENCY, command)
    assert first["status"] == second["status"] == "CONFIRMED"
    assert len(provider.calls) == 1
    assert authority.grant_send_right(first["claim"]) == "ALREADY"


def _first_sender(store, entered, other_done, results):
    try:
        _keys()
        backend = SQLiteConsensusBackend(store)
        attestor = Ed25519ConnectorAttestor(
            Ed25519PrivateKey.from_private_bytes(bytes(range(96, 128)))
        )
        authority = NativePositivePermitAuthority(
            backend, trusted_provider_public_key=attestor.public_key_bytes,
        )
        provider = MockProvider()
        execute = provider.execute

        def blocked_execute(**kwargs):
            entered.set()
            if not other_done.wait(30):
                raise RuntimeError("second dispatcher did not finish")
            return execute(**kwargs)

        provider.execute = blocked_execute
        adapter = NativeClaimedDispatchAdapter(authority, provider, attestor)
        outcome = adapter.dispatch(OPERATION, IDEMPOTENCY, _command())
        results.put(("first", outcome["status"], len(provider.calls)))
    except BaseException:
        results.put(("error", "first", traceback.format_exc()))
    finally:
        entered.set()
        other_done.set()


def _second_sender(store, entered, other_done, results):
    try:
        _keys()
        if not entered.wait(30):
            raise RuntimeError("first sender did not reach the provider")
        backend = SQLiteConsensusBackend(store)
        attestor = Ed25519ConnectorAttestor(
            Ed25519PrivateKey.from_private_bytes(bytes(range(96, 128)))
        )
        authority = NativePositivePermitAuthority(
            backend, trusted_provider_public_key=attestor.public_key_bytes,
        )
        provider = MockProvider()
        adapter = NativeClaimedDispatchAdapter(authority, provider, attestor)
        outcome = adapter.dispatch(OPERATION, IDEMPOTENCY, _command())
        results.put(("second", outcome["status"], len(provider.calls)))
    except BaseException:
        results.put(("error", "second", traceback.format_exc()))
    finally:
        other_done.set()


def test_two_processes_obtain_only_one_send_right(tmp_path, monkeypatch):
    _system(tmp_path, monkeypatch)
    context = multiprocessing.get_context("spawn")
    entered, other_done = context.Event(), context.Event()
    results = context.Queue()
    workers = [
        context.Process(target=_first_sender, args=(str(tmp_path), entered, other_done, results)),
        context.Process(target=_second_sender, args=(str(tmp_path), entered, other_done, results)),
    ]
    messages = []
    try:
        for worker in workers:
            worker.start()
        messages.extend([results.get(timeout=45), results.get(timeout=45)])
        for worker in workers:
            worker.join(10)
            assert worker.exitcode == 0, messages
    except queue.Empty:
        pytest.fail(f"spawned dispatchers failed to report: {messages}")
    finally:
        entered.set()
        other_done.set()
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            worker.join(10)
        results.close()
        results.join_thread()
    assert not [message for message in messages if message[0] == "error"], messages
    assert ("first", "CONFIRMED", 1) in messages
    assert ("second", "UNKNOWN", 0) in messages
    backend = SQLiteConsensusBackend(str(tmp_path))
    with backend._connection() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM w2_positive_v1_events WHERE event='dispatch_send_granted'"
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT status FROM w2_positive_v1_outbox WHERE operation_key=?",
            (OPERATION,),
        ).fetchone()[0] == "CONFIRMED"
