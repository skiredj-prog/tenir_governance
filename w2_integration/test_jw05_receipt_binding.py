"""JW-05: a confirmed receipt must bind the claim, permit, reservation and witness."""
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tenir_governance.operations.provider import MockProvider
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from w2_integration.native_dispatch_adapter import (
    Ed25519ConnectorAttestor, NativeClaimedDispatchAdapter,
)
from w2_integration.native_positive_permit import NativePositivePermitAuthority


OPERATION = "lab:jw05"
IDEMPOTENCY = "lab:jw05-idem"


def _command():
    return {
        "schema": "tenir-dispatch-command/v1",
        "resource_id": "lab:budget",
        "amount": 7,
        "action_type": "payment",
        "agent_id": "lab-agent",
        "metadata": {"recipient": "recipient-1", "currency": "EUR"},
    }


def _system(tmp_path, monkeypatch):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())
    backend = SQLiteConsensusBackend(str(tmp_path))
    backend.register_resource("lab:budget", 50)
    attestor = Ed25519ConnectorAttestor(
        Ed25519PrivateKey.from_private_bytes(bytes(range(96, 128)))
    )
    authority = NativePositivePermitAuthority(
        backend, trusted_provider_public_key=attestor.public_key_bytes,
    )
    command = _command()
    authority.admit_reserve_issue(OPERATION, "eval-jw05", command, resource_id="lab:budget")
    provider = MockProvider()
    adapter = NativeClaimedDispatchAdapter(authority, provider, attestor)
    return backend, authority, provider, attestor, adapter, command


def _sign(attestor, authority, provider, claim, command, **overrides):
    binding = authority.receipt_context(claim)
    binding.update(overrides)
    return attestor.attest_confirmed(
        operation_key=OPERATION, idempotency_key=IDEMPOTENCY, payload=command,
        reconciliation=provider.reconcile(IDEMPOTENCY), binding=binding,
    )


@pytest.mark.parametrize("field,value", [
    ("execution_id", "exec-other"),
    ("reservation_id", "res-other"),
    ("permit_id", "permit-other"),
    ("witness_hash", "0" * 64),
    ("witness_epoch", 99),
])
def test_substituted_receipt_binding_does_not_settle(tmp_path, monkeypatch, field, value):
    backend, authority, provider, attestor, _, command = _system(tmp_path, monkeypatch)
    claim = authority.consume_and_claim(OPERATION, command, IDEMPOTENCY)
    receipt = _sign(attestor, authority, provider, claim, command, **{field: value})
    with pytest.raises(RuntimeError, match="binding mismatch"):
        authority.mark_dispatch_confirmed(claim, receipt)
    with backend._connection() as db:
        assert db.execute("SELECT status FROM w2_positive_v1_outbox").fetchone()[0] == "PENDING"
        assert db.execute("SELECT state FROM reservations").fetchone()[0] == "RESERVED"
        assert db.execute("SELECT settled FROM w2_positive_v1_capacity").fetchone()[0] == 0


def test_confirmed_receipt_carries_the_durable_binding(tmp_path, monkeypatch):
    backend, authority, provider, _, adapter, command = _system(tmp_path, monkeypatch)
    result = adapter.dispatch(OPERATION, IDEMPOTENCY, command)
    assert result["status"] == "CONFIRMED"
    context = authority.receipt_context(result["claim"])
    receipt = result["receipt"]
    assert receipt["attestation_type"] == "connector-reconciled-provider/v2"
    for name, value in context.items():
        assert receipt[name] == value
    assert len(provider.calls) == 1
    with backend._connection() as db:
        assert db.execute("SELECT state FROM reservations").fetchone()[0] == "COMMITTED"
