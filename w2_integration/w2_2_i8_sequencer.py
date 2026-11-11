"""
W2.2 — I8 sequencing contract (Agent 0)
1. Pre-admission: grant evaluation, no reservation
2. Reservation: only after valid grant
3. Consumption: before dispatch, same I8 authority
4. Pre-dispatch refusal: no orphan reservation
5. UNKNOWN/REPAIR: no automatic release
"""
from w2_2a_authority import NativeAuthorityGate
from w2_2b_canonical import bind_causal
from i8_ledger import I8Ledger
from lab_s1_bridge import RC01Lab

class I8Sequencer:
    def __init__(self, i8: I8Ledger, auth: NativeAuthorityGate, rc01: RC01Lab):
        self.i8 = i8
        self.auth = auth
        self.rc01 = rc01
        self.bindings = []

    def pre_admit(self, op_id: str, evaluation_id: str, request_hash: str, idempotency_key: str = None):
        """Step 1: evaluate grant, no reservation. Record causal binding."""
        grant = self.auth.issue(op_id, evaluation_id, request_hash)
        binding = bind_causal(
            operation_id=op_id,
            evaluation_id=evaluation_id,
            grant_id=grant.grant_id,
            idempotency_key=idempotency_key or f"idem-{op_id}",
            request_hash_value=request_hash,
        )
        self.bindings.append(binding)
        self.rc01.transition(op_id, "PRE_ADMITTED", "grant_evaluated")
        return grant, binding

    def try_reserve_and_dispatch(self, op_id: str, request_hash: str, amount: int = 100):
        """Step 2-3: reserve only if authorized, then consume before dispatch."""
        ok, reason = self.auth.authorize_dispatch(op_id, request_hash)
        if not ok:
            self.rc01.transition(op_id, "HOLD", f"pre_dispatch_refused:{reason}")
            return {"reserved": False, "dispatched": False, "reason": reason, "binding": self._last_binding(op_id)}

        # Reservation only after positive authorization (not reached under historical G4-A)
        self.i8.reserve(op_id, amount)
        self.rc01.transition(op_id, "RESERVED", "grant_authorized")
        self.rc01.transition(op_id, "DISPATCHING", "permit_consumed")
        return {"reserved": True, "dispatched": True, "reason": "ok", "binding": self._last_binding(op_id)}

    def _last_binding(self, op_id: str):
        for b in reversed(self.bindings):
            if b["operation_id"] == op_id:
                return b
        return None
