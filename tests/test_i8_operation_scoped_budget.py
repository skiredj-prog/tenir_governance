"""Regression contract for I8 operation-scoped budget reservations."""
from concurrent.futures import ThreadPoolExecutor

from tenir_governance.objects import Resource
from tenir_governance.operations.budget import InProcessBudgetCoordinator


def coordinator(capacity=100.0):
    budget = InProcessBudgetCoordinator()
    budget.register(Resource(resource_id="budget", capacity=capacity))
    return budget


def test_reserve_is_idempotent_per_operation():
    budget = coordinator()
    assert budget.reserve("budget", 60.0, reservation_id="op-1")
    assert budget.reserve("budget", 60.0, reservation_id="op-1")
    assert budget.get_resource("budget").reserved == 60.0


def test_reservation_id_cannot_be_reused_for_different_amount():
    budget = coordinator()
    assert budget.reserve("budget", 60.0, reservation_id="op-1")
    assert not budget.reserve("budget", 50.0, reservation_id="op-1")
    assert budget.get_resource("budget").reserved == 60.0


def test_release_is_scoped_and_idempotent():
    budget = coordinator()
    assert budget.reserve("budget", 30.0, reservation_id="op-1")
    assert budget.reserve("budget", 40.0, reservation_id="op-2")
    budget.release("budget", 30.0, reservation_id="op-1")
    budget.release("budget", 30.0, reservation_id="op-1")
    assert budget.get_resource("budget").reserved == 40.0


def test_commit_is_scoped_and_idempotent():
    budget = coordinator()
    assert budget.reserve("budget", 30.0, reservation_id="op-1")
    assert budget.reserve("budget", 40.0, reservation_id="op-2")
    assert budget.commit("budget", 30.0, reservation_id="op-1")
    assert not budget.commit("budget", 30.0, reservation_id="op-1")
    resource = budget.get_resource("budget")
    assert resource.reserved == 40.0
    assert resource.consumed == 30.0


def test_concurrent_reservations_respect_capacity():
    budget = coordinator(capacity=100.0)
    def reserve(index):
        return budget.reserve("budget", 10.0, reservation_id=f"op-{index}")
    with ThreadPoolExecutor(max_workers=32) as pool:
        outcomes = list(pool.map(reserve, range(50)))
    assert sum(outcomes) == 10
    assert budget.get_resource("budget").reserved == 100.0
