"""Decision B3 (19 Nov 2026): once dispatch was possible, a failure without
proof of non-execution is UNKNOWN + reconciliation, never FAILED with the
budget given back. In-memory path (no consensus backend)."""
import warnings

import pytest

from tenir_governance.objects import Agent, Passage, Resource
from tenir_governance.operations.registry import OperationState
from tenir_governance.sdk import DualKernelCompositionClient

M = dict(pressure=0.2, velocity=0.3, capacity=1.0,
         authority=0.9, context_fit=0.9, evidence_quality=0.9)


@pytest.fixture
def client():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        c = DualKernelCompositionClient()
    c.register_agent(Agent(agent_id="alice", authority_score=0.9))
    c.register_resource(Resource(resource_id="budget", capacity=100.0))
    return c


def _dispatched(client, amount=10.0):
    p = Passage(agent_id="alice", action_type="payment", amount=amount)
    r = client.adjudicate(p, **M, resource_id="budget", amount=amount)
    assert r["operation_state"] == "reserved"
    assert client.dispatch(p.idempotency_key, resource_id="budget", amount=amount)["dispatched"]
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


def test_fail_after_dispatch_with_proof_fails_and_gives_budget_back(client):
    key = _dispatched(client)
    before = _accounted(client)
    assert client.fail(key, resource_id="budget", amount=10.0,
                       authoritative_non_execution=True) is True
    assert client.governor.registry.get(key).state == OperationState.FAILED
    assert _accounted(client) == before - 10.0


def test_fail_before_dispatch_needs_no_proof(client):
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    client.adjudicate(p, **M, resource_id="budget", amount=10.0)
    before = _accounted(client)
    assert client.fail(p.idempotency_key, resource_id="budget", amount=10.0) is True
    assert client.governor.registry.get(p.idempotency_key).state == OperationState.FAILED
    assert _accounted(client) == before - 10.0
