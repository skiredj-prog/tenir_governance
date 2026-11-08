"""Negative controls for the generic synthetic provider; no external effect."""
import multiprocessing
import os
import signal

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from kernel_claim_provider import KernelClaimProofProvider
from native_positive_permit import NativePositivePermitAuthority
from proof_provider import ProofProvider
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend

OP, KEY = "lab:generic-b1", "lab:generic-idem"
COMMAND = {"schema": "tenir-dispatch-command/v1", "resource_id": "budget", "amount": 7,
           "action_type": "payment", "agent_id": "agent", "metadata": {"recipient": "target"}}


def _public_key():
    return Ed25519PrivateKey.from_private_bytes(bytes(range(64, 96))).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _crash_provider(path, after_effect, crash_method):
    authority = NativePositivePermitAuthority(SQLiteConsensusBackend(path), trusted_provider_public_key=_public_key())
    provider = KernelClaimProofProvider(db_path=path + "/provider.db", claim_verifier=authority.verify_dispatch_claim)
    original = provider.execute
    def die(*args, **kwargs):
        if after_effect:
            original(*args, **kwargs)
        if crash_method == "sigkill":
            os.kill(os.getpid(), signal.SIGKILL)
        os._exit(86)
    provider.execute = die
    provider.dispatch_authorized(authority, OP, KEY, COMMAND)
    os._exit(99)


def _race_provider(path, first, entered, other_done, results):
    try:
        if not first and not entered.wait(30):
            raise RuntimeError("first provider did not enter")
        authority = NativePositivePermitAuthority(SQLiteConsensusBackend(path), trusted_provider_public_key=_public_key())
        provider = KernelClaimProofProvider(db_path=path + "/provider.db", claim_verifier=authority.verify_dispatch_claim)
        try:
            if first:
                original = provider.execute
                def wait_before_effect(*args, **kwargs):
                    entered.set()
                    if not other_done.wait(30):
                        raise RuntimeError("second provider did not finish")
                    return original(*args, **kwargs)
                provider.execute = wait_before_effect
            result = provider.dispatch_authorized(authority, OP, KEY, COMMAND)
            results.put(("first" if first else "second", result["status"]))
        finally:
            provider.conn.close()
    except Exception as exc:
        results.put(("error", type(exc).__name__ + ":" + str(exc)))
    finally:
        if not first:
            other_done.set()


def test_two_processes_receive_one_generic_send_right(tmp_path, monkeypatch):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())
    monkeypatch.setenv("TENIR_B3_PRIVATE_KEY_HEX", bytes(range(64, 96)).hex())
    backend = SQLiteConsensusBackend(str(tmp_path))
    assert backend.register_resource("budget", 50) == "REGISTERED"
    authority = NativePositivePermitAuthority(backend, trusted_provider_public_key=_public_key())
    authority.admit_reserve_issue(OP, "eval", COMMAND, resource_id="budget")
    provider = KernelClaimProofProvider(db_path=str(tmp_path / "provider.db"), claim_verifier=authority.verify_dispatch_claim)
    provider.conn.close()
    ctx = multiprocessing.get_context("spawn")
    entered, other_done, results = ctx.Event(), ctx.Event(), ctx.Queue()
    workers = [ctx.Process(target=_race_provider, args=(str(tmp_path), first, entered, other_done, results))
               for first in (True, False)]
    for worker in workers:
        worker.start()
    try:
        outcomes = [results.get(timeout=45), results.get(timeout=45)]
        for worker in workers:
            worker.join(10)
            assert worker.exitcode == 0
        assert ("first", "CONFIRMED") in outcomes
        assert ("second", "UNKNOWN") in outcomes
        provider = KernelClaimProofProvider(db_path=str(tmp_path / "provider.db"), claim_verifier=authority.verify_dispatch_claim)
        try:
            assert provider.execute_count(OP) == 1
        finally:
            provider.conn.close()
        with backend._connection() as db:
            assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_events WHERE operation_key=? AND event='dispatch_send_granted'", (OP,)).fetchone()[0] == 1
    finally:
        entered.set()
        other_done.set()
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            worker.join(10)
        results.close()


def test_native_claim_alone_cannot_call_provider_without_send_right(tmp_path, monkeypatch):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())
    backend = SQLiteConsensusBackend(str(tmp_path))
    assert backend.register_resource("budget", 50) == "REGISTERED"
    authority = NativePositivePermitAuthority(backend)
    authority.admit_reserve_issue(OP, "eval", COMMAND, resource_id="budget")
    claim = authority.consume_and_claim(OP, COMMAND, KEY)
    provider = KernelClaimProofProvider(claim_verifier=authority.verify_dispatch_claim)
    try:
        with pytest.raises(RuntimeError, match="claim"):
            provider.execute(OP, KEY, COMMAND, claim=claim)
        assert provider.execute_count(OP) == 0
        assert backend.get_state("budget").reserved == 7
    finally:
        provider.conn.close()


def test_raw_provider_rejects_forged_native_bound_marker():
    def forged(*args):
        return True
    forged._native_bound = True
    provider = ProofProvider(claim_verifier=forged)
    try:
        with pytest.raises(RuntimeError, match="claim verifier"):
            provider.execute(OP, KEY, COMMAND, claim={"nonce": "forged"})
        assert provider.execute_count(OP) == 0
    finally:
        provider.conn.close()


@pytest.mark.parametrize("after_effect", [False, True], ids=["before-effect", "after-effect"])
@pytest.mark.parametrize("crash_method", ["exit", pytest.param("sigkill", marks=pytest.mark.skipif(os.name == "nt", reason="SIGKILL requires POSIX"))])
def test_crashed_generic_send_never_reissues_effect(tmp_path, monkeypatch, after_effect, crash_method):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())
    monkeypatch.setenv("TENIR_B3_PRIVATE_KEY_HEX", bytes(range(64, 96)).hex())
    backend = SQLiteConsensusBackend(str(tmp_path))
    assert backend.register_resource("budget", 50) == "REGISTERED"
    authority = NativePositivePermitAuthority(backend, trusted_provider_public_key=_public_key())
    authority.admit_reserve_issue(OP, "eval", COMMAND, resource_id="budget")
    process = multiprocessing.get_context("spawn").Process(target=_crash_provider, args=(str(tmp_path), after_effect, crash_method))
    process.start()
    process.join(30)
    try:
        assert process.exitcode == (86 if crash_method == "exit" else -signal.SIGKILL)
        reopened = NativePositivePermitAuthority(SQLiteConsensusBackend(str(tmp_path)), trusted_provider_public_key=_public_key())
        claim = reopened.consume_and_claim(OP, COMMAND, KEY)
        provider = KernelClaimProofProvider(db_path=str(tmp_path / "provider.db"), claim_verifier=reopened.verify_dispatch_claim)
        try:
            assert provider.execute_count(OP) == int(after_effect)
            with pytest.raises(RuntimeError, match="claim"):
                provider.execute(OP, KEY, COMMAND, claim=claim)
            assert provider.dispatch_authorized(reopened, OP, KEY, COMMAND)["status"] == "UNKNOWN"
            replay = provider.dispatch_authorized(reopened, OP, KEY, COMMAND)
            assert replay["status"] == ("CONFIRMED" if after_effect else "UNKNOWN")
            assert provider.execute_count(OP) == int(after_effect)
            assert backend.get_state("budget").reserved == (0 if after_effect else 7)
            with backend._connection() as db:
                assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_events WHERE operation_key=? AND event='dispatch_send_granted'", (OP,)).fetchone()[0] == 1
        finally:
            provider.conn.close()
    finally:
        if process.is_alive():
            process.terminate()
        process.join(10)


def test_generic_ambiguous_no_receipt_never_retries_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())
    monkeypatch.setenv("TENIR_B3_PRIVATE_KEY_HEX", bytes(range(64, 96)).hex())
    backend = SQLiteConsensusBackend(str(tmp_path))
    assert backend.register_resource("budget", 50) == "REGISTERED"
    authority = NativePositivePermitAuthority(backend, trusted_provider_public_key=_public_key())
    authority.admit_reserve_issue(OP, "eval", COMMAND, resource_id="budget")
    provider = KernelClaimProofProvider(claim_verifier=authority.verify_dispatch_claim)
    def fail(*args, **kwargs):
        raise RuntimeError("injected provider outage")
    monkeypatch.setattr(provider, "execute", fail)
    try:
        with pytest.raises(RuntimeError, match="outage"):
            provider.dispatch_authorized(authority, OP, KEY, COMMAND)
        result = provider.dispatch_authorized(authority, OP, KEY, COMMAND)
        assert result["status"] == "UNKNOWN"
        assert provider.execute_count(OP) == 0
        assert backend.get_state("budget").reserved == 7
    finally:
        provider.conn.close()


def test_generic_live_send_capability_is_single_use(tmp_path, monkeypatch):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())
    monkeypatch.setenv("TENIR_B3_PRIVATE_KEY_HEX", bytes(range(64, 96)).hex())
    backend = SQLiteConsensusBackend(str(tmp_path))
    assert backend.register_resource("budget", 50) == "REGISTERED"
    authority = NativePositivePermitAuthority(backend, trusted_provider_public_key=_public_key())
    authority.admit_reserve_issue(OP, "eval", COMMAND, resource_id="budget")
    provider = KernelClaimProofProvider(claim_verifier=authority.verify_dispatch_claim)
    original = provider.execute
    def execute_twice(*args, **kwargs):
        result = original(*args, **kwargs)
        with pytest.raises(RuntimeError, match="claim"):
            original(*args, **kwargs)
        return result
    monkeypatch.setattr(provider, "execute", execute_twice)
    try:
        result = provider.dispatch_authorized(authority, OP, KEY, COMMAND)
        assert result["status"] == "CONFIRMED"
        assert "provider_send_capability" not in result["claim"]
        assert provider.execute_count(OP) == 1
    finally:
        provider.conn.close()


def test_lost_ack_concurrent_confirmation_returns_verified_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())
    monkeypatch.setenv("TENIR_B3_PRIVATE_KEY_HEX", bytes(range(64, 96)).hex())
    backend = SQLiteConsensusBackend(str(tmp_path))
    assert backend.register_resource("budget", 50) == "REGISTERED"
    authority = NativePositivePermitAuthority(backend, trusted_provider_public_key=_public_key())
    authority.admit_reserve_issue(OP, "eval", COMMAND, resource_id="budget")
    provider = KernelClaimProofProvider(claim_verifier=authority.verify_dispatch_claim)
    original = authority.mark_dispatch_unknown
    def concurrent_confirm(claim):
        receipt = provider.reconcile(OP)
        assert receipt is not None
        authority.mark_dispatch_confirmed(claim, receipt)
        return original(claim)
    monkeypatch.setattr(authority, "mark_dispatch_unknown", concurrent_confirm)
    try:
        result = provider.dispatch_authorized(authority, OP, KEY, COMMAND, lose_ack=True)
        assert result["status"] == "CONFIRMED"
        assert provider.verify_receipt(result["receipt"])
        assert provider.execute_count(OP) == 1
    finally:
        provider.conn.close()
