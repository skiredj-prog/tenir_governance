"""Typed evidence boundary for experimental native SQLite attempt qualification.

No signed proof is authority to dispatch, settle budget, or retry.
"""
from __future__ import annotations
from dataclasses import dataclass

class NativeAttemptHold(ValueError):
    """Unsigned, unbound or ambiguous evidence: HOLD."""

@dataclass(frozen=True)
class NativeVerifiedEvidence:
    evidence_id: str
    attempt_id: str
    lei: str
    outcome: str
    authority_id: str
    verification_receipt: str
    nonce: str
    operation_digest: str
