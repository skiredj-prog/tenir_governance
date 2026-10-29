"""Nonce, outbox and claim contract on the integrated PR #85 head."""
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
        assert provider.execute_count("op-direct") == 0
        provider.conn.close()


def test_private_key_never_enters_authority_sqlite():
    with tempfile.TemporaryDirectory() as d:
        backend = SQLiteConsensusBackend(d)
        os.environ["TENIR_W2_PRIVATE_KEY_HEX"] = "11" * 32
        try:
            auth = NativePositivePermitAuthority(backend)
        finally:
            os.environ.pop("TENIR_W2_PRIVATE_KEY_HEX", None)
        with backend._connection() as db:
            assert db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='w2_positive_v1_keys'"
            ).fetchone() is None
        assert auth.key_custody == "ENV_INJECTED"
        assert auth.writers()[0]["production_allowed"] is False


if __name__ == "__main__":
    test_exact_replay_returns_same_execution_without_reexecution()
    test_replay_mismatch_is_rejected()
    test_provider_requires_kernel_claim_when_enabled()
    test_private_key_never_enters_authority_sqlite()
    print("nonce/claim contract: PASS")
