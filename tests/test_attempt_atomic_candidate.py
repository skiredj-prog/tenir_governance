"""P0 issue #67: atomic *candidate* source/attempt projection, no dispatch authority.

Tests use actual SQLite WAL and Ed25519 signed attestation. They do not
assert atomicity with the existing JSON operation registry or provider effects.
"""
from __future__ import annotations

import multiprocessing
import os
import sqlite3

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tenir_governance.operations.attempt_atomic_candidate import (
    AtomicAttemptCandidateAuthority,
)
from tenir_governance.operations.attempt_attestation import (
    Ed25519AttemptVerifier, canonical_attestation_payload,
)
from tenir_governance.operations.attempt_qualification import QualificationError

DIGEST = "sha256:" + "ab" * 32
NONCE = "0123456789abcdef0123456789abcdef"


def _public(private):
    return private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _store(path, public, *, now=1500):
    return AtomicAttemptCandidateAuthority(
        path, Ed25519AttemptVerifier({"root": public}, now_ms=lambda: now)
    )


def _begin(store):
    return store.begin(operation_key="op-1",operation_digest=DIGEST,
                       attempt_id="a1",lei="LEI-1",nonce=NONCE,
                       deadline_ms=1400)


def _signed(private, outcome="EFFECT_CONFIRMED", **kw):
    data = {
        "schema_version": "tenir.attempt.attestation.v1",
        "evidence_id": "evidence-1",
        "attempt_id": "a1", "lei": "LEI-1", "nonce": NONCE,
        "operation_digest": DIGEST,
        "outcome": outcome, "authority_id": "root",
        "issued_at_ms": 1000,"expires_at_ms": 2000,
    }
    data.update(kw)
    return {**data, "signature_hex": private.sign(
        canonical_attestation_payload(data)).hex()}


@pytest.fixture
def candidate(tmp_path):
    private = Ed25519PrivateKey.generate()
    path = tmp_path/"candidate-authority.sqlite"
    authority = _store(path,_public(private))
    _begin(authority)
    return authority,private,path


@pytest.mark.parametrize("outcome,attempt_state,source_state", [
    ("EFFECT_CONFIRMED","RESOLVED","EFFECT_ATTESTED"),
    ("NON_EXECUTION_CONFIRMED","FAILED","NO_EFFECT_ATTESTED"),
])
def test_atomic_projections_terminalize_together(candidate,outcome,attempt_state,source_state):
    authority,key,path = candidate
    qualified=authority.qualify("a1",_signed(key,outcome),observed_ms=1500)
    assert qualified.attempt_state==attempt_state
    assert qualified.candidate_source_state==source_state
    assert qualified.execution_authorized is False
    assert qualified.decision=="HOLD"
    after_restart=_store(path,_public(key)).inspect("a1")
    assert after_restart == qualified
    assert [ev[0] for ev in authority.events("a1")].count(attempt_state)==1


def test_exact_begin_and_attestation_replay_do_not_duplicate(candidate):
    authority,key,_=candidate
    before=len(authority.events("a1"))
    assert _begin(authority).attempt_state=="UNKNOWN"
    assert len(authority.events("a1"))==before
    evidence=_signed(key)
    first=authority.qualify("a1",evidence,observed_ms=1500)
    again=authority.qualify("a1",evidence,observed_ms=1600)
    assert first==again
    assert len([e for e in authority.events("a1") if e[0]=="RESOLVED"])==1


def test_timeout_does_not_promote_or_grant(candidate):
    authority,_,_=candidate
    assert authority.timeout("a1",observed_ms=1399).attempt_state=="UNKNOWN"
    assert authority.timeout("a1",observed_ms=1400).attempt_state=="UNKNOWN"
    assert authority.timeout("a1",observed_ms=1600).execution_authorized is False
    assert len([e for e in authority.events("a1") if e[0]=="TIMEOUT_UNKNOWN"])==1


def test_attempt_or_lei_rebinding_denied(candidate):
    authority,_,_=candidate
    with pytest.raises(QualificationError):
        authority.begin(operation_key="op-1",operation_digest=DIGEST,attempt_id="a2",
                        lei="LEI-1",nonce=NONCE,deadline_ms=1400)
    with pytest.raises(QualificationError):
        authority.begin(operation_key="op-2",operation_digest=DIGEST,attempt_id="a2",
                        lei="LEI-1",nonce=NONCE,deadline_ms=1400)
    with pytest.raises(QualificationError):
        authority.begin(operation_key="op-2",operation_digest=DIGEST,attempt_id="a1",
                        lei="LEI-2",nonce=NONCE,deadline_ms=1400)


@pytest.mark.parametrize("field,changed",[
    ("lei","LEI-different"),
    ("nonce","ffffffffffffffffffffffffffffffff"),
    ("operation_digest","sha256:"+"cd"*32),
    ("attempt_id","different-attempt"),
])
def test_valid_signature_for_different_context_cannot_qualify(candidate,field,changed):
    authority,key,_=candidate
    with pytest.raises(QualificationError,match="context mismatch"):
        authority.qualify("a1",_signed(key,**{field:changed}),observed_ms=1500)
    assert authority.inspect("a1").attempt_state=="UNKNOWN"


def test_invalid_signature_never_mutates(candidate):
    authority,key,_=candidate
    proof=_signed(key)
    proof["outcome"]="NON_EXECUTION_CONFIRMED"
    with pytest.raises(QualificationError,match="authenticated proof rejected"):
        authority.qualify("a1",proof,observed_ms=1500)
    assert authority.inspect("a1").attempt_state=="UNKNOWN"


def test_contradictory_qualified_terminal_rejected(candidate):
    authority,key,_=candidate
    authority.qualify("a1",_signed(key),observed_ms=1500)
    with pytest.raises(QualificationError,match="terminal outcome"):
        authority.qualify("a1",_signed(key,"NON_EXECUTION_CONFIRMED"),observed_ms=1500)
    assert authority.inspect("a1").candidate_source_state=="EFFECT_ATTESTED"


def test_source_and_attempt_identity_are_immutable_in_sqlite(candidate):
    _,_,path=candidate
    with sqlite3.connect(path) as db:
        with pytest.raises(sqlite3.DatabaseError,match="immutable"):
            db.execute("UPDATE candidate_sources SET operation_digest=?",
                       ("sha256:"+"cd"*32,))
    with sqlite3.connect(path) as db:
        with pytest.raises(sqlite3.DatabaseError,match="immutable"):
            db.execute("UPDATE candidate_attempts SET nonce=?",("replaced",))


def test_fault_before_qualification_commit_rolls_back_three_writes(candidate,monkeypatch):
    authority,key,path=candidate
    def reject():
        raise RuntimeError("injected before COMMIT")
    monkeypatch.setattr(authority,"_before_commit",reject)
    with pytest.raises(RuntimeError,match="injected"):
        authority.qualify("a1",_signed(key),observed_ms=1500)
    restored=_store(path,_public(key))
    snap=restored.inspect("a1")
    assert snap.attempt_state==snap.candidate_source_state=="UNKNOWN"
    assert all(e[0]!="RESOLVED" for e in restored.events("a1"))


def test_fault_during_begin_rolls_back_source_and_attempt(tmp_path,monkeypatch):
    key=Ed25519PrivateKey.generate()
    authority=_store(tmp_path/"new.sqlite",_public(key))
    monkeypatch.setattr(authority,"_before_commit",lambda: (_ for _ in ()).throw(
        RuntimeError("injected begin crash")))
    with pytest.raises(RuntimeError,match="injected"):
        _begin(authority)
    with pytest.raises(KeyError):
        authority.inspect("a1")
    with sqlite3.connect(authority.path) as db:
        assert db.execute("SELECT COUNT(*) FROM candidate_sources").fetchone()[0]==0


def _race_worker(path,public,proof,start,results):
    try:
        authority=_store(path,public)
        if not start.wait(10):
            raise RuntimeError("no start signal")
        result=authority.qualify("a1",proof,observed_ms=1500)
        results.put(("ok",result.attempt_state))
    except BaseException as exc:
        results.put(("error",type(exc).__name__,str(exc)))


def test_two_processes_conflicting_signatures_only_one_commits(candidate):
    authority,key,path=candidate
    ctx=multiprocessing.get_context("spawn")
    signal,queue=ctx.Event(),ctx.Queue()
    p1=ctx.Process(target=_race_worker,args=(
        str(path),_public(key),_signed(key),signal,queue))
    p2=ctx.Process(target=_race_worker,args=(
        str(path),_public(key),_signed(key,"NON_EXECUTION_CONFIRMED"),signal,queue))
    for p in (p1,p2): p.start()
    signal.set()
    outcomes=[queue.get(timeout=25) for _ in (p1,p2)]
    for p in (p1,p2):
        p.join(timeout=15)
        assert p.exitcode==0
    assert sum(o[0]=="ok" for o in outcomes)==1, outcomes
    assert sum(o[0]=="error" for o in outcomes)==1, outcomes
    snap=authority.inspect("a1")
    assert (snap.attempt_state,snap.candidate_source_state) in (
        ("RESOLVED","EFFECT_ATTESTED"),("FAILED","NO_EFFECT_ATTESTED"))
    assert len([e for e in authority.events("a1") if e[0] in ("RESOLVED","FAILED")])==1


def _kill_before_commit(path,public,proof):
    authority=_store(path,public)
    authority._before_commit=lambda: os._exit(86)
    authority.qualify("a1",proof,observed_ms=1500)


def test_process_exit_before_commit_leaves_source_attempt_unknown(candidate):
    authority,key,path=candidate
    ctx=multiprocessing.get_context("spawn")
    process=ctx.Process(target=_kill_before_commit,args=(str(path),_public(key),_signed(key)))
    process.start()
    process.join(timeout=15)
    assert process.exitcode==86
    restored=_store(path,_public(key))
    snap=restored.inspect("a1")
    assert snap.attempt_state==snap.candidate_source_state=="UNKNOWN"
    assert not any(e[0]=="RESOLVED" for e in restored.events("a1"))
