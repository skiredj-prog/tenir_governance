# EP-02 A2/A3 acceptance review

Review target: `integration/v5.7-ep02-acceptance`, combining the EP-02 Governor
gate with the v5.7 shared-quarantine and settlement-recovery line.

| Scenario | Status on this stack | Evidence or remaining work |
| --- | --- | --- |
| INT-01–02 | Covered | Missing authorization blocks; verified receipt is durable before eligibility. |
| INT-03–07 | Covered | Complete immutable binding substitution corpus plus dispatch-time payload verification. |
| INT-08 | Covered | Stale trust persists the authorization substate as `HOLD`; provider state remains `RESERVED`. |
| INT-09–10 | Covered | Invalid signatures or trust keys fail closed and create durable security events; no fallback. |
| INT-11–17 | Covered | Immutable request IDs, lookup recovery, durable `TRUST_UNKNOWN`, restart and hard-exit authority tests. |
| INT-18 | Covered by existing provider recovery | `DISPATCHING` recovery reconciles and never resends automatically. |
| INT-19 | Covered on the combined candidate | `test_cross_process_quarantine_activation_wins_after_authority_consumption` blocks authority consumption in one spawned process, activates the durable quarantine epoch from another process, and proves that no dispatch claim is obtained. |
| INT-20 | Covered by existing dispatch budget checks | The consumed receipt does not bypass reservation revalidation. Repeat on the final combined candidate. |
| INT-21 | Covered on the combined candidate | `test_provider_confirmation_terminal_write_recovers_with_durable_budget` injects the terminal registry write failure after a confirmed provider effect, observes explicit `DISPATCHING` settlement-pending state with committed budget ownership, then reopens both stores and completes confirmation idempotently. |
| INT-22 | Covered by existing provider recovery | Ambiguous outcomes remain `UNKNOWN`; no timeout-based release or compensation. |
| INT-23–24 | Covered within the EP-02 trust stack | Independent witness rollback is detected; joint authority/witness restoration remains a documented limitation. |
| INT-25 | Covered | Canonical request and payload bounds reject malformed, cyclic, non-finite, ambiguous and oversized inputs. |
| INT-26 | Covered by existing provider terminality | Terminal operations return their existing state and cannot obtain a new dispatch claim. |
| INT-27 | Covered | A real spawned-process integrated race produces one authority consumption and one registry dispatch winner. |
| INT-28 | Outside Profile S | Multi-host authorization belongs to a later central-authority deployment profile; no claim is made here. |
| INT-29 | Covered | Authority and lookup outage persists `TRUST_UNKNOWN`; recovery uses the original request ID and has no local approval path. |
| INT-30 | Covered within signed-trust tests | Legacy unbound issuer keys are rejected by default and require explicit compatibility mode. |

## Release interpretation

This combined candidate closes the Governor-to-Profile-S authorization path
and replays INT-19 and INT-21 against the v5.7 shared-quarantine and durable
settlement implementation. Final release readiness still depends on exact-SHA
CI and review of the complete integration diff. Profile C and multi-host safety
remain outside this release claim, and FM-20 remains open.
