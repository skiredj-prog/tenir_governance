"""Operations layer — budget coordination, durable registry, HOLD lifecycle,
and dispatch provider abstraction.

Profile: agentic-governance-profile-v1.0.0
Compliance class: profile-bound
"""
from .budget import BudgetCoordinator, InProcessBudgetCoordinator
from .registry import (
    DurableOperationRegistry, OperationRecord, OperationState,
    HoldRecord, ExitReceipt,
)
from .hold import HoldManager
from .provider import (
    DispatchProvider, DispatchResult, ProviderOutcome,
    ReconciliationResult, MockProvider, StripeProvider,
)
from .sqlite_backend import SQLiteConsensusBackend
from .._profile_meta import DISCLOSURE

__all__ = [
    "BudgetCoordinator", "InProcessBudgetCoordinator", "DISCLOSURE",
    "DurableOperationRegistry", "OperationRecord", "OperationState",
    "HoldRecord", "ExitReceipt", "HoldManager",
    "DispatchProvider", "DispatchResult", "ProviderOutcome",
    "ReconciliationResult", "MockProvider", "StripeProvider",
    "SQLiteConsensusBackend",
]
