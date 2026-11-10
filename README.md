# TENIR-Gov · The Execution Control Plane for Agentic AI

> **Architecture & Provenance Scope**  
> **TENIR-Gov** is the sole maintained product and runtime.  
> **HOLD** (`e1-a6-frozen`) is the archived hackathon prototype; selected recovery patterns are being migrated.  
> **REG** is the specification and qualification framework.  
> Claims must name implementation + version + deployment scope.  
> Upstream/in-path gateways (e.g. Magertron) handle pre-execution policy and authorization. TENIR-Gov holds post-dispatch state: UNKNOWN, duplicate suppression, correlated-receipt or bounded-human recovery, and offline-verifiable journal.  
> See [DECISION_HOLD_TENIR_GOV_IDENTITY_LOCK.md](DECISION_HOLD_TENIR_GOV_IDENTITY_LOCK.md), [docs/HOLD_MIGRATION_MATRIX.md](docs/HOLD_MIGRATION_MATRIX.md), [docs/INTEGRATION_CONTRACT.md](docs/INTEGRATION_CONTRACT.md).

> **It decides whether what AI wants to do is allowed to become real.**

**TENIR-Gov** is an Execution Control Plane for agentic AI. It governs the transition from machine-generated decision or intent to consequential execution — deterministically, at runtime, with cryptographically verifiable decision records.

TENIR-Gov occupies one precise layer: the **runtime boundary between decision and action**.

It does not replace an LLM, an identity system, an observability stack, a policy repository, or an enterprise governance program. It provides a deterministic control surface that can be placed **after reasoning and policy evaluation, and before consequential execution**.

> *AI can reason autonomously. TENIR-Gov controls whether that reasoning is admissible for execution.*

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![Governance CI](https://github.com/skiredj-prog/tenir_governance/actions/workflows/tenir-ci.yml/badge.svg)](https://github.com/skiredj-prog/tenir_governance/actions/workflows/tenir-ci.yml)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.21264802.svg)](https://doi.org/10.5281/zenodo.21264802)

> **v5.7.0 published deployment boundary (2026-10-24):** GitHub release `v5.7.0` points to `47ef67e3efc5f3008896a7d88bebf929fa0a98f0`; version 5.7.0 is available on PyPI. Deployment is bounded to a single host and requires a verified production manifest and durable registry configuration. Production multi-host Raft is blocked while FM-20 remains open; no C3 cross-host fault tolerance or exactly-once external provider effect is claimed. Publication, CI success, and deployment qualification are distinct. The Zenodo series DOI is not a version-specific v5.7.0 archive. See [threat model](docs/threat-model-v1.md) and [changelog](CHANGELOG.md).

---

## Three-Sentence Pitch

1. **Problem:** Models reason and agents propose actions — but a consequential action needs a runtime decision about whether it is admissible before execution.
2. **Solution:** TENIR-Gov provides a deterministic execution-control layer at the decision → action boundary, with explicit policy thresholds and fail-closed defaults.
3. **Evidence and limits:** The v5.7.0 source line includes a two-kernel composition, governed HOLD and provider reconciliation, payload binding, and single-host SQLite WAL budget accounting. The CI checks tests and Python distributions; those checks do not constitute independent certification, prove external provider effects, or establish multi-host fault tolerance (C3).

---

# Quick Start

## 5-minute developer path

See [GETTING_STARTED.md](GETTING_STARTED.md).

```bash
python experience/5min_gate.py
```

Synthetic lost response → UNKNOWN → blocked duplicate → correlated receipt → inspectable export.  
Single dispatcher. Export stamps `implementation`, `version`, `adapter_version`, and `deployment_scope: local-single-host-synthetic`.

## Install

```bash
pip install tenir-governance
```

Or with the composition layer and CLI tools (included by default):

```bash
pip install tenir-governance[test]
```

---

*Full architecture, positioning, and historical sections remain below this Quick Start in the complete file. This update adds the provenance block and the 5-minute path stamp.*
