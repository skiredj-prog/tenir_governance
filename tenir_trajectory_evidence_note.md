# TENIR-Gov Kernel — Reproduced Adjudication Trajectory[span_1](start_span)[span_1](end_span)

## What this is[span_2](start_span)[span_2](end_span)

A real, executed run of the `tenir-kernel` reference implementation, produced to
substantiate the T0 → boundary → determination → disposition trajectory
discussed with Eduardo. This is not a description of what the repository
*could* produce — it is the actual output of a live run.[span_3](start_span)[span_3](end_span)

## Method[span_4](start_span)[span_4](end_span)

1. `tenir-kernel/api/server.py` was started as-is (`uvicorn api.server:app`,
   the same entrypoint as `run.sh`), with a clean ledger.[span_5](start_span)[span_5](end_span)
2. The six drift events in `r4/examples/sample_events.json` were replayed
   unmodified against `POST /api/v1/adjudicate`, one HTTP call per event.[span_6](start_span)[span_6](end_span)
3. A single fixed `actor_id` (`OPS-BANK-01`) and a single fixed
   `action_context` (`release_scheduled_wire_batch`) were attached to every
   one of the six calls, so that the six adjudications represent
   *the same declared action* evaluated at six successive institutional
   states. This pairing was supplied by the caller for this run — it is not
   something `sample_events.json` itself encodes.[span_7](start_span)[span_7](end_span)
4. Each response was engraved by the kernel's own `GovernanceLedger` into
   `data/governance_ledger.jsonl` (SHA-256 hash-chained, `prev_hash` linking
   each entry to the one before it).[span_8](start_span)[span_8](end_span)
5. The resulting ledger was independently re-verified outside the kernel
   process: every entry's hash was recomputed from its `{timestamp,
   prev_hash, payload}` body and compared against the stored `hash`, and
   every `prev_hash` was checked against the previous entry's `hash`.
   **Result: full chain valid, 6/6 entries.**[span_9](start_span)[span_9](end_span)

## Result[span_10](start_span)[span_10](end_span)

| evt | P   | V   | K    | decision  | S (score) |[span_11](start_span)[span_11](end_span)
|-----|-----|-----|------|-----------|-----------|[span_12](start_span)[span_12](end_span)
| 1   | 0.7 | 0.6 | 1.10 | PASS      | 2.619041  |[span_13](start_span)[span_13](end_span)
| 2   | 0.8 | 0.9 | 1.00 | PASS      | 1.388887  |[span_14](start_span)[span_14](end_span)
| 3   | 1.0 | 1.1 | 0.95 | FLAG      | 0.863636  |[span_15](start_span)[span_15](end_span)
| 4   | 1.1 | 1.3 | 0.85 | HARD_VETO | 0.594405  |[span_16](start_span)[span_16](end_span)
| 5   | 1.2 | 1.5 | 0.70 | HARD_VETO | 0.388889  |[span_17](start_span)[span_17](end_span)
| 6   | 1.3 | 1.7 | 0.60 | HARD_VETO | 0.271493  |[span_18](start_span)[span_18](end_span)

Same declared action, six successive states, verdict flips PASS → FLAG →
HARD_VETO exactly at the declared thresholds (`flag_below = 0.90`,
`hard_veto_below = 0.75`), each step sealed in the attached
`governance_ledger.jsonl`.[span_19](start_span)[span_19](end_span)

Independently, the kernel's own test suite (`tests/test_tenir_kernel.py`,
K1–K8) passes 8/8, and the full-middleware suite
(`tests/test_regression_corpus.py` — 60+ golden cases — and
`tests/test_policy_governance_guarantees.py`, including
`test_tau_boundary_enforcement`) passes 264/264.[span_20](start_span)[span_20](end_span)

## What this does — and does not — establish[span_21](start_span)[span_21](end_span)

**Establishes:** re-adjudication of one fixed, declared action under a
drifting institutional state produces a deterministic, threshold-exact,
immutably logged verdict change (PASS → FLAG → HARD_VETO), reproducible by
anyone running the same six calls against the same repository.[span_22](start_span)[span_22](end_span)

**Does not establish:** revocation of a persistent authorization object.
The public kernel is stateless and per-call — there is no `auth_id` or
grant that survives between calls and is then explicitly revoked. What is
demonstrated is the same action *description* being re-submitted and
re-adjudicated, not a live permission being pulled back mid-flight. Treat
"disposition" as *the next adjudication of the same action is refused and
logged*, not as *a prior grant is torn up*.[span_23](start_span)[span_23](end_span)

The previously declared limitation still holds unchanged: the membrane is
per-action, not aggregate — a wave of concurrent, individually-admissible
actions can still breach institutional capacity in sum.[span_24](start_span)[span_24](end_span)

## Provenance[span_25](start_span)[span_25](end_span)

- Repository: `tenir_governance.zip` / nested `tenir-kernel.zip`, as
  uploaded.[span_26](start_span)[span_26](end_span)
- Environment: Python 3.12.3, FastAPI 0.141.1, uvicorn 0.52.3, run in a
  clean sandbox, same day as this note.[span_27](start_span)[span_27](end_span)
- `sample_events.json` and all kernel source files used unmodified.[span_28](start_span)[span_28](end_span)
