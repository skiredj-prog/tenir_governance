"""Governor integration tests for the optional Profile S commit gate."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import multiprocessing as mp
import threading

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tenir_governance.composition.governor import DualKernelGovernor
from tenir_governance.experimental.ep01_evidence import SignedClaim, canonical_claim_bytes
from tenir_governance.experimental.ep01_policy import Claim, Verdict
from tenir_governance.experimental.ep02_binding import TrustState
from tenir_governance.experimental.ep02_commit_authorization import (
    CommitAuthorizationRequest,
    ConsumptionDecision,
    ProfileSCommitAuthority,
    authorization_receipt_from_dict,
    authorization_receipt_to_dict,
    authorization_request_from_dict,
    authorization_request_to_dict,
    consumption_receipt_from_dict,
    consumption_receipt_to_dict,
    canonical_payload_bytes,
    operation_binding,
)
from tenir_governance.experimental.ep02_durable_cas import DurableCASResult
from tenir_governance.experimental.ep02_signed_trust import (
    SignedTrustState,
    bound_snapshot_bytes,
    verified_bound_preflight,
)
from tenir_governance.membrane import MembraneState, TransitionMembrane
from tenir_governance.objects import Passage, Resource
from tenir_governance.operations.registry import (
    DurableOperationRegistry,
    OperationRecord,
    OperationState,
)
from tenir_governance.operations.provider import MockProvider
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend


NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)
PAYLOAD = {"amount": "10"}
PAYLOAD_DIGEST = sha256(canonical_payload_bytes(PAYLOAD)).hexdigest()


def _verified_quarantine_release(**kwargs):
    """Test verifier that binds the release proof to the current lock epoch."""
    return {
        "authorized": True,
        "principal": kwargs["authorized_by"],
        "scope": kwargs["scope"],
        "lock_id": kwargs["lock_id"],
        "authorization_id": "test-release-proof",
    }


def _raw_public(key):
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _raw_private(key):
    return key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _integrated_dispatch_worker(
    registry_path, authority_path, receipt_private_key, barrier, queue,
):
    """Independent process competing for the integrated Profile S claim."""
    try:
        signing_key = Ed25519PrivateKey.from_private_bytes(receipt_private_key)
        authority = ProfileSCommitAuthority.open(
            authority_path, receipt_signing_key=signing_key,
        )
        registry = DurableOperationRegistry(registry_path)
        governor = DualKernelGovernor(
            object(),
            TransitionMembrane(MembraneState()),
            registry=registry,
            commit_authority=authority,
            require_commit_authorization=True,
        )
        barrier.wait(timeout=20)
        result = governor.dispatch("idem-1", metadata=PAYLOAD)
        queue.put((result.get("dispatched", False), result.get("reason", "")))
    except Exception as exc:
        queue.put(("error", f"{type(exc).__name__}: {exc}"))


def _blocked_integrated_dispatch_worker(
    registry_path, authority_path, receipt_private_key, entered, release, queue,
):
    """Pause authority consumption so another process can activate quarantine."""
    try:
        signing_key = Ed25519PrivateKey.from_private_bytes(receipt_private_key)
        authority = ProfileSCommitAuthority.open(
            authority_path, receipt_signing_key=signing_key,
        )
        original_consume = authority.consume

        def blocked_consume(*args, **kwargs):
            entered.set()
            if not release.wait(timeout=20):
                raise TimeoutError("test did not release blocked authority call")
            return original_consume(*args, **kwargs)

        authority.consume = blocked_consume
        registry = DurableOperationRegistry(registry_path)
        governor = DualKernelGovernor(
            object(), TransitionMembrane(MembraneState()), registry=registry,
            commit_authority=authority, require_commit_authorization=True,
        )
        result = governor.dispatch("idem-1", metadata=PAYLOAD)
        queue.put((result.get("dispatched", False), result.get("reason", "")))
    except Exception as exc:
        queue.put(("error", f"{type(exc).__name__}: {exc}"))


def _snapshot(root, issuer, revision=1):
    state = TrustState(revision, "policy-v1", (("issuer", "contracts"),), ())
    skeleton = SignedTrustState(
        state, b"", "root", (("issuer", _raw_public(issuer)),),
    )
    return SignedTrustState(
        state, root.sign(bound_snapshot_bytes(skeleton)), "root",
        skeleton.issuer_keys,
    )


def _setup(
    tmp_path,
    *,
    authority_class=ProfileSCommitAuthority,
    resource_id="",
    amount=0,
    provider=None,
):
    root = Ed25519PrivateKey.generate()
    issuer = Ed25519PrivateKey.generate()
    receipt_key = Ed25519PrivateKey.generate()
    path = tmp_path / "authority.sqlite3"
    base = ProfileSCommitAuthority.provision(
        path,
        authority_id="authority-a",
        trust_profile_id="institutional-roots",
        trust_generation=1,
        roots={"root": root.public_key()},
        receipt_key_id="receipt-key-a",
        receipt_key_generation=1,
        receipt_signing_key=receipt_key,
    )
    snapshot = _snapshot(root, issuer)
    assert base.anchor.bootstrap("bootstrap-1", snapshot).result == DurableCASResult.APPLIED
    if authority_class is ProfileSCommitAuthority:
        service = base
    else:
        service = authority_class.open(path, receipt_signing_key=receipt_key)
    service._test_root = root
    service._test_issuer = issuer
    proof_claim = Claim("document-B", "issuer", "document-A", NOW)
    proof = SignedClaim(
        proof_claim, issuer.sign(canonical_claim_bytes(proof_claim)),
    )
    verdict, _, finding = verified_bound_preflight(
        proof, prior="document-A", scope="contracts", now=NOW,
        trust=snapshot, root_keys={"root": root.public_key()},
    )
    assert verdict == Verdict.ADMISSIBLE
    binding = operation_binding(
        authority_id="authority-a",
        profile_id="institutional-roots",
        profile_generation=1,
        institutional_scope="institution-a",
        operation_id="operation-1",
        idempotency_key="idem-1",
        passage_id="passage-1",
        agent_id="agent-1",
        action_type="payment",
        resource_id=resource_id,
        amount=amount,
        payload_digest=PAYLOAD_DIGEST,
        finding=finding,
    )
    request = CommitAuthorizationRequest("authorization-1", binding)
    registry = DurableOperationRegistry(
        str(tmp_path / "operations.json"),
        quarantine_release_verifier=_verified_quarantine_release,
    )
    membrane_state = MembraneState()
    backend = None
    if resource_id:
        membrane_state.register_resource(Resource(resource_id=resource_id, capacity=100.0))
        backend = SQLiteConsensusBackend(str(tmp_path / "budget.sqlite"))
        assert backend.register_resource(resource_id, Decimal("100")) in (
            "REGISTERED", "ALREADY_REGISTERED",
        )
    membrane = TransitionMembrane(membrane_state)
    governor = DualKernelGovernor(
        object(), membrane, registry=registry,
        commit_authority=service, require_commit_authorization=True,
        consensus_backend=backend, provider=provider,
    )
    record = OperationRecord(
        idempotency_key="idem-1",
        passage_id="passage-1",
        agent_id="agent-1",
        action_type="payment",
        operation_id="operation-1",
        institutional_scope="institution-a",
        payload_digest=PAYLOAD_DIGEST,
        amount=float(amount),
        resource_id=resource_id,
        commit_gate_required=True,
    )
    assert registry.register(record)
    if backend is not None:
        assert governor._reserve_budget("idem-1", resource_id, float(amount)) is True
    assert registry.transition_to("idem-1", OperationState.RESERVED)[0]
    return governor, registry, service, receipt_key, snapshot, proof, finding, request


def _authorize(governor, snapshot, proof, finding, request):
    return governor.authorize_commit(
        "idem-1", request, finding=finding, claim=proof,
        current_snapshot=snapshot, now=NOW,
    )


def test_gate_is_disabled_by_default_and_preserves_dispatch_path():
    registry = DurableOperationRegistry()
    governor = DualKernelGovernor(
        object(), TransitionMembrane(MembraneState()), registry=registry,
    )
    assert registry.register(OperationRecord(
        idempotency_key="legacy", passage_id="p", agent_id="a",
        action_type="read",
    ))
    assert registry.transition_to("legacy", OperationState.RESERVED)[0]

    result = governor.dispatch("legacy")

    assert result["dispatched"] is True
    assert registry.get("legacy").state == OperationState.DISPATCHING


def test_enabled_gate_requires_authority():
    try:
        DualKernelGovernor(
            object(), TransitionMembrane(MembraneState()),
            require_commit_authorization=True,
        )
    except ValueError as exc:
        assert "requires a Profile S commit authority" in str(exc)
    else:
        raise AssertionError("enabled commit gate accepted no authority")


def test_adjudication_requires_and_persists_complete_commit_identity(tmp_path):
    governor, registry, _, _, _, _, _, _ = _setup(tmp_path)

    class AlwaysPass:
        def adjudicate(self, **kwargs):
            return {
                "decision": "PASS", "rationale": "pass",
                "ces_state": "nominal", "s_score": 1.0,
            }

    class PassingMembrane:
        def __init__(self, state):
            self.state = state

        def adjudicate(self, **kwargs):
            return {
                "decision": "PASS", "rationale": "pass",
                "checks": [], "execution_status": "pending",
            }

    governor.scoring = AlwaysPass()
    governor.membrane = PassingMembrane(governor.membrane.state)
    missing = Passage(
        passage_id="passage-missing", idempotency_key="idem-missing",
        agent_id="agent-1", action_type="payment",
    )
    complete = Passage(
        passage_id="passage-complete", idempotency_key="idem-complete",
        agent_id="agent-1", action_type="payment",
    )

    rejected = governor.adjudicate(
        missing, institutional_scope="institution-a",
    )
    accepted = governor.adjudicate(
        complete,
        payload_digest=PAYLOAD_DIGEST,
        operation_id="operation-complete",
        institutional_scope="institution-a",
    )

    assert rejected["operation_state"] == "not_registered"
    assert registry.get("idem-missing") is None
    assert accepted["operation_state"] == OperationState.RESERVED.value
    durable = registry.get("idem-complete")
    assert durable.commit_gate_required is True
    assert durable.operation_id == "operation-complete"
    assert durable.institutional_scope == "institution-a"
    assert durable.payload_digest == PAYLOAD_DIGEST


def test_request_and_receipt_serialization_round_trip(tmp_path):
    governor, _, service, _, snapshot, proof, finding, request = _setup(tmp_path)
    authorization = service.authorize(
        request, finding=finding, claim=proof,
        current_snapshot=snapshot, now=NOW,
    )
    consumption = service.consume(
        request, authorization, dispatch_claim_id="dispatch-claim",
    )

    assert authorization_request_from_dict(
        authorization_request_to_dict(request)
    ) == request
    assert authorization_receipt_from_dict(
        authorization_receipt_to_dict(authorization)
    ) == authorization
    assert consumption_receipt_from_dict(
        consumption_receipt_to_dict(consumption)
    ) == consumption


def test_dispatch_requires_explicit_exact_authorization(tmp_path):
    governor, registry, _, _, snapshot, proof, finding, request = _setup(tmp_path)

    blocked = governor.dispatch("idem-1", metadata=PAYLOAD)
    authorized = _authorize(governor, snapshot, proof, finding, request)
    dispatched = governor.dispatch("idem-1", metadata=PAYLOAD)

    assert blocked["dispatched"] is False
    assert "No durable" in blocked["reason"]
    assert authorized["authorized"] is True
    assert dispatched["dispatched"] is True
    durable = registry.get("idem-1")
    assert durable.state == OperationState.DISPATCHING
    assert durable.commit_consumption_receipt["body"]["decision"] == "CONSUMED"


def test_substituted_operation_binding_is_rejected_without_authority_record(tmp_path):
    governor, _, service, _, snapshot, proof, finding, request = _setup(tmp_path)
    substituted = replace(
        request,
        binding=replace(request.binding, payload_digest="f" * 64),
    )

    result = _authorize(governor, snapshot, proof, finding, substituted)

    assert result["authorized"] is False
    assert "does not match" in result["reason"]
    assert service.lookup_authorization(substituted).status.value == "NOT_RECORDED"


def test_registry_itself_refuses_dispatch_without_consumption(tmp_path):
    _, registry, _, _, _, _, _, _ = _setup(tmp_path)
    record = registry.get("idem-1")
    record.commit_dispatch_claim_id = "fabricated-claim"
    record.commit_consumption_receipt = {
        "body": {
            "decision": "CONSUMED",
            "dispatch_claim_id": "fabricated-claim",
        },
        "signature": "00" * 64,
    }
    registry._persist()

    success, previous = registry.transition_to("idem-1", OperationState.DISPATCHING)

    assert success is False
    assert previous == OperationState.RESERVED
    assert registry.get("idem-1").state == OperationState.RESERVED


def test_malformed_request_fails_closed_without_exception(tmp_path):
    governor, _, _, _, snapshot, proof, finding, _ = _setup(tmp_path)

    result = _authorize(governor, snapshot, proof, finding, object())

    assert result["authorized"] is False
    assert "does not match" in result["reason"]


def test_lost_authorization_reply_is_recovered_by_lookup(tmp_path):
    class LostAuthorizationReply(ProfileSCommitAuthority):
        def _fault(self, point):
            if point == "after_authorization_commit_before_reply":
                raise ConnectionError("authorization reply lost")

    governor, _, _, _, snapshot, proof, finding, request = _setup(
        tmp_path, authority_class=LostAuthorizationReply,
    )

    result = _authorize(governor, snapshot, proof, finding, request)

    assert result["authorized"] is True
    assert result["replayed"] is True


def test_lost_consumption_reply_is_recovered_without_second_claim(tmp_path):
    class LostConsumptionReply(ProfileSCommitAuthority):
        def _fault(self, point):
            if point == "after_consumption_commit_before_reply":
                raise ConnectionError("consumption reply lost")

    governor, registry, _, _, snapshot, proof, finding, request = _setup(
        tmp_path, authority_class=LostConsumptionReply,
    )
    assert _authorize(governor, snapshot, proof, finding, request)["authorized"]

    result = governor.dispatch("idem-1", metadata=PAYLOAD)

    assert result["dispatched"] is True
    durable = registry.get("idem-1")
    assert durable.commit_consumption_receipt["body"]["decision"] == "CONSUMED"
    assert durable.state == OperationState.DISPATCHING


def test_trust_advance_makes_consumption_stale_and_blocks_dispatch(tmp_path):
    governor, registry, service, _, snapshot, proof, finding, request = _setup(tmp_path)
    assert _authorize(governor, snapshot, proof, finding, request)["authorized"]
    first = service.anchor.get()
    advanced = _snapshot(service._test_root, service._test_issuer, 2)
    assert service.anchor.compare_and_swap(
        "trust-advance-2", first, advanced,
    ).result == DurableCASResult.APPLIED

    result = governor.dispatch("idem-1", metadata=PAYLOAD)

    assert result["dispatched"] is False
    assert result["commit_consumption_status"] == "STALE"
    durable = registry.get("idem-1")
    assert durable.state == OperationState.RESERVED
    assert durable.commit_consumption_status == "HOLD"
    assert durable.commit_gate_events[-1]["status"] == "HOLD"


def test_quarantine_between_consumption_and_claim_blocks_dispatch(tmp_path):
    governor, registry, _, _, snapshot, proof, finding, request = _setup(tmp_path)
    assert _authorize(governor, snapshot, proof, finding, request)["authorized"]
    assert governor._consume_commit_authorization("idem-1")["consumed"]
    governor.membrane.lock_quarantine("incident-operator")

    blocked = governor.dispatch("idem-1", metadata=PAYLOAD)

    assert blocked["dispatched"] is False
    assert "QUARANTINE_LOCK" in blocked["reason"]
    assert registry.get("idem-1").state == OperationState.RESERVED

    released = governor.membrane.release_quarantine(
        "operator", "institution-a", "ceremony-G-R5-001", "reviewed",
    )
    assert released["released"]
    assert governor.dispatch("idem-1", metadata=PAYLOAD)["dispatched"] is True


def test_blocked_authority_call_does_not_block_quarantine_activation(tmp_path):
    governor, registry, service, _, snapshot, proof, finding, request = _setup(tmp_path)
    assert _authorize(governor, snapshot, proof, finding, request)["authorized"]
    entered = threading.Event()
    release = threading.Event()
    original_consume = service.consume

    def blocked_consume(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original_consume(*args, **kwargs)

    service.consume = blocked_consume
    results = []
    worker = threading.Thread(
        target=lambda: results.append(governor.dispatch("idem-1", metadata=PAYLOAD))
    )
    worker.start()
    assert entered.wait(timeout=5)

    governor.membrane.lock_quarantine("incident-operator")
    release.set()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert results[0]["dispatched"] is False
    assert "QUARANTINE_LOCK" in results[0]["reason"]
    assert registry.get("idem-1").state == OperationState.RESERVED


def test_cross_process_quarantine_activation_wins_after_authority_consumption(tmp_path):
    governor, registry, _, receipt_key, snapshot, proof, finding, request = _setup(tmp_path)
    assert _authorize(governor, snapshot, proof, finding, request)["authorized"]
    ctx = mp.get_context("spawn")
    entered = ctx.Event()
    release = ctx.Event()
    queue = ctx.Queue()
    process = ctx.Process(
        target=_blocked_integrated_dispatch_worker,
        args=(
            registry._store_path,
            str(tmp_path / "authority.sqlite3"),
            _raw_private(receipt_key),
            entered,
            release,
            queue,
        ),
    )

    process.start()
    assert entered.wait(timeout=20)
    governor.lock_quarantine("independent-incident", incident_id="INT-19")
    release.set()
    result = queue.get(timeout=30)
    process.join(timeout=30)

    assert process.exitcode == 0
    assert result[0] is False, result
    assert "QUARANTINE_LOCK" in result[1]
    assert DurableOperationRegistry(registry._store_path).get("idem-1").state == OperationState.RESERVED


def test_tampered_durable_receipt_blocks_dispatch(tmp_path):
    governor, registry, _, _, snapshot, proof, finding, request = _setup(tmp_path)
    assert _authorize(governor, snapshot, proof, finding, request)["authorized"]
    record = registry.get("idem-1")
    record.commit_authorization_receipt["signature"] = "00" * 64
    registry._persist()

    result = governor.dispatch("idem-1", metadata=PAYLOAD)

    assert result["dispatched"] is False
    assert registry.get("idem-1").state == OperationState.RESERVED


def test_two_workers_share_one_consumption_and_one_dispatch_claim(tmp_path):
    governor, registry, service, _, snapshot, proof, finding, request = _setup(tmp_path)
    other = DualKernelGovernor(
        object(), TransitionMembrane(MembraneState()),
        registry=DurableOperationRegistry(registry._store_path),
        commit_authority=service, require_commit_authorization=True,
    )
    assert _authorize(governor, snapshot, proof, finding, request)["authorized"]

    first = governor.dispatch("idem-1", metadata=PAYLOAD)
    second = other.dispatch("idem-1", metadata=PAYLOAD)

    assert first["dispatched"] is True
    assert second["dispatched"] is False
    assert registry.get("idem-1").state == OperationState.DISPATCHING
    lookup = service.lookup_consumption(request)
    assert lookup.receipt.decision == ConsumptionDecision.CONSUMED


def test_two_processes_integrated_gate_obtain_one_dispatch_claim(tmp_path):
    governor, registry, service, receipt_key, snapshot, proof, finding, request = _setup(tmp_path)
    assert _authorize(governor, snapshot, proof, finding, request)["authorized"]
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(2)
    queue = ctx.Queue()
    args = (
        registry._store_path,
        str(tmp_path / "authority.sqlite3"),
        _raw_private(receipt_key),
        barrier,
        queue,
    )
    processes = [ctx.Process(target=_integrated_dispatch_worker, args=args) for _ in range(2)]

    for process in processes:
        process.start()
    results = [queue.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    assert all(result[0] != "error" for result in results), results
    assert sorted(result[0] for result in results) == [False, True]
    assert DurableOperationRegistry(registry._store_path).get("idem-1").state == OperationState.DISPATCHING
    lookup = service.lookup_consumption(request)
    assert lookup.receipt.decision == ConsumptionDecision.CONSUMED


def test_provider_confirmation_terminal_write_recovers_with_durable_budget(tmp_path):
    provider = MockProvider(behavior="confirmed")
    governor, registry, service, _, snapshot, proof, finding, request = _setup(
        tmp_path,
        resource_id="budget",
        amount=10,
        provider=provider,
    )
    assert _authorize(governor, snapshot, proof, finding, request)["authorized"]
    original_transition = registry.transition_to

    def fail_confirm_transition(idempotency_key, new_state, *args, **kwargs):
        if new_state == OperationState.CONFIRMED:
            raise OSError("injected terminal registry write failure")
        return original_transition(idempotency_key, new_state, *args, **kwargs)

    registry.transition_to = fail_confirm_transition
    result = governor.dispatch_and_execute("idem-1", metadata=PAYLOAD)

    assert result["dispatched"] is True
    assert result["executed"] is True
    assert result["recommendation"] == "human_review"
    durable = DurableOperationRegistry(registry._store_path).get("idem-1")
    assert durable.state == OperationState.DISPATCHING
    assert durable.budget_reserved is False
    assert durable.budget_committed is True
    budget = governor.consensus_backend.get_state("budget")
    assert budget.reserved == Decimal("0")
    assert budget.committed == Decimal("10")

    recovered_registry = DurableOperationRegistry(registry._store_path)
    recovered_backend = SQLiteConsensusBackend(str(tmp_path / "budget.sqlite"))
    recovered = DualKernelGovernor(
        object(), TransitionMembrane(MembraneState()),
        registry=recovered_registry,
        consensus_backend=recovered_backend,
        commit_authority=service,
        require_commit_authorization=True,
    )
    assert recovered.confirm("idem-1") is True
    final = recovered_registry.get("idem-1")
    assert final.state == OperationState.CONFIRMED
    assert final.budget_reserved is False
    assert final.budget_committed is True
    final_budget = recovered_backend.get_state("budget")
    assert final_budget.reserved == Decimal("0")
    assert final_budget.committed == Decimal("10")


def test_resume_reports_authorization_step_before_dispatch(tmp_path):
    governor, _, _, _, _, _, _, _ = _setup(tmp_path)

    result = governor.resume_existing("idem-1")

    assert result["action"] == "authorize_commit"


def test_payload_mismatch_blocks_before_consumption_or_provider_call(tmp_path):
    governor, registry, service, _, snapshot, proof, finding, request = _setup(tmp_path)
    provider = MockProvider(behavior="confirmed")
    governor.provider = provider
    assert _authorize(governor, snapshot, proof, finding, request)["authorized"]

    result = governor.dispatch_and_execute(
        "idem-1", metadata={"amount": "11"},
    )

    assert result["dispatched"] is False
    assert "payload" in result["reason"].lower()
    assert provider.calls == []
    assert service.lookup_consumption(request).status.value == "NOT_RECORDED"
    durable = registry.get("idem-1")
    assert durable.state == OperationState.RESERVED
    assert durable.commit_dispatch_claim_id == ""


def test_exact_payload_is_frozen_verified_and_sent_to_provider(tmp_path):
    governor, registry, _, _, snapshot, proof, finding, request = _setup(tmp_path)
    provider = MockProvider(behavior="confirmed")
    governor.provider = provider
    assert _authorize(governor, snapshot, proof, finding, request)["authorized"]
    payload = {"amount": "10"}

    result = governor.dispatch_and_execute("idem-1", metadata=payload)
    payload["amount"] = "mutated-after-call"

    assert result["executed"] is True
    assert provider.calls[0]["metadata"] is not payload
    assert provider.calls[0]["metadata"] == PAYLOAD
    assert registry.get("idem-1").state == OperationState.CONFIRMED


def test_non_json_and_non_finite_payloads_fail_closed(tmp_path):
    governor, registry, service, _, snapshot, proof, finding, request = _setup(tmp_path)
    provider = MockProvider(behavior="confirmed")
    governor.provider = provider
    assert _authorize(governor, snapshot, proof, finding, request)["authorized"]

    cyclic = {}
    cyclic["self"] = cyclic
    too_deep = {}
    cursor = too_deep
    for _ in range(34):
        cursor["nested"] = {}
        cursor = cursor["nested"]
    invalid_results = [
        governor.dispatch_and_execute("idem-1", metadata={"value": float("nan")}),
        governor.dispatch_and_execute("idem-1", metadata={"value": object()}),
        governor.dispatch_and_execute("idem-1", metadata={1: "ambiguous-key"}),
        governor.dispatch_and_execute("idem-1", metadata=cyclic),
        governor.dispatch_and_execute("idem-1", metadata=too_deep),
        governor.dispatch_and_execute(
            "idem-1", metadata={"value": "x" * 1_048_577},
        ),
        governor.dispatch_and_execute("idem-1", metadata={"value": 1 << 4097}),
    ]

    assert all(result["dispatched"] is False for result in invalid_results)
    assert provider.calls == []
    assert service.lookup_consumption(request).status.value == "NOT_RECORDED"
    assert registry.get("idem-1").state == OperationState.RESERVED


def test_unknown_authority_outcome_is_durable_and_recoverable(tmp_path):
    class UnavailableAuthority(ProfileSCommitAuthority):
        def authorize(self, *args, **kwargs):
            raise TimeoutError("authority unavailable")

        def lookup_authorization(self, *args, **kwargs):
            raise ConnectionError("lookup unavailable")

    governor, registry, _, _, snapshot, proof, finding, request = _setup(
        tmp_path, authority_class=UnavailableAuthority,
    )

    result = _authorize(governor, snapshot, proof, finding, request)

    assert result["authorization_status"] == "UNKNOWN"
    durable = registry.get("idem-1")
    assert durable.commit_authorization_status == "TRUST_UNKNOWN"
    assert durable.commit_authorization_request["request_id"] == request.request_id
    assert durable.commit_gate_events[-1]["status"] == "TRUST_UNKNOWN"
    assert governor.resume_existing("idem-1")["action"] == "reconcile_commit_authorization"


def test_invalid_authority_signature_enters_durable_hold_and_security_event(tmp_path):
    class InvalidSignatureAuthority(ProfileSCommitAuthority):
        def authorize(self, *args, **kwargs):
            receipt = super().authorize(*args, **kwargs)
            return replace(receipt, signature=b"\x00" * 64)

    governor, registry, _, _, snapshot, proof, finding, request = _setup(
        tmp_path, authority_class=InvalidSignatureAuthority,
    )

    result = _authorize(governor, snapshot, proof, finding, request)

    assert result["authorization_status"] == "INVALID"
    durable = registry.get("idem-1")
    assert durable.commit_authorization_status == "HOLD"
    assert durable.commit_authorization_receipt is None
    assert durable.commit_gate_events[-1]["security_event"] is True
    assert governor.resume_existing("idem-1")["action"] == "return_authorization_result"


def test_unavailable_trusted_keys_fail_closed_and_are_audited(tmp_path):
    governor, registry, service, _, snapshot, proof, finding, request = _setup(tmp_path)
    assert _authorize(governor, snapshot, proof, finding, request)["authorized"]

    def unavailable_keys():
        raise RuntimeError("key store unavailable")

    service.trusted_receipt_keys = unavailable_keys
    result = governor.dispatch("idem-1", metadata=PAYLOAD)

    assert result["dispatched"] is False
    durable = registry.get("idem-1")
    assert durable.commit_consumption_status == "HOLD"
    assert durable.commit_gate_events[-1]["security_event"] is True
    assert durable.state == OperationState.RESERVED
