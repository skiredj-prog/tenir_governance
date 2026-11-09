"""Run the deliberately unsafe TEST mutants and require eight specific refusals to fail."""
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


CASES = {
    "test_http_endpoint_refuses_invalid_grant_without_effect_or_admission",
    "test_run_once_refuses_unauthenticated_native_permit_without_effect",
    "test_native_positive_dispatch_refuses_invalid_grant",
    "test_proof_provider_direct_refuses_without_claim",
    "test_kernel_provider_direct_refuses_without_claim",
    "test_legacy_positive_dispatch_refuses_before_pending_send",
    "test_legacy_positive_recovery_refuses_without_effect",
    "test_legacy_s1_refuses_before_reserving_or_queueing",
}
MESSAGES = (
    "provider transport was called",
    "denied request changed I8 or pending-send state",
)


def main():
    destination = Path(sys.argv[1] if len(sys.argv) > 1 else "evidence").resolve()
    destination.mkdir(parents=True, exist_ok=True)
    junit, log = destination / "mutation.xml", destination / "mutation.log"
    command = [sys.executable, "-m", "pytest", "-q", "--tb=short", "-o", "addopts=",
               "w2_integration/test_w2_entrypoint_refusals.py", "--junitxml=" + str(junit)]
    env = {**os.environ, "W2_TEST_MUTANT": "1"}
    with log.open("w", encoding="utf-8") as stream:
        result = subprocess.run(command, env=env, stdout=stream, stderr=subprocess.STDOUT, timeout=180)
    if result.returncode != 1:
        raise RuntimeError(f"mutant pytest exit must be 1, got {result.returncode}; see {log}")
    cases = ET.parse(junit).getroot().findall(".//testcase")
    if len(cases) != len(CASES) or {case.get("name") for case in cases} != CASES:
        raise RuntimeError("mutation test identity/count differs from the eight required entrypoints")
    details = []
    for case in cases:
        failure = case.find("failure")
        if (failure is None or case.find("error") is not None or case.find("skipped") is not None
                or not any(failure.get("message", "").startswith("AssertionError: " + message)
                           for message in MESSAGES)):
            raise RuntimeError("mutation did not fail the boundary oracle: " + case.get("name"))
        details.append({"test": case.get("name"), "message": failure.get("message")})
    report = {"mutants_detected": len(details), "expected_pytest_exit": result.returncode,
              "command": command, "failures": details,
              "scope": "test-only mutations; no application bypass flag"}
    (destination / "mutation-summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("8/8 mutations detected by boundary assertions; raw expected failures retained in mutation.xml/log")


if __name__ == "__main__":
    main()
