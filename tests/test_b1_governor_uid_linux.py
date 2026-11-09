"""Disposable Linux cross-UID Governor integration; synthetic effects only."""
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

WRITER, WORKER, OTHER = 61321, 61322, 61323


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
    assert SQLiteConsensusBackend(str(budget)).register_resource("budget", 100) == "REGISTERED"
    service = ApprovedPositiveUidService(base / "state", base / "socket", worker_uids=[WORKER],
                                         approval_public_key=bytes.fromhex(os.environ["TEST_APPROVAL_PUBLIC"]))
    service.open()
    print("READY", flush=True)
    try:
        service.serve_one()
    finally:
        service.close()


def _governor(base):
    from b1_governor_uid_provider import ExperimentalGovernorUidProvider
    from tenir_governance.composition.governor import DualKernelGovernor, ScoringKernelAdapter
    from tenir_governance.membrane import TransitionMembrane, MembraneState, Agent, Resource
    from tenir_governance.objects import Passage
    from tenir_governance.operations.registry import DurableOperationRegistry
    from tenir_kernel.kernel import TenirKernel
    request = json.load(sys.stdin)
    registry = DurableOperationRegistry()
    provider = ExperimentalGovernorUidProvider(base / "socket/positive-lab.sock", writer_uid=WRITER, registry=registry)
    state = MembraneState()
    state.register_resource(Resource(resource_id="budget", capacity=100))
    state.register_agent(Agent(agent_id="alice", authority_score=0.9))
    governor = DualKernelGovernor(ScoringKernelAdapter(TenirKernel()), TransitionMembrane(state),
                                  registry=registry, provider=provider)
    passage = Passage(agent_id="alice", action_type="payment", amount=7, idempotency_key="lab:governor")
    envelope = {"b1_lab_request": request}
    admitted = governor.adjudicate(passage, pressure=0.2, velocity=0.2, capacity=0.9,
                                   authority=0.9, context_fit=1.0, evidence_quality=1.0,
                                   resource_id="budget", amount=7, payload=envelope)
    result = governor.dispatch_and_execute(passage.idempotency_key, "budget", 7, metadata=envelope)
    print(json.dumps({"admitted": admitted["operation_state"], "state": result.get("operation_state"),
                      "executed": result.get("executed")}))


def _bypass_quarantine(base):
    """Characterize current direct socket path; this is not an acceptance test."""
    from b1_governor_uid_provider import ExperimentalGovernorUidProvider
    from tenir_governance.operations.registry import DurableOperationRegistry
    request = json.load(sys.stdin)
    registry = DurableOperationRegistry(str(base / "worker/registry.json"))
    registry.set_quarantine(True, locked_by="lab", incident_id="cross-uid-incident")
    proxy = ExperimentalGovernorUidProvider(base / "socket/positive-lab.sock", writer_uid=WRITER, registry=registry)
    response = proxy._exchange(request)  # A worker can also send this wire request directly.
    print(json.dumps({"quarantine_active": registry.quarantine_is_active(),
                      "record_exists": registry.get("lab:governor") is not None,
                      "response": response}))


def _spoof_server(base):
    path = base / "socket/positive-lab.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        server.listen(1)
        print("READY", flush=True)
        server.settimeout(15)
        conn, _ = server.accept()
        with conn:
            conn.settimeout(5)
            print(json.dumps({"bytes_received": len(conn.recv(1024))}), flush=True)


def _spoof_client(base):
    from b1_governor_uid_provider import ExperimentalGovernorUidProvider
    from tenir_governance.operations.registry import DurableOperationRegistry
    proxy = ExperimentalGovernorUidProvider(base / "socket/positive-lab.sock", writer_uid=WRITER,
                                             registry=DurableOperationRegistry())
    try:
        proxy._exchange({"not": "secret"})
        raise AssertionError("spoofed server UID was accepted")
    except RuntimeError as exc:
        print(json.dumps({"denied": "UID refused" in str(exc)}))


def _base(prefix, owner):
    temporary = tempfile.TemporaryDirectory(prefix=prefix, dir="/run")
    base = Path(temporary.name)
    base.chmod(0o755)
    path = base / "socket"
    path.mkdir(mode=0o700)
    os.chown(path, owner, owner)
    path.chmod(0o700)
    return temporary, base


@pytest.mark.parametrize("mode", ["governor", "bypass-quarantine"])
def test_legacy_signed_service_refuses_governor_proxy_and_direct_worker(mode):
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("requires disposable Linux root controller and setuid")
    from b1_approved_positive_uid_service import canonical_approval
    from w2_2b_canonical import request_hash
    signer = Ed25519PrivateKey.from_private_bytes(bytes(range(96, 128)))
    public = signer.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    temporary, base = _base("tenir-gov-uid-", WRITER)
    try:
        (base / "socket").chmod(0o755)
        (base / "state").mkdir(mode=0o700)
        os.chown(base / "state", WRITER, WRITER)
        (base / "worker").mkdir(mode=0o700)
        os.chown(base / "worker", WORKER, WORKER)
        command = {"schema": "tenir-dispatch-command/v1", "resource_id": "budget", "amount": 7,
                   "action_type": "payment", "agent_id": "alice", "metadata": {"recipient": "lab"}}
        now = int(time.time())
        body = {"schema": "tenir.b1.approval.v1", "audience": "positive-uid-laboratory",
                "approval_id": "approval-governor", "nonce": "nonce-governor", "operation_key": "lab:governor",
                "evaluation_id": "external-test-eval", "idempotency_key": "lab:governor",
                "request_hash": request_hash(command), "issued_at": now, "expires_at": now + 300,
                "key_id": hashlib.sha256(public).hexdigest()}
        request = {"schema": "tenir.positive.approved.uid-lab.v1", "action": "submit", "operation_key": "lab:governor",
                   "evaluation_id": "external-test-eval", "idempotency_key": "lab:governor", "command": command,
                   "approval": {**body, "signature": signer.sign(canonical_approval(body)).hex()}}
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
            process = subprocess.run([sys.executable, actor, mode, str(base)], preexec_fn=_identity(WORKER),
                                     input=json.dumps(request), env=env, capture_output=True, text=True, timeout=20, check=True)
            response = json.loads(process.stdout)
            if mode == "governor":
                assert response["admitted"] == "reserved"
                assert response["state"] == "unknown"
                assert not response["executed"]
            else:
                assert response["quarantine_active"] is True
                assert response["record_exists"] is False
                assert response["response"]["ok"] is False
            assert server.wait(timeout=20) == 0, server.stderr.read()
            with sqlite3.connect(str(base / "state/synthetic-provider.sqlite3")) as db:
                assert db.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 0
            with sqlite3.connect(str(base / "state/budget/consensus.sqlite3")) as db:
                assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0
                assert db.execute("SELECT COUNT(*) FROM b1_approved_requests").fetchone()[0] == 0
                assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_outbox").fetchone()[0] == 0
        finally:
            if server.poll() is None:
                server.kill()
                server.wait()
    finally:
        temporary.cleanup()


def test_client_rejects_spoofed_server_uid_before_sending_request():
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("requires disposable Linux root controller and setuid")
    temporary, base = _base("tenir-gov-spoof-", OTHER)
    actor = str(Path(__file__).resolve())
    server = subprocess.Popen([sys.executable, actor, "spoof-server", str(base)], preexec_fn=_identity(OTHER),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert server.stdout.readline().strip() == "READY", server.stderr.read()
        os.chown(base / "socket", WRITER, WRITER)
        os.chmod(base / "socket", 0o755)
        os.chown(base / "socket/positive-lab.sock", WRITER, WRITER)
        os.chmod(base / "socket/positive-lab.sock", 0o666)
        result = subprocess.run([sys.executable, actor, "spoof-client", str(base)], preexec_fn=_identity(WORKER),
                                capture_output=True, text=True, timeout=20, check=True)
        assert json.loads(result.stdout)["denied"]
        assert json.loads(server.stdout.readline())["bytes_received"] == 0
        assert server.wait(timeout=15) == 0, server.stderr.read()
    finally:
        if server.poll() is None:
            server.kill()
            server.wait()
        temporary.cleanup()


if __name__ == "__main__":
    actions = {"service": _service, "governor": _governor,
               "bypass-quarantine": _bypass_quarantine,
               "spoof-server": _spoof_server, "spoof-client": _spoof_client}
    actions[sys.argv[1]](Path(sys.argv[2]))
