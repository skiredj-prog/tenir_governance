"""Budget coordination protocol — abstracts atomic reservation.

Per review 2 §1: a per-resource threading.Lock protects threads in one process,
but does NOT coordinate multiple worker processes, containers, hosts, or
external writers. This module abstracts the coordination mechanism behind a
protocol so the in-process lock can be replaced with a transactional database,
serializable transaction, or distributed coordination service without changing
the membrane or composition layer.

The protocol defines linearizability as the correctness concept: concurrent
operations should behave as though they occurred in a valid sequential order.
"""
# Profile: agentic-governance-profile-v1.0.0
# Compliance class: profile-bound
# This module is an extension on top of KB3 and TENIR 2.0 schema 1.2.
# It is NOT canonical, NOT doctrinal. It carries implementation-level
# governance enforcement authority only.
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@runtime_checkable
class BudgetCoordinator(Protocol):
    """Protocol for atomic budget coordination.

    Implementations must guarantee that check-and-reserve is atomic: no two
    concurrent callers can both reserve the same available budget.

    The in-process implementation (InProcessBudgetCoordinator) uses
    threading.Lock and is sufficient for single-process, multi-threaded
    deployments. For multi-process or distributed deployments, replace with a
    transactional database, serializable transaction, or distributed lock.
    """

    def reserve(self, resource_id: str, amount: float) -> bool:
        """Atomically check available capacity and reserve if sufficient.

        Returns True if the reservation succeeded, False if insufficient
        capacity. Must be atomic: no race condition between check and reserve.
        """
        ...

    def commit(self, resource_id: str, amount: float) -> bool:
        """Move a reservation from reserved to consumed. Returns True on success."""
        ...

    def release(self, resource_id: str, amount: float) -> None:
        """Release a reservation (passage blocked, failed, or cancelled)."""
        ...

    def get_utilization(self, resource_id: str) -> float | None:
        """Get current utilization fraction (0.0–1.0), or None if unavailable.

        Per review 2 §3: if the utilization measurement is missing, stale, or
        inconsistent, the composition layer must treat it as indeterminate and
        fail closed (HOLD), not silently proceed.
        """
        ...


class InProcessBudgetCoordinator:
    """In-process budget coordination using threading.Lock.

    LIMITATIONS (per review 2 §1):
        - Only coordinates threads sharing the same lock object in one process.
        - Does NOT coordinate multiple worker processes, containers, or hosts.
        - Does NOT provide durability: budget state is lost on process crash.
        - Does NOT provide linearizability across distributed deployments.

    For production deployments, replace with:
        - A transactional database with SELECT ... FOR UPDATE or equivalent.
        - A serializable transaction spanning the check and reserve.
        - A distributed coordination service (etcd, Zookeeper, Redis with
          atomic operations).
        - A single-writer sequencer process.
    """

    def __init__(self):
        self._resources: dict[str, any] = {}
        self._locks: dict[str, threading.Lock] = {}

    def register(self, resource) -> None:
        """Register a Resource object with this coordinator."""
        self._resources[resource.resource_id] = resource
        self._locks[resource.resource_id] = threading.Lock()

    def get_resource(self, resource_id: str):
        return self._resources.get(resource_id)

    def get_lock(self, resource_id: str) -> threading.Lock:
        if resource_id not in self._locks:
            self._locks[resource_id] = threading.Lock()
        return self._locks[resource_id]

    def reserve(self, resource_id: str, amount: float) -> bool:
        resource = self._resources.get(resource_id)
        if resource is None:
            return False
        with self._locks[resource_id]:
            if resource.available >= amount:
                resource.reserved += amount
                return True
            return False

    def commit(self, resource_id: str, amount: float) -> bool:
        resource = self._resources.get(resource_id)
        if resource is None:
            return False
        with self._locks[resource_id]:
            if resource.reserved >= amount:
                resource.reserved -= amount
                resource.consumed += amount
                return True
            return False

    def release(self, resource_id: str, amount: float) -> None:
        resource = self._resources.get(resource_id)
        if resource is None:
            return
        with self._locks[resource_id]:
            resource.reserved = max(0.0, resource.reserved - amount)

    def get_utilization(self, resource_id: str) -> float | None:
        resource = self._resources.get(resource_id)
        if resource is None:
            return None
        with self._locks[resource_id]:
            if resource.capacity <= 0:
                return None  # indeterminate — fail closed
            return (resource.consumed + resource.reserved) / resource.capacity