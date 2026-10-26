"""G1-IPC cross-UID test worker. Run only in a disposable Linux sandbox.

The orchestrator MUST launch this file as the worker UID, not root/engine UID.
Exit 0 means all expected OS denials were observed; exit 1 means boundary failure.
"""
import argparse
import errno
import json
import os
import socket
import sqlite3
import sys
from pathlib import Path

DENIED = {errno.EACCES, errno.EPERM, errno.EROFS}

def attempt(label, action):
    try:
        action()
    except sqlite3.OperationalError as exc:
        # SQLite maps EACCES into OperationalError; require an access-related message.
        msg = str(exc).lower()
        if any(x in msg for x in ('unable to open database file', 'readonly', 'permission denied')):
            return {'test': label, 'result': 'PASS', 'error': str(exc)}
        return {'test': label, 'result': 'FAIL', 'error': str(exc)}
    except OSError as exc:
        if exc.errno in DENIED:
            return {'test': label, 'result': 'PASS', 'errno': exc.errno}
        return {'test': label, 'result': 'FAIL', 'errno': exc.errno, 'error': str(exc)}
    except Exception as exc:
        return {'test': label, 'result': 'FAIL', 'error': repr(exc)}
    return {'test': label, 'result': 'FAIL', 'error': 'write/replace unexpectedly permitted'}

def sqlite_write(db):
    # sqlite3.connect(mode=rw) must not create a new DB.
    con = sqlite3.connect(f'file:{db}?mode=rw', uri=True, timeout=1)
    try:
        con.execute('BEGIN IMMEDIATE')
        con.execute('CREATE TABLE IF NOT EXISTS g1_illicit_write (x INTEGER)')
        con.execute('INSERT INTO g1_illicit_write VALUES (1)')
        con.commit()
    finally:
        con.close()

def replace_with_temp(directory, target):
    # Creation in protected directory should be denied before replacement.
    temp = directory / ('.g1_attack_' + target.name)
    with open(temp, 'xb') as stream:
        stream.write(b'ATTACK')
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', type=Path, required=True)
    ap.add_argument('--expected-worker-uid', type=int, required=True)
    args = ap.parse_args()
    if os.getuid() != args.expected_worker_uid or os.getuid() == 0:
        print(json.dumps({'result':'INVALID_SETUP','uid':os.getuid()}))
        return 2
    db = args.db.resolve(strict=True)
    directory = db.parent
    rows = [attempt('OS-01_sqlite_write', lambda: sqlite_write(db))]
    for suffix in ('', '-wal', '-shm', '-journal'):
        target = Path(str(db) + suffix)
        rows.append(attempt('OS-02_replace_' + (suffix or 'db'), lambda target=target: replace_with_temp(directory, target)))
    rows.append(attempt('OS-03_create_temp', lambda: (directory / '.g1_direct_create').open('xb').close()))
    print(json.dumps({'worker_uid':os.getuid(),'db':str(db),'tests':rows},indent=2))
    return 0 if all(row['result']=='PASS' for row in rows) else 1

if __name__ == '__main__':
    sys.exit(main())
