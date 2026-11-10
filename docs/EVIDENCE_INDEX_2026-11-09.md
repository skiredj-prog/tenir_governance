# TENIR-Gov — Dated Evidence Index (2026-11-09)

**Purpose:** single place that distinguishes published release evidence from local laboratory artifacts. Prevents reconstructed bundles from being read as production qualification.

## 1. Published release (v5.7.0)

| Item | Value | Classification |
|---|---|---|
| Tag / SHA | `v5.7.0` / `47ef67e3efc5f3008896a7d88bebf929fa0a98f0` | Published |
| PyPI | `tenir-governance==5.7.0` | Published package |
| Deployment boundary | Single-host only; verified production manifest required; FM-20 open; multi-host Raft production blocked | Declared limit |
| Claims | No C3, no exactly-once external effects, no Byzantine tolerance, CI ≠ certification | Explicit non-claims |
| Evidence matrix | See `V5.7.0_BACKEND_INVARIANT_MATRIX.md` (DD-575-03 draft) | Scoped to published tag |
| Supporting docs | `CHANGELOG.md`, `docs/DEPLOYMENT_BOUNDARIES_V5.7.0.md`, `docs/POST_RELEASE_V5.7.0_DOCUMENTARY_STATUS.md`, `docs/V5.7.0_INTERNAL_DD_CLOSURE_2026-10-25.md` | Documentary |

## 2. Local laboratory evidence (not in published tag)

| Artifact | SHA / location | Classification | What it does **not** cover |
|---|---|---|---|
| Reconstructed CEB-01 | Local `.w4-evidence/ceb01/` on SHA `1a8dceaac2e47f0ffa93991e76cb95628d905886` | RECONSTRUCTED / LAB_ONLY / NON_CERTIFYING | Does not exercise B1.1 dual-grant/quarantine path; public test keys; no independent topology attestation; `provider.db` not bundled |
| B1.1 laboratory slices (dual-grant, quarantine fence) | Integrated head `1a8dceaac2e47f0ffa93991e76cb95628d905886` (stacked PRs, draft) | Laboratory / synthetic effects only | Real provider, privileged writers, multi-host, production qualification |
| B2 cold-restart anti-rollback slice | Draft PR #128 on same baseline | Scoped lab / NOT a B2 closure (`B2_VERIFIED = HOLD`) | Live external monotone authority, compromised-writer case, real-provider reconciliation, prod-candidate topology |

## 3. Open / HOLD items

- FM-20 (pickle transport) — deferred to architectural work targeted at v5.8.0.
- External monotone authority and protected key custody (B2).
- Real-provider evidence and reconciliation.
- Multi-host production and C3.
- Independent external review of the published 5.7.0 boundary (DD-575-07).
- PyPI hash verification and Sigstore attestation checks (DD-575-01 / DD-575-02) — still open, access-dependent.

## 4. Usage rule

Quote only evidence that matches the layer. A local reconstructed CEB-01 or laboratory PR does not upgrade the published v5.7.0 claims. A green CI run on a laboratory head does not constitute a REG conformity decision or release authorization.

This index is documentary. It does not authorize a tag, merge, or production promotion.
