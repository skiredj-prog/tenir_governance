"""W2.2b tests: canonical consistency, mismatch, substitution"""
from w2_2b_canonical import canonical_bytes, request_hash, bind_causal, CANONICAL_VERSION

def test_same_bytes_same_hash():
    payload = {"scope": {"account": "a", "amount_minor": 100, "currency": "EUR"}, "action_type": "transfer"}
    h1 = request_hash(payload)
    h2 = request_hash(payload)
    assert h1 == h2
    assert len(h1) == 64
    print("same bytes → same hash: PASS")

def test_substitution_detected():
    p1 = {"amount": 100}
    p2 = {"amount": 200}
    assert request_hash(p1) != request_hash(p2)
    print("substitution detected: PASS")

def test_binding_complete():
    rh = request_hash({"x": 1})
    b = bind_causal("op1", "eval1", "grant1", "idem1", rh, "receipt-digest")
    assert b["canon_version"] == CANONICAL_VERSION
    assert b["request_hash"] == rh
    assert b["operation_id"] == "op1"
    print("binding complete: PASS")

def test_replay_same_hash():
    payload = {"k": "v"}
    h = request_hash(payload)
    # Replay produces identical hash, no new proof emitted
    assert request_hash(payload) == h
    print("replay same hash: PASS")

if __name__ == "__main__":
    test_same_bytes_same_hash()
    test_substitution_detected()
    test_binding_complete()
    test_replay_same_hash()
    print("W2.2b tests: PASS")
