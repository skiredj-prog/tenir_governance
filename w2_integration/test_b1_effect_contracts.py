"""User HTTP/Stripe contract tests; transports are exclusively local mocks."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import threading
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient

from b1_approved_positive_uid_service import canonical_approval
from b1_dual_authority import DualApprovedUidService, GovernorReceiptIssuer
from b1_effect_connectors import MockEffectConnector, MockStripeConnector
from b1_effects_lab import EffectContractLab, GrantRefused, create_effect_app
from native_dispatch_adapter import Ed25519ConnectorAttestor
from native_positive_permit import NativePositivePermitAuthority
from tenir_governance.composition.governor import DualKernelGovernor, ScoringKernelAdapter
from tenir_governance.membrane import TransitionMembrane, MembraneState, Agent, Resource
from tenir_governance.operations.registry import DurableOperationRegistry
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from tenir_kernel.kernel import TenirKernel


def _contract_process(root, operation, checkpoint, marker, results=None):
    monkeypatch = pytest.MonkeyPatch()
    fixture = system.__wrapped__(Path(root), monkeypatch)
    service, _, _, world, backend = next(fixture)
    try:
        connector = service.connectors["stripe_charge"]
        original = connector.execute
        if checkpoint == "before_commit":
            def stop_at_commit(name):
                if name == "grant_send_right":
                    os._exit(87)
            backend._before_commit = stop_at_commit

        def effect(wire):
            if checkpoint == "before_io":
                os._exit(87)
            result = original(wire)
            with open(marker, "a", encoding="utf-8") as stream:
                stream.write(wire["operation_id"] + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            if checkpoint == "after_effect":
                os._exit(87)
            return result

        connector.execute = effect
        result = service.run_once(operation)
        if results is not None:
            results.put(result["status"])
        elif checkpoint != "none":
            raise AssertionError("crash checkpoint not reached")
    finally:
        fixture.close()
        monkeypatch.undo()


class MeasuredLabScoring:
    """Known test measurements; no production adapter or policy is weakened."""
    def adjudicate(self, **values):
        coords = {**{key: values[key] for key in ("pressure", "velocity", "capacity", "authority", "context_fit", "evidence_quality")},
                  "option_space": 1.0, "cooperative_control_score": 1.0, "trajectory_risk_score": 0.0,
                  "irreversible_pathway_materiality": 0.0, "manipulation_risk": 0.0, "drift": 0.0}
        result = TenirKernel().adjudicate(pressure=values["pressure"], velocity=values["velocity"],
                                         capacity=values["capacity"], coordinates=coords, mode="ENFORCE")
        return {**result, "kernel": "scoring", "authority": values["authority"],
                "context_fit": values["context_fit"], "evidence_quality": values["evidence_quality"]}


class MockWorld:
    def __init__(self):
        self.mode = "success"
        self.posts, self.gets, self.effects = [], [], {}
        self.lock = threading.Lock()

    def handle(self, request):
        path = request.url.path
        with self.lock:
            if request.method == "GET":
                self.gets.append(path)
                if path == "/v1/payment_intents/search":
                    return httpx.Response(200, json={"has_more": False, "data": list(self.effects.values())})
                reference = path.rsplit("/", 1)[-1]
                for value in self.effects.values():
                    if reference in (value.get("id"), value.get("operation_id")):
                        return httpx.Response(200, json=value)
                return httpx.Response(404, json={"error": "unknown"})
            self.posts.append((path, dict(request.headers), request.content))
            if path == "/api/v1/effects/execute":
                wire = json.loads(request.content)
                operation, key = wire["operation_id"], wire["idempotency_key"]
                result = {"operation_id": operation, "idempotency_key": key, "status": "CONFIRMED",
                          "receipt": {"provider_reference": "effect-" + operation, "intent_hash": key}}
                failed = {"operation_id": operation, "idempotency_key": key, "status": "FAILED",
                          "error": {"code": "mock_pre_execution_rejection", "no_effect": True}}
            elif path == "/v1/payment_intents":
                form = parse_qs(request.content.decode())
                assert form["confirm"] == ["true"]
                operation, key = form["metadata[tenir_operation_id]"][0], form["metadata[tenir_intent_hash]"][0]
                assert request.headers["Idempotency-Key"] == operation
                result = {"id": "pi_" + operation.replace("-", ""), "object": "payment_intent", "livemode": False,
                          "amount": int(form["amount"][0]), "amount_received": int(form["amount"][0]),
                          "currency": form["currency"][0], "payment_method": form["payment_method"][0],
                          "status": "succeeded", "metadata": {"tenir_operation_id": operation, "tenir_intent_hash": key}}
                failed = {"error": {"code": "mock_pre_execution_rejection", "no_effect": True,
                                    "operation_id": operation, "intent_hash": key}}
            elif path.endswith("/confirm"):
                reference = path.split("/")[-2]
                result = next(value for value in self.effects.values() if value.get("id") == reference)
                operation = result["metadata"]["tenir_operation_id"]
                assert request.headers["Idempotency-Key"] == operation
                result.update(status="succeeded", amount_received=result["amount"])
                return httpx.Response(200, json=result)
            else:
                raise AssertionError("unexpected mock endpoint: " + path)
            if self.mode == "reject":
                return httpx.Response(400, json=failed)
            if self.mode == "bare400":
                return httpx.Response(400, json={"error": {"code": "ambiguous_bad_request"}})
            if self.mode == "timeout_no_effect":
                raise httpx.ReadTimeout("simulated lost response", request=request)
            if self.mode == "mismatch":
                if path.startswith("/v1/"):
                    result["metadata"]["tenir_intent_hash"] = "f" * 64
                else:
                    result["receipt"]["intent_hash"] = "f" * 64
            self.effects[operation] = result
            if self.mode == "pending":
                if path.startswith("/v1/"):
                    result.update(status="processing", amount_received=0)
                    return httpx.Response(200, json=result)
                result["status"] = "PENDING"
                return httpx.Response(202, json=result)
            if self.mode == "timeout":
                raise httpx.ReadTimeout("simulated effect with lost acknowledgement", request=request)
            return httpx.Response(200, json=result)


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setenv("TENIR_ENV", "test")
    monkeypatch.setenv("HOST_SCOPE", "single-host")
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())
    backend = SQLiteConsensusBackend(str(tmp_path / "native"))
    backend.register_resource("budget", 1000)
    attestor = Ed25519ConnectorAttestor(Ed25519PrivateKey.from_private_bytes(bytes(range(64, 96))))
    authority = NativePositivePermitAuthority(backend, trusted_provider_public_key=attestor.public_key_bytes)
    independent = Ed25519PrivateKey.from_private_bytes(bytes(range(96, 128)))
    independent_public = independent.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    governor_key = Ed25519PrivateKey.from_private_bytes(bytes(range(128, 160)))
    registry = DurableOperationRegistry(str(tmp_path / "governor.json"))
    state = MembraneState()
    state.register_resource(Resource(resource_id="budget", capacity=1000))
    state.register_agent(Agent(agent_id="alice", authority_score=0.9))
    governor = DualKernelGovernor(MeasuredLabScoring(), TransitionMembrane(state), registry=registry)
    issuer = GovernorReceiptIssuer(governor, governor_key, independent_public, receipt_authority=authority,
                                   policy_context={"pressure": 0.2, "velocity": 0.2, "capacity": 0.9,
                                                   "authority": 0.9, "context_fit": 1.0, "evidence_quality": 1.0})
    gate = object.__new__(DualApprovedUidService)
    gate.authority = authority
    gate._initialize_approval_trust(independent_public)
    gate._initialize_governor_trust(governor_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
                                   registry, policy_context_hash=issuer.policy_context_hash)
    world = MockWorld()
    connectors = {"generic_http_call": MockEffectConnector(httpx.MockTransport(world.handle), destinations={"merchant-demo"}),
                  "stripe_charge": MockStripeConnector(httpx.MockTransport(world.handle), destinations={"pm_mock", "pi_existing"})}
    service = EffectContractLab(issuer, gate, connectors, attestor, agent_id="alice", resource_id="budget")
    with TestClient(create_effect_app(service)) as client:
        yield service, client, independent, world, backend
    for connector in connectors.values():
        connector.close()


def approved(system, action, *, destination=None):
    service, _, independent, _, _ = system
    destination = destination or ("pm_mock" if action == "stripe_charge" else "merchant-demo")
    request = service.prepare(action, {"amount": 120.00, "currency": "EUR", "destination": destination})
    request["approval"]["signature"] = independent.sign(canonical_approval(request["approval"])).hex()
    return service.approve(request)


def status(client, wire):
    return client.get("/api/v1/effects/status/" + wire["operation_id"], headers={
        "X-Tenir-Grant-Id": wire["authority_context"]["grant_id"],
        "X-Tenir-Grant-Signature": wire["authority_context"]["signature"]})


@pytest.mark.parametrize("action", ["generic_http_call", "stripe_charge"])
@pytest.mark.parametrize("mode,expected", [("success", "CONFIRMED"), ("timeout", "UNKNOWN"),
                                           ("reject", "FAILED"), ("bare400", "UNKNOWN"), ("mismatch", "UNKNOWN"),
                                           ("pending", "PENDING")])
def test_http_contract_and_no_retry(system, action, mode, expected):
    service, client, _, world, backend = system
    world.mode = mode
    wire = approved(system, action)
    response = client.post("/api/v1/effects/execute", json=wire)
    assert response.status_code == 202 and response.json()["status"] == "PENDING"
    assert response.json()["operation_id"] == wire["operation_id"]
    assert not world.posts  # HTTP request only commits an outbox command.
    result = service.run_once(wire["operation_id"])
    assert result["status"] == expected
    assert len(world.posts) == 1
    assert status(client, wire).json() == result
    for _ in range(3):
        replay = client.post("/api/v1/effects/execute", json=wire)
        assert replay.status_code == 202 and replay.json() == result
        assert service.run_once(wire["operation_id"]) == result
    assert len(world.posts) == 1
    if expected == "CONFIRMED":
        assert result["receipt"]["operation_id"] == wire["operation_id"]
        assert service.authority._verify_provider_receipt(result["receipt"])
        assert backend.get_state("budget").committed == 120
    else:
        assert backend.get_state("budget").reserved == 120
        assert backend.get_state("budget").committed == 0
    if expected == "FAILED":
        assert result["error"]["code"] == "FAILED_PROVEN"
    if mode == "timeout":
        assert not world.gets  # no automatic retry or reconciliation on timeout
        assert service.reconcile_once(wire["operation_id"])["status"] == "CONFIRMED"
        assert len(world.posts) == 1


@pytest.mark.parametrize("action", ["generic_http_call", "stripe_charge"])
def test_timeout_without_result_stays_unknown_after_read_only_reconciliation(system, action):
    service, client, _, world, _ = system
    world.mode = "timeout_no_effect"
    wire = approved(system, action)
    assert client.post("/api/v1/effects/execute", json=wire).status_code == 202
    assert service.run_once(wire["operation_id"])["status"] == "UNKNOWN"
    assert service.reconcile_once(wire["operation_id"])["status"] == "UNKNOWN"
    assert service.reconcile_once(wire["operation_id"])["status"] == "UNKNOWN"
    assert len(world.posts) == 1


@pytest.mark.parametrize("change", ["missing", "signature", "grant", "amount", "operation", "idempotency"])
def test_direct_worker_without_exact_valid_grant_is_refused(system, change):
    service, client, _, world, backend = system
    wire = approved(system, "stripe_charge")
    if change == "missing":
        del wire["authority_context"]
    elif change == "signature":
        wire["authority_context"]["signature"] = "AA=="
    elif change == "grant":
        wire["authority_context"]["grant_id"] = "unknown"
    elif change == "amount":
        wire["payload"]["amount"] = 119
    elif change == "operation":
        wire["operation_id"] = "00000000-0000-4000-8000-000000000001"
    else:
        wire["idempotency_key"] = "0" * 64
    assert client.post("/api/v1/effects/execute", json=wire).status_code in (403, 409)
    assert not world.posts
    assert backend.get_state("budget").reserved == 0
    with pytest.raises(GrantRefused):
        service.run_once(wire["operation_id"])


def test_independent_approval_and_actual_pass_are_mandatory(system):
    service, client, independent, world, backend = system
    request = service.prepare("stripe_charge", {"amount": 120, "currency": "EUR", "destination": "pm_mock"})
    with pytest.raises(RuntimeError, match="approval malformed"):
        service.approve(request)
    request["approval"]["signature"] = "00" * 64
    with pytest.raises(RuntimeError, match="signature refused"):
        service.approve(request)
    request["approval"].pop("signature")
    request["approval"]["signature"] = independent.sign(canonical_approval(request["approval"])).hex()
    # The unmodified default adapter reports FLAG for unmeasured dimensions.
    service.issuer.governor.scoring = ScoringKernelAdapter(TenirKernel())
    # The exact attribute is asserted to avoid a decorative, ineffective patch.
    assert service.issuer.governor.scoring is not None
    with pytest.raises(RuntimeError, match="allowed decision"):
        service.approve(request)
    assert not world.posts and backend.get_state("budget").reserved == 0


def test_shared_quarantine_after_enqueue_blocks_send(system):
    service, client, _, world, backend = system
    wire = approved(system, "generic_http_call")
    assert client.post("/api/v1/effects/execute", json=wire).status_code == 202
    service.gate.governor_registry.set_quarantine(True, locked_by="lab", incident_id="stop")
    with pytest.raises(RuntimeError, match="quarantine active"):
        service.run_once(wire["operation_id"])
    assert not world.posts and backend.get_state("budget").reserved == 120


def test_concurrent_dispatch_elects_one_send(system):
    service, client, _, world, backend = system
    wire = approved(system, "stripe_charge")
    assert client.post("/api/v1/effects/execute", json=wire).status_code == 202
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: service.run_once(wire["operation_id"]), range(4)))
    assert all(result["status"] in ("UNKNOWN", "CONFIRMED") for result in results)
    assert len(world.posts) == 1
    assert backend.get_state("budget").committed == 120


def test_existing_payment_intent_is_prevalidated_then_confirmed_once(system):
    service, client, _, world, _ = system
    wire = approved(system, "stripe_charge", destination="pi_existing")
    world.effects[wire["operation_id"]] = {
        "id": "pi_existing", "object": "payment_intent", "livemode": False, "amount": 12000,
        "amount_received": 0, "currency": "eur", "status": "requires_confirmation",
        "metadata": {"tenir_operation_id": wire["operation_id"], "tenir_intent_hash": wire["idempotency_key"]}}
    assert client.post("/api/v1/effects/execute", json=wire).status_code == 202
    assert service.run_once(wire["operation_id"])["status"] == "CONFIRMED"
    assert [item[0] for item in world.posts] == ["/v1/payment_intents/pi_existing/confirm"]


def test_no_live_transport_or_unconfigured_destination(system):
    with httpx.HTTPTransport() as transport:
        with pytest.raises(RuntimeError, match="only local"):
            MockStripeConnector(transport, destinations={"pm_mock"})
    service = system[0]
    with pytest.raises(ValueError, match="not configured"):
        service.prepare("generic_http_call", {"amount": 120, "currency": "EUR", "destination": "https://untrusted.invalid"})


def test_status_requires_grant_and_json_duplicates_are_refused(system):
    _, client, _, world, _ = system
    wire = approved(system, "generic_http_call")
    assert client.post("/api/v1/effects/execute", json=wire).status_code == 202
    assert client.get("/api/v1/effects/status/" + wire["operation_id"]).status_code == 403
    assert client.post("/api/v1/effects/execute", content='{"payload":{},"payload":{}}',
                       headers={"Content-Type": "application/json"}).status_code == 400
    assert not world.posts


@pytest.mark.parametrize("checkpoint", ["before_commit", "before_io", "after_effect"])
def test_process_crash_never_reissues_committed_send(system, tmp_path, checkpoint):
    service, client, _, world, backend = system
    wire = approved(system, "stripe_charge")
    assert client.post("/api/v1/effects/execute", json=wire).status_code == 202
    marker = tmp_path / "synthetic-effects.log"
    worker = multiprocessing.get_context("spawn").Process(target=_contract_process,
        args=(str(tmp_path), wire["operation_id"], checkpoint, str(marker)))
    worker.start()
    try:
        worker.join(30)
        assert worker.exitcode == 87
    finally:
        if worker.is_alive():
            worker.terminate()
        worker.join(10)
    result = service.run_once(wire["operation_id"])
    if checkpoint == "before_commit":
        assert result["status"] == "CONFIRMED" and len(world.posts) == 1
        assert not marker.exists()
        assert backend.get_state("budget").committed == 120
    else:
        assert result["status"] == "UNKNOWN" and not world.posts
        assert marker.exists() is (checkpoint == "after_effect")
        assert backend.get_state("budget").reserved == 120
        if marker.exists():
            assert marker.read_text().splitlines() == [wire["operation_id"]]


def test_separate_processes_elect_one_effect(system, tmp_path):
    service, client, _, world, backend = system
    wire = approved(system, "stripe_charge")
    assert client.post("/api/v1/effects/execute", json=wire).status_code == 202
    marker = tmp_path / "process-effects.log"
    ctx = multiprocessing.get_context("spawn")
    results = ctx.Queue()
    workers = [ctx.Process(target=_contract_process, args=(str(tmp_path), wire["operation_id"], "none", str(marker), results)) for _ in range(2)]
    for worker in workers:
        worker.start()
    try:
        for worker in workers:
            worker.join(30)
            assert worker.exitcode == 0
        assert {results.get(timeout=5) for _ in workers} <= {"UNKNOWN", "CONFIRMED"}
        assert marker.read_text().splitlines() == [wire["operation_id"]]
        assert service.status(wire["operation_id"])["status"] == "CONFIRMED"
        assert backend.get_state("budget").committed == 120
        assert not world.posts
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            worker.join(10)
        results.close()


def test_conflicting_late_confirmation_does_not_override_proven_failure(system):
    service, client, _, world, backend = system
    world.mode = "reject"
    wire = approved(system, "generic_http_call")
    assert client.post("/api/v1/effects/execute", json=wire).status_code == 202
    assert service.run_once(wire["operation_id"])["status"] == "FAILED"
    row, _, envelope = service._queued(wire["operation_id"])
    result = service._record(wire["operation_id"], json.loads(row["claim_json"]), envelope["request"]["command"],
                            {"status": "CONFIRMED", "reference": "conflicting-effect", "evidence": {"conflict": True}})
    assert result["status"] == "UNKNOWN"
    assert service.authority.requires_manual_review(wire["operation_id"])
    assert backend.get_state("budget").committed == 0
    assert backend.get_state("budget").reserved == 120
    with backend._connection() as db:
        evidence = db.execute("SELECT observation_json FROM b1_effect_observations ORDER BY sequence").fetchall()
        assert [json.loads(row[0])["status"] for row in evidence] == ["FAILED", "CONFIRMED"]
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM b1_effect_observations")


@pytest.mark.parametrize("environment,scope", [("production", "single-host"), ("test", "multi-host")])
def test_contract_refuses_productive_or_multihost_profile(system, monkeypatch, environment, scope):
    service = system[0]
    monkeypatch.setenv("TENIR_ENV", environment)
    monkeypatch.setenv("HOST_SCOPE", scope)
    with pytest.raises(RuntimeError, match="single-host test/lab only"):
        EffectContractLab(service.issuer, service.gate, service.connectors, service.attestor,
                          agent_id="alice", resource_id="budget")


def test_unknown_cached_replay_survives_approval_expiry(system, monkeypatch):
    service, client, _, world, _ = system
    world.mode = "timeout"
    wire = approved(system, "stripe_charge")
    assert client.post("/api/v1/effects/execute", json=wire).status_code == 202
    first = service.run_once(wire["operation_id"])
    _, _, envelope = service._queued(wire["operation_id"])
    monkeypatch.setattr("b1_dual_authority.time.time", lambda: envelope["request"]["approval"]["expires_at"] + 10)
    response = client.post("/api/v1/effects/execute", json=wire)
    assert response.status_code == 202 and response.json() == first
    assert len(world.posts) == 1 and not world.gets


def test_native_budget_failure_rolls_back_enqueue_and_approval_nonce(system):
    service, client, _, world, backend = system
    wire = approved(system, "stripe_charge")
    assert backend.reserve("another-operation", 950, "budget").status == "RESERVED"
    assert client.post("/api/v1/effects/execute", json=wire).status_code == 403
    with backend._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM b1_effect_commands").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_permits").fetchone()[0] == 0
    assert backend.get_state("budget").reserved == 950 and not world.posts


def test_equivalent_numeric_amount_reuses_the_same_governor_operation(system):
    service, client, _, world, _ = system
    first = service.prepare("stripe_charge", {"amount": 120, "currency": "EUR", "destination": "pm_mock"})
    second = service.prepare("stripe_charge", {"amount": 120.00, "currency": "EUR", "destination": "pm_mock"})
    assert first == second
    wire = approved(system, "stripe_charge")
    wire["payload"]["amount"] = 120
    assert client.post("/api/v1/effects/execute", json=wire).status_code == 202
    assert service.run_once(wire["operation_id"])["status"] == "CONFIRMED"
    assert len(world.posts) == 1
