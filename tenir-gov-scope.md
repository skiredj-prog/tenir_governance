# TENIR-Gov — Scope Declaration
**Status:** Normative for v5.7.0
**Date:** 2026-10-30
**Reference:** REG-ARCH v0.1, §2.2 (Lanes), §5 (Gates), §9 (Exteriority)

TENIR-Gov is a **REG-ARCH Lane B runtime**. It implements the gates governing the transition from a proposed action to its external effect.

## What TENIR-Gov implements (Lane B)
| Gate | Governed passage | Refusal condition |
|---|---|---|
| **G-05 ADMIT** | Intention → situated authorization | Authorization conditions unsatisfied |
| **G-06 ONCE** | Authorization → reservation / key consumption | Key already consumed or concurrent control not guaranteed |
| **G-07 HOLD** | Indeterminate outcome → qualified outcome | Insufficient elements for reconciliation |
| **G-11 Receipt** | Qualified record → receipt | Durable record or scope missing |

## What TENIR-Gov does not implement
Lane A (claim admission), Lane C (representation), Méta (dossier), Extériorité E1 (provider reconciliation). The absence is a scope boundary enforced by REG-ARCH-002.

## Multi-host status (v5.7.0)
Multi-host Raft consensus (C3) is **not claimed**. The runtime refuses to start if `HOST_SCOPE = multi-host` is declared (REG-ARCH-022).
