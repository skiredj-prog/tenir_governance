from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:  # flat handoff compatibility
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    from tenir_v4_test.control_auth import OperatorRegistry, TransitionProof
    from tenir_v4_test.controller import TenirMonitor
    from tenir_v4_test.models import EventSample, OperatingMode
    from tenir_v4_test.overlay_burn import estimate_burn_cost
    from tenir_v4_test.runtime_support import runtime_artifact_path
else:
    from .control_auth import OperatorRegistry, TransitionProof
    from .controller import TenirMonitor
    from .models import EventSample, OperatingMode
    from .overlay_burn import estimate_burn_cost
    from .runtime_support import runtime_artifact_path


def _load_json_object(path: Path, *, expected_label: str) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{expected_label} must be a JSON object")
    return payload


def main() -> None:
    default_ledger_path = runtime_artifact_path("audit/governance_ledger.jsonl", anchor=__file__)
    parser = argparse.ArgumentParser(description="TENIR internal v4 test line demo CLI")
    parser.add_argument(
        "--events-json",
        type=Path,
        required=True,
        help="Path to a JSON file containing a list of event objects.",
    )
    parser.add_argument(
        "--ledger-path",
        type=Path,
        default=default_ledger_path,
        help="Path to the output JSONL ledger.",
    )
    parser.add_argument(
        "--start-mode",
        type=OperatingMode.parse,
        default=OperatingMode.SHADOW_PASSIVE,
        help=(
            "Initial operating mode. Supported values: shadow-off, shadow-passive, "
            "shadow-critical, enforce."
        ),
    )
    parser.add_argument(
        "--close-the-glass-after",
        type=int,
        default=None,
        help="Optional event count after which to switch to ENFORCE.",
    )
    parser.add_argument(
        "--close-the-glass-proof-json",
        type=Path,
        default=None,
        help="Path to a JSON proof authorizing SHADOW_TO_ENFORCE.",
    )
    parser.add_argument(
        "--operator-registry-json",
        type=Path,
        default=None,
        help="Path to a JSON object mapping operator ids to shared secrets.",
    )
    args = parser.parse_args()

    if args.close_the_glass_after is not None and args.close_the_glass_proof_json is None:
        parser.error("--close-the-glass-after requires --close-the-glass-proof-json")
    if args.close_the_glass_proof_json is not None and args.close_the_glass_after is None:
        parser.error("--close-the-glass-proof-json requires --close-the-glass-after")
    if args.close_the_glass_proof_json is not None and args.operator_registry_json is None:
        parser.error("--close-the-glass-proof-json requires --operator-registry-json")

    with args.events_json.open("r", encoding="utf-8") as handle:
        raw_events = json.load(handle)

    registry = OperatorRegistry()
    if args.operator_registry_json is not None:
        for operator_id, secret in _load_json_object(
            args.operator_registry_json,
            expected_label="operator registry",
        ).items():
            registry.register_operator(str(operator_id), str(secret))

    close_proof = None
    if args.close_the_glass_proof_json is not None:
        close_proof = TransitionProof(
            **_load_json_object(
                args.close_the_glass_proof_json,
                expected_label="control proof",
            )
        )

    monitor = TenirMonitor(
        ledger_path=args.ledger_path,
        mode=args.start_mode,
        operator_registry=registry,
    )
    for index, raw in enumerate(raw_events, start=1):
        if args.close_the_glass_after is not None and index == args.close_the_glass_after:
            assert close_proof is not None
            monitor.close_the_glass(close_proof)

        event = EventSample(**raw)
        verdict = monitor.observe(event)
        print(
            json.dumps(
                {
                    "observed_at": verdict.observed_at,
                    "mode": verdict.mode.value,
                    "action": verdict.action,
                    "trajectory": verdict.trajectory.__dict__,
                    "chain_hash": verdict.chain_hash,
                },
                indent=2,
            )
        )

    burn = estimate_burn_cost(args.ledger_path)
    print(
        json.dumps(
            {
                "overlay": "burn_cost",
                "intended_block_count": burn.intended_block_count,
                "alert_count": burn.alert_count,
                "decision_cost": burn.decision_cost,
                "review_cost": burn.review_cost,
                "total_cost": burn.total_cost,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
