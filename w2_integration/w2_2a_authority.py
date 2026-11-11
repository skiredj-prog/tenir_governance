"""
W2.2a — Native authority boundary (experimental)
Does NOT substitute LabS1AuthorizationBoundary for G4-A.
Respects historical G4-A: execution_authorized remains false.
No provider call is made when the grant is invalid, HOLD, expired or revoked.
"""
from datetime import datetime, timezone, timedelta
import hashlib, json

class NativeLabGrant:
    """Minimal grant structure compatible with experimental G4-A constraints."""
    def __init__(self, op_id: str, evaluation_id: str, request_hash: str, expires_at: str = None):
        self.grant_id = f"g4a-lab-{op_id}"
        self.operation_id = op_id
        self.evaluation_id = evaluation_id
        self.request_hash = request_hash
        self.issued_at = datetime.now(timezone.utc).isoformat()
        self.expires_at = expires_at or (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        self.revoked = False
        # Explicit: historical G4-A does not authorize execution
        self.execution_authorized = False
        self.decision = "HOLD"

    def is_valid_for_dispatch(self, request_hash: str) -> tuple[bool, str]:
        if self.revoked:
            return False, "revoked"
        if self.request_hash != request_hash:
            return False, "request_hash_mismatch"
        if datetime.fromisoformat(self.expires_at) < datetime.now(timezone.utc):
            return False, "expired"
        if not self.execution_authorized:
            return False, "execution_not_authorized"  # historical G4-A constraint
        return True, "ok"

class NativeAuthorityGate:
    """Gate that refuses dispatch unless a valid grant exists.
    In current experimental scope, execution_authorized=false always blocks."""
    def __init__(self):
        self.grants = {}

    def issue(self, op_id: str, evaluation_id: str, request_hash: str) -> NativeLabGrant:
        g = NativeLabGrant(op_id, evaluation_id, request_hash)
        self.grants[op_id] = g
        return g

    def authorize_dispatch(self, op_id: str, request_hash: str) -> tuple[bool, str]:
        g = self.grants.get(op_id)
        if not g:
            return False, "no_grant"
        return g.is_valid_for_dispatch(request_hash)
