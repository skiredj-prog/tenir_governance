"""Shared quarantine mark across two registries on one durable store."""
import os

from tenir_governance.composition.governor import DualKernelGovernor
from tenir_governance.membrane import TransitionMembrane, MembraneState
from tenir_governance.operations.registry import DurableOperationRegistry, OperationRecord, OperationState
from tenir_kernel.kernel import TenirKernel
from tenir_governance.composition import ScoringKernelAdapter


def _governor(path):
    state = MembraneState()
    scoring = ScoringKernelAdapter(TenirKernel())
    membrane = TransitionMembrane(state)
    registry = DurableOperationRegistry(path)
    return DualKernelGovernor(scoring, membrane, registry=registry)


def test_second_registry_blocks_dispatch_while_quarantine_mark_is_set(tmp_path):
    store = os.path.join(tmp_path, "ops.json")
    owner = _governor(store)
    record = OperationRecord(
        idempotency_key="op-q",
        passage_id="p-q",
        agent_id="alice",
        action_type="payment",
    )
    assert owner.registry.register(record)
    assert owner.registry.transition_to("op-q", OperationState.RESERVED)[0]

    owner.lock_quarantine(locked_by="watch")

    other = _governor(store)
    assert other.membrane.state.quarantine_active is False
    assert other.registry.quarantine_is_active() is True
    blocked = other.dispatch("op-q")
    assert blocked["dispatched"] is False
    assert "QUARANTINE_LOCK" in blocked["reason"]
    assert other.registry.get("op-q").state == OperationState.RESERVED

    released = owner.release_quarantine(
        authorized_by="operator",
        scope="lei",
        authorization="ceremony-G-R5-001",
        reason="clear",
    )
    assert released["released"] is True
    assert other.registry.quarantine_is_active() is False
    allowed = other.dispatch("op-q")
    assert allowed["dispatched"] is True
