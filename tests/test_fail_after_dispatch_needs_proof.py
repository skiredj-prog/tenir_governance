"""Decision B3 (19 Nov 2026): once dispatch was possible, a failure without
proof of non-execution is UNKNOWN + reconciliation, never FAILED with the
budget given back. In-memory path (no consensus backend).

Since 5.7.6 the proof a caller supplies is proof only once the governor's
attestation verifier authenticates it (tests/test_attestation_verification.py
covers the verifier itself); the client here declares one HMAC key.
"""
import warnings

import pytest

from tenir_governance.composition.policy import CompositionPolicy
from tenir_governance.objects import Agent, Passage, Resource
from tenir_governance.operations.attestation import HmacAttestationVerifier, sign_non_execution_attestation
from tenir_governance.operations.registry import NonExecutionAttestation, OperationState
from tenir_governance.sdk import DualKernelCompositionClient

# 5.7.6 (RFC-0 §4): a FLAG admits only under conditions the policy declares, and
# their acknowledgement is required at dispatch. The test deployment declares one.
TEST_FLAG_CONDITIONS = ("test supervision",)
TEST_FLAG_ACK = {"test supervision": "acknowledged by the test"}
TEST_POLICY_KW = dict(flag_conditions=TEST_FLAG_CONDITIONS, hold_tau_k_seconds=3600.0, hold_tau_k_source="test fixture: one hour, not an SLA")
# 5.7.6: the key the reconciling component signs its attestations with.
KEY_ID, SECRET = "reconciliation", b"test-reconciliation-key-0123456789"

M = dict(irreversibility_materiality=0.0, pressure=0.2, velocity=0.3, capacity=1.0,
         authority=0.9, context_fit=0.9, evidence_quality=0.9, option_space=0.9)


def _client(store_path=None):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        c = DualKernelCompositionClient(
            store_path=store_path, policy=CompositionPolicy(**TEST_POLICY_KW),
            attestation_verifier=HmacAttestationVerifier({KEY_ID: SECRET}),
        )
    c.register_agent(Agent(agent_id="alice", authority_score=0.9))
    c.register_resource(Resource(resource_id="budget", capacity=100.0))
    return c


@pytest.fixture
def client():
    return _client()


def _dispatched(client, amount=10.0):
    p = Passage(agent_id="alice", action_type="payment", amount=amount)
    r = client.adjudicate(p, **M, resource_id="budget", amount=amount)
    assert r["operation_state"] == "reserved"
    assert client.dispatch(p.idempotency_key, resource_id="budget", amount=amount,
                           flag_conditions_met=TEST_FLAG_ACK)["dispatched"]
    return p.idempotency_key


def _accounted(client):
    b = client.governor.membrane.state.resources["budget"]
    return b.consumed + b.reserved


def test_fail_after_dispatch_without_proof_is_unknown_and_keeps_budget(client):
    key = _dispatched(client)
    before = _accounted(client)
    assert client.fail(key, resource_id="budget", amount=10.0) is False
    assert client.governor.registry.get(key).state == OperationState.UNKNOWN
    assert _accounted(client) == before  # nothing given back without proof


def test_fail_on_unknown_without_proof_changes_nothing(client):
    key = _dispatched(client)
    assert client.mark_unknown(key) is True
    before = _accounted(client)
    assert client.fail(key, resource_id="budget", amount=10.0) is False
    assert client.governor.registry.get(key).state == OperationState.UNKNOWN
    assert _accounted(client) == before


def _unsigned(key, **kw):
    base = dict(idempotency_key=key, source="provider", attestation="ch_123 refunded: never captured",
                attested_at="2026-11-19T06:40:00+00:00")
    base.update(kw)
    return NonExecutionAttestation(**base)


def _attestation(key, **kw):
    """A well-formed attestation, signed with the declared key."""
    return sign_non_execution_attestation(_unsigned(key, **kw), KEY_ID, SECRET)


def test_fail_after_dispatch_with_attestation_fails_and_persists_it(client):
    key = _dispatched(client)
    before = _accounted(client)
    att = _attestation(key)
    assert client.fail(key, resource_id="budget", amount=10.0, non_execution_attested=att) is True
    record = client.governor.registry.get(key)
    assert record.state == OperationState.FAILED
    # Persisted as the runtime's verified copy: same facts, verification named.
    assert record.non_execution_attested == att.verified(f"hmac-sha256:{KEY_ID}").to_dict()
    assert record.non_execution_attested["verified_by_runtime"] is True
    assert _accounted(client) == before - 10.0


def test_bare_boolean_is_not_proof(client):
    key = _dispatched(client)
    before = _accounted(client)
    with pytest.warns(DeprecationWarning):
        assert client.governor.fail(key, resource_id="budget", amount=10.0,
                                    authoritative_non_execution=True) is False
    assert client.governor.registry.get(key).state == OperationState.UNKNOWN
    assert _accounted(client) == before


@pytest.mark.parametrize("bad", [dict(source=""), dict(attestation=" "), dict(attested_at="yesterday"),
                                 dict(observed_effect_count=1)])
def test_malformed_attestation_is_refused(client, bad):
    key = _dispatched(client)
    before = _accounted(client)
    with pytest.raises(ValueError):
        _attestation(key, **bad)  # signing validates the form first
    malformed = NonExecutionAttestation(**{**_unsigned(key).to_dict(), **bad, "key_id": KEY_ID, "signature": "00"})
    assert client.fail(key, resource_id="budget", amount=10.0, non_execution_attested=malformed) is False
    assert client.governor.registry.get(key).state == OperationState.DISPATCHING
    assert client.governor.registry.get(key).non_execution_attested is None
    assert _accounted(client) == before


def test_caller_asserted_verification_is_refused(client):
    """5.7.6: only the runtime sets verified_by_runtime; a caller who asserts it
    supplies no proof, and the attempt is UNKNOWN pending reconciliation."""
    key = _dispatched(client)
    before = _accounted(client)
    claimed = _attestation(key).verified(f"hmac-sha256:{KEY_ID}")
    assert client.fail(key, resource_id="budget", amount=10.0, non_execution_attested=claimed) is False
    assert client.governor.registry.get(key).state == OperationState.UNKNOWN
    assert client.governor.registry.get(key).non_execution_attested is None
    assert _accounted(client) == before


def test_attestation_for_another_attempt_is_refused(client):
    key = _dispatched(client)
    other = _dispatched(client, amount=5.0)
    assert client.fail(key, resource_id="budget", amount=10.0,
                       non_execution_attested=_attestation(other)) is False
    assert client.governor.registry.get(key).non_execution_attested is None


def test_attestation_survives_a_restart(tmp_path):
    store = str(tmp_path / "ops.json")
    c = _client(store)
    key = _dispatched(c)
    att = _attestation(key)
    assert c.fail(key, resource_id="budget", amount=10.0, non_execution_attested=att)
    c2 = _client(store)
    assert c2.governor.registry.get(key).non_execution_attested == att.verified(f"hmac-sha256:{KEY_ID}").to_dict()


def test_fail_before_dispatch_needs_no_proof(client):
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    client.adjudicate(p, **M, resource_id="budget", amount=10.0)
    before = _accounted(client)
    assert client.fail(p.idempotency_key, resource_id="budget", amount=10.0) is True
    assert client.governor.registry.get(p.idempotency_key).state == OperationState.FAILED
    assert _accounted(client) == before - 10.0
