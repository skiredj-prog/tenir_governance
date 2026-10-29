"""JW-09: positive G4-A grant is required before I8-backed B3 dispatch."""
import os
import tempfile

import pytest

from kernel_claim_provider import KernelClaimProofProvider
from native_positive_g4a import GRANT_SCHEMA, NativePositiveG4A
from native_positive_permit import NativePositivePermitAuthority
from proof_provider import ProofProvider
from tenir_governance.operations.experimental_g4a_settlement import G4AReceipt
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from w2_2b_canonical import request_hash


OPERATION = "lab:jw09"
EVALUATION = "eval-jw09"
IDEMPOTENCY = "lab:jw09-idem"
RESOURCE = "lab:budget"


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


def _command(amount=7):
    return {
        "schema": "tenir-dispatch-command/v1",
        "resource_id": RESOURCE,
        "amount": amount,
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
    return previous, backend, authority, gate, provider


def test_positive_path_binds_the_same_hash_from_grant_to_b3():
    previous, _, _, gate, provider = _system()
    try:
        command = _command()
        grant = gate.evaluate_and_grant(
            OPERATION, EVALUATION, command, resource_id=RESOURCE,
        )
        assert grant["schema"] == GRANT_SCHEMA
        assert grant["historical_execution_authorized"] is False
        assert grant["request_hash"] == request_hash(command)
        assert grant["evaluation_id"] == EVALUATION
        result = gate.dispatch_to_b3(OPERATION, IDEMPOTENCY, command, grant, provider)
        assert result["receipt"]["request_hash"] == grant["request_hash"]
        assert result["causal"]["grant_id"] == grant["grant_id"]
        assert result["causal"]["evaluation_id"] == EVALUATION
        assert result["causal"]["operation_id"] == OPERATION
        assert provider.execute_count(OPERATION) == 1
    finally:
        _restore(previous)


def test_missing_grant_does_not_call_the_provider():
    previous, _, authority, gate, provider = _system()
    try:
        with pytest.raises(RuntimeError, match="grant required"):
            gate.dispatch_to_b3(OPERATION, IDEMPOTENCY, _command(), None, provider)
        assert provider.execute_count(OPERATION) == 0
        with authority._tx() as db:
            assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_permits").fetchone()[0] == 0
    finally:
        _restore(previous)


def test_expired_grant_does_not_call_the_provider():
    previous, _, authority, gate, provider = _system()
    try:
        grant = gate.evaluate_and_grant(
            OPERATION, EVALUATION, _command(), resource_id=RESOURCE,
        )
        with authority._write("expire_grant") as db:
            db.execute(
                "UPDATE w2_positive_g4a_v1_grants SET expires_at=? WHERE grant_id=?",
                ("2000-01-01T00:00:00+00:00", grant["grant_id"]),
            )
        with pytest.raises(RuntimeError, match="expired"):
            gate.dispatch_to_b3(OPERATION, IDEMPOTENCY, _command(), grant, provider)
        assert provider.execute_count(OPERATION) == 0
    finally:
        _restore(previous)


def test_divergent_hash_does_not_call_the_provider():
    previous, _, authority, gate, provider = _system()
    try:
        grant = gate.evaluate_and_grant(
            OPERATION, EVALUATION, _command(7), resource_id=RESOURCE,
        )
        with pytest.raises(RuntimeError, match="does not match"):
            gate.dispatch_to_b3(OPERATION, IDEMPOTENCY, _command(700), grant, provider)
        assert provider.execute_count(OPERATION) == 0
        with authority._tx() as db:
            assert db.execute("SELECT consumed FROM w2_positive_v1_permits").fetchone()[0] == 0
            assert db.execute("SELECT consumed FROM w2_positive_g4a_v1_grants").fetchone()[0] == 0
    finally:
        _restore(previous)


def test_direct_provider_call_still_refuses():
    with tempfile.TemporaryDirectory() as directory:
        provider = ProofProvider(db_path=directory + "/raw.db")
        with pytest.raises(RuntimeError, match="authenticated kernel claim required"):
            provider.execute(OPERATION, IDEMPOTENCY, _command())
        assert provider.execute_count(OPERATION) == 0
        provider.conn.close()


def test_historical_g4a_execution_flag_is_unchanged():
    assert G4AReceipt.__dataclass_fields__["execution_authorized"].default is False
    assert G4AReceipt.__dataclass_fields__["decision"].default == "HOLD"
    assert NativePositiveG4A.historical_execution_authorized is False
