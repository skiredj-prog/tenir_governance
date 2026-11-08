"""External signer, writer and worker separated by Linux UIDs; synthetic only."""
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

WRITER, WORKER, OTHER = 61221, 61222, 61223


def _identity(uid):
    def change():
        os.setgroups([])
        os.setgid(uid)
        os.setuid(uid)
    return change


def _service(base):
    os.umask(0o077)
    from b1_approved_positive_uid_service import ApprovedPositiveUidService
    from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
    budget = base / "state/budget"
    budget.mkdir(mode=0o700)
    assert SQLiteConsensusBackend(str(budget)).register_resource("budget", 50) == "REGISTERED"
    service = ApprovedPositiveUidService(base / "state", base / "socket", worker_uids=[WORKER],
                                         approval_public_key=bytes.fromhex(os.environ["TEST_APPROVAL_PUBLIC"]))
    service.open()
    print("READY", flush=True)
    try:
        for _ in range(6):
            service.serve_one()
    finally:
        service.close()


def _client(base):
    payload = sys.stdin.buffer.read()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(10)
        conn.connect(str(base / "socket/positive-lab.sock"))
        try:
            conn.sendall(struct.pack("!I", len(payload)) + payload)
            header = conn.recv(4)
            if len(header) != 4:
                print(json.dumps({"ok": False}))
                return
            size = struct.unpack("!I", header)[0]
            data = bytearray()
            while len(data) < size:
                chunk = conn.recv(size - len(data))
                if not chunk:
                    raise RuntimeError("truncated response")
                data.extend(chunk)
            print(data.decode())
        except (BrokenPipeError, ConnectionResetError):
            print(json.dumps({"ok": False}))


def test_worker_requires_independent_signature_and_exact_binding():
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("requires disposable Linux root controller and setuid")
    from b1_approved_positive_uid_service import canonical_approval
    from w2_2b_canonical import request_hash
    signer = Ed25519PrivateKey.from_private_bytes(bytes(range(96, 128)))
    public = signer.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    with tempfile.TemporaryDirectory(prefix="tenir-approval-uid-", dir="/run") as name:
        base = Path(name)
        base.chmod(0o755)
        for dirname, mode in (("state", 0o700), ("socket", 0o755)):
            path = base / dirname
            path.mkdir(mode=mode)
            os.chown(path, WRITER, WRITER)
            path.chmod(mode)
        env = dict(os.environ, TENIR_ENV="test", HOST_SCOPE="single-host")
        for key in ("TENIR_W2_PRIVATE_KEY_HEX", "TENIR_WITNESS_PRIVATE_KEY_HEX", "TENIR_B3_PRIVATE_KEY_HEX"):
            env.pop(key, None)
        writer_env = dict(env, TENIR_W2_PRIVATE_KEY_HEX=bytes(range(32)).hex(),
                          TENIR_WITNESS_PRIVATE_KEY_HEX=bytes(range(32, 64)).hex(),
                          TENIR_B3_PRIVATE_KEY_HEX=bytes(range(64, 96)).hex(), TEST_APPROVAL_PUBLIC=public.hex())
        actor = str(Path(__file__).resolve())
        server = subprocess.Popen([sys.executable, actor, "service", str(base)], preexec_fn=_identity(WRITER),
                                  env=writer_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert server.stdout.readline().strip() == "READY", server.stderr.read()
            def send(uid, obj):
                result = subprocess.run([sys.executable, actor, "client", str(base)], preexec_fn=_identity(uid),
                                        input=json.dumps(obj), env=env, capture_output=True, text=True,
                                        timeout=20, check=True)
                return json.loads(result.stdout)
            command = {"schema": "tenir-dispatch-command/v1", "resource_id": "budget", "amount": 7,
                       "action_type": "payment", "agent_id": "agent", "metadata": {"recipient": "target"}}
            request = {"schema": "tenir.positive.approved.uid-lab.v1", "action": "submit",
                       "operation_key": "op", "evaluation_id": "eval", "idempotency_key": "idem", "command": command}
            now = int(time.time())
            body = {"schema": "tenir.b1.approval.v1", "audience": "positive-uid-laboratory",
                    "approval_id": "approval-1", "nonce": "nonce-1", "operation_key": "op", "evaluation_id": "eval",
                    "idempotency_key": "idem", "request_hash": request_hash(command), "issued_at": now,
                    "expires_at": now + 300, "key_id": hashlib.sha256(public).hexdigest()}
            approval = {**body, "signature": signer.sign(canonical_approval(body)).hex()}
            valid = {**request, "approval": approval}
            assert not send(WORKER, request)["ok"]
            assert not send(WORKER, {**valid, "approval": {**approval, "signature": "00" * 64}})["ok"]
            assert not send(OTHER, valid)["ok"]
            assert send(WORKER, valid)["receipt"]["status"] == "EXECUTED"
            assert send(WORKER, valid)["receipt"]["status"] == "UNKNOWN"
            assert not send(WORKER, {**valid, "command": {**command, "amount": 8}})["ok"]
            assert server.wait(timeout=20) == 0, server.stderr.read()
            with sqlite3.connect(str(base / "state/synthetic-provider.sqlite3")) as db:
                assert db.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 1
        finally:
            if server.poll() is None:
                server.kill()
                server.wait()


if __name__ == "__main__":
    if sys.argv[1] == "service":
        _service(Path(sys.argv[2]))
    else:
        _client(Path(sys.argv[2]))
