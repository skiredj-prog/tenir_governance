"""Operation-scoped budget coordination.

The in-process implementation serializes mutations per resource within one
process. It is not durable and does not coordinate multiple processes/hosts.
Use a transactional or distributed implementation for those deployments.
"""
from __future__ import annotations

import threading
from typing import Protocol, runtime_checkable


@runtime_checkable
class BudgetCoordinator(Protocol):
    def reserve(self, resource_id: str, amount: float, reservation_id: str | None = None) -> bool: ...
    def commit(self, resource_id: str, amount: float, reservation_id: str | None = None) -> bool: ...
    def release(self, resource_id: str, amount: float, reservation_id: str | None = None) -> bool: ...
    def refund(self, resource_id: str, amount: float, reservation_id: str | None = None) -> bool: ...
    def get_utilization(self, resource_id: str) -> float | None: ...


class InProcessBudgetCoordinator:
    """Single-process, thread-safe coordinator with operation-scoped idempotency."""

    def __init__(self):
        self._resources: dict[str, object] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._reservations: dict[tuple[str, str], float] = {}
        self._committed: dict[tuple[str, str], float] = {}

    def _lock_for(self, resource_id: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(resource_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[resource_id] = lock
            return lock

    def register(self, resource) -> None:
        """Register the shared Resource object; preserve an existing lock."""
        with self._lock_for(resource.resource_id):
            self._resources[resource.resource_id] = resource

    def get_resource(self, resource_id: str):
        return self._resources.get(resource_id)

    def reserve(self, resource_id: str, amount: float, reservation_id: str | None = None) -> bool:
        resource = self._resources.get(resource_id)
        if resource is None or amount <= 0:
            return False
        with self._lock_for(resource_id):
            if reservation_id is not None:
                key = (resource_id, reservation_id)
                if key in self._reservations:
                    return self._reservations[key] == amount
                if key in self._committed:
                    return self._committed[key] == amount
            if resource.available < amount:
                return False
            if not resource.reserve(amount):
                return False
            if reservation_id is not None:
                self._reservations[(resource_id, reservation_id)] = amount
            return True

    def commit(self, resource_id: str, amount: float, reservation_id: str | None = None) -> bool:
        resource = self._resources.get(resource_id)
        if resource is None or amount <= 0:
            return False
        with self._lock_for(resource_id):
            if reservation_id is not None:
                key = (resource_id, reservation_id)
                if key in self._committed:
                    return False
                if self._reservations.get(key) != amount:
                    return False
            if not resource.commit(amount):
                return False
            if reservation_id is not None:
                key = (resource_id, reservation_id)
                self._reservations.pop(key, None)
                self._committed[key] = amount
            return True

    def release(self, resource_id: str, amount: float, reservation_id: str | None = None) -> bool:
        resource = self._resources.get(resource_id)
        if resource is None or amount <= 0:
            return False
        with self._lock_for(resource_id):
            if reservation_id is not None:
                key = (resource_id, reservation_id)
                reserved = self._reservations.get(key)
                if reserved is None or reserved != amount:
                    return False
                self._reservations.pop(key, None)
            elif resource.reserved < amount:
                return False
            resource.release(amount)
            return True

    def refund(self, resource_id: str, amount: float, reservation_id: str | None = None) -> bool:
        resource = self._resources.get(resource_id)
        if resource is None or amount <= 0:
            return False
        with self._lock_for(resource_id):
            if reservation_id is not None:
                key = (resource_id, reservation_id)
                committed = self._committed.get(key)
                if committed is None or committed != amount:
                    return False
                self._committed.pop(key, None)
            elif resource.consumed < amount:
                return False
            resource.refund(amount)
            return True

    def get_utilization(self, resource_id: str) -> float | None:
        resource = self._resources.get(resource_id)
        if resource is None:
            return None
        with self._lock_for(resource_id):
            if resource.capacity <= 0:
                return None
            return (resource.consumed + resource.reserved) / resource.capacity
