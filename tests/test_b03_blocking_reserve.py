"""B03: emergency quarantine stays live while budget.reserve is suspended."""
from __future__ import annotations

from multiprocessing import get_context
from pathlib import Path
import threading

import pytest

from tenir_governance.composition.governor import DualKernelGovernor
from tenir_governance.composition import ScoringKernelAdapter
from tenir_governance.membrane import MembraneState, TransitionMembrane
from tenir_governance.objects import Passage, Resource
from tenir_governance.operations.registry import DurableOperationRegistry
from tenir_kernel.kernel import TenirKernel


def _child_adjudicate_while_reserve_waits(
    store, reserve_entered, allow_reserve, release_entered, allow_release, queue,
):
    from types import SimpleNamespace

    governor = DualKernelGovernor(
        ScoringKernelAdapter(TenirKernel()),
        TransitionMembrane(MembraneState()),
        registry=DurableOperationRegistry(str(store)),
    )
    governor.membrane.state.register_resource(Resource(resource_id="budget", capacity=100.0))

    class PausedReserveBackend:
        reserve_calls = 0
        release_calls = 0

        def get_state(self, resource_id):
            assert resource_id == "budget"
            return SimpleNamespace(capacity=100.0, committed=0.0, reserved=0.0, reservations=[])

        def reserve(self, idempotency_key, amount, resource_id):
            self.reserve_calls += 1
            reserve_entered.set()
            if not allow_reserve.wait(12):
                raise TimeoutError("test did not release paused reserve")
            return SimpleNamespace(status="RESERVED", reservation_id="b03-reservation")

        def release(self, reservation_id):
            assert reservation_id == "b03-reservation"
            self.release_calls += 1
            release_entered.set()
            if not allow_release.wait(12):
                raise TimeoutError("test did not release paused compensation")
            return SimpleNamespace(status="RELEASED")

    backend = PausedReserveBackend()
    governor.consensus_backend = backend
    try:
        result = governor.adjudicate(
            Passage(
                agent_id="alice", action_type="payment", amount=10.0,
                idempotency_key="b03-emergency-reserve",
            ),
            pressure=0.2, velocity=0.3, capacity=1.0,
            authority=0.9, context_fit=1.0, evidence_quality=1.0,
            resource_id="budget", amount=10.0,
        )
        record = governor.registry.get("b03-emergency-reserve")
        queue.put({
            "result": result,
            "state": record.state.value if record else None,
            "budget_reserved": record.budget_reserved if record else None,
            "reservation_id": record.reservation_id if record else None,
            "reserve_calls": backend.reserve_calls,
            "release_calls": backend.release_calls,
            "quarantine_active": governor.registry.quarantine_is_active(),
        })
    except BaseException as exc:
        queue.put({"error": repr(exc)})


def test_blocked_external_reserve_does_not_delay_emergency_quarantine(tmp_path):
    store = tmp_path / "b03-operations.json"
    registry = DurableOperationRegistry(str(store))
    reserve_entered = get_context("spawn").Event()
    allow_reserve = get_context("spawn").Event()
    release_entered = get_context("spawn").Event()
    allow_release = get_context("spawn").Event()
    queue = get_context("spawn").Queue()
    child = get_context("spawn").Process(
        target=_child_adjudicate_while_reserve_waits,
        args=(str(store), reserve_entered, allow_reserve,
              release_entered, allow_release, queue),
    )
    child.start()
    locker = DualKernelGovernor(
        ScoringKernelAdapter(TenirKernel()),
        TransitionMembrane(MembraneState()),
        registry=registry,
    )
    activated = threading.Event()
    activation_errors = []

    def activate_emergency(event, locked_by, incident_id):
        try:
            locker.lock_quarantine(locked_by, incident_id=incident_id)
        except BaseException as exc:
            activation_errors.append(repr(exc))
        finally:
            event.set()

    emergency = threading.Thread(
        target=activate_emergency,
        args=(activated, "b03-emergency", "b03-incident"),
        daemon=True,
    )
    compensation_emergency_activated = threading.Event()
    compensation_emergency = threading.Thread(
        target=activate_emergency,
        args=(compensation_emergency_activated,
              "b03-emergency-renewal", "b03-incident-renewal"),
        daemon=True,
    )
    activated_while_reserve_waited = False
    child_was_still_waiting = False
    activated_while_release_waited = False
    child_was_still_compensating = False
    try:
        assert reserve_entered.wait(8), "adjudication never entered the external reserve call"
        emergency.start()
        activated_while_reserve_waited = activated.wait(3)
        child_was_still_waiting = child.is_alive()
        allow_reserve.set()
        assert release_entered.wait(8), "aborted reservation was not compensated"
        compensation_emergency.start()
        activated_while_release_waited = compensation_emergency_activated.wait(3)
        child_was_still_compensating = child.is_alive()
    finally:
        allow_reserve.set()
        allow_release.set()
        if emergency.ident is not None:
            emergency.join(8)
        if compensation_emergency.ident is not None:
            compensation_emergency.join(8)
        child.join(15)

    assert activated_while_reserve_waited, (
        "emergency quarantine could not acquire its shared gate while reserve() waited"
    )
    assert child_was_still_waiting, "reserve() unexpectedly returned before the emergency lock completed"
    assert activated_while_release_waited, (
        "a second emergency lock could not acquire its shared gate while release() waited"
    )
    assert child_was_still_compensating, "release() unexpectedly returned before the second emergency lock completed"
    assert not activation_errors, activation_errors
    assert not emergency.is_alive()
    assert not compensation_emergency.is_alive()
    assert child.exitcode == 0
    observed = queue.get(timeout=3)
    assert "error" not in observed, observed
    assert observed["quarantine_active"] is True
    assert observed["result"]["composed_decision"] == "HARD_VETO"
    assert observed["result"]["ledger_valid"] is True
    assert observed["state"] == "failed"
    assert observed["budget_reserved"] is False
    assert observed["reserve_calls"] == 1
    assert observed["release_calls"] == 1
