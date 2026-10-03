# TENIR-Gov · The Execution Control Plane for Agentic AI

> **It decides whether what AI wants to do is allowed to become real.**

**TENIR-Gov** is an Execution Control Plane for agentic AI. It governs the transition from machine-generated decision or intent to consequential execution — deterministically, at runtime, with cryptographically verifiable decision records.

TENIR-Gov occupies one precise layer: the **runtime boundary between decision and action**.

It does not replace an LLM, an identity system, an observability stack, a policy repository, or an enterprise governance program. It provides a deterministic control surface that can be placed **after reasoning and policy evaluation, and before consequential execution**.

> *AI can reason autonomously. TENIR-Gov controls whether that reasoning is admissible for execution.*

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![CI](https://github.com/skiredj-prog/tenir-gov/actions/workflows/tenir-ci.yml/badge.svg)](https://github.com/skiredj-prog/tenir-gov/actions/workflows/tenir-ci.yml)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.21277138.svg)](https://doi.org/10.5281/zenodo.21277138)

---

## Three-Sentence Pitch

1. **Problem:** Models reason and agents propose actions — but a consequential action needs a runtime decision about whether it is admissible before execution.
2. **Solution:** TENIR-Gov provides a deterministic execution-control layer at the decision → action boundary, with explicit policy thresholds and fail-closed defaults.
3. **Proof:** The R5.0.0 repository includes an independently deployable governance kernel, a full middleware implementation, automated validation, and Merkle-backed decision records.

---

# Positioning

TENIR-Gov is expressed at three levels:

| Level                       | Question                              | Formulation                                                                                                  |
| --------------------------- | ------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| **Category**                | What is it?                           | **The Execution Control Plane for Agentic AI**                                                               |
| **Architectural primitive** | What is distinctive about the design? | **Constitutional Execution Membrane**                                                                        |
| **Core promise**            | What does it provide?                 | **Deterministic, fail-closed control at the decision → execution boundary, with auditable decision records** |

The category describes where TENIR-Gov sits.

The architectural primitive describes the control boundary implemented by the system.

The promise describes the operational property without implying that TENIR-Gov replaces the rest of an organization's governance stack.

---

# What TENIR-Gov Is / Is Not

| TENIR-Gov **IS**                                         | TENIR-Gov **IS NOT**                                |
| -------------------------------------------------------- | --------------------------------------------------- |
| A runtime execution-control layer                        | A full enterprise AI-governance suite               |
| A deterministic admissibility evaluator                  | An LLM or autonomous reasoning engine               |
| A policy-driven execution gate                           | A prompt filter                                     |
| A model-independent control surface                      | A model-evaluation benchmark                        |
| A cryptographically auditable decision layer             | A replacement for IAM or API security               |
| A complement to existing governance and security systems | A replacement for observability or drift monitoring |
| Independently deployable as a lightweight kernel         | A requirement to adopt the complete TENIR stack     |

---

# Why Runtime Control Matters

Agentic AI introduces a distinction between **producing a decision** and **executing that decision**.

Current governance guidance increasingly emphasizes runtime boundaries, constrained autonomy, technical controls, and meaningful accountability.

Gartner has highlighted the gap between written governance policies and runtime controls capable of constraining autonomous actions at machine speed. Its 2026 research also recommends embedding enterprise rules, approvals, escalation paths, and other constraints into agent design and runtime controls.

Singapore's IMDA Model AI Governance Framework for Agentic AI, updated in May 2026, similarly emphasizes bounding agent powers, meaningful human accountability, and technical controls throughout the agent lifecycle.

TENIR-Gov addresses one implementation layer within that broader landscape:

> **the runtime decision about whether a proposed transition is admissible under the active control policy.**

It does not claim that runtime admissibility alone constitutes complete AI governance.

---

# Product Architecture

TENIR-Gov has one deterministic Core with additional control and analysis capabilities around it.

```text
                         TENIR-Gov
                 THE EXECUTION CONTROL PLANE
                              │
             ┌────────────────┴────────────────┐
             │                                 │
             ▼                                 ▼
   CONSTITUTIONAL CORE                CONTROL & VALUE MODULES
             │                                 │
   ┌─────────┼─────────┐              ┌────────┼─────────┐
   │         │         │              │        │         │
   ▼         ▼         ▼              ▼        ▼         ▼
Policy    Admissibility   Merkle     NSL      VPS       EVP
Engine     Decision       Ledger    Policy  Forensics  Analytics
   │
   ▼
PASS / FLAG / HARD_VETO
   │
   ▼
Execution Gateway
   │
   ▼
Consequential Action
```

## Core

The Core provides:

* deterministic policy evaluation;
* admissibility scoring;
* `PASS / FLAG / HARD_VETO` outcomes;
* configurable thresholds;
* execution-gating semantics;
* append-only cryptographic decision records; and
* API access.

The lightweight implementation is located under:

```text
tenir-kernel/
```

The kernel is designed to remain independently deployable from the broader middleware surface.

## Modules

The repository also contains additional components for policy authoring, validation, forensic analysis, integration, and value-protection workflows.

These modules should not be interpreted as changing the authority of the Core.

In particular:

> **EVP consumes governance records read-only. It has no write path back into the Core and cannot alter an admissibility verdict.**

This separation prevents downstream valuation or analytics signals from becoming an implicit control input to the execution decision.

---

# Core Decision Model

The R5.0.0 kernel uses:

```text
S = K / (P × V + ε)
```

Where:

| Symbol | Meaning                                         |
| ------ | ----------------------------------------------- |
| `K`    | Current system capacity                         |
| `P`    | Action pressure                                 |
| `V`    | Action volatility / consequence exposure        |
| `ε`    | Stability floor; canonical R5.0.0 value: `1e-6` |
| `S`    | Calculated admissibility score                  |

The formula is an implementation construct of the R5.0.0 policy model. It should not be interpreted as a universal measure of safety, risk, or organizational capacity.

### Kernel verdicts

For the demonstrator policy profile:

| Condition                          | Kernel verdict | Full middleware disposition |
| ---------------------------------- | -------------- | --------------------------- |
| `S ≥ flag_below`                   | `PASS`         | `allow`                     |
| `hard_veto_below ≤ S < flag_below` | `FLAG`         | `allow_with_alert`          |
| `S < hard_veto_below`              | `HARD_VETO`    | `block`                     |

Default demonstrator thresholds:

```text
epsilon          = 1e-6
hard_veto_below  = 0.5
flag_below       = 1.2
```

Thresholds are **policy parameters**, not universal constants.

Institutional deployments should calibrate them against their declared operating constraints, action classes, and review requirements.

---

# Fail-Closed Semantics

R5.0.0 is designed around a fail-closed control posture at the kernel decision boundary.

Where the active policy determines that a request falls outside the admissible operating region, the kernel produces:

```text
HARD_VETO
```

A `HARD_VETO` is not a recommendation. It is the kernel-level negative execution disposition under the active policy.

The wider system may additionally implement review, escalation, recovery, or alternative handling according to its deployment profile.

---

# Quick Start

## Core — Standalone Kernel

```bash
cd tenir-kernel
pip install -r requirements.txt
./run.sh
```

The development server starts on port `8000`.

Example:

```bash
curl -X POST http://localhost:8000/api/v1/adjudicate \
  -H "Content-Type: application/json" \
  -d '{
    "actor_id": "operator-1",
    "action_context": "deploy-model",
    "p": 0.7,
    "v": 0.5,
    "k": 1.2
  }'
```

## Full Middleware

```bash
pip install -e .
docker compose up -d --build
```

Example Python usage:

```python
from tenir_governance.sdk import TENIRGovernanceClient, GovernanceEvent

client = TENIRGovernanceClient()

result = client.adjudicate(
    GovernanceEvent(
        pressure=0.7,
        velocity=0.5,
        capacity=1.2
    )
)

print(result.to_business_payload())
```

---

# Repository Architecture

## R5.0.0 · IRON OMEGA

```text
tenir-gov/
│
├── tenir-kernel/                  # Standalone deterministic kernel
│   ├── core/
│   │   ├── policy_engine.py       # S = K / (P × V + ε)
│   │   └── ledger.py              # SHA-256 / Merkle-backed records
│   ├── api/
│   │   └── server.py              # FastAPI adjudication endpoint
│   ├── tenir_policies.yaml        # Kernel policy configuration
│   ├── tests/
│   │   └── test_tenir_kernel.py   # K1–K8 kernel tests
│   ├── requirements.txt
│   └── run.sh
│
├── tenir_governance/              # Full governance package
│   ├── nomenclature.py
│   ├── policy_engine.py
│   ├── validator.py
│   ├── regression_corpus.py
│   ├── sdk.py
│   ├── polymorphic_surface.py
│   ├── copy_lint.py
│   └── ledger_migrate.py
│
├── r4/                            # R4 monitor runtime
├── r5_hardened/                   # R5 hardened runtime
├── r5_wired/                      # R5 integration layer
├── interface/                     # Operational interface
├── tests/                         # Governance-package tests
├── Dockerfile
├── docker-compose.yml
└── .github/workflows/             # CI pipeline
```

---

# API Reference

Start the R5 server:

```bash
docker compose up -d --build
```

or:

```bash
uvicorn r5_hardened.IRON_OMEGA_R5.r5_server:app \
  --host 0.0.0.0 \
  --port 8000
```

| Endpoint                          | Method | Purpose                                    |
| --------------------------------- | ------ | ------------------------------------------ |
| `/api/v1/adjudicate`              | POST   | Submit an intent for governance evaluation |
| `/api/v1/oath/sign`               | POST   | Operator oath/signature operation          |
| `/api/v1/transition`              | POST   | Operating-mode transition                  |
| `/api/v1/ledger/verify`           | GET    | Verify ledger integrity                    |
| `/api/v1/ledger/proof/{entry_id}` | GET    | Retrieve an inclusion proof                |
| `/health`                         | GET    | Service health                             |
| `/ws/vps`                         | WS     | VPS runtime stream                         |

Endpoint availability depends on the selected runtime/profile.

---

# Policy Profiles

## Kernel Demonstrator Profile

| Parameter         |  Value |
| ----------------- | -----: |
| `epsilon`         | `1e-6` |
| `hard_veto_below` |  `0.5` |
| `flag_below`      |  `1.2` |

## Full Middleware

| Profile     | Factory                              | Purpose                  |
| ----------- | ------------------------------------ | ------------------------ |
| `default`   | `PolicyEngine.default()`             | Canonical baseline       |
| `partner_a` | `PolicyEngine.um6p_shadow_v4()`      | Shadow-monitor profile   |
| `partner_b` | `PolicyEngine.ocp_sovereign_pilot()` | Industrial pilot profile |

Default policy fingerprint:

```text
d083e0b82a16c04d
```

Profiles are deployment configurations. They should not be interpreted as universal TENIR policy values.

---

# Auditability

TENIR-Gov records governance decisions using an append-only cryptographic ledger structure.

The R5 implementation provides:

* SHA-256 hashing;
* chained records;
* Merkle-based integrity structures;
* ledger verification; and
* inclusion-proof support.

The ledger provides **cryptographic evidence of record integrity**.

It does not, by itself, prove that a policy was correct, that an input was truthful, or that an action was appropriate. Those are separate governance and assurance questions.

---

# Test Suite

R5.0.0 contains separate validation scopes.

| Suite            | Count | Scope                                           |
| ---------------- | ----: | ----------------------------------------------- |
| Kernel           |     8 | Core formula, policy, ledger and API validation |
| Full middleware  |   547 | `tenir_governance` unit/integration coverage    |
| R4 monitor       |    61 | R4 monitor runtime                              |
| Repository total |   557 | Published R5 validation set                     |

The reported coverage figure is:

```text
96% statement coverage
```

for the `tenir_governance` package under the defined test configuration.

Coverage does **not** imply that every runtime component, deployment configuration, integration, or operational environment has equivalent coverage.

Run the kernel tests:

```bash
cd tenir-kernel
pytest tests/ -v
```

Run the full validation configuration:

```bash
pytest tests/ r4/tests/ \
       r5_hardened/IRON_OMEGA_R5/test_r5_all.py \
       r5_hardened/IRON_OMEGA_R5/test_institutional_hardening.py \
       r5_wired/test_r5_governance_integration.py \
       --cov=tenir_governance \
       --cov-report=term-missing
```

---

# CI Validation

The repository includes policy and invariant validation:

```bash
tenir-validate --policy default
tenir-validate --policy partner_a --json
tenir-validate --policy partner_b
```

The validation command checks the selected policy against the repository's defined invariants.

Conformance to these checks should not be interpreted as proof of organizational governance maturity or production suitability.

---

# Performance

R5.0.0 includes an internal benchmark performed on:

```text
AMD Ryzen 9 7950X
64 GB DDR5
Ubuntu 22.04
10,000 sequential requests
localhost
minimal JSON payload
```

Reported measurements:

| Metric                |   Value |
| --------------------- | ------: |
| Mean latency          | 12.4 ms |
| Median / P50          | 10.1 ms |
| P95                   | 24.7 ms |
| Ledger write overhead |  4.2 ms |
| NSL parsing success   |  99.98% |

These measurements are **benchmark observations, not SLAs**.

Results will vary with hardware, payload size, policy complexity, storage, network topology, concurrency, and deployment architecture.

The repository does not use the benchmark to claim a universal throughput guarantee.

---

# Experimental and Historical Components

The repository contains components originating from earlier R4/R5 development stages and experimental runtime work.

These include:

* R4 monitoring components;
* R5 hardened runtime components;
* VPS runtime surfaces;
* CES state modelling;
* additional policy and graph integrations.

Their presence in the repository does not mean that every component is part of the minimal deterministic kernel or has the same implementation status.

The **kernel and full middleware should therefore be distinguished from experimental, integration, and historical components** when evaluating R5.0.0.

---

# CES State Model

The repository contains a Cognitive Engagement System (CES) state model:

```text
REST
  ↓
METABOLIZING
  ↓
TENSION
  ↓
SIGNAL_CONFLICT
  ↓
COLLAPSE
```

with a recovery path from `SIGNAL_CONFLICT` toward `METABOLIZING`.

CES is an internal/experimental governance-state model in the R5 repository. It is **not required to understand or execute the core admissibility calculation**.

| State             | Description                              |
| ----------------- | ---------------------------------------- |
| `REST`            | Stable state                             |
| `METABOLIZING`    | Pressure being absorbed / monitored      |
| `TENSION`         | Elevated attention state                 |
| `SIGNAL_CONFLICT` | Competing signals requiring adjudication |
| `COLLAPSE`        | Critical intervention state              |

---

# Terminology

R5.0.0 replaces several historical internal terms with neutral canonical terminology.

| Historical term       | Canonical R5 term       |
| --------------------- | ----------------------- |
| `SCHIZOPHRENIA`       | `SIGNAL_CONFLICT`       |
| `SCHIZOPHRENIA_ALERT` | `SIGNAL_CONFLICT_ALERT` |
| `WHALE_RESONANCE`     | `DEEP_PATTERN_SIGNAL`   |

Backward-compatible aliases are preserved where required for existing records and compatibility.

---

# Integration Position

TENIR-Gov is designed to complement adjacent infrastructure.

| Layer                 | Primary question                                       | Typical function                 | TENIR-Gov distinction                                                |
| --------------------- | ------------------------------------------------------ | -------------------------------- | -------------------------------------------------------------------- |
| **Identity / IAM**    | Who is allowed to request?                             | Authentication, authorization    | TENIR evaluates the runtime admissibility of the proposed transition |
| **API Gateway**       | Can the request reach the service?                     | Routing, rate limiting           | TENIR evaluates the action under active policy/state                 |
| **Policy Engine**     | Does the request satisfy declared rules?               | Rule evaluation                  | TENIR adds a runtime admissibility layer                             |
| **LLM Guardrails**    | Is generated content/action representation acceptable? | Content and model-level controls | TENIR provides an execution-boundary decision                        |
| **Observability**     | What happened?                                         | Monitoring and telemetry         | TENIR can decide before execution and record the decision            |
| **Execution Control** | Should this transition become real now?                | Runtime enforcement              | **TENIR-Gov**                                                        |

TENIR-Gov is therefore not positioned as a replacement for these systems.

It is an additional control layer at the **decision → execution boundary**.

---

# What TENIR-Gov Does Not Claim

TENIR-Gov does not claim that:

* the admissibility formula is a universal safety metric;
* a Merkle ledger proves the correctness of a decision;
* deterministic execution control eliminates all agentic-AI risk;
* a `HARD_VETO` guarantees physical prevention in every deployment;
* the kernel replaces identity, authorization, cybersecurity, observability, or enterprise governance;
* benchmark latency represents a production SLA;
* repository test coverage represents complete operational assurance.

These distinctions are intentional.

The purpose of TENIR-Gov is narrower:

> **make the decision to cross from autonomous reasoning into consequential execution an explicit, policy-controlled runtime event.**

---

# Scientific / Research Context

TENIR-Gov is part of the broader TENIR research and engineering program concerning runtime admissibility, autonomous systems, governance boundaries, and preservation of decision integrity under pressure.

The R5.0.0 repository implements the **deterministic governance spine**.

Broader TENIR concepts and ontological structures may appear in research materials and future releases, but they should not be inferred to be implemented by the R5.0.0 kernel merely because they exist in the broader framework.

---

# Citation

If you use TENIR-Gov in research or engineering work, cite the Zenodo release:

```text
Skiredj, A. (2026).
TENIR-Gov: Governance Middleware for AI-Enabled Operational Systems.
Zenodo.
https://doi.org/10.5281/zenodo.21277138
```

DOI:

```text
https://doi.org/10.5281/zenodo.21277138
```

A machine-readable citation is also provided through `CITATION.cff`.

---

# License

Apache License 2.0.

See [LICENSE](LICENSE).

Copyright © 2026 Abdelaziz Skiredj / TENIR Labs
