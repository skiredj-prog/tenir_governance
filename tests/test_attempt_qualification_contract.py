"""Conservative T6/T7 candidate contract tests, isolated from dispatch."""
import pytest
from tenir_governance.operations.attempt_qualification import (
    AttemptQualificationStore, AttemptState, VerifiedEvidence, QualificationError,
)

def verifier(raw):
    if not raw.get("verified"):
        raise ValueError("unsigned or unverified")
    return VerifiedEvidence(
        raw["evidence_id"],raw["attempt_id"],raw["lei"],
        raw["outcome"],"independent-attestor",raw["receipt"],
    )

def setup(tmp_path):
    store = AttemptQualificationStore(tmp_path/"attempts.sqlite", verifier)
    store.begin(attempt_id="a1",lei="L1",deadline_ms=100)
    return store

def evidence(outcome="EFFECT_CONFIRMED", **kw):
    return dict(dict(verified=True,evidence_id="e1",attempt_id="a1",lei="L1",
                outcome=outcome,receipt="verified-receipt"),**kw)

def test_t6_qualified_resolution_persists_after_restart(tmp_path):
    s=setup(tmp_path)
    assert s.qualify("a1",evidence(),observed_ms=90)=="RESOLVED"
    assert AttemptQualificationStore(tmp_path/"attempts.sqlite",verifier).read("a1")["state"]=="RESOLVED"
    assert s.qualify("a1",evidence(),observed_ms=91)=="RESOLVED"
    assert [x[0] for x in s.events("a1")].count("RESOLVED")==1

def test_t7_independently_qualified_non_execution(tmp_path):
    s=setup(tmp_path)
    assert s.qualify("a1",evidence("NON_EXECUTION_CONFIRMED"),observed_ms=110)=="FAILED"
    assert s.read("a1")["outcome"]=="NON_EXECUTION_CONFIRMED"
    assert s.read("a1")["verification_receipt"]=="verified-receipt"

def test_timeout_never_proves_failure_or_releases_lei(tmp_path):
    s=setup(tmp_path)
    assert s.timeout("a1",observed_ms=100)=="UNKNOWN"
    assert s.read("a1")["state"]=="UNKNOWN"
    with pytest.raises(QualificationError,match="LEI already bound"):
        s.begin(attempt_id="a2",lei="L1",deadline_ms=200)

def test_unverified_attestation_does_not_mutate(tmp_path):
    s=setup(tmp_path)
    with pytest.raises(QualificationError):
        s.qualify("a1",dict(evidence(),verified=False),observed_ms=101)
    assert s.read("a1")["state"]=="UNKNOWN"

def test_cross_attempt_substitution_rejected(tmp_path):
    s=setup(tmp_path)
    with pytest.raises(QualificationError,match="binding mismatch"):
        s.qualify("a1",dict(evidence(),attempt_id="a2"),observed_ms=99)
    assert s.read("a1")["state"]=="UNKNOWN"

def test_contradictory_terminal_claim_rejected(tmp_path):
    s=setup(tmp_path)
    s.qualify("a1",evidence(),observed_ms=90)
    with pytest.raises(QualificationError,match="terminal state"):
        s.qualify("a1",evidence("NON_EXECUTION_CONFIRMED"),observed_ms=110)
    assert s.read("a1")["state"]=="RESOLVED"

def test_expired_attempt_may_still_be_resolved_by_evidence(tmp_path):
    s=setup(tmp_path)
    assert s.timeout("a1",observed_ms=150)=="UNKNOWN"
    assert s.qualify("a1",evidence(),observed_ms=160)=="RESOLVED"

def test_verifier_return_type_is_not_caller_boolean(tmp_path):
    s=AttemptQualificationStore(tmp_path/"attempts.sqlite",lambda _: True)
    s.begin(attempt_id="a1",lei="L1",deadline_ms=100)
    with pytest.raises(QualificationError,match="VerifiedEvidence"):
        s.qualify("a1",evidence(),observed_ms=99)

def test_t8_new_attempt_denied_even_after_terminal_without_authority(tmp_path):
    s=setup(tmp_path)
    s.qualify("a1",evidence("NON_EXECUTION_CONFIRMED"),observed_ms=99)
    with pytest.raises(QualificationError, match="T8 retry authorization"):
        s.begin(attempt_id="a2",lei="L1",deadline_ms=200)
    # T8 is unimplemented; terminal evidence does not grant retry authority.
