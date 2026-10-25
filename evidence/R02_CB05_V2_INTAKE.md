# R-02 / CB-05 evidence intake — archive v2
**INTEGRITY CHECKED / SAME OPERATOR / NO INDEPENDENT REPRODUCTION / PRODUCTION HOLD**

Source uploaded to the review conversation: `r02_evidence_full_v2.zip`.

- Archive SHA-256: `c41e49dcd685451562dfcd35da9493b46e4b372956754ae433451c2e0faf1d47`
- ZIP entries: **503**; uncompressed size: **6,003,446 bytes**
- ZIP entries with absolute paths or `..` traversal: **none detected**
- Top-level `manifest.sha256`: **2/2** listed files matched.
- `evidence/cb05_ep02/manifest.sha256`: **3/3** listed files matched.
- These manifests are **partial**; they do not individually cover all 503 ZIP entries. The archive SHA-256 covers the whole received ZIP.

## Results read from `evidence/cb05_ep02/run/results.json`
SUT: `9d8bf1c462589ac497e3e03ece48d1d9ab0b9db3`.

- **C1 FAIL_REPRODUCED (same operator)**: revision 2 advance, stale revision 1 control `REJECTED/TRUST_CHECKPOINT_STALE`; restored revision 1 authority permits `AUTHORIZED/COMMIT_AUTHORIZED`, sequence 1; local receipt verifies under store-pinned key.
- **C2 FAIL_REPRODUCED_WHOLE_FILE_REPLACEMENT (same operator)**: roots-only edit refused (`trust profile digest mismatch`); removed guards refused (`immutable-row guards are missing`); unchanged historical rows with recreated guards refused (`invalid durable request row`); fresh attacker-provisioned authority opens, accepts attacker-signed snapshot and reports attacker's receipt key as trusted.
- **C3 PINNED_DIGEST_API_ABSENT (static)**: inspected signatures of `DurableAnchorCAS.open/provision` and `ProfileSCommitAuthority.open/provision`; no external profile digest parameter. Not proof that no other application-layer pinning exists.
- Honest external verifier pin rejection was **not tested**.

## Provenance
`evidence/cb05_ep02/provenance.md` reports Linux 6.18 x86_64, Python 3.12.3, SQLite 3.45.1; harness reused SUT fixtures; operator was the **same assistant session** as R-02/EP-02; harness corrections (column and trigger handling) preceded final results. No second host, independent operator, crash/power-loss or honest-pin verification.

## Repository transfer limitation
This branch contains the **verified intake index**, not the 503 raw archive entries. The connected GitHub file-writing interface supports UTF-8 text files, not binary ZIP or SQLite artifacts; raw archive contents have therefore **not been uploaded to GitHub**. Do not label this branch a complete evidence deposit. Keep the original ZIP unchanged and retain its SHA-256; transfer raw evidence through an authenticated binary-capable workflow before asserting repository evidence completeness.

Related: CB-05 Draft PR #77; R-02 issues #76 and #78. No SUT changes.
