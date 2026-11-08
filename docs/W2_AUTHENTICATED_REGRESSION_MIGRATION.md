# W2: authenticated regression migration and bounded entrypoint refusals

This change migrates the 13 historical failing W2 cases to native authenticated
claims. It does not restore unclaimed provider execution, promote historical
ExperimentalG4AAuthority, or close global B1 / REG W4 / Governor isolation.

## Baseline and invariants

Base: `310065ccbdb7a23e4c86d74ae680ed4c292e8a10`, after merging #122 then
#121 into `fix/b1-governor-uid-lab`. Its tree is
`46f811fd870d13f0bca31227f4a3d28be3c31ca7`, identical to the previously tested
`5edfe6ca06746551f36fa5a1f487de195ef9ca7d`. Exact-SHA results are kept
separate from that tree comparison.

The previous full Linux run [42456415211](https://github.com/skiredj-prog/tenir_governance/actions/runs/42456415211)
had 1713 PASS, 13 FAIL, 16 SKIP, 1 strict XFAIL. All 13 failed test names are
retained in this change. None is skipped or converted to XFAIL.

| Historical failing cases | Current positive-path evidence |
| --- | --- |
| W2.1 restart persistence and two terminal-state protections | Authenticated native dispatch, lost ACK, injected settlement failure, recovery, signed receipt; original RC-01 terminal assertions |
| W2.1 idempotency conflict | Native outbox refuses the second command/key association before a second provider effect |
| W2.2 positive sequence, key restart, completed intent | Native signed permit/grant, stable external test key, one reservation; no implicit promotion of an old permit |
| W2.2 claim recovery and process death after claim | Same execution ID/token after restart; a committed but unsent native claim can send once before expiry |
| W2.2 expired consumed claim | Existing confirmed receipt replays after expiry; an additional negative case forbids a first send from an expired pending claim |
| W2.2 competing consumers | Two spawned processes acknowledge readiness; one confirmation, one idempotency conflict, one claim and send event |
| W2.2b cross-boundary hash and divergent hash | Evaluation, permit, outbox and verified B3 receipt agree; divergent request cannot execute |

The old test that expected an expired claim to initiate its first effect is
not retained as a permitted behavior. It is replaced by both receipt replay
and explicit first-send refusal. This is an intentional contract correction,
not a claim of unchanged availability. Native reservation+permit admission is
atomic; the old half-issued permit scenario is now an interruption before
the separately issued positive grant. Neither change claims that Governor
JSON, SQLite and a provider form one transaction.

All SQLite handles and child processes owned by the migrated fixtures are
closed. Corrupt-schema constructor tests retain the partially initialized
object to close its handle. Spawned process targets are module-level and
use readiness barriers and bounded joins, including on Windows.

## Eight measured entrypoints

Every row has an ordinary negative test in
`w2_integration/test_w2_entrypoint_refusals.py` and a separate mutation
campaign running the **same assertions**.

| ID | Entry | Refused input and pre-state | Independent observation / local invariants |
| --- | --- | --- | --- |
| W2-E01 | HTTP POST /api/v1/effects/execute | Invalid bearer grant signature, no native admission | Actual connector MockTransport receives no POST or GET; no reservation, native outbox or HTTP queue is created |
| W2-E02 | EffectContractLab.run_once | Corrupt native permit signature after valid queued admission | Stripe MockTransport receives zero requests; existing reservation and HTTP queue unchanged; no native outbox/send created |
| W2-E03 | NativePositiveG4A.dispatch_to_b3 | Invalid grant signature after valid admission | B3 observer receives zero requests; reservation unchanged and no native outbox |
| W2-E04 | ProofProvider.execute | Missing claim, no admission | B3 observer receives zero requests; native budget/outbox unchanged; no B3 receipt |
| W2-E05 | KernelClaimProofProvider.execute | Missing claim, no admission | Same checks, without accepting a substitute verifier |
| W2-E06 | Historical PositiveG4A.consume_and_dispatch | Old signed permit, no native claim | Zero B3 observer requests; existing I8 retained, old permit not consumed, no dispatch intent created |
| W2-E07 | Historical PositiveG4A.recover / _repair_dispatch_claim | Previously persisted legacy claim, no native capability | Zero B3 observer requests; existing reservation and pending legacy row unchanged, no automatic replay |
| W2-E08 | Historical run_lab_s1_scenario | Assumed grant, no native authorization | Zero B3 observer requests; no legacy budget or RC-01 files/rows created |

For E01/E02 the observer is the connector's actual `httpx.MockTransport`.
The reconstructed B3 provider has no network boundary: E03–E08 install a
test-only MockTransport observer at its synthetic effect/receipt generation
boundary, after authentication but before ledger storage. This is not claimed
to be an observation of a real external payment. The observer is independent
of TENIR databases, and mutations demonstrate its sensitivity.

Zero new reservations is not the same as deleting an existing reservation.
E02/E03/E06/E07 begin with explicit pre-existing state. Their refusal must leave
that state unchanged; automatic release or erasure would violate the test.
Legacy admission itself is **not** claimed to require the Governor.

Two new pre-fix reproductions exposed writes despite eventual provider refusal:
E06 consumed the legacy permit and created a dispatch intent; E08 reserved I8
and projected DISPATCHING. Both now refuse at entry, before those writes.
Legacy dispatch/recovery bodies are kept below an unconditional refusal solely
as historical migration code and for controlled test mutations; no application
configuration enables them. Pending legacy records require manual review.

## Mutation campaign

Run ordinary guards first, then:

```bash
python scripts/verify_w2_mutations.py evidence
```

The runner sets `W2_TEST_MUTANT=1` **only in a test subprocess**. No application
module reads this variable. It expects pytest exit 1 and exactly the eight
listed AssertionErrors, with no setup error, skip, missing case or unexpected
failure accepted. Raw mutation failures are retained in JUnit and logs.

Mutants: omit HTTP grant verification (E01), omit native permit signature
verification (E02), omit native positive grant verification (E03), disable the
provider claim check (E04/E05), and remove the initial legacy refusal
(E06/E08). E07 removes both the legacy recovery refusal and the lower provider
refusal to test the recovery boundary independently. Six mutants cause an
observable provider request; two cause forbidden admission/intent writes.
These expected failures are **not** included as failures of the normal suite.

## Reproduction and limits

Install the source test environment:

```bash
python -m pip install -e '.[api,test,raft,ep02]'
export PYTHONPATH="$PWD:$PWD/w2_integration"
export TENIR_ENV=test HOST_SCOPE=single-host
python -m pytest -q -ra -o addopts= w2_integration/test_w2_1.py w2_integration/test_w2_2_positive.py w2_integration/test_w2_2b_complete.py w2_integration/test_w2_entrypoint_refusals.py
python scripts/verify_w2_mutations.py evidence
python -m pytest -q -ra -o addopts= tests/ w2_integration/
```

The workflow checks out the exact PR head, installs Raft dependencies, runs
`pip check`, records dependencies/SHA, and retains separate ordinary and
mutated JUnit reports on Linux and Windows. SIGKILL remains POSIX-only; the
new process-exit variant also executes on Windows. No test count from one
scope should be added to an overlapping suite.

Acceptance wording, only after same-SHA validation:
**“Unauthenticated execution is refused for the eight listed entrypoints,
with zero observed mock requests and no new reservation or pending send;
pre-existing reservations are retained.”**

Do not write “all paths are closed.” The old signed UID service's
Governor/quarantine bypass remains a strict XFAIL. Register line #8
(Governor isolation) remains refuted/open. Credentials, privileged writers,
direct SQL, older binaries, global B1, B2 and production are not qualified by
this PR. No main merge, deployment, tag or publication is implied.
