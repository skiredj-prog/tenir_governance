"""Tests for the separate, opt-in native DispatchProvider adapter."""
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest
import sqlite3

from tenir_governance.operations.provider import (
    MockProvider,
    ProviderOutcome,
    ReconciliationResult,
)
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from w2_integration.native_dispatch_adapter import (
    Ed25519ConnectorAttestor,
    NativeClaimedDispatchAdapter,
)
from w2_integration.native_positive_permit import NativePositivePermitAuthority
from w2_integration.native_dispatch_command import COMMAND_SCHEMA


def _system(tmp_path, *, behavior="confirmed", reconcile=ProviderOutcome.CONFIRMED,
            anchored=True):
    backend = SQLiteConsensusBackend(str(tmp_path))
    backend.register_resource("lab:budget", 20)
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(96, 128)))
    attestor = Ed25519ConnectorAttestor(key)
    authority = NativePositivePermitAuthority(
        backend,
        trusted_provider_public_key=(attestor.public_key_bytes if anchored else None),
    )
    provider = MockProvider(behavior=behavior, reconcile_outcome=reconcile)
    payload = {"schema": COMMAND_SCHEMA, "amount": 7, "resource_id": "lab:budget",
               "action_type": "payment", "agent_id": "lab-agent",
               "metadata": {"currency": "EUR", "recipient": "lab-recipient"}}
    authority.admit_reserve_issue(
        "lab:adapter-op", "eval-adapter", payload,
        resource_id="lab:budget",
    )
    return backend, authority, provider, attestor, payload


def _adapter(authority, provider, attestor):
    return NativeClaimedDispatchAdapter(authority, provider, attestor)


def test_confirmed_requires_provider_reconciliation_and_commits_i8(tmp_path):
    backend, authority, provider, attestor, payload = _system(tmp_path)
    adapter = _adapter(authority, provider, attestor)

    result = adapter.dispatch(
        "lab:adapter-op", "lab:adapter-idem", payload,
        action_type="payment", amount=7, agent_id="lab-agent",
    )

    assert result["status"] == "CONFIRMED"
    assert result["receipt"]["attestation_type"] == "connector-reconciled-provider/v2"
    assert authority._verify_provider_receipt(result["receipt"])
    assert len(provider.calls) == 1
    assert len(provider.reconcile_calls) == 1
    with backend._connection() as db:
        reservation = db.execute(
            "SELECT state FROM reservations WHERE idempotency_key='lab:adapter-op'"
        ).fetchone()
        resource = db.execute(
            "SELECT reserved, committed FROM resources WHERE resource_id='lab:budget'"
        ).fetchone()
        assert reservation["state"] == "COMMITTED"
        assert resource["reserved"] == "0" and resource["committed"] == "7"


def test_timeout_replay_reconciles_without_second_execute(tmp_path):
    backend, authority, provider, attestor, payload = _system(
        tmp_path, behavior="timeout", reconcile=ProviderOutcome.CONFIRMED,
    )
    adapter = _adapter(authority, provider, attestor)

    first = adapter.dispatch(
        "lab:adapter-op", "lab:adapter-idem", payload,
        action_type="payment", amount=7, agent_id="lab-agent",
    )
    assert first["status"] == "UNKNOWN"
    assert len(provider.calls) == 1
    assert len(provider.reconcile_calls) == 0

    resumed = adapter.dispatch(
        "lab:adapter-op", "lab:adapter-idem", payload,
        action_type="payment", amount=7, agent_id="lab-agent",
    )
    assert resumed["status"] == "CONFIRMED"
    assert resumed["claim"]["replayed"] is True
    assert len(provider.calls) == 1
    assert len(provider.reconcile_calls) == 1
    with backend._connection() as db:
        assert db.execute(
            "SELECT status FROM w2_positive_v1_outbox WHERE operation_key='lab:adapter-op'"
        ).fetchone()[0] == "CONFIRMED"


def test_confirmed_execute_with_ambiguous_reconcile_retries_reconcile_only(tmp_path):
    backend, authority, provider, attestor, payload = _system(
        tmp_path, behavior="confirmed", reconcile=ProviderOutcome.UNKNOWN,
    )
    adapter = _adapter(authority, provider, attestor)
    original_reconcile = provider.reconcile
    reconcile_attempts = 0

    def ambiguous_then_reconcile(idempotency_key, provider_reference=""):
        nonlocal reconcile_attempts
        reconcile_attempts += 1
        if reconcile_attempts == 1:
            provider.reconcile_calls.append({"idempotency_key": idempotency_key})
            return ReconciliationResult(
                resolved=False, outcome=ProviderOutcome.UNKNOWN,
                error="simulated provider indexing lag",
            )
        return original_reconcile(idempotency_key, provider_reference)

    provider.reconcile = ambiguous_then_reconcile

    first = adapter.dispatch(
        "lab:adapter-op", "lab:adapter-idem", payload,
        action_type="payment", amount=7, agent_id="lab-agent",
    )
    assert first["status"] == "UNKNOWN"
    assert len(provider.calls) == 1
    assert len(provider.reconcile_calls) == 1
    with backend._connection() as db:
        assert db.execute(
            "SELECT state FROM reservations WHERE idempotency_key='lab:adapter-op'"
        ).fetchone()[0] == "RESERVED"

    provider.reconcile_outcome = ProviderOutcome.CONFIRMED
    resumed = adapter.dispatch(
        "lab:adapter-op", "lab:adapter-idem", payload,
        action_type="payment", amount=7, agent_id="lab-agent",
    )
    assert resumed["status"] == "CONFIRMED"
    assert len(provider.calls) == 1
    assert len(provider.reconcile_calls) == 2
    with backend._connection() as db:
        assert db.execute(
            "SELECT state FROM reservations WHERE idempotency_key='lab:adapter-op'"
        ).fetchone()[0] == "COMMITTED"


def test_failed_provider_result_is_frozen_for_manual_review(tmp_path):
    backend, authority, provider, attestor, payload = _system(
        tmp_path, behavior="failed", reconcile=ProviderOutcome.CONFIRMED,
    )
    adapter = _adapter(authority, provider, attestor)

    first = adapter.dispatch(
        "lab:adapter-op", "lab:adapter-idem", payload,
        action_type="payment", amount=7, agent_id="lab-agent",
    )
    assert first["status"] == "HOLD"
    assert authority.requires_manual_review("lab:adapter-op")
    assert not authority.verify_dispatch_claim(
        "lab:adapter-op", "lab:adapter-idem", payload, first["claim"]
    )
    with backend._connection() as db:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute(
                "DELETE FROM w2_positive_v1_events WHERE operation_key='lab:adapter-op' "
                "AND event='dispatch_review'"
            )
    reconciliation = provider.reconcile("lab:adapter-idem")
    signed_receipt = attestor.attest_confirmed(
        operation_key="lab:adapter-op", idempotency_key="lab:adapter-idem",
        payload=payload, reconciliation=reconciliation,
        binding=authority.receipt_context(first["claim"]),
    )
    with pytest.raises(RuntimeError, match="manual review latch"):
        authority.mark_dispatch_confirmed(first["claim"], signed_receipt)

    repeated = adapter.dispatch(
        "lab:adapter-op", "lab:adapter-idem", payload,
        action_type="payment", amount=7, agent_id="lab-agent",
    )
    assert repeated["status"] == "HOLD"
    assert len(provider.calls) == 1
    assert len(provider.reconcile_calls) == 1
    with backend._connection() as db:
        assert db.execute(
            "SELECT status FROM w2_positive_v1_outbox WHERE operation_key='lab:adapter-op'"
        ).fetchone()[0] == "UNKNOWN"
        assert db.execute(
            "SELECT state FROM reservations WHERE idempotency_key='lab:adapter-op'"
        ).fetchone()[0] == "RESERVED"


def test_reconciliation_conflict_is_frozen_and_never_committed(tmp_path):
    backend, authority, provider, attestor, payload = _system(
        tmp_path, behavior="confirmed", reconcile=ProviderOutcome.FAILED,
    )
    adapter = _adapter(authority, provider, attestor)

    result = adapter.dispatch(
        "lab:adapter-op", "lab:adapter-idem", payload,
        action_type="payment", amount=7, agent_id="lab-agent",
    )
    assert result["status"] == "HOLD"
    assert authority.requires_manual_review("lab:adapter-op")
    with backend._connection() as db:
        assert db.execute(
            "SELECT state FROM reservations WHERE idempotency_key='lab:adapter-op'"
        ).fetchone()[0] == "RESERVED"
        assert db.execute(
            "SELECT committed FROM resources WHERE resource_id='lab:budget'"
        ).fetchone()[0] == "0"


def test_late_conflict_after_confirmation_is_latched_for_review(tmp_path):
    backend, authority, provider, attestor, payload = _system(tmp_path)
    adapter = _adapter(authority, provider, attestor)
    confirmed = adapter.dispatch(
        "lab:adapter-op", "lab:adapter-idem", payload,
        action_type="payment", amount=7, agent_id="lab-agent",
    )
    assert confirmed["status"] == "CONFIRMED"

    assert authority.mark_dispatch_review(
        confirmed["claim"], "late conflicting evidence"
    ) == "CONFIRMED_REVIEW"
    repeated = adapter.dispatch(
        "lab:adapter-op", "lab:adapter-idem", payload,
        action_type="payment", amount=7, agent_id="lab-agent",
    )
    assert repeated["status"] == "HOLD"
    with pytest.raises(RuntimeError, match="manual review latch"):
        authority.mark_dispatch_confirmed(confirmed["claim"], confirmed["receipt"])
    assert len(provider.calls) == 1
    assert authority.requires_manual_review("lab:adapter-op")
    with backend._connection() as db:
        assert db.execute(
            "SELECT state FROM reservations WHERE idempotency_key='lab:adapter-op'"
        ).fetchone()[0] == "COMMITTED"


def test_unanchored_connector_attestor_is_rejected_before_claim(tmp_path):
    backend, authority, provider, attestor, payload = _system(tmp_path, anchored=False)
    try:
        _adapter(authority, provider, attestor)
        raise AssertionError("unanchored connector attestor must be rejected")
    except RuntimeError as exc:
        assert "not anchored" in str(exc)
    with backend._connection() as db:
        assert db.execute(
            "SELECT consumed FROM w2_positive_v1_permits WHERE operation_key='lab:adapter-op'"
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM w2_positive_v1_outbox WHERE operation_key='lab:adapter-op'"
        ).fetchone()[0] == 0
