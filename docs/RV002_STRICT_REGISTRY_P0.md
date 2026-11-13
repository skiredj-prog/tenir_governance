# RV-002 P0 — Strict shared-registry mandate fence (candidate v2)

Status: **DRAFT / UNQUALIFIED / NO MERGE / NO RELEASE**.

## Preserved evidence

- Original finding (PR #136 at `f8792518b8c0e284a3528d62484e04f3ebc9bba8`):
  a guarded Governor refused an operation revoked before release, but a
  second Governor without `mandate_authority` shared the same JSON registry
  and dispatched it. This is **B1 FAIL**, not missing coverage.
- Runner C2 persisted DISPATCHING without committed authority events and
  stopped before provider.execute. Restart/replay still unqualified.
- L1 three-worker execution observed only one provider call but lacks an
  independently checked complete linearizability history.

## Proposed correction

1. Configuring a Governor with a SQLite mandate authority **installs a durable
   strict-mode marker on the shared registry** before any admission. The marker
   is bound to one canonical authority-store path and cannot be disabled via
   a second Governor instance.
2. The registry persists `mandate_guard_required=true` on protected records.
   The strict marker also applies to pre-existing records and future
   registrations. A missing/corrupt strict marker after initialization fails
   closed.
3. Every native `RESERVED -> DISPATCHING` transition, including a direct
   registry call or a second unguarded Governor, must supply a **live**
   `MandateClaim` from the configured SQLite authority. Registry checks the
   active SQLite transaction and independently reads current mandate,
   identity, epoch and immutable payload binding. A no-op write retains
   serialized authority-row ownership before persisting DISPATCHING.
4. `resume_existing()` no longer calls an operation protected by strict mode
   "safe to dispatch": it returns `revalidate_mandate` rather than `dispatch`.
5. Default legacy registries that never enrolled in strict mode retain
   backward compatibility. A **shared strict registry cannot be downgraded**
   by an unguarded constructor.

**Scope:** cooperative native code on a single host sharing the same registry
and authority SQLite database. This is NOT a mechanism to protect against
arbitrary malicious Python execution with unrestricted filesystem access, nor
is it a cross-host distributed consensus or authenticated issuer.

**Remaining distributed caveat:** authority SQLite and JSON registry are
different stores. A process crash can persist DISPATCHING while rolling back
the authority transaction. This is an ambiguous claim requiring reconciliation,
not evidence of a provider effect and not a retryable FAILED state.

A claim linearized before revocation ACK is still allowed to finish; the
receiving system must separately enforce target-side commit revocation (C).

## Regression tests included

```bash
python -m pytest -q tests/test_rv002_mandate_authority.py tests/test_rv002_native_governor.py
python -m pytest -q tests/
```

These include:
- A1 valid → one native mock provider call; A2 revocation first → zero;
  A3 substitutions/unbound → refusal.
- B1 unguarded Governor opened before or after strict activation, same durable
  registry → no provider call and no durable DISPATCHING claim.
- Direct registry transition and a fabricated dataclass `MandateClaim`
  → no authority bypass.
- Durable strict-mark deletion → fail-closed.
- `resume_existing` → revalidate_mandate advice.
- C2 **simulated SystemExit** after registry persistence and before authority
  transaction commit → DISPATCHING record, no authority dispatch_claim,
  no provider call; restart attempts must reconcile and not replay.

Unit tests are not an independent SIGKILL campaign.

## Mandatory independent evidence gates

- **B1 — MIXED-INSTANCE:** repeat on this frozen candidate with two separate
  OS processes sharing one durable registry; one guarded + one unguarded.
  Perform both start orders, preserve raw records, provider counters,
  exact SHA. A single unguarded provider call → FAIL.
- **C2 — CRASH:** SIGKILL a worker after registry's durable DISPATCHING
  transition but before SQLite authority COMMIT, then restart a fresh process
  and explicitly attempt `resume_existing` and native dispatch. Report the
  persistent registry state, authority events, provider counter and precise
  fault-injection boundary. Do not infer zero effects from missing traces.
- **L1 — SERIALIZATION:** retain INCONCLUSIVE until external multiprocess
  invocation/ACK/claim histories can be independently linearized.
- **SOURCE-WHEEL:** compare wheel contents byte-for-byte for each
  `tenir_governance` source member or produce a documented equivalence
  check. A successful wheel build alone is not source equivalence.
- Verify all applicable CI suites on the exact commit SHA and preserve
  their run identifiers.

Use only laboratory operations; no live payments, secret material, or
production side effects. No changes to frozen source while testing.

**Integration Lock: HOLD · E2E: HOLD · Production: HOLD.**
