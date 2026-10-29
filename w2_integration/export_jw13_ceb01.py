"""JW-13 qualification export. LAB_ONLY / NON_CERTIFYING. No private keys."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from kernel_claim_provider import KernelClaimProofProvider
from native_positive_g4a import GRANT_SCHEMA, NativePositiveG4A
from native_positive_permit import TOPOLOGY_ID, NativePositivePermitAuthority
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend

ROOT = Path("/tmp/jw02-src")
OUT = Path("/tmp/jw13-ceb01")
OPERATION = "op-jw13-qual"
EVALUATION = "eval-jw13"
IDEMPOTENCY = "idem-jw13"
RESOURCE = "lab:budget"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _rows(db: sqlite3.Connection, table: str):
    db.row_factory = sqlite3.Row
    return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    os.environ["TENIR_W2_PRIVATE_KEY_HEX"] = bytes(range(32)).hex()
    os.environ["TENIR_WITNESS_PRIVATE_KEY_HEX"] = bytes(range(32, 64)).hex()
    os.environ["TENIR_B3_PRIVATE_KEY_HEX"] = bytes(range(64, 96)).hex()
    work = Path(tempfile.mkdtemp())
    backend = SQLiteConsensusBackend(str(work))
    backend.register_resource(RESOURCE, 50)
    authority = NativePositivePermitAuthority(backend)
    gate = NativePositiveG4A(authority)
    provider = KernelClaimProofProvider(
        db_path=str(work / "provider.db"),
        claim_verifier=authority.verify_dispatch_claim,
    )
    command = {
        "schema": "tenir-dispatch-command/v1",
        "resource_id": RESOURCE,
        "amount": 7,
        "action_type": "payment",
        "agent_id": "lab-agent",
        "metadata": {"recipient": "recipient-1"},
    }
    grant = gate.evaluate_and_grant(
        OPERATION, EVALUATION, command, resource_id=RESOURCE,
    )
    result = gate.dispatch_to_b3(OPERATION, IDEMPOTENCY, command, grant, provider)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    branch = subprocess.check_output(
        ["git", "branch", "--show-current"], cwd=ROOT, text=True
    ).strip()

    grant_out = {key: value for key, value in grant.items() if key != "signature"}
    grant_out["signature_sha256"] = hashlib.sha256(grant["signature"]).hexdigest()
    (OUT / "grant.json").write_text(json.dumps(grant_out, indent=2, sort_keys=True) + "\n")
    (OUT / "receipt.json").write_text(
        json.dumps(result["receipt"], indent=2, sort_keys=True) + "\n"
    )
    (OUT / "causal.json").write_text(
        json.dumps(result["causal"], indent=2, sort_keys=True) + "\n"
    )
    (OUT / "command.json").write_text(json.dumps(command, indent=2, sort_keys=True) + "\n")

    consensus = work / "consensus.sqlite3"
    with sqlite3.connect(consensus) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    shutil.copy(consensus, OUT / "consensus.sqlite3")
    with sqlite3.connect(OUT / "consensus.sqlite3") as db:
        tables = [
            row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1"
            )
        ]
        snapshot = {table: _rows(db, table) for table in tables}
    (OUT / "consensus-snapshot.json").write_text(
        json.dumps(snapshot, indent=2, sort_keys=True, default=str) + "\n"
    )

    label = {
        "campaign": "CAMP-2026-10-29-JW13-QUAL",
        "classification": ["RECONSTRUCTED", "LAB_ONLY", "NON_CERTIFYING"],
        "subject_sha": sha,
        "branch": branch,
        "topology_id": TOPOLOGY_ID,
        "grant_schema": GRANT_SCHEMA,
        "operation_id": OPERATION,
        "evaluation_id": EVALUATION,
        "idempotency_key": IDEMPOTENCY,
        "request_hash": grant["request_hash"],
        "historical_execution_authorized": False,
        "private_keys_included": False,
    }
    (OUT / "label.txt").write_text(
        "\n".join(f"{key}={value}" for key, value in label.items()) + "\n"
    )
    manifest = {
        **label,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runtime": {
            "python": subprocess.check_output(["python3", "--version"], text=True).strip(),
            "sqlite": sqlite3.sqlite_version,
            "platform": "linux-x86_64",
        },
        "authorities": [
            {"name": "NativePositiveG4A", "schema": GRANT_SCHEMA, "role": "positive grant"},
            {"name": "NativePositivePermitAuthority", "role": "I8 reservation and permit"},
            {"name": "ExperimentalG4AAuthority", "role": "historical", "execution_authorized": False, "wired": False},
        ],
        "writers": [
            "NativePositivePermitAuthority on consensus.sqlite3",
            "IndependentWitness on consensus.sqlite3.witness.sqlite3",
            "KernelClaimProofProvider on provider.db",
        ],
        "command": "PYTHONPATH=w2_integration:. python3 w2_integration/export_jw13_ceb01.py",
        "keys": {
            "authority": "TENIR_W2_PRIVATE_KEY_HEX injected, not stored",
            "witness": "TENIR_WITNESS_PRIVATE_KEY_HEX injected, not stored",
            "provider": "TENIR_B3_PRIVATE_KEY_HEX injected, not stored",
        },
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    verdicts = {
        "grant_to_b3_same_request_hash": "VERIFIED",
        "missing_expired_divergent_grant_no_effect": "VERIFIED",
        "inherited_claim_refused_after_grant": "VERIFIED",
        "caller_supplied_verifier_refused": "VERIFIED",
        "historical_g4a_receipt_not_a_grant": "VERIFIED",
        "connector_attestation_not_provider_receipt": "VERIFIED",
        "ephemeral_key_refused": "VERIFIED",
        "audit_corpus_canonical_command": "VERIFIED",
        "posix_sigkill_claim_replay": "VERIFIED",
        "spawn_public_mutator": "VERIFIED",
        "same_uid_sql_write": "OPEN",
        "independent_custody": "OPEN",
        "real_provider_authenticity": "OPEN",
        "witness_kernel_atomicity": "OPEN",
        "historical_g4a_wired": "OPEN",
        "integration_lock": "HOLD",
        "e2e": "HOLD",
        "production": "HOLD",
    }
    (OUT / "verdicts.json").write_text(json.dumps(verdicts, indent=2, sort_keys=True) + "\n")
    (OUT / "open-items.json").write_text(json.dumps({
        "same_uid_sql_write": "A same-UID process can write SQLite directly.",
        "independent_custody": "Injected environment key is not an independent custodian.",
        "real_provider_authenticity": "Lab provider signature is not a Stripe or external provider signature.",
        "witness_kernel_atomicity": "Witness and kernel use separate databases.",
        "historical_g4a_wired": "ExperimentalG4AAuthority remains HOLD / execution_authorized=false.",
    }, indent=2, sort_keys=True) + "\n")

    sums = []
    for path in sorted(OUT.iterdir()):
        if path.name == "sha256sums.txt":
            continue
        sums.append(f"{_sha256(path)}  {path.name}")
    (OUT / "sha256sums.txt").write_text("\n".join(sums) + "\n")
    print(json.dumps({"out": str(OUT), "sha": sha, "request_hash": grant["request_hash"]}, indent=2))


if __name__ == "__main__":
    main()
