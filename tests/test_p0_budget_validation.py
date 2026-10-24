"""P0 regressions: budget amounts are finite and strictly positive."""

from decimal import Decimal

import pytest

from tenir_governance.composition.decisions import Decision
from tenir_governance.membrane import MembraneState, TransitionMembrane
from tenir_governance.objects import Passage, Resource
from tenir_governance.operations.budget import InProcessBudgetCoordinator
from tenir_governance.operations.payload_binding import (
    EMPTY_PAYLOAD_HASH,
    canonical_payload_hash,
)
from tenir_governance.operations.registry import OperationRecord


INVALID_AMOUNTS = [
    True,
    False,
    0,
    0.0,
    -1,
    float("nan"),
    float("inf"),
    float("-inf"),
    Decimal("NaN"),
    Decimal("Infinity"),
    "not-a-number",
]


@pytest.mark.parametrize("amount", INVALID_AMOUNTS)
def test_in_process_budget_coordinator_rejects_invalid_mutation_amounts(amount):
    resource = Resource(resource_id="budget", capacity=100.0)
    coordinator = InProcessBudgetCoordinator()
    coordinator.register(resource)

    assert coordinator.reserve("budget", amount) is False
    assert resource.reserved == 0.0
    assert coordinator.reserve("budget", 10.0) is True
    assert coordinator.commit("budget", amount) is False
    coordinator.release("budget", amount)
    assert resource.reserved == 10.0
    assert resource.consumed == 0.0


@pytest.mark.parametrize("amount", INVALID_AMOUNTS)
def test_membrane_vetoes_invalid_budget_amount_before_accounting(amount):
    state = MembraneState()
    resource = Resource(resource_id="budget", capacity=100.0)
    state.register_resource(resource)
    membrane = TransitionMembrane(state)

    result = membrane.adjudicate(
        Passage(agent_id="alice", action_type="payment"),
        resource_id="budget",
        amount=amount,
    )

    assert result["decision"] == Decision.HARD_VETO.value
    assert any(
        check["check_id"] == "I8" and check["decision"] == Decision.HARD_VETO.value
        for check in result["checks"]
    )
    assert resource.reserved == 0.0
    assert resource.consumed == 0.0


@pytest.mark.parametrize("amount", INVALID_AMOUNTS)
@pytest.mark.parametrize("method", ["commit_passage", "fail_passage"])
def test_membrane_lifecycle_rejects_invalid_budget_amount_before_state_change(amount, method):
    state = MembraneState()
    resource = Resource(resource_id="budget", capacity=100.0)
    state.register_resource(resource)
    passage = Passage(agent_id="alice", action_type="payment")
    membrane = TransitionMembrane(state)

    with pytest.raises(ValueError, match="finite and strictly positive"):
        getattr(membrane, method)(passage, resource_id="budget", amount=amount)

    assert passage.execution_status.value == "pending"
    assert resource.reserved == 0.0
    assert resource.consumed == 0.0


def test_payload_hash_uses_canonical_json_and_survives_record_round_trip():
    first = {"recipient": "merchant-1", "details": {"currency": "USD", "kind": "payment"}}
    reordered = {"details": {"kind": "payment", "currency": "USD"}, "recipient": "merchant-1"}
    digest = canonical_payload_hash(first)

    assert digest == canonical_payload_hash(reordered)
    record = OperationRecord(
        idempotency_key="payload-bound",
        passage_id="passage-payload-bound",
        agent_id="alice",
        action_type="payment",
        payload_hash=digest,
    )
    assert OperationRecord.from_dict(record.to_dict()).payload_hash == digest
    assert canonical_payload_hash(None) == EMPTY_PAYLOAD_HASH


@pytest.mark.parametrize("payload", [{"value": float("nan")}, {1: "non-string key"}, {"value": object()}])
def test_payload_hash_rejects_ambiguous_or_non_json_values(payload):
    with pytest.raises(ValueError):
        canonical_payload_hash(payload)
