# TENIR-Gov — Scope and relationship to REG-ARCH

**Runtime:** `tenir_governance` **5.7.6**, tag `v5.7.6` (20 November 2026).

**Document status:** scope declaration; not a REG-ARCH ratification, refinement declaration, or certificate.

## Relationship to REG-ARCH

TENIR-Gov is an **implementation informed by REG-ARCH**. It does **not** claim conformance to REG-ARCH, and REG-ARCH is **not normative** for this runtime.

- **REG-ARCH status:** `v0.4.1-candidate`, **unratified**. Canonical version register: [reg-conformance/specs/reg-arch/README.md](https://github.com/skiredj-prog/reg-conformance/blob/main/specs/reg-arch/README.md), established by [PR #73](https://github.com/skiredj-prog/reg-conformance/pull/73) (merge commit `33c6302ae70c3202a80ca0e7d6e5b1db03d41fc2`).
- **Candidate text:** [specs/reg-arch/v0.4.1-candidate/REG-ARCH.md](https://github.com/skiredj-prog/reg-conformance/blob/main/specs/reg-arch/v0.4.1-candidate/REG-ARCH.md), generated from the committed DOCX.
- **No executable refinement declaration** connects this runtime to REG-ARCH. A versioned refinement mapping with assumptions, trace obligations and executable tests is required before claiming REG-ARCH conformance (planned REG-ARCH sprint 5).
- The candidate specifies architectural obligations and contains model-level test scenarios and oracles. It does **not yet establish complete applicable state machines, door-field contracts or implementation-coupled executable conformance tests** for requirements REG-ARCH-001–024. These are known gaps, **not an absence of all contracts or tests**.
- The historical descriptions of this runtime as `REG-ARCH Lane B` or as implementing named G-doors are **non-normative architectural correspondences**, not demonstrated mappings or conformance claims. This document does not renumber or redefine REG-ARCH gates.

## Conformance evidence and revision separation

- **Last revision pinned by the REG conformance suite:** TENIR-Gov **5.7.0**, commit `47ef67e3efc5f3008896a7d88bebf929fa0a98f0`, declared in [reg-conformance/conformance-target.json](https://github.com/skiredj-prog/reg-conformance/blob/main/conformance-target.json).
- **Current runtime:** 5.7.6. Revisions 5.7.1–5.7.6 have **not been replayed against that pinned conformance suite**. Updating the package version does not move or extend the evidence.
- **Scope of the pinned Raft evidence:** a single-host, localhost/loopback topology with separate ports. Not physical multi-host qualification, Byzantine tolerance, hardened transport, exactly-once effects at an external provider, or independent third-party certification of this runtime and scope.
- An **independent, limited Raft re-run for the earlier 5.6.1 line** is documented in the REG record. It remains valid within its stated environment and does not certify 5.7.6 or the REG-ARCH candidate.

## Declared gaps and decision boundary

The source and governance register tracks the unresolved architectural and conformance gaps:
[REG-ARCH — source, status, precedence and open obligations](https://github.com/skiredj-prog/reg-conformance/blob/main/specs/reg-arch/README.md).

In particular, F-02 (state/transition model), F-03 (incomplete gate contracts), F-05 (model oracles without runtime refinement) and F-13 (unsubstantiated refinement claim) must be addressed and independently checked before an applicable REG-ARCH conformance assertion.

**Source established ≠ ratified ≠ applicable ≠ conformant.** A merged document or successful laboratory test does not change this separation.

This scope document describes the current public runtime. It does not alter `conformance-target.json`, the frozen RFCs, the REG glossary status, or the source code of TENIR-Gov.
