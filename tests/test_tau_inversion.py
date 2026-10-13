"""Comprehensive tests for τ⁻¹ (partial tau inversion).

Per doctrine (Property-Preserving Crossing for Agentic Systems, pp. 11-12):
- τ⁻¹ is a partial, conditional, property-relative inverse — NOT a full
  mathematical inverse.
- 4 classifications: pre-commit, exact-inverse-on-Q, compensation, irreversibility.
- Loss function: L_Q(s, τ, ρ) = distance_Q(ρ(τ(s)), s) + λ · cost(ρ)
- Non-deterministic transitions: τ⁻¹ must be established for ALL relevant results.

P1-1 fix: absence of evidence must not become evidence of non-execution.
  no_external_effect defaults to None (indeterminate), not True.
  PRE_COMMIT requires explicit confirmation that no external effect occurred.

P1-2 fix: empty Q makes the assessment vacuous.
  Empty or missing retained_properties returns INDETERMINATE.
"""
import pytest
from tenir_governance.assessment.tau_inversion import (
    TauInversionClassifier,
    TauInversionResult,
    InversionClassification,
)


class TestInversionClassification:
    """Test the InversionClassification enum and its properties."""

    def test_four_doctrine_classifications_exist(self):
        assert InversionClassification.PRE_COMMIT == "pre_commit"
        assert InversionClassification.EXACT_INVERSE == "exact_inverse"
        assert InversionClassification.COMPENSATION == "compensation"
        assert InversionClassification.IRREVERSIBLE == "irreversible"

    def test_indeterminate_classification_exists(self):
        """P1-1/P1-2: INDETERMINATE is used when assessment cannot be performed."""
        assert InversionClassification.INDETERMINATE == "indeterminate"

    def test_reversibility_rank_ordering(self):
        """Higher rank = more reversible. PRE_COMMIT > EXACT > COMPENSATION > IRREVERSIBLE."""
        assert InversionClassification.PRE_COMMIT.reversibility_rank > InversionClassification.EXACT_INVERSE.reversibility_rank
        assert InversionClassification.EXACT_INVERSE.reversibility_rank > InversionClassification.COMPENSATION.reversibility_rank
        assert InversionClassification.COMPENSATION.reversibility_rank > InversionClassification.IRREVERSIBLE.reversibility_rank

    def test_indeterminate_has_zero_rank(self):
        assert InversionClassification.INDETERMINATE.reversibility_rank == 0

    def test_is_reversible(self):
        assert InversionClassification.PRE_COMMIT.is_reversible is True
        assert InversionClassification.EXACT_INVERSE.is_reversible is True
        assert InversionClassification.COMPENSATION.is_reversible is True
        assert InversionClassification.IRREVERSIBLE.is_reversible is False
        assert InversionClassification.INDETERMINATE.is_reversible is False

    def test_is_determined(self):
        """INDETERMINATE is not a determined assessment."""
        assert InversionClassification.PRE_COMMIT.is_determined is True
        assert InversionClassification.EXACT_INVERSE.is_determined is True
        assert InversionClassification.COMPENSATION.is_determined is True
        assert InversionClassification.IRREVERSIBLE.is_determined is True
        assert InversionClassification.INDETERMINATE.is_determined is False


class TestPreCommitCancellation:
    """Test classification 1: pre-commit cancellation.

    Per doctrine: "annulation pré-commit : l'action n'a pas encore produit
    d'effet externe" — the action has not yet produced an external effect.

    P1-1 fix: PRE_COMMIT now requires EXPLICIT confirmation that no external
    effect occurred. The default is indeterminate, not pre-commit.
    """

    def test_failed_with_explicit_no_external_effect_is_pre_commit(self):
        """A FAILED operation with explicit no_external_effect=True is PRE_COMMIT."""
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
            retained_properties=["balance"],
            metadata={"no_external_effect": True},
        )
        assert result.loss == 0.0

    def test_pre_commit_rationale_mentions_no_external_effect(self):
        classifier = TauInversionClassifier()
        result = classifier.classify(
            operation_state="failed",
            retained_properties=["balance"],
            metadata={"no_external_effect": True},
        )
        assert "no external effect" in result.rationale.lower() or "pre-commit" in result.rationale.lower()

    def test_pre_commit_does_not_require_return_transition(self):
        """Even without a return transition, pre-commit is exact."""
        classifier = TauInversionClassifier()
        result = classifier.classify(
            operation_state="failed",
            retained_properties=["balance"],
            return_transition=None,
            metadata={"no_external_effect": True},
        )
        assert result.classification == InversionClassification.PRE_COMMIT


class TestP1IndeterminateEffect:
    """P1-1: Absence of evidence must not become evidence of non-execution.

    When a FAILED operation's external effect status is unknown, the
    classifier must return INDETERMINATE, not PRE_COMMIT.
    """

    def test_failed_without_effect_metadata_is_indeterminate(self):
        """P1-1: FAILED with no metadata about external effect → INDETERMINATE."""
        classifier = TauInversionClassifier()
        result = classifier.classify(
            operation_state="failed",
            retained_properties=["balance"],
        )
        assert result.classification == InversionClassification.INDETERMINATE
        assert result.distance_q is None
        assert result.loss is None

    def test_failed_with_explicit_none_effect_status_is_indeterminate(self):
        """P1-1: FAILED with no_external_effect=None → INDETERMINATE."""
        classifier = TauInversionClassifier()
        result = classifier.classify(
            operation_state="failed",
            retained_properties=["balance"],
            metadata={"no_external_effect": None},
        )
        assert result.classification == InversionClassification.INDETERMINATE

    def test_indeterminate_rationale_mentions_unknown(self):
        classifier = TauInversionClassifier()
        result = classifier.classify(
            operation_state="failed",
            retained_properties=["balance"],
        )
        assert "unknown" in result.rationale.lower() or "indeterminate" in result.rationale.lower()

    def test_indeterminate_is_not_reversible(self):
        """INDETERMINATE is not reversible — reversibility is unknown."""
        classifier = TauInversionClassifier()
        result = classifier.classify(
            operation_state="failed",
            retained_properties=["balance"],
        )
        assert result.is_reversible is False

    def test_indeterminate_is_not_determined(self):
        """INDETERMINATE is not a determined assessment."""
        classifier = TauInversionClassifier()
        result = classifier.classify(
            operation_state="failed",
            retained_properties=["balance"],
        )
        assert result.classification.is_determined is False

    def test_failed_with_effect_false_proceeds_to_return_evaluation(self):
        """FAILED with no_external_effect=False → effect occurred, evaluate return."""
        classifier = TauInversionClassifier()

        def return_transition(metadata):
            return (0.0, 5.0, "Full refund")

        result = classifier.classify(
            operation_state="failed",
            retained_properties=["balance"],
            return_transition=return_transition,
            metadata={"no_external_effect": False},
        )
        assert result.classification == InversionClassification.EXACT_INVERSE


class TestExactInverseOnQ:
    """Test classification 2: exact inverse on Q."""

    def test_exact_inverse_when_distance_is_zero(self):
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
        assert result.loss == 0.0 + 0.1 * 5.0

    def test_exact_inverse_with_custom_lambda(self):
        classifier = TauInversionClassifier(lambda_weight=0.5)

        def return_transition(metadata):
            return (0.0, 10.0, "Full reversal")

        result = classifier.classify(
            operation_state="confirmed",
            retained_properties=["balance"],
            return_transition=return_transition,
        )
        assert result.classification == InversionClassification.EXACT_INVERSE
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
    """Test classification 3: compensation."""

    def test_compensation_when_distance_is_positive(self):
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
            retained_properties=["balance"],
            return_transition=return_transition,
        )
        assert "compensation" in result.rationale.lower()


class TestIrreversibility:
    """Test classification 4: irreversibility."""

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
            retained_properties=["message_read_status"],
            return_transition=None,
        )
        assert "irrevers" in result.rationale.lower() or "no authorized" in result.rationale.lower()

    def test_failed_with_external_effect_and_no_return_is_irreversible(self):
        """FAILED with explicit effect, no return transition → IRREVERSIBLE."""
        classifier = TauInversionClassifier()
        result = classifier.classify(
            operation_state="failed",
            retained_properties=["balance"],
            metadata={"no_external_effect": False},
            return_transition=None,
        )
        assert result.classification == InversionClassification.IRREVERSIBLE

    def test_return_transition_that_raises_is_indeterminate(self):
        """A failed assessment does not prove no authorized inverse exists.

        If the return-transition callback raises an exception, the classifier
        cannot complete the assessment. This is INDETERMINATE, not IRREVERSIBLE —
        a failed assessment is not evidence of irreversibility.
        """
        classifier = TauInversionClassifier()

        def failing_return(metadata):
            raise RuntimeError("Cannot reverse: data already copied")

        result = classifier.classify(
            operation_state="confirmed",
            retained_properties=["balance"],
            return_transition=failing_return,
        )
        assert result.classification == InversionClassification.INDETERMINATE
        assert result.classification.is_determined is False


class TestP1EmptyQ:
    """P1-2: Empty Q makes the assessment vacuous.

    An empty property set Q means there are no properties to preserve.
    A callback returning zero distance should not automatically establish
    meaningful reversibility. The classifier must return INDETERMINATE.
    """

    def test_empty_retained_properties_is_indeterminate(self):
        """P1-2: Empty Q → INDETERMINATE."""
        classifier = TauInversionClassifier()

        def return_transition(metadata):
            return (0.0, 0.0, "No properties to restore")

        result = classifier.classify(
            operation_state="confirmed",
            retained_properties=[],
            return_transition=return_transition,
        )
        assert result.classification == InversionClassification.INDETERMINATE

    def test_missing_retained_properties_is_indeterminate(self):
        """P1-2: None Q → INDETERMINATE."""
        classifier = TauInversionClassifier()

        def return_transition(metadata):
            return (0.0, 0.0, "Return")

        result = classifier.classify(
            operation_state="confirmed",
            retained_properties=None,
            return_transition=return_transition,
        )
        assert result.classification == InversionClassification.INDETERMINATE

    def test_empty_q_rationale_mentions_vacuous(self):
        classifier = TauInversionClassifier()
        result = classifier.classify(
            operation_state="confirmed",
            retained_properties=[],
            return_transition=lambda m: (0.0, 0.0, ""),
        )
        assert "vacuous" in result.rationale.lower() or "empty" in result.rationale.lower()

    def test_empty_q_does_not_produce_exact_inverse(self):
        """P1-2: Even with a zero-distance callback, empty Q must not be EXACT_INVERSE."""
        classifier = TauInversionClassifier()

        def zero_distance_return(metadata):
            return (0.0, 0.0, "Trivially exact")

        result = classifier.classify(
            operation_state="confirmed",
            retained_properties=[],
            return_transition=zero_distance_return,
        )
        assert result.classification != InversionClassification.EXACT_INVERSE
        assert result.classification == InversionClassification.INDETERMINATE

    def test_empty_q_on_failed_with_no_effect_still_indeterminate(self):
        """P1-2: Empty Q takes priority — even with no_external_effect=True."""
        classifier = TauInversionClassifier()
        result = classifier.classify(
            operation_state="failed",
            retained_properties=[],
            metadata={"no_external_effect": True},
        )
        assert result.classification == InversionClassification.INDETERMINATE


class TestLossFunction:
    """Test the τ⁻¹ loss function L_Q(s, τ, ρ) = distance_Q(ρ(τ(s)), s) + λ · cost(ρ)."""

    def test_loss_function_formula(self):
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
        classifier = TauInversionClassifier(lambda_weight=10.0)
        loss = classifier.compute_loss(distance_q=0.01, return_cost=100.0)
        assert loss == pytest.approx(0.01 + 10.0 * 100.0)

    def test_loss_low_lambda_makes_distance_dominant(self):
        classifier = TauInversionClassifier(lambda_weight=0.001)
        loss = classifier.compute_loss(distance_q=5.0, return_cost=1.0)
        assert loss == pytest.approx(5.0 + 0.001 * 1.0)


class TestNonDeterminism:
    """Test non-deterministic transition handling."""

    def test_non_deterministic_all_results_reversible(self):
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
        assert result.classification == InversionClassification.COMPENSATION
        assert result.distance_q == 0.2
        assert result.return_cost == 3.0
        assert result.non_determinism_handled is True

    def test_non_deterministic_one_result_indeterminate(self):
        """If the return-transition callback raises for ANY result, the
        assessment is indeterminate — a failed assessment does not prove
        no authorized inverse exists for that result.
        """
        classifier = TauInversionClassifier()

        def return_transition(metadata):
            result = metadata.get("result", {})
            dist = result.get("distance", 0.0)
            if dist is None:
                raise RuntimeError("Cannot reverse this outcome")
            return (dist, 1.0, "Return")

        results = [
            {"distance": 0.0},
            {"distance": None},
        ]

        result = classifier.classify(
            operation_state="confirmed",
            retained_properties=["balance"],
            return_transition=return_transition,
            non_deterministic_results=results,
        )
        assert result.classification == InversionClassification.INDETERMINATE
        assert result.non_determinism_handled is True

    def test_non_deterministic_all_exact(self):
        classifier = TauInversionClassifier()

        def return_transition(metadata):
            return (0.0, 1.0, "Exact return")

        results = [{"distance": 0.0}, {"distance": 0.0}]

        result = classifier.classify(
            operation_state="confirmed",
            retained_properties=["balance"],
            return_transition=return_transition,
            non_deterministic_results=results,
        )
        assert result.classification == InversionClassification.EXACT_INVERSE
        assert result.non_determinism_handled is True


class TestPropertyRelativeEquivalence:
    """Test the property-relative nature of τ⁻¹."""

    def test_retained_properties_are_recorded(self):
        classifier = TauInversionClassifier()

        def return_transition(metadata):
            return (0.0, 1.0, "Return")

        result = classifier.classify(
            operation_state="confirmed",
            retained_properties=["balance", "audit_trail", "customer_id"],
            return_transition=return_transition,
        )
        assert result.retained_properties == ["balance", "audit_trail", "customer_id"]

    def test_different_properties_different_classification(self):
        """The same operation can have different τ⁻¹ for different Q sets."""
        classifier = TauInversionClassifier()

        def return_for_balance(metadata):
            return (0.0, 5.0, "Full refund")

        result_balance = classifier.classify(
            operation_state="confirmed",
            retained_properties=["balance"],
            return_transition=return_for_balance,
        )
        assert result_balance.classification == InversionClassification.EXACT_INVERSE

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
            retained_properties=["message_read_status"],
            return_transition=None,
        )
        d = result.to_dict()
        assert d["classification"] == "irreversible"
        assert d["distance_q"] is None
        assert d["return_cost"] is None
        assert d["loss"] is None
        assert d["is_reversible"] is False

    def test_indeterminate_to_dict_has_nulls(self):
        classifier = TauInversionClassifier()
        result = classifier.classify(
            operation_state="failed",
            retained_properties=["balance"],
        )
        d = result.to_dict()
        assert d["classification"] == "indeterminate"
        assert d["distance_q"] is None
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
            retained_properties=["balance"],
            metadata={"no_external_effect": False},
            return_transition=return_transition,
        )
        assert result.classification == InversionClassification.COMPENSATION

    def test_failed_with_external_effect_no_return_is_irreversible(self):
        classifier = TauInversionClassifier()
        result = classifier.classify(
            operation_state="failed",
            retained_properties=["balance"],
            metadata={"no_external_effect": False},
            return_transition=None,
        )
        assert result.classification == InversionClassification.IRREVERSIBLE

class TestNumericValidation:
    """Tests for distance, return_cost, and λ input validation.

    The classifier must reject invalid numeric inputs from the return-transition
    callback: NaN, infinite, or negative values. These indicate a broken callback,
    not a meaningful assessment. Report INDETERMINATE.
    """

    def test_nan_distance_is_indeterminate(self):
        classifier = TauInversionClassifier()

        def bad_distance(metadata):
            return (float("nan"), 1.0, "Bad")

        result = classifier.classify(
            operation_state="confirmed",
            retained_properties=["balance"],
            return_transition=bad_distance,
        )
        assert result.classification == InversionClassification.INDETERMINATE

    def test_infinite_distance_is_indeterminate(self):
        classifier = TauInversionClassifier()

        def inf_distance(metadata):
            return (float("inf"), 1.0, "Infinite")

        result = classifier.classify(
            operation_state="confirmed",
            retained_properties=["balance"],
            return_transition=inf_distance,
        )
        assert result.classification == InversionClassification.INDETERMINATE

    def test_negative_distance_is_indeterminate(self):
        classifier = TauInversionClassifier()

        def neg_distance(metadata):
            return (-0.5, 1.0, "Negative")

        result = classifier.classify(
            operation_state="confirmed",
            retained_properties=["balance"],
            return_transition=neg_distance,
        )
        assert result.classification == InversionClassification.INDETERMINATE

    def test_nan_return_cost_is_indeterminate(self):
        classifier = TauInversionClassifier()

        def bad_cost(metadata):
            return (0.0, float("nan"), "Bad cost")

        result = classifier.classify(
            operation_state="confirmed",
            retained_properties=["balance"],
            return_transition=bad_cost,
        )
        assert result.classification == InversionClassification.INDETERMINATE

    def test_negative_return_cost_is_indeterminate(self):
        classifier = TauInversionClassifier()

        def neg_cost(metadata):
            return (0.0, -2.0, "Negative cost")

        result = classifier.classify(
            operation_state="confirmed",
            retained_properties=["balance"],
            return_transition=neg_cost,
        )
        assert result.classification == InversionClassification.INDETERMINATE

    def test_infinite_return_cost_is_indeterminate(self):
        classifier = TauInversionClassifier()

        def inf_cost(metadata):
            return (0.0, float("inf"), "Infinite cost")

        result = classifier.classify(
            operation_state="confirmed",
            retained_properties=["balance"],
            return_transition=inf_cost,
        )
        assert result.classification == InversionClassification.INDETERMINATE

    def test_nan_lambda_is_indeterminate(self):
        classifier = TauInversionClassifier()

        def good_return(metadata):
            return (0.0, 1.0, "Exact")

        result = classifier.classify(
            operation_state="confirmed",
            retained_properties=["balance"],
            return_transition=good_return,
            lambda_weight=float("nan"),
        )
        assert result.classification == InversionClassification.INDETERMINATE

    def test_negative_lambda_is_indeterminate(self):
        classifier = TauInversionClassifier()

        def good_return(metadata):
            return (0.0, 1.0, "Exact")

        result = classifier.classify(
            operation_state="confirmed",
            retained_properties=["balance"],
            return_transition=good_return,
            lambda_weight=-0.5,
        )
        assert result.classification == InversionClassification.INDETERMINATE

    def test_infinite_lambda_is_indeterminate(self):
        classifier = TauInversionClassifier()

        def good_return(metadata):
            return (0.0, 1.0, "Exact")

        result = classifier.classify(
            operation_state="confirmed",
            retained_properties=["balance"],
            return_transition=good_return,
            lambda_weight=float("inf"),
        )
        assert result.classification == InversionClassification.INDETERMINATE

    def test_valid_inputs_still_classify_correctly(self):
        """Ensure valid inputs are not rejected by the validation."""
        classifier = TauInversionClassifier()

        def good_return(metadata):
            return (0.0, 0.0, "Exact return, no cost")

        result = classifier.classify(
            operation_state="confirmed",
            retained_properties=["balance"],
            return_transition=good_return,
            lambda_weight=0.1,
        )
        assert result.classification == InversionClassification.EXACT_INVERSE
