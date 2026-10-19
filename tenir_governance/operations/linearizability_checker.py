"""Linearizability checker — Wing-Gong algorithm for concurrent history validation.

Implements a linearizability checker that takes a concurrent history of
invocation/response events and verifies that a valid linearization exists
respecting the real-time precedence order.

This is used by the consensus conformance tests to verify that the
ConsensusBackend adapter maintains linearizability (property LIN in
consensus-contract-v1.md §2.1).

Algorithm: Wing-Gong (1993) — recursive search over possible linearizations
with pruning. For small histories this is tractable. For large histories,
a more efficient algorithm (e.g., PO-Check) would be needed.

Reference:
    Wing & Gong, "A Modular Approach to Monitoring Linearizability"
    (1993).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy
from typing import Any, Callable
import itertools


@dataclass
class Invocation:
    """An invocation event: a client calls an operation."""
    op: str               # operation name: "reserve", "commit", "release", "reconcile", "get_state"
    args: tuple           # arguments to the operation
    client_id: int        # which client/thread invoked this
    event_id: int         # unique event identifier


@dataclass
class Response:
    """A response event: the operation returns a result."""
    result: Any           # the return value
    client_id: int        # which client/thread received this
    event_id: int         # unique event identifier


@dataclass
class Operation:
    """A paired invocation + response."""
    invocation: Invocation
    response: Response | None = None  # None if still pending (never completed)

    @property
    def is_complete(self) -> bool:
        return self.response is not None

    @property
    def op(self) -> str:
        return self.invocation.op


@dataclass
class LinearizabilityResult:
    """Result of a linearizability check."""
    linearizable: bool
    linearization: list = field(default_factory=list)  # ordered list of Operations if linearizable
    reason: str = ""


class LinearizabilityChecker:
    """Wing-Gong linearizability checker for ConsensusBackend histories.

    Usage:
        checker = LinearizabilityChecker(sequential_spec)
        result = checker.check(history)
        if result.linearizable:
            print("History is linearizable")
        else:
            print(f"NOT linearizable: {result.reason}")
    """

    def __init__(self, sequential_spec: Callable):
        """
        Args:
            sequential_spec: a callable(op_name, state, args) -> (new_state, result)
                that implements the sequential specification of the object.
                state is a mutable object that the spec can read and modify.
        """
        self._spec = sequential_spec

    def check(self, operations: list) -> LinearizabilityResult:
        """Check if a concurrent history is linearizable.

        Args:
            operations: list of Operation objects (invocation + response pairs)

        Returns:
            LinearizabilityResult with linearizable=True if a valid
            linearization exists, False otherwise.
        """
        # Only check completed operations
        completed = [op for op in operations if op.is_complete]
        if not completed:
            return LinearizabilityResult(linearizable=True, linearization=[])

        # Build the partial order from real-time precedence:
        # op1 precedes op2 if op1.response.event_id < op2.invocation.event_id
        # (op1 completed before op2 started)
        # AND they are from different clients
        precedence = self._build_precedence(completed)
        # Preserve stable operation identities across recursive removal from
        # the remaining list; list indices are not stable during search.
        self._original_indices = {id(op): i for i, op in enumerate(completed)}

        # Try to find a linearization
        result = self._search([], completed, precedence, None, set(), {})
        return result

    def _build_precedence(self, operations: list) -> dict:
        """Build precedence graph: op_index -> set of op_indices that must precede it."""
        n = len(operations)
        precedence = {i: set() for i in range(n)}

        for i in range(n):
            for j in range(n):
                if i == j:
                    continue
                # op_i precedes op_j if op_i completed before op_j started
                # AND they are from different clients
                if operations[i].response.event_id < operations[j].invocation.event_id:
                    precedence[j].add(i)

        return precedence

    def _search(self, linearized: list, remaining: list,
                precedence: dict, state: Any,
                linearized_indices: set[int],
                reservation_aliases: dict[str, str]) -> LinearizabilityResult:
        """Recursively search for a valid linearization (Wing-Gong)."""
        if not remaining:
            return LinearizabilityResult(
                linearizable=True,
                linearization=linearized[:],
            )

        for i, op in enumerate(remaining):
            op_idx = self._original_indices[id(op)]
            if not precedence.get(op_idx, set()).issubset(linearized_indices):
                continue

            # Each candidate branch gets its own state snapshot. Sequential
            # specs commonly mutate state; a rejected candidate must not leak
            # mutations into sibling search branches.
            working_state = deepcopy(state if state is not None else self._initial_state())
            args = op.invocation.args
            if op.op in {"commit", "release", "reconcile", "reconcile_commit", "reconcile_release"} and args:
                args = (reservation_aliases.get(args[0], args[0]), *args[1:])
            try:
                new_state, expected_result = self._spec(op.op, working_state, args)
            except Exception:
                continue

            # Bind concrete backend reservation IDs to model IDs and enforce
            # one-to-one identity across replays and later commit/release calls.
            branch_aliases = reservation_aliases.copy()
            if not self._results_match(expected_result, op.response.result, branch_aliases):
                continue

            new_remaining = remaining[:i] + remaining[i + 1:]
            result = self._search(
                linearized + [op],
                new_remaining,
                precedence,
                new_state,
                linearized_indices | {op_idx},
                branch_aliases,
            )
            if result.linearizable:
                return result

        return LinearizabilityResult(
            linearizable=False,
            reason="No valid linearization found — history violates linearizability",
        )

    def _initial_state(self):
        """Initial state for the sequential specification."""
        return {"capacity": 100, "reserved": 0, "consumed": 0, "reservations": {}}

    def _results_match(
        self, expected: Any, actual: Any,
        reservation_aliases: dict[str, str] | None = None,
    ) -> bool:
        """Compare all expected result fields and bind opaque reservation IDs."""
        if isinstance(expected, dict) and isinstance(actual, dict):
            for key, value in expected.items():
                if key == "reservation_id":
                    actual_id = actual.get(key)
                    if not isinstance(actual_id, str) or not actual_id:
                        return False
                    if reservation_aliases is not None:
                        existing_model_id = reservation_aliases.get(actual_id)
                        if existing_model_id is not None and existing_model_id != value:
                            return False
                        # One model reservation must map to exactly one concrete ID.
                        if any(model_id == value and concrete_id != actual_id
                               for concrete_id, model_id in reservation_aliases.items()):
                            return False
                        reservation_aliases[actual_id] = value
                    continue
                if key not in actual or actual[key] != value:
                    return False
            return True
        if isinstance(expected, (list, tuple)) and isinstance(actual, (list, tuple)):
            return expected == actual
        return expected == actual

    def _find_orig_idx(self, op, remaining, idx, precedence, linearized):
        """Find the original index of op in the precedence graph."""
        # In this simplified implementation, we use the object identity
        return idx

    def _find_in_linearized(self, linearized, remaining, idx):
        """Check if predecessor at idx is in linearized."""
        # Simplified: in the recursive search, we remove linearized ops from remaining
        # So if idx is not in remaining, it's been linearized
        return idx if idx < len(linearized) else -1


def consensus_sequential_spec(op_name: str, state: dict, args: tuple) -> tuple:
    """Sequential specification for ConsensusBackend operations.

    Args:
        op_name: "reserve", "commit", "release", "reconcile", "get_state"
        state: mutable dict with capacity, reserved, consumed, reservations
        args: operation arguments

    Returns:
        (new_state, result) where result matches the operation's return type
    """
    if op_name == "reserve":
        key, amount, resource_id = args
        # Check idempotency
        for rid, rec in state["reservations"].items():
            if rec["key"] == key:
                if rec["amount"] == amount:
                    return state, {"status": "RESERVED", "reservation_id": rid}
                else:
                    return state, {"status": "DUPLICATE_KEY"}
        # Check budget
        available = state["capacity"] - state["reserved"] - state["consumed"]
        if available < amount:
            return state, {"status": "INSUFFICIENT_BUDGET"}
        # Reserve
        rid = f"res-{len(state['reservations'])}"
        state["reservations"][rid] = {"key": key, "amount": amount, "state": "RESERVED"}
        state["reserved"] += amount
        return state, {"status": "RESERVED", "reservation_id": rid}

    elif op_name == "commit":
        reservation_id = args[0]
        rec = state["reservations"].get(reservation_id)
        if rec is None:
            return state, {"status": "NOT_FOUND"}
        if rec["state"] == "COMMITTED":
            return state, {"status": "ALREADY_COMMITTED"}
        if rec["state"] != "RESERVED":
            return state, {"status": "INVALID_STATE"}
        state["reserved"] -= rec["amount"]
        state["consumed"] += rec["amount"]
        rec["state"] = "COMMITTED"
        return state, {"status": "COMMITTED"}

    elif op_name == "release":
        reservation_id = args[0]
        rec = state["reservations"].get(reservation_id)
        if rec is None:
            return state, {"status": "NOT_FOUND"}
        if rec["state"] == "COMMITTED":
            return state, {"status": "ALREADY_COMMITTED"}
        if rec["state"] != "RESERVED":
            return state, {"status": "INVALID_STATE"}
        state["reserved"] -= rec["amount"]
        rec["state"] = "RELEASED"
        return state, {"status": "RELEASED"}

    elif op_name == "reconcile":
        key = args[0]
        # Find reservation by key
        for rid, rec in state["reservations"].items():
            if rec["key"] == key and rec["state"] in ("UNKNOWN", "RESERVED"):
                # In sequential spec, reconcile always succeeds if provider is available
                # For testing, we assume provider returns True (effect observed)
                state["reserved"] -= rec["amount"]
                state["consumed"] += rec["amount"]
                rec["state"] = "COMMITTED"
                return state, {"status": "CONFIRMED", "effect_observed": True}
        return state, {"status": "NOT_FOUND"}

    elif op_name == "get_state":
        return state, {
            "available": state["capacity"] - state["reserved"] - state["consumed"],
            "reserved": state["reserved"],
            "consumed": state["consumed"],
            "capacity": state["capacity"],
        }

    return state, {"status": "ERROR"}