"""Relationship / Delegation — trust, authority transfer, coordination.

Governs: trust, authority transfer, dependencies, coordination.
"""
# Profile: agentic-governance-profile-v1.0.0
# Compliance class: profile-bound
# This module is an extension on top of KB3 and TENIR 2.0 schema 1.2.
# It is NOT canonical, NOT doctrinal. It carries implementation-level
# governance enforcement authority only.
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class RelationshipType(str, Enum):
    DELEGATION = "delegation"
    COORDINATION = "coordination"
    DEPENDENCY = "dependency"
    COMPETITION = "competition"


@dataclass(frozen=True)
class Relationship:
    """A typed relationship between two agents.

    Attributes:
        from_agent_id: The source agent.
        to_agent_id: The target agent.
        rel_type: The relationship type.
        trust_score: Trust level in [0, 1].
    """
    from_agent_id: str
    to_agent_id: str
    rel_type: RelationshipType = RelationshipType.COORDINATION
    trust_score: float = 1.0


@dataclass
class Delegation:
    """A specific delegation event — one agent transferring authority to another.

    This is the governed object for invariant I7.

    Attributes:
        delegating_agent_id: The agent delegating authority.
        delegatee_agent_id: The agent receiving authority.
        delegated_capabilities: Capabilities being transferred.
        timestamp: When the delegation occurred.
        active: Whether this delegation is currently active.
    """
    delegating_agent_id: str
    delegatee_agent_id: str
    delegated_capabilities: frozenset[str] = field(default_factory=frozenset)
    timestamp: float = 0.0
    active: bool = True

    def is_trusted(self, trusted_set: frozenset[str]) -> bool:
        """Is the delegatee in the delegating agent's trusted set?"""
        return self.delegatee_agent_id in trusted_set