"""Signed, scoped and independently time-validated T6/T7 evidence."""
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from tenir_governance.operations.attempt_qualification import (
    AttemptQualificationStore, QualificationError,
)
from tenir_governance.operations.attempt_attestation import (
    Ed25519AttemptVerifier, canonical_attestation_payload,
)

NONCE = "0123456789abcdef"
def signed(private_key, *, outcome="EFFECT_CONFIRMED", **overrides):
    payload = {
        "schema_version":"tenir.attempt.attestation.v1",
        "evidence_id":"evidence-a1",
        "attempt_id":"a1",
        "lei":"LEI-A",
        "nonce":NONCE,
        "outcome":outcome,
        "authority_id":"trusted-authority",
        "issued_at_ms":1000,
        "expires_at_ms":2000,
    }
    payload.update(overrides)
    return {**payload, "signature_hex":private_key.sign(canonical_attestation_payload(payload)).hex()}

def configured(tmp_path, *, revoked=frozenset(), now=1500):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    verifier = Ed25519AttemptVerifier(
        {"trusted-authority":public},revoked_authorities=revoked,now_ms=lambda:now,
    )
    store = AttemptQualificationStore(tmp_path/"attempts.sqlite",verifier)
    store.begin(attempt_id="a1", lei="LEI-A", nonce=NONCE, deadline_ms=1400)
    return store, private

def test_signed_positive_effect_resolves_and_replays(tmp_path):
    store, private = configured(tmp_path)
    proof = signed(private)
    assert store.qualify("a1",proof,observed_ms=1500)=="RESOLVED"
    assert store.qualify("a1",proof,observed_ms=1600)=="RESOLVED"
    assert store.read("a1")["verification_receipt"].startswith("ed25519-sha256:")
    assert [r[0] for r in store.events("a1")].count("RESOLVED")==1

def test_signed_non_execution_fails_after_timeout(tmp_path):
    store, private = configured(tmp_path)
    assert store.timeout("a1",observed_ms=1500)=="UNKNOWN"
    assert store.qualify("a1",signed(private,outcome="NON_EXECUTION_CONFIRMED"),observed_ms=1600)=="FAILED"

@pytest.mark.parametrize("field,value",[
    ("attempt_id","a2"),("lei","LEI-B"),("nonce","ffffffffffffffff"),
    ("outcome","NON_EXECUTION_CONFIRMED"),("authority_id","other-authority"),
    ("expires_at_ms",4000),
])
def test_tampered_signed_payload_rejected(tmp_path,field,value):
    store, private = configured(tmp_path)
    evidence = signed(private)
    evidence[field] = value  # original signature no longer matches payload
    with pytest.raises(QualificationError):
        store.qualify("a1",evidence,observed_ms=1500)
    assert store.read("a1")["state"]=="UNKNOWN"

def test_well_signed_cross_attempt_nonce_still_rejected(tmp_path):
    store, private = configured(tmp_path)
    with pytest.raises(QualificationError,match="binding mismatch"):
        store.qualify("a1",signed(private,nonce="ffffffffffffffff"),observed_ms=1500)
    assert store.read("a1")["state"]=="UNKNOWN"

@pytest.mark.parametrize("now", [999,2001])
def test_time_outside_signed_validity_rejected(tmp_path,now):
    store,private=configured(tmp_path,now=now)
    with pytest.raises(QualificationError,match="expired, premature"):
        store.qualify("a1",signed(private),observed_ms=1500)

def test_revoked_signer_rejected(tmp_path):
    store,private=configured(tmp_path,revoked=frozenset({"trusted-authority"}))
    with pytest.raises(QualificationError,match="verification rejected"):
        store.qualify("a1",signed(private),observed_ms=1500)

def test_other_signer_cannot_substitute_signature(tmp_path):
    store,_=configured(tmp_path)
    attacker = Ed25519PrivateKey.generate()
    with pytest.raises(QualificationError,match="verification rejected"):
        store.qualify("a1",signed(attacker),observed_ms=1500)

def test_unknown_fields_are_rejected(tmp_path):
    store,private=configured(tmp_path)
    proof=signed(private)
    proof["debug_override"]=True
    with pytest.raises(QualificationError):
        store.qualify("a1",proof,observed_ms=1500)

def test_boolean_clock_rejected(tmp_path):
    private=Ed25519PrivateKey.generate()
    public=private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,format=serialization.PublicFormat.Raw)
    verifier=Ed25519AttemptVerifier({"trusted-authority":public},now_ms=lambda:True)
    with pytest.raises(QualificationError,match="clock"):
        verifier(signed(private))

def test_oversized_ttl_even_valid_signature_is_rejected(tmp_path):
    store,private=configured(tmp_path)
    proof=signed(private,expires_at_ms=100000)
    with pytest.raises(QualificationError,match="TTL"):
        store.qualify("a1",proof,observed_ms=1500)
