"""Signed-approval negative controls; these do not authorize real effects."""
from copy import deepcopy
from contextlib import closing
import hashlib
import multiprocessing
import sqlite3
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from b1_approved_positive_uid_service import ApprovedPositiveUidService, canonical_approval
from kernel_claim_provider import KernelClaimProofProvider
from native_positive_g4a import NativePositiveG4A
from native_positive_permit import NativePositivePermitAuthority
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from w2_2b_canonical import request_hash


def _compete_approval(path, public, request, ready, start, results):
    try:
        authority = NativePositivePermitAuthority(SQLiteConsensusBackend(path))
        service = object.__new__(ApprovedPositiveUidService)
        service.authority = authority
        service.gate = NativePositiveG4A(authority)
        service.provider = KernelClaimProofProvider(db_path=path + "/provider.db", claim_verifier=authority.verify_dispatch_claim)
        service._initialize_approval_trust(public)
        ready.put(True)
        if not start.wait(30):
            raise RuntimeError("start gate timeout")
        try:
            service._dispatch(request)
            results.put("sent")
        except Exception as exc:
            results.put(type(exc).__name__ + ":" + str(exc))
        finally:
            service.provider.conn.close()
    except Exception as exc:
        results.put("setup:" + type(exc).__name__ + ":" + str(exc))


@pytest.fixture
def lab(tmp_path, monkeypatch):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())
    monkeypatch.setenv("TENIR_B3_PRIVATE_KEY_HEX", bytes(range(64, 96)).hex())
    backend = SQLiteConsensusBackend(str(tmp_path))
    assert backend.register_resource("budget", 50) == "REGISTERED"
    service = object.__new__(ApprovedPositiveUidService)
    service.authority = NativePositivePermitAuthority(backend)
    service.gate = NativePositiveG4A(service.authority)
    service.provider = KernelClaimProofProvider(claim_verifier=service.authority.verify_dispatch_claim)
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(96, 128)))
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    service._initialize_approval_trust(public)
    command = {"schema": "tenir-dispatch-command/v1", "resource_id": "budget", "amount": 7,
               "action_type": "payment", "agent_id": "agent", "metadata": {"recipient": "target"}}
    req = {"schema": "tenir.positive.approved.uid-lab.v1", "action": "submit", "operation_key": "op",
           "evaluation_id": "eval", "idempotency_key": "idem", "command": command}
    def sign(changes=None, signer=key):
        now = int(time.time())
        body = {"schema": "tenir.b1.approval.v1", "audience": "positive-uid-laboratory",
                "approval_id": "approval-1", "nonce": "nonce-1", "operation_key": req["operation_key"],
                "evaluation_id": req["evaluation_id"], "idempotency_key": req["idempotency_key"],
                "request_hash": request_hash(req["command"]), "issued_at": now, "expires_at": now + 300,
                "key_id": hashlib.sha256(public).hexdigest()}
        body.update(changes or {})
        return {**body, "signature": signer.sign(canonical_approval(body)).hex()}
    yield backend, service, req, sign
    service.provider.conn.close()


def test_signed_exact_approval_executes_only_synthetic_effect_and_exact_replay(lab):
    backend, service, req, sign = lab
    authorized = {**req, "approval": sign()}
    assert service._dispatch(authorized)["status"] == "EXECUTED"
    assert service._dispatch(authorized)["status"] == "UNKNOWN"
    assert service.provider.execute_count("op") == 1
    assert backend.get_state("budget").reserved == 7


@pytest.mark.parametrize("mutation", ["missing", "forged", "payload", "operation", "evaluation", "idempotency", "audience", "expired"])
def test_unapproved_or_substituted_request_fails_before_reservation(lab, mutation):
    backend, service, req, sign = lab
    req = {**deepcopy(req), "approval": sign()}
    if mutation == "missing":
        del req["approval"]
    elif mutation == "forged":
        req["approval"] = sign(signer=Ed25519PrivateKey.from_private_bytes(bytes(range(32))))
    elif mutation == "payload":
        req["command"]["amount"] = 8
    elif mutation == "operation":
        req["operation_key"] = "other"
    elif mutation == "evaluation":
        req["evaluation_id"] = "other"
    elif mutation == "idempotency":
        req["idempotency_key"] = "other"
    elif mutation == "audience":
        req["approval"] = sign({"audience": "other"})
    else:
        now = int(time.time())
        req["approval"] = sign({"issued_at": now - 60, "expires_at": now - 1})
    with pytest.raises((ValueError, RuntimeError)):
        service._dispatch(req)
    assert service.provider.execute_count("op") == 0
    assert backend.get_state("budget").reserved == 0


def test_nonce_or_approval_id_cannot_be_rebound_to_another_operation(lab):
    backend, service, req, sign = lab
    service._dispatch({**req, "approval": sign()})
    changed = {**deepcopy(req), "operation_key": "other", "idempotency_key": "other-idem"}
    for change in ({"approval_id": "approval-2"}, {"nonce": "nonce-2"}):
        approval = sign({**change, "operation_key": "other", "idempotency_key": "other-idem"})
        with pytest.raises(RuntimeError, match="binding"):
            service._dispatch({**changed, "approval": approval})
    assert backend.get_state("budget").reserved == 7
    assert service.provider.execute_count("other") == 0


def test_failed_native_admission_leaves_exact_binding_but_retry_succeeds(lab, monkeypatch):
    backend, service, req, sign = lab
    authorized = {**req, "approval": sign()}
    def fail(operation):
        if operation == "positive_admit":
            raise RuntimeError("injected admission failure")
    monkeypatch.setattr(backend, "_before_commit", fail)
    with pytest.raises(RuntimeError, match="injected admission failure"):
        service._dispatch(authorized)
    with backend._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM b1_approved_requests").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_permits").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM b1_approved_requests")
    assert backend.get_state("budget").reserved == 0
    assert service.provider.execute_count("op") == 0
    monkeypatch.setattr(backend, "_before_commit", lambda _: None)
    assert service._dispatch(authorized)["status"] == "EXECUTED"
    assert service.provider.execute_count("op") == 1


def test_approval_binding_rolls_back_if_its_transaction_fails(lab, monkeypatch):
    backend, service, req, sign = lab
    def fail(operation):
        if operation == "b1_bind_approval":
            raise RuntimeError("injected approval failure")
    monkeypatch.setattr(backend, "_before_commit", fail)
    with pytest.raises(RuntimeError, match="injected approval failure"):
        service._dispatch({**req, "approval": sign()})
    with backend._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM b1_approved_requests").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_permits").fetchone()[0] == 0
    assert backend.get_state("budget").reserved == 0
    assert service.provider.execute_count("op") == 0


def test_trust_key_cannot_be_changed_for_existing_store(lab):
    _, service, _, _ = lab
    other = Ed25519PrivateKey.from_private_bytes(bytes(range(32))).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    with pytest.raises(RuntimeError, match="trust key changed"):
        service._initialize_approval_trust(other)


def test_expired_replay_observes_unknown_without_a_second_send(lab, monkeypatch):
    _, service, req, sign = lab
    authorized = {**req, "approval": sign()}
    assert service._dispatch(authorized)["status"] == "EXECUTED"
    expiry = authorized["approval"]["expires_at"]
    monkeypatch.setattr("b1_approved_positive_uid_service.time.time", lambda: expiry + 1)
    assert service._dispatch(authorized)["status"] == "UNKNOWN"
    assert service.provider.execute_count("op") == 1


def test_cannot_install_trust_over_unapproved_existing_operations(tmp_path):
    backend = SQLiteConsensusBackend(str(tmp_path))
    backend.register_resource("budget", 50)
    authority = NativePositivePermitAuthority(backend)
    authority.admit_reserve_issue("legacy-op", "legacy-eval", {"amount": 7}, resource_id="budget")
    service = object.__new__(ApprovedPositiveUidService)
    service.authority = authority
    public = Ed25519PrivateKey.from_private_bytes(bytes(range(96, 128))).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    with pytest.raises(RuntimeError, match="existing positive operations"):
        service._initialize_approval_trust(public)


def test_two_processes_cannot_bind_one_nonce_to_different_operations(lab, tmp_path):
    backend, service, req, sign = lab
    public = Ed25519PrivateKey.from_private_bytes(bytes(range(96, 128))).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    service.provider.conn.close()
    service.provider = KernelClaimProofProvider(db_path=str(tmp_path / "provider.db"),
                                                claim_verifier=service.authority.verify_dispatch_claim)
    first = {**req, "approval": sign()}
    second = {**deepcopy(req), "operation_key": "other", "idempotency_key": "other-idem"}
    second["approval"] = sign({"approval_id": "approval-2", "operation_key": "other", "idempotency_key": "other-idem"})
    ctx = multiprocessing.get_context("spawn")
    ready, start, results = ctx.Queue(), ctx.Event(), ctx.Queue()
    processes = [ctx.Process(target=_compete_approval, args=(str(tmp_path), public, request, ready, start, results))
                 for request in (first, second)]
    for process in processes:
        process.start()
    try:
        assert [ready.get(timeout=40) for _ in processes] == [True, True]
        start.set()
        outcomes = [results.get(timeout=45) for _ in processes]
        for process in processes:
            process.join(20)
            assert process.exitcode == 0
        assert outcomes.count("sent") == 1
        assert sum("approval binding conflict" in outcome for outcome in outcomes) == 1
        with backend._connection() as db:
            assert db.execute("SELECT COUNT(*) FROM b1_approved_requests").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_outbox").fetchone()[0] == 1
        assert backend.get_state("budget").reserved == 7
        with closing(sqlite3.connect(str(tmp_path / "provider.db"))) as db:
            assert db.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 1
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(10)
        ready.close()
        results.close()
