"""Machine-readable, exact-head, fail-closed JW-01..10 laboratory evidence.

Usage:
  PYTHONPATH=.:w2_integration python audit_tests/qualify_jw01_jw10.py --output-dir /tmp/jw-proof

This runner deliberately NEVER emits an aggregate VERIFIED verdict. Existing
same-host Kernel/Witness custody does not detect a joint rollback, and the
requested topology ID is not the implementation's topology identifier.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parent.parent
BASELINE="c23f9d61c3fa03e81432d766c05fc747b904fa61"
REQUESTED_TOPOLOGY="LAB-TENIR-GOV-PROD-CANDIDATE-09"
TEST_FILES=(
    "audit_tests/test_jw09_jw10_recovery_custody.py",
    "audit_tests/test_jw_joint_rollback_limit.py",
    "w2_integration/test_native_command_binding.py",
    "audit_tests/test_jolt_safety_contract.py",
)
COMPONENTS=(
    "w2_integration/independent_witness.py",
    "w2_integration/native_positive_permit.py",
    "w2_integration/native_dispatch_adapter.py",
    "w2_integration/proof_provider.py",
    "w2_integration/native_dispatch_command.py",
    "w2_integration/w2_2b_canonical.py",
    "tenir_governance/operations/sqlite_backend.py",
    *TEST_FILES,
    "audit_tests/qualify_jw01_jw10.py",
)

# Each entry binds a real, named pytest function to its local JW evidence lane.
# A passing mapped test does NOT imply that full normative JW requirements are
# implemented. Never infer one JW claim merely from another's test result.
MAPPING={
    "JW-01": (
        ("test_native_command_binding","test_command_alone_drives_exact_provider_arguments_and_i8"),
        ("test_native_command_binding","test_legacy_argument_substitution_is_refused_before_consumption"),
    ),
    "JW-02": (
        ("test_jolt_safety_contract","test_public_release_must_prevent_subsequent_positive_dispatch"),
        ("test_native_command_binding","test_stored_outbox_payload_must_match_its_committed_hash"),
    ),
    "JW-03": (
        ("test_jolt_safety_contract","test_pending_replay_must_not_issue_two_concurrent_sends"),
        ("test_jolt_safety_contract","test_review_activation_between_check_and_send_must_fence_dispatch"),
    ),
    "JW-04": (
        ("test_jolt_safety_contract","test_witness_must_bind_its_subject_to_the_current_claim"),
        ("test_jw09_jw10_recovery_custody","test_jw09_foreign_subject_and_command_hash_refused"),
    ),
    "JW-05": (
        ("test_native_command_binding","test_provider_metadata_mutation_does_not_change_attested_payload"),
        ("test_native_command_binding","test_confirmed_replay_keeps_same_command_without_another_send"),
    ),
    "JW-06": (
        ("test_jolt_safety_contract","test_kernel_restore_must_not_create_second_execution_identity"),
        ("test_native_command_binding","test_unknown_restart_reuses_durable_command_and_only_reconciles"),
    ),
    "JW-07": (
        ("test_jolt_safety_contract","test_default_proof_provider_rejects_unclaimed_execution"),
    ),
    "JW-08": (
        ("test_jolt_safety_contract","test_authorized_payload_must_bind_actual_provider_amount"),
        ("test_jolt_safety_contract","test_real_sqlite_lock_wait_must_not_accept_an_expired_permit"),
    ),
    "JW-09": (
        ("test_jw09_jw10_recovery_custody","test_jw09_real_process_crash_leaves_one_witness_and_zero_kernel_outbox"),
        ("test_jw09_jw10_recovery_custody","test_jw09_orphan_checkpoint_reuses_same_execution_and_claim_identity"),
        ("test_jw09_jw10_recovery_custody","test_jw09_legacy_checkpoint_without_binding_not_upgraded"),
    ),
    "JW-10": (
        ("test_jw09_jw10_recovery_custody","test_jw10_ephemeral_rejected_on_verifiable_path"),
        ("test_jw09_jw10_recovery_custody","test_jw10_restart_same_keys_pass_and_changed_witness_fails"),
        ("test_jw09_jw10_recovery_custody","test_jw10_kernel_change_rejected_on_restart"),
    ),
}
LIMIT_TESTS=(
    ("test_jw_joint_rollback_limit","test_joint_kernel_and_witness_snapshot_restore_is_not_detected_locally"),
    ("test_jw_joint_rollback_limit","test_witness_only_rollback_is_detected_by_preserved_kernel_claim"),
)


def _git(*args):
    result=subprocess.run(["git",*args],cwd=ROOT,text=True,capture_output=True,check=True)
    return result.stdout.strip()


def _sha(path:Path)->str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _emit(path:Path,value):
    path.write_text(json.dumps(value,indent=2,sort_keys=True,ensure_ascii=False)+"\n",encoding="utf-8")


def _topology_constant() -> str:
    tree=ast.parse((ROOT/"w2_integration/native_positive_permit.py").read_text())
    for node in tree.body:
        if isinstance(node,ast.Assign):
            if any(isinstance(t,ast.Name) and t.id=="TOPOLOGY_ID" for t in node.targets):
                return ast.literal_eval(node.value)
    raise RuntimeError("native topology identifier missing")


def _junit_cases(path:Path):
    root=ET.parse(path).getroot()
    cases={}
    for case in root.iter("testcase"):
        module=case.attrib.get("classname","").split(".")[-1]
        name=case.attrib.get("name","")
        failures=list(case.findall("failure"))+list(case.findall("error"))
        skipped=bool(case.findall("skipped"))
        cases[(module,name)]={
            "passed":not failures and not skipped,
            "skipped":skipped,
            "failures":[(x.text or "")[:300] for x in failures],
        }
    return cases


def _matching(cases,module,base_name):
    return [x for (m,name),x in cases.items()
            if m==module and (name==base_name or name.startswith(base_name+"["))]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--output-dir",required=True)
    args=parser.parse_args()
    out=Path(args.output_dir).resolve()
    out.mkdir(parents=True,exist_ok=True)
    head=_git("rev-parse","HEAD")
    ancestry=subprocess.run(["git","merge-base","--is-ancestor",BASELINE,head],cwd=ROOT)
    if ancestry.returncode:
        raise RuntimeError("pinned JW-08 baseline not ancestor of candidate head")
    expected_topology=_topology_constant()

    command=[sys.executable,"-m","pytest","-q","-ra","--strict-markers",
             f"--junitxml={out/'pytest-junit.xml'}",*TEST_FILES]
    env=dict(os.environ)
    env["PYTHONPATH"]=os.pathsep.join([str(ROOT),str(ROOT/"w2_integration")])
    run=subprocess.run(command,cwd=ROOT,env=env,capture_output=True,text=True)
    if run.returncode != 0:
        print("QUALIFICATION PYTEST FAILED (raw test diagnostics):",flush=True)
        print(run.stdout[-18000:],flush=True)
        print(run.stderr[-8000:],file=sys.stderr,flush=True)
    (out/"pytest-raw.txt").write_text(
        "$ "+" ".join(command)+"\n\nSTDOUT\n"+run.stdout+
        "\nSTDERR\n"+run.stderr,encoding="utf-8",
    )
    freeze=subprocess.run([sys.executable,"-m","pip","freeze"],capture_output=True,text=True)
    (out/"dependencies-freeze.txt").write_text(
        freeze.stdout if freeze.returncode==0 else freeze.stderr,encoding="utf-8",
    )
    missing=[]
    cases=_junit_cases(out/"pytest-junit.xml") if (out/"pytest-junit.xml").exists() else {}
    matrix={}
    for jw, required in MAPPING.items():
        details=[]
        for module,name in required:
            found=_matching(cases,module,name)
            ok=bool(found) and all(x["passed"] for x in found)
            if not ok:missing.append(jw+":"+module+"."+name)
            details.append({"test":module+"."+name,"observed":len(found),"pass":ok})
        matrix[jw]={"status":"SCOPED_TEST_PASS" if all(x["pass"] for x in details)
                    else "NOT_DEMONSTRATED",
                    "normative_verified":False,"evidence":details}
    limits={}
    for module,name in LIMIT_TESTS:
        found=_matching(cases,module,name)
        ok=bool(found) and all(x["passed"] for x in found)
        limits[name]={"test_observed":len(found),"pass":ok}
        if not ok:missing.append("LIMIT:"+name)

    current_actual=expected_topology
    checks_pass=(run.returncode==0 and not missing)
    # The double-store rollback falsification is a *known security gap*.
    # Passing JW tests CANNOT negate this or confer production VERIFIED.
    limits_exposed=limits["test_joint_kernel_and_witness_snapshot_restore_is_not_detected_locally"]["pass"]
    overall="HOLD_NOT_VERIFIED" if checks_pass and limits_exposed else "TEST_FAILURE"
    topo={
        "requested_topology_id":REQUESTED_TOPOLOGY,
        "implementation_topology_id":current_actual,
        "topology_id_match":REQUESTED_TOPOLOGY==current_actual,
        "kernel_storage":"SQLite WAL",
        "witness_storage":"separate SQLite WAL",
        "custody":"injected lab Ed25519 keys for verifiable mode; local key ID pins",
        "external_monotonic_anchor":False,
        "multi_host_consensus":False,
        "provider_external_effect_tested_by_joint_rollback_case":False,
        "scope":"single-host reproducible lab, not institutional cutover",
    }
    _emit(out/"topology.json",topo)
    sources={}
    for name in COMPONENTS:
        file=ROOT/name
        if not file.is_file():
            missing.append("COMPONENT_MISSING:"+name)
        else:
            sources[name]={"sha256":_sha(file),"size":file.stat().st_size}
    proof={
        "schema":"tenir.jw01-jw10.laboratory-evidence.v1",
        "source_head_sha":head,
        "jw08_baseline_sha":BASELINE,
        "baseline_is_ancestor":True,
        "requested_topology_id":REQUESTED_TOPOLOGY,
        "actual_topology_id":current_actual,
        "verdict":overall if not missing else "TEST_FAILURE",
        "normative_verified":False,
        "jw_evidence":matrix,
        "negative_limits":limits,
        "joint_kernel_witness_rollback_detection_demonstrated":False,
        "reason":"Joint restoration of both local SQLite stores is not detected without an independent monotonic anchor.",
        "pytest_returncode":run.returncode,
        "junit_case_count":len(cases),
        "missing_or_failed_mandatory_tests":missing,
        "component_sha256":sources,
    }
    _emit(out/"qualification-result.json",proof)
    artifact_names=[
        "pytest-raw.txt","pytest-junit.xml","dependencies-freeze.txt",
        "topology.json","qualification-result.json",
    ]
    _emit(out/"evidence-sha256.json",{
        "head_sha":head,
        "files":{name:_sha(out/name) for name in artifact_names if (out/name).exists()},
    })
    print(json.dumps({"candidate_sha":head,"observed_tests":len(cases),
                      "status":proof["verdict"],"normative_verified":False,
                      "topology_id_match":topo["topology_id_match"],
                      "missing":missing},sort_keys=True))
    # CI should be green only for a *correctly documented HOLD* with all
    # required tests observed; it must never consider this global VERIFIED.
    return 0 if proof["verdict"]=="HOLD_NOT_VERIFIED" and not topo["topology_id_match"] else 1


if __name__=="__main__":
    sys.exit(main())
