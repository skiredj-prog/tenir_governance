"""Adversarial input and legacy-boundary tests for EP-02 trust paths."""
from datetime import datetime, timezone
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tenir_governance.experimental.ep01_evidence import (
    SignedClaim,
    canonical_claim_bytes,
)
from tenir_governance.experimental.ep01_policy import Claim, Verdict
from tenir_governance.experimental.ep02_authenticated_anchor import (
    anchor_payload,
    new_anchor_challenge,
)
from tenir_governance.experimental.ep02_binding import TrustState
from tenir_governance.experimental.ep02_durable_cas import (
    DurableAnchorCAS,
    StoreIntegrityError,
)
from tenir_governance.experimental.ep02_rotation import TrustCheckpoint
from tenir_governance.experimental.ep02_external_anchor import (
    AnchorObservation,
    AnchorStatus,
    assess_anchor,
)
from tenir_governance.experimental.ep02_signed_trust import (
    SignedTrustState,
    bound_snapshot_bytes,
    canonical_trust_bytes,
    verify_bound_snapshot,
    verify_trust_snapshot,
    verified_preflight,
)


NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)


def _raw_public(key):
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _legacy_fixture():
    root, issuer = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    state = TrustState(1, "policy-v1", (("issuer", "contracts"),))
    trust = SignedTrustState(
        state, root.sign(canonical_trust_bytes(state)), "root",
    )
    claim = Claim("B", "issuer", "A", NOW)
    proof = SignedClaim(claim, issuer.sign(canonical_claim_bytes(claim)))
    return trust, proof, root.public_key(), issuer.public_key()


def test_legacy_v1_preflight_is_fail_closed_without_explicit_acknowledgment():
    trust, proof, root, issuer = _legacy_fixture()
    assert verified_preflight(
        proof, prior="A", scope="contracts", now=NOW, trust=trust,
        root_keys={"root": root}, issuer_keys={"issuer": issuer},
    ) == (Verdict.REJECT, "LEGACY_UNBOUND_ISSUER_KEYS_DISABLED", None)
    accepted = verified_preflight(
        proof, prior="A", scope="contracts", now=NOW, trust=trust,
        root_keys={"root": root}, issuer_keys={"issuer": issuer},
        allow_legacy_unbound_issuer_keys=True,
    )
    assert accepted[0] == Verdict.ADMISSIBLE


@pytest.mark.parametrize("state", [
    TrustState(True, "policy-v1", (("issuer", "contracts"),)),
    TrustState("1", "policy-v1", (("issuer", "contracts"),)),
    TrustState(1, 7, (("issuer", "contracts"),)),
    TrustState(1, "", (("issuer", "contracts"),)),
    TrustState(1, "policy-v1", (("issuer",),)),
    TrustState(1, "policy-v1", (("issuer", "contracts"),) * 2),
    TrustState(1, "policy-v1", (("issuer", "contracts"),), ("issuer", "issuer")),
    TrustState(1, "policy-v1", (("issuer", "contracts"),), (7,)),
])
def test_malformed_trust_state_is_rejected_before_canonicalization(state):
    with pytest.raises((TypeError, ValueError)):
        canonical_trust_bytes(state)


@pytest.mark.parametrize("issuer_keys", [
    (("", b"x" * 32),),
    (("issuer", b"short"),),
    (("issuer", b"x" * 32), ("issuer", b"y" * 32)),
    (("issuer with spaces", b"x" * 32),),
    (("x" * 129, b"x" * 32),),
])
def test_malformed_bound_issuer_keys_are_rejected(issuer_keys):
    state = TrustState(1, "policy-v1", (("issuer", "contracts"),))
    with pytest.raises(ValueError):
        bound_snapshot_bytes(SignedTrustState(state, b"", "root", issuer_keys))


def test_anchor_payload_rejects_bool_revision_uppercase_digest_and_bad_challenge():
    with pytest.raises(ValueError):
        anchor_payload("anchor", TrustCheckpoint(True, "a" * 64), b"q" * 32)
    with pytest.raises(ValueError):
        anchor_payload("anchor", TrustCheckpoint(1, "A" * 64), b"q" * 32)
    with pytest.raises(ValueError):
        anchor_payload("anchor", TrustCheckpoint(1, "a" * 64), bytearray(32))
    challenges = {new_anchor_challenge() for _ in range(32)}
    assert len(challenges) == 32
    assert all(len(challenge) == 32 for challenge in challenges)


def test_verifiers_return_false_for_malformed_types_instead_of_coercing_or_crashing():
    root = Ed25519PrivateKey.generate()
    malformed = SignedTrustState(
        TrustState("1", "policy-v1", (("issuer", "contracts"),)),
        b"x" * 64,
        "root",
    )
    assert not verify_trust_snapshot(malformed, root_keys={"root": root.public_key()})
    assert not verify_bound_snapshot(malformed, root_keys={"root": root.public_key()})
    assert not verify_trust_snapshot(malformed, root_keys=None)
    assert not verify_bound_snapshot(malformed, root_keys=None)


def test_anchor_comparison_rejects_malformed_local_and_remote_checkpoints():
    valid = TrustCheckpoint(1, "a" * 64)
    malformed = TrustCheckpoint(True, "A" * 64)
    assert assess_anchor(
        malformed, AnchorObservation(AnchorStatus.CURRENT, valid),
    ) == (False, "LOCAL_CHECKPOINT_INVALID")
    assert assess_anchor(
        valid, AnchorObservation(AnchorStatus.CURRENT, malformed),
    ) == (False, "ANCHOR_CHECKPOINT_INVALID")


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink and mode semantics")
def test_durable_authority_rejects_symlinked_database_and_sidecar(tmp_path):
    root = Ed25519PrivateKey.generate()
    real = tmp_path / "real.sqlite3"
    store = DurableAnchorCAS.provision(
        real, authority_id="authority", trust_profile_id="roots",
        trust_generation=1, roots={"root": root.public_key()},
    )
    store.close()
    linked = tmp_path / "linked.sqlite3"
    linked.symlink_to(real)
    with pytest.raises(StoreIntegrityError, match="symlink"):
        DurableAnchorCAS.open(linked)

    sidecar_target = tmp_path / "sidecar-target"
    sidecar_target.write_bytes(b"")
    Path(f"{real}-wal").symlink_to(sidecar_target)
    with pytest.raises(StoreIntegrityError, match="symlink"):
        DurableAnchorCAS.open(real)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
def test_provisioned_authority_file_is_not_group_or_world_accessible(tmp_path):
    root = Ed25519PrivateKey.generate()
    path = tmp_path / "authority.sqlite3"
    DurableAnchorCAS.provision(
        path, authority_id="authority", trust_profile_id="roots",
        trust_generation=1, roots={"root": root.public_key()},
    )
    assert path.stat().st_mode & 0o077 == 0
