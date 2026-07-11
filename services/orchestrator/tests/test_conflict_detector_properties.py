"""Property-based tests for the Conflict Detector subsystem.

These tests define correctness properties for:
- File Lock Mutual Exclusion (Property 19)
- Conflict Probability Bounds and Monotonicity (Property 26)
- Reordering Constraint Preservation (Property 27)

Run with:
  cd services/orchestrator && .venv/bin/python -m pytest tests/test_conflict_detector_properties.py -v

Validates: Requirements 19.1, 19.2, 35.2, 36.4
"""

from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import given, settings, assume

from vikram_orchestrator.conflict_detector import (
    ConflictDetector,
    FileLock,
    FileOverlap,
    LockAcquireError,
    LockRegistry,
    ReorderProposal,
    conflict_probability,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Task IDs: short alphanumeric strings
def task_id_st() -> st.SearchStrategy[str]:
    """Generate plausible task identifiers."""
    return st.text(
        min_size=1,
        max_size=20,
        alphabet=st.characters(categories=("L", "N", "Pd")),
    )


def file_path_st() -> st.SearchStrategy[str]:
    """Generate plausible file paths."""
    segment = st.text(
        min_size=1,
        max_size=15,
        alphabet=st.characters(categories=("L", "N", "Pd")),
    )
    return st.lists(segment, min_size=1, max_size=5).map(lambda parts: "/".join(parts))


def distinct_task_ids_st(n: int = 2) -> st.SearchStrategy[list[str]]:
    """Generate a list of N distinct task IDs."""
    return st.lists(
        task_id_st(),
        min_size=n,
        max_size=n,
        unique=True,
    )


def historical_rate_st() -> st.SearchStrategy[float]:
    """Generate historical conflict rates in [0, 1]."""
    return st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)


def priority_st() -> st.SearchStrategy[int]:
    """Generate task priority values (lower = more important)."""
    return st.integers(min_value=1, max_value=100)


def task_queue_st(
    min_size: int = 2, max_size: int = 8
) -> st.SearchStrategy[list[str]]:
    """Generate a task queue with unique task IDs."""
    return st.lists(
        task_id_st(),
        min_size=min_size,
        max_size=max_size,
        unique=True,
    )


# ---------------------------------------------------------------------------
# Property 19: File Lock Mutual Exclusion
# Validates: Requirements 19.1, 19.2
# ---------------------------------------------------------------------------


class TestFileLockMutualExclusion:
    """Two tasks cannot hold locks on the same file simultaneously.
    Acquire must fail for an already-locked path."""

    @given(
        task_ids=distinct_task_ids_st(2),
        path=file_path_st(),
    )
    @settings(max_examples=200)
    def test_second_acquire_fails_on_locked_path(
        self, task_ids: list[str], path: str
    ) -> None:
        """**Validates: Requirements 19.1**

        When task A holds a lock on a file, task B cannot acquire the same lock.
        """
        registry = LockRegistry()
        task_a, task_b = task_ids[0], task_ids[1]

        # Task A acquires the lock successfully
        lock = registry.acquire(task_a, path)
        assert lock.task_id == task_a
        assert lock.path == path

        # Task B attempting to acquire the same path must fail
        try:
            registry.acquire(task_b, path)
            assert False, (
                f"Task '{task_b}' should not be able to acquire lock on '{path}' "
                f"already held by task '{task_a}'"
            )
        except LockAcquireError as e:
            assert e.path == path
            assert e.held_by == task_a

    @given(
        task_ids=distinct_task_ids_st(2),
        paths=st.lists(file_path_st(), min_size=1, max_size=10, unique=True),
    )
    @settings(max_examples=200)
    def test_no_simultaneous_locks_on_same_file(
        self, task_ids: list[str], paths: list[str]
    ) -> None:
        """**Validates: Requirements 19.1**

        For any file path, at most one task holds the lock at any time.
        After task A acquires locks on all paths, task B cannot acquire any of them.
        """
        registry = LockRegistry()
        task_a, task_b = task_ids[0], task_ids[1]

        # Task A acquires all paths
        for p in paths:
            registry.acquire(task_a, p)

        # Task B cannot acquire any of them
        for p in paths:
            try:
                registry.acquire(task_b, p)
                assert False, f"Task B acquired lock on '{p}' held by task A"
            except LockAcquireError:
                pass

    @given(
        task_ids=distinct_task_ids_st(2),
        path=file_path_st(),
    )
    @settings(max_examples=200)
    def test_release_allows_other_task_to_acquire(
        self, task_ids: list[str], path: str
    ) -> None:
        """**Validates: Requirements 19.1**

        After a lock is released, another task can acquire it.
        """
        registry = LockRegistry()
        task_a, task_b = task_ids[0], task_ids[1]

        # Task A acquires and then releases
        registry.acquire(task_a, path)
        released = registry.release(task_a, path)
        assert released is True

        # Task B can now acquire
        lock = registry.acquire(task_b, path)
        assert lock.task_id == task_b
        assert lock.path == path

    @given(
        task_id=task_id_st(),
        path=file_path_st(),
    )
    @settings(max_examples=100)
    def test_same_task_reacquire_is_idempotent(
        self, task_id: str, path: str
    ) -> None:
        """**Validates: Requirements 19.1**

        A task re-acquiring its own lock is idempotent (no error).
        """
        registry = LockRegistry()

        lock1 = registry.acquire(task_id, path)
        lock2 = registry.acquire(task_id, path)

        assert lock1.task_id == lock2.task_id
        assert lock1.path == lock2.path

    @given(
        task_ids=distinct_task_ids_st(2),
        paths_a=st.lists(file_path_st(), min_size=1, max_size=5, unique=True),
        paths_b=st.lists(file_path_st(), min_size=1, max_size=5, unique=True),
    )
    @settings(max_examples=200)
    def test_non_overlapping_paths_allow_concurrent_locks(
        self, task_ids: list[str], paths_a: list[str], paths_b: list[str]
    ) -> None:
        """**Validates: Requirements 19.2**

        Two tasks can hold locks on different files simultaneously without conflict.
        """
        registry = LockRegistry()
        task_a, task_b = task_ids[0], task_ids[1]

        # Make paths disjoint
        paths_b_filtered = [p for p in paths_b if p not in paths_a]
        assume(len(paths_b_filtered) > 0)

        # Task A locks its paths
        for p in paths_a:
            registry.acquire(task_a, p)

        # Task B locks non-overlapping paths — should succeed
        for p in paths_b_filtered:
            lock = registry.acquire(task_b, p)
            assert lock.task_id == task_b

    @given(
        task_ids=st.lists(task_id_st(), min_size=3, max_size=6, unique=True),
        path=file_path_st(),
    )
    @settings(max_examples=100)
    def test_mutual_exclusion_with_multiple_tasks(
        self, task_ids: list[str], path: str
    ) -> None:
        """**Validates: Requirements 19.1**

        With N tasks, only the first acquirer holds the lock. All others fail.
        """
        registry = LockRegistry()

        # First task acquires
        registry.acquire(task_ids[0], path)

        # All other tasks fail
        for other_task in task_ids[1:]:
            try:
                registry.acquire(other_task, path)
                assert False, f"Task '{other_task}' should not acquire lock held by '{task_ids[0]}'"
            except LockAcquireError as e:
                assert e.held_by == task_ids[0]


# ---------------------------------------------------------------------------
# Property 26: Conflict Probability Bounds and Monotonicity
# Validates: Requirements 35.2
# ---------------------------------------------------------------------------


class TestConflictProbabilityBoundsAndMonotonicity:
    """Probability is always in [0, 1]. More overlapping factors increase probability."""

    @given(
        same_functions=st.booleans(),
        within_proximity=st.booleans(),
        historical_rate=historical_rate_st(),
    )
    @settings(max_examples=300)
    def test_probability_always_in_unit_interval(
        self,
        same_functions: bool,
        within_proximity: bool,
        historical_rate: float,
    ) -> None:
        """**Validates: Requirements 35.2**

        The conflict probability is always in [0.0, 1.0] regardless of inputs.
        """
        prob = conflict_probability(
            file="any/file.py",
            task_a_targets={},
            task_b_targets={},
            same_functions=same_functions,
            changes_within_proximity=within_proximity,
            historical_rate=historical_rate,
        )

        assert 0.0 <= prob <= 1.0, (
            f"Probability {prob} outside [0, 1] with "
            f"same_functions={same_functions}, proximity={within_proximity}, "
            f"historical_rate={historical_rate}"
        )

    @given(historical_rate=historical_rate_st())
    @settings(max_examples=200)
    def test_more_overlap_factors_increase_probability(
        self, historical_rate: float
    ) -> None:
        """**Validates: Requirements 35.2**

        Adding more overlap factors (same_functions, proximity) monotonically
        increases or maintains the conflict probability.
        """
        # Base case: no extra factors
        p_base = conflict_probability(
            file="test.py",
            task_a_targets={},
            task_b_targets={},
            same_functions=False,
            changes_within_proximity=False,
            historical_rate=historical_rate,
        )

        # Adding same_functions factor
        p_functions = conflict_probability(
            file="test.py",
            task_a_targets={},
            task_b_targets={},
            same_functions=True,
            changes_within_proximity=False,
            historical_rate=historical_rate,
        )

        # Adding proximity factor
        p_proximity = conflict_probability(
            file="test.py",
            task_a_targets={},
            task_b_targets={},
            same_functions=False,
            changes_within_proximity=True,
            historical_rate=historical_rate,
        )

        # Adding both factors
        p_both = conflict_probability(
            file="test.py",
            task_a_targets={},
            task_b_targets={},
            same_functions=True,
            changes_within_proximity=True,
            historical_rate=historical_rate,
        )

        assert p_functions >= p_base, (
            f"Adding same_functions should not decrease probability: "
            f"base={p_base}, with_functions={p_functions}"
        )
        assert p_proximity >= p_base, (
            f"Adding proximity should not decrease probability: "
            f"base={p_base}, with_proximity={p_proximity}"
        )
        assert p_both >= p_functions, (
            f"Adding both factors should be >= functions alone: "
            f"functions={p_functions}, both={p_both}"
        )
        assert p_both >= p_proximity, (
            f"Adding both factors should be >= proximity alone: "
            f"proximity={p_proximity}, both={p_both}"
        )

    @given(
        rate_low=st.floats(min_value=0.0, max_value=0.49, allow_nan=False, allow_infinity=False),
        rate_high=st.floats(min_value=0.5, max_value=1.0, allow_nan=False, allow_infinity=False),
        same_functions=st.booleans(),
        within_proximity=st.booleans(),
    )
    @settings(max_examples=200)
    def test_higher_historical_rate_increases_probability(
        self,
        rate_low: float,
        rate_high: float,
        same_functions: bool,
        within_proximity: bool,
    ) -> None:
        """**Validates: Requirements 35.2**

        A higher historical conflict rate results in a higher or equal probability.
        """
        assume(rate_high > rate_low)

        p_low = conflict_probability(
            file="test.py",
            task_a_targets={},
            task_b_targets={},
            same_functions=same_functions,
            changes_within_proximity=within_proximity,
            historical_rate=rate_low,
        )

        p_high = conflict_probability(
            file="test.py",
            task_a_targets={},
            task_b_targets={},
            same_functions=same_functions,
            changes_within_proximity=within_proximity,
            historical_rate=rate_high,
        )

        assert p_high >= p_low, (
            f"Higher historical rate ({rate_high}) should not decrease probability: "
            f"p_low={p_low} (rate={rate_low}), p_high={p_high} (rate={rate_high})"
        )

    @given(historical_rate=historical_rate_st())
    @settings(max_examples=100)
    def test_base_probability_minimum_value(
        self, historical_rate: float
    ) -> None:
        """**Validates: Requirements 35.2**

        The minimum probability (no extra factors) is base * (0.7 + 0.3 * rate),
        which with base=0.3 and rate=0 gives 0.3 * 0.7 = 0.21.
        """
        p = conflict_probability(
            file="test.py",
            task_a_targets={},
            task_b_targets={},
            same_functions=False,
            changes_within_proximity=False,
            historical_rate=historical_rate,
        )

        # With no extra factors, base=0.3
        # adjusted = 0.3 * (0.7 + 0.3 * historical_rate)
        expected = 0.3 * (0.7 + 0.3 * historical_rate)
        assert abs(p - expected) < 1e-9, (
            f"Base probability should be {expected}, got {p} (rate={historical_rate})"
        )

    @given(historical_rate=historical_rate_st())
    @settings(max_examples=100)
    def test_all_factors_probability_formula(
        self, historical_rate: float
    ) -> None:
        """**Validates: Requirements 35.2**

        With all factors enabled, base=0.9, adjusted = 0.9 * (0.7 + 0.3 * rate),
        capped at 1.0.
        """
        p = conflict_probability(
            file="test.py",
            task_a_targets={},
            task_b_targets={},
            same_functions=True,
            changes_within_proximity=True,
            historical_rate=historical_rate,
        )

        expected = min(0.9 * (0.7 + 0.3 * historical_rate), 1.0)
        assert abs(p - expected) < 1e-9, (
            f"All-factors probability should be {expected}, got {p} (rate={historical_rate})"
        )


# ---------------------------------------------------------------------------
# Property 27: Reordering Constraint Preservation
# Validates: Requirements 36.4
# ---------------------------------------------------------------------------


class TestReorderingConstraintPreservation:
    """Any proposed reorder must preserve dependency constraints and priority ordering."""

    @given(data=st.data())
    @settings(max_examples=200)
    def test_reorder_preserves_dependencies(self, data: st.DataObject) -> None:
        """**Validates: Requirements 36.4**

        If task A depends on task B, then B must appear before A in any
        proposed reordering.
        """
        # Generate a task queue
        queue = data.draw(task_queue_st(min_size=3, max_size=6))
        n = len(queue)

        # Generate priorities for each task
        priorities = {task: data.draw(priority_st()) for task in queue}

        # Generate dependencies (ensuring no cycles by only allowing forward deps)
        deps: dict[str, list[str]] = {}
        for i, task in enumerate(queue):
            # Can only depend on tasks earlier in the original queue (avoids cycles)
            possible_deps = queue[:i]
            if possible_deps:
                dep_list = data.draw(
                    st.lists(
                        st.sampled_from(possible_deps),
                        min_size=0,
                        max_size=min(2, len(possible_deps)),
                        unique=True,
                    )
                )
                if dep_list:
                    deps[task] = dep_list

        # Generate some conflicts to trigger reordering
        conflicts = []
        if n >= 2:
            # Create a conflict between two tasks
            idx_a = data.draw(st.integers(min_value=0, max_value=n - 1))
            idx_b = data.draw(st.integers(min_value=0, max_value=n - 1))
            assume(idx_a != idx_b)
            conflicts.append(
                FileOverlap(
                    path="shared/file.py",
                    task_a_id=queue[idx_a],
                    task_b_id=queue[idx_b],
                    overlap_type="same_file",
                    conflict_probability=0.8,
                )
            )

        detector = ConflictDetector()
        proposal = detector.propose_reorder(queue, conflicts, priorities, deps)

        if proposal is not None:
            # Verify dependency constraints in the proposed order
            position = {task: idx for idx, task in enumerate(proposal.proposed_order)}

            for task_id in proposal.proposed_order:
                for dep_id in deps.get(task_id, []):
                    if dep_id in position:
                        assert position[dep_id] < position[task_id], (
                            f"Dependency violated: '{task_id}' depends on '{dep_id}', "
                            f"but '{dep_id}' is at position {position[dep_id]} and "
                            f"'{task_id}' is at position {position[task_id]} in proposed order "
                            f"{proposal.proposed_order}. Deps: {deps}"
                        )

    @given(data=st.data())
    @settings(max_examples=200)
    def test_reorder_preserves_priority_ordering(self, data: st.DataObject) -> None:
        """**Validates: Requirements 36.4**

        A higher-priority task (lower priority number) must not be placed
        behind a lower-priority task (higher priority number) unless forced
        by a dependency constraint.
        """
        queue = data.draw(task_queue_st(min_size=3, max_size=6))
        n = len(queue)

        # Generate distinct priorities
        priorities = {
            task: data.draw(priority_st()) for task in queue
        }

        # Generate dependencies (forward-only to avoid cycles)
        deps: dict[str, list[str]] = {}
        for i, task in enumerate(queue):
            possible_deps = queue[:i]
            if possible_deps:
                dep_list = data.draw(
                    st.lists(
                        st.sampled_from(possible_deps),
                        min_size=0,
                        max_size=min(2, len(possible_deps)),
                        unique=True,
                    )
                )
                if dep_list:
                    deps[task] = dep_list

        # Generate conflicts
        conflicts = []
        if n >= 2:
            idx_a = data.draw(st.integers(min_value=0, max_value=n - 1))
            idx_b = data.draw(st.integers(min_value=0, max_value=n - 1))
            assume(idx_a != idx_b)
            conflicts.append(
                FileOverlap(
                    path="conflict/file.py",
                    task_a_id=queue[idx_a],
                    task_b_id=queue[idx_b],
                    overlap_type="same_function",
                    conflict_probability=0.75,
                )
            )

        detector = ConflictDetector()
        proposal = detector.propose_reorder(queue, conflicts, priorities, deps)

        if proposal is not None:
            # Use the static verification method
            is_valid = ConflictDetector.verify_constraints(
                proposal.proposed_order, priorities, deps
            )
            assert is_valid, (
                f"Proposed reorder violates constraints. "
                f"Order: {proposal.proposed_order}, "
                f"Priorities: {priorities}, Deps: {deps}"
            )

    @given(data=st.data())
    @settings(max_examples=200)
    def test_reorder_contains_same_tasks(self, data: st.DataObject) -> None:
        """**Validates: Requirements 36.4**

        A reorder proposal must contain exactly the same set of tasks as the
        original queue (no tasks added or removed).
        """
        queue = data.draw(task_queue_st(min_size=2, max_size=6))
        n = len(queue)

        priorities = {task: data.draw(priority_st()) for task in queue}
        deps: dict[str, list[str]] = {}

        # Generate a conflict
        idx_a = data.draw(st.integers(min_value=0, max_value=n - 1))
        idx_b = data.draw(st.integers(min_value=0, max_value=n - 1))
        assume(idx_a != idx_b)

        conflicts = [
            FileOverlap(
                path="shared.py",
                task_a_id=queue[idx_a],
                task_b_id=queue[idx_b],
                overlap_type="same_file",
                conflict_probability=0.8,
            )
        ]

        detector = ConflictDetector()
        proposal = detector.propose_reorder(queue, conflicts, priorities, deps)

        if proposal is not None:
            assert set(proposal.proposed_order) == set(queue), (
                f"Proposed order {proposal.proposed_order} has different tasks "
                f"than original {queue}"
            )
            assert len(proposal.proposed_order) == len(queue), (
                f"Proposed order length {len(proposal.proposed_order)} != "
                f"original length {len(queue)}"
            )

    @given(data=st.data())
    @settings(max_examples=100)
    def test_verify_constraints_catches_dependency_violation(
        self, data: st.DataObject
    ) -> None:
        """**Validates: Requirements 36.4**

        verify_constraints correctly identifies when a dependency is violated.
        """
        # Generate 3 tasks with guaranteed distinct IDs
        queue = data.draw(
            st.lists(task_id_st(), min_size=3, max_size=3, unique=True)
        )
        # Use uniform priority so priority ordering doesn't interfere
        uniform_priority = data.draw(priority_st())
        priorities = {task: uniform_priority for task in queue}

        # Create a dependency: task at index 2 depends on task at index 0
        deps = {queue[2]: [queue[0]]}

        # Valid order: queue[0] before queue[2] (original order satisfies this)
        assert ConflictDetector.verify_constraints(queue, priorities, deps) is True

        # Invalid order: swap queue[0] and queue[2] so the dependency is violated
        invalid_order = list(queue)
        invalid_order[0], invalid_order[2] = invalid_order[2], invalid_order[0]

        is_valid = ConflictDetector.verify_constraints(invalid_order, priorities, deps)
        assert is_valid is False, (
            f"Swapped order {invalid_order} should violate dependency "
            f"'{queue[2]}' depends on '{queue[0]}'. "
            f"Priorities: {priorities}, Deps: {deps}"
        )

    @given(data=st.data())
    @settings(max_examples=100)
    def test_no_conflicts_returns_none(self, data: st.DataObject) -> None:
        """**Validates: Requirements 36.4**

        When there are no conflicts, propose_reorder returns None
        (no reordering needed).
        """
        queue = data.draw(task_queue_st(min_size=2, max_size=5))
        priorities = {task: data.draw(priority_st()) for task in queue}
        deps: dict[str, list[str]] = {}

        detector = ConflictDetector()
        proposal = detector.propose_reorder(queue, [], priorities, deps)

        assert proposal is None, (
            f"With no conflicts, propose_reorder should return None, "
            f"got {proposal}"
        )

    @given(data=st.data())
    @settings(max_examples=100)
    def test_reorder_original_order_preserved(self, data: st.DataObject) -> None:
        """**Validates: Requirements 36.4**

        The proposal's original_order field matches the input queue exactly.
        """
        queue = data.draw(task_queue_st(min_size=2, max_size=6))
        n = len(queue)

        priorities = {task: data.draw(priority_st()) for task in queue}
        deps: dict[str, list[str]] = {}

        idx_a = data.draw(st.integers(min_value=0, max_value=n - 1))
        idx_b = data.draw(st.integers(min_value=0, max_value=n - 1))
        assume(idx_a != idx_b)

        conflicts = [
            FileOverlap(
                path="file.py",
                task_a_id=queue[idx_a],
                task_b_id=queue[idx_b],
                overlap_type="same_file",
                conflict_probability=0.9,
            )
        ]

        detector = ConflictDetector()
        proposal = detector.propose_reorder(queue, conflicts, priorities, deps)

        if proposal is not None:
            assert proposal.original_order == queue, (
                f"original_order {proposal.original_order} != input queue {queue}"
            )
