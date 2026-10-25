"""Experimental G1 daemon with real HTTP loopback E8 preauthorization.

LAB ONLY: no OS-UID isolation proof, real grants, quarantine persistence,
remote host witness, full-store digest, or external provider dispatch.
"""
import argparse
import hashlib
import json
import os
import socket
import sqlite3
import struct
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from e8_lab import WitnessClient, canonical as e8_canonical, digest as e8_digest, head_digest
from g1_ipc import canonical, initialize, receive, send, validate


def initialize_guarded(db, *, realm, store_id, initial_head):
    """Explicit lab bootstrap. Operator must provision and independently pin in reality."""
    if initial_head['realm_id'] != realm or initial_head['store_id'] != store_id or initial_head['sequence'] != 0:
        raise ValueError('initial head scope/sequence mismatch')
    initialize(db)
    with sqlite3.connect(db, isolation_level=None) as con:
        con.execute('PRAGMA synchronous=FULL')
        con.execute('BEGIN IMMEDIATE')
        con.executescript("""
        CREATE TABLE IF NOT EXISTS g1_e8_head (
          scope TEXT PRIMARY KEY, head_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS g1_e8_hold (
          scope TEXT PRIMARY KEY, reason TEXT NOT NULL
        );
        """)
        scope=realm + '\x00' + store_id
        cur=con.execute('SELECT head_json FROM g1_e8_head WHERE scope=?',(scope,)).fetchone()
        if cur and json.loads(cur[0]) != initial_head:
            raise ValueError('refuse local head reset')
        if not cur:
            con.execute('INSERT INTO g1_e8_head VALUES (?,?)',(scope,e8_canonical(initial_head).decode()))


def process_guarded(db, request, peer_uid, keys, *, store_id, witness, crash_after_preauth=False):
    """Signed G1 IPC envelope, experimental E8 HTTP-loopback CAS, durable local commit.

    Witness CAS and local SQLite commit DO NOT share an atomic commit.
    """
    h,p=validate(request,peer_uid,keys)
    scope=h['realm']+'\x00'+store_id
    req_digest=hashlib.sha256(canonical({k:request[k] for k in ('header','auth','payload')})).hexdigest()
    con=sqlite3.connect(db,timeout=10,isolation_level=None)
    con.execute('PRAGMA synchronous=FULL')
    try:
        con.execute('BEGIN IMMEDIATE')
        hold=con.execute('SELECT reason FROM g1_e8_hold WHERE scope=?',(scope,)).fetchone()
        if hold:
            con.execute('ROLLBACK')
            return {'decision':'HARD_HOLD','reason':hold[0]}
        row=con.execute('SELECT request_digest,status,result_digest FROM g1_idempotency_ledger WHERE request_id=?',(h['request_id'],)).fetchone()
        if row:
            con.execute('ROLLBACK')
            if row[0] != req_digest:
                return {'decision':'CONFLICT','reason':'request_id_mismatch'}
            return {'decision':'LOCAL_COMMITTED_PREAUTHORIZED','replayed':True,'result_digest':row[2]}
        existing=con.execute('SELECT request_id FROM g1_local_commits WHERE realm=? AND transaction_id=?',
                             (h['realm'],p['transaction_id'])).fetchone()
        if existing:
            con.execute('ROLLBACK')
            return {'decision':'CONFLICT','reason':'transaction_already_committed'}
        local=con.execute('SELECT head_json FROM g1_e8_head WHERE scope=?',(scope,)).fetchone()
        if not local:
            con.execute('ROLLBACK')
            return {'decision':'HARD_HOLD','reason':'E8_UNPROVISIONED'}
        head=json.loads(local[0])
        body=dict(protocol='e8-lab-preauth-v0.1',realm_id=h['realm'],store_id=store_id,
                  epoch=h['epoch'],expected_head_digest=head_digest(head),
                  state_digest=head['state_digest'],key_digest=head['key_digest'],
                  quarantine_digest=head['quarantine_digest'],request_id=h['request_id'],
                  transaction_id=p['transaction_id'],payload_digest=p['payload_digest'],
                  grant_digest=e8_digest(request['auth']['grant_signature']))
        try:
            verdict=witness.pre_auth(body)
        except (TimeoutError,ValueError):
            con.execute('INSERT OR REPLACE INTO g1_e8_hold VALUES (?,?)', (scope,'WITNESS_OUTCOME_UNKNOWN'))
            con.commit()
            return {'decision':'HOLD_GLOBAL','reason':'WITNESS_OUTCOME_UNKNOWN'}
        if verdict['decision'] != 'APPROVED':
            reason=verdict['reason']
            con.execute('INSERT OR REPLACE INTO g1_e8_hold VALUES (?,?)',(scope,reason))
            con.commit()
            return {'decision':'HARD_HOLD','reason':reason}
        token=verdict['cmt']
        new_head=token['new_head']
        if crash_after_preauth:
            # A test-only fault after durable witness CAS but before SQLite COMMIT.
            raise RuntimeError('INJECTED_CRASH_AFTER_PREAUTH')
        result='sha256:'+hashlib.sha256(canonical({'realm':h['realm'],
             'transaction_id':p['transaction_id'],'payload_digest':p['payload_digest']})).hexdigest()
        con.execute('INSERT INTO g1_local_commits VALUES (?,?,?,?,?)',
                    (h['realm'],h['epoch'],p['transaction_id'],p['payload_digest'],h['request_id']))
        con.execute('INSERT INTO g1_idempotency_ledger (request_id,epoch,worker_uid,operation,request_digest,status,result_digest) VALUES (?,?,?,?,?,?,?)',
                    (h['request_id'],h['epoch'],peer_uid,h['operation'],req_digest,'COMMITTED',result))
        con.execute('UPDATE g1_e8_head SET head_json=? WHERE scope=?',(e8_canonical(new_head).decode(),scope))
        con.commit()
        # Preauthorization proves neither provider execution nor grant authorization.
        return {'decision':'LOCAL_COMMITTED_PREAUTHORIZED','result_digest':result,
                'witness_head_digest':token['new_head_digest'],'replayed':False}
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


def serve_guarded(db,sockpath,public_key,worker_uid,store_id,witness,once=False):
    keys={worker_uid: Ed25519PublicKey.from_public_bytes(Path(public_key).read_bytes())}
    if os.path.lexists(sockpath):
        raise RuntimeError('socket path already exists: refuse to unlink')
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as server:
        server.bind(sockpath)
        os.chmod(sockpath,0o660)
        server.listen(16)
        while True:
            client,_=server.accept()
            with client:
                try:
                    _,peer_uid,_=struct.unpack('3i',client.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
                    req=receive(client)
                    reply=process_guarded(db,req,peer_uid,keys,store_id=store_id,witness=witness)
                except (ValueError,EOFError,sqlite3.Error) as exc:
                    reply={'decision':'DENIED','reason':type(exc).__name__}
                send(client,reply)
            if once:
                return


if __name__=='__main__':
    a=argparse.ArgumentParser(description='LAB ONLY: pre-authorized G1 Unix daemon')
    a.add_argument('--db',required=True)
    a.add_argument('--socket',required=True)
    a.add_argument('--worker-public-key',required=True)
    a.add_argument('--worker-uid',type=int,required=True)
    a.add_argument('--store-id',required=True)
    a.add_argument('--witness-url',required=True)
    a.add_argument('--engine-secret-key',required=True,help='raw 32-byte Ed25519 test key, kept outside G1 DB')
    a.add_argument('--pinned-witness-public-key',required=True,help='raw 32-byte independent test public key')
    a.add_argument('--once',action='store_true')
    args=a.parse_args()
    witness=WitnessClient(args.witness_url, Ed25519PrivateKey.from_private_bytes(Path(args.engine_secret_key).read_bytes()),
                          Ed25519PublicKey.from_public_bytes(Path(args.pinned_witness_public_key).read_bytes()))
    serve_guarded(args.db,args.socket,args.worker_public_key,args.worker_uid,args.store_id,witness,args.once)
