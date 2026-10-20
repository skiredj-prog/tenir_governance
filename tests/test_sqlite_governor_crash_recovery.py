"""Crash-recovery campaign across the durable registry and SQLite budget.

These tests terminate child processes at the boundaries between the two
durable stores.  Recovery must be idempotent, must not duplicate provider
effects or reservations, and must preserve I8 after both stores are reopened.
"""

from __future__ import annotations

import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from tenir_governance.composition import DualKernelGovernor, ScoringKernelAdapter
from tenir_governance.membrane import MembraneState, TransitionMembrane
from tenir_governance.objects import Resource
from tenir_governance.operations import (
    DurableOperationRegistry,
    MockProvider,
    OperationRecord,
    OperationState,
    ProviderOutcome,
)
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from tenir_kernel.kernel import TenirKernel


_CRASH_EXIT = 86
_AMOUNT = Decimal("10")
_CAPACITY = Decimal("100")


def _governor(store_dir: Path, provider=None):
    state = MembraneState()
    state.register_resource(Resource(resource_id="budget", capacity=float(_CAPACITY)))
    registry = DurableOperationRegistry(str(store_dir / "operations.json"))
    backend = SQLiteConsensusBackend(str(store_dir / "sqlite"))
    assert backend.register_resource("budget", _CAPACITY) in (
        "REGISTERED", "ALREADY_REGISTERED",
    )
    governor = DualKernelGovernor(
        ScoringKernelAdapter(TenirKernel()),
        TransitionMembrane(state),
        registry=registry,
        consensus_backend=backend,
        provider=provider,
    )
    return governor, registry, backend


def _register_created(store_dir: Path, key: str):
    governor, registry, backend = _governor(store_dir)
    assert registry.register(OperationRecord(
        idempotency_key=key,
        passage_id=f"passage-{key}",
        agent_id="alice",
        action_type="payment",
        amount=float(_AMOUNT),
        resource_id="budget",
    ))
    _assert_i8(backend)
    return governor, registry, backend


def _prepare_reserved(store_dir: Path, key: str):
    governor, registry, backend = _register_created(store_dir, key)
    assert governor._reserve_budget(key, "budget", float(_AMOUNT)) is True
    assert registry.transition_to(key, OperationState.RESERVED)[0] is True
    record = registry.get(key)
    assert record is not None
    assert record.budget_reserved is True
    assert record.reservation_id
    _assert_i8(backend, reserved=_AMOUNT, committed=Decimal("0"))
    return governor, registry, backend


def _prepare_unknown_committed(store_dir: Path, key: str):
    _governor_instance, registry, backend = _prepare_reserved(store_dir, key)
    assert registry.transition_to(key, OperationState.DISPATCHING)[0] is True
    record = registry.get(key)
    assert record is not None
    assert backend.commit(record.reservation_id).status == "COMMITTED"
    assert registry.set_budget_flags(
        key,
        budget_reserved=False,
        budget_committed=True,
        reservation_id=record.reservation_id,
    )
    assert registry.transition_to(key, OperationState.UNKNOWN)[0] is True
    _assert_i8(backend, reserved=Decimal("0"), committed=_AMOUNT)
    return registry, backend


def _assert_i8(
    backend: SQLiteConsensusBackend,
    *,
    reserved: Decimal | None = None,
    committed: Decimal | None = None,
):
    state = backend.get_state("budget")
    assert state.reserved >= 0
    assert state.committed >= 0
    assert state.reserved + state.committed <= state.capacity
    if reserved is not None:
        assert state.reserved == reserved
    if committed is not None:
        assert state.committed == committed
    return state


def _run_crash_child(store_dir: Path, scenario: str, key: str):
    result = subprocess.run(
        [sys.executable, __file__, "--crash-child", scenario, str(store_dir), key],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == _CRASH_EXIT, (
        f"scenario {scenario!r} did not reach its crash point: "
        f"returncode={result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r}"
    )


def _provider(outcome: ProviderOutcome):
    return MockProvider(behavior="timeout", reconcile_outcome=outcome)


def test_crash_after_sqlite_reserve_before_registry_ownership_replays_once(tmp_path):
    key = "sqlite-crash-after-reserve"
    _register_created(tmp_path, key)

    _run_crash_child(tmp_path, "reserve_before_registry_flags", key)

    governor, registry, backend = _governor(tmp_path)
    intermediate = registry.get(key)
    assert intermediate is not None
    assert intermediate.budget_reserved is False
    assert intermediate.reservation_id == ""
    state = _assert_i8(backend, reserved=_AMOUNT, committed=Decimal("0"))
    assert len([item for item in state.reservations if item["key"] == key]) == 1

    assert governor._reserve_budget(key, "budget", float(_AMOUNT)) is True
    recovered = registry.get(key)
    assert recovered is not None
    assert recovered.budget_reserved is True
    assert recovered.reservation_id == state.reservations[0]["reservation_id"]
    final_state = _assert_i8(backend, reserved=_AMOUNT, committed=Decimal("0"))
    assert len([item for item in final_state.reservations if item["key"] == key]) == 1


@pytest.mark.parametrize(
    "scenario",
    ["dispatch_claim_before_commit", "commit_before_registry_flags"],
)
def test_dispatch_crash_recovers_to_failed_without_budget_leak(tmp_path, scenario):
    key = f"sqlite-{scenario}"
    _prepare_reserved(tmp_path, key)

    _run_crash_child(tmp_path, scenario, key)

    governor, registry, backend = _governor(
        tmp_path, provider=_provider(ProviderOutcome.FAILED),
    )
    interrupted = registry.get(key)
    assert interrupted is not None
    assert interrupted.state == OperationState.DISPATCHING
    _assert_i8(backend)

    result = governor.reconcile_unknown(key)
    assert result["reconciled"] is True
    recovered = registry.get(key)
    assert recovered is not None
    assert recovered.state == OperationState.FAILED
    assert recovered.budget_reserved is False
    assert recovered.budget_committed is False
    _assert_i8(backend, reserved=Decimal("0"), committed=Decimal("0"))


def test_crash_after_provider_effect_reconciles_to_confirmed_once(tmp_path):
    key = "sqlite-effect-before-response"
    marker = tmp_path / "provider-effect.marker"
    _prepare_reserved(tmp_path, key)

    _run_crash_child(tmp_path, "provider_effect_before_response", key)

    assert marker.read_text(encoding="utf-8") == key
    governor, registry, backend = _governor(
        tmp_path, provider=_provider(ProviderOutcome.CONFIRMED),
    )
    interrupted = registry.get(key)
    assert interrupted is not None
    assert interrupted.state == OperationState.DISPATCHING

    result = governor.reconcile_unknown(key)
    assert result["reconciled"] is True
    recovered = registry.get(key)
    assert recovered is not None
    assert recovered.state == OperationState.CONFIRMED
    assert recovered.budget_reserved is False
    assert recovered.budget_committed is True
    state = _assert_i8(backend, reserved=Decimal("0"), committed=_AMOUNT)
    assert len([item for item in state.reservations if item["key"] == key]) == 1


@pytest.mark.parametrize(
    "scenario, outcome, terminal_state, expected_committed",
    [
        (
            "refund_before_registry_flags",
            ProviderOutcome.FAILED,
            OperationState.FAILED,
            Decimal("0"),
        ),
        (
            "failed_flags_before_terminal",
            ProviderOutcome.FAILED,
            OperationState.FAILED,
            Decimal("0"),
        ),
        (
            "confirmed_flags_before_terminal",
            ProviderOutcome.CONFIRMED,
            OperationState.CONFIRMED,
            _AMOUNT,
        ),
    ],
)
def test_terminal_settlement_crash_replays_idempotently(
    tmp_path, scenario, outcome, terminal_state, expected_committed,
):
    key = f"sqlite-{scenario}"
    _prepare_unknown_committed(tmp_path, key)

    _run_crash_child(tmp_path, scenario, key)

    governor, registry, backend = _governor(tmp_path, provider=_provider(outcome))
    _assert_i8(backend)
    replay = governor.reconcile_unknown(key)
    assert replay["reconciled"] is True

    recovered = registry.get(key)
    assert recovered is not None
    assert recovered.state == terminal_state
    assert recovered.budget_reserved is False
    assert recovered.budget_committed is (terminal_state == OperationState.CONFIRMED)
    state = _assert_i8(
        backend,
        reserved=Decimal("0"),
        committed=expected_committed,
    )
    matching = [item for item in state.reservations if item["key"] == key]
    assert len(matching) == 1
    expected_reservation_state = (
        "COMMITTED" if terminal_state == OperationState.CONFIRMED else "REFUNDED"
    )
    assert matching[0]["state"] == expected_reservation_state


@pytest.mark.parametrize(
    "operation, starting_state, expected_reserved, expected_committed",
    [
        ("commit", "RESERVED", _AMOUNT, Decimal("0")),
        ("release", "RESERVED", _AMOUNT, Decimal("0")),
        ("refund", "COMMITTED", Decimal("0"), _AMOUNT),
        ("reconcile_failed", "RESERVED", _AMOUNT, Decimal("0")),
        ("late_compensation", "RELEASED", Decimal("0"), Decimal("0")),
    ],
)
def test_sqlite_internal_crash_rolls_back_each_mutation(
    tmp_path, operation, starting_state, expected_reserved, expected_committed,
):
    key = f"sqlite-transaction-{operation}"
    backend = SQLiteConsensusBackend(str(tmp_path / "sqlite"))
    assert backend.register_resource("budget", _CAPACITY) == "REGISTERED"
    reservation = backend.reserve(key, _AMOUNT, "budget")
    assert reservation.status == "RESERVED"
    if starting_state == "COMMITTED":
        assert backend.commit(reservation.reservation_id).status == "COMMITTED"
    elif starting_state == "RELEASED":
        assert backend.release(reservation.reservation_id).status == "RELEASED"

    _run_crash_child(tmp_path, f"sqlite_transaction_{operation}", key)

    reopened = SQLiteConsensusBackend(str(tmp_path / "sqlite"))
    state = _assert_i8(
        reopened,
        reserved=expected_reserved,
        committed=expected_committed,
    )
    matching = [item for item in state.reservations if item["key"] == key]
    assert len(matching) == 1
    assert matching[0]["state"] == starting_state
    if operation == "late_compensation":
        assert state.compensations == []


def _child_governor_scenario(scenario: str, store_dir: Path, key: str):
    provider = None
    if scenario in (
        "refund_before_registry_flags",
        "failed_flags_before_terminal",
    ):
        provider = _provider(ProviderOutcome.FAILED)
    elif scenario == "confirmed_flags_before_terminal":
        provider = _provider(ProviderOutcome.CONFIRMED)
    elif scenario == "provider_effect_before_response":
        provider = MockProvider(behavior="confirmed")

        def crash_after_effect(*_args, **_kwargs):
            (store_dir / "provider-effect.marker").write_text(key, encoding="utf-8")
            os._exit(_CRASH_EXIT)

        provider.set_custom_handler(crash_after_effect)

    governor, registry, backend = _governor(store_dir, provider=provider)

    def crash(*_args, **_kwargs):
        os._exit(_CRASH_EXIT)

    if scenario == "reserve_before_registry_flags":
        registry.set_budget_flags = crash
        governor._reserve_budget(key, "budget", float(_AMOUNT))
    elif scenario == "dispatch_claim_before_commit":
        backend.commit = crash
        governor.dispatch(key, "budget", float(_AMOUNT))
    elif scenario == "commit_before_registry_flags":
        registry.set_budget_flags = crash
        governor.dispatch(key, "budget", float(_AMOUNT))
    elif scenario == "provider_effect_before_response":
        governor.dispatch_and_execute(key, "budget", float(_AMOUNT))
    elif scenario == "refund_before_registry_flags":
        registry.set_budget_flags = crash
        governor.reconcile_unknown(key)
    elif scenario in ("failed_flags_before_terminal", "confirmed_flags_before_terminal"):
        terminal = (
            OperationState.FAILED
            if scenario == "failed_flags_before_terminal"
            else OperationState.CONFIRMED
        )
        original_transition = registry.transition_to

        def crash_before_terminal(idempotency_key, target_state, **kwargs):
            if target_state == terminal:
                os._exit(_CRASH_EXIT)
            return original_transition(idempotency_key, target_state, **kwargs)

        registry.transition_to = crash_before_terminal
        governor.reconcile_unknown(key)
    else:
        raise AssertionError(f"unknown governor crash scenario: {scenario}")
    raise AssertionError(f"scenario {scenario!r} did not terminate")


def _child_sqlite_transaction_scenario(scenario: str, store_dir: Path, key: str):
    operation = scenario.removeprefix("sqlite_transaction_")
    provider_outcome = True if operation == "late_compensation" else False
    backend = SQLiteConsensusBackend(
        str(store_dir / "sqlite"),
        provider_reconcile=lambda _key: provider_outcome,
    )
    state = backend.get_state("budget")
    reservation = next(item for item in state.reservations if item["key"] == key)

    def crash_before_commit(active_operation):
        expected = "reconcile" if operation in ("reconcile_failed", "late_compensation") else operation
        if active_operation == expected:
            os._exit(_CRASH_EXIT)

    backend._before_commit = crash_before_commit
    if operation == "commit":
        backend.commit(reservation["reservation_id"])
    elif operation == "release":
        backend.release(reservation["reservation_id"])
    elif operation == "refund":
        backend.refund(reservation["reservation_id"])
    elif operation in ("reconcile_failed", "late_compensation"):
        backend.reconcile(key)
    else:
        raise AssertionError(f"unknown SQLite crash operation: {operation}")
    raise AssertionError(f"scenario {scenario!r} did not terminate")


def _crash_child_main():
    scenario = sys.argv[2]
    store_dir = Path(sys.argv[3])
    key = sys.argv[4]
    if scenario.startswith("sqlite_transaction_"):
        _child_sqlite_transaction_scenario(scenario, store_dir, key)
    else:
        _child_governor_scenario(scenario, store_dir, key)


if __name__ == "__main__" and len(sys.argv) >= 5 and sys.argv[1] == "--crash-child":
    _crash_child_main()
