"""Property-based tests for the Task Scheduler subsystem.

These tests define correctness properties for:
- Priority Queue Ordering with Preemption (Property 16)
- Concurrency Limit Invariant (Property 17)
- Dependency Enforcement (Property 18)
- Timeout Enforcement (Property 20)

Run with:
  cd services/orchestrator && .venv/bin/python -m pytest tests/test_scheduler_properties.py -v

Validates: Requirements 16.1, 16.2, 16.3, 17.1, 18.1, 18.2, 18.3, 20.1, 20.2, 20.4
"""

from __future__ import annotations

import time

import hypothesis.strategies as st
from hypothesis import given, settings, assume

from vikram_orchestrator.scheduler import (
    Priority,
    Scheduler,
    SchedulerConfig,
    TaskQueueEntry,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


def task_id_st() -> st.SearchStrategy[str]:
    """Generate plausible task identifiers."""
    return st.text(
        min_size=1,
        max_size=20,
        alphabet=st.characters(categories=("L", "N", "Pd")),
    )


def priority_st() -> st.SearchStrategy[Priority]:
    """Generate a random Priority value."""
    return st.sampled_from(list(Priority))


def non_critical_priority_st() -> st.SearchStrategy[Priority]:
    """Generate a non-critical priority value."""
    return st.sampled_from([Priority.HIGH, Priority.NORMAL, Priority.LOW])


def enqueued_at_st() -> st.SearchStrategy[float]:
    """Generate enqueue timestamps."""
    return st.floats(
        min_value=1000.0, max_value=2000.0,
        allow_nan=False, allow_infinity=False,
    )


def max_concurrency_st() -> st.SearchStrategy[int]:
    """Generate valid max concurrency values (1-10)."""
    return st.integers(min_value=1, max_value=10)


def timeout_seconds_st() -> st.SearchStrategy[int]:
    """Generate timeout values in seconds."""
    return st.integers(min_value=60, max_value=14400)


def task_entry_st(
    priority: st.SearchStrategy[Priority] | None = None,
) -> st.SearchStrategy[TaskQueueEntry]:
    """Generate a TaskQueueEntry with configurable priority strategy."""
    return st.builds(
        TaskQueueEntry,
        task_id=task_id_st(),
        priority=priority or priority_st(),
        enqueued_at=enqueued_at_st(),
        depends_on=st.just([]),
        repos=st.just([]),
        timeout_seconds=timeout_seconds_st(),
    )


def distinct_task_entries_st(
    min_size: int = 2,
    max_size: int = 8,
) -> st.SearchStrategy[list[TaskQueueEntry]]:
    """Generate a list of task entries with unique task_ids."""
    return st.lists(
        task_entry_st(),
        min_size=min_size,
        max_size=max_size,
    ).filter(lambda entries: len({e.task_id for e in entries}) == len(entries))


# ---------------------------------------------------------------------------
# Property 16: Priority Queue Ordering with Preemption
# Validates: Requirements 16.1, 16.2, 16.3
# ---------------------------------------------------------------------------


class TestPriorityQueueOrderingWithPreemption:
    """Tasks are dequeued in strict priority order (critical > high > normal > low).
    Within same priority, FIFO ordering is preserved. Critical tasks can preempt
    lowest-priority running tasks.

    **Validates: Requirements 16.1, 16.2, 16.3**
    """

    @given(entries=distinct_task_entries_st(min_size=2, max_size=10))
    @settings(max_examples=300)
    def test_dequeue_returns_highest_priority_first(
        self, entries: list[TaskQueueEntry]
    ) -> None:
        """**Validates: Requirements 16.1, 16.2**

        Dequeue always returns the highest-priority task among ready tasks.
        """
        config = SchedulerConfig(max_concurrency=10)
        scheduler = Scheduler(config)

        for entry in entries:
            entry.depends_on = []  # Ensure all are ready
            scheduler.enqueue(entry)

        dequeued_order: list[TaskQueueEntry] = []
        while True:
            task = scheduler.dequeue_next()
            if task is None:
                break
            dequeued_order.append(task)

        # Verify strict priority ordering
        for i in range(len(dequeued_order) - 1):
            curr = dequeued_order[i]
            nxt = dequeued_order[i + 1]

            assert curr.priority.value <= nxt.priority.value, (
                f"Task '{curr.task_id}' (priority={curr.priority.name}) "
                f"came before '{nxt.task_id}' (priority={nxt.priority.name}) "
                f"but has lower priority"
            )

    @given(
        priority=priority_st(),
        timestamps=st.lists(
            enqueued_at_st(),
            min_size=3,
            max_size=8,
            unique=True,
        ),
    )
    @settings(max_examples=300)
    def test_fifo_within_same_priority(
        self, priority: Priority, timestamps: list[float]
    ) -> None:
        """**Validates: Requirements 16.2**

        Within the same priority level, FIFO ordering (earliest enqueued_at
        first) is preserved.
        """
        config = SchedulerConfig(max_concurrency=10)
        scheduler = Scheduler(config)

        # Create entries all with same priority but different timestamps
        entries = [
            TaskQueueEntry(
                task_id=f"task-{i}",
                priority=priority,
                enqueued_at=ts,
                depends_on=[],
            )
            for i, ts in enumerate(timestamps)
        ]

        for entry in entries:
            scheduler.enqueue(entry)

        dequeued: list[TaskQueueEntry] = []
        while True:
            task = scheduler.dequeue_next()
            if task is None:
                break
            dequeued.append(task)

        # Verify FIFO: dequeued timestamps should be non-decreasing
        for i in range(len(dequeued) - 1):
            assert dequeued[i].enqueued_at <= dequeued[i + 1].enqueued_at, (
                f"FIFO violated: task '{dequeued[i].task_id}' "
                f"(enqueued_at={dequeued[i].enqueued_at}) dequeued before "
                f"'{dequeued[i+1].task_id}' (enqueued_at={dequeued[i+1].enqueued_at})"
            )

    @given(
        running_priority=non_critical_priority_st(),
        num_running=st.integers(min_value=1, max_value=5),
    )
    @settings(max_examples=200)
    def test_preemption_targets_lowest_priority_running(
        self, running_priority: Priority, num_running: int
    ) -> None:
        """**Validates: Requirements 16.3**

        When a critical task arrives at max concurrency, exactly the
        lowest-priority running task is preempted.
        """
        max_conc = num_running
        config = SchedulerConfig(max_concurrency=max_conc)
        scheduler = Scheduler(config)

        # Fill up running slots with non-critical tasks
        for i in range(num_running):
            entry = TaskQueueEntry(
                task_id=f"running-{i}",
                priority=running_priority,
                enqueued_at=1000.0 + i,
                depends_on=[],
            )
            scheduler.enqueue(entry)
            scheduler.dequeue_next()

        # All slots filled
        assert scheduler.get_running_count() == max_conc

        # Preempt for a critical task
        preempted_id = scheduler.preempt_for_critical("critical-task")

        assert preempted_id is not None, (
            "Preemption should succeed when non-critical tasks are running"
        )
        # After preemption, running count decreased by 1
        assert scheduler.get_running_count() == max_conc - 1

        # The preempted task should be back in the queue as preempted
        queue = scheduler.get_queue()
        preempted_in_queue = [t for t in queue if t.task_id == preempted_id]
        assert len(preempted_in_queue) == 1, (
            f"Preempted task '{preempted_id}' should be in queue"
        )
        assert preempted_in_queue[0].status == "preempted", (
            f"Preempted task should have status 'preempted', "
            f"got '{preempted_in_queue[0].status}'"
        )

    @given(data=st.data())
    @settings(max_examples=200)
    def test_preemption_selects_lowest_among_mixed_priorities(
        self, data: st.DataObject
    ) -> None:
        """**Validates: Requirements 16.3**

        When running tasks have mixed priorities, preemption selects the
        task with the lowest priority (highest numeric value).
        """
        max_conc = 3
        config = SchedulerConfig(max_concurrency=max_conc)
        scheduler = Scheduler(config)

        # Create tasks with distinct priorities (all non-critical)
        priorities_to_use = [Priority.HIGH, Priority.NORMAL, Priority.LOW]
        for i, prio in enumerate(priorities_to_use):
            entry = TaskQueueEntry(
                task_id=f"task-{prio.name.lower()}",
                priority=prio,
                enqueued_at=1000.0 + i,
                depends_on=[],
            )
            scheduler.enqueue(entry)
            scheduler.dequeue_next()

        assert scheduler.get_running_count() == 3

        preempted_id = scheduler.preempt_for_critical("critical-new")

        # The LOW priority task should be preempted (it has the lowest priority)
        assert preempted_id == "task-low", (
            f"Expected LOW priority task to be preempted, got '{preempted_id}'"
        )

    @given(num_critical=st.integers(min_value=1, max_value=5))
    @settings(max_examples=100)
    def test_preemption_fails_when_all_running_are_critical(
        self, num_critical: int
    ) -> None:
        """**Validates: Requirements 16.3**

        Preemption returns None when all running tasks are critical
        (cannot preempt a critical task for another critical task).
        """
        config = SchedulerConfig(max_concurrency=num_critical)
        scheduler = Scheduler(config)

        # Fill with critical tasks
        for i in range(num_critical):
            entry = TaskQueueEntry(
                task_id=f"critical-{i}",
                priority=Priority.CRITICAL,
                enqueued_at=1000.0 + i,
                depends_on=[],
            )
            scheduler.enqueue(entry)
            scheduler.dequeue_next()

        preempted_id = scheduler.preempt_for_critical("new-critical")

        assert preempted_id is None, (
            f"Should not preempt when all running tasks are critical, "
            f"but preempted '{preempted_id}'"
        )

    @given(entries=distinct_task_entries_st(min_size=2, max_size=6))
    @settings(max_examples=200)
    def test_preempted_task_retains_data(
        self, entries: list[TaskQueueEntry]
    ) -> None:
        """**Validates: Requirements 16.3**

        Preempted tasks are re-queued without data loss (priority,
        task_id, enqueued_at, repos are preserved).
        """
        config = SchedulerConfig(max_concurrency=1)
        scheduler = Scheduler(config)

        # Enqueue a non-critical task and run it
        entry = entries[0]
        entry.priority = Priority.LOW
        entry.depends_on = []
        scheduler.enqueue(entry)
        dequeued = scheduler.dequeue_next()
        assert dequeued is not None

        original_task_id = dequeued.task_id
        original_priority = dequeued.priority
        original_enqueued_at = dequeued.enqueued_at
        original_repos = dequeued.repos

        # Preempt
        preempted_id = scheduler.preempt_for_critical("critical-task")
        assert preempted_id == original_task_id

        # Find it in queue and check data preservation
        queue = scheduler.get_queue()
        requeued = [t for t in queue if t.task_id == original_task_id][0]

        assert requeued.priority == original_priority
        assert requeued.enqueued_at == original_enqueued_at
        assert requeued.repos == original_repos


# ---------------------------------------------------------------------------
# Property 17: Concurrency Limit Invariant
# Validates: Requirements 17.1
# ---------------------------------------------------------------------------


class TestConcurrencyLimitInvariant:
    """The number of concurrently executing tasks never exceeds the configured
    max_concurrency value.

    **Validates: Requirements 17.1**
    """

    @given(
        max_conc=max_concurrency_st(),
        entries=distinct_task_entries_st(min_size=2, max_size=10),
    )
    @settings(max_examples=300)
    def test_running_count_never_exceeds_max(
        self, max_conc: int, entries: list[TaskQueueEntry]
    ) -> None:
        """**Validates: Requirements 17.1**

        For any sequence of enqueue/dequeue operations, the number of
        simultaneously running tasks never exceeds max_concurrency.
        """
        config = SchedulerConfig(max_concurrency=max_conc)
        scheduler = Scheduler(config)

        for entry in entries:
            entry.depends_on = []
            scheduler.enqueue(entry)

        # Try to dequeue all of them
        dequeued_count = 0
        while True:
            task = scheduler.dequeue_next()
            if task is None:
                break
            dequeued_count += 1
            # After each dequeue, check invariant
            assert scheduler.get_running_count() <= max_conc, (
                f"Running count {scheduler.get_running_count()} exceeds "
                f"max_concurrency {max_conc} after dequeuing {dequeued_count} tasks"
            )

        # Final check
        assert scheduler.get_running_count() <= max_conc
        assert dequeued_count <= max_conc, (
            f"Dequeued {dequeued_count} tasks but max_concurrency is {max_conc}"
        )

    @given(
        max_conc=max_concurrency_st(),
        extra_tasks=st.integers(min_value=1, max_value=10),
    )
    @settings(max_examples=200)
    def test_dequeue_blocks_when_at_capacity(
        self, max_conc: int, extra_tasks: int
    ) -> None:
        """**Validates: Requirements 17.1**

        When running_count equals max_concurrency, dequeue_next returns None
        even if there are ready tasks in the queue.
        """
        num_tasks = max_conc + extra_tasks  # Ensure more tasks than slots
        config = SchedulerConfig(max_concurrency=max_conc)
        scheduler = Scheduler(config)

        # Enqueue more tasks than max concurrency
        for i in range(num_tasks):
            entry = TaskQueueEntry(
                task_id=f"task-{i}",
                priority=Priority.NORMAL,
                enqueued_at=1000.0 + i,
                depends_on=[],
            )
            scheduler.enqueue(entry)

        # Fill up concurrency slots
        for _ in range(max_conc):
            task = scheduler.dequeue_next()
            assert task is not None

        # Now at capacity — next dequeue should return None
        assert scheduler.dequeue_next() is None
        assert scheduler.get_running_count() == max_conc

    @given(
        max_conc=max_concurrency_st(),
        entries=distinct_task_entries_st(min_size=3, max_size=10),
    )
    @settings(max_examples=200)
    def test_concurrency_recovers_after_completion(
        self, max_conc: int, entries: list[TaskQueueEntry]
    ) -> None:
        """**Validates: Requirements 17.1**

        After a running task completes (resolve_dependencies), the
        scheduler can dispatch another task — but never exceeds the limit.
        """
        config = SchedulerConfig(max_concurrency=max_conc)
        scheduler = Scheduler(config)

        for entry in entries:
            entry.depends_on = []
            scheduler.enqueue(entry)

        # Fill to capacity
        dispatched: list[str] = []
        for _ in range(max_conc):
            task = scheduler.dequeue_next()
            if task is None:
                break
            dispatched.append(task.task_id)

        assert scheduler.get_running_count() <= max_conc

        # Complete one task
        if dispatched:
            scheduler.resolve_dependencies(dispatched[0])
            assert scheduler.get_running_count() <= max_conc

            # Should now be able to dequeue one more
            next_task = scheduler.dequeue_next()
            if next_task is not None:
                assert scheduler.get_running_count() <= max_conc

    @given(
        max_conc=max_concurrency_st(),
        num_tasks=st.integers(min_value=2, max_value=10),
    )
    @settings(max_examples=200)
    def test_preemption_does_not_violate_concurrency(
        self, max_conc: int, num_tasks: int
    ) -> None:
        """**Validates: Requirements 17.1**

        Even with preemption, running count never exceeds max_concurrency.
        """
        actual_tasks = min(num_tasks, max_conc)
        config = SchedulerConfig(max_concurrency=max_conc)
        scheduler = Scheduler(config)

        # Fill with non-critical tasks
        for i in range(actual_tasks):
            entry = TaskQueueEntry(
                task_id=f"task-{i}",
                priority=Priority.LOW,
                enqueued_at=1000.0 + i,
                depends_on=[],
            )
            scheduler.enqueue(entry)
            scheduler.dequeue_next()

        # Preempt
        scheduler.preempt_for_critical("critical-1")

        # Running count should now be one less
        assert scheduler.get_running_count() <= max_conc

        # Enqueue and dequeue the critical task
        critical_entry = TaskQueueEntry(
            task_id="critical-1",
            priority=Priority.CRITICAL,
            enqueued_at=2000.0,
            depends_on=[],
        )
        scheduler.enqueue(critical_entry)
        scheduler.dequeue_next()

        assert scheduler.get_running_count() <= max_conc


# ---------------------------------------------------------------------------
# Property 18: Dependency Enforcement
# Validates: Requirements 18.1, 18.2, 18.3
# ---------------------------------------------------------------------------


class TestDependencyEnforcement:
    """A task with unfulfilled dependencies never starts. Dependencies
    completing unblock dependent tasks. Failed dependencies cascade failure.

    **Validates: Requirements 18.1, 18.2, 18.3**
    """

    @given(
        dep_task_id=task_id_st(),
        dependent_task_id=task_id_st(),
    )
    @settings(max_examples=300)
    def test_task_with_unresolved_dependency_is_blocked(
        self, dep_task_id: str, dependent_task_id: str
    ) -> None:
        """**Validates: Requirements 18.1**

        A task with depends_on that are not yet completed is placed in
        'blocked' status and cannot be dequeued.
        """
        assume(dep_task_id != dependent_task_id)

        config = SchedulerConfig(max_concurrency=10)
        scheduler = Scheduler(config)

        # Enqueue dependent task (its dependency is not yet completed)
        entry = TaskQueueEntry(
            task_id=dependent_task_id,
            priority=Priority.CRITICAL,
            enqueued_at=1000.0,
            depends_on=[dep_task_id],
        )
        scheduler.enqueue(entry)

        # Task should be blocked
        queue = scheduler.get_queue()
        assert len(queue) == 1
        assert queue[0].status == "blocked", (
            f"Task with unresolved dependency should be 'blocked', "
            f"got '{queue[0].status}'"
        )

        # Cannot dequeue a blocked task
        result = scheduler.dequeue_next()
        assert result is None, (
            "Should not dequeue a task with unresolved dependencies"
        )

    @given(
        dep_task_id=task_id_st(),
        dependent_task_id=task_id_st(),
    )
    @settings(max_examples=300)
    def test_dependency_completion_unblocks_task(
        self, dep_task_id: str, dependent_task_id: str
    ) -> None:
        """**Validates: Requirements 18.2**

        When a dependency task completes, tasks that depend on it are
        unblocked and become ready for scheduling.
        """
        assume(dep_task_id != dependent_task_id)

        config = SchedulerConfig(max_concurrency=10)
        scheduler = Scheduler(config)

        # Enqueue the dependency task and the dependent task
        dep_entry = TaskQueueEntry(
            task_id=dep_task_id,
            priority=Priority.HIGH,
            enqueued_at=1000.0,
            depends_on=[],
        )
        scheduler.enqueue(dep_entry)

        dependent_entry = TaskQueueEntry(
            task_id=dependent_task_id,
            priority=Priority.NORMAL,
            enqueued_at=1001.0,
            depends_on=[dep_task_id],
        )
        scheduler.enqueue(dependent_entry)

        # Dependent should be blocked
        queue = scheduler.get_queue()
        blocked = [t for t in queue if t.task_id == dependent_task_id]
        assert blocked[0].status == "blocked"

        # Run the dependency to completion
        scheduler.dequeue_next()  # dequeues dep_entry (it's higher priority & ready)
        unblocked = scheduler.resolve_dependencies(dep_task_id)

        # The dependent task should now be unblocked
        assert dependent_task_id in unblocked, (
            f"Task '{dependent_task_id}' should be unblocked after "
            f"'{dep_task_id}' completes. Unblocked: {unblocked}"
        )

        # Dependent should now be dequeue-able
        task = scheduler.dequeue_next()
        assert task is not None
        assert task.task_id == dependent_task_id

    @given(data=st.data())
    @settings(max_examples=200)
    def test_failed_dependency_cascades_to_all_dependents(
        self, data: st.DataObject
    ) -> None:
        """**Validates: Requirements 18.3**

        If a dependency task fails, all dependent tasks transition to
        'dependency_failed' status.
        """
        num_dependents = data.draw(st.integers(min_value=1, max_value=5))
        dep_task_id = data.draw(task_id_st())
        dependent_ids = [
            f"dependent-{i}" for i in range(num_dependents)
        ]
        # Ensure dep_task_id is not in dependent_ids
        assume(dep_task_id not in dependent_ids)

        config = SchedulerConfig(max_concurrency=10)
        scheduler = Scheduler(config)

        # Enqueue the dependency task
        dep_entry = TaskQueueEntry(
            task_id=dep_task_id,
            priority=Priority.HIGH,
            enqueued_at=1000.0,
            depends_on=[],
        )
        scheduler.enqueue(dep_entry)
        scheduler.dequeue_next()  # Start it running

        # Enqueue dependent tasks
        for i, tid in enumerate(dependent_ids):
            entry = TaskQueueEntry(
                task_id=tid,
                priority=Priority.NORMAL,
                enqueued_at=1001.0 + i,
                depends_on=[dep_task_id],
            )
            scheduler.enqueue(entry)

        # Fail the dependency
        cascade_failed = scheduler.mark_dependency_failed(dep_task_id)

        # All dependents should be in cascade_failed
        for tid in dependent_ids:
            assert tid in cascade_failed, (
                f"Task '{tid}' depends on failed '{dep_task_id}' "
                f"but was not cascade-failed. Cascade: {cascade_failed}"
            )

        # Verify status in queue
        queue = scheduler.get_queue()
        for entry in queue:
            if entry.task_id in dependent_ids:
                assert entry.status == "dependency_failed", (
                    f"Task '{entry.task_id}' should have status "
                    f"'dependency_failed', got '{entry.status}'"
                )

    @given(data=st.data())
    @settings(max_examples=200)
    def test_transitive_dependency_failure_cascades(
        self, data: st.DataObject
    ) -> None:
        """**Validates: Requirements 18.3**

        Dependency failure cascades transitively: if A depends on B
        and B depends on C, failing C should cascade-fail both B and A.
        """
        task_c = "task-c"
        task_b = "task-b"
        task_a = "task-a"

        config = SchedulerConfig(max_concurrency=10)
        scheduler = Scheduler(config)

        # C has no dependencies
        scheduler.enqueue(TaskQueueEntry(
            task_id=task_c,
            priority=Priority.HIGH,
            enqueued_at=1000.0,
            depends_on=[],
        ))
        scheduler.dequeue_next()  # Start C

        # B depends on C
        scheduler.enqueue(TaskQueueEntry(
            task_id=task_b,
            priority=Priority.NORMAL,
            enqueued_at=1001.0,
            depends_on=[task_c],
        ))

        # A depends on B
        scheduler.enqueue(TaskQueueEntry(
            task_id=task_a,
            priority=Priority.NORMAL,
            enqueued_at=1002.0,
            depends_on=[task_b],
        ))

        # Fail C
        cascade_failed = scheduler.mark_dependency_failed(task_c)

        # B should be cascade-failed (depends on C)
        assert task_b in cascade_failed, (
            f"Task '{task_b}' depends on failed '{task_c}' but not cascade-failed"
        )
        # A should also be cascade-failed (depends on B which is now failed)
        assert task_a in cascade_failed, (
            f"Task '{task_a}' depends on '{task_b}' which depends on failed "
            f"'{task_c}', but not cascade-failed. Cascade: {cascade_failed}"
        )

    @given(
        dep_task_id=task_id_st(),
        dependent_task_id=task_id_st(),
    )
    @settings(max_examples=200)
    def test_blocked_task_cannot_be_dispatched(
        self, dep_task_id: str, dependent_task_id: str
    ) -> None:
        """**Validates: Requirements 18.1**

        A blocked task (with unresolved deps) is never returned by
        dequeue_next, even if it has the highest priority.
        """
        assume(dep_task_id != dependent_task_id)

        config = SchedulerConfig(max_concurrency=10)
        scheduler = Scheduler(config)

        # Enqueue a blocked critical task
        blocked_entry = TaskQueueEntry(
            task_id=dependent_task_id,
            priority=Priority.CRITICAL,
            enqueued_at=1000.0,
            depends_on=[dep_task_id],
        )
        scheduler.enqueue(blocked_entry)

        # Enqueue a ready low-priority task
        ready_entry = TaskQueueEntry(
            task_id=dep_task_id,
            priority=Priority.LOW,
            enqueued_at=1001.0,
            depends_on=[],
        )
        scheduler.enqueue(ready_entry)

        # Dequeue should return the ready low-priority task, not the blocked critical
        result = scheduler.dequeue_next()
        assert result is not None
        assert result.task_id == dep_task_id, (
            f"Expected ready task '{dep_task_id}' to be dequeued, "
            f"not blocked task. Got '{result.task_id}'"
        )

    @given(data=st.data())
    @settings(max_examples=200)
    def test_multiple_dependencies_all_must_complete(
        self, data: st.DataObject
    ) -> None:
        """**Validates: Requirements 18.1, 18.2**

        A task with multiple dependencies remains blocked until ALL
        dependencies complete, not just one.
        """
        num_deps = data.draw(st.integers(min_value=2, max_value=4))
        dep_ids = [f"dep-{i}" for i in range(num_deps)]
        dependent_id = "dependent-task"

        config = SchedulerConfig(max_concurrency=10)
        scheduler = Scheduler(config)

        # Enqueue all dependency tasks
        for i, dep_id in enumerate(dep_ids):
            scheduler.enqueue(TaskQueueEntry(
                task_id=dep_id,
                priority=Priority.HIGH,
                enqueued_at=1000.0 + i,
                depends_on=[],
            ))
            scheduler.dequeue_next()

        # Enqueue the dependent task
        scheduler.enqueue(TaskQueueEntry(
            task_id=dependent_id,
            priority=Priority.CRITICAL,
            enqueued_at=2000.0,
            depends_on=dep_ids,
        ))

        # Complete all but the last dependency
        for dep_id in dep_ids[:-1]:
            unblocked = scheduler.resolve_dependencies(dep_id)
            # Should not unblock yet
            assert dependent_id not in unblocked, (
                f"Task should remain blocked until ALL deps complete. "
                f"Completed so far: {dep_ids[:dep_ids.index(dep_id)+1]}, "
                f"remaining: {dep_ids[dep_ids.index(dep_id)+1:]}"
            )

        # Complete the last dependency
        unblocked = scheduler.resolve_dependencies(dep_ids[-1])
        assert dependent_id in unblocked, (
            f"Task should be unblocked after ALL dependencies complete"
        )


# ---------------------------------------------------------------------------
# Property 20: Timeout Enforcement
# Validates: Requirements 20.1, 20.2, 20.4
# ---------------------------------------------------------------------------


class TestTimeoutEnforcement:
    """Tasks exceeding their timeout are identified. Phase timeout at 50%
    triggers a warning.

    **Validates: Requirements 20.1, 20.2, 20.4**
    """

    @given(
        timeout_secs=st.integers(min_value=60, max_value=14400),
        elapsed_extra=st.floats(
            min_value=1.0, max_value=5000.0,
            allow_nan=False, allow_infinity=False,
        ),
    )
    @settings(max_examples=300)
    def test_task_exceeding_timeout_is_detected(
        self, timeout_secs: int, elapsed_extra: float
    ) -> None:
        """**Validates: Requirements 20.1**

        When elapsed wall-clock time exceeds the configured timeout,
        check_timeout reports timed_out=True with reason 'task_timeout'.
        """
        config = SchedulerConfig(default_timeout_seconds=7200)
        scheduler = Scheduler(config)

        # Enqueue a task with custom timeout
        entry = TaskQueueEntry(
            task_id="test-task",
            priority=Priority.NORMAL,
            enqueued_at=1000.0,
            depends_on=[],
            timeout_seconds=timeout_secs,
        )
        scheduler.enqueue(entry)
        scheduler.dequeue_next()  # Mark as running

        # started_at is in the past such that elapsed exceeds timeout
        started_at = 1000.0
        # current time will be simulated via time.time() inside check_timeout
        # The implementation uses time.time() internally, so we need to
        # test with actual elapsed time or use the started_at field
        # Let's set started_at far enough in the past
        import unittest.mock
        fake_now = started_at + timeout_secs + elapsed_extra
        with unittest.mock.patch("vikram_orchestrator.scheduler.time.time", return_value=fake_now):
            timed_out, reason = scheduler.check_timeout(
                task_id="test-task",
                started_at=started_at,
            )

        assert timed_out is True, (
            f"Task elapsed {timeout_secs + elapsed_extra}s exceeds "
            f"timeout {timeout_secs}s but timed_out is False"
        )
        assert reason == "task_timeout", (
            f"Expected reason 'task_timeout', got '{reason}'"
        )

    @given(
        timeout_secs=st.integers(min_value=100, max_value=14400),
        elapsed_fraction=st.floats(
            min_value=0.0, max_value=0.49, allow_nan=False, allow_infinity=False
        ),
    )
    @settings(max_examples=300)
    def test_task_within_timeout_is_ok(
        self, timeout_secs: int, elapsed_fraction: float
    ) -> None:
        """**Validates: Requirements 20.1**

        When elapsed time is well within the timeout, check_timeout reports
        timed_out=False with reason ''.
        """
        config = SchedulerConfig(default_timeout_seconds=7200)
        scheduler = Scheduler(config)

        entry = TaskQueueEntry(
            task_id="test-task",
            priority=Priority.NORMAL,
            enqueued_at=1000.0,
            depends_on=[],
            timeout_seconds=timeout_secs,
        )
        scheduler.enqueue(entry)
        scheduler.dequeue_next()

        started_at = 1000.0
        fake_now = started_at + (timeout_secs * elapsed_fraction)

        import unittest.mock
        with unittest.mock.patch("vikram_orchestrator.scheduler.time.time", return_value=fake_now):
            timed_out, reason = scheduler.check_timeout(
                task_id="test-task",
                started_at=started_at,
            )

        assert timed_out is False, (
            f"Task elapsed {timeout_secs * elapsed_fraction}s is within "
            f"timeout {timeout_secs}s but timed_out is True"
        )
        assert reason == "", (
            f"Expected empty reason, got '{reason}'"
        )

    @given(
        timeout_secs=st.integers(min_value=100, max_value=14400),
        phase_fraction=st.floats(
            min_value=0.51, max_value=0.99, allow_nan=False, allow_infinity=False
        ),
    )
    @settings(max_examples=300)
    def test_phase_timeout_warning_at_50_percent(
        self, timeout_secs: int, phase_fraction: float
    ) -> None:
        """**Validates: Requirements 20.4**

        When a single work phase consumes more than 50% of the total task
        time limit, a phase_timeout_warning is emitted.
        """
        config = SchedulerConfig(
            default_timeout_seconds=7200,
            phase_timeout_pct=0.5,
        )
        scheduler = Scheduler(config)

        entry = TaskQueueEntry(
            task_id="test-task",
            priority=Priority.NORMAL,
            enqueued_at=1000.0,
            depends_on=[],
            timeout_seconds=timeout_secs,
        )
        scheduler.enqueue(entry)
        scheduler.dequeue_next()

        started_at = 1000.0
        phase_started = started_at
        # Phase elapsed = total elapsed (since phase_started == started_at)
        # Phase elapsed > 50% of timeout, but total < 100% (no full timeout)
        total_elapsed = timeout_secs * phase_fraction
        fake_now = started_at + total_elapsed

        import unittest.mock
        with unittest.mock.patch("vikram_orchestrator.scheduler.time.time", return_value=fake_now):
            timed_out, reason = scheduler.check_timeout(
                task_id="test-task",
                started_at=started_at,
                current_phase_started=phase_started,
            )

        # Should not be a full timeout (since fraction < 1.0)
        # But phase_elapsed > 50% threshold should trigger warning
        if total_elapsed >= timeout_secs:
            # If somehow at or above timeout, task_timeout takes precedence
            assert timed_out is True
            assert reason == "task_timeout"
        else:
            assert timed_out is True, (
                f"Phase elapsed {total_elapsed}s > phase budget "
                f"{timeout_secs * 0.5}s, expected timed_out=True"
            )
            assert reason == "phase_timeout_warning", (
                f"Phase elapsed {total_elapsed}s > phase budget "
                f"{timeout_secs * 0.5}s, expected 'phase_timeout_warning', "
                f"got '{reason}'"
            )

    @given(
        timeout_secs=st.integers(min_value=100, max_value=14400),
        phase_fraction=st.floats(
            min_value=0.01, max_value=0.49, allow_nan=False, allow_infinity=False
        ),
    )
    @settings(max_examples=200)
    def test_no_phase_warning_when_under_50_percent(
        self, timeout_secs: int, phase_fraction: float
    ) -> None:
        """**Validates: Requirements 20.4**

        When a phase has consumed less than 50% of the total time limit,
        no phase warning is emitted.
        """
        config = SchedulerConfig(
            default_timeout_seconds=7200,
            phase_timeout_pct=0.5,
        )
        scheduler = Scheduler(config)

        entry = TaskQueueEntry(
            task_id="test-task",
            priority=Priority.NORMAL,
            enqueued_at=1000.0,
            depends_on=[],
            timeout_seconds=timeout_secs,
        )
        scheduler.enqueue(entry)
        scheduler.dequeue_next()

        started_at = 1000.0
        phase_started = started_at
        fake_now = started_at + (timeout_secs * phase_fraction)

        import unittest.mock
        with unittest.mock.patch("vikram_orchestrator.scheduler.time.time", return_value=fake_now):
            timed_out, reason = scheduler.check_timeout(
                task_id="test-task",
                started_at=started_at,
                current_phase_started=phase_started,
            )

        assert timed_out is False, (
            f"Phase only consumed {phase_fraction * 100:.1f}% of timeout, "
            f"should not trigger any timeout"
        )
        assert reason == "", (
            f"Phase only consumed {phase_fraction * 100:.1f}% of timeout, "
            f"expected empty reason, got '{reason}'"
        )

    @given(
        timeout_secs=st.integers(min_value=100, max_value=14400),
        phase_offset_fraction=st.floats(
            min_value=0.1, max_value=0.4, allow_nan=False, allow_infinity=False
        ),
    )
    @settings(max_examples=200)
    def test_task_timeout_takes_precedence_over_phase_warning(
        self, timeout_secs: int, phase_offset_fraction: float
    ) -> None:
        """**Validates: Requirements 20.1, 20.2**

        When both task timeout and phase warning conditions are true,
        the task timeout takes precedence.
        """
        config = SchedulerConfig(
            default_timeout_seconds=7200,
            phase_timeout_pct=0.5,
        )
        scheduler = Scheduler(config)

        entry = TaskQueueEntry(
            task_id="test-task",
            priority=Priority.NORMAL,
            enqueued_at=1000.0,
            depends_on=[],
            timeout_seconds=timeout_secs,
        )
        scheduler.enqueue(entry)
        scheduler.dequeue_next()

        started_at = 1000.0
        # Phase started sometime after task started
        phase_started = started_at + (timeout_secs * phase_offset_fraction)
        # Current time exceeds task timeout
        fake_now = started_at + timeout_secs + 1.0

        import unittest.mock
        with unittest.mock.patch("vikram_orchestrator.scheduler.time.time", return_value=fake_now):
            timed_out, reason = scheduler.check_timeout(
                task_id="test-task",
                started_at=started_at,
                current_phase_started=phase_started,
            )

        assert timed_out is True, (
            "Task timeout should take precedence"
        )
        assert reason == "task_timeout", (
            f"Expected 'task_timeout' to take precedence, got '{reason}'"
        )
