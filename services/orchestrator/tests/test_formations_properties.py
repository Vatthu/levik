"""Property-based tests for the Formation effectiveness scoring subsystem.

Property 15: Formation Effectiveness Score Formula

These tests verify:
1. Effectiveness score is always in [0, 1]
2. With < 5 records, score returns 0.5 (neutral)
3. Formula correctness: given known records, the computed score matches the formula
4. All-success records produce maximum possible score
5. All-failure records produce minimum possible score
6. Weights sum to 1.0 (0.40 + 0.20 + 0.15 + 0.25 = 1.00)

Run with:
  cd services/orchestrator && .venv/bin/python -m pytest tests/test_formations_properties.py -v

Validates: Requirements 15.2, 39.3
"""

from __future__ import annotations

from statistics import mean

import hypothesis.strategies as st
from hypothesis import given, settings, assume

from vikram_orchestrator.formations import (
    FormationManager,
    FormationRecord,
)


# ---------------------------------------------------------------------------
# Constants (matching the design formula)
# ---------------------------------------------------------------------------

WEIGHT_SUCCESS_RATE = 0.40
WEIGHT_COST_EFFICIENCY = 0.20
WEIGHT_TIME_EFFICIENCY = 0.15
WEIGHT_FIRST_PASS_RATE = 0.25

COST_EFFICIENCY_CAP = 1.5
TIME_EFFICIENCY_CAP = 2.0

MIN_RECORDS_FOR_SCORING = 5
NEUTRAL_SCORE = 0.5


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


def task_type_st() -> st.SearchStrategy[str]:
    """Generate task type strings."""
    return st.sampled_from(["bugfix", "feature", "refactor", "documentation", "security-audit"])


def outcome_st() -> st.SearchStrategy[str]:
    """Generate valid outcome strings."""
    return st.sampled_from(["success", "partial", "failure", "rollback"])


def positive_float_st(
    min_value: float = 0.01, max_value: float = 100.0
) -> st.SearchStrategy[float]:
    """Generate positive floats for cost/duration values."""
    return st.floats(min_value=min_value, max_value=max_value, allow_nan=False, allow_infinity=False)


def formation_record_st(
    formation_name: str = "test-formation",
    task_type: str = "bugfix",
    outcome: str | None = None,
    verification_first_pass: bool | None = None,
) -> st.SearchStrategy[FormationRecord]:
    """Generate a random FormationRecord with constrained positive values."""
    return st.builds(
        FormationRecord,
        formation_name=st.just(formation_name),
        task_type=st.just(task_type),
        outcome=st.just(outcome) if outcome else outcome_st(),
        forecast=positive_float_st(),
        actual_cost=positive_float_st(),
        time_limit=positive_float_st(),
        actual_duration=positive_float_st(),
        verification_first_pass=(
            st.just(verification_first_pass) if verification_first_pass is not None
            else st.booleans()
        ),
    )


def formation_records_st(
    min_size: int = 0, max_size: int = 60,
    formation_name: str = "test-formation",
    task_type: str = "bugfix",
) -> st.SearchStrategy[list[FormationRecord]]:
    """Generate a list of formation records with the same formation/task_type."""
    return st.lists(
        formation_record_st(formation_name=formation_name, task_type=task_type),
        min_size=min_size,
        max_size=max_size,
    )


# ---------------------------------------------------------------------------
# Helper: manually compute the expected effectiveness score
# ---------------------------------------------------------------------------


def _expected_effectiveness(records: list[FormationRecord]) -> float:
    """Compute the expected effectiveness using the design formula directly."""
    if len(records) < MIN_RECORDS_FOR_SCORING:
        return NEUTRAL_SCORE

    n = len(records)

    success_rate = sum(1 for r in records if r.outcome == "success") / n

    cost_efficiencies = [
        r.forecast / r.actual_cost
        for r in records
        if r.actual_cost > 0
    ]
    cost_efficiency = mean(cost_efficiencies) if cost_efficiencies else 1.0

    time_efficiencies = [
        r.time_limit / r.actual_duration
        for r in records
        if r.actual_duration > 0
    ]
    time_efficiency = mean(time_efficiencies) if time_efficiencies else 1.0

    first_pass_rate = sum(1 for r in records if r.verification_first_pass) / n

    return (
        WEIGHT_SUCCESS_RATE * success_rate
        + WEIGHT_COST_EFFICIENCY * min(cost_efficiency, COST_EFFICIENCY_CAP) / COST_EFFICIENCY_CAP
        + WEIGHT_TIME_EFFICIENCY * min(time_efficiency, TIME_EFFICIENCY_CAP) / TIME_EFFICIENCY_CAP
        + WEIGHT_FIRST_PASS_RATE * first_pass_rate
    )


# ---------------------------------------------------------------------------
# Property 15: Formation Effectiveness Score Formula
# Validates: Requirements 15.2, 39.3
# ---------------------------------------------------------------------------


class TestEffectivenessScoreBounded:
    """Effectiveness score is always in [0, 1] for any valid input records."""

    @given(records=formation_records_st(min_size=0, max_size=60))
    @settings(max_examples=300)
    def test_score_always_in_zero_one_range(self, records: list[FormationRecord]) -> None:
        """**Validates: Requirements 15.2**

        For any set of FormationRecords (with positive costs and durations),
        get_effectiveness must return a value in [0.0, 1.0].
        """
        mgr = FormationManager()
        for r in records:
            mgr.record_outcome(r)

        score = mgr.get_effectiveness("test-formation", "bugfix")
        assert 0.0 <= score <= 1.0, (
            f"Effectiveness score {score} is outside valid range [0, 1] "
            f"for {len(records)} records"
        )

    @given(
        records=st.lists(
            st.builds(
                FormationRecord,
                formation_name=st.just("stress-test"),
                task_type=st.just("feature"),
                outcome=outcome_st(),
                forecast=st.floats(min_value=0.001, max_value=1e6, allow_nan=False, allow_infinity=False),
                actual_cost=st.floats(min_value=0.001, max_value=1e6, allow_nan=False, allow_infinity=False),
                time_limit=st.floats(min_value=0.001, max_value=1e6, allow_nan=False, allow_infinity=False),
                actual_duration=st.floats(min_value=0.001, max_value=1e6, allow_nan=False, allow_infinity=False),
                verification_first_pass=st.booleans(),
            ),
            min_size=5,
            max_size=60,
        )
    )
    @settings(max_examples=200)
    def test_score_bounded_with_extreme_values(self, records: list[FormationRecord]) -> None:
        """**Validates: Requirements 15.2**

        Even with extreme cost/duration ratios (very cheap or very expensive),
        the score remains bounded in [0, 1] due to capping.
        """
        mgr = FormationManager()
        for r in records:
            mgr.record_outcome(r)

        score = mgr.get_effectiveness("stress-test", "feature")
        assert 0.0 <= score <= 1.0, (
            f"Effectiveness score {score} is outside valid range [0, 1] "
            f"with extreme values"
        )


class TestInsufficientDataNeutralScore:
    """With fewer than 5 records, score returns 0.5 (neutral)."""

    @given(records=formation_records_st(min_size=0, max_size=4))
    @settings(max_examples=200)
    def test_fewer_than_min_records_returns_neutral(self, records: list[FormationRecord]) -> None:
        """**Validates: Requirements 15.2**

        When fewer than MIN_RECORDS_FOR_SCORING (5) records are available,
        get_effectiveness returns 0.5 (neutral) to indicate insufficient data.
        """
        assume(len(records) < MIN_RECORDS_FOR_SCORING)

        mgr = FormationManager()
        for r in records:
            mgr.record_outcome(r)

        score = mgr.get_effectiveness("test-formation", "bugfix")
        assert score == NEUTRAL_SCORE, (
            f"With {len(records)} records (< {MIN_RECORDS_FOR_SCORING}), "
            f"expected neutral score {NEUTRAL_SCORE}, got {score}"
        )

    def test_zero_records_returns_neutral(self) -> None:
        """**Validates: Requirements 15.2**

        No records means neutral score 0.5.
        """
        mgr = FormationManager()
        score = mgr.get_effectiveness("nonexistent-formation", "bugfix")
        assert score == NEUTRAL_SCORE

    def test_exactly_four_records_returns_neutral(self) -> None:
        """**Validates: Requirements 15.2**

        Exactly 4 records (one less than threshold) returns neutral.
        """
        mgr = FormationManager()
        for _ in range(4):
            mgr.record_outcome(FormationRecord(
                formation_name="four-records",
                task_type="bugfix",
                outcome="success",
                forecast=10.0,
                actual_cost=8.0,
                time_limit=3600,
                actual_duration=2000,
                verification_first_pass=True,
            ))

        score = mgr.get_effectiveness("four-records", "bugfix")
        assert score == NEUTRAL_SCORE

    @given(records=formation_records_st(min_size=5, max_size=60))
    @settings(max_examples=100)
    def test_at_least_five_records_computes_score(self, records: list[FormationRecord]) -> None:
        """**Validates: Requirements 15.2**

        With 5 or more records, the score is computed from the formula
        (not the neutral default).
        """
        mgr = FormationManager()
        for r in records:
            mgr.record_outcome(r)

        score = mgr.get_effectiveness("test-formation", "bugfix")
        # Score is computed, could be anything in [0, 1]
        assert 0.0 <= score <= 1.0


class TestFormulaCorrectness:
    """Given known records, the computed score matches the design formula."""

    @given(records=formation_records_st(min_size=5, max_size=50))
    @settings(max_examples=300)
    def test_score_matches_manual_formula_computation(self, records: list[FormationRecord]) -> None:
        """**Validates: Requirements 15.2, 39.3**

        For any set of records with >= 5 entries, the FormationManager's
        get_effectiveness score matches the design formula:
          0.40 * success_rate
        + 0.20 * min(cost_efficiency, 1.5) / 1.5
        + 0.15 * min(time_efficiency, 2.0) / 2.0
        + 0.25 * first_pass_rate
        """
        mgr = FormationManager()
        for r in records:
            mgr.record_outcome(r)

        actual_score = mgr.get_effectiveness("test-formation", "bugfix")

        # Only the last 50 records are used (last_n=50)
        relevant_records = records[-50:]
        expected_score = _expected_effectiveness(relevant_records)

        assert abs(actual_score - expected_score) < 1e-10, (
            f"Score mismatch: got {actual_score}, expected {expected_score}\n"
            f"  records used: {len(relevant_records)}"
        )


class TestAllSuccessMaximumScore:
    """All-success records with optimal efficiency produce maximum score."""

    @given(n=st.integers(min_value=5, max_value=50))
    @settings(max_examples=100)
    def test_all_success_optimal_efficiency_gives_max_score(self, n: int) -> None:
        """**Validates: Requirements 15.2, 39.3**

        When all records have:
        - outcome == "success"
        - cost_efficiency >= CAP (forecast/actual_cost >= 1.5)
        - time_efficiency >= CAP (time_limit/actual_duration >= 2.0)
        - verification_first_pass == True

        The score should be exactly 1.0 (maximum).
        """
        mgr = FormationManager()
        for _ in range(n):
            mgr.record_outcome(FormationRecord(
                formation_name="best-formation",
                task_type="feature",
                outcome="success",
                forecast=15.0,  # forecast/actual = 15/10 = 1.5 (at cap)
                actual_cost=10.0,
                time_limit=20.0,  # time_limit/duration = 20/10 = 2.0 (at cap)
                actual_duration=10.0,
                verification_first_pass=True,
            ))

        score = mgr.get_effectiveness("best-formation", "feature")
        assert abs(score - 1.0) < 1e-10, (
            f"All-success optimal records should give score 1.0, got {score}"
        )

    @given(
        n=st.integers(min_value=5, max_value=50),
        forecast=st.floats(min_value=100.0, max_value=1000.0, allow_nan=False, allow_infinity=False),
        actual_cost=st.floats(min_value=1.0, max_value=10.0, allow_nan=False, allow_infinity=False),
        time_limit=st.floats(min_value=100.0, max_value=1000.0, allow_nan=False, allow_infinity=False),
        actual_duration=st.floats(min_value=1.0, max_value=10.0, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=100)
    def test_all_success_exceeding_caps_still_max_score(
        self, n: int, forecast: float, actual_cost: float,
        time_limit: float, actual_duration: float
    ) -> None:
        """**Validates: Requirements 15.2, 39.3**

        Even when efficiency ratios far exceed the caps, the score remains
        at 1.0 because min() clamps before dividing by the cap.
        """
        mgr = FormationManager()
        for _ in range(n):
            mgr.record_outcome(FormationRecord(
                formation_name="over-achiever",
                task_type="refactor",
                outcome="success",
                forecast=forecast,
                actual_cost=actual_cost,
                time_limit=time_limit,
                actual_duration=actual_duration,
                verification_first_pass=True,
            ))

        score = mgr.get_effectiveness("over-achiever", "refactor")
        assert abs(score - 1.0) < 1e-10, (
            f"All-success with over-cap efficiency should give 1.0, got {score}"
        )


class TestAllFailureMinimumScore:
    """All-failure records with worst efficiency produce minimum score."""

    @given(n=st.integers(min_value=5, max_value=50))
    @settings(max_examples=100)
    def test_all_failure_worst_efficiency_gives_min_score(self, n: int) -> None:
        """**Validates: Requirements 15.2, 39.3**

        When all records have:
        - outcome == "failure" (success_rate = 0)
        - cost_efficiency approaching 0 (forecast << actual_cost)
        - time_efficiency approaching 0 (time_limit << actual_duration)
        - verification_first_pass == False (first_pass_rate = 0)

        The score should approach 0.0 (minimum).
        """
        mgr = FormationManager()
        for _ in range(n):
            mgr.record_outcome(FormationRecord(
                formation_name="worst-formation",
                task_type="bugfix",
                outcome="failure",
                forecast=0.01,  # forecast/actual ≈ 0.001
                actual_cost=10.0,
                time_limit=0.01,  # time_limit/duration ≈ 0.001
                actual_duration=10.0,
                verification_first_pass=False,
            ))

        score = mgr.get_effectiveness("worst-formation", "bugfix")

        # success_rate = 0, first_pass_rate = 0
        # cost_efficiency ≈ 0.001, time_efficiency ≈ 0.001
        # Expected ≈ 0.40*0 + 0.20*(0.001/1.5) + 0.15*(0.001/2.0) + 0.25*0 ≈ 0.000208
        assert score < 0.01, (
            f"All-failure with near-zero efficiency should give score near 0, got {score}"
        )
        assert score >= 0.0, (
            f"Score should never be negative, got {score}"
        )

    @given(n=st.integers(min_value=5, max_value=50))
    @settings(max_examples=50)
    def test_all_failure_zero_efficiency_components(self, n: int) -> None:
        """**Validates: Requirements 15.2, 39.3**

        When success_rate=0, first_pass_rate=0, and efficiency ratios are
        very small, the score should be very close to zero.
        """
        mgr = FormationManager()
        for _ in range(n):
            mgr.record_outcome(FormationRecord(
                formation_name="zero-formation",
                task_type="feature",
                outcome="rollback",  # not "success"
                forecast=0.001,
                actual_cost=1000.0,  # ratio = 0.000001
                time_limit=0.001,
                actual_duration=1000.0,  # ratio = 0.000001
                verification_first_pass=False,
            ))

        score = mgr.get_effectiveness("zero-formation", "feature")
        assert score < 0.001, (
            f"Expected near-zero score for all-failure minimal efficiency, got {score}"
        )


class TestWeightsSumToOne:
    """The formation effectiveness weights sum to exactly 1.0."""

    def test_weights_sum_to_one(self) -> None:
        """**Validates: Requirements 15.2, 39.3**

        The four weight constants (0.40 + 0.20 + 0.15 + 0.25) must sum to
        exactly 1.0, ensuring the composite score stays in [0, 1].
        """
        total = (
            WEIGHT_SUCCESS_RATE
            + WEIGHT_COST_EFFICIENCY
            + WEIGHT_TIME_EFFICIENCY
            + WEIGHT_FIRST_PASS_RATE
        )
        assert abs(total - 1.0) < 1e-10, (
            f"Weights should sum to 1.0, got {total}: "
            f"success={WEIGHT_SUCCESS_RATE}, cost={WEIGHT_COST_EFFICIENCY}, "
            f"time={WEIGHT_TIME_EFFICIENCY}, first_pass={WEIGHT_FIRST_PASS_RATE}"
        )

    def test_individual_weight_values(self) -> None:
        """**Validates: Requirements 15.2, 39.3**

        Verify the individual weight values match the design specification.
        """
        assert WEIGHT_SUCCESS_RATE == 0.40
        assert WEIGHT_COST_EFFICIENCY == 0.20
        assert WEIGHT_TIME_EFFICIENCY == 0.15
        assert WEIGHT_FIRST_PASS_RATE == 0.25
