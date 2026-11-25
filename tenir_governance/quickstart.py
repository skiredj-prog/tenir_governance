"""A local TENIR-Gov execution walkthrough; every provider effect is synthetic.

This exercises the existing governor, not a second execution engine. The export
is an unsigned diagnostic record. Hashes establish local consistency, not an
independent witness, provider authenticity, or production qualification.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys
import time
from typing import Any

import tenir_governance
import tenir_kernel
from tenir_governance import (
    Agent, CompositionPolicy, DualKernelGovernor, DurableOperationRegistry,
    HashChainedLedger, LedgerEntry, MembraneState, Passage, Resource,
    ScoringKernelAdapter, TransitionMembrane,
)
from tenir_governance.operations.provider import (
    DispatchResult, ProviderOutcome, ReconciliationResult,
)
from tenir_governance.operations.payload_binding import canonical_payload_hash
from tenir_kernel.kernel import TenirKernel

SCHEMA = "tenir-quickstart/v1"
OPERATION_ID = "synthetic-booking-001"
AMOUNT = 20.0
CAPACITY = 100.0
PAYLOAD = {"hotel": "synthetic-hotel", "nights": 1, "unit": "demo-unit"}
# RFC-0 §4 (5.7.6): the scoring kernel never measures four axes, so the admission
# is a FLAG. A FLAG admits only under conditions the deployment declares, and
# their acknowledgement is required at dispatch. This walkthrough declares one
# synthetic condition and acknowledges it explicitly; neither is an assessment.
FLAG_CONDITIONS = ("synthetic walkthrough: an operator reviews the booking before it is sent",)
FLAG_CONDITIONS_MET = {FLAG_CONDITIONS[0]: "synthetic acknowledgement recorded by the walkthrough itself"}
# tau_K is declared because no governed deployment runs without one; this value
# is a walkthrough fixture, derived from no SLA. A real deployment derives it
# from the time its operators really take to resolve a HOLD (RFC-5 §1.1).
TAU_K_SECONDS = 900.0
TAU_K_SOURCE = "synthetic walkthrough fixture: 15 min, not derived from an SLA"
FILES = ("evidence.json", "registry.json")


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _provenance() -> dict[str, Any]:
    sources = {}
    for module in (tenir_governance, tenir_kernel):
        directory = Path(module.__file__).resolve().parent
        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.suffix in (".py", ".json", ".manifest"):
                sources[f"{module.__name__}/{path.relative_to(directory).as_posix()}"] = _sha(path.read_bytes())
    return {
        "product": "TENIR-Gov",
        "package": "tenir-governance",
        "runtime_version": tenir_governance.__version__,
        "python_version": sys.version.split()[0],
        "scenario": "lost-response/v1",
        "source_sha256": sources,
        "scope": "single-process synthetic provider; file-backed operation registry; in-memory budget",
        "independent_attestation": False,
        "hold_benchmark_evidence": False,
    }


class SyntheticHotel:
    """Provider fixture: each execute creates an effect, then loses its reply.

    Deliberately no provider-side deduplication: a second execute would create a
    second booking. Only the governor prevents that call in this walkthrough.
    This provider is not a real integration and has no restart persistence.
    """

    def __init__(self, *, receipt_available: bool = True):
        self.receipt_available = receipt_available
        self.calls: list[dict[str, Any]] = []
        self.receipts: list[dict[str, Any]] = []
        self.reconciliation_calls: list[str] = []

    def execute(self, idempotency_key, action_type, amount, agent_id, metadata=None):
        call = {
            "idempotency_key": idempotency_key, "action_type": action_type,
            "amount": amount, "agent_id": agent_id, "payload": metadata,
            "payload_hash": canonical_payload_hash(metadata),
        }
        self.calls.append(call)
        self.receipts.append({
            **call, "provider_reference": f"synthetic-receipt-{len(self.calls)}",
            "synthetic": True,
        })
        return DispatchResult(
            outcome=ProviderOutcome.UNKNOWN,
            error="Synthetic booking created; response deliberately lost.",
        )

    def reconcile(self, idempotency_key, provider_reference=""):
        self.reconciliation_calls.append(idempotency_key)
        matches = [item for item in self.receipts if item["idempotency_key"] == idempotency_key]
        if not self.receipt_available or len(matches) != 1:
            return ReconciliationResult(resolved=False, outcome=ProviderOutcome.UNKNOWN)
        receipt = matches[0]
        return ReconciliationResult(
            resolved=True, outcome=ProviderOutcome.CONFIRMED,
            provider_reference=receipt["provider_reference"], raw_response=receipt,
        )


def run_demo(output: Path, *, receipt_available: bool = True) -> dict[str, Any]:
    """Run once into a new directory, refusing to overwrite an earlier run."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    provider = SyntheticHotel(receipt_available=receipt_available)
    state = MembraneState()
    resource = Resource(resource_id="demo-budget", capacity=CAPACITY)
    state.register_resource(resource)
    state.register_agent(Agent(agent_id="demo-agent", authority_score=0.9))
    registry = DurableOperationRegistry(store_path=str(output / "registry.json"))
    governor = DualKernelGovernor(
        ScoringKernelAdapter(TenirKernel()), TransitionMembrane(state),
        registry=registry, provider=provider,
        policy=CompositionPolicy(flag_conditions=FLAG_CONDITIONS,
                                 hold_tau_k_seconds=TAU_K_SECONDS, hold_tau_k_source=TAU_K_SOURCE),
    )
    passage = Passage(
        passage_id="synthetic-passage-001", idempotency_key=OPERATION_ID,
        agent_id="demo-agent", action_type="booking", amount=AMOUNT,
    )
    steps = []

    def record(name, response):
        steps.append({
            "step": name, "response": response,
            "provider_dispatches": len(provider.calls),
            "synthetic_effects": len(provider.receipts),
            "budget": {"capacity": resource.capacity, "reserved": resource.reserved,
                       "consumed": resource.consumed},
        })

    # Explicit scenario fixtures, not real assessments of an agent or provider.
    admission = governor.adjudicate(
        passage, irreversibility_materiality=0.0, pressure=0.2, velocity=0.2, capacity=0.9,
        authority=0.9, context_fit=1.0, evidence_quality=1.0, option_space=0.9,
        resource_id="demo-budget", amount=AMOUNT, payload=PAYLOAD,
    )
    record("admission", admission)
    if admission.get("operation_state") != "reserved":
        raise RuntimeError(f"Scenario was not admitted: {admission}")
    record("lost_response", governor.dispatch_and_execute(
        OPERATION_ID, metadata=PAYLOAD, flag_conditions_met=FLAG_CONDITIONS_MET))
    record("retry", governor.dispatch_and_execute(
        OPERATION_ID, metadata=PAYLOAD, flag_conditions_met=FLAG_CONDITIONS_MET))
    record("reconciliation", governor.reconcile_unknown(OPERATION_ID))
    result = {
        "schema": SCHEMA, "provenance": _provenance(),
        "operation_id": OPERATION_ID, "payload": PAYLOAD,
        "payload_hash": canonical_payload_hash(PAYLOAD),
        "flag_conditions": list(FLAG_CONDITIONS), "flag_conditions_met": FLAG_CONDITIONS_MET,
        "receipt_available": receipt_available,
        "steps": steps,
        "provider": {"synthetic": True, "dispatches": provider.calls,
                     "effects": provider.receipts,
                     "reconciliation_calls": provider.reconciliation_calls,
                     "observed_receipt": provider.receipts[0] if receipt_available else None},
        "operation": registry.get(OPERATION_ID).to_dict(),
        "ledger": [asdict(entry) for entry in governor.ledger.entries],
        "execution_seconds": round(time.perf_counter() - started, 6),
    }
    (output / "evidence.json").write_bytes(_json_bytes(result))
    manifest = {"schema": SCHEMA, "files": {name: _sha((output / name).read_bytes()) for name in FILES}}
    (output / "manifest.json").write_bytes(_json_bytes(manifest))
    verify_demo(output)
    return result


def verify_demo(output: Path, *, expected_manifest_sha256: str | None = None) -> dict[str, Any]:
    """Check the fixed local file inventory, ledger chain and scenario facts.

    Does not rerun the provider, call a model, or trust an exported success flag.
    An external expected manifest digest can detect replacement of the whole
    bundle; without it, a wholesale rewrite can recompute all local hashes.
    """
    output = Path(output)
    manifest_bytes = (output / "manifest.json").read_bytes()
    digest = _sha(manifest_bytes)
    if expected_manifest_sha256 is not None and digest != expected_manifest_sha256.lower():
        raise ValueError("Manifest differs from the supplied expected digest")
    manifest = json.loads(manifest_bytes)
    if manifest.get("schema") != SCHEMA or set(manifest.get("files", {})) != set(FILES):
        raise ValueError("Unsupported manifest or unexpected file inventory")
    for name in FILES:
        if _sha((output / name).read_bytes()) != manifest["files"][name]:
            raise ValueError(f"File digest mismatch: {name}")
    evidence = json.loads((output / "evidence.json").read_bytes())

    def require(condition, message):
        if not condition:
            raise ValueError(message)

    require(evidence["schema"] == SCHEMA, "Unsupported evidence schema")
    provenance = evidence["provenance"]
    require(provenance["product"] == "TENIR-Gov" and provenance["package"] == "tenir-governance",
            "Incorrect implementation identity")
    require(provenance["independent_attestation"] is False and provenance["hold_benchmark_evidence"] is False,
            "Unsupported evidence claim")
    require(bool(provenance["runtime_version"]) and bool(provenance["source_sha256"]), "Missing runtime provenance")
    require(evidence["operation_id"] == OPERATION_ID, "Unexpected scenario operation")
    require(evidence["payload"] == PAYLOAD and evidence["payload_hash"] == canonical_payload_hash(PAYLOAD),
            "Payload binding mismatch")
    steps = evidence["steps"]
    require([step["step"] for step in steps] == ["admission", "lost_response", "retry", "reconciliation"],
            "Incomplete or reordered walkthrough")
    admission, lost, retry, reconciled = [step["response"] for step in steps]
    require(admission["operation_state"] == "reserved" and admission["composed_decision"] in ("PASS", "FLAG"),
            "No admissible reservation")
    require(evidence["flag_conditions"] == list(FLAG_CONDITIONS)
            and evidence["flag_conditions_met"] == FLAG_CONDITIONS_MET
            and (admission["composed_decision"] != "FLAG"
                 or admission["flag_conditions"] == list(FLAG_CONDITIONS)),
            "FLAG conditions differ from the declared walkthrough conditions")
    require(lost["dispatched"] is True and lost["operation_state"] == "unknown", "Lost response did not remain UNKNOWN")
    require(retry["dispatched"] is False and retry["operation_state"] == "unknown", "Retry was not refused while UNKNOWN")
    require([step["provider_dispatches"] for step in steps] == [0, 1, 1, 1], "Unexpected provider dispatch count")
    require([step["synthetic_effects"] for step in steps] == [0, 1, 1, 1], "Unexpected synthetic effect count")
    require(steps[0]["budget"] == {"capacity": CAPACITY, "reserved": AMOUNT, "consumed": 0.0}, "Invalid reservation")
    for step in steps[1:]:
        require(step["budget"] == {"capacity": CAPACITY, "reserved": 0.0, "consumed": AMOUNT},
                "Budget changed after its dispatch commitment")
    provider = evidence["provider"]
    require(provider["synthetic"] is True and len(provider["dispatches"]) == len(provider["effects"]) == 1,
            "Expected exactly one synthetic provider call and effect")
    call, effect = provider["dispatches"][0], provider["effects"][0]
    require(call == {"idempotency_key": OPERATION_ID, "action_type": "booking", "amount": AMOUNT,
                     "agent_id": "demo-agent", "payload": PAYLOAD, "payload_hash": canonical_payload_hash(PAYLOAD)},
            "Provider call differs from the admitted operation")
    require(effect == {**call, "provider_reference": "synthetic-receipt-1", "synthetic": True}, "Uncorrelated synthetic effect")
    require(provider["reconciliation_calls"] == [OPERATION_ID], "Reconciliation identity mismatch")
    require(type(evidence["receipt_available"]) is bool, "Invalid receipt availability")
    final_state = "confirmed" if evidence["receipt_available"] else "unknown"
    require(reconciled["operation_state"] == final_state and reconciled["reconciled"] is evidence["receipt_available"],
            "Resolution does not match available evidence")
    if evidence["receipt_available"]:
        require(provider["observed_receipt"] == effect and reconciled["provider_reference"] == effect["provider_reference"],
                "Confirmation lacks its correlated receipt")
    else:
        require(provider["observed_receipt"] is None, "Unavailable receipt was presented as observed")
    operation = evidence["operation"]
    require(operation["state"] == final_state and operation["idempotency_key"] == OPERATION_ID,
            "Final operation mismatch")
    require(operation["payload_hash"] == evidence["payload_hash"], "Registry payload mismatch")
    require(operation["composed_decision"] in ("PASS", "FLAG")
            and (operation["composed_decision"] != "FLAG"
                 or (operation["flag_conditions"] == list(FLAG_CONDITIONS)
                     and operation["flag_conditions_met"] == FLAG_CONDITIONS_MET)),
            "Admission terms or their acknowledgement are missing from the operation record")
    registry = DurableOperationRegistry(store_path=str(output / "registry.json"))
    require(registry.get(OPERATION_ID).to_dict() == operation, "Registry and export disagree")
    ledger = HashChainedLedger()
    ledger.entries.extend(LedgerEntry(**entry) for entry in evidence["ledger"])
    require(ledger.entry_count > 0 and ledger.verify_chain(), "Invalid or missing governance ledger chain")
    require(all(entry.idempotency_key == OPERATION_ID for entry in ledger.entries), "Ledger operation mismatch")
    return {"final_state": final_state, "manifest_sha256": digest, "evidence": evidence}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TENIR-Gov: one synthetic effect, a lost response, and governed recovery.")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("preflight", help="Check local runtime imports; setup time is separate from the walkthrough")
    run = commands.add_parser("run", help="Run the synthetic walkthrough")
    run.add_argument("--output", type=Path, help="New directory for this run (existing directories are refused)")
    run.add_argument("--keep-unknown", action="store_true", help="Withhold the receipt and preserve UNKNOWN")
    verify = commands.add_parser("verify", help="Verify a local walkthrough without contacting any provider")
    verify.add_argument("directory", type=Path)
    verify.add_argument("--expected-manifest-sha256")
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            print(f"Ready: TENIR-Gov {tenir_governance.__version__}; Python {sys.version.split()[0]}; jsonschema {importlib.metadata.version('jsonschema')}.")
            print("No model, network, account or provider credentials are required. Start the five-minute walkthrough clock after setup.")
            return 0
        if args.command == "verify":
            verified = verify_demo(args.directory, expected_manifest_sha256=args.expected_manifest_sha256)
            print(f"Verified local consistency: files, ledger chain, payload, budget, receipt and state ({verified['final_state'].upper()}).")
            print("Unsigned synthetic evidence; no independent attestation or production qualification.")
            return 0
        output = getattr(args, "output", None) or Path("tenir-quickstart-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        result = run_demo(output, receipt_available=not getattr(args, "keep_unknown", False))
        admission = result["steps"][0]["response"]["composed_decision"]
        print(f"TENIR-Gov runtime {tenir_governance.__version__} | source preview | synthetic local walkthrough")
        print(f"1. Admission: {admission}; 20 demo-units reserved. No provider call yet.")
        print("2. Booking created; reply lost. Outcome: UNKNOWN. Provider dispatches: 1.")
        print("3. Retry refused by the governor. Provider dispatches: 1; synthetic bookings: 1.")
        if result["receipt_available"]:
            print("4. Status lookup found the correlated synthetic receipt. Outcome: CONFIRMED.")
        else:
            print("4. Status lookup has no usable receipt. Outcome remains UNKNOWN; no resend or refund.")
        print(f"5. Local export verified: {output.resolve()}")
        print(f"Walkthrough execution: {result['execution_seconds']:.3f}s (excludes setup and human reading).")
        print(f"Manifest SHA-256: {_sha((output / 'manifest.json').read_bytes())}")
        print(f'Verify again: python -m tenir_governance.quickstart verify "{output}"')
        print("Next: read docs/GETTING_STARTED.md and replace the SyntheticHotel provider boundary.")
        print("Scope: one process; synthetic provider; unsigned local evidence; no production qualification.")
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError) as exc:
        print(f"Quickstart failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
