"""G1-IPC sandbox orchestrator: root-only setup, then worker UID denial probe.

Requires Linux, root in a disposable VM/container, and existing dedicated UIDs.
Never point at an existing database or production directory.
"""
import argparse
import json
import os
import pwd
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--engine-user', required=True)
    p.add_argument('--worker-user', required=True)
    p.add_argument('--output', type=Path, required=True)
    args=p.parse_args()
    if sys.platform != 'linux' or os.geteuid() != 0:
        p.error('Linux root required for disposable sandbox provisioning')
    engine=pwd.getpwnam(args.engine_user)
    worker=pwd.getpwnam(args.worker_user)
    if engine.pw_uid == worker.pw_uid or engine.pw_uid == 0 or worker.pw_uid == 0:
        p.error('require distinct non-root UIDs')
    args.output.mkdir(parents=True, exist_ok=True)
    # Private ephemeral sandbox: no caller-controlled DB paths.
    base=Path(tempfile.mkdtemp(prefix='g1-ipc-', dir='/tmp'))
    dbdir=base/'db'
    try:
        os.chmod(base,0o755)
        dbdir.mkdir(mode=0o700)
        db=dbdir/'g1.sqlite'
        con=sqlite3.connect(db)
        con.execute('CREATE TABLE protected (id INTEGER PRIMARY KEY, value TEXT)')
        con.execute('INSERT INTO protected(value) VALUES (?)',('original',))
        con.commit()
        con.close()
        os.chown(dbdir,engine.pw_uid,engine.pw_gid)
        os.chmod(dbdir,0o700)
        os.chown(db,engine.pw_uid,engine.pw_gid)
        os.chmod(db,0o600)
        worker_script=Path(__file__).with_name('g1_os_worker_probe.py').resolve()
        # preexec_fn only for a single-threaded isolated test controller.
        def drop_privileges():
            os.initgroups(args.worker_user,worker.pw_gid)
            os.setgid(worker.pw_gid)
            os.setuid(worker.pw_uid)
        result=subprocess.run([sys.executable,str(worker_script),'--db',str(db),'--expected-worker-uid',str(worker.pw_uid)],capture_output=True,text=True,timeout=30,preexec_fn=drop_privileges)
        # Worker cannot resolve protected DB path: path.resolve(strict=True) may succeed
        # on Linux without directory read, but SQLite/open must fail. Record exact behavior.
        con=sqlite3.connect(db)
        count=con.execute('SELECT count(*) FROM protected').fetchone()[0]
        con.close()
        report={'engine_uid':engine.pw_uid,'worker_uid':worker.pw_uid,'returncode':result.returncode,'stdout':result.stdout,'stderr':result.stderr,'original_row_count':count,'sandbox_deleted':True}
        (args.output/'g1_os_probe.json').write_text(json.dumps(report,indent=2))
        print(json.dumps(report,indent=2))
        return 0 if result.returncode==0 and count==1 else 1
    finally:
        shutil.rmtree(base)

if __name__=='__main__':
    sys.exit(main())
