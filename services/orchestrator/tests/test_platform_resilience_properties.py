"""Property-based tests for Platform Resilience — retry backoff formula.

Property 29: Retry Backoff Formula
For any agent call retry sequence, the delay before attempt N SHALL equal
min(base_seconds * 2^(N-1), max_delay_seconds) where base_seconds=1 and
max_delay_seconds=60. After max_attempts=3 exhausted, the system signals
"exhausted" rather than producing another delay.

Properties verified:
1. Backoff delay for attempt N = min(base * multiplier^(N-1), max_delay)
2. Delay is always >= base (1s)
3. Delay never exceeds max_delay (60s)
4. Delay is monotonically non-decreasing with attempt number
5. After max_attempts (3), the system signals "exhausted" rather than
   producing another delay

Run with:
  cd services/orchestrator && .venv/bin/python -m pytest tests/test_platform_resilience_properties.py -v

Validates: Requirements 53.1, 53.2
"""

from __future__ import annotations

import pytest
import hypothesis.strategies as st
from hypothesis import given, settings

from vikram_orchestrator.platform_resilience import (
    BackoffConfig,
    RetriesExhausted,
    compute_backoff,
    compute_backoff_sequence,
    BASE_DELAY_SECONDS,
    BACKOFF_MULTIPLIER,
    MAX_DELAY_SECONDS,
    MAX_ATTEMPTS,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


def backoff_config_st() -> st.SearchStrategy[BackoffConfig]:
    """Generate valid BackoffConfig instances with reasonable parameter ranges."""
    return st.builds(
        BackoffConfig,
        base_delay=st.floats(min_value=0.1, max_value=10.0, allow_nan=False, allow_infinity=False),
        multiplier=st.integers(min_value=2, max_value=5),
        max_delay=st.floats(min_value=10.0, max_value=120.0, allow_nan=False, allow_infinity=False),
        max_attempts=st.integers(min_value=1, max_value=10),
    )


# ---------------------------------------------------------------------------
# Property 29: Retry Backoff Formula
# Validates: Requirements 53.1, 53.2
# ---------------------------------------------------------------------------


class TestRetryBackoffFormula:
    """Property 29: Retry Backoff Formula.

    The delay for attempt N equals min(base * multiplier^(N-1), max_delay).
    Delays are monotonically non-decreasing. No delay exceeds max_delay.
    After max_attempts, the system signals exhausted.
    """

    @given(config=backoff_config_st(), data=st.data())
    @settings(max_examples=200)
    def test_delay_matches_exponential_formula(self, config: BackoffConfig, data: st.DataObject) -> None:
        """**Validates: Requirements 53.1, 53.2**

        Property 1: For any retry config and valid attempt number N, the
        computed delay equals min(base_delay * multiplier^(N-1), max_delay).
        """
        attempt = data.draw(st.integers(min_value=1, max_value=config.max_attempts))

        delay = compute_backoff(attempt, config)
        expected = min(config.base_delay * (config.multiplier ** (attempt - 1)), config.max_delay)

        assert abs(delay - expected) < 1e-9, (
            f"Attempt {attempt}: got delay={delay}, expected={expected} "
            f"(base={config.base_delay}, mult={config.multiplier}, max={config.max_delay})"
        )

    @given(config=backoff_config_st(), data=st.data())
    @settings(max_examples=200)
    def test_delay_always_gte_base_delay(self, config: BackoffConfig, data: st.DataObject) -> None:
        """**Validates: Requirements 53.1, 53.2**

        Property 2: The delay is always >= min(base_delay, max_delay).
        Since base_delay * multiplier^(N-1) with N >= 1 gives at minimum
        base_delay (when N=1), and the cap only applies from above, delay
        is always >= min(base_delay, max_delay).
        """
        attempt = data.draw(st.integers(min_value=1, max_value=config.max_attempts))

        delay = compute_backoff(attempt, config)
        minimum = min(config.base_delay, config.max_delay)

        assert delay >= minimum - 1e-9, (
            f"Attempt {attempt}: delay={delay} is less than "
            f"min(base={config.base_delay}, max_delay={config.max_delay})={minimum}"
        )

    @given(config=backoff_config_st())
    @settings(max_examples=200)
    def test_no_delay_exceeds_max_delay(self, config: BackoffConfig) -> None:
        """**Validates: Requirements 53.1**

        Property 3: No delay in the retry sequence ever exceeds max_delay.
        """
        delays = compute_backoff_sequence(config)

        for i, delay in enumerate(delays):
            assert delay <= config.max_delay + 1e-9, (
                f"Delay for attempt {i+1} ({delay}s) exceeds max_delay ({config.max_delay}s)"
            )

    @given(config=backoff_config_st())
    @settings(max_examples=200)
    def test_delays_monotonically_non_decreasing(self, config: BackoffConfig) -> None:
        """**Validates: Requirements 53.1, 53.2**

        Property 4: The retry delay sequence is monotonically non-decreasing.
        Each subsequent attempt waits at least as long as the previous one.
        """
        delays = compute_backoff_sequence(config)

        for i in range(1, len(delays)):
            assert delays[i] >= delays[i - 1] - 1e-9, (
                f"Delay sequence not monotonically non-decreasing: "
                f"delay[{i}]={delays[i]} < delay[{i-1}]={delays[i-1]} "
                f"(config: base={config.base_delay}, mult={config.multiplier})"
            )

    @given(config=backoff_config_st(), data=st.data())
    @settings(max_examples=200)
    def test_exhausted_after_max_attempts(self, config: BackoffConfig, data: st.DataObject) -> None:
        """**Validates: Requirements 53.2**

        Property 5: After max_attempts, the system signals "exhausted"
        (raises RetriesExhausted) rather than producing another delay.
        This indicates the fallback chain should activate.
        """
        attempt_beyond = data.draw(
            st.integers(min_value=config.max_attempts + 1, max_value=config.max_attempts + 10)
        )

        with pytest.raises(RetriesExhausted) as exc_info:
            compute_backoff(attempt_beyond, config)

        assert exc_info.value.attempts == config.max_attempts
        assert exc_info.value.config == config

    # -------------------------------------------------------------------
    # Concrete example: default configuration
    # -------------------------------------------------------------------

    def test_default_config_sequence(self) -> None:
        """**Validates: Requirements 53.1, 53.2**

        With default parameters (base=1s, multiplier=2, max=60s, attempts=3),
        the expected delays are [1.0, 2.0, 4.0] seconds.
        """
        config = BackoffConfig()
        delays = compute_backoff_sequence(config)

        assert delays == [1.0, 2.0, 4.0], (
            f"Default config should produce [1.0, 2.0, 4.0], got {delays}"
        )

    def test_default_config_exhausts_after_3(self) -> None:
        """**Validates: Requirements 53.2**

        With default max_attempts=3, requesting attempt 4 raises
        RetriesExhausted.
        """
        config = BackoffConfig()

        with pytest.raises(RetriesExhausted):
            compute_backoff(4, config)
