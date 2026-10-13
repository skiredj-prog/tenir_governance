"""Comprehensive tests for τ⁻¹ (partial tau inversion).

Per doctrine (Property-Preserving Crossing for Agentic Systems, pp. 11-12):
- τ⁻¹ is a partial, conditional, property-relative inverse — NOT a full
  mathematical inverse.
- 4 classifications: pre-commit, exact-inverse-on-Q, compensation, irreversibility.
- Loss function: L_Q(s, τ, ρ) = distance_Q(ρ(τ(s)), s) + λ · cost(ρ)
- Non-deterministic transitions: τ⁻¹ must be established for ALL relevant results.
"""
import pytest
from tenir_governance.assessment.tau_inversion import (
    TauInversionClassifier,
    TauInversionResult,
    InversionClassification,
)


class TestInversionClassification:
    """Test the InversionClassification enum and its properties."""

    def test_four_classifications_exist(self):
        assert InversionClassification.PRE_COMMIT == "pre_commit"
        assert InversionClassification.EXACT_INVERSE == "exact_inverse"
        assert InversionClassification.COMPENSATION == "compensation"
        assert InversionClassification.IRREVERSIBLE == "irreversible"

    def test_reversibility_rank_ordering(self):
        """Higher rank = more reversible. PRE_COMMIT > EXACT > COMPENSATION > IRREVERSIBLE."""
        assert InversionClassification.PRE_COMMIT.reversibility_rank > InversionClassification.EXACT_INVERSE.reversibility_rank
        assert InversionClassification.EXACT_INVERSE.reversibility_rank > InversionClassification.COMPENSATION.reversibility_rank
        assert InversionClassification.COMPENSATION.reversibility_rank > InversionClassification.IRREVERSIBLE.reversibility_rank

    def test_is_reversible(self):
        assert InversionClassification.PRE_COMMIT.is_reversible is True
        assert InversionClassification.EXACT_INVERSE.is_reversible is True
        assert InversionClassification.COMPENSATION.is_reversible is True
        assert InversionClassification.IRREVERSIBLE.is_reversible is False

    def test_irreversible_is_not_reversible(self):
        """Irreversibility means NO authorized operation satisfies the return criterion."""
        assert not InversionClassification.IRREVERSIBLE.is_reversible


class TestPreCommitCancellation:
    """Test classification 1: pre-commit cancellation.

    Per doctrine: "annulation pré-commit : l'action n'a pas encore produit
    d'effet externe" — the action has not yet produced an external effect.
    """

    def test_failed_operation_with_no_external_effect_is_pre_commit(self):
        """A FAILED operation that produced no external effect is PRE_COMMIT."""
        classifier = TauInversionClassifier()
        result = classifier.classify(
            operation_state="failed",
            action_type="payment",
            retained_properties=["balance"],
            metadata={"no_external_effect": True},
        )
        assert result.classification == InversionClassification.PRE_COMMIT
        assert result.distance_q == 0.0
        assert result.return_cost == 0.0
        assert result.loss == 0.0
        assert result.is_reversible is True

    def test_pre_commit_has_zero_loss(self):
        """Pre-commit cancellation is trivially exact — zero loss."""
        classifier = TauInversionClassifier()
        result = classifier.classify(
            operation_state="failed",
            metadata={"no_external_effect": True},
        )
        assert result.loss == 0.0

    def test_pre_commit_rationale_mentions_no_external_effect(self):
        classifier = TauInversionClassifier()
        result = classifier.classify(
            operation_state="failed",
            metadata={"no_external_effect": True},
        )
        assert "no external effect" in result.rationale.lower() or "pre-commit" in result.rationale.lower()

    def test_pre_commit_does_not_require_return_transition(self):
        """Even without a return transition, pre-commit is exact."""
        classifier = TauInversionClassifier()
        result = classifier.classify(
            operation_state="failed",
            return_transition=None,
            metadata={"no_external_effect": True},
        )
        assert result.classification == InversionClassification.PRE_COMMIT


class TestExactInverseOnQ:
    """Test classification 2: exact inverse on Q.

    Per doctrine: "inverse exact sur Q : l'opération inverse rétablit les
    propriétés ciblées" — the inverse operation restores the targeted properties.
    ρ(τ(s)) ≈_Q s holds with distance_Q = 0.
    """

    def test_exact_inverse_when_distance_is_zero(self):
        """A return transition with distance_Q = 0 is EXACT_INVERSE."""
        classifier = TauInversionClassifier()

        def return_transition(metadata):
            return (0.0, 5.0, "Full refund issued")

        result = classifier.classify(
            operation_state="confirmed",
            action_type="payment",
            retained_properties=["balance"],
            return_transition=return_transition,
        )
        assert result.classification == InversionClassification.EXACT_INVERSE
        assert result.distance_q == 0.0
        assert result.return_cost == 5.0
        assert result.loss == 0.0 + 0.1 * 5.0  # λ=0.1 default

    def test_exact_inverse_with_custom_lambda(self):
        """Test loss function with custom λ."""
        classifier = TauInversionClassifier(lambda_weight=0.5)

        def return_transition(metadata):
            return (0.0, 10.0, "Full reversal")

        result = classifier.classify(
            operation_state="confirmed",
            retained_properties=["balance"],
            return_transition=return_transition,
        )
        assert result.classification == InversionClassification.EXACT_INVERSE
        assert result.loss == 0.0 + 0.5 * 10.0
        assert result.loss == 5.0

    def test_exact_inverse_rationale_mentions_targeted_properties(self):
        classifier = TauInversionClassifier()

        def return_transition(metadata):
            return (0.0, 1.0, "Reversal")

        result = classifier.classify(
            operation_state="confirmed",
            retained_properties=["balance", "audit_trail"],
            return_transition=return_transition,
        )
        assert "exact" in result.rationale.lower() or "targeted" in result.rationale.lower()


class TestCompensation:
    """Test classification 3: compensation.

    Per doctrine: "compensation : une opération réduit certains effets sans
    restaurer l'état initial" — an operation reduces certain effects without
    restoring the initial state. distance_Q > 0 but finite.
    """

    def test_compensation_when_distance_is_positive(self):
        """A return transition with distance_Q > 0 is COMPENSATION."""
        classifier = TauInversionClassifier()

        def return_transition(metadata):
            return (0.3, 2.0, "Partial refund with FX loss")

        result = classifier.classify(
            operation_state="confirmed",
            action_type="transfer",
            retained_properties=["balance"],
            return_transition=return_transition,
        )
        assert result.classification == InversionClassification.COMPENSATION
        assert result.distance_q == 0.3
        assert result.return_cost == 2.0
        assert result.loss == 0.3 + 0.1 * 2.0
        assert result.is_reversible is True

    def test_compensation_loss_calculation(self):
        """L_Q = distance_Q + λ · cost(ρ)"""
        classifier = TauInversionClassifier(lambda_weight=0.2)

        def return_transition(metadata):
            return (0.5, 10.0, "Compensation payment")

        result = classifier.classify(
            operation_state="confirmed",
            retained_properties=["balance"],
            return_transition=return_transition,
        )
        expected_loss = 0.5 + 0.2 * 10.0
        assert result.loss == pytest.approx(expected_loss)
        assert result.classification == InversionClassification.COMPENSATION

    def test_compensation_rationale_mentions_distance(self):
        classifier = TauInversionClassifier()

        def return_transition(metadata):
            return (0.3, 2.0, "Partial refund")

        result = classifier.classify(
            operation_state="confirmed",
            return_transition=return_transition,
        )
        assert "compensation" in result.rationale.lower()


class TestIrreversibility:
    """Test classification 4: irreversibility.

    Per doctrine: "irréversibilité : aucune opération autorisée ne satisfait
    le critère de retour choisi" — no authorized operation satisfies the
    chosen return criterion.
    """

    def test_no_return_transition_is_irreversible(self):
        """Confirmed operation with no return transition is IRREVERSIBLE."""
        classifier = TauInversionClassifier()
        result = classifier.classify(
            operation_state="confirmed",
            action_type="message_sent",
            retained_properties=["message_read_status"],
            return_transition=None,
        )
        assert result.classification == InversionClassification.IRREVERSIBLE
        assert result.distance_q is None
        assert result.return_cost is None
        assert result.loss is None
        assert result.is_reversible is False

    def test_irreversible_rationale_mentions_no_authorized_operation(self):
        classifier = TauInversionClassifier()
        result = classifier.classify(
            operation_state="confirmed",
            return_transition=None,
        )
        assert "irrevers" in result.rationale.lower() or "no authorized" in result.rationale.lower()

    def test_failed_operation_with_external_effect_and_no_return_is_irreversible(self):
        """A FAILED operation that DID produce an effect, with no return transition."""
        classifier = TauInversionClassifier()
        result = classifier.classify(
            operation_state="failed",
            metadata={"no_external_effect": False},
            return_transition=None,
        )
        assert result.classification == InversionClassification.IRREVERSIBLE

    def test_return_transition_that_raises_is_irreversible(self):
        """If the return transition raises an exception, it's IRREVERSIBLE."""
        classifier = TauInversionClassifier()

        def failing_return(metadata):
            raise RuntimeError("Cannot reverse: data already copied")

        result = classifier.classify(
            operation_state="confirmed",
            return_transition=failing_return,
        )
        assert result.classification == InversionClassification.IRREVERSIBLE
        assert result.is_reversible is False


class TestLossFunction:
    """Test the τ⁻¹ loss function L_Q(s, τ, ρ) = distance_Q(ρ(τ(s)), s) + λ · cost(ρ)."""

    def test_loss_function_formula(self):
        """L_Q = distance_Q + λ · cost(ρ)"""
        classifier = TauInversionClassifier(lambda_weight=0.1)
        loss = classifier.compute_loss(distance_q=0.5, return_cost=10.0)
        assert loss == pytest.approx(0.5 + 0.1 * 10.0)

    def test_loss_function_with_custom_lambda(self):
        classifier = TauInversionClassifier(lambda_weight=0.3)
        loss = classifier.compute_loss(distance_q=0.5, return_cost=10.0, lambda_weight=0.5)
        assert loss == pytest.approx(0.5 + 0.5 * 10.0)

    def test_loss_zero_distance_zero_cost(self):
        classifier = TauInversionClassifier()
        loss = classifier.compute_loss(distance_q=0.0, return_cost=0.0)
        assert loss == 0.0

    def test_loss_high_lambda_makes_cost_dominant(self):
        """With high λ, cost(ρ) dominates the loss."""
        classifier = TauInversionClassifier(lambda_weight=10.0)
        loss = classifier.compute_loss(distance_q=0.01, return_cost=100.0)
        assert loss == pytest.approx(0.01 + 10.0 * 100.0)

    def test_loss_low_lambda_makes_distance_dominant(self):
        """With low λ, distance_Q dominates the loss."""
        classifier = TauInversionClassifier(lambda_weight=0.001)
        loss = classifier.compute_loss(distance_q=5.0, return_cost=1.0)
        assert loss == pytest.approx(5.0 + 0.001 * 1.0)


class TestNonDeterminism:
    """Test non-deterministic transition handling.

    Per doctrine: "Lorsque plusieurs effets ou résultats rendent la transition
    non déterministe, l'existence d'un inverse doit être établie pour l'ensemble
    des résultats pertinents, ou alors la limitation est signalée."
    """

    def test_non_deterministic_all_results_reversible(self):
        """τ⁻¹ exists for all relevant results → classified based on worst case."""
        classifier = TauInversionClassifier()

        def return_transition(metadata):
            result = metadata.get("result", {})
            dist = result.get("distance", 0.0)
            cost = result.get("cost", 0.0)
            return (dist, cost, f"Return for {result}")

        results = [
            {"distance": 0.0, "cost": 1.0},
            {"distance": 0.0, "cost": 2.0},
            {"distance": 0.2, "cost": 3.0},
        ]

        result = classifier.classify(
            operation_state="confirmed",
            retained_properties=["balance"],
            return_transition=return_transition,
            non_deterministic_results=results,
        )
        # Worst case: max distance = 0.2, max cost = 3.0
        assert result.classification == InversionClassification.COMPENSATION
        assert result.distance_q == 0.2
        assert result.return_cost == 3.0
        assert result.non_determinism_handled is True

    def test_non_deterministic_one_result_irreversible(self):
        """If ANY result cannot be reversed, the limitation is signaled."""
        classifier = TauInversionClassifier()

        def return_transition(metadata):
            result = metadata.get("result", {})
            dist = result.get("distance", 0.0)
            if dist is None:
                raise RuntimeError("Cannot reverse this outcome")
            return (dist, 1.0, "Return")

        results = [
            {"distance": 0.0},
            {"distance": None},  # This one can't be reversed
        ]

        result = classifier.classify(
            operation_state="confirmed",
            return_transition=return_transition,
            non_deterministic_results=results,
        )
        assert result.classification == InversionClassification.IRREVERSIBLE
        assert result.non_determinism_handled is True

    def test_non_deterministic_all_exact(self):
        """All results have distance 0 → EXACT_INVERSE."""
        classifier = TauInversionClassifier()

        def return_transition(metadata):
            return (0.0, 1.0, "Exact return")

        results = [{"distance": 0.0}, {"distance": 0.0}]

        result = classifier.classify(
            operation_state="confirmed",
            return_transition=return_transition,
            non_deterministic_results=results,
        )
        assert result.classification == InversionClassification.EXACT_INVERSE
        assert result.non_determinism_handled is True


class TestPropertyRelativeEquivalence:
    """Test the property-relative nature of τ⁻¹.

    Per doctrine: τ⁻¹ is relative to properties Q ⊆ Π. The inverse only
    needs to restore the explicitly retained properties Q, not the entire
    world state.
    """

    def test_retained_properties_are_recorded(self):
        """The retained properties Q are recorded in the result."""
        classifier = TauInversionClassifier()

        def return_transition(metadata):
            return (0.0, 1.0, "Return")

        result = classifier.classify(
            operation_state="confirmed",
            retained_properties=["balance", "audit_trail", "customer_id"],
            return_transition=return_transition,
        )
        assert result.retained_properties == ["balance", "audit_trail", "customer_id"]

    def test_empty_retained_properties(self):
        """Empty Q means no properties need to be restored."""
        classifier = TauInversionClassifier()

        def return_transition(metadata):
            return (0.0, 0.0, "No properties to restore")

        result = classifier.classify(
            operation_state="confirmed",
            retained_properties=[],
            return_transition=return_transition,
        )
        assert result.classification == InversionClassification.EXACT_INVERSE

    def test_different_properties_different_classification(self):
        """The same operation can have different τ⁻¹ for different Q sets."""
        classifier = TauInversionClassifier()

        # With Q = ["balance"], the return transition restores balance exactly
        def return_for_balance(metadata):
            return (0.0, 5.0, "Full refund")

        result_balance = classifier.classify(
            operation_state="confirmed",
            retained_properties=["balance"],
            return_transition=return_for_balance,
        )
        assert result_balance.classification == InversionClassification.EXACT_INVERSE

        # With Q = ["balance", "market_position"], the return only partially restores
        def return_for_balance_and_market(metadata):
            return (0.4, 5.0, "Refund issued but market position changed")

        result_both = classifier.classify(
            operation_state="confirmed",
            retained_properties=["balance", "market_position"],
            return_transition=return_for_balance_and_market,
        )
        assert result_both.classification == InversionClassification.COMPENSATION


class TestTauInversionResultSerialization:
    """Test TauInversionResult.to_dict()."""

    def test_to_dict_contains_all_fields(self):
        classifier = TauInversionClassifier()

        def return_transition(metadata):
            return (0.0, 5.0, "Full refund")

        result = classifier.classify(
            operation_state="confirmed",
            retained_properties=["balance"],
            return_transition=return_transition,
        )
        d = result.to_dict()
        assert d["classification"] == "exact_inverse"
        assert d["retained_properties"] == ["balance"]
        assert d["distance_q"] == 0.0
        assert d["return_cost"] == 5.0
        assert d["loss"] == pytest.approx(0.5)
        assert d["lambda_weight"] == 0.1
        assert d["is_reversible"] is True
        assert "rationale" in d
        assert "return_transition_description" in d
        assert "non_determinism_handled" in d

    def test_irreversible_to_dict_has_nulls(self):
        classifier = TauInversionClassifier()
        result = classifier.classify(
            operation_state="confirmed",
            return_transition=None,
        )
        d = result.to_dict()
        assert d["classification"] == "irreversible"
        assert d["distance_q"] is None
        assert d["return_cost"] is None
        assert d["loss"] is None
        assert d["is_reversible"] is False


class TestFailedWithExternalEffect:
    """Test the edge case: FAILED operation that DID produce an external effect."""

    def test_failed_with_external_effect_needs_return_transition(self):
        """A FAILED operation that produced an effect needs a return transition."""
        classifier = TauInversionClassifier()

        def return_transition(metadata):
            return (0.2, 3.0, "Compensation for partial effect")

        result = classifier.classify(
            operation_state="failed",
            metadata={"no_external_effect": False},
            return_transition=return_transition,
        )
        assert result.classification == InversionClassification.COMPENSATION

    def test_failed_with_external_effect_no_return_is_irreversible(self):
        classifier = TauInversionClassifier()
        result = classifier.classify(
            operation_state="failed",
            metadata={"no_external_effect": False},
            return_transition=None,
        )
        assert result.classification == InversionClassification.IRREVERSIBLE