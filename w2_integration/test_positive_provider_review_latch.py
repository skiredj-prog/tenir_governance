"""Laboratory containment after caught provider failures; not crash fencing."""
from copy import deepcopy

import pytest

from kernel_claim_provider import KernelClaimProofProvider
from native_positive_g4a import NativePositiveG4A
from native_positive_permit import NativePositivePermitAuthority
from proof_provider import AckLost
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend

OP = "lab:review-provider"
KEY = "lab:review-provider-idem"
RESOURCE = "lab:budget"
COMMAND = {"schema": "tenir-dispatch-command/v1", "resource_id": RESOURCE,
           "amount": 7, "action_type": "payment", "agent_id": "lab:agent",
           "metadata": {"recipient": "lab:recipient"}}


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())
    monkeypatch.setenv("TENIR_B3_PRIVATE_KEY_HEX", bytes(range(64, 96)).hex())
    backend = SQLiteConsensusBackend(str(tmp_path))
    assert backend.register_resource(RESOURCE, 50) == "REGISTERED"
    authority = NativePositivePermitAuthority(backend)
    gate = NativePositiveG4A(authority)
    provider = KernelClaimProofProvider(db_path=str(tmp_path / "provider.db"),
                                       claim_verifier=authority.verify_dispatch_claim)
    yield backend, authority, gate, provider
    provider.conn.close()


@pytest.mark.parametrize("failure", ["before_effect", "lost_ack", "invalid_receipt"])
def test_caught_provider_failure_latches_review_and_blocks_direct_replay(system, monkeypatch, failure):
    backend, authority, gate, provider = system
    grant = gate.evaluate_and_grant(OP, "lab:eval", COMMAND, resource_id=RESOURCE)
    original = provider.execute
    def fail(*args, **kwargs):
        if failure == "before_effect":
            raise RuntimeError("injected before effect")
        if failure == "lost_ack":
            return original(*args, lose_ack=True, **kwargs)
        receipt = original(*args, **kwargs)
        return {**receipt, "request_hash": "different"}
    monkeypatch.setattr(provider, "execute", fail)
    with pytest.raises((RuntimeError, AckLost)):
        gate.dispatch_to_b3(OP, KEY, deepcopy(COMMAND), grant, provider)
    expected_effects = 0 if failure == "before_effect" else 1
    assert provider.execute_count(OP) == expected_effects
    assert authority.requires_manual_review(OP)
    with backend._connection() as db:
        assert db.execute("SELECT status FROM w2_positive_v1_outbox WHERE operation_key=?", (OP,)).fetchone()[0] == "UNKNOWN"
        assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_events WHERE operation_key=? AND event='dispatch_review'", (OP,)).fetchone()[0] == 1
    monkeypatch.setattr(provider, "execute", original)
    claim = authority.consume_and_claim(OP, deepcopy(COMMAND), KEY)
    assert claim["replayed"] and claim["status"] == "UNKNOWN"
    assert not authority.verify_dispatch_claim(OP, KEY, COMMAND, claim)
    reopened = NativePositivePermitAuthority(SQLiteConsensusBackend(backend._store_dir))
    assert reopened.requires_manual_review(OP)
    assert not reopened.verify_dispatch_claim(OP, KEY, COMMAND, claim)
    with pytest.raises(RuntimeError, match="claim"):
        provider.execute(OP, KEY, deepcopy(COMMAND), claim=claim)
    assert provider.execute_count(OP) == expected_effects
    assert backend.get_state(RESOURCE).reserved == 7
