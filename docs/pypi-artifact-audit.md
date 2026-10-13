# PyPI Artifact Audit Report — `tenir-governance==5.3.0`

**Date:** 2026-10-13  
**Auditor:** WRITER Agent (automated)  
**Scope:** Published PyPI distribution vs. GitHub tag `6237f39` vs. installed behavior  
**Package:** `tenir-governance` 5.3.0  
**PyPI URL:** https://pypi.org/project/tenir-governance/5.3.0/  

---

## 1. Artifact Identity

| Property | Value |
|---|---|
| Wheel filename | `tenir_governance-5.3.0-py3-none-any.whl` |
| Wheel size | 119,517 bytes (119 KB) |
| Wheel SHA-256 | `f5b54efd4a1ed1dfaa8aa042259f66329c5357571e29a5ffdd487351b841fb7c` |
| sdist filename | `tenir_governance-5.3.0.tar.gz` |
| sdist size | 135,575 bytes (135 KB) |
| sdist SHA-256 | `22f32d1ecd6cd166250b6111b9b7ddd59b0cd4ad0440f7d8a6e3f43dc7c42bb0` |
| Upload time | 2026-10-12T23:49:48Z |
| Python requirement | `>=3.10` |
| License | Apache-2.0 (SPDX expression) |
| Author | Abdelaziz Skiredj <skiredj@gmail.com> |

**Hash verification:** Both SHA-256 hashes match the PyPI JSON metadata exactly. ✅

**Build provenance:** The published package was built from commit `8d44dc8` (the PyPI packaging commit), NOT from tag `v5.3.0` at `6237f39`. This is expected — the packaging commit came after the v5.3.0 merge and includes HOLD lifecycle + packaging fixes. The `v5.3.0` tag represents the initial two-kernel composition merge; the published package includes HOLD lifecycle (committed at `3eeeb51`) but NOT provider integration (committed at `ddd50d6`).

---

## 2. Package Completeness

### Wheel contents (41 Python/JSON files)

All core modules are present:

| Module | Present | Notes |
|---|---|---|
| `tenir_governance/__init__.py` | ✅ | Exports DualKernelCompositionClient, HoldManager, etc. |
| `tenir_governance/sdk.py` | ✅ | 24,211 bytes |
| `tenir_governance/composition/governor.py` | ✅ | 46,146 bytes |
| `tenir_governance/composition/hold_strategy.py` | ✅ | 16,151 bytes — HOLD lifecycle |
| `tenir_governance/composition/lattice.py` | ✅ | Decision lattice + hash-chained ledger |
| `tenir_governance/composition/schema_adapter.py` | ✅ | Schema 1.2 conformance |
| `tenir_governance/membrane/__init__.py` | ✅ | TransitionMembrane, QUARANTINE_LOCK |
| `tenir_governance/operations/registry.py` | ✅ | 23,071 bytes — DurableOperationRegistry |
| `tenir_governance/operations/hold.py` | ✅ | 16,832 bytes — HoldManager |
| `tenir_governance/operations/budget.py` | ✅ | BudgetCoordinator |
| `tenir_governance/objects/*.py` | ✅ | Agent, Passage, Resource, etc. |
| `tenir_kernel/*.py` | ✅ | Core kernel, geometry, schema |
| `tenir_kernel/schemas/tenir_1_2.schema.json` | ✅ | Canonical schema |
| `tenir_governance/operations/provider.py` | ❌ | **NOT in published package** — committed at `ddd50d6`, after packaging |

### Missing from published wheel

| File | Reason | Impact |
|---|---|---|
| `tenir_governance/operations/provider.py` | Committed at `ddd50d6`, after the `8d44dc8` packaging commit | Provider integration (MockProvider, StripeProvider, dispatch_and_execute) is not available to PyPI users |

### Tests

The sdist includes 12 test files (test_composition.py, test_hold_lifecycle.py, etc.) but NOT test_provider.py (same reason — committed after packaging).

### Entry points

All 3 CLI entry points are present and functional:

| Entry point | Module | Status |
|---|---|---|
| `tenir-kernel` | `tenir_kernel.cli:main` | ✅ Works |
| `tenir-sdk` | `tenir_governance.sdk:main` | ✅ Works |
| `tenir-validate` | `tenir_governance.validator:main` | ✅ Works |

### Dependencies

| Dependency | Version constraint | Type |
|---|---|---|
| `jsonschema` | `>=4.22,<5` | Core (required) |
| `fastapi`, `pydantic`, `uvicorn` | Various | `[api]` extra |
| `neo4j`, `networkx`, `httpx`, `websockets` | Various | `[r5]` extra |

---

## 3. Installed Defaults — The In-Memory Registry Concern

### Finding: CONFIRMED

**The reviewer's highest-priority concern is confirmed in the published package.**

`DualKernelCompositionClient()` defaults to `store_path=None`, which creates an in-memory `DurableOperationRegistry`. This means:

- **Idempotency claims are lost on process restart.**
- A crashed and restarted process will re-adjudicate operations that were already CREATED or RESERVED, potentially leading to duplicate dispatch.
- The `require_persistence=True` guard exists but is opt-in (`False` by default).

### Test Evidence

**TEST 5 — No persistence (in-memory default):**
```
State after adjudicate: reserved
Re-adjudication after restart: reserved
Composed decision: FLAG
>>> CRITICAL: Same operation re-adjudicated! Idempotency lost!
```

**TEST 4 — With store_path (persistence enabled):**
```
State after adjudicate: reserved
Store file exists: True
Re-adjudication after restart: reserved
Composed decision: HARD_VETO  (blocked — idempotency preserved)
```

**TEST 6 — require_persistence guard:**
```
require_persistence=True without store: ValueError raised (GOOD)
Message: "require_persistence=True requires a store_path..."
```

### Assessment

- The `require_persistence` parameter is a correct safety mechanism, but it is opt-in.
- The docstring clearly documents the in-memory default and its consequences.
- However, a user reading only the README quickstart (`DualKernelCompositionClient()`) would not see this warning and could assume restart-safe idempotency.

### Fix Applied

Added a `warnings.warn()` call when `store_path` is None and `require_persistence` is False, so users see a visible warning at initialization time:

```
DualKernelCompositionClient initialized without store_path.
The operation registry is in-memory only — idempotency claims
will be lost on restart, allowing duplicate adjudication.
Pass store_path or require_persistence=True for production use.
```

---

## 4. Release Integrity

| Check | Result |
|---|---|
| Wheel SHA-256 matches PyPI JSON | ✅ `f5b54efd...` |
| sdist SHA-256 matches PyPI JSON | ✅ `22f32d1e...` |
| Wheel format | `py3-none-any` (pure Python, platform-independent) |
| Metadata version | 2.4 |
| License expression | Apache-2.0 (SPDX) |
| License file | `LICENSE` present in dist-info |
| Project URLs | All 5 present (Homepage, Repository, Documentation, Bug Tracker, DOI) |
| Signed | ❌ No signature (`has_sig: false`) |

**Note:** No GPG or Sigstore signatures. This is common for PyPI packages but means integrity depends on PyPI's TLS and the package hash.

---

## 5. Reproducibility — Targeted Tests from Clean Install

All tests run in a fresh virtualenv with `pip install tenir-governance==5.3.0` (Python 3.14):

### TEST 1: Basic adjudication ✅
- Two-kernel composition produces FLAG decision, operation enters RESERVED state.
- Result dict has all expected keys (16 fields including ledger_entries, ledger_valid).

### TEST 2: HOLD lifecycle ✅
- `enter_hold()` transitions operation to HOLD state.
- `resolve_hold()` on a non-HOLD operation correctly returns `hold_resolved: False` with reason.
- State machine respects current state before transitioning.

### TEST 3: Duplicate prevention (in-memory) ✅
- First adjudication: RESERVED.
- Second adjudication of same passage: HARD_VETO (blocked by CREATED blocks fresh adjudication).
- In-memory idempotency works within a single process lifetime.

### TEST 4: Persistence with store_path ✅
- Store file created on disk.
- After simulated restart (new client, same store), re-adjudication is blocked (HARD_VETO).
- Durable idempotency works across restarts when store_path is configured.

### TEST 5: No persistence (in-memory default) ⚠️ CRITICAL
- After simulated restart, same operation is re-adjudicated successfully.
- Idempotency is lost without persistent storage.

### TEST 6: require_persistence guard ✅
- `ValueError` raised when `require_persistence=True` without `store_path`.
- Clear error message explaining the consequence.

### TEST 7: Corruption recovery ✅
- Corrupted store file raises `RuntimeError` with clear message: "Registry store is corrupted — refuse to register new operations."
- Fail-closed behavior: does not silently proceed with corrupted data.

### TEST 8: Provider module absence ✅ (expected)
- `tenir_governance.operations.provider` not importable from published package.
- This is expected — provider integration was committed after the packaging commit.

### TEST 9: CLI entry points ✅
- All 3 entry points (`tenir-kernel`, `tenir-sdk`, `tenir-validate`) work correctly.

---

## 6. Summary of Findings

### Confirmed Issues

| # | Issue | Severity | Status |
|---|---|---|---|
| 1 | **In-memory registry default** — users can assume restart-safe idempotency without getting it | High | **Fixed** — runtime warning added |
| 2 | **Provider module missing** from published package | Medium | Expected — needs v5.3.1 release to include |
| 3 | **No package signatures** (GPG/Sigstore) | Low | Recommended for future releases |
| 4 | **No Trusted Publishing** (OIDC from GitHub Actions) | Low | Recommended for maintainer workflow |

### Non-Issues (Working Correctly)

| Check | Result |
|---|---|
| Package completeness (core modules) | ✅ All present |
| Entry points | ✅ All 3 functional |
| Hash verification | ✅ Match PyPI metadata |
| Fail-closed composition | ✅ HOLD on errors, never PASS |
| HOLD lifecycle | ✅ Working as designed |
| Duplicate prevention (in-process) | ✅ CREATED blocks fresh adjudication |
| Persistence with store_path | ✅ Restart-safe |
| Corruption recovery | ✅ Fail-closed with clear error |
| require_persistence guard | ✅ Correct ValueError |
| Schema conformance | ✅ conform_to_schema available |

---

## 7. Recommendations

1. **Publish v5.3.1** including the provider integration (`ddd50d6`) and the in-memory warning fix. This makes the PyPI package match the GitHub HEAD.
2. **Enable Trusted Publishing** (GitHub Actions → PyPI OIDC) to eliminate long-lived API tokens.
3. **Consider making persistence the default** in a future major version, or at minimum adding a prominent warning in the README quickstart.
4. **Add Sigstore signatures** for high-assurance distribution.
5. **Pin the build to the release tag** so published artifacts always correspond to a tagged commit.
