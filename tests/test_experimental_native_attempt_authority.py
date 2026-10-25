"""Slice 2: cryptographically bound native SQLite attempts, no dispatch."""
from __future__ import annotations

import multiprocessing
import os
import sqlite3
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tenir_governance.operations.experimental_native_attempt_authority import (
    ExperimentalNativeAttemptAuthority,
)
from tenir_governance.operations.experimental_native_sqlite_authority import SqliteAuthorityHold
from tenir_governance.operations.native_attempt_attestation import (
    Ed25519AttemptVerifier, canonical_attestation_payload,
)
from tenir_governance.operations.registry import OperationRecord,OperationState
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend

NONCE="abcdef0123456789abcdef0123456789"


def _public(private):
    return private.public_key().public_bytes(
        serialization.Encoding.Raw,serialization.PublicFormat.Raw)


def _source():
    return OperationRecord(idempotency_key="lab:op-1",passage_id="lab:passage-1",
                           agent_id="lab:agent-1",action_type="payment",
                           resource_id="lab:budget",amount=20.0)


def _open(path,public,*,revoked=frozenset(),now=1500):
    backend=SQLiteConsensusBackend(str(path))
    verifier=Ed25519AttemptVerifier(
        {"issuer-1":public},revoked_authorities=revoked,now_ms=lambda:now,
    )
    return backend,ExperimentalNativeAttemptAuthority(
        backend,verifier,experimental_only=True
    )


@pytest.fixture
def candidate(tmp_path):
    path=tmp_path/"single-store"
    private=Ed25519PrivateKey.generate()
    backend,authority=_open(path,_public(private))
    assert backend.register_resource("lab:budget",Decimal("100"))=="REGISTERED"
    authority.admit_created(_source())
    return backend,authority,private,path


def _begin(authority,**overrides):
    args=dict(operation_key="lab:op-1",attempt_id="lab:attempt-1",
              lei="lab:lei-1",nonce=NONCE,deadline_ms=1400)
    args.update(overrides)
    return authority.begin_attempt(**args)


def _signed(private,receipt,*,outcome="EFFECT_CONFIRMED",**changes):
    data=dict(
        schema_version="tenir.attempt.attestation.v1",
        evidence_id="signed-proof-1",
        attempt_id=receipt.attempt_id,
        lei=receipt.lei,
        nonce=NONCE,
        operation_digest=receipt.operation_digest,
        outcome=outcome,
        authority_id="issuer-1",
        issued_at_ms=1000,
        expires_at_ms=2000,
    )
    data.update(changes)
    return {**data,"signature_hex":private.sign(
        canonical_attestation_payload(data)).hex()}


def _budget(backend):
    return backend.get_state("lab:budget")


def test_attempt_binds_real_native_operation_hash_and_reservation(candidate):
    backend,authority,private,path=candidate
    attempt=_begin(authority)
    assert attempt.operation_digest.startswith("sha256:")
    assert attempt.attempt_state=="UNKNOWN"
    assert attempt.decision=="HOLD"
    assert not attempt.execution_authorized
    assert not attempt.budget_settlement_authorized
    assert authority.inspect_attempt(attempt.attempt_id)==attempt
    assert len(authority.events(attempt.attempt_id))==1
    assert _budget(backend).reserved==Decimal("20")
    assert _budget(backend).committed==Decimal("0")


@pytest.mark.parametrize("outcome,expected",[
    ("EFFECT_CONFIRMED","RESOLVED"),
    ("NON_EXECUTION_CONFIRMED","FAILED"),
])
def test_signed_t6_t7_attestation_never_settles_or_dispatches(
        candidate,outcome,expected):
    backend,authority,private,path=candidate
    initial=_begin(authority)
    raw=_signed(private,initial,outcome=outcome)
    after=authority.qualify(initial.attempt_id,raw,observed_ms=1500)
    assert after.attempt_state==expected
    assert after.operation_digest==initial.operation_digest
    assert after.evidence_id=="signed-proof-1"
    assert after.decision=="HOLD"
    assert after.execution_authorized is False
    assert after.budget_settlement_authorized is False
    assert after.is_production_authority is False
    assert authority.qualify(initial.attempt_id,raw,observed_ms=1600)==after
    assert authority.events(after.attempt_id)[-1][0]==expected
    assert len(authority.events(after.attempt_id))==2
    assert _budget(backend).reserved==Decimal("20")
    assert _budget(backend).committed==Decimal("0")
    assert authority.inspect(_source()).operation_state=="RESERVED"


def test_begin_exact_replay_but_no_new_lei_or_attempt(candidate):
    _,authority,private,path=candidate
    created=_begin(authority)
    assert _begin(authority)==created
    assert len(authority.events(created.attempt_id))==1
    for changes in (
        {"attempt_id":"lab:attempt-2"},
        {"lei":"lab:lei-2"},
        {"nonce":"abcdefabcdefabcdefabcdefabcdefab"},
        {"deadline_ms":1401},
    ):
        with pytest.raises(SqliteAuthorityHold,match="already bound"):
            _begin(authority,**changes)
    assert len(authority.events(created.attempt_id))==1


def test_timeout_cannot_prove_nonexecution_and_is_idempotent(candidate):
    backend,authority,_,_=candidate
    a=_begin(authority)
    authority.timeout(a.attempt_id,observed_ms=1399)
    authority.timeout(a.attempt_id,observed_ms=1400)
    after=authority.timeout(a.attempt_id,observed_ms=1450)
    assert after.attempt_state=="UNKNOWN"
    assert [r[0] for r in authority.events(a.attempt_id)].count("TIMEOUT_UNKNOWN")==1
    assert _budget(backend).reserved==Decimal("20")


@pytest.mark.parametrize("changed",[
    {"attempt_id":"forged-attempt"},
    {"lei":"lab:other-lei"},
    {"nonce":"ffffffffffffffffffffffffffffffff"},
    {"operation_digest":"sha256:"+"f"*64},
])
def test_authenticated_but_unbound_proof_fails_closed(candidate,changed):
    _,authority,private,_=candidate
    a=_begin(authority)
    with pytest.raises(SqliteAuthorityHold,match="binding mismatch"):
        authority.qualify(a.attempt_id,_signed(private,a,**changed),observed_ms=1500)
    assert authority.inspect_attempt(a.attempt_id).attempt_state=="UNKNOWN"


def test_tampered_ed25519_signature_rejected(candidate):
    _,authority,private,_=candidate
    a=_begin(authority)
    forged=_signed(private,a)
    forged["outcome"]="NON_EXECUTION_CONFIRMED"
    with pytest.raises(SqliteAuthorityHold,match="signed native evidence rejected"):
        authority.qualify(a.attempt_id,forged,observed_ms=1500)
    assert authority.inspect_attempt(a.attempt_id).attempt_state=="UNKNOWN"


@pytest.mark.parametrize("revoked,now",[
    (frozenset({"issuer-1"}),1500),
    (frozenset(),2001),
    (frozenset(),999),
])
def test_revoked_expired_or_premature_proof_rejected(candidate,revoked,now):
    backend,authority,private,path=candidate
    a=_begin(authority)
    _,unsafe= _open(path,_public(private),revoked=revoked,now=now)
    with pytest.raises(SqliteAuthorityHold,match="signed native evidence rejected"):
        unsafe.qualify(a.attempt_id,_signed(private,a),observed_ms=1500)
    assert authority.inspect_attempt(a.attempt_id).attempt_state=="UNKNOWN"


def test_contradictory_second_attestation_cannot_rewrite_terminal(candidate):
    backend,authority,private,path=candidate
    a=_begin(authority)
    authority.qualify(a.attempt_id,_signed(private,a),observed_ms=1500)
    with pytest.raises(SqliteAuthorityHold,match="terminal attestation"):
        authority.qualify(a.attempt_id,_signed(
            private,a,outcome="NON_EXECUTION_CONFIRMED"),observed_ms=1550)
    assert authority.inspect_attempt(a.attempt_id).attempt_state=="RESOLVED"
    assert _budget(backend).reserved==Decimal("20")


def test_attestation_rollback_before_commit_changes_no_terminal_state(candidate,monkeypatch):
    backend,authority,private,path=candidate
    a=_begin(authority)
    def fail(label):
        if label=="experimental_native_attempt_qualify":
            raise RuntimeError("crash boundary")
    monkeypatch.setattr(backend,"_before_commit",fail)
    with pytest.raises(RuntimeError,match="crash boundary"):
        authority.qualify(a.attempt_id,_signed(private,a),observed_ms=1500)
    _,reopened=_open(path,_public(private))
    assert reopened.inspect_attempt(a.attempt_id).attempt_state=="UNKNOWN"
    assert len(reopened.events(a.attempt_id))==1
    assert _budget(backend).reserved==Decimal("20")


def test_attempt_source_record_tampering_blocks_evidence(candidate):
    _,authority,private,path=candidate
    a=_begin(authority)
    with sqlite3.connect(authority.backend._database_path) as db:
        db.execute("DROP TRIGGER experimental_native_no_update")
        db.execute("UPDATE experimental_native_operations SET record_sha256=? "
                   "WHERE operation_key='lab:op-1'",("0"*64,))
    with pytest.raises(SqliteAuthorityHold,match="checksum mismatch"):
        authority.qualify(a.attempt_id,_signed(private,a),observed_ms=1500)


def test_terminal_records_and_events_have_sqlite_immutability_guards(candidate):
    _,authority,private,path=candidate
    a=_begin(authority)
    authority.qualify(a.attempt_id,_signed(private,a),observed_ms=1500)
    with sqlite3.connect(authority.backend._database_path) as db:
        for command in (
            "UPDATE experimental_native_attempts SET state='UNKNOWN'",
            "DELETE FROM experimental_native_attempts",
            "UPDATE experimental_native_attempt_events SET transition='FAKE'",
            "DELETE FROM experimental_native_attempt_events",
        ):
            with pytest.raises(sqlite3.DatabaseError):
                db.execute(command)


def _child_settle(path,pub,proof,start,queue):
    try:
        _,authority=_open(path,pub)
        assert start.wait(timeout=10)
        result=authority.qualify("lab:attempt-1",proof,observed_ms=1500)
        queue.put(("ok",result.attempt_state))
    except BaseException as exc:
        queue.put(("hold",type(exc).__name__,str(exc)))


def test_competing_signed_t6_t7_cross_process_terminalize_only_once(candidate):
    backend,authority,private,path=candidate
    a=_begin(authority)
    ctx=multiprocessing.get_context("spawn")
    start,queue=ctx.Event(),ctx.Queue()
    children=[ctx.Process(target=_child_settle,args=(
        str(path),_public(private),_signed(private,a,outcome=o),start,queue))
        for o in ("EFFECT_CONFIRMED","NON_EXECUTION_CONFIRMED")]
    for p in children:p.start()
    start.set()
    outcomes=[queue.get(timeout=30) for _ in children]
    for p in children:
        p.join(timeout=15)
        assert p.exitcode==0
    assert sum(x[0]=="ok" for x in outcomes)==1,outcomes
    assert sum(x[0]=="hold" for x in outcomes)==1,outcomes
    assert authority.inspect_attempt(a.attempt_id).attempt_state in ("RESOLVED","FAILED")
    assert len(authority.events(a.attempt_id))==2
    assert _budget(backend).reserved==Decimal("20")


def _exit_precommit(path,pub,proof):
    backend,authority=_open(path,pub)
    backend._before_commit=lambda operation: os._exit(86)
    authority.qualify("lab:attempt-1",proof,observed_ms=1500)


def test_process_exit_precommit_is_atomic_on_restart(candidate):
    _,authority,private,path=candidate
    a=_begin(authority)
    ctx=multiprocessing.get_context("spawn")
    proc=ctx.Process(target=_exit_precommit,args=(str(path),_public(private),_signed(private,a)))
    proc.start()
    proc.join(timeout=15)
    assert proc.exitcode==86
    _,reopened=_open(path,_public(private))
    assert reopened.inspect_attempt(a.attempt_id).attempt_state=="UNKNOWN"
    assert len(reopened.events(a.attempt_id))==1


def _exit_after_commit(path,pub,proof):
    _,authority=_open(path,pub)
    assert authority.qualify("lab:attempt-1",proof,observed_ms=1500).attempt_state=="RESOLVED"
    os._exit(87)


def test_process_exit_after_commit_idempotently_recovers(candidate):
    _,authority,private,path=candidate
    a=_begin(authority)
    proof=_signed(private,a)
    ctx=multiprocessing.get_context("spawn")
    proc=ctx.Process(target=_exit_after_commit,args=(str(path),_public(private),proof))
    proc.start()
    proc.join(timeout=15)
    assert proc.exitcode==87
    _,reopened=_open(path,_public(private))
    assert reopened.qualify(a.attempt_id,proof,observed_ms=1600).attempt_state=="RESOLVED"
    assert len(reopened.events(a.attempt_id))==2
