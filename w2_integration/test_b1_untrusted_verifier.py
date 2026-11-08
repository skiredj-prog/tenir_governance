"""Negative control for injected provider claim verification, laboratory only."""
import pytest

from kernel_claim_provider import KernelClaimProofProvider
from native_positive_permit import NativePositivePermitAuthority
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend


def test_caller_supplied_true_callback_cannot_authorize_effect():
    # A caller's assertion is not an authority, even with correctly shaped fields.
    with pytest.raises((ValueError, RuntimeError), match="native.*verifier"):
        KernelClaimProofProvider(claim_verifier=lambda *args: True)


def test_replacing_native_callback_after_construction_cannot_authorize_effect(tmp_path):
    authority = NativePositivePermitAuthority(SQLiteConsensusBackend(str(tmp_path)))
    provider = KernelClaimProofProvider(claim_verifier=authority.verify_dispatch_claim)
    try:
        provider._native_claim_verifier = lambda *args: True
        claim = {"operation": "lab", "idempotency_key": "key", "audience": "kernel-dispatch",
                 "token": "x", "nonce": "x", "claim_token": "x"}
        with pytest.raises(RuntimeError, match="claim"):
            provider.execute("lab", "key", {"amount": 1}, claim=claim)
        assert provider.execute_count("lab") == 0
    finally:
        provider.conn.close()
