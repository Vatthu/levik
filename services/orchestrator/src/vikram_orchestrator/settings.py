from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    orchestrator_socket: str = "/tmp/vikram-orchestrator.sock"
    host_socket: str = "/tmp/vikramd.sock"
    state_dir: Path = Path.home() / ".vikram" / "orchestrator"
    checkpoint_db: Path = Path.home() / ".vikram" / "db" / "orchestrator.sqlite"

    agent_retry_count: int = 2
    agent_retry_backoff_seconds: float = 1.0
    plan_min_lines: int = 3

    # -----------------------------------------------------------------------
    # Scheduler config knobs (Requirements 16.1, 17.1)
    # -----------------------------------------------------------------------
    max_concurrency: int = 3
    """Maximum number of concurrently running tasks (1–10, default: 3)."""

    max_concurrency_limit: int = 10
    """Absolute upper bound for max_concurrency (default: 10)."""

    default_task_timeout_seconds: int = 7200
    """Default per-task wall-clock timeout in seconds (default: 2 hours)."""

    # -----------------------------------------------------------------------
    # Budget config knobs (Requirement 5.1)
    # -----------------------------------------------------------------------
    daily_ceiling_usd: float | None = None
    """System-wide daily cost ceiling in USD. None means unlimited."""

    default_task_max_cost_usd: float | None = None
    """Default per-task budget in USD when not explicitly set."""

    budget_warning_threshold_pct: float = 80.0
    """Percentage of budget at which to emit a warning notification."""

    # -----------------------------------------------------------------------
    # Alert threshold config knobs (Requirement 10.4)
    # -----------------------------------------------------------------------
    alert_error_rate_pct: float = 30.0
    """Platform-wide error rate threshold (%) for health alerting."""

    alert_error_rate_window_minutes: float = 10.0
    """Rolling window (minutes) for error rate calculation."""

    alert_latency_seconds: float = 60.0
    """Average agent call latency threshold (seconds) for latency alerts."""

    alert_latency_window_minutes: float = 5.0
    """Rolling window (minutes) for latency degradation alerts."""

    alert_provider_failure_count: int = 3
    """Consecutive provider failures before triggering provider-down alert."""

    alert_quiet_hours_start: int | None = None
    """Hour (UTC, 0–23) at which quiet hours begin. None disables quiet hours."""

    alert_quiet_hours_end: int | None = None
    """Hour (UTC, 0–23) at which quiet hours end. None disables quiet hours."""

    # -----------------------------------------------------------------------
    # Retry / resilience config knobs (Requirements 53.1, 53.2)
    # -----------------------------------------------------------------------
    retry_base_delay_seconds: float = 1.0
    """Base delay for exponential backoff on retryable errors."""

    retry_multiplier: int = 2
    """Multiplier for exponential backoff (delay = base * multiplier^(attempt-1))."""

    retry_max_delay_seconds: float = 60.0
    """Maximum capped delay between retry attempts."""

    retry_max_attempts: int = 3
    """Maximum number of retry attempts before activating fallback chain."""

    model_config = SettingsConfigDict(
        env_prefix="VIKRAM_",
        env_file=".env",
        env_file_encoding="utf-8",
    )


settings = Settings()
