# Profile: agentic-governance-profile-v1.0.0
# Compliance class: profile-bound
"""API-level errors of the composition layer.

A structurally malformed request is refused at the API boundary, before any
governance evaluation: nothing is registered, reserved, held or vetoed. Such a
refusal is not a governance decision. It is raised as
:class:`InvalidArgumentError` (code ``INVALID_ARGUMENT``), never returned as a
``HARD_VETO``, so that a caller, a transport and an auditor can tell an
API-level rejection from a governance-level veto (author's decision of
23 November 2026, R8; amended RFC-2 v0.2 §1.2). 5.7.6 returned these refusals
as ``composed_decision = "HARD_VETO"`` with ``operation_state =
"not_registered"``.

What is malformed: an action payload that is not canonical JSON, an execution
amount that is not a finite non-negative number (or not strictly positive when
a resource is named), and, under Profile S, a missing or ill-formed payload
digest or operation identity. What is *not* malformed, and stays a governance
decision: a request whose measures are absent or invalid (``HOLD`` for
insufficient evidence, rule A), a payload that does not match the authorized
one at dispatch (binding refusal), or any membrane invariant.
"""
from __future__ import annotations

from typing import Any


class InvalidArgumentError(ValueError):
    """A malformed request, refused before registration. Not a decision."""

    code = "INVALID_ARGUMENT"

    def __init__(self, reason: str, *, field: str | None = None,
                 passage_id: str | None = None, idempotency_key: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.field = field
        self.passage_id = passage_id
        self.idempotency_key = idempotency_key

    def to_dict(self) -> dict[str, Any]:
        """The transport-level shape of this rejection. No decision fields:
        a transport must not present it as PASS, FLAG, HOLD or HARD_VETO."""
        return {
            "error": self.code,
            "reason": self.reason,
            "field": self.field,
            "passage_id": self.passage_id,
            "idempotency_key": self.idempotency_key,
            "operation_state": "not_registered",
        }

    def __str__(self) -> str:
        where = f" ({self.field})" if self.field else ""
        return f"{self.code}{where}: {self.reason}"


__all__ = ["InvalidArgumentError"]
