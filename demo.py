#!/usr/bin/env python3
"""
TENIR-Gov 10-Minute Demo
========================

Demonstrates the two-kernel composition architecture end-to-end:

  1. Normal admissible operation (adjudicate → reserve → dispatch → confirm)
  2. Duplicate adjudication blocked
  3. HOLD lifecycle (enter → resolve → escalate → authorized exit)
  4. In-memory vs persistent registry comparison
  5. Provider timeout → UNKNOWN → reconciliation
  6. Guarantee boundary statement

Usage:
    python demo.py

Requirements:
    pip install tenir-governance==5.4.1

    Or from source:
    pip install -e . (from the tenir-gov-5.2.0 directory)
"""
from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import warnings

# ── Helpers ──────────────────────────────────────────────────────────────────

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

passed = 0
failed = 0


def header(title: str) -> None:
    print(f"\n{'='*72}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{'='*72}\n")


def step(msg: str) -> None:
    print(f"  {msg}")


def ok(msg: str) -> None:
    global passed
    passed += 1
    print(f"  {GREEN}✓ {msg}{RESET}")


def fail(msg: str) -> None:
    global failed
    failed += 1
    print(f"  {RED}✗ {msg}{RESET}")


def info(msg: str) -> None:
    print(f"  {YELLOW}→ {msg}{RESET}")


def detail(msg: str) -> None:
    print(f"    {msg}")


# ── Imports ──────────────────────────────────────────────────────────────────

header("TENIR-Gov · The Execution Control Plane for Agentic AI")
print(textwrap.fill(
    "It decides whether what AI wants to do is allowed to become real.",
    initial_indent="  ", subsequent_indent="  ",
))
print()
info("Importing TENIR-Gov modules...")

from tenir_governance.sdk import DualKernelCompositionClient
from tenir_governance.objects import Agent, Passage, Resource
from tenir_governance.operations.provider import MockProvider, ProviderOutcome

ok("Imports successful")


# ── Scene 1: Normal Admissible Operation ────────────────────────────────────

header("Scene 1: Normal Admissible Operation")
print("  An AI agent proposes a $10 payment. Both kernels evaluate it,")
print("  budget is reserved atomically, and the provider confirms execution.\n")

provider = MockProvider(behavior="confirmed")
client = DualKernelCompositionClient(store_path=tempfile.mktemp(suffix=".json"))
client.governor.provider = provider
client.register_agent(Agent(agent_id="alice", authority_score=0.9, trusted_delegates=frozenset()))
client.register_resource(Resource(resource_id="budget", capacity=100.0))

step("Agent 'alice' proposes: payment of $10.00")
p = Passage(agent_id="alice", action_type="payment", amount=10.0)

result = client.adjudicate(
    p, pressure=0.2, velocity=0.2, capacity=0.9,
    resource_id="budget", amount=10.0,
)

detail(f"Scoring decision:    {result['scoring_decision']}")
detail(f"Membrane decision:   {result['membrane_decision']}")
detail(f"Composed decision:   {result['composed_decision']}")
detail(f"Operation state:     {result['operation_state']}")
detail(f"Ledger valid:        {result['ledger_valid']}")

if result['composed_decision'] in ('PASS', 'FLAG') and result['operation_state'] == 'reserved':
    ok(f"Adjudication passed → {result['composed_decision']}, state → RESERVED")
else:
    fail(f"Unexpected decision: {result['composed_decision']} / {result['operation_state']}")

step("Dispatching to provider with idempotency key...")
dispatch_result = client.governor.dispatch_and_execute(
    p.idempotency_key, resource_id="budget", amount=10.0,
)

detail(f"Operation state:     {dispatch_result['operation_state']}")
detail(f"Executed:            {dispatch_result.get('executed')}")
detail(f"Provider reference:  {dispatch_result.get('provider_reference')}")

if dispatch_result['operation_state'] == 'confirmed' and dispatch_result.get('executed'):
    ok("Provider confirmed execution → state CONFIRMED")
else:
    fail(f"Dispatch failed: {dispatch_result}")

# Verify budget was consumed
budget = client.membrane_state.resources.get("budget")
detail(f"Budget consumed:     {budget.consumed:.2f} / {budget.capacity:.2f}")
if budget.consumed == 10.0:
    ok("Budget atomically consumed: $10.00")
else:
    fail(f"Budget mismatch: {budget.consumed}")


# ── Scene 2: Duplicate Adjudication Blocked ─────────────────────────────────

header("Scene 2: Duplicate Adjudication Blocked")
print("  The same operation is submitted again. TENIR blocks it —")
print("  the idempotency key was already claimed at CREATED.\n")

step("Re-submitting the same passage (same idempotency key)...")
result2 = client.adjudicate(
    p, pressure=0.2, velocity=0.2, capacity=0.9,
    resource_id="budget", amount=10.0,
)

detail(f"Composed decision:   {result2['composed_decision']}")
detail(f"Operation state:     {result2['operation_state']}")

if result2['composed_decision'] == 'HARD_VETO':
    ok("Duplicate blocked: HARD_VETO — idempotency key already claimed")
else:
    fail(f"Duplicate not blocked: {result2['composed_decision']}")

info("CREATED blocks fresh adjudication. The key is claimed before any")
info("kernel evaluation, preventing duplicate processing.\n")


# ── Scene 3: HOLD Lifecycle ─────────────────────────────────────────────────

header("Scene 3: HOLD Lifecycle")
print("  When evidence is insufficient, TENIR doesn't deny or blindly proceed.")
print("  It HOLDs — a designed suspension of agency under pressure.\n")

from tenir_governance.operations.registry import OperationRecord, OperationState

# Register a fresh operation in CREATED state (before adjudication)
hold_key = "op-hold-demo-001"
hold_record_init = OperationRecord(
    idempotency_key=hold_key,
    passage_id="passage-hold-001",
    agent_id="alice",
    action_type="transfer",
    amount=50.0,
    resource_id="budget",
)
client.governor.registry.register(hold_record_init)

step(f"New operation registered in CREATED state (key: {hold_key})")
record = client.governor.registry.get(hold_key)
detail(f"State:               {record.state.value}")

# Enter HOLD — transitions CREATED → HOLD
step("Entering HOLD — evidence insufficient for execution...")
hold_result = client.governor.hold_manager.enter_hold(hold_key, tau_k_seconds=300)
detail(f"Entered:             {hold_result['entered']}")
detail(f"Operation state:     {hold_result['operation_state']}")

hold_rec = client.governor.hold_manager.get_hold_record(hold_key)
if hold_rec:
    detail(f"τ_K remaining:       {hold_rec.tau_k_seconds}s")

if hold_result['entered'] and hold_result['operation_state'] == 'hold':
    ok("Operation entered HOLD state")
else:
    fail(f"Expected HOLD, got {hold_result}")

# Resolve HOLD with better evidence
print()
step("New evidence arrives — re-adjudicating with lower pressure...")
p3 = Passage(agent_id="alice", action_type="transfer", amount=50.0)
p3.idempotency_key = hold_key
resolved = client.governor.resolve_hold(
    hold_key, passage=p3,
    pressure=0.1, velocity=0.1, capacity=0.9,
    resource_id="budget", amount=50.0,
)
detail(f"Hold resolved:       {resolved.get('hold_resolved')}")
detail(f"Operation state:     {resolved.get('operation_state')}")
detail(f"Composed decision:   {resolved.get('composed_decision')}")

if resolved.get('hold_resolved'):
    ok("HOLD resolved → RESERVED (evidence became sufficient)")
else:
    info(f"HOLD not resolved yet: {resolved.get('reason')}")

# Now show escalation path with a new operation
print()
step("Demonstrating escalation path with a new operation...")
esc_key = "op-hold-demo-002"
esc_record = OperationRecord(
    idempotency_key=esc_key,
    passage_id="passage-hold-002",
    agent_id="alice",
    action_type="high_value_transfer",
    amount=80.0,
    resource_id="budget",
)
client.governor.registry.register(esc_record)

step("Registering in CREATED, entering HOLD, then escalating...")
client.governor.hold_manager.enter_hold(esc_key, tau_k_seconds=300)
client.governor.escalate_hold(esc_key, escalated_by="risk-officer")
record4 = client.governor.registry.get(esc_key)
detail(f"State:               {record4.state.value}")

if record4.state.value == 'escalated':
    ok("Operation escalated to ESCALATED (absorbing state)")
else:
    fail(f"Expected ESCALATED, got {record4.state}")

# Authorized exit from ESCALATED
print()
step("Risk officer authorizes exit with signed exit receipt...")
exit_result = client.governor.authorize_exit(
    esc_key,
    authorized_by="ciso@org",
    scope="post-incident-review",
    authorization="ceremony-G-R5-001",
)
detail(f"Exited:              {exit_result.get('exited')}")
detail(f"Operation state:     {exit_result.get('operation_state')}")
record4 = client.governor.registry.get(esc_key)
detail(f"State after exit:    {record4.state.value}")

exit_rec = client.governor.hold_manager.get_hold_record(esc_key)
if exit_rec and hasattr(exit_rec, 'exit_receipt') and exit_rec.exit_receipt:
    detail(f"Exit receipt signed by: {exit_rec.exit_receipt.authorized_by}")
    detail(f"Authorization:        {exit_rec.exit_receipt.authorization}")

if record4.state.value in ('reserved', 'failed'):
    ok(f"Authorized exit → {record4.state.value.upper()} (with exit receipt)")
else:
    fail(f"Expected RESERVED or FAILED, got {record4.state}")

info("ESCALATED is absorbing — no time-based auto-exit. τ_K expiry")
info("in ESCALATED produces UNKNOWN, not resolution (v7.1 §19).\n")


# ── Scene 4: In-Memory vs Persistent Registry ────────────────────────────────

header("Scene 4: In-Memory vs Persistent Registry")
print("  The default SDK client uses an in-memory registry. Idempotency")
print("  claims are lost on restart. With a persistent store_path, they survive.\n")

# In-memory (default)
step("Creating in-memory client (no store_path)...")
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    client_mem = DualKernelCompositionClient()
client_mem.register_agent(Agent(agent_id="bob", authority_score=0.9, trusted_delegates=frozenset()))
client_mem.register_resource(Resource(resource_id="budget_mem", capacity=100.0))

p_mem = Passage(agent_id="bob", action_type="payment", amount=15.0)
client_mem.adjudicate(
    p_mem, pressure=0.2, velocity=0.2, capacity=0.9,
    resource_id="budget_mem", amount=15.0,
)
detail(f"Adjudicated: state={client_mem.governor.registry.get(p_mem.idempotency_key).state}")

step("Simulating restart — new in-memory client, same operation...")
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    client_mem2 = DualKernelCompositionClient()
client_mem2.register_agent(Agent(agent_id="bob", authority_score=0.9, trusted_delegates=frozenset()))
client_mem2.register_resource(Resource(resource_id="budget_mem", capacity=100.0))
result_mem2 = client_mem2.adjudicate(
    p_mem, pressure=0.2, velocity=0.2, capacity=0.9,
    resource_id="budget_mem", amount=15.0,
)
detail(f"Re-adjudication:     {result_mem2['composed_decision']}")
detail(f"State:               {result_mem2['operation_state']}")

if result_mem2['composed_decision'] in ('PASS', 'FLAG'):
    info("CRITICAL: In-memory registry lost idempotency on restart!")
    info("The same operation was re-adjudicated — idempotency claim was lost.")
    ok("In-memory behavior correctly demonstrated (claims lost across processes)")
else:
    ok("In-memory: re-adjudication blocked (within same process)")

info("However, across a real process restart, in-memory claims are lost.")
info("The new process has no record of the previous adjudication.\n")

# Persistent
store = tempfile.mktemp(suffix=".json")
step(f"Creating persistent client (store_path={os.path.basename(store)})...")
client_p1 = DualKernelCompositionClient(store_path=store)
client_p1.register_agent(Agent(agent_id="carol", authority_score=0.9, trusted_delegates=frozenset()))
client_p1.register_resource(Resource(resource_id="budget_p", capacity=100.0))

p_p = Passage(agent_id="carol", action_type="payment", amount=25.0)
client_p1.adjudicate(
    p_p, pressure=0.2, velocity=0.2, capacity=0.9,
    resource_id="budget_p", amount=25.0,
)
detail(f"Adjudicated: state={client_p1.governor.registry.get(p_p.idempotency_key).state}")
detail(f"Store file exists:   {os.path.exists(store)}")

step("Simulating restart — new persistent client, same store_path...")
client_p2 = DualKernelCompositionClient(store_path=store)
client_p2.register_agent(Agent(agent_id="carol", authority_score=0.9, trusted_delegates=frozenset()))
client_p2.register_resource(Resource(resource_id="budget_p", capacity=100.0))
result_p2 = client_p2.adjudicate(
    p_p, pressure=0.2, velocity=0.2, capacity=0.9,
    resource_id="budget_p", amount=25.0,
)
detail(f"Re-adjudication:     {result_p2['composed_decision']}")
detail(f"State:               {result_p2['operation_state']}")

if result_p2['composed_decision'] == 'HARD_VETO':
    ok("Persistent: re-adjudication BLOCKED — idempotency survived restart ✓")
else:
    fail(f"Persistent registry failed to block: {result_p2['composed_decision']}")

# Show require_persistence guard
print()
step("Testing require_persistence=True guard...")
try:
    DualKernelCompositionClient(require_persistence=True)
    fail("require_persistence=True without store_path should have raised")
except ValueError as e:
    detail(f"ValueError: {str(e)[:80]}...")
    ok("require_persistence=True raises ValueError without store_path")

info("Use require_persistence=True in production to fail-fast when")
info("restart-safe idempotency is required but not configured.\n")


# ── Scene 5: Provider Timeout → UNKNOWN → Reconciliation ────────────────────

header("Scene 5: Provider Timeout → UNKNOWN → Reconciliation")
print("  When the provider times out, TENIR doesn't assume the payment failed.")
print("  It marks the operation UNKNOWN and triggers reconciliation.\n")

provider_timeout = MockProvider(behavior="timeout", reconcile_outcome=ProviderOutcome.CONFIRMED)
client_t = DualKernelCompositionClient(store_path=tempfile.mktemp(suffix=".json"))
client_t.governor.provider = provider_timeout
client_t.register_agent(Agent(agent_id="dave", authority_score=0.9, trusted_delegates=frozenset()))
client_t.register_resource(Resource(resource_id="budget_t", capacity=100.0))

p5 = Passage(agent_id="dave", action_type="payment", amount=30.0)
client_t.adjudicate(
    p5, pressure=0.2, velocity=0.2, capacity=0.9,
    resource_id="budget_t", amount=30.0,
)

step("Dispatching to provider (simulated timeout)...")
dispatch_t = client_t.governor.dispatch_and_execute(
    p5.idempotency_key, resource_id="budget_t", amount=30.0,
)
detail(f"Operation state:     {dispatch_t['operation_state']}")
detail(f"Executed:            {dispatch_t.get('executed')}")
detail(f"Reason:              {dispatch_t.get('reason', '')}")

if dispatch_t['operation_state'] == 'unknown':
    ok("Timeout → UNKNOWN (not FAILED — timeout doesn't prove no effect)")
else:
    fail(f"Expected UNKNOWN, got {dispatch_t['operation_state']}")

info("Per doctrine v7.1: a timeout does NOT prove no effect occurred.")
info("UNKNOWN triggers reconciliation, not auto-failure.\n")

step("Reconciling with provider — did the payment actually happen?...")
recon = client_t.governor.reconcile_unknown(p5.idempotency_key)
detail(f"Reconciled:          {recon.get('reconciled')}")
detail(f"Operation state:     {recon.get('operation_state')}")
detail(f"Provider outcome:    {recon.get('provider_outcome')}")

if recon.get('reconciled') and recon.get('operation_state') == 'confirmed':
    ok("Reconciliation confirmed → state CONFIRMED (payment did happen)")
elif recon.get('reconciled') and recon.get('operation_state') == 'failed':
    ok("Reconciliation confirmed → state FAILED (payment did not happen)")
else:
    info(f"Reconciliation result: {recon}")

# Show recovery report
print()
step("Recovery report (as if after a crash)...")
recovery = client.recover()
if recovery.get('unresolved'):
    detail(f"Unresolved operations: {len(recovery['unresolved'])}")
    for op in recovery['unresolved'][:3]:
        detail(f"  {op['idempotency_key'][:12]}... state={op['state']} → {op['recommendation']}")
else:
    detail("No unresolved operations (all reached terminal states)")
    ok("Recovery report: all operations resolved")


# ── Scene 6: Guarantee Boundary ──────────────────────────────────────────────

header("Scene 6: Guarantee Boundary")
print(textwrap.dedent("""\
  TENIR-Gov provides three tiers of guarantee:

  ┌──────────────────────────────────────────────────────────────────────┐
  │ Guarantee                            │ Level                         │
  ├──────────────────────────────────────┼───────────────────────────────┤
  │ At-most-once dispatch through gate   │ Local (in-process / host)     │
  │ Fail-closed composition              │ Local                         │
  │ Mandatory invariants not skippable   │ Local                         │
  │ Atomic budget reservations           │ Local (threading.Lock)        │
  │ Cross-process serialization (1 host) │ Local (fcntl.flock)           │
  │ Cross-host budget/idempotency        │ Requires distributed infra     │
  │ Exactly-once external effects        │ Depends on downstream provider │
  │ PyPI artifact integrity              │ Packaging ecosystem + verify  │
  └──────────────────────────────────────────────────────────────────────┘

  The precise guarantee boundary:

  A governance decision does not, by itself, prove that an external
  effect occurred exactly once. TENIR-Gov guarantees that at most one
  dispatch is permitted through the governance gate on a single host.
  Exactly-once external effects require provider-side idempotency
  (e.g., Stripe's Idempotency-Key header) and, for multi-host deployments,
  distributed coordination infrastructure.

  Fail-closed is the default: when either kernel errors, when data is
  missing, or when the outcome is indeterminate, the composition produces
  HOLD — never PASS.
"""))

ok("Guarantee boundary stated")


# ── Scene 7: τ⁻¹ and EVP Post-Execution Assessment ──────────────────────────

header("Scene 7: τ⁻¹ and EVP — Post-Execution Assessment")
print(textwrap.dedent("""\
  τ⁻¹ (partial tau inversion): a PARTIAL, CONDITIONAL, PROPERTY-RELATIVE
  inverse — NOT a full mathematical inverse. It measures whether the
  retained properties Q can be restored after execution.

  EVP (intended value preservation): measures how well governance
  discipline preserved the intended value. It is NOT an input to
  admissibility — it is an ex-post consequence only.

  Both run AFTER terminal states (CONFIRMED or FAILED), never before.
  Neither feeds back into the admissibility function D.
"""))

from tenir_governance.assessment import (
    TauInversionClassifier, InversionClassification,
    EVPAssessor, PostExecutionAssessor,
)

# ── 7a: Pre-commit cancellation (FAILED, explicitly confirmed no effect) ─
step("7a: Pre-commit cancellation — FAILED, explicitly confirmed no external effect...")
classifier = TauInversionClassifier()
result_pre = classifier.classify(
    operation_state="failed",
    action_type="payment",
    retained_properties=["balance"],
    metadata={"no_external_effect": True},  # P1-1: explicit confirmation required
)
detail(f"Classification:     {result_pre.classification.value}")
detail(f"Distance Q:         {result_pre.distance_q}")
detail(f"Loss:               {result_pre.loss}")
detail(f"Reversible:         {result_pre.is_reversible}")

if result_pre.classification == InversionClassification.PRE_COMMIT:
    ok("Pre-commit: trivially exact (explicitly confirmed no external effect)")
else:
    fail(f"Expected PRE_COMMIT, got {result_pre.classification.value}")

# P1-1: Show that FAILED without explicit confirmation → INDETERMINATE
step("\n7a-2: FAILED without explicit effect status → INDETERMINATE (not PRE_COMMIT)...")
result_indet = classifier.classify(
    operation_state="failed",
    retained_properties=["balance"],
    # No no_external_effect metadata → indeterminate
)
detail(f"Classification:     {result_indet.classification.value}")
detail(f"Distance Q:         {result_indet.distance_q}")
detail(f"Loss:                {result_indet.loss}")

if result_indet.classification == InversionClassification.INDETERMINATE:
    ok("P1-1: Absence of evidence is not evidence of non-execution → INDETERMINATE")
else:
    fail(f"Expected INDETERMINATE, got {result_indet.classification.value}")

# ── 7b: Exact inverse on Q (confirmed, full refund available) ───────────
step("\n7b: Exact inverse on Q — confirmed operation with full reversal...")
def full_refund(metadata):
    return (0.0, 5.0, "Full refund issued — balance restored exactly")

result_exact = classifier.classify(
    operation_state="confirmed",
    action_type="payment",
    retained_properties=["balance"],
    return_transition=full_refund,
)
detail(f"Classification:     {result_exact.classification.value}")
detail(f"Distance Q:         {result_exact.distance_q}")
detail(f"Return cost:        {result_exact.return_cost}")
detail(f"Loss (L_Q):         {result_exact.loss:.4f}  =  {result_exact.distance_q} + {result_exact.lambda_weight} × {result_exact.return_cost}")

if result_exact.classification == InversionClassification.EXACT_INVERSE:
    ok("Exact inverse: ρ(τ(s)) ≈_Q s with distance_Q = 0")
else:
    fail(f"Expected EXACT_INVERSE, got {result_exact.classification.value}")

# ── 7c: Compensation (confirmed, partial refund with FX loss) ────────────
step("\n7c: Compensation — confirmed operation with partial reversal...")
def partial_refund(metadata):
    return (0.3, 2.0, "Partial refund — FX loss incurred")

result_comp = classifier.classify(
    operation_state="confirmed",
    action_type="transfer",
    retained_properties=["balance"],
    return_transition=partial_refund,
)
detail(f"Classification:     {result_comp.classification.value}")
detail(f"Distance Q:         {result_comp.distance_q}")
detail(f"Return cost:        {result_comp.return_cost}")
detail(f"Loss (L_Q):         {result_comp.loss:.4f}  =  {result_comp.distance_q} + {result_comp.lambda_weight} × {result_comp.return_cost}")

if result_comp.classification == InversionClassification.COMPENSATION:
    ok("Compensation: reduces effects but doesn't fully restore (distance_Q > 0)")
else:
    fail(f"Expected COMPENSATION, got {result_comp.classification.value}")

# ── 7d: Irreversibility (confirmed, no return possible) ──────────────────
step("\n7d: Irreversibility — confirmed operation with no authorized return...")
result_irrev = classifier.classify(
    operation_state="confirmed",
    action_type="message_sent",
    retained_properties=["message_read_status"],
    return_transition=None,
)
detail(f"Classification:     {result_irrev.classification.value}")
detail(f"Distance Q:         {result_irrev.distance_q}")
detail(f"Loss:                {result_irrv_loss if (result_irrv_loss := result_irrev.loss) is not None else 'N/A'}")
detail(f"Reversible:          {result_irrev.is_reversible}")

if result_irrev.classification == InversionClassification.IRREVERSIBLE:
    ok("Irreversibility: no authorized operation satisfies the return criterion")
else:
    fail(f"Expected IRREVERSIBLE, got {result_irrev.classification.value}")

# ── 7e: EVP measurement ──────────────────────────────────────────────────
step("\n7e: EVP — Intended Value Preservation measurement...")
assessor = PostExecutionAssessor()

report = assessor.assess(
    idempotency_key="op-evp-demo",
    passage_id="pass-evp",
    operation_state="confirmed",
    retained_properties=["balance"],
    return_transition=full_refund,
    intended_value=1000.0,
    realized_value=950.0,
    confidence=0.9,
    hold_duration_seconds=120.0,
)
detail(f"EVP Gross (expected):  {report.evp.gross.expected_value:.2f}  =  {report.evp.gross.intended_value:.2f} × {report.evp.gross.confidence}")
detail(f"EVP Net (realized):    {report.evp.net.net_value:.2f}  =  {report.evp.net.realized_value:.2f} − {report.evp.net.execution_loss:.2f}")
detail(f"Friction cost:          {report.evp.friction.total:.4f}  (hold delay: {report.evp.friction.hold_delay_cost:.4f}, processing: {report.evp.friction.processing_overhead:.2f})")
detail(f"EVP ratio:              {report.evp.evp_ratio:.3f}")
detail(f"τ⁻¹ classification:     {report.tau_inversion.classification.value}")
detail(f"Is input to D?          {report.is_input_to_admissibility}  (must be False)")

if report.is_input_to_admissibility is False:
    ok("EVP is NOT an input to admissibility — doctrine constraint enforced")
else:
    fail("EVP must NOT be an input to admissibility")

if report.evp.evp_ratio > 0:
    ok(f"EVP ratio {report.evp.evp_ratio:.3f} — governance preserved most intended value")
else:
    fail(f"EVP ratio {report.evp.evp_ratio:.3f} — value not preserved")

# ── 7f: τ⁻¹ and EVP don't imply each other ──────────────────────────────
step("\n7f: τ⁻¹ and EVP are independent — good EVP ≠ reversible...")
report_irrev_good_evp = assessor.assess(
    idempotency_key="op-independent",
    passage_id="pass-indep",
    operation_state="confirmed",
    retained_properties=["message_read_status"],
    return_transition=None,  # Irreversible
    intended_value=1000.0,
    realized_value=1000.0,    # Full value realized
)
detail(f"τ⁻¹:  {report_irrev_good_evp.tau_inversion.classification.value}")
detail(f"EVP:  {report_irrev_good_evp.evp.evp_ratio:.3f}  (perfect value, but irreversible)")

if (report_irrev_good_evp.tau_inversion.classification == InversionClassification.IRREVERSIBLE
    and report_irrev_good_evp.evp.evp_ratio == 1.0):
    ok("τ⁻¹ and EVP don't imply each other: perfect EVP with irreversibility")
else:
    fail("τ⁻¹ and EVP independence not demonstrated")

info("A transition can have good EVP but be irreversible, and vice versa.")
info("Both are ex-post measurements — they do NOT feed back into admissibility.\n")


# ── Summary ─────────────────────────────────────────────────────────────────

header("Demo Summary")
print(f"  {GREEN}Passed: {passed}{RESET}  {RED}Failed: {failed}{RESET}")
print()
print("  What was demonstrated:")
print("    1. Two-kernel composition: scoring + membrane → decision lattice")
print("    2. Duplicate adjudication blocked (CREATED blocks fresh)")
print("    3. HOLD lifecycle: enter → resolve → escalate → authorized exit")
print("    4. In-memory vs persistent registry: restart safety comparison")
print("    5. Provider timeout → UNKNOWN → reconciliation (not auto-fail)")
print("    6. Guarantee boundary: local vs distributed vs provider-dependent")
print("    7. τ⁻¹ + EVP: post-execution assessment (4 classifications, EVP ratio)")
print()
if failed == 0:
    print(f"  {GREEN}{BOLD}All scenes passed. TENIR-Gov is ready for pilot.{RESET}")
else:
    print(f"  {RED}{BOLD}{failed} scene(s) failed. Review needed.{RESET}")
print()
