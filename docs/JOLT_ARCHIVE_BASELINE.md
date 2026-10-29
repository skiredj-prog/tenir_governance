# Reproducible laboratory baseline for the Jolt-Witness corrections

This is an audit source snapshot, not a release candidate or a production
authority. The Governor is not connected to the experimental dispatch adapter.

Input archive: `Auditer les failles du mécanisme Jolt-Witness.zip`.
Archive SHA-256:
`8bab352ef0d87a86981d99bd93389b05b9f0684228f8bc6b00f86df7827f4217`.

Reconstruction starts at `17e7bd45cd0abae5cd0eed57e9beb96891b72e8b`,
with the complete Python files supplied in the archive overlaid on their
repository paths. `independent_witness.py` is the file identified by blob
`6bc044647b2c48c80ed1a6b0fe4129d07a4ee76e` in the supplied patch.
The other patch fragments have not been applied over the complete files.

The baseline retains the audit's safety assertions. On Windows with CPython
3.12 and pytest 9.2.0, the supplied targeted suites produced 20 PASS, 4 FAIL,
2 SKIP. Two failures were open provider connections at temporary-directory
cleanup; two were outdated cross-boundary tests missing `resource_id`.
The skips require POSIX SIGKILL. The safety corpus produced 2 PASS and 8 FAIL
across 10 cases; two failures cover the same expiry defect.

Known open findings include: command/actual-provider argument divergence
(JW-01), public I8 settlement bypass (JW-02), repeated pending dispatch
(JW-03), review/dispatch interleaving (JW-04), Kernel rollback producing a
second execution identity (JW-05), unrelated witness evidence accepted
(JW-06), and expiry before lock acquisition (JW-07).

The snapshot and all subsequent corrective commits keep Integration Lock,
E2E and production on HOLD until the joint guarantees are demonstrated.
Do not attribute historical or later test results to this commit.
