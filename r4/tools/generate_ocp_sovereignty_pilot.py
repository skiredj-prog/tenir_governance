from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:  # flat script compatibility
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    from tenir_v4_test.adjudication import build_pilot_payload, sample_pilot_template
    from tenir_v4_test.runtime_support import runtime_root
else:
    from tenir_v4_test.adjudication import build_pilot_payload, sample_pilot_template
    from tenir_v4_test.runtime_support import runtime_root


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = (
    runtime_root(ROOT) / "adjudication" / "ocp_sovereignty_pilot_amplified_loop.json"
)


def _load_template(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate an partner_b Sovereignty pilot payload from a template or sample."
    )
    parser.add_argument(
        "--scenario",
        default="amplified_loop",
        help="Built-in sample scenario name when --input-json is not provided.",
    )
    parser.add_argument(
        "--input-json",
        type=Path,
        default=None,
        help="Optional path to a pilot template JSON file.",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Destination for the enriched pilot payload JSON.",
    )
    parser.add_argument(
        "--stdout-only",
        action="store_true",
        help="Print the payload to stdout without writing a file.",
    )
    args = parser.parse_args()

    template = (
        _load_template(args.input_json)
        if args.input_json is not None
        else sample_pilot_template(args.scenario)
    )
    payload = build_pilot_payload(template)
    rendered = json.dumps(payload, indent=2, ensure_ascii=False)

    if args.stdout_only:
        print(rendered)
        return

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(rendered + "\n", encoding="utf-8")
    print(f"Wrote partner_b Sovereignty pilot payload to {args.output_json}")


if __name__ == "__main__":
    main()
