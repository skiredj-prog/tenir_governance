# Security Policy

## Supported Versions
| Version | Supported          |
| ------- | ------------------ |
| R5.0.0  | :white_check_mark: |
| < R5.0  | :x:                |

## Threat Model & Cryptographic Boundaries
TENIR-Gov is designed as a foundational governance middleware (Canon 1.0). 
- **Fail-Closed Initialization:** The system requires an explicit `OATH_SECRET` environment variable. It will fail to initialize if this is missing, preventing forged mode-transition oaths.
- **Replay Attacks:** Nonces are persistently recorded in the Merkle ledger and recovered on startup to prevent replay attacks across the Polymorphic Adjudication Surface.
- **Out of Scope:** The current R5.0.0 release utilizes a single-node Merkle ledger. While this provides cryptographically verifiable local tamper-evidence, true non-repudiation against a fully compromised host (e.g., root access to the server) is out of scope for this release and requires external anchoring (planned for future releases).

## Reporting a Vulnerability
Please report security vulnerabilities by opening a confidential security advisory on GitHub or contacting the maintainers directly. Do not open public issues for unpatched vulnerabilities.
