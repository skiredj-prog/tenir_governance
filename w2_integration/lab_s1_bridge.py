"""
W2 LAB-S1 Integration Bridge — Experimental
PROOF-01 / Candidate / Non-certifying

This module introduces an explicit LAB-S1 integration boundary.
It does NOT wire the historical G4-A (experimental_g4a_settlement.py) directly to the provider.
It does NOT create a second I8 ledger or a second G4-A engine.

Responsibilities preserved:
- G4-A: authorization boundary (simulated grant assumed for LAB-S1)
- B3: external simulated provider (ProofProvider)
- I8: single budget authority (I8Ledger)
- RC-01: reconciliation states (implemented minimally here)
"""
from proof_provider import ProofProvider, AckLost
from i8_ledger import I8Ledger
import json
from datetime import datetime, timezone

class RC01Lab:
    def __init__(self):
        self.states = {}

    def transition(self, op_id: str, to: str, reason: str):
        prev = self.states.get(op_id, "INIT")
        self.states[op_id] = to
        return {"ts": datetime.now(timezone.utc).isoformat(), "op": op_id, "from": prev, "to": to, "reason": reason}

def run_lab_s1_scenario(op_id: str = "op-w2-lab-001"):
    """Minimal LAB-S1 scenario: grant assumed → reserve → dispatch → lost ACK → REPAIR → recovery"""
    provider = ProofProvider()
    i8 = I8Ledger()
    rc01 = RC01Lab()
    traces = []

    # 1. Authorization boundary (LAB-S1 explicit, not historical G4-A wiring)
    traces.append(rc01.transition(op_id, "RESERVED", "lab_s1_grant_assumed"))
    i8.reserve(op_id, 100)

    # 2. Dispatch intent + B3 execute with lost ACK
    traces.append(rc01.transition(op_id, "DISPATCHING", "dispatch_intent"))
    payload = {"scope": {"account": "lab", "amount_minor": 100, "currency": "EUR"}, "action_type": "transfer"}
    try:
        provider.execute(op_id, "idem-w2-001", payload, lose_ack=True)
    except AckLost:
        pass
    traces.append(rc01.transition(op_id, "UNKNOWN", "ack_lost"))

    # 3. Reconcile
    receipt = provider.reconcile(op_id)
    assert receipt and receipt["effect_status"] == "EXECUTED"

    # 4. Settle fails → REPAIR
    traces.append(rc01.transition(op_id, "REPAIR", "settle_failed"))
    i8.fail_next_settle()
    try:
        i8.settle(op_id)
    except RuntimeError:
        pass

    # 5. Recovery
    i8.settle(op_id)
    traces.append(rc01.transition(op_id, "RECONCILED_CONFIRMED", "recovery"))

    return {
        "op_id": op_id,
        "final_state": rc01.states[op_id],
        "execute_count": provider.execute_count(op_id),
        "settlement_count": i8.net_settlement_count(op_id),
        "receipt_status": receipt["effect_status"],
        "traces": traces,
    }

if __name__ == "__main__":
    result = run_lab_s1_scenario()
    print(json.dumps(result, indent=2))
    assert result["execute_count"] == 1
    assert result["settlement_count"] == 1
    assert result["final_state"] == "RECONCILED_CONFIRMED"
    print("W2 LAB-S1 minimal integration: PASS")
