# Changelog

## [5.7.7] — unreleased candidate, not published

Candidate prepared on 2026-11-23, on `1881c90` (v5.7.6), for conditions (b) and (c) of the HOLD on the REG qualification of 5.7.6, then for the author's decision of 2026-11-23 00:52 UTC (rule A). Not merged, not tagged, not published: each of those steps is a separate release decision on a precise SHA, and the independent review comes first. The source declares `5.7.7`, the version the author set as target, so that no build of this branch presents itself as 5.7.6; until the release decision, 5.7.7 names this candidate, not a published release. The rule it implements is a normative orientation pending ratification of the amended RFC-2 v0.2, not a ratified text. It changes no result published for 5.7.6, which stays published and unchanged.

**This candidate changes documented behaviour.** A 5.7.6 caller runs unchanged, but some of its requests get a different decision: read "Upgrading from 5.7.6".

### Upgrading from 5.7.6

- **No deployment without a declared τ_K.** `DualKernelCompositionClient` now refuses to construct unless its `CompositionPolicy` declares `hold_tau_k_seconds` and `hold_tau_k_source` (`ValueError`, with the reason in words). τ_K is the operational measurement of the real time a human of your organization needs to resolve a HOLD: derive it from your actual SLAs and real-world testing (RFC-0 §6, RFC-5 §1.1); the runtime does not invent it and offers no default. Without it, a HOLD has no end: held operations accumulate in the registry for as long as the deployment lives and `expire_hold` cannot act (measured on this candidate, 2026-11-23: 1000 unresolved HOLDs, none expirable). Runtime author's decision of 2026-11-23 (R11, option C). The library governor `DualKernelGovernor` still accepts a policy without τ_K, for tests and instruments: that is a declared limit, not a deployment path.
- **A measure outside the kernel's declared domain is now rejected explicitly, it is no longer held.** `ScoringKernelAdapter` declares its admissible domain for each of the eight measures it reads (`declared_domain()`: the closed interval `[0, 1]` for each). A finite value outside it (`1.5`, `-0.1`, `2`) is refused at the API boundary as `InvalidArgumentError` (`code = "INVALID_ARGUMENT"`, `field` = the measure(s), the values in the reason) by `adjudicate` and `resolve_hold`, before registration, like every malformed request (R8): nothing is registered, reserved, held or vetoed, the ledger keeps the refusal under kernel `api`, and a HOLD under re-adjudication is untouched. The refusal precedes every evaluation: no veto is drawn from a malformed request, even one whose other measures would be conclusive. Between 19 and 23 November (decision 5 follow-up) such a value was read as an *unknown* measure and the request was closed by a `HOLD`, a disposition; 5.7.6 let the kernel raise and the fail-closed `HOLD` masked any veto. Runtime author's decision of 2026-11-23 10:18 UTC (P8, option A); draft RFC-2 v0.2 §1.4 (not ratified): the kernel declares its domain and rejects any value outside it explicitly, the standard prescribes no domain. **What stays an unknown measure** (rule A, `HOLD` unless a conclusive veto governs): `None`, JSON `null`, `NaN`, `±inf`; a boolean or a non-number too (declared limit of this change, decision 5 unchanged on that point). A caller that passed an out-of-domain value to mean "unknown" must pass `None` or `NaN`; a caller whose instrument produces values outside `[0, 1]` must normalize them, the runtime does not. The rationale `invalid measure(s) (non-finite or outside [0, 1])` now reads `invalid measure(s) (non-finite or not a number)`.
- **`ledger_valid` in a decision result is now an incremental check; the full verification is `verify_ledger()`.** Up to 5.7.6 every decision re-hashed the whole ledger (`verify_chain()`, O(N)), so the cost of a decision grew with the ledger: measured on this candidate on 2026-11-23, 97 % of the time of `adjudicate` at 1000 held operations (R11 liveness measurement). `ledger_valid` now reports `HashChainedLedger.verify_new_entries()`: the entries appended since the previous verification are re-hashed and linked to the verified prefix, whose last entry (the anchor) is re-hashed too. O(k) per decision. It detects an altered or re-forged new link, an altered or replaced anchor, and a truncated prefix. **Declared limit**: an alteration of an older entry inside the already verified prefix is not seen by this check; it is seen by the full verification, `HashChainedLedger.verify_chain()` or `DualKernelGovernor.verify_ledger()`, which is unchanged and is the one to run for an audit, a manifest check or the end of a run. A caller that read `ledger_valid` as a full-chain audit must call `verify_ledger()`. Runtime author's decision of 2026-11-23 10:18 UTC (verify_chain, option A).
- **A measure that is not a number is rejected explicitly too.** A boolean, a string, a container passed as a measure raises `InvalidArgumentError` (`field` = the measure, type in the reason) at the same two entries as an out-of-domain value, before registration. Decision 5 (19 November) read it as an unknown measure (`HOLD`); the author's decision of 2026-11-23 11:22 UTC aligns it with P8: it is not a measure and not an unknown. `None`, JSON `null`, `NaN` and `±inf` stay unknown measures. The rationale `invalid measure(s) (non-finite or not a number)` now reads `invalid measure(s) (non-finite)`. A measure is a Python `int` or `float` (and their subclasses, e.g. `numpy.float64`); `bool`, `Decimal`, `Fraction`, `numpy.int64` and any other type are refused: convert with `float()`. In 5.7.6 those values were not admitted either, they were silently held (`HOLD`, measure read as `None`); the visible change is the explicit refusal. **Declared limit**: the governor checks domain and type only through the scoring adapter's own `malformed_measures()` / `out_of_domain()`; an adapter that declares neither is not checked at the governor's entry (its own entry decides).
- **The ledger is verified in full before every dispatch claim, whatever the caller.** `DualKernelGovernor.dispatch()` re-verifies the whole hash chain from GENESIS under the quarantine gate, immediately before the `RESERVED → DISPATCHING` claim; a ledger that does not verify claims nothing (`dispatched: false`, `ledger_integrity_failed: true`, reason `LEDGER_INTEGRITY_FAILED`, one `api`/`HARD_VETO` ledger entry, the operation stays `RESERVED`). Every path to a claim passes there: `dispatch_and_execute`, a direct `dispatch()` call, `DualKernelCompositionClient.dispatch()`. This closes, at the last step before any external effect, the declared limit of the incremental per-decision check (an altered entry inside the already verified prefix). An earlier state of this candidate (`335c4b3`) ran the verification in `dispatch_and_execute` only, before taking the gate: a direct `dispatch()` call and the SDK client bypassed it, and a window stayed open between the verification and the claim (human review of the project owner, 2026-11-23 14:42 UTC; decision 14:54 UTC, option A). Policy switch `CompositionPolicy.ledger_full_verification_at_dispatch` (default `True`; `False` leaves the full verification to the caller, `verify_ledger()`). Declared cost: linear in the ledger, once per claim, never per HOLD. **Declared limit**: the `HARD_VETO` entry written on failure is appended to a chain that already does not verify, so the chain cannot prove it (`audit_entry_chain_proven: false` in the refusal); it is readable and timestamped.
- **Declared limit, durable registry.** `DurableOperationRegistry` with a `store_path` rewrites the whole store file at every transition, so the cost of a transition grows with the number of records (measured 2026-11-23 on this candidate: 0.95 s per HOLD at 1000 records, 1.38 MB file; 513 s for 1000 HOLDs). Declared in 5.7.7, no change; correction planned for 5.8.0 (incremental or append-only store). τ_K bounds how long records stay open; it does not change the cost of a transition (author's decision of 2026-11-23 11:22 UTC).
- **An absent mandatory measure now gives `HOLD`, not `HARD_VETO`.** Absent means `None`, or JSON `null`, for `pressure`, `velocity`, `capacity`, `authority`, `context_fit`, `evidence_quality` or `irreversibility_materiality` (omitted or `None`, below). In 5.7.6 each of them was a hard gate, terminal for the attempt. Now the attempt is registered in `HOLD`: it is never dispatched while the measure is missing, it resolves only by a complete re-adjudication with the measure (`resolve_hold`), and a conclusive veto still governs (below).
- **A HOLD for insufficient evidence reserves nothing.** When a resource is in scope, the amount is *not* reserved while a measure is missing or invalid (this covers an unmeasured `option_space` too, which 5.7.6 held *with* a reservation). Only the caller can lift such a HOLD, by supplying the measure it withheld, so a reservation would let any caller lock a shared budget by omitting a measure (point R1 of the review of 2026-11-23: ten requests without `authority` reserved a whole budget on the first candidate). The reservation is taken once, at the re-adjudication that completes the evidence, under I8 at that moment; if the budget was consumed meanwhile, that re-adjudication is a fail-closed `HARD_VETO` (`FAILED`, nothing dispatched). The response carries `budget_reserved`. Every other retained operation reserves as before: an admission, and a HOLD on complete evidence (budget escalation, FLAG without a declared condition, fail-closed kernel error), including a HOLD the policy keeps at the re-adjudication of an evidence HOLD. The generic governor still invents no bound: **without a policy `hold_tau_k_seconds`, a HOLD on a missing measure stays open** until resolved, escalated or exited; the exposure is on the registry, not on the budget. Declare `CompositionPolicy(hold_tau_k_seconds=..., hold_tau_k_source=...)` to bound it.
- **A re-adjudication with the measure still missing keeps the `HOLD`.** In 5.7.6, `resolve_hold` without the epistemic inputs gave `HARD_VETO` and closed the attempt `FAILED`.
- **Reason strings changed.** `"<axis> missing: cannot adjudicate without …"` is no longer emitted. A HOLD states `INSUFFICIENT_EVIDENCE: absent measure(s) …` (and `invalid measure(s) …` for an invalid one), ending `S not computed.` when P, V or C is unknown; a veto under an unknown measure ends `Conclusive whatever the unknown measure(s) are: …`. A caller that parses rationales must adapt.
- **The scoring result of an unknown measure carries no geometry report.** `constraint_geometry` and `operating_mode` are `null` (no facet is evaluated on a placeholder); `s_score` is computed when P, V and C are measured; `authority`, `context_fit` and `evidence_quality` are reported as measured, or `null`.
- **An omitted `irreversibility_materiality` is now an unknown measure, no longer `0.0`.** In 5.7.6 the argument defaulted to `0.0`, the most favourable value of that axis, so a caller that never measured it was adjudicated as if it had measured no irreversibility at all. It now defaults to `None`: omitted, the axis is unknown, the attempt is held unless a conclusive veto governs, exactly like the six other mandatory measures, and `declared_unmeasured` names it (author's decision of 2026-11-23 04:54 UTC, after the reviews of the same day: no favourable default for an unmeasured axis, decision D2 of 19 November applied to this axis). **Every caller that omitted the argument gets `HOLD` where it got an admission**: measure the axis, or pass the value you stand by. `0.0` passed explicitly is a measure and keeps the 5.7.6 result.
- **A malformed request now raises `InvalidArgumentError`, it no longer returns a `HARD_VETO`.** A payload that is not canonical JSON, an invalid amount (negative, zero with a resource named, NaN, ±inf, a boolean), a missing Profile S payload digest or invalid Profile S identities are refused at the API boundary: `adjudicate` and `resolve_hold` raise `tenir_governance.composition.InvalidArgumentError` (a `ValueError`, `code = "INVALID_ARGUMENT"`, `field`, `passage_id`, `idempotency_key`, `to_dict()`). Nothing is registered, reserved or adjudicated, and the ledger records the refusal under kernel `api`, decision `INVALID_ARGUMENT`, never as a composed decision (point R8 of the review of 2026-11-23). In 5.7.6 the same requests returned a `HARD_VETO` dict with `operation_state = "not_registered"`, which read like a governance veto. A caller that tested `composed_decision == "HARD_VETO"` on these requests must catch the exception; a transport maps it to its own invalid-argument rejection.
- **A geometry whose facet reads a never-measured axis is rejected at construction.** `ScoringKernelAdapter` raises `ValueError` when any facet of the kernel geometry, hard or soft, reads `cooperative_control_score`, `trajectory_risk_score`, `manipulation_risk` or `drift`: this interface never measures those axes, so such a facet would be evaluated on a placeholder, never on a measure (point R7). The reference geometry reads none of them and is unaffected. A deployment with a custom geometry on one of those axes must measure the axis through the interface, or remove the facet.
- **An absent or invalid `option_space` follows the same path as every other unknown measure.** It gives `HOLD` as in 5.7.6, but the reason now reads `INSUFFICIENT_EVIDENCE: absent measure(s) option_space …` instead of `option_space unmeasured`, `declared_unmeasured` lists it with the four never-measured axes, and the HOLD reserves nothing (above).

### Changed

- **Declared domain, explicit rejection** (`ScoringKernelAdapter.MEASURE_DOMAIN`, `declared_domain()`, `out_of_domain()`; `DualKernelGovernor._refuse_out_of_domain`). The adapter declares `[0, 1]` for its eight measures; a finite value outside it raises `InvalidArgumentError` at the adapter's entry and, before registration, at the governor's (`adjudicate`, `resolve_hold`), through `_refuse_malformed`. The governor prescribes no domain: it enforces the kernel's declaration only, and a scoring adapter that declares none is not checked by the governor. `_finite` keeps the finiteness test alone.
- **Non-number measures refused** (`ScoringKernelAdapter.malformed_measures()`, `_refuse_out_of_domain`): checked before the domain, at the adapter's and the governor's entries.
- **Full ledger verification at the dispatch claim** (`DualKernelGovernor.dispatch()`, under the quarantine gate, via `_refuse_on_ledger_integrity`; `CompositionPolicy.ledger_full_verification_at_dispatch`). The pre-gate check of `_dispatch_and_execute_under_quarantine_lock` is removed: it covered neither a direct `dispatch()` call nor the SDK client.
- **Incremental ledger verification** (`HashChainedLedger.verify_new_entries()`, `verified_count`, `_entry_hash`; `DualKernelGovernor.verify_ledger()`). The three `ledger_valid` fields of `adjudicate` and `resolve_hold` use the incremental check; `verify_chain()` stays the full one and now records the verified prefix as well.
- **Rule A: an absent measure is unknown, exactly like an invalid one** (`ScoringKernelAdapter`, `tenir_governance/composition/governor.py`). The absence of a measure is proof neither of a violation nor of admissibility: `HOLD`, unless a conclusive veto governs, that is one the valid measures establish whatever value the unknown ones would take: a `TAU BREACH` or the kernel's hard-veto floor on an S computed from measured P, V and C; a measured capacity of 0 (S = 0 whatever P and V are); a hard geometry facet violated whatever its unknown axes are (the authority or context-fit floor on a measured value; in general, a facet violated even by the most favourable value of its unknown axes in [0, 1]); option space at its block floor; every membrane invariant (I3, I4, I6, I7, I8, I9), in the composition. A measured value of zero is a measure: `authority=0.0` meets the authority floor and vetoes.
- **No veto and no reason rest on a placeholder.** In 5.7.6 an absent capacity was read as 0.0 and named `TAU BREACH: S=0.0000`, and an invalid authority with `irreversibility_materiality` above 0.75 was vetoed on the authority placeholder 0.5 (`Irreversible-pathway materiality exceeds the authority margin`), although a measured authority of 1.0 satisfies that facet. Both now give `HOLD`.
- **An explicit `irreversibility_materiality=None` no longer raises.** In 5.7.6 the kernel raised on it, and the governor's fail-closed `HOLD` (`Scoring kernel error`) then masked any conclusive veto, such as a `TAU BREACH`. It is now an absent measure under rule A.
- **No reservation on insufficient evidence** (`DualKernelGovernor.adjudicate`, `resolve_hold`). The scoring result carries `insufficient_evidence`; a composed `HOLD` on it reserves no budget, locally or on the consensus backend, and the ledger records the deferral (`hold_lifecycle`). `resolve_hold` reserves when the evidence is complete and the operation is retained (`PASS`, `FLAG`, or a policy `HOLD`); a failed reservation there is a fail-closed `HARD_VETO`; a reservation taken in a re-adjudication that then retains the HOLD (quarantine, lost transition) is released again. Responses of `adjudicate` and `resolve_hold` carry `budget_reserved`.
- **`irreversibility_materiality` is a mandatory measure** (`ScoringKernelAdapter.adjudicate`, `DualKernelGovernor.adjudicate`, `resolve_hold`, `DualKernelCompositionClient.adjudicate`): default `None`, no declared default any more; the facet "irreversible-pathway materiality exceeds the authority margin" is evaluated on a measure or not at all.
- **No facet is evaluated on a placeholder, and unknown axes are `UNKNOWN`** (`ScoringKernelAdapter`). The four axes this interface never measures keep their coordinate placeholder for the kernel's own bookkeeping, but no facet may read them (rejected at construction, above), and an absent or invalid measured axis is routed to the insufficient-evidence path before any facet is evaluated: no default value can trigger a geometry veto. `declared_unmeasured` names the unknown measured axes and the four never-measured ones.
- **`INVALID_ARGUMENT` is an API refusal, not a decision** (`tenir_governance/composition/errors.py`, `DualKernelGovernor._refuse_malformed`). The four entry rejections of `adjudicate` and the amount check of `resolve_hold` raise `InvalidArgumentError`; the ledger entry is kept under kernel `api`. A governance veto (`HARD_VETO`) is only ever the result of an adjudication.
- `docs/EP-01-epistemic-authority-boundary.md`: missing inputs hold; a measured zero vetoes. `docs/agentic-governance-guide.md`: the example of a call without `authority` now shows the `HOLD` (it asserted the 5.7.6 `HARD_VETO`).
- Added `docs/caller-contract-stale-contradictory.md`: what the caller must do with a `STALE` or `CONTRADICTORY` measure, since this interface carries one value per measure, with no age, no source and no second reading.

### Unchanged

- An invalid measure (NaN, ±inf, a non-number) gives `HOLD`, and a conclusive veto governs it (decision 5). A finite value outside `[0, 1]` is no longer in this list: it is rejected at entry (above).
- `verify_chain()` is unchanged: full re-hash of every entry, from GENESIS; tamper detection on any entry. An absent or invalid `option_space` gives `HOLD` (5.7.6, decision 2).
- A structurally malformed request is still rejected at entry, before any adjudication: nothing is registered or reserved. Only its label changed (`InvalidArgumentError`, above); it is not confused with an unknown measure.
- `STALE` and `CONTRADICTORY` stay the caller's responsibility (declared limit): the runtime cannot tell them apart from a current value.

### Tests

- `tests/test_ledger_incremental_verification.py` (+3): a direct `dispatch()` call on a ledger that does not verify refuses, takes no claim, writes the `api`/`HARD_VETO` entry and reports `audit_entry_chain_proven: false`, and the chain stays broken; `DualKernelCompositionClient.dispatch()` is covered by the same verification; an intact ledger lets a direct `dispatch()` claim.
- `tests/test_declared_domain_rejection.py` (+8 cases): a boolean, a string, a list and a dict are malformed and refused at the adapter and at the governor (nothing registered); `None`, `NaN`, `±inf` are neither malformed nor out of domain. `tests/test_ledger_incremental_verification.py` (+3): an old entry altered inside the verified prefix refuses the dispatch, fail-closed, with no claim taken; an intact ledger dispatches; with the policy switch off the caller keeps `verify_ledger()`; the switch must be a bool. `tests/test_non_finite_measure_guard.py`: the two non-number cases expect `InvalidArgumentError`.
- `tests/test_declared_domain_rejection.py` (73 cases): the kernel declares a finite domain for each of the eight measures; its edges are values; `None`, `NaN`, `±inf`, a boolean and a string are not out of domain; every finite value outside `[0, 1]` on every axis raises `InvalidArgumentError` with the axis as field and the value in the reason; several out-of-domain measures are all named; the refusal precedes a conclusive veto; `None` and `NaN` still hold; at the governor nothing is registered, nothing reserved, one `api`/`INVALID_ARGUMENT` ledger entry, no composed decision, not a fail-closed HOLD; `resolve_hold` refuses and leaves the HOLD untouched, then resolves with a measure; a scoring adapter without a declared domain is not checked by the governor.
- `tests/test_ledger_incremental_verification.py` (10 cases): empty ledger; the incremental check covers the new entries and agrees with the full check; a tampered new link, a re-hashed but unlinked new link, a tampered anchor, a replaced anchor and a truncated prefix are detected incrementally; an altered old entry inside the prefix is the declared limit and is caught by the full check; the per-decision cost does not grow with the ledger (50 vs 5000 entries); the governor reports the incremental check and offers the full one.
- `tests/test_non_finite_measure_guard.py`: the nine out-of-range cases and the two "TAU BREACH over out-of-range evidence" cases now expect `InvalidArgumentError` (they asserted `HOLD`). `tests/test_unknown_axes_and_invalid_argument.py`: `1.5` is no longer an unknown option space. `tests/test_absent_measure_is_unknown.py`: rationale wording.
- `tests/test_sdk_requires_tau_k.py`: the client refuses a policy without τ_K and the default policy, says why, constructs with a sourced τ_K; a τ_K without a source is still refused; the library governor still accepts a policy without τ_K (declared limit).
- `tests/test_absent_measure_is_unknown.py` (15 cases): absent P, V, K and their combinations give `HOLD` with no S and no `TAU BREACH`; a measured K = 0 with P or V absent vetoes; an authority, context_fit or option-space veto, and a membrane invariant, govern an absent K; the HOLD resolves once the measure is supplied; JSON `null` on the SDK path holds.
- `tests/test_ep01_epistemic_boundary.py`: a missing authority, context_fit or evidence_quality holds, like an invalid one, with S computed from the measured P, V and C; a TAU BREACH, the kernel floor, a measured capacity of 0 and the option-space floor govern it; a measured floor veto on one input governs another missing one; `authority=0.0` vetoes; no veto on the authority placeholder; an explicit `irreversibility_materiality=None` holds, and a TAU BREACH governs it; a facet violated whatever its unknown axis is vetoes (custom geometry).
- `tests/test_absent_measure_hold_lifecycle.py` (28 cases): for each of the seven mandatory measures, `HOLD`, nothing reserved, no dispatch and no provider call; a re-adjudication with the measure still missing keeps the HOLD and reserves nothing; the measure arrives, then one reservation and exactly one dispatch; `authority=0.0` at re-adjudication vetoes; a duplicate idempotency key neither reserves nor admits; `τ_K` exhausted fails closed even when the measure arrives (`CONFIRMED_NONEXECUTION`); `expire_hold` fails closed with nothing to release; without a declared `τ_K` the HOLD stays open but holds no budget (declared limit); ten incomplete requests cannot lock the budget and a complete one is admitted (R1); a budget consumed while held fails closed at re-adjudication (I8); a re-adjudication to a policy HOLD on complete evidence reserves; an unmeasured option space and an invalid measure reserve nothing; a HOLD on complete evidence still reserves; an authorized exit from `ESCALATED` returns to `HOLD`; a revoked delegation vetoes whatever the absent measure, at adjudication and at re-adjudication; a malformed request is rejected at entry, never held; JSON `null` authority on the SDK path holds.
- `tests/test_issue141_hold_budget_resolution.py`: the #141 property (a HOLD's reservation is never counted twice at re-adjudication) is kept on the reservation now taken at resolution; an unreserved evidence HOLD neither blocks later complete operations nor is protected from them.
- `tests/test_b03_blocking_reserve.py`: the retained operation that exercises the external reservation path now measures `option_space` (an unmeasured option space is an evidence HOLD and reserves nothing).
- `tests/test_ep01_epistemic_boundary.py` (omitted axis): an omitted `irreversibility_materiality` holds and is declared unmeasured, with no geometry report; `0.0` passed explicitly is a measure and keeps the 5.7.6 result; a TAU BREACH and the authority floor govern it; the materiality facet never vetoes on the unknown axis; the governor holds it and reserves nothing; the SDK has no default for it. Every other test that omitted the axis now measures it explicitly (`irreversibility_materiality=0.0`), as the quickstart and the guide examples do.
- `tests/test_unknown_axes_and_invalid_argument.py` (29 cases): a hard or soft facet on any of the four never-measured axes is rejected at construction, the reference geometry is accepted; absent and invalid measured axes give `HOLD` with no geometry report, no `TAU BREACH` and no reservation, whatever placeholder-dependent facet the geometry holds; an absent `option_space` is declared unmeasured and reserves nothing, and reserves at re-adjudication; seven malformed requests raise `InvalidArgumentError` with the right field, register nothing, reserve nothing and leave one `api`/`INVALID_ARGUMENT` ledger entry on a valid chain; `to_dict()` carries no decision field.
- `tests/test_composition.py`: the three tests that asserted the 5.7.6 hard gate on missing epistemic inputs now assert `HOLD`; the non-canonical payload and the invalid amounts now expect `InvalidArgumentError`. `tests/test_ep02_governor_commit_gate.py`: the missing Profile S digest expects `InvalidArgumentError`.
- `tests/test_deployment_manifest.py`: the release version of the test manifests is read from the package, and the version test expects 5.7.7.

### Verification scope

Every result quoted for this candidate was obtained by its author, in one process, on in-memory registries and budgets, with the governor's and the hold manager's clocks replaced for the `τ_K` cases, and a mock provider. Nothing here was observed on durable storage, under concurrent processes, after a crash and recovery, or against a real provider or consensus backend (the B03 tests exercise the single-host lock backend and a paused in-process backend, not a networked one). The idempotence and non-execution guarantees of the absent-measure HOLD are therefore shown for that scope only. An independent, named review has not taken place; the counts quoted in the review request are local results, not a qualification.

### Addendum on 5.7.6, dated 2026-11-23 (not published: its publication waits for the independent review)

This addendum is appended; the 5.7.6 entry below and the evidence of that release are left as published. The 5.7.6 entry says that the reg-conformance harness had not been re-run on that code. It was run on the distribution on 2026-11-22, and the release controller decided, on `1881c90` only:

- **Provenance: PASS.** The PyPI wheel and sdist are the 5.7.6 of `1881c90`: PEP 740 attestations verified offline (online Sigstore verification is a stated reserve), the 69 files of the wheel identical to the commit.
- **REG qualification: HOLD.** With the released REG v0.7.0 harness and its TENIR-Gov port, exit code 1: CT-R4-001 and CT-R4-002 find no admission, because 5.7.6 holds every FLAG when no condition is declared. With a port that declares a FLAG condition and acknowledges it at dispatch, which is part of no REG revision, exit code 2 (INCOMPLETE). That is not a PASS.
- **Priority of HARD_VETO: PASS on the cases examined.** No veto was masked as a HOLD.
- **Rule of 20 November on K: open deviation.** 5 of the 49 cases judged give HARD_VETO for an absent P, V or K where the rule gives HOLD. This is what the change above addresses.
- **General production: HOLD, maintained.**
- **Tests on the distribution: 1456 of 1457.** The 1457 tests of the release workflow ran on the tagged checkout. From the sdist, `tests/test_guide_examples.py` fails because the sdist does not ship `docs/agentic-governance-guide.md`.

5.7.6 stays what its entry says: a technical single-host release with bounded guarantees, not REG-certified. The PyPI and GitHub releases stay published; no withdrawal and no emergency fix follow from these results.

## [5.7.6] — 2026-11-20 — Single-host pilot line: the three 5.7.5 trust boundaries closed, #141 fixed

Second release of the 5.7.5 single-host pilot line. 5.7.5 was merged on `main` (`42513bf`) and held there by its release decision: three of the limits its own "Scope, known limits and trust boundaries" states were judged blocking for a pilot (a proof of non-execution asserted by its caller, FLAG conditions declared but not enforced, no re-validation of the mandate before dispatch), and #141 held its qualification. No v5.7.5 tag and no PyPI publication were made; its entry below stays as the record of that unpublished candidate. 5.7.6 closes those four points on the same code line, with no other change of scope. FM-20 stays Open and multi-host stays disabled; remediation target 5.8.0, as announced in 5.7.0.

**What this release is.** A technical release of the single-host line, with bounded guarantees, for a controlled pilot on one host. It is not a REG certification (the reg-conformance harness has not been re-run on this code; see "Scope, known limits and trust boundaries"), not a general production qualification, and not the completion of the "Auditable Evidence & Pilot Readiness" milestone.

**Not a strict-semver patch.** This release adds public API on top of 5.7.5 (`tenir_governance.operations.attestation`, an `attestation_verifier` on the governor and the client, `CompositionPolicy.revalidate_mandate_at_dispatch`, a `flag_conditions_met` keyword on `dispatch` and `dispatch_and_execute`, new `OperationRecord` and `NonExecutionAttestation` fields) and changes documented behaviour; each change is listed under "Behaviour change" below. A 5.7.5 caller does not run unchanged on 5.7.6: with the default policy every FLAG is now held, a dispatch needs the acknowledgement of the declared FLAG conditions, an unsigned attestation is no longer proof, and a call without `option_space` is held. Read "Upgrading from 5.7.5".

### Upgrading from 5.7.5 (and from 5.7.0)

- **Declare the conditions under which a FLAG authorizes**: `CompositionPolicy(flag_conditions=("...",))`. With the default policy (no condition) a FLAG now composes to `HOLD`. Because four axes are never measured, the scoring kernel gives `FLAG` at best on the default path, so a deployment that declares no condition admits nothing: this is RFC-0 §4 applied, not a defect.
- **Acknowledge them at dispatch**: `dispatch(key, ..., flag_conditions_met={"<condition>": "<how it was met>"})` (same keyword on `dispatch_and_execute` and on `DualKernelCompositionClient.dispatch`). The mapping must name exactly the declared conditions, each with a non-empty string; otherwise the dispatch is refused and the operation stays `RESERVED`. A `PASS` admission needs no acknowledgement. `resume_existing` returns `action = "acknowledge_flag_conditions"` for a FLAG-admitted operation not yet acknowledged.
- **Pass `option_space`**: a call without it is now held (`HOLD`, `INSUFFICIENT_EVIDENCE`), not admitted on the neutral `0.5`.
- **Configure a verifier and sign the attestations**: `DualKernelGovernor(..., attestation_verifier=HmacAttestationVerifier({"<key_id>": <secret>}))` (or `Ed25519AttestationVerifier`), and build the proof with `sign_non_execution_attestation(attestation, "<key_id>", <secret>)`. An attestation that does not authenticate is refused: `fail()` returns `False` and a `DISPATCHING` operation moves to `UNKNOWN`, budget kept. Without a configured verifier, no caller-supplied attestation is accepted after dispatch; the runtime's own paths (`dispatch_and_execute`, `reconcile_unknown`) are unaffected.
- **Mandate re-validation is on by default** (`CompositionPolicy.revalidate_mandate_at_dispatch = True`). A dispatch whose mandate changed since admission is refused fail-closed (reservation released, `FAILED`). Setting it to `False` restores the 5.7.5 behaviour and is a documented weakening.

### Scope, known limits and trust boundaries

Read before deploying. Each limit below was observed on this code by a script or a test; none is inferred.

- **Deployment scope.** Unchanged from 5.7.5: one controlled host and one governor process, with the in-memory, JSON-file or SQLite WAL registry on that host (Profile S). Raft stays restricted to development/test loopback nodes while FM-20 is Open. Not exercised: the SQLite WAL backend under concurrent processes, and clock skew between processes.
- **The proof of non-execution is authenticated under a key the deployment declares; the runtime still does not observe the provider.** A caller attestation is proof once its signature verifies under the `key_id` it names, among the keys given to `attestation_verifier`. The trust boundary moves from "whoever can call `fail()`" to "whoever holds a declared key": the deployment declares which reconciling components may attest, and keeps their keys. A valid signature says that a key holder attested; it does not say that the key holder observed the provider.
- **A FLAG's conditions are enforced as an acknowledgement, not verified.** The runtime refuses a dispatch until the caller acknowledges every declared condition with a non-empty statement, persists and ledgers the acknowledgement, and refuses a FLAG that states no condition. It does not check that the acknowledged condition was actually met: that remains the deployment's supervision.
- **`option_space` missing gives `HOLD`, not `HARD_VETO`.** The HOLD is resolvable by re-adjudication with the measure (`resolve_hold`); a conclusive veto on the other inputs still governs. The REG harness vector PX-REG-STATE-010, which failed on 5.7.5 for this axis, has not been re-run on this code.
- **The mandate re-validated before dispatch is the governor's own state.** Checked: the acting agent is still registered (when it was at admission), the delegation in scope is still trusted, the in-memory budget limit still covers the amount (only when no consensus backend is configured: with a backend the limit lives in the backend, which refuses a re-registration with another capacity and enforces I8 on every reserve), and the composition policy version is unchanged. Not checked: anything outside the governor (an external authorization system, a provider-side limit). A `RESERVED` record written before 5.7.6, or through direct registry access, carries no admission terms: it dispatches as a `PASS` admission, with no FLAG acknowledgement to check. The re-check under the dispatch gate performs no I/O and refuses without a claim; the fail-closed settlement happens on the next `dispatch()` call.
- **A budget HOLD now resolves whatever its share of the remaining capacity (#141, fixed).** A HOLD at or above `budget_escalation_threshold` (85 % of the capacity by default) still composes to `HOLD` on re-adjudication: that is the escalation rule of the policy, not the #141 defect, and a deployment lifts it by declaring a higher threshold.
- **REG conformance is not demonstrated.** The reg-conformance harness has not been re-run on 5.7.6. Its last result on this line is the one stated in the 5.7.5 entry (`NOT_CERTIFIABLE`, from the unmerged draft skiredj-prog/reg-conformance#81; REG v0.7.0 is the normative reference). To exercise this code, that port must declare `flag_conditions`, pass `option_space`, acknowledge the conditions at dispatch and sign its attestations.

### Proof of non-execution is authenticated (first 5.7.5 boundary)

#### Added
- `tenir_governance.operations.attestation`: the `AttestationVerifier` protocol (`verify(attestation) -> str | None`, never raises), `HmacAttestationVerifier(keys)` (HMAC-SHA256 with the standard library; a verified attestation is recorded with `verification = "hmac-sha256:<key_id>"`), `Ed25519AttestationVerifier(public_keys)` (needs the optional `cryptography` package; `"ed25519:<key_id>"`), `sign_non_execution_attestation(attestation, key_id, secret)`, `sign_non_execution_attestation_ed25519` and `hmac_signature`. Keys shorter than 16 bytes are refused. Exported from `tenir_governance.operations` and `tenir_governance`.
- `NonExecutionAttestation.key_id`, `.signature` and `.verification`; `signing_payload()` (the canonical bytes a signature covers: the four facts and `observed_effect_count`, never the signature or the runtime fields); `verified(method)` (the runtime's copy, `verified_by_runtime = True` with the method named).
- `DualKernelGovernor(attestation_verifier=...)` and `DualKernelCompositionClient(attestation_verifier=...)`.

#### Changed
- `DurableOperationRegistry.record_non_execution_attested` persists only a verified attestation (`verified_by_runtime = True` with a named `verification`) and raises `ValueError` otherwise. The runtime's own channels record `verification = "provider-channel"` (`dispatch_and_execute`) and `"provider-reconciliation-channel"` (`reconcile_unknown`).
- `governor.fail(..., non_execution_attested=...)` authenticates a caller attestation under `attestation_verifier` before recording it, and records the verified copy (same facts, method named), not the caller's object.

#### Behaviour change (5.7.6)
- **An attestation that does not authenticate is not proof.** Unsigned, signed under an undeclared `key_id`, with a signature that does not verify, or carrying a caller-set `verified_by_runtime` or `verification`: `fail()` returns `False`, a `DISPATCHING` operation moves to `UNKNOWN` with its budget kept, an `UNKNOWN` one is unchanged, and the ledger records a `HOLD` entry `NON_EXECUTION_ATTESTATION_REFUSED (<why>)`. With no verifier configured, every caller attestation after dispatch is refused this way. On 5.7.5 the same well-formed attestation was accepted from any caller.
- The restart test (`tests/test_fail_after_dispatch_needs_proof.py`) now compares the persisted record with the verified copy.

### A FLAG admits only under declared conditions, acknowledged at dispatch (RFC-0 §4, decision 3; second 5.7.5 boundary)

#### Changed
- Composition (`adjudicate` and `resolve_hold`): a composed `FLAG` under a policy that declares no `flag_conditions` becomes `HOLD`, with the rationale `FLAG states no condition: the policy declares no flag_conditions, so the conditional authorization cannot be stated (RFC-0 §4, decision 3); held, not admitted. Declare CompositionPolicy.flag_conditions or re-adjudicate.` With conditions declared, the FLAG stands and names them, as in 5.7.5.
- A FLAG admission persists its terms on the operation: `OperationRecord.composed_decision` and `OperationRecord.flag_conditions` are written in the same persist as the `RESERVED` transition (`DurableOperationRegistry.transition_to(..., admission_terms=...)`; a `PASS` admission records an empty list). `OperationRecord.flag_conditions_met` holds the acknowledgement once given.
- `dispatch(..., flag_conditions_met=None)` and `dispatch_and_execute(..., flag_conditions_met=None)` (and `DualKernelCompositionClient.dispatch`): a FLAG-admitted operation dispatches only when the mapping names exactly the declared conditions, each with a non-empty string. The acknowledgement is persisted (`DurableOperationRegistry.record_flag_conditions_met`) and ledgered once (`FLAG conditions acknowledged at dispatch: ...`); a replay with the same acknowledgement is accepted, a different one is refused. The check runs again under the dispatch gate, before any claim.
- `resume_existing` returns `action = "acknowledge_flag_conditions"`, with the conditions, for a FLAG-admitted `RESERVED` operation without acknowledgement.

#### Behaviour change (5.7.6)
- **With the default `CompositionPolicy`, a FLAG is held, not admitted.** On 5.7.5 (and 5.7.0) it was admitted with no condition stated. Every deployment, test and example now declares its conditions: `demo.py`, `tenir_governance/quickstart.py`, the guide and the test suite (`flag_conditions=("test supervision",)`, acknowledged at dispatch).
- **A FLAG-admitted operation is not dispatched without the acknowledgement**, whatever the caller; the refusal names the missing conditions and the operation stays `RESERVED`.

### `option_space` is required for admission (decision 2 applied to the seventh axis)

#### Changed
- The scoring adapter holds a call whose `option_space` is `None` when the result would otherwise be `PASS` or `FLAG`: `HOLD`, rationale `INSUFFICIENT_EVIDENCE: option_space unmeasured; held, not admitted (re-adjudicate with the option-space measure).` A conclusive veto on the other inputs still governs, and `declared_unmeasured` still lists the axis. On 5.7.5 the neutral `0.5` let the call reach `PASS` or `FLAG` (the last favourable default of the seven axes).

#### Behaviour change (5.7.6)
- A caller that omitted `option_space` now gets `HOLD` and a `HOLD` operation (budget reserved), resolvable with `resolve_hold(..., option_space=...)`. The guide shows the held call and its resolution.

### The mandate is re-validated before dispatch (third 5.7.5 boundary)

#### Added
- `CompositionPolicy.revalidate_mandate_at_dispatch` (bool, default `True`).
- `OperationRecord.delegating_agent_id`, `.delegatee_agent_id`, `.agent_registered_at_admission` and `.policy_version`, written at admission and persisted: the mandate the dispatch re-validates against.
- Dispatch results carry `mandate_revalidation_failed`, `mandate_changes` (one sentence per change, e.g. `acting agent 'alice' is no longer registered`, `budget limit of 'budget' no longer covers the amount (...)`, `composition policy version changed from 'composition-v1.0.0' to 'composition-v1.0.1' since admission`) and, on the fail-closed path, `execution_record = "CONFIRMED_NONEXECUTION"`.

#### Changed
- `dispatch` and `dispatch_and_execute` on a `RESERVED` operation re-check the mandate under the settlement lock, before any dispatch claim: acting agent, delegation, in-memory budget limit (when no consensus backend is configured) and policy version, as listed under "Scope". On a change the reservation is released first, the operation reaches `FAILED` with no effect, and the ledger records a `HARD_VETO` entry `MANDATE_REVALIDATION_FAILED at dispatch (<changes>)`. If the release cannot be proven, the operation is left as it is with `budget_reconciliation_required = True`.
- Under the quarantine gate the mandate is re-checked once more without I/O; a change refuses without a claim (`MANDATE_REVALIDATION_FAILED under the dispatch gate (...)`), and the next `dispatch()` call settles fail-closed.

#### Behaviour change (5.7.6)
- **A dispatch whose mandate changed since admission is refused.** On 5.7.5 it dispatched (documented there as a limit). The guide shows a budget limit lowered under the reservation and a removed acting agent, both refused with the change named.

### Fixed
- **#141: a budget HOLD above half the remaining capacity could not be resolved.** `resolve_hold` let I8 count the operation's own reservation a second time; it now declares that reservation to the membrane (`existing_reservation_amount`), as `adjudicate` already did. With capacity 100, held amounts 50.5, 60 and 84 now resolve and dispatch (10 and 50 as before); another operation's reservation still bounds the budget. `tests/test_issue141_hold_budget_resolution.py` fails on the previous code.

### Guide, demo and quickstart
- `docs/agentic-governance-guide.md` (v5.7.6): the client of §2 declares its FLAG condition and its attestation verifier; `option_space` is passed everywhere, with a held call without it; §4 shows a dispatch refused without the acknowledgement, then accepted, and an unsigned attestation refused (`UNKNOWN`, budget kept), then a signed one accepted on the same operation; a new subsection shows the mandate re-validation; §6 shows the `acknowledge_flag_conditions` resumption; §11 lists the new keywords. `tests/test_guide_examples.py` runs every block.
- `demo.py` and `tenir_governance/quickstart.py` declare a FLAG condition, acknowledge it at dispatch and pass `option_space`; the quickstart's evidence file records `flag_conditions` and `flag_conditions_met`, and `verify_demo` checks them.

### Verification scope
- 73 new tests in four files: `tests/test_attestation_verification.py` (32), `tests/test_flag_conditions_enforced.py` (18), `tests/test_mandate_revalidation_at_dispatch.py` (15), `tests/test_issue141_hold_budget_resolution.py` (8). Run against the 5.7.5 code (`42513bf`): the attestation file does not import (the module does not exist there) and 40 of the 41 tests of the three other files fail; the one that passes checks that an unmeasured `option_space` masks no conclusive veto, which 5.7.5 already guaranteed.
- Full suite: 1457 passed (1384 existing tests adapted or unchanged, 73 new) on Python 3.12, single process; in-memory, JSON-file and SQLite registries; `SingleHostLockBackend` for the backend release path of the mandate re-validation. Not exercised: the SQLite WAL backend under concurrent processes, Raft, clock skew between processes.
- The Ed25519 tests run with the `cryptography` package of the `raft` extra (the release workflow installs it) and are skipped without it.
- The reg-conformance harness has not been re-run on this code.

---

## [5.7.5] — 2026-11-20 — Single-host pilot line: fail-closed hardening of 5.7.0 (merged on `main`, unpublished)

**Status.** Merged on `main` on 2026-11-20 (`42513bf`, PR #142) and held there by the release decision of the same day: no v5.7.5 tag, no PyPI publication. Three of the limits listed under "Scope, known limits and trust boundaries" were judged blocking for a pilot (the caller-asserted attestation, the unenforced FLAG conditions, the missing re-validation of the mandate before dispatch) and #141 held the qualification. 5.7.6, above, closes them on the same code line; this entry is kept unchanged as the record of that candidate, except for the two wording corrections noted in place.

First release of the 5.7.5 single-host pilot line (ROADMAP_90_DAYS, "5.7.5 — Auditable Evidence & Pilot Readiness"). It hardens 5.7.0: no favorable default for an unmeasured axis, no conclusive veto masked by an invalid measure, no `fail()` after dispatch without a persisted proof of non-execution, and a HOLD bound (τ_K) that a deployment can declare and that refuses alone. FM-20 stays Open and multi-host stays disabled; remediation target 5.8.0, as announced in 5.7.0.

**What this release is.** A technical release of the single-host line, with bounded guarantees, for a controlled pilot on one host. It is not a REG certification (the REG harness result on this code is `NOT_CERTIFIABLE`, see "Scope, known limits and trust boundaries"), not a general production qualification, and not the completion of the "Auditable Evidence & Pilot Readiness" milestone: the evidence package, the report exports, the rehearsals and the pilot acceptance that ROADMAP_90_DAYS lists for 5.7.5 are not in it.

**Not a strict-semver patch.** This release adds public API on top of 5.7.0: the policy fields and HoldRecord fields below, `NonExecutionAttestation`, `cancel_escalation`, `DualKernelCompositionClient.mark_unknown`, and `tenir_governance/quickstart.py` (with `tests/test_quickstart.py`), merged on `main` after the v5.7.0 tag. It also changes documented behaviour; each change is listed under "Behaviour change" below. A 5.7.0 caller that relied on the axis defaults, on `fail()` after dispatch without proof, or on an early `expire_escalated` does not run unchanged on 5.7.5: read "Upgrading from 5.7.0".

### Upgrading from 5.7.0

- Pass measured `authority`, `context_fit` and `evidence_quality`, and `pressure`, `velocity` and `capacity`: a call without them is now refused with `HARD_VETO`.
- After dispatch, `fail()` needs a `NonExecutionAttestation` (`non_execution_attested=`). Without it, `fail()` returns `False` and a `DISPATCHING` operation moves to `UNKNOWN` with its budget kept. `authoritative_non_execution=True` alone is deprecated and is no longer proof.
- `expire_escalated` now refuses before τ_K is exhausted, and when no τ_K is set. An authority ends an escalation early with `cancel_escalation`.
- Inputs that produced a fail-closed `HOLD` with `scoring_decision = None` (a non-finite or out-of-range measure) now give `scoring_decision = HOLD`, or `HARD_VETO` when a conclusive veto applies.

### Scope, known limits and trust boundaries

Read before deploying. Each limit below was observed on this code by a script; the lowered budget limit and the removed acting agent were set through internal access, because no API allows them. (Wording corrected in 5.7.6: the entry first said "by a script driving the public API".)

- **Deployment scope.** One controlled host and one governor process, with the in-memory, JSON-file or SQLite WAL registry on that host (Profile S). Raft stays restricted to development/test loopback nodes while FM-20 is Open. Not exercised: the SQLite WAL backend under concurrent processes, and clock skew between processes.
- **The proof of non-execution is asserted by its caller; the runtime does not authenticate it.** `NonExecutionAttestation` is checked for form (the four fields, an ISO 8601 time, `observed_effect_count = 0`) and for the attempt it names; `verified_by_runtime` is always `False`. Any caller that can call `fail()` releases the budget of a dispatched operation with a well-formed attestation, whatever its `source` says: the runtime does not check that the source observed the provider. The trust boundary is the deployment's: restrict `fail(..., non_execution_attested=...)` to the component that reconciles against the provider, and prefer the runtime's own paths (`dispatch_and_execute` and `reconcile_unknown` record their attestation from the provider's answer). Against 5.7.0, which released the budget after dispatch with no proof at all, this is a stronger gate, not an end-to-end guarantee.
- **A FLAG's conditions are declared, not enforced.** `CompositionPolicy.flag_conditions` is carried on every FLAG result and named in the ledger; the runtime does not check that they are met before dispatch, and the default policy declares none. Under RFC-0 §4 a FLAG that states no condition is an implementation gap, which the deployment's policy has to fill.
- **`option_space` is not a hard gate.** When absent it is replaced by a neutral `0.5`, listed in `declared_unmeasured` with a soft gate reason, and the call can still reach `PASS` or `FLAG`; the six axes under "Upgrading from 5.7.0" are hard gates. The REG harness vector PX-REG-STATE-010 fails for this reason (on 5.7.0 it failed for four axes).
- **No re-validation of the mandate between admission and dispatch on the default path.** `dispatch` and `dispatch_and_execute` re-check the payload digest, the quarantine lock and the presence of the budget reservation. They do not re-check a budget limit lowered below the amount, the removal of the acting agent, a withdrawn delegation, or a change of the governor's `CompositionPolicy` version after admission: the operation dispatches. 5.7.5 does not guarantee the dynamic revocation of a mandate up to dispatch. The experimental Profile S commit gate (`require_commit_authorization=True`, off by default) refuses on any change of the signed trust state, but not on the governor's own policy version.
- **A budget HOLD above half the remaining capacity cannot be resolved (#141, open, unchanged from 5.7.0).** On re-adjudication, I8 counts the operation's own reservation: with capacity 100, held amounts 50.5, 60 and 90 are refused with `HARD_VETO` ("Aggregate budget violation"), the reservation is released and the operation reaches `FAILED`, whatever the new evidence; 10 and 50 resolve and dispatch. The failure is closed (no effect), and `resolve_hold` still returns `hold_resolved = True` on that path.
- **REG conformance is not demonstrated.** The reg-conformance harness, run from skiredj-prog/reg-conformance#81 (`7ee03ab`, level 2; an unmerged draft extension of the harness, not the released REG v0.7.0, which is the normative reference), gives `NOT_CERTIFIABLE` on this code. (Wording corrected in 5.7.6: the entry first cited #81 as if it were the released harness.) With the default port, its 61 vectors, 4 effect probes and 17 property checks give the same results as on 5.7.0 (10 PASS and 50 ADAPTER_REQUIRED among the vectors; one FAIL among the property checks, PX-REG-STATE-010, above). With the τ_K port (τ_K = 1 s), CT-R4-004b passes with the reserve that its FLAG states no condition, and CT-R4-005 is INCONCLUSIVE: a refusal under quarantine lock states neither a terminal disposition nor a resumption condition, and the adapter contract has no field for one.

### No favorable default for an unmeasured axis; a FLAG states its declared conditions (decisions 2 and 3, 19 Nov)

#### Behaviour change (5.7.5)
- **A call that does not supply `authority`, `context_fit` and `evidence_quality` (or `pressure`, `velocity`, `capacity`) is now refused with `HARD_VETO`**, and the operation reaches `FAILED`. On 5.7.0 the same call was admitted on the `1.0` / `0.5` defaults. The scoring rationale names each missing axis (`"authority missing: cannot adjudicate without authority assessment"`, ...), and `declared_unmeasured` lists them.
- `docs/agentic-governance-guide.md` and `demo.py` now pass these axes explicitly. The guide shows a call without them (`HARD_VETO`, with its reason) and states the observed decision: `FLAG`, not `PASS` (§2, §4, §9, §10). Its "PASS" was already wrong on 5.7.0, which returned `FLAG` because four axes are never measured.

#### Changed
- `DualKernelCompositionClient.adjudicate`: `authority`, `context_fit` and `evidence_quality` default to `None` (were `1.0`). An axis that is not supplied now reaches the scoring adapter as missing, which is a hard gate there: the request is not admitted. Callers that relied on the `1.0` defaults must now pass measured values.
- `Agent.authority_score` defaults to `None` (was `1.0`). No kernel reads it.
- Same rule applied to S: `pressure`, `velocity` and `capacity` of `DualKernelGovernor.adjudicate`, `resolve_hold` and `DualKernelCompositionClient.adjudicate` default to `None` (were `0.5`, `0.5`, `1.0`, which give S ≈ 4, far above τ). A missing one is a hard gate and is listed in `declared_unmeasured`.
- `Relationship.trust_score` defaults to `None` (was `1.0`). No kernel reads it.

#### Added
- Scoring and composed results carry `declared_unmeasured`: the axes the score did not measure (`option_space` when absent, the four axes with no measurement input, and any missing mandatory axis). Their neutral placeholders keep the geometry complete; they are declared, not measured.
- `CompositionPolicy.flag_conditions` (tuple of non-empty strings, default empty): the conditions under which a FLAG authorizes (RFC-0 §4), declared by the deployment. Every FLAG result carries them in `flag_conditions`, and the composed ledger rationale names them. With the default policy a FLAG states no condition, as in 5.7.0; the runtime invents none and does not enforce them.

#### Not changed
- `confidence = 1.0` in the post-execution value assessment (`assessment/evp.py`, `assessor.py`, `assess_post_execution`) is unchanged: it weighs the expected value of an operation already terminal, not its admission.

### A non-finite measure is insufficient evidence and masks no veto (decision 5)

#### Fixed
- A non-finite measure (NaN, ±inf, or a non-number) made the scoring kernel raise; the governor then composed a fail-closed `HOLD`, which masked a conclusive `HARD_VETO` the other inputs supported (e.g. TAU breach with unknown evidence quality). The scoring adapter now checks finiteness at entry: the measure is reported as `null`, listed in `unmeasured_axes` and `declared_unmeasured`, and replaced by a neutral placeholder; every conclusive veto is still evaluated and governs (RFC-0 §4). Without a veto the result is `HOLD` with `INSUFFICIENT_EVIDENCE`, never `PASS` or `FLAG`. A non-finite pressure, velocity or capacity leaves S uncomputed: `HOLD`, except a veto that holds without S (missing mandatory measure, option space at its block floor, capacity 0).
- Composition dropped a scoring `HOLD` when the membrane passed (`compose("HOLD", "PASS")` gave `PASS`). The adapter never produced a scoring `HOLD` before, so this was latent; it would have admitted on non-finite evidence with the guard above. New lattice rule: a scoring `HOLD` composes to `HOLD`.
- `tests/test_non_finite_measure_guard.py` fails on the previous code.
- Two cases were still masked after the guard (reproduced by a 63-row veto-masking script on this branch): (a) a finite value outside [0, 1] (1.5, -0.1) passed the guard, then made the kernel raise, so the fail-closed `HOLD` masked a TAU breach; it is now invalid like a non-finite value (`null`, `unmeasured_axes`, `HOLD` unless a veto governs). (b) With P, V or C invalid, authority 0 or context_fit 0 was not evaluated, so the result was `HOLD` and a later favourable `resolve_hold` could admit; every hard geometry facet whose axes are all validly measured is now evaluated on that path. A facet that reads an invalid axis is not used. New tests in the same file.

#### Behaviour change
- Inputs that used to produce a fail-closed `HOLD` with `scoring_decision = None` now produce `scoring_decision = HOLD` (or `HARD_VETO` when a conclusive veto applies) with the reason. Not documented in the guide before; no example changes.

### Proof of non-execution is a persisted attestation, not a boolean (RFC-4 v0.3 §2.3, T7)

#### Changed
- Proof that a dispatched effect did not occur is now a `NonExecutionAttestation` (`tenir_governance.operations`): `idempotency_key`, `source`, `attestation`, `attested_at` (ISO 8601), `observed_effect_count = 0`, `verified_by_runtime = False`. It is validated, then persisted on the operation record as `OperationRecord.non_execution_attested` before the record leaves `DISPATCHING` or `UNKNOWN`. It survives a restart with `store_path`. A recorded attestation is never replaced; a replay for the same attempt keeps the first one.
- `governor.fail(..., non_execution_attested=...)` takes the attestation (object or dict). It is refused when malformed, when it names another attempt, or when the record is not `DISPATCHING` / `UNKNOWN`.
- The provider path (`dispatch_and_execute`, provider outcome `FAILED`) records an attestation with source `provider`; `reconcile_unknown` records one with source `provider-reconciliation` before `FAILED`, and leaves the operation and budget unchanged if it cannot be persisted.

#### Behaviour change (5.7.5)
- **`authoritative_non_execution=True` is no longer proof.** Passed alone to `governor.fail`, it emits a `DeprecationWarning`; after dispatch the call returns `False` and a `DISPATCHING` operation moves to `UNKNOWN`, budget kept (the B3 rule below, with the attestation as the only proof).
- `DualKernelCompositionClient.fail(..., non_execution_attested=None)` replaces the `authoritative_non_execution` keyword added earlier on this branch (never released).
- Guide §4 (step 3b) and the API table show the attestation.

### A failure after dispatch needs proof of non-execution (decision B3)

#### Fixed
- Without a consensus backend, `fail()` on a dispatched operation (`DISPATCHING`) moved it to `FAILED` and gave its budget back with no proof that the effect did not occur (fail-open). It now requires proof of non-execution (since the section above, a persisted attestation) once the operation is `DISPATCHING` or `UNKNOWN`. Without it, a `DISPATCHING` operation moves to `UNKNOWN` (reconciliation), keeps its budget, and `fail()` returns `False`; an `UNKNOWN` one is left unchanged. The backend path already refused. `tests/test_fail_after_dispatch_needs_proof.py` fails on the previous code.
- **Behaviour change**: a caller that called `fail()` after dispatch, without proof, now gets `False` and an `UNKNOWN` operation. Documented in the guide (§4, API table) before this release as "→ FAILED + libère le budget"; both are updated. `tests/test_fault_injection.py::test_reconciliation_finds_no_effect` now passes the proof its reconciliation established.

#### Added
- `DualKernelCompositionClient.fail` passes the proof through (now `non_execution_attested`), and `DualKernelCompositionClient.mark_unknown(key)` exposes the governor's method (DISPATCHING → UNKNOWN, budget kept).

### Guide and demo examples run as written

Three guide examples and one demo step already failed on 5.7.0. Each is rewritten and its result written as observed:
- §4 (lifecycle): confirm, fail and the uncertain outcome were chained on one operation; each now runs on its own. An UNKNOWN operation counts its amount as consumed until reconciliation.
- §7 (budget escalation): the new client registered no agent or budget, so the first payment was vetoed. It now registers them; 40 on 100 gives `FLAG`, then 20 more (60 % ≥ 50 %) escalates to `HOLD`.
- §8 (schema 1.2): `adjudicate_schema` requires an explicit `canonical_context`; the example supplies one and prints the observed fields (`CAUTION`, `allow_with_alert`).
- `demo.py`: an approved authorized exit returns the operation to `HOLD` (fresh re-adjudication required), as `authorize_exit` documents; the demo expected `RESERVED` or `FAILED`.
- `tests/test_guide_examples.py` runs every Python block of the guide in order.

### τ_K as a policy-declared fail-safe bound on HOLD (fixes #140)

The τ_K checks below (Added, Fixed) act only when the deployment's policy declares a τ_K; with the default policy no HOLD carries a bound and they do nothing. The escalation changes under "Changed" apply to every deployment.

#### Added
- `CompositionPolicy.hold_tau_k_seconds` (default `None`) and `CompositionPolicy.hold_tau_k_source`. When the policy declares a τ_K, every HOLD created by `DualKernelGovernor.adjudicate` carries it, with its source. A τ_K without a source is rejected at policy validation. The governor still invents no τ_K when the policy declares none.
- `HoldRecord.resolved_at`: time at which re-adjudication resolved the HOLD to admission.
- `HoldRecord.exhaustion_disposition`, `exhausted_at`, `execution_record`: the disposition applied at the bound, the advance point, and the canonical record (`CONFIRMED_NONEXECUTION`), persisted with the operation.

#### Fixed
- An exhausted τ_K now refuses alone wherever a held operation could advance, without a timer (RN-001 §2a, RFC-5 §2):
  - `resolve_hold`: no re-adjudication after the bound, whatever the new evidence;
  - `escalate_hold`: a HOLD past its bound is closed, not escalated;
  - `authorize_exit(approve=True)`: a late approval is not accepted;
  - `dispatch` / `dispatch_and_execute`: a HOLD still unanswered past the bound, or a RESERVED operation whose HOLD was resolved after the bound (or at an unrecorded time), is not dispatched.
  In each case the reservation is released first, the operation reaches `FAILED` with `exit_reason="expired"`, and the ledger records a `HARD_VETO` entry naming the bound, its source and the advance point. If the release cannot be proven, the operation stays open for reconciliation and the ledger records the refusal with `budget reconciliation required`. In `resolve_hold` the bound is checked before identity and payload: a late call closes the attempt whoever makes it.
- The bound counts from HOLD entry. An escalation or an authorized exit does not restart it.
- The refusal is terminal for that attempt identity; a later effect needs a new attempt. Every advance point checks before any dispatch claim, so the attempt was never dispatched: the result carries `execution_record="CONFIRMED_NONEXECUTION"` and the ledger entry says so. `FAILED` stays the runtime's label.

#### Changed (decision B2/B3, 19 Nov)
- `expire_escalated` is time-driven like `expire_hold`: it refuses before τ_K is exhausted, and when no τ_K is set. Past the bound it still gives `UNKNOWN` (runtime label, v7.1 §19) and now records `execution_record="CONFIRMED_NONEXECUTION"`: an escalation never reached dispatch. The reservation is still left to reconciliation.
- New `cancel_escalation(key, cancelled_by, reason, decision)` (HoldManager and governor): an authority ends an escalation early. All three fields are required and kept in an `ExitReceipt`; the reservation is released first; the attempt reaches `FAILED` with `exit_reason="cancelled"` and `CONFIRMED_NONEXECUTION`. Calling `expire_escalated` early is no longer a way to end an escalation.
- A denied exit (`authorize_exit(approve=False)`) from `ESCALATED` now records `execution_record="CONFIRMED_NONEXECUTION"` on the hold record and in its result, like the τ_K and `cancel_escalation` paths; the escalated attempt was never dispatched. It left the field empty before.
- An explicit `expire_hold` past τ_K now records `exhaustion_disposition="fail-closed"`, `exhausted_at="expire_hold"` and `execution_record="CONFIRMED_NONEXECUTION"` on the hold record and returns `execution_record`, like the advance points and `expire_escalated`; a HOLD is never dispatched. It recorded only `exit_reason="expired"` before.
- `test_hold_manager_expire_escalated`, which expired an escalation without waiting, is rewritten.

#### Not changed
- `expire_hold` keeps its transition (HOLD → `FAILED`, `exit_reason="expired"`); only its record is completed (see "Changed" above). `expire_escalated` still yields `UNKNOWN` per v7.1 §19 (see "Changed" above).
- A late denial (`authorize_exit(approve=False)`) still denies.
- No τ_K default anywhere; a HOLD created without a declared bound carries `tau_k_seconds = 0.0` as before.

#### Verification scope
- 29 tests in `tests/test_hold_tau_k_fail_safe.py` (28 at the τ_K fix, one added with the `expire_hold` record); the enforcement ones fail when the advance-point check is disabled. Time is moved through the governor's clock, not by editing a HOLD record.
- Single process, in-memory and JSON-file registries. Not exercised: the SQLite WAL backend under concurrent processes, and clock skew between processes.

---

## [5.7.0] — Single-host safety, durable accounting and deployment evidence

This version targets single-host deployments with explicitly bounded guarantees. Any Harv activity is an independent review, not a certification.

### Added
- Single-host SQLite WAL consensus backend with atomic budget/reservation transactions, rollback, and validated migration from legacy JSON state.
- Deployment manifest schema and verifier, with explicit restrictions for unsafe production configurations.
- Canonical payload hashing in permits and dispatch-time payload verification.
- Fail-closed HOLD handling when provider failure conflicts with an S3 `HYPOTHESIS_VIOLATED` signal.

### Changed
- Raft replicated state uses deterministic inputs and a replicated logical clock; reservation identifiers are created outside replicated methods.
- Budget and membrane boundaries reject non-positive and non-finite amounts.
- Cross-process persistence uses platform-aware locks and atomic file replacement.
- Recovery and reconciliation preserve reservation ownership and idempotency across retries and restart.

### Fixed
- A late external effect after reservation release can no longer mutate `RELEASED` to `COMMITTED`; it records an idempotent compensation transaction instead.
- Unknown provider outcomes remain unresolved until evidence supports a safe reconciliation decision.

### Security and deployment limits
- FM-20 remains **Open**: PySyncObj command transport still uses pickle. `production_allowed` is fixed to `false`; v5.7.0 rejects `HOST_SCOPE=multi-host`, rejects production manifests with Raft enabled, and requires a verified manifest for production startup. Remediation target: 5.8.0. This is a hard-disable mitigation, not a transport fix.
- Use the SQLite WAL backend for the bounded single-host deployment profile.
- This package does not claim Byzantine fault tolerance or exactly-once external provider effects.

### Verification scope
- Source integration into `main` was recorded at commit `fb9355cc55fae14e88190650694a2cc32513e3c4`. Governance CI on that commit completed successfully; final release verification is independently required against the exact tagged commit.
- The release workflow installs `.[api,test,raft]`, requires PySyncObj, runs the Raft suite explicitly, then runs the full suite and rejects skipped Raft tests. Passing CI is not a formal guarantee.
- Raft integration tests use local loopback clusters and do not constitute a physical multi-host network or production security test.

### Compatibility note
- v5.7.0 extends the separately published 5.6.1 patch (2026-10-20); the latter remains its own release entry.
- `admissible_structural` describes catalog coherence only and does not imply `effective_guarantee_demonstrated`.

---


All notable changes to TENIR-Gov are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [5.6.1] — 2026-10-20 — Published patch: Budget safety and deterministic Raft state

### Corrected in this branch
- Reject `reconcile_commit()` unless the reservation is `RESERVED`; repeated commits remain idempotent, while a late effect after `RELEASED` returns `RECONCILE_CONFLICT` and records a deterministic incident.
- Replace replicated wall-clock timestamps with a replicated logical clock.
- Validate finite, positive reservation amounts and non-negative finite capacities; preserve decimal arithmetic in the replicated budget and fractional values in read results.
- Return the actual reservation state on idempotent replay.
- Make resource registration non-destructive; reject implicit capacity changes.

### Additional safety corrections under review
- Preserve compatibility with five-argument legacy Raft reserve commands and normalize restored float accounting before Decimal arithmetic.
- Preserve exact Decimal values in Raft state reads and fractional values in the shared consensus state contract.
- Validate governor amounts before budget gating; bind dispatch resource and amount to the adjudicated durable record; block dispatch under QUARANTINE_LOCK and serialize quarantine transitions against dispatch/provider execution.
- Add regression tests for legacy state/journal compatibility, large-capacity decimal precision, invalid amounts, dispatch identity mismatch, pre-reserved operations under quarantine, empty Stripe search, concurrent reconciliation, and linearizability-checker branch isolation.
- Keep Stripe reconciliation UNKNOWN when search returns no result; an empty search is not proof of non-execution. Serialize reconciliation state transitions with budget release/refund to prevent duplicate refunds.
- Prevent live resource replacement from orphaning reservations or swapping the resource lock; correct the sequential oracle so COMMITTED cannot follow RELEASED, bind concrete reservation IDs across replay/commit, and exercise the actual concurrent Raft history.
- Normalize Decimal input before the single-host backend persists reservation JSON; add regressions for Decimal idempotency, HOLD identity preservation, and preventing a HOLD veto from releasing another operation’s reservation.
- Replace fixed leader-failover sleeps with bounded polling across the three- and five-node tests; release reserved accounting when an UNKNOWN reservation is explicitly reconciled as non-executed.
- Allow recovery reconciliation to start from DISPATCHING after a crash; normalize indeterminate outcomes to UNKNOWN and check terminal transition success before reporting reconciliation.

### Release metadata / DOI provenance
- Software citation DOI: `10.5281/zenodo.21277138` (consistent with
  `pyproject.toml` and the existing public software citation).
- The legacy `10.5281/zenodo.21264803` claim is not asserted as a
  peer-reviewed article pending confirmation of its publication details.
- Python 3.12 wheel/sdist and full-test preflight runs as a **non-publishing**
  PR CI job; no release tag is created by this process.

### FM-20 release boundary (mandatory while unresolved)
- PySyncObj uses pickle command transport. Raft in 5.6.1 is **experimental only**:
  constructors reject production, staging, missing environment declarations,
  multi-host scope, remote addresses, and wildcard bindings before network startup.
- Explicit `TENIR_ENV=test` or `TENIR_ENV=development` and loopback-only peers
  are required for local integration tests; this does **not** demonstrate
  physical multi-host security or resolve FM-20.
- The tag-triggered release workflow must install `.[test,raft]` and execute
  the Raft suite explicitly before PyPI publishing; no tag is authorized by
  this release-candidate preparation.

### Test perimeter
- Reproduction: `TENIR_ENV=test python -m pip install -e ".[test,raft]" && TENIR_ENV=test pytest -q tests/test_raft_backend.py`
- Included: local `pysyncobj` Raft integration tests, I8 regressions, invalid-amount cases, idempotent replay, resource re-registration, logical-clock replication and fractional-budget accounting.
- Environment: test cluster nodes use `localhost` and separate ports; this is not a physical multi-host network test.
- Excluded: Byzantine tolerance, hardened peer transport/authentication, third-party attestation, and proof of exactly-once external effects.
- Raft regression suite result for the original implementation snapshot: **46 collected, 46 passed, 0 failed, 0 skipped** in 119.01 s on GitHub Actions `ubuntu-latest`, Python 3.11.17, pytest 8.4.2, `pysyncobj` 0.3.17. Tested implementation commit: `82037052556c5bfd731bd59fe958cf4df61d21b6`; CI run: [#39205580977](https://github.com/skiredj-prog/tenir_governance/actions/runs/39205580977). For current-branch validation, first install the dependencies shown in the reproduction command above, then run `pytest -q tests/test_raft_backend.py`.

### Status
- Release status: published as v5.6.1 on 2026-10-20; historical test counts retain their original commit and run identifiers.
- `admissible_structural` describes catalog coherence only and does not imply `effective_guarantee_demonstrated`.


---

## [5.6.0] — 2026-10-19 — Minor: Multi-Host Raft Consensus Backend

This minor release adds `RaftConsensusBackend`, a multi-host `ConsensusBackend` implementation using the Raft consensus algorithm (via pysyncobj). This closes the C3 (cross-host fault tolerance) gap identified in the REG-Conformance audit: the cluster tolerates crash-stops of up to `f` nodes with `n ≥ 2f+1`, while maintaining C1 (safety/linearizability), I8 (budget conservation), and C2 (liveness) across hosts.

### Added
- **`RaftConsensusBackend`** (`tenir_governance/operations/raft_backend.py`) — Multi-host `ConsensusBackend` using Raft (pysyncobj). State (budget capacity, reserved, committed, reservations registry) is replicated across all nodes via `@replicated` commands. I8 is enforced on the leader before replication. Satisfies C1, C2, C3 (crash-stop), and I8 for multi-host deployments.
  - **Key design decision**: Reservation IDs are generated client-side and passed as command arguments, not generated inside the `@replicated` method. This ensures deterministic replication — a fundamental Raft requirement (all state machine commands must produce identical results on all nodes).
- **Tests: Raft multi-node** (`tests/test_raft_backend.py`) — 29 tests across 5 categories:
  - Multi-node reserve/commit/release (10 tests): operations from leader and followers, idempotency, insufficient budget, full lifecycle from mixed nodes.
  - I8 invariant (5 tests): I8 holds after multiple reserves, mixed commit/release, overcommit rejection, on followers, and under concurrent reserves.
  - Leader election and failover (5 tests): leader exists, failover after crash, 5-node failover, exactly one leader, state preservation after failover.
  - Liveness (4 tests): operations terminate under quorum, from all nodes, concurrent, and under minority failure.
  - Linearizability (5 tests): sequential history, concurrent history, mixed ops, after failover, and Wing-Gong checker on Raft histories.

### Changed
- Version bumped to 5.6.0 in `pyproject.toml`.
- `pysyncobj` added as optional dependency (required only for `RaftConsensusBackend`).

### Tests
- Total: **787 tests** passing (697 in `tests/` + 61 in `r4/tests/` + 29 new Raft tests = 787).
- Raft backend: 29 tests pass (10 multi-node + 5 I8 + 5 failover + 4 liveness + 5 linearizability).

### Conformance
- C1 (safety/linearizability): ✓ — Raft provides linearizability for committed commands. Verified by Wing-Gong checker on Raft histories.
- C2 (liveness): ✓ — operations terminate under healthy quorum. Verified by liveness tests.
- C3 (cross-host fault tolerance): ✓ — tolerates `f` crash-stops with `n ≥ 2f+1`. Verified by failover tests (3-node and 5-node).
- I8 (budget conservation): ✓ — enforced on leader before replication, holds on all nodes. Verified by I8 tests.
- Byzantine fault tolerance: ✗ — not implemented (Raft is crash-only).

### Limitations
- `RaftConsensusBackend` does not provide byzantine fault tolerance (Raft is a crash-fault-tolerant protocol).
- Network partitions: safety is maintained (Raft prevents split-brain), but liveness requires a quorum of connected nodes.
- `pysyncobj` uses pickle for serialization (not cryptographically signed). For production deployments with untrusted nodes, consider adding authentication/encryption via `SyncObjConf(password=...)`.
- The Wing-Gong linearizability checker is exponential in the worst case; it is tractable for small histories used in tests but would need a more efficient algorithm for large-scale verification.

---

## [5.5.0] — 2026-10-19 — Minor: Cross-Process Budget, Consensus Backend, Liveness

This minor release adds the `DurableBudgetCoordinator` for cross-process budget conservation (I8), a reference `ConsensusBackend` adapter implementing the REG-Conformance interface, a linearizability checker, and liveness tests. These components close the implementation gaps identified in the REG-Conformance audit: cross-process I8, consensus interface conformance, and liveness verification.

### Added
- **`DurableBudgetCoordinator`** (`tenir_governance/operations/budget.py`) — Cross-process budget coordination using `fcntl.flock` + durable JSON state. Extends I8 enforcement from single-process (`threading.Lock`) to multi-process on a single host. All read-check-write sequences are serialized via exclusive file lock. I8 (`reserved + consumed ≤ capacity`) is maintained atomically under the lock at all times.
- **`SingleHostLockBackend`** (`tenir_governance/operations/consensus_backend.py`) — Reference adapter implementing the `ConsensusBackend` interface defined in `reg-conformance/specs/consensus-interface-v1.json`. Wraps `DurableBudgetCoordinator` and adds a reservation registry with `reserve/commit/release/reconcile/get_state` operations. Conforms to C1 (safety), I8 (budget), C2 (liveness) for single-host deployments. The `ConsensusBackend` Protocol is defined as a `runtime_checkable` Protocol for implementation-neutral conformance.
- **`LinearizabilityChecker`** (`tenir_governance/operations/linearizability_checker.py`) — Wing-Gong algorithm for concurrent history validation. Takes a history of invocation/response events and verifies that a valid linearization exists respecting real-time precedence. Includes `consensus_sequential_spec` for the ConsensusBackend operations.
- **Tests: cross-process I8** (`tests/test_durable_budget_cross_process.py`) — 10 tests: two processes reserve same budget (one fails), multiple processes reserve within budget, cross-process reserve then commit, I8 under concurrent reserve and release, plus 6 unit tests for DurableBudgetCoordinator.
- **Tests: liveness** (`tests/test_liveness.py`) — 15 tests: termination within T_max for all operations, concurrent operations all terminate, max latency under load, reconciliation liveness (confirmed/failed/unresolved/not-found), liveness conditional (insufficient budget returns immediately), linearizability with liveness under concurrent load.

### Changed
- Version bumped to 5.5.0 in `__init__.py`, `tenir_kernel/__init__.py`, `pyproject.toml`, `CITATION.cff`, `.zenodo.json`, `SECURITY.md`, `demo.py`.

### Tests
- Total: **758 tests** passing, 0 failures, 0 regressions (687 in `tests/`, 61 in `r4/tests/`, 10 new cross-process I8 + 15 new liveness = 758).
- Cross-process I8: 10 tests pass (4 subprocess-based + 6 unit).
- Liveness: 15 tests pass (termination, reconciliation, conditional, linearizability).

### Limitations
- `DurableBudgetCoordinator` is single-host only (`fcntl.flock` does not cross host boundaries). Cross-host requires a `DurableBudgetCoordinator` backed by a transactional database or distributed coordination service.
- `SingleHostLockBackend` satisfies C1 (safety) and I8 for single-host, multi-process deployments. It does NOT satisfy C3 (fault tolerance across hosts) or byzantine tolerance.
- `LinearizabilityChecker` uses the Wing-Gong recursive search, which is tractable for small histories but exponential in the worst case. For large histories, a more efficient algorithm (e.g., PO-Check) would be needed.

---

## [5.4.4] — 2026-10-19 — Patch: Test Coverage, Attribution Fixes

This patch adds missing regression tests for authorize_exit budget paths, adds mutation-test documentation for the _reserve_budget lock, corrects the repository test count from 669 to 733 (669 in `tests/` + 61 in `r4/tests/` + 3 new), and fixes budget fix attribution in documentation.

### Added
- `test_authorize_exit_deny_releases_reservation` — verifies that authorize_exit(deny) releases the budget reservation and clears `budget_reserved`/`budget_committed` flags. Previously untested path.
- `test_authorize_exit_approve_keeps_reservation` — verifies that authorize_exit(approve) keeps the existing reservation without re-reserving. Previously untested path.
- `test_concurrent_adjudication_fails_without_lock` — mutation-test documentation verifying the `_reserve_budget` lock is load-bearing: without it, two concurrent threads would both reserve, violating I8.

### Fixed
- **Test count corrected** — README, docs/manuscript.md, and CHANGELOG previously stated 669 as "Repository total". The actual total is 733 (669 in `tests/` + 61 in `r4/tests/` + 3 new tests). The 669 figure was the `tests/` directory only.
- **Budget fix attribution** — 5.4.3 CHANGELOG now states "The I8 budget coordination fix was introduced in 5.4.2; this release consolidates it doctrinally and fixes documentation only." Previously, documentation could be read as implying the fix was new in 5.4.3.

### Tests
- Total: 733 tests passing, 0 failures, 0 regressions (672 in `tests/`, 61 in `r4/tests/`).
- authorize_exit deny: reservation released, flags cleared — verified.
- authorize_exit approve: reservation kept, no double-reserve — verified.
- _reserve_budget lock: concurrency test passes with lock, mutation documentation explains why it would fail without it.

---

## [5.4.3] — 2026-10-19 — Patch: CAVE Doctrinal Alignment, Metadata Fixes

This patch aligns the CAVE acronym to the doctrinal definition (Control / Aim / Veto / Epistemics), fixes stale metadata across the corpus, and updates documentation references. The I8 budget coordination fix was introduced in 5.4.2; this release consolidates it doctrinally and fixes documentation only.

### Changed
- **CAVE canonical definition flipped** — `CAVEFieldNames.EXPANSION` is now `Control · Aim · Veto · Epistemics` (doctrinal definition). The former runtime definition `Context · Action · Value · Effect` is now in `FORBIDDEN_EXPANSIONS`. The copy-lint CI step now blocks the old variant.
- **`.zenodo.json`** — description and notes updated from 5.2.0 references to 5.4.3, including I8 budget fix and CAVE alignment.
- **`CITATION.cff`** — version bumped to 5.4.3, date to 2026-10-19, abstract updated from 5.2.0 reference.
- **`SECURITY.md`** — all pip install examples updated from 5.4.1 to 5.4.3.
- **`README.md`** — proof paragraph updated to v5.4.2 with I8 budget fix and 733 tests; test count table updated from 663 to 733 (672 in `tests/`, 61 in `r4/tests/`).
- **`demo.py`** — install instructions updated to 5.4.3.
- **`tenir_kernel/__init__.py`** — version bumped from 5.2.0 to 5.4.3.
- **`tenir_governance/__init__.py`` — module docstring version updated from 5.2.0 to 5.4.3.
- **`tenir_governance/composition/governor.py`** — comments referencing "5.2.0's TenirKernel" cleaned to remove stale version.
- **`tests/test_cross_process.py`**, **`tests/test_fault_injection.py`** — skip reasons updated from "5.2.0 repo" to "kernel repo".
- **`docs/manuscript.md`** — test count updated from 663 to 733, version references updated to v5.4.3, changelog table extended with 5.4.2 and 5.4.3 entries.
- **`HARDENING_REPORT_v1.0.0-rc2.md`** — CAVE definition and NOM-001 check updated to Control/Aim/Veto/Epistemics.
- **`CONTRIBUTING.md`** — NOM-001 invariant updated to Control / Aim / Veto / Epistemics.

### Tests
- Total: 730 tests passing, 0 failures, 0 regressions (669 in tests/, 61 in r4/tests/).
- CAVE flip verified: `test_cave_canonical_definition` and `test_deprecated_cave_always_blocks` both pass with the new definitions.

---

## [5.4.2] — 2026-10-18 — Patch: Budget Coordination Fix

This patch fixes two bugs in the I8 aggregate budget invariant and hardens the budget lifecycle.

### Fixed
- **Double-reservation on HOLD resolve** — The membrane's I8 check no longer mutates the budget (pure `available >= amount` check). Reservation ownership moved entirely to the governor's `_reserve_budget()`, which is idempotent via the `budget_reserved` flag. `resolve_hold()` no longer re-reserves.
- **Phantom reservation after ESCALATED→UNKNOWN→FAILED** — `reconcile_unknown()` and `fail()` now use the durable `budget_committed` flag to choose `refund()` (if committed) vs `release()` (if only reserved), instead of inferring from the previous state.
- **`authorize_exit` approve double-reservation** — Approve no longer re-reserves the budget already held by the HOLD; it keeps the existing reservation.
- **`authorize_exit` deny budget leak** — Deny now releases the reservation and clears the `budget_reserved` flag.
- **`revalidate_budget_at_dispatch` now applied** — Budget is rechecked at the dispatch boundary (policy declared `True` but it was never read). The revalidation checks that the operation's reservation is still held (`reserved >= amount`), fail-closed to FAILED if not.
- **`dispatch()` silent commit failure** — If `resource.commit()` returns `False`, the governor now fail-closes to FAILED instead of silently proceeding to DISPATCHING with no budget consumed.
- **`fail()` and `expire_hold()` flag cleanup** — Both now clear `budget_reserved` and `budget_committed` flags after releasing/refunding the budget.

### Added
- `budget_reserved` and `budget_committed` fields on `OperationRecord` (persisted).
- `set_budget_flags()` method on `DurableOperationRegistry`.
- `_prospective_utilization()` method on `DualKernelGovernor` — counts the operation's own amount until it holds a reservation, so the policy sees the same load it would see after the reservation is taken.
- `_reserve_budget()` method on `DualKernelGovernor` — idempotent, under lock, fail-closed.
- `tests/test_hold_budget_regression.py` — 5 regression tests: double-reservation on HOLD resolve, phantom reservation after ESCALATED→UNKNOWN→FAILED, concurrent adjudication reserves budget once, authorize_exit deny releases reservation and clears flags, authorize_exit approve keeps reservation (no double-reserve). Plus mutation-test documentation verifying the _reserve_budget lock is load-bearing.

### Changed
- `tests/test_membrane.py` — Updated for pure membrane behavior (no budget mutation). Concurrency test moved to governor level.

### Tests
- Total: 733 tests passing, 0 failures, 0 regressions (672 in `tests/`, 61 in `r4/tests/`).
- I8 invariant verified on 9 paths: PASS/FLAG, HARD_VETO, HOLD→resolve, ESCALATED→UNKNOWN→FAILED, authorize_exit deny, concurrent, aggregate, fail from RESERVED, expire_hold.

### Known Limitations
- Budget coordination is single-process only (`InProcessBudgetCoordinator` with `threading.Lock`). Cross-process requires a `DurableBudgetCoordinator` backed by `fcntl.flock` or a database transaction.
- Legacy HOLD operations created before this patch have `budget_reserved = False` by default; the reservation from the old membrane code is in `resource.reserved` but not tracked by the flag. Migration is needed if persistent registries contain old HOLD operations.

---

## [5.4.1] — 2026-10-13 — Patch: Assessment Semantics Hardening

This patch corrects two semantic issues in the τ⁻¹ classifier identified during the v5.4.0 hardening review.

### Fixed
- **Callback exception now returns INDETERMINATE, not IRREVERSIBLE** — If the return-transition callback raises an exception, the classifier previously reported IRREVERSIBLE. A failed assessment does not prove that no authorized inverse exists; it means the assessment could not be completed. Both deterministic and non-deterministic callback paths now return INDETERMINATE.
- **Numeric input validation** — distance_q, return_cost, and λ must be finite and non-negative. NaN, infinite, or negative values from the callback now return INDETERMINATE instead of producing meaningless classifications or loss calculations.

### Tests
- 10 new tests for numeric validation (NaN, infinite, negative distance/cost/λ)
- 2 existing tests updated to expect INDETERMINATE instead of IRREVERSIBLE for callback exceptions
- Total: 663 tests passing, 0 failures, 0 regressions (pytest tests/ now runs without --ignore)

---

## [5.4.0] — 2026-10-13 — Minor: τ⁻¹ (Partial Tau Inversion) and EVP (Intended Value Preservation) Assessment Layer

This minor release adds the post-execution assessment layer: τ⁻¹ (partial tau inversion) and EVP (intended value preservation). Both are **ex-post** assessment concepts — they run AFTER execution, not before. Neither is an input to the admissibility function. Both are **opt-in** — `dispatch_and_execute()` does not auto-trigger assessment.

### Added
- **`tenir_governance/assessment/tau_inversion.py`** — `TauInversionClassifier` with 5 classifications: `PRE_COMMIT` (cancelled before effect), `EXACT_INVERSE` (property-preserving return on Q), `COMPENSATION` (partial recovery with loss), `IRREVERSIBLE` (no valid return transition), `INDETERMINATE` (insufficient information). Loss function: L_Q(s, τ, ρ) = distance_Q(ρ(τ(s)), s) + λ · cost(ρ).
- **`tenir_governance/assessment/evp.py`** — `EVPAssessor` with frozen equations: EVP Gross = intended × confidence; EVP Net = realized_value − execution_loss; EVP Ratio = EVP Net / EVP Gross. Friction costs reported **separately** (not subtracted from EVP Net). `FrictionCost` and `GovernanceDebt` dataclasses.
- **`tenir_governance/assessment/assessor.py`** — `PostExecutionAssessor` composing τ⁻¹ and EVP into a single `AssessmentReport`. Documents: opt-in lifecycle (not auto-triggered), in-memory storage limitation (no restart persistence, no tamper evidence), `return_transition` is assessment-only (not a compensating action).
- **Governor integration** — `assess_post_execution()` method on `DualKernelGovernor`. Assessor is opt-in via constructor parameter.
- **SDK integration** — `enable_assessor=True` parameter on `DualKernelCompositionClient`. `assess_post_execution()` convenience method.
- **Tests** — 110 assessment tests: 48 in `test_tau_inversion.py`, 37 in `test_evp.py`, 25 in `test_assessor.py`. Total: 643 tests passing, 0 failures, 0 regressions.

### P1 Semantic Fixes (from peer review)
- **P1-1: Absence of evidence is not evidence of non-execution** — `no_external_effect` defaults to `None` (indeterminate), not `True`. FAILED operations without explicit confirmation return `INDETERMINATE`, not `PRE_COMMIT`.
- **P1-2: Empty Q is vacuous** — Empty/missing `retained_properties` returns `INDETERMINATE`. A zero-distance callback with empty Q does not establish meaningful reversibility.
- **P1-3: EVP equations frozen** — `EVP Net = realized_value − execution_loss`. Friction reported **separately**, not subtracted from EVP Net. Definition, formula, ratio, and examples all agree. 6 hand-calculated EVP tests added.

### Doctrine Compliance
- τ⁻¹ is a partial, conditional, property-relative inverse — NOT a full mathematical inverse (Property-Preserving Crossing manuscript, pp. 11–12).
- EVP measures intended value protection as an **outcome** of governance discipline — EVP ∉ Admissibility Function. High expected value never justifies an otherwise inadmissible transition (TENIR Property-Preserving Transitions Manuscript, Layer 4).
- τ⁻¹ and EVP do not imply each other — a transition can have τ⁻¹ but poor EVP, or vice versa.
- Both are ex-post (post-execution) — v7.1 explicitly defers EVP: "Downstream value measurement is not treated."

---

## [5.3.1] — 2026-10-13 — Patch: Provider Integration, Security, Demo

### Added
- **`tenir_governance/operations/hold.py`** — `HoldManager` class implementing the full HOLD lifecycle: `enter_hold()`, `resolve_hold()`, `escalate_hold()`, `authorize_exit()`, `expire_hold()`, `expire_escalated()`, `check_tau_k_expiry()`, `find_holds()`, `find_escalated()`, `find_unresolved_holds()`.
- **`HoldRecord`** dataclass (hold_start, tau_k_seconds, tau_k_source, escalated_at, escalated_by, exit_reason, exit_receipt) with durable persistence across restarts.
- **`ExitReceipt`** dataclass (authorized_by, scope, timestamp, authorization, reason) for authenticated exit from ESCALATED state.
- **`OperationState.HOLD`** and **`OperationState.ESCALATED`** states with legal transitions: CREATED→HOLD, HOLD→RESERVED|FAILED|ESCALATED, ESCALATED→RESERVED|FAILED|UNKNOWN.
- **`tenir_governance/composition/hold_strategy.py`** — `CompositionHoldManager` with 5 strategies (Priority-Enforce, Serialize, Pareto-Restore, Escalate, Fail-Closed-Collectif) and progressive unblock (Déblocage progressif post Fail-Closed).
- **QUARANTINE_LOCK** in membrane — `lock_quarantine()` / `release_quarantine()` with authenticated authorization and receipt. Distinct from HOLD: blocks ALL admission/dispatch for an LEI until authorized release.
- **Governor HOLD integration** — `adjudicate()` transitions to HOLD state; `resolve_hold()` re-adjudicates with new evidence; `resume_existing()` returns `resolve_or_escalate` for HOLD and `await_authorized_exit` for ESCALATED; `recover()` includes HOLD and ESCALATED operations.
- **Tests** — 59 new tests in `test_hold_lifecycle.py` covering HOLD lifecycle, authorized exit, τ_K expiry, governor integration, QUARANTINE_LOCK, composition-level HOLD, state transitions, and persistence. Total: 501 tests passing.

### Doctrine Compliance
- HOLD is a designed suspension of agency, not a system failure (v7.1 §6).
- ESCALATED is absorbing — no time-based exit; τ_K expiry → UNKNOWN, not resolution (v7.1 §19).
- QUARANTINE_LOCK is distinct from HOLD — blocks everything until authorized release (v7.1 §6).
- τ_K is derived from existing org SLAs, never invented (Blueprint v4.4 §0.5).
- Progressive unblock verifies invariants before each TAU release (Blueprint v4.4 §8).

---

## [5.3.0] — 2026-10-12 — Two-Kernel Composition (Agentic Governance Profile v1.0.0)

### Added
- **`tenir_governance/objects/`** — Governed object types for object-centric governance: `Agent`, `Passage`, `Trajectory`, `Resource`, `Relationship`, `MissionInvariant`. These are the domain objects the composition layer operates on.
- **`tenir_governance/composition/`** — Two-kernel composition layer:
  - `decisions.py` — `Decision` enum (PASS/FLAG/HOLD/HARD_VETO), `ExecutionStatus`, `CheckType` with KB3 posture mapping (PASS→NORMAL, FLAG→CAUTION, HOLD→HOLDING-FIRST, HARD_VETO→HOLDING-FIRST).
  - `policy.py` — `CompositionPolicy` (frozen, versioned, configurable budget escalation threshold, default 85%).
  - `lattice.py` — Decision lattice `compose()` function and `HashChainedLedger` with tamper-evident hash chaining.
  - `governor.py` — `DualKernelGovernor` bridging the continuous risk monitor (`TenirKernel` via `ScoringKernelAdapter`) with the discrete invariant enforcer (`TransitionMembrane`). Durable dispatch state machine: CREATED→RESERVED→DISPATCHING→CONFIRMED|FAILED|UNKNOWN. `resume_existing()` for crash recovery. Fail-closed: kernel exceptions → HOLD, never PASS.
  - `schema_adapter.py` — `conform_to_schema()` mapping governor outputs to TENIR 2.0 schema 1.2 canonical fields (posture, execution_mode, compliance_class, degraded_mode, synthesis, trace, disclosure metadata).
- **`tenir_governance/membrane/`** — `TransitionMembrane` and `MembraneState` implementing discrete invariant enforcement (I3–I9: delegation trust, budget limits, quarantine, terminality, trajectory, temporal constraints).
- **`tenir_governance/operations/`** — Durable operation infrastructure:
  - `budget.py` — `BudgetCoordinator` protocol + `InProcessBudgetCoordinator`.
  - `registry.py` — `DurableOperationRegistry` with `fcntl.flock` cross-process locking, atomic `transition_to()` returning `(bool, prev_state)`, directory fsync for power-loss durability.
- **`tenir_governance/_profile_meta.py`** — `ProfileDisclosure` dataclass declaring `agentic-governance-profile-v1.0.0`, `profile-bound` compliance class, TENIR 2.0 schema 1.2.
- **`DualKernelCompositionClient`** in `sdk.py` — Optional SDK client wrapping the governor for consumers that need two-kernel agentic governance. The standard `TENIRGovernanceClient.adjudicate()` path is unchanged.
- **Validator checks** — `COMP-001` (composition lattice rules: fail-closed, escalation, veto) and `DISC-001` (profile disclosure metadata compliance per versioning policy §6).
- **Tests** — 65 composition tests, 15 membrane tests, 13 fault injection tests, 6 cross-process tests, 17 object tests (116 new tests total).

### Changed
- Package version bumped to `5.3.0` (minor, additive — no breaking changes).
- `__init__.py` exports updated to include composition layer, objects, membrane, and operations packages.
- Validator `validate_all()` now includes composition lattice and profile disclosure checks.

### Compliance
- **KB3**: Posture and execution mode are separate output fields, never collapsed. Implementation states (OperationState, Decision enum) are not rendered as doctrine. Degraded status and compliance class remain visible. No overconfident wording — all guarantees scoped explicitly.
- **TENIR 2.0 schema 1.2**: All module outputs conform to the canonical schema via `conform_to_schema()`. Schema field names are stable; new modules add fields but don't rename existing ones.
- **Versioning policy**: New modules are Profile/Module namespace (§2.3), not canon. `profile-bound` compliance class. Platform version 5.3.0 (minor bump). Profile version `agentic-governance-profile-v1.0.0`. Disclosure metadata per §6. Hard prohibitions §3 observed: no module imitates canonical version line, no API implies authority it doesn't hold.

### Guarantee Boundary
- **Locally guaranteed (in-process)**: At-most-once dispatch through the governance gate, fail-closed composition, mandatory invariant checks not skippable by healthy score, atomic budget reservations, hash-chained ledger integrity.
- **Requires distributed infrastructure**: Cross-host budget coordination, distributed idempotency key registry, exactly-once external effects across service boundaries.
- **Depends on external provider behavior**: Provider-side idempotency key support for exactly-once external effects.

---

## [5.2.0] — 2026-10-07 — TENIR 2.0 Kernel & Security Hardening

### Added
- `tenir_kernel` dependency-light execution kernel with schema 1.2 validation, constraint geometry, CLI, API wrapper, and Python/HTTP connectors.
- Secure-schema12 runtime object path and browser-compatible WebSocket authentication.

### Changed
- Package version and public API metadata updated to `5.2.0`.
- R5 HTTP services require bearer authentication and operator/oath secrets; Docker network bindings are localhost-only by default.
- Mode recovery and signed transition recording are aligned across wired and hardened R5 surfaces.

### Fixed
- SHADOW_OFF now returns `NOT_EVALUATED` without evaluating inputs.
- Malformed connector authorization responses fail closed.
- Unknown geometry severities fail closed.
- CAUTION posture is surfaced as a `FLAG` rather than silently passing.
- Legacy wired transition ledger calls use the canonical nonce field.

### Validation
- Release branch validation is performed through the repository CI gate and kernel/runtime regression suites.

---

## [5.1.0] — 2026-07-01 — Coverage Uplift & Governance Kernel

### Added
- **tenir-kernel/** — lightweight governance kernel (Tier 1): six-file reference
  implementation of S = K/(P×V+ε) with YAML policy engine, FastAPI adjudication
  endpoint, and append-only SHA-256 Merkle ledger. Operates without Neo4j.
  Thresholds (hard_veto=0.75, flag=0.90) mirror R5 full middleware defaults.
- **tests/test_coverage_uplift.py** — 118 new tests covering SDK public interface
  (TENIRGovernanceClient, GovernanceEvent, GovernanceResult), Validator CLI and
  ledger-chain checks, CopyLinter document scanning and CLI, LedgerMigrate
  pipeline and verification flow, and all regression corpus helper functions.
- **tenir-kernel/tests/test_tenir_kernel.py** — 8 targeted kernel validation tests
  (K1–K8) covering all verdict branches, epsilon guard, formula numerics,
  determinism, and Merkle ledger chain integrity.

### Changed
- Statement coverage: 72% → 96% across tenir_governance package (no source changes)
- Total test suite: 431 → 557 (549 middleware + 8 kernel; 555 passing, 2 skipped)
- README: complete rewrite documenting two-tier architecture, updated counts,
  threshold correspondence table, per-profile policy documentation

### Fixed
- CITATION.cff: updated abstract, full manuscript title, correct test counts
- Manuscript test count reconciliation: previous drafts reported 445/422;
  definitive figures are 557 total / 555 passing / 2 skipped / 96% coverage

---

## [5.0.0] — 2026-06-29 (IRON OMEGA R5) — Current Release

### Summary
Full production release of TENIR-Gov as an integrated governance middleware.
Merges R4 (partner_a Shadow v4) and R5 (IRON OMEGA) into a single installable package with a shared canonical contract.

### Added — Governance Package (`tenir_governance`)
- **Sprint 0** — `nomenclature.py`: single canonical term registry for R4+R5 vocabularies, three-register language system (Canonical / Operator / Public)
- **Sprint 1** — `policy_engine.py`: frozen `PolicyEngine` dataclass with three factory profiles (default, partner_a Shadow v4, partner_b Sovereign Pilot); `evaluate_membrane()` as the sole membrane decision function
- **Sprint 2** — `__init__.py`: clean public API boundary; installable via `pip install -e .`
- **Sprint 3** — `validator.py`: `TENIRValidator` with 9 named invariant checks (POL-001 → NOM-002); CLI (`tenir-validate`) and pytest fixture
- **Sprint 4** — `regression_corpus.py`: 281 golden `GoldenCase` fixtures across 16 scenario groups
- **Sprint 5** — `sdk.py`: `TENIRGovernanceClient` public API for both R4 (`adjudicate()`) and R5 (`adjudicate_from_nsl()`) paths
- **Sprint 6** — Plain-language hardening: `SCHIZOPHRENIA → SIGNAL_CONFLICT`, `WHALE_RESONANCE → DEEP_PATTERN_SIGNAL` with backward-compat aliases
- **Sprint 7** — CES classifier extended with `pressure` and `velocity` parameters; 14 corpus fixtures updated to reflect activity-based classification
- **Sprint 8** — Expanded posture family: `ROAM`, `LEARN`, `TRANSMIT`, `REST`, `SLEEP`, `STOP`; `AdmissibilityClass` added
- **Sprint 9** — `copy_lint.py`: CI-gated public-safe lexicon enforcement for `.md`, `.html`, `.txt`
- **Sprint 10** — `polymorphic_surface.py`: V5 Persona/Workflow surface contract (4 states, Two-Glass bridge, `requires_ceremony()`)
- **Sprint 11** — `ledger_migrate.py`: forensic JSONL ledger migration with migration-genesis hash and chain continuity

### Added — R5 Runtime (`r5_hardened/`, `r5_wired/`)
- LALR(1) NSL grammar parser with LLM fine-tuning pipeline (QLoRA / Axolotl)
- Neo4j 5.x graph ontology for CP-Net policy representation
- Merkle epoch audit ledger with inclusion proofs and peer broadcast
- `KeyCeremony` protocol for operator oath epoch-binding
- FastAPI server (`r5_server.py`) with `/adjudicate`, `/oath/sign`, `/transition`, `/ledger/verify`, `/ledger/proof/{id}` endpoints
- WebSocket hub (`ws_hub.py`) + Three.js VPS live engine

### Added — Infrastructure
- `Dockerfile` reproducibility container (Python 3.11-slim)
- `docker-compose.yml` with profile-gated Neo4j (`--profile graph`)
- 6-gate blocking GitHub Actions CI pipeline (`tenir-ci.yml`)
- `CITATION.cff`, `.zenodo.json` for Zenodo and GitHub citation metadata

### Validation
- 445 tests passing (100%): 313 governance package · 61 R4 · 56 R5 hardened · 15 R5 integration
- 70% statement coverage on `tenir_governance`
- 9/9 policy invariant checks across default, partner_a, and partner_b profiles
- Policy fingerprint (default): `d083e0b82a16c04d`

### Known Limitations
- Multi-tenant / multi-organisation isolation not benchmarked
- Distributed ledger replication out of scope
- Administrative override records disabled (fail-closed posture)
- NSL grammars require domain-specific schema definitions

---

## [4.0.0] — 2026-01 (partner_a Shadow v4)

### Summary
R4 release: internal UAT package for partner_a Shadow-mode deployment.
Introduced `TenirMonitor`, `HashChainedLedger`, `OperatorRegistry`, `PolicyBundle`, CAVE adjudication framework, and burn-cost estimation.
R4 runtime known issues (G1–G6) documented in `HARDENING_REPORT_v1.0.0-rc2.md`; these are addressed by construction in R5 through the shared `PolicyEngine.evaluate_membrane()` contract.
