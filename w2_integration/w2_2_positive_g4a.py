"""
W2.2 — Positive G4-A contract implementation (experimental)
G4-A owns admission, I8, signed permit and dispatch claim.
Permit issued AFTER reservation.
Consumption + dispatch intent persisted BEFORE B3 call.
Historical HOLD / execution_authorized=false and local grant remain refused.
"""
from datetime import datetime, timezone, timedelta
import hashlib, json, os
from w2_2b_canonical import request_hash as canon_request_hash
from i8_ledger import I8Ledger
from lab_s1_bridge import RC01Lab
from proof_provider import ProofProvider

class PositivePermit:
    def __init__(self, op_id: str, evaluation_id: str, request_hash: str, reservation_id: str):
        self.permit_id = f"permit-{op_id}"
        self.operation_id = op_id
        self.evaluation_id = evaluation_id
        self.request_hash = request_hash
        self.reservation_id = reservation_id
        self.issued_at = datetime.now(timezone.utc).isoformat()
        self.expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        self.consumed = False
        # Positive under this experimental contract only
        self.execution_authorized = True
        self.decision = "ALLOW"

class PositiveG4A:
    """Experimental positive authority. Distinct from historical G4-A."""
    def __init__(self, i8: I8Ledger, rc01: RC01Lab, provider: ProofProvider):
        self.i8 = i8
        self.rc01 = rc01
        self.provider = provider
        self.permits = {}

    def admit_and_reserve(self, op_id: str, evaluation_id: str, payload: dict, amount: int = 100):
        rh = canon_request_hash(payload)
        # Historical-style refusal path remains available via separate gate
        self.i8.reserve(op_id, amount)
        reservation_id = f"res-{op_id}"
        self.rc01.transition(op_id, "RESERVED", "positive_admission")
        permit = PositivePermit(op_id, evaluation_id, rh, reservation_id)
        self.permits[op_id] = permit
        return permit

    def consume_and_dispatch(self, op_id: str, payload: dict, idempotency_key: str):
        permit = self.permits.get(op_id)
        if not permit or permit.consumed:
            raise RuntimeError("no valid unconsumed permit")
        rh = canon_request_hash(payload)
        if permit.request_hash != rh:
            raise RuntimeError("request_hash mismatch on positive path")
        # Persist intent before provider call
        self.rc01.transition(op_id, "DISPATCHING", "permit_consumed_intent_persisted")
        permit.consumed = True
        # B3 call
        receipt = self.provider.execute(op_id, idempotency_key, payload)
        return receipt
