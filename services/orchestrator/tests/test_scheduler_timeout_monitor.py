"""Unit tests for dependency resolution, timeout enforcement, and TimeoutMonitor.

Tests:
- Transitive dependency failure cascading
- Background timeout monitor (threaded and async)
- Phase timeout warning at 50% threshold
- On timeout: checkpoint + pause + notify callbacks

Validates: Requirements 18.1, 18.2, 18.3, 20.1, 20.2, 20.3, 20.4
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest

from vikram_orchestrator.scheduler import (
    Priority,
    Scheduler,
    SchedulerConfig,
    TaskQueueEntry,
    TimeoutMonitor,
    TimeoutResult,
    TIMEOUT_CHECK_INTERVAL,
)


# ---------------------------------------------------------------------------
# Dependency Resolution Tests
# Validates: Requirements 18.1, 18.2, 18.3
# ---------------------------------------------------------------------------


class TestDependencyResolution:
    """Tests for resolve_dependencies and mark_dependency_failed."""

    def test_resolve_dependencies_unblocks_ready_tasks(self) -> None:
        """When a dependency completes, blocked dependents become queued."""
        scheduler = Scheduler(SchedulerConfig(max_concurrency=10))

        # Task A (no deps) and Task B (depends on A)
        scheduler.enqueue(TaskQueueEntry(
            task_id="task-a", priority=Priority.HIGH, enqueued_at=1000.0, depends_on=[],
        ))
        scheduler.enqueue(TaskQueueEntry(
            task_id="task-b", priority=Priority.NORMAL, enqueued_at=1001.0, depends_on=["task-a"],
        ))

        # B should be blocked
        queue = scheduler.get_queue()
        task_b = [t for t in queue if t.task_id == "task-b"][0]
        assert task_b.status == "blocked"

        # Run A
        scheduler.dequeue_next()
        # Complete A
        unblocked = scheduler.resolve_dependencies("task-a")

        assert "task-b" in unblocked

    def test_transitive_cascade_three_levels(self) -> None:
        """Failing C cascades to B (depends on C) and A (depends on B)."""
        scheduler = Scheduler(SchedulerConfig(max_concurrency=10))

        scheduler.enqueue(TaskQueueEntry(
            task_id="c", priority=Priority.HIGH, enqueued_at=1000.0, depends_on=[],
        ))
        scheduler.dequeue_next()  # Start C

        scheduler.enqueue(TaskQueueEntry(
            task_id="b", priority=Priority.NORMAL, enqueued_at=1001.0, depends_on=["c"],
        ))
        scheduler.enqueue(TaskQueueEntry(
            task_id="a", priority=Priority.NORMAL, enqueued_at=1002.0, depends_on=["b"],
        ))

        cascade = scheduler.mark_dependency_failed("c")

        assert "b" in cascade
        assert "a" in cascade

    def test_transitive_cascade_diamond_dependency(self) -> None:
        """Diamond: D depends on B and C, both depend on A. Failing A cascades to all."""
        scheduler = Scheduler(SchedulerConfig(max_concurrency=10))

        scheduler.enqueue(TaskQueueEntry(
            task_id="a", priority=Priority.HIGH, enqueued_at=1000.0, depends_on=[],
        ))
        scheduler.dequeue_next()

        scheduler.enqueue(TaskQueueEntry(
            task_id="b", priority=Priority.NORMAL, enqueued_at=1001.0, depends_on=["a"],
        ))
        scheduler.enqueue(TaskQueueEntry(
            task_id="c", priority=Priority.NORMAL, enqueued_at=1002.0, depends_on=["a"],
        ))
        scheduler.enqueue(TaskQueueEntry(
            task_id="d", priority=Priority.NORMAL, enqueued_at=1003.0, depends_on=["b", "c"],
        ))

        cascade = scheduler.mark_dependency_failed("a")

        assert "b" in cascade
        assert "c" in cascade
        assert "d" in cascade

    def test_cascade_does_not_affect_independent_tasks(self) -> None:
        """Tasks with no dependency on the failed task are unaffected."""
        scheduler = Scheduler(SchedulerConfig(max_concurrency=10))

        scheduler.enqueue(TaskQueueEntry(
            task_id="root", priority=Priority.HIGH, enqueued_at=1000.0, depends_on=[],
        ))
        scheduler.dequeue_next()

        scheduler.enqueue(TaskQueueEntry(
            task_id="dependent", priority=Priority.NORMAL, enqueued_at=1001.0, depends_on=["root"],
        ))
        scheduler.enqueue(TaskQueueEntry(
            task_id="independent", priority=Priority.NORMAL, enqueued_at=1002.0, depends_on=[],
        ))

        cascade = scheduler.mark_dependency_failed("root")

        assert "dependent" in cascade
        assert "independent" not in cascade

        # Independent task can still be dequeued
        task = scheduler.dequeue_next()
        assert task is not None
        assert task.task_id == "independent"


# ---------------------------------------------------------------------------
# Timeout Enforcement and Callbacks
# Validates: Requirements 20.1, 20.2, 20.3, 20.4
# ---------------------------------------------------------------------------


class TestTimeoutCallbacks:
    """Tests for checkpoint and notify callbacks during timeout enforcement."""

    def test_timeout_invokes_checkpoint_callback(self) -> None:
        """On task timeout, the checkpoint callback is invoked before pausing."""
        checkpoint_calls: list[str] = []

        def on_checkpoint(task_id: str) -> None:
            checkpoint_calls.append(task_id)

        scheduler = Scheduler(
            SchedulerConfig(max_concurrency=10),
            on_checkpoint=on_checkpoint,
        )

        entry = TaskQueueEntry(
            task_id="timeout-task",
            priority=Priority.NORMAL,
            enqueued_at=1000.0,
            depends_on=[],
            timeout_seconds=100,
        )
        scheduler.enqueue(entry)
        scheduler.dequeue_next()

        # Manually set started_at to a known value
        task = scheduler._find_task("timeout-task")
        task.started_at = 1000.0

        # Simulate time past timeout
        fake_now = 1000.0 + 101
        with patch("vikram_orchestrator.scheduler.time.time", return_value=fake_now):
            results = scheduler.run_timeout_checks()

        assert len(results) == 1
        assert results[0].action == "checkpoint_pause_notify"
        assert "timeout-task" in checkpoint_calls

    def test_timeout_invokes_notify_callback(self) -> None:
        """On task timeout, the notify callback is invoked with TimeoutResult."""
        notify_calls: list[TimeoutResult] = []

        def on_notify(result: TimeoutResult) -> None:
            notify_calls.append(result)

        scheduler = Scheduler(
            SchedulerConfig(max_concurrency=10),
            on_timeout_notify=on_notify,
        )

        entry = TaskQueueEntry(
            task_id="notify-task",
            priority=Priority.NORMAL,
            enqueued_at=1000.0,
            depends_on=[],
            timeout_seconds=60,
        )
        scheduler.enqueue(entry)
        scheduler.dequeue_next()

        # Manually set started_at
        task = scheduler._find_task("notify-task")
        task.started_at = 1000.0

        fake_now = 1000.0 + 61
        with patch("vikram_orchestrator.scheduler.time.time", return_value=fake_now):
            results = scheduler.run_timeout_checks()

        assert len(results) == 1
        assert len(notify_calls) == 1
        assert notify_calls[0].task_id == "notify-task"
        assert notify_calls[0].reason == "task_timeout"

    def test_phase_timeout_warning_invokes_notify(self) -> None:
        """Phase timeout warning at 50% also triggers the notify callback."""
        notify_calls: list[TimeoutResult] = []

        def on_notify(result: TimeoutResult) -> None:
            notify_calls.append(result)

        scheduler = Scheduler(
            SchedulerConfig(max_concurrency=10, phase_timeout_pct=0.5),
            on_timeout_notify=on_notify,
        )

        entry = TaskQueueEntry(
            task_id="phase-task",
            priority=Priority.NORMAL,
            enqueued_at=1000.0,
            depends_on=[],
            timeout_seconds=200,
        )
        scheduler.enqueue(entry)
        scheduler.dequeue_next()
        scheduler.start_phase("phase-task", "implementation")

        # Phase has consumed 51% of total timeout (102s out of 200s)
        # But total elapsed is only 102s (well under 200s timeout)
        fake_now = 1000.0 + 102
        with patch("vikram_orchestrator.scheduler.time.time", return_value=fake_now):
            # Need to set current_phase_started_at manually since start_phase uses real time
            task = scheduler._find_task("phase-task")
            task.current_phase_started_at = 1000.0
            results = scheduler.run_timeout_checks()

        assert len(results) == 1
        assert results[0].reason == "phase_timeout_warning"
        assert len(notify_calls) == 1
        assert notify_calls[0].reason == "phase_timeout_warning"

    def test_checkpoint_failure_does_not_prevent_pause(self) -> None:
        """If checkpoint callback raises, the task is still paused."""
        def failing_checkpoint(task_id: str) -> None:
            raise RuntimeError("checkpoint storage unavailable")

        scheduler = Scheduler(
            SchedulerConfig(max_concurrency=10),
            on_checkpoint=failing_checkpoint,
        )

        entry = TaskQueueEntry(
            task_id="resilient-task",
            priority=Priority.NORMAL,
            enqueued_at=1000.0,
            depends_on=[],
            timeout_seconds=100,
        )
        scheduler.enqueue(entry)
        scheduler.dequeue_next()

        # Manually set started_at
        task = scheduler._find_task("resilient-task")
        task.started_at = 1000.0

        fake_now = 1000.0 + 101
        with patch("vikram_orchestrator.scheduler.time.time", return_value=fake_now):
            results = scheduler.run_timeout_checks()

        # Task should still be paused despite checkpoint failure
        assert len(results) == 1
        task = scheduler._find_task("resilient-task")
        assert task.status == "paused"

    def test_notify_failure_does_not_crash_monitor(self) -> None:
        """If notify callback raises, the monitor doesn't crash."""
        def failing_notify(result: TimeoutResult) -> None:
            raise RuntimeError("notification service down")

        scheduler = Scheduler(
            SchedulerConfig(max_concurrency=10),
            on_timeout_notify=failing_notify,
        )

        entry = TaskQueueEntry(
            task_id="robust-task",
            priority=Priority.NORMAL,
            enqueued_at=1000.0,
            depends_on=[],
            timeout_seconds=100,
        )
        scheduler.enqueue(entry)
        scheduler.dequeue_next()

        # Manually set started_at
        task = scheduler._find_task("robust-task")
        task.started_at = 1000.0

        fake_now = 1000.0 + 101
        with patch("vikram_orchestrator.scheduler.time.time", return_value=fake_now):
            # Should not raise
            results = scheduler.run_timeout_checks()

        assert len(results) == 1
        task = scheduler._find_task("robust-task")
        assert task.status == "paused"


# ---------------------------------------------------------------------------
# TimeoutMonitor Tests (Background Monitor)
# Validates: Requirements 20.1, 20.2
# ---------------------------------------------------------------------------


class TestTimeoutMonitor:
    """Tests for the TimeoutMonitor background thread/async task."""

    def test_monitor_starts_and_stops(self) -> None:
        """Monitor can be started and stopped cleanly."""
        scheduler = Scheduler(SchedulerConfig(max_concurrency=10))
        monitor = TimeoutMonitor(scheduler, interval_seconds=0.1)

        assert not monitor.is_running

        monitor.start()
        assert monitor.is_running

        monitor.stop()
        assert not monitor.is_running

    def test_monitor_detects_timeout_within_interval(self) -> None:
        """Monitor detects task timeout within one check cycle."""
        checkpoint_calls: list[str] = []
        notify_calls: list[TimeoutResult] = []

        scheduler = Scheduler(
            SchedulerConfig(max_concurrency=10),
            on_checkpoint=lambda tid: checkpoint_calls.append(tid),
            on_timeout_notify=lambda r: notify_calls.append(r),
        )

        entry = TaskQueueEntry(
            task_id="monitored-task",
            priority=Priority.NORMAL,
            enqueued_at=time.time() - 200,  # Enqueued in the past
            depends_on=[],
            timeout_seconds=1,  # 1 second timeout — will already be exceeded
        )
        scheduler.enqueue(entry)
        dequeued = scheduler.dequeue_next()
        # Force started_at far in the past so it's already timed out
        dequeued.started_at = time.time() - 10

        # Run monitor for one quick cycle
        monitor = TimeoutMonitor(scheduler, interval_seconds=0.05)
        monitor.start()
        time.sleep(0.2)  # Wait for at least one check cycle
        monitor.stop()

        # Should have detected the timeout
        assert len(notify_calls) >= 1
        assert notify_calls[0].task_id == "monitored-task"
        assert "monitored-task" in checkpoint_calls

    def test_monitor_does_nothing_when_no_running_tasks(self) -> None:
        """Monitor runs without errors when queue is empty."""
        scheduler = Scheduler(SchedulerConfig(max_concurrency=10))
        monitor = TimeoutMonitor(scheduler, interval_seconds=0.05)

        monitor.start()
        time.sleep(0.15)
        monitor.stop()

        assert monitor.last_results == []

    def test_monitor_default_interval(self) -> None:
        """Monitor defaults to 30-second check interval."""
        scheduler = Scheduler(SchedulerConfig(max_concurrency=10))
        monitor = TimeoutMonitor(scheduler)

        assert monitor.interval_seconds == TIMEOUT_CHECK_INTERVAL
        assert monitor.interval_seconds == 30

    def test_monitor_double_start_is_safe(self) -> None:
        """Starting monitor twice doesn't create duplicate threads."""
        scheduler = Scheduler(SchedulerConfig(max_concurrency=10))
        monitor = TimeoutMonitor(scheduler, interval_seconds=0.1)

        monitor.start()
        monitor.start()  # Should log warning but not crash
        assert monitor.is_running

        monitor.stop()

    def test_monitor_async_mode(self) -> None:
        """Monitor runs correctly in async mode."""
        checkpoint_calls: list[str] = []
        notify_calls: list[TimeoutResult] = []

        scheduler = Scheduler(
            SchedulerConfig(max_concurrency=10),
            on_checkpoint=lambda tid: checkpoint_calls.append(tid),
            on_timeout_notify=lambda r: notify_calls.append(r),
        )

        entry = TaskQueueEntry(
            task_id="async-task",
            priority=Priority.NORMAL,
            enqueued_at=time.time() - 100,
            depends_on=[],
            timeout_seconds=1,
        )
        scheduler.enqueue(entry)
        dequeued = scheduler.dequeue_next()
        dequeued.started_at = time.time() - 10

        monitor = TimeoutMonitor(scheduler, interval_seconds=0.05)

        async def run_briefly() -> None:
            task = asyncio.create_task(monitor.run_async())
            await asyncio.sleep(0.15)
            monitor.stop()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(run_briefly())

        assert len(notify_calls) >= 1
        assert notify_calls[0].task_id == "async-task"


# ---------------------------------------------------------------------------
# Phase Timeout Warning Integration Tests
# Validates: Requirements 20.3, 20.4
# ---------------------------------------------------------------------------


class TestPhaseTimeoutWarning:
    """Tests for phase timeout warning behavior."""

    def test_phase_exceeding_50_percent_triggers_warning(self) -> None:
        """A single phase using >50% of total time triggers a warning."""
        scheduler = Scheduler(SchedulerConfig(max_concurrency=10, phase_timeout_pct=0.5))

        entry = TaskQueueEntry(
            task_id="phase-warn",
            priority=Priority.NORMAL,
            enqueued_at=1000.0,
            depends_on=[],
            timeout_seconds=1000,
        )
        scheduler.enqueue(entry)
        scheduler.dequeue_next()

        # Set phase start
        task = scheduler._find_task("phase-warn")
        task.current_phase = "implementation"
        task.current_phase_started_at = 1000.0

        # 510s elapsed — phase is 51% of timeout (1000s)
        fake_now = 1510.0
        with patch("vikram_orchestrator.scheduler.time.time", return_value=fake_now):
            results = scheduler.run_timeout_checks()

        assert len(results) == 1
        assert results[0].reason == "phase_timeout_warning"
        assert results[0].current_phase == "implementation"
        assert results[0].action == "warn"

    def test_extend_timeout_and_resume(self) -> None:
        """A paused-on-timeout task can be extended and resumed."""
        scheduler = Scheduler(SchedulerConfig(max_concurrency=10))

        entry = TaskQueueEntry(
            task_id="extend-me",
            priority=Priority.NORMAL,
            enqueued_at=1000.0,
            depends_on=[],
            timeout_seconds=100,
        )
        scheduler.enqueue(entry)
        scheduler.dequeue_next()

        # Manually set started_at
        task = scheduler._find_task("extend-me")
        task.started_at = 1000.0

        # Simulate timeout
        fake_now = 1000.0 + 101
        with patch("vikram_orchestrator.scheduler.time.time", return_value=fake_now):
            scheduler.run_timeout_checks()

        task = scheduler._find_task("extend-me")
        assert task.status == "paused"

        # Extend by 300 seconds
        assert scheduler.extend_timeout("extend-me", 300)
        assert task.timeout_seconds == 400  # 100 + 300

        # Resume
        assert scheduler.resume_after_timeout("extend-me")
        assert task.status == "running"
