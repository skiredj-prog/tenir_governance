"""Safety assertions against the supplied archive; failures are audit evidence.

No implementation fixes or weakened assertions are applied to this fixture.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import threading
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tenir_governance.operations.provider import MockProvider
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from w2_integration.independent_witness import IndependentWitness
from w2_integration.native_dispatch_adapter import (
    Ed25519ConnectorAttestor, NativeClaimedDispatchAdapter,
)
from w2_integration.native_positive_permit import NativePositivePermitAuthority
from w2_integration.proof_provider import ProofProvider


def system(tmp_path, monkeypatch):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())
    backend = SQLiteConsensusBackend(str(tmp_path))
    backend.register_resource("lab:budget", 20)
    attestor = Ed25519ConnectorAttestor(
        Ed25519PrivateKey.from_private_bytes(bytes(range(96, 128)))
    )
    authority = NativePositivePermitAuthority(
        backend, trusted_provider_public_key=attestor.public_key_bytes,
    )
    payload = {
        "schema": "tenir-dispatch-command/v1",
        "resource_id": "lab:budget",
        "amount": 7,
        "action_type": "payment",
        "agent_id": "lab-agent",
        "metadata": {"recipient": "recipient-1"},
    }
    issued = authority.admit_reserve_issue("op-audit", "eval", payload, resource_id="lab:budget")
    provider = MockProvider()
    adapter = NativeClaimedDispatchAdapter(authority, provider, attestor)
    return backend, authority, provider, adapter, payload, issued


def dispatch(adapter, payload, **kwargs):
    return adapter.dispatch("op-audit", "idem-audit", payload,
                            action_type="payment", amount=7, agent_id="lab-agent", **kwargs)


def test_authorized_payload_must_bind_actual_provider_amount(tmp_path, monkeypatch):
    backend, authority, provider, adapter, payload, _ = system(tmp_path, monkeypatch)
    result = adapter.dispatch("op-audit", "idem-audit", payload,
                              action_type="other", amount=700, agent_id="other-agent",
                              metadata={"recipient": "different-recipient"})
    with backend._connection() as db:
        committed = db.execute("SELECT committed FROM resources WHERE resource_id='lab:budget'").fetchone()[0]
    sent_amount = provider.calls[0]["amount"] if provider.calls else None
    print(json.dumps({"result": result["status"], "sent_amount": sent_amount,
                      "authorized_amount": payload["amount"], "i8_committed": committed}))
    assert provider.calls == [], "changed provider command must be rejected before execution"


def test_public_release_must_prevent_subsequent_positive_dispatch(tmp_path, monkeypatch):
    backend, authority, provider, adapter, payload, issued = system(tmp_path, monkeypatch)
    released = backend.release(issued["reservation_id"])
    assert released.status == "POSITIVE_AUTHORITY_OWNS_RESERVATION"
    with backend._connection() as db:
        assert db.execute(
            "SELECT state FROM reservations WHERE reservation_id=?",
            (issued["reservation_id"],),
        ).fetchone()[0] == "RESERVED"
        db.execute(
            "UPDATE reservations SET state='RELEASED' WHERE reservation_id=?",
            (issued["reservation_id"],),
        )
    result = dispatch(adapter, payload)
    with backend._connection() as db:
        budget = dict(db.execute("SELECT reserved, committed FROM resources WHERE resource_id='lab:budget'").fetchone())
    print(json.dumps({"release": released.status, "dispatch": result["status"],
                      "execute_calls": len(provider.calls), "budget": budget}))
    assert provider.calls == [], "RELEASED I8 reservation must not authorize an external send"


def test_pending_replay_must_not_issue_two_concurrent_sends(tmp_path, monkeypatch):
    _, _, provider, adapter, payload, _ = system(tmp_path, monkeypatch)
    first_entered, release_first = threading.Event(), threading.Event()
    original_execute = provider.execute
    count_lock = threading.Lock()
    count = 0
    results, errors = [], []

    def blocked_execute(**kwargs):
        nonlocal count
        with count_lock:
            count += 1
            current = count
        if current == 1:
            first_entered.set()
            assert release_first.wait(10), "test did not release first provider"
        return original_execute(**kwargs)

    monkeypatch.setattr(provider, "execute", blocked_execute)

    def first_worker():
        try:
            results.append(dispatch(adapter, payload)["status"])
        except BaseException as exc:
            errors.append(repr(exc))

    worker = threading.Thread(target=first_worker)
    worker.start()
    try:
        assert first_entered.wait(10), "first dispatch did not reach provider"
        results.append(dispatch(adapter, payload)["status"])
    finally:
        release_first.set()
        worker.join(10)
    assert not worker.is_alive() and not errors
    print(json.dumps({"execute_calls": len(provider.calls), "results": results}))
    assert len(provider.calls) == 1, "only one caller may send for the pending execution"


def test_review_activation_between_check_and_send_must_fence_dispatch(tmp_path, monkeypatch):
    _, authority, provider, adapter, payload, _ = system(tmp_path, monkeypatch)
    claim = authority.consume_and_claim("op-audit", payload, "idem-audit")
    original_check = authority.requires_manual_review

    def interleave_review(operation_key):
        observed = original_check(operation_key)
        authority.mark_dispatch_review(claim, "review committed after caller read")
        return observed

    monkeypatch.setattr(authority, "requires_manual_review", interleave_review)
    result = dispatch(adapter, payload)
    print(json.dumps({"result": result["status"], "review": original_check("op-audit"),
                      "execute_calls": len(provider.calls)}))
    assert provider.calls == [], "a committed review latch must fence the subsequent send"


def test_kernel_restore_must_not_create_second_execution_identity(tmp_path, monkeypatch):
    backend, authority, _, _, payload, _ = system(tmp_path, monkeypatch)
    snapshot = sqlite3.connect(str(tmp_path / "kernel-before-claim.sqlite3"))
    try:
        with backend._connection() as db:
            db.backup(snapshot)
        first = authority.consume_and_claim("op-audit", payload, "idem-audit")
        with backend._connection() as db:
            snapshot.backup(db)
    finally:
        snapshot.close()
    reopened = NativePositivePermitAuthority(backend)
    second = reopened.consume_and_claim("op-audit", payload, "idem-audit")
    print(json.dumps({"first_execution": first["execution_id"], "second_execution": second["execution_id"],
                      "first_witness_revision": first["witness"]["record"]["revision"],
                      "second_witness_revision": second["witness"]["record"]["revision"]}))
    assert first["execution_id"] == second["execution_id"], "witness must fence a restored consumed approval"


def test_witness_must_bind_its_subject_to_the_current_claim(tmp_path, monkeypatch):
    _, authority, _, _, payload, _ = system(tmp_path, monkeypatch)
    unrelated = authority.witness.issue("unrelated-operation", "f" * 64)
    monkeypatch.setattr(authority.witness, "issue", lambda *_args, **_kwargs: unrelated)
    claim = authority.consume_and_claim("op-audit", payload, "idem-audit")
    accepted = authority.verify_dispatch_claim("op-audit", "idem-audit", payload, claim)
    print(json.dumps({"witness_subject": claim["witness"]["record"]["subject"],
                      "expected_subject": "op-audit:idem-audit", "accepted": accepted}))
    assert not accepted, "an authentic witness record for another subject must not authorize this claim"


def test_expiry_must_be_rechecked_after_transaction_lock_acquisition(tmp_path, monkeypatch):
    import w2_integration.native_positive_permit as native_module
    backend, authority, _, _, payload, _ = system(tmp_path, monkeypatch)
    with backend._connection() as db:
        expiry = datetime.fromisoformat(db.execute("SELECT expires_at FROM w2_positive_v1_permits").fetchone()[0])

    class Clock:
        current = expiry - timedelta(seconds=1)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    original_tx = backend._transaction

    @contextmanager
    def transaction_after_wait(operation):
        # Model time advancing during BEGIN IMMEDIATE's lock wait without a sleep.
        Clock.current = expiry + timedelta(seconds=1)
        with original_tx(operation) as db:
            yield db

    monkeypatch.setattr(native_module, "datetime", Clock)
    monkeypatch.setattr(backend, "_transaction", transaction_after_wait)
    with pytest.raises(RuntimeError, match="expired"):
        authority.consume_and_claim("op-audit", payload, "idem-audit")


def test_default_proof_provider_rejects_unclaimed_execution(tmp_path):
    provider = ProofProvider(str(tmp_path / "provider.sqlite3"))
    try:
        with pytest.raises(RuntimeError, match="authenticated kernel claim required"):
            provider.execute("direct", "direct-idem", {"amount": 7})
        assert provider.execute_count("direct") == 0
    finally:
        provider.conn.close()


def test_review_latch_blocks_sequential_dispatch_and_cannot_be_deleted(tmp_path, monkeypatch):
    backend, authority, provider, adapter, payload, _ = system(tmp_path, monkeypatch)
    claim = authority.consume_and_claim("op-audit", payload, "idem-audit")
    authority.mark_dispatch_review(claim, "audit review")
    assert dispatch(adapter, payload)["status"] == "HOLD"
    assert provider.calls == []
    with backend._connection() as db:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM w2_positive_v1_events WHERE event='dispatch_review'")


def test_real_sqlite_lock_wait_must_not_accept_an_expired_permit(tmp_path, monkeypatch):
    import w2_integration.native_positive_permit as native_module

    class ShortLivedIssueClock:
        @classmethod
        def now(cls, tz=None):
            return datetime.now(timezone.utc) - timedelta(minutes=5) + timedelta(seconds=2)

    monkeypatch.setattr(native_module, "datetime", ShortLivedIssueClock)
    backend, authority, _, _, payload, _ = system(tmp_path, monkeypatch)
    monkeypatch.setattr(native_module, "datetime", datetime)
    with backend._connection() as db:
        expiry = datetime.fromisoformat(db.execute("SELECT expires_at FROM w2_positive_v1_permits").fetchone()[0])
    starting_transaction = threading.Event()
    real_transaction = backend._transaction

    @contextmanager
    def observed_transaction(operation):
        # consume_and_claim has already captured now before reaching this hook.
        starting_transaction.set()
        with real_transaction(operation) as db:
            yield db

    monkeypatch.setattr(backend, "_transaction", observed_transaction)
    result, failures = [], []

    def claimant():
        try:
            result.append(authority.consume_and_claim("op-audit", payload, "idem-audit"))
        except Exception as exc:
            failures.append(str(exc))

    with backend._connection() as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        worker = threading.Thread(target=claimant)
        worker.start()
        try:
            assert starting_transaction.wait(10)
            assert datetime.now(timezone.utc) < expiry, "test did not start before expiry"
            assert worker.is_alive(), "claimant did not wait for the SQLite lock"
            remaining = (expiry - datetime.now(timezone.utc)).total_seconds() + 0.2
            time.sleep(max(0, remaining))
        finally:
            blocker.rollback()
        worker.join(10)
    assert not worker.is_alive()
    print(json.dumps({"expired_at": expiry.isoformat(), "observed_after_lock": datetime.now(timezone.utc).isoformat(),
                      "claim_issued": bool(result), "failures": failures}))
    assert not result and any("expired" in text for text in failures), "real writer-lock wait must recheck expiry"
