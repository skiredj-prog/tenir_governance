# Security Policy

## Supported Versions
| Version | Supported |
| ------- | --------- |
| Current main release | :white_check_mark: |
| Pre-R5 releases | :x: |

## Threat Model & Cryptographic Boundaries
The R5 HTTP services require `TENIR_API_TOKEN`, `OATH_SECRET`, and `TENIR_OPERATOR_ID` at startup. The API token is a shared bearer credential; it is not per-user IAM. Mode transitions are oath-bound, operator-bound, ordered through `SHADOW_CRITICAL` before `ENFORCE`, and appended to the local ledger before the live mode changes.

The local hash-chain/Merkle ledger provides tamper evidence, not protection against a compromised host or administrator. Neo4j is a projection, not the authoritative transition ledger. The implementation does not provide TLS termination, token rotation, per-principal identity, rate limiting, external ledger anchoring, or production consensus; deploy behind infrastructure that supplies those controls. Legacy R5 wired mode rejects ENFORCE because it does not accept schema 1.2 objects.

## Reporting a Vulnerability
Please report security vulnerabilities by opening a confidential security advisory on GitHub or contacting the maintainers directly. Do not open public issues for unpatched vulnerabilities.
