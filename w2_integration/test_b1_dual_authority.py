"""Experimental dual authority tests: no external provider or real payment."""
from copy import deepcopy
from contextlib import closing, contextmanager
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import threading
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from b1_approved_positive_uid_service import canonical_approval
from b1_dual_authority import DualApprovedUidService, GovernorReceiptIssuer
from kernel_claim_provider import KernelClaimProofProvider
from native_positive_g4a import NativePositiveG4A
from native_positive_permit import NativePositivePermitAuthority
from tenir_governance.composition.governor import DualKernelGovernor, ScoringKernelAdapter
from tenir_governance.membrane import TransitionMembrane, MembraneState, Agent, Resource
from tenir_governance.operations.registry import DurableOperationRegistry, OperationState
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from tenir_kernel.kernel import TenirKernel
from w2_2b_canonical import request_hash


def _crash_issuer_worker(native_path, registry_path, request, checkpoint):
    backend = SQLiteConsensusBackend(native_path)
    authority = NativePositivePermitAuthority(backend)
    registry = DurableOperationRegistry(registry_path)
    state = MembraneState()
    state.register_resource(Resource(resource_id="budget", capacity=50))
    state.register_agent(Agent(agent_id="alice", authority_score=0.9))
    governor = DualKernelGovernor(ScoringKernelAdapter(TenirKernel()), TransitionMembrane(state), registry=registry)
    independent = Ed25519PrivateKey.from_private_bytes(bytes(range(96, 128)))
    issuer = GovernorReceiptIssuer(
        governor, Ed25519PrivateKey.from_private_bytes(bytes(range(128, 160))),
        independent.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw), receipt_authority=authority,
        policy_context={"pressure": 0.2, "velocity": 0.2, "capacity": 0.9,
                        "authority": 0.9, "context_fit": 1.0, "evidence_quality": 1.0},
        allowed_decisions=("PASS", "FLAG"))
    if checkpoint == "before_commit":
        def stop(operation):
            if operation == "b1_governor_receipt":
                os._exit(79)
        backend._before_commit = stop
    else:
        original = authority._write

        @contextmanager
        def stop_after_commit(operation):
            with original(operation) as db:
                yield db
            if operation == "b1_governor_receipt":
                os._exit(79)
        authority._write = stop_after_commit
    issuer.issue(request)
    raise AssertionError("crash checkpoint not reached")


def _after_claim_worker(native_path, registry_path, independent_public, governor_public, policy_context_hash, request, receipt,
                        claimed, proceed, results):
    try:
        backend = SQLiteConsensusBackend(native_path)
        service = object.__new__(DualApprovedUidService)
        service.authority = NativePositivePermitAuthority(backend)
        service.gate = NativePositiveG4A(service.authority)
        service.provider = KernelClaimProofProvider(db_path=native_path + "/provider.db",
                                                   claim_verifier=service.authority.verify_dispatch_claim)
        service._initialize_approval_trust(independent_public)
        registry = DurableOperationRegistry(registry_path)
        service._initialize_governor_trust(governor_public, registry, policy_context_hash=policy_context_hash,
                                           allowed_decisions=("PASS", "FLAG"))
        original = service.authority.consume_and_claim
        def wait_after_claim(*args, **kwargs):
            claim = original(*args, **kwargs)
            claimed.set()
            if not proceed.wait(30):
                raise RuntimeError("quarantine gate timeout")
            return claim
        service.authority.consume_and_claim = wait_after_claim
        try:
            service._dispatch({**request, "governor_receipt": receipt})
            results.put("unexpected-send")
        except Exception as exc:
            results.put(type(exc).__name__ + ":" + str(exc))
        finally:
            service.provider.conn.close()
    except Exception as exc:
        results.put("setup:" + type(exc).__name__ + ":" + str(exc))
        claimed.set()


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())
    monkeypatch.setenv("TENIR_B3_PRIVATE_KEY_HEX", bytes(range(64, 96)).hex())
    independent = Ed25519PrivateKey.from_private_bytes(bytes(range(96, 128)))
    governor_key = Ed25519PrivateKey.from_private_bytes(bytes(range(128, 160)))
    independent_public = independent.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    governor_public = governor_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    backend = SQLiteConsensusBackend(str(tmp_path / "native"))
    assert backend.register_resource("budget", 50) == "REGISTERED"
    authority = NativePositivePermitAuthority(backend)
    service = object.__new__(DualApprovedUidService)
    service.authority = authority
    service.gate = NativePositiveG4A(authority)
    service.provider = KernelClaimProofProvider(claim_verifier=authority.verify_dispatch_claim)
    service._initialize_approval_trust(independent_public)
    registry = DurableOperationRegistry(str(tmp_path / "governor.json"))
    state = MembraneState()
    state.register_resource(Resource(resource_id="budget", capacity=50))
    state.register_agent(Agent(agent_id="alice", authority_score=0.9))
    governor = DualKernelGovernor(ScoringKernelAdapter(TenirKernel()), TransitionMembrane(state), registry=registry)
    issuer = GovernorReceiptIssuer(governor, governor_key, independent_public, receipt_authority=authority,
                                   policy_context={"pressure": 0.2, "velocity": 0.2, "capacity": 0.9,
                                                   "authority": 0.9, "context_fit": 1.0, "evidence_quality": 1.0},
                                   allowed_decisions=("PASS", "FLAG"))
    service._initialize_governor_trust(governor_public, registry, policy_context_hash=issuer.policy_context_hash,
                                       allowed_decisions=("PASS", "FLAG"))
    command = {"schema": "tenir-dispatch-command/v1", "resource_id": "budget", "amount": 7,
               "action_type": "payment", "agent_id": "alice", "metadata": {"recipient": "synthetic"}}
    now = int(time.time())
    body = {"schema": "tenir.b1.approval.v1", "audience": "positive-uid-laboratory",
            "approval_id": "approval", "nonce": "nonce", "operation_key": "lab:dual",
            "evaluation_id": "eval", "idempotency_key": "lab:dual", "request_hash": request_hash(command),
            "issued_at": now, "expires_at": now + 300, "key_id": hashlib.sha256(independent_public).hexdigest()}
    request = {"schema": "tenir.positive.approved.uid-lab.v1", "action": "submit",
               "operation_key": "lab:dual", "evaluation_id": "eval", "idempotency_key": "lab:dual",
               "command": command, "approval": {**body, "signature": independent.sign(canonical_approval(body)).hex()}}
    yield backend, service, registry, issuer, request
    service.provider.conn.close()


def test_real_governor_allowed_flag_and_independent_signature_are_both_required(system):
    backend, service, registry, issuer, request = system
    with pytest.raises(RuntimeError, match="Governor receipt required"):
        service._dispatch(request)
    assert backend.get_state("budget").reserved == 0
    receipt = issuer.issue(request)
    assert receipt["decision"] == "FLAG"
    assert registry.get("lab:dual").state == OperationState.RESERVED
    assert service._dispatch({**request, "governor_receipt": receipt})["status"] == "EXECUTED"
    assert service.provider.execute_count("lab:dual") == 1
    assert backend.get_state("budget").reserved == 7


def test_expiry_after_claim_blocks_send(system, monkeypatch):
    backend, service, _, issuer, request = system
    receipt = issuer.issue(request)
    original = service.authority.consume_and_claim

    def expire_after_claim(*args, **kwargs):
        claim = original(*args, **kwargs)
        monkeypatch.setattr("b1_dual_authority.time.time", lambda: request["approval"]["expires_at"])
        return claim

    monkeypatch.setattr(service.authority, "consume_and_claim", expire_after_claim)
    with pytest.raises(RuntimeError, match="expired"):
        service._dispatch({**request, "governor_receipt": receipt})
    assert service.provider.execute_count("lab:dual") == 0
    assert backend.get_state("budget").reserved == 7
    with backend._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_events WHERE event='dispatch_send_granted'").fetchone()[0] == 0


def test_quarantine_cycle_during_adjudication_cannot_issue_new_epoch_receipt(system, monkeypatch):
    backend, service, registry, issuer, request = system
    original = issuer.governor.adjudicate

    def verifier(*, authorized_by, scope, authorization, reason, lock_id):
        return {"authorized": True, "principal": authorized_by, "scope": scope,
                "lock_id": lock_id, "authorization_id": "lab-release"}

    registry._quarantine_release_verifier = verifier

    def cycle_after_adjudication(*args, **kwargs):
        result = original(*args, **kwargs)
        registry.set_quarantine(True, locked_by="lab", incident_id="cycle")
        assert registry.authorized_release_quarantine(
            authorized_by="lab", scope="lab", authorization="lab-proof")["released"]
        return result

    monkeypatch.setattr(issuer.governor, "adjudicate", cycle_after_adjudication)
    with pytest.raises(RuntimeError, match="epoch changed"):
        issuer.issue(request)
    assert backend.get_state("budget").reserved == 0
    assert service.provider.execute_count("lab:dual") == 0
    with backend._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM b1_governor_receipts").fetchone()[0] == 0


def test_expiry_while_acquiring_send_transaction_blocks_send(system, monkeypatch):
    backend, service, _, issuer, request = system
    receipt = issuer.issue(request)
    original = service.authority._write

    @contextmanager
    def delayed_write(operation):
        with original(operation) as db:
            if operation == "positive_g4a_consume":
                monkeypatch.setattr("b1_dual_authority.time.time", lambda: request["approval"]["expires_at"])
            yield db

    monkeypatch.setattr(service.authority, "_write", delayed_write)
    with pytest.raises(RuntimeError, match="expired before send"):
        service._dispatch({**request, "governor_receipt": receipt})
    assert service.provider.execute_count("lab:dual") == 0
    assert backend.get_state("budget").reserved == 7
    with backend._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_events WHERE event='dispatch_send_granted'").fetchone()[0] == 0


@pytest.mark.parametrize("field,value", [
    ("schema", "another-schema"), ("audience", "another-service"),
    ("operation_key", "another-operation"), ("evaluation_id", "another-evaluation"),
    ("key_id", "0" * 64), ("unexpected", True),
])
def test_stored_receipt_context_tampering_cannot_replay(system, field, value):
    backend, _, _, issuer, request = system
    receipt = issuer.issue(request)
    receipt[field] = value
    body = {key: item for key, item in receipt.items() if key != "signature"}
    receipt["signature"] = issuer._key.sign(canonical_approval(body)).hex()
    # Privileged corruption fixture: remove the append-only trigger explicitly.
    # A real compromised signer/writer is outside this lab's trust boundary.
    with backend._connection() as db:
        db.execute("DROP TRIGGER b1_governor_receipts_no_update")
        db.execute("UPDATE b1_governor_receipts SET receipt_json=?", (json.dumps(receipt),))
    with pytest.raises(RuntimeError, match="stored Governor receipt"):
        issuer.issue(request)


def test_receipts_are_append_only(system):
    backend, _, _, issuer, request = system
    issuer.issue(request)
    with backend._connection() as db:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("UPDATE b1_governor_receipts SET request_hash='other'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM b1_governor_receipts")


def test_governor_receipt_replay_does_not_readjudicate(system, monkeypatch):
    _, _, _, issuer, request = system
    receipt = issuer.issue(request)
    def forbidden(*args, **kwargs):
        raise AssertionError("replay must not call Governor again")
    monkeypatch.setattr(issuer.governor, "adjudicate", forbidden)
    assert issuer.issue(request) == receipt


def test_failure_after_governor_adjudication_stays_fail_closed(system, monkeypatch):
    backend, service, registry, issuer, request = system
    def fail(operation):
        if operation == "b1_governor_receipt":
            raise RuntimeError("injected receipt persistence failure")
    monkeypatch.setattr(backend, "_before_commit", fail)
    with pytest.raises(RuntimeError, match="injected receipt persistence failure"):
        issuer.issue(request)
    assert registry.get("lab:dual").state == OperationState.RESERVED
    with backend._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM b1_governor_receipts").fetchone()[0] == 0
    monkeypatch.setattr(backend, "_before_commit", lambda _: None)
    with pytest.raises(RuntimeError, match="requires recovery"):
        issuer.issue(request)
    assert backend.get_state("budget").reserved == 0
    assert service.provider.execute_count("lab:dual") == 0


@pytest.mark.parametrize("checkpoint", ["before_commit", "after_commit"])
def test_process_exit_at_receipt_persistence_recovers_without_readjudication(system, monkeypatch, checkpoint):
    backend, service, registry, issuer, request = system
    worker = multiprocessing.get_context("spawn").Process(
        target=_crash_issuer_worker, args=(backend._store_dir, registry._store_path, request, checkpoint))
    worker.start()
    try:
        worker.join(30)
        assert worker.exitcode == 79, "issuer did not exit at the requested receipt boundary"
    finally:
        if worker.is_alive():
            worker.terminate()
        worker.join(10)
    assert registry.get("lab:dual").state == OperationState.RESERVED
    with backend._connection() as db:
        rows = db.execute("SELECT receipt_json FROM b1_governor_receipts").fetchall()
    assert len(rows) == (0 if checkpoint == "before_commit" else 1)

    def forbidden(*args, **kwargs):
        raise AssertionError("recovery must not adjudicate again")
    monkeypatch.setattr(issuer.governor, "adjudicate", forbidden)
    if checkpoint == "before_commit":
        with pytest.raises(RuntimeError, match="requires recovery"):
            issuer.issue(request)
    else:
        assert issuer.issue(request) == json.loads(rows[0]["receipt_json"])
    assert backend.get_state("budget").reserved == 0
    assert service.provider.execute_count("lab:dual") == 0


def test_forged_independent_approval_never_adjudicates(system):
    backend, service, registry, issuer, request = system
    invalid = deepcopy(request)
    invalid["approval"]["signature"] = "00" * 64
    with pytest.raises(RuntimeError, match="signature refused"):
        issuer.issue(invalid)
    assert registry.get("lab:dual") is None
    assert backend.get_state("budget").reserved == 0
    assert service.provider.execute_count("lab:dual") == 0


def test_flag_requires_explicit_operator_policy(system):
    backend, service, registry, issuer, request = system
    strict = GovernorReceiptIssuer(issuer.governor, issuer._key, issuer._independent_public,
                                   receipt_authority=issuer._receipt_authority,
                                   policy_context=issuer._context)
    with pytest.raises(RuntimeError, match="allowed decision"):
        strict.issue(request)
    assert registry.get("lab:dual").state == OperationState.RESERVED
    assert backend.get_state("budget").reserved == 0
    assert service.provider.execute_count("lab:dual") == 0


def test_invalid_approval_nonce_does_not_create_governor_record(system):
    _, _, registry, issuer, request = system
    invalid = deepcopy(request)
    invalid["approval"]["nonce"] = ""
    with pytest.raises((ValueError, RuntimeError)):
        issuer.issue(invalid)
    assert registry.get("lab:dual") is None


def test_forged_governor_receipt_never_reserves(system):
    backend, service, _, issuer, request = system
    receipt = issuer.issue(request)
    receipt["signature"] = "00" * 64
    with pytest.raises(RuntimeError, match="signature refused"):
        service._dispatch({**request, "governor_receipt": receipt})
    assert backend.get_state("budget").reserved == 0
    assert service.provider.execute_count("lab:dual") == 0


def test_signed_governor_receipt_for_other_policy_context_is_rejected(system):
    backend, service, _, issuer, request = system
    receipt = issuer.issue(request)
    receipt["policy_context_hash"] = "a" * 64
    body = {name: value for name, value in receipt.items() if name != "signature"}
    receipt["signature"] = issuer._key.sign(canonical_approval(body)).hex()
    with pytest.raises(RuntimeError, match="binding refused"):
        service._dispatch({**request, "governor_receipt": receipt})
    assert backend.get_state("budget").reserved == 0
    assert service.provider.execute_count("lab:dual") == 0


def test_governor_and_independent_approval_keys_must_differ(system):
    _, service, registry, issuer, _ = system
    with pytest.raises(RuntimeError, match="must be distinct"):
        service._initialize_governor_trust(issuer._independent_public, registry,
                                           policy_context_hash=issuer.policy_context_hash,
                                           allowed_decisions=("PASS", "FLAG"))


def test_shared_quarantine_blocks_admission_even_with_both_proofs(system):
    backend, service, registry, issuer, request = system
    receipt = issuer.issue(request)
    registry.set_quarantine(True, locked_by="lab", incident_id="incident")
    with pytest.raises(RuntimeError, match="quarantine active"):
        service._dispatch({**request, "governor_receipt": receipt})
    assert backend.get_state("budget").reserved == 0
    assert service.provider.execute_count("lab:dual") == 0


def test_quarantine_release_does_not_revalidate_old_governor_receipt(system):
    backend, service, registry, issuer, request = system
    receipt = issuer.issue(request)
    def verifier(*, authorized_by, scope, authorization, reason, lock_id):
        return {"authorized": True, "principal": authorized_by, "scope": scope,
                "lock_id": lock_id, "authorization_id": "lab-release"}
    registry._quarantine_release_verifier = verifier
    registry.set_quarantine(True, locked_by="lab", incident_id="incident")
    released = registry.authorized_release_quarantine(authorized_by="lab", scope="lab", authorization="lab-proof")
    assert released["released"] is True
    assert not registry.quarantine_is_active()
    with pytest.raises(RuntimeError, match="quarantine epoch changed"):
        service._dispatch({**request, "governor_receipt": receipt})
    assert backend.get_state("budget").reserved == 0
    assert service.provider.execute_count("lab:dual") == 0


def test_changed_governor_trust_is_detected_before_admission(system):
    backend, service, _, issuer, request = system
    receipt = issuer.issue(request)
    with backend._connection() as db:
        db.execute("UPDATE b1_governor_trust SET decisions='[]' WHERE singleton=1")
    with pytest.raises(RuntimeError, match="trust key or policy changed"):
        service._dispatch({**request, "governor_receipt": receipt})
    assert backend.get_state("budget").reserved == 0
    assert service.provider.execute_count("lab:dual") == 0


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_missing_or_corrupt_governor_registry_blocks_admission(system, damage):
    backend, service, registry, issuer, request = system
    receipt = issuer.issue(request)
    if damage == "missing":
        Path(registry._store_path).unlink()
    else:
        Path(registry._store_path).write_text("{", encoding="utf-8")
    with pytest.raises(RuntimeError, match="registry unavailable or corrupt"):
        service._dispatch({**request, "governor_receipt": receipt})
    assert backend.get_state("budget").reserved == 0
    assert service.provider.execute_count("lab:dual") == 0


def test_quarantine_after_claim_blocks_send_without_releasing_reservation(system, monkeypatch):
    backend, service, registry, issuer, request = system
    receipt = issuer.issue(request)
    original = service.authority.consume_and_claim
    def lock_after_claim(*args, **kwargs):
        claim = original(*args, **kwargs)
        registry.set_quarantine(True, locked_by="lab", incident_id="incident")
        return claim
    monkeypatch.setattr(service.authority, "consume_and_claim", lock_after_claim)
    with pytest.raises(RuntimeError, match="quarantine active"):
        service._dispatch({**request, "governor_receipt": receipt})
    assert backend.get_state("budget").reserved == 7
    assert service.provider.execute_count("lab:dual") == 0
    with backend._connection() as db:
        assert db.execute("SELECT status FROM w2_positive_v1_outbox WHERE operation_key='lab:dual'").fetchone()[0] == "PENDING"
        assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_events WHERE event='dispatch_send_granted'").fetchone()[0] == 0


def test_cross_process_quarantine_after_claim_blocks_send(system):
    backend, service, registry, issuer, request = system
    receipt = issuer.issue(request)
    independent_public = issuer._independent_public
    governor_public = issuer._key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ctx = multiprocessing.get_context("spawn")
    claimed, proceed, results = ctx.Event(), ctx.Event(), ctx.Queue()
    worker = ctx.Process(target=_after_claim_worker, args=(backend._store_dir, registry._store_path,
                                                           independent_public, governor_public, issuer.policy_context_hash, request,
                                                           receipt, claimed, proceed, results))
    worker.start()
    try:
        assert claimed.wait(30), "worker never reached the post-claim barrier"
        registry.set_quarantine(True, locked_by="lab", incident_id="cross-process-incident")
        assert registry.quarantine_is_active()
        proceed.set()
        result = results.get(timeout=30)
        worker.join(10)
        assert worker.exitcode == 0
        assert "quarantine active" in result, result
        with backend._connection() as db:
            assert db.execute("SELECT status FROM w2_positive_v1_outbox WHERE operation_key='lab:dual'").fetchone()[0] == "PENDING"
            assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_events WHERE event='dispatch_send_granted'").fetchone()[0] == 0
        assert backend.get_state("budget").reserved == 7
        with closing(sqlite3.connect(backend._store_dir + "/provider.db")) as db:
            assert db.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 0
    finally:
        proceed.set()
        if worker.is_alive():
            worker.terminate()
        worker.join(10)
        results.close()


def test_emergency_quarantine_does_not_wait_for_already_started_provider(system, monkeypatch):
    _, service, registry, issuer, request = system
    receipt = issuer.issue(request)
    entered, release, locked = threading.Event(), threading.Event(), threading.Event()
    original = service.provider.execute
    results = []
    def blocked_provider(*args, **kwargs):
        entered.set()
        if not release.wait(10):
            raise RuntimeError("test provider timeout")
        return original(*args, **kwargs)
    def run():
        try:
            results.append(service._dispatch({**request, "governor_receipt": receipt})["status"])
        except Exception as exc:
            results.append(type(exc).__name__ + ":" + str(exc))
    def emergency():
        registry.set_quarantine(True, locked_by="lab", incident_id="incident")
        locked.set()
    monkeypatch.setattr(service.provider, "execute", blocked_provider)
    dispatch_thread = threading.Thread(target=run)
    lock_thread = threading.Thread(target=emergency)
    try:
        dispatch_thread.start()
        assert entered.wait(10)
        lock_thread.start()
        assert locked.wait(3), "external provider blocked quarantine activation"
    finally:
        release.set()
        dispatch_thread.join(10)
        if lock_thread.ident is not None:
            lock_thread.join(10)
    assert results == ["EXECUTED"]
    assert service.provider.execute_count("lab:dual") == 1
