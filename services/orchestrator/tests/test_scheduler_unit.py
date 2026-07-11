"""Unit tests for Task Scheduler (Task 19.6).

Tests:
- Priority ordering with mixed priorities (all 4 levels)
- Preemption only affects the lowest-priority running task
- Dependency cascade failure propagation with status verification
- Timeout checkpoint and pause behavior

Validates: Requirements 16.2, 16.3, 18.3, 20.1
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from vikram_orchestrator.scheduler import (
    Priority,
    Scheduler,
    SchedulerConfig,
    TaskQueueEntry,
    TimeoutResult,
)


# ---------------------------------------------------------------------------
# Priority Ordering Tests
# Validates: Requirement 16.2
# ---------------------------------------------------------------------------


class TestPriorityOrderingMixed:
    """Tests that mixed-priority tasks are dequeued in strict priority order."""

    def test_dequeue_order_all_four_priorities(self) -> None:
        """Tasks with all 4 priority levels are dequeued critical > high > normal > low."""
        scheduler = Scheduler(SchedulerConfig(max_concurrency=10))

        # Enqueue in reverse priority order (low first) to prove ordering is by priority
        scheduler.enqueue(TaskQueueEntry(
            task_id="low-task", priority=Priority.LOW, enqueued_at=1000.0,
        ))
        scheduler.enqueue(TaskQueueEntry(
            task_id="normal-task", priority=Priority.NORMAL, enqueued_at=1001.0,
        ))
        scheduler.enqueue(TaskQueueEntry(
            task_id="high-task", priority=Priority.HIGH, enqueued_at=1002.0,
        ))
        scheduler.enqueue(TaskQueueEntry(
            task_id="critical-task", priority=Priority.CRITICAL, enqueued_at=1003.0,
        ))

        # Dequeue should yield tasks in priority order
        t1 = scheduler.dequeue_next()
        t2 = scheduler.dequeue_next()
        t3 = scheduler.dequeue_next()
        t4 = scheduler.dequeue_next()

        assert t1.task_id == "critical-task"
        assert t2.task_id == "high-task"
        assert t3.task_id == "normal-task"
        assert t4.task_id == "low-task"

    def test_fifo_within_same_priority_mixed_queue(self) -> None:
        """Within the same priority level, FIFO ordering is maintained even when
        other priorities are interspersed."""
        scheduler = Scheduler(SchedulerConfig(max_concurrency=10))

        # Interleave normal and high tasks
        scheduler.enqueue(TaskQueueEntry(
            task_id="normal-1", priority=Priority.NORMAL, enqueued_at=1000.0,
        ))
        scheduler.enqueue(TaskQueueEntry(
            task_id="high-1", priority=Priority.HIGH, enqueued_at=1001.0,
        ))
        scheduler.enqueue(TaskQueueEntry(
            task_id="normal-2", priority=Priority.NORMAL, enqueued_at=1002.0,
        ))
        scheduler.enqueue(TaskQueueEntry(
            task_id="high-2", priority=Priority.HIGH, enqueued_at=1003.0,
        ))

        t1 = scheduler.dequeue_next()
        t2 = scheduler.dequeue_next()
        t3 = scheduler.dequeue_next()
        t4 = scheduler.dequeue_next()

        # High tasks first (FIFO among highs), then normal tasks (FIFO among normals)
        assert t1.task_id == "high-1"
        assert t2.task_id == "high-2"
        assert t3.task_id == "normal-1"
        assert t4.task_id == "normal-2"

    def test_get_queue_returns_priority_ordered_view(self) -> None:
        """get_queue() returns tasks sorted by priority then enqueued_at."""
        scheduler = Scheduler(SchedulerConfig(max_concurrency=10))

        scheduler.enqueue(TaskQueueEntry(
            task_id="low-1", priority=Priority.LOW, enqueued_at=1000.0,
        ))
        scheduler.enqueue(TaskQueueEntry(
            task_id="critical-1", priority=Priority.CRITICAL, enqueued_at=1001.0,
        ))
        scheduler.enqueue(TaskQueueEntry(
            task_id="normal-1", priority=Priority.NORMAL, enqueued_at=1002.0,
        ))
        scheduler.enqueue(TaskQueueEntry(
            task_id="high-1", priority=Priority.HIGH, enqueued_at=1003.0,
        ))

        queue = scheduler.get_queue()
        task_ids = [t.task_id for t in queue]

        assert task_ids == ["critical-1", "high-1", "normal-1", "low-1"]


# ---------------------------------------------------------------------------
# Preemption Tests
# Validates: Requirement 16.3
# ---------------------------------------------------------------------------


class TestPreemptionTargetsLowest:
    """Tests that preemption only affects the lowest-priority running task."""

    def test_preempt_selects_lowest_priority_among_running(self) -> None:
        """When multiple tasks run at different priorities, preemption hits the lowest."""
        scheduler = Scheduler(SchedulerConfig(max_concurrency=3))

        # Start three tasks at different priorities
        scheduler.enqueue(TaskQueueEntry(
            task_id="high-running", priority=Priority.HIGH, enqueued_at=1000.0,
        ))
        scheduler.enqueue(TaskQueueEntry(
            task_id="normal-running", priority=Priority.NORMAL, enqueued_at=1001.0,
        ))
        scheduler.enqueue(TaskQueueEntry(
            task_id="low-running", priority=Priority.LOW, enqueued_at=1002.0,
        ))

        scheduler.dequeue_next()  # high-running
        scheduler.dequeue_next()  # normal-running
        scheduler.dequeue_next()  # low-running

        # Verify all three are running
        assert scheduler.get_running_count() == 3

        # Preempt for a critical task — should preempt the low-priority task
        preempted = scheduler.preempt_for_critical("critical-incoming")
        assert preempted == "low-running"

        # Verify the preempted task's status
        queue = scheduler.get_queue()
        low_task = [t for t in queue if t.task_id == "low-running"][0]
        assert low_task.status == "preempted"

        # High and normal are still running
        high_task = [t for t in queue if t.task_id == "high-running"][0]
        normal_task = [t for t in queue if t.task_id == "normal-running"][0]
        assert high_task.status == "running"
        assert normal_task.status == "running"

    def test_preempt_does_not_affect_critical_running_task(self) -> None:
        """A critical running task cannot be preempted by another critical task."""
        scheduler = Scheduler(SchedulerConfig(max_concurrency=1))

        scheduler.enqueue(TaskQueueEntry(
            task_id="critical-running", priority=Priority.CRITICAL, enqueued_at=1000.0,
        ))
        scheduler.dequeue_next()

        # Cannot preempt a critical task
        preempted = scheduler.preempt_for_critical("another-critical")
        assert preempted is None

    def test_preempt_with_two_same_priority_selects_most_recent(self) -> None:
        """When two tasks share the lowest priority, preempt the most recently started."""
        scheduler = Scheduler(SchedulerConfig(max_concurrency=3))

        # Enqueue and dequeue in sequence so started_at timestamps differ
        entry1 = TaskQueueEntry(
            task_id="normal-a", priority=Priority.NORMAL, enqueued_at=1000.0,
        )
        entry2 = TaskQueueEntry(
            task_id="normal-b", priority=Priority.NORMAL, enqueued_at=1001.0,
        )
        entry3 = TaskQueueEntry(
            task_id="high-c", priority=Priority.HIGH, enqueued_at=1002.0,
        )

        scheduler.enqueue(entry1)
        scheduler.enqueue(entry2)
        scheduler.enqueue(entry3)

        # Dequeue in priority order: high-c first, then normal-a, normal-b
        with patch("vikram_orchestrator.scheduler.time.time", return_value=2000.0):
            scheduler.dequeue_next()  # high-c starts at 2000
        with patch("vikram_orchestrator.scheduler.time.time", return_value=2001.0):
            scheduler.dequeue_next()  # normal-a starts at 2001
        with patch("vikram_orchestrator.scheduler.time.time", return_value=2002.0):
            scheduler.dequeue_next()  # normal-b starts at 2002

        # Both normal tasks are lowest priority; normal-b started later (less progress)
        preempted = scheduler.preempt_for_critical("critical-incoming")
        assert preempted == "normal-b"

    def test_preempted_task_can_be_rescheduled(self) -> None:
        """A preempted task retains 'preempted' status and can be re-dequeued later."""
        scheduler = Scheduler(SchedulerConfig(max_concurrency=2))

        scheduler.enqueue(TaskQueueEntry(
            task_id="normal-task", priority=Priority.NORMAL, enqueued_at=1000.0,
        ))
        scheduler.enqueue(TaskQueueEntry(
            task_id="low-task", priority=Priority.LOW, enqueued_at=1001.0,
        ))

        scheduler.dequeue_next()  # normal-task
        scheduler.dequeue_next()  # low-task

        # Preempt
        preempted = scheduler.preempt_for_critical("crit-1")
        assert preempted == "low-task"
        assert scheduler.get_running_count() == 1

        # The preempted task can now be picked up again (after the critical task or
        # when concurrency allows)
        # First, complete normal-task to free a slot
        scheduler.resolve_dependencies("normal-task")
        # Now dequeue — the preempted low-task should be available
        rescheduled = scheduler.dequeue_next()
        assert rescheduled is not None
        assert rescheduled.task_id == "low-task"
        assert rescheduled.status == "running"


# ---------------------------------------------------------------------------
# Dependency Cascade Failure Propagation Tests
# Validates: Requirement 18.3
# ---------------------------------------------------------------------------


class TestDependencyCascadeFailure:
    """Tests that dependency failure cascades correctly mark all dependents."""

    def test_single_dependency_failure_marks_dependent(self) -> None:
        """A task depending on a failed task is marked 'dependency_failed'."""
        scheduler = Scheduler(SchedulerConfig(max_concurrency=10))

        scheduler.enqueue(TaskQueueEntry(
            task_id="root", priority=Priority.HIGH, enqueued_at=1000.0,
        ))
        scheduler.dequeue_next()

        scheduler.enqueue(TaskQueueEntry(
            task_id="child", priority=Priority.NORMAL, enqueued_at=1001.0,
            depends_on=["root"],
        ))

        cascade = scheduler.mark_dependency_failed("root")

        assert "child" in cascade
        queue = scheduler.get_queue()
        child = [t for t in queue if t.task_id == "child"][0]
        assert child.status == "dependency_failed"

    def test_multi_level_cascade_propagation(self) -> None:
        """Failure propagates transitively: A→B→C, failing A cascades to B and C."""
        scheduler = Scheduler(SchedulerConfig(max_concurrency=10))

        scheduler.enqueue(TaskQueueEntry(
            task_id="a", priority=Priority.HIGH, enqueued_at=1000.0,
        ))
        scheduler.dequeue_next()

        scheduler.enqueue(TaskQueueEntry(
            task_id="b", priority=Priority.NORMAL, enqueued_at=1001.0,
            depends_on=["a"],
        ))
        scheduler.enqueue(TaskQueueEntry(
            task_id="c", priority=Priority.NORMAL, enqueued_at=1002.0,
            depends_on=["b"],
        ))

        cascade = scheduler.mark_dependency_failed("a")

        assert set(cascade) == {"b", "c"}
        queue = scheduler.get_queue()
        for task in queue:
            assert task.status == "dependency_failed"

    def test_cascade_does_not_affect_unrelated_tasks(self) -> None:
        """Tasks without dependency links remain schedulable after cascade."""
        scheduler = Scheduler(SchedulerConfig(max_concurrency=10))

        scheduler.enqueue(TaskQueueEntry(
            task_id="root", priority=Priority.HIGH, enqueued_at=1000.0,
        ))
        scheduler.dequeue_next()

        scheduler.enqueue(TaskQueueEntry(
            task_id="dependent", priority=Priority.NORMAL, enqueued_at=1001.0,
            depends_on=["root"],
        ))
        scheduler.enqueue(TaskQueueEntry(
            task_id="unrelated", priority=Priority.NORMAL, enqueued_at=1002.0,
            depends_on=[],
        ))

        cascade = scheduler.mark_dependency_failed("root")

        assert "dependent" in cascade
        assert "unrelated" not in cascade

        # Unrelated task can still be scheduled
        task = scheduler.dequeue_next()
        assert task is not None
        assert task.task_id == "unrelated"
        assert task.status == "running"

    def test_multiple_roots_partial_failure(self) -> None:
        """Task with multiple deps only fails if ANY dependency fails."""
        scheduler = Scheduler(SchedulerConfig(max_concurrency=10))

        # dep-a and dep-b are both roots
        scheduler.enqueue(TaskQueueEntry(
            task_id="dep-a", priority=Priority.HIGH, enqueued_at=1000.0,
        ))
        scheduler.enqueue(TaskQueueEntry(
            task_id="dep-b", priority=Priority.HIGH, enqueued_at=1001.0,
        ))
        scheduler.dequeue_next()  # dep-a
        scheduler.dequeue_next()  # dep-b

        # child depends on both dep-a and dep-b
        scheduler.enqueue(TaskQueueEntry(
            task_id="child", priority=Priority.NORMAL, enqueued_at=1002.0,
            depends_on=["dep-a", "dep-b"],
        ))

        # Fail dep-a — child is cascade-failed even though dep-b is fine
        cascade = scheduler.mark_dependency_failed("dep-a")
        assert "child" in cascade

        queue = scheduler.get_queue()
        child = [t for t in queue if t.task_id == "child"][0]
        assert child.status == "dependency_failed"


# ---------------------------------------------------------------------------
# Timeout Checkpoint and Pause Behavior Tests
# Validates: Requirement 20.1
# ---------------------------------------------------------------------------


class TestTimeoutCheckpointPause:
    """Tests that timeout enforcement checkpoints the task then pauses it."""

    def test_timeout_transitions_task_to_paused(self) -> None:
        """A task exceeding its timeout is moved to 'paused' status."""
        scheduler = Scheduler(SchedulerConfig(max_concurrency=10))

        entry = TaskQueueEntry(
            task_id="timeout-task",
            priority=Priority.NORMAL,
            enqueued_at=1000.0,
            timeout_seconds=120,
        )
        scheduler.enqueue(entry)
        scheduler.dequeue_next()

        # Set known started_at
        task = scheduler._find_task("timeout-task")
        task.started_at = 1000.0

        # Time advances past the timeout
        with patch("vikram_orchestrator.scheduler.time.time", return_value=1121.0):
            results = scheduler.run_timeout_checks()

        assert len(results) == 1
        assert results[0].task_id == "timeout-task"
        assert results[0].reason == "task_timeout"
        assert results[0].action == "checkpoint_pause_notify"

        # Verify the task is now paused
        task = scheduler._find_task("timeout-task")
        assert task.status == "paused"

    def test_checkpoint_called_before_pause(self) -> None:
        """The on_checkpoint callback fires before the task is paused."""
        call_order: list[str] = []

        def on_checkpoint(task_id: str) -> None:
            # Record the task's status at checkpoint time
            task = scheduler._find_task(task_id)
            call_order.append(f"checkpoint:{task.status}")

        scheduler = Scheduler(
            SchedulerConfig(max_concurrency=10),
            on_checkpoint=on_checkpoint,
        )

        entry = TaskQueueEntry(
            task_id="ordered-task",
            priority=Priority.NORMAL,
            enqueued_at=1000.0,
            timeout_seconds=60,
        )
        scheduler.enqueue(entry)
        scheduler.dequeue_next()

        task = scheduler._find_task("ordered-task")
        task.started_at = 1000.0

        with patch("vikram_orchestrator.scheduler.time.time", return_value=1061.0):
            scheduler.run_timeout_checks()

        # At checkpoint time, task was still "running" (not yet paused)
        assert call_order == ["checkpoint:running"]
        # After the check, it should be paused
        task = scheduler._find_task("ordered-task")
        assert task.status == "paused"

    def test_paused_task_can_be_extended_and_resumed(self) -> None:
        """A paused-on-timeout task can have its timeout extended and then resumed."""
        scheduler = Scheduler(SchedulerConfig(max_concurrency=10))

        entry = TaskQueueEntry(
            task_id="extend-task",
            priority=Priority.NORMAL,
            enqueued_at=1000.0,
            timeout_seconds=100,
        )
        scheduler.enqueue(entry)
        scheduler.dequeue_next()

        task = scheduler._find_task("extend-task")
        task.started_at = 1000.0

        # Trigger timeout
        with patch("vikram_orchestrator.scheduler.time.time", return_value=1101.0):
            scheduler.run_timeout_checks()

        assert task.status == "paused"

        # Extend timeout
        extended = scheduler.extend_timeout("extend-task", 300)
        assert extended is True
        assert task.timeout_seconds == 400  # 100 + 300

        # Resume
        resumed = scheduler.resume_after_timeout("extend-task")
        assert resumed is True
        assert task.status == "running"

    def test_timeout_result_includes_elapsed_and_phase(self) -> None:
        """TimeoutResult contains elapsed time and current phase information."""
        scheduler = Scheduler(SchedulerConfig(max_concurrency=10))

        entry = TaskQueueEntry(
            task_id="detailed-task",
            priority=Priority.NORMAL,
            enqueued_at=1000.0,
            timeout_seconds=200,
        )
        scheduler.enqueue(entry)
        scheduler.dequeue_next()
        scheduler.start_phase("detailed-task", "implementation")

        task = scheduler._find_task("detailed-task")
        task.started_at = 1000.0

        # Exceed total timeout
        with patch("vikram_orchestrator.scheduler.time.time", return_value=1201.0):
            results = scheduler.run_timeout_checks()

        assert len(results) == 1
        result = results[0]
        assert result.elapsed_seconds == pytest.approx(201.0, abs=1.0)
        assert result.timeout_seconds == 200
        assert result.current_phase == "implementation"
