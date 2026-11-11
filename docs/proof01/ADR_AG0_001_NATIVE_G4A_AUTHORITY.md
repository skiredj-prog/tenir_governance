# ADR-AG0-001 — Native positive G4-A authority selection (laboratory only)

- **Date:** 2026-10-29
- **Decision owner:** Agent 0 / PMO architectural coordination (owner approval for production remains separate)
- **Decision status:** **ACCEPTED FOR DESIGN SELECTION ONLY; OPERATIONAL AUTHORITY NOT ADMITTED**
- **Contract version:** `ADR-AG0-001/v1.0`
- **Candidate grant wire schema:** `native-positive-g4a-grant/v1`
- **Integration:** `INTEGRATION_LOCK_HOLD`
- **Normative / REG+ assurance:** `NOT_VERIFIED`
- **Production / external effects:** `HOLD`
- **Scope:** explicit POSIX single-host, lab SQLite, non-certifying; no live provider authorization.

## Evidence pins and branch reality

| Subject | Exact identity | Status |
| --- | --- | --- |
| G4-A historical simulated authority | PR #73 `9d8bf1c462589ac497e3e03ece48d1d9ab0b9db3`; `ExperimentalG4AAuthority` | Simulation only, `decision=HOLD` and `execution_authorized=false` |
| Candidate positive grant interface | PR #95 `edd42e41feff93d329fda62ad6e1aaee58be6ff0`; `w2_integration/native_positive_g4a.py`; blob `0a776493dad28aec19b3ddd22f63bd69e38af477` | Draft, independently scoped candidate |
| Candidate JW-01..10 laboratory qualification | PR #101 `e788f73483c156491290ba9d42a053291ae14221` (on PR #98) | 51/51 tests PASS according to run #40821726225, aggregate `HOLD_NOT_VERIFIED` |
| Actions receipt | run `40821726225`; artifact `12874299887`; GitHub digest `sha256:0c091dea9ace4da8775987729b175bf75f6806d6f1aab134b8a4bcec23d3ca68` | CI success is **not** normative approval; independent byte custody not established here |
| Actual code topology on #101 | `lab-s1-native-sqlite-single-host-2026-10-28` | Mismatches requested `LAB-TENIR-GOV-PROD-CANDIDATE-09` |

**Critical:** PR #95 and PR #101 have diverged histories. The file `w2_integration/native_positive_g4a.py` is not in PR #101's exact commit tree. The 51 passing tests **cannot** be used as evidence that this candidate positive grant interface is integrated. No integrated SUT SHA or shared evidence bundle for this junction has been established.

## Decision

1. **Select** `NativePositiveG4A` and `native-positive-g4a-grant/v1` as the **named design candidate** for a lab-scoped, positive execution grant boundary. It must be implemented/qualified against a single integrated SHA before any operational authority is recognized.
2. **Assign authority ownership without duplication:** `NativePositivePermitAuthority` owns native SQLite permit, reservation linkage, dispatch claim and outbox state in the candidate lab; `NativePositiveG4A` mediates the versioned grant and must not become a second reservation/budget authority. The provider must verify the actual native claim. Any fallback path that dispatches without the same qualified authority is forbidden within the intended interface contract, but **exclusivity is not yet demonstrated**.
3. **Retain unchanged** `ExperimentalG4AAuthority` and `G4AReceipt`: historical simulation, `HOLD`, `execution_authorized=false`, `budget_settlement_authorized=false`, `is_production_authority=false`. Do **not** rename an existing class, flip its flag, or treat a positive laboratory grant as a promotion of the historical receipt.
4. **Withhold execution authority and promotion** until explicit integration, privilege isolation, crash recovery and independent assurance gates below are satisfied. The design selection is not an owner-issued deployment grant, standard ratification or REG+ profile admission.

## Candidate contract: gate and reject semantics

The versioned grant is a signed, short-lived, one-use **candidate lab authorization** that binds at least: schema version, operation key, evaluation ID, canonical request hash, permit ID, reservation ID, resource ID/amount, grant ID, issued/expiry times and signing key identity. Dispatch must additionally bind the attempted idempotency key, stored native claim, concrete payload, authority epoch/topology and relevant witness/monotonic trust identity. Fields not present or proven in `v1` must be called **missing obligations**, not implicitly assumed.

**Allowed order (laboratory only):** evaluate candidate → native I8 reservation + positive permit → signed `native-positive-g4a-grant/v1` → validate persisted grant and claim → durable dispatch intent → provider's independently verified claim gate → B3 simulated effect → receipt / UNKNOWN / reconciliation.

**Reject/HOLD** when the grant is absent, expired, changed, unsupported or already consumed; request/evaluation/permit/reservation/payload/epoch differ; the trusted identity cannot be verified; the witness or custody is missing/stale; or the provider could be invoked on an alternate path. No unverified evidence may convert UNKNOWN to confirmed or release I8 capacity. A caller-controlled configuration flag is not sufficient to downgrade custody requirements after a store has entered a verifiable mode.

**Observed implementation gap in PR #95:** reservation/permit precedes the separate grant insert; claim persistence precedes a separate grant-consumption write; B3 remains outside both. These intermediate crash/retry windows need a single-SHA fault-injection oracle, idempotent replay policy and non-duplication proof. Tests of the happy path and invalid grant do not establish these crash semantics. The current causal helper also does not demonstrate binding of the dispatch-time idempotency key into the signed grant; this must be made explicit in the integrated contract.

## Gates to change status

| Gate | Required independently reviewable outcome | Current status |
| --- | --- | --- |
| AG0-G1 source and interface lock | Pin actual merged/integrated SUT 40-hex SHA including PR #95 selection, PR #101 fixes, parent commits, schema and migration protocol; reproduce both suites | **OPEN** |
| AG0-G2 single budget/dispatch authority | Inventory every writer and dispatch route; reject direct G4-A/G3/SQLite/provider bypass and hostile same-UID writes, including real negative controls | **OPEN (B1 / R-01)** |
| AG0-G3 crash-safe atomicity | Inject crashes between reservation/permit, grant, claim, Witness and Kernel commits; demonstrate no orphan promotion, double settlement or double effect | **OPEN** |
| AG0-G4 anti-downgrade | Persistent, monotone verifiable-mode floor independent of runtime `verifiable_recovery` argument; restart with false must fail closed after verified mode | **OPEN (JW-10)** |
| AG0-G5 joint rollback | External monotone anchor unaffected by restoration of both Kernel and Witness SQLite stores; repeat joint-rollback falsification | **KNOWN SECURITY GAP** |
| AG0-G6 independent custody/provenance | Demonstrated B2 privilege/key custody; externally authenticated provider evidence; no adapter re-signing of untrusted claims | **OPEN** |
| AG0-G7 exact topology and assurance | One declared deployment manifest/digest/topology, raw CEB-01, independent W4 replay and RFC-7 scoped report against the same SHA | **PENDING** |

**Orthogonal results:** `TEST_PASS` may coexist with `SECURITY_FAIL` where a bypass/rollback is reproduced. A CI `SUCCESS` with global `HOLD_NOT_VERIFIED` is an expected valid test result, never a deployment authorization.

## Handoffs and responsibilities

- **Agent 1 / W2:** reconcile PR #95 with PR #98/#101 into one exact candidate tree, keep only one native I8/budget owner, supply graph of authority call sites, crash/retry tests, original evidence and deployment manifest.
- **Agent 2 / W3-B1:** prove effective writer isolation against same-UID/higher-privilege attacks and direct provider bypass, not just cooperative API checks.
- **Agent C / W3-B2:** separately qualify witness/key custody and independently anchored monotone rollback resistance; do not equate separate SQLite files with independent trust domains.
- **Agent 3 / W4:** replay original CEB-01 on the integrated SHA/topology independently; evidence from PR #101 or older W4 is not transferable.
- **Agent 0 / PMO:** maintain this decision, identity pins, dependency map and scope labels. No silent normative or production promotion.

## Change control

This ADR selects an architectural **candidate**. It does not modify source, schemas, credentials, runtime flags, production topology, RFC authority or release state. Updating the selected interface, grant contract version or owner requires a new reviewable ADR revision and exact-source evidence. Do not merge #95, #98 or #101, turn on dispatch in production, or close PROOF-01 on the strength of this decision alone.

**Canonical status string:** `AG0_G4A_CANDIDATE_SELECTED / PR95_NOT_IN_JW101 / NATIVE_AUTHORITY_UNQUALIFIED / JOINT_ROLLBACK_SECURITY_GAP / ANTI_DOWNGRADE_OPEN / B1_B2_OPEN / INTEGRATION_LOCK_HOLD / PRODUCTION_HOLD`
