# Codex handoff — EP-01 / EP-02 (2026-10-24)

## Verified checkpoint
- PR #35 (draft, base main): `test/ep-01-epistemic-authority-boundary` — canonical epistemic mapping, documentary standing/effective-time policy, Ed25519 claim preflight; not wired to governor.
- PR #36 (draft, base PR #35): `design/ep02-trust-versioned-commit`, HEAD `08df2ce83796690623748505db974f23912a7462`.
- GitHub Actions run **40059702985**: completed **success**; all 8 jobs green including the targeted `tests/test_ep02_local_cas.py`.
- No merge, release, or governor modification authorized. Preserve A2/A3 boundaries.

## What exists (prototype, not institutional assurance)
- `experimental/ep02_signed_trust.py`: signed snapshot, bound issuer keys, signature preflight; issuer keys covered by root signature in v2 path.
- `experimental/ep02_binding.py`: immutable finding binding, fingerprint comparisons.
- `experimental/ep02_rotation.py`: detects same-revision equivocation and rollback relative to supplied checkpoint.
- `experimental/ep02_checkpoint_store.py`: POSIX `fcntl` local lock, temp+replace+fsync checkpoint; `bootstrap=True` is explicitly requested by caller.
- `experimental/ep02_external_anchor.py` and `ep02_authenticated_anchor.py`: pure comparison and Ed25519 challenge/response. Neither is a server nor external CAS.
- `experimental/ep02_local_cas.py`: local single-host CAS; returns APPLIED/UNCHANGED/CONFLICT/NOT_INITIALIZED; raises on write errors (outcome ambiguous).
- CI characterizations include key substitution, revocation, time-effect, cross-process collision, local rollback, restored-file vulnerability, signed remote observations, and local CAS.

## Priority execution for Codex
1. **Audit**, without changing production runtime. Confirm that same-host CAS is actually linearizable under local POSIX assumptions; include two-process race and crash at before-fsync, after-fsync, after-replace, after-directory-fsync. Avoid asserting crash safety solely from mock fsync.
2. Add durable **request ID/idempotency** and `UNKNOWN` recovery semantics to the *experimental local anchor* only. Ensure retry cannot advance twice or reinterpret a conflicting acknowledgment. Use explicit fencing/reconciliation with persisted evidence.
3. Design & characterize a truly **external** monotone CAS authority (e.g., Raft/DB compare-and-set) on a separate opt-in interface, with failure semantics for network partition and competing hosts. Authenticated READ does not guarantee atomic WRITE.
4. Lock down **bootstrap/reinitialization**, stable root trust, key rotation and trusted high-water storage. Restoring a checkpoint and its locally trusted state together is not detectable today.
5. Identify and test potentially unsafe paths: snapshot v1 legacy helper vs v2 signed key binding; empty/chosen caller challenges; replayed signed messages; malformed issuer keys; file symlinks and storage permissions; digest normalization; input type hardening.
6. Map to A2/A3 only **after** EP-02 proof obligations are clear. A3 `CONFIRMED` != budget-settled; never treat preflight `ADMISSIBLE` as commit authorization.

## Acceptance criteria
- Reproduce full current CI first; add *failing tests before fixes*, then targeted and full gates.
- Separately label: (i) single-process, (ii) same-host multiprocess, (iii) cross-host, (iv) institutional/production claims.
- Acknowledge all ambiguous failure outcomes; do not auto-compensate irreversible external effects.
- Keep PR #35/#36 drafts; use a new child PR for substantial work or explicitly update #36 with traceable commits.
- No merge, tag, release or production integration without explicit user approval.
- Update PR descriptions / docs with tested properties, assumptions and unresolved proof obligations.

## Verification links
- https://github.com/skiredj-prog/tenir_governance/pull/35
- https://github.com/skiredj-prog/tenir_governance/pull/36
- https://github.com/skiredj-prog/tenir_governance/actions/runs/40059702985
