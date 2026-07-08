# TENIR 2C Institutional Hardening — Final Delivery Report

**Version:** 1.0.0-rc2
**Policy:** `tenir-canonical-v1.0.0`
**Date:** 2026-04-19
**Scope:** Unified governance runtime for R4 (partner_a Shadow v4) and R5 (IRON_OMEGA)

---

## Executive Summary

This package delivers the institutional-grade governance runtime requested for the
partner_a/partner_b engagement. It merges R4 and R5 into a single `tenir_governance` package
with:

- one source of truth for all nomenclature (R4, R5, and business layers)
- one policy contract (`PolicyEngine`) that both runtimes depend on explicitly
- one CI-gating validator (`TENIRValidator`) with nine invariant checks
- 55 regression cases covering all CES states, all four operating modes, and
  every membrane decision
- a polymorphic surface state contract for the V5 Two-Glass architecture
- a copy-lint tool enforcing the public-safe lexicon
- a ledger migration tool for legacy labels (SCHIZOPHRENIA → SIGNAL_CONFLICT etc.)

**Test status: 293/293 passed. Validator: 9/9 passed across all three policy
variants (default, partner_a Shadow v4, partner_b Sovereign Pilot).**

---

## Sprint-by-Sprint Delivery

### Sprint 0 — Canonical Nomenclature Registry

`tenir_governance/nomenclature.py` is the single source of truth for every
identifier that appears in code, API responses, UI labels, and documentation.
The three-register language system (Canonical / Operator / Public) is enforced
throughout via `OPERATOR_LABEL` and `PUBLIC_LABEL` dicts on every class.

**Resolved inconsistencies:**

- `CAVE` is locked to `Context · Action · Value · Effect`. The earlier
  `Control · Aim · Veto · Epistemics` variant is in `FORBIDDEN_EXPANSIONS` and
  triggers an immediate BLOCK from the copy-lint CI step.
- R4 `OperatingMode` enum values map cleanly to R5 canonical strings via
  `OperatingModeNames.R4_TO_R5`.

### Sprint 1 — Policy Engine + Validator Merge

`tenir_governance/policy_engine.py` provides `PolicyEngine`, a frozen dataclass
that is the single policy contract. Three factory methods:

- `PolicyEngine.default()` — canonical version `tenir-canonical-v1.0.0`
- `PolicyEngine.um6p_shadow_v4()` — exact R4 threshold equivalence
  (`s_alert=0.90`, `s_block=0.75`, `event_window=8`)
- `PolicyEngine.ocp_sovereign_pilot()` — tighter for industrial lock-in contexts
  (`tau_floor=0.50`, `reaction_budget=3`)

Every membrane decision flows through `PolicyEngine.evaluate_membrane()`. No
local threshold literals are permitted anywhere else in the codebase; this is
enforced by the validator's POL-001 check.

### Sprint 2 — Package Boundaries

`tenir_governance/__init__.py` exposes the public API. External consumers import
from this module only; submodule internals are private. The package installs
cleanly via `pip install -e .` with explicit `packages = ["tenir_governance"]`
in `pyproject.toml`.

### Sprint 3 — Validator as CI Gate

`tenir_governance/validator.py` provides `TENIRValidator` with nine named
checks. It is both a standalone CLI tool and a pytest fixture:

| Check ID | Description |
|----------|-------------|
| POL-001 | Policy contract validates without error |
| POL-002 | Policy version matches expected string |
| POL-003 | TAU floor ordering: τ < s_block ≤ s_alert |
| KER-001 | Kernel math S = K/(P·V+ε) reproducible across 4 fixtures |
| MEM-001 | Membrane decisions monotone non-decreasing as S decreases |
| MEM-002 | SHADOW→intended_block and ENFORCE→block differentiated at TAU breach |
| CES-001 | All five CES states reachable from classifier |
| NOM-001 | CAVE canonical definition is Context/Action/Value/Effect |
| NOM-002 | R4↔R5 mode mapping covers all modes |

The `.github/workflows/tenir-ci.yml` workflow runs five jobs
(policy-gate → regression-corpus → nomenclature-check → r4-compat → corpus-coverage)
with a blocking `all-clear` gate.

### Sprint 4 — Regression Corpus

`tenir_governance/regression_corpus.py` ships 55 `GoldenCase` fixtures across
16 scenario groups:

- `ces_rest`, `ces_metabolizing`, `ces_tension`, `ces_schizophrenia`, `ces_collapse`
- `tau_breach`, `option_space_collapse`, `shadow_mode`, `kernel_math`
- `ocp_h2` — partner_b-H2-2026-001 industrial scenarios
- `partner_a` — partner_a partnership scenarios
- `nsl_mapping` — NSL intent → kernel parameter mapping
- `r4_compat`, `stress`, `edge_cases`
- `holding_first`, `enforce_ceremony`

Each case runs four assertions (membrane decision, alert flag, intended_block
flag, CES state) against the canonical policy — 220 parametrized assertions
plus the 9 inventory and 11 validator tests.

### Sprint 5 — SDK/Governance Branch

`tenir_governance/sdk.py` provides `TENIRGovernanceClient` as the public
interface both R4 (`adjudicate()`) and R5 (`adjudicate_from_nsl()`) wire
through. `GovernanceResult.to_business_payload()` emits the UI-ready dict;
`to_r5_frame()` emits the WebSocket frame payload.

Policy validation runs on client init — no governance decisions can be produced
with an invalid policy.

### Sprint 6 — Plain-Language Hardening

Three `rename-now` directives executed per `TENIR_public_safe_lexicon.csv`:

| Deprecated | Canonical |
|------------|-----------|
| `SCHIZOPHRENIA` | `SIGNAL_CONFLICT` |
| `SCHIZOPHRENIA_ALERT` | `SIGNAL_CONFLICT_ALERT` |
| `WHALE_RESONANCE` | `DEEP_PATTERN_SIGNAL` |

Backward-compat aliases preserved in `CESStateNames.LEGACY_ALIASES` and
`WSFrameTypes.LEGACY_ALIASES`. The `normalize_ces_state()` and
`normalize_ws_frame_type()` helpers let existing ledgers re-read cleanly.

`PUBLIC_BANNED_TERMS` and `PUBLIC_TRANSLATE_ON_FIRST_USE` sets are exposed for
the copy-lint tool.

### Sprint 7 — CES Doctrine Reconciliation

This was the explicit next-sprint item from the previous hardening report
("reconcile CES taxonomy between corpus, doctrine, and runtime"). The
classifier now uses pressure and velocity in addition to S and option_space.

**Final semantics (doctrine-locked):**

```
COLLAPSE          S < tau_floor, OR
                  S ≤ s_block_floor AND option_space ≤ 0.30

SIGNAL_CONFLICT   S < s_alert_floor AND option_space ≥ 0.55
                  (contradictory but viable)

TENSION           s_block_floor ≤ S < s_alert_floor, OR
                  S ≤ s_block_floor AND 0.30 < option_space < 0.55

METABOLIZING      S ≥ s_alert_floor AND activity ≥ 1.00
                  AND S < 2.5 × s_alert_floor (not over-resourced)

REST              S ≥ s_alert_floor (default above alert floor)
```

14 corpus fixtures had their labels corrected (they were authored before the
activity-based classifier existed). Signature is backward-compatible —
`pressure` and `velocity` are keyword-only with `None` defaults.

### Sprint 8 — Expanded Posture Family (V5 Doctrine)

Six new postures added per Master Doctrine v5:
`ROAM`, `LEARN`, `TRANSMIT`, `REST`, `SLEEP`, `STOP`.

Split into `ACTIVE_ADJUDICATION` (the four R4/R5 operational modes) and
`SUSPENDED_POSTURES` (REST, SLEEP, STOP).

`AdmissibilityClass` added for V5 symptomatic layer:
`OBSERVED` (1.00), `DERIVED` (0.95), `INFERRED` (0.50), `HELD` (0.00),
`FORBIDDEN` (0.00).

### Sprint 9 — Copy-Lint CLI Tool

`tenir_governance/copy_lint.py` scans `.md`, `.html`, `.txt` files for:

1. Banned terms from `PUBLIC_BANNED_TERMS` (blocks in exposure=public,
   warns in exposure=operator)
2. Branded terms from `PUBLIC_TRANSLATE_ON_FIRST_USE` that appear without
   translation on first use in public content
3. Deprecated CAVE expansion (always blocks)

Exposure class detected from front-matter tag:
```yaml
---
exposure: public
---
```

Or HTML comment: `<!-- exposure: public -->`.

CLI: `python -m tenir_governance.copy_lint path/to/docs/`

### Sprint 10 — Polymorphic Surface State Contract

`tenir_governance/polymorphic_surface.py` implements the V5 Persona/Workflow
architecture. Four canonical surface states:

| State | Doctrine term | UI meaning |
|-------|--------------|-----------|
| `AMBIENT` | Cockpit | Monitoring, biological view |
| `ANTICIPATION` | Chambre d'Anticipation | Ghost trajectory simulation |
| `ADJUDICATION` | Crisis override | High-contrast, cryptographic demand |
| `FORENSIC` | Console | Raw ledger, NSL grammar, Merkle |

`recommend_surface_state(SurfaceContext)` returns the UI state given the
current governance runtime condition. Precedence:
`ADJUDICATION (crisis) > FORENSIC (audit) > ANTICIPATION (sim) > AMBIENT (default)`.

**Two-Glass bridge:**

| Direction | Who may cross |
|-----------|---------------|
| `iron_to_glass` | All IRON personas |
| `glass_to_iron` | Executive only (requires ceremony) |

`requires_ceremony(from, to)` returns True only for
`ADJUDICATION → AMBIENT` (resolving a crisis back to monitoring is the one
transition that implicitly asserts closure, so it demands operator oath).

### Sprint 11 — Ledger Migration Tool

`tenir_governance/ledger_migrate.py` rewrites existing JSONL ledgers to apply
the rename-now directives without invalidating forensic replay.

- Creates `<ledger>.pre-migration.jsonl` as untouched backup
- Writes new ledger rooted at a migration-genesis hash that carries the
  original chain's final hash for forensic continuity
- Chain integrity verifiable via `verify_migrated_ledger()` or
  `--verify` CLI flag

---

## Test Matrix

| Test Suite | Count | Status |
|-----------|-------|--------|
| Validator CI gate | 11 | PASS |
| Policy engine contract | 10 | PASS |
| Membrane core invariants | 6 | PASS |
| Nomenclature alignment | 4 | PASS |
| Corpus inventory | 9 | PASS |
| Golden corpus (55 cases × 4 assertions) | 220 | PASS |
| Surface state recommender (Sprint 10) | 8 | PASS |
| Surface transitions (Sprint 10) | 4 | PASS |
| Surface access control (Sprint 10) | 4 | PASS |
| Two-Glass bridge (Sprint 10) | 2 | PASS |
| Opacity map (Sprint 10) | 3 | PASS |
| Copy-lint (Sprint 9) | 5 | PASS |
| Ledger migration (Sprint 11) | 5 | PASS |
| **TOTAL** | **293** | **PASS** |

Validator standalone run: `9/9 checks passed` across default, partner_a, and partner_b
policy variants. Policy fingerprint (default): `d083e0b82a16c04d`.

---

## Known Items (Out of Scope for This Hardening Pass)

These were explicitly flagged in the V4 UAT audit and remain open in the
R4/R5 runtime code. They are not regressions from this hardening — they
predate it. The governance package itself does not suffer from any of them.

| ID | Area | Status |
|----|------|--------|
| G1 | Nonce restoration incomplete across sovereign paths | R4 runtime |
| G2 | Trajectory derivatives wrong on replay | R4 runtime |
| G3 | Override/dissent signatures not in ledger entry | R4 runtime |
| G4 | Replay can inflate event count | R4 runtime |
| G5 | LLM runtime exceptions drop telemetry | R5 runtime |
| G6 | Testament generation from unverified ledger | R4 runtime |

Wiring R4's `TenirMonitor` and R5's `r5_server.py` to import from
`tenir_governance` (replacing their local policy objects) closes most of
these by construction — the `PolicyEngine.evaluate_membrane()` call already
carries `policy_version` into every ledger entry, which is what G6 needs.

---

## File Inventory

```
tenir_hardened/
├── pyproject.toml
├── tenir_governance/
│   ├── __init__.py              (public API, 1.0.0-rc2)
│   ├── nomenclature.py          (Sprint 0+6+8 — canonical vocabulary)
│   ├── policy_engine.py         (Sprint 1+7 — policy + membrane + CES)
│   ├── validator.py             (Sprint 1+3 — 9-check CI gate)
│   ├── regression_corpus.py     (Sprint 4 — 55 golden cases)
│   ├── sdk.py                   (Sprint 5 — public client)
│   ├── polymorphic_surface.py   (Sprint 10 — V5 surface contract)
│   ├── copy_lint.py             (Sprint 9 — public-safe enforcement)
│   └── ledger_migrate.py        (Sprint 11 — legacy label migration)
├── tests/
│   ├── test_regression_corpus.py  (261 tests — Sprints 0-5)
│   └── test_sprints_9_10_11.py    (32 tests — Sprints 9, 10, 11)
├── .github/workflows/tenir-ci.yml (5-job blocking CI gate)
└── HARDENING_REPORT_v1.0.0-rc2.md (this document)
```

---

## How to Use This Package

### Install

```bash
cd tenir_hardened
pip install -e .
```

### Run validator

```bash
tenir-validate --policy default
tenir-validate --policy partner_a
tenir-validate --policy partner_b --json
```

### Run full regression suite

```bash
pytest tests/ -v
```

### Use the SDK

```python
from tenir_governance import TENIRGovernanceClient, GovernanceEvent

client = TENIRGovernanceClient.for_um6p_shadow()
result = client.adjudicate(GovernanceEvent(
    pressure=0.8, velocity=0.7, capacity=1.0, option_space=0.5
))
print(result.to_business_payload())
```

### Lint public copy

```bash
python -m tenir_governance.copy_lint docs/
python -m tenir_governance.copy_lint --exposure public homepage.html
```

### Migrate legacy ledger

```bash
# Dry run first
python -m tenir_governance.ledger_migrate --dry-run audit/ledger.jsonl

# Apply
python -m tenir_governance.ledger_migrate audit/ledger.jsonl

# Verify
python -m tenir_governance.ledger_migrate --verify audit/ledger.jsonl
```

---

## Canonical Closing

Per TENIR Master Doctrine Note v5 Regroup:

> The interface is not the system; it is a lawful projection of a
> higher-dimensional invariant under contextual constraint.

This package preserves that doctrine by ensuring every surface expression —
R4 enum, R5 string, business label, public copy, CI gate — flows from the
same registered canonical term. The nomenclature registry is that invariant.
The polymorphic surface contract is how it lawfully projects.

`TAU` is identity. `TENIR` is fidelity to identity under pressure. Both now
have one authoritative codebase.
