"""Require three native quarantine mutants to fail the specified safety oracles."""
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


CASES = (
    ("1", "test_native_quarantine_blocks_admission_with_previously_valid_proof",
     "quarantine admission changed native I8/permit/outbox"),
    ("1", "test_native_quarantine_blocks_send_with_zero_provider_requests",
     "quarantine permitted a synthetic provider request"),
    ("commit", "test_native_quarantine_rechecked_at_commit_after_activation_in_body[claim]",
     "quarantine activation during transaction was not fenced at commit"),
)


def main():
    destination = Path(sys.argv[1] if len(sys.argv) > 1 else "evidence").resolve()
    destination.mkdir(parents=True, exist_ok=True)
    records = []
    for index, (mutation, case, message) in enumerate(CASES, 1):
        prefix = destination / f"quarantine-mutant-{index}"
        command = [sys.executable, "-m", "pytest", "-q", "--tb=short", "-o", "addopts=",
                   "--basetemp=" + str(prefix) + "-tmp",
                   "w2_integration/test_b11_native_quarantine.py::" + case,
                   "--junitxml=" + str(prefix) + ".xml"]
        with Path(str(prefix) + ".log").open("w", encoding="utf-8") as log:
            result = subprocess.run(command, env={**os.environ, "B11Q_TEST_MUTANT": mutation},
                                    stdout=log, stderr=subprocess.STDOUT, timeout=180)
        cases = ET.parse(str(prefix) + ".xml").getroot().findall(".//testcase")
        if result.returncode != 1 or len(cases) != 1 or cases[0].get("name") != case:
            raise RuntimeError(f"mutation {index} has unexpected result/identity")
        failure = cases[0].find("failure")
        if (failure is None or cases[0].find("error") is not None
                or cases[0].find("skipped") is not None
                or message not in failure.get("message", "")):
            raise RuntimeError(f"mutation {index} did not fail its safety oracle")
        records.append({"test": case, "mutation": mutation, "message": message,
                        "command": command, "expected_exit": result.returncode})
    (destination / "quarantine-mutations.json").write_text(
        json.dumps({"detected": len(records), "cases": records}, indent=2) + "\n", encoding="utf-8")
    print("3/3 quarantine mutations detected by safety assertions")


if __name__ == "__main__":
    main()
