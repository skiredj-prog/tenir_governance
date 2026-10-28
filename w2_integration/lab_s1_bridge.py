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
import sqlite3
from datetime import datetime, timezone

class RC01Lab:
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS rc01_states (
                operation_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        self.conn.commit()

    def transition(self, op_id: str, to: str, reason: str):
        terminal = {"RECONCILED_CONFIRMED", "RECONCILED_NOT_EXECUTED", "CONFIRMED", "FAILED"}
        with self.conn:  # atomic transaction
            cur = self.conn.execute("SELECT state FROM rc01_states WHERE operation_id = ?", (op_id,))
            row = cur.fetchone()
            prev = row[0] if row else "INIT"
            if prev in terminal:
                if to not in terminal:
                    raise RuntimeError(f"refusing to leave terminal state {prev} -> {to}")
                if to != prev:
                    raise RuntimeError(f"refusing terminal-to-terminal replacement {prev} -> {to}")
            ts = datetime.now(timezone.utc).isoformat()
            self.conn.execute(
                "INSERT OR REPLACE INTO rc01_states (operation_id, state, updated_at) VALUES (?, ?, ?)",
                (op_id, to, ts)
            )
        return {"ts": ts, "op": op_id, "from": prev, "to": to, "reason": reason}

    def get_state(self, op_id: str):
        cur = self.conn.execute("SELECT state FROM rc01_states WHERE operation_id = ?", (op_id,))
        row = cur.fetchone()
        return row[0] if row else None

def run_lab_s1_scenario(op_id: str = "op-w2-lab-001", db_dir: str = None):
    """Minimal LAB-S1 scenario with optional persistent stores"""
    import tempfile, os
    if db_dir is None:
        db_dir = tempfile.mkdtemp(prefix="w2lab_")
    provider_db = os.path.join(db_dir, "provider.db")
    i8_db = os.path.join(db_dir, "i8.db")

    rc01_db = os.path.join(db_dir, "rc01.db")
    provider = ProofProvider(db_path=provider_db)
    i8 = I8Ledger(db_path=i8_db)
    rc01 = RC01Lab(db_path=rc01_db)
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

    # 4. Settle fails FIRST, then enter REPAIR
    i8.fail_next_settle()
    settle_failed = False
    try:
        i8.settle(op_id)
    except RuntimeError:
        settle_failed = True
    if settle_failed:
        traces.append(rc01.transition(op_id, "REPAIR", "settle_failed"))

    # 5. Recovery
    i8.settle(op_id)
    traces.append(rc01.transition(op_id, "RECONCILED_CONFIRMED", "recovery"))

    return {
        "op_id": op_id,
        "final_state": rc01.get_state(op_id),
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
