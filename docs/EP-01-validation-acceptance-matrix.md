# EP-01 validation acceptance matrix

Status: experimental specification, no runtime enforcement.

| Case | Trusted signature | Standing | Effective time | Conflict | Required disposition |
|---|---|---|---|---|---|
| EP-01 | yes | not established | current | none | HOLD |
| EP-02 | yes | unauthorized | current | none | REJECT |
| EP-03 | yes | authorized | future | none | HOLD |
| EP-04 | yes | authorized | current | competing valid claims | HOLD and preserve both claims |
| EP-05 | yes | authorized | current | none | Candidate acceptance, subject to commit binding |

Authenticity, scope authority, and effective-time policies need independently trusted provenance. A caller-provided boolean must not count as verified signature. No claim of native conformance or production readiness.
