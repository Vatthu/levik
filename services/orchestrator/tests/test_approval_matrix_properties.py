"""Property-based tests for the Approval Matrix subsystem.

These tests define correctness properties for:
- First-matching-rule evaluation (Property 23)
- Confidence score arithmetic with ceiling (Property 24)
- Risk classification maximum principle (Property 25)

Run with:
  cd services/orchestrator && .venv/bin/python -m pytest tests/test_approval_matrix_properties.py -v

Validates: Requirements 30.1, 30.2, 30.4, 32.1, 32.2, 32.5, 34.3
"""

from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import given, settings, assume

from vikram_orchestrator.approval_matrix import (
    ApprovalMatrix,
    ApprovalPolicyDecision,
    ConfidenceScore,
    DEFAULT_DECREMENT,
    DEFAULT_INCREMENT,
    PolicyRule,
    RiskClassification,
    RISK_LEVEL_ORDER,
    RuleConditions,
    TIER_CEILINGS,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

VALID_ROUTINGS = ["auto_approve", "founder_review", "escalate_and_halt"]
VALID_RISK_LEVELS = ["low", "medium", "high", "critical"]
VALID_TIERS = ["routine", "moderate", "complex", "critical"]


def approval_routing_st() -> st.SearchStrategy[str]:
    """Generate valid approval routing values."""
    return st.sampled_from(VALID_ROUTINGS)


def risk_level_st() -> st.SearchStrategy[str]:
    """Generate valid risk level strings."""
    return st.sampled_from(VALID_RISK_LEVELS)


def complexity_tier_st() -> st.SearchStrategy[str]:
    """Generate valid complexity tier strings."""
    return st.sampled_from(VALID_TIERS)


def rule_conditions_st() -> st.SearchStrategy[RuleConditions]:
    """Generate rule conditions — some fields may be None (unconditional)."""
    return st.builds(
        RuleConditions,
        risk_level=st.one_of(st.none(), st.lists(risk_level_st(), min_size=1, max_size=4)),
        file_patterns=st.none(),  # Simplify for property testing
        max_lines_changed=st.one_of(st.none(), st.integers(min_value=1, max_value=10000)),
        max_files_changed=st.one_of(st.none(), st.integers(min_value=1, max_value=100)),
        min_confidence_score=st.one_of(
            st.none(),
            st.floats(min_value=0.0, max_value=60.0, allow_nan=False, allow_infinity=False),
        ),
        max_cost_consumed_pct=st.one_of(
            st.none(),
            st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
        ),
        repo_path_prefix=st.none(),
    )


def policy_rule_st() -> st.SearchStrategy[PolicyRule]:
    """Generate a policy rule."""
    return st.builds(
        PolicyRule,
        name=st.text(min_size=1, max_size=30, alphabet=st.characters(categories=("L", "N", "Pd"))),
        priority=st.integers(min_value=1, max_value=1000),
        conditions=rule_conditions_st(),
        routing=approval_routing_st(),
    )


def policy_rules_st(min_size: int = 1, max_size: int = 10) -> st.SearchStrategy[list[PolicyRule]]:
    """Generate a list of policy rules with unique priorities."""
    return st.lists(
        policy_rule_st(),
        min_size=min_size,
        max_size=max_size,
    ).filter(lambda rules: len({r.priority for r in rules}) == len(rules))


def change_context_st() -> st.SearchStrategy[dict]:
    """Generate a change context dict with plausible field values."""
    return st.fixed_dictionaries({
        "risk_level": risk_level_st(),
        "lines_changed": st.integers(min_value=0, max_value=50000),
        "files_changed": st.integers(min_value=0, max_value=200),
        "confidence_score": st.floats(
            min_value=0.0, max_value=60.0, allow_nan=False, allow_infinity=False
        ),
        "cost_consumed_pct": st.floats(
            min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False
        ),
        "changed_files": st.just([]),  # Simplify file pattern matching for properties
        "repo_path": st.text(min_size=0, max_size=50),
    })


def risk_signal_st() -> st.SearchStrategy[dict[str, str]]:
    """Generate a risk signal dict with name and level."""
    return st.fixed_dictionaries({
        "name": st.text(min_size=1, max_size=20, alphabet=st.characters(categories=("L", "N"))),
        "level": risk_level_st(),
    })


def risk_signals_st(min_size: int = 0, max_size: int = 10) -> st.SearchStrategy[list[dict[str, str]]]:
    """Generate a list of risk signals."""
    return st.lists(risk_signal_st(), min_size=min_size, max_size=max_size)


def confidence_operations_st() -> st.SearchStrategy[list[str]]:
    """Generate a sequence of increment/decrement operations."""
    return st.lists(
        st.sampled_from(["increment", "decrement"]),
        min_size=1,
        max_size=100,
    )


# ---------------------------------------------------------------------------
# Property 23: Approval Matrix First-Matching-Rule Evaluation
# Validates: Requirements 30.1, 30.2, 30.4
# ---------------------------------------------------------------------------


class TestFirstMatchingRuleEvaluation:
    """evaluate() processes rules in priority order (lowest number = highest
    priority) and returns the routing of the FIRST rule whose conditions
    are satisfied."""

    @given(rules=policy_rules_st(), context=change_context_st())
    @settings(max_examples=200)
    def test_evaluate_returns_valid_routing(
        self, rules: list[PolicyRule], context: dict
    ) -> None:
        """**Validates: Requirements 30.1, 30.2**

        evaluate() always returns a valid ApprovalPolicyDecision with a
        recognized routing value regardless of rules and context.
        """
        matrix = ApprovalMatrix(rules=rules)
        decision = matrix.evaluate(context)

        assert isinstance(decision, ApprovalPolicyDecision)
        assert decision.routing in VALID_ROUTINGS

    @given(rules=policy_rules_st(), context=change_context_st())
    @settings(max_examples=200)
    def test_evaluate_respects_priority_order(
        self, rules: list[PolicyRule], context: dict
    ) -> None:
        """**Validates: Requirements 30.2**

        When multiple rules match, the rule with the lowest priority number
        (highest priority) determines the routing.
        """
        matrix = ApprovalMatrix(rules=rules)
        decision = matrix.evaluate(context)

        # Manually find which rules match and verify we got the highest-priority one
        sorted_rules = sorted(rules, key=lambda r: r.priority)
        matching_rules = [
            r for r in sorted_rules if matrix._rule_matches(r, context)
        ]

        if matching_rules:
            # The first matching rule in priority order should be the one selected
            expected_rule = matching_rules[0]
            assert decision.routing == expected_rule.routing, (
                f"Expected routing '{expected_rule.routing}' from rule '{expected_rule.name}' "
                f"(priority={expected_rule.priority}), got '{decision.routing}' "
                f"from rule '{decision.matched_rule_name}'"
            )
            assert decision.matched_rule_name == expected_rule.name
        else:
            # No rule matches — should default to founder_review
            assert decision.routing == "founder_review"
            assert decision.matched_rule_name is None

    @given(context=change_context_st())
    @settings(max_examples=100)
    def test_no_rules_defaults_to_founder_review(self, context: dict) -> None:
        """**Validates: Requirements 30.4**

        When the matrix has no rules, evaluate() defaults to founder_review.
        """
        matrix = ApprovalMatrix(rules=[])
        decision = matrix.evaluate(context)

        assert decision.routing == "founder_review"
        assert decision.matched_rule_name is None

    @given(rules=policy_rules_st(), context=change_context_st())
    @settings(max_examples=200)
    def test_no_match_defaults_to_founder_review(
        self, rules: list[PolicyRule], context: dict
    ) -> None:
        """**Validates: Requirements 30.4**

        When no rule in the matrix matches the context, the result is
        founder_review.
        """
        matrix = ApprovalMatrix(rules=rules)
        decision = matrix.evaluate(context)

        sorted_rules = sorted(rules, key=lambda r: r.priority)
        matching_rules = [
            r for r in sorted_rules if matrix._rule_matches(r, context)
        ]

        if not matching_rules:
            assert decision.routing == "founder_review"
            assert decision.matched_rule_name is None

    @given(data=st.data())
    @settings(max_examples=100)
    def test_first_match_not_later_match_wins(self, data: st.DataObject) -> None:
        """**Validates: Requirements 30.2**

        If we construct two rules where both match but have different
        priorities, the one with lower priority number wins.
        """
        routing_a = data.draw(approval_routing_st())
        routing_b = data.draw(approval_routing_st())
        assume(routing_a != routing_b)

        # Both rules have empty conditions (match everything)
        rule_high = PolicyRule(
            name="high-priority",
            priority=1,
            conditions=RuleConditions(),
            routing=routing_a,
        )
        rule_low = PolicyRule(
            name="low-priority",
            priority=100,
            conditions=RuleConditions(),
            routing=routing_b,
        )

        # Test both orderings of rule insertion to ensure sort is correct
        matrix = ApprovalMatrix(rules=[rule_low, rule_high])
        context = data.draw(change_context_st())
        decision = matrix.evaluate(context)

        assert decision.routing == routing_a, (
            f"Higher-priority rule (priority=1, routing={routing_a}) should win "
            f"over lower-priority rule (priority=100, routing={routing_b}), "
            f"but got {decision.routing}"
        )
        assert decision.matched_rule_name == "high-priority"


# ---------------------------------------------------------------------------
# Property 24: Confidence Score Arithmetic with Ceiling
# Validates: Requirements 32.1, 32.2, 32.5
# ---------------------------------------------------------------------------


class TestConfidenceScoreArithmetic:
    """Confidence score never exceeds its configured ceiling for the tier.
    increment/decrement follows asymmetric +1/-3 default."""

    @given(
        tier=complexity_tier_st(),
        operations=confidence_operations_st(),
    )
    @settings(max_examples=200)
    def test_score_never_exceeds_ceiling(
        self, tier: str, operations: list[str]
    ) -> None:
        """**Validates: Requirements 32.5**

        After any sequence of increment/decrement operations, the confidence
        score never exceeds the configured ceiling for the tier.
        """
        matrix = ApprovalMatrix()
        repo = "test-repo"
        ceiling = TIER_CEILINGS[tier]

        for op in operations:
            if op == "increment":
                matrix.increment_confidence(tier, repo)
            else:
                matrix.decrement_confidence(tier, repo)

            cs = matrix.get_confidence(tier, repo)
            assert cs.score <= ceiling, (
                f"Score {cs.score} exceeds ceiling {ceiling} for tier '{tier}' "
                f"after operation '{op}'"
            )

    @given(
        tier=complexity_tier_st(),
        operations=confidence_operations_st(),
    )
    @settings(max_examples=200)
    def test_score_never_goes_below_zero(
        self, tier: str, operations: list[str]
    ) -> None:
        """**Validates: Requirements 32.2**

        After any sequence of increment/decrement operations, the confidence
        score never drops below zero.
        """
        matrix = ApprovalMatrix()
        repo = "test-repo"

        for op in operations:
            if op == "increment":
                matrix.increment_confidence(tier, repo)
            else:
                matrix.decrement_confidence(tier, repo)

            cs = matrix.get_confidence(tier, repo)
            assert cs.score >= 0.0, (
                f"Score {cs.score} went below zero for tier '{tier}' "
                f"after operation '{op}'"
            )

    @given(
        tier=complexity_tier_st(),
        num_increments=st.integers(min_value=1, max_value=200),
    )
    @settings(max_examples=200)
    def test_increment_respects_ceiling(
        self, tier: str, num_increments: int
    ) -> None:
        """**Validates: Requirements 32.1, 32.5**

        Incrementing many times saturates at the ceiling and never exceeds it.
        Default increment is +1.
        """
        matrix = ApprovalMatrix()
        repo = "ceiling-test"
        ceiling = TIER_CEILINGS[tier]

        for _ in range(num_increments):
            matrix.increment_confidence(tier, repo)

        cs = matrix.get_confidence(tier, repo)
        expected = min(num_increments * DEFAULT_INCREMENT, ceiling)
        assert abs(cs.score - expected) < 1e-9, (
            f"After {num_increments} increments, score should be {expected} "
            f"(ceiling={ceiling}), got {cs.score}"
        )

    @given(
        tier=complexity_tier_st(),
        num_decrements=st.integers(min_value=1, max_value=200),
    )
    @settings(max_examples=200)
    def test_decrement_floors_at_zero(
        self, tier: str, num_decrements: int
    ) -> None:
        """**Validates: Requirements 32.2**

        Decrementing from zero always stays at zero. Default decrement is -3.
        """
        matrix = ApprovalMatrix()
        repo = "floor-test"

        for _ in range(num_decrements):
            matrix.decrement_confidence(tier, repo)

        cs = matrix.get_confidence(tier, repo)
        assert cs.score == 0.0, (
            f"After {num_decrements} decrements from 0, score should be 0.0, "
            f"got {cs.score}"
        )

    @given(
        tier=complexity_tier_st(),
        increments=st.integers(min_value=0, max_value=100),
        decrements=st.integers(min_value=0, max_value=100),
    )
    @settings(max_examples=200)
    def test_asymmetric_increment_decrement(
        self, tier: str, increments: int, decrements: int
    ) -> None:
        """**Validates: Requirements 32.1, 32.2**

        Increment is +1 (default), decrement is -3 (default). The score
        follows these asymmetric steps clamped to [0, ceiling].
        """
        matrix = ApprovalMatrix()
        repo = "asymmetric-test"
        ceiling = TIER_CEILINGS[tier]

        # Apply all increments first, then all decrements
        for _ in range(increments):
            matrix.increment_confidence(tier, repo)
        for _ in range(decrements):
            matrix.decrement_confidence(tier, repo)

        cs = matrix.get_confidence(tier, repo)

        # Compute expected: increment up to ceiling, then decrement down to 0
        after_increments = min(increments * DEFAULT_INCREMENT, ceiling)
        expected = max(after_increments - decrements * DEFAULT_DECREMENT, 0.0)

        assert abs(cs.score - expected) < 1e-9, (
            f"After {increments} increments and {decrements} decrements, "
            f"expected score={expected}, got {cs.score} "
            f"(ceiling={ceiling}, tier={tier})"
        )

    @given(
        tier=complexity_tier_st(),
        custom_increment=st.floats(min_value=0.1, max_value=10.0, allow_nan=False, allow_infinity=False),
        custom_decrement=st.floats(min_value=0.1, max_value=20.0, allow_nan=False, allow_infinity=False),
        operations=confidence_operations_st(),
    )
    @settings(max_examples=200)
    def test_custom_amounts_respect_bounds(
        self,
        tier: str,
        custom_increment: float,
        custom_decrement: float,
        operations: list[str],
    ) -> None:
        """**Validates: Requirements 32.1, 32.2, 32.5**

        Even with custom increment/decrement amounts, the score is always
        within [0, ceiling].
        """
        matrix = ApprovalMatrix()
        repo = "custom-amounts"
        ceiling = TIER_CEILINGS[tier]

        for op in operations:
            if op == "increment":
                matrix.increment_confidence(tier, repo, amount=custom_increment)
            else:
                matrix.decrement_confidence(tier, repo, amount=custom_decrement)

            cs = matrix.get_confidence(tier, repo)
            assert 0.0 <= cs.score <= ceiling, (
                f"Score {cs.score} out of bounds [0, {ceiling}] for tier '{tier}' "
                f"after op='{op}' with amounts inc={custom_increment}, dec={custom_decrement}"
            )


# ---------------------------------------------------------------------------
# Property 25: Risk Classification Maximum Principle
# Validates: Requirements 34.3
# ---------------------------------------------------------------------------


class TestRiskClassificationMaximumPrinciple:
    """The overall risk level is the MAXIMUM of all applicable risk signals."""

    @given(signals=risk_signals_st(min_size=1, max_size=20))
    @settings(max_examples=200)
    def test_overall_risk_is_maximum_of_signals(
        self, signals: list[dict[str, str]]
    ) -> None:
        """**Validates: Requirements 34.3**

        When a change touches files matching multiple risk levels, the overall
        risk is the highest applicable level.
        """
        classification = ApprovalMatrix.classify_risk(signals)

        # The maximum level is the one with the highest ordering value
        expected_max_level = max(
            (s["level"] for s in signals),
            key=lambda lvl: RISK_LEVEL_ORDER[lvl],
        )
        assert classification.level == expected_max_level, (
            f"Overall risk should be '{expected_max_level}' (the maximum), "
            f"got '{classification.level}'. Signals: {[(s['name'], s['level']) for s in signals]}"
        )

    @given(signals=risk_signals_st(min_size=1, max_size=20))
    @settings(max_examples=200)
    def test_risk_never_below_any_signal(
        self, signals: list[dict[str, str]]
    ) -> None:
        """**Validates: Requirements 34.3**

        The overall risk classification is never lower than any individual signal.
        """
        classification = ApprovalMatrix.classify_risk(signals)

        for signal in signals:
            assert RISK_LEVEL_ORDER[classification.level] >= RISK_LEVEL_ORDER[signal["level"]], (
                f"Overall risk '{classification.level}' is below signal "
                f"'{signal['name']}' with level '{signal['level']}'"
            )

    @given(level=risk_level_st())
    @settings(max_examples=50)
    def test_single_signal_determines_risk(self, level: str) -> None:
        """**Validates: Requirements 34.3**

        With a single signal, the overall risk equals that signal's level.
        """
        signal = {"name": "single-signal", "level": level}
        classification = ApprovalMatrix.classify_risk([signal])

        assert classification.level == level

    def test_empty_signals_defaults_to_low(self) -> None:
        """**Validates: Requirements 34.3**

        With no signals, the overall risk defaults to LOW.
        """
        classification = ApprovalMatrix.classify_risk([])
        assert classification.level == "low"

    @given(
        non_critical_signals=st.lists(
            st.fixed_dictionaries({
                "name": st.text(min_size=1, max_size=10, alphabet=st.characters(categories=("L",))),
                "level": st.sampled_from(["low", "medium", "high"]),
            }),
            min_size=0,
            max_size=10,
        )
    )
    @settings(max_examples=100)
    def test_critical_signal_always_wins(
        self, non_critical_signals: list[dict[str, str]]
    ) -> None:
        """**Validates: Requirements 34.3**

        If any signal is CRITICAL, the overall risk is always CRITICAL
        regardless of other signals.
        """
        critical_signal = {"name": "critical-trigger", "level": "critical"}
        all_signals = non_critical_signals + [critical_signal]

        classification = ApprovalMatrix.classify_risk(all_signals)
        assert classification.level == "critical", (
            f"With a CRITICAL signal present, overall risk should be 'critical', "
            f"got '{classification.level}'"
        )

    @given(signals=risk_signals_st(min_size=1, max_size=20))
    @settings(max_examples=100)
    def test_classification_returns_valid_risk_level(
        self, signals: list[dict[str, str]]
    ) -> None:
        """**Validates: Requirements 34.3**

        The classification result always returns a valid risk level string.
        """
        classification = ApprovalMatrix.classify_risk(signals)
        assert classification.level in VALID_RISK_LEVELS
        assert isinstance(classification, RiskClassification)
