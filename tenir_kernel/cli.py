"""Command-line entry points for the quickstart kernel."""

from __future__ import annotations

import argparse
import json
import os
from importlib.resources import files
from pathlib import Path
import sys
from typing import Sequence

from .kernel import TenirKernel
from .schema import validate_runtime_object


def _read_object(path: str) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("runtime object file must contain a JSON object")
    return value


def _demo_object() -> dict:
    source = files("tenir_kernel").joinpath("examples/quickstart_runtime_object.json")
    return json.loads(source.read_text(encoding="utf-8"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tenir-kernel", description="TENIR 2.0 runtime kernel")
    commands = parser.add_subparsers(dest="command", required=True)

    demo = commands.add_parser("demo", help="validate and adjudicate the included sample")
    demo.add_argument("--mode", default="SHADOW_PASSIVE", choices=(
        "SHADOW_OFF", "SHADOW_PASSIVE", "SHADOW_CRITICAL", "ENFORCE"
    ))

    validate = commands.add_parser("validate", help="validate a schema 1.2 JSON object")
    validate.add_argument("runtime_object")

    adjudicate = commands.add_parser("adjudicate", help="adjudicate a schema 1.2 JSON object")
    adjudicate.add_argument("runtime_object")
    adjudicate.add_argument("--mode", default="SHADOW_PASSIVE", choices=(
        "SHADOW_OFF", "SHADOW_PASSIVE", "SHADOW_CRITICAL", "ENFORCE"
    ))

    serve = commands.add_parser("serve", help="start the authenticated HTTP API")
    serve.add_argument("--host", default=os.getenv("TENIR_BIND_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.getenv("TENIR_PORT", "8000")))
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "serve":
            if len(os.getenv("TENIR_API_TOKEN", "")) < 32:
                raise ValueError("set TENIR_API_TOKEN to a secret of at least 32 characters")
            try:
                import uvicorn
            except ImportError as exc:
                raise RuntimeError("Install the API extra: pip install -e '.[api]'") from exc
            uvicorn.run("tenir_kernel.api:app", host=args.host, port=args.port, reload=False)
            return

        if args.command == "demo":
            runtime_object = _demo_object()
            mode = args.mode
        else:
            runtime_object = _read_object(args.runtime_object)
            mode = getattr(args, "mode", None)

        if args.command == "validate":
            validate_runtime_object(runtime_object)
            result = {"valid": True, "schema_version": runtime_object["schema_version"]}
        else:
            result = TenirKernel().adjudicate_runtime_object(runtime_object, mode=mode)
        print(json.dumps(result, indent=2, sort_keys=True))
    except (OSError, json.JSONDecodeError, ValueError, RuntimeError) as exc:
        print(f"tenir-kernel: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
