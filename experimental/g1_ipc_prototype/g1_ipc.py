"""Experimental G1 IPC: local-only SQLite commit, no external effects."""
import argparse
import base64
import hashlib
import json
import os
import socket
import sqlite3
import struct
from pathlib import Path
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

MAX_FRAME = 65536

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()

def unique_json(raw):
    def pairs(items):
        out = {}
        for k, v in items:
            if k in out:
                raise ValueError('duplicate JSON key')
            out[k] = v
        return out
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))

def recv_exact(sock, count):
    buf = bytearray()
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            raise EOFError('truncated frame')
        buf.extend(chunk)
    return bytes(buf)

def receive(sock):
    size = struct.unpack('!I', recv_exact(sock, 4))[0]
    if size > MAX_FRAME:
        raise ValueError('oversized frame')
    return unique_json(recv_exact(sock, size))

def send(sock, value):
    data = canonical(value)
    if len(data) > MAX_FRAME:
        raise ValueError('oversized response')
    sock.sendall(struct.pack('!I', len(data)) + data)

def initialize(db):
    con = sqlite3.connect(db, timeout=10)
    con.execute('PRAGMA synchronous=FULL')
    con.executescript('''
    CREATE TABLE IF NOT EXISTS g1_idempotency_ledger (
      request_id TEXT PRIMARY KEY,
      epoch INTEGER NOT NULL,
      worker_uid INTEGER NOT NULL,
      operation TEXT NOT NULL,
      request_digest TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('COMMITTED','REJECTED')),
      result_digest TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS g1_local_commits (
      realm TEXT NOT NULL,
      epoch INTEGER NOT NULL,
      transaction_id TEXT NOT NULL,
      payload_digest TEXT NOT NULL,
      request_id TEXT NOT NULL UNIQUE,
      PRIMARY KEY(realm, transaction_id)
    );
    ''')
    con.close()

def validate(request, peer_uid, keys):
    if not isinstance(request, dict) or set(request) != {'header', 'auth', 'payload', 'envelope_signature'}:
        raise ValueError('envelope fields')
    h, a, p = request['header'], request['auth'], request['payload']
    if not all(isinstance(x, dict) for x in (h, a, p)):
        raise ValueError('invalid object')
    if set(h) != {'protocol_version', 'request_id', 'timestamp', 'realm', 'epoch', 'operation'}:
        raise ValueError('header fields')
    if set(a) != {'worker_uid', 'grant_signature'}:
        raise ValueError('auth fields')
    if set(p) != {'transaction_id', 'state_transition', 'payload_digest'}:
        raise ValueError('payload fields')
    if h['protocol_version'] != 'g1-ipc-v0.1' or h['operation'] != 'COMMIT':
        raise ValueError('unsupported protocol or operation')
    if type(a['worker_uid']) is not int or a['worker_uid'] != peer_uid:
        raise ValueError('peer UID mismatch')
    if type(h['epoch']) is not int or h['epoch'] < 0:
        raise ValueError('invalid epoch')
    if type(h['timestamp']) is not int:
        raise ValueError('invalid timestamp')
    if not all(isinstance(h[k], str) and h[k] and len(h[k]) <= 128 for k in ('request_id','realm')):
        raise ValueError('invalid header identifiers')
    if not isinstance(p['transaction_id'], str) or not p['transaction_id'] or len(p['transaction_id']) > 128:
        raise ValueError('invalid transaction id')
    if p['state_transition'] != {'from': 'PENDING', 'to': 'COMMITTED'}:
        raise ValueError('invalid transition')
    if not isinstance(p['payload_digest'], str) or not p['payload_digest'].startswith('sha256:') or len(p['payload_digest']) != 71:
        raise ValueError('invalid digest')
    try:
        bytes.fromhex(p['payload_digest'][7:])
    except ValueError as exc:
        raise ValueError('invalid hex digest') from exc
    # v0.1 has no signed grant claims. Do not misrepresent grant_signature as an authorized grant.
    if a['grant_signature'] != 'NOT_IMPLEMENTED':
        raise ValueError('grant protocol not implemented')
    key = keys.get(peer_uid)
    if key is None:
        raise ValueError('unknown peer')
    sig = request['envelope_signature']
    if not isinstance(sig, str) or not sig.startswith('ed25519:'):
        raise ValueError('invalid signature format')
    try:
        signature = base64.b64decode(sig[8:], validate=True)
        if len(signature) != 64:
            raise ValueError('invalid signature length')
        key.verify(signature, b'TENIR-G1-IPC-ENVELOPE-v0.1\x00' + canonical({k: request[k] for k in ('header','auth','payload')}))
    except (InvalidSignature, ValueError) as exc:
        raise ValueError('signature verification failed') from exc
    return h, p

def process(db, request, peer_uid, keys):
    h, p = validate(request, peer_uid, keys)
    digest = hashlib.sha256(canonical({k: request[k] for k in ('header','auth','payload')})).hexdigest()
    con = sqlite3.connect(db, timeout=10, isolation_level=None)
    try:
        con.execute('PRAGMA synchronous=FULL')
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT request_digest,status,result_digest FROM g1_idempotency_ledger WHERE request_id=?', (h['request_id'],)).fetchone()
        if row:
            con.execute('ROLLBACK')
            if row[0] != digest:
                return {'decision': 'CONFLICT', 'request_id': h['request_id']}
            return {'decision': row[1], 'request_id': h['request_id'], 'result_digest': row[2], 'replayed': True}
        existing = con.execute('SELECT request_id FROM g1_local_commits WHERE realm=? AND transaction_id=?', (h['realm'], p['transaction_id'])).fetchone()
        if existing:
            con.execute('ROLLBACK')
            return {'decision': 'CONFLICT', 'request_id': h['request_id'], 'reason': 'transaction_already_committed'}
        result = 'sha256:' + hashlib.sha256(canonical({'realm': h['realm'], 'transaction_id': p['transaction_id'], 'payload_digest': p['payload_digest']})).hexdigest()
        con.execute('INSERT INTO g1_local_commits VALUES (?,?,?,?,?)', (h['realm'],h['epoch'],p['transaction_id'],p['payload_digest'],h['request_id']))
        con.execute('INSERT INTO g1_idempotency_ledger (request_id,epoch,worker_uid,operation,request_digest,status,result_digest) VALUES (?,?,?,?,?,?,?)', (h['request_id'],h['epoch'],peer_uid,h['operation'],digest,'COMMITTED',result))
        con.execute('COMMIT')
        return {'decision':'COMMITTED','request_id':h['request_id'],'result_digest':result,'replayed':False}
    except Exception:
        if con.in_transaction:
            con.execute('ROLLBACK')
        raise
    finally:
        con.close()

def serve(db, sockpath, public_key, uid, once=False):
    initialize(db)
    keys = {uid: Ed25519PublicKey.from_public_bytes(Path(public_key).read_bytes())}
    if os.path.lexists(sockpath):
        raise RuntimeError('socket path already exists: refuse to unlink')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(sockpath)
        os.chmod(sockpath, 0o660)
        server.listen(16)
        while True:
            client, _ = server.accept()
            with client:
                try:
                    pid, peer_uid, peer_gid = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    req = receive(client)
                    reply = process(db, req, peer_uid, keys)
                except (ValueError, EOFError, sqlite3.Error) as exc:
                    reply = {'decision': 'DENIED', 'reason': type(exc).__name__}
                send(client, reply)
            if once:
                return

def client_request(sockpath, request):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(sockpath)
        send(sock, request)
        return receive(sock)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    parser.add_argument('--socket', required=True)
    parser.add_argument('--public-key', required=True)
    parser.add_argument('--worker-uid', required=True, type=int)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    serve(args.db, args.socket, args.public_key, args.worker_uid, args.once)
