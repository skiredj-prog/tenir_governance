"""Governor integration tests for the optional Profile S commit gate."""
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
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
    operation_binding,
)
from tenir_governance.experimental.ep02_durable_cas import DurableCASResult
from tenir_governance.experimental.ep02_signed_trust import (
    SignedTrustState,
    bound_snapshot_bytes,
    verified_bound_preflight,
)
from tenir_governance.membrane import MembraneState, TransitionMembrane
from tenir_governance.objects import Passage
from tenir_governance.operations.registry import (
    DurableOperationRegistry,
    OperationRecord,
    OperationState,
)


NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)
PAYLOAD_DIGEST = sha256(b'{"amount":"10"}').hexdigest()


def _raw_public(key):
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _snapshot(root, issuer, revision=1):
    state = TrustState(revision, "policy-v1", (("issuer", "contracts"),), ())
    skeleton = SignedTrustState(
        state, b"", "root", (("issuer", _raw_public(issuer)),),
    )
    return SignedTrustState(
        state, root.sign(bound_snapshot_bytes(skeleton)), "root",
        skeleton.issuer_keys,
    )


def _setup(tmp_path, *, authority_class=ProfileSCommitAuthority):
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
        resource_id="",
        amount=0,
        payload_digest=PAYLOAD_DIGEST,
        finding=finding,
    )
    request = CommitAuthorizationRequest("authorization-1", binding)
    registry = DurableOperationRegistry(str(tmp_path / "operations.json"))
    membrane = TransitionMembrane(MembraneState())
    governor = DualKernelGovernor(
        object(), membrane, registry=registry,
        commit_authority=service, require_commit_authorization=True,
    )
    record = OperationRecord(
        idempotency_key="idem-1",
        passage_id="passage-1",
        agent_id="agent-1",
        action_type="payment",
        operation_id="operation-1",
        institutional_scope="institution-a",
        payload_digest=PAYLOAD_DIGEST,
        commit_gate_required=True,
    )
    assert registry.register(record)
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

    blocked = governor.dispatch("idem-1")
    authorized = _authorize(governor, snapshot, proof, finding, request)
    dispatched = governor.dispatch("idem-1")

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

    result = governor.dispatch("idem-1")

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

    result = governor.dispatch("idem-1")

    assert result["dispatched"] is False
    assert result["commit_consumption_status"] == "STALE"
    assert registry.get("idem-1").state == OperationState.RESERVED


def test_quarantine_between_consumption_and_claim_blocks_dispatch(tmp_path):
    governor, registry, _, _, snapshot, proof, finding, request = _setup(tmp_path)
    assert _authorize(governor, snapshot, proof, finding, request)["authorized"]
    assert governor._consume_commit_authorization("idem-1")["consumed"]
    governor.membrane.lock_quarantine("incident-operator")

    blocked = governor.dispatch("idem-1")

    assert blocked["dispatched"] is False
    assert "QUARANTINE_LOCK" in blocked["reason"]
    assert registry.get("idem-1").state == OperationState.RESERVED

    released = governor.membrane.release_quarantine(
        "operator", "institution-a", "ceremony-G-R5-001", "reviewed",
    )
    assert released["released"]
    assert governor.dispatch("idem-1")["dispatched"] is True


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
    worker = threading.Thread(target=lambda: results.append(governor.dispatch("idem-1")))
    worker.start()
    assert entered.wait(timeout=5)

    governor.membrane.lock_quarantine("incident-operator")
    release.set()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert results[0]["dispatched"] is False
    assert "QUARANTINE_LOCK" in results[0]["reason"]
    assert registry.get("idem-1").state == OperationState.RESERVED


def test_tampered_durable_receipt_blocks_dispatch(tmp_path):
    governor, registry, _, _, snapshot, proof, finding, request = _setup(tmp_path)
    assert _authorize(governor, snapshot, proof, finding, request)["authorized"]
    record = registry.get("idem-1")
    record.commit_authorization_receipt["signature"] = "00" * 64
    registry._persist()

    result = governor.dispatch("idem-1")

    assert result["dispatched"] is False
    assert registry.get("idem-1").state == OperationState.RESERVED


def test_two_workers_share_one_consumption_and_one_dispatch_claim(tmp_path):
    governor, registry, service, _, snapshot, proof, finding, request = _setup(tmp_path)
    other = DualKernelGovernor(
        object(), governor.membrane, registry=DurableOperationRegistry(registry._store_path),
        commit_authority=service, require_commit_authorization=True,
    )
    assert _authorize(governor, snapshot, proof, finding, request)["authorized"]

    first = governor.dispatch("idem-1")
    second = other.dispatch("idem-1")

    assert first["dispatched"] is True
    assert second["dispatched"] is False
    assert registry.get("idem-1").state == OperationState.DISPATCHING
    lookup = service.lookup_consumption(request)
    assert lookup.receipt.decision == ConsumptionDecision.CONSUMED


def test_resume_reports_authorization_step_before_dispatch(tmp_path):
    governor, _, _, _, _, _, _, _ = _setup(tmp_path)

    result = governor.resume_existing("idem-1")

    assert result["action"] == "authorize_commit"
