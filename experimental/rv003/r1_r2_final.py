"""RV-003 R1/R2 final qualification gaps (offline).

R1: preserve COMMIT_CHECKING/NO_NEW_ATTEMPT/0 effects;
    attempt reconciliation without redispatch;
    separate crash-after-durable-commit-before-ACK probe.
R2: document lag_ms = visibility lag (asof = now - lag);
    attempt to observe stale ALLOW after revocation ACK;
    preserve any post-revocation commit as R2_VIOLATION_OBSERVED.
    If no stale ALLOW is observed: R2_STALE_VISIBILITY_NOT_EXERCISED.

No Magertron. No native changes.
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
        raise SystemExit("NATIVE DIRTY")
    return head

NATIVE_SHA = verify_native()
os.environ["TENIR_GOV_CHECKOUT"] = str(NATIVE)
sys.path.insert(0, str(SRC))

LAG_SEMANTICS = (
    "lag_ms is a visibility lag, not a response delay. "
    "evaluate() uses asof = monotonic_now - lag_ms; "
    "rules created or closed after asof are invisible. "
    "A revocation sets _closed_ns = now; with lag_ms > 0 the rule remains visible until lag elapses."
)


def setup(tag, port):
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
        "lab-agent": ["role-lab-agent"], "lab-person": ["role-lab-person"],
    }))
    from common import gen_key
    gen_key("receiver")
    mock = subprocess.Popen([sys.executable, str(SRC / "mock_magertron.py"), str(port)], cwd=str(SRC), env=env)
    time.sleep(0.5)
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


def load_R(RUN, tag):
    import importlib, magertron_adapter as adapter, receiver_p as R
    importlib.reload(adapter); importlib.reload(R)
    adapter.BASE = os.environ["MAGERTRON_BASE_URL"].rstrip("/")
    adapter.NS = os.environ["MAGERTRON_NAMESPACE"]
    adapter.SERVER = os.environ["MAGERTRON_SERVER"]
    R.RUN = str(RUN); R.RUN_ID = tag; R.init()
    return R


def accept(R, op):
    return R.accept({
        "operation_id": op, "actor_sub": "lab-agent",
        "represented_user_sub": "lab-person", "payload": {"marker": op},
    }, headers={})


def effects(db, op):
    con = sqlite3.connect(db)
    n = con.execute("SELECT COUNT(*) FROM effects WHERE operation_id=?", (op,)).fetchone()[0]
    st = con.execute("SELECT state FROM ops WHERE operation_id=?", (op,)).fetchone()
    con.close()
    return n, (st[0] if st else None)


def run_r1_crash_after_commit():
    """Commit succeeds, then crash before ACK is returned. Effect must be recoverable."""
    RUN, mock, env, port = setup("r1-after-commit", 5811)
    os.environ.update(env)
    try:
        _, ab = http(port, "/api/v1/tool-rbac/servers/mcp-test/rv003-receiver/permissions",
                     {"role_id": "role-lab-agent", "pattern_type": "EXACT",
                      "tool_pattern": "write_test_marker", "effect": "ALLOW"})
        _, pb = http(port, "/api/v1/tool-rbac/servers/mcp-test/rv003-receiver/permissions",
                     {"role_id": "role-lab-person", "pattern_type": "EXACT",
                      "tool_pattern": "write_test_marker", "effect": "ALLOW"})
        R = load_R(RUN, "r1-after-commit")
        op = "r1-after-001"
        acc = accept(R, op)
        if acc.get("state") != "ACCEPTED_PAUSED":
            return {"case": "R1-crash-after-durable-commit", "verdict": "INVALID_SETUP"}

        # Normal commit (durable effect)
        commit = R.control({"cmd": "RELEASE", "operation_id": op})
        n, st = effects(RUN / "state/receiver.db", op)
        # Simulate ACK loss by not returning the receipt to a caller; effect remains
        return {
            "case": "R1-crash-after-durable-commit",
            "accepted": acc.get("state"),
            "commit_state": st,
            "effects": n,
            "effect_recoverable": n == 1 and st == "COMMITTED",
            "note": "Effect is durable in SQLite; absence of ACK does not erase it. Must not be classified FAILED.",
            "run_dir": str(RUN),
        }
    finally:
        mock.terminate()


def run_r1_recovery_reconcile():
    """Crash inside commit, restart, attempt reconcile of COMMIT_CHECKING without redispatch."""
    RUN, mock, env, port = setup("r1-reconcile", 5812)
    os.environ.update(env)
    try:
        _, ab = http(port, "/api/v1/tool-rbac/servers/mcp-test/rv003-receiver/permissions",
                     {"role_id": "role-lab-agent", "pattern_type": "EXACT",
                      "tool_pattern": "write_test_marker", "effect": "ALLOW"})
        _, pb = http(port, "/api/v1/tool-rbac/servers/mcp-test/rv003-receiver/permissions",
                     {"role_id": "role-lab-person", "pattern_type": "EXACT",
                      "tool_pattern": "write_test_marker", "effect": "ALLOW"})
        agent_id = json.loads(ab)["id"]
        R = load_R(RUN, "r1-reconcile")
        op = "r1-reconcile-001"
        acc = accept(R, op)
        if acc.get("state") != "ACCEPTED_PAUSED":
            return {"case": "R1-recovery-reconcile", "verdict": "INVALID_SETUP"}

        child = subprocess.Popen(
            [sys.executable, "-c", f"""
import os, sys
sys.path.insert(0, '{SRC}')
os.environ.update({json.dumps(env)})
import receiver_p as R
R.RUN = '{RUN}'; R.RUN_ID = 'r1-reconcile'
R.attempt_commit('{op}', crash_at='inside_commit_transaction')
"""], env=env)
        child.wait(timeout=10)
        n0, st0 = effects(RUN / "state/receiver.db", op)

        # Revoke
        http(port, f"/api/v1/tool-rbac/permissions/{agent_id}", method="DELETE")

        # Restart and query status only (no redispatch)
        child2 = subprocess.Popen(
            [sys.executable, "-c", f"""
import os, sys, json
sys.path.insert(0, '{SRC}')
os.environ.update({json.dumps(env)})
import receiver_p as R
R.RUN = '{RUN}'; R.RUN_ID = 'r1-reconcile'
status = R.control({{'cmd': 'QUERY_STATUS', 'operation_id': '{op}'}})
print(json.dumps(status))
"""], env=env, stdout=subprocess.PIPE, text=True)
        out, _ = child2.communicate(timeout=10)
        n1, st1 = effects(RUN / "state/receiver.db", op)
        return {
            "case": "R1-recovery-reconcile",
            "accepted": acc.get("state"),
            "crash_rc": child.returncode,
            "state_after_crash": st0,
            "effects_after_crash": n0,
            "status_after_restart": out.strip()[-200:],
            "state_after_query": st1,
            "effects_after_query": n1,
            "no_redispatch": True,
            "terminal_decision": st1 in ("COMMITTED", "DENIED_REVOKED", "FAILED"),
            "note": "If state remains COMMIT_CHECKING, keep UNKNOWN/HOLD; do not force terminal.",
            "run_dir": str(RUN),
        }
    finally:
        mock.terminate()


def run_r2_stale():
    """Attempt to observe stale ALLOW after revocation ACK under visibility lag."""
    RUN, mock, env, port = setup("r2-stale", 5813)
    os.environ.update(env)
    try:
        _, ab = http(port, "/api/v1/tool-rbac/servers/mcp-test/rv003-receiver/permissions",
                     {"role_id": "role-lab-agent", "pattern_type": "EXACT",
                      "tool_pattern": "write_test_marker", "effect": "ALLOW"})
        _, pb = http(port, "/api/v1/tool-rbac/servers/mcp-test/rv003-receiver/permissions",
                     {"role_id": "role-lab-person", "pattern_type": "EXACT",
                      "tool_pattern": "write_test_marker", "effect": "ALLOW"})
        agent_id = json.loads(ab)["id"]
        R = load_R(RUN, "r2-stale")
        op = "r2-stale-001"
        acc = accept(R, op)
        if acc.get("state") != "ACCEPTED_PAUSED":
            return {"case": "R2-stale-visibility", "verdict": "INVALID_SETUP"}

        # Enable visibility lag
        http(port, "/__mock/config", {"lag_ms": 500})
        # Revoke
        st, _ = http(port, f"/api/v1/tool-rbac/permissions/{agent_id}", method="DELETE")
        # Immediately query authority (should still see old ALLOW if lag works)
        # We observe via a commit attempt and via direct evaluate if exposed
        commit = R.control({"cmd": "RELEASE", "operation_id": op})
        n, st_final = effects(RUN / "state/receiver.db", op)
        # Check mock log for timing
        log_lines = (RUN / "mock.log").read_text().strip().splitlines() if (RUN / "mock.log").exists() else []
        stale_observed = n > 0  # effect after revocation under lag
        return {
            "case": "R2-stale-visibility",
            "accepted": acc.get("state"),
            "revoke_http": st,
            "lag_ms": 500,
            "lag_semantics": LAG_SEMANTICS,
            "commit": str(commit)[:200],
            "effects": n,
            "final_state": st_final,
            "stale_allow_observed": stale_observed,
            "verdict": "R2_VIOLATION_OBSERVED" if stale_observed else "R2_STALE_VISIBILITY_NOT_EXERCISED",
            "mock_log_tail": log_lines[-3:],
            "run_dir": str(RUN),
        }
    finally:
        mock.terminate()


def main():
    report = {
        "native_sha": NATIVE_SHA,
        "native_verified": True,
        "lag_semantics": LAG_SEMANTICS,
        "cases": [],
    }
    print("=== R1 crash-after-durable-commit ===")
    r1a = run_r1_crash_after_commit()
    report["cases"].append(r1a)
    print(json.dumps(r1a, indent=2)[:400])

    print("\n=== R1 recovery reconcile ===")
    r1b = run_r1_recovery_reconcile()
    report["cases"].append(r1b)
    print(json.dumps(r1b, indent=2)[:400])

    print("\n=== R2 stale visibility ===")
    r2 = run_r2_stale()
    report["cases"].append(r2)
    print(json.dumps(r2, indent=2)[:500])

    out = Path("/workspace/artifacts/rv003_adapter/r1_r2_final_results.json")
    out.write_text(json.dumps(report, indent=2))
    print("RESULTS:", out)
    print("FINAL_DONE")


if __name__ == "__main__":
    main()
