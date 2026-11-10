# Getting Started

## 5-minute developer gate

The fastest way to see the core property (knows when not to act + evidence-bound recovery):

```bash
python experience/5min_gate.py
```

What you should observe:

- An authorized synthetic booking whose response is lost
- Outcome remains UNKNOWN
- A retry is blocked (no second dispatch)
- A correlated receipt resolves the outcome
- An inspectable export is produced

Clock starts after any preflight. Local, no credentials.

See [experience/README.md](experience/README.md) for the timeline.

## Identity

TENIR-Gov is the maintained product. HOLD is the historical prototype. See [DECISION_HOLD_TENIR_GOV_IDENTITY_LOCK.md](DECISION_HOLD_TENIR_GOV_IDENTITY_LOCK.md).
