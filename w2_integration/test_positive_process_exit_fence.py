"""Single-host laboratory crash tests; no real external provider."""
import multiprocessing
import os
import signal

import pytest

from kernel_claim_provider import KernelClaimProofProvider
from native_positive_g4a import NativePositiveG4A
from native_positive_permit import NativePositivePermitAuthority
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend

OP, KEY, RESOURCE = "lab:fence", "lab:fence-key", "lab:budget"
COMMAND = {"schema": "tenir-dispatch-command/v1", "resource_id": RESOURCE,
           "amount": 7, "action_type": "payment", "agent_id": "lab:agent",
           "metadata": {"recipient": "lab:recipient"}}


def _crash_worker(path, after_effect, crash_method):
    backend = SQLiteConsensusBackend(path)
    authority = NativePositivePermitAuthority(backend)
    gate = NativePositiveG4A(authority)
    grant = gate.evaluate_and_grant(OP, "lab:eval", COMMAND, resource_id=RESOURCE)
    provider = KernelClaimProofProvider(db_path=os.path.join(path, "provider.db"),
                                       claim_verifier=authority.verify_dispatch_claim)
    original = provider.execute
    def die(*args, **kwargs):
        if after_effect:
            original(*args, **kwargs)
        if crash_method == "sigkill":
            os.kill(os.getpid(), signal.SIGKILL)
        os._exit(86)
    provider.execute = die
    gate.dispatch_to_b3(OP, KEY, COMMAND, grant, provider)
    os._exit(99)


def _die_before_send_commit(path):
    backend = SQLiteConsensusBackend(path)
    authority = NativePositivePermitAuthority(backend)
    gate = NativePositiveG4A(authority)
    grant = gate.evaluate_and_grant(OP, "lab:eval", COMMAND, resource_id=RESOURCE)
    provider = KernelClaimProofProvider(db_path=os.path.join(path, "provider.db"),
                                       claim_verifier=authority.verify_dispatch_claim)
    def crash(operation):
        if operation == "positive_g4a_consume":
            os._exit(87)
    backend._before_commit = crash
    gate.dispatch_to_b3(OP, KEY, COMMAND, grant, provider)
    os._exit(99)


def _race_dispatch(path, start, ready, results):
    try:
        authority = NativePositivePermitAuthority(SQLiteConsensusBackend(path))
        gate = NativePositiveG4A(authority)
        grant = gate.evaluate_and_grant(OP, "lab:eval", COMMAND, resource_id=RESOURCE)
        provider = KernelClaimProofProvider(db_path=os.path.join(path, "provider.db"),
                                           claim_verifier=authority.verify_dispatch_claim)
        ready.put(True)
        if not start.wait(30):
            raise RuntimeError("race start timed out")
        try:
            gate.dispatch_to_b3(OP, KEY, COMMAND, grant, provider)
            results.put("sent")
        except Exception as exc:
            results.put(type(exc).__name__)
        finally:
            provider.conn.close()
    except Exception as exc:
        results.put("setup:" + type(exc).__name__)


def test_concurrent_processes_grant_only_one_send(tmp_path, monkeypatch):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())
    monkeypatch.setenv("TENIR_B3_PRIVATE_KEY_HEX", bytes(range(64, 96)).hex())
    backend = SQLiteConsensusBackend(str(tmp_path))
    assert backend.register_resource(RESOURCE, 50) == "REGISTERED"
    authority = NativePositivePermitAuthority(backend)
    NativePositiveG4A(authority).evaluate_and_grant(OP, "lab:eval", COMMAND, resource_id=RESOURCE)
    initial = KernelClaimProofProvider(db_path=str(tmp_path / "provider.db"), claim_verifier=authority.verify_dispatch_claim)
    initial.conn.close()
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    ready = ctx.Queue()
    results = ctx.Queue()
    processes = [ctx.Process(target=_race_dispatch, args=(str(tmp_path), start, ready, results)) for _ in range(2)]
    for process in processes:
        process.start()
    try:
        assert [ready.get(timeout=40) for _ in processes] == [True, True]
        start.set()
        outcomes = [results.get(timeout=45) for _ in processes]
        for process in processes:
            process.join(20)
            assert process.exitcode == 0
        assert sorted(outcomes) == ["RuntimeError", "sent"]
        provider = KernelClaimProofProvider(db_path=str(tmp_path / "provider.db"), claim_verifier=authority.verify_dispatch_claim)
        try:
            assert provider.execute_count(OP) == 1
        finally:
            provider.conn.close()
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(10)
        results.close()
        ready.close()


def test_crash_before_send_intent_commit_rolls_back_and_exact_retry_sends_once(tmp_path, monkeypatch):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())
    monkeypatch.setenv("TENIR_B3_PRIVATE_KEY_HEX", bytes(range(64, 96)).hex())
    backend = SQLiteConsensusBackend(str(tmp_path))
    assert backend.register_resource(RESOURCE, 50) == "REGISTERED"
    NativePositiveG4A(NativePositivePermitAuthority(backend)).evaluate_and_grant(
        OP, "lab:eval", COMMAND, resource_id=RESOURCE)
    process = multiprocessing.get_context("spawn").Process(target=_die_before_send_commit, args=(str(tmp_path),))
    process.start()
    process.join(30)
    try:
        assert process.exitcode == 87
        authority = NativePositivePermitAuthority(SQLiteConsensusBackend(str(tmp_path)))
        gate = NativePositiveG4A(authority)
        with authority._tx() as db:
            assert db.execute("SELECT consumed FROM w2_positive_g4a_v1_grants WHERE operation_key=?", (OP,)).fetchone()[0] == 0
            assert db.execute("SELECT status FROM w2_positive_v1_outbox WHERE operation_key=?", (OP,)).fetchone()[0] == "PENDING"
            assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_events WHERE operation_key=? AND event='positive_send_intent'", (OP,)).fetchone()[0] == 0
        claim = authority.consume_and_claim(OP, COMMAND, KEY)
        assert not authority.verify_dispatch_claim(OP, KEY, COMMAND, claim)
        provider = KernelClaimProofProvider(db_path=str(tmp_path / "provider.db"),
                                           claim_verifier=authority.verify_dispatch_claim)
        try:
            assert provider.execute_count(OP) == 0
            grant = gate.evaluate_and_grant(OP, "lab:eval", COMMAND, resource_id=RESOURCE)
            assert gate.dispatch_to_b3(OP, KEY, COMMAND, grant, provider)["status"] == "EXECUTED"
            assert provider.execute_count(OP) == 1
        finally:
            provider.conn.close()
    finally:
        if process.is_alive():
            process.terminate()
        process.join(10)


@pytest.mark.parametrize("after_effect", [False, True], ids=["before-effect", "after-effect"])
@pytest.mark.parametrize("crash_method", ["exit", pytest.param("sigkill", marks=pytest.mark.skipif(os.name == "nt", reason="SIGKILL requires POSIX"))])
def test_process_exit_never_reopens_direct_send(tmp_path, monkeypatch, after_effect, crash_method):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())
    monkeypatch.setenv("TENIR_B3_PRIVATE_KEY_HEX", bytes(range(64, 96)).hex())
    backend = SQLiteConsensusBackend(str(tmp_path))
    assert backend.register_resource(RESOURCE, 50) == "REGISTERED"
    gate = NativePositiveG4A(NativePositivePermitAuthority(backend))
    gate.evaluate_and_grant(OP, "lab:eval", COMMAND, resource_id=RESOURCE)
    process = multiprocessing.get_context("spawn").Process(target=_crash_worker, args=(str(tmp_path), after_effect, crash_method))
    process.start()
    process.join(30)
    try:
        assert process.exitcode == (86 if crash_method == "exit" else -signal.SIGKILL)
        authority = NativePositivePermitAuthority(SQLiteConsensusBackend(str(tmp_path)))
        claim = authority.consume_and_claim(OP, COMMAND, KEY)
        assert claim["replayed"]
        provider = KernelClaimProofProvider(db_path=str(tmp_path / "provider.db"),
                                           claim_verifier=authority.verify_dispatch_claim)
        try:
            assert provider.execute_count(OP) == int(after_effect)
            assert not authority.verify_dispatch_claim(OP, KEY, COMMAND, claim)
            with pytest.raises(RuntimeError, match="claim"):
                provider.execute(OP, KEY, COMMAND, claim=claim)
            assert provider.execute_count(OP) == int(after_effect)
            with authority._tx() as db:
                row = db.execute("SELECT status FROM w2_positive_v1_outbox WHERE operation_key=?", (OP,)).fetchone()
                assert row["status"] == "UNKNOWN"
            assert authority.backend.get_state(RESOURCE).reserved == 7
        finally:
            provider.conn.close()
    finally:
        if process.is_alive():
            process.terminate()
        process.join(10)


def test_live_send_capability_is_one_use_and_never_returned(tmp_path, monkeypatch):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())
    monkeypatch.setenv("TENIR_B3_PRIVATE_KEY_HEX", bytes(range(64, 96)).hex())
    backend = SQLiteConsensusBackend(str(tmp_path))
    assert backend.register_resource(RESOURCE, 50) == "REGISTERED"
    authority = NativePositivePermitAuthority(backend)
    gate = NativePositiveG4A(authority)
    provider = KernelClaimProofProvider(db_path=str(tmp_path / "provider.db"),
                                       claim_verifier=authority.verify_dispatch_claim)
    captured = []
    original = provider.execute
    def observe(*args, **kwargs):
        captured.append(dict(kwargs["claim"]))
        receipt = original(*args, **kwargs)
        assert not authority.verify_dispatch_claim(OP, KEY, COMMAND, captured[0])
        return receipt
    provider.execute = observe
    try:
        grant = gate.evaluate_and_grant(OP, "lab:eval", COMMAND, resource_id=RESOURCE)
        result = gate.dispatch_to_b3(OP, KEY, COMMAND, grant, provider)
        assert result["status"] == "EXECUTED"
        assert "positive_send_capability" not in result["claim"]
        assert provider.execute_count(OP) == 1
        assert not authority.verify_dispatch_claim(OP, KEY, COMMAND, captured[0])
        with pytest.raises(RuntimeError, match="claim"):
            original(OP, KEY, COMMAND, claim=captured[0])
        assert provider.execute_count(OP) == 1
    finally:
        provider.conn.close()


def test_provider_with_foreign_verifier_fails_before_consuming_grant(tmp_path, monkeypatch):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())
    backend = SQLiteConsensusBackend(str(tmp_path))
    assert backend.register_resource(RESOURCE, 50) == "REGISTERED"
    authority = NativePositivePermitAuthority(backend)
    gate = NativePositiveG4A(authority)
    provider = KernelClaimProofProvider(claim_verifier=lambda *args: True)
    try:
        grant = gate.evaluate_and_grant(OP, "lab:eval", COMMAND, resource_id=RESOURCE)
        with pytest.raises(RuntimeError, match="native claim verifier"):
            gate.dispatch_to_b3(OP, KEY, COMMAND, grant, provider)
        with backend._connection() as db:
            assert db.execute("SELECT consumed FROM w2_positive_g4a_v1_grants WHERE operation_key=?", (OP,)).fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_outbox WHERE operation_key=?", (OP,)).fetchone()[0] == 0
        assert provider.execute_count(OP) == 0
    finally:
        provider.conn.close()


def test_review_after_committed_send_intent_is_recorded_and_blocks_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())
    backend = SQLiteConsensusBackend(str(tmp_path))
    assert backend.register_resource(RESOURCE, 50) == "REGISTERED"
    authority = NativePositivePermitAuthority(backend)
    gate = NativePositiveG4A(authority)
    provider = KernelClaimProofProvider(db_path=str(tmp_path / "provider.db"),
                                       claim_verifier=authority.verify_dispatch_claim)
    original = provider.execute
    def latch_then_send(*args, **kwargs):
        assert authority.latch_review_before_send(OP, "manual review") == "REVIEW_AFTER_GRANT"
        return original(*args, **kwargs)
    provider.execute = latch_then_send
    try:
        grant = gate.evaluate_and_grant(OP, "lab:eval", COMMAND, resource_id=RESOURCE)
        with pytest.raises(RuntimeError, match="claim"):
            gate.dispatch_to_b3(OP, KEY, COMMAND, grant, provider)
        assert authority.requires_manual_review(OP)
        assert provider.execute_count(OP) == 0
        assert backend.get_state(RESOURCE).reserved == 7
    finally:
        provider.conn.close()
