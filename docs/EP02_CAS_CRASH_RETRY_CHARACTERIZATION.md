# EP-02 local CAS: process crash and retry characterization

Scope: experimental follow-up to issue #37 and draft PR #36, based on
`1ca735cdfb68b371d8fb63450508eaece05f2bfb`. No governor, A2/A3, budget,
production transport or execution path changes are part of this change.

## Test contract

The existing CI job explicitly runs `tests/test_ep02_local_cas.py`. Eight cases
are added to that module:

| Cases | Assertions |
| --- | --- |
| Three synchronized two-process races | One APPLIED and one CONFLICT; both responses and fresh readback identify the same winning checkpoint, whose fingerprint belongs to one of the two signed successors actually submitted. |
| Before file fsync | The original checkpoint remains visible after process termination; retry can apply the successor. |
| After file fsync, before replace | The original checkpoint remains visible; retry can apply the successor. |
| After replace, before directory fsync | The successor is visible to a fresh process while the host kernel remains running; the legacy retry reports CONFLICT. |
| After directory fsync, before acknowledgment | The successor is visible and the legacy retry reports CONFLICT. |
| Lost acknowledgment followed by another writer | A revision-3 checkpoint remains intact; retry of the revision-2 attempt returns the current conflict without attributing the earlier successful write to that attempt. |

Workers use POSIX `spawn`, an explicit barrier for races, bounded waits and
kill/join cleanup. Each crash hook executes the real write/flush, fsync and/or
replace up to the selected boundary, then calls `os._exit(73)`. A successful
test requires that exit code. A raised Python exception alone cannot satisfy
the crash assertions. Each test uses an isolated store directory.

These are characterization assertions for the existing JSON CAS. They do not
specify durable request idempotency. A future request-ID API needs new failing
acceptance tests and an atomic checkpoint/outcome transaction; it must preserve
the distinction between the historical outcome and the current checkpoint.

## Execution and provenance

The exact parent commit's push workflow `40060152243` completed all eight jobs.
Its logs show 482 executed pytest cases: EP 60, regression 259, governance
sprints 32, R5 58, R5 wired 12, R4 61. The static summary banner saying 445 is
not an executed-test count.

The child branch adds an exact push filter for
`test/ep02-crash-retry-characterization`, so a subsequently authorized push can
run the existing gates on that event's commit. This does not itself establish
remote validation of the child. Record and verify the checkout SHA in the
resulting job logs before claiming such validation.

Local POSIX execution uses WSL Ubuntu with Python 3.14.4, native cryptography
46.0.5 and existing pytest 9.1.1 Python sources appended after native library
paths; no dependencies are installed. Plugin autoload is disabled. The source
archive, test overlay, filesystem, commands, JUnit and environment are recorded
in the separate evidence report. These versions differ from the GitHub runner.
Complete local reproduction of all CI gates requires dependencies absent from
this WSL installation; the targeted EP gate must not be called a full local CI
run.

Run the focused module and the same 13 EP modules selected in `tenir-ci.yml`.
The expected collection sizes after this addition are 11 and 68 respectively;
use actual JUnit results to report executed counts. Do not infer a result from
these expected collection sizes.

## Guarantee boundary and next objective

Process termination leaves the kernel, filesystem and host powered on. Even
the after-replace observation proves no persistence across physical power
loss. Three races provide bounded evidence for those interleavings, not a
universal proof of linearizability. Use a verified local POSIX filesystem for
the persistence characterization; distinguish exploratory tmpfs results.

The prototype still has no durable request ID or result ledger. A lost reply
can leave the caller's outcome UNKNOWN, and current-checkpoint readback cannot
attribute an overwritten historical result. The following remain open:

- One atomic transaction for checkpoint and immutable request outcome;
  same-ID/context retries must recover their original result after later writes.
- Explicit, authenticated bootstrap and root trust; exclude legacy write paths
  from the new authority rather than maintaining two independent writers.
- External monotone authority and trusted high-water storage: restoring all
  locally trusted files together remains undetectable locally.
- Adversarial input, path, permissions and challenge-freshness qualification.
- Cross-host behavior, power-loss testing and any governor/A2/A3 integration.

Authenticated READ does not establish atomic external WRITE. An experimental
APPLIED or preflight ADMISSIBLE result is not production commit authorization.
No merge, tag, release or production integration is authorized by this document.
