"""Unit tests for ProviderRateLimiter and RepoContentionChecker (Task 19.3).

Tests the high-level concurrency management classes that wrap the
RateLimitTracker and LockRegistry respectively.

Validates: Requirements 16.3, 17.1, 17.2, 17.3, 17.4, 17.5
"""

from __future__ import annotations

import time

import pytest

from vikram_orchestrator.conflict_detector import LockRegistry
from vikram_orchestrator.scheduler import (
    Priority,
    ProviderRateLimiter,
    RateLimitConfig,
    RateLimitTracker,
    RepoContentionChecker,
    Scheduler,
    SchedulerConfig,
    TaskQueueEntry,
)


# ---------------------------------------------------------------------------
# ProviderRateLimiter Tests
# Validates: Requirement 17.5
# ---------------------------------------------------------------------------


class TestProviderRateLimiter:
    """Tests for the high-level ProviderRateLimiter coordinator."""

    def test_configure_and_try_acquire_within_limit(self) -> None:
        """Calls within the rate limit window succeed."""
        limiter = ProviderRateLimiter()
        limiter.configure("openai", max_calls=3, window_seconds=60.0)

        now = 1000.0
        assert limiter.try_acquire("openai", "task-1", now) is True
        assert limiter.try_acquire("openai", "task-1", now + 1) is True
        assert limiter.try_acquire("openai", "task-2", now + 2) is True

    def test_try_acquire_queues_when_limit_exhausted(self) -> None:
        """Calls beyond the rate limit are queued instead of recorded."""
        limiter = ProviderRateLimiter()
        limiter.configure("openai", max_calls=2, window_seconds=60.0)

        now = 1000.0
        assert limiter.try_acquire("openai", "task-1", now) is True
        assert limiter.try_acquire("openai", "task-1", now + 1) is True
        # Limit reached
        assert limiter.try_acquire("openai", "task-2", now + 2) is False

        # Verify the call was queued
        status = limiter.get_status("openai", now + 2)
        assert status["queue_depth"] == 1

    def test_drain_queued_releases_calls_on_window_rotation(self) -> None:
        """After window rotation, queued calls are drained and released."""
        limiter = ProviderRateLimiter()
        limiter.configure("anthropic", max_calls=2, window_seconds=60.0)

        base = 1000.0
        limiter.try_acquire("anthropic", "task-1", base)
        limiter.try_acquire("anthropic", "task-1", base + 1)
        # Queue additional calls
        limiter.try_acquire("anthropic", "task-2", base + 2)  # queued
        limiter.try_acquire("anthropic", "task-3", base + 3)  # queued

        # After window rotates (61s later), drain
        released = limiter.drain_queued("anthropic", base + 61)
        assert "task-2" in released
        assert "task-3" in released

    def test_drain_queued_respects_limit(self) -> None:
        """Drain only releases as many calls as capacity allows."""
        limiter = ProviderRateLimiter()
        limiter.configure("openai", max_calls=1, window_seconds=60.0)

        base = 1000.0
        limiter.try_acquire("openai", "task-1", base)  # fills limit
        limiter.try_acquire("openai", "task-2", base + 1)  # queued
        limiter.try_acquire("openai", "task-3", base + 2)  # queued

        # After window rotates, only 1 call can be released (max_calls=1)
        released = limiter.drain_queued("openai", base + 61)
        assert len(released) == 1
        assert released[0] == "task-2"

        # The other is still queued
        status = limiter.get_status("openai", base + 61)
        assert status["queue_depth"] == 1

    def test_release_task(self) -> None:
        """Releasing a task removes it from active tasks."""
        limiter = ProviderRateLimiter()
        limiter.configure("openai", max_calls=10, window_seconds=60.0)

        limiter.try_acquire("openai", "task-1")
        status = limiter.get_status("openai")
        assert "task-1" in status["active_tasks"]

        limiter.release_task("openai", "task-1")
        status = limiter.get_status("openai")
        assert "task-1" not in status["active_tasks"]

    def test_get_status_unconfigured_provider(self) -> None:
        """Status for an unconfigured provider shows unlimited capacity."""
        limiter = ProviderRateLimiter()
        status = limiter.get_status("unknown")
        assert status["can_call"] is True
        assert status["remaining"] is None
        assert status["queue_depth"] == 0

    def test_get_status_configured_provider(self) -> None:
        """Status for a configured provider shows accurate metrics."""
        limiter = ProviderRateLimiter()
        limiter.configure("openai", max_calls=5, window_seconds=60.0)

        now = 1000.0
        limiter.try_acquire("openai", "task-1", now)
        limiter.try_acquire("openai", "task-2", now + 1)

        status = limiter.get_status("openai", now + 2)
        assert status["can_call"] is True
        assert status["current_usage"] == 2
        assert status["max_calls"] == 5
        assert status["remaining"] == 3
        assert status["queue_depth"] == 0

    def test_unconfigured_provider_always_acquires(self) -> None:
        """Unconfigured providers always allow calls (no limit)."""
        limiter = ProviderRateLimiter()
        # No configure call
        assert limiter.try_acquire("mystery-provider", "task-1") is True
        assert limiter.try_acquire("mystery-provider", "task-1") is True

    def test_multiple_providers_independent(self) -> None:
        """Rate limits are tracked independently per provider."""
        limiter = ProviderRateLimiter()
        limiter.configure("openai", max_calls=1, window_seconds=60.0)
        limiter.configure("anthropic", max_calls=1, window_seconds=60.0)

        now = 1000.0
        assert limiter.try_acquire("openai", "task-1", now) is True
        assert limiter.try_acquire("openai", "task-1", now + 1) is False  # exhausted

        # anthropic still has capacity
        assert limiter.try_acquire("anthropic", "task-1", now + 2) is True


# ---------------------------------------------------------------------------
# RepoContentionChecker Tests
# Validates: Requirements 17.2, 17.3, 19.1
# ---------------------------------------------------------------------------


class TestRepoContentionChecker:
    """Tests for the RepoContentionChecker class."""

    def test_no_repos_not_contended(self) -> None:
        """A task with no repos is never contended."""
        checker = RepoContentionChecker()
        assert checker.is_contended("task-1", []) is False

    def test_unlocked_repos_not_contended(self) -> None:
        """Repos that are not locked are not contended."""
        checker = RepoContentionChecker()
        assert checker.is_contended("task-1", ["repo/a", "repo/b"]) is False

    def test_repo_locked_by_different_task_is_contended(self) -> None:
        """A repo locked by another task shows as contended."""
        registry = LockRegistry()
        registry.acquire("task-A", "repo/backend")

        checker = RepoContentionChecker(lock_registry=registry)
        assert checker.is_contended("task-B", ["repo/backend"]) is True

    def test_repo_locked_by_same_task_not_contended(self) -> None:
        """A repo locked by the same task is not contended (idempotent)."""
        registry = LockRegistry()
        registry.acquire("task-A", "repo/backend")

        checker = RepoContentionChecker(lock_registry=registry)
        assert checker.is_contended("task-A", ["repo/backend"]) is False

    def test_acquire_repos(self) -> None:
        """Acquiring repos creates locks in the registry."""
        registry = LockRegistry()
        checker = RepoContentionChecker(lock_registry=registry)

        acquired = checker.acquire_repos("task-1", ["repo/a", "repo/b", "repo/c"])
        assert len(acquired) == 3
        assert set(acquired) == {"repo/a", "repo/b", "repo/c"}

        # Verify they are locked
        is_locked, holder = registry.is_locked("repo/a")
        assert is_locked is True
        assert holder == "task-1"

    def test_acquire_repos_sorted_order(self) -> None:
        """Repos are acquired in sorted order to prevent deadlocks."""
        registry = LockRegistry()
        checker = RepoContentionChecker(lock_registry=registry)

        acquired = checker.acquire_repos("task-1", ["repo/z", "repo/a", "repo/m"])
        # Should be acquired in sorted order
        assert acquired == ["repo/a", "repo/m", "repo/z"]

    def test_acquire_repos_skips_already_held_by_other(self) -> None:
        """Repos held by another task are skipped (not acquired)."""
        registry = LockRegistry()
        registry.acquire("task-other", "repo/b")

        checker = RepoContentionChecker(lock_registry=registry)
        acquired = checker.acquire_repos("task-1", ["repo/a", "repo/b", "repo/c"])

        # repo/b should be skipped (held by task-other)
        assert "repo/a" in acquired
        assert "repo/b" not in acquired
        assert "repo/c" in acquired

    def test_release_repos(self) -> None:
        """Releasing repos removes locks from the registry."""
        registry = LockRegistry()
        checker = RepoContentionChecker(lock_registry=registry)

        checker.acquire_repos("task-1", ["repo/a", "repo/b"])
        released = checker.release_repos("task-1", ["repo/a", "repo/b"])

        assert len(released) == 2
        is_locked, _ = registry.is_locked("repo/a")
        assert is_locked is False

    def test_release_repos_only_own_locks(self) -> None:
        """Can only release locks held by the specified task."""
        registry = LockRegistry()
        registry.acquire("task-1", "repo/a")
        registry.acquire("task-2", "repo/b")

        checker = RepoContentionChecker(lock_registry=registry)
        released = checker.release_repos("task-1", ["repo/a", "repo/b"])

        # Only repo/a should be released (task-1 holds it)
        assert released == ["repo/a"]
        # repo/b still locked by task-2
        is_locked, holder = registry.is_locked("repo/b")
        assert is_locked is True
        assert holder == "task-2"

    def test_get_contention_map(self) -> None:
        """Contention map returns all active locks."""
        registry = LockRegistry()
        registry.acquire("task-1", "repo/a")
        registry.acquire("task-2", "repo/b")

        checker = RepoContentionChecker(lock_registry=registry)
        contention_map = checker.get_contention_map()

        assert contention_map == {"repo/a": "task-1", "repo/b": "task-2"}

    def test_get_contention_map_empty(self) -> None:
        """Contention map is empty when no locks exist."""
        checker = RepoContentionChecker()
        assert checker.get_contention_map() == {}


# ---------------------------------------------------------------------------
# Integration Tests: Scheduler uses ProviderRateLimiter and RepoContentionChecker
# Validates: Requirements 16.3, 17.2, 17.3, 17.5
# ---------------------------------------------------------------------------


class TestSchedulerIntegrationWithNewClasses:
    """Tests that the Scheduler correctly uses the new ProviderRateLimiter
    and RepoContentionChecker classes."""

    def test_scheduler_creates_default_instances(self) -> None:
        """Scheduler creates default ProviderRateLimiter and RepoContentionChecker."""
        scheduler = Scheduler()
        assert scheduler.contention_checker is not None
        assert scheduler.provider_rate_limiter is not None

    def test_scheduler_accepts_custom_contention_checker(self) -> None:
        """Scheduler can be constructed with a custom RepoContentionChecker."""
        registry = LockRegistry()
        checker = RepoContentionChecker(lock_registry=registry)
        scheduler = Scheduler(contention_checker=checker)

        assert scheduler.contention_checker is checker

    def test_scheduler_accepts_custom_provider_rate_limiter(self) -> None:
        """Scheduler can be constructed with a custom ProviderRateLimiter."""
        tracker = RateLimitTracker()
        limiter = ProviderRateLimiter(tracker=tracker)
        scheduler = Scheduler(provider_rate_limiter=limiter)

        assert scheduler.provider_rate_limiter is limiter

    def test_scheduler_contention_check_uses_checker(self) -> None:
        """Scheduler's dequeue_next uses RepoContentionChecker for contention."""
        registry = LockRegistry()
        registry.acquire("other-task", "repo/locked")

        checker = RepoContentionChecker(lock_registry=registry)
        scheduler = Scheduler(
            config=SchedulerConfig(max_concurrency=5),
            contention_checker=checker,
            lock_registry=registry,
        )

        entry = TaskQueueEntry(
            task_id="task-B",
            priority=Priority.HIGH,
            enqueued_at=1000.0,
            repos=["repo/locked"],
        )
        scheduler.enqueue(entry)

        # Task should not be dequeued due to contention
        result = scheduler.dequeue_next()
        assert result is None

    def test_scheduler_provider_rate_limiter_integration(self) -> None:
        """Scheduler's provider_rate_limiter can be used for call gating."""
        scheduler = Scheduler(config=SchedulerConfig(max_concurrency=5))

        # Configure rate limit through the scheduler's provider_rate_limiter
        scheduler.provider_rate_limiter.configure("openai", max_calls=2, window_seconds=60.0)

        now = 1000.0
        # Task acquires calls
        assert scheduler.provider_rate_limiter.try_acquire("openai", "task-1", now) is True
        assert scheduler.provider_rate_limiter.try_acquire("openai", "task-1", now + 1) is True
        # Limit reached
        assert scheduler.provider_rate_limiter.try_acquire("openai", "task-2", now + 2) is False

        # Verify status
        status = scheduler.provider_rate_limiter.get_status("openai", now + 2)
        assert status["can_call"] is False
        assert status["queue_depth"] == 1

    def test_preemption_with_provider_rate_limiter(self) -> None:
        """Preemption interacts correctly with the ProviderRateLimiter."""
        tracker = RateLimitTracker()
        tracker.configure_provider(RateLimitConfig(
            provider="openai",
            max_calls_per_window=10,
            window_seconds=60.0,
        ))
        limiter = ProviderRateLimiter(tracker=tracker)

        scheduler = Scheduler(
            config=SchedulerConfig(max_concurrency=1),
            rate_limiter=tracker,
            provider_rate_limiter=limiter,
        )

        # Run a task with formation=openai
        entry = TaskQueueEntry(
            task_id="task-low",
            priority=Priority.LOW,
            enqueued_at=1000.0,
            formation="openai",
        )
        scheduler.enqueue(entry)
        scheduler.dequeue_next()

        # Record call via the rate limiter
        tracker.record_call("openai", "task-low")
        assert "task-low" in tracker.get_active_tasks("openai")

        # Preempt
        preempted = scheduler.preempt_for_critical("critical-task")
        assert preempted == "task-low"

        # Provider capacity freed
        assert "task-low" not in tracker.get_active_tasks("openai")
