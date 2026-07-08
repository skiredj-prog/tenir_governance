"""
R4 ↔ tenir_governance Integration Test (Sprint 12)
===================================================
Proves that R4's TenirMonitor routes every membrane decision through
the shared tenir_governance.PolicyEngine — and that the shared engine's
rationale lands in the ledger for forensic audit.

This closes parts of G3 (override signatures) and G6 (testament integrity)
from the V4 UAT audit register: every ledger entry now carries
policy_version from the shared engine, and rationale from the canonical
decision path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tenir_v4_test import (
    EventSample,
    OperatingMode,
    PolicyBundle,
    TenirMonitor,
)


# ─── FIXTURES ─────────────────────────────────────────────────────────────────

@pytest.fixture
def bundle() -> PolicyBundle:
    return PolicyBundle()


@pytest.fixture
def monitor(tmp_path: Path, bundle: PolicyBundle) -> TenirMonitor:
    return TenirMonitor(
        policy=bundle,
        ledger_path=tmp_path / "integration.jsonl",
        mode=OperatingMode.SHADOW_PASSIVE,
    )


# ─── TESTS ────────────────────────────────────────────────────────────────────

class TestPolicyBundleBridge:
    """PolicyBundle.to_policy_engine() returns a working shared engine."""

    def test_bridge_returns_policy_engine(self, bundle: PolicyBundle) -> None:
        engine = bundle.to_policy_engine()
        # Duck-type check (no direct import of tenir_governance)
        assert hasattr(engine, "evaluate_membrane")
        assert hasattr(engine, "classify_ces")
        assert hasattr(engine, "validate")

    def test_bridged_engine_validates(self, bundle: PolicyBundle) -> None:
        engine = bundle.to_policy_engine()
        engine.validate()  # must not raise

    def test_bridged_engine_preserves_thresholds(self, bundle: PolicyBundle) -> None:
        engine = bundle.to_policy_engine()
        assert engine.s_alert_floor == bundle.s_alert_floor
        assert engine.s_block_floor == bundle.s_block_floor
        assert engine.epsilon == bundle.epsilon
        assert engine.event_window == bundle.event_window
        assert engine.tau_floor == bundle.tau_floor

    def test_bridged_engine_has_fingerprint(self, bundle: PolicyBundle) -> None:
        engine = bundle.to_policy_engine()
        fp = engine.fingerprint()
        assert isinstance(fp, str) and len(fp) == 16


class TestSharedEngineInLedger:
    """Every R4 observation carries shared-engine metadata to the ledger."""

    def _stable_event(self) -> EventSample:
        return EventSample(pressure=0.3, velocity=0.3, capacity=2.0, option_space=0.8)

    def _stressed_event(self) -> EventSample:
        return EventSample(pressure=1.5, velocity=1.5, capacity=0.70, option_space=0.25)

    def test_ledger_entry_carries_shared_decision(
        self, monitor: TenirMonitor, tmp_path: Path
    ) -> None:
        monitor.observe(self._stressed_event())

        ledger_lines = (tmp_path / "integration.jsonl").read_text().strip().split("\n")
        entry = json.loads(ledger_lines[-1])
        payload = entry.get("payload", entry)
        assert "shared_engine_decision" in payload, (
            "Shared engine decision must appear in every observation ledger entry"
        )

    def test_ledger_entry_carries_rationale(
        self, monitor: TenirMonitor, tmp_path: Path
    ) -> None:
        monitor.observe(self._stressed_event())

        ledger_lines = (tmp_path / "integration.jsonl").read_text().strip().split("\n")
        entry = json.loads(ledger_lines[-1])
        payload = entry.get("payload", entry)
        # Rationale only appears when shared engine produced one (non-allow)
        rationale = payload.get("rationale")
        assert rationale, "Stressed event must carry rationale from shared engine"
        assert isinstance(rationale, str) and rationale.strip()

    def test_shared_engine_decision_matches_r4_intent(
        self, monitor: TenirMonitor, tmp_path: Path
    ) -> None:
        """
        When R4 says allow_with_intended_block, shared engine must agree.
        This is the correctness cross-check that eliminates drift.
        """
        verdict = monitor.observe(self._stressed_event())
        ledger_lines = (tmp_path / "integration.jsonl").read_text().strip().split("\n")
        entry = json.loads(ledger_lines[-1])
        payload = entry.get("payload", entry)
        r4_action = payload.get("action")
        shared_decision = payload.get("shared_engine_decision")

        # Both must be in the same semantic category
        decisions_are_compatible = {
            "allow": {"allow"},
            "allow_with_alert": {"allow_with_alert", "allow"},
            "allow_with_intended_block": {"allow_with_intended_block"},
            "block": {"block"},
            "block_on_alert": {"block", "allow_with_alert"},
        }
        expected_shared = decisions_are_compatible.get(r4_action, set())
        assert shared_decision in expected_shared or shared_decision == r4_action, (
            f"R4 action {r4_action!r} and shared decision {shared_decision!r} disagree"
        )

    def test_policy_version_comes_from_shared_engine(
        self, monitor: TenirMonitor, tmp_path: Path
    ) -> None:
        monitor.observe(self._stable_event())
        ledger_lines = (tmp_path / "integration.jsonl").read_text().strip().split("\n")
        entry = json.loads(ledger_lines[-1])
        payload = entry.get("payload", entry)
        assert payload.get("policy_version") == "partner_a-shadow-v4-test-policy-2"

    def test_stable_event_shared_decision_is_allow(
        self, monitor: TenirMonitor, tmp_path: Path
    ) -> None:
        monitor.observe(self._stable_event())
        ledger_lines = (tmp_path / "integration.jsonl").read_text().strip().split("\n")
        entry = json.loads(ledger_lines[-1])
        payload = entry.get("payload", entry)
        assert payload.get("shared_engine_decision") == "allow"


class TestTauFloorAcrossRuntimes:
    """TAU floor semantics must be consistent between R4 local and shared engine."""

    def test_tau_breach_triggers_shared_engine_block_in_enforce(
        self, tmp_path: Path
    ) -> None:
        bundle = PolicyBundle()
        monitor = TenirMonitor(
            policy=bundle,
            ledger_path=tmp_path / "enforce.jsonl",
            mode=OperatingMode.ENFORCE,
        )
        # TAU-breaching event
        monitor.observe(EventSample(pressure=2.0, velocity=2.0, capacity=0.5, option_space=0.1))

        ledger_lines = (tmp_path / "enforce.jsonl").read_text().strip().split("\n")
        entry = json.loads(ledger_lines[-1])
        payload = entry.get("payload", entry)
        assert payload.get("shared_engine_decision") == "block"
        assert "TAU BREACH" in payload.get("rationale", "") or \
               "block" in payload.get("rationale", "").lower()
