# PROOF-01 — B3 ↔ REG-RC-01 ↔ I8 (REPAIR integration lab)

**STATUS: LAB EXPERIMENT / NOT CERTIFYING / B1+B2 OPEN / NO REAL G4-A / NO REAL BANK EFFECT.**

## Run from extracted root

```bash
python -m pip install -r requirements.txt
python -m pytest tests/test_provider.py tests/test_repair_integration.py -q -rs
PYTHONPATH=. python scripts/demo_repair.py --out evidence/demo
```

Python 3.11+ on POSIX/Linux; multi-process tests use `fork` and deliberate child `SIGKILL` (-9). No network required. Nothing is written to GitHub and no production resources are touched.

## Components and documentary authority

- `proof_provider/` — reused candidate B3 fixture: *simulated* durable provider effects, Ed25519 receipts, Merkle inclusion and bundle verifier; not actual remote-bank truth.
- `integration/repair.py` — **new lab-only** local I8 ledger and cooperative reconciliation bridge, separate from the TENIR-Gov source tree and its production SQL/JSON paths.
- `integration/witness.py` — separate-path cooperative SQLite witness. It is **not privilege isolated or externally pinned**; valid historical rollback may go undetected.
- `tests/test_repair_integration.py` — suites A–D real executed tests, Suite E `SKIP/OPEN` for system-only vectors, and a deliberately successful direct same-UID SQL bypass control.
- `docs/REG-RC-01_CANDIDATE_v0.1.md` — normative **candidate**, not ratified; the contract must not silently inherit confidence from this lab.
- `docs/RFC7_CANDIDATE_VECTORS.json` — candidate vectors (not adopted or executed as independent conformance).

**Important:** A unit test `PASS` that shows a security bypass is a **security FAIL**. `test_c5_direct_sql_bypass_negative_control` is exactly this case. A test `SKIP` means the vector is unimplemented here, not solved.

## Boundaries demonstrated

- Provider's `EXECUTED` or negative-final fixture receipt is cryptographically checked with an explicitly supplied *pinned* key and checkpoint digest. This lab constructs those pins itself, **not independently**.
- Lost ACK produces `UNKNOWN`, retained reservation; no blind replay in reconciliation.
- Verified provider result enters `REPAIR` if local settlement is delayed, fails or crashes. Receipt digest survives and remains externally readable.
- I8 reservation -> `COMMITTED` or `RELEASED` uses a **single local SQLite transaction**; an interrupted mid-transaction update rolls back. A separate durable intent records unfinished work. No normal SQLite half-commit is claimed.
- On local commit success, a subsequent witness acknowledgement is still required before exposing the terminal view. Crashes after commit re-enter `REPAIR` with durable `settlement_done=1`, then finish without double settlement.
- Concurrent cooperative reconciliations converge on the same terminal result with **one physical ledger event**.
- Missing/corrupted witness is fail-closed for cooperative recovery. The local authority refuses silent re-provisioning on restart; provisioning is explicit.
- Direct SQL by an equal-privilege actor remains possible. Local witness history rollback, provider identity signing-key compromise and direct G4-A bypass are **open system-level failures**.

## Lab-only intentionally missing

- Real grant Standing/Structure/Commit validation, mandatory unique ingress, OS identity boundary, G4-A integration, externally pinned witness/provider checkpoint, independent second verifier implementation, network/provider mutual authentication, crash/power-loss durability on non-test infrastructure.
- `tau_deadline` is a **synthetic test value**; no empirical RFC-5 SLA derivation in this lab. τ_K expiration does not prove absence of external effect.
- A terminal `EXECUTED` consumes committed budget: **available I8 budget does NOT automatically increase on confirmation**. Restoration means reaching a correct recoverable operational posture, not magically releasing spent capacity.
- No `REVIEW_REQUIRED` workflow implementation; an unavailable/mismatched evidence or witness yields `HOLD` without auto-resolution.

## Acceptance gates

A–D green in the POSIX laboratory; E must stay explicitly open until integrated system tests. Do not infer conformance or production authority from test counts. Re-run extraction and suite with exact source digests and independently provisioned trust anchors before any external assurance claim.
