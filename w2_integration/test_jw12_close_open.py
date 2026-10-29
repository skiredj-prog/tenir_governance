"""JW-12: close the laboratory bypasses that can be closed in-process."""
import os
import tempfile

import pytest

from kernel_claim_provider import KernelClaimProofProvider
from native_positive_g4a import NativePositiveG4A
from native_positive_permit import NativePositivePermitAuthority
from proof_provider import ProofProvider
from tenir_governance.operations.experimental_g4a_settlement import G4AReceipt
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend

OPERATION = "lab:jw12"
EVALUATION = "eval-jw12"
IDEMPOTENCY = "lab:jw12-idem"
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


def _command():
    return {
        "schema": "tenir-dispatch-command/v1",
        "resource_id": RESOURCE,
        "amount": 7,
        "action_type": "payment",
        "agent_id": "lab-agent",
        "metadata": {"recipient": "recipient-1"},
    }


def test_caller_supplied_verifier_cannot_execute():
    with tempfile.TemporaryDirectory() as directory:
        provider = ProofProvider(
            db_path=directory + "/raw.db",
            claim_verifier=lambda *args: True,
        )
        with pytest.raises(RuntimeError, match="caller-supplied claim verifier refused"):
            provider.execute(OPERATION, IDEMPOTENCY, _command(), claim={"nonce": "x"})
        assert provider.execute_count(OPERATION) == 0
        provider.conn.close()


def test_historical_receipt_cannot_authorize_dispatch():
    previous = _keys()
    directory = tempfile.mkdtemp()
    try:
        backend = SQLiteConsensusBackend(directory)
        backend.register_resource(RESOURCE, 50)
        authority = NativePositivePermitAuthority(backend)
        gate = NativePositiveG4A(authority)
        provider = KernelClaimProofProvider(
            db_path=directory + "/provider.db",
            claim_verifier=authority.verify_dispatch_claim,
        )
        historical = G4AReceipt(
            operation_key=OPERATION, reservation_id="r", attempt_id="a",
            payload_hash="h", authority_epoch="e", trust_generation=1,
            operation_state="RESERVED", budget_state="RESERVED", version=1,
            event_hash="x",
        )
        with pytest.raises(RuntimeError, match="historical G4-A receipt"):
            gate.dispatch_to_b3(
                OPERATION, IDEMPOTENCY, _command(),
                {"grant_id": "historical", "simulated": True, "decision": "HOLD",
                 "execution_authorized": historical.execution_authorized},
                provider,
            )
        assert provider.execute_count(OPERATION) == 0
    finally:
        _restore(previous)


def test_connector_attestation_is_not_a_provider_receipt():
    previous = _keys()
    directory = tempfile.mkdtemp()
    try:
        backend = SQLiteConsensusBackend(directory)
        backend.register_resource(RESOURCE, 50)
        authority = NativePositivePermitAuthority(backend)
        gate = NativePositiveG4A(authority)
        grant = gate.evaluate_and_grant(
            OPERATION, EVALUATION, _command(), resource_id=RESOURCE,
        )

        class FakeConnector:
            def execute(self, *args, **kwargs):
                return {
                    "attestation_type": "connector-reconciled-provider/v2",
                    "request_hash": grant["request_hash"],
                    "effect_status": "EXECUTED",
                }

        with pytest.raises(RuntimeError, match="native claim provider"):
            gate.dispatch_to_b3(OPERATION, IDEMPOTENCY, _command(), grant, FakeConnector())
        with authority._tx() as db:
            assert db.execute("SELECT consumed FROM w2_positive_v1_permits").fetchone()[0] == 0
    finally:
        _restore(previous)
