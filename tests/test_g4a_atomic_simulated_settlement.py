"""G4A-T01..T10. Laboratory only; T11 is the unchanged regression suites."""
from __future__ import annotations

from dataclasses import asdict, replace
from decimal import Decimal
import json
import multiprocessing as mp
import os
from pathlib import Path
import sqlite3

import pytest

from tenir_governance.operations.experimental_g4a_settlement import (
    ExperimentalG4AAuthority, SimulatedEvidence,
)
from tenir_governance.operations.experimental_g3_exclusive_owner import G3ExperimentalExclusiveOwner
from tenir_governance.operations.experimental_native_sqlite_authority import (
    ExperimentalSqliteOperationAuthority, SqliteAuthorityHold,
)
from tenir_governance.operations.registry import DurableOperationRegistry, OperationRecord
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend


def record(key="lab:g4a"):
    return OperationRecord(idempotency_key=key, passage_id="lab:passage", agent_id="lab:agent",
                           action_type="simulation", resource_id="lab:budget", amount=20.0)


def open_authority(path):
    return ExperimentalG4AAuthority.open(
        SQLiteConsensusBackend(str(path)), trust_generation=1, experimental_only=True)


@pytest.fixture
def lab(tmp_path):
    source=DurableOperationRegistry(str(tmp_path/"source.json"))
    for key in ("lab:g4a", "lab:other"):
        assert source.register(record(key))
    backend=SQLiteConsensusBackend(str(tmp_path/"budget"))
    backend.register_resource("lab:budget", Decimal("100"))
    G3ExperimentalExclusiveOwner(source, backend, experimental_only=True).activate()
    authority=ExperimentalG4AAuthority.provision(
        backend, trust_generation=1, experimental_only=True)
    return source, backend, authority


def admit(authority, key="lab:g4a"):
    return authority.admit_created(record(key), attempt_id="lab:attempt:"+key,
                                   request_id="lab:admit:"+key)


def evidence(receipt, outcome="EFFECT_CONFIRMED", evidence_id="lab:evidence"):
    return SimulatedEvidence(operation_key=receipt.operation_key,
        reservation_id=receipt.reservation_id, attempt_id=receipt.attempt_id,
        payload_hash=receipt.payload_hash, authority_epoch=receipt.authority_epoch,
        trust_generation=receipt.trust_generation, outcome=outcome, evidence_id=evidence_id)


def counts(backend):
    with backend._connection() as db:
        return {name:db.execute("SELECT count(*) FROM "+name).fetchone()[0] for name in (
            "reservations", "experimental_native_operations", "g4a_current", "g4a_outbox", "g4a_commands")}


def test_t01_admission_is_one_atomic_event_and_snapshot(lab):
    source,backend,a=lab
    before=Path(source._store_path).read_bytes()
    first=admit(a)
    assert first.operation_state==first.budget_state=="RESERVED"
    assert first.execution_authorized is False and first.simulated is True
    assert counts(backend)==dict.fromkeys(counts(backend),1)
    assert admit(a)==first==a.inspect("lab:g4a")
    assert Path(source._store_path).read_bytes()==before
    assert backend.get_state("lab:budget").reserved==Decimal("20")


@pytest.mark.parametrize("action", ["admit", "settle"])
def test_t02_quarantine_between_preflight_and_transaction_is_rechecked(lab, action):
    source,backend,a=lab
    issued=admit(a) if action=="settle" else None
    before=counts(backend)
    a._before_guard=lambda: source.set_quarantine(True,locked_by="test")
    with pytest.raises(SqliteAuthorityHold,match="quarantine"):
        (a.settle("lab:settle", [evidence(issued)]) if issued else admit(a))
    assert counts(backend)==before
    assert backend.get_state("lab:budget").committed==0


@pytest.mark.parametrize("damage", ["marker", "sentinel", "source", "epoch"])
def test_t03_drift_refuses_without_writes(lab, damage):
    source,backend,a=lab
    issued=admit(a); before=counts(backend)
    if damage=="marker": Path(source._store_path+".g3-sqlite-owner.json").write_text("{}")
    elif damage=="sentinel": Path(source._store_path+".g3-sqlite-owner.initialized").unlink()
    elif damage=="source": Path(source._store_path).write_text("{}")
    else:
        with backend._connection() as db:
            db.execute("DROP TRIGGER g3_exclusive_monotonic")
            db.execute("DROP TRIGGER g4a_guard_experimental_g3_exclusive_epoch_update")
            db.execute("UPDATE experimental_g3_exclusive_epoch SET epoch='different'")
    with pytest.raises(SqliteAuthorityHold): a.settle("lab:settle",[evidence(issued)])
    assert counts(backend)==before


def crash_worker(path, stage, payload, action):
    a=open_authority(path)
    def crash(at):
        if at==stage: os._exit(86)
    a._fault=crash
    if action=="admit": admit(a)
    else: a.settle("lab:settle",[SimulatedEvidence(**payload)])


@pytest.mark.parametrize("stage", ["after_budget", "before_commit", "after_commit"])
@pytest.mark.parametrize("action", ["admit", "EFFECT_CONFIRMED", "NON_EXECUTION_CONFIRMED"])
def test_t04_t05_real_process_crash_and_replay(lab, stage, action):
    _,backend,a=lab
    issued=admit(a) if action!="admit" else None
    payload=asdict(evidence(issued,action)) if issued else None
    ctx=mp.get_context("spawn")
    p=ctx.Process(target=crash_worker,args=(backend._store_dir,stage,payload,action))
    p.start();p.join(20)
    assert p.exitcode==86
    reopened=open_authority(backend._store_dir)
    before=counts(backend)
    if action=="admit":
        assert before["g4a_current"]==(1 if stage=="after_commit" else 0)
        result=admit(reopened)
        assert admit(reopened)==result
        assert counts(backend)["g4a_outbox"]==1
    else:
        assert reopened.inspect("lab:g4a").version==(2 if stage=="after_commit" else 1)
        result=reopened.settle("lab:settle",[SimulatedEvidence(**payload)])
        assert reopened.settle("lab:settle",[SimulatedEvidence(**payload)])==result
        assert counts(backend)["g4a_outbox"]==2
        state=backend.get_state("lab:budget")
        assert state.reserved==0
        assert state.committed==(20 if action=="EFFECT_CONFIRMED" else 0)


def race_worker(path, payload, ready, go, queue):
    try:
        a=open_authority(path);ready.put(True);assert go.wait(15)
        result=a.settle("lab:settle",[SimulatedEvidence(**payload)])
        queue.put(("ok",asdict(result)))
    except Exception as exc: queue.put(("error",type(exc).__name__))


def test_t06_two_processes_settle_once(lab):
    _,backend,a=lab;issued=admit(a)
    ctx=mp.get_context("spawn");ready=ctx.Queue();go=ctx.Event();out=ctx.Queue()
    workers=[ctx.Process(target=race_worker,args=(backend._store_dir,asdict(evidence(issued)),ready,go,out)) for _ in range(2)]
    for p in workers:p.start()
    for _ in workers:assert ready.get(timeout=20)
    go.set();results=[out.get(timeout=20) for _ in workers]
    for p in workers:p.join(20);assert p.exitcode==0
    assert all(r[0]=="ok" for r in results) and results[0]==results[1]
    assert counts(backend)["g4a_outbox"]==2
    assert backend.get_state("lab:budget").committed==20


def test_t07_contradiction_trust_change_and_terminal_overwrite_rejected(lab):
    _,backend,a=lab;issued=admit(a);yes=evidence(issued)
    no=evidence(issued,"NON_EXECUTION_CONFIRMED","lab:no")
    for batch in ([yes,no],[replace(yes,trust_generation=2)]):
        with pytest.raises(SqliteAuthorityHold):a.settle("lab:settle",batch)
    with pytest.raises(SqliteAuthorityHold):
        ExperimentalG4AAuthority.open(backend,trust_generation=2,experimental_only=True)
    assert counts(backend)["g4a_outbox"]==1
    a.settle("lab:settle",[yes])
    with pytest.raises(SqliteAuthorityHold): a.settle("lab:other",[no])
    assert backend.get_state("lab:budget").committed==20


@pytest.mark.parametrize("mutation", ["operation_key","reservation_id","attempt_id","payload_hash","authority_epoch","evidence_id"])
def test_binding_rejects_substitution_without_mutation(lab,mutation):
    _,backend,a=lab;issued=admit(a);proof=evidence(issued)
    value="" if mutation=="evidence_id" else "foreign"
    with pytest.raises(SqliteAuthorityHold):a.settle("lab:settle",[replace(proof,**{mutation:value})])
    assert counts(backend)["g4a_outbox"]==1


@pytest.mark.parametrize("sql", [
    "UPDATE reservations SET state='COMMITTED'", "UPDATE resources SET committed='20'",
    "DELETE FROM reservations", "UPDATE g4a_current SET operation_state='SIMULATED_CONFIRMED'",
    "DELETE FROM g4a_outbox", "UPDATE g4a_commands SET input_digest='x'",
    "UPDATE g4a_meta SET trust_generation=2",
])
def test_t08_direct_sql_is_rejected(lab,sql):
    _,backend,a=lab;admit(a)
    with backend._connection() as db:
        with pytest.raises(sqlite3.DatabaseError):db.execute(sql)
    assert a.inspect("lab:g4a").budget_state=="RESERVED"


@pytest.mark.parametrize("method", ["commit","release","refund","reconcile"])
def test_t08_direct_budget_api_cannot_settle(lab,method):
    _,backend,a=lab;issued=admit(a)
    try:getattr(backend,method)("lab:g4a" if method=="reconcile" else issued.reservation_id)
    except sqlite3.DatabaseError:pass
    assert a.inspect("lab:g4a").budget_state=="RESERVED"
    assert backend.get_state("lab:budget").reserved==20


def test_t08_legacy_native_admission_is_fenced(lab):
    _,backend,a=lab
    legacy=ExperimentalSqliteOperationAuthority(backend,experimental_only=True)
    with pytest.raises((sqlite3.DatabaseError,SqliteAuthorityHold)):legacy.admit_created(record())
    assert counts(backend)["reservations"]==0
    assert admit(a).execution_authorized is False


def test_t09_no_provider_surface_or_execution_grant(lab,monkeypatch):
    from tenir_governance.operations.provider import MockProvider,StripeProvider
    def forbidden(*args,**kwargs):pytest.fail("provider called by G4-A")
    monkeypatch.setattr(MockProvider,"execute",forbidden)
    monkeypatch.setattr(StripeProvider,"execute",forbidden)
    _,_,a=lab;issued=admit(a)
    for name in ("dispatch","dispatch_and_execute","execute","provider"):
        assert not hasattr(a,name)
    with pytest.raises((TypeError,ValueError)):replace(issued,execution_authorized=True)
    terminal=a.settle("lab:settle",[evidence(issued)])
    assert terminal.execution_authorized is False and terminal.decision=="HOLD"


@pytest.mark.parametrize("corruption",["counter","event","snapshot","schema"])
def test_t10_restart_rejects_privileged_corruption(lab,corruption):
    _,backend,a=lab;admit(a)
    with backend._connection() as db:
        # Negative control: hostile DDL privileges are outside this trust boundary.
        db.create_function("tenir_g4a_write",0,lambda:1)
        if corruption=="counter":db.execute("UPDATE resources SET reserved='19'")
        elif corruption=="event":
            db.execute("DROP TRIGGER g4a_outbox_no_update")
            db.execute("UPDATE g4a_outbox SET event_json='{}'")
        elif corruption=="snapshot":
            db.execute("DROP TRIGGER experimental_native_no_update")
            db.execute("UPDATE experimental_native_operations SET record_json='{}'")
        else:db.execute("DROP TRIGGER g4a_guard_resources_update")
    with pytest.raises(SqliteAuthorityHold):open_authority(backend._store_dir)


def test_unknown_preserves_reservation_and_replay_result(lab):
    _,backend,a=lab;issued=admit(a)
    unknown=evidence(issued,"UNKNOWN")
    first=a.settle("lab:unknown",[unknown])
    assert first.operation_state=="SIMULATED_UNKNOWN" and first.budget_state=="RESERVED"
    terminal=a.settle("lab:settle",[evidence(issued,"EFFECT_CONFIRMED","lab:yes")])
    assert terminal.version==3 and backend.get_state("lab:budget").committed==20
    assert a.settle("lab:unknown",[unknown])==first
    assert counts(backend)["g4a_outbox"]==3


def test_request_collision_fails_closed(lab):
    _,backend,a=lab;issued=admit(a)
    a.settle("lab:settle",[evidence(issued)])
    with pytest.raises(SqliteAuthorityHold):
        a.settle("lab:settle",[evidence(issued,"NON_EXECUTION_CONFIRMED")])
    assert counts(backend)["g4a_outbox"]==2


def test_provision_and_open_are_explicit_no_auto_migration(lab):
    _,backend,a=lab
    with pytest.raises(SqliteAuthorityHold):ExperimentalG4AAuthority.open(backend,trust_generation=1)
    with pytest.raises(SqliteAuthorityHold):ExperimentalG4AAuthority.provision(backend,trust_generation=1,experimental_only=True)
    assert a.inspect_all()==[]


def gated_settlement_worker(path, payload, ready, go, result):
    a=open_authority(path)
    def before():
        ready.set();assert go.wait(15)
    a._before_guard=before
    try:
        a.settle("lab:settle",[SimulatedEvidence(**payload)])
        result.put("settled")
    except SqliteAuthorityHold:result.put("HOLD")


def quarantine_worker(source_path, database_path, observations, finished):
    """Observe actual kernel contention on the production lock acquisition.

    A worker-start signal is insufficient: the process could be descheduled
    before it attempts flock. Probe the same descriptor nonblockingly, then
    preserve the original blocking call. Never infer contention from elapsed
    time or from an acknowledgement that has not yet arrived.
    """
    from contextlib import closing
    import fcntl
    from unittest.mock import patch

    gate = os.stat(source_path + ".quarantine.gate.lock")
    real_flock = fcntl.flock

    def observed_flock(fd, operation):
        stat = os.fstat(fd)
        if (operation != fcntl.LOCK_EX
                or (stat.st_dev, stat.st_ino) != (gate.st_dev, gate.st_ino)):
            return real_flock(fd, operation)
        try:
            real_flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            observations.put({"phase": "contended", "inode": stat.st_ino,
                              "device": stat.st_dev})
            real_flock(fd, operation)
        else:
            observations.put({"phase": "uncontended", "inode": stat.st_ino,
                              "device": stat.st_dev})
        # Read only after acquiring the gate. WAL readers see the committed
        # state, not the parent's uncommitted changes. This also checks the
        # ordering of gate acquisition relative to the SQLite commit.
        with closing(sqlite3.connect(database_path)) as db:
            version = db.execute(
                "SELECT version FROM g4a_current WHERE operation_key='lab:g4a'"
            ).fetchone()[0]
            budget_state = db.execute("SELECT state FROM reservations").fetchone()[0]
        observations.put({"phase": "acquired", "version": version,
                          "budget_state": budget_state})

    with patch("fcntl.flock", observed_flock):
        DurableOperationRegistry(source_path).set_quarantine(True, locked_by="other-process")
    finished.set()


def test_t02_two_process_quarantine_commit_before_settlement(lab):
    source,backend,a=lab;issued=admit(a)
    ctx=mp.get_context("spawn");ready=ctx.Event();go=ctx.Event();result=ctx.Queue()
    p=ctx.Process(target=gated_settlement_worker,args=(backend._store_dir,asdict(evidence(issued)),ready,go,result))
    p.start();assert ready.wait(20)
    source.set_quarantine(True,locked_by="parent")
    go.set();assert result.get(timeout=20)=="HOLD"
    p.join(20);assert p.exitcode==0
    assert counts(backend)["g4a_outbox"]==1 and backend.get_state("lab:budget").committed==0


def test_t02_settlement_transaction_before_quarantine_commit(lab):
    source,backend,a=lab;issued=admit(a)
    ctx=mp.get_context("spawn");observations=ctx.Queue();finished=ctx.Event()
    p=ctx.Process(target=quarantine_worker,args=(
        source._store_path, backend._database_path, observations, finished))
    def inside(stage):
        if stage=="after_budget":
            p.start()
            observed = observations.get(timeout=20)
            gate = os.stat(source._store_path + ".quarantine.gate.lock")
            assert observed == {"phase": "contended", "inode": gate.st_ino,
                                "device": gate.st_dev}
            assert not finished.is_set()
    a._fault=inside
    try:
        result=a.settle("lab:settle",[evidence(issued)])
        assert result.operation_state=="SIMULATED_CONFIRMED"
        assert observations.get(timeout=20) == {
            "phase": "acquired", "version": 2, "budget_state": "COMMITTED"}
        assert finished.wait(20)
        p.join(20)
        assert p.exitcode==0
    finally:
        if p.pid is not None:
            p.join(5)
            if p.is_alive():
                p.terminate()
                p.join(5)
        observations.close()
        observations.join_thread()
    with pytest.raises(SqliteAuthorityHold):admit(a,"lab:other")
    assert counts(backend)["g4a_outbox"]==2


def test_direct_refund_and_new_reserve_cannot_mutate_g4(lab):
    _,backend,a=lab;issued=admit(a)
    a.settle("lab:settle",[evidence(issued)])
    with pytest.raises(sqlite3.DatabaseError):backend.refund(issued.reservation_id)
    with pytest.raises(sqlite3.DatabaseError):backend.reserve("lab:other",1,"lab:budget")
    assert backend.get_state("lab:budget").committed==20
    assert counts(backend)["reservations"]==1


@pytest.mark.parametrize("damage",["missing_mark","pending_release","malformed"])
def test_quarantine_unknown_is_fail_closed(lab,damage):
    source,backend,a=lab
    source.set_quarantine(True,locked_by="test")
    if damage=="missing_mark":Path(source._quarantine_path()).unlink()
    elif damage=="pending_release":Path(source._quarantine_release_pending_path()).write_text("pending")
    else:Path(source._quarantine_path()).write_text("{}")
    with pytest.raises(SqliteAuthorityHold):admit(a)
    assert counts(backend)["reservations"]==0


def test_changed_epoch_rejects_replay_of_committed_result(lab):
    source,backend,a=lab;issued=admit(a)
    a.settle("lab:settle",[evidence(issued)])
    mark=Path(source._store_path+".g3-sqlite-owner.json")
    payload=json.loads(mark.read_text());payload["epoch"]="stale";mark.write_text(json.dumps(payload))
    with pytest.raises(SqliteAuthorityHold):a.settle("lab:settle",[evidence(issued)])
    assert counts(backend)["g4a_outbox"]==2


def provision_crash_worker(path):
    class CrashProvision(ExperimentalG4AAuthority):
        def _fault(self,stage):
            if stage=="before_commit":os._exit(88)
    CrashProvision.provision(SQLiteConsensusBackend(path),trust_generation=1,experimental_only=True)


def test_provision_crash_preserves_original_g3_guards(tmp_path):
    source=DurableOperationRegistry(str(tmp_path/"source.json"));source.register(record())
    backend=SQLiteConsensusBackend(str(tmp_path/"budget"));backend.register_resource("lab:budget",100)
    G3ExperimentalExclusiveOwner(source,backend,experimental_only=True).activate()
    with backend._connection() as db:
        before=[tuple(r) for r in db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")]
    ctx=mp.get_context("spawn");p=ctx.Process(target=provision_crash_worker,args=(backend._store_dir,))
    p.start();p.join(20);assert p.exitcode==88
    with backend._connection() as db:
        after=[tuple(r) for r in db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")]
    assert after==before
    with pytest.raises(SqliteAuthorityHold):open_authority(backend._store_dir)


@pytest.mark.parametrize("bad",[True,0,-1,2**63])
def test_invalid_trust_generations_rejected(lab,bad):
    _,backend,_=lab
    with pytest.raises(SqliteAuthorityHold):
        ExperimentalG4AAuthority.open(backend,trust_generation=bad,experimental_only=True)


def test_no_late_effect_reopens_released_reservation(lab):
    _,backend,a=lab;issued=admit(a)
    first=a.settle("lab:no",[evidence(issued,"NON_EXECUTION_CONFIRMED")])
    with pytest.raises(SqliteAuthorityHold):a.settle("lab:late",[evidence(issued)])
    assert a.inspect("lab:g4a")==first
    state=backend.get_state("lab:budget")
    assert state.reserved==state.committed==0 and state.reservations[0]["state"]=="RELEASED"
