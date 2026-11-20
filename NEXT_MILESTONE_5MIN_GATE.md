# Next Milestone: 5-Minute Gate + Single Dispatcher

**Date:** 2026-11-10  
**Status:** Active  
**Depends on:** Identity lock (merged)

## Non-negotiable properties

### 1. 5-minute developer gate
Synthetic path that operationalizes “knows when not to act” and evidence-bound recovery:

- Authorized synthetic booking whose response is deliberately lost
- UNKNOWN: authorization succeeded; outcome remains unconfirmed
- Retry attempt is blocked (no second dispatch)
- Correlated receipt recovers the outcome
- Exported record is inspectable offline
- Developer can identify the hook to replace with their own tool

Clock starts when preflight passes. Local, no account, no provider credentials.

**Acceptance:** An unfamiliar developer completes the scenario unaided within five minutes after preflight.

### 2. One authoritative dispatcher and one budget owner per mission
During any transitional period, HOLD and TENIR-Gov must not independently:
- reserve funds
- consume approvals
- decide retries

Dual-control races violate holding. This rule applies to the integration slice and any adapter in front of TENIR-Gov.

## Immediate next work
- Implement the synthetic scenario as the public TENIR-Gov “Get started” path.
- Ensure the slice has a single dispatcher and single budget owner.
- Surface original journal + adapter version so recovery remains inspectable.
- Qualify only the paths that actually move (duplicates, late receipts, revocation, crashes around dispatch).

Identity documentation is closed. These two properties are the measure of the next milestone.
