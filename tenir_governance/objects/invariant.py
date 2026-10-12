"""Mission / System Invariant — global constraints and acceptable outcomes.

Governs: global constraints, objectives, acceptable outcomes.
"""
# Profile: agentic-governance-profile-v1.0.0
# Compliance class: profile-bound
# This module is an extension on top of KB3 and TENIR 2.0 schema 1.2.
# It is NOT canonical, NOT doctrinal. It carries implementation-level
# governance enforcement authority only.
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass(frozen=True)
class MissionInvariant:
    """A global constraint that must hold across the system.

    Attributes:
        invariant_id: Unique identifier.
        description: Human-readable description.
        predicate: A callable that takes a system state dict and returns bool.
        severity: "hard" (HARD_VETO on violation) or "soft" (FLAG on violation).
    """
    invariant_id: str
    description: str
    predicate: Callable[[dict], bool] = field(default=lambda _: True)
    severity: str = "hard"

    def evaluate(self, system_state: dict) -> tuple[bool, Optional[str]]:
        """Check whether this invariant holds.

        Returns:
            (True, None) if the invariant holds.
            (False, reason) if violated.
        """
        try:
            holds = self.predicate(system_state)
            if holds:
                return True, None
            return False, f"Invariant '{self.invariant_id}' violated: {self.description}"
        except Exception as e:
            return False, f"Invariant '{self.invariant_id}' evaluation error: {e}"