"""Acceptance tests for the native approval/nonce/outbox contract."""
import os
import tempfile

from proof_provider import ProofProvider
from native_positive_permit import NativePositivePermitAuthority
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend


def test_exact_replay_returns_same_execution_without_reexecution():
    with tempfile.TemporaryDirectory() as d:
        auth = NativePositivePermitAuthority(SQLiteConsensusBackend(d))
        payload = {"amount": 3, "currency": "EUR"}
        auth.admit_reserve_issue("op-replay", "approval-1", payload)
        first = auth.consume_and_claim("op-replay", payload, "idem-replay")
        second = auth.consume_and_claim("op-replay", payload, "idem-replay")
        assert second["replayed"] is True
        assert second["execution_id"] == first["execution_id"]
        assert second["claim_token"] == first["claim_token"]


def test_replay_mismatch_is_rejected():
    with tempfile.TemporaryDirectory() as d:
        auth = NativePositivePermitAuthority(SQLiteConsensusBackend(d))
        auth.admit_reserve_issue("op-conflict", "approval-1", {"amount": 3})
        auth.consume_and_claim("op-conflict", {"amount": 3}, "idem-conflict")
        try:
            auth.consume_and_claim("op-conflict", {"amount": 4}, "idem-conflict")
        except RuntimeError as exc:
            assert "conflict" in str(exc)
        else:
            raise AssertionError("conflicting replay must be refused")


def test_provider_requires_kernel_claim_when_enabled():
    with tempfile.TemporaryDirectory() as d:
        provider = ProofProvider(os.path.join(d, "provider.db"), require_claim=True)
        for claim in (None, {}):
            try:
                provider.execute("op-direct", "idem-direct", {"amount": 1}, claim=claim)
            except PermissionError as exc:
                assert "claim" in str(exc)
            else:
                raise AssertionError("direct provider call must be refused")
        provider.conn.close()


if __name__ == "__main__":
    test_exact_replay_returns_same_execution_without_reexecution()
    test_replay_mismatch_is_rejected()
    test_provider_requires_kernel_claim_when_enabled()
    print("nonce/claim contract: PASS")
