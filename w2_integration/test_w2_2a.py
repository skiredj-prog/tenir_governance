"""W2.2a tests: native authority gate refuses dispatch under historical G4-A constraints"""
from w2_2a_authority import NativeAuthorityGate
import hashlib, json

def test_no_grant_refused():
    gate = NativeAuthorityGate()
    ok, reason = gate.authorize_dispatch("op1", "hash1")
    assert not ok and reason == "no_grant"
    print("no grant: PASS")

def test_execution_not_authorized():
    gate = NativeAuthorityGate()
    payload = {"amount": 100}
    rh = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    gate.issue("op1", "eval1", rh)
    ok, reason = gate.authorize_dispatch("op1", rh)
    assert not ok and reason == "execution_not_authorized"
    print("execution_authorized=false blocks: PASS")

def test_hash_mismatch():
    gate = NativeAuthorityGate()
    gate.issue("op1", "eval1", "correct-hash")
    ok, reason = gate.authorize_dispatch("op1", "wrong-hash")
    assert not ok and reason == "request_hash_mismatch"
    print("hash mismatch: PASS")

def test_revoked():
    gate = NativeAuthorityGate()
    g = gate.issue("op1", "eval1", "h")
    g.revoked = True
    ok, reason = gate.authorize_dispatch("op1", "h")
    assert not ok and reason == "revoked"
    print("revoked: PASS")

if __name__ == "__main__":
    test_no_grant_refused()
    test_execution_not_authorized()
    test_hash_mismatch()
    test_revoked()
    print("W2.2a authority tests: PASS")
