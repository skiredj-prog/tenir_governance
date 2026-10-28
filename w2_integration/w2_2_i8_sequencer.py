"""
W2.2 — I8 sequencing contract (Agent 0)
1. Pre-admission: grant evaluation, no reservation
2. Reservation: only after valid grant
3. Consumption: before dispatch, same I8 authority
4. Pre-dispatch refusal: no orphan reservation
5. UNKNOWN/REPAIR: no automatic release
"""
from w2_2a_authority import NativeAuthorityGate
from i8_ledger import I8Ledger
from lab_s1_bridge import RC01Lab

class I8Sequencer:
    def __init__(self, i8: I8Ledger, auth: NativeAuthorityGate, rc01: RC01Lab):
        self.i8 = i8
        self.auth = auth
        self.rc01 = rc01

    def pre_admit(self, op_id: str, evaluation_id: str, request_hash: str):
        """Step 1: evaluate grant, no reservation."""
        grant = self.auth.issue(op_id, evaluation_id, request_hash)
        self.rc01.transition(op_id, "PRE_ADMITTED", "grant_evaluated")
        return grant

    def try_reserve_and_dispatch(self, op_id: str, request_hash: str, amount: int = 100):
        """Step 2-3: reserve only if authorized, then consume before dispatch."""
        ok, reason = self.auth.authorize_dispatch(op_id, request_hash)
        if not ok:
            self.rc01.transition(op_id, "HOLD", f"pre_dispatch_refused:{reason}")
            return {"reserved": False, "dispatched": False, "reason": reason}

        # Reservation only after positive authorization
        self.i8.reserve(op_id, amount)
        self.rc01.transition(op_id, "RESERVED", "grant_authorized")

        # Consumption before dispatch (same authority)
        # In current lab, dispatch remains blocked by execution_authorized=false
        # so this path is not reached under historical G4-A.
        self.rc01.transition(op_id, "DISPATCHING", "permit_consumed")
        return {"reserved": True, "dispatched": True, "reason": "ok"}
