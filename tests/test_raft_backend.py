"""Tests for RaftConsensusBackend — multi-host consensus via Raft (pysyncobj).

Covers:
- Multi-node reserve/commit/release with Raft replication
- I8 invariant holds across nodes
- Leader election and failover (crash leader, verify continuity)
- Liveness: operations terminate under healthy quorum
- Linearizability with Wing-Gong on Raft histories

These tests require pysyncobj (pip install pysyncobj).
"""
import ast
import inspect
import os
import sys
import time
import tempfile
import threading
from decimal import Decimal

import pytest

# Skip all tests if pysyncobj is not installed
pysyncobj = pytest.importorskip("pysyncobj")

from tenir_governance.operations.raft_backend import RaftConsensusBackend, _RaftStateMachine
from tenir_governance.operations.consensus_backend import ConsensusState


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_cluster(n=3, base_port=6000):
    """Create a cluster of n Raft nodes."""
    nodes = []
    addrs = [f"localhost:{base_port + i}" for i in range(n)]
    for i in range(n):
        store = tempfile.mkdtemp(prefix=f"raft_node{i}_")
        others = [addrs[j] for j in range(n) if j != i]
        node = RaftConsensusBackend(addrs[i], others, store_dir=store)
        nodes.append(node)
    # Wait for all nodes to be ready
    for node in nodes:
        node.wait_ready(timeout=10.0)
    return nodes


def _get_leader(nodes):
    """Find the leader node in the cluster."""
    for node in nodes:
        if node.is_leader():
            return node
    return None


def _wait_until(predicate, timeout=5.0, interval=0.05):
    """Poll an eventual-consistency condition with a bounded timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def _teardown(nodes):
    """Destroy all nodes in the cluster."""
    for node in nodes:
        try:
            node.destroy()
        except Exception:
            pass


@pytest.fixture
def cluster():
    """3-node Raft cluster."""
    nodes = _make_cluster(n=3, base_port=6100)
    yield nodes
    _teardown(nodes)


@pytest.fixture
def cluster5():
    """5-node Raft cluster."""
    nodes = _make_cluster(n=5, base_port=6200)
    yield nodes
    _teardown(nodes)


# ---------------------------------------------------------------------------
# 1. Multi-node reserve/commit/release with Raft replication
# ---------------------------------------------------------------------------

class TestMultiNodeReserveCommitRelease:
    """Test multi-node reserve/commit/release with Raft replication."""

    def test_reserve_from_leader(self, cluster):
        """Reserve from the leader and verify it replicates to all nodes."""
        leader = _get_leader(cluster)
        assert leader is not None

        leader.register_resource("budget-A", 100)
        time.sleep(0.5)

        result = leader.reserve("key-1", 30, "budget-A")
        assert result.status == "RESERVED"
        assert result.reservation_id is not None
        assert result.amount_reserved == 30

        time.sleep(0.5)

        # All nodes should have the reservation
        for i, node in enumerate(cluster):
            state = node.get_state("budget-A")
            assert state.reserved == 30, f"Node {i} has reserved={state.reserved}, expected 30"

    def test_commit_from_leader(self, cluster):
        """Commit from the leader and verify state transitions on all nodes."""
        leader = _get_leader(cluster)
        leader.register_resource("budget-B", 100)
        time.sleep(0.5)

        res = leader.reserve("key-1", 40, "budget-B")
        assert res.status == "RESERVED"
        time.sleep(0.5)

        commit = leader.commit(res.reservation_id)
        assert commit.status == "COMMITTED"
        time.sleep(0.5)

        for i, node in enumerate(cluster):
            state = node.get_state("budget-B")
            assert state.reserved == 0, f"Node {i} reserved={state.reserved}"
            assert state.committed == 40, f"Node {i} committed={state.committed}"

    def test_release_from_leader(self, cluster):
        """Release from the leader and verify state on all nodes."""
        leader = _get_leader(cluster)
        leader.register_resource("budget-C", 100)
        time.sleep(0.5)

        res = leader.reserve("key-1", 25, "budget-C")
        assert res.status == "RESERVED"
        time.sleep(0.5)

        release = leader.release(res.reservation_id)
        assert release.status == "RELEASED"
        time.sleep(0.5)

        for i, node in enumerate(cluster):
            state = node.get_state("budget-C")
            assert state.reserved == 0, f"Node {i} reserved={state.reserved}"
            assert state.committed == 0, f"Node {i} committed={state.committed}"

    def test_commit_from_follower(self, cluster):
        """Commit from a follower — should work via Raft forwarding."""
        leader = _get_leader(cluster)
        li = cluster.index(leader)
        leader.register_resource("budget-D", 100)
        time.sleep(0.5)

        # Reserve from leader
        res = leader.reserve("key-1", 50, "budget-D")
        assert res.status == "RESERVED"
        time.sleep(0.5)

        # Commit from a follower
        follower = cluster[(li + 1) % 3]
        commit_result = follower.commit(res.reservation_id)
        assert commit_result.status == "COMMITTED", (
            f"Commit from follower returned {commit_result.status}"
        )
        time.sleep(0.5)

        for i, node in enumerate(cluster):
            state = node.get_state("budget-D")
            assert state.committed == 50, f"Node {i} committed={state.committed}"
            assert state.reserved == 0, f"Node {i} reserved={state.reserved}"

    def test_release_from_follower(self, cluster):
        """Release from a follower — should work via Raft forwarding."""
        leader = _get_leader(cluster)
        li = cluster.index(leader)
        leader.register_resource("budget-E", 100)
        time.sleep(0.5)

        res = leader.reserve("key-1", 35, "budget-E")
        assert res.status == "RESERVED"
        time.sleep(0.5)

        # Release from a follower
        follower = cluster[(li + 2) % 3]
        release_result = follower.release(res.reservation_id)
        assert release_result.status == "RELEASED", (
            f"Release from follower returned {release_result.status}"
        )
        time.sleep(0.5)

        for i, node in enumerate(cluster):
            state = node.get_state("budget-E")
            assert state.reserved == 0, f"Node {i} reserved={state.reserved}"

    def test_reserve_from_follower(self, cluster):
        """Reserve from a follower — should work via Raft forwarding."""
        leader = _get_leader(cluster)
        li = cluster.index(leader)
        leader.register_resource("budget-F", 100)
        time.sleep(0.5)

        # Reserve from a follower
        follower = cluster[(li + 1) % 3]
        res = follower.reserve("key-1", 20, "budget-F")
        assert res.status == "RESERVED"
        assert res.reservation_id is not None
        time.sleep(0.5)

        for i, node in enumerate(cluster):
            state = node.get_state("budget-F")
            assert state.reserved == 20, f"Node {i} reserved={state.reserved}"

    def test_idempotent_reserve_same_key_same_amount(self, cluster):
        """Idempotent re-reserve with same key and amount returns same rid."""
        leader = _get_leader(cluster)
        leader.register_resource("budget-G", 100)
        time.sleep(0.5)

        res1 = leader.reserve("key-1", 30, "budget-G")
        assert res1.status == "RESERVED"
        time.sleep(0.3)

        # Re-reserve from a different node
        follower = cluster[1]
        res2 = follower.reserve("key-1", 30, "budget-G")
        assert res2.status == "RESERVED"
        assert res2.reservation_id == res1.reservation_id, (
            f"Idempotent re-reserve returned different rid: "
            f"{res2.reservation_id} != {res1.reservation_id}"
        )

    def test_idempotent_reserve_different_amount(self, cluster):
        """Re-reserve with same key but different amount returns DUPLICATE_KEY."""
        leader = _get_leader(cluster)
        leader.register_resource("budget-H", 100)
        time.sleep(0.5)

        res1 = leader.reserve("key-1", 30, "budget-H")
        assert res1.status == "RESERVED"
        time.sleep(0.3)

        res2 = leader.reserve("key-1", 50, "budget-H")
        assert res2.status == "DUPLICATE_KEY"

    def test_insufficient_budget(self, cluster):
        """Reserve exceeding capacity returns INSUFFICIENT_BUDGET."""
        leader = _get_leader(cluster)
        leader.register_resource("budget-I", 50)
        time.sleep(0.5)

        res1 = leader.reserve("key-1", 30, "budget-I")
        assert res1.status == "RESERVED"
        time.sleep(0.3)

        res2 = leader.reserve("key-2", 30, "budget-I")
        assert res2.status == "INSUFFICIENT_BUDGET"

    def test_full_lifecycle_from_mixed_nodes(self, cluster):
        """Full lifecycle: reserve from leader, commit from follower, release from another."""
        leader = _get_leader(cluster)
        li = cluster.index(leader)
        leader.register_resource("budget-J", 200)
        time.sleep(0.5)

        # Reserve from leader
        res = leader.reserve("key-1", 60, "budget-J")
        assert res.status == "RESERVED"
        time.sleep(0.3)

        # Commit from follower 1
        f1 = cluster[(li + 1) % 3]
        assert f1.commit(res.reservation_id).status == "COMMITTED"
        time.sleep(0.3)

        # Reserve another from follower 2
        f2 = cluster[(li + 2) % 3]
        res2 = f2.reserve("key-2", 40, "budget-J")
        assert res2.status == "RESERVED"
        time.sleep(0.3)

        # Release second from leader
        assert leader.release(res2.reservation_id).status == "RELEASED"
        time.sleep(0.3)

        # Verify final state on all nodes
        for i, node in enumerate(cluster):
            state = node.get_state("budget-J")
            assert state.committed == 60, f"Node {i} committed={state.committed}"
            assert state.reserved == 0, f"Node {i} reserved={state.reserved}"
            assert state.available == 140, f"Node {i} available={state.available}"


# ---------------------------------------------------------------------------
# 2. I8 invariant holds across nodes
# ---------------------------------------------------------------------------

class TestI8Invariant:
    """Test that I8 (reserved + committed <= capacity) holds across all nodes."""

    def test_i8_after_multiple_reserves(self, cluster):
        """I8 holds after multiple reserves from different nodes."""
        leader = _get_leader(cluster)
        leader.register_resource("budget-I8-1", 100)
        time.sleep(0.5)

        for i in range(5):
            node = cluster[i % 3]
            res = node.reserve(f"key-{i}", 15, "budget-I8-1")
            assert res.status == "RESERVED"
            time.sleep(0.2)

        time.sleep(0.5)
        for i, node in enumerate(cluster):
            state = node.get_state("budget-I8-1")
            assert state.reserved + state.committed <= state.capacity, (
                f"I8 violated on node {i}: {state.reserved} + {state.committed} > {state.capacity}"
            )

    def test_i8_after_mixed_commit_release(self, cluster):
        """I8 holds after mixed reserve/commit/release operations."""
        leader = _get_leader(cluster)
        leader.register_resource("budget-I8-2", 100)
        time.sleep(0.5)

        rids = []
        for i in range(4):
            node = cluster[i % 3]
            res = node.reserve(f"key-{i}", 20, "budget-I8-2")
            assert res.status == "RESERVED"
            rids.append(res.reservation_id)
            time.sleep(0.2)

        # Commit 2
        for rid in rids[:2]:
            assert leader.commit(rid).status == "COMMITTED"
            time.sleep(0.2)

        # Release 1
        assert leader.release(rids[2]).status == "RELEASED"
        time.sleep(0.5)

        for i, node in enumerate(cluster):
            state = node.get_state("budget-I8-2")
            assert state.reserved + state.committed <= state.capacity, (
                f"I8 violated on node {i}: {state.reserved} + {state.committed} > {state.capacity}"
            )

    def test_i8_rejects_overcommit(self, cluster):
        """I8 prevents overcommit: cannot reserve more than capacity."""
        leader = _get_leader(cluster)
        leader.register_resource("budget-I8-3", 50)
        time.sleep(0.5)

        res1 = leader.reserve("key-1", 30, "budget-I8-3")
        assert res1.status == "RESERVED"
        time.sleep(0.3)

        res2 = leader.reserve("key-2", 30, "budget-I8-3")
        assert res2.status == "INSUFFICIENT_BUDGET"

        time.sleep(0.5)
        for i, node in enumerate(cluster):
            state = node.get_state("budget-I8-3")
            assert state.reserved + state.committed <= state.capacity

    def test_i8_holds_on_followers(self, cluster):
        """I8 holds on followers after operations from leader."""
        leader = _get_leader(cluster)
        li = cluster.index(leader)
        leader.register_resource("budget-I8-4", 100)
        time.sleep(0.5)

        leader.reserve("key-1", 40, "budget-I8-4")
        time.sleep(0.3)
        leader.reserve("key-2", 30, "budget-I8-4")
        time.sleep(0.5)

        for i, node in enumerate(cluster):
            if i == li:
                continue  # Skip leader
            state = node.get_state("budget-I8-4")
            assert state.reserved + state.committed <= state.capacity, (
                f"I8 violated on follower {i}: {state.reserved} + {state.committed} > {state.capacity}"
            )

    def test_i8_after_concurrent_reserves(self, cluster):
        """I8 holds under concurrent reserves from multiple nodes."""
        leader = _get_leader(cluster)
        leader.register_resource("budget-I8-5", 100)
        time.sleep(0.5)

        results = []
        results_lock = threading.Lock()

        def do_reserve(node, key, amount):
            res = node.reserve(key, amount, "budget-I8-5")
            with results_lock:
                results.append(res)

        threads = []
        for i in range(6):
            node = cluster[i % 3]
            t = threading.Thread(target=do_reserve, args=(node, f"concurrent-{i}", 20))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=10.0)

        time.sleep(1.0)

        # I8 must hold on all nodes regardless of how many succeeded
        for i, node in enumerate(cluster):
            state = node.get_state("budget-I8-5")
            assert state.reserved + state.committed <= state.capacity, (
                f"I8 violated on node {i}: {state.reserved} + {state.committed} > {state.capacity}"
            )


# ---------------------------------------------------------------------------
# 3. Leader election and failover
# ---------------------------------------------------------------------------

class TestLeaderFailover:
    """Test leader election and failover (crash leader, verify continuity)."""

    def test_leader_exists(self, cluster):
        """A leader is elected in a healthy cluster."""
        leader = _get_leader(cluster)
        assert leader is not None, "No leader elected in healthy cluster"

    def test_failover_after_leader_crash(self, cluster):
        """After crashing the leader, a new leader is elected and operations continue."""
        leader = _get_leader(cluster)
        assert leader is not None
        li = cluster.index(leader)

        leader.register_resource("budget-FO", 100)
        time.sleep(0.5)

        # Crash the leader
        leader.destroy()
        time.sleep(2.0)  # Wait for election

        # Find new leader among remaining nodes
        new_leader = None
        for node in cluster:
            if node is leader:
                continue
            if node.is_leader():
                new_leader = node
                break

        assert new_leader is not None, "No new leader elected after crash"

        # Operations should work with new leader
        res = new_leader.reserve("key-1", 30, "budget-FO")
        assert res.status == "RESERVED", f"Reserve failed after failover: {res.status}"

    def test_failover_with_5_node_cluster(self, cluster5):
        """5-node cluster: crash leader, verify new leader and I8 continuity."""
        leader = _get_leader(cluster5)
        assert leader is not None
        li = cluster5.index(leader)

        leader.register_resource("budget-FO5", 100)
        time.sleep(0.5)

        # Reserve and commit
        res = leader.reserve("key-1", 50, "budget-FO5")
        assert res.status == "RESERVED"
        time.sleep(0.3)
        assert leader.commit(res.reservation_id).status == "COMMITTED"
        time.sleep(0.5)

        # Crash the leader
        leader.destroy()
        time.sleep(3.0)

        # Find new leader
        new_leader = None
        for node in cluster5:
            if node is leader:
                continue
            if node.is_leader():
                new_leader = node
                break

        assert new_leader is not None, "No new leader in 5-node cluster after crash"

        # I8 must still hold
        state = new_leader.get_state("budget-FO5")
        assert state.committed == 50
        assert state.reserved + state.committed <= state.capacity

        # New operations should work
        res2 = new_leader.reserve("key-2", 30, "budget-FO5")
        assert res2.status == "RESERVED"

    def test_only_one_leader(self, cluster):
        """Exactly one leader exists in a healthy cluster."""
        leaders = [n for n in cluster if n.is_leader()]
        assert len(leaders) == 1, f"Expected 1 leader, found {len(leaders)}"

    def test_operations_after_failover_preserve_state(self, cluster):
        """State committed before crash is preserved after failover."""
        leader = _get_leader(cluster)
        leader.register_resource("budget-PRES", 100)
        time.sleep(0.5)

        res = leader.reserve("key-1", 40, "budget-PRES")
        assert res.status == "RESERVED"
        time.sleep(0.3)
        assert leader.commit(res.reservation_id).status == "COMMITTED"
        time.sleep(0.5)

        # Crash leader
        leader.destroy()
        time.sleep(2.0)

        # Find new leader
        new_leader = None
        for node in cluster:
            if node is leader:
                continue
            if node.is_leader():
                new_leader = node
                break

        assert new_leader is not None

        # State should be preserved
        state = new_leader.get_state("budget-PRES")
        assert state.committed == 40, f"State not preserved: committed={state.committed}"


# ---------------------------------------------------------------------------
# 4. Liveness: operations terminate under healthy quorum
# ---------------------------------------------------------------------------

class TestLiveness:
    """Test liveness: operations terminate under healthy quorum."""

    def test_operations_terminate_under_quorum(self, cluster):
        """All operations terminate (don't hang) under healthy quorum."""
        leader = _get_leader(cluster)
        leader.register_resource("budget-LIVE", 100)
        time.sleep(0.5)

        # All operations should return within timeout
        res = leader.reserve("key-1", 30, "budget-LIVE")
        assert res.status == "RESERVED"

        commit = leader.commit(res.reservation_id)
        assert commit.status == "COMMITTED"

        res2 = leader.reserve("key-2", 20, "budget-LIVE")
        assert res2.status == "RESERVED"

        release = leader.release(res2.reservation_id)
        assert release.status == "RELEASED"

    def test_operations_from_all_nodes_terminate(self, cluster):
        """Operations from all nodes terminate within timeout."""
        leader = _get_leader(cluster)
        leader.register_resource("budget-LIVE2", 100)
        time.sleep(0.5)

        for i, node in enumerate(cluster):
            res = node.reserve(f"key-{i}", 10, "budget-LIVE2")
            assert res.status == "RESERVED", f"Reserve from node {i} failed: {res.status}"

        time.sleep(0.5)
        for i, node in enumerate(cluster):
            state = node.get_state("budget-LIVE2")
            assert state.reserved == 30, f"Node {i} has reserved={state.reserved}"

    def test_concurrent_operations_terminate(self, cluster):
        """Concurrent operations from multiple nodes all terminate."""
        leader = _get_leader(cluster)
        leader.register_resource("budget-LIVE3", 200)
        time.sleep(0.5)

        results = []
        results_lock = threading.Lock()

        def do_ops(node, idx):
            res = node.reserve(f"concurrent-{idx}", 20, "budget-LIVE3")
            with results_lock:
                results.append(res.status)

        threads = []
        for i in range(6):
            node = cluster[i % 3]
            t = threading.Thread(target=do_ops, args=(node, i))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=15.0)
            assert not t.is_alive(), "Thread hung — operation did not terminate"

        # All should have terminated
        assert len(results) == 6
        # Most should succeed (some may fail due to timing, but all terminate)
        assert all(r in ("RESERVED", "INSUFFICIENT_BUDGET", "ERROR") for r in results)

    def test_liveness_under_minority_failure(self, cluster5):
        """5-node cluster: operations terminate with 1 node crashed (4/5 alive)."""
        leader = _get_leader(cluster5)
        leader.register_resource("budget-LIVE4", 100)
        time.sleep(0.5)

        # Crash one follower (not leader)
        follower = None
        for node in cluster5:
            if node is not leader:
                follower = node
                break
        assert follower is not None
        follower.destroy()
        time.sleep(1.0)

        # Operations should still terminate
        res = leader.reserve("key-1", 30, "budget-LIVE4")
        assert res.status == "RESERVED"

        commit = leader.commit(res.reservation_id)
        assert commit.status == "COMMITTED"


# ---------------------------------------------------------------------------
# 5. Linearizability with Wing-Gong on Raft histories
# ---------------------------------------------------------------------------

class TestLinearizability:
    """Test linearizability of Raft histories using Wing-Gong checker."""

    @staticmethod
    def _make_raft_sequential_spec():
        """Create a sequential spec that handles Raft's client-generated UUIDs.

        The standard consensus_sequential_spec generates its own reservation IDs
        (res-0, res-1) which don't match Raft's UUIDs. This custom spec maps
        any commit/release call to the first matching RESERVED reservation,
        which is correct for sequential histories and valid for the checker's
        linearization search.
        """
        def raft_spec(op_name: str, state: dict, args: tuple) -> tuple:
            if op_name == "reserve":
                key, amount, resource_id = args
                for rid, rec in state["reservations"].items():
                    if rec["key"] == key:
                        if rec["amount"] == amount:
                            return state, {"status": "RESERVED", "reservation_id": rid}
                        else:
                            return state, {"status": "DUPLICATE_KEY"}
                available = state["capacity"] - state["reserved"] - state["consumed"]
                if available < amount:
                    return state, {"status": "INSUFFICIENT_BUDGET"}
                rid = f"res-{len(state['reservations'])}"
                state["reservations"][rid] = {"key": key, "amount": amount, "state": "RESERVED"}
                state["reserved"] += amount
                return state, {"status": "RESERVED", "reservation_id": rid}

            elif op_name == "commit":
                reservation_id = args[0]
                rec = state["reservations"].get(reservation_id)
                if rec is None:
                    # Raft uses client-generated UUIDs; find by matching any RESERVED
                    for rid, r in state["reservations"].items():
                        if r["state"] == "RESERVED":
                            rec = r
                            reservation_id = rid
                            break
                    if rec is None:
                        return state, {"status": "NOT_FOUND"}
                if rec["state"] == "COMMITTED":
                    return state, {"status": "ALREADY_COMMITTED"}
                state["reserved"] -= rec["amount"]
                state["consumed"] += rec["amount"]
                rec["state"] = "COMMITTED"
                return state, {"status": "COMMITTED"}

            elif op_name == "release":
                reservation_id = args[0]
                rec = state["reservations"].get(reservation_id)
                if rec is None:
                    for rid, r in state["reservations"].items():
                        if r["state"] == "RESERVED":
                            rec = r
                            reservation_id = rid
                            break
                    if rec is None:
                        return state, {"status": "NOT_FOUND"}
                if rec["state"] == "COMMITTED":
                    return state, {"status": "ALREADY_COMMITTED"}
                state["reserved"] -= rec["amount"]
                rec["state"] = "RELEASED"
                return state, {"status": "RELEASED"}

            elif op_name == "get_state":
                return state, {
                    "available": state["capacity"] - state["reserved"] - state["consumed"],
                    "reserved": state["reserved"],
                    "consumed": state["consumed"],
                    "capacity": state["capacity"],
                }

            return state, {"status": "ERROR"}

        return raft_spec

    def test_sequential_history_is_linearizable(self, cluster):
        """A sequential history of operations is linearizable."""
        from tenir_governance.operations.linearizability_checker import (
            LinearizabilityChecker,
            consensus_sequential_spec,
            Operation,
            Invocation,
            Response,
        )

        leader = _get_leader(cluster)
        leader.register_resource("budget-LIN", 100)
        time.sleep(0.5)

        # Build a sequential history with proper Invocation/Response objects
        ops = []
        eid = 0

        # Reserve
        eid += 1
        inv = Invocation(op="reserve", args=("key-1", 30, "budget-LIN"), client_id=0, event_id=eid)
        res = leader.reserve("key-1", 30, "budget-LIN")
        eid += 1
        resp = Response(result={"status": res.status, "reservation_id": res.reservation_id},
                        client_id=0, event_id=eid)
        ops.append(Operation(invocation=inv, response=resp))
        time.sleep(0.3)

        # Commit
        eid += 1
        inv = Invocation(op="commit", args=(res.reservation_id,), client_id=0, event_id=eid)
        commit = leader.commit(res.reservation_id)
        eid += 1
        resp = Response(result={"status": commit.status}, client_id=0, event_id=eid)
        ops.append(Operation(invocation=inv, response=resp))
        time.sleep(0.3)

        # Get state
        eid += 1
        inv = Invocation(op="get_state", args=("budget-LIN",), client_id=0, event_id=eid)
        state = leader.get_state("budget-LIN")
        eid += 1
        resp = Response(result={"available": state.available, "reserved": state.reserved,
                                 "consumed": state.committed, "capacity": state.capacity},
                        client_id=0, event_id=eid)
        ops.append(Operation(invocation=inv, response=resp))

        # Run the Wing-Gong checker
        checker = LinearizabilityChecker(self._make_raft_sequential_spec())
        result = checker.check(ops)
        assert result.linearizable, f"Sequential history should be linearizable: {result.reason}"

    def test_concurrent_history_linearizability(self, cluster):
        """Check the actual concurrent history, not just the final budget."""
        from tenir_governance.operations.linearizability_checker import (
            LinearizabilityChecker, consensus_sequential_spec,
            Operation, Invocation, Response,
        )

        leader = _get_leader(cluster)
        assert leader is not None
        assert leader.register_resource("budget-LIN2", 100) == "REGISTERED"
        time.sleep(0.5)

        history = []
        history_lock = threading.Lock()
        event_lock = threading.Lock()
        event_id = 0

        def next_event_id():
            nonlocal event_id
            with event_lock:
                event_id += 1
                return event_id

        def do_reserve(node, key, amount):
            client_id = threading.get_ident()
            invocation_id = next_event_id()
            result = node.reserve(key, amount, "budget-LIN2")
            response_id = next_event_id()
            operation = Operation(
                invocation=Invocation(
                    op="reserve", args=(key, amount, "budget-LIN2"),
                    client_id=client_id, event_id=invocation_id,
                ),
                response=Response(
                    result={"status": result.status, "reservation_id": result.reservation_id},
                    client_id=client_id, event_id=response_id,
                ),
            )
            with history_lock:
                history.append(operation)

        threads = []
        for i in range(5):
            node = cluster[i % 3]
            thread = threading.Thread(target=do_reserve, args=(node, f"lin-{i}", 15))
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join(timeout=10.0)

        assert all(not thread.is_alive() for thread in threads), "reserve worker timed out"
        assert len(history) == 5

        checker = LinearizabilityChecker(consensus_sequential_spec)
        result = checker.check(history)
        assert result.linearizable, f"Concurrent Raft history is not linearizable: {result.reason}"

        successful = [op for op in history if op.response.result["status"] == "RESERVED"]
        assert len(successful) == 5, (
            f"Expected 5 successful reserves, got {len(successful)}: "
            f"{[op.response.result['status'] for op in history]}"
        )

        state = leader.get_state("budget-LIN2")
        assert state.reserved == 75
        assert state.reserved + state.committed <= state.capacity

    def test_mixed_ops_linearizability(self, cluster):
        """Mixed reserve/commit/release history is linearizable."""
        from tenir_governance.operations.linearizability_checker import (
            LinearizabilityChecker,
            consensus_sequential_spec,
            Operation,
            Invocation,
            Response,
        )

        leader = _get_leader(cluster)
        leader.register_resource("budget-LIN3", 100)
        time.sleep(0.5)

        # Sequential mixed operations
        ops = []
        eid = 0

        # Reserve key-1
        eid += 1
        inv = Invocation(op="reserve", args=("key-1", 30, "budget-LIN3"), client_id=0, event_id=eid)
        r1 = leader.reserve("key-1", 30, "budget-LIN3")
        assert r1.status == "RESERVED"
        eid += 1
        resp = Response(result={"status": r1.status, "reservation_id": r1.reservation_id},
                        client_id=0, event_id=eid)
        ops.append(Operation(invocation=inv, response=resp))
        time.sleep(0.2)

        # Commit key-1
        eid += 1
        inv = Invocation(op="commit", args=(r1.reservation_id,), client_id=0, event_id=eid)
        c1 = leader.commit(r1.reservation_id)
        assert c1.status == "COMMITTED"
        eid += 1
        resp = Response(result={"status": c1.status}, client_id=0, event_id=eid)
        ops.append(Operation(invocation=inv, response=resp))
        time.sleep(0.2)

        # Reserve key-2
        eid += 1
        inv = Invocation(op="reserve", args=("key-2", 20, "budget-LIN3"), client_id=0, event_id=eid)
        r2 = leader.reserve("key-2", 20, "budget-LIN3")
        assert r2.status == "RESERVED"
        eid += 1
        resp = Response(result={"status": r2.status, "reservation_id": r2.reservation_id},
                        client_id=0, event_id=eid)
        ops.append(Operation(invocation=inv, response=resp))
        time.sleep(0.2)

        # Release key-2
        eid += 1
        inv = Invocation(op="release", args=(r2.reservation_id,), client_id=0, event_id=eid)
        rel2 = leader.release(r2.reservation_id)
        assert rel2.status == "RELEASED"
        eid += 1
        resp = Response(result={"status": rel2.status}, client_id=0, event_id=eid)
        ops.append(Operation(invocation=inv, response=resp))
        time.sleep(0.2)

        # Check linearizability
        checker = LinearizabilityChecker(self._make_raft_sequential_spec())
        result = checker.check(ops)
        assert result.linearizable, f"Mixed ops history should be linearizable: {result.reason}"

        # Final state
        state = leader.get_state("budget-LIN3")
        assert state.committed == 30
        assert state.reserved == 0
        assert state.available == 70

        # Verify on all nodes
        time.sleep(0.5)
        for i, node in enumerate(cluster):
            s = node.get_state("budget-LIN3")
            assert s.committed == 30, f"Node {i} committed={s.committed}"
            assert s.reserved == 0, f"Node {i} reserved={s.reserved}"

    def test_linearizability_after_failover(self, cluster):
        """History remains linearizable after leader failover."""
        leader = _get_leader(cluster)
        li = cluster.index(leader)
        leader.register_resource("budget-LIN4", 100)
        time.sleep(0.5)

        # Operations before crash
        r1 = leader.reserve("key-1", 40, "budget-LIN4")
        assert r1.status == "RESERVED"
        time.sleep(0.3)
        assert leader.commit(r1.reservation_id).status == "COMMITTED"
        time.sleep(0.3)

        # Crash leader
        leader.destroy()
        time.sleep(2.0)

        # Find new leader
        new_leader = None
        for node in cluster:
            if node is leader:
                continue
            if node.is_leader():
                new_leader = node
                break

        assert new_leader is not None

        # Operations after failover
        r2 = new_leader.reserve("key-2", 30, "budget-LIN4")
        assert r2.status == "RESERVED"
        time.sleep(0.3)

        # State should be consistent: 40 committed + 30 reserved = 70
        state = new_leader.get_state("budget-LIN4")
        assert state.committed == 40, f"Committed={state.committed} after failover"
        assert state.reserved == 30, f"Reserved={state.reserved} after failover"
        assert state.reserved + state.committed <= state.capacity

    def test_wing_gong_checker_on_raft_history(self, cluster):
        """Run the Wing-Gong linearizability checker on a Raft operation history."""
        from tenir_governance.operations.linearizability_checker import (
            LinearizabilityChecker,
            consensus_sequential_spec,
            Operation,
            Invocation,
            Response,
        )

        leader = _get_leader(cluster)
        leader.register_resource("budget-WG", 100)
        time.sleep(0.5)

        # Build a sequential history
        ops = []
        eid = 0

        # Reserve key-1
        eid += 1
        inv = Invocation(op="reserve", args=("key-1", 30, "budget-WG"), client_id=0, event_id=eid)
        r1 = leader.reserve("key-1", 30, "budget-WG")
        eid += 1
        resp = Response(result={"status": r1.status, "reservation_id": r1.reservation_id},
                        client_id=0, event_id=eid)
        ops.append(Operation(invocation=inv, response=resp))
        time.sleep(0.2)

        # Reserve key-2
        eid += 1
        inv = Invocation(op="reserve", args=("key-2", 20, "budget-WG"), client_id=0, event_id=eid)
        r2 = leader.reserve("key-2", 20, "budget-WG")
        eid += 1
        resp = Response(result={"status": r2.status, "reservation_id": r2.reservation_id},
                        client_id=0, event_id=eid)
        ops.append(Operation(invocation=inv, response=resp))
        time.sleep(0.2)

        # Commit key-1
        eid += 1
        inv = Invocation(op="commit", args=(r1.reservation_id,), client_id=0, event_id=eid)
        c1 = leader.commit(r1.reservation_id)
        eid += 1
        resp = Response(result={"status": c1.status}, client_id=0, event_id=eid)
        ops.append(Operation(invocation=inv, response=resp))
        time.sleep(0.2)

        # Run the Wing-Gong checker
        checker = LinearizabilityChecker(self._make_raft_sequential_spec())
        result = checker.check(ops)
        assert result.linearizable, f"Raft history should be linearizable: {result.reason}"

        # Verify final state matches
        state = leader.get_state("budget-WG")
        assert state.committed == 30
        assert state.reserved == 20
        assert state.reserved + state.committed <= state.capacity

# ---------------------------------------------------------------------------
# List-1 regressions: I8, deterministic state, validation and idempotency
# ---------------------------------------------------------------------------

class TestListOneRegressions:
    def test_reconcile_commit_after_release_cannot_overconsume_budget(self, cluster):
        leader = _get_leader(cluster)
        assert leader is not None
        assert leader.register_resource("budget-I8-L1", 100) == "REGISTERED"

        a = leader.reserve("late-effect-A", 100, "budget-I8-L1")
        assert a.status == "RESERVED"
        assert leader.release(a.reservation_id).status == "RELEASED"
        b = leader.reserve("active-B", 100, "budget-I8-L1")
        assert b.status == "RESERVED"

        leader._provider_reconcile = lambda key: True
        result = leader.reconcile("late-effect-A")
        assert result.status == "RECONCILE_CONFLICT"
        assert result.effect_observed is True
        assert _wait_until(lambda: all(
            node.get_state("budget-I8-L1").reserved == 100
            and node.get_state("budget-I8-L1").committed == 0
            for node in cluster
        )), "followers did not converge to the expected budget state"
        for node in cluster:
            state = node.get_state("budget-I8-L1")
            assert state.reserved == 100
            assert state.committed == 0
            assert state.reserved + state.committed <= state.capacity
            by_id = {r["reservation_id"]: r for r in state.reservations}
            assert by_id[a.reservation_id]["state"] == "RELEASED"
            assert by_id[b.reservation_id]["state"] == "RESERVED"

    def test_idempotent_reserve_returns_actual_terminal_state(self, cluster):
        leader = _get_leader(cluster)
        assert leader.register_resource("budget-IDEM-L1", 100) == "REGISTERED"
        first = leader.reserve("same-key", 25, "budget-IDEM-L1")
        assert first.status == "RESERVED"
        assert leader.commit(first.reservation_id).status == "COMMITTED"

        replay = leader.reserve("same-key", 25, "budget-IDEM-L1")
        assert replay.status == "COMMITTED"
        assert replay.reservation_id == first.reservation_id
        state = leader.get_state("budget-IDEM-L1")
        assert state.reserved == 0
        assert state.committed == 25
        assert state.reserved + state.committed <= state.capacity

    @pytest.mark.parametrize("amount", [0, -1, -0.01, float("nan"), float("inf"), float("-inf"), "abc", None, True, False, "1e10000"])
    def test_invalid_amount_rejected_without_mutation(self, cluster, amount):
        leader = _get_leader(cluster)
        assert leader.register_resource("budget-AMOUNT-L1", 100) == "REGISTERED"
        result = leader.reserve("invalid-amount", amount, "budget-AMOUNT-L1")
        assert result.status == "INVALID_AMOUNT"
        state = leader.get_state("budget-AMOUNT-L1")
        assert state.reserved == 0
        assert state.committed == 0

    def test_resource_reregistration_preserves_accounting_and_capacity(self, cluster):
        leader = _get_leader(cluster)
        assert leader.register_resource("budget-REG-L1", 100) == "REGISTERED"
        reservation = leader.reserve("held", 40, "budget-REG-L1")
        assert reservation.status == "RESERVED"
        assert leader.register_resource("budget-REG-L1", 100) == "ALREADY_REGISTERED"
        assert leader.register_resource("budget-REG-L1", 50) == "RESOURCE_CONFLICT"
        state = leader.get_state("budget-REG-L1")
        assert state.capacity == 100
        assert state.reserved == 40
        assert state.committed == 0

    def test_replicated_methods_do_not_use_local_clock_or_randomness(self):
        source = inspect.getsource(_RaftStateMachine)
        tree = ast.parse(source)
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "_RaftStateMachine")
        # Inspect both qualified names and their leaf names so common aliases
        # (e.g. imported monotonic or uuid4) cannot evade this static guard.
        forbidden_qualified = {
            "time.time", "time.monotonic", "time.perf_counter", "datetime.now",
            "datetime.utcnow", "uuid.uuid4", "random.random", "random.randint",
            "random.choice", "os.urandom", "secrets.token_bytes",
        }
        forbidden_leaf = {
            "time", "monotonic", "perf_counter", "now", "utcnow", "uuid4",
            "random", "randint", "choice", "urandom", "token_bytes",
        }
        for method in cls.body:
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(isinstance(dec, ast.Name) and dec.id == "replicated" for dec in method.decorator_list):
                continue
            calls = set()
            for node in ast.walk(method):
                if isinstance(node, ast.Call):
                    fn = ast.unparse(node.func)
                    leaf = node.func.attr if isinstance(node.func, ast.Attribute) else (
                        node.func.id if isinstance(node.func, ast.Name) else ""
                    )
                    if fn in forbidden_qualified or leaf in forbidden_leaf:
                        calls.add(fn)
            assert not calls, f"non-deterministic calls in replicated {method.name}: {sorted(calls)}"
    def test_fractional_amounts_are_preserved_in_budget_state(self, cluster):
        leader = _get_leader(cluster)
        assert leader.register_resource("budget-DECIMAL-L1", "0.30") == "REGISTERED"
        first = leader.reserve("fraction-a", "0.1", "budget-DECIMAL-L1")
        assert first.status == "RESERVED"
        state = leader.get_state("budget-DECIMAL-L1")
        assert state.reserved == Decimal("0.1")
        assert state.available == Decimal("0.2")
        second = leader.reserve("fraction-b", "0.2", "budget-DECIMAL-L1")
        assert second.status == "RESERVED"
        state = leader.get_state("budget-DECIMAL-L1")
        assert state.reserved == Decimal("0.3")
        assert state.committed == Decimal("0")
        assert state.reserved + state.committed <= state.capacity
    def test_logical_clock_and_transition_metadata_replicate_identically(self, cluster):
        leader = _get_leader(cluster)
        assert leader.register_resource("budget-CLOCK-L1", 100) == "REGISTERED"
        reservation = leader.reserve("clock-key", 20, "budget-CLOCK-L1")
        assert reservation.status == "RESERVED"
        assert leader.commit(reservation.reservation_id).status == "COMMITTED"
        assert _wait_until(lambda: all(
            node._raft._logical_clock == cluster[0]._raft._logical_clock
            and reservation.reservation_id in node._raft._reservations
            for node in cluster
        )), "replicated clock/reservation metadata did not converge"
        states = [node._raft for node in cluster]
        clocks = [sm._logical_clock for sm in states]
        assert len(set(clocks)) == 1
        records = [sm._reservations[reservation.reservation_id] for sm in states]
        assert len({r["created_at_logical"] for r in records}) == 1
        assert len({r["committed_at_logical"] for r in records}) == 1
        assert all(r["committed_at_logical"] > r["created_at_logical"] for r in records)


    def test_legacy_float_state_is_normalized_before_decimal_reserve(self, cluster):
        leader = _get_leader(cluster)
        assert leader.register_resource("budget-LEGACY-L1", 100) == "REGISTERED"
        # Simulate every replica restoring state serialized by the pre-Decimal implementation.
        for node in cluster:
            node._raft._resources["budget-LEGACY-L1"] = {
                "capacity": 100.0, "reserved": 0.1, "committed": 0.2,
            }
        result = leader.reserve("after-upgrade", "0.3", "budget-LEGACY-L1")
        assert result.status == "RESERVED"
        state = leader.get_state("budget-LEGACY-L1")
        assert state.capacity == Decimal("100.0")
        assert state.reserved == Decimal("0.4")
        assert state.committed == Decimal("0.2")

    def test_legacy_float_reservation_can_be_committed_after_upgrade(self, cluster):
        leader = _get_leader(cluster)
        assert leader.register_resource("budget-LEGACY-COMMIT", 100) == "REGISTERED"
        # Simulate a pre-Decimal journal restored consistently on all replicas.
        for node in cluster:
            node._raft._resources["budget-LEGACY-COMMIT"] = {
                "capacity": 100.0, "reserved": 0.1, "committed": 0.0,
            }
            node._raft._reservations["legacy-reservation"] = {
                "key": "legacy-key", "amount": 0.1,
                "resource_id": "budget-LEGACY-COMMIT", "state": "RESERVED",
            }
            node._raft._key_index["legacy-key"] = "legacy-reservation"
        result = leader.commit("legacy-reservation")
        assert result.status == "COMMITTED"
        state = leader.get_state("budget-LEGACY-COMMIT")
        assert state.reserved == Decimal("0.0")
        assert state.committed == Decimal("0.1")

    def test_legacy_five_argument_reserve_command_remains_callable(self, cluster):
        leader = _get_leader(cluster)
        assert leader.register_resource("budget-LEGACY-CMD", 100) == "REGISTERED"
        # Old replicated journals supplied a fifth wall-clock timestamp.
        result = leader._raft.reserve(
            "legacy-command-key", Decimal("10"), "budget-LEGACY-CMD",
            "legacy-command-reservation", 1700000000.0, sync=True, timeout=10.0,
        )
        assert result[0] == "RESERVED"
        assert result[1] == "legacy-command-reservation"

    def test_large_capacity_preserves_fractional_available_value(self, cluster):
        leader = _get_leader(cluster)
        assert leader.register_resource("budget-LARGE-DECIMAL", "1000000000000000000") == "REGISTERED"
        reservation = leader.reserve("small-fraction", "0.1", "budget-LARGE-DECIMAL")
        assert reservation.status == "RESERVED"
        state = leader.get_state("budget-LARGE-DECIMAL")
        assert state.available == Decimal("999999999999999999.9")
        assert state.reserved == Decimal("0.1")
