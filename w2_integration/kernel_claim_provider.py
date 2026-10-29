"""Claim-bound dispatch adapter for the reconstructed laboratory provider.

A native claim verifier is mandatory. Merely supplying fields named token,
nonce, or audience is not authorization.
"""
from __future__ import annotations

import hashlib
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from proof_provider import AckLost, ProofProvider


class KernelClaimProofProvider(ProofProvider):
    def __init__(self, db_path: str = ":memory:", audience: str = "kernel-dispatch",
                 *, claim_verifier=None):
        if claim_verifier is None:
            raise ValueError("native claim verifier required")
        self.audience = audience
        self._native_claim_verifier = claim_verifier
        super().__init__(db_path, claim_verifier=self._verify_native_claim)

    def _verify_native_claim(self, operation_id, idempotency_key, payload, claim) -> bool:
        if not isinstance(claim, dict):
            return False
        if (claim.get("operation") != operation_id
                or claim.get("idempotency_key") != idempotency_key
                or claim.get("audience") != self.audience):
            return False
        token = claim.get("token")
        if not isinstance(token, str) or not token:
            return False
        if claim.get("nonce") != token or claim.get("claim_token") != token:
            return False
        return bool(self._native_claim_verifier(
            operation_id, idempotency_key, payload, claim
        ))

    def execute(self, operation_id: str, idempotency_key: str, payload: dict,
                lose_ack: bool = False, *, claim=None, binding=None):
        return super().execute(
            operation_id, idempotency_key, payload, lose_ack=lose_ack, claim=claim,
            binding=binding,
        )

    def dispatch_authorized(self, authority, operation_id: str, idempotency_key: str,
                            payload: dict, *, lose_ack: bool = False) -> dict:
        """Drive the lab outbox without inventing a second key after uncertainty."""
        provider_key = self.public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
        provider_key_id = hashlib.sha256(provider_key).hexdigest()
        if getattr(authority, "trusted_provider_key_id", None) != provider_key_id:
            raise RuntimeError("provider key is not anchored by native authority")
        claim = authority.consume_and_claim(operation_id, payload, idempotency_key)
        if claim.get("status") == "CONFIRMED":
            receipt = claim.get("receipt")
            if not self.verify_receipt(receipt):
                raise RuntimeError("confirmed provider receipt failed verification")
            return {"status": "CONFIRMED", "claim": claim, "receipt": receipt,
                    "replayed": True}
        try:
            receipt = self.execute(
                operation_id, idempotency_key, payload, lose_ack=lose_ack, claim=claim,
                binding=authority.receipt_context(claim),
            )
        except AckLost:
            status = authority.mark_dispatch_unknown(claim)
            return {"status": status, "claim": claim, "receipt": None,
                    "reason": "provider_ack_lost"}
        except Exception:
            authority.mark_dispatch_unknown(claim)
            raise
        if not self.verify_receipt(receipt):
            authority.mark_dispatch_unknown(claim)
            raise RuntimeError("provider receipt invalid; dispatch retained UNKNOWN")
        authority.mark_dispatch_confirmed(claim, receipt)
        return {"status": "CONFIRMED", "claim": claim, "receipt": receipt,
                "replayed": bool(claim.get("replayed"))}
