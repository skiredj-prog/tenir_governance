"""Protocol tests only; the OS isolation test runs separately on Linux.

``B11_TEST_MUTANT`` is read only by this test module.  It replaces the
retirement guard with the minimal historical native-only dispatch for one
isolated run, so the boundary oracle must observe a provider request and fail.
It is never an application configuration.
"""
import os

import httpx
import pytest

from kernel_claim_provider import KernelClaimProofProvider
from native_positive_g4a import NativePositiveG4A
from native_positive_permit import NativePositivePermitAuthority
from positive_uid_service import ExperimentalPositiveUidService
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from w2_integration.native_dispatch_command import snapshot_command


MUTANT = os.environ.get("B11_TEST_MUTANT") == "1"


def _state(backend):
    """Capture every native admission/send table this endpoint could mutate."""
    tables = (
        "resources",
        "reservations",
        "w2_positive_v1_capacity",
        "w2_positive_v1_permits",
        "w2_positive_v1_outbox",
        "w2_positive_v1_events",
        "w2_positive_g4a_v1_grants",
    )
    with backend._connection() as db:
        present = {
            row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        return {
            table: [tuple(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY rowid")]
            for table in tables if table in present
        }


def _unsafe_historical_native_dispatch(service, obj):
    """Test-only mutation approximating the retired native-only send path.

    This deliberately omits Governor and independent-approval proof.  Keeping
    it here (rather than in application code) lets the mutation campaign prove
    that the external-effect oracle is load-bearing.
    """
    command = snapshot_command(obj["command"])
    grant = service.gate.evaluate_and_grant(
        obj["operation_key"], obj["evaluation_id"], command,
        resource_id=command["resource_id"],
    )
    return service.gate.dispatch_to_b3(
        obj["operation_key"], obj["idempotency_key"], command, grant,
        service.provider,
    )


def test_legacy_native_uid_endpoint_refuses_before_any_effect(tmp_path, monkeypatch):
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
        observed_requests = []

        def observe_effect(request):
            observed_requests.append((request.method, request.url.path))
            return httpx.Response(200, json={"synthetic": True})

        with httpx.Client(transport=httpx.MockTransport(observe_effect), trust_env=False) as client:
            original_execute = service.provider.execute

            def observed_execute(*args, **kwargs):
                # This observer is outside all TENIR SQLite state.  It fires at
                # the synthetic provider boundary immediately before its effect.
                client.post("https://provider.invalid/b11-native-uid-effect")
                return original_execute(*args, **kwargs)

            monkeypatch.setattr(service.provider, "execute", observed_execute)
            if MUTANT:
                monkeypatch.setattr(
                    ExperimentalPositiveUidService, "_dispatch", _unsafe_historical_native_dispatch,
                )
            before = _state(backend)
            refused = False
            try:
                service._dispatch(req)
            except RuntimeError as exc:
                assert "legacy native UID service disabled" in str(exc)
                refused = True
            # The independent observer must be checked before state owned by
            # TENIR.  The controlled mutant turns this assertion red.
            assert observed_requests == [], "provider transport was called"
            assert _state(backend) == before, "denied request changed I8 or pending-send state"
            assert refused, "legacy native UID endpoint did not refuse"

        with pytest.raises(RuntimeError, match="legacy native UID service disabled"):
            service._dispatch(req)
        with pytest.raises(RuntimeError, match="legacy native UID service disabled"):
            service._dispatch({**req, "command": {**command, "amount": 8}})
        with pytest.raises(RuntimeError, match="legacy native UID service disabled"):
            service._dispatch({**req, "operation_key": "lab:other"})
        assert backend.get_state("lab:budget").reserved == 0
        with pytest.raises(RuntimeError, match="legacy native UID service disabled"):
            service._dispatch({**req, "action": "execute"})
        assert service.provider.execute_count("lab:op") == 0
        with backend._connection() as db:
            assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_outbox").fetchone()[0] == 0
    finally:
        service.provider.conn.close()
