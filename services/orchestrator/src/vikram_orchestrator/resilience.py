"""Resilience module for agent call retry with exponential backoff and fallback chain.

This module implements:
- Exponential backoff delay computation for retry sequences
- Fallback chain activation after primary retries exhausted
- Escalation policy when all fallback models exhausted

Requirements: 53.1, 53.2, 53.3, 53.4
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

BASE_DELAY_SECONDS: float = 1.0
BACKOFF_MULTIPLIER: int = 2
MAX_DELAY_SECONDS: float = 60.0
MAX_ATTEMPTS: int = 3


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class RetryConfig:
    """Configuration for retry backoff behavior."""

    base_delay: float = BASE_DELAY_SECONDS
    multiplier: int = BACKOFF_MULTIPLIER
    max_delay: float = MAX_DELAY_SECONDS
    max_attempts: int = MAX_ATTEMPTS


@dataclass
class RetryResult:
    """Result of a retry sequence."""

    attempts_made: int
    delays: list[float] = field(default_factory=list)
    exhausted: bool = False
    fallback_activated: bool = False


# ---------------------------------------------------------------------------
# Core backoff computation
# ---------------------------------------------------------------------------


def compute_backoff_delay(attempt: int, config: RetryConfig | None = None) -> float:
    """Compute the backoff delay for a given attempt number.

    The delay for attempt N (1-indexed) is:
        min(base_delay * multiplier^(N-1), max_delay)

    Args:
        attempt: The attempt number (1-indexed). Attempt 1 is the first retry.
        config: Optional retry configuration. Uses defaults if not provided.

    Returns:
        The delay in seconds before this attempt should be made.

    Raises:
        ValueError: If attempt is less than 1 or greater than max_attempts.
    """
    if config is None:
        config = RetryConfig()

    if attempt < 1:
        raise ValueError(f"Attempt must be >= 1, got {attempt}")
    if attempt > config.max_attempts:
        raise ValueError(
            f"Attempt {attempt} exceeds max_attempts ({config.max_attempts})"
        )

    raw_delay = config.base_delay * (config.multiplier ** (attempt - 1))
    return min(raw_delay, config.max_delay)


def compute_retry_sequence(config: RetryConfig | None = None) -> list[float]:
    """Compute the full sequence of backoff delays for all retry attempts.

    Args:
        config: Optional retry configuration. Uses defaults if not provided.

    Returns:
        List of delays in seconds, one per retry attempt.
    """
    if config is None:
        config = RetryConfig()

    return [compute_backoff_delay(i, config) for i in range(1, config.max_attempts + 1)]
