from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path

if __package__ in {None, ""}:  # flat script compatibility
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    from tenir_v4_test import EventSample, OperatingMode, OperatorRegistry, TenirMonitor
    from tenir_v4_test.ledger import HashChainedLedger
    from tenir_v4_test.runtime_support import managed_tempdir
else:
    from tenir_v4_test import EventSample, OperatingMode, OperatorRegistry, TenirMonitor
    from tenir_v4_test.ledger import HashChainedLedger
    from tenir_v4_test.runtime_support import managed_tempdir


def _registry() -> OperatorRegistry:
    registry = OperatorRegistry()
    registry.register_operator("limit-runner", "limit-runner-shared-secret")
    return registry


def volume_soak(event_count: int = 5000) -> dict:
    with managed_tempdir(anchor=__file__, prefix="tenir-v4-limit-") as tmpdir:
        ledger = Path(tmpdir) / "soak.jsonl"
        monitor = TenirMonitor(ledger_path=ledger)
        last_action = None
        for idx in range(event_count):
            event = EventSample(
                pressure=round(1.05 + 0.35 * math.sin(idx / 13.0), 6),
                velocity=round(1.00 + 0.40 * math.cos(idx / 17.0), 6),
                capacity=round(0.95 + 0.20 * math.sin(idx / 19.0), 6),
                option_space=max(0.10, round(0.80 - (0.00005 * idx), 6)),
                metadata={"idx": idx},
            )
            verdict = monitor.observe(event)
            last_action = verdict.action
        recovered = HashChainedLedger(ledger)
        entry_count = sum(1 for _ in recovered.iter_entries())
        return {
            "scenario": "volume_soak",
            "event_count": event_count,
            "entry_count": entry_count,
            "last_action": last_action,
            "last_hash": recovered.last_hash,
        }


def replay_equivalence(event_count: int = 1200, reboot_every: int = 37) -> dict:
    random.seed(20260413)
    events = []
    for _ in range(event_count):
        events.append(
            EventSample(
                pressure=round(random.uniform(0.7, 1.8), 6),
                velocity=round(random.uniform(0.7, 1.9), 6),
                capacity=round(random.uniform(0.55, 1.3), 6),
                option_space=round(random.uniform(0.15, 0.95), 6),
            )
        )

    with managed_tempdir(anchor=__file__, prefix="tenir-v4-limit-") as tmpdir:
        ledger_a = Path(tmpdir) / "continuous.jsonl"
        ledger_b = Path(tmpdir) / "replayed.jsonl"

        continuous = TenirMonitor(ledger_path=ledger_a)
        final_continuous = None
        for event in events:
            final_continuous = continuous.observe(event)

        restarted = TenirMonitor(ledger_path=ledger_b)
        final_restarted = None
        for idx, event in enumerate(events, start=1):
            final_restarted = restarted.observe(event)
            if idx % reboot_every == 0 and idx != event_count:
                restarted = TenirMonitor(ledger_path=ledger_b)

        assert final_continuous is not None
        assert final_restarted is not None
        return {
            "scenario": "replay_equivalence",
            "event_count": event_count,
            "reboot_every": reboot_every,
            "same_action": final_restarted.action == final_continuous.action,
            "same_trajectory": final_restarted.trajectory == final_continuous.trajectory,
            "same_chain_hash": final_restarted.chain_hash == final_continuous.chain_hash,
        }


def enforce_persistence() -> dict:
    with managed_tempdir(anchor=__file__, prefix="tenir-v4-limit-") as tmpdir:
        ledger = Path(tmpdir) / "control.jsonl"
        registry = _registry()
        first = TenirMonitor(ledger_path=ledger, operator_registry=registry)
        first.close_the_glass(
            registry.issue_transition_proof(
                operator_id="limit-runner",
                action="SHADOW_TO_ENFORCE",
                reason="persistence check",
            )
        )
        rebooted = TenirMonitor(ledger_path=ledger, operator_registry=registry)
        verdict = rebooted.observe(
            EventSample(pressure=1.4, velocity=1.7, capacity=0.6, option_space=0.18)
        )
        return {
            "scenario": "enforce_persistence",
            "recovered_mode": rebooted.mode.value,
            "action_after_reboot": verdict.action,
            "block_expected": verdict.action == "block",
        }


def mode_matrix() -> dict:
    alert_only_event = EventSample(
        pressure=1.05,
        velocity=1.1,
        capacity=0.95,
        option_space=0.30,
    )
    critical_event = EventSample(
        pressure=1.3,
        velocity=1.5,
        capacity=0.6,
        option_space=0.18,
    )

    with managed_tempdir(anchor=__file__, prefix="tenir-v4-limit-") as tmpdir:
        outputs = {}
        for mode in [
            OperatingMode.SHADOW_OFF,
            OperatingMode.SHADOW_PASSIVE,
            OperatingMode.SHADOW_CRITICAL,
            OperatingMode.ENFORCE,
        ]:
            ledger = Path(tmpdir) / f"{mode.cli_value}.jsonl"
            monitor = TenirMonitor(ledger_path=ledger, mode=mode)
            alert_verdict = monitor.observe(alert_only_event)
            critical_verdict = monitor.observe(critical_event)
            outputs[mode.cli_value] = {
                "alert_only_action": alert_verdict.action,
                "critical_action": critical_verdict.action,
            }

        return {
            "scenario": "mode_matrix",
            "results": outputs,
        }


def control_round_trip() -> dict:
    alert_only_event = EventSample(
        pressure=1.05,
        velocity=1.1,
        capacity=0.95,
        option_space=0.30,
    )
    critical_event = EventSample(
        pressure=1.3,
        velocity=1.5,
        capacity=0.6,
        option_space=0.18,
    )

    with managed_tempdir(anchor=__file__, prefix="tenir-v4-limit-") as tmpdir:
        ledger = Path(tmpdir) / "control-round-trip.jsonl"
        registry = _registry()
        first = TenirMonitor(
            ledger_path=ledger,
            mode=OperatingMode.SHADOW_CRITICAL,
            operator_registry=registry,
        )
        shadow_alert = first.observe(alert_only_event)
        shadow_critical = first.observe(critical_event)
        first.close_the_glass(
            registry.issue_transition_proof(
                operator_id="limit-runner",
                action="SHADOW_TO_ENFORCE",
                reason="round-trip drill",
            )
        )

        enforced = TenirMonitor(ledger_path=ledger, operator_registry=registry)
        mode_after_close_reboot = enforced.mode.value
        enforce_alert = enforced.observe(alert_only_event)
        enforced.reopen_to_shadow(
            registry.issue_transition_proof(
                operator_id="limit-runner",
                action="ENFORCE_TO_SHADOW",
                reason="round-trip drill complete",
            )
        )

        reopened = TenirMonitor(ledger_path=ledger, operator_registry=registry)
        shadow_after_reopen = reopened.observe(critical_event)
        return {
            "scenario": "control_round_trip",
            "start_mode": OperatingMode.SHADOW_CRITICAL.value,
            "mode_after_close_reboot": mode_after_close_reboot,
            "mode_after_reopen_reboot": reopened.mode.value,
            "shadow_critical_alert_action": shadow_alert.action,
            "shadow_critical_critical_action": shadow_critical.action,
            "enforce_alert_action": enforce_alert.action,
            "shadow_after_reopen_critical_action": shadow_after_reopen.action,
        }


if __name__ == "__main__":
    results = [
        volume_soak(),
        replay_equivalence(),
        enforce_persistence(),
        mode_matrix(),
        control_round_trip(),
    ]
    print(json.dumps(results, indent=2))
