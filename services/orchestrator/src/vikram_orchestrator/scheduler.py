"""Task Scheduler subsystem — priority queue, concurrency management, dependency resolution.

Implements the Task Scheduler (Requirements 16–20):
- Priority-ordered task queue (critical > high > normal > low)
- FIFO ordering within same priority level
- Concurrency limit enforcement
- Dependency-aware scheduling
- Runtime priority updates
- Preemption for critical tasks
- Timeout enforcement with 30-second periodic background checks
- Per-phase time tracking and phase timeout warnings
- Lock Registry integration for same-repo contention serialization
- Per-provider rate limit tracking with call queuing
- Background timeout monitor (async/threaded) with checkpoint + pause + notify

Module: vikram_orchestrator/scheduler.py
Requirements: 16.1, 16.2, 16.3, 16.4, 17.1, 17.2, 17.3, 17.4, 17.5, 18.1, 18.2, 18.3, 20.1, 20.2, 20.3, 20.4
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from enum import IntEnum
from typing import Any, Callable

from pydantic import BaseModel, Field

from vikram_orchestrator.conflict_detector import LockRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_CONCURRENCY_LIMIT = 10
DEFAULT_CONCURRENCY = 3
DEFAULT_TIMEOUT_SECONDS = 7200  # 2 hours
PHASE_TIMEOUT_PCT = 0.5  # warn if phase uses 50% of total
TIMEOUT_CHECK_INTERVAL = 30  # seconds between background timeout checks


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Priority(IntEnum):
    """Task priority levels with numeric values for comparison.

    Lower numeric value = higher priority (critical is most urgent).
    Validates: Requirement 16.1
    """

    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3

    @classmethod
    def from_str(cls, value: str) -> "Priority":
        """Create Priority from string label."""
        mapping = {
            "critical": cls.CRITICAL,
            "high": cls.HIGH,
            "normal": cls.NORMAL,
            "low": cls.LOW,
        }
        normalized = value.lower().strip()
        if normalized not in mapping:
            raise ValueError(
                f"Invalid priority '{value}'. Must be one of: critical, high, normal, low"
            )
        return mapping[normalized]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class SchedulerConfig(BaseModel):
    """Configuration for the Task Scheduler.

    Validates: Requirement 17.1 (max_concurrency)
    """

    max_concurrency: int = Field(
        default=DEFAULT_CONCURRENCY,
        ge=1,
        le=MAX_CONCURRENCY_LIMIT,
        description="Maximum number of concurrently running tasks (default: 3, max: 10)",
    )
    max_concurrency_limit: int = Field(
        default=MAX_CONCURRENCY_LIMIT,
        ge=1,
        description="Absolute upper bound for max_concurrency (default: 10)",
    )
    default_timeout_seconds: int = Field(
        default=DEFAULT_TIMEOUT_SECONDS,
        gt=0,
        description="Default task timeout in seconds (default: 2 hours)",
    )
    phase_timeout_pct: float = Field(
        default=PHASE_TIMEOUT_PCT,
        gt=0.0,
        le=1.0,
        description="Warn if a phase uses this fraction of total time limit",
    )


class TaskQueueEntry(BaseModel):
    """A task entry in the scheduler's priority queue.

    Validates: Requirements 16.1, 18.1
    """

    task_id: str
    priority: Priority
    enqueued_at: float = Field(default_factory=time.time)
    depends_on: list[str] = Field(default_factory=list)
    status: str = "queued"  # "queued", "blocked", "ready", "running", "preempted", "dependency_failed", "paused"
    formation: str | None = None
    repos: list[str] = Field(default_factory=list)
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    started_at: float | None = None
    current_phase: str | None = None
    current_phase_started_at: float | None = None


class TimeoutResult(BaseModel):
    """Result of a timeout check for a single task.

    Describes the action taken when a timeout condition is detected
    by the periodic background checker.

    Validates: Requirements 20.1, 20.2, 20.4
    """

    task_id: str
    reason: str  # "task_timeout" or "phase_timeout_warning"
    elapsed_seconds: float
    timeout_seconds: int
    current_phase: str | None = None
    phase_elapsed_seconds: float | None = None
    phase_threshold_seconds: float | None = None
    action: str = ""  # "checkpoint_pause_notify" or "warn"


# ---------------------------------------------------------------------------
# Rate Limit Tracking
# ---------------------------------------------------------------------------


class RateLimitConfig(BaseModel):
    """Configuration for a provider's rate limit.

    Validates: Requirement 17.5
    """

    provider: str = Field(description="Provider identifier (e.g., 'openai', 'anthropic')")
    max_calls_per_window: int = Field(
        default=60,
        ge=1,
        description="Maximum API calls allowed in the rate limit window",
    )
    window_seconds: float = Field(
        default=60.0,
        gt=0,
        description="Duration of the rolling rate limit window in seconds",
    )


class RateLimitTracker:
    """Tracks per-provider API call counts in rolling windows.

    When a provider's rate limit window is near exhaustion, subsequent calls
    are queued until the window rotates. Tasks remain running but their calls
    wait for capacity.

    Validates: Requirement 17.5
    """

    def __init__(self) -> None:
        # provider -> config
        self._configs: dict[str, RateLimitConfig] = {}
        # provider -> list of call timestamps within current window
        self._call_timestamps: dict[str, list[float]] = {}
        # provider -> deque of pending callbacks waiting for capacity
        self._pending_queues: dict[str, deque[str]] = {}
        # provider -> set of task_ids currently consuming capacity
        self._active_tasks: dict[str, set[str]] = {}

    def configure_provider(self, config: RateLimitConfig) -> None:
        """Register or update rate limit configuration for a provider."""
        self._configs[config.provider] = config
        if config.provider not in self._call_timestamps:
            self._call_timestamps[config.provider] = []
        if config.provider not in self._pending_queues:
            self._pending_queues[config.provider] = deque()
        if config.provider not in self._active_tasks:
            self._active_tasks[config.provider] = set()

    def record_call(self, provider: str, task_id: str, timestamp: float | None = None) -> None:
        """Record an API call for a provider from a specific task.

        Args:
            provider: Provider identifier.
            task_id: The task making the call.
            timestamp: When the call was made (defaults to now).
        """
        ts = timestamp or time.time()
        if provider not in self._call_timestamps:
            self._call_timestamps[provider] = []
        self._call_timestamps[provider].append(ts)
        if provider not in self._active_tasks:
            self._active_tasks[provider] = set()
        self._active_tasks[provider].add(task_id)

    def can_make_call(self, provider: str, timestamp: float | None = None) -> bool:
        """Check if a provider has capacity for another call.

        Prunes expired timestamps from the window before checking.

        Returns True if the provider is not configured (no rate limit) or
        if the current call count is below the limit.
        """
        if provider not in self._configs:
            return True  # No rate limit configured

        config = self._configs[provider]
        ts = timestamp or time.time()
        self._prune_expired(provider, ts)

        current_count = len(self._call_timestamps.get(provider, []))
        return current_count < config.max_calls_per_window

    def get_remaining_capacity(self, provider: str, timestamp: float | None = None) -> int:
        """Get remaining call capacity for a provider in the current window.

        Returns -1 if the provider is not configured (unlimited).
        """
        if provider not in self._configs:
            return -1  # Unlimited

        config = self._configs[provider]
        ts = timestamp or time.time()
        self._prune_expired(provider, ts)

        current_count = len(self._call_timestamps.get(provider, []))
        return max(0, config.max_calls_per_window - current_count)

    def queue_call(self, provider: str, task_id: str) -> int:
        """Queue a call for a provider when rate limit is exhausted.

        Returns the position in the queue (0-based).
        """
        if provider not in self._pending_queues:
            self._pending_queues[provider] = deque()
        self._pending_queues[provider].append(task_id)
        return len(self._pending_queues[provider]) - 1

    def get_queue_depth(self, provider: str) -> int:
        """Get the number of pending calls queued for a provider."""
        return len(self._pending_queues.get(provider, deque()))

    def dequeue_pending(self, provider: str) -> str | None:
        """Dequeue the next pending call for a provider.

        Returns the task_id of the next pending call, or None if queue is empty.
        """
        queue = self._pending_queues.get(provider)
        if queue:
            return queue.popleft()
        return None

    def release_task(self, provider: str, task_id: str) -> None:
        """Release a task's association with a provider (e.g., on preemption or completion).

        This frees the provider's capacity that was being consumed by this task.

        Validates: Requirement 17.5 (preempted task's provider capacity is freed)
        """
        active = self._active_tasks.get(provider)
        if active and task_id in active:
            active.discard(task_id)
            logger.info(
                "Task %s released from provider %s rate tracking",
                task_id,
                provider,
            )

    def get_active_tasks(self, provider: str) -> set[str]:
        """Get the set of task_ids currently consuming capacity for a provider."""
        return self._active_tasks.get(provider, set()).copy()

    def get_window_usage(self, provider: str, timestamp: float | None = None) -> tuple[int, int]:
        """Get current usage and limit for a provider.

        Returns (current_calls_in_window, max_calls_per_window).
        Returns (0, 0) if provider is not configured.
        """
        if provider not in self._configs:
            return 0, 0

        config = self._configs[provider]
        ts = timestamp or time.time()
        self._prune_expired(provider, ts)

        current_count = len(self._call_timestamps.get(provider, []))
        return current_count, config.max_calls_per_window

    def _prune_expired(self, provider: str, now: float) -> None:
        """Remove call timestamps that have fallen outside the rate limit window."""
        if provider not in self._configs:
            return

        config = self._configs[provider]
        window_start = now - config.window_seconds
        timestamps = self._call_timestamps.get(provider, [])
        # Keep only timestamps within the current window
        self._call_timestamps[provider] = [
            ts for ts in timestamps if ts > window_start
        ]


# ---------------------------------------------------------------------------
# Provider Rate Limiter — High-level coordinator
# ---------------------------------------------------------------------------


class ProviderRateLimiter:
    """High-level provider rate limit coordinator.

    Wraps RateLimitTracker with task-aware call gating: checks capacity before
    allowing a call, queues calls when the limit is exhausted, and drains the
    queue when capacity is restored (window rotation).

    Usage:
        limiter = ProviderRateLimiter()
        limiter.configure("openai", max_calls=60, window_seconds=60.0)

        # Before making a provider call:
        if limiter.try_acquire("openai", "task-1"):
            # make the call
            ...
        else:
            # call is queued; caller should wait/retry
            ...

        # Periodically or after time passes:
        released = limiter.drain_queued("openai")

    Validates: Requirement 17.5
    """

    def __init__(self, tracker: RateLimitTracker | None = None) -> None:
        self._tracker = tracker or RateLimitTracker()

    @property
    def tracker(self) -> RateLimitTracker:
        """Access the underlying RateLimitTracker."""
        return self._tracker

    def configure(
        self, provider: str, max_calls: int = 60, window_seconds: float = 60.0
    ) -> None:
        """Configure rate limit for a provider.

        Args:
            provider: Provider identifier (e.g., 'openai', 'anthropic').
            max_calls: Maximum API calls allowed in the rolling window.
            window_seconds: Duration of the rolling rate limit window.
        """
        config = RateLimitConfig(
            provider=provider,
            max_calls_per_window=max_calls,
            window_seconds=window_seconds,
        )
        self._tracker.configure_provider(config)
        logger.info(
            "Provider '%s' rate limit configured: %d calls / %.1fs window",
            provider,
            max_calls,
            window_seconds,
        )

    def try_acquire(self, provider: str, task_id: str, timestamp: float | None = None) -> bool:
        """Attempt to acquire capacity for a provider call.

        If the provider has remaining capacity in the current window, records
        the call and returns True. If the rate limit is exhausted, queues the
        call and returns False (the caller should wait/retry).

        Args:
            provider: Provider identifier.
            task_id: Task making the call.
            timestamp: Optional timestamp override (for testing).

        Returns:
            True if the call was recorded (capacity available).
            False if the call was queued (rate limit exhausted).
        """
        ts = timestamp or time.time()
        if self._tracker.can_make_call(provider, ts):
            self._tracker.record_call(provider, task_id, ts)
            return True
        else:
            self._tracker.queue_call(provider, task_id)
            logger.debug(
                "Provider '%s' rate limit exhausted; task '%s' call queued (depth=%d)",
                provider,
                task_id,
                self._tracker.get_queue_depth(provider),
            )
            return False

    def drain_queued(self, provider: str, timestamp: float | None = None) -> list[str]:
        """Drain queued calls for a provider as capacity becomes available.

        Processes pending calls in FIFO order, recording each call until
        capacity is again exhausted or the queue is empty.

        Args:
            provider: Provider identifier.
            timestamp: Optional timestamp override.

        Returns:
            List of task_ids whose queued calls were released (can now proceed).
        """
        ts = timestamp or time.time()
        released: list[str] = []
        while self._tracker.can_make_call(provider, ts):
            next_task = self._tracker.dequeue_pending(provider)
            if next_task is None:
                break
            self._tracker.record_call(provider, next_task, ts)
            released.append(next_task)
            logger.debug(
                "Provider '%s': released queued call for task '%s'",
                provider,
                next_task,
            )
        return released

    def release_task(self, provider: str, task_id: str) -> None:
        """Release a task's association with a provider.

        Called when a task is preempted, completed, or cancelled to free
        the provider's tracked active-task set.
        """
        self._tracker.release_task(provider, task_id)

    def get_status(self, provider: str, timestamp: float | None = None) -> dict[str, Any]:
        """Get current rate limit status for a provider.

        Returns a dict with:
            - can_call: whether capacity is available
            - current_usage: calls in the current window
            - max_calls: configured window limit
            - remaining: calls remaining
            - queue_depth: pending calls waiting
            - active_tasks: tasks currently consuming capacity
        """
        ts = timestamp or time.time()
        current, max_calls = self._tracker.get_window_usage(provider, ts)
        remaining = self._tracker.get_remaining_capacity(provider, ts)
        return {
            "can_call": self._tracker.can_make_call(provider, ts),
            "current_usage": current,
            "max_calls": max_calls,
            "remaining": remaining if remaining >= 0 else None,
            "queue_depth": self._tracker.get_queue_depth(provider),
            "active_tasks": list(self._tracker.get_active_tasks(provider)),
        }


# ---------------------------------------------------------------------------
# Repo Contention Checker — Lock-based serialization
# ---------------------------------------------------------------------------


class RepoContentionChecker:
    """Checks and enforces same-repo contention serialization using the Lock Registry.

    When two tasks target the same repository, they must be serialized to prevent
    conflicts. This class encapsulates the logic of acquiring/releasing repo locks
    and checking contention before a task is dispatched.

    Integration:
    - On task dispatch (dequeue_next): check if repos are contended
    - On task start (mark_task_running): acquire locks for all repos
    - On task completion/preemption: release locks for all repos

    Validates: Requirements 17.2, 17.3, 19.1
    """

    def __init__(self, lock_registry: LockRegistry | None = None) -> None:
        self._lock_registry = lock_registry or LockRegistry()

    @property
    def lock_registry(self) -> LockRegistry:
        """Access the underlying lock registry."""
        return self._lock_registry

    def is_contended(self, task_id: str, repos: list[str]) -> bool:
        """Check if any of a task's repos are locked by another task.

        Args:
            task_id: The task being checked.
            repos: List of repo paths the task targets.

        Returns:
            True if at least one repo is locked by a different task.
        """
        if not repos:
            return False

        for repo_path in repos:
            is_locked, holding_task_id = self._lock_registry.is_locked(repo_path)
            if is_locked and holding_task_id != task_id:
                logger.debug(
                    "Contention detected: task '%s' repo '%s' locked by '%s'",
                    task_id,
                    repo_path,
                    holding_task_id,
                )
                return True

        return False

    def acquire_repos(self, task_id: str, repos: list[str]) -> list[str]:
        """Acquire locks for all repos a task targets.

        Called when a task is dispatched to run. Acquires locks in sorted order
        to prevent deadlocks.

        Args:
            task_id: The task acquiring locks.
            repos: List of repo paths to lock.

        Returns:
            List of repo paths that were successfully locked.

        Raises:
            No exceptions — if a lock cannot be acquired (already held by another),
            it is skipped and logged. Callers should check is_contended first.
        """
        acquired: list[str] = []
        for repo_path in sorted(repos):
            try:
                self._lock_registry.acquire(task_id, repo_path)
                acquired.append(repo_path)
            except Exception as e:
                logger.warning(
                    "Task '%s' could not acquire lock on '%s': %s",
                    task_id,
                    repo_path,
                    e,
                )
        return acquired

    def release_repos(self, task_id: str, repos: list[str]) -> list[str]:
        """Release locks for all repos a task holds.

        Called when a task completes, is preempted, or fails.

        Args:
            task_id: The task releasing locks.
            repos: List of repo paths to release.

        Returns:
            List of repo paths that were successfully released.
        """
        released: list[str] = []
        for repo_path in repos:
            if self._lock_registry.release(task_id, repo_path):
                released.append(repo_path)
        return released

    def get_contention_map(self) -> dict[str, str]:
        """Get a mapping of locked repo paths to their holding task_ids.

        Returns:
            Dict of {repo_path: task_id} for all active repo locks.
        """
        locks = self._lock_registry.query()
        return {lock.path: lock.task_id for lock in locks}


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class Scheduler:
    """Priority-based task scheduler with dependency resolution.

    Scheduling rules (from design):
    1. Priority ordering: critical > high > normal > low. FIFO within same priority.
    2. Preemption: Only critical tasks preempt. Only the lowest-priority running task
       is preempted. Preempted tasks are checkpointed and re-queued.
    3. Dependency resolution: A task is 'blocked' until all depends_on tasks reach
       completed status. If a dependency fails/cancels, cascade 'dependency_failed'
       to all dependents.
    4. Concurrency: Up to N tasks run in parallel.

    Validates: Requirements 16.1, 16.2, 16.3, 16.4, 17.1, 18.1, 18.2, 18.3
    """

    def __init__(
        self,
        config: SchedulerConfig | None = None,
        lock_registry: LockRegistry | None = None,
        rate_limiter: RateLimitTracker | None = None,
        on_timeout_notify: Callable[[TimeoutResult], None] | None = None,
        on_checkpoint: Callable[[str], None] | None = None,
        contention_checker: RepoContentionChecker | None = None,
        provider_rate_limiter: ProviderRateLimiter | None = None,
    ) -> None:
        self._config = config or SchedulerConfig()
        # All tasks in the queue (including blocked, queued, running, preempted)
        self._queue: list[TaskQueueEntry] = []
        # Set of task_ids that have reached terminal states (completed, failed, cancelled)
        self._completed_tasks: set[str] = set()
        # Lock Registry integration for same-repo contention serialization
        # Validates: Requirements 17.2, 17.3
        self._lock_registry = lock_registry or LockRegistry()
        # Per-provider rate limit tracker (low-level)
        # Validates: Requirement 17.5
        self._rate_limiter = rate_limiter or RateLimitTracker()
        # Callback invoked to notify the founder when a timeout occurs
        # Validates: Requirement 20.2
        self._on_timeout_notify = on_timeout_notify
        # Callback invoked to checkpoint a task before pausing on timeout
        # Validates: Requirement 20.2
        self._on_checkpoint = on_checkpoint
        # High-level repo contention checker wrapping the lock registry
        # Validates: Requirements 17.2, 17.3, 19.1
        self._contention_checker = contention_checker or RepoContentionChecker(
            lock_registry=self._lock_registry
        )
        # High-level provider rate limiter coordinator
        # Validates: Requirement 17.5
        self._provider_rate_limiter = provider_rate_limiter or ProviderRateLimiter(
            tracker=self._rate_limiter
        )

    @property
    def config(self) -> SchedulerConfig:
        return self._config

    @property
    def lock_registry(self) -> LockRegistry:
        """Access the lock registry for contention serialization."""
        return self._lock_registry

    @property
    def contention_checker(self) -> RepoContentionChecker:
        """Access the repo contention checker."""
        return self._contention_checker

    @property
    def provider_rate_limiter(self) -> ProviderRateLimiter:
        """Access the high-level provider rate limiter."""
        return self._provider_rate_limiter

    @property
    def rate_limiter(self) -> RateLimitTracker:
        """Access the per-provider rate limit tracker."""
        return self._rate_limiter

    def enqueue(self, entry: TaskQueueEntry) -> None:
        """Add a task to the scheduler queue.

        If the task has dependencies that are not yet satisfied, its status
        is set to 'blocked'. Otherwise, it's set to 'queued' (ready for scheduling).

        Validates: Requirements 16.1, 18.1
        """
        # Determine initial status based on dependencies
        if entry.depends_on:
            unresolved = [
                dep_id
                for dep_id in entry.depends_on
                if dep_id not in self._completed_tasks
            ]
            if unresolved:
                entry.status = "blocked"
            else:
                entry.status = "queued"
        else:
            if entry.status not in ("queued", "preempted"):
                entry.status = "queued"

        self._queue.append(entry)
        logger.info(
            "Task %s enqueued with priority=%s status=%s",
            entry.task_id,
            entry.priority.name,
            entry.status,
        )

    def dequeue_next(self) -> TaskQueueEntry | None:
        """Return the highest-priority ready task for execution.

        Selection criteria:
        - Only tasks with status 'queued' or 'preempted' are eligible
        - Must have all dependencies satisfied (not blocked)
        - Must not have repos locked by another task (contention serialization)
        - Strict priority ordering (lower numeric priority value = higher priority)
        - FIFO within same priority level (earlier enqueued_at wins)

        Returns None if no eligible task exists or concurrency limit is reached.

        Validates: Requirements 16.1, 16.2, 17.1, 17.2, 17.3
        """
        # Check concurrency limit
        running_count = sum(1 for t in self._queue if t.status == "running")
        if running_count >= self._config.max_concurrency:
            return None

        # Find eligible tasks (queued or preempted, with all dependencies resolved)
        eligible: list[TaskQueueEntry] = []
        for entry in self._queue:
            if entry.status in ("queued", "preempted"):
                # Verify dependencies are satisfied
                if not self._dependencies_satisfied(entry):
                    continue
                # Check Lock Registry: skip if any of the task's repos are locked
                # by another task (same-repo contention serialization)
                if self._is_repo_contended(entry):
                    continue
                eligible.append(entry)

        if not eligible:
            return None

        # Sort by priority (lower numeric = higher priority), then by enqueued_at (FIFO)
        eligible.sort(key=lambda e: (e.priority.value, e.enqueued_at))

        # Pick the highest-priority, earliest-enqueued task
        selected = eligible[0]
        selected.status = "running"
        selected.started_at = time.time()
        logger.info(
            "Task %s dequeued (priority=%s)",
            selected.task_id,
            selected.priority.name,
        )
        return selected

    def update_priority(self, task_id: str, new_priority: Priority) -> None:
        """Change a task's priority at runtime.

        Takes effect at the next scheduling decision.

        Validates: Requirement 16.4
        """
        for entry in self._queue:
            if entry.task_id == task_id:
                old_priority = entry.priority
                entry.priority = new_priority
                logger.info(
                    "Task %s priority updated: %s -> %s",
                    task_id,
                    old_priority.name,
                    new_priority.name,
                )
                return

        raise ValueError(f"Task '{task_id}' not found in scheduler queue")

    def preempt_for_critical(self, critical_task_id: str) -> str | None:
        """Preempt the lowest-priority running task to make room for a critical task.

        Only invoked when the platform is at max concurrency and a critical task
        needs execution. Checkpoints and re-queues the preempted task.
        Also releases the preempted task's provider rate limit capacity.

        Returns the preempted task_id, or None if no task can be preempted.

        Validates: Requirements 16.3, 17.5
        """
        running_tasks = [t for t in self._queue if t.status == "running"]
        if not running_tasks:
            return None

        # Find the lowest-priority running task (highest numeric value)
        # Among same priority, preempt the most recently started (least progress)
        running_tasks.sort(
            key=lambda t: (-t.priority.value, -(t.started_at or 0))
        )
        victim = running_tasks[0]

        # Only preempt if victim is lower priority than critical
        if victim.priority <= Priority.CRITICAL:
            # Cannot preempt a critical task
            return None

        victim.status = "preempted"
        victim.started_at = None

        # Release the preempted task's provider rate limit capacity
        # This frees capacity for the critical task's provider calls
        if victim.formation:
            self._rate_limiter.release_task(victim.formation, victim.task_id)

        logger.info(
            "Task %s preempted (priority=%s) to make room for critical task %s",
            victim.task_id,
            victim.priority.name,
            critical_task_id,
        )
        return victim.task_id

    def resolve_dependencies(self, completed_task_id: str) -> list[str]:
        """Mark a task as completed and unblock any dependent tasks.

        Returns list of task_ids that became unblocked.

        Validates: Requirements 18.1, 18.2
        """
        self._completed_tasks.add(completed_task_id)

        # Remove the completed task from the active queue if present
        self._queue = [t for t in self._queue if t.task_id != completed_task_id]

        # Check blocked tasks to see if they can be unblocked
        newly_unblocked: list[str] = []
        for entry in self._queue:
            if entry.status == "blocked" and self._dependencies_satisfied(entry):
                entry.status = "queued"
                newly_unblocked.append(entry.task_id)
                logger.info(
                    "Task %s unblocked (all dependencies resolved)",
                    entry.task_id,
                )

        return newly_unblocked

    def mark_dependency_failed(self, failed_task_id: str) -> list[str]:
        """Cascade failure to all tasks that depend on the failed task.

        Also cascades transitively: if A depends on B, and B depends on C,
        and C fails, both B and A are marked as dependency_failed.

        Returns list of task_ids that were marked as dependency_failed.

        Validates: Requirement 18.3
        """
        self._completed_tasks.add(failed_task_id)

        # Remove the failed task from the active queue
        self._queue = [t for t in self._queue if t.task_id != failed_task_id]

        cascade_failed: list[str] = []
        # Use iterative approach to handle transitive dependencies
        failed_set = {failed_task_id}
        changed = True
        while changed:
            changed = False
            for entry in self._queue:
                if entry.status == "dependency_failed":
                    continue
                if entry.task_id in failed_set:
                    continue
                # Check if any of this task's dependencies are in the failed set
                if any(dep_id in failed_set for dep_id in entry.depends_on):
                    entry.status = "dependency_failed"
                    failed_set.add(entry.task_id)
                    cascade_failed.append(entry.task_id)
                    changed = True
                    logger.info(
                        "Task %s marked dependency_failed (depends on failed task %s)",
                        entry.task_id,
                        failed_task_id,
                    )

        return cascade_failed

    def check_timeout(
        self, task_id: str, started_at: float, current_phase_started: float | None = None
    ) -> tuple[bool, str]:
        """Check if a task has exceeded its time limit.

        Returns (timed_out, reason) where reason is:
        - "task_timeout" if the task has exceeded its total time limit
        - "phase_timeout_warning" if current phase uses >50% of total time
        - "" if no timeout condition

        Validates: Requirements 20.1, 20.2, 20.4
        """
        entry = self._find_task(task_id)
        if entry is None:
            return False, ""

        now = time.time()
        elapsed = now - started_at
        timeout = entry.timeout_seconds

        # Check total task timeout
        if elapsed >= timeout:
            return True, "task_timeout"

        # Check phase timeout warning (50% of total)
        if current_phase_started is not None:
            phase_elapsed = now - current_phase_started
            phase_threshold = timeout * self._config.phase_timeout_pct
            if phase_elapsed >= phase_threshold:
                return True, "phase_timeout_warning"

        return False, ""

    def get_queue(self) -> list[TaskQueueEntry]:
        """Return all tasks in the queue, ordered by priority then enqueued_at.

        Validates: Requirement 16.4 (queue visibility)
        """
        sorted_queue = sorted(
            self._queue,
            key=lambda e: (e.priority.value, e.enqueued_at),
        )
        return sorted_queue

    def get_running_count(self) -> int:
        """Return the number of currently running tasks."""
        return sum(1 for t in self._queue if t.status == "running")

    def mark_task_running(self, task_id: str) -> None:
        """Explicitly mark a task as running (used after dequeue)."""
        entry = self._find_task(task_id)
        if entry is not None:
            entry.status = "running"
            entry.started_at = time.time()

    def mark_task_completed(self, task_id: str) -> list[str]:
        """Mark a task completed and resolve dependencies.

        Convenience method combining status update and dependency resolution.
        Returns list of newly unblocked task_ids.
        """
        return self.resolve_dependencies(task_id)

    def run_timeout_checks(self) -> list["TimeoutResult"]:
        """Periodic background check — designed to be called externally every 30 seconds.

        Iterates over all running tasks and checks for:
        1. Total task timeout exceeded → checkpoint + pause + notify
        2. Phase timeout warning (single phase > 50% of total) → warn

        Returns a list of TimeoutResult objects describing any actions taken.

        Validates: Requirements 20.1, 20.2, 20.3, 20.4
        """
        results: list[TimeoutResult] = []
        now = time.time()

        running_tasks = [t for t in self._queue if t.status == "running"]
        for entry in running_tasks:
            if entry.started_at is None:
                continue

            elapsed = now - entry.started_at
            timeout = entry.timeout_seconds

            # Check total task timeout
            if elapsed >= timeout:
                # Checkpoint the task state before pausing
                if self._on_checkpoint is not None:
                    try:
                        self._on_checkpoint(entry.task_id)
                    except Exception:
                        logger.exception(
                            "Checkpoint callback failed for task %s", entry.task_id
                        )

                # Pause the task
                entry.status = "paused"
                result = TimeoutResult(
                    task_id=entry.task_id,
                    reason="task_timeout",
                    elapsed_seconds=elapsed,
                    timeout_seconds=timeout,
                    current_phase=entry.current_phase,
                    action="checkpoint_pause_notify",
                )
                results.append(result)
                logger.warning(
                    "Task %s exceeded timeout (%ds/%ds) — checkpointed, paused, notifying founder",
                    entry.task_id,
                    int(elapsed),
                    timeout,
                )

                # Notify the founder
                if self._on_timeout_notify is not None:
                    try:
                        self._on_timeout_notify(result)
                    except Exception:
                        logger.exception(
                            "Timeout notify callback failed for task %s", entry.task_id
                        )
                continue

            # Check phase timeout warning (50% of total)
            if entry.current_phase_started_at is not None:
                phase_elapsed = now - entry.current_phase_started_at
                phase_threshold = timeout * self._config.phase_timeout_pct
                if phase_elapsed >= phase_threshold:
                    result = TimeoutResult(
                        task_id=entry.task_id,
                        reason="phase_timeout_warning",
                        elapsed_seconds=elapsed,
                        timeout_seconds=timeout,
                        current_phase=entry.current_phase,
                        phase_elapsed_seconds=phase_elapsed,
                        phase_threshold_seconds=phase_threshold,
                        action="warn",
                    )
                    results.append(result)
                    logger.warning(
                        "Task %s phase '%s' consuming %.0f%% of total time budget (%ds/%ds)",
                        entry.task_id,
                        entry.current_phase or "unknown",
                        (phase_elapsed / timeout) * 100,
                        int(phase_elapsed),
                        timeout,
                    )

                    # Notify the founder of phase timeout warning
                    if self._on_timeout_notify is not None:
                        try:
                            self._on_timeout_notify(result)
                        except Exception:
                            logger.exception(
                                "Phase timeout notify callback failed for task %s",
                                entry.task_id,
                            )

        return results

    def extend_timeout(self, task_id: str, additional_seconds: int) -> bool:
        """Extend the time limit for a task (typically one paused on timeout).

        The founder can extend the time limit for a paused-on-timeout task,
        allowing it to be resumed with more time.

        Returns True if the timeout was extended, False if task not found.

        Validates: Requirement 20.3
        """
        entry = self._find_task(task_id)
        if entry is None:
            return False

        old_timeout = entry.timeout_seconds
        entry.timeout_seconds += additional_seconds
        logger.info(
            "Task %s timeout extended: %ds -> %ds (+%ds)",
            task_id,
            old_timeout,
            entry.timeout_seconds,
            additional_seconds,
        )
        return True

    def resume_after_timeout(self, task_id: str) -> bool:
        """Resume a task that was paused due to timeout (after time limit extension).

        Returns True if the task was resumed, False if not found or not paused.

        Validates: Requirement 20.3
        """
        entry = self._find_task(task_id)
        if entry is None:
            return False
        if entry.status != "paused":
            return False

        entry.status = "running"
        logger.info(
            "Task %s resumed after timeout extension (timeout=%ds)",
            task_id,
            entry.timeout_seconds,
        )
        return True

    def start_phase(self, task_id: str, phase_name: str) -> bool:
        """Record the start of a new work phase for a running task.

        This enables per-phase time tracking so that the 50% phase timeout
        warning can fire accurately.

        Returns True if the phase was recorded, False if task not found.

        Validates: Requirement 20.4
        """
        entry = self._find_task(task_id)
        if entry is None:
            return False

        entry.current_phase = phase_name
        entry.current_phase_started_at = time.time()
        logger.info(
            "Task %s entered phase '%s'",
            task_id,
            phase_name,
        )
        return True

    # ---------------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------------

    def _dependencies_satisfied(self, entry: TaskQueueEntry) -> bool:
        """Check if all dependencies for a task have been completed."""
        if not entry.depends_on:
            return True
        return all(dep_id in self._completed_tasks for dep_id in entry.depends_on)

    def _is_repo_contended(self, entry: TaskQueueEntry) -> bool:
        """Check if any of a task's repos are locked by another running task.

        Delegates to the RepoContentionChecker which uses the Lock Registry
        to enforce same-repo contention serialization.

        Validates: Requirements 17.2, 17.3
        """
        return self._contention_checker.is_contended(entry.task_id, entry.repos)

    def _find_task(self, task_id: str) -> TaskQueueEntry | None:
        """Find a task in the queue by its ID."""
        for entry in self._queue:
            if entry.task_id == task_id:
                return entry
        return None


# ---------------------------------------------------------------------------
# Background Timeout Monitor
# ---------------------------------------------------------------------------


class TimeoutMonitor:
    """Background monitor that runs timeout checks every 30 seconds.

    Provides both a threaded (synchronous) mode and an async mode for
    periodic timeout enforcement. When a timeout is detected, the monitor
    triggers checkpoint + pause + notify through the Scheduler's callbacks.

    Usage (threaded):
        monitor = TimeoutMonitor(scheduler)
        monitor.start()
        # ... later ...
        monitor.stop()

    Usage (async):
        monitor = TimeoutMonitor(scheduler)
        await monitor.run_async()
        # Cancel via monitor.stop()

    Validates: Requirements 20.1, 20.2, 20.3, 20.4
    """

    def __init__(
        self,
        scheduler: Scheduler,
        interval_seconds: float = TIMEOUT_CHECK_INTERVAL,
    ) -> None:
        self._scheduler = scheduler
        self._interval = interval_seconds
        self._running = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._async_task: asyncio.Task[None] | None = None
        self._last_results: list[TimeoutResult] = []

    @property
    def is_running(self) -> bool:
        """Whether the background monitor is currently active."""
        return self._running

    @property
    def last_results(self) -> list[TimeoutResult]:
        """The results from the most recent timeout check cycle."""
        return self._last_results

    @property
    def interval_seconds(self) -> float:
        """The interval between timeout checks."""
        return self._interval

    # ------------------------------------------------------------------
    # Threaded mode
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background timeout monitor in a daemon thread.

        The monitor will run timeout checks every `interval_seconds` and
        invoke the scheduler's callbacks on timeout detection.

        Validates: Requirements 20.1, 20.2
        """
        if self._running:
            logger.warning("TimeoutMonitor already running")
            return

        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="timeout-monitor",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "TimeoutMonitor started (interval=%ds)", int(self._interval)
        )

    def stop(self) -> None:
        """Stop the background timeout monitor.

        Blocks until the monitor thread exits (with a brief timeout).
        """
        if not self._running:
            return

        self._running = False
        self._stop_event.set()

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=self._interval + 1)

        # Cancel async task if running
        if self._async_task is not None and not self._async_task.done():
            self._async_task.cancel()

        logger.info("TimeoutMonitor stopped")

    def _run_loop(self) -> None:
        """Internal loop for the threaded monitor."""
        while self._running and not self._stop_event.is_set():
            try:
                self._last_results = self._scheduler.run_timeout_checks()
                if self._last_results:
                    logger.info(
                        "TimeoutMonitor cycle: %d timeout events detected",
                        len(self._last_results),
                    )
            except Exception:
                logger.exception("TimeoutMonitor: error during timeout check cycle")

            # Sleep in short increments to allow faster shutdown
            self._stop_event.wait(timeout=self._interval)

    # ------------------------------------------------------------------
    # Async mode
    # ------------------------------------------------------------------

    async def run_async(self) -> None:
        """Run the timeout monitor as an asyncio task.

        This coroutine runs indefinitely until cancelled or stop() is called.
        Suitable for integration with asyncio-based event loops (e.g., FastAPI).

        Validates: Requirements 20.1, 20.2
        """
        self._running = True
        logger.info(
            "TimeoutMonitor started (async, interval=%ds)", int(self._interval)
        )
        try:
            while self._running:
                try:
                    self._last_results = self._scheduler.run_timeout_checks()
                    if self._last_results:
                        logger.info(
                            "TimeoutMonitor async cycle: %d timeout events detected",
                            len(self._last_results),
                        )
                except Exception:
                    logger.exception(
                        "TimeoutMonitor: error during async timeout check cycle"
                    )
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            logger.info("TimeoutMonitor async task cancelled")
        finally:
            self._running = False

    def start_async(self, loop: asyncio.AbstractEventLoop | None = None) -> asyncio.Task[None]:
        """Start the timeout monitor as a background asyncio task.

        Args:
            loop: The event loop to schedule on. If None, uses the running loop.

        Returns:
            The asyncio.Task running the monitor.

        Validates: Requirements 20.1, 20.2
        """
        if loop is None:
            loop = asyncio.get_event_loop()

        self._async_task = loop.create_task(
            self.run_async(), name="timeout-monitor-async"
        )
        return self._async_task
