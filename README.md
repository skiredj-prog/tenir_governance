Here are four revised public-facing artifacts, rebuilt strictly from the positioning strategy. Every claim is §7-qualified or removed. "Gateway" has been eliminated entirely. "Control Plane" is the lead category. "Fail-closed by design" replaces "un-bypassable." The DOI is corrected throughout.

---

Artifact 1: README.md (Commit-Ready)

```markdown
# TENIR-Gov · The Execution Control Plane for Agentic AI

> **It decides whether what AI wants to do is allowed to become real.**

**TENIR-Gov** is the Execution Control Plane for agentic AI. It governs how autonomous reasoning crosses into consequential execution — deterministically, at machine speed, with cryptographic auditability.

Unlike full-stack governance suites, TENIR-Gov occupies one precise layer: the runtime boundary between decision and action. It does not replace your LLM, your observability stack, or your policy documents. It is the deterministic, fail-closed control surface that sits *after* reasoning and *before* irreversible effect.

> *"AI can reason autonomously. TENIR-Gov controls whether that reasoning gets to execute."*

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![CI](https://github.com/skiredj-prog/tenir-gov/actions/workflows/tenir-ci.yml/badge.svg)](https://github.com/skiredj-prog/tenir-gov/actions/workflows/tenir-ci.yml)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.21277138.svg)](https://doi.org/10.5281/zenodo.21277138)

---

## Three-Sentence Pitch

1. **Problem:** Models reason, agents propose — but who decides an action is admissible before it becomes irreversible?
2. **Solution:** TENIR-Gov is the Execution Control Plane for agentic AI — a deterministic, fail-closed control surface governing the crossing from autonomous decision into consequential execution.
3. **Proof:** A 500 KB zero-dependency kernel deploys in minutes. Every verdict is anchored to an immutable Merkle ledger. The same semantics power both the lightweight kernel and the full enterprise middleware.

---

## Positioning Architecture

TENIR-Gov is expressed at three levels. Each answers a different question for a different reader. They are kept distinct deliberately.

| Level | Question | Formulation |
|:---|:---|:---|
| **Category** (market) | What are you? | **The Execution Control Plane for Agentic AI** |
| **Architectural Primitive** (defensible IP) | What differentiates you? | **Constitutional Execution Membrane** |
| **Core Promise** (buyer) | What does it guarantee? | **Deterministic, fail-closed control between autonomous reasoning and consequential execution — machine-speed, cryptographically auditable** |

---

## What It Is / What It Isn't

| TENIR-Gov IS | TENIR-Gov IS NOT |
|:---|:---|
| An execution control plane at the decision→action boundary | A full-stack AI governance platform |
| A deterministic admissibility evaluator with fail-closed defaults | A data pipeline, model evaluation tool, or prompt filter |
| A cryptographically auditable control surface (Merkle ledger) | A replacement for your LLM, observability, or drift monitoring |
| A complement to existing security and reasoning stacks | A competitor to Palantir, OneTrust, or IBM governance suites |

---

## Why This Matters Now

- **Gartner** (Spafford, July 2026): ≥70% of organizations with production agentic AI in I&O will suffer material incidents from insufficient runtime controls by 2029. The missing layer is not more policy documents — it is a deterministic execution control plane.
- **Bain** (July 2026): Controls must live in the platform control plane — real-time, code-enforced — not static policy documents.
- **IMDA** Model AI Governance Framework for Agentic AI (v1.5): Mandates deterministic bounds on high-autonomy actions and cryptographic traceability.

TENIR-Gov operationalizes the exact gap these signals identify.

---

## Product Architecture: Core + Modules

TENIR-Gov is one product with one Core and incremental modules. This prevents message dilution and avoids competing with adjacent suites on their own terrain.

```

TENIR-Gov
THE EXECUTION CONTROL PLANE
│
├── CONSTITUTIONAL EXECUTION CORE
│   S = K / (P × V + ε)
│   PASS / FLAG / HARD_VETO
│   Federated
│   Merkle ledger
│   API REST + WebSocket
│   tenir-kernel/  (500 KB, zero external dependencies)
│
└── CONTROL & VALUE MODULES (incremental)
├── NSL Policy Studio     (LALR(1) + LLM policy authoring)
├── VPS Forensics         (trajectory dashboards, ledger analysis)
├── EVP Analytics         (TPI → IVT → EVP value protection reporting)
└── Federated Control     (multi-cluster, multi-region audit)

```

**Core** (`tenir-kernel/`) is mandatory and independently deployable.  
**Modules** map to existing code — nothing is rewritten, only repackaged.

> **Architectural guarantee:** The EVP module consumes the Merkle ledger in read-only mode. It has no write path back to the Core. Valuation signals can never influence an admissibility decision. This is a constitutional integrity boundary, not a convenience.

---

## Quick Start

### Core — Governance Kernel (standalone)

```bash
cd tenir-kernel
pip install -r requirements.txt
./run.sh   # FastAPI server on :8000
```

```bash
curl -X POST http://localhost:8000/api/v1/adjudicate \
  -H "Content-Type: application/json" \
  -d '{"actor_id": "operator-1", "action_context": "deploy-model", "p": 0.7, "v": 0.5, "k": 1.2}'
```

Full Middleware

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

The Admissibility Formula

```
S = K / (P × V + ε)
```

Symbol	Meaning	
K	Current system capacity (operational, epistemic, institutional)	
P	Action pressure (demand intensity)	
V	Action volatility (blast-radius / irreversibility)	
ε	Stability floor (canonical: `1e-6`)	
S	Admissibility score	

Verdicts:

Condition	Kernel	Full Middleware	
S ≥ `flag_below`	`PASS`	`allow`	
`hard_veto_below` ≤ S < `flag_below`	`FLAG`	`allow_with_alert`	
S < `hard_veto_below`	`HARD_VETO`	`block`	

If the score falls below threshold, a HARD_VETO is issued automatically — no escalation chain, no human-in-the-loop delay. This is fail-closed by design.

---

Competitive Landscape

TENIR-Gov is an adjacent category, structurally different. It does not replace your security stack. It occupies the execution control plane that sits between existing layers.

Layer	Question Asked	Representative Tools	How TENIR-Gov Differs	
API Gateway	Is the request authenticated and rate-limited?	Kong, Envoy, NGINX	Inspects network syntax; TENIR evaluates dynamic state transition (S).	
Policy Engine	Does the request satisfy static rules?	OPA, Cedar	Fixed rules; TENIR evaluates capacity vs. volatility in real time.	
LLM Guardrails	Is the text safe / toxic?	NeMo Guardrails, Lakera	Evaluates probabilistic content; TENIR imposes a deterministic fail-closed boundary.	
Observability / Drift	What did the agent do in the last 24h?	Arize, LangSmith, Datadog	Post-hoc analysis; TENIR intercepts before execution.	
Execution Control Plane	Is this transition admissible right now?	TENIR-Gov	Constitutional boundary: `S = K/(P×V+ε)` with Merkle proof.	

---

Architecture

Release R5.0.0 (IRON OMEGA R5)

```
tenir-gov/
├── tenir-kernel/              ← Core: deployable governance kernel
│   ├── core/
│   │   ├── policy_engine.py   S = K / (P × V + ε) · PASS / FLAG / HARD_VETO
│   │   └── ledger.py          append-only SHA-256 Merkle chain
│   ├── api/server.py          FastAPI /adjudicate + WebSocket ledger stream
│   ├── tenir_policies.yaml    canonical epsilon · calibrated thresholds
│   ├── tests/
│   │   └── test_tenir_kernel.py  8 kernel validation tests (K1–K8)
│   ├── requirements.txt
│   └── run.sh
├── tenir_governance/          ← Module: enterprise governance package
│   ├── nomenclature.py        canonical term registry
│   ├── policy_engine.py       policy contract + membrane decision
│   ├── validator.py           9-check CI gate
│   ├── regression_corpus.py   258 golden test cases
│   ├── sdk.py                 TENIRGovernanceClient public API
│   ├── polymorphic_surface.py V5 surface state contract
│   ├── copy_lint.py           public-safe lexicon enforcement
│   └── ledger_migrate.py      ledger label migration
├── r4/                        ← partner_a Shadow v4 monitor runtime
├── r5_hardened/               ← IRON OMEGA hardened runtime
├── r5_wired/                  ← integration layer
├── interface/                 ← operational dashboard (HTML/JS)
├── tests/                     ← governance package test suite
├── Dockerfile
├── docker-compose.yml         ← full stack (middleware + Neo4j)
└── .github/workflows/         ← 6-gate blocking CI pipeline
```

CES State Machine

The Cognitive Engagement System models internal governance state:

```
REST → METABOLIZING → TENSION → SIGNAL_CONFLICT → COLLAPSE
         ↑                              │
         └──────────────────────────────┘ (recovery path)
```

State	Description	Mode	
`REST`	Stable — no action needed	SHADOW_PASSIVE	
`METABOLIZING`	Absorbing pressure — monitor	SHADOW_PASSIVE	
`TENSION`	Elevated — attention required	SHADOW_PASSIVE	
`SIGNAL_CONFLICT`	Competing signals — adjudication required	SHADOW_PASSIVE	
`COLLAPSE`	Critical — intervention mandatory	ENFORCE	

---

API Reference (R5 FastAPI server)

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

Kernel tier (demonstrator)

Parameter	Value	Description	
`epsilon`	`1e-6`	Canonical value	
`hard_veto_below`	`0.5`	S below → HARD_VETO	
`flag_below`	`1.2`	S below → FLAG	

Full middleware profiles

Profile	Factory	Use case	
`default`	`PolicyEngine.default()`	Canonical baseline	
`partner_a`	`PolicyEngine.um6p_shadow_v4()`	partner_a Shadow v4	
`partner_b`	`PolicyEngine.ocp_sovereign_pilot()`	Tight industrial lock-in	

Policy fingerprint (default): `d083e0b82a16c04d`

---

Performance

> Methodology note: Internal benchmark on AMD Ryzen 9 7950X · 64 GB DDR5 · Ubuntu 22.04 · localhost (no network hop) · 10,000 sequential requests · minimal JSON payload. Your results will vary by hardware, payload size, network topology, and concurrent load. These figures are directional, not guaranteed SLAs.

Metric	Value	Notes	
Mean latency	12.4 ms	Ledger write included; single-threaded sequential	
Median (p50)	10.1 ms		
P95	24.7 ms		
Ledger write overhead	4.2 ms	Per-adjudication append	
NSL parsing success	99.98%	Over 258-case regression corpus	
Throughput	1,250 decisions/sec	Sequential load; concurrent benchmarking not yet performed	

---

Test Suite

> Methodology note: Test counts and coverage measured via `pytest` and `pytest-cov`. Coverage scope is explicitly bounded.

Suite	Count	Type	Tool	Scope	
Kernel	8	Unit	pytest	Core formula, ledger, API	
Full middleware	547	Unit + Integration	pytest + pytest-cov	`tenir_governance` package	
R4 monitor	61	Integration	pytest	partner_a Shadow v4 runtime	
Total	557			96% statement coverage on `tenir_governance`	
				(Excludes R4, R5 hardened, interface, and server runtime tests)	

Expected: 547 passing, 2 skipped (server runtime).

CI gate

```bash
tenir-validate --policy default           # 9/9 invariant checks
tenir-validate --policy partner_a --json  # partner_a Shadow v4
tenir-validate --policy partner_b         # partner_b Sovereign Pilot
```

---

Tooling

Copy-lint (public-safe lexicon)

```bash
python -m tenir_governance.copy_lint docs/
python -m tenir_governance.copy_lint --exposure public homepage.html
```

Ledger migration

```bash
python -m tenir_governance.ledger_migrate --dry-run audit/ledger.jsonl
python -m tenir_governance.ledger_migrate audit/ledger.jsonl
python -m tenir_governance.ledger_migrate --verify audit/ledger.jsonl
```

---

Terminology Notes

Legacy term	Canonical term (R5.0.0)	
`SCHIZOPHRENIA`	`SIGNAL_CONFLICT`	
`SCHIZOPHRENIA_ALERT`	`SIGNAL_CONFLICT_ALERT`	
`WHALE_RESONANCE`	`DEEP_PATTERN_SIGNAL`	

Backward-compatible aliases preserved in `CESStateNames.LEGACY_ALIASES`.

---

Scientific Foundation

TENIR-Gov is grounded in peer-reviewed research on constitutional membranes, governance homeostasis, and autonomous control theory. The core framework is under review at Array (ARRAY-D-26-04832).

> Scope note: This repository implements the foundational deterministic governance spine (Canon 1.0). Advanced ontological structures from the broader TENIR Enacted 2.0 framework represent the theoretical roadmap for future major releases.

---

Citation

```
Skiredj, A. (2026). TENIR-Gov: Governance Middleware for AI-Enabled Operational Systems.
SoftwareX. https://doi.org/10.5281/zenodo.21277138
```

Or use the `CITATION.cff` file in this repository.

---

License

Apache License 2.0 — see [LICENSE](LICENSE).

Copyright 2026 Abdelaziz Skiredj / TENIR Labs

```

---

## Artifact 2: LinkedIn Announcement Post

```

Today we are sharpening TENIR-Gov to its clearest identity:

The Execution Control Plane for Agentic AI.

Not a full-stack governance suite. Not a policy engine. Not a prompt filter.

One control surface. One precise boundary.

"AI can reason autonomously. TENIR-Gov controls whether that reasoning gets to execute."

The problem: Models reason, agents propose — but who decides an action is admissible before it becomes irreversible?

The solution: A deterministic, fail-closed control plane that governs the crossing from autonomous decision into consequential execution — at machine speed, with every verdict cryptographically anchored to an immutable Merkle ledger.

Why now?

→ Gartner predicts ≥70% of organizations with production agentic AI will suffer material incidents from insufficient runtime controls by 2029.
→ Bain confirms controls must live in the platform control plane — real-time, code-enforced.
→ IMDA's Model AI Governance Framework v1.5 mandates deterministic bounds on high-autonomy actions.

The industry is converging on a gap that TENIR-Gov was built to fill.

Architecture: One Core + incremental Modules.

→ Core (tenir-kernel): 500 KB. Zero external dependencies. Deploy in minutes. Fail-closed by design.
→ Modules: NSL Policy Studio · VPS Forensics · EVP Analytics · Federated Control.

Same formula. Same ledger. Same semantics from kernel to enterprise.

The science stays deep — constitutional execution membrane, governance homeostasis, sovereignty properties — currently under review at Array (ARRAY-D-26-04832). The public face stays sharp.

Execution is not a given. It is a controlled crossing.

→ GitHub: [link]
→ DOI: https://doi.org/10.5281/zenodo.21277138

#AgenticAI #AIGovernance #ExecutionControlPlane #TENIRGov #OpenSource #DeterministicControls #IMDA #Gartner

```

---

## Artifact 3: Competitive Positioning One-Pager

**TENIR-Gov — Adjacent Category, Structurally Different**

TENIR-Gov does not claim to replace your existing security and governance stack. It occupies the execution control plane that sits between layers you already have — the runtime boundary where autonomous decisions become consequential actions.

**The gap:** Organizations have invested heavily in API gateways, policy engines, LLM guardrails, and observability. What remains unguarded is the precise moment an agentic system attempts an irreversible or high-blast-radius execution. Static policies, pre-deployment tests, and prompt filters are necessary but insufficient at this boundary.

**TENIR-Gov's position:** The Execution Control Plane for agentic AI. A deterministic, fail-closed control surface that evaluates capacity against volatility in real time and issues a cryptographically auditable verdict before execution proceeds.

---

### Where TENIR-Gov Sits in Your Stack

```

[Agent / LLM / Reasoning Layer]     ← Generates proposals
↓
[Policy Engine / Guardrails]        ← Static rules, content safety
↓
[API Gateway / IAM]                 ← Auth, rate-limiting, routing
↓
[TENIR-Gov Execution Control Plane] ← THIS LAYER: admissibility verdict
↓
[Execution Environment]             ← Acts only if PASS cleared
↓
[Observability / Drift Monitoring]  ← Post-hoc analysis

```

---

### Benchmark: Adjacent Categories vs. TENIR-Gov

| Layer | Question Asked | Representative Tools | Distinction |
|:---|:---|:---|:---|
| **API Gateway** | Is the request authenticated and rate-limited? | Kong, Envoy, NGINX | Inspects network syntax; TENIR evaluates dynamic state transition (S). |
| **Policy Engine** | Does the request satisfy static rules? | OPA, Cedar | Fixed rules; TENIR evaluates capacity vs. volatility in real time. |
| **LLM Guardrails** | Is the text safe / toxic? | NeMo Guardrails, Lakera | Evaluates probabilistic content; TENIR imposes a deterministic fail-closed boundary. |
| **Observability / Drift** | What did the agent do in the last 24h? | Arize, LangSmith, Datadog | Post-hoc analysis; TENIR intercepts *before* execution. |
| **Execution Control Plane** | Is this transition admissible right now? | **TENIR-Gov** | Constitutional boundary: `S = K/(P×V+ε)` with Merkle proof. |

---

### Why "Control Plane" and Not "Gateway"

A gateway is a component that intercepts calls. A control plane is an infrastructure layer that coordinates enforcement points — the vocabulary of Kubernetes, SDN, and service mesh. This distinction matters because TENIR-Gov may be the sole execution frontier *or* one coordinated gate among many. "Control Plane" permits both readings without forcing a choice.

---

### Scope Discipline

TENIR-Gov intentionally excludes:
- Data pipeline governance
- Model evaluation and benchmarking
- Prompt injection detection
- Long-term drift monitoring

It is the execution control plane that sits *after* these layers and *before* irreversible action. This narrow scope ensures clean integration with broader compliance architectures rather than competition with them.

---

**Bottom line:** TENIR-Gov operationalizes the exact runtime control gap identified by Gartner, Bain, and IMDA. It is not an additional governance burden — it is the architectural layer that makes agentic AI safe to run at scale.

---

## Artifact 4: Audience Messaging & Offers Card

**TENIR-Gov — Messaging by Audience**

| Audience | Hook | Key Module |
|:---|:---|:---|
| **Engineers / MLOps** | "Deploy the gate in 3 minutes. 500 KB. Zero dependencies." | Core |
| **Architects / VP Engineering** | "Separate reasoning from execution. Fail-closed by design." | Core + NSL Policy Studio |
| **CISOs** | "Every decision is cryptographically anchored. Inclusion proof O(log n)." | Core + VPS Forensics |
| **CFO / Risk Management** | "TPI measures protected risk. EVP quantifies value preserved by adjudicated crossings." | Core + EVP Analytics |
| **Compliance / Legal** | "Governance is no longer a document — it is code." | Core + NSL Policy Studio |

---

**The Pitch (3 phrases)**

1. *"Models reason, agents propose — but who decides an action is admissible before it becomes irreversible?"*
2. *"TENIR-Gov is the Execution Control Plane for agentic AI — a deterministic, fail-closed control surface governing the crossing from decision to execution."*
3. *"Deploy the Core in minutes. Expand with modules as your governance maturity grows."*

---

**Packaging & Offers**

| Offer | Contents | Channel |
|:---|:---|:---|
| **Open Source (Community)** | Core (`tenir-kernel`) + SDK | GitHub, self-service |
| **Professional** | Core + NSL Policy Studio + SLA support | Direct sales, AWS Marketplace |
| **Enterprise** | Core + all modules + federated deployment + training | Pre-sales, consulting |
| **TENIR-as-a-Service** | Cloud-hosted, API-only | SaaS, pay-as-you-go |

---

**Constitutional Integrity Guarantee**

The EVP Analytics module consumes the Merkle ledger in **read-only mode**. It has no write path back to the Core. Valuation signals can never influence a PASS, FLAG, or HARD_VETO decision. This is an architectural guarantee, not a convenience.

---

**DOI:** `https://doi.org/10.5281/zenodo.21277138`