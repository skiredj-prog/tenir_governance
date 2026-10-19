# Security Policy

## Supported Versions

| Version / Line                         | Supported | Notes |
|----------------------------------------|-----------|-------|
| 5.4.x (Agentic Governance Profile)     | ✅        | Current release line on PyPI. Includes two-kernel composition, provider integration, HOLD lifecycle, and opt-in τ⁻¹ / EVP post-execution assessment. |
| 5.3.x (Agentic Governance Profile)     | ✅        | Supported prior line. Includes two-kernel composition, DurableOperationRegistry, and schema 1.2 conformance; it does not include the v5.4.x assessment layer. |
| 5.2.x (TENIR 2.0 kernel + R5 hardening)| ✅        | Still supported. |
| 5.1.x / 5.0.x (IRON OMEGA R5)          | ⚠️ Limited | Security fixes only; migrate to 5.2+ recommended. |
| Pre-R5 / 4.x and earlier               | ❌        | No longer supported. |

Only the latest patch release within a supported minor line receives security updates.

Install from PyPI with an explicit version pin in production:

```bash
pip install "tenir-governance==5.4.3"
# or with extras
pip install "tenir-governance[api]==5.4.3"
pip install "tenir-governance[r5]==5.4.3"
```

## Reporting a Vulnerability

**Do not open public GitHub issues for unpatched vulnerabilities.**

Preferred channels (in order):

1. GitHub Security Advisories (confidential) — open a private advisory against the repository.
2. Direct contact with maintainers (see `CITATION.cff` / `pyproject.toml`).
3. If the above are unavailable, email the primary maintainer with the subject line `[SECURITY]`.

Please include:

- Affected version(s) and component(s) (kernel, composition layer, membrane, registry, R5 HTTP surface, PyPI package metadata, etc.)
- Clear reproduction steps or proof-of-concept (if safe to share)
- Impact assessment (confidentiality, integrity, availability, or governance bypass)
- Whether the issue is already being exploited
- How the package was obtained (PyPI, GitHub source, wheel hash, etc.)

We will acknowledge receipt within 5 business days and aim to provide an initial assessment within 14 days. Coordinated disclosure is preferred; we request a reasonable embargo period (typically 30–90 days) depending on severity and complexity.

## Threat Model Overview

TENIR-Gov is an **execution-control plane** that sits between agent decision-making and consequential action. It enforces deterministic, fail-closed admissibility decisions and produces tamper-evident records.

### In Scope

- Runtime adjudication (scoring kernel + transition membrane)
- Fail-closed composition of continuous risk signals and discrete invariants
- Local durability of operation state (idempotency key claim, dispatch state machine)
- Integrity of the local hash-chained / Merkle decision ledger
- Correct enforcement of mandatory invariants
- Schema conformance and disclosure metadata
- Integrity of the published PyPI artifacts (sdist / wheel) corresponding to a signed Git tag

### Out of Scope (Explicit Non-Guarantees)

TENIR-Gov does **not** provide, and must not be relied upon as, the sole source of:

- Network-level protection (TLS termination, mTLS, firewalling, DDoS mitigation)
- Identity & Access Management (per-principal authentication, fine-grained authorization, credential rotation)
- Distributed consensus or multi-host durability
- Exactly-once delivery of external side-effects (depends on the downstream provider's idempotency support)
- Protection against a fully compromised host, privileged administrator, or supply-chain attack on the runtime or the Python packaging ecosystem itself
- Content moderation, prompt injection defense, or model-level safety
- Compliance certification (SOC 2, ISO 27001, EU AI Act conformity assessment, etc.)

Deployments **must** place TENIR-Gov behind infrastructure that supplies the missing controls.

## PyPI / Supply-Chain Considerations

Because the package is distributed via PyPI, the following additional controls and limitations apply:

### Trusted Installation Practices

- Always pin the exact version in production requirements or lock files.
- Prefer installing from a verified wheel or sdist whose hash matches the release artifacts published on GitHub (when available).
- Use a private package index, Artifactory, or `pip` with `--require-hashes` / `uv` lockfiles for high-assurance environments.
- Verify the package name is exactly `tenir-governance` (hyphen) to avoid typosquatting.
- Review the project metadata on PyPI (homepage, repository, DOI, maintainer) before first use.

### What the Project Does and Does Not Guarantee on PyPI

| Property                              | Status / Scope                                      |
|---------------------------------------|-----------------------------------------------------|
| Source code matches a Git tag         | Intended; verify via GitHub release assets          |
| Wheel / sdist integrity               | Relies on PyPI + TLS; project may publish hashes    |
| Reproducible builds                   | Not formally claimed; best-effort from tagged source|
| Automated Trusted Publishing          | Recommended for maintainers (GitHub Actions → PyPI) |
| Signature / attestations (Sigstore)   | Not currently claimed; may be added in future       |
| Protection against PyPI account compromise | Out of scope for the library itself             |

Maintainers should enable **Trusted Publishing** (OIDC) from GitHub Actions to PyPI so that releases do not require long-lived API tokens.

### Optional Extras and Dependency Surface

- Core package depends only on `jsonschema`.
- `[api]` and `[r5]` extras pull in FastAPI, Pydantic, uvicorn, Neo4j driver, etc.
- Treat optional extras as part of the attack surface: pin them and monitor advisories for those transitive dependencies.

## Component-Specific Boundaries

### Dual-Kernel Composition (v5.3.0+)

- Both kernels always execute. Exceptions or missing data produce `HOLD`, never `PASS`.
- Mandatory membrane invariants cannot be skipped by a healthy scoring result.
- Decision lattice is fail-closed by construction.
- Guarantee scope is **local** (single process / single host).

### DurableOperationRegistry

- Idempotency key is claimed **before** any kernel evaluation.
- State machine: `CREATED → HOLD → RESERVED → DISPATCHING → CONFIRMED | FAILED | UNKNOWN | ESCALATED`.
- Atomic claim of the `DISPATCHING` transition (at-most-once on a single host).
- Durability model: file-backed store with atomic rename + directory fsync + `fcntl.flock`.
- **Limitations**:
  - Single-host only. `fcntl.flock` does not coordinate across hosts.
  - No write-ahead log. A crash during persistence can lose the most recent in-memory transition (recovery path via `resume_existing()` exists).
  - Budget coordinator remains in-process (`threading.Lock`). Cross-process budget safety is not provided by the registry alone.
  - **In-memory default**: If `store_path` is not provided, the registry is in-memory only. Idempotency claims are lost on restart. Use `require_persistence=True` or pass `store_path` in production.

### Local Ledger (Hash Chain / Merkle)

- Provides **tamper evidence**, not resistance against a compromised host or administrator.
- Neo4j (when used) is a projection only; it is never the authoritative transition ledger.
- External anchoring, peer replication, or consensus are out of scope.

### R5 HTTP / API Surface

- Requires `TENIR_API_TOKEN`, `OATH_SECRET`, and `TENIR_OPERATOR_ID` at startup.
- API token is a shared bearer credential, not per-user IAM.
- Mode transitions are operator- and oath-bound, ordered through `SHADOW_CRITICAL` before `ENFORCE`, and recorded in the local ledger before the live mode changes.
- Docker bindings default to localhost. Production deployments must not expose the service directly to untrusted networks.
- Legacy R5 "wired" mode can accept `ENFORCE` transitions; treat it with the same operator, oath, and network controls as the hardened surface.

## Cryptographic & Integrity Properties

| Property                        | Mechanism                              | Scope / Limitation                          |
|---------------------------------|----------------------------------------|---------------------------------------------|
| Decision integrity              | SHA-256 hash chain / Merkle structure  | Local evidence only                         |
| Operation durability            | Atomic rename + fsync + file lock      | Single host                                 |
| Fail-closed composition         | Explicit lattice + exception handling  | Local                                       |
| Mandatory invariant enforcement | TransitionMembrane (I3–I9 family)      | Local                                       |
| Schema & disclosure integrity   | `conform_to_schema()` + ProfileDisclosure | Prevents authority over-claim             |
| Package distribution integrity  | PyPI + TLS (+ optional hashes)         | Depends on packaging ecosystem              |

No claim is made of resistance to a privileged attacker who can modify process memory, the filesystem, or the underlying OS.

## Deployment Recommendations

1. **Pin versions** — Always install a specific release from PyPI (`tenir-governance==5.4.3`).
2. **Network isolation** — Bind to localhost or a private interface; terminate TLS at a reverse proxy.
3. **Secrets management** — Inject tokens and secrets via a secrets manager; never hard-code.
4. **Least privilege** — Run under a dedicated low-privilege user; restrict filesystem permissions on ledger and registry store.
5. **Monitoring** — Alert on unexpected `HARD_VETO`/`HOLD` rates, ledger verification failures, and registry recovery events.
6. **Backup & recovery** — Regularly back up the ledger and registry store. Test the `resume_existing()` / `recover()` paths.
7. **Multi-host** — Replace the file-backed registry and in-process budget coordinator with a transactional store that provides cross-host atomicity.
8. **Supply-chain hygiene** — Use lock files / hash pinning, monitor advisories for core and optional dependencies, prefer Trusted Publishing for releases.

## Guarantee Boundary Summary (v5.4.3)

| Guarantee                                      | Level                                      |
|------------------------------------------------|--------------------------------------------|
| At-most-once dispatch through the governance gate | Local (in-process / single-host)        |
| Fail-closed composition                        | Local                                      |
| Mandatory invariants not skippable             | Local                                      |
| Atomic budget reservations                     | Local (`threading.Lock`)                   |
| Cross-process serialization (single host)      | Local (`fcntl.flock`)                      |
| Cross-host budget / idempotency coordination   | Requires distributed infrastructure        |
| Exactly-once external effects                  | Depends on downstream provider             |
| Integrity of PyPI artifacts                    | Relies on packaging ecosystem + verification practices |

## Contact & Updates

This policy is versioned with the project. Significant changes to the threat model, guarantee boundaries, or distribution practices will be noted in the CHANGELOG and, where appropriate, announced via a security advisory.

For non-security questions, open a regular issue or discussion.
