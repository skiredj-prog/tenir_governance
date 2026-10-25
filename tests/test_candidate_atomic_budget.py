"""G1 candidate-only admission: source + attempt + I8 in ONE SQLite transaction.

No test authorizes production dispatch or changes the JSON operation registry.
"""
from __future__ import annotations

import multiprocessing
import os
import sqlite3
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tenir_governance.operations.candidate_atomic_budget import (
    CandidateAtomicBudgetAdmission, CandidateBudgetHold,
)
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from tenir_governance.operations.attempt_attestation import (
    Ed25519AttemptVerifier, canonical_attestation_payload,
)

DIGEST="sha256:"+"ab"*32
NONCE="0123456789abcdef0123456789abcdef"


def _pub(priv):
    return priv.public_key().public_bytes(
        serialization.Encoding.Raw,serialization.PublicFormat.Raw
    )


def _open(path, pub):
    backend=SQLiteConsensusBackend(str(path))
    gate=CandidateAtomicBudgetAdmission(
        backend,Ed25519AttemptVerifier({"candidate-attestor":pub},
                                       now_ms=lambda:1500),
        experimental_only=True)
    return backend,gate


@pytest.fixture
def candidate(tmp_path):
    private=Ed25519PrivateKey.generate()
    path=tmp_path/"candidate-ledger"
    backend,gate=_open(path,_pub(private))
    assert backend.register_resource("candidate:budget",Decimal("100"))=="REGISTERED"
    return backend,gate,private,path


def _admit(gate,op="candidate:op-1",attempt="a1",lei="candidate:lei-1",
           amount=Decimal("20"),**kw):
    params=dict(operation_key=op,operation_digest=DIGEST,
                attempt_id=attempt,lei=lei,nonce=NONCE,deadline_ms=1400,
                resource_id="candidate:budget",amount=amount)
    params.update(kw)
    return gate.admit(**params)


def _signed(private,outcome="NON_EXECUTION_CONFIRMED"):
    payload=dict(
        schema_version="tenir.attempt.attestation.v1",
        evidence_id="signed-1",attempt_id="a1",lei="candidate:lei-1",
        nonce=NONCE,operation_digest=DIGEST,
        outcome=outcome,authority_id="candidate-attestor",
        issued_at_ms=1000,expires_at_ms=2000,
    )
    return {**payload,"signature_hex":private.sign(
        canonical_attestation_payload(payload)).hex()}


def _state(backend):
    return backend.get_state("candidate:budget")


def test_single_sqlite_transaction_creates_source_attempt_budget_and_hold(candidate):
    backend,gate,_,path=candidate
    result=_admit(gate)
    assert result.decision=="HOLD"
    assert result.execution_authorized is False
    assert result.source_is_production is False
    assert result.status=="CANDIDATE_RESERVED_HOLD"
    assert gate.attempts.inspect("a1").candidate_source_state=="UNKNOWN"
    assert gate.attempts.inspect("a1").attempt_state=="UNKNOWN"
    state=_state(backend)
    assert state.reserved==Decimal("20") and state.committed==Decimal("0")
    rows=[x for x in state.reservations if x["key"]=="candidate:op-1"]
    assert len(rows)==1 and rows[0]["state"]=="RESERVED"
    assert rows[0]["reservation_id"]==result.reservation_id
    assert len(gate.attempts.events("a1"))==1


def test_idempotent_begin_replay_does_not_double_spend(candidate):
    backend,gate,_,path=candidate
    a=_admit(gate)
    b=_admit(gate)
    assert a==b
    assert _state(backend).reserved==Decimal("20")
    assert len(gate.attempts.events("a1"))==1


@pytest.mark.parametrize("different",[
    {"amount":Decimal("21")},
    {"attempt":"a2"},
    {"lei":"candidate:other-lei"},
    {"operation_digest":"sha256:"+"cd"*32},
    {"resource_id":"candidate:other-resource"},
])
def test_replay_bound_to_identical_operation_and_reservation(candidate,different):
    backend,gate,_,path=candidate
    _admit(gate)
    with pytest.raises(CandidateBudgetHold):
        _admit(gate,**different)
    assert _state(backend).reserved==Decimal("20")


def test_insufficient_capacity_rejects_all_three_writes(candidate):
    backend,gate,_,path=candidate
    with pytest.raises(CandidateBudgetHold,match="INSUFFICIENT_BUDGET"):
        _admit(gate,amount=Decimal("101"))
    assert _state(backend).reserved==Decimal("0")
    with pytest.raises(KeyError):
        gate.attempts.inspect("a1")


def test_fault_before_commit_rolls_back_source_attempt_event_and_budget(candidate,monkeypatch):
    backend,gate,priv,path=candidate
    monkeypatch.setattr(gate,"_before_commit",
                        lambda: (_ for _ in ()).throw(RuntimeError("precommit fault")))
    with pytest.raises(RuntimeError,match="precommit fault"):
        _admit(gate)
    reread,_=_open(path,_pub(priv))
    assert _state(reread).reserved==Decimal("0")
    assert len(_state(reread).reservations)==0
    with pytest.raises(KeyError):
        gate.attempts.inspect("a1")


def test_qualified_attempt_cannot_be_re_admitted(candidate):
    backend,gate,private,path=candidate
    _admit(gate)
    proof=gate.attempts.qualify("a1",_signed(private),observed_ms=1500)
    assert proof.attempt_state=="FAILED"
    with pytest.raises(CandidateBudgetHold,match="not re-admissible"):
        _admit(gate)
    assert _state(backend).reserved==Decimal("20")
    assert _state(backend).committed==Decimal("0")


def test_uncertain_budget_blocks_new_candidate_admission(candidate):
    backend,gate,_,path=candidate
    _admit(gate)
    assert backend.reconcile("candidate:op-1").status=="UNRESOLVED"
    with pytest.raises(CandidateBudgetHold,match="uncertain"):
        _admit(gate,op="candidate:op-2",attempt="a2",lei="candidate:lei-2")
    assert _state(backend).reserved==Decimal("20")


def test_drifted_budget_counter_blocks_without_new_attempt(candidate):
    backend,gate,_,path=candidate
    _admit(gate)
    with sqlite3.connect(backend._database_path) as conn:
        conn.execute("UPDATE resources SET reserved='40' WHERE resource_id='candidate:budget'")
    with pytest.raises(CandidateBudgetHold,match="I8 budget counters"):
        _admit(gate,op="candidate:op-2",attempt="a2",lei="candidate:lei-2")
    with pytest.raises(KeyError):
        gate.attempts.inspect("a2")


def test_g3_fenced_shadow_epoch_does_not_grant_candidate_budget_admission(candidate):
    backend,gate,_,path=candidate
    with sqlite3.connect(backend._database_path) as conn:
        conn.execute("CREATE TABLE shadow_authority_fence(epoch INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO shadow_authority_fence VALUES(1)")
    with pytest.raises(CandidateBudgetHold,match="G3 fenced-shadow"):
        _admit(gate)
    assert _state(backend).reserved==Decimal("0")


@pytest.mark.parametrize("param",[
    {"op":"live:op"},
    {"lei":"live:lei"},
    {"resource_id":"production"},
    {"amount":Decimal("-1")},
    {"amount":float("nan")},
])
def test_candidate_namespace_or_invalid_amount_does_not_touch_budget(candidate,param):
    backend,gate,_,path=candidate
    with pytest.raises(CandidateBudgetHold):
        _admit(gate,**param)
    assert _state(backend).reserved==Decimal("0")


def _spawn_claim(path,pub,op,aid,lei,quantity,start,queue):
    try:
        backend,gate=_open(path,pub)
        assert start.wait(10)
        answer=_admit(gate,op=op,attempt=aid,lei=lei,amount=Decimal(quantity))
        queue.put(("ok",answer.reservation_id))
    except BaseException as exc:
        queue.put(("error",type(exc).__name__,str(exc)))


def test_two_processes_claim_capacity_only_once(candidate):
    backend,gate,private,path=candidate
    # Consume 90 before the race; only one 7-unit claim can fit.
    first=_admit(gate,amount=Decimal("90"))
    ctx=multiprocessing.get_context("spawn")
    start,queue=ctx.Event(),ctx.Queue()
    workers=[
        ctx.Process(target=_spawn_claim,
                    args=(str(path),_pub(private),
                          "candidate:race-"+str(i),"race-"+str(i),
                          "candidate:race-lei-"+str(i),"7",start,queue))
        for i in range(2)
    ]
    for p in workers:p.start()
    start.set()
    answers=[queue.get(timeout=30) for _ in workers]
    for p in workers:
        p.join(timeout=15)
        assert p.exitcode==0
    assert sum(x[0]=="ok" for x in answers)==1,answers
    assert sum(x[0]=="error" for x in answers)==1,answers
    assert _state(backend).reserved==Decimal("97")
    assert _state(backend).committed==Decimal("0")


def _child_kill_before_commit(path,pub):
    _,gate=_open(path,pub)
    gate._before_commit=lambda:os._exit(86)
    _admit(gate)


def test_child_hard_exit_before_commit_does_not_reserve(candidate):
    backend,gate,private,path=candidate
    ctx=multiprocessing.get_context("spawn")
    child=ctx.Process(target=_child_kill_before_commit,
                      args=(str(path),_pub(private)))
    child.start()
    child.join(timeout=15)
    assert child.exitcode==86
    reopened,_=_open(path,_pub(private))
    assert _state(reopened).reserved==Decimal("0")
    with pytest.raises(KeyError):
        gate.attempts.inspect("a1")


def test_preexisting_budget_reservation_without_candidate_is_not_adopted(candidate):
    backend,gate,_,path=candidate
    original=backend.reserve("candidate:op-1",Decimal("20"),"candidate:budget")
    assert original.status=="RESERVED"
    with pytest.raises(CandidateBudgetHold,match="partial"):
        _admit(gate)
    with pytest.raises(KeyError):
        gate.attempts.inspect("a1")
    assert _state(backend).reserved==Decimal("20")


def test_compensation_pending_blocks_new_candidate_budget_claim(candidate):
    backend,gate,_,path=candidate
    # A genuine existing budget record can carry compensation uncertainty.
    old=backend.reserve("candidate:prior",Decimal("10"),"candidate:budget")
    assert old.status=="RESERVED"
    with sqlite3.connect(backend._database_path) as conn:
        conn.execute(
            "INSERT INTO compensations (compensation_id,original_reservation_id,"
            "resource_id,amount,status,reason,created_at) "
            "VALUES('c1',?,?,?,'REQUIRED','late-effect',0)",
            (old.reservation_id,"candidate:budget","10"),
        )
    with pytest.raises(CandidateBudgetHold,match="compensation"):
        _admit(gate)
    assert _state(backend).reserved==Decimal("10")


def test_replay_refuses_prior_external_commit_without_grant(candidate):
    backend,gate,_,path=candidate
    receipt=_admit(gate)
    assert backend.commit(receipt.reservation_id).status=="COMMITTED"
    with pytest.raises(CandidateBudgetHold):
        _admit(gate)
    assert _state(backend).committed==Decimal("20")
    assert _state(backend).reserved==Decimal("0")


def _kill_after_commit(path,pub):
    _,gate=_open(path,pub)
    receipt=_admit(gate)
    assert receipt.status=="CANDIDATE_RESERVED_HOLD"
    os._exit(87)


def test_restart_after_committed_candidate_admission_is_exact_replay(candidate):
    backend,gate,private,path=candidate
    ctx=multiprocessing.get_context("spawn")
    child=ctx.Process(target=_kill_after_commit,args=(str(path),_pub(private)))
    child.start()
    child.join(timeout=15)
    assert child.exitcode==87
    restarted,other_gate=_open(path,_pub(private))
    assert _state(restarted).reserved==Decimal("20")
    assert _admit(other_gate)==_admit(gate)
    assert _state(restarted).reserved==Decimal("20")
    assert len(other_gate.attempts.events("a1"))==1


def test_opt_in_is_mandatory_even_with_candidate_resource_prefix(tmp_path):
    key=Ed25519PrivateKey.generate()
    backend=SQLiteConsensusBackend(str(tmp_path/"budget"))
    assert backend.register_resource("candidate:budget",Decimal("10"))=="REGISTERED"
    verifier=Ed25519AttemptVerifier({"candidate-attestor":_pub(key)},
                                    now_ms=lambda:1500)
    with pytest.raises(CandidateBudgetHold,match="explicit experimental_only"):
        CandidateAtomicBudgetAdmission(backend,verifier)
    assert _state(backend).reserved==Decimal("0")


@pytest.mark.parametrize("outcome,expected,claimed_reserved,claimed_committed", [
    ("EFFECT_CONFIRMED","COMMITTED",Decimal("0"),Decimal("20")),
    ("NON_EXECUTION_CONFIRMED","RELEASED",Decimal("0"),Decimal("0")),
])
def test_signed_settlement_is_atomic_and_replayed_without_budget_drift(
        candidate,outcome,expected,claimed_reserved,claimed_committed):
    backend,gate,private,path=candidate
    _admit(gate)
    proof=_signed(private,outcome)
    first=gate.settle_attested_candidate(
        attempt_id="a1",signed_evidence=proof,observed_ms=1500)
    assert first.status=="CANDIDATE_EVIDENCE_SETTLED_HOLD"
    assert first.budget_state==expected
    assert first.decision=="HOLD" and not first.execution_authorized
    assert first.is_production_settlement is False
    assert _state(backend).reserved==claimed_reserved
    assert _state(backend).committed==claimed_committed
    assert gate.settle_attested_candidate(
        attempt_id="a1",signed_evidence=proof,observed_ms=1600)==first
    assert len([e for e in gate.attempts.events("a1")
                if e[0] in ("RESOLVED","FAILED")])==1


def test_invalid_signed_claim_never_settles_budget(candidate):
    backend,gate,private,path=candidate
    _admit(gate)
    altered=_signed(private)
    altered["outcome"]="EFFECT_CONFIRMED"
    with pytest.raises(CandidateBudgetHold,match="signed candidate evidence rejected"):
        gate.settle_attested_candidate(
            attempt_id="a1",signed_evidence=altered,observed_ms=1500)
    assert _state(backend).reserved==Decimal("20")
    assert gate.attempts.inspect("a1").attempt_state=="UNKNOWN"


def test_conflicting_signed_terminal_claim_cannot_refund_committed_budget(candidate):
    backend,gate,private,path=candidate
    _admit(gate)
    gate.settle_attested_candidate(
        attempt_id="a1",signed_evidence=_signed(private,"EFFECT_CONFIRMED"),
        observed_ms=1500)
    with pytest.raises(CandidateBudgetHold,match="terminal candidate"):
        gate.settle_attested_candidate(
            attempt_id="a1",signed_evidence=_signed(private,"NON_EXECUTION_CONFIRMED"),
            observed_ms=1600)
    assert _state(backend).reserved==Decimal("0")
    assert _state(backend).committed==Decimal("20")


def test_injected_settlement_crash_rolls_back_budget_and_evidence(candidate,monkeypatch):
    backend,gate,private,path=candidate
    _admit(gate)
    monkeypatch.setattr(gate,"_before_commit",
                        lambda: (_ for _ in ()).throw(RuntimeError("settlement precommit crash")))
    with pytest.raises(RuntimeError,match="settlement precommit crash"):
        gate.settle_attested_candidate(
            attempt_id="a1",signed_evidence=_signed(private),
            observed_ms=1500)
    assert gate.attempts.inspect("a1").attempt_state=="UNKNOWN"
    assert _state(backend).reserved==Decimal("20")
    assert _state(backend).committed==Decimal("0")
    assert len(gate.attempts.events("a1"))==1


def test_out_of_band_budget_commit_blocks_signed_candidate_settlement(candidate):
    backend,gate,private,path=candidate
    receipt=_admit(gate)
    assert backend.commit(receipt.reservation_id).status=="COMMITTED"
    with pytest.raises(CandidateBudgetHold,match="diverged"):
        gate.settle_attested_candidate(
            attempt_id="a1",signed_evidence=_signed(private),
            observed_ms=1500)
    assert _state(backend).committed==Decimal("20")
    assert gate.attempts.inspect("a1").attempt_state=="UNKNOWN"


def test_unknown_budget_never_settles_even_with_valid_signature(candidate):
    backend,gate,private,path=candidate
    _admit(gate)
    assert backend.reconcile("candidate:op-1").status=="UNRESOLVED"
    with pytest.raises(CandidateBudgetHold,match="uncertain"):
        gate.settle_attested_candidate(
            attempt_id="a1",signed_evidence=_signed(private,"EFFECT_CONFIRMED"),
            observed_ms=1500)
    assert gate.attempts.inspect("a1").attempt_state=="UNKNOWN"
    assert _state(backend).committed==Decimal("0")


def _race_signed_settlement(path,pub,proof,start,queue):
    try:
        _,gate=_open(path,pub)
        assert start.wait(10)
        receipt=gate.settle_attested_candidate(
            attempt_id="a1",signed_evidence=proof,observed_ms=1500)
        queue.put(("ok",receipt.budget_state))
    except BaseException as exc:
        queue.put(("error",type(exc).__name__,str(exc)))


def test_conflicting_signed_settlements_cross_process_linearize_once(candidate):
    backend,gate,private,path=candidate
    _admit(gate)
    ctx=multiprocessing.get_context("spawn")
    start,queue=ctx.Event(),ctx.Queue()
    workers=[
        ctx.Process(target=_race_signed_settlement,
                    args=(str(path),_pub(private),_signed(private,outcome),start,queue))
        for outcome in ("EFFECT_CONFIRMED","NON_EXECUTION_CONFIRMED")
    ]
    for p in workers:p.start()
    start.set()
    outcomes=[queue.get(timeout=30) for _ in workers]
    for p in workers:
        p.join(timeout=15)
        assert p.exitcode==0
    assert sum(x[0]=="ok" for x in outcomes)==1,outcomes
    assert sum(x[0]=="error" for x in outcomes)==1,outcomes
    snapshot=gate.attempts.inspect("a1")
    state=_state(backend)
    assert (snapshot.attempt_state,snapshot.candidate_source_state,
            state.reserved,state.committed) in (
                ("RESOLVED","EFFECT_ATTESTED",Decimal("0"),Decimal("20")),
                ("FAILED","NO_EFFECT_ATTESTED",Decimal("0"),Decimal("0")),
            )
    assert len([ev for ev in gate.attempts.events("a1")
                if ev[0] in ("RESOLVED","FAILED")])==1


def _exit_before_signed_settlement(path,pub,proof):
    _,gate=_open(path,pub)
    gate._before_commit=lambda:os._exit(88)
    gate.settle_attested_candidate(attempt_id="a1",
                                   signed_evidence=proof,observed_ms=1500)


def test_process_exit_before_signed_settlement_commit_retains_unknown(candidate):
    backend,gate,private,path=candidate
    _admit(gate)
    ctx=multiprocessing.get_context("spawn")
    child=ctx.Process(target=_exit_before_signed_settlement,
                      args=(str(path),_pub(private),_signed(private)))
    child.start()
    child.join(timeout=15)
    assert child.exitcode==88
    reopened,observer=_open(path,_pub(private))
    assert observer.attempts.inspect("a1").attempt_state=="UNKNOWN"
    assert _state(reopened).reserved==Decimal("20")
    assert _state(reopened).committed==Decimal("0")
    assert len(observer.attempts.events("a1"))==1
