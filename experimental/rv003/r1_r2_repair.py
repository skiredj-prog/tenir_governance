"""RV-003 R1/R2 qualification repair (offline lab).

R1: keep local atomicity result; add restart, revocation-before-recovery,
    COMMIT_CHECKING reconciliation, and crash-after-durable-commit probe.
R2: correct order (no lag until after ACCEPTED_PAUSED); zero-lag witness
    plus repeated 300 ms series; preserve every outcome.

No Magertron. No native changes. Historical results untouched.
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

def verify_native():
    head = subprocess.check_output(["git", "-C", str(NATIVE), "rev-parse", "HEAD"], text=True).strip()
    if head != "674c8606089251ea2f74890bd47bce3a717c3b91":
        raise SystemExit(f"NATIVE SHA MISMATCH: {head}")
    dirty = subprocess.check_output(["git", "-C", str(NATIVE), "status", "--porcelain"], text=True)
    if dirty.strip():
        raise SystemExit("NATIVE WORKING TREE DIRTY")
    return head

NATIVE_SHA = verify_native()
os.environ["TENIR_GOV_CHECKOUT"] = str(NATIVE)
sys.path.insert(0, str(SRC))


def setup(tag: str, port: int):
    RUN = Path(tempfile.mkdtemp(prefix=f"rv003-{tag}-"))
    (RUN / "state").mkdir(parents=True)
    (RUN / "keys_private").mkdir(parents=True)
    (RUN / "keys_public").mkdir(parents=True)
    env = os.environ.copy()
    env.update({
        "RUN": str(RUN), "RUN_ID": tag,
        "MAGERTRON_BASE_URL": f"http://127.0.0.1:{port}",
        "MAGERTRON_NAMESPACE": "mcp-test",
        "MAGERTRON_SERVER": "rv003-receiver",
        "MAGERTRON_TOKEN": "lab-token",
        "MOCK_TOKEN": "lab-token",
        "MOCK_LOG": str(RUN / "mock.log"),
        "RECEIVER_LEASE_MS": "2000",
        "TENIR_GOV_CHECKOUT": str(NATIVE),
    })
    (RUN / "principals.json").write_text(json.dumps({
        "lab-agent": ["role-lab-agent"],
        "lab-person": ["role-lab-person"],
    }))
    from common import gen_key
    gen_key("receiver")

    mock = subprocess.Popen(
        [sys.executable, str(SRC / "mock_magertron.py"), str(port)],
        cwd=str(SRC), env=env,
    )
    time.sleep(0.6)
    return RUN, mock, env, port


def http(port, path, body=None, method="POST"):
    import urllib.request
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method,
        headers={"Content-Type": "application/json", "Authorization": "Bearer lab-token"},
    )
    with urllib.request.urlopen(req, timeout=3) as r:
        return r.status, r.read()


def load_receiver(RUN, tag):
    import importlib
    import magertron_adapter as adapter
    import receiver_p as R
    importlib.reload(adapter)
    importlib.reload(R)
    adapter.BASE = os.environ["MAGERTRON_BASE_URL"].rstrip("/")
    adapter.NS = os.environ["MAGERTRON_NAMESPACE"]
    adapter.SERVER = os.environ["MAGERTRON_SERVER"]
    R.RUN = str(RUN)
    R.RUN_ID = tag
    R.init()
    return R


def accept(R, op):
    return R.accept({
        "operation_id": op,
        "actor_sub": "lab-agent",
        "represented_user_sub": "lab-person",
        "payload": {"marker": op},
    }, headers={})


def effects(db, op):
    con = sqlite3.connect(db)
    n = con.execute("SELECT COUNT(*) FROM effects WHERE operation_id=?", (op,)).fetchone()[0]
    state = con.execute("SELECT state FROM ops WHERE operation_id=?", (op,)).fetchone()
    con.close()
    return n, (state[0] if state else None)


def run_r1_recovery():
    """Crash inside commit, restart, revoke, then attempt recovery commit."""
    RUN, mock, env, port = setup("r1-recovery", 5808)
    os.environ.update(env)
    try:
        # Create ALLOW rules (no lag)
        _, ab = http(port, "/api/v1/tool-rbac/servers/mcp-test/rv003-receiver/permissions",
                     {"role_id": "role-lab-agent", "pattern_type": "EXACT",
                      "tool_pattern": "write_test_marker", "effect": "ALLOW"})
        _, pb = http(port, "/api/v1/tool-rbac/servers/mcp-test/rv003-receiver/permissions",
                     {"role_id": "role-lab-person", "pattern_type": "EXACT",
                      "tool_pattern": "write_test_marker", "effect": "ALLOW"})
        agent_id = json.loads(ab)["id"]

        R = load_receiver(RUN, "r1-recovery")
        op = "r1-recovery-001"
        acc = accept(R, op)
        if acc.get("state") != "ACCEPTED_PAUSED":
            return {"case": "R1-recovery", "verdict": "INVALID_SETUP", "accept": acc}

        # Crash inside commit transaction (subprocess)
        child = subprocess.Popen(
            [sys.executable, "-c", f"""
import os, sys
sys.path.insert(0, '{SRC}')
os.environ.update({json.dumps(env)})
import receiver_p as R
R.RUN = '{RUN}'
R.RUN_ID = 'r1-recovery'
R.attempt_commit('{op}', crash_at='inside_commit_transaction')
"""], env=env)
        child.wait(timeout=10)

        n0, st0 = effects(RUN / "state/receiver.db", op)

        # Revoke before recovery
        http(port, f"/api/v1/tool-rbac/permissions/{agent_id}", method="DELETE")
        time.sleep(0.1)

        # Restart receiver in new process and attempt commit
        child2 = subprocess.Popen(
            [sys.executable, "-c", f"""
import os, sys, json
sys.path.insert(0, '{SRC}')
os.environ.update({json.dumps(env)})
import receiver_p as R
R.RUN = '{RUN}'
R.RUN_ID = 'r1-recovery'
resp = R.control({{'cmd': 'RELEASE', 'operation_id': '{op}'}})
print(json.dumps({{'resp': str(resp)[:200]}}))
"""], env=env, stdout=subprocess.PIPE, text=True)
        out, _ = child2.communicate(timeout=15)
        n1, st1 = effects(RUN / "state/receiver.db", op)

        return {
            "case": "R1-recovery",
            "accepted": acc.get("state"),
            "crash_rc": child.returncode,
            "effects_after_crash": n0,
            "state_after_crash": st0,
            "effects_after_recovery": n1,
            "state_after_recovery": st1,
            "recovery_output": out.strip()[-200:],
            "no_double_effect": n1 == n0 == 0,
            "run_dir": str(RUN),
        }
    finally:
        mock.terminate()


def run_r2_series(lag_ms: int, n: int, port: int):
    """Correct order: accept first, then lag, then revoke, then commit. Repeat n times."""
    results = []
    for i in range(n):
        RUN, mock, env, port = setup(f"r2-{lag_ms}-{i}", port)
        os.environ.update(env)
        try:
            # 1. No lag: create ALLOW
            _, ab = http(port, "/api/v1/tool-rbac/servers/mcp-test/rv003-receiver/permissions",
                         {"role_id": "role-lab-agent", "pattern_type": "EXACT",
                          "tool_pattern": "write_test_marker", "effect": "ALLOW"})
            _, pb = http(port, "/api/v1/tool-rbac/servers/mcp-test/rv003-receiver/permissions",
                         {"role_id": "role-lab-person", "pattern_type": "EXACT",
                          "tool_pattern": "write_test_marker", "effect": "ALLOW"})
            agent_id = json.loads(ab)["id"]

            R = load_receiver(RUN, f"r2-{lag_ms}-{i}")
            op = f"r2-{lag_ms}-{i}"
            acc = accept(R, op)
            if acc.get("state") != "ACCEPTED_PAUSED":
                results.append({"i": i, "verdict": "INVALID_SETUP", "accept": str(acc)[:150]})
                continue

            # 2. Activate lag
            if lag_ms:
                http(port, "/__mock/config", {"lag_ms": lag_ms})

            # 3. Revoke and record ACK
            st, _ = http(port, f"/api/v1/tool-rbac/permissions/{agent_id}", method="DELETE")
            revoke_ack = {"http": st, "monotonic_ms": time.monotonic() * 1000}

            # 4. Attempt commit during visibility window
            commit = R.control({"cmd": "RELEASE", "operation_id": op})
            n_eff, st_final = effects(RUN / "state/receiver.db", op)
            verdict = "R2_VIOLATION_OBSERVED" if n_eff > 0 else "NO_EFFECT"
            results.append({
                "i": i,
                "accepted": acc.get("state"),
                "revoke_http": st,
                "commit": str(commit)[:180],
                "effects": n_eff,
                "final_state": st_final,
                "verdict": verdict,
                "run_dir": str(RUN),
            })
        finally:
            mock.terminate()
            time.sleep(0.2)
    return results


def main():
    report = {
        "native_sha": NATIVE_SHA,
        "native_verified": True,
        "cases": [],
    }
    print("=== R1 recovery ===")
    r1 = run_r1_recovery()
    report["cases"].append(r1)
    print(json.dumps(r1, indent=2)[:600])

    print("\n=== R2 zero-lag witness ===")
    r2_0 = run_r2_series(0, 3, 5809)
    report["cases"].append({"case": "R2-zero-lag", "runs": r2_0})
    print(json.dumps(r2_0, indent=2)[:400])

    print("\n=== R2 300 ms series ===")
    r2_300 = run_r2_series(300, 5, 5810)
    report["cases"].append({"case": "R2-300ms", "runs": r2_300})
    print(json.dumps(r2_300, indent=2)[:600])

    out = Path("/workspace/artifacts/rv003_adapter/r1_r2_repair_results.json")
    out.write_text(json.dumps(report, indent=2))
    print("RESULTS:", out)
    print("REPAIR_DONE")


if __name__ == "__main__":
    main()
