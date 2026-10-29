"""JW-01: the approved durable command is the provider's only argument source."""
from copy import deepcopy
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tenir_governance.operations.provider import MockProvider, ProviderOutcome
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from w2_integration.native_dispatch_adapter import (
    Ed25519ConnectorAttestor, NativeClaimedDispatchAdapter,
)
from w2_integration.native_positive_permit import NativePositivePermitAuthority
from w2_2b_canonical import request_hash


SCHEMA = "tenir-dispatch-command/v1"
OPERATION = "lab:jw01"
IDEMPOTENCY = "lab:jw01-idem"


def _command():
    return {
        "schema": SCHEMA,
        "resource_id": "lab:budget",
        "amount": 7,
        "action_type": "payment",
        "agent_id": "lab-agent",
        "metadata": {
            "recipient": "recipient-1",
            "currency": "EUR",
            "order": {"reference": "order-1"},
        },
    }


def _system(tmp_path, monkeypatch, *, admit=True, command=None):
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
    provider = MockProvider()
    command = _command() if command is None else command
    if admit:
        authority.admit_reserve_issue(
            OPERATION, "eval-jw01", command, resource_id="lab:budget",
        )
    adapter = NativeClaimedDispatchAdapter(authority, provider, attestor)
    return backend, authority, provider, attestor, adapter, command


def _assert_unconsumed(backend, provider):
    assert provider.calls == []
    with backend._connection() as db:
        row = db.execute(
            "SELECT consumed, nonce_status FROM w2_positive_v1_permits WHERE operation_key=?",
            (OPERATION,),
        ).fetchone()
        assert row["consumed"] == 0
        assert row["nonce_status"] == "ISSUED"
        assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_outbox").fetchone()[0] == 0


def test_command_alone_drives_exact_provider_arguments_and_i8(tmp_path, monkeypatch):
    backend, _, provider, _, adapter, command = _system(tmp_path, monkeypatch)
    result = adapter.dispatch(OPERATION, IDEMPOTENCY, command)
    assert result["status"] == "CONFIRMED"
    assert len(provider.calls) == 1
    sent = provider.calls[0]
    assert sent["idempotency_key"] == IDEMPOTENCY
    for field in ("action_type", "amount", "agent_id", "metadata"):
        assert sent[field] == command[field]
    assert result["receipt"]["request_hash"] == request_hash(command)
    with backend._connection() as db:
        resource = db.execute(
            "SELECT reserved, committed FROM resources WHERE resource_id='lab:budget'"
        ).fetchone()
        assert resource["reserved"] == "0" and resource["committed"] == "7"


@pytest.mark.parametrize("field, replacement", [
    ("amount", 700),
    ("action_type", "transfer"),
    ("agent_id", "other-agent"),
    ("metadata", {"recipient": "recipient-2", "currency": "EUR"}),
    ("metadata", {"recipient": "recipient-1", "currency": "USD"}),
    ("metadata", {"recipient": "recipient-1", "currency": "EUR", "order": {"reference": "order-2"}}),
])
def test_legacy_argument_substitution_is_refused_before_consumption(
    tmp_path, monkeypatch, field, replacement,
):
    backend, _, provider, _, adapter, command = _system(tmp_path, monkeypatch)
    assertions = {name: deepcopy(command[name]) for name in ("action_type", "amount", "agent_id", "metadata")}
    assertions[field] = replacement
    result = adapter.dispatch(OPERATION, IDEMPOTENCY, command, **assertions)
    assert result["status"] == "HOLD"
    assert result["claim"] is None
    _assert_unconsumed(backend, provider)


def test_equal_legacy_arguments_are_only_assertions(tmp_path, monkeypatch):
    _, _, provider, _, adapter, command = _system(tmp_path, monkeypatch)
    assertions = {name: deepcopy(command[name]) for name in ("action_type", "amount", "agent_id", "metadata")}
    assertions["amount"] = 7.0
    result = adapter.dispatch(OPERATION, IDEMPOTENCY, command, **assertions)
    assert result["status"] == "CONFIRMED"
    assert type(provider.calls[0]["amount"]) is int
    assert provider.calls[0]["metadata"] == command["metadata"]


@pytest.mark.parametrize("field", ["schema", "resource_id", "amount", "action_type", "agent_id", "metadata"])
def test_incomplete_dispatch_command_has_no_implicit_defaults(tmp_path, monkeypatch, field):
    backend, _, provider, _, adapter, command = _system(tmp_path, monkeypatch)
    incomplete = deepcopy(command)
    del incomplete[field]
    with pytest.raises(RuntimeError):
        adapter.dispatch(OPERATION, IDEMPOTENCY, incomplete,
                         action_type="payment", amount=7, agent_id="lab-agent")
    _assert_unconsumed(backend, provider)


def test_sparse_historical_payload_cannot_be_silently_upgraded(tmp_path, monkeypatch):
    backend, _, provider, _, adapter, payload = _system(
        tmp_path, monkeypatch, command={"amount": 7, "resource_id": "lab:budget"},
    )
    with pytest.raises(RuntimeError):
        adapter.dispatch(OPERATION, IDEMPOTENCY, payload,
                         action_type="payment", amount=7, agent_id="lab-agent")
    _assert_unconsumed(backend, provider)


@pytest.mark.parametrize("bad_amount", [True, False, "7", 0, -1, float("nan"), float("inf")])
def test_dispatch_command_rejects_nonpositive_or_non_json_amount(tmp_path, monkeypatch, bad_amount):
    backend, _, provider, _, adapter, command = _system(tmp_path, monkeypatch)
    command["amount"] = bad_amount
    with pytest.raises(RuntimeError):
        adapter.dispatch(OPERATION, IDEMPOTENCY, command)
    _assert_unconsumed(backend, provider)


def test_unknown_command_schema_is_refused(tmp_path, monkeypatch):
    backend, _, provider, _, adapter, command = _system(tmp_path, monkeypatch)
    command["schema"] = "tenir-dispatch-command/v999"
    with pytest.raises(RuntimeError):
        adapter.dispatch(OPERATION, IDEMPOTENCY, command)
    _assert_unconsumed(backend, provider)


def test_unrecognized_command_field_is_refused(tmp_path, monkeypatch):
    backend, _, provider, _, adapter, command = _system(tmp_path, monkeypatch)
    command["unsigned_override"] = "ignored-by-old-reader"
    with pytest.raises(RuntimeError):
        adapter.dispatch(OPERATION, IDEMPOTENCY, command)
    _assert_unconsumed(backend, provider)


def test_admission_amount_override_must_equal_the_authorized_command(tmp_path, monkeypatch):
    backend, authority, _, _, _, command = _system(tmp_path, monkeypatch, admit=False)
    with pytest.raises(RuntimeError):
        authority.admit_reserve_issue(
            OPERATION, "eval-jw01", command, amount=1, resource_id="lab:budget",
        )
    with backend._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_permits").fetchone()[0] == 0
        assert db.execute("SELECT reserved FROM resources").fetchone()[0] == "0"


def test_caller_mutation_during_admission_cannot_split_budget_and_payload(tmp_path, monkeypatch):
    backend, authority, provider, _, adapter, command = _system(tmp_path, monkeypatch, admit=False)
    original = deepcopy(command)
    normalize_amount = backend._amount

    def mutate_caller_during_amount_check(amount):
        command["amount"] = 700
        command["metadata"]["recipient"] = "changed-during-admission"
        return normalize_amount(amount)

    monkeypatch.setattr(backend, "_amount", mutate_caller_during_amount_check)
    issued = authority.admit_reserve_issue(
        OPERATION, "eval-jw01", command, resource_id="lab:budget",
    )
    assert issued["amount"] == "7"
    assert issued["request_hash"] == request_hash(original)
    result = adapter.dispatch(OPERATION, IDEMPOTENCY, original)
    assert result["status"] == "CONFIRMED"
    assert provider.calls[0]["amount"] == 7
    assert provider.calls[0]["metadata"] == original["metadata"]


def test_schema_promotion_during_admission_cannot_authorize_a_changed_amount(tmp_path, monkeypatch):
    import w2_integration.native_positive_permit as native_module
    _, authority, provider, _, adapter, command = _system(tmp_path, monkeypatch, admit=False)
    command["schema"] = "legacy-payload"
    original = deepcopy(command)
    hash_request = native_module.canon_request_hash

    def promote_caller_after_budget_read(payload):
        command["schema"] = SCHEMA
        command["amount"] = 700
        return hash_request(payload)

    monkeypatch.setattr(native_module, "canon_request_hash", promote_caller_after_budget_read)
    issued = authority.admit_reserve_issue(
        OPERATION, "eval-jw01", command, resource_id="lab:budget",
    )
    assert issued["amount"] == "7"
    assert issued["request_hash"] == request_hash(original)
    with pytest.raises(RuntimeError):
        adapter.dispatch(OPERATION, IDEMPOTENCY, command,
                         action_type="payment", amount=700, agent_id="lab-agent")
    assert provider.calls == []


def test_caller_mutation_during_claim_does_not_change_the_sent_command(tmp_path, monkeypatch):
    _, authority, provider, _, adapter, command = _system(tmp_path, monkeypatch)
    original = deepcopy(command)
    consume = authority.consume_and_claim

    def mutate_caller_then_claim(operation, snapshot, key):
        command["amount"] = 700
        command["metadata"]["recipient"] = "changed-recipient"
        return consume(operation, snapshot, key)

    monkeypatch.setattr(authority, "consume_and_claim", mutate_caller_then_claim)
    result = adapter.dispatch(OPERATION, IDEMPOTENCY, command)
    assert result["status"] == "CONFIRMED"
    assert provider.calls[0]["amount"] == original["amount"]
    assert provider.calls[0]["metadata"] == original["metadata"]
    assert result["receipt"]["request_hash"] == request_hash(original)


def test_provider_metadata_mutation_does_not_change_attested_payload(tmp_path, monkeypatch):
    _, authority, provider, _, adapter, command = _system(tmp_path, monkeypatch)
    original = deepcopy(command)
    execute = provider.execute

    def mutate_received_metadata(**kwargs):
        result = execute(**kwargs)
        kwargs["metadata"]["order"]["reference"] = "provider-private-note"
        return result

    monkeypatch.setattr(provider, "execute", mutate_received_metadata)
    result = adapter.dispatch(OPERATION, IDEMPOTENCY, command)
    assert result["status"] == "CONFIRMED"
    assert command == original
    assert result["receipt"]["request_hash"] == request_hash(original)
    assert authority._verify_provider_receipt(result["receipt"])


def test_stored_outbox_payload_must_match_its_committed_hash(tmp_path, monkeypatch):
    backend, authority, provider, _, adapter, command = _system(tmp_path, monkeypatch)
    consume = authority.consume_and_claim

    def corrupt_outbox_after_claim(operation, snapshot, key):
        claim = consume(operation, snapshot, key)
        altered = deepcopy(snapshot)
        altered["metadata"]["recipient"] = "tampered-recipient"
        with backend._connection() as db:
            db.execute(
                "UPDATE w2_positive_v1_outbox SET payload_json=? WHERE execution_id=?",
                (json.dumps(altered), claim["execution_id"]),
            )
        return claim

    monkeypatch.setattr(authority, "consume_and_claim", corrupt_outbox_after_claim)
    with pytest.raises(RuntimeError):
        adapter.dispatch(OPERATION, IDEMPOTENCY, command)
    assert provider.calls == []


def test_unknown_restart_reuses_durable_command_and_only_reconciles(tmp_path, monkeypatch):
    backend, _, provider, attestor, adapter, command = _system(tmp_path, monkeypatch)
    provider.behavior = "timeout"
    first = adapter.dispatch(OPERATION, IDEMPOTENCY, command)
    assert first["status"] == "UNKNOWN"
    assert len(provider.calls) == 1
    recovered_authority = NativePositivePermitAuthority(
        backend, trusted_provider_public_key=attestor.public_key_bytes,
    )
    recovered = NativeClaimedDispatchAdapter(recovered_authority, provider, attestor)
    second = recovered.dispatch(OPERATION, IDEMPOTENCY, deepcopy(command))
    assert second["status"] == "CONFIRMED"
    assert second["claim"]["execution_id"] == first["claim"]["execution_id"]
    assert len(provider.calls) == 1 and len(provider.reconcile_calls) == 1
    assert second["receipt"]["request_hash"] == request_hash(command)


def test_confirmed_replay_keeps_same_command_without_another_send(tmp_path, monkeypatch):
    _, _, provider, _, adapter, command = _system(tmp_path, monkeypatch)
    first = adapter.dispatch(OPERATION, IDEMPOTENCY, command)
    second = adapter.dispatch(OPERATION, IDEMPOTENCY, deepcopy(command))
    assert first["status"] == second["status"] == "CONFIRMED"
    assert first["claim"]["execution_id"] == second["claim"]["execution_id"]
    assert len(provider.calls) == 1
    assert first["receipt"] == second["receipt"]
