"""B03: emergency quarantine stays live while budget.reserve is suspended."""
from __future__ import annotations

from multiprocessing import get_context
from pathlib import Path
import os
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


def _persistent_governor(path):
    from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend

    state = MembraneState()
    state.register_resource(Resource(resource_id="budget", capacity=100.0))
    backend = SQLiteConsensusBackend(str(Path(path) / "sqlite"))
    backend.register_resource("budget", 100)
    registry = DurableOperationRegistry(str(Path(path) / "operations.json"))
    governor = DualKernelGovernor(
        ScoringKernelAdapter(TenirKernel()), TransitionMembrane(state),
        registry=registry, consensus_backend=backend,
    )
    return governor, registry, backend


def _veto_during_reserve(governor, backend, key):
    reserve = backend.reserve

    def reserve_then_quarantine(*args, **kwargs):
        result = reserve(*args, **kwargs)
        governor.lock_quarantine("b03-recovery-test", incident_id=key)
        return result

    backend.reserve = reserve_then_quarantine


def _adjudicate_budget(governor, key):
    return governor.adjudicate(
        Passage(agent_id="alice", action_type="payment", amount=10.0, idempotency_key=key),
        pressure=0.2, velocity=0.3, capacity=1.0,
        authority=0.9, context_fit=1.0, evidence_quality=1.0,
        resource_id="budget", amount=10.0,
    )


def _assert_recoverable_veto(path, key):
    governor, registry, backend = _persistent_governor(path)
    record = registry.get(key)
    assert record.state.value == "created", "unsettled veto became irrecoverably terminal"
    assert record.budget_committed is False
    recovery = governor.recover()
    pending = next(item for item in recovery["unresolved"] if item["idempotency_key"] == key)
    assert pending["recommendation"] == "reconcile_budget"
    assert governor.resume_existing(key)["action"] == "reconcile_budget"
    assert governor.dispatch(key)["dispatched"] is False
    assert _adjudicate_budget(governor, key)["composed_decision"] == "HARD_VETO"
    before = backend.get_state("budget")
    assert before.reserved + before.committed <= before.capacity
    assert before.committed == 0
    assert len(before.reservations) == 1
    reservation_id = before.reservations[0]["reservation_id"]

    assert governor.fail(key) is True
    assert governor.fail(key) is False
    settled = registry.get(key)
    assert settled.state.value == "failed"
    assert settled.budget_reserved is False
    assert settled.budget_committed is False
    after = backend.get_state("budget")
    assert after.reserved == 0
    assert after.committed == 0
    assert after.available == after.capacity == 100
    assert len(after.reservations) == 1
    assert after.reservations[0]["reservation_id"] == reservation_id
    assert after.reservations[0]["state"] == "RELEASED"
    assert governor.recover()["unresolved_count"] == 0


@pytest.mark.parametrize("effect_before_error", [False, True])
def test_veto_release_failure_remains_publicly_recoverable(tmp_path, effect_before_error):
    governor, registry, backend = _persistent_governor(tmp_path)
    key = "b03-release-error"
    _veto_during_reserve(governor, backend, key)
    release = backend.release

    def interrupted_release(reservation_id):
        if effect_before_error:
            assert release(reservation_id).status == "RELEASED"
        raise ConnectionError("release acknowledgement lost")

    backend.release = interrupted_release
    result = _adjudicate_budget(governor, key)
    assert result["composed_decision"] == "HARD_VETO"
    assert "uncertain" in result["composed_rationale"]
    assert registry.get(key).budget_reserved is True
    assert backend.get_state("budget").reserved == (0 if effect_before_error else 10)
    _assert_recoverable_veto(tmp_path, key)


def _crash_during_veto_settlement(path, key, boundary):
    governor, registry, backend = _persistent_governor(path)
    _veto_during_reserve(governor, backend, key)
    reserve = backend.reserve
    release = backend.release
    flags = registry.set_budget_flags

    def crash_reserve(*args, **kwargs):
        result = reserve(*args, **kwargs)
        if boundary == "after_reserve":
            os._exit(86)
        return result

    def crash_release(reservation_id):
        if boundary == "before_release":
            os._exit(86)
        result = release(reservation_id)
        if boundary == "after_release":
            os._exit(86)
        return result

    def crash_flags(*args, **kwargs):
        result = flags(*args, **kwargs)
        if boundary == "after_flags" and kwargs.get("budget_reserved") is False:
            os._exit(86)
        return result

    backend.reserve = crash_reserve
    backend.release = crash_release
    registry.set_budget_flags = crash_flags
    _adjudicate_budget(governor, key)
    raise AssertionError("crash injection boundary was not reached")


@pytest.mark.parametrize("boundary", ["after_reserve", "before_release", "after_release", "after_flags"])
def test_veto_compensation_survives_process_crash(tmp_path, boundary):
    key = "b03-crash-veto"
    child = get_context("spawn").Process(
        target=_crash_during_veto_settlement, args=(str(tmp_path), key, boundary),
    )
    child.start()
    try:
        child.join(15)
    finally:
        if child.is_alive():
            child.terminate()
            child.join(5)
    assert child.exitcode == 86, "child did not die at the requested persistence boundary"
    _assert_recoverable_veto(tmp_path, key)


def test_recovery_winning_before_reserve_cannot_allocate_on_terminal_operation(tmp_path):
    governor, registry, backend = _persistent_governor(tmp_path)
    recovery_governor, _, _ = _persistent_governor(tmp_path)
    key = "b03-recovery-wins-before-reserve"
    score = governor.scoring.adjudicate
    reserve = backend.reserve
    reserve_calls = []

    def score_after_recovery(**kwargs):
        assert recovery_governor.fail(key) is True
        assert registry.get(key).state.value == "failed"
        return score(**kwargs)

    def counted_reserve(*args, **kwargs):
        reserve_calls.append(args)
        return reserve(*args, **kwargs)

    governor.scoring.adjudicate = score_after_recovery
    backend.reserve = counted_reserve
    result = _adjudicate_budget(governor, key)
    assert result["composed_decision"] == "HARD_VETO"
    assert reserve_calls == [], "adjudication allocated budget on an already failed operation"
    record = registry.get(key)
    assert record.state.value == "failed"
    assert record.budget_reserved is False
    assert record.budget_committed is False
    state = backend.get_state("budget")
    assert state.reserved == state.committed == 0
    assert state.reservations == []
