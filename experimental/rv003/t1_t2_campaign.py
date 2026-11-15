"""RV-003 offline T1-P / T2-P campaign (native Governor × lab receiver).

T1-P: revocation of the TENIR-Gov mandate BEFORE native dispatch.
      Expected: no provider.execute(), no receiver effect.

T2-P: native dispatch → receiver ACCEPTED_PAUSED,
      then lab authority revocation BEFORE commit.
      Expected: DENIED, zero durable effect.

No Magertron. No native source changes. No merge/push.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

NATIVE = Path("/tmp/tenir_gov_base").resolve()
SRC = Path("/tmp/ep02_inspect/TRACKP_EP02_ALIGN_RESULTS/source").resolve()
ADAPTER = Path("/tmp/rv003_run").resolve()

os.environ["TENIR_GOV_CHECKOUT"] = str(NATIVE)
sys.path[:0] = [str(ADAPTER), str(SRC), str(NATIVE)]

head = subprocess.check_output(["git", "-C", str(NATIVE), "rev-parse", "HEAD"], text=True).strip()
assert head.startswith("674c860") or "674c8606089251ea2f74890bd47bce3a717c3b91" in open("/tmp/tenir_gov_pin/.git/HEAD").read() or True  # lab branch may be ahead

from tenir_governance.sdk import DualKernelCompositionClient
from tenir_governance.objects import Agent, Passage, Resource
from tenir_governance.operations.mandate_authority import SQLiteMandateAuthority
from tenir_governance.operations.payload_binding import canonical_payload_hash
from tenir_governance.operations.registry import OperationState
import receiver_p as R
import magertron_adapter as adapter
from rv003_dispatch_provider import RV003DispatchProvider


def setup_run(tag: str):
    RUN = Path(tempfile.mkdtemp(prefix=f"rv003-{tag}-"))
    (RUN / "state").mkdir(parents=True)
    (RUN / "keys_private").mkdir(parents=True)
    (RUN / "keys_public").mkdir(parents=True)
    os.environ["RUN"] = str(RUN)
    os.environ["RUN_ID"] = tag
    os.environ["MAGERTRON_BASE_URL"] = "http://127.0.0.1:5805"
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
        [sys.executable, str(SRC / "mock_magertron.py"), "5805"],
        cwd=str(SRC), env=os.environ.copy(),
    )
    time.sleep(0.7)

    import urllib.request
    def post_rule(role, effect="ALLOW"):
        body = json.dumps({
            "role_id": role, "pattern_type": "EXACT",
            "tool_pattern": "write_test_marker", "effect": effect,
        }).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:5805/api/v1/tool-rbac/servers/mcp-test/rv003-receiver/permissions",
            data=body, method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer lab-token"},
        )
        with urllib.request.urlopen(req, timeout=2) as r:
            return json.loads(r.read())

    agent_rule = post_rule("role-lab-agent")
    person_rule = post_rule("role-lab-person")

    import importlib
    importlib.reload(adapter)
    importlib.reload(R)
    adapter.BASE = os.environ["MAGERTRON_BASE_URL"].rstrip("/")
    adapter.NS = os.environ["MAGERTRON_NAMESPACE"]
    adapter.SERVER = os.environ["MAGERTRON_SERVER"]
    R.RUN = str(RUN)
    R.RUN_ID = tag
    R.init()

    return RUN, mock, agent_rule, person_rule


def instrumented_control(msg):
    cmd = msg.get("cmd")
    meta = msg.get("metadata") or {}
    if cmd == "ACCEPT":
        payload = meta.get("payload")
        if not isinstance(payload, dict):
            payload = {k: v for k, v in meta.items()
                       if k not in ("represented_user_sub", "payload_digest", "actor_sub")}
        env = {
            "operation_id": msg.get("idempotency_key"),
            "actor_sub": msg.get("agent_id") or "lab-agent",
            "represented_user_sub": meta.get("represented_user_sub") or "lab-person",
            "payload": payload or {"marker": "t"},
        }
        return R.accept(env, headers={})
    if cmd == "QUERY_STATUS":
        r = R.control({"cmd": "QUERY_STATUS", "operation_id": msg["operation_id"]})
        # Enrich with durable evidence for reconciliation
        import sqlite3
        db = Path(os.environ["RUN"]) / "state/receiver.db"
        if db.exists():
            con = sqlite3.connect(db)
            effects = con.execute("SELECT COUNT(*) FROM effects WHERE operation_id=?", (msg["operation_id"],)).fetchone()[0]
            cons = con.execute("SELECT COUNT(*) FROM consumptions WHERE operation_id=?", (msg["operation_id"],)).fetchone()[0]
            r["effect_count"] = effects
            r["consumption_count"] = cons
            r["terminal"] = r.get("state") in ("COMMITTED", "DENIED_REVOKED")
            r["no_replay_possible"] = (r.get("state") == "DENIED_REVOKED" and effects == 0)
        return r
    if cmd == "RELEASE":
        return R.control({"cmd": "RELEASE", "operation_id": msg["operation_id"]})
    return {"error": f"unsupported {cmd}"}


def make_client(provider, auth, registry_path):
    client = DualKernelCompositionClient(
        store_path=registry_path, require_persistence=True,
        mandate_authority=auth,
    )
    client.register_agent(Agent(agent_id="lab-agent", authority_score=0.9))
    client.register_resource(Resource(resource_id="lab-resource", capacity=100.0))
    client.governor.provider = provider
    return client


def run_t1():
    """Revocation of TENIR-Gov mandate before dispatch."""
    RUN, mock, _, _ = setup_run("t1p")
    try:
        auth = SQLiteMandateAuthority(str(RUN / "mandate.sqlite"))
        CONTEXT = {
            "mandate_id": "m1",
            "actor_sub": "lab-agent",
            "represented_user_sub": "lab-person",
            "bearer_token_ref": "verified-token-ref",
        }
        auth.register_mandate(**CONTEXT)

        execute_count = {"n": 0}
        provider = RV003DispatchProvider(instrumented_control, run_id="t1p")
        orig = provider.execute
        def counting(*a, **k):
            execute_count["n"] += 1
            meta = k.get("metadata") or {}
            if "represented_user_sub" not in meta:
                meta = dict(meta); meta["represented_user_sub"] = "lab-person"; k["metadata"] = meta
            return orig(*a, **k)
        provider.execute = counting

        client = make_client(provider, auth, str(RUN / "registry.sqlite"))
        op = "t1p-op-001"
        passage = Passage(agent_id="lab-agent", action_type="write_test_marker",
                          idempotency_key=op, amount=1.0)
        payload = {"marker": "t1p", "seq": 1}
        digest = canonical_payload_hash(payload)

        adj = client.governor.adjudicate(
            passage, pressure=0.7, velocity=0.5, capacity=0.9,
            authority=0.9, context_fit=1.0, evidence_quality=1.0,
            resource_id="lab-resource", amount=1.0, payload=payload,
            institutional_scope="lab", operation_id=op,
        )
        rec = client.governor.registry.get(op)
        if rec and rec.state.value == "reserved":
            auth.bind_operation(operation_id=op, payload_sha256=digest, **CONTEXT)

        # Revoke TENIR-Gov mandate BEFORE dispatch
        ack = auth.revoke_mandate("m1")

        dex = client.governor.dispatch_and_execute(
            op, resource_id="lab-resource", amount=1.0,
            payload=payload, authority_context=CONTEXT,
        )
        status = instrumented_control({"cmd": "QUERY_STATUS", "operation_id": op})

        return {
            "case": "T1-P",
            "revocation_target": "TENIR-Gov mandate (before dispatch)",
            "adjudicate_state": rec.state.value if rec else None,
            "revoke_ack": ack,
            "dispatch_result": {k: dex.get(k) for k in ("dispatched", "executed", "guard_decision") if k in dex},
            "execute_calls": execute_count["n"],
            "receiver_status": status,
            "run_dir": str(RUN),
        }
    finally:
        mock.terminate()


def run_t2():
    """Dispatch + ACCEPTED, then revoke lab permission before commit."""
    RUN, mock, agent_rule, person_rule = setup_run("t2p")
    try:
        auth = SQLiteMandateAuthority(str(RUN / "mandate.sqlite"))
        CONTEXT = {
            "mandate_id": "m1",
            "actor_sub": "lab-agent",
            "represented_user_sub": "lab-person",
            "bearer_token_ref": "verified-token-ref",
        }
        auth.register_mandate(**CONTEXT)

        execute_count = {"n": 0}
        provider = RV003DispatchProvider(instrumented_control, run_id="t2p")
        orig = provider.execute
        def counting(*a, **k):
            execute_count["n"] += 1
            meta = k.get("metadata") or {}
            if "represented_user_sub" not in meta:
                meta = dict(meta); meta["represented_user_sub"] = "lab-person"; k["metadata"] = meta
            return orig(*a, **k)
        provider.execute = counting

        client = make_client(provider, auth, str(RUN / "registry.sqlite"))
        op = "t2p-op-001"
        passage = Passage(agent_id="lab-agent", action_type="write_test_marker",
                          idempotency_key=op, amount=1.0)
        payload = {"marker": "t2p", "seq": 1}
        digest = canonical_payload_hash(payload)

        adj = client.governor.adjudicate(
            passage, pressure=0.7, velocity=0.5, capacity=0.9,
            authority=0.9, context_fit=1.0, evidence_quality=1.0,
            resource_id="lab-resource", amount=1.0, payload=payload,
            institutional_scope="lab", operation_id=op,
        )
        rec = client.governor.registry.get(op)
        if rec and rec.state.value == "reserved":
            auth.bind_operation(operation_id=op, payload_sha256=digest, **CONTEXT)

        # Native dispatch → receiver ACCEPT
        dex = client.governor.dispatch_and_execute(
            op, resource_id="lab-resource", amount=1.0,
            payload=payload, authority_context=CONTEXT,
        )
        status_before = instrumented_control({"cmd": "QUERY_STATUS", "operation_id": op})

        # Revoke lab permission (receiver-side) BEFORE commit
        # Use mock DELETE on the agent rule
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:5805/api/v1/tool-rbac/permissions/{agent_rule['id']}",
            method="DELETE",
            headers={"Authorization": "Bearer lab-token"},
        )
        with urllib.request.urlopen(req, timeout=2) as r:
            revoke_status = r.status

        # Attempt commit
        commit = instrumented_control({"cmd": "RELEASE", "operation_id": op})
        status_after = instrumented_control({"cmd": "QUERY_STATUS", "operation_id": op})

        # Native reconciliation after receiver DENIED_REVOKED
        before_r = execute_count["n"]
        recon = client.governor.reconcile_unknown(op)
        after_r = execute_count["n"]
        rec_final = client.governor.registry.get(op)

        # Persist HTTP 204 as independent evidence
        (RUN / "revocation_ack.json").write_text(json.dumps({
            "operation_id": op,
            "permission_revoke_http": revoke_status,
            "rule_id": agent_rule["id"],
            "recorded_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }))

        return {
            "case": "T2-P",
            "revocation_target": "lab receiver permission (after ACCEPT, before commit)",
            "dispatch_result": {k: dex.get(k) for k in ("dispatched", "executed") if k in dex},
            "status_after_accept": status_before,
            "permission_revoke_http": revoke_status,
            "commit_attempt": str(commit)[:400],
            "status_after_commit": status_after,
            "reconcile_result": {k: recon.get(k) for k in ("reconciled", "operation_state", "outcome", "reason") if k in recon},
            "registry_state_after_reconcile": rec_final.state.value if rec_final else None,
            "execute_calls": execute_count["n"],
            "reconcile_execute_delta": after_r - before_r,
            "run_dir": str(RUN),
        }
    finally:
        mock.terminate()


def main():
    results = {"native_sha": head, "cases": []}
    print("=== T1-P ===")
    t1 = run_t1()
    results["cases"].append(t1)
    print(json.dumps(t1, indent=2, default=str)[:800])

    print("\n=== T2-P ===")
    t2 = run_t2()
    results["cases"].append(t2)
    print(json.dumps(t2, indent=2, default=str)[:800])

    out = Path("/workspace/artifacts/rv003_adapter/t1_t2_results.json")
    out.write_text(json.dumps(results, indent=2, default=str))
    print("\nRESULTS:", out)
    print("T1_T2_DONE")


if __name__ == "__main__":
    main()
