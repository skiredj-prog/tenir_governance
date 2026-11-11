"""P0 storage compatibility with existing SQLiteConsensusBackend.

Same SQLite database *file* does not mean same transaction or same authority.
An attested attempt MUST NOT silently reserve, commit, release, refund or
settle a real budget reservation.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from tenir_governance.operations.attempt_atomic_candidate import AtomicAttemptCandidateAuthority
from tenir_governance.operations.attempt_attestation import (
    Ed25519AttemptVerifier, canonical_attestation_payload,
)

DIGEST="sha256:"+"ab"*32
NONCE="0123456789abcdef0123456789abcdef"


def _signed(private,outcome):
    data=dict(schema_version="tenir.attempt.attestation.v1",
              evidence_id="budget-proof",attempt_id="attempt-budget",
              lei="LEI-budget",nonce=NONCE,operation_digest=DIGEST,
              outcome=outcome,authority_id="budget-attestor",
              issued_at_ms=1000,expires_at_ms=2000)
    return {**data,"signature_hex":private.sign(canonical_attestation_payload(data)).hex()}


@pytest.mark.parametrize("outcome",["EFFECT_CONFIRMED","NON_EXECUTION_CONFIRMED"])
def test_candidate_coexists_with_budget_sqlite_but_never_settles(tmp_path,outcome):
    dirname=tmp_path/"shared-budget"
    backend=SQLiteConsensusBackend(str(dirname))
    assert backend.register_resource("budget",Decimal("100"))=="REGISTERED"
    reservation=backend.reserve("op-budget",Decimal("20"),"budget")
    assert reservation.status=="RESERVED"
    initial=backend.get_state("budget")
    assert initial.reserved==Decimal("20")
    assert initial.committed==Decimal("0")

    private=Ed25519PrivateKey.generate()
    public=private.public_key().public_bytes(
        serialization.Encoding.Raw,serialization.PublicFormat.Raw)
    candidate=AtomicAttemptCandidateAuthority(
        dirname/"consensus.sqlite3",
        Ed25519AttemptVerifier({"budget-attestor":public},now_ms=lambda:1500),
    )
    candidate.begin(operation_key="op-budget",attempt_id="attempt-budget",
                    lei="LEI-budget",nonce=NONCE,operation_digest=DIGEST,
                    deadline_ms=1400)
    qualified=candidate.qualify(
        "attempt-budget",_signed(private,outcome),observed_ms=1500)
    assert qualified.execution_authorized is False
    assert qualified.is_governor_operation_state is False
    assert qualified.decision=="HOLD"

    reopened=SQLiteConsensusBackend(str(dirname))
    after=reopened.get_state("budget")
    assert after.capacity==initial.capacity
    assert after.reserved==initial.reserved==Decimal("20")
    assert after.committed==initial.committed==Decimal("0")
    matching=[v for v in after.reservations if v["key"]=="op-budget"]
    assert len(matching)==1
    assert matching[0]["state"]=="RESERVED"
    # Even "NON_EXECUTION_CONFIRMED" is not authority to call release().


def test_budget_commit_cannot_turn_candidate_observation_into_grant(tmp_path):
    dirname=tmp_path/"shared-budget"
    backend=SQLiteConsensusBackend(str(dirname))
    assert backend.register_resource("budget",Decimal("100"))=="REGISTERED"
    reservation=backend.reserve("op-budget",Decimal("20"),"budget")
    assert reservation.status=="RESERVED"

    private=Ed25519PrivateKey.generate()
    public=private.public_key().public_bytes(
        serialization.Encoding.Raw,serialization.PublicFormat.Raw)
    candidate=AtomicAttemptCandidateAuthority(
        dirname/"consensus.sqlite3",
        Ed25519AttemptVerifier({"budget-attestor":public},now_ms=lambda:1500),
    )
    candidate.begin(operation_key="op-budget",attempt_id="attempt-budget",
                    lei="LEI-budget",nonce=NONCE,operation_digest=DIGEST,
                    deadline_ms=1400)
    assert backend.commit(reservation.reservation_id).status=="COMMITTED"
    evidence=candidate.qualify(
        "attempt-budget",_signed(private,"NON_EXECUTION_CONFIRMED"),
        observed_ms=1500)
    assert evidence.attempt_state=="FAILED"
    assert evidence.execution_authorized is False
    after=backend.get_state("budget")
    assert after.committed==Decimal("20")
    assert after.reserved==Decimal("0")
    # Candidate has no refund authority; real backend still owns settlement.
