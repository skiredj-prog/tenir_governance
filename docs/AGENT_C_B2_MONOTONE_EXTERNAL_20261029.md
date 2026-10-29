# Agent C — B2 custody and external monotone anchor qualification (candidate)

**Reviewed exact candidate:** \`e788f73483c156491290ba9d42a053291ae14221\` (PR #101, stacked on #98).  
**Source-review disposition:** \`B2_NOT_VERIFIED / EXTERNAL_MONOTONE_NOT_VERIFIED / JW10_DOWNGRADE_OPEN\`.  
**Boundary:** read-only source review and reference-contract oracle; **not a native service integration, external custody attestation, or production compliance proof**.

## Source observations

1. \`w2_integration/independent_witness.py\`: \`IndependentWitness\` loads \`TENIR_WITNESS_PRIVATE_KEY_HEX\` in the in-process signer and uses a separate **local SQLite WAL** database. \`EXTERNAL_HEX\` identifies how bytes were provided, **not** independent HSM/KMS custody.
2. \`w2_integration/native_positive_permit.py\`: \`NativePositivePermitAuthority\` reads \`TENIR_W2_PRIVATE_KEY_HEX\`. Only the opt-in \`verifiable_recovery=True\` branch checks and persists local Kernel/Witness key fingerprints. The default branch requires a separate anti-downgrade closure.
3. Witness checkpoint and Kernel claim are **two database transactions**. A crash can leave an orphan Witness checkpoint; JW-09 tests same-identity recovery but does not prove cross-store atomicity.
4. \`audit_tests/test_jw_joint_rollback_limit.py\` directly reproduces restoration of **both** local SQLite databases to a coherent old snapshot and generation of a new claim identity. It does **not** call an external provider or show duplicate external effects.
5. \`docs/EP02_EXTERNAL_MONOTONE_CAS.md\` is an authenticated CAS **experimental design**, not wired to the native execution path; its own full trust-root rollback is an explicit remaining limitation.

## B2 exit — required real evidence

- Independent operator/custodian under a different administrative control plane and separate IAM/OS identity. Name threat model and exact source/deployment SHA and topology.
- HSM/KMS/third-party non-exportable private signing key; independently verified public root, authorized signing policy, revocation and rotation.
- Producer credentials unable to **read/export** signing private material, sign an unauthorized checkpoint, **modify/delete** Witness anchor state, or replace the trust root. Preserve raw DENY logs and effective policy.
- Canonical signer/recipient binding: subject/tenant, command hash, source SHA, topology, epoch, sequence, prior hash, exact request ID, expiry and authority scope. Verify independent signed bytes.
- Negative tests: wrong tenant, changed digest, forged signature, replay, expired/revoked key generation, provider process restart, and signer unavailability all refuse authorization.
- Independent W4 verification of original receipt/bytes, signer identity and all negative controls. Merely injecting a 32-byte hex key from the producer is insufficient.

## External monotone exit — required real evidence

- A **durable high-water CAS** authority outside the restore and admin domain of **both** Kernel and Witness, with independently held signer. External endpoint, authentic public trust and available recovery model must be documented.
- Before an irreversible effect, use a fresh signed observation or durable CAS receipt bound to the exact state/epoch/digest/command; prevent TOCTOU between validation and consumption. **An anchor receipt is not a G4-A execution grant.**
- Real **joint rollback** of both local stores while external high-water remains advanced: startup/claim/dispatch all refuse; matching revision with conflicting digest also refuses.
- Fail-closed on unavailable, stale, wrong-key, wrong-topology, same-ID different-content, timeout/UNKNOWN. Recovery uses exact old request ID, not a new effect.
- Real crash after successful anchor CAS but before native commit: restore the same bound identity without duplicate provider effects; retain original evidence.
- Prove that attacker **cannot also roll back the external anchor** or its public-trust material; otherwise limitation remains, even if the anchor is hosted remotely.

## Reference-model tests

\`audit_tests/test_agent_c_anchor_reference.py\` is a small **pure reference model** verifying challenge binding, signature substitution, CAS replay/collision, state drift and joint local rollback against a non-rolled-back in-memory external model. Pass means **contract consistency only**; not real external monotonicity, distributed access control, service durability, or live provider effect.

## Gate dispositions

| Gate | Result |
|---|---|
| Source-level finding: environment key + local databases | EVIDENCED |
| Local joint rollback is not detected | EVIDENCED in existing negative control |
| External private-key custody | GAP — independent custodian missing |
| Non-rollbackable external monotone source | GAP — live deployment missing |
| JW-10 default-path anti-downgrade | GAP — guard not universal |
| Joint-recovery B1 writer isolation | Separate OPEN gate |
| Integration/E2E/Production | HOLD |

Tracked in [Agent C #105](https://github.com/skiredj-prog/tenir_governance/issues/105) and [Agent C #106](https://github.com/skiredj-prog/tenir_governance/issues/106). No production source changes in this audit branch.