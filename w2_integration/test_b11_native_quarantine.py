"""Native B1 live quarantine; synthetic effects, no production claims."""
from contextlib import contextmanager
import multiprocessing
import os
from pathlib import Path
import sqlite3
import threading

import httpx
import pytest

from test_b11_native_dual_grant import (
    native_system, _approved, _envelope, _native_state, _observe_provider_calls,
)
from kernel_claim_provider import KernelClaimProofProvider
from native_positive_g4a import NativePositiveG4A
from native_positive_permit import NativePositivePermitAuthority
from tenir_governance.operations.registry import DurableOperationRegistry
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend


@pytest.fixture(autouse=True)
def _controlled_mutation(monkeypatch):
    # Only the test process can remove this check. No product bypass flag.
    if os.environ.get("B11Q_TEST_MUTANT") == "1":
        monkeypatch.setattr(NativePositivePermitAuthority, "_require_b1_live_quarantine",
                            lambda *args: None)
    elif os.environ.get("B11Q_TEST_MUTANT") == "commit":
        original = NativePositivePermitAuthority._require_b1_live_quarantine
        def omit_commit_recheck(authority, db, row, payload):
            original(authority, db, row, payload)
            authority._b1_quarantine_frames.frames[id(db)].clear()
        monkeypatch.setattr(NativePositivePermitAuthority, "_require_b1_live_quarantine",
                            omit_commit_recheck)


def _prepared(system):
    service, independent, world, backend = system
    wire = _approved(service, independent, "generic_http_call")
    envelope = _envelope(service, wire)
    request = envelope["request"]
    proof = service.authority.bind_b1_dual_envelope(envelope)
    return service, request, proof, backend


def _admit(authority, request, proof):
    return authority.admit_reserve_issue(
        request["operation_key"], request["evaluation_id"], request["command"],
        resource_id=request["command"]["resource_id"], dual_grant_id=proof["dual_grant_id"],
    )


def _claim(authority, request):
    return authority.consume_and_claim(
        request["operation_key"], request["command"], request["idempotency_key"],
    )


def test_native_quarantine_blocks_admission_with_previously_valid_proof(native_system):
    service, request, proof, backend = _prepared(native_system)
    service.gate.governor_registry.set_quarantine(True, locked_by="test")
    before = _native_state(backend)
    refusal = None
    try:
        _admit(service.authority, request, proof)
    except RuntimeError as exc:
        refusal = exc
    assert _native_state(backend) == before, "quarantine admission changed native I8/permit/outbox"
    assert refusal is not None and "quarantine" in str(refusal).lower()
    assert native_system[2].posts == []


@pytest.mark.parametrize("stage", ["admit", "claim", "send"])
def test_native_quarantine_rechecked_at_commit_after_activation_in_body(native_system, stage):
    service, request, proof, backend = _prepared(native_system)
    authority, registry = service.authority, service.gate.governor_registry
    if stage != "admit":
        _admit(authority, request, proof)
    if stage == "send":
        claim = _claim(authority, request)
    before = _native_state(backend)
    # Pause after the body's first live check by activating at a trusted seam
    # inside the body, before the final commit guard.
    if stage == "admit":
        run = lambda: authority.admit_reserve_issue(
            request["operation_key"], request["evaluation_id"], request["command"],
            resource_id="budget", dual_grant_id=proof["dual_grant_id"],
            _admit_hook=lambda db: registry.set_quarantine(True, locked_by="race"),
        )
    elif stage == "send":
        run = lambda: authority.grant_send_right(
            claim, _send_hook=lambda db: registry.set_quarantine(True, locked_by="race"))
    else:
        issue = authority.witness.issue
        def activate(*args, **kwargs):
            result = issue(*args, **kwargs)
            registry.set_quarantine(True, locked_by="race")
            return result
        authority.witness.issue = activate
        run = lambda: _claim(authority, request)
    refusal = None
    try:
        run()
    except RuntimeError as exc:
        refusal = exc
    assert _native_state(backend) == before, "quarantine activation during transaction was not fenced at commit"
    assert refusal is not None and "quarantine" in str(refusal).lower()
    assert native_system[2].posts == []


@pytest.mark.parametrize("damage", ["missing-registry", "corrupt-registry", "missing-mark", "corrupt-mark", "release-pending", "missing-binding"])
def test_native_quarantine_missing_or_damaged_state_fails_closed(native_system, damage):
    service, request, proof, backend = _prepared(native_system)
    registry = service.gate.governor_registry
    path = Path(registry._store_path)
    if damage == "missing-registry":
        path.unlink()
    elif damage == "corrupt-registry":
        path.write_text("{", encoding="utf-8")
    elif damage == "missing-binding":
        # Controlled pre-fence migration image; no product bypass setting.
        with backend._connection() as db:
            db.execute("DROP TABLE b1_native_quarantine_registry")
    else:
        registry.set_quarantine(True, locked_by="test")
        if damage == "missing-mark":
            Path(registry._quarantine_path()).unlink()
        elif damage == "corrupt-mark":
            Path(registry._quarantine_path()).write_text("{", encoding="utf-8")
        else:
            Path(registry._quarantine_release_pending_path()).write_text("pending", encoding="utf-8")
    before = _native_state(backend)
    with pytest.raises(RuntimeError, match="quarantine"):
        _admit(service.authority, request, proof)
    assert _native_state(backend) == before
    assert native_system[2].posts == []


def test_release_does_not_resurrect_old_native_epoch(native_system):
    service, request, proof, backend = _prepared(native_system)
    registry = service.gate.governor_registry
    def verifier(*, authorized_by, scope, authorization, reason, lock_id):
        return {"authorized": True, "principal": authorized_by, "scope": scope,
                "lock_id": lock_id, "authorization_id": "test-only-release"}
    registry._quarantine_release_verifier = verifier
    registry.set_quarantine(True, locked_by="test")
    assert registry.authorized_release_quarantine(
        authorized_by="test", scope="lab", authorization="synthetic"
    )["released"]
    before = _native_state(backend)
    with pytest.raises(RuntimeError, match="epoch changed"):
        _admit(service.authority, request, proof)
    assert _native_state(backend) == before


def test_native_quarantine_pin_cannot_be_repointed(native_system, tmp_path):
    service, request, proof, backend = _prepared(native_system)
    from b1_native_quarantine import bind_registry
    with pytest.raises(RuntimeError, match="binding changed"):
        with service.authority._write("test_repin") as db:
            bind_registry(db, DurableOperationRegistry(str(tmp_path / "other.json")))
    with backend._connection() as db:
        for statement in ("DELETE FROM b1_native_quarantine_registry",
                          "UPDATE b1_native_quarantine_registry SET path='other'"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute(statement)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink fixture; Windows requires additional link privileges")
def test_native_registry_alias_cannot_split_quarantine_sidecars(native_system, tmp_path):
    service, request, proof, backend = _prepared(native_system)
    from b1_native_quarantine import bind_registry
    alias = tmp_path / "alias.json"
    alias.symlink_to(service.gate.governor_registry._store_path)
    with pytest.raises(RuntimeError, match="symlink aliases"):
        with service.authority._write("test_alias") as db:
            bind_registry(db, DurableOperationRegistry(str(alias)))


def test_native_g4a_rechecks_quarantine_after_claim(native_system, tmp_path, monkeypatch):
    service, request, proof, backend = _prepared(native_system)
    gate = NativePositiveG4A(service.authority)
    grant = gate.evaluate_and_grant(
        request["operation_key"], request["evaluation_id"], request["command"],
        resource_id="budget", dual_grant_id=proof["dual_grant_id"],
    )
    original = service.authority.consume_and_claim
    def activate(*args, **kwargs):
        claim = original(*args, **kwargs)
        service.gate.governor_registry.set_quarantine(True, locked_by="after-claim")
        return claim
    monkeypatch.setattr(service.authority, "consume_and_claim", activate)
    monkeypatch.setenv("TENIR_B3_PRIVATE_KEY_HEX", bytes(range(64, 96)).hex())
    provider = KernelClaimProofProvider(db_path=str(tmp_path / "provider.db"),
                                       claim_verifier=service.authority.verify_dispatch_claim)
    calls = _observe_provider_calls(provider, monkeypatch)
    try:
        with pytest.raises(RuntimeError, match="quarantine"):
            gate.dispatch_to_b3(request["operation_key"], request["idempotency_key"],
                                request["command"], grant, provider)
    finally:
        provider.conn.close()
    assert calls == []
    with backend._connection() as db:
        assert db.execute("SELECT consumed FROM w2_positive_g4a_v1_grants").fetchone()[0] == 0
        assert db.execute("SELECT status FROM w2_positive_v1_outbox").fetchone()[0] == "PENDING"
        assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_events WHERE event='dispatch_send_granted'").fetchone()[0] == 0


def _activate_worker(path, attempted, done):
    import tenir_governance.operations.registry as module
    original = module.acquire_file_lock
    def observed(*args, **kwargs):
        attempted.set()  # reached OS-lock acquisition, not merely process start
        return original(*args, **kwargs)
    module.acquire_file_lock = observed
    DurableOperationRegistry(path).set_quarantine(True, locked_by="child")
    done.set()


def _native_worker(directory, request, proof, mode, ready, release, outcome):
    backend = SQLiteConsensusBackend(directory)
    authority = NativePositivePermitAuthority(backend)
    try:
        if mode in ("crash-before", "crash-after", "commit-first"):
            def checkpoint(operation):
                if operation == "positive_admit":
                    ready.set()
                    if not release.wait(15):
                        raise RuntimeError("checkpoint timeout")
                    if mode == "crash-before":
                        os._exit(79)
            backend._before_commit = checkpoint
        elif mode == "gate-busy":
            import tenir_governance.operations.registry as module
            original = module.acquire_file_lock
            def observed(*args, **kwargs):
                ready.set()
                return original(*args, **kwargs)
            module.acquire_file_lock = observed
        result = _admit(authority, request, proof)
        if mode == "crash-after":
            os._exit(80)
        outcome.put(("admitted", result["permit_id"]))
    except Exception as exc:
        outcome.put(("refused", str(exc)))


def _join(process):
    process.join(20)
    if process.is_alive():
        process.terminate()
        process.join(5)
        pytest.fail("child deadlocked or exceeded test deadline")


def test_native_writer_rolls_back_instead_of_waiting_on_busy_gate(native_system):
    service, request, proof, backend = _prepared(native_system)
    before = _native_state(backend)
    ctx = multiprocessing.get_context("spawn")
    attempted, release, output = ctx.Event(), ctx.Event(), ctx.Queue()
    process = ctx.Process(target=_native_worker, args=(backend._store_dir, request, proof,
                          "gate-busy", attempted, release, output))
    with service.gate.governor_registry.quarantine_guard():
        process.start()
        try:
            assert attempted.wait(15), "native child never reached gate acquisition"
            result = output.get(timeout=10)  # must finish while the gate is still held
        finally:
            _join(process)
        assert result[0] == "refused" and "quarantine" in result[1]
    assert process.exitcode == 0
    assert _native_state(backend) == before


def test_native_commit_lease_serializes_activation(native_system):
    service, request, proof, backend = _prepared(native_system)
    ctx = multiprocessing.get_context("spawn")
    ready, release, output = ctx.Event(), ctx.Event(), ctx.Queue()
    attempted, done = ctx.Event(), ctx.Event()
    writer = ctx.Process(target=_native_worker, args=(backend._store_dir, request, proof,
                         "commit-first", ready, release, output))
    emergency = ctx.Process(target=_activate_worker,
                            args=(service.gate.governor_registry._store_path, attempted, done))
    writer.start()
    try:
        assert ready.wait(15), "writer never reached precommit under its lease"
        emergency.start()
        assert attempted.wait(15), "emergency process never attempted the gate"
        assert not done.wait(0.2), "activation passed a native commit lease"
    finally:
        release.set()
        _join(writer)
        if emergency.pid is not None:
            _join(emergency)
    assert writer.exitcode == emergency.exitcode == 0
    assert output.get(timeout=5)[0] == "admitted"
    assert done.is_set() and service.gate.governor_registry.quarantine_is_active()
    before = _native_state(backend)
    with pytest.raises(RuntimeError, match="quarantine"):
        _claim(service.authority, request)
    assert _native_state(backend) == before


def test_slow_witness_does_not_block_emergency_quarantine(native_system, monkeypatch):
    service, request, proof, backend = _prepared(native_system)
    _admit(service.authority, request, proof)
    before = _native_state(backend)
    entered, release = threading.Event(), threading.Event()
    issue = service.authority.witness.issue
    results = []
    def suspended(*args, **kwargs):
        entered.set()
        assert release.wait(15)
        return issue(*args, **kwargs)
    def claim():
        try:
            _claim(service.authority, request)
            results.append("unexpected-claim")
        except RuntimeError as exc:
            results.append(str(exc))
    monkeypatch.setattr(service.authority.witness, "issue", suspended)
    thread = threading.Thread(target=claim)
    ctx = multiprocessing.get_context("spawn")
    attempted, done = ctx.Event(), ctx.Event()
    emergency = ctx.Process(target=_activate_worker,
                            args=(service.gate.governor_registry._store_path, attempted, done))
    thread.start()
    try:
        assert entered.wait(10)
        emergency.start()
        assert done.wait(10), "witness I/O held the emergency gate"
    finally:
        release.set()
        thread.join(20)
        if emergency.pid is not None:
            _join(emergency)
    assert not thread.is_alive() and emergency.exitcode == 0
    assert len(results) == 1 and "quarantine" in results[0]
    assert _native_state(backend) == before
    assert native_system[2].posts == []


@pytest.mark.parametrize("mode", ["crash-before", "crash-after"])
def test_process_death_releases_gate_and_recovery_cannot_send(native_system, mode):
    service, request, proof, backend = _prepared(native_system)
    ctx = multiprocessing.get_context("spawn")
    ready, release, output = ctx.Event(), ctx.Event(), ctx.Queue()
    worker = ctx.Process(target=_native_worker, args=(backend._store_dir, request, proof,
                         mode, ready, release, output))
    worker.start()
    try:
        assert ready.wait(15)
    finally:
        release.set()
        _join(worker)
    assert worker.exitcode == (79 if mode == "crash-before" else 80)
    attempted, done = ctx.Event(), ctx.Event()
    emergency = ctx.Process(target=_activate_worker,
                            args=(service.gate.governor_registry._store_path, attempted, done))
    emergency.start()
    try:
        assert done.wait(15), "dead writer retained the quarantine lease"
    finally:
        _join(emergency)
    assert emergency.exitcode == 0
    with backend._connection() as db:
        expected = int(mode == "crash-after")
        for table in ("reservations", "w2_positive_v1_permits", "b1_approved_requests"):
            assert db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == expected
        assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_outbox").fetchone()[0] == 0
    restarted = NativePositivePermitAuthority(SQLiteConsensusBackend(backend._store_dir))
    before = _native_state(backend)
    with pytest.raises(RuntimeError, match="quarantine"):
        _admit(restarted, request, proof)
    assert _native_state(backend) == before
    assert native_system[2].posts == []


def test_native_quarantine_blocks_send_with_zero_provider_requests(native_system):
    service, request, proof, backend = _prepared(native_system)
    _admit(service.authority, request, proof)
    claim = _claim(service.authority, request)
    service.gate.governor_registry.set_quarantine(True, locked_by="test")
    before = _native_state(backend)
    requests = []
    def observe(request):
        requests.append(request)
        return httpx.Response(200, json={"synthetic": True})
    refusal = None
    try:
        result = service.authority.grant_send_right(claim)
        if result == "GRANTED":
            with httpx.Client(transport=httpx.MockTransport(observe), trust_env=False) as client:
                client.post("https://provider.invalid/effect")
    except RuntimeError as exc:
        refusal = exc
    assert requests == [], "quarantine permitted a synthetic provider request"
    assert _native_state(backend) == before, "quarantine send changed native state"
    assert refusal is not None and "quarantine" in str(refusal).lower()


def test_quarantine_allows_unknown_replay_but_no_new_send_right(native_system):
    service, request, proof, backend = _prepared(native_system)
    _admit(service.authority, request, proof)
    original = _claim(service.authority, request)
    assert service.authority.grant_send_right(original) == "GRANTED"
    service.authority.mark_dispatch_unknown(original)
    service.gate.governor_registry.set_quarantine(True, locked_by="test")
    before = _native_state(backend)
    replay = _claim(service.authority, request)
    assert replay["status"] == "UNKNOWN"
    assert replay["execution_id"] == original["execution_id"]
    assert replay["claim_token"] == original["claim_token"]
    with pytest.raises(RuntimeError, match="quarantine"):
        service.authority.grant_send_right(replay)
    assert _native_state(backend) == before
    assert native_system[2].posts == []


def test_busy_commit_retries_only_local_claim_with_same_witness_identity(native_system, monkeypatch):
    service, request, proof, backend = _prepared(native_system)
    _admit(service.authority, request, proof)
    import native_positive_permit as module
    from b1_native_quarantine import QuarantineGateBusy
    original = module.commit_guard
    checks = []
    @contextmanager
    def busy_once(db, pending):
        checks.append(len(pending))
        if len(checks) == 2:  # first final-commit gate, after durable witness issue
            raise QuarantineGateBusy("test native quarantine gate busy")
        with original(db, pending):
            yield
    witnessed = []
    issue = service.authority.witness.issue
    def observe(*args, **kwargs):
        receipt = issue(*args, **kwargs)
        witnessed.append((receipt["record"]["execution_id"], receipt["record"]["claim_token"]))
        return receipt
    monkeypatch.setattr(module, "commit_guard", busy_once)
    monkeypatch.setattr(service.authority.witness, "issue", observe)
    claim = _claim(service.authority, request)
    assert len(witnessed) == 2 and witnessed[0] == witnessed[1]
    assert witnessed[0] == (claim["execution_id"], claim["claim_token"])
    assert len(checks) == 4
    with backend._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_outbox").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_events WHERE event='dispatch_claimed'").fetchone()[0] == 1
    assert native_system[2].posts == []
