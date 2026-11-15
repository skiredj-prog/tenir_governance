"""Negative tests for the RV-003 DispatchProvider mapping.

These must pass before any qualification claim.
They do not touch native sources.
"""
import json
from rv003_dispatch_provider import (
    RV003DispatchProvider, ProviderOutcome, DispatchResult, ReconciliationResult,
)


def fake_control_factory(script):
    """script: list of responses keyed by cmd."""
    idx = {"n": 0}
    def control(msg):
        cmd = msg.get("cmd")
        # simple scripted replies
        replies = script.get(cmd, [])
        if idx["n"] < len(replies):
            r = replies[idx["n"]]
            idx["n"] += 1
            return r
        return {"state": "UNKNOWN", "effect_count": 0}
    return control


def test_hold_never_failed():
    ctrl = fake_control_factory({
        "ACCEPT": [{"state": "HOLD_AUTHORITY_UNKNOWN"}],
    })
    p = RV003DispatchProvider(ctrl)
    r = p.execute("k1", "write_test_marker", 0.0, "agent-a", {})
    assert r.outcome != ProviderOutcome.FAILED
    assert r.outcome in (ProviderOutcome.PENDING, ProviderOutcome.UNKNOWN)
    assert "hold_or_ambiguous_not_failed" in r.safety_signals


def test_no_premature_confirmed():
    ctrl = fake_control_factory({
        "ACCEPT": [{"state": "ACCEPTED_PAUSED"}],
        "QUERY_STATUS": [{"state": "ACCEPTED_PAUSED", "effect_count": 0}],
    })
    p = RV003DispatchProvider(ctrl)
    r = p.execute("k2", "write_test_marker", 0.0, "agent-a", {})
    assert r.outcome == ProviderOutcome.PENDING
    recon = p.reconcile("k2")
    assert recon.outcome != ProviderOutcome.CONFIRMED


def test_failed_only_on_terminal_denial():
    ctrl = fake_control_factory({
        "QUERY_STATUS": [{
            "state": "DENIED_REVOKED",
            "effect_count": 0,
            "terminal": True,
            "no_replay_possible": True,
        }],
    })
    p = RV003DispatchProvider(ctrl)
    recon = p.reconcile("k3")
    assert recon.outcome == ProviderOutcome.FAILED
    assert "terminal_non_execution_authoritative" in recon.safety_signals


def test_idempotency_conflict():
    ctrl = fake_control_factory({
        "ACCEPT": [{"state": "ACCEPTED_PAUSED"}],
    })
    p = RV003DispatchProvider(ctrl)
    r1 = p.execute("k4", "tool", 0.0, "a1", {"payload_digest": "aaa"})
    assert r1.outcome == ProviderOutcome.PENDING
    r2 = p.execute("k4", "tool", 0.0, "a1", {"payload_digest": "bbb"})
    assert r2.outcome != ProviderOutcome.FAILED
    assert "request_conflict_original_preserved" in r2.safety_signals
    # original state must still be queryable as PENDING / accepted
    recon = p.reconcile("k4")
    assert recon.outcome in (ProviderOutcome.PENDING, ProviderOutcome.UNKNOWN)


if __name__ == "__main__":
    test_hold_never_failed()
    test_no_premature_confirmed()
    test_failed_only_on_terminal_denial()
    test_idempotency_conflict()
    print("NEGATIVE_TESTS_OK")
