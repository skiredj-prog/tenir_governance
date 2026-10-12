"""Trajectory / Workflow — the evolving sequence of actions.

Governs: sequence, cumulative risk, temporal constraints.
"""
# Profile: agentic-governance-profile-v1.0.0
# Compliance class: profile-bound
# This module is an extension on top of KB3 and TENIR 2.0 schema 1.2.
# It is NOT canonical, NOT doctrinal. It carries implementation-level
# governance enforcement authority only.
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Trajectory:
    """A sequence of passages forming a workflow.

    Attributes:
        trajectory_id: Unique identifier.
        passages: Ordered list of Passage objects.
        temporal_constraints: List of (min_gap, max_gap) tuples between steps.
            e.g. [(0, 5)] means step 1 must follow step 0 within 5 time units.
    """
    trajectory_id: str = ""
    passages: List[object] = field(default_factory=list)  # List[Passage]
    temporal_constraints: List[tuple] = field(default_factory=list)
    timestamps: List[float] = field(default_factory=list)

    def add_passage(self, passage: object, timestamp: float = 0.0) -> None:
        """Append a passage to the trajectory."""
        self.passages.append(passage)
        self.timestamps.append(timestamp)

    def check_temporal_consistency(self) -> tuple[bool, Optional[str]]:
        """I9: verify the sequence satisfies all temporal constraints.

        Returns:
            (True, None) if consistent, (False, reason) if violated.
        """
        for i, (min_gap, max_gap) in enumerate(self.temporal_constraints):
            if i + 1 >= len(self.timestamps):
                continue
            gap = self.timestamps[i + 1] - self.timestamps[i]
            if gap < min_gap:
                return False, f"Step {i+1} follows step {i} too closely: gap={gap} < min={min_gap}"
            if gap > max_gap:
                return False, f"Step {i+1} follows step {i} too late: gap={gap} > max={max_gap}"
        return True, None

    @property
    def step_count(self) -> int:
        return len(self.passages)