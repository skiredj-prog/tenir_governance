# 5-Minute Developer Gate

Synthetic lost-response recovery that operationalizes:
- knows when not to act
- evidence-bound recovery
- single authoritative dispatcher and budget owner

## Run

```bash
python experience/5min_gate.py
```

Expected output includes `"outcome": "CONFIRMED"` and a correlated receipt. The second dispatch is blocked.

## Timeline (after preflight)

| Elapsed | What happens |
|---------|--------------|
| 0–1 min | Authorized synthetic booking, response deliberately lost |
| 1–2 min | UNKNOWN (authorization succeeded, outcome unconfirmed) |
| 2–3 min | Retry blocked — no second dispatch |
| 3–4 min | Correlated receipt recovers the outcome |
| 4–5 min | Exported record is inspectable |

Clock starts when preflight passes. Local, no credentials.

This is a scaffold. It demonstrates the single-dispatcher rule and the recovery path. It is not yet wired to the full TENIR-Gov kernel or Workbench.
