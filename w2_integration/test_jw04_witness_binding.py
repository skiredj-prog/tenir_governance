"""JW-04: one witness resolution per command, and expiry inside the transaction."""
import sqlite3

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tenir_governance.operations.provider import MockProvider
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from w2_integration.independent_witness import IndependentWitness
from w2_integration.native_dispatch_adapter import (
    Ed25519ConnectorAttestor, NativeClaimedDispatchAdapter,
)
from w2_integration import native_positive_permit as permit_module
from w2_integration.native_positive_permit import NativePositivePermitAuthority


OPERATION = "lab:jw04"
IDEMPOTENCY = "lab:jw04-idem"


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
    authority.admit_reserve_issue(OPERATION, "eval-jw04", command, resource_id="lab:budget")
    provider = MockProvider()
    adapter = NativeClaimedDispatchAdapter(authority, provider, attestor)
    return backend, authority, provider, adapter, command


def _checkpoint_count(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT COUNT(*) FROM witness_checkpoints").fetchone()[0]


def test_same_command_keeps_one_witness_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())
    witness = IndependentWitness(str(tmp_path / "witness.sqlite3"))
    first = witness.issue("op:idem", "fingerprint-1")
    second = witness.issue("op:idem", "fingerprint-1")
    assert second == first
    assert _checkpoint_count(witness.db_path) == 1
    assert witness.verify(first, subject="op:idem")
    with pytest.raises(RuntimeError, match="already resolved"):
        witness.issue("op:idem", "fingerprint-2")
    assert _checkpoint_count(witness.db_path) == 1


def test_proof_for_another_subject_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())
    witness = IndependentWitness(str(tmp_path / "witness.sqlite3"))
    evidence = witness.issue("op-a:idem-a", "fingerprint-a")
    witness.issue("op-b:idem-b", "fingerprint-b")
    assert witness.verify(evidence, subject="op-a:idem-a")
    assert witness.verify(evidence, subject="op-b:idem-b") is False


def test_kernel_retry_reuses_the_orphan_witness_resolution(tmp_path, monkeypatch):
    import hashlib
    import json
    backend, authority, _, _, command = _system(tmp_path, monkeypatch)
    with backend._connection() as db:
        permit = db.execute(
            "SELECT permit_id, reservation_id, audience, request_hash FROM w2_positive_v1_permits"
        ).fetchone()
    fingerprint = hashlib.sha256(
        b"TENIR-W2-CLAIM-FINGERPRINT-v1\0" + json.dumps({
            "operation_key": OPERATION,
            "idempotency_key": IDEMPOTENCY,
            "request_hash": permit["request_hash"],
            "permit_id": permit["permit_id"],
            "reservation_id": permit["reservation_id"],
            "audience": permit["audience"],
        }, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    subject = f"{OPERATION}:{IDEMPOTENCY}"
    orphan = authority.witness.issue(subject, fingerprint)
    assert _checkpoint_count(authority.witness.db_path) == 1
    claim = authority.consume_and_claim(OPERATION, command, IDEMPOTENCY)
    assert claim["witness"]["record"]["revision"] == orphan["record"]["revision"]
    assert claim["witness"]["signature"] == orphan["signature"]
    assert claim["witness"]["record"]["subject"] == subject
    assert _checkpoint_count(authority.witness.db_path) == 1
    assert authority.witness.verify(claim["witness"], subject=subject)


def test_replay_refuses_a_witness_bound_to_another_subject(tmp_path, monkeypatch):
    backend, authority, _, adapter, command = _system(tmp_path, monkeypatch)
    claim = authority.consume_and_claim(OPERATION, command, IDEMPOTENCY)
    other = authority.witness.issue("other:subject", "other-fingerprint")
    import json
    witness_hash = __import__("hashlib").sha256(
        b"TENIR-W2-CLAIM-WITNESS-v1\0"
        + json.dumps(other, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with backend._connection() as db:
        db.execute(
            "UPDATE w2_positive_v1_outbox SET witness_evidence_json=?, witness_hash=? "
            "WHERE execution_id=?",
            (json.dumps(other, sort_keys=True, separators=(",", ":")), witness_hash,
             claim["execution_id"]),
        )
    with pytest.raises(RuntimeError, match="witness subject"):
        adapter.dispatch(OPERATION, IDEMPOTENCY, command)


def test_expiry_is_rechecked_inside_the_claim_transaction(tmp_path, monkeypatch):
    backend, authority, provider, adapter, command = _system(tmp_path, monkeypatch)
    real = permit_module.datetime

    class Expired(real):
        @classmethod
        def now(cls, tz=None):
            return real.fromisoformat("2099-01-01T00:00:00+00:00")

    monkeypatch.setattr(permit_module, "datetime", Expired)
    with pytest.raises(RuntimeError, match="permit expired"):
        authority.consume_and_claim(OPERATION, command, IDEMPOTENCY)
    assert provider.calls == []
    with backend._connection() as db:
        assert db.execute("SELECT consumed FROM w2_positive_v1_permits").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_outbox").fetchone()[0] == 0


def test_expired_permit_does_not_receive_a_send_right(tmp_path, monkeypatch):
    backend, authority, provider, adapter, command = _system(tmp_path, monkeypatch)
    claim = authority.consume_and_claim(OPERATION, command, IDEMPOTENCY)
    real = permit_module.datetime

    class Expired(real):
        @classmethod
        def now(cls, tz=None):
            return real.fromisoformat("2099-01-01T00:00:00+00:00")

    monkeypatch.setattr(permit_module, "datetime", Expired)
    assert authority.grant_send_right(claim) == "EXPIRED"
    result = adapter.dispatch(OPERATION, IDEMPOTENCY, command)
    assert result["status"] == "HOLD"
    assert provider.calls == []
    with backend._connection() as db:
        assert db.execute(
            "SELECT 1 FROM w2_positive_v1_events WHERE event='dispatch_send_granted'"
        ).fetchone() is None
        assert db.execute("SELECT status FROM w2_positive_v1_outbox").fetchone()[0] == "PENDING"
