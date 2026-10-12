"""Action / Passage — the operational unit of adjudication.

Governs: admission, authorization, preconditions, execution status.

Revised per review 2 §4: added stable operation identity (idempotency key)
for at-most-once effect enforcement across retries and crash recovery.
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
from uuid import uuid4


class ExecutionStatus(str, Enum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    RESOLVED = "resolved"
    FAILED = "failed"
    HELD = "held"
    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"


@dataclass
class Passage:
    """A specific action proposed by an agent.

    Attributes:
        passage_id: Unique identifier (auto-generated if not provided).
        agent_id: The agent initiating this passage.
        action_type: E.g. "payment", "delegation", "read".
        target_realm: The realm this passage crosses into.
        amount: Optional monetary amount (for budget gates).
        recipient_id: Optional recipient agent_id (for delegation gates).
        execution_status: Current execution status.
        permit_id: Optional permit bound to this passage (for at-most-once).
        idempotency_key: Stable operation identity for at-most-once enforcement.
            Per review 2 §4: the original operation must retain a stable identity
            across retries. A retry cannot create a new logical operation for the
            same intended effect. A confirmed effect cannot be dispatched again
            under a new attempt.
    """
    passage_id: str = field(default_factory=lambda: str(uuid4()))
    agent_id: str = ""
    action_type: str = ""
    target_realm: str = ""
    amount: float = 0.0
    recipient_id: Optional[str] = None
    execution_status: ExecutionStatus = ExecutionStatus.PENDING
    permit_id: Optional[str] = None
    idempotency_key: Optional[str] = None

    def __post_init__(self):
        """Auto-generate idempotency key if not provided.

        The idempotency key is stable across retries of the SAME logical operation.
        It prevents duplicate external effects after crashes, timeouts, or
        lost responses.
        """
        if self.idempotency_key is None:
            self.idempotency_key = f"op-{self.passage_id}"

    @property
    def is_terminal(self) -> bool:
        """I3: has this passage reached a terminal state?

        Terminal states: RESOLVED (effect confirmed), FAILED (no effect confirmed),
        CONFIRMED (effect confirmed — alias of RESOLVED for external effects).
        """
        return self.execution_status in (
            ExecutionStatus.RESOLVED, ExecutionStatus.FAILED, ExecutionStatus.CONFIRMED
        )

    @property
    def is_executed(self) -> bool:
        """I4: has this passage produced a real effect?"""
        return self.execution_status in (ExecutionStatus.DISPATCHED, ExecutionStatus.CONFIRMED)

    @property
    def is_dispatched(self) -> bool:
        """Has this passage been dispatched to the execution system?"""
        return self.execution_status in (ExecutionStatus.DISPATCHED, ExecutionStatus.UNKNOWN)

    @property
    def is_unresolved(self) -> bool:
        """Is the outcome of this passage still uncertain?

        Per review 2 §4: an unresolved outcome should stay blocked from
        conflicting re-execution.
        """
        return self.execution_status in (
            ExecutionStatus.DISPATCHED, ExecutionStatus.UNKNOWN, ExecutionStatus.HELD
        )