# EP-01 — Canonical epistemic integration mapping (schema 1.2)

Status: **characterization / proposal**, not an executable document-precedence contract.
Source of authority: `tenir_kernel/schemas/tenir_1_2.schema.json`.

## Reuse, do not invent new epistemic layer

| Existing canonical field | Semantics available today | Documentary case gap |
|---|---|---|
| `epistemic.status`: `ok / contested / invalid` | Canonical classification of epistemic condition | No rule for deriving `contested` from contradictory signed claims |
| `epistemic.evidence_quality` | Bounded quality signal | Not cryptographic authentication or a standing proof |
| `epistemic.manipulation_risk`, `drift`, `epistemic_surrender_risk` | Risk-related signals | Not document priority or issuer mandate |
| `epistemic.confidence_ceiling` | `high / medium / low` | Not admissibility of supersession |
| `constraint_geometry.coordinates.authority_source` | Required nonempty source identifier | Not a validated delegation chain |
| `constraint_geometry.coordinates.context_source` | Required nonempty source identifier | Not a freshness / effective-time rule |
| `synthesis.epistemic_state` | Extensible container | Container existence does not imply enforcement |

## Proposed binding outside the scoring function

A dedicated input validator, distinct from Evaluate, would resolve:

1. document identity and signature;
2. issuer identity and delegated standing within a stated jurisdiction/scope;
3. effective-time evaluation against the supplied deterministic evaluation time;
4. supersession relation under the applicable policy version;
5. contradictory evidence retained as competing claims rather than overwritten.

It would then emit a signed/traceable **documentary authority finding**
carrying `source_refs`, `authority_scope`, `effective_at`,
`policy_version`, `finding` and `evidence_refs`. That finding could
inform the already-canonical epistemic fields and standing controls.
**No claim is made that such finding exists or is trusted today.**

## Acceptance ladder

- **Level 0 — Schema**: canonical fields and source identifiers exist (verified statically).
- **Level 1 — Validation**: reject unsigned, unauthorized, premature or contradictory supersession.
- **Level 2 — Binding**: accepted finding bound to a specific transition and evaluation instant.
- **Level 3 — Runtime**: commit rejects stale/revoked authority under demonstrated backend properties.
- **Level 4 — Conformance**: adversarial test logs and deployment-specific evidence.

The current PR only supports Level 0 and narrow existing scalar-gate tests.
Do not advertise EP-01..04 as Levels 1–4.
