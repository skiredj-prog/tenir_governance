"""Disposable Linux cross-UID test of dual-authority lab; no real effects."""
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

WRITER, WORKER = 61421, 61422
CONTEXT = {"pressure": 0.2, "velocity": 0.2, "capacity": 0.9,
           "authority": 0.9, "context_fit": 1.0, "evidence_quality": 1.0}


def _identity(uid):
    def change():
        os.setgroups([])
        os.setgid(uid)
        os.setuid(uid)
    return change


def _issuer(base):
    os.umask(0o077)
    from b1_dual_authority import GovernorReceiptIssuer
    from native_positive_permit import NativePositivePermitAuthority
    from tenir_governance.composition.governor import DualKernelGovernor, ScoringKernelAdapter
    from tenir_governance.membrane import TransitionMembrane, MembraneState, Agent, Resource
    from tenir_governance.operations.registry import DurableOperationRegistry
    from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
    from tenir_kernel.kernel import TenirKernel
    request = json.load(sys.stdin)
    budget = base / "state/budget"
    budget.mkdir(mode=0o700)
    backend = SQLiteConsensusBackend(str(budget))
    assert backend.register_resource("budget", 50) == "REGISTERED"
    native = NativePositivePermitAuthority(backend)
    registry = DurableOperationRegistry(str(base / "state/governor.json"))
    state = MembraneState()
    state.register_resource(Resource(resource_id="budget", capacity=50))
    state.register_agent(Agent(agent_id="alice", authority_score=0.9))
    governor = DualKernelGovernor(ScoringKernelAdapter(TenirKernel()), TransitionMembrane(state), registry=registry)
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(os.environ["TEST_GOVERNOR_PRIVATE"]))
    issuer = GovernorReceiptIssuer(governor, key, bytes.fromhex(os.environ["TEST_INDEPENDENT_PUBLIC"]),
                                   receipt_authority=native, policy_context=CONTEXT, allowed_decisions=("PASS", "FLAG"))
    print(json.dumps(issuer.issue(request)))


def _service(base):
    os.umask(0o077)
    from b1_dual_authority import DualApprovedUidService
    from tenir_governance.operations.registry import DurableOperationRegistry
    registry = DurableOperationRegistry(str(base / "state/governor.json"))
    service = DualApprovedUidService(base / "state", base / "socket", worker_uids=[WORKER],
                                     approval_public_key=bytes.fromhex(os.environ["TEST_INDEPENDENT_PUBLIC"]),
                                     governor_public_key=bytes.fromhex(os.environ["TEST_GOVERNOR_PUBLIC"]),
                                     governor_registry=registry, policy_context_hash=os.environ["TEST_POLICY_HASH"],
                                     allowed_decisions=("PASS", "FLAG"))
    service.open()
    print("READY", flush=True)
    try:
        for _ in range(int(os.environ.get("TEST_SERVE_COUNT", "1"))):
            service.serve_one()
    finally:
        service.close()


def _lock(base):
    os.umask(0o077)
    from tenir_governance.operations.registry import DurableOperationRegistry
    registry = DurableOperationRegistry(str(base / "state/governor.json"))
    registry.set_quarantine(True, locked_by="lab", incident_id="dual-incident")
    print(json.dumps({"active": registry.quarantine_is_active()}))


def _send(base):
    payload = sys.stdin.buffer.read()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(10)
        conn.connect(str(base / "socket/positive-lab.sock"))
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


@pytest.mark.parametrize("quarantine", [False, True])
def test_distinct_uid_service_requires_both_actual_governor_and_independent_approval(quarantine):
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("requires disposable Linux root controller and setuid")
    from b1_approved_positive_uid_service import canonical_approval
    from w2_2b_canonical import request_hash
    independent = Ed25519PrivateKey.from_private_bytes(bytes(range(96, 128)))
    governor_key = Ed25519PrivateKey.from_private_bytes(bytes(range(128, 160)))
    independent_public = independent.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    governor_public = governor_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    with tempfile.TemporaryDirectory(prefix="tenir-dual-uid-", dir="/run") as name:
        base = Path(name)
        base.chmod(0o755)
        for dirname, mode in (("state", 0o700), ("socket", 0o755)):
            path = base / dirname
            path.mkdir(mode=mode)
            os.chown(path, WRITER, WRITER)
            path.chmod(mode)
        command = {"schema": "tenir-dispatch-command/v1", "resource_id": "budget", "amount": 7,
                   "action_type": "payment", "agent_id": "alice", "metadata": {"recipient": "synthetic"}}
        now = int(time.time())
        body = {"schema": "tenir.b1.approval.v1", "audience": "positive-uid-laboratory",
                "approval_id": "approval", "nonce": "nonce", "operation_key": "lab:dual",
                "evaluation_id": "eval", "idempotency_key": "lab:dual", "request_hash": request_hash(command),
                "issued_at": now, "expires_at": now + 300, "key_id": hashlib.sha256(independent_public).hexdigest()}
        request = {"schema": "tenir.positive.approved.uid-lab.v1", "action": "submit", "operation_key": "lab:dual",
                   "evaluation_id": "eval", "idempotency_key": "lab:dual", "command": command,
                   "approval": {**body, "signature": independent.sign(canonical_approval(body)).hex()}}
        env = dict(os.environ, TENIR_ENV="test", HOST_SCOPE="single-host")
        for key in ("TENIR_W2_PRIVATE_KEY_HEX", "TENIR_WITNESS_PRIVATE_KEY_HEX", "TENIR_B3_PRIVATE_KEY_HEX",
                    "TEST_GOVERNOR_PRIVATE"):
            env.pop(key, None)
        writer_env = dict(env, TENIR_W2_PRIVATE_KEY_HEX=bytes(range(32)).hex(),
                          TENIR_WITNESS_PRIVATE_KEY_HEX=bytes(range(32, 64)).hex(),
                          TENIR_B3_PRIVATE_KEY_HEX=bytes(range(64, 96)).hex(),
                          TEST_INDEPENDENT_PUBLIC=independent_public.hex(), TEST_GOVERNOR_PUBLIC=governor_public.hex())
        actor = str(Path(__file__).resolve())
        result = subprocess.run([sys.executable, actor, "issuer", str(base)], preexec_fn=_identity(WRITER),
                                input=json.dumps(request), env=dict(writer_env, TEST_GOVERNOR_PRIVATE=bytes(range(128, 160)).hex()),
                                capture_output=True, text=True, timeout=20, check=True)
        receipt = json.loads(result.stdout)
        assert receipt["decision"] in ("PASS", "FLAG")
        if quarantine:
            locked = subprocess.run([sys.executable, actor, "lock", str(base)], preexec_fn=_identity(WRITER),
                                    env=writer_env, capture_output=True, text=True, timeout=20, check=True)
            assert json.loads(locked.stdout)["active"]
        server = subprocess.Popen([sys.executable, actor, "service", str(base)], preexec_fn=_identity(WRITER),
                                  env=dict(writer_env, TEST_POLICY_HASH=receipt["policy_context_hash"], TEST_SERVE_COUNT="2"),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert server.stdout.readline().strip() == "READY", server.stderr.read()
            def send(obj):
                result = subprocess.run([sys.executable, actor, "send", str(base)], preexec_fn=_identity(WORKER),
                                        input=json.dumps(obj), env=env, capture_output=True, text=True,
                                        timeout=20, check=True)
                return json.loads(result.stdout)
            assert not send(request)["ok"]  # independent approval alone is insufficient
            result = send({**request, "governor_receipt": receipt})
            if quarantine:
                assert not result["ok"]
            else:
                assert result["ok"] and result["receipt"]["status"] == "EXECUTED"
                assert result["receipt"]["synthetic"] is True
                assert result["receipt"]["execution_authorized"] is False
            assert server.wait(timeout=20) == 0, server.stderr.read()
            with sqlite3.connect(str(base / "state/synthetic-provider.sqlite3")) as db:
                assert db.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == (0 if quarantine else 1)
        finally:
            if server.poll() is None:
                server.kill()
                server.wait()


if __name__ == "__main__":
    {"issuer": _issuer, "service": _service, "lock": _lock, "send": _send}[sys.argv[1]](Path(sys.argv[2]))
