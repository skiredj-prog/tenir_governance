# Profile: agentic-governance-profile-v1.0.0
# Compliance class: profile-bound
# This module is an extension on top of KB3 and TENIR 2.0 schema 1.2.
# It is NOT canonical, NOT doctrinal. It carries implementation-level
# governance enforcement authority only.
"""Cross-process tests using multiprocessing against a file-backed durable registry.

Per snippet §3: "Test two independent processes. Run competing reservations and
duplicate dispatch attempts against the same durable backend. Threads alone
cannot demonstrate cross-process coordination."

Per review 3 §1: the in-process threading.Lock only coordinates threads in one
process. For cross-process safety, the DurableOperationRegistry's file-backed
store must enforce uniqueness across processes.

These tests spawn independent Python processes that share a file-backed
registry and verify:
  1. Exactly one process successfully registers a given idempotency key.
  2. The other process is blocked from fresh adjudication.
  3. After a restart, the registry state is preserved.
  4. Budget is not double-consumed across processes.
"""
import json
import multiprocessing
import os
import sys
import tempfile
import time

import pytest

# Ensure the package is importable in child processes
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _child_register(store_path, key, result_queue):
    """Child process: try to register an operation with the given key."""
    from tenir_governance.operations import DurableOperationRegistry, OperationRecord, OperationState
    reg = DurableOperationRegistry(store_path=store_path)
    rec = OperationRecord(
        idempotency_key=key,
        passage_id=f"p-{key}",
        agent_id="alice",
        action_type="payment",
        amount=25.0,
        resource_id="budget",
    )
    success = reg.register(rec)
    result_queue.put(("register", key, success))


def _child_adjudicate(store_path, key, result_queue):
    """Child process: create a governor with the shared registry and adjudicate."""
    from tenir_governance import (
        Agent, Passage, Resource,
        TransitionMembrane, MembraneState,
        DualKernelGovernor, DurableOperationRegistry,
    )
    from tenir_governance.composition.governor import ScoringKernelAdapter
    from tenir_kernel.kernel import TenirKernel
    reg = DurableOperationRegistry(store_path=store_path)

    state = MembraneState()
    state.register_resource(Resource(resource_id="budget", capacity=100.0))
    state.register_agent(Agent(agent_id="alice", authority_score=0.9,
                              trusted_delegates=frozenset()))

    gov = DualKernelGovernor(
        ScoringKernelAdapter(TenirKernel()), TransitionMembrane(state),
        registry=reg,
    )

    p = Passage(agent_id="alice", action_type="payment", amount=25.0,
                idempotency_key=key)
    r = gov.adjudicate(
        p, pressure=0.2, velocity=0.3, capacity=1.0,
        authority=0.9, context_fit=0.9, evidence_quality=0.9,
        resource_id="budget", amount=25.0,
    )
    result_queue.put(("adjudicate", key, r["composed_decision"]))


def _child_dispatch(store_path, key, result_queue):
    """Child process: try to dispatch an existing RESERVED operation.

    Always attempts the atomic transition_to(DISPATCHING) directly — this
    is the operation that must be cross-process atomic. The file lock ensures
    only one process succeeds.
    """
    from tenir_governance.operations import DurableOperationRegistry, OperationState
    reg = DurableOperationRegistry(store_path=store_path)
    success, _ = reg.transition_to(key, OperationState.DISPATCHING, claimed_by=f"proc-{os.getpid()}")
    result_queue.put(("dispatch", key, "dispatched" if success else "blocked"))


# Governor-dependent tests require modules not yet built in the kernel repo
# (ScoringKernel, TransitionMembrane, MembraneState, DualKernelGovernor).
try:
    from tenir_governance import (
        Agent, Passage, Resource,
        TransitionMembrane, MembraneState,
        DualKernelGovernor,
    )
    from tenir_governance.composition.governor import ScoringKernelAdapter
    from tenir_kernel.kernel import TenirKernel
    _GOVERNOR_AVAILABLE = True
except ImportError:
    _GOVERNOR_AVAILABLE = False

_SKIP = pytest.mark.skipif(
    not _GOVERNOR_AVAILABLE,
    reason="DualKernelGovernor / membrane / scoring not yet built in the kernel repo",
)


class TestCrossProcessRegistry:
    """Test that the file-backed registry enforces uniqueness across processes."""

    def test_two_processes_register_same_key_one_wins(self, tmp_path):
        """Two independent processes try to register the same idempotency key.
        Exactly one should succeed."""
        store = str(tmp_path / "operations.json")
        key = "op-cross-1"

        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()

        p1 = ctx.Process(target=_child_register, args=(store, key, queue))
        p2 = ctx.Process(target=_child_register, args=(store, key, queue))
        p1.start()
        p2.start()
        p1.join(timeout=10)
        p2.join(timeout=10)

        results = []
        while not queue.empty():
            results.append(queue.get())

        # Collect register results
        register_results = [r[2] for r in results if r[0] == "register"]
        assert len(register_results) == 2, f"Expected 2 results, got {len(register_results)}"
        successes = sum(1 for s in register_results if s)
        assert successes == 1, f"Expected exactly 1 successful registration, got {successes}"

    def test_registry_state_survives_restart_in_new_process(self, tmp_path):
        """An operation registered in one process is visible in a new process."""
        store = str(tmp_path / "operations.json")
        key = "op-cross-2"

        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()

        # Process 1: register
        p1 = ctx.Process(target=_child_register, args=(store, key, queue))
        p1.start()
        p1.join(timeout=10)

        # Process 2: try to register the same key — should fail
        p2 = ctx.Process(target=_child_register, args=(store, key, queue))
        p2.start()
        p2.join(timeout=10)

        results = []
        while not queue.empty():
            results.append(queue.get())

        register_results = [r[2] for r in results if r[0] == "register"]
        assert register_results[0] is True, "First process should succeed"
        assert register_results[1] is False, "Second process should be blocked"

    @_SKIP
    def test_two_processes_adjudicate_same_key_one_pass(self, tmp_path):
        """Two independent processes adjudicate the same idempotency key.
        Exactly one should get PASS, the other HARD_VETO."""
        store = str(tmp_path / "operations.json")
        key = "op-cross-adjudicate-1"

        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()

        p1 = ctx.Process(target=_child_adjudicate, args=(store, key, queue))
        p2 = ctx.Process(target=_child_adjudicate, args=(store, key, queue))
        p1.start()
        p2.start()
        p1.join(timeout=10)
        p2.join(timeout=10)

        results = []
        while not queue.empty():
            results.append(queue.get())

        adjudicate_results = [r[2] for r in results if r[0] == "adjudicate"]
        assert len(adjudicate_results) == 2, f"Expected 2 results, got {len(adjudicate_results)}"

        allowed = sum(1 for d in adjudicate_results if d in ("PASS", "FLAG"))
        vetoes = sum(1 for d in adjudicate_results if d == "HARD_VETO")
        assert allowed == 1, f"Expected 1 allowed (PASS or FLAG), got {allowed}"
        assert vetoes == 1, f"Expected 1 HARD_VETO, got {vetoes}"

    def test_two_processes_dispatch_same_key_one_wins(self, tmp_path):
        """Two independent processes try to dispatch the same RESERVED operation.
        Exactly one should succeed."""
        store = str(tmp_path / "operations.json")
        key = "op-cross-dispatch-1"

        # First: register and transition to RESERVED (in-process)
        from tenir_governance.operations import DurableOperationRegistry, OperationRecord, OperationState
        reg = DurableOperationRegistry(store_path=store)
        rec = OperationRecord(
            idempotency_key=key, passage_id=f"p-{key}",
            agent_id="alice", action_type="payment",
            amount=25.0, resource_id="budget",
        )
        reg.register(rec)
        reg.transition_to(key, OperationState.RESERVED)

        # Now two processes try to dispatch
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()

        p1 = ctx.Process(target=_child_dispatch, args=(store, key, queue))
        p2 = ctx.Process(target=_child_dispatch, args=(store, key, queue))
        p1.start()
        p2.start()
        p1.join(timeout=10)
        p2.join(timeout=10)

        results = []
        while not queue.empty():
            results.append(queue.get())

        dispatch_results = [r[2] for r in results if r[0] == "dispatch"]
        assert len(dispatch_results) == 2, f"Expected 2 results, got {len(dispatch_results)}"

        dispatched = sum(1 for d in dispatch_results if d == "dispatched")
        blocked = sum(1 for d in dispatch_results if d == "blocked")
        assert dispatched == 1, f"Expected 1 dispatched, got {dispatched}"
        assert blocked == 1, f"Expected 1 blocked, got {blocked}"


@_SKIP
class TestCrossProcessBudgetConservation:
    """Per snippet §3: test budget conservation across independent processes.

    Note: the file-backed registry provides cross-process uniqueness for
    idempotency keys, but the in-process budget (Resource with threading.Lock)
    does NOT provide cross-process budget conservation. This test documents
    that limitation. A production deployment would need a database-backed
    BudgetCoordinator for cross-process budget safety.
    """

    def test_cross_process_duplicate_dispatch_blocked(self, tmp_path):
        """Two processes try to dispatch the same operation. The file-backed
        registry prevents duplicate dispatch, even across processes."""
        store = str(tmp_path / "operations.json")
        key = "op-cross-budget-1"

        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()

        # Both processes try to adjudicate and dispatch the same key
        p1 = ctx.Process(target=_child_adjudicate, args=(store, key, queue))
        p2 = ctx.Process(target=_child_adjudicate, args=(store, key, queue))
        p1.start()
        p2.start()
        p1.join(timeout=10)
        p2.join(timeout=10)

        results = []
        while not queue.empty():
            results.append(queue.get())

        adjudicate_results = [r[2] for r in results if r[0] == "adjudicate"]
        allowed = sum(1 for d in adjudicate_results if d in ("PASS", "FLAG"))
        assert allowed == 1, f"Expected 1 allowed (PASS or FLAG) across processes, got {allowed}"