"""B1.1-B native authorization is durable, exact and fail-closed.

The fixture is the existing mock-only Governor + independent-approval lab.
It deliberately invokes the native APIs directly after that B1 profile has
been configured: a valid-looking command alone must not reserve I8 or create
an outbox claim.
"""
from copy import deepcopy
import os

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from b1_approved_positive_uid_service import canonical_approval
from b1_dual_authority import DualApprovedUidService, GovernorReceiptIssuer
from b1_effect_connectors import MockEffectConnector, MockStripeConnector
from b1_effects_lab import EffectContractLab
from kernel_claim_provider import KernelClaimProofProvider
from native_dispatch_adapter import Ed25519ConnectorAttestor, NativeClaimedDispatchAdapter
from native_positive_g4a import NativePositiveG4A
from native_positive_permit import NativePositivePermitAuthority
from tenir_governance.composition.governor import DualKernelGovernor
from tenir_governance.membrane import Agent, MembraneState, Resource, TransitionMembrane
from tenir_governance.operations.provider import MockProvider
from tenir_governance.operations.registry import DurableOperationRegistry
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from tenir_kernel.kernel import TenirKernel


_MUTANT = os.environ.get("B11B_TEST_MUTANT") == "1"
_B1_PROFILE_TABLES = (
    "b1_approval_trust",
    "b1_governor_trust",
    "b1_governor_issuer_trust",
    "b1_governor_receipts",
    "b1_approved_requests",
    "b1_native_quarantine_registry",
)


class _MeasuredLabScoring:
    """Small deterministic scoring fixture; not a production policy adapter."""

    def adjudicate(self, **values):
        coords = {
            **{name: values[name] for name in (
                "pressure", "velocity", "capacity", "authority", "context_fit", "evidence_quality",
            )},
            "option_space": 1.0,
            "cooperative_control_score": 1.0,
            "trajectory_risk_score": 0.0,
            "irreversible_pathway_materiality": 0.0,
            "manipulation_risk": 0.0,
            "drift": 0.0,
        }
        result = TenirKernel().adjudicate(
            pressure=values["pressure"], velocity=values["velocity"], capacity=values["capacity"],
            coordinates=coords, mode="ENFORCE",
        )
        return {
            **result,
            "kernel": "scoring",
            "authority": values["authority"],
            "context_fit": values["context_fit"],
            "evidence_quality": values["evidence_quality"],
        }


class _NoEffectWorld:
    """Independent synthetic-provider observer for zero-effect assertions."""

    def __init__(self):
        self.posts = []

    def handle(self, request):
        self.posts.append((request.method, request.url.path, request.content))
        raise AssertionError("B1.1-B boundary test unexpectedly called a provider")


def _native_state(backend):
    tables = (
        "resources",
        "b1_approved_requests",
        "w2_positive_v1_events",
        "w2_positive_v1_capacity",
        "w2_positive_v1_permits",
        "w2_positive_v1_outbox",
        "reservations",
        "b1_native_dual_grants",
        "b1_native_dual_permit_links",
    )
    with backend._connection() as db:
        present = {
            row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        return {
            table: [tuple(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY rowid")]
            for table in tables if table in present
        }


def _envelope(service, wire):
    _, envelope = service._lookup(wire)
    return envelope


def _observe_provider_calls(provider, monkeypatch):
    """Observe attempted synthetic execution independently of any ledger."""
    observed = []
    original = provider.execute

    def observe(request):
        observed.append((request.method, request.url.path))
        return httpx.Response(200, json={"synthetic": True})

    def execute(*args, **kwargs):
        with httpx.Client(transport=httpx.MockTransport(observe), trust_env=False) as client:
            client.post("https://provider.invalid/b11-native-effect")
        return original(*args, **kwargs)

    monkeypatch.setattr(provider, "execute", execute)
    return observed


@pytest.fixture
def native_system(tmp_path, monkeypatch):
    """B1 proof authority without an HTTP TestClient or real transport."""
    monkeypatch.setenv("TENIR_ENV", "test")
    monkeypatch.setenv("HOST_SCOPE", "single-host")
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())
    backend = SQLiteConsensusBackend(str(tmp_path / "native"))
    assert backend.register_resource("budget", 1000) == "REGISTERED"
    attestor = Ed25519ConnectorAttestor(Ed25519PrivateKey.from_private_bytes(bytes(range(64, 96))))
    authority = NativePositivePermitAuthority(backend, trusted_provider_public_key=attestor.public_key_bytes)
    independent = Ed25519PrivateKey.from_private_bytes(bytes(range(96, 128)))
    independent_public = independent.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    governor_key = Ed25519PrivateKey.from_private_bytes(bytes(range(128, 160)))
    registry = DurableOperationRegistry(str(tmp_path / "governor.json"))
    state = MembraneState()
    state.register_resource(Resource(resource_id="budget", capacity=1000))
    state.register_agent(Agent(agent_id="alice", authority_score=0.9))
    governor = DualKernelGovernor(_MeasuredLabScoring(), TransitionMembrane(state), registry=registry)
    context = {
        "pressure": 0.2, "velocity": 0.2, "capacity": 0.9,
        "authority": 0.9, "context_fit": 1.0, "evidence_quality": 1.0,
    }
    issuer = GovernorReceiptIssuer(
        governor, governor_key, independent_public, receipt_authority=authority,
        policy_context=context,
    )
    gate = object.__new__(DualApprovedUidService)
    gate.authority = authority
    gate._initialize_approval_trust(independent_public)
    gate._initialize_governor_trust(
        governor_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
        registry, policy_context_hash=issuer.policy_context_hash,
    )
    world = _NoEffectWorld()
    connectors = {
        "generic_http_call": MockEffectConnector(
            httpx.MockTransport(world.handle), destinations={"merchant-demo"},
        ),
        "stripe_charge": MockStripeConnector(
            httpx.MockTransport(world.handle), destinations={"pm_mock", "pi_existing"},
        ),
    }
    service = EffectContractLab(issuer, gate, connectors, attestor, agent_id="alice", resource_id="budget")
    try:
        yield service, independent, world, backend
    finally:
        for connector in connectors.values():
            connector.close()


def _approved(service, independent, action):
    destination = "pm_mock" if action == "stripe_charge" else "merchant-demo"
    request = service.prepare(action, {"amount": 120.00, "currency": "EUR", "destination": destination})
    request["approval"]["signature"] = independent.sign(canonical_approval(request["approval"])).hex()
    return service.approve(request)


def _copy_b1_profile(source, target):
    """Install a controlled pre-B1.1-B store image for migration testing.

    The helper copies only already-pinned trust material and the signed receipt,
    and registry topology pin after a legacy native permit has been created in
    a separate temporary store. Missing-pin refusal has its own boundary test;
    this fixture isolates refusal of the absent dual-permit link.
    It deliberately does not copy any dual-grant link.
    """
    with source._connection() as db:
        schemas = {}
        rows = {}
        for table in _B1_PROFILE_TABLES:
            schema = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            assert schema and schema["sql"]
            schemas[table] = schema["sql"]
            rows[table] = [dict(row) for row in db.execute("SELECT * FROM " + table)]
    with target._write("test_install_legacy_b1_profile") as db:
        for table in _B1_PROFILE_TABLES:
            db.execute(schemas[table])
            for row in rows[table]:
                columns = tuple(row)
                placeholders = ",".join("?" for _ in columns)
                db.execute(
                    "INSERT INTO " + table + " (" + ",".join(columns) + ") VALUES (" + placeholders + ")",
                    tuple(row[column] for column in columns),
                )


def _legacy_unlinked_authority(native_system, tmp_path):
    """Create a pre-B1.1-B permit, then install a B1 profile around it."""
    service, independent, world, source_backend = native_system
    wire = _approved(service, independent, "generic_http_call")
    envelope = _envelope(service, wire)
    request = envelope["request"]
    provider_key = Ed25519PrivateKey.from_private_bytes(bytes(range(64, 96)))
    legacy_backend = SQLiteConsensusBackend(str(tmp_path / "legacy-native"))
    assert legacy_backend.register_resource("budget", 1000) == "REGISTERED"
    legacy = NativePositivePermitAuthority(
        legacy_backend,
        trusted_provider_public_key=provider_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
    )
    legacy.admit_reserve_issue(
        request["operation_key"], request["evaluation_id"], request["command"],
        resource_id=request["command"]["resource_id"],
    )
    _copy_b1_profile(source_backend, legacy)
    return legacy, legacy_backend, request, envelope, world


def test_b1_profile_refuses_native_admission_without_durable_dual_grant(native_system, monkeypatch):
    service, independent, world, backend = native_system
    wire = _approved(service, independent, "generic_http_call")
    envelope = _envelope(service, wire)
    request = envelope["request"]
    before = _native_state(backend)

    if _MUTANT:
        monkeypatch.setattr(
            NativePositivePermitAuthority,
            "_require_b1_dual_admission",
            lambda *args, **kwargs: None,
        )
    refusal = None
    try:
        service.authority.admit_reserve_issue(
            request["operation_key"], request["evaluation_id"], request["command"],
            resource_id=request["command"]["resource_id"],
        )
    except RuntimeError as exc:
        refusal = exc

    assert world.posts == []  # independent mock provider observer
    assert _native_state(backend) == before, "native I8/permit/outbox changed after denied admission"
    assert refusal is not None, "native admission accepted without a durable B1 dual grant"
    assert "durable B1 dual grant required" in str(refusal)


def test_b1_profile_binds_exact_dual_proof_before_native_admission_and_claim(native_system):
    service, independent, world, backend = native_system
    wire = _approved(service, independent, "stripe_charge")
    envelope = _envelope(service, wire)
    request = envelope["request"]

    dual = service.authority.bind_b1_dual_envelope(envelope)
    assert service.authority.bind_b1_dual_envelope(deepcopy(envelope)) == dual

    first = service.authority.admit_reserve_issue(
        request["operation_key"], request["evaluation_id"], request["command"],
        resource_id=request["command"]["resource_id"], dual_grant_id=dual["dual_grant_id"],
    )
    replay = service.authority.admit_reserve_issue(
        request["operation_key"], request["evaluation_id"], request["command"],
        resource_id=request["command"]["resource_id"], dual_grant_id=dual["dual_grant_id"],
    )
    assert replay["permit_id"] == first["permit_id"]
    claim = service.authority.consume_and_claim(
        request["operation_key"], request["command"], request["idempotency_key"],
    )
    exact_replay = service.authority.consume_and_claim(
        request["operation_key"], request["command"], request["idempotency_key"],
    )
    assert claim["execution_id"] == exact_replay["execution_id"]
    assert claim["claim_token"] == exact_replay["claim_token"]
    assert world.posts == []
    with backend._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_outbox").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM b1_native_dual_permit_links").fetchone()[0] == 1


@pytest.mark.parametrize("change", ["approval-signature", "receipt-signature", "command"])
def test_b1_profile_refuses_substituted_dual_proof_before_native_effect(native_system, change):
    service, independent, world, backend = native_system
    wire = _approved(service, independent, "generic_http_call")
    envelope = _envelope(service, wire)
    forged = deepcopy(envelope)
    if change == "approval-signature":
        forged["request"]["approval"]["signature"] = "00" * 64
    elif change == "receipt-signature":
        forged["governor_receipt"]["signature"] = "00" * 64
    else:
        forged["request"]["command"]["amount"] = 119
    before = _native_state(backend)

    with pytest.raises(RuntimeError):
        service.authority.bind_b1_dual_envelope(forged)

    assert world.posts == []
    assert _native_state(backend) == before


def test_b1_profile_native_g4a_entrypoint_cannot_bypass_dual_grant(native_system):
    service, independent, world, backend = native_system
    wire = _approved(service, independent, "generic_http_call")
    request = _envelope(service, wire)["request"]
    before = _native_state(backend)

    with pytest.raises(RuntimeError, match="durable B1 dual grant required"):
        NativePositiveG4A(service.authority).evaluate_and_grant(
            request["operation_key"], request["evaluation_id"], request["command"],
            resource_id=request["command"]["resource_id"],
        )

    assert world.posts == []
    assert _native_state(backend) == before


def test_b1_profile_direct_kernel_provider_and_adapter_refuse_before_effect(native_system, tmp_path, monkeypatch):
    """Both direct dispatch facades must reach the durable B1 check first."""
    service, independent, world, backend = native_system
    wire = _approved(service, independent, "generic_http_call")
    request = _envelope(service, wire)["request"]
    before = _native_state(backend)
    # The fixture pins this exact connector public key in the native authority.
    monkeypatch.setenv("TENIR_B3_PRIVATE_KEY_HEX", bytes(range(64, 96)).hex())
    provider = KernelClaimProofProvider(
        db_path=str(tmp_path / "kernel-provider.sqlite3"),
        claim_verifier=service.authority.verify_dispatch_claim,
    )
    transport = MockProvider()
    attestor = Ed25519ConnectorAttestor(
        Ed25519PrivateKey.from_private_bytes(bytes(range(64, 96)))
    )
    adapter = NativeClaimedDispatchAdapter(service.authority, transport, attestor)
    provider_calls = _observe_provider_calls(provider, monkeypatch)
    adapter_calls = _observe_provider_calls(transport, monkeypatch)
    try:
        with pytest.raises(RuntimeError, match="durable B1 dual grant missing or mismatched"):
            provider.dispatch_authorized(
                service.authority, request["operation_key"], request["idempotency_key"], request["command"],
            )
        with pytest.raises(RuntimeError, match="durable B1 dual grant missing or mismatched"):
            adapter.dispatch(
                request["operation_key"], request["idempotency_key"], request["command"],
            )
        provider_effects = provider.execute_count(request["operation_key"])
    finally:
        provider.conn.close()

    assert provider_calls == [] and adapter_calls == [], "provider transport was called"
    assert provider_effects == 0
    assert transport.calls == []
    assert world.posts == []
    assert _native_state(backend) == before


def test_b1_profile_g4a_rechecks_dual_binding_in_send_transaction(native_system, tmp_path, monkeypatch):
    """The send-election transaction must not rely only on an earlier claim."""
    service, independent, world, _ = native_system
    wire = _approved(service, independent, "generic_http_call")
    envelope = _envelope(service, wire)
    request = envelope["request"]
    dual = service.authority.bind_b1_dual_envelope(envelope)
    gate = NativePositiveG4A(service.authority)
    grant = gate.evaluate_and_grant(
        request["operation_key"], request["evaluation_id"], request["command"],
        resource_id=request["command"]["resource_id"], dual_grant_id=dual["dual_grant_id"],
    )
    monkeypatch.setenv("TENIR_B3_PRIVATE_KEY_HEX", bytes(range(64, 96)).hex())
    provider = KernelClaimProofProvider(
        db_path=str(tmp_path / "g4a-provider.sqlite3"),
        claim_verifier=service.authority.verify_dispatch_claim,
    )
    provider_calls = _observe_provider_calls(provider, monkeypatch)
    original = service.authority._require_b1_dual_binding
    calls = 0

    def recheck(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("test B1 send-transaction recheck")
        return original(*args, **kwargs)

    monkeypatch.setattr(service.authority, "_require_b1_dual_binding", recheck)
    try:
        with pytest.raises(RuntimeError, match="test B1 send-transaction recheck"):
            gate.dispatch_to_b3(
                request["operation_key"], request["idempotency_key"], request["command"], grant, provider,
            )
        provider_effects = provider.execute_count(request["operation_key"])
    finally:
        provider.conn.close()

    assert calls == 2
    assert provider_calls == [], "provider transport was called"
    assert provider_effects == 0
    assert world.posts == []
    with service.authority._tx() as db:
        assert db.execute("SELECT consumed FROM w2_positive_g4a_v1_grants").fetchone()[0] == 0
        assert db.execute("SELECT status FROM w2_positive_v1_outbox").fetchone()[0] == "PENDING"
        assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_events WHERE event='dispatch_send_granted'").fetchone()[0] == 0


def test_b1_profile_legacy_native_dispatch_facades_refuse_unlinked_permit(native_system, tmp_path, monkeypatch):
    """A formerly valid native permit is not a substitute for dual authority."""
    legacy, backend, request, _, world = _legacy_unlinked_authority(native_system, tmp_path)
    before = _native_state(backend)
    monkeypatch.setenv("TENIR_B3_PRIVATE_KEY_HEX", bytes(range(64, 96)).hex())
    provider = KernelClaimProofProvider(
        db_path=str(tmp_path / "legacy-kernel-provider.sqlite3"),
        claim_verifier=legacy.verify_dispatch_claim,
    )
    transport = MockProvider()
    adapter = NativeClaimedDispatchAdapter(
        legacy, transport,
        Ed25519ConnectorAttestor(Ed25519PrivateKey.from_private_bytes(bytes(range(64, 96)))),
    )
    provider_calls = _observe_provider_calls(provider, monkeypatch)
    adapter_calls = _observe_provider_calls(transport, monkeypatch)
    try:
        with pytest.raises(RuntimeError, match="durable B1 dual grant missing or mismatched"):
            provider.dispatch_authorized(
                legacy, request["operation_key"], request["idempotency_key"], request["command"],
            )
        with pytest.raises(RuntimeError, match="durable B1 dual grant missing or mismatched"):
            adapter.dispatch(
                request["operation_key"], request["idempotency_key"], request["command"],
            )
        provider_effects = provider.execute_count(request["operation_key"])
    finally:
        provider.conn.close()

    assert provider_calls == [] and adapter_calls == [], "provider transport was called"
    assert provider_effects == 0
    assert transport.calls == []
    assert world.posts == []
    assert _native_state(backend) == before


def test_b1_profile_refuses_retroactive_adoption_of_unlinked_legacy_permit(native_system, tmp_path):
    """A proof added after a legacy permit must not make it dispatchable."""
    legacy, legacy_backend, request, envelope, world = _legacy_unlinked_authority(native_system, tmp_path)
    dual = legacy.bind_b1_dual_envelope(envelope)
    before = _native_state(legacy_backend)

    with pytest.raises(RuntimeError, match="unlinked B1 permit requires review"):
        legacy.admit_reserve_issue(
            request["operation_key"], request["evaluation_id"], request["command"],
            resource_id=request["command"]["resource_id"], dual_grant_id=dual["dual_grant_id"],
        )

    assert world.posts == []
    assert _native_state(legacy_backend) == before


def test_orphan_approval_binding_requires_review(native_system):
    service, independent, world, backend = native_system
    envelope = _envelope(service, _approved(service, independent, "generic_http_call"))
    request = envelope["request"]
    dual = service.authority.bind_b1_dual_envelope(envelope)
    with service.authority._write("test_seed_orphan") as db:
        db.execute("INSERT INTO b1_approved_requests VALUES(?,?,?,?,?,?,?)", (
            dual["approval_id"], dual["approval_nonce"], dual["operation_key"], dual["idempotency_key"],
            dual["approval_hash"], dual["approval_body_json"], dual["approval_signature"],
        ))
    before = _native_state(backend)
    refusal = None
    try:
        service.authority.admit_reserve_issue(
            request["operation_key"], request["evaluation_id"], request["command"],
            resource_id="budget", dual_grant_id=dual["dual_grant_id"],
        )
    except RuntimeError as exc:
        refusal = exc
    assert world.posts == []
    assert _native_state(backend) == before, "orphan approval was adopted into native I8"
    assert refusal is not None and "orphaned approval" in str(refusal)


def test_changed_allowed_decisions_fences_previously_bound_proof(native_system):
    service, independent, world, backend = native_system
    envelope = _envelope(service, _approved(service, independent, "generic_http_call"))
    request = envelope["request"]
    dual = service.authority.bind_b1_dual_envelope(envelope)
    service.authority.admit_reserve_issue(
        request["operation_key"], request["evaluation_id"], request["command"],
        resource_id="budget", dual_grant_id=dual["dual_grant_id"],
    )
    with service.authority._write("test_change_allowed_decisions") as db:
        db.execute("UPDATE b1_governor_trust SET decisions='[\"FLAG\"]' WHERE singleton=1")
    before = _native_state(backend)
    refusal = None
    try:
        service.authority.consume_and_claim(request["operation_key"], request["command"], request["idempotency_key"])
    except RuntimeError as exc:
        refusal = exc
    assert world.posts == []
    assert _native_state(backend) == before, "claim used a now-disallowed Governor decision"
    assert refusal is not None


@pytest.mark.parametrize("boundary", ["b1_native_dual_bind", "positive_admit", "positive_claim"])
def test_native_commit_failure_rolls_back_boundary_state(native_system, monkeypatch, boundary):
    service, independent, world, backend = native_system
    envelope = _envelope(service, _approved(service, independent, "generic_http_call"))
    request = envelope["request"]
    authority = service.authority
    if boundary != "b1_native_dual_bind":
        dual = authority.bind_b1_dual_envelope(envelope)
    if boundary == "positive_claim":
        authority.admit_reserve_issue(
            request["operation_key"], request["evaluation_id"], request["command"],
            resource_id="budget", dual_grant_id=dual["dual_grant_id"],
        )
    before = _native_state(backend)

    def fail_before_commit(name):
        if name == boundary:
            raise RuntimeError("injected native commit failure")

    monkeypatch.setattr(backend, "_before_commit", fail_before_commit)
    with pytest.raises(RuntimeError, match="injected native commit failure"):
        if boundary == "b1_native_dual_bind":
            authority.bind_b1_dual_envelope(envelope)
        elif boundary == "positive_admit":
            authority.admit_reserve_issue(
                request["operation_key"], request["evaluation_id"], request["command"],
                resource_id="budget", dual_grant_id=dual["dual_grant_id"],
            )
        else:
            authority.consume_and_claim(request["operation_key"], request["command"], request["idempotency_key"])
    assert world.posts == []
    assert _native_state(backend) == before
    # The separate Witness may retain an unused checkpoint; native rollback
    # must not turn it into an outbox claim or consume the approval's nonce.


@pytest.mark.parametrize("boundary", ["admit", "claim", "send"])
def test_native_profile_rechecks_expiry_at_each_boundary(native_system, monkeypatch, boundary):
    service, independent, world, backend = native_system
    envelope = _envelope(service, _approved(service, independent, "generic_http_call"))
    request = envelope["request"]
    authority = service.authority
    dual = authority.bind_b1_dual_envelope(envelope)
    if boundary != "admit":
        authority.admit_reserve_issue(
            request["operation_key"], request["evaluation_id"], request["command"],
            resource_id="budget", dual_grant_id=dual["dual_grant_id"],
        )
    if boundary == "send":
        claim = authority.consume_and_claim(request["operation_key"], request["command"], request["idempotency_key"])
    before = _native_state(backend)
    monkeypatch.setattr("native_positive_permit.time.time", lambda: dual["expires_at"])
    with pytest.raises(RuntimeError):
        if boundary == "admit":
            authority.admit_reserve_issue(
                request["operation_key"], request["evaluation_id"], request["command"],
                resource_id="budget", dual_grant_id=dual["dual_grant_id"],
            )
        elif boundary == "claim":
            authority.consume_and_claim(request["operation_key"], request["command"], request["idempotency_key"])
        else:
            authority.grant_send_right(claim)
    assert world.posts == []
    assert _native_state(backend) == before
