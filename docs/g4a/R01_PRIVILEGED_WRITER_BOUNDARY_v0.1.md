# R-01 — Privileged writer boundary: acceptance contract v0.1

Status: **DRAFT / RED-first / NOT PRODUCTION QUALIFIED**. Baseline G4-A `9d8bf1c462589ac497e3e03ece48d1d9ab0b9db3`. Scope: POSIX single-host disposable SQLite lab only. R-04 closed for its experimental test scope; unrelated to privileged SQL isolation.

## Claim boundary
The G4-A connection-local `tenir_g4a_write` guard and triggers exclude **ordinary uninstrumented SQL clients**. They do **not** exclude a principal able to register the same SQLite function, execute DDL, replace the DB file, or execute hostile Python in the trusted process. Restart-time I8 detection is not prevention of the write. No global capture claim is admissible.

## Authority and threat inventory (required before any extended claim)
Record: UID/GID; executable/process identity; DB and directory ownership/modes/ACL; SQLite connection capabilities; ability to register UDF; DDL privileges; filesystem replace/rename; access to quarantine sidecars and locks; recovery privileges; writer inventory independently discovered vs declared. State which actors are inside the trusted computing base. If privileged actor is in scope without effective OS isolation, result is **FAIL for exclusivity**, not OUT_OF_SCOPE.

## Acceptance criteria
- R01-A: ordinary client without guard UDF cannot mutate protected budget, current, commands, or outbox; durable snapshot unchanged.
- R01-B: explicitly privileged client can be demonstrated to bypass the SQL guard in current baseline; preserve as a **counterexample**, not a passing security test.
- R01-C: after privileged mutation, reopen/inspect fails closed where corruption is observable; never claim rollback of an already committed effect.
- R01-D: for any future claim covering hostile writers, independent OS identity/permission tests must deny modification, DDL, UDF forgery *and* file replacement; document permitted administration/recovery.
- R01-E: writers outside the declared boundary are OUT_OF_SCOPE only if excluded **before execution**; any extension requires fresh inventory and tests.

## Negative test vectors
| ID | Injection | Required oracle | Current expectation |
|---|---|---|---|
| R01-N01 | Ordinary SQL UPDATE of reserved/committed | SQL refusal; unchanged DB | PASS candidate; rerun |
| R01-N02 | Ordinary SQL INSERT/UPDATE outbox/current | Refusal; unchanged DB | NOT RUN |
| R01-N03 | Register spoofed `tenir_g4a_write` UDF then change budget | If write commits, FAIL of privileged exclusivity; restart detection separately | KNOWN COUNTEREXAMPLE |
| R01-N04 | Privileged DROP TRIGGER then mutate | If write commits, FAIL of privileged exclusivity | NOT RUN |
| R01-N05 | Same-UID file replacement / DB copy | If replacement succeeds, FAIL of file-level exclusivity | NOT RUN |
| R01-N06 | Independent writer inventory discovers undeclared path | HOLD / FAIL of completeness claim | NOT RUN |

Record exact commit, environment, UID/GID, file metadata, SQL statement, pre/post SQLite snapshots, restart result, event sequence, and immutable artifact hashes. Never mutate production data.

**Gate:** No extension to real dispatch, production, multi-host, or privileged writer coverage without enforced authority boundary and independent review. FM-20 remains open.
