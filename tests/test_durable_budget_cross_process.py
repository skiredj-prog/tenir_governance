"""Tests for DurableBudgetCoordinator cross-process I8 conservation.

These tests verify that I8 (reserved + consumed <= capacity) is maintained
across multiple processes using fcntl.flock coordination.

Tests run actual subprocesses to verify cross-process atomicity.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

import pytest


def _run_subprocess_worker(store_path, resource_id, key, amount, action, capacity):
    """Run a worker in a subprocess that performs a budget operation."""
    script = f"""
import sys, json, os
sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})
from tenir_governance.operations.budget import DurableBudgetCoordinator

coord = DurableBudgetCoordinator({repr(store_path)})

# Register resource if not exists
if {repr(resource_id)} not in coord._resources:
    class R:
        resource_id = {repr(resource_id)}
        capacity = {capacity}.0
        reserved = 0.0
        consumed = 0.0
    coord.register(R())

action = {repr(action)}
if action == 'reserve':
    result = coord.reserve({repr(resource_id)}, {amount}.0)
    print(f"RESERVE:{result}")
elif action == 'commit':
    result = coord.commit({repr(resource_id)}, {amount}.0)
    print(f"COMMIT:{{result}}")
elif action == 'release':
    coord.release({repr(resource_id)}, {amount}.0)
    print("RELEASE:done")
elif action == 'state':
    state = coord.get_state({repr(resource_id)})
    print(f"STATE:{{json.dumps(state)}}")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=30
    )
    return result.stdout.strip(), result.stderr.strip()


class TestCrossProcessBudgetConservation:
    """Verify I8 is maintained across multiple processes."""

    def test_two_processes_reserve_same_budget_one_fails(self, tmp_path):
        """Two processes try to reserve the same budget simultaneously.
        I8: total reserved must not exceed capacity."""
        store = str(tmp_path / "budget.json")

        # First, register the resource
        worker_script = f"""
import sys, os
sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})
from tenir_governance.operations.budget import DurableBudgetCoordinator

coord = DurableBudgetCoordinator({repr(store)})
class R:
    resource_id = 'res1'
    capacity = 100.0
    reserved = 0.0
    consumed = 0.0
coord.register(R())
print("REGISTERED")
"""
        subprocess.run([sys.executable, "-c", worker_script], capture_output=True, timeout=10)

        # Two processes reserve 80 each (capacity is 100, only one should succeed)
        script = f"""
import sys, os
sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})
from tenir_governance.operations.budget import DurableBudgetCoordinator
coord = DurableBudgetCoordinator({repr(store)})
result = coord.reserve('res1', 80.0)
print(f"RESULT:{{result}}")
"""
        # Run both simultaneously
        p1 = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p2 = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        out1, _ = p1.communicate(timeout=10)
        out2, _ = p2.communicate(timeout=10)

        r1 = "True" in out1.decode().strip()
        r2 = "True" in out2.decode().strip()

        # Exactly one should succeed (I8: 80 + 80 = 160 > 100)
        assert r1 != r2, f"I8 violated: both reserves succeeded (r1={r1}, r2={r2})"

        # Verify state
        state_script = f"""
import sys, json, os
sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})
from tenir_governance.operations.budget import DurableBudgetCoordinator
coord = DurableBudgetCoordinator({repr(store)})
state = coord.get_state('res1')
print(json.dumps(state))
"""
        result = subprocess.run([sys.executable, "-c", state_script], capture_output=True, text=True, timeout=10)
        state = json.loads(result.stdout.strip())

        # I8: reserved + consumed <= capacity
        assert state['reserved'] + state['consumed'] <= state['capacity'], \
            f"I8 violated: {state['reserved']} + {state['consumed']} > {state['capacity']}"
        assert state['reserved'] == 80, f"Expected reserved=80, got {state['reserved']}"

    def test_multiple_processes_reserve_within_budget(self, tmp_path):
        """Multiple processes reserve amounts that fit within budget.
        I8 must hold at all times."""
        store = str(tmp_path / "budget.json")

        # Register resource
        worker_script = f"""
import sys, os
sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})
from tenir_governance.operations.budget import DurableBudgetCoordinator
coord = DurableBudgetCoordinator({repr(store)})
class R:
    resource_id = 'res1'
    capacity = 100.0
    reserved = 0.0
    consumed = 0.0
coord.register(R())
print("OK")
"""
        subprocess.run([sys.executable, "-c", worker_script], capture_output=True, timeout=10)

        # 5 processes each reserve 20 (total 100, exactly fits)
        processes = []
        for i in range(5):
            script = f"""
import sys, os
sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})
from tenir_governance.operations.budget import DurableBudgetCoordinator
coord = DurableBudgetCoordinator({repr(store)})
result = coord.reserve('res1', 20.0)
print(f"{{result}}")
"""
            p = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            processes.append(p)

        results = []
        for p in processes:
            out, _ = p.communicate(timeout=10)
            results.append("True" in out.decode().strip())

        # All 5 should succeed (5 * 20 = 100 = capacity)
        assert all(results), f"Some reservations failed: {results}"

        # Verify I8
        state_script = f"""
import sys, json, os
sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})
from tenir_governance.operations.budget import DurableBudgetCoordinator
coord = DurableBudgetCoordinator({repr(store)})
state = coord.get_state('res1')
print(json.dumps(state))
"""
        result = subprocess.run([sys.executable, "-c", state_script], capture_output=True, text=True, timeout=10)
        state = json.loads(result.stdout.strip())

        assert state['reserved'] == 100, f"Expected reserved=100, got {state['reserved']}"
        assert state['reserved'] + state['consumed'] <= state['capacity'], "I8 violated"

    def test_cross_process_reserve_then_commit(self, tmp_path):
        """One process reserves, another commits. I8 must hold throughout."""
        store = str(tmp_path / "budget.json")

        # Register + reserve
        setup_script = f"""
import sys, os
sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})
from tenir_governance.operations.budget import DurableBudgetCoordinator
coord = DurableBudgetCoordinator({repr(store)})
class R:
    resource_id = 'res1'
    capacity = 100.0
    reserved = 0.0
    consumed = 0.0
coord.register(R())
r = coord.reserve('res1', 40.0)
print(f"RESERVE:{{r}}")
"""
        result = subprocess.run([sys.executable, "-c", setup_script], capture_output=True, text=True, timeout=10)
        assert "True" in result.stdout

        # Commit from a different process
        commit_script = f"""
import sys, os
sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})
from tenir_governance.operations.budget import DurableBudgetCoordinator
coord = DurableBudgetCoordinator({repr(store)})
r = coord.commit('res1', 40.0)
print(f"COMMIT:{{r}}")
state = coord.get_state('res1')
import json
print(f"STATE:{{json.dumps(state)}}")
"""
        result = subprocess.run([sys.executable, "-c", commit_script], capture_output=True, text=True, timeout=10)
        assert "True" in result.stdout

        # Parse state
        for line in result.stdout.strip().split('\n'):
            if line.startswith("STATE:"):
                state = json.loads(line[6:])
                assert state['consumed'] == 40, f"Expected consumed=40, got {state['consumed']}"
                assert state['reserved'] == 0, f"Expected reserved=0, got {state['reserved']}"
                assert state['reserved'] + state['consumed'] <= state['capacity'], "I8 violated"

    def test_i8_holds_under_concurrent_reserve_and_release(self, tmp_path):
        """Concurrent reserves and releases: I8 must hold at all times."""
        store = str(tmp_path / "budget.json")

        # Register
        setup = f"""
import sys, os
sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})
from tenir_governance.operations.budget import DurableBudgetCoordinator
coord = DurableBudgetCoordinator({repr(store)})
class R:
    resource_id = 'res1'
    capacity = 100.0
    reserved = 0.0
    consumed = 0.0
coord.register(R())
"""
        subprocess.run([sys.executable, "-c", setup], capture_output=True, timeout=10)

        # Reserve 60
        reserve_60 = f"""
import sys, os
sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})
from tenir_governance.operations.budget import DurableBudgetCoordinator
coord = DurableBudgetCoordinator({repr(store)})
r = coord.reserve('res1', 60.0)
print(r)
"""
        result = subprocess.run([sys.executable, "-c", reserve_60], capture_output=True, text=True, timeout=10)
        assert "True" in result.stdout

        # Concurrently: reserve 50 (should fail, only 40 available) and release 60
        reserve_50 = subprocess.Popen(
            [sys.executable, "-c", f"""
import sys, os
sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})
from tenir_governance.operations.budget import DurableBudgetCoordinator
coord = DurableBudgetCoordinator({repr(store)})
r = coord.reserve('res1', 50.0)
print(r)
"""],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        release_60 = subprocess.Popen(
            [sys.executable, "-c", f"""
import sys, os
sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})
from tenir_governance.operations.budget import DurableBudgetCoordinator
coord = DurableBudgetCoordinator({repr(store)})
coord.release('res1', 60.0)
print("released")
"""],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )

        out_r50, _ = reserve_50.communicate(timeout=10)
        out_rel, _ = release_60.communicate(timeout=10)

        # The reserve of 50 either fails (if it ran before release) or succeeds (if after)
        # Either way, I8 must hold. Check final state.
        state_script = f"""
import sys, json, os
sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})
from tenir_governance.operations.budget import DurableBudgetCoordinator
coord = DurableBudgetCoordinator({repr(store)})
state = coord.get_state('res1')
print(json.dumps(state))
"""
        result = subprocess.run([sys.executable, "-c", state_script], capture_output=True, text=True, timeout=10)
        state = json.loads(result.stdout.strip())

        # I8 must hold regardless of interleaving
        assert state['reserved'] + state['consumed'] <= state['capacity'], \
            f"I8 violated: {state['reserved']} + {state['consumed']} > {state['capacity']}"


class TestDurableBudgetCoordinatorUnit:
    """Unit tests for DurableBudgetCoordinator (single process)."""

    def test_reserve_within_budget(self, tmp_path):
        from tenir_governance.operations.budget import DurableBudgetCoordinator
        coord = DurableBudgetCoordinator(str(tmp_path / "budget.json"))

        class R:
            resource_id = 'res1'
            capacity = 100.0
            reserved = 0.0
            consumed = 0.0
        coord.register(R())

        assert coord.reserve('res1', 30.0) == True
        state = coord.get_state('res1')
        assert state['reserved'] == 30.0

    def test_reserve_exceeds_budget(self, tmp_path):
        from tenir_governance.operations.budget import DurableBudgetCoordinator
        coord = DurableBudgetCoordinator(str(tmp_path / "budget.json"))

        class R:
            resource_id = 'res1'
            capacity = 100.0
            reserved = 0.0
            consumed = 0.0
        coord.register(R())

        assert coord.reserve('res1', 30.0) == True
        assert coord.reserve('res1', 80.0) == False  # only 70 available

    def test_commit_releases_reservation(self, tmp_path):
        from tenir_governance.operations.budget import DurableBudgetCoordinator
        coord = DurableBudgetCoordinator(str(tmp_path / "budget.json"))

        class R:
            resource_id = 'res1'
            capacity = 100.0
            reserved = 0.0
            consumed = 0.0
        coord.register(R())

        coord.reserve('res1', 40.0)
        assert coord.commit('res1', 40.0) == True
        state = coord.get_state('res1')
        assert state['reserved'] == 0
        assert state['consumed'] == 40

    def test_release_frees_budget(self, tmp_path):
        from tenir_governance.operations.budget import DurableBudgetCoordinator
        coord = DurableBudgetCoordinator(str(tmp_path / "budget.json"))

        class R:
            resource_id = 'res1'
            capacity = 100.0
            reserved = 0.0
            consumed = 0.0
        coord.register(R())

        coord.reserve('res1', 50.0)
        coord.release('res1', 50.0)
        state = coord.get_state('res1')
        assert state['reserved'] == 0
        assert state['available'] == 100

    def test_i8_always_holds(self, tmp_path):
        from tenir_governance.operations.budget import DurableBudgetCoordinator
        coord = DurableBudgetCoordinator(str(tmp_path / "budget.json"))

        class R:
            resource_id = 'res1'
            capacity = 100.0
            reserved = 0.0
            consumed = 0.0
        coord.register(R())

        # Reserve and commit various amounts
        coord.reserve('res1', 30.0)
        coord.commit('res1', 30.0)
        coord.reserve('res1', 50.0)
        coord.release('res1', 20.0)

        state = coord.get_state('res1')
        assert state['reserved'] + state['consumed'] <= state['capacity']

    def test_state_survives_new_instance(self, tmp_path):
        """Budget state persists across coordinator instances (same process)."""
        from tenir_governance.operations.budget import DurableBudgetCoordinator
        store = str(tmp_path / "budget.json")

        coord1 = DurableBudgetCoordinator(store)
        class R:
            resource_id = 'res1'
            capacity = 100.0
            reserved = 0.0
            consumed = 0.0
        coord1.register(R())
        coord1.reserve('res1', 40.0)

        # New instance, same store
        coord2 = DurableBudgetCoordinator(store)
        state = coord2.get_state('res1')
        assert state is not None
        assert state['reserved'] == 40.0, f"Expected 40, got {state['reserved']}"