"""Regression tests for the Wing-Gong linearizability checker."""
from tenir_governance.operations.linearizability_checker import (
    Invocation, LinearizabilityChecker, Operation, Response,
    consensus_sequential_spec,
)


def _op(name, args, client, start, end, result):
    return Operation(
        invocation=Invocation(op=name, args=args, client_id=client, event_id=start),
        response=Response(result=result, client_id=client, event_id=end),
    )


def test_checker_uses_stable_indices_for_real_time_precedence():
    def spec(op, state, args):
        return state or {}, {"status": op}

    # Deliberately store operations in an order different from their real-time
    # dependency: B completed before C started, while A overlaps both.
    a = _op("A", (), 1, 1, 6, {"status": "A"})
    c = _op("C", (), 3, 4, 5, {"status": "C"})
    b = _op("B", (), 2, 2, 3, {"status": "B"})
    result = LinearizabilityChecker(spec).check([a, c, b])

    assert result.linearizable
    order = [operation.op for operation in result.linearization]
    assert order.index("B") < order.index("C")


def test_checker_does_not_leak_mutations_from_failed_search_branch():
    # A's failed response would mutate state if the checker reused a mutable
    # state object across branches. B then A is the valid linearization.
    a = _op(
        "reserve", ("key-A", 70, "budget"), 1, 1, 4,
        {"status": "INSUFFICIENT_BUDGET"},
    )
    b = _op(
        "reserve", ("key-B", 40, "budget"), 2, 2, 3,
        {"status": "RESERVED", "reservation_id": "backend-generated-id"},
    )
    result = LinearizabilityChecker(consensus_sequential_spec).check([a, b])

    assert result.linearizable
    assert [operation.invocation.args[0] for operation in result.linearization] == [
        "key-B", "key-A"
    ]


def test_checker_binds_backend_reservation_id_to_later_commit():
    reserve = _op(
        "reserve", ("key-A", 10, "budget"), 1, 1, 2,
        {"status": "RESERVED", "reservation_id": "backend-reservation-1"},
    )
    commit = _op(
        "commit", ("backend-reservation-1",), 1, 3, 4,
        {"status": "COMMITTED"},
    )
    result = LinearizabilityChecker(consensus_sequential_spec).check([reserve, commit])
    assert result.linearizable


def test_checker_rejects_changed_id_for_idempotent_reservation_replay():
    first = _op(
        "reserve", ("key-A", 10, "budget"), 1, 1, 2,
        {"status": "RESERVED", "reservation_id": "backend-reservation-1"},
    )
    replay = _op(
        "reserve", ("key-A", 10, "budget"), 1, 3, 4,
        {"status": "RESERVED", "reservation_id": "backend-reservation-2"},
    )
    result = LinearizabilityChecker(consensus_sequential_spec).check([first, replay])
    assert not result.linearizable


def test_sequential_spec_rejects_commit_after_release():
    state = {"capacity": 100, "reserved": 0, "consumed": 0, "reservations": {}}
    state, reserved = consensus_sequential_spec(
        "reserve", state, ("key-A", 10, "budget")
    )
    reservation_id = reserved["reservation_id"]
    state, released = consensus_sequential_spec("release", state, (reservation_id,))
    state, committed = consensus_sequential_spec("commit", state, (reservation_id,))

    assert released["status"] == "RELEASED"
    assert committed["status"] == "INVALID_STATE"
    assert state["reserved"] == 0
    assert state["consumed"] == 0
