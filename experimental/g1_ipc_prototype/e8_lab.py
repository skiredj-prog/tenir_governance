"""E8 laboratory witness: durable SQLite CAS, signed CMTs, HTTP transport.

NOT PRODUCTION: HTTP may only bind loopback; no authenticated TLS/mTLS, real
G1 grants, remote host isolation, quorum, or authorization to external effects.
"""
import base64
import hashlib
import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urlrequest
from urllib.error import URLError
from urllib.parse import urlparse

from cryptography.exceptions import InvalidSignature

DOMAIN = b'TENIR-E8-LAB-v0.1\x00'
EMPTY = 'sha256:' + '0' * 64


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return 'sha256:' + hashlib.sha256(DOMAIN + canonical(value)).hexdigest()


def sign(key, body):
    return 'ed25519:' + base64.b64encode(key.sign(DOMAIN + canonical(body))).decode()


def verify(key, body, signature):
    if not isinstance(signature, str) or not signature.startswith('ed25519:'):
        raise ValueError('invalid signature')
    raw = base64.b64decode(signature[8:], validate=True)
    if len(raw) != 64:
        raise ValueError('signature length')
    key.verify(raw, DOMAIN + canonical(body))


def head_digest(head):
    return digest(head)


class WitnessStore:
    def __init__(self, db, witness_key, engine_public_key, witness_id='e8-lab-01'):
        self.db, self.witness_key, self.engine_public_key, self.witness_id = db, witness_key, engine_public_key, witness_id
        with self.connection() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS heads (scope TEXT PRIMARY KEY, head_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tokens (scope TEXT NOT NULL, request_id TEXT NOT NULL,
                request_digest TEXT NOT NULL, response_json TEXT NOT NULL,
                PRIMARY KEY(scope, request_id));
            """)

    def connection(self):
        c = sqlite3.connect(self.db, timeout=10, isolation_level=None)
        c.execute('PRAGMA synchronous=FULL')
        c.execute('PRAGMA busy_timeout=10000')
        return c

    @staticmethod
    def scope(body):
        return body['realm_id'] + '\x00' + body['store_id']

    def provision_for_test(self, realm_id, store_id, epoch=5, state_digest=EMPTY, key_digest=EMPTY, quarantine_digest=EMPTY):
        """Offline-only test provisioning; NEVER accessible over HTTP."""
        head = dict(realm_id=realm_id, store_id=store_id, epoch=epoch, sequence=0,
                    state_digest=state_digest, key_digest=key_digest,
                    quarantine_digest=quarantine_digest, previous_head_digest=EMPTY)
        with self.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            cur = con.execute('SELECT head_json FROM heads WHERE scope=?', (self.scope(head),)).fetchone()
            if cur and json.loads(cur[0]) != head:
                con.execute('ROLLBACK')
                raise ValueError('refuse witness re-provisioning')
            if not cur:
                con.execute('INSERT INTO heads VALUES (?,?)', (self.scope(head), canonical(head).decode()))
            con.commit()
        return head

    def current_for_test(self, realm_id, store_id):
        with self.connection() as con:
            row = con.execute('SELECT head_json FROM heads WHERE scope=?', (realm_id + '\x00' + store_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def _response(self, body):
        return {'body': body, 'signature': sign(self.witness_key, body)}

    def pre_auth(self, envelope):
        body = envelope.get('body')
        if not isinstance(body, dict):
            raise ValueError('body required')
        required = {'protocol','realm_id','store_id','epoch','expected_head_digest','state_digest',
                    'key_digest','quarantine_digest','next_state_digest','request_id','transaction_id','payload_digest','grant_digest'}
        if set(body) != required or body['protocol'] != 'e8-lab-preauth-v0.1':
            raise ValueError('invalid preauth schema')
        for name in required - {'epoch'}:
            if not isinstance(body[name], str) or not body[name] or len(body[name]) > 256:
                raise ValueError('invalid preauth field: ' + name)
        if type(body['epoch']) is not int or body['epoch'] < 0:
            raise ValueError('invalid epoch')
        verify(self.engine_public_key, body, envelope.get('signature'))
        request_hash = digest(body)
        scope = self.scope(body)
        with self.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            existing = con.execute('SELECT request_digest,response_json FROM tokens WHERE scope=? AND request_id=?',
                                   (scope, body['request_id'])).fetchone()
            if existing:
                if existing[0] != request_hash:
                    return self._response(dict(decision='REJECTED', reason='E8_REQUEST_CONFLICT', request_digest=request_hash))
                old_response = json.loads(existing[1])
                current_row = con.execute('SELECT head_json FROM heads WHERE scope=?', (scope,)).fetchone()
                current_head = json.loads(current_row[0]) if current_row else None
                if current_head is None or head_digest(current_head) != old_response['body']['cmt']['new_head_digest']:
                    return self._response(dict(decision='REJECTED', reason='E8_SUPERSEDED_AUTHORIZATION', request_digest=request_hash))
                return old_response
            row = con.execute('SELECT head_json FROM heads WHERE scope=?', (scope,)).fetchone()
            if row is None:
                return self._response(dict(decision='REJECTED', reason='E8_UNKNOWN_STORE', request_digest=request_hash))
            old = json.loads(row[0])
            old_digest = head_digest(old)
            if body['epoch'] != old['epoch']:
                reason = 'E8_ROLLBACK_DETECTED' if body['epoch'] < old['epoch'] else 'E8_UNAPPROVED_EPOCH'
                return self._response(dict(decision='REJECTED', reason=reason, request_digest=request_hash))
            if body['expected_head_digest'] != old_digest:
                return self._response(dict(decision='REJECTED', reason='E8_HEAD_DIVERGENCE', request_digest=request_hash))
            if any(body[k] != old[k] for k in ('state_digest','key_digest','quarantine_digest')):
                return self._response(dict(decision='REJECTED', reason='E8_HEAD_DIVERGENCE', request_digest=request_hash))
            # The trusted old digest describes *actual committed business rows*.
            # The proposed next digest is computed by G1 over its predicted
            # post-transaction snapshot and signed as part of PRE_AUTH_CHECK.
            # Witness cannot independently verify G1's proposed state truth.
            if (not body['next_state_digest'].startswith('sha256:')
                    or len(body['next_state_digest']) != 71):
                return self._response(dict(decision='REJECTED', reason='E8_INVALID_NEXT_STATE', request_digest=request_hash))
            try:
                bytes.fromhex(body['next_state_digest'][7:])
            except ValueError:
                return self._response(dict(decision='REJECTED', reason='E8_INVALID_NEXT_STATE', request_digest=request_hash))
            new = dict(old, sequence=old['sequence']+1, previous_head_digest=old_digest,
                       state_digest=body['next_state_digest'])
            token = dict(protocol='e8-lab-cmt-v0.1', witness_id=self.witness_id,
                         realm_id=body['realm_id'], store_id=body['store_id'], epoch=body['epoch'],
                         sequence=new['sequence'], previous_head_digest=old_digest,
                         new_head_digest=head_digest(new), new_head=new,
                         request_id=body['request_id'], request_digest=request_hash,
                         transaction_id=body['transaction_id'], payload_digest=body['payload_digest'],
                         grant_digest=body['grant_digest'],
                         next_state_digest=body['next_state_digest'],
                         valid_until=int(time.time())+30)
            response = self._response({'decision':'APPROVED','request_digest':request_hash,'cmt':token})
            con.execute('UPDATE heads SET head_json=? WHERE scope=?', (canonical(new).decode(),scope))
            con.execute('INSERT INTO tokens VALUES (?,?,?,?)', (scope,body['request_id'],request_hash,canonical(response).decode()))
            con.commit()  # witness persistence precedes CMT acknowledgment
            return response


class WitnessClient:
    def __init__(self, url, engine_private_key, pinned_witness_public_key, timeout=1.5):
        parsed = urlparse(url)
        if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1','::1','localhost'):
            raise ValueError('LAB ONLY: witness transport restricted to HTTP loopback')
        self.url, self.engine_private_key = url.rstrip('/'), engine_private_key
        self.pinned_witness_public_key, self.timeout = pinned_witness_public_key, timeout

    def pre_auth(self, body):
        envelope = {'body':body,'signature':sign(self.engine_private_key,body)}
        req = urlrequest.Request(self.url+'/preauth', data=canonical(envelope),
                                 headers={'Content-Type':'application/json'}, method='POST')
        try:
            with urlrequest.urlopen(req, timeout=self.timeout) as response:
                data = response.read(32769)
                if len(data) > 32768:
                    raise ValueError('oversized witness response')
        except (URLError, OSError, TimeoutError) as exc:
            raise TimeoutError('WITNESS_OUTCOME_UNKNOWN') from exc
        try:
            result = json.loads(data)
            verify(self.pinned_witness_public_key, result['body'], result['signature'])
            result_body = result['body']
            if result_body['request_digest'] != digest(body):
                raise ValueError('wrong request digest')
            if result_body['decision'] == 'APPROVED':
                t = result_body['cmt']
                expected = {'realm_id':body['realm_id'],'store_id':body['store_id'],'epoch':body['epoch'],
                            'previous_head_digest':body['expected_head_digest'], 'request_id':body['request_id'],
                            'request_digest':digest(body), 'transaction_id':body['transaction_id'],
                            'payload_digest':body['payload_digest'],'grant_digest':body['grant_digest'],
                            'next_state_digest':body['next_state_digest']}
                if any(t.get(k) != v for k,v in expected.items()):
                    raise ValueError('CMT scope mismatch')
                if t['valid_until'] < int(time.time()) or t['protocol'] != 'e8-lab-cmt-v0.1':
                    raise ValueError('CMT stale or invalid')
                if (head_digest(t['new_head']) != t['new_head_digest']
                        or t['new_head']['sequence'] != t['sequence']
                        or t['new_head']['state_digest'] != body['next_state_digest']):
                    raise ValueError('CMT head mismatch')
            elif result_body['decision'] != 'REJECTED':
                raise ValueError('unexpected witness decision')
            return result_body
        except (InvalidSignature, KeyError, TypeError, ValueError, IndexError) as exc:
            raise TimeoutError('WITNESS_UNTRUSTED_RESPONSE') from exc


def make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != '/preauth':
                self.send_error(404)
                return
            try:
                length = int(self.headers.get('Content-Length','0'))
                if length < 1 or length > 32768:
                    raise ValueError('invalid frame')
                envelope = json.loads(self.rfile.read(length))
                response = store.pre_auth(envelope)
                content = canonical(response)
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except (ValueError, InvalidSignature, KeyError, TypeError):
                self.send_error(400, 'bad request')
        def log_message(self, format, *args):
            return
    return Handler


def start_loopback_test_server(store):
    server = ThreadingHTTPServer(('127.0.0.1',0), make_handler(store))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f'http://127.0.0.1:{server.server_port}'
