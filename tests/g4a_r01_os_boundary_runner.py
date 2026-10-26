"""Linux-root disposable R-01 OS boundary probe on the ACTUAL G4-A lab store.

This is an OS-filesystem test, NOT an integrated G1-IPC or E8 witness test.
The engine UID remains capable of direct SQLite writes: R-01's same-UID
privileged threat is NOT prevented by POSIX permissions.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import sqlite3
import subprocess
import sys
import tempfile

from tenir_governance.operations.experimental_g4a_settlement import (
    ExperimentalG4AAuthority,
)
from tenir_governance.operations.experimental_g3_exclusive_owner import (
    G3ExperimentalExclusiveOwner,
)
from tenir_governance.operations.registry import DurableOperationRegistry, OperationRecord
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend


def record():
    return OperationRecord(
        idempotency_key="lab:r01", passage_id="lab:passage",
        agent_id="lab:agent", action_type="simulation",
        resource_id="lab:budget", amount=20.0,
    )


def verifier(*, authorized_by, scope, authorization, reason, lock_id):
    if authorization != "LAB-RELEASE" or not lock_id:
        return None
    return dict(
        authorized=True, principal=authorized_by, scope=scope,
        lock_id=lock_id, authorization_id="lab-release-" + lock_id,
    )


def as_identity(user, script, args=(), *, timeout=20, import_path=""):
    def drop():
        os.initgroups(user.pw_name, user.pw_gid)
        os.setgid(user.pw_gid)
        os.setuid(user.pw_uid)
    return subprocess.run(
        [sys.executable, "-c", script, *map(str, args)],
        capture_output=True, text=True, timeout=timeout,
        preexec_fn=drop, cwd="/tmp",
        env=dict(os.environ, TENIR_ENV="test", HOST_SCOPE="single-host",
                 PYTHONPATH=str(import_path)),
    )


ATTACK_SCRIPT = r"""
import errno, json, os, pathlib, sqlite3, sys
path = pathlib.Path(sys.argv[1])
mode = sys.argv[2]
try:
    if mode == 'direct-sql':
        con = sqlite3.connect(str(path), timeout=1)
        con.execute('CREATE TABLE r01_bypass(x INTEGER)')
        con.commit()
    elif mode == 'direct-write':
        with open(path, 'r+b') as f:
            f.seek(0); f.write(b'R01')
    elif mode == 'replace':
        tmp = path.with_name(path.name + '.attacker-replacement')
        tmp.write_bytes(b'REPLACED')
        os.replace(tmp, path)
    else:
        raise ValueError('unsupported probe')
    print(json.dumps({'result':'BYPASS','path':str(path),'mode':mode}))
    sys.exit(9)
except (OSError, sqlite3.Error) as exc:
    print(json.dumps({'result':'DENIED','path':str(path),'mode':mode,
                      'error_type':type(exc).__name__,
                      'errno':getattr(exc,'errno',None)}))
    sys.exit(0)
"""


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--engine-user", required=True)
    p.add_argument("--worker-user", required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if sys.platform != "linux" or os.geteuid() != 0:
        p.error("Linux root is required for a disposable OS sandbox")
    engine = pwd.getpwnam(args.engine_user)
    worker = pwd.getpwnam(args.worker_user)
    if (engine.pw_uid == worker.pw_uid
            or engine.pw_uid == 0 or worker.pw_uid == 0):
        p.error("distinct non-root engine and worker UIDs required")

    args.output.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="g4a-r01-os-", dir="/tmp"))
    os.chmod(base, 0o755)
    observations = []
    try:
        # The GitHub checkout is intentionally private to the runner UID.
        # Stage a ROOT-OWNED, read-only copy of the exact checked-out source
        # into an accessible sandbox import directory. Do not chmod or expose
        # the checkout itself, and do not use the editable pip finder.
        import_root = base / "importable"
        import_root.mkdir(mode=0o755)
        repo_root = Path(__file__).resolve().parents[1]
        for package in ("tenir_governance", "tenir_kernel"):
            shutil.copytree(repo_root / package, import_root / package)
        for root, directories, files in os.walk(import_root):
            os.chmod(root, 0o755)
            for filename in files:
                os.chmod(os.path.join(root, filename), 0o644)

        source_dir = base / "source"
        source_dir.mkdir(mode=0o700)
        budget_dir = base / "budget"
        budget_dir.mkdir(mode=0o700)
        source_path = source_dir / "source.json"

        source = DurableOperationRegistry(
            str(source_path), quarantine_release_verifier=verifier
        )
        assert source.register(record())
        backend = SQLiteConsensusBackend(str(budget_dir))
        backend.register_resource("lab:budget", Decimal("100"))
        G3ExperimentalExclusiveOwner(
            source, backend, experimental_only=True
        ).activate()
        ExperimentalG4AAuthority.provision(
            backend, trust_generation=1, experimental_only=True
        )

        # Generate a REAL valid inactive quarantine snapshot to probe
        # sidecar mutation, without blocking the positive engine control.
        source.set_quarantine(True, locked_by="lab")
        assert source.authorized_release_quarantine(
            authorized_by="lab-operator", scope="lab",
            authorization="LAB-RELEASE", reason="fixture"
        )["released"] is True

        # The engine UID owns the complete protected store and all parents
        # except the disposable base. The worker has no directory traverse.
        for directory in (source_dir, budget_dir):
            for root, dirs, files in os.walk(directory):
                os.chown(root, engine.pw_uid, engine.pw_gid)
                os.chmod(root, 0o700)
                for filename in files:
                    path = os.path.join(root, filename)
                    os.chown(path, engine.pw_uid, engine.pw_gid)
                    os.chmod(path, 0o600)
        db = Path(backend._database_path)
        assert db.exists()
        sidecars = sorted(
            path for path in source_dir.iterdir()
            if path.is_file() and path != source_path
        )
        paths = [db, source_path, *sidecars]
        assert len(sidecars) >= 3, "no real G3/quarantine sidecar inventory"

        before = {str(path): _sha(path) for path in paths}
        for path in paths:
            for mode in (("direct-sql", "replace") if path == db
                         else ("direct-write", "replace")):
                result = as_identity(worker, ATTACK_SCRIPT, (path, mode))
                try:
                    detail = json.loads(result.stdout.strip())
                except json.JSONDecodeError:
                    detail = {"result": "INVALID_OUTPUT", "stdout": result.stdout}
                detail.update(
                    returncode=result.returncode,
                    stderr=result.stderr[-1000:],
                    path_role="G4A_SQLITE" if path == db else "G3_SIDECAR_OR_SOURCE",
                )
                observations.append(detail)
                if result.returncode != 0 or detail.get("result") != "DENIED":
                    raise AssertionError(f"R01 worker bypass: {detail}")
        after = {str(path): _sha(path) for path in paths}
        assert before == after, "R01 denied worker nevertheless changed protected bytes"

        # Positive control: actual G4-A admission as engine UID.
        positive_script = """
import json, sys
from tenir_governance.operations.experimental_g4a_settlement import ExperimentalG4AAuthority
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from tenir_governance.operations.registry import OperationRecord
a=ExperimentalG4AAuthority.open(
    SQLiteConsensusBackend(sys.argv[1]),trust_generation=1,experimental_only=True)
r=OperationRecord(idempotency_key='lab:r01',passage_id='lab:passage',
    agent_id='lab:agent',action_type='simulation',resource_id='lab:budget',amount=20.0)
out=a.admit_created(r,attempt_id='lab:r01-attempt',request_id='lab:r01-admit')
print(json.dumps({'operation_state':out.operation_state,
                  'execution_authorized':out.execution_authorized}))
"""
        result = as_identity(engine, positive_script, (budget_dir,), import_path=import_root)
        assert result.returncode == 0, {
            "stdout": result.stdout, "stderr": result.stderr,
        }
        positive = json.loads(result.stdout.strip())
        assert positive == {
            "operation_state": "RESERVED", "execution_authorized": False,
        }
        with sqlite3.connect(db) as con:
            assert con.execute("SELECT COUNT(*) FROM g4a_current").fetchone()[0] == 1

        # Same-UID privileged writer is NOT denied by OS isolation.
        # No mutation is made: access itself is sufficient to demonstrate
        # the residual authority boundary.
        same_uid_script = """
import os,sys
fd=os.open(sys.argv[1],os.O_RDWR)
os.close(fd)
print('SAME_UID_DB_WRITE_ACCESS')
"""
        same_uid = as_identity(engine, same_uid_script, (db,), import_path=import_root)
        assert same_uid.returncode == 0
        assert "SAME_UID_DB_WRITE_ACCESS" in same_uid.stdout

        report = {
            "assurance": "G4A_REAL_STORE_OS_WORKER_ONLY",
            "security_verdict": "WORKER_UID_BOUNDARY_PASS_SAME_UID_PRIVILEGED_OPEN",
            "engine_uid": engine.pw_uid, "worker_uid": worker.pw_uid,
            "negative_probes": observations,
            "negative_probe_count": len(observations),
            "worker_all_denied": True,
            "positive_control": positive,
            "same_uid_engine_can_open_db_for_write": True,
            "witness_independent": False,
            "g1_ipc_authenticated": False,
            "provider_effects": 0,
            "production_hold": True,
        }
        args.output.joinpath("g4a-r01-os-probes.json").write_text(
            json.dumps(report, indent=2, sort_keys=True)
        )
        print(json.dumps({
            "negative_probe_count": len(observations),
            "worker_all_denied": True,
            "positive_control": positive,
            "same_uid_write_access": True,
            "qualification": "WORKER_UID_ONLY_NOT_SYSTEM_P0",
        }, sort_keys=True))
        return 0
    finally:
        shutil.rmtree(base)


if __name__ == "__main__":
    raise SystemExit(main())
