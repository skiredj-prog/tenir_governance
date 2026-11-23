# Caller contract: STALE and CONTRADICTORY measurements

Status: candidate documentation for 5.7.7, not released. It states a declared
limit of this runtime: TENIR-Gov does not detect STALE or CONTRADICTORY
measurements, and depends on its caller for them. Written on 2026-11-23 under
the author's decision of that day ("explicit caller contract now, native
interface later"). Each disposition below was observed by a script on 5.7.6 as
published (`1881c90`) and on this candidate; none is inferred.

## 1. What the interface can and cannot express

`DualKernelGovernor.adjudicate`, `DualKernelGovernor.resolve_hold` and
`DualKernelCompositionClient.adjudicate` take one value per measurement:
`pressure`, `velocity`, `capacity`, `option_space`, `authority`, `context_fit`,
`evidence_quality`, and `irreversibility_materiality` (omitted, it is an
unknown measure since the decision of 23 November 2026: HOLD unless a
conclusive veto governs). There is no observation time, no source and no
second reading.
The runtime therefore cannot tell a STALE or a CONTRADICTORY measurement
(RFC-2 v0.1 §2.5) from a current one. What it handles is UNKNOWN: a value that
is not a finite number (invalid), or `None` (absent). A finite value outside
the kernel's declared domain `[0, 1]` is refused at entry as `INVALID_ARGUMENT`
(P8, 23 November 2026), it is not an unknown measure.

Detecting STALE and CONTRADICTORY, and translating them before the call, is the
caller's responsibility. TENIR-Gov makes no claim about STALE or CONTRADICTORY
measurements. A deployment that claims RFC-2 §2.5 behaviour for them claims it
for its own caller layer, and has to show that layer.

## 2. What the caller must do

1. **Declare**, with the deployment and before any call, a freshness window
   and a contradiction tolerance for each measurement.
2. **STALE** (older than its window): do not pass the value. Pass the
   measurement as UNKNOWN (point 4).
3. **CONTRADICTORY** (two or more valid readings that differ beyond the
   tolerance): do not choose a reading at the moment of the call. Pass the
   measurement as UNKNOWN, unless the deployment declared beforehand a
   deterministic resolution rule (RFC-2 v0.1 §2.5); then apply that rule and
   record it (point 5). Applying it is part of the deployment's declaration,
   not a choice made at the call.
4. **Pass UNKNOWN as absent**: `None` in process, `null` on a JSON transport.
   Observed dispositions, every other input at its best value:

   | Measurement | 5.7.6, as NaN | 5.7.6, as None | 5.7.7 candidate, as NaN | 5.7.7 candidate, as None |
   |---|---|---|---|---|
   | pressure, velocity | HOLD | HARD_VETO | HOLD | HOLD |
   | capacity | HOLD | HARD_VETO, TAU BREACH stated on placeholder 0.0 | HOLD | HOLD |
   | authority, context_fit, evidence_quality | HOLD | HARD_VETO | HOLD | HOLD |
   | option_space | HOLD | HOLD | HOLD | HOLD |

   On this candidate, `None` and NaN give the same disposition on every
   measurement: HOLD for insufficient evidence, unless a conclusive veto
   established by the other measurements governs (HARD_VETO). On 5.7.6, `None`
   gave HARD_VETO, terminal for the attempt, on six of the seven: a caller of
   5.7.6 has to pass `float("nan")` in process to obtain the HOLD.
5. **Record the translation** with the operation's idempotency key: the
   original readings, their observation times and sources, the window or
   tolerance applied, and the rule. The runtime's ledger can only say
   "absent measure(s) capacity" or "invalid measure(s) capacity", never why.

The HOLD that follows keeps its guarantees: nothing is dispatched while the
measurement is unknown, no budget is reserved while it is unknown (the
reservation is taken at the re-adjudication that completes the evidence, under
I8 at that moment, so a caller cannot lock a budget by passing UNKNOWN), a
declared `τ_K` fails the attempt closed at exhaustion, and the attempt
resolves only by `resolve_hold` with a fresh,
consistent measurement, which re-runs both kernels. Without a policy
`hold_tau_k_seconds`, no bound is invented: the HOLD and its reservation stay
open until resolved, escalated or exited.

## 3. What the caller must not do

Observed on 5.7.6 for capacity (qualification of 22 November 2026, group E of
the K-priority replay); the candidate decides these rows the same way, since
the runtime cannot see what the caller hid:

| Caller's choice | Result |
|---|---|
| stale reading 0.0 passed as current | HARD_VETO |
| stale reading 0.9 passed as current | FLAG: admitted, one effect |
| contradictory 0.0 and 0.9, caller passes the lower | HARD_VETO |
| contradictory 0.0 and 0.9, caller passes the higher | FLAG: admitted, one effect |

The two admissions leave no trace in the runtime that anything was stale or
contradictory.

## 4. Limits

- This is a contract on the caller. TENIR-Gov neither enforces nor observes it.
- Low confidence is a separate input (`evidence_quality`) and is not covered.
- The REG harness port for TENIR-Gov declares no STALE or CONTRADICTORY
  degradation, so the property checks PX-REG-STATE-002 and PX-REG-STATE-004
  stay ADAPTER_REQUIRED. A port could implement the translation of §2 and run
  them; the result would then test the caller's translation together with the
  runtime's handling of UNKNOWN, and would have to say so. Not implemented.
- A native interface, where the runtime receives for each measurement an
  observation time with a declared freshness window, the source, and every
  reading, is planned for a later version. Not implemented.
