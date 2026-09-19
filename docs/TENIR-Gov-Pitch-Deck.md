# TENIR-Gov — Pitch Deck
### Runtime admissibility for autonomous AI agents
**September 2026 · TENIR**

---

## 1. The problem

AI agents can increasingly **reason, decide and trigger actions** autonomously.

The missing layer is not another model or another policy document. It is a runtime mechanism that can determine, **at the moment of execution**, whether a proposed action remains admissible given the live state of the system.

**Authorized ≠ admissible.**

An action can satisfy an initial authorization and still become unacceptable when context, pressure, velocity, capacity or stability changes.

---

## 2. The proposition

**TENIR-Gov is a runtime layer that turns agent autonomy into admissible autonomy.**

The separation is deliberate:

**Agent / model → proposes the action**  
**TENIR-Gov membrane → evaluates execution admissibility**  
**System → executes, suspends or blocks**

The current product is focused on the **execution boundary**, not on replacing the agent, policy engine, IAM or enterprise governance stack.

---

## 3. The architecture

### Level 1 — Runtime / Core
Real-time execution admissibility.

- PVKS: Pressure, Velocity, Capacity, Stability
- Admissibility score: Aₜ = K / (P × V + ε)
- Verdicts: **PASS / FLAG / HARD_VETO**
- Verifiable execution trace through a Merkle register
- Current status: **defined, implemented and tested**

### Level 2 — EPV / Value Protection
Asynchronous measurement of what controlled execution protects.

- Exposure and protected-value measures
- Institutional / operational effects
- **AFRI**: Approval Fatigue Reduction Index
- Current status: **framework formalized; empirical calibration in progress**

### Level 3 — Systemic impact
Longer-horizon measurement.

- Carbon impact
- System-level effects
- Cryptographic evidence of integrity
- Current status: **research / validation phase**

The three levels are intentionally kept separate by maturity.

---

## 4. Product today

TENIR-Gov is already a functional and instrumented product.

**Current measured indicators:**

- **557 automated tests**
- **96% code coverage**
- **12.4 ms mean decision time**

The current development focus is not simply adding features. It is strengthening the evidence around **where the membrane works, how it behaves under varied conditions, and how its higher-level value can be measured.**

---

## 5. From control to measurable value

TENIR-Gov creates a chain of evidence:

**Action proposed**  
→ **admissibility evaluated**  
→ **PASS / FLAG / HARD_VETO**  
→ **execution or suspension**  
→ **protected value / exposure measured**

EPV is the framework for measuring this second-order value.

AFRI focuses on one specific question:

> Can autonomous execution reduce repetitive human approvals while preserving human intervention where it matters?

These metrics are being treated as **calibration subjects**, not as already-proven business outcomes.

---

## 6. Validation program

The next phase is built around real cases rather than assumptions.

**Test → Measure → Calibrate → Document**

Priority work:

- Real-world agentic AI use cases
- Repeatable test protocols
- Core runtime behaviour
- EPV / AFRI calibration
- Integration feedback
- Evidence pack with comparable results

The objective is to establish a clear distinction between **demonstrated capability, calibrated metrics and hypotheses still requiring validation.**

---

## 7. Why now / why Morocco

Morocco is building significant AI, digital and sovereign infrastructure.

TENIR-Gov addresses a complementary question:

**How do organizations retain decision control as autonomous systems become capable of acting at operational speed?**

The ambition is to develop and validate the technology from Morocco, build evidence with African use cases, and confront the model with international markets and standards.

---

## 8. What we are building toward

TENIR-Gov is being developed as a **runtime admissibility infrastructure** for autonomous agents.

The near-term goal is not to claim that every higher-level impact is already proven.

It is to build the chain of evidence:

**Runtime control → Value protection → Systemic impact**

with each step measured at the appropriate level of maturity.

**TENIR**  
*Keep the system capable of deciding when the conditions change.*

---

### Contact
**Abdelaziz Skiredj**  
Strategic Architect & Digital Transformation Leader  
TENIR
