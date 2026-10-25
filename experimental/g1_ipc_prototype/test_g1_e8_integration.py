"""INT-E8-01 pre-authorization contract: mock oracle + RED tests for the real G1 prototype.

IMPORTANT: model tests illustrate ordering only. They DO NOT implement a witness,
validate CMT signatures, exercise an independent host, or establish G1/E8 assurance.
Actual-engine tests are explicitly XFAIL until G1 integrates an E8 client.
Run: python -m pytest -q test_g1_e8_integration.py
"""
import base64
import os
import sqlite3

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from unittest.mock import Mock

from g1_ipc import canonical, initialize, process, validate


UID = os.getuid()
KEY = Ed25519PrivateKey.generate()
KEYS = {UID: KEY.public_key()}


def signed_request(*, epoch=5, request_id="int-e8-01", transaction_id="tx-01", payload_digest=None):
    unsigned = {
        "header": {
            "protocol_version": "g1-ipc-v0.1",
            "request_id": request_id,
            "timestamp": 1777218342,
            "realm": "banking-dora-ict",
            "epoch": epoch,
            "operation": "COMMIT",
        },
        "auth": {"worker_uid": UID, "grant_signature": "NOT_IMPLEMENTED"},
        "payload": {
            "transaction_id": transaction_id,
            "state_transition": {"from": "PENDING", "to": "COMMITTED"},
            "payload_digest": payload_digest or "sha256:" + "0" * 64,
        },
    }
    signature = KEY.sign(
        b"TENIR-G1-IPC-ENVELOPE-v0.1\x00"
        + canonical(unsigned)
    )
    return dict(unsigned, envelope_signature="ed25519:" + base64.b64encode(signature).decode())


def rows(db):
    with sqlite3.connect(db) as con:
        return (
            con.execute("SELECT count(*) FROM g1_local_commits").fetchone()[0],
            con.execute("SELECT count(*) FROM g1_idempotency_ledger").fetchone()[0],
        )


class _ReferenceGate:
    """TEST-ONLY shim: illustrates the contract, NOT the deployed daemon or real E8.

    In this model, Mock.pre_auth_check is called before the existing local
    SQLite process. No CMT cryptography, CAS, one-time grant or durable HOLD.
    Never count this shim as INT-E8-01 integration evidence.
    """

    def __init__(self, db, witness, effect_calls):
        self.db, self.witness, self.effect_calls = db, witness, effect_calls
        self.events = []

    def submit(self, request):
        header, payload = validate(request, UID, KEYS)
        try:
            decision = self.witness.pre_auth_check(
                realm=header["realm"],
                epoch=header["epoch"],
                request_id=header["request_id"],
                transaction_id=payload["transaction_id"],
                payload_digest=payload["payload_digest"],
            )
        except (TimeoutError, ConnectionError):
            self.events.append("WITNESS_OUTCOME_UNKNOWN")
            return {"decision": "HOLD_GLOBAL", "reason": "WITNESS_OUTCOME_UNKNOWN"}
        if decision["status"] != "APPROVED":
            code = decision["reason"]
            self.events.append(code)
            return {"decision": "HARD_HOLD", "reason": code}
        # NOTE: This is NOT sufficient for production. A real G1 engine must
        # verify externally pinned witness signatures and atomically bind a
        # single-use authorization to this operation and the authoritative head.
        return process(self.db, request, UID, KEYS)


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "g1_e8.sqlite")
    initialize(path)
    return path


def test_model_rejects_rollback_before_business_write(db):
    """Mock contract demonstration only; NOT a G1↔E8 integration PASS."""
    witness = Mock()
    witness.pre_auth_check.return_value = {
        "status": "REJECTED", "reason": "E8_ROLLBACK_DETECTED", "cmt": None
    }
    effect_calls = []
    gate = _ReferenceGate(db, witness, effect_calls)
    reply = gate.submit(signed_request(epoch=4))

    assert reply == {"decision": "HARD_HOLD", "reason": "E8_ROLLBACK_DETECTED"}
    assert rows(db) == (0, 0)  # no business or idempotency mutation
    assert effect_calls == []  # no provider effects
    assert gate.events == ["E8_ROLLBACK_DETECTED"]
    witness.pre_auth_check.assert_called_once()
    assert witness.pre_auth_check.call_args.kwargs["epoch"] == 4


def test_model_rejects_equal_epoch_divergence(db):
    witness = Mock()
    witness.pre_auth_check.return_value = {
        "status": "REJECTED", "reason": "E8_HEAD_DIVERGENCE", "cmt": None
    }
    gate = _ReferenceGate(db, witness, [])
    assert gate.submit(signed_request(epoch=5, payload_digest="sha256:" + "f"*64))["decision"] == "HARD_HOLD"
    assert rows(db) == (0, 0)


def test_model_witness_timeout_fails_closed_without_commit(db):
    witness = Mock()
    witness.pre_auth_check.side_effect = TimeoutError("injected witness timeout")
    gate = _ReferenceGate(db, witness, [])
    assert gate.submit(signed_request()) == {
        "decision": "HOLD_GLOBAL", "reason": "WITNESS_OUTCOME_UNKNOWN"
    }
    assert rows(db) == (0, 0)


def test_model_pre_auth_observed_before_business_write(db):
    witness = Mock()
    def witness_approves(**kwargs):
        assert rows(db) == (0, 0), "pre-auth must precede the business commit"
        return {"status": "APPROVED", "reason": "OK", "cmt": "MODEL_ONLY_UNVERIFIED"}
    witness.pre_auth_check.side_effect = witness_approves
    gate = _ReferenceGate(db, witness, [])
    reply = gate.submit(signed_request())
    assert reply["decision"] == "COMMITTED"
    assert rows(db) == (1, 1)
    assert gate.effect_calls == []
    # Do NOT interpret this as validation of the CMT or witness independence.


@pytest.mark.xfail(strict=True, reason="RED: real g1_ipc.process has no E8 pre-authorization gate")
def test_actual_engine_must_hold_on_rollback(db):
    """Contract for G1 integration: currently FAILS because real process commits."""
    # A witness refusal MUST be enforced by the actual engine, not a wrapping
    # test shim. This test intentionally bypasses the model.
    reply = process(db, signed_request(epoch=4), UID, KEYS)
    assert reply["decision"] == "HARD_HOLD"
    assert rows(db) == (0, 0)


@pytest.mark.xfail(strict=True, reason="RED: real g1_ipc.process accepts divergent state without E8")
def test_actual_engine_must_hold_on_divergent_head(db):
    reply = process(db, signed_request(epoch=5, payload_digest="sha256:" + "f"*64), UID, KEYS)
    assert reply["decision"] == "HARD_HOLD"
    assert rows(db) == (0, 0)
