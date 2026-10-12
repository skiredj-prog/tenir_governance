"""Resource / Shared State — budgets, inventory, capacity.

Governs: budgets, inventory, account balances, capacity.
"""
# Profile: agentic-governance-profile-v1.0.0
# Compliance class: profile-bound
# This module is an extension on top of KB3 and TENIR 2.0 schema 1.2.
# It is NOT canonical, NOT doctrinal. It carries implementation-level
# governance enforcement authority only.
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict


@dataclass
class Resource:
    """A shared resource with a capacity.

    Attributes:
        resource_id: Unique identifier.
        capacity: Total capacity / budget limit.
        consumed: Currently consumed amount.
        reserved: Amount reserved by pending (non-executed) passages.
    """
    resource_id: str = ""
    capacity: float = 0.0
    consumed: float = 0.0
    reserved: float = 0.0

    @property
    def available(self) -> float:
        """Available capacity after consumed + reserved."""
        return self.capacity - self.consumed - self.reserved

    def reserve(self, amount: float) -> bool:
        """Reserve capacity for a pending passage. Returns True if successful."""
        if self.available >= amount:
            self.reserved += amount
            return True
        return False

    def commit(self, amount: float) -> bool:
        """Commit a reservation (move from reserved to consumed)."""
        if self.reserved >= amount:
            self.reserved -= amount
            self.consumed += amount
            return True
        return False

    def release(self, amount: float) -> None:
        """Release a reservation (passage was blocked or failed before dispatch)."""
        self.reserved = max(0.0, self.reserved - amount)

    def refund(self, amount: float) -> None:
        """Reverse a committed effect (passage failed after dispatch).

        Per review 3 §1: when reconciliation determines no effect occurred,
        the committed budget must be returned to available.
        """
        self.consumed = max(0.0, self.consumed - amount)


@dataclass
class Budget(Resource):
    """A financial budget — a specialized resource."""
    resource_id: str = "budget"
    capacity: float = 0.0