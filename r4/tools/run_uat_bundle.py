from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:  # flat script compatibility
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    from tenir_v4_test.runtime_support import runtime_root
else:
    from tenir_v4_test.runtime_support import runtime_root


ROOT = Path(__file__).resolve().parents[1]


def _timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _environment_metadata(env: dict[str, str]) -> dict[str, str]:
    return {
        "python_version": sys.version,
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "runtime_root": env["TENIR_V4_RUNTIME_ROOT"],
    }


def _artifact_manifest(*, output_dir: Path, summary: dict[str, Any]) -> dict[str, Any]:
    checksums = {}
    for path in sorted(output_dir.iterdir()):
        if path.is_file():
            checksums[path.name] = _sha256_file(path)
    return {
        "artifact_name": output_dir.name,
        "artifact_type": "internal_uat_bundle",
        "generated_at": summary["generated_at"],
        "package_root": summary["package_root"],
        "output_directory": summary["output_directory"],
        "classification": "internal-restricted",
        "intended_audience": "internal-review",
        "overall_status": summary["overall_status"],
        "environment": summary["environment"],
        "checksums": checksums,
    }


def _run_step(
    *,
    name: str,
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    output_dir: Path,
) -> dict[str, Any]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )
    stdout_path = output_dir / f"{name}.stdout.txt"
    stderr_path = output_dir / f"{name}.stderr.txt"
    _write_text(stdout_path, result.stdout)
    _write_text(stderr_path, result.stderr)
    return {
        "name": name,
        "command": command,
        "returncode": result.returncode,
        "passed": result.returncode == 0,
        "stdout_file": stdout_path.name,
        "stderr_file": stderr_path.name,
    }


def _checklist_status(results: dict[str, dict[str, Any]]) -> dict[str, bool]:
    tests_ok = results["tests"]["passed"]
    limits_ok = results["limits"]["passed"]
    example_ok = results["example"]["passed"]
    cli_module_ok = results["cli_module"]["passed"]
    cli_flat_ok = results["cli_flat"]["passed"]
    return {
        "functional_path_verified": tests_ok and limits_ok,
        "cli_handoff_verified": cli_module_ok and cli_flat_ok,
        "partner_demo_path_verified": example_ok and cli_module_ok and cli_flat_ok,
        "internal_uat_pass": tests_ok and limits_ok and example_ok and cli_module_ok and cli_flat_ok,
    }


def _markdown_summary(summary: dict[str, Any]) -> str:
    lines = [
        "# v4 UAT Bundle Summary",
        "",
        f"- Generated at: `{summary['generated_at']}`",
        f"- Package root: `{summary['package_root']}`",
        f"- Output directory: `{summary['output_directory']}`",
        f"- Overall status: `{summary['overall_status']}`",
        f"- Artifact manifest: `ARTIFACT_MANIFEST.json`",
        "",
        "## Command Results",
        "",
    ]
    for item in summary["commands"]:
        status = "PASS" if item["passed"] else "FAIL"
        cmd = " ".join(item["command"])
        lines.extend(
            [
                f"- `{item['name']}`: `{status}`",
                f"  command: `{cmd}`",
                f"  stdout: `{item['stdout_file']}`",
                f"  stderr: `{item['stderr_file']}`",
            ]
        )
    lines.extend(
        [
            "",
            "## Checklist Status",
            "",
        ]
    )
    for key, value in summary["checklist_status"].items():
        lines.append(f"- `{key}`: `{value}`")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run and export the v4 internal UAT bundle.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Optional output directory for the generated bundle.",
    )
    args = parser.parse_args()

    default_output = runtime_root(ROOT) / "uat" / _timestamp_slug()
    output_dir = args.output_dir if args.output_dir is not None else default_output
    output_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["TENIR_V4_RUNTIME_ROOT"] = str(runtime_root(ROOT))

    commands = {
        "tests": [sys.executable, "-m", "unittest", "-v"],
        "limits": [sys.executable, "tools/run_limit_scenarios.py"],
        "example": [sys.executable, "example_run.py"],
        "cli_module": [
            sys.executable,
            "-m",
            "tenir_v4_test.cli",
            "--events-json",
            "examples/sample_events.json",
            "--close-the-glass-after",
            "5",
        ],
        "cli_flat": [
            sys.executable,
            "tenir_v4_test/cli.py",
            "--events-json",
            "examples/sample_events.json",
            "--close-the-glass-after",
            "5",
        ],
    }

    results: dict[str, dict[str, Any]] = {}
    for name, command in commands.items():
        results[name] = _run_step(
            name=name,
            command=command,
            cwd=ROOT,
            env=env,
            output_dir=output_dir,
        )

    checklist_status = _checklist_status(results)
    overall_pass = all(item["passed"] for item in results.values())

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "package_root": str(ROOT),
        "output_directory": str(output_dir),
        "overall_status": "PASS" if overall_pass else "FAIL",
        "environment": _environment_metadata(env),
        "commands": list(results.values()),
        "checklist_status": checklist_status,
    }

    summary_json_path = output_dir / "uat_summary.json"
    summary_md_path = output_dir / "UAT_STATUS.md"
    _write_text(summary_json_path, json.dumps(summary, indent=2))
    _write_text(summary_md_path, _markdown_summary(summary))
    manifest = _artifact_manifest(output_dir=output_dir, summary=summary)
    manifest_path = output_dir / "ARTIFACT_MANIFEST.json"
    _write_text(manifest_path, json.dumps(manifest, indent=2))

    print(json.dumps(summary, indent=2))

    if not overall_pass:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
