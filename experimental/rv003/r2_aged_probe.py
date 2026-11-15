"""RV-003 R2 corrected: ALLOW rules aged beyond lag before revocation.

Order:
1. Create ALLOW permissions (no lag).
2. Wait > lag_ms so rules are visible even under lag.
3. Accept operation.
4. Enable lag.
5. Revoke and record ACK.
6. Immediately query authority / attempt commit while lag still hides the revocation.
7. Preserve any post-revocation effect as R2_VIOLATION_OBSERVED.
8. If no stale ALLOW is observed: R2_STALE_VISIBILITY_NOT_EXERCISED.

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
    return head

NATIVE_SHA = verify_native()
os.environ["TENIR_GOV_CHECKOUT"] = str(NATIVE)
sys.path.insert(0, str(SRC))

LAG_MS = 400


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


def run_r2_aged(n_runs=5, port=5814):
    results = []
    for i in range(n_runs):
        RUN, mock, env, port = setup(f"r2-aged-{i}", port)
        os.environ.update(env)
        try:
            # 1. Create ALLOW (no lag)
            _, ab = http(port, "/api/v1/tool-rbac/servers/mcp-test/rv003-receiver/permissions",
                         {"role_id": "role-lab-agent", "pattern_type": "EXACT",
                          "tool_pattern": "write_test_marker", "effect": "ALLOW"})
            _, pb = http(port, "/api/v1/tool-rbac/servers/mcp-test/rv003-receiver/permissions",
                         {"role_id": "role-lab-person", "pattern_type": "EXACT",
                          "tool_pattern": "write_test_marker", "effect": "ALLOW"})
            agent_id = json.loads(ab)["id"]

            # 2. Age the rules beyond lag so they are visible even under lag
            time.sleep((LAG_MS + 100) / 1000.0)

            R = load_R(RUN, f"r2-aged-{i}")
            op = f"r2-aged-{i}"
            acc = accept(R, op)
            if acc.get("state") != "ACCEPTED_PAUSED":
                results.append({"i": i, "verdict": "INVALID_SETUP", "accept": str(acc)[:120]})
                continue

            # 3. Enable lag
            http(port, "/__mock/config", {"lag_ms": LAG_MS})

            # 4. Revoke and record ACK
            st, _ = http(port, f"/api/v1/tool-rbac/permissions/{agent_id}", method="DELETE")
            revoke_t = time.monotonic() * 1000

            # 5. Immediate commit attempt (revocation should still be invisible)
            commit = R.control({"cmd": "RELEASE", "operation_id": op})
            n, st_final = effects(RUN / "state/receiver.db", op)

            # 6. Also capture an authority observation right after revoke
            # (via a second QUERY if available, else from commit receipts)
            log_tail = []
            if (RUN / "mock.log").exists():
                log_tail = (RUN / "mock.log").read_text().strip().splitlines()[-4:]

            verdict = "R2_VIOLATION_OBSERVED" if n > 0 else "NO_EFFECT_UNDER_LAG"
            results.append({
                "i": i,
                "accepted": acc.get("state"),
                "revoke_http": st,
                "revoke_monotonic_ms": revoke_t,
                "commit": str(commit)[:200],
                "effects": n,
                "final_state": st_final,
                "verdict": verdict,
                "mock_log_tail": log_tail,
                "run_dir": str(RUN),
            })
        finally:
            mock.terminate()
            time.sleep(0.15)
    return results


def main():
    report = {
        "native_sha": NATIVE_SHA,
        "native_verified": True,
        "lag_ms": LAG_MS,
        "lag_semantics": "visibility lag: asof = now - lag_ms; rules must be older than lag to remain visible",
        "runs": run_r2_aged(),
    }
    # Aggregate
    verdicts = [r.get("verdict") for r in report["runs"]]
    report["summary"] = {
        "total": len(report["runs"]),
        "violations": verdicts.count("R2_VIOLATION_OBSERVED"),
        "no_effect": verdicts.count("NO_EFFECT_UNDER_LAG"),
        "invalid": verdicts.count("INVALID_SETUP"),
        "overall": "R2_VIOLATION_OBSERVED" if "R2_VIOLATION_OBSERVED" in verdicts else "R2_STALE_VISIBILITY_NOT_EXERCISED",
    }
    out = Path("/workspace/artifacts/rv003_adapter/r2_aged_results.json")
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report["summary"], indent=2))
    print("R2_AGED_DONE")


if __name__ == "__main__":
    main()
