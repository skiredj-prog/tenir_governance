# HOLD capability migration into TENIR-Gov

**Date:** 2026-11-10. **Status:** source inventory and migration contract; full runtime migration is not complete. Product naming follows [PRODUCT_IDENTITY.md](PRODUCT_IDENTITY.md).

The objective is one maintained TENIR-Gov product with one authoritative execution path per operation. Preserve the HOLD prototype as an independently identifiable historical implementation. This inventory does not declare its SDK, profiles, Studio or journal compatible with TENIR-Gov.

## Inspected baselines

- **TENIR-Gov:** source `092bb45ad375a769bed4c51c670f800a6b6b7671`; runtime source unchanged from published tag [`v5.7.0` / `47ef67e3efc5f3008896a7d88bebf929fa0a98f0`](https://github.com/skiredj-prog/tenir_governance/tree/47ef67e3efc5f3008896a7d88bebf929fa0a98f0). Later experimental checkouts are outside this inventory.
- **HOLD prototype:** submission source [`f3bb683d7be76c608b8557bf2d41a89b7abfb6d0`](https://github.com/skiredj-prog/hold/tree/f3bb683d7be76c608b8557bf2d41a89b7abfb6d0). Its benchmark has its own `e1-a6-frozen` anchor; the submission HEAD and benchmark perimeter are not interchangeable.
- **New consolidation work:** the TENIR-Gov quickstart and these documents. Identify its exact commit or local source fingerprints when reporting a run; do not attribute it to the existing PyPI release.

## Capability ownership

| Capability | Existing implementation | Consolidation status and next gate |
|---|---|---|
| Admission and dispatch | TENIR-Gov [`DualKernelGovernor`](https://github.com/skiredj-prog/tenir_governance/blob/47ef67e3efc5f3008896a7d88bebf929fa0a98f0/tenir_governance/composition/governor.py#L300), including `adjudicate`, `dispatch`, `dispatch_and_execute` | Retain as the selected execution path for the new quickstart. Do not place a second independent HOLD dispatcher around it. |
| Unknown outcomes and reconciliation | TENIR-Gov [`DispatchProvider`](https://github.com/skiredj-prog/tenir_governance/blob/47ef67e3efc5f3008896a7d88bebf929fa0a98f0/tenir_governance/operations/provider.py#L103) and [`OperationState`](https://github.com/skiredj-prog/tenir_governance/blob/47ef67e3efc5f3008896a7d88bebf929fa0a98f0/tenir_governance/operations/registry.py#L87); HOLD [`Outcome`](https://github.com/skiredj-prog/hold/blob/f3bb683d7be76c608b8557bf2d41a89b7abfb6d0/hold/outcome.py#L12) | Equivalent concerns already exist in separate implementations. The quickstart exercises TENIR-Gov; full provider/receipt semantics still require an explicit mapping. |
| Guided lost-response experience | HOLD [`JURY_DEMO.md`](https://github.com/skiredj-prog/hold/blob/f3bb683d7be76c608b8557bf2d41a89b7abfb6d0/JURY_DEMO.md) and `experience/` | Adopt the developer journey in a new TENIR-Gov example. This is new evidence against the selected Governor path, not a relabelled HOLD benchmark or a port of all `experience/` behavior. |
| Habilitation and tool SDK | HOLD [`Habilitation` / `Mission`](https://github.com/skiredj-prog/hold/blob/f3bb683d7be76c608b8557bf2d41a89b7abfb6d0/hold/runtime.py#L100), [`habilitation/v1`](https://github.com/skiredj-prog/hold/blob/f3bb683d7be76c608b8557bf2d41a89b7abfb6d0/hold/profile.py#L179) | **Not migrated.** Specify mandate scope, revisions, tool binding, budgets and actor authentication before adding a TENIR-Gov facade. No profile import compatibility is claimed. |
| Human decisions and revocation | HOLD [`Mission.answer`](https://github.com/skiredj-prog/hold/blob/f3bb683d7be76c608b8557bf2d41a89b7abfb6d0/hold/runtime.py#L712), [`Mission.revoke`](https://github.com/skiredj-prog/hold/blob/f3bb683d7be76c608b8557bf2d41a89b7abfb6d0/hold/runtime.py#L851); TENIR-Gov [Profile S commit gate](EP02_GOVERNOR_COMMIT_GATE.md) | **Not equivalent / not migrated.** Profile S is opt-in and disabled by default. A HOLD actor string or profile signature is not automatically an authenticated Profile S authorization. |
| Budget ownership | HOLD mission/effect store; TENIR-Gov [`BudgetCoordinator`](https://github.com/skiredj-prog/tenir_governance/blob/47ef67e3efc5f3008896a7d88bebf929fa0a98f0/tenir_governance/operations/budget.py#L27) and [`SQLiteConsensusBackend`](https://github.com/skiredj-prog/tenir_governance/blob/47ef67e3efc5f3008896a7d88bebf929fa0a98f0/tenir_governance/operations/sqlite_backend.py#L50) | **No shared budget contract yet.** Specify reservation, commit, release and refund phases; one owner performs mutations. Separate stores are not one atomic transaction. |
| Journal and offline verification | HOLD [`hold.event/v1`](https://github.com/skiredj-prog/hold/blob/f3bb683d7be76c608b8557bf2d41a89b7abfb6d0/hold/journal.py#L18), [decision replay](https://github.com/skiredj-prog/hold/blob/f3bb683d7be76c608b8557bf2d41a89b7abfb6d0/hold/replay.py#L164); TENIR-Gov [`HashChainedLedger`](https://github.com/skiredj-prog/tenir_governance/blob/47ef67e3efc5f3008896a7d88bebf929fa0a98f0/tenir_governance/composition/lattice.py#L146) | **No journal-format compatibility.** Preserve original payloads. A new export identifies its own schema and verifier; hashes alone do not establish independent origin or completeness. |
| Studio, Llama, WhatsApp | HOLD `hold/studio/`, model integration, `hold/relay/` | **Not migrated.** They are optional later integrations; none is required for the first TENIR-Gov local demo. Live provider/relay qualification is separate. |
| REG Workbench / conformance | Separate diagnostic and assessment artifacts | **Integration not delivered here.** Matching labels or exporting a JSON record does not establish REG compatibility or conformance. |

## Contract required before the SDK port

The following logical namespaces are the required design crosswalk, **not a new implemented wire schema**. Keep native source fields and identify the translator version when an adapter is introduced.

| Dimension | Required content and rule |
|---|---|
| `authority.*` | Mandate identity, issuer/principal, subject, scope, revision/hash, validity and revocation. A business sign-off does not itself supply dispatch authority. |
| `decision.*` | Historical disposition, reasons, policy/profile identity, input snapshot and time. Keep current disposition separate when unresolved evidence later causes HOLD. |
| `dispatch.*` | Logical effect identity, attempt identity, payload digest, claimed/attempted state and governing runtime. An idempotency key alone does not establish semantic equivalence across missions. |
| `effect.*` | Outcome, provider correlation and evidence reference. Missing response or `NOT_FOUND` is not proof of non-execution. Preserve UNKNOWN until evidence resolves it. |
| `mission.*` | Workflow identity and status. STOP/revocation prevents new actions; late evidence may update the effect without resuming the mission. |
| `budget.*` | Resource/scope, units, reservation/consumption/refund ownership and transition ledger. UNKNOWN does not implicitly release capacity. |
| `evidence.*` | Original schema, artifact hash, producing implementation/version, verifier/version, observed scope and any translation metadata. Preserve original bytes and custody limitations. |

Do not map invariant identifiers by number: HOLD `I1` and a REG/TENIR `I1` are namespaced assertions until their definitions and scopes have been compared. Likewise, a HOLD habilitation is not automatically a REG deployment profile. Missing fields are explicit gaps, not defaults that grant authority.

## Execution and evidence ownership

For each integrated operation, select one authoritative dispatcher, one operation registry and one owner of each budget. Evaluation or read-only projections may be compared in parallel; two runtimes must not both dispatch or reserve for the same operation.

Provider credentials must be available only through the selected enforcement path for any claim that the path governs all relevant effects. A real provider adapter must define correlation, idempotency behavior, confirmed non-execution, uncertain outcomes and reconciliation. A synthetic adapter proves none of those properties for a customer's system.

An export must identify the runtime actually called, including configuration and fallback behavior. Carry source commit or module fingerprints alongside package version: source changes may retain an old package version during development. For translated records, retain the original format and hashes; a presentation adapter cannot confer signatures, independent custody or additional assurance.

## Work order and exit gates

1. **Identity and onboarding.** Publish one developer entry for this source version; demonstrate lost response, refused repeated dispatch and reconciliation through the existing Governor. Check that an unfamiliar developer can observe the benefit within five minutes **after setup**. Setup time and human walkthrough time are separate measurements; automated runtime duration does not establish the human target.
2. **Mandate and SDK contract.** Add the explicit crosswalk above, choose a public facade and map one tool binding. Require tests for scope, revision, expired/replayed authority and payload changes. Preserve the historical HOLD implementation and its tests for comparison.
3. **Mission lifecycle migration.** Port bounded decisions and revocation with separate effect state. Test STOP before/after dispatch, late receipt after STOP, absent/contradictory receipts and no automatic reactivation. Keep UNKNOWN visible across restart.
4. **Persistence and budget qualification.** Test concurrent spending, duplicate identities, crashes before/after each durable write and dispatch, stale storage and replay. Document expected semantic differences rather than forcing matching status strings. Single-host qualification does not transfer to multiple hosts.
5. **Evidence and interface migration.** Add a versioned export adapter and independently runnable verifier before moving Studio/Workbench consumers. Validate original payload preservation and explicit unsupported fields. Run a new declared campaign for any benchmark claim about the integrated runtime.
6. **Provider and deployment qualification.** Select one real integration and test its actual authority, credential isolation, idempotency and reconciliation contract. Publish only the guarantees observed for that provider and deployment. Retire duplicate implementation paths only after the replacement passes the corresponding gates.

The [HOLD hardening plan](https://github.com/skiredj-prog/hold/blob/f3bb683d7be76c608b8557bf2d41a89b7abfb6d0/HARDENING_PLAN.md#frozen-perimeter) preserves E1-A6 protocol, traces, scoring and frozen experiment code. This migration follows that boundary. No result or source artifact from the frozen campaign is rewritten by this work.
