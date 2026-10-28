"""Dispatch only after NativePositivePermitAuthority.verify_claim.

A structurally complete claim is not enough. Direct ProofProvider.execute
without this class remains a bypass and does not close B1.
"""
from __future__ import annotations

from proof_provider import ProofProvider


class KernelClaimProofProvider(ProofProvider):
    def __init__(self, db_path: str, authority, audience: str = "proof-provider"):
        super().__init__(db_path, require_claim=True)
        if authority is None:
            raise TypeError("authority required")
        self.authority = authority
        self.audience = audience

    def execute(self, operation_id: str, idempotency_key: str, payload: dict, lose_ack: bool = False, claim=None):
        if not isinstance(claim, dict):
            raise PermissionError("kernel claim required")
        if not self.authority.verify_claim(
            claim.get("claim_token"),
            claim.get("execution_id"),
            operation_id,
            payload,
            claim.get("audience", self.audience),
            idempotency_key,
        ):
            raise PermissionError("claim not issued by authority")
        before = self.execute_count(operation_id)
        result = super().execute(operation_id, idempotency_key, payload, lose_ack=lose_ack, claim=claim)
        if self.execute_count(operation_id) > before:
            self._confirm(claim.get("execution_id"))
        return result

    def _confirm(self, execution_id: str) -> None:
        with self.authority._write("confirm_outbox") as db:
            db.execute(
                "UPDATE w2_positive_v1_outbox SET status='CONFIRMED' WHERE execution_id=? AND status='PENDING'",
                (execution_id,),
            )
