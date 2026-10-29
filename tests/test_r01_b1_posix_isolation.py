"""R01-N01..N06 RED-first; separate Linux UIDs on disposable G4-A lab.

Passing pytest records the observations, INCLUDING privileged FAIL verdicts.
It never converts a committed hostile write into a safety PASS.
"""
from __future__ import annotations
import hashlib, json, os, sqlite3, subprocess, sys, tempfile
from pathlib import Path
import pytest

WRITER=61021
AGENT=61022
ACTOR=Path(__file__).with_name("r01_b1_lab_actor.py")
SOURCE_SHA="9d8bf1c462589ac497e3e03ece48d1d9ab0b9db3"


def _set_credentials(uid):
    def apply():
        os.setgroups([])
        os.setgid(uid)
        os.setuid(uid)
    return apply


def _run(action, root, uid):
    env=dict(os.environ)
    env.update(TENIR_ENV="test",HOST_SCOPE="single-host")
    repo=str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"]=os.pathsep.join([repo,env.get("PYTHONPATH","")])
    p=subprocess.run(
        [sys.executable,str(ACTOR),action,str(root)],
        preexec_fn=_set_credentials(uid), cwd=repo, env=env,
        capture_output=True,text=True,timeout=45)
    assert p.returncode==0 and p.stdout.strip(),(
        action,p.returncode,p.stdout[-1000:],p.stderr[-1000:])
    result=json.loads(p.stdout.strip().splitlines()[-1])
    return dict(returncode=p.returncode,result=result,
                stdout=p.stdout.strip()[-1800:],
                stderr=p.stderr.strip()[-1800:])


def _hash(root):
    dbpath=root/"budget"/"consensus.sqlite3"
    with sqlite3.connect(str(dbpath)) as db:
        schema=[list(r) for r in db.execute(
            "SELECT type,name,sql FROM sqlite_master ORDER BY type,name")]
        tables=[r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        rows={}
        for table in tables:
            name='"'+table.replace('"','""')+'"'
            table_rows=[]
            for r in db.execute("SELECT * FROM "+name):
                table_rows.append([
                    {"bytes_hex":bytes(v).hex()} if isinstance(v,bytes) else v
                    for v in r])
            table_rows.sort(key=lambda x:json.dumps(x,sort_keys=True,default=str))
            rows[table]=table_rows
    blob=json.dumps(dict(schema=schema,rows=rows),sort_keys=True,
                    separators=(",",":"),default=str).encode()
    return hashlib.sha256(blob).hexdigest()


def _report(root,vector,scope,verdict,before,after,actions,**extra):
    obj=dict(schema="tenir.r01.negative-evidence.v1",
             sut_sha=SOURCE_SHA,vector=vector,scope=scope,
             security_verdict=verdict,writer_uid=WRITER,untrusted_uid=AGENT,
             dir_owner=root.stat().st_uid,dir_mode=oct(root.stat().st_mode&0o777),
             pre_sha256=before,post_sha256=after,
             mutation_observed=before!=after,
             actions=actions,**extra)
    path=os.environ.get("R01_EVIDENCE_DIR")
    if path:
        folder=Path(path)
        folder.mkdir(parents=True,exist_ok=True)
        (folder/(vector+".json")).write_text(
            json.dumps(obj,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    print("R01_EVIDENCE "+json.dumps(
        dict(vector=vector,verdict=verdict,mutated=before!=after),sort_keys=True),
        flush=True)
    return obj


@pytest.fixture
def lab():
    if sys.platform!="linux" or os.geteuid()!=0:
        pytest.fail("R-01 requires root Linux CI controller: not skippable")
    assert ACTOR.is_file()
    with tempfile.TemporaryDirectory(prefix="r01-b1-",dir="/tmp") as temp:
        top=Path(temp)
        top.chmod(0o711)
        root=top/"writer-state"
        root.mkdir(mode=0o700)
        os.chown(root,WRITER,WRITER)
        root.chmod(0o700)
        boot=_run("bootstrap",root,WRITER)
        assert boot["result"]["ok"],boot
        assert boot["result"]["state"]=="HOLD"
        db=root/"budget"/"consensus.sqlite3"
        assert db.exists() and db.stat().st_uid==WRITER
        assert (root/"budget").stat().st_uid==WRITER
        assert (root/"budget").stat().st_mode&0o777==0o700
        yield root


@pytest.mark.parametrize("vector,action",[
    ("R01-N01","ordinary_budget"),
    ("R01-N02","ordinary_outbox"),
])
def test_ordinary_sql_update_denied_by_existing_triggers(lab,vector,action):
    before=_hash(lab)
    attempted=_run(action,lab,WRITER)
    after=_hash(lab)
    assert attempted["result"]["ok"] is False,attempted
    assert before==after
    _report(lab,vector,"same UID ordinary uninstrumented SQL",
            "PASS",before,after,[attempted],
            refusal_type=attempted["result"].get("exception"))


def test_n03_spoofed_udf_is_committed_privileged_bypass(lab):
    before=_hash(lab)
    hostile=_run("spoofed_udf",lab,WRITER)
    after=_hash(lab)
    assert hostile["result"]["ok"] is True,hostile
    assert after!=before
    reopened=_run("direct_native",lab,WRITER)
    assert reopened["result"]["ok"] is False,reopened
    _report(lab,"R01-N03","same-UID UDF spoof","FAIL",before,after,
            [hostile,reopened],committed=True,
            reopen_detects_not_prevents=True)


def test_n04_trigger_ddl_can_commit_without_authority(lab):
    before=_hash(lab)
    hostile=_run("drop_trigger",lab,WRITER)
    after=_hash(lab)
    assert hostile["result"]["ok"] is True,hostile
    assert after!=before
    reopened=_run("direct_native",lab,WRITER)
    assert reopened["result"]["ok"] is False,reopened
    _report(lab,"R01-N04","same-UID DDL/trigger removal",
            "FAIL",before,after,[hostile,reopened],committed=True,
            reopen_detects_not_prevents=True)


def test_n05_same_uid_can_replace_database_inode(lab):
    before=_hash(lab)
    hostile=_run("replace",lab,WRITER)
    after=_hash(lab)
    assert hostile["result"]["ok"] is True,hostile
    assert hostile["result"]["inode_changed"] is True,hostile
    _report(lab,"R01-N05","same-UID database file replacement",
            "FAIL",before,after,[hostile],inode_changed=True,
            no_claim_of_effect_mutation=before==after)


def test_n06_alternative_writer_inventory_does_not_claim_completeness(lab):
    from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
    from tenir_governance.operations.experimental_g4a_settlement import ExperimentalG4AAuthority
    assert callable(SQLiteConsensusBackend._connect)
    assert callable(SQLiteConsensusBackend._transaction)
    assert callable(ExperimentalG4AAuthority._guard)
    listed=[
        "ExperimentalG4AAuthority._guard (cooperative)",
        "SQLiteConsensusBackend._connect (direct SQLite connection)",
        "SQLiteConsensusBackend._transaction (alternate writer path)",
        "sqlite3.connect + create_function(tenir_g4a_write)",
        "SQLite DDL DROP TRIGGER",
        "os.replace consensus.sqlite3 by same UID",
    ]
    before=_hash(lab)
    after=_hash(lab)
    _report(lab,"R01-N06","independent writer inventory",
            "FAIL",before,after,[],discovered_paths=listed,
            all_entrypoints_fenced=False)
    assert before==after


@pytest.mark.parametrize("action",[
    "ordinary_budget","ordinary_outbox","spoofed_udf",
    "drop_trigger","replace","direct_sql","direct_native",
    "modify_source","modify_marker","modify_witness",
])
def test_distinct_uid_denied_sql_ddl_file_access(lab,action):
    before=_hash(lab)
    attempted=_run(action,lab,AGENT)
    after=_hash(lab)
    assert attempted["result"]["ok"] is False,attempted
    assert before==after
    assert attempted["result"]["exception"] in {
        "PermissionError","OperationalError","DatabaseError",
        "OSError","SqliteAuthorityHold","FileNotFoundError",
    },attempted
    _report(lab,"B1-OS-"+action.replace("_","-"),
            "independent Linux UID, 0700 owner dir",
            "PASS",before,after,[attempted],
            os_permission_denial=True,not_same_uid=True)


def test_writer_uid_can_read_its_own_native_store(lab):
    before=_hash(lab)
    owner=_run("direct_native",lab,WRITER)
    after=_hash(lab)
    assert owner["result"]["ok"],owner
    assert owner["result"]["inspect"]=="HOLD"
    assert before==after
    _report(lab,"B1-WRITER-HEALTH","trusted writer identity",
            "PASS",before,after,[owner],dispatch_authorized=False)


def test_same_uid_attacker_still_bypasses_posix_permissions(lab):
    before=_hash(lab)
    hostile=_run("spoofed_udf",lab,WRITER)
    after=_hash(lab)
    assert hostile["result"]["ok"] is True and before!=after
    _report(lab,"B1-SAME-UID-COUNTEREXAMPLE",
            "same Unix UID as writer","FAIL",before,after,
            [hostile],privilege_separation_required=True)
