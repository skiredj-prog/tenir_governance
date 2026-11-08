"""Governor proxy tests; all service/provider effects are synthetic."""
import hashlib
import time
from copy import deepcopy

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from b1_approved_positive_uid_service import ApprovedPositiveUidService, canonical_approval
from b1_governor_uid_provider import ExperimentalGovernorUidProvider
from kernel_claim_provider import KernelClaimProofProvider
from native_positive_g4a import NativePositiveG4A
from native_positive_permit import NativePositivePermitAuthority
from tenir_governance.composition.governor import DualKernelGovernor, ScoringKernelAdapter
from tenir_governance.membrane import TransitionMembrane, MembraneState, Agent, Resource
from tenir_governance.objects import Passage
from tenir_governance.operations.provider import ProviderOutcome
from tenir_governance.operations.registry import DurableOperationRegistry, OperationRecord, OperationState
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from tenir_kernel.kernel import TenirKernel
from w2_2b_canonical import request_hash


@pytest.fixture
def integrated_lab(tmp_path, monkeypatch):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())
    monkeypatch.setenv("TENIR_B3_PRIVATE_KEY_HEX", bytes(range(64, 96)).hex())
    backend = SQLiteConsensusBackend(str(tmp_path))
    assert backend.register_resource("budget", 100) == "REGISTERED"
    service = object.__new__(ApprovedPositiveUidService)
    service.authority = NativePositivePermitAuthority(backend)
    service.gate = NativePositiveG4A(service.authority)
    service.provider = KernelClaimProofProvider(claim_verifier=service.authority.verify_dispatch_claim)
    signer = Ed25519PrivateKey.from_private_bytes(bytes(range(96, 128)))
    public = signer.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    service._initialize_approval_trust(public)
    proxy = object.__new__(ExperimentalGovernorUidProvider)
    calls = []
    def exchange(request):
        calls.append(request)
        try:
            return {"ok": True, "receipt": service._dispatch(request)}
        except (ValueError, RuntimeError):
            return {"ok": False, "error": "request refused"}
    proxy._exchange = exchange
    state = MembraneState()
    state.register_resource(Resource(resource_id="budget", capacity=100))
    state.register_agent(Agent(agent_id="alice", authority_score=0.9))
    governor = DualKernelGovernor(ScoringKernelAdapter(TenirKernel()), TransitionMembrane(state),
                                  registry=DurableOperationRegistry(), provider=proxy)
    proxy.registry = governor.registry
    assert governor.registry.register(OperationRecord(idempotency_key="lab:op", passage_id="lab:p",
                                                       agent_id="alice", action_type="payment", amount=7,
                                                       resource_id="budget"))
    assert governor.registry.transition_to("lab:op", OperationState.RESERVED)[0]
    assert governor.registry.transition_to("lab:op", OperationState.DISPATCHING)[0]
    def request_for(key):
        command = {"schema": "tenir-dispatch-command/v1", "resource_id": "budget", "amount": 7,
                   "action_type": "payment", "agent_id": "alice", "metadata": {"recipient": "lab"}}
        now = int(time.time())
        body = {"schema": "tenir.b1.approval.v1", "audience": "positive-uid-laboratory",
                "approval_id": "approval-" + key, "nonce": "nonce-" + key,
                "operation_key": key, "evaluation_id": "eval-" + key, "idempotency_key": key,
                "request_hash": request_hash(command), "issued_at": now, "expires_at": now + 300,
                "key_id": hashlib.sha256(public).hexdigest()}
        return {"schema": "tenir.positive.approved.uid-lab.v1", "action": "submit",
                "operation_key": key, "evaluation_id": "eval-" + key, "idempotency_key": key,
                "command": command, "approval": {**body, "signature": signer.sign(canonical_approval(body)).hex()}}
    yield governor, backend, service, proxy, calls, request_for
    service.provider.conn.close()


def test_governor_to_signed_synthetic_service_stays_unknown(integrated_lab):
    governor, backend, service, proxy, calls, request_for = integrated_lab
    passage = Passage(agent_id="alice", action_type="payment", amount=7, idempotency_key="lab:governor")
    signed = request_for(passage.idempotency_key)
    envelope = {"b1_lab_request": signed}
    admitted = governor.adjudicate(passage, pressure=0.2, velocity=0.2, capacity=0.9,
                                   authority=0.9, context_fit=1.0, evidence_quality=1.0,
                                   resource_id="budget", amount=7, payload=envelope)
    assert admitted["operation_state"] == OperationState.RESERVED.value
    result = governor.dispatch_and_execute(passage.idempotency_key, "budget", 7,
                                           metadata=envelope)
    assert result["operation_state"] == OperationState.UNKNOWN.value, result
    assert governor.registry.get(passage.idempotency_key).state == OperationState.UNKNOWN
    assert service.provider.execute_count(passage.idempotency_key) == 1
    assert backend.get_state("budget").reserved == 7
    assert len(calls) == 1
    assert proxy.reconcile(passage.idempotency_key).outcome == ProviderOutcome.UNKNOWN


@pytest.mark.parametrize("field", ["amount", "action_type", "agent_id", "idempotency", "resource"])
def test_substituted_governor_arguments_never_reach_service(integrated_lab, field):
    _, backend, service, proxy, calls, request_for = integrated_lab
    signed = request_for("lab:op")
    args = {"idempotency_key": "lab:op", "action_type": "payment", "amount": 7,
            "agent_id": "alice", "metadata": {"b1_lab_request": signed}}
    if field == "idempotency":
        args["idempotency_key"] = "different"
    elif field == "resource":
        args["metadata"]["b1_lab_request"]["command"]["resource_id"] = "different"
    else:
        args[field] = {"amount": 8, "action_type": "refund", "agent_id": "other"}[field]
    assert proxy.execute(**args).outcome == ProviderOutcome.UNKNOWN
    assert calls == []
    assert service.provider.execute_count("lab:op") == 0
    assert backend.get_state("budget").reserved == 0


def test_malformed_or_untrusted_response_cannot_confirm(integrated_lab):
    _, _, _, proxy, _, request_for = integrated_lab
    proxy._exchange = lambda _: {"ok": True, "receipt": {"status": "EXECUTED", "synthetic": False,
                                                         "execution_authorized": True}}
    result = proxy.execute("lab:op", "payment", 7, "alice", {"b1_lab_request": request_for("lab:op")})
    assert result.outcome == ProviderOutcome.UNKNOWN
    assert "refused" in result.error


def test_forged_signature_rejected_by_service_without_native_reservation(integrated_lab):
    _, backend, service, proxy, calls, request_for = integrated_lab
    request = request_for("lab:op")
    request["approval"]["signature"] = "00" * 64
    result = proxy.execute("lab:op", "payment", 7, "alice", {"b1_lab_request": request})
    assert result.outcome == ProviderOutcome.UNKNOWN
    assert len(calls) == 1
    assert service.provider.execute_count("lab:op") == 0
    assert backend.get_state("budget").reserved == 0


@pytest.mark.parametrize("amount", [float("nan"), float("inf"), -1, True])
def test_invalid_governor_amount_never_reaches_service(integrated_lab, amount):
    _, backend, service, proxy, calls, request_for = integrated_lab
    result = proxy.execute("lab:op", "payment", amount, "alice", {"b1_lab_request": request_for("lab:op")})
    assert result.outcome == ProviderOutcome.UNKNOWN
    assert calls == []
    assert service.provider.execute_count("lab:op") == 0
    assert backend.get_state("budget").reserved == 0


def test_registry_must_have_current_dispatching_record(integrated_lab):
    governor, backend, service, proxy, calls, request_for = integrated_lab
    assert governor.registry.register(OperationRecord(idempotency_key="lab:reserved", passage_id="lab:q",
                                                       agent_id="alice", action_type="payment", amount=7,
                                                       resource_id="budget"))
    assert governor.registry.transition_to("lab:reserved", OperationState.RESERVED)[0]
    result = proxy.execute("lab:reserved", "payment", 7, "alice",
                           {"b1_lab_request": request_for("lab:reserved")})
    assert result.outcome == ProviderOutcome.UNKNOWN
    assert calls == []
    assert service.provider.execute_count("lab:reserved") == 0
    assert backend.get_state("budget").reserved == 0


def test_unsupported_platform_refuses_before_opening_any_socket(tmp_path, monkeypatch):
    monkeypatch.setattr("b1_governor_uid_provider.sys.platform", "win32")
    with pytest.raises(RuntimeError, match="Linux peer credentials"):
        ExperimentalGovernorUidProvider(tmp_path / "positive-lab.sock", writer_uid=123, registry=object())


def test_governor_payload_binding_refuses_post_adjudication_substitution(integrated_lab):
    governor, backend, service, _, calls, request_for = integrated_lab
    passage = Passage(agent_id="alice", action_type="payment", amount=7, idempotency_key="lab:binding")
    envelope = {"b1_lab_request": request_for(passage.idempotency_key)}
    admitted = governor.adjudicate(passage, pressure=0.2, velocity=0.2, capacity=0.9,
                                   authority=0.9, context_fit=1.0, evidence_quality=1.0,
                                   resource_id="budget", amount=7, payload=envelope)
    assert admitted["operation_state"] == OperationState.RESERVED.value
    altered = deepcopy(envelope)
    altered["b1_lab_request"]["command"]["metadata"]["recipient"] = "other"
    result = governor.dispatch_and_execute(passage.idempotency_key, "budget", 7, metadata=altered)
    assert result["dispatched"] is False
    assert result["operation_state"] == OperationState.RESERVED.value
    assert calls == []
    assert service.provider.execute_count(passage.idempotency_key) == 0
    assert backend.get_state("budget").reserved == 0


def test_governor_stays_unknown_if_independent_signature_is_forged(integrated_lab):
    governor, backend, service, _, calls, request_for = integrated_lab
    passage = Passage(agent_id="alice", action_type="payment", amount=7, idempotency_key="lab:forged")
    request = request_for(passage.idempotency_key)
    request["approval"]["signature"] = "00" * 64
    envelope = {"b1_lab_request": request}
    admitted = governor.adjudicate(passage, pressure=0.2, velocity=0.2, capacity=0.9,
                                   authority=0.9, context_fit=1.0, evidence_quality=1.0,
                                   resource_id="budget", amount=7, payload=envelope)
    assert admitted["operation_state"] == OperationState.RESERVED.value
    result = governor.dispatch_and_execute(passage.idempotency_key, "budget", 7, metadata=envelope)
    assert result["operation_state"] == OperationState.UNKNOWN.value
    assert len(calls) == 1
    assert service.provider.execute_count(passage.idempotency_key) == 0
    assert backend.get_state("budget").reserved == 0
