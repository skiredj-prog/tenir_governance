"""EP-02 signed trust anchors and fail-closed evaluation binding.

Experimental only. Caller must securely provision a root Ed25519 public key.
Signed snapshots bind revision, policy, mandates and revocations. This is NOT
a consensus trust registry nor an atomic execution authorization.
"""
from dataclasses import dataclass
from cryptography.hazmat.primitives import serialization
import json
import re
from typing import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from tenir_governance.experimental.ep02_binding import TrustState, BoundFinding, bind, revalidate
from tenir_governance.experimental.ep01_evidence import SignedClaim, verify_and_evaluate
from tenir_governance.experimental.ep01_policy import Verdict


_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def _validated_trust_state(state: TrustState) -> TrustState:
    if not isinstance(state, TrustState):
        raise TypeError("expected TrustState")
    if type(state.revision) is not int or state.revision < 1:
        raise ValueError("invalid trust revision")
    if not isinstance(state.policy_version, str) or not state.policy_version:
        raise ValueError("invalid policy version")
    if not isinstance(state.mandates, tuple) or any(
        not isinstance(item, tuple)
        or len(item) != 2
        or not all(isinstance(value, str) and value for value in item)
        for item in state.mandates
    ):
        raise ValueError("invalid trust mandate")
    if len(set(state.mandates)) != len(state.mandates):
        raise ValueError("duplicate trust mandate")
    if not isinstance(state.revoked, tuple) or any(
        not isinstance(value, str) or not value for value in state.revoked
    ):
        raise ValueError("invalid trust revocation")
    if len(set(state.revoked)) != len(state.revoked):
        raise ValueError("duplicate trust revocation")
    return state


def canonical_trust_bytes(state: TrustState) -> bytes:
    state = _validated_trust_state(state)
    return json.dumps(
        {"revision": state.revision, "policy_version": state.policy_version,
         "mandates": sorted([list(x) for x in state.mandates]),
         "revoked": sorted(state.revoked)},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


@dataclass(frozen=True)
class SignedTrustState:
    state: TrustState
    signature: bytes
    signer_id: str
    issuer_keys: tuple[tuple[str, bytes], ...] = ()


def verify_trust_snapshot(
    signed: SignedTrustState, *, root_keys: Mapping[str, Ed25519PublicKey],
    minimum_revision: int = 1,
) -> bool:
    """Authenticate signed snapshot with a caller-provisioned root key.

    minimum_revision must be supplied from independently durable state;
    this function alone cannot prevent rollback to an older valid snapshot.
    """
    if (
        not isinstance(signed, SignedTrustState)
        or type(minimum_revision) is not int
        or minimum_revision < 1
        or not isinstance(root_keys, Mapping)
        or not isinstance(signed.signer_id, str)
        or not _IDENTITY.fullmatch(signed.signer_id)
        or not isinstance(signed.signature, bytes)
        or len(signed.signature) != 64
    ):
        return False
    try:
        state = _validated_trust_state(signed.state)
    except (TypeError, ValueError):
        return False
    key = root_keys.get(signed.signer_id)
    if not isinstance(key, Ed25519PublicKey):
        return False
    if state.revision < minimum_revision:
        return False
    try:
        key.verify(signed.signature, canonical_trust_bytes(signed.state))
    except (InvalidSignature, TypeError, ValueError):
        return False
    return True


def verified_preflight(
    claim: SignedClaim, *, prior: str, scope: str, now,
    trust: SignedTrustState, root_keys: Mapping[str, Ed25519PublicKey],
    issuer_keys: Mapping[str, Ed25519PublicKey],
    minimum_revision: int = 1,
    allow_legacy_unbound_issuer_keys: bool = False,
) -> tuple[Verdict, str, BoundFinding | None]:
    if allow_legacy_unbound_issuer_keys is not True:
        return Verdict.REJECT, "LEGACY_UNBOUND_ISSUER_KEYS_DISABLED", None
    if not verify_trust_snapshot(trust, root_keys=root_keys,
                                 minimum_revision=minimum_revision):
        return Verdict.REJECT, "TRUST_SNAPSHOT_UNVERIFIED", None
    issuer = claim.claim.issuer
    if issuer in trust.state.revoked:
        return Verdict.REJECT, "ISSUER_REVOKED", None
    verdict, reason = verify_and_evaluate(
        (claim,), prior=prior, scope=scope,
        keys=issuer_keys,
        mandates={key: frozenset(s for i, s in trust.state.mandates if i == key)
                  for key, _ in trust.state.mandates},
        now=now,
    )
    if verdict != Verdict.ADMISSIBLE:
        return verdict, reason, None
    from hashlib import sha256
    from tenir_governance.experimental.ep01_evidence import canonical_claim_bytes
    digest = sha256(canonical_claim_bytes(claim.claim) + claim.signature).hexdigest()
    receipt = bind(trust=trust.state, issuer=issuer, scope=scope,
                   prior_document=prior, claim_digest=digest)
    if receipt is None:
        return Verdict.REJECT, "TRUST_SCOPE_REJECTED", None
    return Verdict.ADMISSIBLE, "VERIFIED_PREFLIGHT_ONLY", receipt


def verified_revalidation(
    receipt: BoundFinding, claim: SignedClaim, *, prior: str, scope: str,
    now, current: SignedTrustState,
    root_keys: Mapping[str, Ed25519PublicKey],
    issuer_keys: Mapping[str, Ed25519PublicKey],
    minimum_revision: int,
    allow_legacy_unbound_issuer_keys: bool = False,
) -> tuple[Verdict, str]:
    verdict, reason, current_receipt = verified_preflight(
        claim, prior=prior, scope=scope, now=now, trust=current,
        root_keys=root_keys, issuer_keys=issuer_keys,
        minimum_revision=minimum_revision,
        allow_legacy_unbound_issuer_keys=allow_legacy_unbound_issuer_keys,
    )
    if verdict != Verdict.ADMISSIBLE or current_receipt is None:
        return verdict, reason
    if not revalidate(receipt, current=current.state,
                      claim_digest=current_receipt.claim_digest,
                      prior_document=prior, scope=scope):
        return Verdict.HOLD, "TRUST_OR_CLAIM_CHANGED"
    return Verdict.ADMISSIBLE, "PREFLIGHT_STILL_VALID_NOT_COMMITTED"

def bound_snapshot_bytes(signed: SignedTrustState) -> bytes:
    """Domain-separated signature payload covering issuer keys and mandates."""
    entries = list(signed.issuer_keys)
    if len({name for name, _ in entries}) != len(entries):
        raise ValueError("duplicate issuer key")
    if any(not isinstance(name, str) or not _IDENTITY.fullmatch(name)
           or not isinstance(raw, bytes) or len(raw) != 32
           for name, raw in entries):
        raise ValueError("invalid issuer key entry")
    body = {"trust": json.loads(canonical_trust_bytes(signed.state)),
            "issuer_keys": [[name, raw.hex()] for name, raw in sorted(entries)]}
    return b"TENIR-EP02-SNAPSHOT-V2\\x00" + json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def verify_bound_snapshot(
    signed: SignedTrustState, *, root_keys: Mapping[str, Ed25519PublicKey],
    minimum_revision: int = 1,
) -> bool:
    if (
        not isinstance(signed, SignedTrustState)
        or type(minimum_revision) is not int
        or minimum_revision < 1
        or not isinstance(root_keys, Mapping)
        or not isinstance(signed.signer_id, str)
        or not _IDENTITY.fullmatch(signed.signer_id)
        or not isinstance(signed.signature, bytes)
        or len(signed.signature) != 64
    ):
        return False
    try:
        state = _validated_trust_state(signed.state)
    except (TypeError, ValueError):
        return False
    if state.revision < minimum_revision:
        return False
    root = root_keys.get(signed.signer_id)
    if not isinstance(root, Ed25519PublicKey):
        return False
    try:
        root.verify(signed.signature, bound_snapshot_bytes(signed))
    except (InvalidSignature, TypeError, ValueError):
        return False
    return True


def verified_bound_preflight(
    claim: SignedClaim, *, prior: str, scope: str, now,
    trust: SignedTrustState, root_keys: Mapping[str, Ed25519PublicKey],
    minimum_revision: int = 1,
) -> tuple[Verdict, str, BoundFinding | None]:
    """Only keys authenticated within the signed trust snapshot can verify claims."""
    if not verify_bound_snapshot(trust, root_keys=root_keys,
                                 minimum_revision=minimum_revision):
        return Verdict.REJECT, "TRUST_SNAPSHOT_UNVERIFIED", None
    keys = {issuer: Ed25519PublicKey.from_public_bytes(raw)
            for issuer, raw in trust.issuer_keys}
    issuer = claim.claim.issuer
    if issuer in trust.state.revoked:
        return Verdict.REJECT, "ISSUER_REVOKED", None
    if issuer not in keys:
        return Verdict.REJECT, "ISSUER_KEY_NOT_BOUND", None
    verdict, reason = verify_and_evaluate(
        (claim,), prior=prior, scope=scope, keys=keys,
        mandates={name: frozenset(s for i, s in trust.state.mandates if i == name)
                  for name, _ in trust.state.mandates}, now=now)
    if verdict != Verdict.ADMISSIBLE:
        return verdict, reason, None
    from hashlib import sha256
    from tenir_governance.experimental.ep01_evidence import canonical_claim_bytes
    digest = sha256(canonical_claim_bytes(claim.claim) + claim.signature).hexdigest()
    receipt = bind(trust=trust.state, issuer=issuer, scope=scope,
                   prior_document=prior, claim_digest=digest)
    from dataclasses import replace
    if receipt is None:
        return Verdict.REJECT, "TRUST_SCOPE_REJECTED", None
    receipt = replace(receipt, signed_snapshot_digest=sha256(bound_snapshot_bytes(trust)).hexdigest())
    return Verdict.ADMISSIBLE, "BOUND_PREFLIGHT_NOT_COMMITTED", receipt


def revalidate_bound(
    receipt: BoundFinding, claim: SignedClaim, *, prior: str, scope: str, now,
    current: SignedTrustState, root_keys: Mapping[str, Ed25519PublicKey],
    minimum_revision: int,
) -> tuple[Verdict, str]:
    verdict, reason, fresh = verified_bound_preflight(
        claim, prior=prior, scope=scope, now=now, trust=current,
        root_keys=root_keys, minimum_revision=minimum_revision)
    if verdict != Verdict.ADMISSIBLE or fresh is None:
        return verdict, reason
    if (not revalidate(receipt, current=current.state,
                       claim_digest=fresh.claim_digest,
                       prior_document=prior, scope=scope)
        or receipt.signed_snapshot_digest != fresh.signed_snapshot_digest):
        return Verdict.HOLD, "TRUST_OR_CLAIM_CHANGED"
    return Verdict.ADMISSIBLE, "BOUND_PREFLIGHT_VALID_NOT_COMMITTED"

