"""Disposable Linux root-controller test: G4-A simulated, no provider calls."""
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

import pytest

WRITER, WORKER, OTHER = 61021, 61022, 61023


def identity(uid):
    def change():
        os.setgroups([])
        os.setgid(uid)
        os.setuid(uid)
    return change


def record():
    from tenir_governance.operations.registry import OperationRecord
    return OperationRecord(idempotency_key="lab:uid", passage_id="lab:passage",
                           agent_id="lab:agent", action_type="simulation",
                           resource_id="lab:budget", amount=20.0)


def service_actor(base):
    os.umask(0o077)
    from tenir_governance.operations.registry import DurableOperationRegistry
    from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
    from tenir_governance.operations.experimental_g3_exclusive_owner import G3ExperimentalExclusiveOwner
    from tenir_governance.operations.experimental_g4a_settlement import ExperimentalG4AAuthority
    from tenir_governance.operations.experimental_g4a_uid_service import ExperimentalUidG4AService
    state = base / "state"
    if os.environ.get("UID_LAB_REOPEN") != "1":
        registry = DurableOperationRegistry(str(state / "source.json"))
        assert registry.register(record())
        backend = SQLiteConsensusBackend(str(state / "budget"))
        assert backend.register_resource("lab:budget", 100) == "REGISTERED"
        G3ExperimentalExclusiveOwner(registry, backend, experimental_only=True).activate()
        ExperimentalG4AAuthority.provision(backend, trust_generation=1, experimental_only=True)
    service = ExperimentalUidG4AService(state, base / "socket", worker_uids=[WORKER], trust_generation=1)
    fault_stage = os.environ.get("UID_LAB_FAULT_STAGE")
    if fault_stage:
        def exit_at_fault(stage):
            if stage == fault_stage:
                os._exit(86)
        service.authority._fault = exit_at_fault
    if os.environ.get("UID_LAB_CRASH_AFTER_ADMIT") == "1":
        original = service._dispatch
        def die_after_commit(request):
            result = original(request)
            if request.get("action") == "admit":
                os._exit(86)
            return result
        service._dispatch = die_after_commit
    service.open()
    print("READY", flush=True)
    try:
        for _ in range(int(os.environ.get("UID_LAB_SERVE_COUNT", "8"))):
            service.serve_one()
    finally:
        service.close()


def client_actor(base, action):
    if action in {"sql", "udf", "drop", "replace", "source", "marker", "wal", "shm"}:
        try:
            dbpath = base / "state/budget/consensus.sqlite3"
            if action in {"sql", "udf", "drop"}:
                with sqlite3.connect(str(dbpath)) as db:
                    if action == "udf":
                        db.create_function("tenir_g4a_write", 0, lambda: 1)
                        db.execute("UPDATE resources SET reserved='19' WHERE resource_id='lab:budget'")
                    elif action == "drop":
                        db.execute("DROP TRIGGER g4a_guard_resources_update")
                    else:
                        db.execute("INSERT INTO resources VALUES('lab:rogue','1','0','0')")
            elif action == "replace":
                os.replace(base / "worker/replacement", dbpath)
            else:
                target = {"source": base / "state/source.json",
                          "marker": base / "state/source.json.g3-sqlite-owner.json",
                          "wal": Path(str(dbpath) + "-wal"),
                          "shm": Path(str(dbpath) + "-shm")}[action]
                with target.open("ab") as stream:
                    stream.write(b"unauthorized")
        except (sqlite3.Error, OSError) as exc:
            print(json.dumps({"denied": True, "error": type(exc).__name__}))
            return
        raise AssertionError("worker modified protected state")
    command = {"schema": "tenir.g4a.uid-lab.v1", "action": "inspect", "operation_key": "lab:uid"}
    if action in ("admit", "replay", "gated-admit"):
        command = {"schema": command["schema"], "action": "admit", "record": record().to_dict(),
                   "attempt_id": "lab:attempt", "request_id": "lab:request"}
    if action == "invalid":
        command["sql"] = "DROP TABLE resources"
    if action in ("positive", "dispatch"):
        command = {"schema": command["schema"], "action": "positive_grant" if action == "positive" else "dispatch",
                   "operation_key": "lab:uid", "request_id": "lab:unauthorized-grant"}
    payload = json.dumps(command).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(5)
        conn.connect(str(base / "socket/g4a-lab.sock"))
        if action == "gated-admit":
            print("CLIENT_READY", flush=True)
            if sys.stdin.readline().strip() != "START":
                raise RuntimeError("client start gate missing")
        try:
            conn.sendall(struct.pack("!I", len(payload)) + payload)
            header = conn.recv(4)
            if len(header) != 4:
                print(json.dumps({"ok": False, "transport": "closed"}))
                return
            size = struct.unpack("!I", header)[0]
            parts = bytearray()
            while len(parts) < size:
                chunk = conn.recv(size - len(parts))
                if not chunk:
                    raise RuntimeError("truncated reply")
                parts.extend(chunk)
            print(parts.decode())
        except (ConnectionResetError, BrokenPipeError):
            print(json.dumps({"ok": False, "transport": "reset"}))


def test_uid_separation_real_processes():
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("requires disposable Linux root controller and setuid")
    with tempfile.TemporaryDirectory(prefix="tenir-b1-", dir="/run") as name:
        base = Path(name)
        base.chmod(0o755)
        for name, mode in (("state", 0o700), ("socket", 0o755)):
            path = base / name
            path.mkdir(mode=mode)
            os.chown(path, WRITER, WRITER)
            path.chmod(mode)
        worker_dir = base / "worker"
        worker_dir.mkdir(mode=0o700)
        os.chown(worker_dir, WORKER, WORKER)
        replacement = worker_dir / "replacement"
        replacement.write_bytes(b"hostile replacement")
        os.chown(replacement, WORKER, WORKER)
        env = dict(os.environ, TENIR_ENV="test", HOST_SCOPE="single-host")
        actor = str(Path(__file__).resolve())
        server = subprocess.Popen([sys.executable, actor, "service", str(base)],
                                  preexec_fn=identity(WRITER), env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert server.stdout.readline().strip() == "READY", server.stderr.read()
            def call(uid, action):
                proc = subprocess.run([sys.executable, actor, action, str(base)],
                                      preexec_fn=identity(uid), env=env, capture_output=True,
                                      text=True, timeout=15, check=True)
                return json.loads(proc.stdout)
            def state_hashes():
                return {str(p.relative_to(base / "state")): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in (base / "state").rglob("*") if p.is_file()}
            before = state_hashes()
            for action in ("sql", "udf", "drop", "replace", "source", "marker", "wal", "shm"):
                assert call(WORKER, action)["denied"] is True, action
            assert state_hashes() == before
            assert call(OTHER, "inspect")["ok"] is False
            assert call(WRITER, "inspect")["ok"] is False
            assert call(WORKER, "invalid")["ok"] is False
            assert call(WORKER, "positive")["ok"] is False
            assert call(WORKER, "dispatch")["ok"] is False
            assert state_hashes() == before
            first = call(WORKER, "admit")
            assert first["ok"] and first["receipt"]["decision"] == "HOLD"
            assert first["receipt"]["execution_authorized"] is False
            assert call(WORKER, "replay") == first
            assert call(WORKER, "inspect")["receipt"] == first["receipt"]
            assert server.wait(timeout=15) == 0, server.stderr.read()
        finally:
            if server.poll() is None:
                server.kill()
                server.wait()


@pytest.mark.parametrize("fault_stage,committed", [
    ("after_budget", False), ("before_commit", False),
    ("after_commit", True), ("after_dispatch", True),
])
def test_lost_admission_reply_requires_operator_socket_cleanup_and_exact_replay(fault_stage, committed):
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("requires disposable Linux root controller and setuid")
    with tempfile.TemporaryDirectory(prefix="tenir-b1-recovery-", dir="/run") as name:
        base = Path(name)
        base.chmod(0o755)
        for dirname, mode in (("state", 0o700), ("socket", 0o755)):
            path = base / dirname
            path.mkdir(mode=mode)
            os.chown(path, WRITER, WRITER)
            path.chmod(mode)
        actor = str(Path(__file__).resolve())
        env = dict(os.environ, TENIR_ENV="test", HOST_SCOPE="single-host")

        def launch(extra):
            process = subprocess.Popen([sys.executable, actor, "service", str(base)],
                                       preexec_fn=identity(WRITER), env=dict(env, **extra),
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            assert process.stdout.readline().strip() == "READY", process.stderr.read()
            return process

        def request(action):
            proc = subprocess.run([sys.executable, actor, action, str(base)],
                                  preexec_fn=identity(WORKER), env=env, capture_output=True,
                                  text=True, timeout=15, check=True)
            return json.loads(proc.stdout)

        trigger = ({"UID_LAB_CRASH_AFTER_ADMIT": "1"} if fault_stage == "after_dispatch"
                   else {"UID_LAB_FAULT_STAGE": fault_stage})
        server = launch(trigger)
        try:
            assert request("admit")["ok"] is False
            assert server.wait(timeout=15) == 86
            dbpath = base / "state/budget/consensus.sqlite3"
            with sqlite3.connect(str(dbpath)) as db:
                assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == int(committed)
                assert db.execute("SELECT COUNT(*) FROM g4a_commands").fetchone()[0] == int(committed)
                assert db.execute("SELECT COUNT(*) FROM g4a_outbox").fetchone()[0] == int(committed)
            # A crashed server leaves its socket. Only this trusted test
            # controller removes it after establishing the writer has exited.
            stale_socket = base / "socket/g4a-lab.sock"
            assert stale_socket.lstat().st_uid == WRITER
            stale_socket.unlink()
            server = launch({"UID_LAB_REOPEN": "1", "UID_LAB_SERVE_COUNT": "3"})
            first = request("replay")
            second = request("replay")
            inspected = request("inspect")
            assert first["ok"] and first == second == inspected
            assert first["receipt"]["execution_authorized"] is False
            assert server.wait(timeout=15) == 0, server.stderr.read()
            with sqlite3.connect(str(dbpath)) as db:
                assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 1
                assert db.execute("SELECT COUNT(*) FROM g4a_commands").fetchone()[0] == 1
                assert db.execute("SELECT COUNT(*) FROM g4a_outbox").fetchone()[0] == 1
        finally:
            if server.poll() is None:
                server.kill()
                server.wait()


def test_two_connected_worker_processes_replay_one_admission():
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("requires disposable Linux root controller and setuid")
    with tempfile.TemporaryDirectory(prefix="tenir-b1-race-", dir="/run") as name:
        base = Path(name)
        base.chmod(0o755)
        for dirname, mode in (("state", 0o700), ("socket", 0o755)):
            path = base / dirname
            path.mkdir(mode=mode)
            os.chown(path, WRITER, WRITER)
            path.chmod(mode)
        actor = str(Path(__file__).resolve())
        env = dict(os.environ, TENIR_ENV="test", HOST_SCOPE="single-host", UID_LAB_SERVE_COUNT="2")
        server = subprocess.Popen([sys.executable, actor, "service", str(base)],
                                  preexec_fn=identity(WRITER), env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        clients = []
        try:
            assert server.stdout.readline().strip() == "READY", server.stderr.read()
            for _ in range(2):
                clients.append(subprocess.Popen([sys.executable, actor, "gated-admit", str(base)],
                    preexec_fn=identity(WORKER), env=env, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            for client in clients:
                assert client.stdout.readline().strip() == "CLIENT_READY"
            for client in clients:
                client.stdin.write("START\n")
                client.stdin.flush()
            results = []
            for client in clients:
                output, error = client.communicate(timeout=15)
                assert client.returncode == 0, error
                results.append(json.loads(output))
            assert results[0] == results[1] and results[0]["ok"]
            assert results[0]["receipt"]["execution_authorized"] is False
            assert server.wait(timeout=15) == 0, server.stderr.read()
            with sqlite3.connect(str(base / "state/budget/consensus.sqlite3")) as db:
                assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 1
                assert db.execute("SELECT COUNT(*) FROM g4a_commands").fetchone()[0] == 1
                assert db.execute("SELECT COUNT(*) FROM g4a_outbox").fetchone()[0] == 1
        finally:
            for client in clients:
                if client.poll() is None:
                    client.kill()
                    client.wait()
            if server.poll() is None:
                server.kill()
                server.wait()


if __name__ == "__main__":
    if sys.argv[1] == "service":
        service_actor(Path(sys.argv[2]))
    else:
        client_actor(Path(sys.argv[2]), sys.argv[1])
