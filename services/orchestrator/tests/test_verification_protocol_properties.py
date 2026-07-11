"""Property-based tests for the Verification Protocol subsystem.

These tests define correctness properties for:
- Verification Failure Iteration Bound (Property 28)

Run with:
  cd services/orchestrator && .venv/bin/python -m pytest tests/test_verification_protocol_properties.py -v

Validates: Requirements 29.2, 29.3
"""

from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import given, settings, assume

from vikram_orchestrator.verification_protocol import (
    FeedbackLoopResult,
    PropertyResult,
    PropertyType,
    VerificationProtocol,
    VerificationStrategy,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_FIX_ITERATIONS = 3  # Per requirement 29.2


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


def property_id_st() -> st.SearchStrategy[str]:
    """Generate plausible property identifiers."""
    return st.text(
        min_size=1,
        max_size=30,
        alphabet=st.characters(categories=("L", "N", "Pd")),
    )


def property_type_st() -> st.SearchStrategy[PropertyType]:
    """Generate a random PropertyType."""
    return st.sampled_from(list(PropertyType))


def counterexample_st() -> st.SearchStrategy[str]:
    """Generate plausible counterexample strings."""
    return st.text(min_size=1, max_size=200)


def diagnostic_st() -> st.SearchStrategy[str]:
    """Generate plausible diagnostic messages."""
    return st.text(min_size=1, max_size=300)


def failing_property_result_st() -> st.SearchStrategy[PropertyResult]:
    """Generate a PropertyResult that represents a failure."""
    return st.builds(
        PropertyResult,
        property_id=property_id_st(),
        passed=st.just(False),
        counterexample=counterexample_st().map(lambda s: s if s else "x"),
        iterations=st.integers(min_value=1, max_value=1000),
        shrunk_input=counterexample_st().map(lambda s: s if s else "y"),
        diagnostic=diagnostic_st().map(lambda s: s if s else "failed"),
    )


def num_consecutive_failures_st() -> st.SearchStrategy[int]:
    """Generate number of consecutive failures (1 to 10)."""
    return st.integers(min_value=1, max_value=10)


def attempt_number_st() -> st.SearchStrategy[int]:
    """Generate attempt numbers (1 to 10)."""
    return st.integers(min_value=1, max_value=10)


# ---------------------------------------------------------------------------
# Property 28: Verification Failure Iteration Bound
# Validates: Requirements 29.2, 29.3
# ---------------------------------------------------------------------------


class TestVerificationFailureIterationBound:
    """The verification protocol respects a maximum iteration count.

    When verification fails repeatedly, the system allows at most 3 fix
    iterations before escalating to the founder.

    **Validates: Requirements 29.2, 29.3**
    """

    @given(
        failure=failing_property_result_st(),
        attempt=st.integers(min_value=1, max_value=MAX_FIX_ITERATIONS),
    )
    @settings(max_examples=300)
    def test_within_limit_produces_fix_prompt(
        self, failure: PropertyResult, attempt: int
    ) -> None:
        """**Validates: Requirements 29.2**

        When the attempt number is within the allowed iteration limit (1-3),
        the feedback_loop should produce a fix prompt (not escalation).
        """
        protocol = VerificationProtocol()
        result = protocol.feedback_loop(failure, attempt)

        assert result.should_retry is True, (
            f"Attempt {attempt} (within limit {MAX_FIX_ITERATIONS}) should "
            f"produce a retry, but got should_retry=False. "
            f"Result: {result}"
        )
        assert result.fix_prompt is not None and len(result.fix_prompt) > 0, (
            f"Attempt {attempt} should produce a non-empty fix prompt. "
            f"Got: {result.fix_prompt}"
        )
        assert result.escalate is False, (
            f"Attempt {attempt} (within limit) should not escalate. "
            f"Result: {result}"
        )

    @given(
        failure=failing_property_result_st(),
        attempt=st.integers(min_value=MAX_FIX_ITERATIONS + 1, max_value=20),
    )
    @settings(max_examples=300)
    def test_exceeding_limit_triggers_escalation(
        self, failure: PropertyResult, attempt: int
    ) -> None:
        """**Validates: Requirements 29.2, 29.3**

        When the attempt number exceeds the maximum iteration limit (>3),
        the feedback_loop should escalate to the founder rather than
        producing another fix prompt.
        """
        protocol = VerificationProtocol()
        result = protocol.feedback_loop(failure, attempt)

        assert result.escalate is True, (
            f"Attempt {attempt} (exceeds limit {MAX_FIX_ITERATIONS}) should "
            f"trigger escalation, but got escalate=False. "
            f"Result: {result}"
        )
        assert result.should_retry is False, (
            f"Attempt {attempt} (exceeds limit) should not allow retry. "
            f"Result: {result}"
        )

    @given(
        failure=failing_property_result_st(),
    )
    @settings(max_examples=200)
    def test_boundary_attempt_at_max_still_retries(
        self, failure: PropertyResult,
    ) -> None:
        """**Validates: Requirements 29.2**

        Attempt exactly at the maximum (attempt == 3) is the last allowed
        fix iteration and should still produce a fix prompt (not escalation).
        The escalation happens only AFTER the 3rd attempt fails.
        """
        protocol = VerificationProtocol()
        result = protocol.feedback_loop(failure, MAX_FIX_ITERATIONS)

        assert result.should_retry is True, (
            f"Attempt {MAX_FIX_ITERATIONS} (boundary) should still allow retry. "
            f"Result: {result}"
        )
        assert result.escalate is False, (
            f"Attempt {MAX_FIX_ITERATIONS} (boundary) should not escalate yet. "
            f"Result: {result}"
        )

    @given(
        failure=failing_property_result_st(),
    )
    @settings(max_examples=200)
    def test_boundary_attempt_past_max_escalates(
        self, failure: PropertyResult,
    ) -> None:
        """**Validates: Requirements 29.2, 29.3**

        Attempt at MAX_FIX_ITERATIONS + 1 is the first attempt beyond the
        limit and must escalate to the founder.
        """
        protocol = VerificationProtocol()
        result = protocol.feedback_loop(failure, MAX_FIX_ITERATIONS + 1)

        assert result.escalate is True, (
            f"Attempt {MAX_FIX_ITERATIONS + 1} should escalate. "
            f"Result: {result}"
        )
        assert result.should_retry is False, (
            f"Attempt {MAX_FIX_ITERATIONS + 1} should not allow retry. "
            f"Result: {result}"
        )

    @given(
        failure=failing_property_result_st(),
        attempt=st.integers(min_value=MAX_FIX_ITERATIONS + 1, max_value=20),
    )
    @settings(max_examples=200)
    def test_escalation_includes_failure_details(
        self, failure: PropertyResult, attempt: int
    ) -> None:
        """**Validates: Requirements 29.3**

        When escalation occurs after exceeding the iteration limit, the
        result should include the persistent failure details (counterexample,
        property info, diagnostic) for presentation to the founder.
        """
        protocol = VerificationProtocol()
        result = protocol.feedback_loop(failure, attempt)

        assert result.escalate is True
        assert result.failure_details is not None, (
            f"Escalation result must include failure_details for the founder. "
            f"Result: {result}"
        )
        # The failure details should reference the original counterexample
        assert result.failure_details.counterexample == failure.counterexample, (
            f"Escalation failure_details should preserve the original "
            f"counterexample. Expected: {failure.counterexample}, "
            f"Got: {result.failure_details.counterexample}"
        )
        assert result.failure_details.property_id == failure.property_id, (
            f"Escalation failure_details should preserve the property_id. "
            f"Expected: {failure.property_id}, "
            f"Got: {result.failure_details.property_id}"
        )

    @given(
        failure=failing_property_result_st(),
        attempt=st.integers(min_value=1, max_value=MAX_FIX_ITERATIONS),
    )
    @settings(max_examples=200)
    def test_fix_prompt_references_counterexample(
        self, failure: PropertyResult, attempt: int
    ) -> None:
        """**Validates: Requirements 29.2**

        When a fix prompt is generated (within iteration limit), it should
        reference the counterexample from the failure so the implementation
        agent has context for the targeted fix.
        """
        assume(failure.counterexample is not None and len(failure.counterexample) > 0)

        protocol = VerificationProtocol()
        result = protocol.feedback_loop(failure, attempt)

        assert result.should_retry is True
        assert result.fix_prompt is not None
        # The fix prompt should contain information about the failure
        assert failure.counterexample in result.fix_prompt or failure.property_id in result.fix_prompt, (
            f"Fix prompt should reference the counterexample or property_id. "
            f"Prompt: {result.fix_prompt}, "
            f"Counterexample: {failure.counterexample}, "
            f"Property ID: {failure.property_id}"
        )

    @given(
        failure=failing_property_result_st(),
    )
    @settings(max_examples=100)
    def test_all_attempts_sequence_respects_bound(
        self, failure: PropertyResult,
    ) -> None:
        """**Validates: Requirements 29.2, 29.3**

        Running feedback_loop for attempts 1 through MAX+1 should produce
        exactly MAX retries followed by an escalation. This validates the
        full iteration sequence.
        """
        protocol = VerificationProtocol()

        retry_count = 0
        escalated = False

        for attempt in range(1, MAX_FIX_ITERATIONS + 2):
            result = protocol.feedback_loop(failure, attempt)
            if result.should_retry:
                retry_count += 1
            if result.escalate:
                escalated = True
                break

        assert retry_count == MAX_FIX_ITERATIONS, (
            f"Expected exactly {MAX_FIX_ITERATIONS} retries before escalation, "
            f"got {retry_count}."
        )
        assert escalated is True, (
            f"Expected escalation after {MAX_FIX_ITERATIONS} retries."
        )
