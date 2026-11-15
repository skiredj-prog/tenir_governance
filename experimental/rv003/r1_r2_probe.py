"""RV-003 targeted R1/R2 race probes (offline lab only).

R1: crash inside the check-to-commit window.
R2: authority visibility lag allowing commit after recorded revocation.

No Magertron. No native changes. Uses existing receiver crash_at hooks and mock lag.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SRC = Path("/tmp/ep02_inspect/TRACKP_EP02_ALIGN_RESULTS/source")
NATIVE = Path("/tmp/tenir_gov_base")
os.environ["TENIR_GOV_CHECKOUT"] = str(NATIVE)
sys.path.insert(0, str(SRC))

def setup(tag: str, lag_ms: int = 0):
    RUN = Path(tempfile.mkdtemp(prefix=f"rv003-{tag}-"))
    (RUN / "state").mkdir(parents=True)
    (RUN / "keys_private").mkdir(parents=True)
    (RUN / "keys_public").mkdir(parents=True)
    os.environ["RUN"] = str(RUN)
    os.environ["RUN_ID"] = tag
    os.environ["MAGERTRON_BASE_URL"] = "http://127.0.0.1:5807"
    os.environ["MAGERTRON_NAMESPACE"] = "mcp-test"
    os.environ["MAGERTRON_SERVER"] = "rv003-receiver"
    os.environ["MAGERTRON_TOKEN"] = "lab-token"
    os.environ["MOCK_TOKEN"] = "lab-token"
    os.environ["MOCK_LOG"] = str(RUN / "mock.log")
    os.environ["RECEIVER_LEASE_MS"] = "2000"

    (RUN / "principals.json").write_text(json.dumps({
        "lab-agent": ["role-lab-agent"],
        "lab-person": ["role-lab-person"],
    }))
    from common import gen_key
    gen_key("receiver")

    mock = subprocess.Popen(
        [sys.executable, str(SRC / "mock_magertron.py"), "5807"],
        cwd=str(SRC), env=os.environ.copy(),
    )
    time.sleep(0.6)

    import urllib.request
    def post(path, body=None, method="POST"):
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(
            f"http://127.0.0.1:5807{path}", data=data, method=method,
            headers={"Content-Type": "application/json", "Authorization": "Bearer lab-token"},
        )
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status, r.read()

    if lag_ms:
        post("/__mock/config", {"lag_ms": lag_ms})

    agent = json.loads(post("/api/v1/tool-rbac/servers/mcp-test/rv003-receiver/permissions", {
        "role_id": "role-lab-agent", "pattern_type": "EXACT",
        "tool_pattern": "write_test_marker", "effect": "ALLOW",
    })[1])
    person = json.loads(post("/api/v1/tool-rbac/servers/mcp-test/rv003-receiver/permissions", {
        "role_id": "role-lab-person", "pattern_type": "EXACT",
        "tool_pattern": "write_test_marker", "effect": "ALLOW",
    })[1])

    import importlib
    import magertron_adapter as adapter
    import receiver_p as R
    importlib.reload(adapter)
    importlib.reload(R)
    adapter.BASE = "http://127.0.0.1:5807"
    adapter.NS = "mcp-test"
    adapter.SERVER = "rv003-receiver"
    R.RUN = str(RUN)
    R.RUN_ID = tag
    R.init()
    return RUN, mock, R, agent, person, post


def accept_op(R, op: str):
    return R.accept({
        "operation_id": op,
        "actor_sub": "lab-agent",
        "represented_user_sub": "lab-person",
        "payload": {"marker": op},
    }, headers={})


def count_effects(db: Path, op: str) -> int:
    con = sqlite3.connect(db)
    n = con.execute("SELECT COUNT(*) FROM effects WHERE operation_id=?", (op,)).fetchone()[0]
    con.close()
    return n


def run_r1():
    """Crash inside check-to-commit window. Expect no durable effect."""
    RUN, mock, R, agent, person, post = setup("r1-crash")
    try:
        op = "r1-op-001"
        acc = accept_op(R, op)
        # Crash in a child process so the parent survives
        child = subprocess.Popen(
            [sys.executable, "-c", f"""
import os, sys
sys.path.insert(0, '{SRC}')
os.environ['RUN'] = '{RUN}'
os.environ['RUN_ID'] = 'r1-crash'
os.environ['TENIR_GOV_CHECKOUT'] = '{NATIVE}'
import receiver_p as R
R.RUN = '{RUN}'
R.RUN_ID = 'r1-crash'
R.attempt_commit('{op}', crash_at='inside_commit_transaction')
"""],
            env=os.environ.copy(),
        )
        child.wait(timeout=10)
        crashed = child.returncode == 77
        effects = count_effects(RUN / "state/receiver.db", op)
        con = sqlite3.connect(RUN / "state/receiver.db")
        state = con.execute("SELECT state FROM ops WHERE operation_id=?", (op,)).fetchone()
        state = state[0] if state else None
        con.close()
        return {
            "case": "R1-crash-inside-commit",
            "accepted": acc.get("state"),
            "crashed": crashed,
            "child_rc": child.returncode,
            "effects_after_crash": effects,
            "state_after_crash": state,
            "no_durable_effect": effects == 0,
            "run_dir": str(RUN),
        }
    finally:
        mock.terminate()


def run_r2():
    """Visibility lag: revoke, then commit under lag. Expect possible commit after recorded revocation."""
    RUN, mock, R, agent, person, post = setup("r2-lag", lag_ms=300)
    try:
        op = "r2-op-001"
        acc = accept_op(R, op)
        # Record revocation
        post(f"/api/v1/tool-rbac/permissions/{agent['id']}", method="DELETE")
        time.sleep(0.05)
        # Commit under lag
        commit = R.control({"cmd": "RELEASE", "operation_id": op})
        effects = count_effects(RUN / "state/receiver.db", op)
        return {
            "case": "R2-visibility-lag",
            "accepted": acc.get("state"),
            "commit_result": str(commit)[:300],
            "effects": effects,
            "committed_despite_revocation": effects > 0,
            "run_dir": str(RUN),
        }
    finally:
        mock.terminate()


def main():
    results = {"native_sha": "674c8606089251ea2f74890bd47bce3a717c3b91", "cases": []}
    print("=== R1 ===")
    r1 = run_r1()
    results["cases"].append(r1)
    print(json.dumps(r1, indent=2))

    print("\n=== R2 ===")
    r2 = run_r2()
    results["cases"].append(r2)
    print(json.dumps(r2, indent=2))

    out = Path("/workspace/artifacts/rv003_adapter/r1_r2_results.json")
    out.write_text(json.dumps(results, indent=2))
    print("RESULTS:", out)
    print("R1_R2_DONE")


if __name__ == "__main__":
    main()
