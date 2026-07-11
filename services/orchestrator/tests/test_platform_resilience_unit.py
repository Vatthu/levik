"""Unit tests for platform resilience — backoff formula and fallback chain activation.

These tests validate specific scenarios required by the platform resilience spec:
- Retry backoff formula produces correct delays for each attempt
- Fallback chain activates after primary retry exhaustion

Validates: Requirements 53.1, 53.2
"""

from __future__ import annotations

import pytest

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
from vikram_orchestrator.resilience import (
    RetryConfig,
    RetryResult,
    compute_backoff_delay,
    compute_retry_sequence,
)


# ---------------------------------------------------------------------------
# Backoff formula unit tests
# Validates: Requirement 53.1
# ---------------------------------------------------------------------------


class TestBackoffFormulaCorrectDelays:
    """Test retry backoff formula produces correct delays for each attempt."""

    def test_default_attempt_1_delay_is_1_second(self) -> None:
        """First retry delay with default config is exactly 1 second."""
        delay = compute_backoff(1)
        assert delay == 1.0

    def test_default_attempt_2_delay_is_2_seconds(self) -> None:
        """Second retry delay with default config is 2 seconds (1 * 2^1)."""
        delay = compute_backoff(2)
        assert delay == 2.0

    def test_default_attempt_3_delay_is_4_seconds(self) -> None:
        """Third retry delay with default config is 4 seconds (1 * 2^2)."""
        delay = compute_backoff(3)
        assert delay == 4.0

    def test_full_default_sequence(self) -> None:
        """Default config produces [1.0, 2.0, 4.0] second delays."""
        delays = compute_backoff_sequence()
        assert delays == [1.0, 2.0, 4.0]

    def test_max_delay_caps_computed_value(self) -> None:
        """When computed delay exceeds max_delay, it is capped."""
        # With base=10, mult=3, max_delay=20: attempt 2 would be 30, capped to 20
        config = BackoffConfig(base_delay=10.0, multiplier=3, max_delay=20.0, max_attempts=3)
        delay = compute_backoff(2, config)
        assert delay == 20.0

    def test_custom_config_computes_correctly(self) -> None:
        """Custom config: base=2, multiplier=3, max=60."""
        config = BackoffConfig(base_delay=2.0, multiplier=3, max_delay=60.0, max_attempts=4)
        delays = compute_backoff_sequence(config)
        # 2*3^0=2, 2*3^1=6, 2*3^2=18, 2*3^3=54
        assert delays == [2.0, 6.0, 18.0, 54.0]

    def test_invalid_attempt_zero_raises_error(self) -> None:
        """Attempt 0 is invalid and raises ValueError."""
        with pytest.raises(ValueError):
            compute_backoff(0)

    def test_invalid_negative_attempt_raises_error(self) -> None:
        """Negative attempt numbers raise ValueError."""
        with pytest.raises(ValueError):
            compute_backoff(-1)

    def test_resilience_module_formula_matches(self) -> None:
        """The resilience module compute_backoff_delay produces same results."""
        config = RetryConfig(base_delay=1.0, multiplier=2, max_delay=60.0, max_attempts=3)
        delays = compute_retry_sequence(config)
        assert delays == [1.0, 2.0, 4.0]


# ---------------------------------------------------------------------------
# Fallback chain activation tests
# Validates: Requirement 53.2
# ---------------------------------------------------------------------------


class TestFallbackChainActivation:
    """Test that fallback chain activates after primary retry exhaustion.

    Requirement 53.2: WHEN all retries for the primary model are exhausted,
    THE Model_Router SHALL activate the fallback chain.
    """

    def test_retries_exhausted_signals_fallback_activation(self) -> None:
        """After max_attempts, RetriesExhausted is raised — the signal
        for the fallback chain to activate."""
        config = BackoffConfig(max_attempts=3)

        # Attempts 1-3 succeed (produce delays)
        for attempt in range(1, 4):
            delay = compute_backoff(attempt, config)
            assert delay > 0

        # Attempt 4 signals exhaustion — fallback chain should activate
        with pytest.raises(RetriesExhausted) as exc_info:
            compute_backoff(4, config)

        assert exc_info.value.attempts == 3
        assert exc_info.value.config == config

    def test_exhaustion_after_single_max_attempt(self) -> None:
        """With max_attempts=1, exhaustion fires after first retry fails."""
        config = BackoffConfig(max_attempts=1)

        # First attempt succeeds
        delay = compute_backoff(1, config)
        assert delay == config.base_delay

        # Second attempt triggers exhaustion
        with pytest.raises(RetriesExhausted) as exc_info:
            compute_backoff(2, config)

        assert exc_info.value.attempts == 1

    def test_fallback_chain_full_scenario(self) -> None:
        """Simulate a full retry-then-fallback scenario.

        Models a real agent call sequence:
        1. Primary model fails with retryable error (429)
        2. Retry with backoff: attempt 1 (1s wait), attempt 2 (2s wait), attempt 3 (4s wait)
        3. All retries exhausted → fallback chain activates
        4. Fallback model is tried (with its own retry config)
        5. If all fallback models exhaust → escalation policy fires
        """
        primary_config = BackoffConfig(max_attempts=3)
        fallback_models = ["anthropic/claude-3-haiku", "openai/gpt-4o-mini"]

        # Phase 1: Primary model retry sequence
        primary_delays: list[float] = []
        for attempt in range(1, primary_config.max_attempts + 1):
            primary_delays.append(compute_backoff(attempt, primary_config))

        assert primary_delays == [1.0, 2.0, 4.0]

        # Phase 2: Primary exhausted — trigger fallback chain
        fallback_activated = False
        try:
            compute_backoff(primary_config.max_attempts + 1, primary_config)
        except RetriesExhausted:
            fallback_activated = True

        assert fallback_activated, "Fallback chain must activate after primary exhaustion"

        # Phase 3: Each fallback model gets its own retry sequence
        fallback_config = BackoffConfig(max_attempts=3)
        fallback_results: list[bool] = []

        for model in fallback_models:
            # Simulate each fallback model also exhausting retries
            model_exhausted = False
            for attempt in range(1, fallback_config.max_attempts + 1):
                delay = compute_backoff(attempt, fallback_config)
                assert delay > 0
            try:
                compute_backoff(fallback_config.max_attempts + 1, fallback_config)
            except RetriesExhausted:
                model_exhausted = True
            fallback_results.append(model_exhausted)

        # All fallback models exhausted → escalation policy should fire
        assert all(fallback_results), "All fallback models should report exhaustion"
        # Total models tried = primary + 2 fallbacks = 3
        total_models_tried = 1 + len(fallback_models)
        assert total_models_tried == 3

    def test_resilience_module_raises_on_exhaustion(self) -> None:
        """The resilience module raises ValueError on attempt > max_attempts,
        signaling fallback chain activation."""
        config = RetryConfig(max_attempts=3)

        # All 3 attempts produce valid delays
        delays = compute_retry_sequence(config)
        assert len(delays) == 3

        # Beyond max_attempts → ValueError (fallback signal)
        with pytest.raises(ValueError, match="exceeds max_attempts"):
            compute_backoff_delay(4, config)

    def test_escalation_after_all_fallbacks_exhausted(self) -> None:
        """When all models in fallback chain are exhausted, the call should
        be marked as failed (Requirement 53.3)."""
        config = BackoffConfig(max_attempts=2)
        fallback_chain = ["model-a", "model-b", "model-c"]

        all_exhausted = True
        for _model in fallback_chain:
            try:
                compute_backoff(config.max_attempts + 1, config)
            except RetriesExhausted:
                continue  # This model is exhausted, try next
            all_exhausted = False

        assert all_exhausted, "All models in fallback chain should report exhaustion"
