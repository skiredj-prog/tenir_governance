# Decision Record: HOLD / TENIR-Gov Identity Lock

**Date:** 2026-11-10  
**Status:** Locked  
**Motive:** De-disambiguate HOLD vs TENIR-Gov. Casual interchange in analysis and reports is creating operational and evidentiary risk.

## Naming Rules (post-consolidation)

| Name | Meaning | Is NOT | Visibility |
|------|---------|--------|------------|
| **TENIR-Gov** | The single maintained product and runtime. Incorporates selected HOLD capabilities once migrated and qualified. | A backend behind HOLD. Interchangeable with HOLD. | Public product name |
| **HOLD** | Historical hackathon prototype and its frozen experiments (e1-a6-frozen and related). Reference implementation / testbed only. | The ongoing product. A live backend alternative. | Historical / internal |
| **REG** | Specification and qualification framework. Versioned profiles, invariants, deployment assumptions, conformance tests. | A product or runtime. | Technical / evidence |
| **TENIR** | Overall architecture and research programme. | The executable product. | Research / architecture |

## Claims Ownership Rule

Every current claim must identify:
- the implementation,
- the version,
- the tested deployment scope,
- the supporting test or evidence.

HOLD’s benchmarks remain evidence about the frozen HOLD implementation only. Migrated functionality earns evidence against the integrated TENIR-Gov release.

## Correction Rule

- Historical reports retain original provenance.
- Current reports, READMEs, decks, knowledge-graph nodes, and external materials must be corrected wherever “HOLD” and “TENIR-Gov” have been used interchangeably.
- A reviewer must always be able to determine exactly which implementation produced a result.

## Sequence (locked)

1. Fix identity and claims ownership  
2. Map and migrate capabilities  
3. Ship the unified developer experience  
4. Qualify broader deployment  

Runtime consolidation remains incremental and evidence-gated. Product identity does not.

## One-authoritative-dispatcher rule

During any transitional period, each mission has one authoritative dispatcher and one budget owner. HOLD and TENIR-Gov do not independently reserve funds, consume approvals, or decide retries.

## Note on HOLD as state name

In this repository, **HOLD** also appears as an operation state (e.g. `OperationState.HOLD`, G-07 HOLD). This is distinct from the historical product named HOLD. Current materials should make the distinction explicit where the two could be confused.

---

*This record locks identity. It does not constitute runtime compatibility evidence.*
