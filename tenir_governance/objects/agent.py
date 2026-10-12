"""Agent / Principal — the decision-making unit capable of acting.

Governs: identity, authority, permissions, capabilities.
"""
# Profile: agentic-governance-profile-v1.0.0
# Compliance class: profile-bound
# This module is an extension on top of KB3 and TENIR 2.0 schema 1.2.
# It is NOT canonical, NOT doctrinal. It carries implementation-level
# governance enforcement authority only.
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Agent:
    """An autonomous unit (TAU) that can initiate actions.

    Attributes:
        agent_id: Unique identifier.
        authority_score: Authority level in [0, 1].
        trusted_delegates: Set of agent_ids this agent may delegate authority to.
        capabilities: Set of capability strings (e.g. {"payment", "read", "write"}).
    """
    agent_id: str
    authority_score: float = 1.0
    trusted_delegates: frozenset[str] = field(default_factory=frozenset)
    capabilities: frozenset[str] = field(default_factory=frozenset)

    def can_delegate_to(self, other_agent_id: str) -> bool:
        """I7 helper: is other_agent_id in this agent's trusted delegation set?"""
        return other_agent_id in self.trusted_delegates

    def has_capability(self, capability: str) -> bool:
        return capability in self.capabilities


@dataclass(frozen=True)
class Principal:
    """The human or organisational principal on whose behalf an agent acts."""
    principal_id: str
    agent: Agent