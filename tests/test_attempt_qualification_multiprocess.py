"""Multi-process and transactional interruption tests for candidate T6/T7 store.

These prove only single-host SQLite serialization with independent processes,
not consensus, physical power-loss survival or an external side-effect guarantee.
"""
from __future__ import annotations

import multiprocessing
import sqlite3

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from tenir_governance.operations.attempt_qualification import (
    AttemptQualificationStore, QualificationError,
)
from tenir_governance.operations.attempt_attestation import (
    Ed25519AttemptVerifier, canonical_attestation_payload,
)

NONCE = "0123456789abcdef"

def _verifier(public):
    return Ed25519AttemptVerifier({"trusted": public}, now_ms=lambda: 1500)

def _payload(private, outcome):
    data = dict(
        schema_version="tenir.attempt.attestation.v1", evidence_id="e-" + outcome,
        attempt_id="a1", lei="LEI-A", nonce=NONCE, outcome=outcome,
        authority_id="trusted", issued_at_ms=1000, expires_at_ms=2000,
    )
    return {**data,"signature_hex":private.sign(canonical_attestation_payload(data)).hex()}

def _worker_qualify(path, public, proof, start, results):
    try:
        store = AttemptQualificationStore(path, _verifier(public))
        if not start.wait(10):
            raise RuntimeError("start signal timeout")
        results.put(("ok",store.qualify("a1",proof,observed_ms=1500)))
    except BaseException as exc:
        results.put(("error",type(exc).__name__,str(exc)))

def _worker_begin(path, aid, start, results):
    try:
        store=AttemptQualificationStore(path,lambda _: None)
        if not start.wait(10):
            raise RuntimeError("start signal timeout")
        store.begin(attempt_id=aid,lei="LEI-A",nonce=NONCE,deadline_ms=1800)
        results.put(("ok",aid))
    except BaseException as exc:
        results.put(("error",type(exc).__name__,str(exc)))

def _launch(workers):
    ctx = multiprocessing.get_context("spawn")
    start, result_queue = ctx.Event(), ctx.Queue()
    processes = [ctx.Process(target=fn,args=(*args,start,result_queue)) for fn,args in workers]
    for process in processes: process.start()
    start.set()
    results = [result_queue.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=15)
        assert not process.is_alive(), "child did not terminate"
        assert process.exitcode == 0
    return results

def test_conflicting_signed_outcomes_cross_process_are_serialized(tmp_path):
    private=Ed25519PrivateKey.generate()
    public=private.public_key().public_bytes(
        serialization.Encoding.Raw,serialization.PublicFormat.Raw)
    path=str(tmp_path/"race.sqlite")
    parent=AttemptQualificationStore(path,_verifier(public))
    parent.begin(attempt_id="a1",lei="LEI-A",nonce=NONCE,deadline_ms=1100)
    workers=[
        (_worker_qualify,(path,public,_payload(private,"EFFECT_CONFIRMED"))),
        (_worker_qualify,(path,public,_payload(private,"NON_EXECUTION_CONFIRMED"))),
    ]
    outcomes=_launch(workers)
    assert sum(item[0]=="ok" for item in outcomes)==1, outcomes
    assert sum(item[0]=="error" for item in outcomes)==1, outcomes
    assert parent.read("a1")["state"] in ("RESOLVED","FAILED")
    assert sum(e[0] in ("RESOLVED","FAILED") for e in parent.events("a1"))==1

def test_same_lei_creation_race_only_one_attempt(tmp_path):
    path=str(tmp_path/"creation.sqlite")
    AttemptQualificationStore(path,lambda _: None)
    outcomes=_launch([
        (_worker_begin,(path,"a1")),
        (_worker_begin,(path,"a2")),
    ])
    assert sum(item[0]=="ok" for item in outcomes)==1, outcomes
    assert sum(item[0]=="error" for item in outcomes)==1, outcomes
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM attempts WHERE lei='LEI-A'").fetchone()[0]==1

def test_failed_event_write_rolls_back_terminal_state(tmp_path):
    private=Ed25519PrivateKey.generate()
    public=private.public_key().public_bytes(
        serialization.Encoding.Raw,serialization.PublicFormat.Raw)
    path=tmp_path/"rollback.sqlite"
    store=AttemptQualificationStore(path,_verifier(public))
    store.begin(attempt_id="a1",lei="LEI-A",nonce=NONCE,deadline_ms=1100)
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TRIGGER fault BEFORE INSERT ON events
                   WHEN NEW.transition='RESOLVED'
                   BEGIN SELECT RAISE(ABORT,'injected event failure'); END""")
    with pytest.raises(sqlite3.DatabaseError, match="injected event failure"):
        store.qualify("a1",_payload(private,"EFFECT_CONFIRMED"),observed_ms=1500)
    assert store.read("a1")["state"]=="UNKNOWN"
    assert all(event[0]!="RESOLVED" for event in store.events("a1"))
