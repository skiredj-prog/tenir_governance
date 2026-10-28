"""
W2.2 LAB-S1 Integrated Subject — Experimental Candidate
PROOF-01 / Non-certifying

This module defines an explicit authorization boundary for LAB-S1.
It does NOT claim effective native G4-A authority (execution_authorized remains false in historical G4-A).
It uses a single I8 ledger instance for the scenario.
"""
import os, json, hashlib
from datetime import datetime, timezone
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

    auth = LabS1AuthorizationBoundary()
    provider = ProofProvider(db_path=provider_db)
    i8 = I8Ledger(db_path=i8_db)                   # single owner for this scenario
    rc01 = RC01Lab(db_path=rc01_db)

    payload = {"scope": {"account": "lab", "amount_minor": 100, "currency": "EUR"}, "action_type": "transfer"}
    request_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    evaluation_id = "eval-w22-001"

    traces = []
    events = []

    # 1. Lab grant (explicitly not native G4-A)
    grant = auth.issue_lab_grant(op_id, evaluation_id, request_hash)
    traces.append(rc01.transition(op_id, "RESERVED", "lab_grant_issued"))
    i8.reserve(op_id, 100)
    events.append({"stream": "i8", "event": "reserve", "op": op_id, "amount": 100})

    # 2. Validate then dispatch
    if not auth.validate_dispatch(op_id, request_hash):
        raise RuntimeError("dispatch refused: no valid lab grant")
    traces.append(rc01.transition(op_id, "DISPATCHING", "dispatch_intent"))
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
        "settlement_count": i8.net_settlement_count(op_id),
        "grant": grant,
        "receipt": receipt,
        "traces": traces,
        "events": events,
        "i8_db": i8_db,
    }
