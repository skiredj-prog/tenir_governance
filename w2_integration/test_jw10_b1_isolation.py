"""JW-10: once a positive grant exists, inherited paths cannot create a claim."""
import os
import tempfile

import pytest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import hashlib

from kernel_claim_provider import KernelClaimProofProvider
from native_dispatch_adapter import Ed25519ConnectorAttestor, NativeClaimedDispatchAdapter
from native_positive_g4a import NativePositiveG4A
from native_positive_permit import NativePositivePermitAuthority
from proof_provider import ProofProvider
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend

OPERATION = "lab:jw10"
EVALUATION = "eval-jw10"
IDEMPOTENCY = "lab:jw10-idem"
RESOURCE = "lab:budget"


class CountingProvider:
    def __init__(self):
        self.calls = []

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return type("Result", (), {"outcome": "CONFIRMED", "provider_reference": "x"})()

    def reconcile(self, *args, **kwargs):
        return type("Recon", (), {"resolved": True, "outcome": "CONFIRMED"})()


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


def _command():
    return {
        "schema": "tenir-dispatch-command/v1",
        "resource_id": RESOURCE,
        "amount": 7,
        "action_type": "payment",
        "agent_id": "lab-agent",
        "metadata": {"recipient": "recipient-1"},
    }


def _system():
    previous = _keys()
    directory = tempfile.mkdtemp()
    backend = SQLiteConsensusBackend(directory)
    backend.register_resource(RESOURCE, 50)
    authority = NativePositivePermitAuthority(backend)
    gate = NativePositiveG4A(authority)
    provider = KernelClaimProofProvider(
        db_path=directory + "/provider.db",
        claim_verifier=authority.verify_dispatch_claim,
    )
    raw = provider.public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    authority.trusted_provider_public_key = provider.public_key
    authority.trusted_provider_key_id = hashlib.sha256(raw).hexdigest()
    return previous, backend, authority, gate, provider


def test_inherited_claim_is_refused_after_grant():
    previous, _, authority, gate, provider = _system()
    try:
        grant = gate.evaluate_and_grant(
            OPERATION, EVALUATION, _command(), resource_id=RESOURCE,
        )
        with pytest.raises(RuntimeError, match="grant required before claim"):
            authority.consume_and_claim(OPERATION, _command(), IDEMPOTENCY)
        with pytest.raises(RuntimeError, match="grant required before claim"):
            provider.dispatch_authorized(authority, OPERATION, IDEMPOTENCY, _command())
        assert provider.execute_count(OPERATION) == 0
        result = gate.dispatch_to_b3(OPERATION, IDEMPOTENCY, _command(), grant, provider)
        assert result["receipt"]["request_hash"] == grant["request_hash"]
        assert provider.execute_count(OPERATION) == 1
    finally:
        _restore(previous)


def test_adapter_does_not_send_when_grant_exists():
    previous = _keys()
    try:
        directory = tempfile.mkdtemp()
        backend = SQLiteConsensusBackend(directory)
        backend.register_resource(RESOURCE, 50)
        attestor = Ed25519ConnectorAttestor(Ed25519PrivateKey.from_private_bytes(bytes(range(32, 64))))
        authority = NativePositivePermitAuthority(
            backend, trusted_provider_public_key=attestor.public_key_bytes,
        )
        gate = NativePositiveG4A(authority)
        gate.evaluate_and_grant(OPERATION, EVALUATION, _command(), resource_id=RESOURCE)
        provider = CountingProvider()
        adapter = NativeClaimedDispatchAdapter(authority, provider, attestor)
        result = adapter.dispatch(OPERATION, IDEMPOTENCY, _command())
        assert result["status"] == "HOLD"
        assert provider.calls == []
    finally:
        _restore(previous)


def test_direct_provider_execute_remains_refused():
    with tempfile.TemporaryDirectory() as directory:
        provider = ProofProvider(db_path=directory + "/raw.db")
        with pytest.raises(RuntimeError, match="authenticated kernel claim required"):
            provider.execute(OPERATION, IDEMPOTENCY, _command())
        assert provider.execute_count(OPERATION) == 0
        provider.conn.close()
