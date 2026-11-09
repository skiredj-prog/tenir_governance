"""Prove the B1.1-B native admission zero-effect oracle detects its mutant."""
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


CASE = "test_b1_profile_refuses_native_admission_without_durable_dual_grant"
MESSAGE = "native I8/permit/outbox changed after denied admission"


def main():
    destination = Path(sys.argv[1] if len(sys.argv) > 1 else "evidence").resolve()
    destination.mkdir(parents=True, exist_ok=True)
    junit = destination / "b11-native-dual-mutation.xml"
    log = destination / "b11-native-dual-mutation.log"
    base_temp = destination / "b11-native-dual-pytest-tmp"
    command = [
        sys.executable, "-m", "pytest", "-q", "--tb=short", "-o", "addopts=",
        "--basetemp=" + str(base_temp),
        "w2_integration/test_b11_native_dual_grant.py::" + CASE,
        "--junitxml=" + str(junit),
    ]
    env = {**os.environ, "B11B_TEST_MUTANT": "1"}
    with log.open("w", encoding="utf-8") as stream:
        result = subprocess.run(command, env=env, stdout=stream, stderr=subprocess.STDOUT, timeout=180)
    if result.returncode != 1:
        raise RuntimeError(f"mutant pytest exit must be 1, got {result.returncode}; see {log}")
    cases = ET.parse(junit).getroot().findall(".//testcase")
    if len(cases) != 1 or cases[0].get("name") != CASE:
        raise RuntimeError("mutation test identity/count differs from the B1.1-B admission boundary")
    case = cases[0]
    failure = case.find("failure")
    if (failure is None or case.find("error") is not None or case.find("skipped") is not None
            or MESSAGE not in failure.get("message", "")):
        raise RuntimeError("mutation did not fail the B1.1-B zero-effect oracle")
    report = {
        "mutants_detected": 1,
        "expected_pytest_exit": result.returncode,
        "command": command,
        "failure": {"test": CASE, "message": failure.get("message")},
        "scope": "test-only monkeypatch; no application bypass flag",
    }
    (destination / "b11-native-dual-mutation-summary.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print("1/1 B1.1-B controlled mutation detected by the native zero-effect assertion")


if __name__ == "__main__":
    main()
