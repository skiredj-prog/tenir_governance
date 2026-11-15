# Product identity and claims ownership

**Decision date:** 2026-11-10. **Status:** consolidation policy for current development. This document does not change a released artifact or establish runtime compatibility.

TENIR-Gov is the single product identity for maintained execution-governance development. Selected capabilities from the HOLD prototype will enter that product through the migration gates in [HOLD_MIGRATION.md](HOLD_MIGRATION.md). A shared product identity does not mean that the implementations have already been merged.

## Names and their scope

| Name | Use in current material |
|---|---|
| **TENIR-Gov** | The maintained product and runtime. Name the package, source revision, enabled path and deployment scope when describing behavior. The Python distribution remains `tenir-governance`; the import remains `tenir_governance`. |
| **HOLD prototype** | The historical hackathon implementation, its source, demonstrations and experiments. Use this full label on first mention; link its own version and evidence. |
| **REG** | The specification and qualification framework. A conformance statement needs a named profile, implementation, environment and executed assessment. |
| **TENIR** | The broader architecture/research programme. Do not use this alone as the identity of an executable artifact. |
| **HOLD disposition / operation state** | A runtime term, qualified by component and field. This is not a reference to the HOLD prototype. |

Use one current developer entry: [Getting started](GETTING_STARTED.md). Keep historical HOLD links accessible under prototype/experiment provenance, rather than presenting a second interchangeable installation path. Existing package names, schema identifiers, DOI records and frozen artifacts retain their original names.

## Publication versus development

The published release baseline is [`v5.7.0`, `47ef67e3efc5f3008896a7d88bebf929fa0a98f0`](https://github.com/skiredj-prog/tenir_governance/tree/47ef67e3efc5f3008896a7d88bebf929fa0a98f0). The consolidation starts from `092bb45ad375a769bed4c51c670f800a6b6b7671`: its differences from that release are documentation only. New onboarding code and subsequent migrations are development changes until included in an identified release. Installing PyPI 5.7.0 does not obtain those new changes.

Later experimental branches are separate subjects. Their tests, authority mechanisms or readiness labels do not describe the published package unless the exact implementation has been merged, packaged and checked. A branch name or aggregate passing-test count is insufficient provenance.

## A claim belongs to an implementation

Every current capability or assurance claim must identify:

1. **Subject:** product, package version and exact source revision; include local modifications when relevant.
2. **Path:** component, API/entry point, configuration, enabled options and schema/profile versions.
3. **Boundary:** host/store scope, provider, credentials/authority assumptions and whether effects are synthetic.
4. **Evidence:** protocol/test selection, run or artifact reference, platform and observed result.
5. **Status and limits:** implemented, observed in a scoped run, planned, or not assessed; known exclusions remain attached.

Use precise statements such as: “The TENIR-Gov quickstart run at the recorded revision observed one dispatch to its synthetic provider after a lost response and refused the repeated dispatch.” Do not shorten this into a universal claim of exactly-once provider effects.

The HOLD E1-A6 results belong to `e1-a6-frozen`, with the protocol, traces and limitations published in the [HOLD results](https://github.com/skiredj-prog/hold/blob/f3bb683d7be76c608b8557bf2d41a89b7abfb6d0/E1_A6_RESULTS.md). They do not establish TENIR-Gov behavior, a migrated implementation, REG conformance, or a real provider integration. A port requires its own tests and, for a new benchmark claim, its own declared protocol and run.

## Keep the meanings separate

These are distinct facts, even when one user journey displays all of them:

- Business approval approves a proposed deployment or use case.
- A mandate specifies an actor's bounded authority.
- An execution disposition permits, suspends or refuses a proposed transition.
- Dispatch records whether the governed path attempted the external call.
- An effect outcome states what evidence establishes about the external action.
- Mission status describes the workflow, including closure or revocation.

`PASS` is not an effect receipt. A consumed authorization is not a provider call. A human permission to continue is not confirmation of an external effect. Revocation can stop future actions while a previously dispatched effect remains `UNKNOWN` and awaits reconciliation.

The migration contract names these dimensions explicitly; it does not silently map unrelated uses of `HOLD`, `I1`, `profile`, `sigma` or `tau` across implementations. Preserve each source namespace until an explicit semantic mapping is tested.

## Correcting reports and developer material

For active pages and reports, replace ambiguous implementation names with the actual subject and attach the claim's scope. If that subject cannot be established, mark it **unattributed / not assessed** until checked. Do not infer it from a nearby diagram, a product name, or a similar status label.

For a dated report, frozen benchmark, signed record or published release, preserve the original bytes and identifiers. Add a separately dated correction or contextual link identifying the affected passage, actual implementation and consequence for the claim. Never relabel a HOLD trace as a TENIR-Gov trace.

Before publishing current material, check that:

- The install command obtains the version that contains the demonstrated feature.
- Each current runtime assertion identifies TENIR-Gov's actual implementation path.
- HOLD prototype results appear only with their historical attribution.
- REG compatibility is described as planned until the named assessment has run.
- A successful local demo is not presented as enterprise deployment qualification.

The existing [deployment boundaries](DEPLOYMENT_BOUNDARIES_V5.7.0.md) and [post-release documentary status](POST_RELEASE_V5.7.0_DOCUMENTARY_STATUS.md) remain applicable to v5.7.0.
