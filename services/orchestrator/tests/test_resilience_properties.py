"""Property-based tests for the Resilience module — retry backoff formula.

Property 29: Retry Backoff Formula
For any agent call retry sequence, the delay before attempt N SHALL equal
min(base_seconds * 2^(N-1), max_delay_seconds) where base_seconds=1 and
max_delay_seconds=60. After max_attempts=3 exhausted, fallback chain activates.

Run with:
  cd services/orchestrator && .venv/bin/python -m pytest tests/test_resilience_properties.py -v

Validates: Requirements 53.1, 53.2
"""

from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import given, settings, assume

from vikram_orchestrator.resilience import (
    RetryConfig,
    compute_backoff_delay,
    compute_retry_sequence,
    BASE_DELAY_SECONDS,
    BACKOFF_MULTIPLIER,
    MAX_DELAY_SECONDS,
    MAX_ATTEMPTS,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


def retry_config_st() -> st.SearchStrategy[RetryConfig]:
    """Generate valid RetryConfig instances with reasonable parameter ranges."""
    return st.builds(
        RetryConfig,
        base_delay=st.floats(min_value=0.1, max_value=10.0, allow_nan=False, allow_infinity=False),
        multiplier=st.integers(min_value=2, max_value=5),
        max_delay=st.floats(min_value=10.0, max_value=120.0, allow_nan=False, allow_infinity=False),
        max_attempts=st.integers(min_value=1, max_value=10),
    )


def attempt_for_config_st(config: RetryConfig) -> st.SearchStrategy[int]:
    """Generate a valid attempt number for the given config."""
    return st.integers(min_value=1, max_value=config.max_attempts)


# ---------------------------------------------------------------------------
# Property 29: Retry Backoff Formula
# Validates: Requirements 53.1, 53.2
# ---------------------------------------------------------------------------


class TestRetryBackoffFormula:
    """The delay for attempt N equals min(base * multiplier^(N-1), max_delay).
    Delays are monotonically non-decreasing up to the cap.
    No delay exceeds max_delay. Exactly max_attempts before fallback."""

    @given(config=retry_config_st(), data=st.data())
    @settings(max_examples=200)
    def test_delay_matches_formula(self, config: RetryConfig, data: st.DataObject) -> None:
        """**Validates: Requirements 53.1, 53.2**

        For any retry config and valid attempt number N, the computed delay
        equals min(base_delay * multiplier^(N-1), max_delay).
        """
        attempt = data.draw(attempt_for_config_st(config))

        delay = compute_backoff_delay(attempt, config)
        expected = min(config.base_delay * (config.multiplier ** (attempt - 1)), config.max_delay)

        assert abs(delay - expected) < 1e-9, (
            f"Attempt {attempt}: delay={delay}, expected={expected} "
            f"(base={config.base_delay}, mult={config.multiplier}, max={config.max_delay})"
        )

    @given(config=retry_config_st())
    @settings(max_examples=200)
    def test_delays_monotonically_non_decreasing(self, config: RetryConfig) -> None:
        """**Validates: Requirements 53.1, 53.2**

        The retry delay sequence is monotonically non-decreasing up to the cap.
        Each subsequent attempt waits at least as long as the previous one.
        """
        delays = compute_retry_sequence(config)

        for i in range(1, len(delays)):
            assert delays[i] >= delays[i - 1], (
                f"Delay sequence not monotonically non-decreasing: "
                f"delay[{i}]={delays[i]} < delay[{i-1}]={delays[i-1]} "
                f"(config: base={config.base_delay}, mult={config.multiplier})"
            )

    @given(config=retry_config_st())
    @settings(max_examples=200)
    def test_no_delay_exceeds_max(self, config: RetryConfig) -> None:
        """**Validates: Requirements 53.1**

        No delay in the retry sequence ever exceeds the configured max_delay.
        """
        delays = compute_retry_sequence(config)

        for i, delay in enumerate(delays):
            assert delay <= config.max_delay + 1e-9, (
                f"Delay for attempt {i+1} ({delay}s) exceeds max_delay ({config.max_delay}s)"
            )

    @given(config=retry_config_st())
    @settings(max_examples=200)
    def test_exactly_max_attempts_in_sequence(self, config: RetryConfig) -> None:
        """**Validates: Requirements 53.2**

        The retry sequence produces exactly max_attempts delays — no more,
        no fewer. After these attempts are exhausted, fallback chain activates.
        """
        delays = compute_retry_sequence(config)

        assert len(delays) == config.max_attempts, (
            f"Expected {config.max_attempts} delays in sequence, got {len(delays)}"
        )

    def test_default_config_produces_expected_sequence(self) -> None:
        """**Validates: Requirements 53.1, 53.2**

        With default parameters (base=1s, multiplier=2, max=60s, attempts=3),
        the expected delays are [1, 2, 4] seconds.
        """
        config = RetryConfig()
        delays = compute_retry_sequence(config)

        assert delays == [1.0, 2.0, 4.0], (
            f"Default config should produce [1, 2, 4], got {delays}"
        )

    @given(
        base_delay=st.floats(min_value=0.1, max_value=5.0, allow_nan=False, allow_infinity=False),
        multiplier=st.integers(min_value=2, max_value=4),
        max_attempts=st.integers(min_value=2, max_value=8),
    )
    @settings(max_examples=200)
    def test_cap_reached_delays_are_constant(
        self, base_delay: float, multiplier: int, max_attempts: int
    ) -> None:
        """**Validates: Requirements 53.1**

        Once the exponential growth hits max_delay, all subsequent delays
        in the sequence remain constant at max_delay.
        """
        # Use a small max_delay to ensure the cap is reached quickly
        max_delay = base_delay * multiplier  # Will cap after attempt 2 at latest
        config = RetryConfig(
            base_delay=base_delay,
            multiplier=multiplier,
            max_delay=max_delay,
            max_attempts=max_attempts,
        )
        delays = compute_retry_sequence(config)

        cap_reached = False
        for i, delay in enumerate(delays):
            if abs(delay - max_delay) < 1e-9:
                cap_reached = True
            if cap_reached:
                assert abs(delay - max_delay) < 1e-9, (
                    f"After cap reached, delay[{i}]={delay} should equal max_delay={max_delay}"
                )

    @given(config=retry_config_st(), data=st.data())
    @settings(max_examples=100)
    def test_attempt_beyond_max_raises_error(self, config: RetryConfig, data: st.DataObject) -> None:
        """**Validates: Requirements 53.2**

        Attempting to compute a delay for attempt > max_attempts raises
        ValueError, signaling that retries are exhausted and fallback
        chain should activate.
        """
        attempt_beyond = data.draw(
            st.integers(min_value=config.max_attempts + 1, max_value=config.max_attempts + 10)
        )

        try:
            compute_backoff_delay(attempt_beyond, config)
            assert False, (
                f"Expected ValueError for attempt {attempt_beyond} > max_attempts {config.max_attempts}"
            )
        except ValueError:
            pass  # Expected behavior — retries exhausted

    @given(config=retry_config_st())
    @settings(max_examples=200)
    def test_first_delay_equals_base(self, config: RetryConfig) -> None:
        """**Validates: Requirements 53.1**

        The first retry attempt always has delay equal to
        min(base_delay, max_delay) — i.e., base_delay * multiplier^0 = base_delay,
        capped at max_delay.
        """
        delay = compute_backoff_delay(1, config)
        expected = min(config.base_delay, config.max_delay)

        assert abs(delay - expected) < 1e-9, (
            f"First attempt delay={delay}, expected min(base={config.base_delay}, "
            f"max={config.max_delay})={expected}"
        )
