"""Protocol tests only; the OS isolation test runs separately on Linux."""
import pytest

from kernel_claim_provider import KernelClaimProofProvider
from native_positive_g4a import NativePositiveG4A
from native_positive_permit import NativePositivePermitAuthority
from positive_uid_service import ExperimentalPositiveUidService
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend


def test_protocol_never_returns_claim_and_replay_does_not_send_again(tmp_path, monkeypatch):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())
    monkeypatch.setenv("TENIR_B3_PRIVATE_KEY_HEX", bytes(range(64, 96)).hex())
    backend = SQLiteConsensusBackend(str(tmp_path))
    assert backend.register_resource("lab:budget", 50) == "REGISTERED"
    service = object.__new__(ExperimentalPositiveUidService)
    service.authority = NativePositivePermitAuthority(backend)
    service.gate = NativePositiveG4A(service.authority)
    service.provider = KernelClaimProofProvider(claim_verifier=service.authority.verify_dispatch_claim)
    command = {"schema": "tenir-dispatch-command/v1", "resource_id": "lab:budget", "amount": 7,
               "action_type": "payment", "agent_id": "lab:agent", "metadata": {"recipient": "lab"}}
    req = {"schema": "tenir.positive.uid-lab.v1", "action": "submit", "operation_key": "lab:op",
           "evaluation_id": "lab:eval", "idempotency_key": "lab:idem", "command": command}
    try:
        assert service._dispatch(req) == {"status": "EXECUTED", "synthetic": True, "execution_authorized": False}
        assert service._dispatch(req) == {"status": "UNKNOWN", "synthetic": True, "execution_authorized": False}
        with pytest.raises(ValueError, match="identity conflict"):
            service._dispatch({**req, "command": {**command, "amount": 8}})
        with pytest.raises(ValueError, match="identity conflict"):
            service._dispatch({**req, "operation_key": "lab:other"})
        assert backend.get_state("lab:budget").reserved == 7
        with pytest.raises(ValueError, match="unsupported"):
            service._dispatch({**req, "action": "execute"})
        assert service.provider.execute_count("lab:op") == 1
    finally:
        service.provider.conn.close()
