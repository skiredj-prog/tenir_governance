"""
W2.2 LAB-S1 Integrated Subject — Experimental Candidate
PROOF-01 / Non-certifying

This module defines an explicit authorization boundary for LAB-S1.
It does NOT claim effective native G4-A authority (execution_authorized remains false in historical G4-A).
It uses a single I8 ledger instance for the scenario.
"""
import os, json, hashlib
from datetime import datetime, timezone
from w2_2a_authority import NativeAuthorityGate
from w2_2b_canonical import request_hash as canon_request_hash
from proof_provider import ProofProvider, AckLost
from i8_ledger import I8Ledger
from lab_s1_bridge import RC01Lab

class LabS1AuthorizationBoundary:
    """Explicit experimental boundary. Not a wiring of historical G4-A."""
    def __init__(self):
        self.grants = {}

    def issue_lab_grant(self, op_id: str, evaluation_id: str, request_hash: str) -> dict:
        grant = {
            "grant_id": f"lab-grant-{op_id}",
            "operation_id": op_id,
            "evaluation_id": evaluation_id,
            "request_hash": request_hash,
            "issued_at": datetime.now(timezone.utc).isoformat(),
            "execution_authorized": False,  # explicit: does not override G4-A restriction
            "scope": "LAB-S1-simulated",
        }
        self.grants[op_id] = grant
        return grant

    def validate_dispatch(self, op_id: str, request_hash: str) -> bool:
        g = self.grants.get(op_id)
        if not g:
            return False
        return g["request_hash"] == request_hash

def run_integrated_candidate(db_dir: str, op_id: str = "op-w22-integrated"):
    provider_db = os.path.join(db_dir, "provider.db")
    i8_db = os.path.join(db_dir, "i8.db")          # single I8 path
    rc01_db = os.path.join(db_dir, "rc01.db")

    auth = NativeAuthorityGate()
    provider = ProofProvider(db_path=provider_db)
    i8 = I8Ledger(db_path=i8_db)
    rc01 = RC01Lab(db_path=rc01_db)

    payload = {"scope": {"account": "lab", "amount_minor": 100, "currency": "EUR"}, "action_type": "transfer"}
    request_hash = canon_request_hash(payload)
    evaluation_id = "eval-w22-001"

    traces = []
    events = []
    provider_called = False

    # 1. Issue grant via native gate
    grant = auth.issue(op_id, evaluation_id, request_hash)

    # 2. Mandatory authorization check BEFORE any reservation or provider call
    ok, reason = auth.authorize_dispatch(op_id, request_hash)
    if not ok:
        traces.append(rc01.transition(op_id, "HOLD", f"dispatch_refused:{reason}"))
        events.append({"stream": "authority", "event": "dispatch_refused", "op": op_id, "reason": reason})
        return {
            "op_id": op_id,
            "final_state": rc01.get_state(op_id),
            "execute_count": 0,
            "provider_called": False,
            "settlement_count": 0,
            "reservation_count": 0,
            "grant": {"grant_id": grant.grant_id, "execution_authorized": grant.execution_authorized},
            "receipt": None,
            "traces": traces,
            "events": events,
            "i8_db": i8_db,
            "dispatch_refused": True,
            "refuse_reason": reason,
        }

    # Only reached if authorized (currently never under historical G4-A)
    # Reservation happens only after positive authorization
    i8.reserve(op_id, 100)
    traces.append(rc01.transition(op_id, "RESERVED", "grant_authorized"))
    events.append({"stream": "i8", "event": "reserve", "op": op_id, "amount": 100})

    # Only reached if authorized (currently never, under historical G4-A)
    traces.append(rc01.transition(op_id, "DISPATCHING", "dispatch_intent"))
    provider_called = True
    try:
        provider.execute(op_id, "idem-w22-001", payload, lose_ack=True)
    except AckLost:
        pass
    traces.append(rc01.transition(op_id, "UNKNOWN", "ack_lost"))
    events.append({"stream": "provider", "event": "ack_lost", "op": op_id})

    receipt = provider.reconcile(op_id)
    assert receipt and receipt["effect_status"] == "EXECUTED"

    # 3. Settle fails → REPAIR
    i8.fail_next_settle()
    try:
        i8.settle(op_id)
    except RuntimeError:
        traces.append(rc01.transition(op_id, "REPAIR", "settle_failed"))
        events.append({"stream": "i8", "event": "settle_failed", "op": op_id})

    # 4. Recovery
    i8.settle(op_id)
    traces.append(rc01.transition(op_id, "RECONCILED_CONFIRMED", "recovery"))
    events.append({"stream": "i8", "event": "settle", "op": op_id, "result": "success"})

    return {
        "op_id": op_id,
        "final_state": rc01.get_state(op_id),
        "execute_count": provider.execute_count(op_id),
        "provider_called": provider_called,
        "settlement_count": i8.net_settlement_count(op_id),
        "grant": {"grant_id": grant.grant_id, "execution_authorized": grant.execution_authorized},
        "receipt": receipt,
        "traces": traces,
        "events": events,
        "i8_db": i8_db,
        "dispatch_refused": False,
    }
