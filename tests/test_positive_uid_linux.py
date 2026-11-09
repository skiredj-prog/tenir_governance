"""Disposable Linux UID controller; exclusively synthetic local effects."""
import json
import os
from pathlib import Path
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile

import pytest

WRITER, WORKER, OTHER = 61121, 61122, 61123


def _identity(uid):
    def change():
        os.setgroups([])
        os.setgid(uid)
        os.setuid(uid)
    return change


def _request(command):
    return {"schema": "tenir.positive.uid-lab.v1", "action": "submit",
            "operation_key": "lab:uid-positive", "evaluation_id": "lab:eval",
            "idempotency_key": "lab:idem", "command": command}


def _command(amount=7):
    return {"schema": "tenir-dispatch-command/v1", "resource_id": "lab:budget",
            "amount": amount, "action_type": "payment", "agent_id": "lab:agent",
            "metadata": {"recipient": "lab:recipient"}}


def _service(base):
    os.umask(0o077)
    from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
    from positive_uid_service import ExperimentalPositiveUidService
    budget = base / "state/budget"
    if os.environ.get("UID_POSITIVE_REOPEN") != "1":
        budget.mkdir(mode=0o700)
        assert SQLiteConsensusBackend(str(budget)).register_resource("lab:budget", 50) == "REGISTERED"
    service = ExperimentalPositiveUidService(base / "state", base / "socket", worker_uids=[WORKER])
    crash = os.environ.get("UID_POSITIVE_CRASH")
    if crash:
        original = service.provider.execute
        def interrupted(*args, **kwargs):
            if crash == "after-effect":
                original(*args, **kwargs)
            os._exit(86)
        service.provider.execute = interrupted
    service.open()
    print("READY", flush=True)
    try:
        for _ in range(int(os.environ.get("UID_POSITIVE_SERVE_COUNT", "5"))):
            service.serve_one()
    finally:
        service.close()


def _client(base, action):
    if action == "state-read":
        try:
            (base / "state/budget/consensus.sqlite3").read_bytes()
        except PermissionError:
            print(json.dumps({"denied": True}))
            return
        raise AssertionError("worker read writer state")
    if action == "state-write":
        try:
            sqlite3.connect(str(base / "state/synthetic-provider.sqlite3"))
        except (PermissionError, sqlite3.OperationalError):
            print(json.dumps({"denied": True}))
            return
        raise AssertionError("worker opened provider database")
    obj = _request(_command(8 if action == "changed" else 7))
    if action == "direct-claim":
        obj = {"schema": obj["schema"], "action": "execute", "claim": "forged"}
    encoded = json.dumps(obj).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(10)
        conn.connect(str(base / "socket/positive-lab.sock"))
        try:
            conn.sendall(struct.pack("!I", len(encoded)) + encoded)
            header = conn.recv(4)
            if len(header) != 4:
                print(json.dumps({"ok": False}))
                return
            length = struct.unpack("!I", header)[0]
            data = bytearray()
            while len(data) < length:
                chunk = conn.recv(length - len(data))
                if not chunk:
                    raise RuntimeError("truncated response")
                data.extend(chunk)
            print(data.decode())
        except (BrokenPipeError, ConnectionResetError):
            print(json.dumps({"ok": False}))


def test_legacy_native_uid_service_refuses_before_any_effect():
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("requires disposable Linux root controller and setuid")
    with tempfile.TemporaryDirectory(prefix="tenir-positive-uid-", dir="/run") as name:
        base = Path(name)
        base.chmod(0o755)
        for name, mode in (("state", 0o700), ("socket", 0o755)):
            path = base / name
            path.mkdir(mode=mode)
            os.chown(path, WRITER, WRITER)
            path.chmod(mode)
        actor = str(Path(__file__).resolve())
        env = dict(os.environ, TENIR_ENV="test", HOST_SCOPE="single-host")
        for key in ("TENIR_W2_PRIVATE_KEY_HEX", "TENIR_WITNESS_PRIVATE_KEY_HEX", "TENIR_B3_PRIVATE_KEY_HEX"):
            env.pop(key, None)
        writer_env = dict(env, TENIR_W2_PRIVATE_KEY_HEX=bytes(range(32)).hex(),
                          TENIR_WITNESS_PRIVATE_KEY_HEX=bytes(range(32, 64)).hex(),
                          TENIR_B3_PRIVATE_KEY_HEX=bytes(range(64, 96)).hex())
        server = subprocess.Popen([sys.executable, actor, "service", str(base)], preexec_fn=_identity(WRITER),
                                  env=writer_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert server.stdout.readline().strip() == "READY", server.stderr.read()
            def call(uid, action):
                result = subprocess.run([sys.executable, actor, action, str(base)], preexec_fn=_identity(uid),
                                        env=env, capture_output=True, text=True, timeout=20, check=True)
                return json.loads(result.stdout)
            assert call(WORKER, "state-read")["denied"]
            assert call(WORKER, "state-write")["denied"]
            assert not call(OTHER, "submit")["ok"]
            assert not call(WORKER, "direct-claim")["ok"]
            # UID separation plus a native permit is not a Governor decision.
            # The positive cross-UID path is covered by the dual-authority
            # service, never by this retired endpoint.
            assert not call(WORKER, "submit")["ok"]
            assert not call(WORKER, "submit")["ok"]
            assert not call(WORKER, "changed")["ok"]
            assert server.wait(timeout=20) == 0, server.stderr.read()
            with sqlite3.connect(str(base / "state/synthetic-provider.sqlite3")) as db:
                assert db.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 0
            with sqlite3.connect(str(base / "state/budget/consensus.sqlite3")) as db:
                assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0
                assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_outbox").fetchone()[0] == 0
        finally:
            if server.poll() is None:
                server.kill()
                server.wait()


def test_retired_native_uid_service_never_reaches_a_crashable_provider():
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("requires disposable Linux root controller and setuid")
    with tempfile.TemporaryDirectory(prefix="tenir-positive-crash-", dir="/run") as name:
        base = Path(name)
        base.chmod(0o755)
        for name, mode in (("state", 0o700), ("socket", 0o755)):
            path = base / name
            path.mkdir(mode=mode)
            os.chown(path, WRITER, WRITER)
            path.chmod(mode)
        actor = str(Path(__file__).resolve())
        worker_env = dict(os.environ, TENIR_ENV="test", HOST_SCOPE="single-host")
        for key in ("TENIR_W2_PRIVATE_KEY_HEX", "TENIR_WITNESS_PRIVATE_KEY_HEX", "TENIR_B3_PRIVATE_KEY_HEX"):
            worker_env.pop(key, None)
        writer_env = dict(worker_env, TENIR_W2_PRIVATE_KEY_HEX=bytes(range(32)).hex(),
                          TENIR_WITNESS_PRIVATE_KEY_HEX=bytes(range(32, 64)).hex(),
                          TENIR_B3_PRIVATE_KEY_HEX=bytes(range(64, 96)).hex())
        def launch(extra):
            server = subprocess.Popen([sys.executable, actor, "service", str(base)], preexec_fn=_identity(WRITER),
                                      env=dict(writer_env, **extra), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            assert server.stdout.readline().strip() == "READY", server.stderr.read()
            return server
        def submit():
            result = subprocess.run([sys.executable, actor, "submit", str(base)], preexec_fn=_identity(WORKER),
                                    env=worker_env, capture_output=True, text=True, timeout=20, check=True)
            return json.loads(result.stdout)
        server = launch({"UID_POSITIVE_CRASH": "after-effect", "UID_POSITIVE_SERVE_COUNT": "1"})
        try:
            assert not submit()["ok"]
            assert server.wait(timeout=20) == 0, server.stderr.read()
            with sqlite3.connect(str(base / "state/synthetic-provider.sqlite3")) as db:
                assert db.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 0
            with sqlite3.connect(str(base / "state/budget/consensus.sqlite3")) as db:
                assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0
                assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_outbox").fetchone()[0] == 0
        finally:
            if server.poll() is None:
                server.kill()
                server.wait()


if __name__ == "__main__":
    if sys.argv[1] == "service":
        _service(Path(sys.argv[2]))
    else:
        _client(Path(sys.argv[2]), sys.argv[1])
