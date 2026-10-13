"""τ⁻¹ — Partial Tau Inversion (post-execution assessment).

Per doctrine (Property-Preserving Crossing for Agentic Systems, pp. 11-12):

    "Nous définissons τ⁻¹ comme un inverse partiel, conditionnel et relatif
     à des propriétés."

τ⁻¹ is a PARTIAL, CONDITIONAL, PROPERTY-RELATIVE inverse — NOT a full
mathematical inverse. In a mathematical system, reversibility means a
determined inverse transition returns to the target state. In an operational
system, this return may be impossible: a message has been read, a decision
has influenced a third party, a deadline has expired, a transaction has been
compensated at market price, or data has been copied.

Formal definition:
    Let Q ⊆ Π be the set of relevant return properties, and ≈_Q an
    equivalence relation on states that compares only those properties.
    For a departure subset D ⊆ R_A, τ⁻¹ exists if, for each state s ∈ D
    and each authorized result s' of τ(s), a return transition ρ exists
    such that:

        ρ = τ⁻¹_Q,  ρ(τ(s)) ≈_Q s

    This definition does NOT claim to restore the entire world state.
    It asserts only the return of the explicitly retained properties Q.
    When multiple effects or results make the transition non-deterministic,
    the existence of an inverse must be established for the set of all
    relevant results, or the limitation is signaled.

The Gateway distinguishes four classifications:
    1. Pre-commit cancellation — the action has not yet produced an
       external effect.
    2. Exact inverse on Q — the inverse operation restores the targeted
       properties.
    3. Compensation — an operation reduces certain effects without
       restoring the initial state.
    4. Irreversibility — no authorized operation satisfies the chosen
       return criterion.

Loss function:
    L_Q(s, τ, ρ) = distance_Q(ρ(τ(s)), s) + λ · cost(ρ)

    where:
    - distance_Q measures how far the returned state is from the original
      on the retained properties Q only
    - cost(ρ) is the cost of executing the return transition
    - λ is a tunable weight balancing fidelity vs cost

CRITICAL: τ⁻¹ is an EX-POST (post-execution) assessment. It runs AFTER
the dispatch state machine reaches a terminal state. It is NOT an input
to the admissibility function D. It does NOT affect PASS/FLAG/HOLD/HARD_VETO.
It does NOT imply EVP, and EVP does not imply it.

Profile: agentic-governance-profile-v1.0.0
Compliance class: profile-bound
This module is an extension on top of KB3 and TENIR 2.0 schema 1.2.
It is NOT canonical, NOT doctrinal. It carries implementation-level
governance enforcement authority only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


class InversionClassification(str, Enum):
    """The τ⁻¹ classifications per doctrine.

    The four doctrine classifications are ordered by decreasing
    reversibility:
        PRE_COMMIT > EXACT_INVERSE > COMPENSATION > IRREVERSIBLE

    This ordering avoids attributing a high reversibility score to an
    action simply because an "undo" button exists (doctrine p. 12).

    An additional INDETERMINATE classification is used when the
    assessment cannot be performed due to insufficient information:
    - The external effect status of a FAILED operation is unknown.
    - The retained property set Q is empty or missing.
    """
    PRE_COMMIT = "pre_commit"
    # The action has not yet produced an external effect.
    # Cancellation is trivially exact — no external state changed.
    # REQUIRES explicit confirmation that no external effect occurred.

    EXACT_INVERSE = "exact_inverse"
    # The inverse operation restores the targeted properties Q.
    # ρ(τ(s)) ≈_Q s holds with distance_Q = 0 (or near-zero).
    # REQUIRES a non-empty Q.

    COMPENSATION = "compensation"
    # An operation reduces certain effects without restoring the initial
    # state. distance_Q > 0 but the return transition is authorized and
    # partially effective.
    # REQUIRES a non-empty Q.

    IRREVERSIBLE = "irreversible"
    # No authorized operation satisfies the chosen return criterion.
    # τ⁻¹ does not exist for the relevant properties Q.

    INDETERMINATE = "indeterminate"
    # The assessment cannot be performed due to insufficient information.
    # This is NOT a classification of reversibility — it means the
    # classifier cannot determine the correct classification.
    # Uses: unknown external effect status, empty/missing Q.

    @property
    def reversibility_rank(self) -> int:
        """Higher = more reversible. Used for comparison, not as a score.

        INDETERMINATE has no rank — it is not a reversibility classification.
        """
        return {
            InversionClassification.PRE_COMMIT: 4,
            InversionClassification.EXACT_INVERSE: 3,
            InversionClassification.COMPENSATION: 2,
            InversionClassification.IRREVERSIBLE: 1,
            InversionClassification.INDETERMINATE: 0,
        }[self]

    @property
    def is_reversible(self) -> bool:
        """True if any form of return is possible (not IRREVERSIBLE).

        INDETERMINATE returns False because the reversibility is unknown,
        not because it is irreversible.
        """
        return self not in (InversionClassification.IRREVERSIBLE,
                            InversionClassification.INDETERMINATE)

    @property
    def is_determined(self) -> bool:
        """True if the classification is a determined assessment.

        INDETERMINATE is not a determined assessment — it means the
        classifier could not determine the correct classification.
        """
        return self != InversionClassification.INDETERMINATE


@dataclass
class TauInversionResult:
    """Result of a τ⁻¹ assessment for a single operation.

    Attributes:
        classification: One of the four InversionClassification values.
        retained_properties: The set Q of properties evaluated for return.
        distance_q: distance_Q(ρ(τ(s)), s) — how far the returned state
            is from the original on the retained properties Q. 0.0 means
            exact restoration on Q. None if no return transition was
            attempted (e.g., IRREVERSIBLE or PRE_COMMIT with no effect).
        return_cost: cost(ρ) — the cost of executing the return transition.
            0.0 for PRE_COMMIT (no return needed). None if no return
            transition exists.
        loss: L_Q(s, τ, ρ) = distance_Q(ρ(τ(s)), s) + λ · cost(ρ).
            The total loss of the best available return transition.
            None if no return transition exists (IRREVERSIBLE).
        lambda_weight: The λ used in the loss calculation.
        rationale: Human-readable explanation of the classification.
        return_transition_description: Description of the return transition
            ρ, if one exists. Empty for IRREVERSIBLE.
        non_determinism_handled: True if the transition was non-deterministic
            and the assessment covered all relevant results s'.
    """
    classification: InversionClassification
    retained_properties: list[str] = field(default_factory=list)
    distance_q: float | None = None
    return_cost: float | None = None
    loss: float | None = None
    lambda_weight: float = 0.1
    rationale: str = ""
    return_transition_description: str = ""
    non_determinism_handled: bool = False

    @property
    def is_reversible(self) -> bool:
        """Convenience: True if any form of return is possible."""
        return self.classification.is_reversible

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification.value,
            "retained_properties": self.retained_properties,
            "distance_q": self.distance_q,
            "return_cost": self.return_cost,
            "loss": self.loss,
            "lambda_weight": self.lambda_weight,
            "rationale": self.rationale,
            "return_transition_description": self.return_transition_description,
            "non_determinism_handled": self.non_determinism_handled,
            "is_reversible": self.is_reversible,
        }


class TauInversionClassifier:
    """Classifies the τ⁻¹ (partial tau inversion) of a completed operation.

    This classifier operates on operations that have reached a terminal
    state in the dispatch state machine (CONFIRMED or FAILED). It does
    NOT participate in admissibility decisions.

    Usage:
        classifier = TauInversionClassifier()
        result = classifier.classify(
            operation_state="confirmed",
            action_type="payment",
            retained_properties=["balance", "audit_trail"],
            return_transition=return_transition_fn,
            lambda_weight=0.1,
        )
        print(result.classification)  # e.g., EXACT_INVERSE
    """

    def __init__(self, lambda_weight: float = 0.1):
        """
        Args:
            lambda_weight: Default λ for the loss function
                L_Q = distance_Q + λ · cost(ρ). Controls the balance
                between fidelity (distance) and cost. Higher λ means
                cost matters more.
        """
        self.default_lambda = lambda_weight

    def classify(
        self,
        operation_state: str,
        action_type: str = "",
        retained_properties: list[str] | None = None,
        return_transition: Callable[..., tuple[float, float, str]] | None = None,
        lambda_weight: float | None = None,
        non_deterministic_results: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TauInversionResult:
        """Classify the τ⁻¹ of a completed operation.

        Args:
            operation_state: The terminal state of the operation
                ("confirmed" or "failed"). If "failed", the operation
                may not have produced an external effect, potentially
                making it PRE_COMMIT.
            action_type: The type of action (e.g., "payment", "transfer").
            retained_properties: The set Q of properties to evaluate for
                return. MUST be a non-empty list — an empty Q makes the
                assessment vacuous (no properties to preserve). If None or
                empty, returns INDETERMINATE.
            return_transition: A callable that simulates the return
                transition ρ. It receives (metadata) and returns
                (distance_q, return_cost, description). If None, no return
                transition is available → IRREVERSIBLE (unless PRE_COMMIT).
            lambda_weight: Override the default λ for this classification.
            non_deterministic_results: If the transition was non-deterministic,
                provide the list of possible results. The classifier will
                verify that τ⁻¹ exists for all relevant results.
            metadata: Additional context passed to the return_transition.

        Returns:
            TauInversionResult with the classification and loss calculation.

        Classification logic:
            1. If retained_properties Q is empty or missing → INDETERMINATE
               (cannot assess property preservation without a defined Q).
            2. If operation_state == "failed" and external effect status is
               unknown → INDETERMINATE (absence of evidence is not evidence
               of non-execution).
            3. If operation_state == "failed" and external effect is
               explicitly confirmed absent → PRE_COMMIT.
            4. If no return_transition is provided → IRREVERSIBLE.
            5. If return_transition is provided, execute it and measure
               distance_Q:
               - distance_q == 0 → EXACT_INVERSE
               - distance_q > 0 but finite → COMPENSATION
            6. For non-deterministic transitions, verify all results.
        """
        lam = lambda_weight if lambda_weight is not None else self.default_lambda
        metadata = metadata or {}

        # ── 0. Reject empty Q as vacuous assessment ─────────────────────
        # P1-2: An empty property set Q makes the assessment vacuous.
        # Q defines the properties whose preservation is being assessed.
        # With no explicitly retained properties, an exact inverse claim
        # has no substantive property-preservation content. A callback
        # returning zero distance should not automatically establish
        # meaningful reversibility.
        if not retained_properties:
            return TauInversionResult(
                classification=InversionClassification.INDETERMINATE,
                retained_properties=[],
                distance_q=None,
                return_cost=None,
                loss=None,
                lambda_weight=lam,
                rationale=(
                    "Indeterminate: retained property set Q is empty or "
                    "missing. Cannot assess property preservation without "
                    "a defined Q. An empty Q makes any reversibility claim "
                    "vacuous."
                ),
                return_transition_description="",
                non_determinism_handled=non_deterministic_results is not None,
            )

        # ── 1. FAILED operations: require explicit effect status ────────
        # P1-1: Absence of evidence must not become evidence of non-execution.
        # The implementation's own comments acknowledge that a failed
        # operation may have produced an external effect. We require
        # explicit, appropriately sourced confirmation before classifying
        # as PRE_COMMIT.
        #
        # no_external_effect can be:
        #   True  → explicitly confirmed no effect → PRE_COMMIT
        #   False → explicitly confirmed effect occurred → proceed to return
        #   None  → unknown → INDETERMINATE
        if operation_state == "failed":
            no_external_effect = metadata.get("no_external_effect", None)
            if no_external_effect is None:
                # Effect status unknown — cannot determine classification
                return TauInversionResult(
                    classification=InversionClassification.INDETERMINATE,
                    retained_properties=retained_properties,
                    distance_q=None,
                    return_cost=None,
                    loss=None,
                    lambda_weight=lam,
                    rationale=(
                        "Indeterminate: operation FAILED but external "
                        "effect status is unknown. Absence of evidence is "
                        "not evidence of non-execution. Require explicit "
                        "confirmation before classifying as PRE_COMMIT."
                    ),
                    return_transition_description="",
                    non_determinism_handled=non_deterministic_results is not None,
                )
            elif no_external_effect is True:
                # Explicitly confirmed: no external effect occurred
                return TauInversionResult(
                    classification=InversionClassification.PRE_COMMIT,
                    retained_properties=retained_properties,
                    distance_q=0.0,
                    return_cost=0.0,
                    loss=0.0,
                    lambda_weight=lam,
                    rationale=(
                        "Pre-commit cancellation: the action did not produce "
                        "an external effect (explicitly confirmed). Return "
                        "is trivially exact — no external state changed."
                    ),
                    return_transition_description="None needed — no effect to reverse.",
                    non_determinism_handled=non_deterministic_results is not None,
                )
            # no_external_effect is False → effect DID occur → fall through
            # to return transition evaluation below

        # ── 2. No return transition available → IRREVERSIBLE ────────────
        # Per doctrine: "irréversibilité : aucune opération autorisée ne
        # satisfait le critère de retour choisi."
        if return_transition is None:
            return TauInversionResult(
                classification=InversionClassification.IRREVERSIBLE,
                retained_properties=retained_properties,
                distance_q=None,
                return_cost=None,
                loss=None,
                lambda_weight=lam,
                rationale=(
                    "Irreversibility: no authorized return transition ρ "
                    "satisfies the chosen return criterion for properties Q."
                ),
                return_transition_description="",
                non_determinism_handled=non_deterministic_results is not None,
            )

        # ── 3. Execute the return transition and measure distance ───────
        # Per doctrine: the return transition ρ is executed, and we measure
        # distance_Q(ρ(τ(s)), s) — how far the returned state is from the
        # original on the retained properties Q only.
        #
        # For non-deterministic transitions, we must verify τ⁻¹ for ALL
        # relevant results (doctrine: "l'existence d'un inverse doit être
        # établie pour l'ensemble des résultats pertinents, ou alors la
        # limitation est signalée").
        all_distances: list[float] = []
        all_costs: list[float] = []
        all_descriptions: list[str] = []
        non_determinism_handled = False

        if non_deterministic_results:
            # Non-deterministic: evaluate return for each possible result
            non_determinism_handled = True
            for nd_result in non_deterministic_results:
                nd_metadata = {**metadata, "result": nd_result}
                try:
                    dist, cost, desc = return_transition(nd_metadata)
                except Exception as e:
                    # If the return transition fails for any result,
                    # signal the limitation
                    return TauInversionResult(
                        classification=InversionClassification.IRREVERSIBLE,
                        retained_properties=retained_properties,
                        distance_q=None,
                        return_cost=None,
                        loss=None,
                        lambda_weight=lam,
                        rationale=(
                            f"Non-deterministic transition: return transition "
                            f"failed for result {nd_result}. Limitation "
                            f"signaled: τ⁻¹ not established for all results."
                        ),
                        return_transition_description=f"Failed: {e}",
                        non_determinism_handled=True,
                    )
                all_distances.append(dist)
                all_costs.append(cost)
                all_descriptions.append(desc)
            # Use the worst case (max distance, max cost) for classification
            distance_q = max(all_distances)
            return_cost = max(all_costs)
            description = "; ".join(all_descriptions)
        else:
            # Deterministic: single evaluation
            try:
                distance_q, return_cost, description = return_transition(metadata)
            except Exception as e:
                return TauInversionResult(
                    classification=InversionClassification.IRREVERSIBLE,
                    retained_properties=retained_properties,
                    distance_q=None,
                    return_cost=None,
                    loss=None,
                    lambda_weight=lam,
                    rationale=(
                        f"Return transition failed: {e}. No authorized "
                        f"operation satisfies the return criterion."
                    ),
                    return_transition_description=f"Failed: {e}",
                    non_determinism_handled=False,
                )

        # ── 4. Classify based on distance_Q ─────────────────────────────
        loss = distance_q + lam * return_cost

        if distance_q == 0.0:
            # Exact inverse on Q
            classification = InversionClassification.EXACT_INVERSE
            rationale = (
                "Exact inverse on Q: the return transition ρ restores the "
                "targeted properties. ρ(τ(s)) ≈_Q s holds with distance_Q = 0."
            )
        else:
            # Compensation: reduces effects but doesn't fully restore
            classification = InversionClassification.COMPENSATION
            rationale = (
                f"Compensation: the return transition ρ reduces certain "
                f"effects without restoring the initial state. "
                f"distance_Q = {distance_q:.4f} > 0."
            )

        return TauInversionResult(
            classification=classification,
            retained_properties=retained_properties,
            distance_q=distance_q,
            return_cost=return_cost,
            loss=loss,
            lambda_weight=lam,
            rationale=rationale,
            return_transition_description=description,
            non_determinism_handled=non_determinism_handled,
        )

    def compute_loss(
        self,
        distance_q: float,
        return_cost: float,
        lambda_weight: float | None = None,
    ) -> float:
        """Compute the τ⁻¹ loss function L_Q.

        L_Q(s, τ, ρ) = distance_Q(ρ(τ(s)), s) + λ · cost(ρ)

        This is a pure function — it does not execute any transition.
        It's exposed for cases where the caller has already measured
        distance_Q and cost(ρ) separately.

        Args:
            distance_q: distance_Q(ρ(τ(s)), s) — how far the returned
                state is from the original on retained properties Q.
            return_cost: cost(ρ) — the cost of the return transition.
            lambda_weight: λ — the weight balancing fidelity vs cost.

        Returns:
            The loss value L_Q.
        """
        lam = lambda_weight if lambda_weight is not None else self.default_lambda
        return distance_q + lam * return_cost