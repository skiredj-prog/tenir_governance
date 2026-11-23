"""5.7.6: a non-execution attestation is proof only once the runtime has
authenticated it (audit reserve P1 "attestation non authentifiée", 20 Nov 2026).

- A caller's attestation must carry a signature a declared key verifies; the
  governor records the verified copy (`verified_by_runtime=True`, method named).
- Without a verifier, with an unknown key, a wrong key, a tampered payload, or
  a caller-asserted verification, nothing is proof: the attempt is UNKNOWN,
  the budget is kept and the refusal is ledgered.
- The runtime's own provider observation (`dispatch_and_execute`,
  `reconcile_unknown`) is the runtime channel and needs no verifier.
- The registry never persists an unverified attestation.
"""
import warnings
from dataclasses import replace

import pytest

from tenir_governance.composition import DualKernelGovernor, ScoringKernelAdapter
from tenir_governance.composition.policy import CompositionPolicy
from tenir_governance.membrane import MembraneState, TransitionMembrane
from tenir_governance.objects import Agent, Passage, Resource
from tenir_governance.operations import MockProvider, OperationState, ProviderOutcome
from tenir_governance.operations.attestation import (
    Ed25519AttestationVerifier, HmacAttestationVerifier,
    sign_non_execution_attestation, sign_non_execution_attestation_ed25519,
)
from tenir_governance.operations.registry import DurableOperationRegistry, NonExecutionAttestation
from tenir_governance.sdk import DualKernelCompositionClient
from tenir_kernel.kernel import TenirKernel

CONDITIONS = ("ops supervision",)
ACK = {"ops supervision": "ops rota 2026-11-20"}
POLICY = CompositionPolicy(flag_conditions=CONDITIONS)
M = dict(irreversibility_materiality=0.0, pressure=0.2, velocity=0.3, capacity=1.0, authority=0.9, context_fit=0.9,
         evidence_quality=0.9, option_space=0.9)
KEYS = {"ops": b"ops-reconciliation-key-0123456789", "audit": b"audit-key-abcdefghijklmnopqrstuvwxyz"}


def _attestation(key, **kw):
    base = dict(idempotency_key=key, source="ops-reconciliation", attestation="no charge for this attempt",
                attested_at="2026-11-20T19:00:00+00:00")
    base.update(kw)
    return NonExecutionAttestation(**base)


def _client(verifier=None, provider=None):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        c = DualKernelCompositionClient(policy=POLICY, attestation_verifier=verifier)
    c.register_agent(Agent(agent_id="alice", authority_score=0.9))
    c.register_resource(Resource(resource_id="budget", capacity=100.0))
    if provider is not None:
        c.governor.provider = provider
    return c


def _dispatched(client, amount=10.0):
    p = Passage(agent_id="alice", action_type="payment", amount=amount)
    assert client.adjudicate(p, **M, resource_id="budget", amount=amount)["operation_state"] == "reserved"
    assert client.dispatch(p.idempotency_key, "budget", amount, flag_conditions_met=ACK)["dispatched"]
    return p.idempotency_key


def _accounted(client):
    b = client.governor.membrane.state.resources["budget"]
    return b.consumed + b.reserved


def _refusals(client, key):
    return [e for e in client.governor.ledger.entries
            if e.idempotency_key == key and "NON_EXECUTION_ATTESTATION_REFUSED" in e.rationale]


# --- the verifier -----------------------------------------------------------

def test_hmac_round_trip_names_the_key():
    att = _attestation("op-1")
    signed = sign_non_execution_attestation(att, "ops", KEYS["ops"])
    assert signed.key_id == "ops" and len(signed.signature) == 64
    assert signed.verified_by_runtime is False and signed.verification == ""
    verifier = HmacAttestationVerifier(KEYS)
    assert verifier.verify(signed) == "hmac-sha256:ops"
    assert verifier.key_ids == ("audit", "ops")


@pytest.mark.parametrize("tamper", [
    dict(key_id="unknown"), dict(signature=""), dict(signature="00" * 32),
    dict(attestation="a different statement"), dict(idempotency_key="op-2"),
    dict(observed_effect_count=1), dict(attested_at="2026-11-20T19:00:01+00:00"),
])
def test_hmac_refuses_tampered_or_unknown(tamper):
    signed = sign_non_execution_attestation(_attestation("op-1"), "ops", KEYS["ops"])
    assert HmacAttestationVerifier(KEYS).verify(replace(signed, **tamper)) is None


def test_hmac_refuses_a_signature_under_another_declared_key():
    signed = sign_non_execution_attestation(_attestation("op-1"), "audit", KEYS["ops"])  # wrong secret
    assert HmacAttestationVerifier(KEYS).verify(signed) is None


def test_hmac_verify_never_raises_on_garbage():
    verifier = HmacAttestationVerifier(KEYS)
    assert verifier.verify(_attestation("op-1", key_id="ops", signature="not-hex")) is None
    assert verifier.verify(NonExecutionAttestation(idempotency_key="", source="", attestation="",
                                                   attested_at="", key_id="ops", signature="00")) is None


@pytest.mark.parametrize("keys", [{}, {"": b"0123456789abcdef"}, {"ops": b"short"}, {"ops": "short"}])
def test_hmac_verifier_rejects_weak_or_anonymous_keys(keys):
    with pytest.raises(ValueError):
        HmacAttestationVerifier(keys)


def test_signing_requires_a_key_id_and_a_well_formed_attestation():
    with pytest.raises(ValueError):
        sign_non_execution_attestation(_attestation("op-1"), "", KEYS["ops"])
    with pytest.raises(ValueError):
        sign_non_execution_attestation(_attestation("op-1", observed_effect_count=1), "ops", KEYS["ops"])


def test_ed25519_round_trip():
    cryptography = pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    signed = sign_non_execution_attestation_ed25519(_attestation("op-1"), "ops-ed", private)
    verifier = Ed25519AttestationVerifier({"ops-ed": public.hex()})
    assert verifier.verify(signed) == "ed25519:ops-ed"
    assert verifier.verify(replace(signed, attestation="tampered")) is None
    other = Ed25519PrivateKey.generate()
    assert verifier.verify(sign_non_execution_attestation_ed25519(_attestation("op-1"), "ops-ed", other)) is None


# --- the registry -----------------------------------------------------------

def test_registry_refuses_an_unverified_attestation():
    reg = DurableOperationRegistry()
    from tenir_governance.operations.registry import OperationRecord
    reg.register(OperationRecord(idempotency_key="op-1", passage_id="p", agent_id="a", action_type="payment"))
    reg.transition_to("op-1", OperationState.RESERVED)
    reg.transition_to("op-1", OperationState.DISPATCHING)
    signed = sign_non_execution_attestation(_attestation("op-1"), "ops", KEYS["ops"])
    with pytest.raises(ValueError, match="verified by the runtime"):
        reg.record_non_execution_attested("op-1", signed)
    assert reg.get("op-1").non_execution_attested is None
    assert reg.record_non_execution_attested("op-1", signed.verified("hmac-sha256:ops")) is True
    assert reg.get("op-1").non_execution_attested["verification"] == "hmac-sha256:ops"


def test_attestation_form_rejects_inconsistent_verification_fields():
    with pytest.raises(ValueError):
        _attestation("op-1", verified_by_runtime=True).validate()  # verified without a method
    with pytest.raises(ValueError):
        _attestation("op-1", verification="hmac-sha256:ops").validate()  # method without verification
    with pytest.raises(ValueError):
        _attestation("op-1").verified("")


# --- the governor: caller channel -------------------------------------------

def test_verified_attestation_closes_the_attempt_and_records_the_method():
    client = _client(HmacAttestationVerifier(KEYS))
    key = _dispatched(client)
    before = _accounted(client)
    signed = sign_non_execution_attestation(_attestation(key), "ops", KEYS["ops"])
    assert client.fail(key, "budget", 10.0, non_execution_attested=signed) is True
    record = client.governor.registry.get(key)
    assert record.state == OperationState.FAILED
    assert record.non_execution_attested["verified_by_runtime"] is True
    assert record.non_execution_attested["verification"] == "hmac-sha256:ops"
    assert record.non_execution_attested["signature"] == signed.signature
    assert _accounted(client) == before - 10.0
    assert _refusals(client, key) == []


def test_dict_form_is_verified_too():
    client = _client(HmacAttestationVerifier(KEYS))
    key = _dispatched(client)
    signed = sign_non_execution_attestation(_attestation(key), "ops", KEYS["ops"])
    assert client.fail(key, "budget", 10.0, non_execution_attested=signed.to_dict()) is True
    assert client.governor.registry.get(key).state == OperationState.FAILED


def test_without_a_verifier_a_caller_attestation_is_not_proof():
    """The 5.7.5 gap: a well-formed attestation released the budget. Now it is
    UNKNOWN + reconciliation, budget kept, and the refusal is in the ledger."""
    client = _client(verifier=None)
    key = _dispatched(client)
    before = _accounted(client)
    signed = sign_non_execution_attestation(_attestation(key), "ops", KEYS["ops"])
    assert client.fail(key, "budget", 10.0, non_execution_attested=signed) is False
    record = client.governor.registry.get(key)
    assert record.state == OperationState.UNKNOWN
    assert record.non_execution_attested is None
    assert _accounted(client) == before
    (entry,) = _refusals(client, key)
    assert "no attestation_verifier is configured" in entry.rationale
    assert "DISPATCHING → UNKNOWN" in entry.rationale


def test_unsigned_attestation_is_refused_even_with_a_verifier():
    client = _client(HmacAttestationVerifier(KEYS))
    key = _dispatched(client)
    before = _accounted(client)
    assert client.fail(key, "budget", 10.0, non_execution_attested=_attestation(key)) is False
    assert client.governor.registry.get(key).state == OperationState.UNKNOWN
    assert _accounted(client) == before
    (entry,) = _refusals(client, key)
    assert "signature not verified under key_id '-'" in entry.rationale


@pytest.mark.parametrize("forge", ["wrong-secret", "unknown-key", "tampered"])
def test_forged_attestation_is_refused(forge):
    client = _client(HmacAttestationVerifier(KEYS))
    key = _dispatched(client)
    before = _accounted(client)
    if forge == "wrong-secret":
        att = sign_non_execution_attestation(_attestation(key), "ops", b"not-the-declared-secret-at-all")
    elif forge == "unknown-key":
        att = sign_non_execution_attestation(_attestation(key), "intruder", KEYS["ops"])
    else:
        att = replace(sign_non_execution_attestation(_attestation(key), "ops", KEYS["ops"]),
                      attestation="charge ch_999 never captured (edited)")
    assert client.fail(key, "budget", 10.0, non_execution_attested=att) is False
    assert client.governor.registry.get(key).state == OperationState.UNKNOWN
    assert client.governor.registry.get(key).non_execution_attested is None
    assert _accounted(client) == before
    assert len(_refusals(client, key)) == 1


def test_caller_asserted_verification_is_refused_before_any_check():
    client = _client(HmacAttestationVerifier(KEYS))
    key = _dispatched(client)
    claimed = sign_non_execution_attestation(_attestation(key), "ops", KEYS["ops"]).verified("hmac-sha256:ops")
    assert client.fail(key, "budget", 10.0, non_execution_attested=claimed) is False
    assert client.governor.registry.get(key).state == OperationState.UNKNOWN
    (entry,) = _refusals(client, key)
    assert "caller asserted verified_by_runtime" in entry.rationale


def test_refused_attestation_leaves_the_attempt_recoverable():
    """UNKNOWN is not terminal: a later verified attestation still closes it."""
    client = _client(HmacAttestationVerifier(KEYS))
    key = _dispatched(client)
    assert client.fail(key, "budget", 10.0, non_execution_attested=_attestation(key)) is False
    assert client.governor.registry.get(key).state == OperationState.UNKNOWN
    signed = sign_non_execution_attestation(_attestation(key), "ops", KEYS["ops"])
    assert client.fail(key, "budget", 10.0, non_execution_attested=signed) is True
    assert client.governor.registry.get(key).state == OperationState.FAILED


def test_before_dispatch_no_attestation_is_needed_or_verified():
    client = _client(verifier=None)
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    client.adjudicate(p, **M, resource_id="budget", amount=10.0)
    assert client.fail(p.idempotency_key, "budget", 10.0) is True
    assert client.governor.registry.get(p.idempotency_key).state == OperationState.FAILED


# --- the governor: runtime channel ------------------------------------------

def test_provider_failed_outcome_is_the_runtime_channel_without_a_verifier():
    client = _client(verifier=None, provider=MockProvider(behavior="failed"))
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    client.adjudicate(p, **M, resource_id="budget", amount=10.0)
    out = client.governor.dispatch_and_execute(p.idempotency_key, "budget", 10.0, flag_conditions_met=ACK)
    assert out["operation_state"] == OperationState.FAILED.value
    record = client.governor.registry.get(p.idempotency_key)
    assert record.non_execution_attested["verified_by_runtime"] is True
    assert record.non_execution_attested["verification"] == "provider-channel"
    assert record.non_execution_attested["source"] == "provider"
    assert _accounted(client) == 0.0


def test_reconciliation_failed_outcome_is_the_runtime_channel():
    provider = MockProvider(behavior="timeout", reconcile_outcome=ProviderOutcome.FAILED)
    client = _client(verifier=None, provider=provider)
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    client.adjudicate(p, **M, resource_id="budget", amount=10.0)
    out = client.governor.dispatch_and_execute(p.idempotency_key, "budget", 10.0, flag_conditions_met=ACK)
    assert out["operation_state"] == OperationState.UNKNOWN.value
    assert client.governor.reconcile_unknown(p.idempotency_key)["operation_state"] == OperationState.FAILED.value
    record = client.governor.registry.get(p.idempotency_key)
    assert record.non_execution_attested["verification"] == "provider-reconciliation-channel"
    assert record.non_execution_attested["verified_by_runtime"] is True


def test_governor_rejects_a_verifier_without_verify():
    state = MembraneState()
    with pytest.raises((TypeError, ValueError)):
        DualKernelGovernor(ScoringKernelAdapter(TenirKernel()), TransitionMembrane(state),
                           policy=POLICY, attestation_verifier=object())


def test_sdk_exposes_the_attestation_tools():
    import tenir_governance
    for name in ("NonExecutionAttestation", "HmacAttestationVerifier", "Ed25519AttestationVerifier",
                 "sign_non_execution_attestation", "AttestationVerifier"):
        assert hasattr(tenir_governance, name), name
