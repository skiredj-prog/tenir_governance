"""Adversarial reproductions against feat/governor-durable-budget-ownership.

These tests do not change production code. On 32c3b7a they are expected to fail:

- in-memory expire_hold releases aggregate budget without ownership flags;
- expire_hold with a durable reservation_id releases that id even when flags are false;
- expire_hold succeeds before an explicit tau_K has elapsed.
"""
from types import SimpleNamespace

from tenir_governance.operations import OperationRecord
from tests.test_hold_lifecycle import _setup_governor


class _RecordingBackend:
    def __init__(self):
        self.released: list[str] = []

    def release(self, reservation_id: str):
        self.released.append(reservation_id)
        return SimpleNamespace(status="RELEASED")


def test_pr13_expire_hold_does_not_release_unowned_memory_budget():
    gov, state = _setup_governor()
    budget = state.resources["budget"]
    assert budget.reserve(10.0)
    rec = OperationRecord(
        idempotency_key="op-pr13-unowned",
        passage_id="p-unowned",
        agent_id="alice",
        action_type="payment",
        amount=10.0,
        resource_id="budget",
        budget_reserved=False,
        budget_committed=False,
    )
    assert gov.registry.register(rec)
    assert gov.hold_manager.enter_hold("op-pr13-unowned", tau_k_seconds=300)["entered"]
    gov.registry.get("op-pr13-unowned").hold_record.hold_start = "2000-01-01T00:00:00+00:00"

    result = gov.expire_hold("op-pr13-unowned")

    assert result["expired"] is True
    assert budget.reserved == 10.0
    assert budget.consumed == 0.0


def test_pr13_expire_hold_does_not_release_unowned_backend_reservation():
    gov, _state = _setup_governor()
    backend = _RecordingBackend()
    gov.consensus_backend = backend
    rec = OperationRecord(
        idempotency_key="op-pr13-backend",
        passage_id="p-backend",
        agent_id="alice",
        action_type="payment",
        amount=10.0,
        resource_id="budget",
        budget_reserved=False,
        budget_committed=False,
        reservation_id="res-owned-by-other",
    )
    assert gov.registry.register(rec)
    assert gov.hold_manager.enter_hold("op-pr13-backend", tau_k_seconds=300)["entered"]
    gov.registry.get("op-pr13-backend").hold_record.hold_start = "2000-01-01T00:00:00+00:00"

    result = gov.expire_hold("op-pr13-backend")

    assert result["expired"] is True
    assert backend.released == []


def test_pr13_expire_hold_rejects_unelapsed_tau_k():
    gov, _state = _setup_governor()
    rec = OperationRecord(
        idempotency_key="op-pr13-premature",
        passage_id="p-premature",
        agent_id="alice",
        action_type="payment",
    )
    assert gov.registry.register(rec)
    assert gov.hold_manager.enter_hold("op-pr13-premature", tau_k_seconds=3600)["entered"]

    result = gov.expire_hold("op-pr13-premature")

    assert result["expired"] is False
    record = gov.registry.get("op-pr13-premature")
    assert record.state.value == "HOLD"
    assert record.hold_record.exit_reason != "expired"
