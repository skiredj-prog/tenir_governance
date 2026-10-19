"""Durable compensation regressions for the single-host budget backend.

These tests pin the recovery contract for a committed reservation that must be
compensated after authoritative provider evidence confirms no external effect.
The reservation journal, not a caller-side boolean, owns idempotency.
"""
from decimal import Decimal

import pytest

from tenir_governance.operations.consensus_backend import SingleHostLockBackend


class SimulatedProcessCrash(BaseException):
    """Model abrupt process termination without ordinary exception cleanup."""


def _committed_reservation(tmp_path):
    backend = SingleHostLockBackend(str(tmp_path))
    backend.register_resource("payments", Decimal("100"))
    reservation = backend.reserve("payment-001", Decimal("10"), "payments")
    assert reservation.status == "RESERVED"
    assert backend.commit(reservation.reservation_id).status == "COMMITTED"
    return backend, reservation.reservation_id


def test_refund_is_durable_and_idempotent(tmp_path):
    backend, reservation_id = _committed_reservation(tmp_path)

    first = backend.refund(reservation_id)
    replay = backend.refund(reservation_id)

    assert first.status == "REFUNDED"
    assert replay.status == "ALREADY_REFUNDED"
    state = backend.get_state("payments")
    assert state.reserved == Decimal("0")
    assert state.committed == Decimal("0")
    reservation = next(
        item for item in state.reservations
        if item["reservation_id"] == reservation_id
    )
    assert reservation["state"] == "REFUNDED"


def test_crash_after_refund_budget_write_recovers_without_double_refund(
    tmp_path, monkeypatch,
):
    backend, reservation_id = _committed_reservation(tmp_path)

    def crash_before_reservation_registry_write(_reservations):
        raise SimulatedProcessCrash("crash after durable budget refund")

    monkeypatch.setattr(
        backend, "_persist_reservations", crash_before_reservation_registry_write,
    )
    with pytest.raises(SimulatedProcessCrash):
        backend.refund(reservation_id)

    reopened = SingleHostLockBackend(str(tmp_path))
    state = reopened.get_state("payments")
    assert state.reserved == Decimal("0")
    assert state.committed == Decimal("0")
    reservation = next(
        item for item in state.reservations
        if item["reservation_id"] == reservation_id
    )
    assert reservation["state"] == "REFUNDED"
    assert reopened.refund(reservation_id).status == "ALREADY_REFUNDED"
    final_state = reopened.get_state("payments")
    assert final_state.reserved == Decimal("0")
    assert final_state.committed == Decimal("0")
