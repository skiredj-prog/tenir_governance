"""Native positive path and B3 must share canon-v1 request_hash.

Unclaimed provider execution is not a way to demonstrate that property.
"""
import os
import tempfile

from kernel_claim_provider import KernelClaimProofProvider
from native_positive_permit import NativePositivePermitAuthority
from proof_provider import ProofProvider
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from w2_2b_canonical import request_hash


def _keys():
    previous = {
        name: os.environ.get(name)
        for name in (
            "TENIR_W2_PRIVATE_KEY_HEX",
            "TENIR_WITNESS_PRIVATE_KEY_HEX",
            "TENIR_B3_PRIVATE_KEY_HEX",
        )
    }
    os.environ["TENIR_W2_PRIVATE_KEY_HEX"] = bytes(range(32)).hex()
    os.environ["TENIR_WITNESS_PRIVATE_KEY_HEX"] = bytes(range(32, 64)).hex()
    os.environ["TENIR_B3_PRIVATE_KEY_HEX"] = bytes(range(64, 96)).hex()
    return previous


def _restore(previous):
    for name, value in previous.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def _command(amount):
    return {
        "schema": "tenir-dispatch-command/v1",
        "resource_id": "budget",
        "amount": amount,
        "action_type": "payment",
        "agent_id": "lab-agent",
        "metadata": {"currency": "EUR"},
    }


def test_native_b3_same_hash():
    previous = _keys()
    try:
        _test_native_b3_same_hash()
    finally:
        _restore(previous)


def _test_native_b3_same_hash():
    _keys()
    payload = _command(100)
    expected = request_hash(payload)
    with tempfile.TemporaryDirectory() as d:
        backend = SQLiteConsensusBackend(d)
        backend.register_resource("budget", 1000000)
        authority = NativePositivePermitAuthority(backend)
        issued = authority.admit_reserve_issue(
            "op-x", "eval-x", payload, resource_id="budget",
        )
        assert issued["request_hash"] == expected
        provider = KernelClaimProofProvider(
            db_path=d + "/provider.db",
            claim_verifier=authority.verify_dispatch_claim,
        )
        claim = authority.consume_and_claim("op-x", payload, "idem-x")
        receipt = provider.execute("op-x", "idem-x", payload, claim=claim)
        assert receipt["request_hash"] == expected
        assert receipt["request_hash"] == issued["request_hash"]
        assert provider.execute_count("op-x") == 1


def test_divergent_refused_before_effect():
    previous = _keys()
    try:
        _test_divergent_refused_before_effect()
    finally:
        _restore(previous)


def _test_divergent_refused_before_effect():
    _keys()
    with tempfile.TemporaryDirectory() as d:
        backend = SQLiteConsensusBackend(d)
        backend.register_resource("budget", 1000000)
        authority = NativePositivePermitAuthority(backend)
        authority.admit_reserve_issue(
            "op-y", "eval-y", _command(100), resource_id="budget",
        )
        provider = KernelClaimProofProvider(
            db_path=d + "/provider.db",
            claim_verifier=authority.verify_dispatch_claim,
        )
        try:
            authority.consume_and_claim("op-y", _command(200), "idem-y")
            raise AssertionError("native claim should refuse")
        except RuntimeError:
            pass
        assert provider.execute_count("op-y") == 0


def test_unclaimed_execution_flag_cannot_create_an_effect():
    with tempfile.TemporaryDirectory() as d:
        try:
            ProofProvider(
                allow_unclaimed_execution=True, db_path=d + "/provider.db",
            )
            raise AssertionError("unclaimed mode must be refused")
        except RuntimeError as exc:
            assert "unclaimed provider execution is disabled" in str(exc)
        provider = ProofProvider(db_path=d + "/closed.db")
        try:
            provider.execute("direct", "direct-idem", {"amount": 7})
            raise AssertionError("direct execute must be refused")
        except RuntimeError as exc:
            assert "authenticated kernel claim required" in str(exc)
        assert provider.execute_count("direct") == 0
        provider.conn.close()
