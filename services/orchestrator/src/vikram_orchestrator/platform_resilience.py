"""Platform resilience module — retry backoff formula.

Implements exponential backoff delay computation for agent call retries.
The formula is: delay(N) = min(base * multiplier^(N-1), max_delay)

Parameters (defaults):
- Base delay: 1 second
- Multiplier: 2 (doubles each attempt)
- Maximum delay: 60 seconds
- Maximum attempts: 3

After max_attempts are exhausted, the system signals "exhausted" (raises
RetriesExhausted) to indicate fallback chain should activate.

Requirements: 53.1, 53.2
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_DELAY_SECONDS: float = 1.0
BACKOFF_MULTIPLIER: int = 2
MAX_DELAY_SECONDS: float = 60.0
MAX_ATTEMPTS: int = 3


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RetriesExhausted(Exception):
    """Raised when all retry attempts have been exhausted."""

    def __init__(self, attempts: int, config: "BackoffConfig") -> None:
        self.attempts = attempts
        self.config = config
        super().__init__(
            f"All {attempts} retry attempts exhausted "
            f"(base={config.base_delay}s, multiplier={config.multiplier}, "
            f"max_delay={config.max_delay}s)"
        )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class BackoffConfig:
    """Configuration for exponential backoff retry behavior."""

    base_delay: float = BASE_DELAY_SECONDS
    multiplier: int = BACKOFF_MULTIPLIER
    max_delay: float = MAX_DELAY_SECONDS
    max_attempts: int = MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# Core backoff computation
# ---------------------------------------------------------------------------


def compute_backoff(attempt: int, config: BackoffConfig | None = None) -> float:
    """Compute the backoff delay for a given attempt number.

    The delay for attempt N (1-indexed) is:
        min(base_delay * multiplier^(N-1), max_delay)

    Args:
        attempt: The attempt number (1-indexed). Attempt 1 is the first retry.
        config: Optional backoff configuration. Uses defaults if not provided.

    Returns:
        The delay in seconds before this attempt should be made.

    Raises:
        ValueError: If attempt is less than 1.
        RetriesExhausted: If attempt exceeds max_attempts, signaling
            that the fallback chain should activate.
    """
    if config is None:
        config = BackoffConfig()

    if attempt < 1:
        raise ValueError(f"Attempt number must be >= 1, got {attempt}")

    if attempt > config.max_attempts:
        raise RetriesExhausted(attempts=config.max_attempts, config=config)

    raw_delay = config.base_delay * (config.multiplier ** (attempt - 1))
    return min(raw_delay, config.max_delay)


def compute_backoff_sequence(config: BackoffConfig | None = None) -> list[float]:
    """Compute the full sequence of backoff delays for all retry attempts.

    Returns a list of delays (one per attempt) up to max_attempts.

    Args:
        config: Optional backoff configuration. Uses defaults if not provided.

    Returns:
        List of delays in seconds, one per retry attempt.
    """
    if config is None:
        config = BackoffConfig()

    return [compute_backoff(i, config) for i in range(1, config.max_attempts + 1)]
