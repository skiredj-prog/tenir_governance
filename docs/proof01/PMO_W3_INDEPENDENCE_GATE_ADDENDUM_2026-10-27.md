# PROOF-01 / PMO — W3 structural independence gate (addendum v0.2 candidate)

**Date:** 2026-10-27. **Owner:** Agent 0 / PMO. **Review status:** candidate, **not a security qualification**, `SOURCE_LOCK=PINNED`, `INTEGRATED_SUT=PENDING`, `SYSTEM_ASSURANCE=HOLD`.

## 1. Clarification: two non-interchangeable deployment profiles

| Profile | Purpose | Allowed positive result | Structural-independence status |
|---|---|---|---|
| `LAB-S1` — single Linux x86_64 host, isolated processes/users/containers, distinct SQLite paths, mock provider | Functional integration, I8/REPAIR, crash/race, known Q1/Q6/R-01 adversarial discovery | Scoped laboratory observations ONLY | **NOT ESTABLISHED** for W3 §§3.1/3.4/3.5, and independent custody/roots |
| `IND-M1` — distinct host, identity/administrator, network and preferably infrastructure provider, independent key custody/time/entropy, separately observed trust anchor | Test W3 structural independence against specified actor compromise and collusion | Candidate bounded W3 independence determination | **PENDING evidence** |

`LAB-S1` is an engineering test **not** a substitute for `IND-M1`. The PMO may form a bounded **functional integration lock** from LAB-S1 W2 data only. An **independence assurance lock** remains HOLD until all W3 proof obligations have been assessed on IND-M1 or explicitly excluded from a narrower claim *before testing*. Production HOLD always.

- §3.1 Infrastructure: separate UID, container, namespace, volume on same host **do not** meet separate host/provider/admin network requirement; shared kernel/admin is common-mode compromise.
- §3.4 Clock: same host wall clock is NOT independently sourced time. Identify W3's actual time source, trust, drift bound and clock manipulation tests.
- §3.5 Failure asymmetry: runtime crash and witness crash on same host are not mutually independent. **Safety override:** witness unavailable must not silently authorize a new consequential effect; other legitimate non-consequential Agent 2 functions MAY continue if policy permits. W3 text suggesting "Agent 2 continues" must not be interpreted as permission to execute without required evidence.
- §2.1 Separate SQLite filenames do not on their own establish isolation. Report distinct realpaths, device/inode, bind mounts/volume identity, owner/ACL, actual runtime effective UID/GID, symlinks, credential and write-channel inventory. An unprivileged read-only mount blocks only tested capabilities; root/host-admin compromise remains in-scope when asserted.
- §3.2 Identity/trust: mock provider keys under the same operator do not establish externally controlled roots. Distinguish provider evidence signature from independent witness key custody and independent trust-root governance.
- §4.6 Trust substitution: negative test must inject a replacement root in a disposable environment and check a **pin external to the mutable actor**, including rotation/revocation.
- §4.8 Legal constraints: simulated test cannot establish jurisdiction/legal compel resistance or a defensible operator identity. Not validated, not "PASS"/legal assurance. Blind witness design must show hashes/commitments only in its observed inputs (§3.3).
- §1.3 Key creation date local metadata ≠ independently attested pre-existence. Requires independently verifiable historical timestamp/register outside the actor's authority.

## 2. W3 dossier mandatory matrix

Agent C owns independent witness contract, evidence protocol and negatives; Agent 2 owns runtime authority/privileged-writer R-01 and provides adversarial actor credentials/environment. Each line below MUST have: **positive design/observation**, **negative inability demonstration and threat-model maximum**, **independent observable test/source**, **SHA/deployment identity**, **status** (ESTABLISHED_IN_SCOPE / REFUTED / INDETERMINATE / NOT_TESTED / EXCLUDED_PREDECLARED), and **unobservable residue/falsification floor**.

- **Identity (1.1–1.4):** independent root, operational identity/operator/jurisdiction, identity antedating first observed action, and no overlapping accounts/secrets.
- **Privileges (2.1–2.5):** W3 private-key read/write denial from Agent 2, non-impersonation under full Agent 2 compromise, directional commitments flow and observed egress, actual OS/VM/account boundary, separately operable revocation.
- **Independence (3.1–3.6):** distinct host/provider/admin network, independent root, blind input, independent clock, asymmetric failure, independently generated entropy.
- **Adversarial (4.1–4.9):** W3 compromise, W3+Agent2 collusion, replay, omission/order selection, eclipse/partition, root substitution, key compromise/revocation, legal compulsion, and ritualistic signing/monitoring fatigue.

**Acceptance criterion:** an auditor can establish (a) control of Agent 2 alone cannot produce a W3-valid attestation within declared attack scope; (b) control of W3 alone cannot manufacture evidence of a separately verified operator identity for Agent 2; (c) control of both has an explicit **residual falsification floor** stated and acceptable to the auditor. Missing independently verifiable pre-existence §1.3, blind content §3.3, separate credential control, adversarial attempts or collusion floor => **REJECT this W3 independence claim**, even if the cryptographic checks and unit tests pass.

A signature proves a signing act; it does not alone prove that independently governed observation happened or that unobserved events did not exist.

## 3. Non-overlapping ownership

- **Agent 2 #74:** actor full-compromise model, R01-N01..N06, privileged writer paths, runtime IAM/DB/FS/network access and impersonation attacks (supplies attacker capability inventory).
- **Agent C #76 / #78:** W3 operator/key/witness roots and custody, Q1/Q6, witness independence 1.1–4.9, full matrix, time/entropy/fault/rollback, named witness credential rotation and provenance (produces W3 acceptance dossier).
- **Agent 1 #81:** genuine W2 integration/export of RC-01/I8/B3, *not* owner of witness trust-root semantics.
- **Agent 3 REG #56:** independently review W2/W3 evidence and accept/refute each claim based on externally pinned anchors; no source-record rewriting or re-signing.
- **Agent 0 #82:** lock profiles separately and only promote claims that pass the named profile and threat model.

## 4. Evidence format and reproducible probes

Convergence bundles follow `CEB-01/0.1` as inventoried `evidence/` with two profiles and explicit domain labels:

- `evidence/w3/write-path-inventory.json` — actors/process identity, fs/volume path/inode/owner/ACL, UID/GID, privileges, secrets, network/endpoint, SQL UDF/DDL/DB replacement rights, real observed write results.
- `evidence/w3/independence-matrix.json` — per criterion 1.1..4.9: proof and counterexample pointers, tested adversary powers, unseen region, disposition, falsification floor.
- `evidence/w3/trust-and-custody.json` — immutable trust pins supplied externally, operator and independent admin identity, creation-time attestation, signer rotation/revocation, monitored expiry.
- `evidence/w3/attack-results.json` — independent W3 capture and Agent2 compromise/partition/rollback tests, test pass distinct from security verdict.
- `convergence.json` references exact SUT full SHA + deployment digest + topology/profile/instance, all raw evidence SHA-256; claims about IND-M1 must **NOT** use LAB-S1 source as sufficient proof.

Do not falsely equate independent source SHA, shared-test host, observed clock reading or self-attested owner with provenance independence.

## 5. E8 exact branch verification

At review on 2026-10-27, GitHub comparison reports **identical (ahead 0 / behind 0)** between branch `draft/e8-container-boundary-evidence-20261026` and source-lock SHA `62ef6f30efd27003d654758b7fe8e2f885356da2` (REG PR #52). **Faïçal must pin this 40-hex commit** in the clone via `git rev-parse HEAD` before tests; branch name alone is not an immutable baseline. If HEAD moves, flag SOURCE_LOCK_DRIFT and require PMO re-lock or checkout exact commit. External review invitation/identity does not alone meet W3 §1.2/§1.3.

## 6. PMO gate status

- `SOURCE_REFERENCE_PINN`: YES, including E8 branch SHA match at review time.
- `LAB_S1_FUNCTIONAL_INTEGRATION_LOCK`: **PENDING W2 full SHA/deployment export**.
- `IND_M1_W3_INDEPENDENCE_LOCK`: **HOLD**, no qualifying independent infrastructure/custody/timing evidence.
- `PROOF01_SYSTEM_CONVERGENCE`: **HOLD**; `PRODUCTION`: **HOLD**.
- PR #83 remains DRAFT. Existing source lock remains immutable as documented; this addendum updates deployment *claim boundaries*, not pinned source SHAs.
