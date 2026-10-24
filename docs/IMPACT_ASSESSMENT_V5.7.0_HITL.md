# TENIR-Gov v5.7.0 — Impact Assessment: human authorization claims

**Status:** post-release documentary addendum; not a formal impact assessment of a particular deployment, not a safety certification.  
**Release identity:** `v5.7.0` / `47ef67e3efc5f3008896a7d88bebf929fa0a98f0`  
**Date:** 2026-10-24

## Claims — controlled wording

| Claim | Disposition | Reason |
|---|---|---|
| “Human-in-the-loop as a safety guarantee.” | **RETIRED / MUST NOT CLAIM** | Human approval alone does not prove correctness, comprehension, independence, risk reduction or provider-side safety. |
| “Human-in-the-loop as an authorization mechanism with cryptographic provenance.” | **RETAINED AS A CONDITIONAL DESIGN / INTEGRATION CLAIM** | The authorizing record can be bound to identified intent and independently verifiable evidence, **provided** the deployed integration actually implements identity binding, digest verification, timestamp provenance, signature validation and durable audit records. |

## Required qualification

Do not imply that `v5.7.0` universally proves a cryptographically signed **human** approval workflow. An implementation-specific evidence matrix must identify the approval issuer, trust root, cryptographic primitive, signed fields, immutable operation fingerprint, verification boundary, replay/expiry policy and tests on the exact release artifact. A Merkle audit log or payload SHA-256 alone is not proof that a human reviewed the right context.

For the **Amber** class, human approval is a necessary authorization condition **where the declared REG profile requires it**. It is not sufficient to establish admissibility or safety. The reviewer must have a genuinely independent path to inspect raw context and/or verify the digest against independently obtained raw data; a hash without accessible underlying evidence does not meaningfully mitigate automation bias.

REG E2 is a **specification of an external boundary**, not a retroactive implementation guarantee of TENIR-Gov. Cross-product claims require a versioned interface contract and exact-code test evidence.

## Acceptance criteria for elevating the retained claim

1. Bind approver identity, authorization channel, decision time, exact operation fingerprint and policy version.
2. Preserve an independently inspectable source and confirm the reviewer can compare it to the signed/digested evidence.
3. Reject missing, stale, replayed, untrusted or mismatched approval before dispatch.
4. Record dissent and refusal and measure approval reversals / override correctness with an independently specified ground truth.
5. Run and retain positive and negative tests on the deployed release and integration, including UNKNOWN, HOLD, quarantine and post-approval payload changes.

**Until all items are evidenced, represent the statement as a conditional architecture capability, not a verified production guarantee.**
