# EP-01 — Epistemic Authority Boundary (characterization only)

Status: DRAFT / NON-NATIVE CONFORMANCE. One runtime behavior changed in the
5.7.7 candidate (not released): missing inputs, below.

## Boundary observed

The ScoringKernelAdapter takes scalar `authority`, `context_fit`, and
`evidence_quality`. It does **not** receive a document graph, issuer
authorizations, supersession relations, or effective dates. Presence of a
scalar alone is not evidence of its authenticity, issuer standing, or source
precedence.

Missing inputs (candidate 5.7.7, rule A of 23 November 2026, pending
ratification of the amended RFC-2 v0.2): a missing input (`None`, JSON `null`)
is unknown, exactly like an invalid one (NaN, ±inf, not a number). It is
insufficient evidence: HOLD, never PASS or FLAG, unless a
conclusive veto governs, that is one the valid measures establish whatever
value the unknown ones would take (an S computed from measured P, V and C at or
below a floor, a hard facet such as the authority floor on a measured value,
option space at its block floor, a membrane invariant). No veto and no reason
rest on a placeholder. A measured value of zero is not missing: `authority=0.0`
meets the authority floor and vetoes. Up to 5.7.6, missing inputs produced
hard gates (HARD_VETO, terminal for the attempt).

A finite value outside the kernel's declared domain `[0, 1]` (`1.5`, `-0.1`)
is neither a measure nor an unknown: it is a malformed request, refused
explicitly at the API boundary as `INVALID_ARGUMENT` before any evaluation
(author's decision of 23 November 2026, P8; draft RFC-2 v0.2 §1.4: the
kernel declares its domain, the standard prescribes none). Nothing is held
and no veto is drawn from it.

## Four documentary acceptance scenarios (not implemented)

| ID | Input | Required disposition | Current coverage |
|---|---|---|---|
| EP-01 | Two authentic documents; replacement not authorized | Do not infer supersession | GAP: no document precedence evaluator |
| EP-02 | Authentic amendment from non-authorized issuer | Reject authority-dependent transition | GAP: no issuer-scope binding |
| EP-03 | Authentic authorized amendment with future effective date | Old rule remains applicable until activation | GAP: no deterministic effective-time resolution |
| EP-04 | Two conflicting sources with equal standing | Freeze affected transition; explicit resolution | GAP: no contradiction lifecycle |

## Executable characterization

`tests/test_ep01_epistemic_boundary.py` covers:
- Missing numeric epistemic inputs hold at the scoring boundary (5.7.7
  candidate; 5.7.6 hard-vetoed), and a conclusive veto still governs them.
- Numeric authority zero is distinct from missing authority, and vetoes.
- No veto rests on the authority placeholder.
- Ed25519-valid but semantically conflicting strings cannot be distinguished
  by the scoring API when their scalar inputs are the same.
- Document-effective-time and precedence are not inputs of that API.

These tests **do not** validate the four documentary acceptance scenarios.
In particular, a passed cryptographic verification test does not establish
issuer authorization, document priority, or admissible execution.

## Next design evidence required

1. Identify canonical epistemic contract and its scope, not just scalar proxies.
2. Bind claims to documents, issuer standing, policy version and effective time.
3. Make conflict preservation and release decisions deterministic and auditable.
4. Prove evaluation-to-commit binding under concurrent changes.
5. Run EP-01..04 against the actual selected integration backend before any
   REG/REG+ conformance assertion.

Workstreams A2/A3 remain independent.
