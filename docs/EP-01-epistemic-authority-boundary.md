# EP-01 — Epistemic Authority Boundary (characterization only)

Status: DRAFT / NON-NATIVE CONFORMANCE. No runtime behavior changed.

## Boundary observed

The ScoringKernelAdapter takes scalar `authority`, `context_fit`, and
`evidence_quality`. Missing inputs produce hard gates. It does **not**
receive a document graph, issuer authorizations, supersession relations,
or effective dates. Presence of a scalar alone is not evidence of its
authenticity, issuer standing, or source precedence.

## Four documentary acceptance scenarios (not implemented)

| ID | Input | Required disposition | Current coverage |
|---|---|---|---|
| EP-01 | Two authentic documents; replacement not authorized | Do not infer supersession | GAP: no document precedence evaluator |
| EP-02 | Authentic amendment from non-authorized issuer | Reject authority-dependent transition | GAP: no issuer-scope binding |
| EP-03 | Authentic authorized amendment with future effective date | Old rule remains applicable until activation | GAP: no deterministic effective-time resolution |
| EP-04 | Two conflicting sources with equal standing | Freeze affected transition; explicit resolution | GAP: no contradiction lifecycle |

## Executable characterization

`tests/test_ep01_epistemic_boundary.py` covers:
- Missing numeric epistemic inputs hard-veto at scoring boundary.
- Numeric authority zero is distinct from missing authority.
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
