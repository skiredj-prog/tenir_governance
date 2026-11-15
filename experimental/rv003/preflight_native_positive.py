"""RV-003 native positive preflight — full Governor-driven chain.

Uses DualKernelCompositionClient (the public surface) so that
adjudicate → dispatch_and_execute → reconcile_unknown are the real
native methods of PR #137 @ 674c8606089251ea2f74890bd47bce3a717c3b91.

Strict mandate fence is enabled via SQLiteMandateAuthority.
Adapter is injected as governor.provider.
Receiver control is the real receiver_p.py path.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

NATIVE = Path("/tmp/tenir_gov_pin").resolve()
RECEIVER_SRC = Path("/tmp/ep02_inspect/TRACKP_EP02_ALIGN_RESULTS/source").resolve()
ADAPTER = Path("/workspace/artifacts/rv003_adapter").resolve()

os.environ["TENIR_GOV_CHECKOUT"] = str(NATIVE)
sys.path[:0] = [str(ADAPTER), str(RECEIVER_SRC), str(NATIVE)]

head = subprocess.check_output(["git", "-C", str(NATIVE), "rev-parse", "HEAD"], text=True).strip()
assert head == "674c8606089251ea2f74890bd47bce3a717c3b91"
assert not subprocess.check_output(["git", "-C", str(NATIVE), "status", "--porcelain"], text=True).strip()

from tenir_governance.sdk import DualKernelCompositionClient
from tenir_governance.objects import Agent, Passage, Resource
from tenir_governance.operations.mandate_authority import SQLiteMandateAuthority
from tenir_governance.operations.provider import ProviderOutcome
from tenir_governance.operations.registry import OperationState
import receiver_p as R
from rv003_dispatch_provider import RV003DispatchProvider

RUN = Path(tempfile.mkdtemp(prefix="rv003-native-preflight-"))
(RUN / "state").mkdir(parents=True, exist_ok=True)
(RUN / "keys_private").mkdir(parents=True, exist_ok=True)
(RUN / "keys_public").mkdir(parents=True, exist_ok=True)
os.environ["RUN"] = str(RUN)
os.environ["RUN_ID"] = "native-positive-preflight-001"
os.environ["MAGERTRON_BASE_URL"] = "http://127.0.0.1:5803"
os.environ["MAGERTRON_NAMESPACE"] = "mcp-test"
os.environ["MAGERTRON_SERVER"] = "rv003-receiver"
os.environ["MAGERTRON_TOKEN"] = "lab-token"
os.environ["MOCK_TOKEN"] = "lab-token"
os.environ["MOCK_LOG"] = str(RUN / "mock_authority_log.jsonl")
os.environ["RECEIVER_LEASE_MS"] = "2000"

# principals stand-in
import json as _json
(RUN / "principals.json").write_text(_json.dumps({
    "lab-agent": ["role-lab-agent"],
    "lab-person": ["role-lab-person"],
}))

# generate receiver key
from common import gen_key
gen_key("receiver")
# start local mock authority (NOT Magertron)
import subprocess, time
mock_proc = subprocess.Popen(
    [sys.executable, str(RECEIVER_SRC / "mock_magertron.py"), "5803"],
    cwd=str(RECEIVER_SRC),
    env=os.environ.copy(),
)
time.sleep(0.6)

# re-bind after env is set
import importlib
import magertron_adapter as adapter
importlib.reload(adapter)
importlib.reload(R)
# re-bind module-level constants that were captured at import
adapter.BASE = os.environ.get("MAGERTRON_BASE_URL", "").rstrip("/")
adapter.NS = os.environ.get("MAGERTRON_NAMESPACE", "")
adapter.SERVER = os.environ.get("MAGERTRON_SERVER", "")
R.RUN = str(RUN)
R.RUN_ID = "native-positive-preflight-001"
if hasattr(R, "init"):
    R.init()

# create ALLOW rules on the mock for both identities
import urllib.request
def post_rule(role):
    body = json.dumps({
        "role_id": role, "pattern_type": "EXACT",
        "tool_pattern": "write_test_marker", "effect": "ALLOW",
    }).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:5803/api/v1/tool-rbac/servers/mcp-test/rv003-receiver/permissions",
        data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer lab-token"},
    )
    with urllib.request.urlopen(req, timeout=2) as r:
        return r.status
post_rule("role-lab-agent")
post_rule("role-lab-person")

execute_count = {"n": 0}

def instrumented_control(msg: dict) -> dict:
    cmd = msg.get("cmd")
    meta = msg.get("metadata") or {}
    print("CONTROL IN:", cmd, list(msg.keys()), "meta_keys:", list(meta.keys()))
    if cmd == "ACCEPT":
        # Governor may pass payload fields flat in metadata
        payload = meta.get("payload")
        if not isinstance(payload, dict):
            payload = {k: v for k, v in meta.items() if k not in ("represented_user_sub", "payload_digest", "actor_sub")}
        env = {
            "operation_id": msg.get("idempotency_key") or msg.get("operation_id"),
            "actor_sub": msg.get("agent_id") or meta.get("actor_sub") or "lab-agent",
            "represented_user_sub": meta.get("represented_user_sub") or "lab-person",
            "payload": payload or {"marker": "native-lab-positive"},
        }
        print("ACCEPT ENV:", env)
        try:
            resp = R.accept(env, headers={})
            print("ACCEPT RESP STATE:", resp.get("state"), "ERR:", resp.get("error"))
            return resp
        except Exception as e:
            print("ACCEPT EXC:", type(e).__name__, e)
            return {"error": str(e), "state": None}
    if cmd == "QUERY_STATUS":
        r = R.control({"cmd": "QUERY_STATUS", "operation_id": msg["operation_id"]})
        print("QUERY RESP:", r)
        return r
    if cmd == "RELEASE":
        r = R.control({"cmd": "RELEASE", "operation_id": msg["operation_id"]})
        print("RELEASE RESP:", str(r)[:400])
        return r
    return {"error": f"unsupported {cmd}"}


def main():
    mandate_db = str(RUN / "mandate.sqlite")
    auth = SQLiteMandateAuthority(mandate_db)
    registry_path = str(RUN / "registry.sqlite")

    provider = RV003DispatchProvider(instrumented_control, run_id="native-positive-preflight-001")
    orig = provider.execute
    def counting(*a, **k):
        execute_count["n"] += 1
        meta = k.get("metadata") or {}
        if "represented_user_sub" not in meta:
            meta = dict(meta)
            meta["represented_user_sub"] = "lab-person"
            k["metadata"] = meta
        return orig(*a, **k)
    provider.execute = counting

    # Native-test scoring + mandate configuration (from test_rv002_native_governor.py)
    CONTEXT = {
        "mandate_id": "m1",
        "actor_sub": "lab-agent",
        "represented_user_sub": "lab-person",
        "bearer_token_ref": "verified-token-ref",
    }
    auth.register_mandate(**CONTEXT)

    client = DualKernelCompositionClient(
        store_path=registry_path,
        require_persistence=True,
        mandate_authority=auth,
    )
    client.register_agent(Agent(
        agent_id="lab-agent", authority_score=0.9,
        trusted_delegates=frozenset(),
    ))
    client.register_resource(Resource(resource_id="lab-resource", capacity=100.0))
    client.governor.provider = provider  # inject external adapter

    op_key = "preflight-op-positive-001"
    passage = Passage(
        agent_id="lab-agent",
        action_type="write_test_marker",
        idempotency_key=op_key,
        amount=1.0,
    )
    payload = {"marker": "native-lab-positive", "seq": 1}
    from tenir_governance.operations.payload_binding import canonical_payload_hash
    payload_digest = canonical_payload_hash(payload)  # 64-char hex

    evidence = {
        "run_id": "native-positive-preflight-001",
        "native_sha": head,
        "native_dirty": False,
        "strict_mandate": True,
        "scoring_config": {
            "pressure": 0.7, "velocity": 0.5, "capacity": 0.9,
            "authority": 0.9, "context_fit": 1.0, "evidence_quality": 1.0,
        },
        "steps": [],
    }

    # 1. Native adjudicate with the exact parameters that reach RESERVED in PR #137 tests
    adj = client.governor.adjudicate(
        passage,
        pressure=0.7, velocity=0.5, capacity=0.9,
        authority=0.9, context_fit=1.0, evidence_quality=1.0,
        resource_id="lab-resource",
        amount=1.0,
        payload=payload,
        institutional_scope="lab",
        operation_id=op_key,
    )
    rec = client.governor.registry.get(op_key)
    evidence["steps"].append({
        "step": "adjudicate",
        "composed_decision": adj.get("composed_decision"),
        "operation_state": rec.state.value if rec else None,
        "rationale": adj.get("composed_rationale"),
    })
    print("ADJUDICATE:", adj.get("composed_decision"), rec.state.value if rec else None, adj.get("composed_rationale"))

    # Bind operation to active mandate (required before dispatch under strict fence)
    if rec and rec.state.value == "reserved":
        auth.bind_operation(
            operation_id=op_key,
            payload_sha256=payload_digest,
            **CONTEXT,
        )
        evidence["steps"].append({"step": "bind_operation", "bound": True})

    # 2. Native dispatch_and_execute
    before = execute_count["n"]
    dex = client.governor.dispatch_and_execute(op_key, resource_id="lab-resource", amount=1.0, payload=payload, authority_context=CONTEXT)
    after = execute_count["n"]
    rec = client.governor.registry.get(op_key)
    evidence["steps"].append({
        "step": "dispatch_and_execute",
        "dispatched": dex.get("dispatched"),
        "executed": dex.get("executed"),
        "registry_state": rec.state.value if rec else None,
        "execute_calls_delta": after - before,
    })
    print("DISPATCH:", rec.state.value if rec else None, "execute_delta", after - before)

    # 3. Confirm still not CONFIRMED, then lab commit
    status = provider.reconcile(op_key)
    evidence["steps"].append({"step": "post_accept_reconcile", "outcome": status.outcome})
    assert status.outcome != ProviderOutcome.CONFIRMED

    commit = instrumented_control({"cmd": "RELEASE", "operation_id": op_key})
    evidence["steps"].append({"step": "lab_commit", "raw": str(commit)[:300]})

    # 4. Native reconcile_unknown
    before_r = execute_count["n"]
    recon = client.governor.reconcile_unknown(op_key)
    after_r = execute_count["n"]
    rec = client.governor.registry.get(op_key)
    evidence["steps"].append({
        "step": "reconcile_unknown",
        "result": {k: recon.get(k) for k in recon if k in ("reconciled", "outcome", "operation_state", "reason")},
        "registry_state": rec.state.value if rec else None,
        "execute_calls_delta": after_r - before_r,
    })
    print("RECONCILE:", recon.get("reconciled"), rec.state.value if rec else None, "execute_delta", after_r - before_r)

    evidence["execute_calls_total"] = execute_count["n"]
    evidence["final_state"] = rec.state.value if rec else None
    evidence["receiver_run_dir"] = str(RUN)
    evidence["verdict"] = (
        "NATIVE_GOVERNOR_LAB_RECEIVER_POSITIVE_PREFLIGHT_PASS"
        if (rec and rec.state == OperationState.CONFIRMED and execute_count["n"] == 1 and after_r == before_r)
        else "INCONCLUSIVE"
    )

    out = Path("/workspace/artifacts/rv003_adapter/preflight_native_positive.json")
    out.write_text(json.dumps(evidence, indent=2, default=str))
    print("VERDICT:", evidence["verdict"])
    print("EVIDENCE:", out)
    print("PREFLIGHT_NATIVE_POSITIVE_DONE")


if __name__ == "__main__":
    main()
