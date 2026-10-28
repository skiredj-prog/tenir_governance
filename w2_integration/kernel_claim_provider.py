"""Dispatch provider that refuses any effect without a kernel claim.

Direct ProofProvider.execute remains a known bypass. This class does not close B1.
"""
from __future__ import annotations

import sqlite3

from proof_provider import ProofProvider


class KernelClaimProofProvider(ProofProvider):
    def __init__(self, db_path: str = ":memory:", audience: str = "kernel-dispatch"):
        super().__init__(db_path)
        self.audience = audience
        self.conn.execute("CREATE TABLE IF NOT EXISTS claim_nonces (nonce TEXT PRIMARY KEY)")
        self.conn.commit()

    def execute(self, operation_id: str, idempotency_key: str, payload: dict, lose_ack: bool = False, claim=None):
        self._require_claim(operation_id, idempotency_key, claim)
        return super().execute(operation_id, idempotency_key, payload, lose_ack=lose_ack)

    def _require_claim(self, operation_id: str, idempotency_key: str, claim) -> None:
        if not isinstance(claim, dict):
            raise RuntimeError("claim required")
        if claim.get("operation") != operation_id:
            raise RuntimeError("claim operation mismatch")
        if claim.get("idempotency_key") != idempotency_key:
            raise RuntimeError("claim idempotency mismatch")
        if claim.get("audience") != self.audience:
            raise RuntimeError("claim audience mismatch")
        token = claim.get("token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("claim token required")
        nonce = claim.get("nonce")
        if not isinstance(nonce, str) or not nonce:
            raise RuntimeError("claim nonce required")
        try:
            self.conn.execute("INSERT INTO claim_nonces (nonce) VALUES (?)", (nonce,))
            self.conn.commit()
        except sqlite3.IntegrityError as exc:
            raise RuntimeError("claim nonce replay") from exc
