"""Test-only native fixtures for the historical W2 regression invariants.

No substitute verifier or unclaimed provider mode. Known keys below are public
test fixtures, not deployment credentials. This does not make legacy PositiveG4A
or LAB-S1 an authorized entrypoint.
"""
from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from kernel_claim_provider import KernelClaimProofProvider
from lab_s1_bridge import RC01Lab
from native_positive_permit import NativePositivePermitAuthority
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend


RESOURCE = "lab:w2-regression-budget"
TEST_KEYS = {
    "TENIR_W2_PRIVATE_KEY_HEX": bytes(range(32)).hex(),
    "TENIR_WITNESS_PRIVATE_KEY_HEX": bytes(range(32, 64)).hex(),
    "TENIR_B3_PRIVATE_KEY_HEX": bytes(range(64, 96)).hex(),
}


def command(amount=100, account="a"):
    return {"schema": "tenir-dispatch-command/v1", "resource_id": RESOURCE,
            "amount": amount, "action_type": "transfer", "agent_id": "lab-w2",
            "metadata": {"account": account, "currency": "EUR"}}


@contextmanager
def native_lab(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with patch.dict(os.environ, TEST_KEYS):
        backend = SQLiteConsensusBackend(str(directory))
        backend.register_resource(RESOURCE, 1000000)
        public = Ed25519PrivateKey.from_private_bytes(bytes(range(64, 96))).public_key()
        auth = NativePositivePermitAuthority(
            backend, trusted_provider_public_key=public.public_bytes(Encoding.Raw, PublicFormat.Raw),
        )
        provider = KernelClaimProofProvider(
            db_path=str(directory / "provider.db"), claim_verifier=auth.verify_dispatch_claim,
        )
        try:
            yield SimpleNamespace(backend=backend, auth=auth, provider=provider)
        finally:
            provider.conn.close()


def reservation(lab, operation):
    with lab.backend._connection() as db:
        return dict(db.execute(
            "SELECT * FROM reservations WHERE idempotency_key=?", (operation,),
        ).fetchone())


def event_count(lab, operation, event):
    with lab.backend._connection() as db:
        return db.execute(
            "SELECT COUNT(*) FROM w2_positive_v1_events WHERE operation_key=? AND event=?",
            (operation, event),
        ).fetchone()[0]


def native_s1_scenario(directory, operation):
    """Exercise lost ACK, failed settlement and RC-01 using native claims.

    This is a test composition, not a new production bridge. RC-01 remains a
    separate database and its projection is not claimed to be atomic with I8.
    """
    payload = command()
    with native_lab(directory) as lab, ExitStack() as cleanup:
        rc01 = RC01Lab(str(Path(directory) / "rc01.db"))
        cleanup.callback(rc01.conn.close)
        lab.auth.admit_reserve_issue(operation, "eval-s1", payload, resource_id=RESOURCE)
        rc01.transition(operation, "RESERVED", "native_permit_issued")
        rc01.transition(operation, "DISPATCHING", "native_claim_dispatch")
        unknown = lab.provider.dispatch_authorized(
            lab.auth, operation, "idem-" + operation, payload, lose_ack=True,
        )
        assert unknown["status"] == "UNKNOWN"
        assert lab.provider.execute_count(operation) == 1
        rc01.transition(operation, "UNKNOWN", "ack_lost")
        assert reservation(lab, operation)["state"] == "RESERVED"
        with patch.object(lab.backend, "_commit_in_connection",
                          side_effect=RuntimeError("injected settlement failure")):
            try:
                lab.provider.dispatch_authorized(lab.auth, operation, "idem-" + operation, payload)
            except RuntimeError as exc:
                assert "injected settlement failure" in str(exc)
            else:
                raise AssertionError("settlement fault must be observed")
        rc01.transition(operation, "REPAIR", "settle_failed")
        assert reservation(lab, operation)["state"] == "RESERVED"
        recovered = lab.provider.dispatch_authorized(lab.auth, operation, "idem-" + operation, payload)
        assert recovered["status"] == "CONFIRMED"
        assert lab.provider.verify_receipt(recovered["receipt"])
        rc01.transition(operation, "RECONCILED_CONFIRMED", "authenticated_recovery")
        return {"execute_count": lab.provider.execute_count(operation),
                "settlement_count": event_count(lab, operation, "dispatch_confirmed"),
                "final_state": rc01.get_state(operation), "receipt": recovered["receipt"]}
