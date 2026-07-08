# TENIR-Gov — Governance Middleware for AI-Enabled Operational Systems

## Scope of this Release (R5.0.0)
> **Note:** This repository implements the foundational deterministic governance spine (Canon 1.0). Advanced ontological structures from the broader TENIR Enacted 2.0 framework (e.g., "The Cave", Epistemic Sovereignty) represent the theoretical roadmap for future major releases. This release focuses strictly on the core admissibility formula and Merkle-backed auditability.



[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![CI](https://github.com/skiredj-prog/tenir-gov/actions/workflows/tenir-ci.yml/badge.svg)](https://github.com/skiredj-prog/tenir-gov/actions/workflows/tenir-ci.yml)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.10823456.svg)](https://doi.org/10.5281/zenodo.10823456)

**TENIR-Gov** is an open-source governance middleware that sits between decision-producing AI agents and downstream execution services. It enforces explicit policies, validates operational intents through a neuro-symbolic grammar layer, and persists every governance decision in a cryptographically verifiable Merkle-based audit ledger — independently of any underlying AI model.

> *"The interface is not the system; it is a lawful projection of a higher-dimensional invariant under contextual constraint."*
> — TENIR Master Doctrine v5

---

## Two-Tier Architecture

TENIR-Gov ships as two complementary tiers that share the same admissibility formula:

```
┌──────────────────────────────────────────────────────────────┐
│                    TIER 2 — Full Middleware                   │
│  tenir_governance/   (549 tests · 96% coverage)              │
│  Neo4j policy graph · NSL parser · admin plane · SDK         │
│  CES state machine · polymorphic surface · ledger migration  │
├──────────────────────────────────────────────────────────────┤
│                    TIER 1 — Governance Kernel                 │
│  tenir-kernel/       (8 targeted kernel tests)               │
│  S = K / (P × V + ε)  ·  YAML policy  ·  Merkle ledger     │
│  FastAPI endpoint  ·  independently deployable               │
└──────────────────────────────────────────────────────────────┘
```

The kernel tier demonstrates that the core admissibility logic is separable from the institutional surface. This is a design invariant, not an accident: governance policy (YAML) is decoupled from governance logic (code), and the formula is the same in both tiers.

---

## Overview

Modern deployments of autonomous AI agents lack a formal, model-independent governance layer. TENIR-Gov addresses this by implementing governance as **infrastructure** rather than as a property of any single model.

```
Incoming Intent
      │
      ▼
┌─────────────────────────────────────────┐
│            Governance Spine             │
│  Polymorphic Surface → Nomenclature →   │
│  Policy Engine → Membrane Verdict       │
│   (PASS / FLAG / BLOCK)                 │
└─────────┬──────────────┬────────────────┘
          │              │
          ▼              ▼
    Audit Ledger    Execution Gateway
   (Merkle chain)  (downstream APIs)
```

### Key capabilities

| Capability | Implementation |
|:---|:---|
| **Governance Kernel** | `tenir-kernel/` — minimal 6-file deployable (formula + YAML + Merkle ledger + FastAPI) |
| **Neuro-Symbolic Validation** | LALR(1) grammar parser + optional LLM fine-tuning via QLoRA |
| **Deterministic Policy Engine** | Frozen dataclass contract; single source of truth for all thresholds |
| **Graph-Based Policy Store** | Neo4j 5.x with full ontology (CP-Net structure) |
| **Cryptographic Audit Ledger** | Merkle epoch trees + hash chain; inclusion proofs at O(log n) |
| **Administrative Governance Plane** | Policy lifecycle, approval workflows, asymmetric-key change-control |
| **Polymorphic Surface Contract** | Four UI states (AMBIENT / ANTICIPATION / ADJUDICATION / FORENSIC) |
| **Legacy Migration Tooling** | `ledger_migrate.py` rewrites JSONL ledgers preserving forensic continuity |

---

## Architecture

### Release R5.0.0 (IRON OMEGA R5)

```
tenir-gov/
├── tenir-kernel/              ← Tier 1: deployable governance kernel
│   ├── core/
│   │   ├── policy_engine.py   S = K / (P × V + ε) · three verdicts: PASS / FLAG / HARD_VETO
│   │   └── ledger.py          append-only SHA-256 Merkle chain
│   ├── api/server.py          FastAPI /adjudicate endpoint + WebSocket ledger stream
│   ├── tenir_policies.yaml    canonical epsilon (1e-6) · calibrated thresholds
│   ├── tests/
│   │   └── test_tenir_kernel.py  8 kernel validation tests (K1–K8)
│   ├── requirements.txt
│   └── run.sh
├── tenir_governance/          ← Tier 2: core governance package (public API)
│   ├── nomenclature.py        Sprint 0 — canonical term registry (R4+R5 unified)
│   ├── policy_engine.py       Sprint 1 — policy contract + membrane decision
│   ├── validator.py           Sprint 3 — 9-check CI gate (CLI + pytest fixture)
│   ├── regression_corpus.py   Sprint 4 — 258 golden test cases
│   ├── sdk.py                 Sprint 5 — TENIRGovernanceClient public API
│   ├── polymorphic_surface.py Sprint 10 — V5 surface state contract
│   ├── copy_lint.py           Sprint 9 — public-safe lexicon enforcement
│   └── ledger_migrate.py      Sprint 11 — ledger label migration
├── r4/                        ← R4 partner_a Shadow v4 monitor runtime
│   ├── tenir_v4_test/         adjudication, control-auth, ledger, models
│   └── tests/                 61 R4 monitor tests
├── r5_hardened/               ← R5 IRON OMEGA — hardened runtime
│   └── IRON_OMEGA_R5/         NSL grammar, graph ontology, Merkle crypto, WebSocket hub
├── r5_wired/                  ← R5 wired to govern package (integration layer)
├── interface/                 ← operational dashboard (HTML/JS)
├── tests/                     ← governance package test suite
├── Dockerfile                 ← reproducibility container
├── docker-compose.yml         ← full stack (middleware + Neo4j)
└── .github/workflows/         ← 6-gate blocking CI pipeline
```

### CES State Machine

The Cognitive Engagement System (CES) models the governance agent's internal state:

```
REST → METABOLIZING → TENSION → SIGNAL_CONFLICT → COLLAPSE
         ↑                              │
         └──────────────────────────────┘ (recovery path)
```

| State | Description | Operating mode |
|:---|:---|:---|
| `REST` | Stable — no action needed | SHADOW_PASSIVE |
| `METABOLIZING` | Absorbing pressure — monitor | SHADOW_PASSIVE |
| `TENSION` | Elevated — attention required | SHADOW_PASSIVE |
| `SIGNAL_CONFLICT` | Competing signals — adjudication required | SHADOW_PASSIVE |
| `COLLAPSE` | Critical — intervention mandatory | ENFORCE |

---

## Quick Start

### Tier 1 — Governance Kernel (standalone)

```bash
cd tenir-kernel
pip install -r requirements.txt
./run.sh   # starts FastAPI server on :8000
```

```bash
curl -X POST http://localhost:8000/api/v1/adjudicate \
  -H "Content-Type: application/json" \
  -d '{"actor_id": "operator-1", "action_context": "deploy-model", "p": 0.7, "v": 0.5, "k": 1.2}'
```

### Tier 2 — Full Middleware Stack

```bash
pip install -e .
docker compose up -d --build
```

```python
from tenir_governance.sdk import TENIRGovernanceClient, GovernanceEvent

client = TENIRGovernanceClient()
result = client.adjudicate(
    GovernanceEvent(pressure=0.7, velocity=0.5, capacity=1.2)
)
print(result.to_business_payload())
```

---

## Running the Test Suite

### Kernel tests (8 tests)

```bash
cd tenir-kernel
pytest tests/ -v
# Expected: 8 passed
```

### Full suite (557 tests)

```bash
pytest tests/ r4/tests/ \
       r5_hardened/IRON_OMEGA_R5/test_r5_all.py \
       r5_hardened/IRON_OMEGA_R5/test_institutional_hardening.py \
       r5_wired/test_r5_governance_integration.py \
       --cov=tenir_governance --cov-report=term-missing
```

**Expected result:** 547 passing, 2 skipped (server runtime), 96% statement coverage on `tenir_governance`.

Kernel tests run independently:
```bash
cd tenir-kernel && pytest tests/ -v   # 8/8 passing
```

Total across both tiers: **557 tests**.

### CI gate

```bash
tenir-validate --policy default           # 9/9 invariant checks
tenir-validate --policy partner_a --json  # partner_a Shadow v4
tenir-validate --policy partner_b         # partner_b Sovereign Pilot
```

---

## Policy Profiles

### Kernel tier (demonstrator profile)

| Parameter | Value | Description |
|:---|:---|:---|
| `epsilon` | `1e-6` | Canonical value — identical to full middleware |
| `hard_veto_below` | `0.5` | S below → HARD_VETO |
| `flag_below` | `1.2` | S below → FLAG (HOLDING-FIRST) |

### Full middleware profiles

| Profile | Factory | Use case |
|:---|:---|:---|
| `default` | `PolicyEngine.default()` | Canonical baseline |
| `partner_a` | `PolicyEngine.um6p_shadow_v4()` | partner_a Shadow v4 (`s_alert=0.90`, `event_window=8`) |
| `partner_b` | `PolicyEngine.ocp_sovereign_pilot()` | Tight industrial lock-in (`tau_floor=0.50`, `reaction_budget=3`) |

Policy fingerprint (default): `d083e0b82a16c04d`

### Threshold correspondence

| Kernel verdict | Full middleware verdict | Condition |
|:---|:---|:---|
| `PASS` | `allow` | S ≥ flag_below |
| `FLAG` | `allow_with_alert` | hard_veto_below ≤ S < flag_below |
| `HARD_VETO` | `block` | S < hard_veto_below |

---

## API Reference (R5 FastAPI server)

Start the server:

```bash
docker compose up -d --build
# or: uvicorn r5_hardened.IRON_OMEGA_R5.r5_server:app --host 0.0.0.0 --port 8000
```

| Endpoint | Method | Description |
|:---|:---|:---|
| `/api/v1/adjudicate` | POST | Submit intent for governance evaluation |
| `/api/v1/oath/sign` | POST | Operator oath signature for mode transition |
| `/api/v1/transition` | POST | Transition operating mode (requires oath) |
| `/api/v1/ledger/verify` | GET | Chain integrity verification |
| `/api/v1/ledger/proof/{entry_id}` | GET | Merkle inclusion proof for an entry |
| `/health` | GET | Service health check |
| `ws://…/ws/vps` | WS | Live VPS Three.js engine feed |

---

## Tooling

### Copy-lint (public-safe lexicon enforcement)

```bash
python -m tenir_governance.copy_lint docs/
python -m tenir_governance.copy_lint --exposure public homepage.html
```

### Ledger migration (legacy label rename)

```bash
python -m tenir_governance.ledger_migrate --dry-run audit/ledger.jsonl
python -m tenir_governance.ledger_migrate audit/ledger.jsonl
python -m tenir_governance.ledger_migrate --verify audit/ledger.jsonl
```

---

## Figures

| Figure | Description |
|:---|:---|
| `Figure_1` | TENIR Conceptual Governance Architecture — TAU invariant, CES state machine, Metabolic Rhizome |
| `Figure_2` | Governance Spine Architecture — request flow through nomenclature, policy engine, membrane verdict |
| `Figure_3` | Global Runtime Architecture — full stack from operator cockpit to external systems |
| `Figure_4` | Governance Decision Pipeline — linear request-to-ledger flow |
| `Figure_5` | Merkle Ledger Verification — SHA-256 chain from Block N-2 to integrity status |

---

## Terminology Notes

| Legacy term | Canonical term (R5.0.0) | Notes |
|:---|:---|:---|
| `SCHIZOPHRENIA` | `SIGNAL_CONFLICT` | State validation conflict |
| `SCHIZOPHRENIA_ALERT` | `SIGNAL_CONFLICT_ALERT` | — |
| `WHALE_RESONANCE` | `DEEP_PATTERN_SIGNAL` | — |

Backward-compatible aliases are preserved in `CESStateNames.LEGACY_ALIASES`; existing ledgers re-read cleanly.

---

## Performance (R5.0.0 Benchmark)

Measured on AMD Ryzen 9 7950X · 64 GB DDR5 · Ubuntu 22.04 · 10,000 sequential requests:

| Metric | Value |
|:---|:---|
| Mean policy latency | 12.4 ms |
| Median latency | 10.1 ms |
| P95 latency | 24.7 ms |
| Ledger write overhead | 4.2 ms |
| NSL parsing success rate | 99.98% |
| Throughput | 1,250 decisions/sec |

---

## Known Limitations (R5.0.0)

- Multi-tenant / multi-organisation isolation has not been benchmarked.
- Distributed ledger replication and global graph synchronisation are out of scope.
- NSL grammars require domain-specific schema definitions (not plug-and-play).
- Merkle ledger storage grows linearly with transaction volume.
- Administrative override records are structurally declared but disabled in this release (strict fail-closed posture).
- Kernel-tier thresholds are demonstrator defaults; institutional deployments require domain-calibrated YAML policies.

---

## Citation

If you use TENIR-Gov in your research, please cite:

```
Skiredj, A. (2026). TENIR-Gov: Governance Middleware for AI-Enabled Operational Systems.
SoftwareX. https://doi.org/10.5281/zenodo.10823456
```

Or use the `CITATION.cff` file in this repository.

---

## License

Apache License 2.0 — see [LICENSE](LICENSE).

Copyright 2026 Abdelaziz Skiredj / TENIR Labs


## Architectural Characteristics (-ilities)
- **Reliability:** The Neuro-Symbolic Language (NSL) parser enforces strict fail-closed deterministic validation. If an upstream AI model produces malformed or adversarial output, the Membrane Verdict deterministically defaults to BLOCK.
- **Scalability:** The core admissibility formula and FastAPI endpoints are stateless, allowing horizontal scaling of the governance API. The primary scaling bottleneck is the Neo4j graph traversal during complex policy evaluations.
- **Maintainability:** The system strictly separates policy from application logic via a two-tier architecture (lightweight `tenir-kernel` vs. full `tenir_governance` middleware), ensuring governance rules can evolve independently of the underlying AI models.
