
# TENIR-Gov · Deterministic Execution Gateway

> **Given current capacity and the full constraint profile of this proposed action, is execution admissible right now?**

**TENIR-Gov** is an open-source deterministic execution gateway for agentic AI systems. It sits at the boundary where a decision becomes an irreversible (or costly) effect — the crossing from the Decision Realm to the Execution Realm — and answers one sharp question at machine speed.

Unlike full-stack governance platforms, TENIR-Gov deliberately does **one thing**: it evaluates admissibility via a deterministic formula, enforces a cryptographically auditable **HARD_VETO** when constraints are violated, and logs every crossing decision to an immutable Merkle ledger. It is the control surface that lives *after* reasoning and *alongside* (not instead of) your data pipelines, model evaluation layers, and long-term monitoring tools.

> *"The interface is not the system; it is a lawful projection of a higher-dimensional invariant under contextual constraint."*
> — TENIR Master Doctrine v5

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![CI](https://github.com/skiredj-prog/tenir-gov/actions/workflows/tenir-ci.yml/badge.svg)](https://github.com/skiredj-prog/tenir-gov/actions/workflows/tenir-ci.yml)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.21277138.svg)](https://doi.org/10.5281/zenodo.21277138)

---

## Why This Matters Now

Gartner predicts that ≥70% of organizations with production agentic AI in I&O will suffer material incidents from insufficient runtime controls by 2029. Static policies, pre-deployment tests, and prompt-level guardrails are necessary but insufficient once agents hold execution authority. The missing layer is a **deterministic, fail-closed execution gate** operating at machine speed — precisely where TENIR sits.

- **Bain** (July 2026): Controls must live in the platform control plane — real-time, code-enforced — not policy documents.
- **IMDA** Model AI Governance Framework for Agentic AI (v1.5): Mandates deterministic bounds on high-autonomy actions and cryptographic traceability.
- **Gartner** (July 2026): Calls for architectural separation of cognitive/reasoning layers from deterministic execution layers.

TENIR-Gov operationalizes this exact gap.

---

## What It Is / What It Isn't

| TENIR-Gov IS | TENIR-Gov IS NOT |
|:---|:---|
| A deterministic execution gate at the decision→action boundary | A full-stack AI governance platform |
| A runtime admissibility evaluator (12.4 ms mean latency) | A data pipeline or model evaluation tool |
| A fail-closed HARD_VETO with Merkle audit trail | A prompt filter or prompt injection detector |
| A complement to your existing reasoning and monitoring stack | A replacement for your LLM, observability, or drift monitoring |

---

## Two-Tier Architecture

TENIR-Gov ships as two complementary tiers that share **identical decision semantics**:

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

The kernel tier demonstrates that core admissibility logic is separable from the institutional surface. Governance policy (YAML) is decoupled from governance logic (code), and the formula is identical in both tiers.

---

## Quick Start

### Tier 1 — Governance Kernel (standalone, ~500 KB, zero external dependencies)

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

Tier 2 — Full Middleware Stack

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

A new developer reaches a working HARD_VETO demonstration in under three minutes.

---

The Admissibility Formula

Execution admissibility is computed deterministically from capacity and constraint geometry:

```
S = K / (P × V + ε)
```

Symbol	Meaning	
K	Current system capacity (operational, epistemic, institutional)	
P	Action pressure (demand intensity)	
V	Action volatility (blast-radius / irreversibility)	
ε	Stability floor (canonical: `1e-6`)	
S	Admissibility score	

Verdict mapping:

Kernel verdict	Full middleware verdict	Condition	
`PASS`	`allow`	S ≥ flag_below	
`FLAG`	`allow_with_alert`	hard_veto_below ≤ S < flag_below	
`HARD_VETO`	`block`	S < hard_veto_below	

If `S` falls below the calibrated threshold, a HARD_VETO is issued automatically — no escalation chain, no human-in-the-loop delay.

---

Key Capabilities

Capability	Implementation	
Governance Kernel	`tenir-kernel/` — minimal 6-file deployable (formula + YAML + Merkle ledger + FastAPI)	
Neuro-Symbolic Validation	LALR(1) grammar parser + optional LLM fine-tuning via QLoRA	
Deterministic Policy Engine	Frozen dataclass contract; single source of truth for all thresholds	
Graph-Based Policy Store	Neo4j 5.x with full ontology (CP-Net structure)	
Cryptographic Audit Ledger	Merkle epoch trees + SHA-256 hash chain; inclusion proofs at O(log n)	
Administrative Governance Plane	Policy lifecycle, approval workflows, asymmetric-key change-control	
Polymorphic Surface Contract	Four UI states (AMBIENT / ANTICIPATION / ADJUDICATION / FORENSIC)	
Legacy Migration Tooling	`ledger_migrate.py` rewrites JSONL ledgers preserving forensic continuity	

---

Architecture

Release R5.0.0 (IRON OMEGA R5)

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
├── r5_hardened/               ← R5 IRON OMEGA — hardened runtime
├── r5_wired/                  ← R5 wired to govern package (integration layer)
├── interface/                 ← operational dashboard (HTML/JS)
├── tests/                     ← governance package test suite
├── Dockerfile
├── docker-compose.yml         ← full stack (middleware + Neo4j)
└── .github/workflows/         ← 6-gate blocking CI pipeline
```

CES State Machine

The Cognitive Engagement System (CES) models the governance agent's internal state:

```
REST → METABOLIZING → TENSION → SIGNAL_CONFLICT → COLLAPSE
         ↑                              │
         └──────────────────────────────┘ (recovery path)
```

State	Description	Operating mode	
`REST`	Stable — no action needed	SHADOW_PASSIVE	
`METABOLIZING`	Absorbing pressure — monitor	SHADOW_PASSIVE	
`TENSION`	Elevated — attention required	SHADOW_PASSIVE	
`SIGNAL_CONFLICT`	Competing signals — adjudication required	SHADOW_PASSIVE	
`COLLAPSE`	Critical — intervention mandatory	ENFORCE	

---

API Reference (R5 FastAPI server)

Start the server:

```bash
docker compose up -d --build
# or: uvicorn r5_hardened.IRON_OMEGA_R5.r5_server:app --host 0.0.0.0 --port 8000
```

Endpoint	Method	Description	
`/api/v1/adjudicate`	POST	Submit intent for governance evaluation	
`/api/v1/oath/sign`	POST	Operator oath signature for mode transition	
`/api/v1/transition`	POST	Transition operating mode (requires oath)	
`/api/v1/ledger/verify`	GET	Chain integrity verification	
`/api/v1/ledger/proof/{entry_id}`	GET	Merkle inclusion proof for an entry	
`/health`	GET	Service health check	
`ws://…/ws/vps`	WS	Live VPS Three.js engine feed	

---

Policy Profiles

Kernel tier (demonstrator profile)

Parameter	Value	Description	
`epsilon`	`1e-6`	Canonical value — identical to full middleware	
`hard_veto_below`	`0.5`	S below → HARD_VETO	
`flag_below`	`1.2`	S below → FLAG (HOLDING-FIRST)	

Full middleware profiles

Profile	Factory	Use case	
`default`	`PolicyEngine.default()`	Canonical baseline	
`partner_a`	`PolicyEngine.um6p_shadow_v4()`	partner_a Shadow v4 (`s_alert=0.90`, `event_window=8`)	
`partner_b`	`PolicyEngine.ocp_sovereign_pilot()`	Tight industrial lock-in (`tau_floor=0.50`, `reaction_budget=3`)	

Policy fingerprint (default): `d083e0b82a16c04d`

---

Performance (R5.0.0 Benchmark)

Measured on AMD Ryzen 9 7950X · 64 GB DDR5 · Ubuntu 22.04 · 10,000 sequential requests:

Metric	Value	
Mean policy latency	12.4 ms	
Median latency	10.1 ms	
P95 latency	24.7 ms	
Ledger write overhead	4.2 ms	
NSL parsing success rate	99.98%	
Throughput	1,250 decisions/sec	

---

Running the Test Suite

Kernel tests (8 tests)

```bash
cd tenir-kernel
pytest tests/ -v
# Expected: 8 passed
```

Full suite (557 tests)

```bash
pytest tests/ r4/tests/ \
       r5_hardened/IRON_OMEGA_R5/test_r5_all.py \
       r5_hardened/IRON_OMEGA_R5/test_institutional_hardening.py \
       r5_wired/test_r5_governance_integration.py \
       --cov=tenir_governance --cov-report=term-missing
```

Expected result: 547 passing, 2 skipped (server runtime), 96% statement coverage on `tenir_governance`.

CI gate

```bash
tenir-validate --policy default           # 9/9 invariant checks
tenir-validate --policy partner_a --json  # partner_a Shadow v4
tenir-validate --policy partner_b         # partner_b Sovereign Pilot
```

---

Tooling

Copy-lint (public-safe lexicon enforcement)

```bash
python -m tenir_governance.copy_lint docs/
python -m tenir_governance.copy_lint --exposure public homepage.html
```

Ledger migration (legacy label rename)

```bash
python -m tenir_governance.ledger_migrate --dry-run audit/ledger.jsonl
python -m tenir_governance.ledger_migrate audit/ledger.jsonl
python -m tenir_governance.ledger_migrate --verify audit/ledger.jsonl
```

---

Terminology Notes

Legacy term	Canonical term (R5.0.0)	Notes	
`SCHIZOPHRENIA`	`SIGNAL_CONFLICT`	State validation conflict	
`SCHIZOPHRENIA_ALERT`	`SIGNAL_CONFLICT_ALERT`	—	
`WHALE_RESONANCE`	`DEEP_PATTERN_SIGNAL`	—	

Backward-compatible aliases are preserved in `CESStateNames.LEGACY_ALIASES`.

---

Scientific Foundation

TENIR-Gov is grounded in peer-reviewed research on constitutional membranes, governance homeostasis, and autonomous control theory. The core framework is currently under review at Array (ARRAY-D-26-04832). The gateway positioning is the industry-facing expression of a deeper scientific architecture — we maintain the full membrane model (CES, trajectory awareness, multi-crossing potential) internally while exposing the clearest possible interface for production use.

> Note: This repository implements the foundational deterministic governance spine (Canon 1.0). Advanced ontological structures from the broader TENIR Enacted 2.0 framework represent the theoretical roadmap for future major releases.

---

Citation

If you use TENIR-Gov in your research, please cite:

```
Skiredj, A. (2026). TENIR-Gov: Governance Middleware for AI-Enabled Operational Systems.
SoftwareX. https://doi.org/10.5281/zenodo.21277138
```

Or use the `CITATION.cff` file in this repository.

---

License

Apache License 2.0 — see [LICENSE](LICENSE).

Copyright 2026 Abdelaziz Skiredj / TENIR Labs

---

TENIR-Gov: Execution is not a given. It is a gated decision.

```

---

## Artifact 2: LinkedIn Announcement Post

```

Today we are sharpening TENIR-Gov to its clearest identity yet:

The Deterministic Execution Gateway for agentic AI.

Not a full-stack governance platform. Not a policy engine. Not a prompt filter.

One question, answered at machine speed:
"Given current capacity and the full constraint profile of this proposed action, is execution admissible right now?"

If the answer is no, a HARD_VETO fires automatically. Cryptographically logged to a SHA-256 Merkle chain. Immutably auditable with O(log n) inclusion proofs. No escalation chain. No human delay.

Why now?

→ Gartner predicts ≥70% of organizations with production agentic AI will suffer material incidents from insufficient runtime controls by 2029.
→ Bain's latest brief confirms controls must live in the platform control plane — real-time, code-enforced.
→ IMDA's Model AI Governance Framework v1.5 mandates deterministic bounds on high-autonomy actions.

The industry is converging on a gap that TENIR-Gov was built to fill: the boundary between deciding and doing.

Two tiers. Identical semantics. Zero lock-in.

→ tenir-kernel: 500 KB, zero dependencies, 12.4 ms mean latency, 1,250 decisions/sec
→ tenir_governance: Full neuro-symbolic stack (NSL + LALR(1)), Neo4j policy graph, FastAPI/WebSocket, 549 tests, 96% coverage

Same formula. Same Merkle ledger. Same fail-closed guarantee.

The science stays deep — constitutional membranes, governance homeostasis, sovereignty properties — currently under review at Array (ARRAY-D-26-04832). The public face stays sharp.

Execution is not a given. It is a gated decision.

→ GitHub: [link]
→ DOI: https://doi.org/10.5281/zenodo.21277138

#AgenticAI #AIGovernance #DeterministicExecution #TENIRGov #OpenSource #MachineSpeedControls #IMDA #Gartner

```

---

## Artifact 3: One-Page Gartner I&O Alignment Note

**TENIR-Gov / Gartner I&O Alignment — August 2026**

**Analyst prediction:** Gartner (Spafford, July 2026) forecasts that ≥70% of organizations with production agentic AI in I&O will suffer material incidents from insufficient runtime controls by 2029.

**Root cause:** The architectural separation between cognitive/reasoning layers and deterministic execution layers is incomplete. Organizations have invested heavily in models, prompts, and pre-deployment evaluation — but lack a control surface at the exact boundary where a decision becomes an irreversible effect.

**TENIR-Gov's position:** TENIR-Gov is the deterministic execution gateway that occupies this boundary. It does not compete with reasoning layers, data pipelines, or monitoring tools. It complements them by providing the missing fail-closed control surface.

| Gartner Requirement | TENIR-Gov Capability |
|---|---|
| Real-time runtime controls | **12.4 ms** mean latency; **1,250 decisions/s** sustained |
| Separation of reasoning from execution | Explicit Decision Realm → Execution Realm architecture; two-tier design (kernel vs. full middleware) |
| Fail-closed default | **HARD_VETO** issued automatically when constraints violated; administrative overrides structurally declared but disabled in R5.0.0 |
| Auditability & accountability | Immutable **SHA-256 Merkle ledger**; inclusion proofs at O(log n); every decision cryptographically anchored |
| Code-enforced, not policy-document | Deterministic formula `S = K/(P×V+ε)` with constraint geometry; frozen dataclass policy contract |
| Machine-speed vs. human-speed gap | Autonomous veto requires no human escalation chain |

**The Knight Capital counterfactual:** In 2012, Knight Capital lost $440M in 45 minutes because a control failure propagated at machine speed while human escalation chains operated at human speed. A deterministic execution gate with autonomous HARD_VETO would have halted the first invalid order — not the 4 millionth. This illustrates the speed differential TENIR-Gov is designed to close.

**Scope discipline:** TENIR-Gov intentionally excludes data pipeline governance, model evaluation, prompt injection detection, and long-term drift monitoring. It is the execution gate that sits *after* these layers and *before* irreversible action. This narrow scope ensures it integrates cleanly with broader compliance architectures rather than competing with them.

**Bottom line:** TENIR-Gov operationalizes the exact control-plane gap Gartner identifies. It is not an additional governance burden — it is the architectural layer that makes agentic AI safe to run at scale.

---

## Artifact 4: IMDA Model AI Governance Framework Mapping

**TENIR-Gov / IMDA Model AI Governance Framework for Agentic AI (v1.5) — Mapping Document**

| IMDA v1.5 Requirement | TENIR-Gov Implementation | Evidence |
|---|---|---|
| **Deterministic bounds on irreversible/high-autonomy actions** | The admissibility formula `S = K/(P×V+ε)` computes a deterministic threshold for every proposed execution. Constraint geometry maps action risk profiles against real-time capacity. Three verdicts: PASS / FLAG / HARD_VETO. | `tenir-kernel/core/policy_engine.py`; 99.98% NSL parsing success |
| **Cryptographic traceability of governance decisions** | Every admissibility evaluation produces a Merkle root anchoring the decision, its inputs, and the constraint profile to an append-only SHA-256 chain. Inclusion proofs at O(log n). | `tenir-kernel/core/ledger.py`; `tenir_governance` persistent ledger |
| **Clear separation of evaluation from commitment** | Explicit architectural separation: evaluation occurs in the Decision Realm; commitment (execution) occurs only after Gateway admissibility clearance. | Two-realm architecture; HARD_VETO as commitment gate |
| **Real-time enforcement, not post-hoc review** | **12.4 ms** mean latency; autonomous HARD_VETO requires no human escalation. | R5.0.0 benchmark (AMD Ryzen 9 7950X, 10,000 sequential requests) |
| **Fail-closed behavior** | If capacity cannot be verified or constraints are violated, default behavior is HARD_VETO (execution denied). Administrative overrides disabled in R5.0.0. | Kernel design invariant; `tenir_policies.yaml` |
| **Retained human accountability** | Merkle ledger provides non-repudiable audit trail linking every executed action to its governance clearance, enabling post-hoc accountability without requiring real-time human approval. | `/api/v1/ledger/verify` and `/api/v1/ledger/proof/{entry_id}` endpoints |

**How TENIR-Gov fits in a compliant stack:**

```

[Agent / LLM / Reasoning Layer]  ← Evaluates options, generates proposals
↓
[TENIR-Gov Gateway]              ← Determines admissibility (THIS LAYER)
↓
[Execution Environment]          ← Acts only if admissible = True
↓
[Monitoring / Drift Detection]   ← Long-term observability (complementary)

```

**Statement of scope:** TENIR-Gov intentionally does *not* provide data pipeline governance, model evaluation, prompt injection detection, or long-term drift monitoring. It is the deterministic execution gate that sits *after* these layers and *before* irreversible action. This narrow scope ensures it integrates cleanly with broader compliance architectures rather than competing with them.

---

**DOI:** `https://doi.org/10.5281/zenodo.21277138`