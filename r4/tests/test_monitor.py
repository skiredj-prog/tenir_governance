import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from tenir_v4_test import (
    EventSample,
    LedgerIntegrityError,
    OperatingMode,
    OperatorRegistry,
    PolicyBundle,
    TenirMonitor,
    TransitionProof,
)
from tenir_v4_test.ledger import HashChainedLedger
from tenir_v4_test.models import BurnInputs
from tenir_v4_test.overlay_burn import estimate_burn_cost
from tenir_v4_test.runtime_support import runtime_root
from tenir_v4_test.trajectory import compute_stability
from tools.run_uat_bundle import _artifact_manifest


ROOT = Path(__file__).resolve().parents[1]
TEMP_ROOT = runtime_root(ROOT) / "tmp"
TEMP_ROOT.mkdir(parents=True, exist_ok=True)


class RepoTemporaryDirectory:
    def __init__(self, *args, **kwargs) -> None:
        self.name = str(TEMP_ROOT / f"tenir-v4-test-{uuid4().hex}")
        Path(self.name).mkdir(parents=True, exist_ok=False)

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, exc_type, exc, tb) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        shutil.rmtree(self.name, ignore_errors=True)


tempfile.TemporaryDirectory = RepoTemporaryDirectory


def _operator_registry() -> OperatorRegistry:
    registry = OperatorRegistry()
    registry.register_operator("op-1", "shared-secret-op-1")
    registry.register_operator("op-2", "shared-secret-op-2")
    registry.register_operator("cli-operator", "shared-secret-cli")
    return registry


def _transition_proof(
    *,
    registry: OperatorRegistry,
    operator_id: str,
    action: str,
    reason: str,
    ttl_seconds: int = 300,
    nonce: str | None = None,
) -> TransitionProof:
    return registry.issue_transition_proof(
        operator_id=operator_id,
        action=action,
        reason=reason,
        ttl_seconds=ttl_seconds,
        nonce=nonce,
    )


class ValidationTests(unittest.TestCase):
    def test_operating_mode_parse_accepts_aliases(self) -> None:
        self.assertEqual(OperatingMode.parse("shadow"), OperatingMode.SHADOW_PASSIVE)
        self.assertEqual(OperatingMode.parse("shadow-off"), OperatingMode.SHADOW_OFF)
        self.assertEqual(OperatingMode.parse("critical"), OperatingMode.SHADOW_CRITICAL)
        self.assertEqual(OperatingMode.parse("enforce"), OperatingMode.ENFORCE)

    def test_operating_mode_parse_rejects_unknown_value(self) -> None:
        with self.assertRaises(ValueError):
            OperatingMode.parse("mystery-mode")

    def test_event_rejects_negative_values(self) -> None:
        for kwargs in [
            {"pressure": -0.1, "velocity": 1.0, "capacity": 1.0},
            {"pressure": 0.1, "velocity": -1.0, "capacity": 1.0},
            {"pressure": 0.1, "velocity": 1.0, "capacity": -1.0},
        ]:
            with self.assertRaises(ValueError):
                EventSample(**kwargs).validate()

    def test_event_rejects_option_space_outside_unit_interval(self) -> None:
        with self.assertRaises(ValueError):
            EventSample(pressure=1.0, velocity=1.0, capacity=1.0, option_space=-0.01).validate()
        with self.assertRaises(ValueError):
            EventSample(pressure=1.0, velocity=1.0, capacity=1.0, option_space=1.01).validate()

    def test_event_rejects_non_finite_numbers(self) -> None:
        for bad in [float("nan"), float("inf"), float("-inf")]:
            with self.assertRaises(ValueError):
                EventSample(pressure=bad, velocity=1.0, capacity=1.0).validate()

    def test_event_requires_timezone_aware_iso_timestamp(self) -> None:
        with self.assertRaises(ValueError):
            EventSample(
                pressure=1.0,
                velocity=1.0,
                capacity=1.0,
                observed_at="2026-04-13T00:00:00",
            ).validate()
        with self.assertRaises(ValueError):
            EventSample(
                pressure=1.0,
                velocity=1.0,
                capacity=1.0,
                observed_at="not-a-timestamp",
            ).validate()

    def test_policy_rejects_invalid_threshold_configuration(self) -> None:
        with self.assertRaises(ValueError):
            PolicyBundle(event_window=2).validate()
        with self.assertRaises(ValueError):
            PolicyBundle(s_alert_floor=0.5, s_block_floor=0.6).validate()
        with self.assertRaises(ValueError):
            PolicyBundle(option_space_alert_floor=0.2, option_space_block_floor=0.3).validate()
        with self.assertRaises(ValueError):
            PolicyBundle(option_space_alert_floor=1.1).validate()

    def test_burn_inputs_reject_non_finite_or_negative_values(self) -> None:
        with self.assertRaises(ValueError):
            BurnInputs(cost_per_alert=-1.0).validate()
        with self.assertRaises(ValueError):
            BurnInputs(cost_per_review_minute=float("inf")).validate()


class TrajectoryTests(unittest.TestCase):
    def test_zero_pressure_and_velocity_remain_finite(self) -> None:
        event = EventSample(pressure=0.0, velocity=0.0, capacity=1.0, option_space=1.0)
        stability = compute_stability(event, epsilon=1e-6)
        self.assertGreater(stability, 0)
        self.assertLess(stability, 2_000_000)

    def test_projected_horizon_none_when_stability_not_declining(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "ledger.jsonl"
            monitor = TenirMonitor(ledger_path=ledger)
            monitor.observe(EventSample(pressure=1.0, velocity=1.0, capacity=1.0, option_space=0.8))
            verdict = monitor.observe(EventSample(pressure=0.8, velocity=0.8, capacity=1.1, option_space=0.9))
            self.assertIsNone(verdict.trajectory.projected_events_to_zero)
            self.assertGreaterEqual(verdict.trajectory.ds_de, 0.0)

    def test_event_window_caps_internal_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor = TenirMonitor(
                policy=PolicyBundle(event_window=4),
                ledger_path=Path(tmpdir) / "ledger.jsonl",
            )
            for idx in range(10):
                monitor.observe(
                    EventSample(
                        pressure=1.0 + (idx * 0.01),
                        velocity=1.0 + (idx * 0.02),
                        capacity=1.0 - (idx * 0.01),
                        option_space=max(0.1, 0.9 - (idx * 0.05)),
                    )
                )
            self.assertEqual(len(monitor.tracker._stability_window), 4)
            self.assertLessEqual(len(monitor.tracker._first_derivative_window), 4)

    def test_recovery_after_alert_can_return_to_allow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "ledger.jsonl"
            monitor = TenirMonitor(ledger_path=ledger)
            monitor.observe(EventSample(pressure=1.3, velocity=1.5, capacity=0.7, option_space=0.18))
            verdict = monitor.observe(EventSample(pressure=0.7, velocity=0.8, capacity=1.2, option_space=0.9))
            self.assertEqual(verdict.action, "allow")
            self.assertFalse(verdict.trajectory.alert)
            self.assertFalse(verdict.trajectory.intended_block)

    def test_long_run_oscillation_does_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "ledger.jsonl"
            monitor = TenirMonitor(ledger_path=ledger)
            for idx in range(250):
                if idx % 2 == 0:
                    event = EventSample(pressure=0.8, velocity=0.9, capacity=1.1, option_space=0.8)
                else:
                    event = EventSample(pressure=1.4, velocity=1.6, capacity=0.6, option_space=0.22)
                verdict = monitor.observe(event)
            self.assertIsNotNone(verdict)
            entries = list(HashChainedLedger(ledger).iter_entries())
            self.assertEqual(len(entries), 250)


class PersistenceAndControlTests(unittest.TestCase):
    def _alert_only_event(self) -> EventSample:
        return EventSample(
            pressure=1.05,
            velocity=1.1,
            capacity=0.95,
            option_space=0.30,
        )

    def _critical_event(self) -> EventSample:
        return EventSample(
            pressure=1.3,
            velocity=1.5,
            capacity=0.6,
            option_space=0.18,
        )

    def test_shadow_default_never_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "ledger.jsonl"
            monitor = TenirMonitor(ledger_path=ledger)
            verdict = monitor.observe(self._critical_event())
            self.assertEqual(monitor.mode, OperatingMode.SHADOW_PASSIVE)
            self.assertEqual(verdict.action, "allow_with_intended_block")

    def test_shadow_off_cleanly_bypasses_governance_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "ledger.jsonl"
            monitor = TenirMonitor(ledger_path=ledger, mode="shadow-off")
            verdict = monitor.observe(self._critical_event())
            self.assertEqual(monitor.mode, OperatingMode.SHADOW_OFF)
            self.assertEqual(verdict.action, "allow")
            self.assertTrue(verdict.trajectory.intended_block)

    def test_shadow_critical_blocks_only_critical_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "ledger.jsonl"
            monitor = TenirMonitor(ledger_path=ledger, mode="shadow-critical")
            alert_only = monitor.observe(self._alert_only_event())
            critical = monitor.observe(self._critical_event())
            self.assertEqual(alert_only.action, "allow_with_alert")
            self.assertEqual(critical.action, "block")

    def test_enforce_blocks_even_alert_only_condition(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "ledger.jsonl"
            monitor = TenirMonitor(ledger_path=ledger, mode="enforce")
            verdict = monitor.observe(self._alert_only_event())
            self.assertEqual(monitor.mode, OperatingMode.ENFORCE)
            self.assertEqual(verdict.action, "block_on_alert")

    def test_enforce_persists_across_restart_and_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "ledger.jsonl"
            registry = _operator_registry()
            first = TenirMonitor(ledger_path=ledger, operator_registry=registry)
            first.close_the_glass(
                _transition_proof(
                    registry=registry,
                    operator_id="op-1",
                    action="SHADOW_TO_ENFORCE",
                    reason="drill",
                )
            )

            rebooted = TenirMonitor(ledger_path=ledger, operator_registry=registry)
            self.assertEqual(rebooted.mode, OperatingMode.ENFORCE)
            verdict = rebooted.observe(self._critical_event())
            self.assertEqual(verdict.action, "block")

    def test_reopen_to_shadow_persists_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "ledger.jsonl"
            registry = _operator_registry()
            first = TenirMonitor(ledger_path=ledger, operator_registry=registry)
            first.close_the_glass(
                _transition_proof(
                    registry=registry,
                    operator_id="op-1",
                    action="SHADOW_TO_ENFORCE",
                    reason="drill",
                )
            )
            first.reopen_to_shadow(
                _transition_proof(
                    registry=registry,
                    operator_id="op-1",
                    action="ENFORCE_TO_SHADOW",
                    reason="drill complete",
                )
            )

            rebooted = TenirMonitor(ledger_path=ledger, operator_registry=registry)
            self.assertEqual(rebooted.mode, OperatingMode.SHADOW_PASSIVE)
            verdict = rebooted.observe(self._critical_event())
            self.assertEqual(verdict.action, "allow_with_intended_block")

    def test_last_control_transition_wins_on_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "ledger.jsonl"
            registry = _operator_registry()
            monitor = TenirMonitor(ledger_path=ledger, operator_registry=registry)
            monitor.close_the_glass(
                _transition_proof(
                    registry=registry,
                    operator_id="op-1",
                    action="SHADOW_TO_ENFORCE",
                    reason="first",
                )
            )
            monitor.reopen_to_shadow(
                _transition_proof(
                    registry=registry,
                    operator_id="op-1",
                    action="ENFORCE_TO_SHADOW",
                    reason="second",
                )
            )
            monitor.close_the_glass(
                _transition_proof(
                    registry=registry,
                    operator_id="op-2",
                    action="SHADOW_TO_ENFORCE",
                    reason="third",
                )
            )

            rebooted = TenirMonitor(ledger_path=ledger, operator_registry=registry)
            self.assertEqual(rebooted.mode, OperatingMode.ENFORCE)

    def test_shadow_critical_mode_persists_from_observation_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "ledger.jsonl"
            first = TenirMonitor(ledger_path=ledger, mode="shadow-critical")
            first.observe(self._critical_event())
            rebooted = TenirMonitor(ledger_path=ledger)
            self.assertEqual(rebooted.mode, OperatingMode.SHADOW_CRITICAL)

    def test_shadow_off_mode_persists_from_observation_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "ledger.jsonl"
            first = TenirMonitor(ledger_path=ledger, mode="shadow-off")
            first.observe(self._critical_event())
            rebooted = TenirMonitor(ledger_path=ledger)
            self.assertEqual(rebooted.mode, OperatingMode.SHADOW_OFF)

    def test_shadow_critical_close_and_reopen_round_trip_is_replayable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "ledger.jsonl"
            registry = _operator_registry()
            first = TenirMonitor(
                ledger_path=ledger,
                mode="shadow-critical",
                operator_registry=registry,
            )
            alert_only = first.observe(self._alert_only_event())
            critical = first.observe(self._critical_event())
            first.close_the_glass(
                _transition_proof(
                    registry=registry,
                    operator_id="op-1",
                    action="SHADOW_TO_ENFORCE",
                    reason="incident drill",
                )
            )

            enforced = TenirMonitor(ledger_path=ledger, operator_registry=registry)
            self.assertEqual(enforced.mode, OperatingMode.ENFORCE)
            enforce_verdict = enforced.observe(self._alert_only_event())
            enforced.reopen_to_shadow(
                _transition_proof(
                    registry=registry,
                    operator_id="op-1",
                    action="ENFORCE_TO_SHADOW",
                    reason="incident closed",
                )
            )

            reopened = TenirMonitor(ledger_path=ledger, operator_registry=registry)
            self.assertEqual(reopened.mode, OperatingMode.SHADOW_PASSIVE)
            reopened_verdict = reopened.observe(self._critical_event())

            self.assertEqual(alert_only.action, "allow_with_alert")
            self.assertEqual(critical.action, "block")
            self.assertEqual(enforce_verdict.action, "block_on_alert")
            self.assertEqual(reopened_verdict.action, "allow_with_intended_block")

    def test_replay_restores_trajectory_equivalence(self) -> None:
        events = [
            EventSample(pressure=1.0, velocity=1.0, capacity=1.0, option_space=0.9),
            EventSample(pressure=1.1, velocity=1.2, capacity=0.85, option_space=0.6),
            EventSample(pressure=1.2, velocity=1.3, capacity=0.75, option_space=0.4),
            EventSample(pressure=1.3, velocity=1.4, capacity=0.65, option_space=0.25),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_a = Path(tmpdir) / "continuous.jsonl"
            ledger_b = Path(tmpdir) / "replayed.jsonl"

            continuous = TenirMonitor(ledger_path=ledger_a)
            for event in events[:-1]:
                continuous.observe(event)
            verdict_continuous = continuous.observe(events[-1])

            staged = TenirMonitor(ledger_path=ledger_b)
            for event in events[:2]:
                staged.observe(event)
            staged = TenirMonitor(ledger_path=ledger_b)
            for event in events[2:-1]:
                staged.observe(event)
            staged = TenirMonitor(ledger_path=ledger_b)
            verdict_replayed = staged.observe(events[-1])

            self.assertEqual(verdict_replayed.action, verdict_continuous.action)
            self.assertEqual(verdict_replayed.trajectory, verdict_continuous.trajectory)
            self.assertEqual(verdict_replayed.chain_hash, verdict_continuous.chain_hash)

    def test_control_transitions_require_valid_signed_proofs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = _operator_registry()
            monitor = TenirMonitor(
                ledger_path=Path(tmpdir) / "ledger.jsonl",
                operator_registry=registry,
            )
            with self.assertRaises(PermissionError):
                monitor.close_the_glass(
                    TransitionProof(
                        operator_id="op-1",
                        action="SHADOW_TO_ENFORCE",
                        reason="x",
                        nonce="nonce-a",
                        issued_at="2026-04-16T00:00:00+00:00",
                        expires_at="2026-04-16T00:05:00+00:00",
                        signature="0" * 64,
                    )
                )
            valid = _transition_proof(
                registry=registry,
                operator_id="op-1",
                action="SHADOW_TO_ENFORCE",
                reason="valid",
            )
            monitor.close_the_glass(valid)
            with self.assertRaises(PermissionError):
                monitor.close_the_glass(valid)


class LedgerIntegrityTests(unittest.TestCase):
    def test_empty_ledger_starts_at_genesis(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = HashChainedLedger(Path(tmpdir) / "ledger.jsonl")
            self.assertEqual(ledger.last_hash, HashChainedLedger.GENESIS_HASH)

    def test_verify_chain_detects_payload_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "ledger.jsonl"
            ledger = HashChainedLedger(path)
            ledger.append({"type": "x", "value": 1})
            ledger.append({"type": "x", "value": 2})
            lines = path.read_text(encoding="utf-8").splitlines()
            first = json.loads(lines[0])
            first["payload"]["value"] = 999
            lines[0] = json.dumps(first, sort_keys=True)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(LedgerIntegrityError):
                HashChainedLedger(path)

    def test_verify_chain_detects_previous_hash_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "ledger.jsonl"
            ledger = HashChainedLedger(path)
            ledger.append({"type": "x", "value": 1})
            ledger.append({"type": "x", "value": 2})
            lines = path.read_text(encoding="utf-8").splitlines()
            second = json.loads(lines[1])
            second["previous_hash"] = "evil"
            lines[1] = json.dumps(second, sort_keys=True)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(LedgerIntegrityError):
                HashChainedLedger(path)

    def test_verify_chain_detects_chain_hash_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "ledger.jsonl"
            ledger = HashChainedLedger(path)
            ledger.append({"type": "x", "value": 1})
            lines = path.read_text(encoding="utf-8").splitlines()
            entry = json.loads(lines[0])
            entry["chain_hash"] = "0" * 64
            lines[0] = json.dumps(entry, sort_keys=True)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(LedgerIntegrityError):
                HashChainedLedger(path)

    def test_verify_chain_detects_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "ledger.jsonl"
            path.write_text('{"bad-json": true\n', encoding="utf-8")
            with self.assertRaises(LedgerIntegrityError):
                HashChainedLedger(path)

    def test_blank_lines_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "ledger.jsonl"
            ledger = HashChainedLedger(path)
            ledger.append({"type": "x", "value": 1})
            original = path.read_text(encoding="utf-8")
            path.write_text("\n" + original + "\n\n", encoding="utf-8")
            recovered = HashChainedLedger(path)
            self.assertNotEqual(recovered.last_hash, HashChainedLedger.GENESIS_HASH)

    def test_two_ledger_instances_can_append_without_corrupting_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "ledger.jsonl"
            first = HashChainedLedger(path)
            second = HashChainedLedger(path)
            first.append({"type": "x", "value": 1})
            second.append({"type": "x", "value": 2})
            recovered = HashChainedLedger(path)
            entries = list(recovered.iter_entries())
            self.assertEqual(len(entries), 2)
            self.assertEqual(recovered.last_hash, entries[-1]["chain_hash"])


class OverlayTests(unittest.TestCase):
    def test_overlay_counts_alerts_and_intended_blocks_independently(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "ledger.jsonl"
            monitor = TenirMonitor(ledger_path=ledger)
            monitor.observe(
                EventSample(pressure=1.3, velocity=1.5, capacity=0.6, option_space=0.18)
            )
            estimate = estimate_burn_cost(ledger)
            self.assertEqual(estimate.intended_block_count, 1)
            self.assertEqual(estimate.alert_count, 1)

    def test_overlay_ignores_control_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "ledger.jsonl"
            registry = _operator_registry()
            monitor = TenirMonitor(ledger_path=ledger, operator_registry=registry)
            monitor.close_the_glass(
                _transition_proof(
                    registry=registry,
                    operator_id="op-1",
                    action="SHADOW_TO_ENFORCE",
                    reason="drill",
                )
            )
            monitor.reopen_to_shadow(
                _transition_proof(
                    registry=registry,
                    operator_id="op-1",
                    action="ENFORCE_TO_SHADOW",
                    reason="done",
                )
            )
            estimate = estimate_burn_cost(ledger)
            self.assertEqual(estimate.intended_block_count, 0)
            self.assertEqual(estimate.alert_count, 0)

    def test_overlay_raises_on_tampered_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "ledger.jsonl"
            ledger = HashChainedLedger(path)
            ledger.append({"type": "observation", "action": "allow", "trajectory": {"alert": False}})
            lines = path.read_text(encoding="utf-8").splitlines()
            entry = json.loads(lines[0])
            entry["payload"]["action"] = "block"
            lines[0] = json.dumps(entry, sort_keys=True)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(LedgerIntegrityError):
                estimate_burn_cost(path)


class CliTests(unittest.TestCase):
    def _write_events(self, path: Path) -> None:
        events = [
            {"pressure": 1.0, "velocity": 1.0, "capacity": 1.0, "option_space": 0.9},
            {"pressure": 1.2, "velocity": 1.3, "capacity": 0.8, "option_space": 0.3},
            {"pressure": 1.4, "velocity": 1.6, "capacity": 0.6, "option_space": 0.18},
        ]
        path.write_text(json.dumps(events), encoding="utf-8")

    def _write_registry(self, path: Path) -> OperatorRegistry:
        registry = _operator_registry()
        registry_payload = {
            "cli-operator": "shared-secret-cli",
        }
        path.write_text(json.dumps(registry_payload), encoding="utf-8")
        return registry

    def _write_close_proof(self, path: Path, *, reason: str) -> None:
        registry = _operator_registry()
        proof = _transition_proof(
            registry=registry,
            operator_id="cli-operator",
            action="SHADOW_TO_ENFORCE",
            reason=reason,
        )
        path.write_text(json.dumps(asdict(proof)), encoding="utf-8")

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        env["TENIR_V4_RUNTIME_ROOT"] = str(runtime_root(ROOT))
        return env

    def test_module_cli_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            events_json = Path(tmpdir) / "events.json"
            ledger = Path(tmpdir) / "ledger.jsonl"
            registry_json = Path(tmpdir) / "operators.json"
            proof_json = Path(tmpdir) / "close-proof.json"
            self._write_events(events_json)
            self._write_registry(registry_json)
            self._write_close_proof(
                proof_json,
                reason="manual drill for internal test line",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tenir_v4_test.cli",
                    "--events-json",
                    str(events_json),
                    "--ledger-path",
                    str(ledger),
                    "--close-the-glass-after",
                    "2",
                    "--close-the-glass-proof-json",
                    str(proof_json),
                    "--operator-registry-json",
                    str(registry_json),
                ],
                cwd=ROOT,
                env=self._env(),
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn('"overlay": "burn_cost"', result.stdout)
            self.assertTrue(ledger.exists())

    def test_flat_script_cli_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            events_json = Path(tmpdir) / "events.json"
            ledger = Path(tmpdir) / "ledger.jsonl"
            registry_json = Path(tmpdir) / "operators.json"
            proof_json = Path(tmpdir) / "close-proof.json"
            self._write_events(events_json)
            self._write_registry(registry_json)
            self._write_close_proof(
                proof_json,
                reason="manual drill for internal test line",
            )
            cli_path = ROOT / "tenir_v4_test" / "cli.py"
            result = subprocess.run(
                [
                    sys.executable,
                    str(cli_path),
                    "--events-json",
                    str(events_json),
                    "--ledger-path",
                    str(ledger),
                    "--close-the-glass-after",
                    "2",
                    "--close-the-glass-proof-json",
                    str(proof_json),
                    "--operator-registry-json",
                    str(registry_json),
                ],
                cwd=ROOT,
                env=self._env(),
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn('"mode": "ENFORCE"', result.stdout)
            self.assertTrue(ledger.exists())

    def test_cli_accepts_explicit_start_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            events_json = Path(tmpdir) / "events.json"
            ledger = Path(tmpdir) / "ledger.jsonl"
            self._write_events(events_json)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tenir_v4_test.cli",
                    "--events-json",
                    str(events_json),
                    "--ledger-path",
                    str(ledger),
                    "--start-mode",
                    "shadow-critical",
                ],
                cwd=ROOT,
                env=self._env(),
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn('"mode": "SHADOW_CRITICAL"', result.stdout)
            self.assertTrue(ledger.exists())

    def test_cli_rejects_close_the_glass_without_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            events_json = Path(tmpdir) / "events.json"
            ledger = Path(tmpdir) / "ledger.jsonl"
            self._write_events(events_json)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tenir_v4_test.cli",
                    "--events-json",
                    str(events_json),
                    "--ledger-path",
                    str(ledger),
                    "--close-the-glass-after",
                    "2",
                ],
                cwd=ROOT,
                env=self._env(),
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--close-the-glass-proof-json", result.stderr)

    def test_cli_rejects_invalid_start_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            events_json = Path(tmpdir) / "events.json"
            ledger = Path(tmpdir) / "ledger.jsonl"
            self._write_events(events_json)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tenir_v4_test.cli",
                    "--events-json",
                    str(events_json),
                    "--ledger-path",
                    str(ledger),
                    "--start-mode",
                    "mystery-mode",
                ],
                cwd=ROOT,
                env=self._env(),
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--start-mode", result.stderr)

    def test_cli_handles_empty_event_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            events_json = Path(tmpdir) / "events.json"
            ledger = Path(tmpdir) / "ledger.jsonl"
            events_json.write_text("[]", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tenir_v4_test.cli",
                    "--events-json",
                    str(events_json),
                    "--ledger-path",
                    str(ledger),
                ],
                cwd=ROOT,
                env=self._env(),
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn('"overlay": "burn_cost"', result.stdout)
            self.assertIn('"intended_block_count": 0', result.stdout)
            self.assertIn('"alert_count": 0', result.stdout)


class UatBundleTests(unittest.TestCase):
    def test_artifact_manifest_includes_checksums_and_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            alpha = output_dir / "alpha.txt"
            beta = output_dir / "beta.txt"
            alpha.write_text("alpha", encoding="utf-8")
            beta.write_text("beta", encoding="utf-8")

            summary = {
                "generated_at": "2026-04-14T00:00:00+00:00",
                "package_root": str(ROOT),
                "output_directory": str(output_dir),
                "overall_status": "PASS",
                "environment": {
                    "python_version": "test",
                    "python_executable": sys.executable,
                    "platform": "test-platform",
                    "runtime_root": str(runtime_root(ROOT)),
                },
            }
            manifest = _artifact_manifest(output_dir=output_dir, summary=summary)

            self.assertEqual(manifest["artifact_type"], "internal_uat_bundle")
            self.assertEqual(manifest["overall_status"], "PASS")
            self.assertIn("alpha.txt", manifest["checksums"])
            self.assertEqual(
                manifest["checksums"]["alpha.txt"],
                sha256(b"alpha").hexdigest(),
            )
            self.assertEqual(manifest["environment"]["platform"], "test-platform")


class CreativeScenarioTests(unittest.TestCase):
    def test_many_restarts_preserve_final_outcome_in_seeded_run(self) -> None:
        random.seed(42)
        events = []
        for _ in range(40):
            pressure = round(random.uniform(0.7, 1.6), 3)
            velocity = round(random.uniform(0.7, 1.8), 3)
            capacity = round(random.uniform(0.55, 1.2), 3)
            option_space = round(random.uniform(0.15, 0.95), 3)
            events.append(
                EventSample(
                    pressure=pressure,
                    velocity=velocity,
                    capacity=capacity,
                    option_space=option_space,
                )
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_a = Path(tmpdir) / "continuous.jsonl"
            ledger_b = Path(tmpdir) / "restarts.jsonl"

            continuous = TenirMonitor(ledger_path=ledger_a)
            final_continuous = None
            for event in events:
                final_continuous = continuous.observe(event)

            restarted = TenirMonitor(ledger_path=ledger_b)
            final_restarted = None
            for idx, event in enumerate(events, start=1):
                final_restarted = restarted.observe(event)
                if idx % 5 == 0 and idx != len(events):
                    restarted = TenirMonitor(ledger_path=ledger_b)

            self.assertIsNotNone(final_continuous)
            self.assertIsNotNone(final_restarted)
            self.assertEqual(final_restarted.action, final_continuous.action)
            self.assertEqual(final_restarted.trajectory, final_continuous.trajectory)
            self.assertEqual(final_restarted.chain_hash, final_continuous.chain_hash)

    def test_metadata_round_trips_in_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "ledger.jsonl"
            monitor = TenirMonitor(ledger_path=ledger)
            event = EventSample(
                pressure=1.0,
                velocity=1.0,
                capacity=1.0,
                option_space=0.9,
                metadata={"nested": {"partner": "partner_a", "phase": "shadow"}},
            )
            monitor.observe(event)
            entries = list(HashChainedLedger(ledger).iter_entries())
            self.assertEqual(
                entries[0]["payload"]["event"]["metadata"]["nested"]["partner"],
                "partner_a",
            )


if __name__ == "__main__":
    unittest.main()
