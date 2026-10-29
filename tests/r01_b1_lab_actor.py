"""Disposable R-01/B1 actor used from independent Linux EUID processes.

This is NOT a production writer service, does not expose IPC and cannot issue
a provider call. Bootstrap/admission runs only as the isolated writer EUID.
Hostile actor actions are intentionally outside application-level guardrails.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from decimal import Decimal

from tenir_governance.operations.registry import DurableOperationRegistry, OperationRecord
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from tenir_governance.operations.experimental_g3_exclusive_owner import G3ExperimentalExclusiveOwner
from tenir_governance.operations.experimental_g4a_settlement import ExperimentalG4AAuthority


def _record():
    return OperationRecord(
        idempotency_key="lab:b1", passage_id="lab:b1-passage",
        agent_id="lab:b1-agent",action_type="simulation",
        resource_id="lab:budget",amount=20.0,
    )


def _bootstrap(path: Path):
    source=DurableOperationRegistry(str(path/"source.json"))
    assert source.register(_record())
    backend=SQLiteConsensusBackend(str(path/"budget"))
    assert backend.register_resource("lab:budget",Decimal("100"))=="REGISTERED"
    G3ExperimentalExclusiveOwner(source,backend,experimental_only=True).activate()
    ExperimentalG4AAuthority.provision(
        backend,trust_generation=1,experimental_only=True)
    authority=ExperimentalG4AAuthority.open(
        backend,trust_generation=1,experimental_only=True)
    receipt=authority.admit_created(
        _record(),attempt_id="lab:attempt:b1",request_id="lab:admit:b1")
    assert receipt.execution_authorized is False and receipt.simulated
    return {"state":"HOLD","writer_euid":os.geteuid(),
            "budget_reserved":"20","provider_called":False}


def _sql(path: Path, action: str):
    dbpath=path/"budget"/"consensus.sqlite3"
    with sqlite3.connect(str(dbpath), timeout=5) as db:
        if action=="ordinary_budget":
            db.execute("UPDATE resources SET reserved='19' WHERE resource_id='lab:budget'")
        elif action=="ordinary_outbox":
            db.execute("UPDATE g4a_outbox SET event_json='{}'")
        elif action=="spoofed_udf":
            db.create_function("tenir_g4a_write",0,lambda:1)
            db.execute("UPDATE resources SET reserved='19' WHERE resource_id='lab:budget'")
        elif action=="drop_trigger":
            db.execute("DROP TRIGGER g4a_guard_resources_update")
            db.execute("UPDATE resources SET reserved='19' WHERE resource_id='lab:budget'")
        else:
            raise AssertionError("unsupported SQL action")
    return {"sql_commit":True}


def _replace(path:Path):
    target=path/"budget"/"consensus.sqlite3"
    # Create an identical SQLite backup, then rename it over the database.
    # Even without corrupting data, success proves the actor controls the
    # authority file inode (and can equally substitute hostile content).
    with tempfile.TemporaryDirectory(prefix="b1-actor-") as tmp:
        replacement=Path(tmp)/"replacement.sqlite3"
        with sqlite3.connect(str(target),timeout=5) as source:
            with sqlite3.connect(str(replacement)) as copy:
                source.backup(copy)
        prior=os.stat(target).st_ino
        os.replace(replacement,target)
        return {"replace_committed":True,"inode_changed":os.stat(target).st_ino!=prior}


def _direct_native(path:Path):
    authority=ExperimentalG4AAuthority.open(
        SQLiteConsensusBackend(str(path/"budget")),
        trust_generation=1,experimental_only=True,
    )
    return {"inspect":authority.inspect("lab:b1").decision}


def main():
    os.umask(0o077)
    action=sys.argv[1]
    path=Path(sys.argv[2]).resolve()
    try:
        if action=="bootstrap":
            result=_bootstrap(path)
        elif action in {"ordinary_budget","ordinary_outbox","spoofed_udf","drop_trigger"}:
            result=_sql(path,action)
        elif action=="replace":
            result=_replace(path)
        elif action=="direct_native":
            result=_direct_native(path)
        elif action=="direct_sql":
            with sqlite3.connect(str(path/"budget"/"consensus.sqlite3"),timeout=3) as db:
                db.execute("INSERT INTO resources VALUES('lab:rogue','1','0','0')")
            result={"sql_commit":True}
        elif action=="modify_source":
            with open(path/"source.json","r+",encoding="utf-8") as stream:
                stream.write("{}")
            result={"source_modified":True}
        elif action=="modify_witness":
            with open(path/"budget"/"consensus.sqlite3.witness.sqlite3","ab") as stream:
                stream.write(b"rogue")
            result={"witness_modified":True}
        elif action=="modify_marker":
            with open(path/"source.json.g3-sqlite-owner.json","w",encoding="utf-8") as stream:
                stream.write("{}")
            result={"marker_modified":True}
        else:
            raise ValueError("unknown actor action")
        print(json.dumps({"ok":True,"action":action,"euid":os.geteuid(),**result},
                         sort_keys=True))
    except BaseException as exc:
        print(json.dumps({"ok":False,"action":action,"euid":os.geteuid(),
                          "exception":type(exc).__name__,
                          "errno":getattr(exc,"errno",None),
                          "message":str(exc)[:500]},sort_keys=True))
    # Negative runner uses the machine-readable result, not process success,
    # to distinguish denied writes and committed privileged mutations.


if __name__=="__main__":
    main()
