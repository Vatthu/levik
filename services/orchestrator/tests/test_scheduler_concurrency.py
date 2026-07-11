"""Unit tests for scheduler concurrency management and preemption (Task 19.3).

Tests Lock Registry integration, per-provider rate limit tracking, and
preemption/rate-limiter interaction.

Validates: Requirements 16.3, 17.1, 17.2, 17.3, 17.4, 17.5
"""

from __future__ import annotations

import time

import pytest

from vikram_orchestrator.conflict_detector import LockRegistry
from vikram_orchestrator.scheduler import (
    Priority,
    RateLimitConfig,
    RateLimitTracker,
    Scheduler,
    SchedulerConfig,
    TaskQueueEntry,
)


# ---------------------------------------------------------------------------
# Lock Registry Integration Tests
# Validates: Requirements 17.2, 17.3
# ---------------------------------------------------------------------------


class TestLockRegistryIntegration:
    """Tests that the scheduler skips tasks whose repos are locked by another task."""

    def test_task_with_locked_repo_is_skipped(self) -> None:
        """A task targeting a repo locked by another task cannot be dequeued."""
        lock_registry = LockRegistry()
        scheduler = Scheduler(
            config=SchedulerConfig(max_concurrency=5),
            lock_registry=lock_registry,
        )

        # Lock a repo path by task-A
        lock_registry.acquire("task-A", "repo/backend")

        # Enqueue task-B targeting the same repo
        entry = TaskQueueEntry(
            task_id="task-B",
            priority=Priority.HIGH,
            enqueued_at=1000.0,
            depends_on=[],
            repos=["repo/backend"],
        )
        scheduler.enqueue(entry)

        # task-B should not be dequeued (repo is locked by task-A)
        result = scheduler.dequeue_next()
        assert result is None

    def test_task_with_unlocked_repo_is_dispatched(self) -> None:
        """A task targeting repos that are not locked by others can be dequeued."""
        lock_registry = LockRegistry()
        scheduler = Scheduler(
            config=SchedulerConfig(max_concurrency=5),
            lock_registry=lock_registry,
        )

        # Lock a different repo
        lock_registry.acquire("task-A", "repo/frontend")

        # Enqueue task-B targeting a different repo
        entry = TaskQueueEntry(
            task_id="task-B",
            priority=Priority.HIGH,
            enqueued_at=1000.0,
            depends_on=[],
            repos=["repo/backend"],
        )
        scheduler.enqueue(entry)

        # task-B should be dequeued (its repo is not locked)
        result = scheduler.dequeue_next()
        assert result is not None
        assert result.task_id == "task-B"

    def test_task_with_no_repos_ignores_lock_check(self) -> None:
        """A task with no repos specified bypasses lock contention check."""
        lock_registry = LockRegistry()
        lock_registry.acquire("task-A", "some/repo")

        scheduler = Scheduler(
            config=SchedulerConfig(max_concurrency=5),
            lock_registry=lock_registry,
        )

        entry = TaskQueueEntry(
            task_id="task-B",
            priority=Priority.NORMAL,
            enqueued_at=1000.0,
            depends_on=[],
            repos=[],  # No repos
        )
        scheduler.enqueue(entry)

        result = scheduler.dequeue_next()
        assert result is not None
        assert result.task_id == "task-B"

    def test_same_task_reacquiring_own_lock_is_ok(self) -> None:
        """A task can be dequeued if it holds the lock on its own repo."""
        lock_registry = LockRegistry()
        scheduler = Scheduler(
            config=SchedulerConfig(max_concurrency=5),
            lock_registry=lock_registry,
        )

        # task-B holds the lock on its own repo
        lock_registry.acquire("task-B", "repo/backend")

        entry = TaskQueueEntry(
            task_id="task-B",
            priority=Priority.HIGH,
            enqueued_at=1000.0,
            depends_on=[],
            repos=["repo/backend"],
        )
        scheduler.enqueue(entry)

        # Should be dequeued since it holds its own lock
        result = scheduler.dequeue_next()
        assert result is not None
        assert result.task_id == "task-B"

    def test_lock_released_allows_dequeue(self) -> None:
        """After a lock is released, the previously contended task can be dequeued."""
        lock_registry = LockRegistry()
        scheduler = Scheduler(
            config=SchedulerConfig(max_concurrency=5),
            lock_registry=lock_registry,
        )

        lock_registry.acquire("task-A", "repo/shared")

        entry = TaskQueueEntry(
            task_id="task-B",
            priority=Priority.HIGH,
            enqueued_at=1000.0,
            depends_on=[],
            repos=["repo/shared"],
        )
        scheduler.enqueue(entry)

        # Cannot dequeue yet
        assert scheduler.dequeue_next() is None

        # Release the lock
        lock_registry.release("task-A", "repo/shared")

        # Now it can be dequeued
        result = scheduler.dequeue_next()
        assert result is not None
        assert result.task_id == "task-B"

    def test_multiple_repos_any_locked_blocks_task(self) -> None:
        """If a task targets multiple repos and any one is locked, it's blocked."""
        lock_registry = LockRegistry()
        scheduler = Scheduler(
            config=SchedulerConfig(max_concurrency=5),
            lock_registry=lock_registry,
        )

        # Lock one of the repos
        lock_registry.acquire("task-A", "repo/api")

        entry = TaskQueueEntry(
            task_id="task-B",
            priority=Priority.HIGH,
            enqueued_at=1000.0,
            depends_on=[],
            repos=["repo/frontend", "repo/api"],  # api is locked
        )
        scheduler.enqueue(entry)

        assert scheduler.dequeue_next() is None

    def test_contended_task_skipped_lower_priority_dispatched(self) -> None:
        """When highest-priority task is contended, a lower-priority uncontended task is dispatched."""
        lock_registry = LockRegistry()
        scheduler = Scheduler(
            config=SchedulerConfig(max_concurrency=5),
            lock_registry=lock_registry,
        )

        lock_registry.acquire("task-X", "repo/locked")

        # High priority but contended
        entry_high = TaskQueueEntry(
            task_id="task-high",
            priority=Priority.HIGH,
            enqueued_at=1000.0,
            depends_on=[],
            repos=["repo/locked"],
        )
        scheduler.enqueue(entry_high)

        # Lower priority but uncontended
        entry_low = TaskQueueEntry(
            task_id="task-low",
            priority=Priority.NORMAL,
            enqueued_at=1001.0,
            depends_on=[],
            repos=["repo/free"],
        )
        scheduler.enqueue(entry_low)

        result = scheduler.dequeue_next()
        assert result is not None
        assert result.task_id == "task-low"


# ---------------------------------------------------------------------------
# Rate Limit Tracker Tests
# Validates: Requirement 17.5
# ---------------------------------------------------------------------------


class TestRateLimitTracker:
    """Tests for per-provider rate limit tracking with call queuing."""

    def test_unconfigured_provider_always_allows(self) -> None:
        """Providers without rate limit config always allow calls."""
        tracker = RateLimitTracker()
        assert tracker.can_make_call("openai") is True
        assert tracker.get_remaining_capacity("openai") == -1

    def test_configured_provider_enforces_limit(self) -> None:
        """Calls are blocked when limit is reached within the window."""
        tracker = RateLimitTracker()
        tracker.configure_provider(RateLimitConfig(
            provider="openai",
            max_calls_per_window=3,
            window_seconds=60.0,
        ))

        now = time.time()
        # Record 3 calls
        for i in range(3):
            assert tracker.can_make_call("openai", now) is True
            tracker.record_call("openai", f"task-{i}", now)

        # 4th call should be blocked
        assert tracker.can_make_call("openai", now) is False
        assert tracker.get_remaining_capacity("openai", now) == 0

    def test_window_rotation_frees_capacity(self) -> None:
        """Calls from expired windows are pruned, freeing capacity."""
        tracker = RateLimitTracker()
        tracker.configure_provider(RateLimitConfig(
            provider="anthropic",
            max_calls_per_window=2,
            window_seconds=60.0,
        ))

        base_time = 1000.0
        tracker.record_call("anthropic", "task-1", base_time)
        tracker.record_call("anthropic", "task-1", base_time + 1)

        # At base_time + 2, limit is reached
        assert tracker.can_make_call("anthropic", base_time + 2) is False

        # After window rotates (61 seconds later), capacity is freed
        assert tracker.can_make_call("anthropic", base_time + 61) is True
        assert tracker.get_remaining_capacity("anthropic", base_time + 61) == 2

    def test_queue_call_and_dequeue(self) -> None:
        """Calls can be queued and dequeued in FIFO order."""
        tracker = RateLimitTracker()
        tracker.configure_provider(RateLimitConfig(
            provider="openai",
            max_calls_per_window=1,
            window_seconds=60.0,
        ))

        # Queue multiple calls
        pos1 = tracker.queue_call("openai", "task-1")
        pos2 = tracker.queue_call("openai", "task-2")
        pos3 = tracker.queue_call("openai", "task-3")

        assert pos1 == 0
        assert pos2 == 1
        assert pos3 == 2
        assert tracker.get_queue_depth("openai") == 3

        # Dequeue in order
        assert tracker.dequeue_pending("openai") == "task-1"
        assert tracker.dequeue_pending("openai") == "task-2"
        assert tracker.dequeue_pending("openai") == "task-3"
        assert tracker.dequeue_pending("openai") is None

    def test_release_task_frees_provider_association(self) -> None:
        """Releasing a task removes it from active tasks for a provider."""
        tracker = RateLimitTracker()
        tracker.configure_provider(RateLimitConfig(
            provider="openai",
            max_calls_per_window=10,
            window_seconds=60.0,
        ))

        tracker.record_call("openai", "task-1")
        assert "task-1" in tracker.get_active_tasks("openai")

        tracker.release_task("openai", "task-1")
        assert "task-1" not in tracker.get_active_tasks("openai")

    def test_get_window_usage(self) -> None:
        """Window usage returns correct current/max tuple."""
        tracker = RateLimitTracker()
        tracker.configure_provider(RateLimitConfig(
            provider="openai",
            max_calls_per_window=5,
            window_seconds=60.0,
        ))

        now = time.time()
        tracker.record_call("openai", "task-1", now)
        tracker.record_call("openai", "task-1", now + 1)

        current, max_calls = tracker.get_window_usage("openai", now + 2)
        assert current == 2
        assert max_calls == 5

    def test_unconfigured_provider_window_usage(self) -> None:
        """Unconfigured providers return (0, 0) for window usage."""
        tracker = RateLimitTracker()
        current, max_calls = tracker.get_window_usage("unknown")
        assert current == 0
        assert max_calls == 0


# ---------------------------------------------------------------------------
# Preemption + Rate Limiter Integration Tests
# Validates: Requirements 16.3, 17.5
# ---------------------------------------------------------------------------


class TestPreemptionRateLimiterIntegration:
    """Tests that preemption correctly interacts with the rate limiter."""

    def test_preemption_releases_provider_capacity(self) -> None:
        """When a task is preempted, its provider rate limit association is freed."""
        rate_limiter = RateLimitTracker()
        rate_limiter.configure_provider(RateLimitConfig(
            provider="openai",
            max_calls_per_window=10,
            window_seconds=60.0,
        ))

        scheduler = Scheduler(
            config=SchedulerConfig(max_concurrency=1),
            rate_limiter=rate_limiter,
        )

        # Start a task with formation (provider) = "openai"
        entry = TaskQueueEntry(
            task_id="task-low",
            priority=Priority.LOW,
            enqueued_at=1000.0,
            depends_on=[],
            formation="openai",
        )
        scheduler.enqueue(entry)
        scheduler.dequeue_next()

        # Record that the task is using the provider
        rate_limiter.record_call("openai", "task-low")
        assert "task-low" in rate_limiter.get_active_tasks("openai")

        # Preempt for a critical task
        preempted = scheduler.preempt_for_critical("critical-task")
        assert preempted == "task-low"

        # Provider capacity should be freed
        assert "task-low" not in rate_limiter.get_active_tasks("openai")

    def test_preemption_without_formation_doesnt_crash(self) -> None:
        """Preemption works even if the task has no formation set."""
        scheduler = Scheduler(config=SchedulerConfig(max_concurrency=1))

        entry = TaskQueueEntry(
            task_id="task-low",
            priority=Priority.LOW,
            enqueued_at=1000.0,
            depends_on=[],
            formation=None,  # No formation
        )
        scheduler.enqueue(entry)
        scheduler.dequeue_next()

        # Should not crash
        preempted = scheduler.preempt_for_critical("critical-task")
        assert preempted == "task-low"


# ---------------------------------------------------------------------------
# Dynamic Max Concurrency Tests
# Validates: Requirement 17.1
# ---------------------------------------------------------------------------


class TestDynamicMaxConcurrency:
    """Tests that max_concurrency can be dynamically updated."""

    def test_max_concurrency_update_takes_effect(self) -> None:
        """Changing max_concurrency in config affects subsequent scheduling decisions."""
        config = SchedulerConfig(max_concurrency=2)
        scheduler = Scheduler(config=config)

        # Enqueue 4 tasks
        for i in range(4):
            scheduler.enqueue(TaskQueueEntry(
                task_id=f"task-{i}",
                priority=Priority.NORMAL,
                enqueued_at=1000.0 + i,
                depends_on=[],
            ))

        # With max_concurrency=2, only 2 should be dequeued
        scheduler.dequeue_next()
        scheduler.dequeue_next()
        assert scheduler.dequeue_next() is None
        assert scheduler.get_running_count() == 2

        # Dynamically increase max_concurrency
        config.max_concurrency = 4

        # Now more tasks can be dequeued
        task3 = scheduler.dequeue_next()
        assert task3 is not None
        task4 = scheduler.dequeue_next()
        assert task4 is not None
        assert scheduler.get_running_count() == 4

    def test_max_concurrency_decrease_doesnt_preempt(self) -> None:
        """Decreasing max_concurrency doesn't preempt already-running tasks."""
        config = SchedulerConfig(max_concurrency=3)
        scheduler = Scheduler(config=config)

        for i in range(3):
            scheduler.enqueue(TaskQueueEntry(
                task_id=f"task-{i}",
                priority=Priority.NORMAL,
                enqueued_at=1000.0 + i,
                depends_on=[],
            ))
            scheduler.dequeue_next()

        assert scheduler.get_running_count() == 3

        # Decrease max_concurrency
        config.max_concurrency = 1

        # Already-running tasks stay running
        assert scheduler.get_running_count() == 3

        # But no new tasks can be dequeued until running count drops
        scheduler.enqueue(TaskQueueEntry(
            task_id="task-new",
            priority=Priority.NORMAL,
            enqueued_at=2000.0,
            depends_on=[],
        ))
        assert scheduler.dequeue_next() is None

    def test_max_concurrency_bounds(self) -> None:
        """max_concurrency stays within valid bounds (1-10)."""
        # Test lower bound
        config = SchedulerConfig(max_concurrency=1)
        assert config.max_concurrency == 1

        # Test upper bound
        config = SchedulerConfig(max_concurrency=10)
        assert config.max_concurrency == 10

        # Test invalid values raise validation errors
        with pytest.raises(Exception):
            SchedulerConfig(max_concurrency=0)

        with pytest.raises(Exception):
            SchedulerConfig(max_concurrency=11)
