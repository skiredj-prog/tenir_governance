"""Agent C's model-level adversarial oracles. NOT native/production certification."""
import copy
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from audit_tests.agent_c_anchor_reference import (
    AnchorConflict, Checkpoint, ProducerSideVerifier, ReferenceRemoteAuthority, digest,
)

S = "jw-lab-subject"
TOPO = "lab-s1-native-sqlite-single-host-2026-10-28"

def setup():
    authority = ReferenceRemoteAuthority()
    original = Checkpoint(S, 0, digest("bootstrap"), TOPO)
    authority.bootstrap(original)
    verifier = ProducerSideVerifier(authority.public_bytes, S, TOPO)
    return authority, verifier, original

def advance(authority, old, *, request="req-1"):
    next_cp = Checkpoint(S, old.revision + 1, digest(f"state-{old.revision + 1}"), TOPO)
    authority.advance(next_cp, expected_revision=old.revision, request_id=request)
    return next_cp

def observe(authority, verifier, cp, challenge="different-nonce-01"):
    return verifier.assess(cp, authority.observe(S, challenge), challenge)

def test_reference_happy_path_matches_without_authorizing_dispatch():
    a, v, x = setup(); y = advance(a, x)
    assert observe(a, v, y) == "MATCHED_REFERENCE_ONLY"

def test_joint_kernel_witness_rollback_detected_if_anchor_is_not_restored():
    a, v, old = setup(); new = advance(a, old)
    assert new.revision == 1
    assert observe(a, v, old) == "HOLD_LOCAL_ROLLBACK"

def test_equal_revision_wrong_digest_is_fork_not_verified():
    a, v, old = setup(); new = advance(a, old)
    assert observe(a, v, Checkpoint(S, new.revision, digest("different"), TOPO)) == "HOLD_FORK"

def test_local_ahead_anchor_requires_reconciliation_no_dispatch():
    a, v, old = setup()
    local = Checkpoint(S, 1, digest("local-unanchored"), TOPO)
    assert observe(a, v, local) == "HOLD_ANCHOR_BEHIND"

def test_remote_outage_blocks_verification():
    a, v, old = setup()
    assert v.assess(old, None, "fresh-nonce-00001") == "HOLD_ANCHOR_UNAVAILABLE"

def test_replayed_observation_wrong_challenge_rejected():
    a, v, old = setup()
    stale = a.observe(S, "fresh-nonce-00001")
    assert v.assess(old, stale, "fresh-nonce-00002") == "HOLD_WRONG_AUTHORITY_OR_CHALLENGE"

def test_observation_signer_substitution_rejected():
    a, v, old = setup()
    fake = ReferenceRemoteAuthority()
    fake.bootstrap(old)
    assert v.assess(old, fake.observe(S, "fresh-nonce-00001"), "fresh-nonce-00001") == "HOLD_INVALID_OBSERVATION"

def test_signature_mutation_rejected():
    a, v, old = setup()
    forged = copy.deepcopy(a.observe(S, "fresh-nonce-00001"))
    forged["payload"]["checkpoint"]["digest"] = digest("tampered")
    assert v.assess(old, forged, "fresh-nonce-00001") == "HOLD_INVALID_OBSERVATION"

def test_anchor_refuses_same_request_id_with_different_payload():
    a, v, old = setup(); advance(a, old)
    forged = Checkpoint(S, 1, digest("different"), TOPO)
    with pytest.raises(AnchorConflict, match="IDEMPOTENCY_CONTEXT_CONFLICT"):
        a.advance(forged, expected_revision=0, request_id="req-1")

def test_anchor_exact_retry_returns_same_receipt():
    a, v, old = setup(); n = advance(a, old)
    a1 = a.advance(n, expected_revision=0, request_id="req-1")
    a2 = a.advance(n, expected_revision=0, request_id="req-1")
    assert a1 == a2

def test_anchor_rejects_stale_cas_and_revision_jump():
    a, v, old = setup(); advance(a, old)
    with pytest.raises(AnchorConflict, match="STALE_OR_CROSS_TOPOLOGY"):
        a.advance(Checkpoint(S, 2, digest("2"), TOPO), expected_revision=0, request_id="req-2")
    with pytest.raises(AnchorConflict, match="STALE_OR_CROSS_TOPOLOGY"):
        a.advance(Checkpoint(S, 3, digest("3"), TOPO), expected_revision=1, request_id="req-3")

def test_anchor_rejects_foreign_topology_and_subject():
    a, v, old = setup()
    with pytest.raises(AnchorConflict, match="STALE_OR_CROSS_TOPOLOGY"):
        a.advance(Checkpoint(S, 1, digest("x"), "other-topology"), expected_revision=0, request_id="req-x")
    with pytest.raises(AnchorConflict, match="UNKNOWN_SUBJECT"):
        a.advance(Checkpoint("other", 1, digest("x"), TOPO), expected_revision=0, request_id="req-y")

def test_producer_verifier_has_only_public_key_no_signing_authority():
    a, v, old = setup()
    assert not hasattr(v, "private_key")
    assert not hasattr(v, "sign")
    assert len(a.public_bytes) == 32

def test_an_old_observation_must_not_be_reused_for_a_newer_local_state():
    a, v, old = setup()
    old_obs = a.observe(S, "fresh-nonce-00001")
    newer = advance(a, old)
    assert v.assess(newer, old_obs, "fresh-nonce-00002") == "HOLD_WRONG_AUTHORITY_OR_CHALLENGE"

def test_remote_anchor_reset_is_out_of_scope_not_proven_by_reference_model():
    a, v, old = setup()
    advance(a, old)
    reborn = ReferenceRemoteAuthority()
    reborn.bootstrap(old)
    assert v.assess(old, reborn.observe(S, "fresh-nonce-00001"), "fresh-nonce-00001") == "HOLD_INVALID_OBSERVATION"


def test_rollback_of_anchor_and_preserved_signing_key_is_undetectable_in_reference_model():
    """EXPECTED LIMIT: signatures do not prove recency if the authority itself rolls back."""
    root = Ed25519PrivateKey.generate()
    authority = ReferenceRemoteAuthority(root)
    old = Checkpoint(S, 0, digest("bootstrap"), TOPO)
    authority.bootstrap(old)
    verifier = ProducerSideVerifier(authority.public_bytes, S, TOPO)
    advance(authority, old)
    restarted = ReferenceRemoteAuthority(root)
    restarted.bootstrap(old)
    challenge = "fresh-nonce-00001"
    assert verifier.assess(old, restarted.observe(S, challenge), challenge) == "MATCHED_REFERENCE_ONLY"
    # This is an EXPECTED SECURITY LIMIT, not evidence of actual nonrollbackability.
