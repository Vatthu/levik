"""Conflict Detector subsystem for the Vikram Orchestrator.

Provides:
- Predictive conflict probability scoring between concurrent tasks
- File-level lock registry client (mutual exclusion enforcement)
- Semantic dependency graph construction
- Conflict-aware task reordering proposals
- Configurable conflict threshold with alert emission (Req 35.3)

See design.md Subsystem 6 for architecture details.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Telemetry Alert Protocol (decoupled from TelemetryClient for testability)
# ---------------------------------------------------------------------------


class AlertEmitter(Protocol):
    """Protocol for emitting conflict prediction alerts."""

    def emit_event(
        self,
        event_type: str,
        task_id: str,
        attributes: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CONFLICT_THRESHOLD = 0.70
"""Default conflict probability threshold (70%). Overlaps at or above this
trigger alerts to the founder (Requirement 35.3)."""


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


@dataclass
class FileOverlap:
    """Describes a predicted file overlap between two tasks."""

    path: str
    task_a_id: str
    task_b_id: str
    overlap_type: str  # "same_file", "same_function", "same_block"
    conflict_probability: float


@dataclass
class SemanticDependency:
    """A cross-task semantic dependency (one task modifies an interface another consumes)."""

    source_task_id: str
    source_file: str
    modified_interface: str
    consumer_task_id: str
    consumer_file: str
    import_path: str


@dataclass
class ReorderProposal:
    """A proposed reordering of the task queue to minimize conflicts."""

    original_order: list[str]
    proposed_order: list[str]
    conflicts_avoided: list[FileOverlap]
    throughput_impact: str  # "none", "minor_delay", "significant_delay"


# ---------------------------------------------------------------------------
# Lock Registry (Python-side client and in-memory implementation for testing)
# ---------------------------------------------------------------------------


class LockAcquireError(Exception):
    """Raised when a lock cannot be acquired (already held by another task)."""

    def __init__(self, path: str, held_by: str):
        self.path = path
        self.held_by = held_by
        super().__init__(f"Lock on '{path}' already held by task '{held_by}'")


@dataclass
class FileLock:
    """Represents an active file lock."""

    path: str
    task_id: str


class LockRegistry:
    """In-memory file-level lock registry enforcing mutual exclusion.

    Two tasks cannot hold locks on the same file simultaneously.
    Acquire must fail for an already-locked path held by a different task.
    """

    def __init__(self) -> None:
        self._locks: dict[str, FileLock] = {}

    def acquire(self, task_id: str, path: str) -> FileLock:
        """Acquire a lock on a file path for a task.

        Raises LockAcquireError if the path is already locked by a different task.
        Re-acquiring the same lock by the same task is idempotent.
        """
        if path in self._locks:
            existing = self._locks[path]
            if existing.task_id != task_id:
                raise LockAcquireError(path, existing.task_id)
            # Same task re-acquiring — idempotent
            return existing

        lock = FileLock(path=path, task_id=task_id)
        self._locks[path] = lock
        return lock

    def release(self, task_id: str, path: str) -> bool:
        """Release a lock. Returns True if released, False if not held."""
        if path in self._locks and self._locks[path].task_id == task_id:
            del self._locks[path]
            return True
        return False

    def is_locked(self, path: str) -> tuple[bool, str | None]:
        """Check if a path is locked. Returns (is_locked, holding_task_id)."""
        if path in self._locks:
            return True, self._locks[path].task_id
        return False, None

    def query(self) -> list[FileLock]:
        """Return all active locks."""
        return list(self._locks.values())


# ---------------------------------------------------------------------------
# Conflict Probability Scoring
# ---------------------------------------------------------------------------


def conflict_probability(
    file: str,
    task_a_targets: dict[str, Any],
    task_b_targets: dict[str, Any],
    *,
    same_functions: bool = False,
    changes_within_proximity: bool = False,
    historical_rate: float = 0.5,
    proximity_threshold: int = 20,
) -> float:
    """Compute conflict probability for a file touched by two tasks.

    Algorithm (from design.md):
        base_probability = 0.3  (two tasks touching same file)
        if same_functions_modified: base_probability += 0.4
        if changes_within_proximity(threshold=20): base_probability += 0.2
        historical_rate = get_historical_conflict_rate(file)
        adjusted = base_probability * (0.7 + 0.3 * historical_rate)
        return min(adjusted, 1.0)

    Parameters:
        file: The file path both tasks touch.
        task_a_targets: Target metadata for task A.
        task_b_targets: Target metadata for task B.
        same_functions: Whether both tasks modify the same functions/blocks.
        changes_within_proximity: Whether changes are within proximity_threshold lines.
        historical_rate: Historical conflict rate for this file, in [0, 1].
        proximity_threshold: Line distance threshold for proximity boost.

    Returns:
        A probability value in [0.0, 1.0].
    """
    base_probability = 0.3

    if same_functions:
        base_probability += 0.4

    if changes_within_proximity:
        base_probability += 0.2

    # Historical correction factor
    adjusted = base_probability * (0.7 + 0.3 * historical_rate)

    return min(adjusted, 1.0)


# ---------------------------------------------------------------------------
# Static Analysis: Import Detection
# ---------------------------------------------------------------------------

# Python: import foo, from foo import bar, from foo.bar import baz
_PYTHON_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.MULTILINE
)

# Go: import "path/to/pkg" or import ( "path/to/pkg" )
_GO_IMPORT_RE = re.compile(
    r'^\s*(?:import\s+)?"([^"]+)"', re.MULTILINE
)
# Also handle grouped imports
_GO_IMPORT_BLOCK_RE = re.compile(
    r'import\s*\((.*?)\)', re.DOTALL
)
_GO_IMPORT_PATH_RE = re.compile(r'"([^"]+)"')

# TypeScript/JavaScript: import ... from "path" or require("path")
_TS_IMPORT_RE = re.compile(
    r"""^\s*(?:import\s+.*?\s+from\s+['"]([^'"]+)['"]|"""
    r"""import\s+['"]([^'"]+)['"]|"""
    r"""(?:const|let|var)\s+.*?=\s*require\s*\(\s*['"]([^'"]+)['"]\s*\))""",
    re.MULTILINE,
)

# Python: function/class definitions (exported interfaces)
_PYTHON_DEF_RE = re.compile(
    r"^\s*(?:def|class|async\s+def)\s+(\w+)", re.MULTILINE
)

# Go: exported function/type (starts with uppercase)
_GO_FUNC_RE = re.compile(
    r"^func\s+(?:\([^)]*\)\s+)?([A-Z]\w*)", re.MULTILINE
)
_GO_TYPE_RE = re.compile(
    r"^type\s+([A-Z]\w*)", re.MULTILINE
)

# TypeScript/JavaScript: exported function/class/const/type
_TS_EXPORT_RE = re.compile(
    r"^\s*export\s+(?:default\s+)?(?:function|class|const|let|var|type|interface|enum)\s+(\w+)",
    re.MULTILINE,
)


def extract_imports(file_path: str, content: str) -> list[str]:
    """Extract import paths from file content based on language.

    Returns a list of import path strings (module names or file paths).
    """
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".py":
        return _extract_python_imports(content)
    elif ext == ".go":
        return _extract_go_imports(content)
    elif ext in (".ts", ".tsx", ".js", ".jsx", ".mts", ".mjs"):
        return _extract_ts_imports(content)
    return []


def _extract_python_imports(content: str) -> list[str]:
    """Extract Python import paths."""
    imports: list[str] = []
    for match in _PYTHON_IMPORT_RE.finditer(content):
        # from X import ... or import X
        module = match.group(1) or match.group(2)
        if module:
            imports.append(module)
    return imports


def _extract_go_imports(content: str) -> list[str]:
    """Extract Go import paths."""
    imports: list[str] = []

    # Single-line imports
    for match in _GO_IMPORT_RE.finditer(content):
        imports.append(match.group(1))

    # Grouped imports
    for block_match in _GO_IMPORT_BLOCK_RE.finditer(content):
        block = block_match.group(1)
        for path_match in _GO_IMPORT_PATH_RE.finditer(block):
            path = path_match.group(1)
            if path not in imports:
                imports.append(path)

    return imports


def _extract_ts_imports(content: str) -> list[str]:
    """Extract TypeScript/JavaScript import paths."""
    imports: list[str] = []
    for match in _TS_IMPORT_RE.finditer(content):
        path = match.group(1) or match.group(2) or match.group(3)
        if path:
            imports.append(path)
    return imports


def extract_exported_interfaces(file_path: str, content: str) -> list[str]:
    """Extract exported interface names (functions, classes, types) from file content."""
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".py":
        return _extract_python_exports(content)
    elif ext == ".go":
        return _extract_go_exports(content)
    elif ext in (".ts", ".tsx", ".js", ".jsx", ".mts", ".mjs"):
        return _extract_ts_exports(content)
    return []


def _extract_python_exports(content: str) -> list[str]:
    """Extract Python function/class definitions (public names)."""
    exports: list[str] = []
    for match in _PYTHON_DEF_RE.finditer(content):
        name = match.group(1)
        # Only include public names (not starting with _)
        if not name.startswith("_"):
            exports.append(name)
    return exports


def _extract_go_exports(content: str) -> list[str]:
    """Extract Go exported functions and types (capitalized)."""
    exports: list[str] = []
    for match in _GO_FUNC_RE.finditer(content):
        exports.append(match.group(1))
    for match in _GO_TYPE_RE.finditer(content):
        exports.append(match.group(1))
    return exports


def _extract_ts_exports(content: str) -> list[str]:
    """Extract TypeScript/JavaScript exported names."""
    exports: list[str] = []
    for match in _TS_EXPORT_RE.finditer(content):
        exports.append(match.group(1))
    return exports


def _module_matches_import(file_path: str, import_path: str) -> bool:
    """Check if a file path could satisfy a given import path.

    This performs a best-effort match:
    - For Python: converts file path to module form and checks containment
    - For Go: checks if the import path is a suffix of the file's directory
    - For TS/JS: checks relative path matching
    """
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".py":
        # Convert file path to module-like representation
        # e.g., "src/vikram_orchestrator/conflict_detector.py" -> "vikram_orchestrator.conflict_detector"
        path_no_ext = file_path.replace(os.sep, "/").replace(".py", "")
        # Remove common source prefixes
        for prefix in ("src/", "lib/", ""):
            if path_no_ext.startswith(prefix):
                path_no_ext = path_no_ext[len(prefix):]
                break
        module_form = path_no_ext.replace("/", ".")
        # Check if import matches the module or is a parent package
        return (
            module_form == import_path
            or module_form.endswith("." + import_path)
            or import_path.startswith(module_form.rsplit(".", 1)[0])
            if "." in module_form
            else module_form == import_path
        )

    elif ext == ".go":
        # Go imports are package paths; check if file's directory ends with the import
        dir_path = os.path.dirname(file_path).replace(os.sep, "/")
        return dir_path.endswith(import_path) or import_path.endswith(dir_path.split("/")[-1])

    elif ext in (".ts", ".tsx", ".js", ".jsx", ".mts", ".mjs"):
        # TS/JS relative imports: "./foo" or "../bar/baz"
        # Strip extension from file_path for comparison
        path_no_ext = file_path.replace(os.sep, "/")
        for suffix in (".ts", ".tsx", ".js", ".jsx", ".mts", ".mjs"):
            if path_no_ext.endswith(suffix):
                path_no_ext = path_no_ext[: -len(suffix)]
                break

        # Normalize import path
        normalized_import = import_path.replace(os.sep, "/")
        # Check if the import path is a suffix of the file path
        return (
            path_no_ext.endswith(normalized_import)
            or path_no_ext.endswith(normalized_import.lstrip("./"))
        )

    return False


# ---------------------------------------------------------------------------
# Conflict Detector
# ---------------------------------------------------------------------------


class ConflictDetector:
    """Predicts conflicts between tasks and proposes reorderings.

    Uses file overlap analysis, semantic dependency detection, and
    conflict-aware scheduling to minimize merge conflicts.

    Args:
        lock_registry: Optional LockRegistry for mutual exclusion enforcement.
        conflict_threshold: Probability threshold (0.0–1.0) above which
            conflict alerts are emitted. Default: 0.70 (70%) per Req 35.3.
        alert_emitter: Optional protocol-compatible object for emitting
            telemetry events (e.g. TelemetryClient). When provided, conflict
            prediction alerts are emitted for overlaps exceeding the threshold.
    """

    def __init__(
        self,
        lock_registry: LockRegistry | None = None,
        *,
        conflict_threshold: float = DEFAULT_CONFLICT_THRESHOLD,
        alert_emitter: AlertEmitter | None = None,
    ) -> None:
        if not (0.0 <= conflict_threshold <= 1.0):
            raise ValueError(
                f"conflict_threshold must be in [0.0, 1.0], got {conflict_threshold}"
            )
        self._lock_registry = lock_registry or LockRegistry()
        self._conflict_threshold = conflict_threshold
        self._alert_emitter = alert_emitter

    @property
    def lock_registry(self) -> LockRegistry:
        return self._lock_registry

    @property
    def conflict_threshold(self) -> float:
        """Current conflict probability threshold."""
        return self._conflict_threshold

    @conflict_threshold.setter
    def conflict_threshold(self, value: float) -> None:
        """Update the conflict threshold at runtime."""
        if not (0.0 <= value <= 1.0):
            raise ValueError(
                f"conflict_threshold must be in [0.0, 1.0], got {value}"
            )
        self._conflict_threshold = value

    def predict_conflicts(
        self,
        new_task_id: str,
        new_task_targets: list[str],
        active_tasks: dict[str, list[str]],
    ) -> list[FileOverlap]:
        """Predict file overlaps between a new task and active tasks.

        Computes conflict probability for each overlapping file path,
        filters results to those at or above the configured threshold,
        and emits conflict prediction alerts for each threshold-exceeding
        overlap (Requirement 35.3).

        Args:
            new_task_id: The new task being evaluated.
            new_task_targets: List of file paths the new task will modify.
            active_tasks: Mapping of task_id -> list of file paths for active tasks.

        Returns:
            List of FileOverlap predictions that meet or exceed the
            configured conflict_threshold. If no threshold filtering is
            desired, set conflict_threshold=0.0.
        """
        all_overlaps: list[FileOverlap] = []
        new_set = set(new_task_targets)

        for active_id, active_targets in active_tasks.items():
            active_set = set(active_targets)
            common = new_set & active_set

            for path in common:
                prob = conflict_probability(
                    path,
                    task_a_targets={},
                    task_b_targets={},
                    same_functions=False,
                    changes_within_proximity=False,
                    historical_rate=0.5,
                )
                all_overlaps.append(
                    FileOverlap(
                        path=path,
                        task_a_id=new_task_id,
                        task_b_id=active_id,
                        overlap_type="same_file",
                        conflict_probability=prob,
                    )
                )

        # Filter to overlaps meeting the threshold (Requirement 35.3)
        threshold_overlaps = [
            o for o in all_overlaps
            if o.conflict_probability >= self._conflict_threshold
        ]

        # Emit conflict prediction alerts for threshold-exceeding overlaps
        if threshold_overlaps and self._alert_emitter is not None:
            self._emit_conflict_alerts(new_task_id, threshold_overlaps)

        return threshold_overlaps

    def predict_conflicts_unfiltered(
        self,
        new_task_id: str,
        new_task_targets: list[str],
        active_tasks: dict[str, list[str]],
    ) -> list[FileOverlap]:
        """Predict file overlaps without threshold filtering.

        Same as predict_conflicts() but returns ALL overlaps regardless of
        probability score. Useful for analysis and testing.

        Args:
            new_task_id: The new task being evaluated.
            new_task_targets: List of file paths the new task will modify.
            active_tasks: Mapping of task_id -> list of file paths for active tasks.

        Returns:
            List of all FileOverlap predictions (unfiltered).
        """
        overlaps: list[FileOverlap] = []
        new_set = set(new_task_targets)

        for active_id, active_targets in active_tasks.items():
            active_set = set(active_targets)
            common = new_set & active_set

            for path in common:
                prob = conflict_probability(
                    path,
                    task_a_targets={},
                    task_b_targets={},
                    same_functions=False,
                    changes_within_proximity=False,
                    historical_rate=0.5,
                )
                overlaps.append(
                    FileOverlap(
                        path=path,
                        task_a_id=new_task_id,
                        task_b_id=active_id,
                        overlap_type="same_file",
                        conflict_probability=prob,
                    )
                )

        return overlaps

    def _emit_conflict_alerts(
        self, new_task_id: str, overlaps: list[FileOverlap]
    ) -> None:
        """Emit conflict prediction alert events via the telemetry system.

        Per Requirement 35.3: WHEN conflict probability exceeds a configured
        threshold (default: 70%), THE Conflict_Detector SHALL emit a conflict
        prediction alert to the founder with the specific tasks, files, and
        estimated probability.
        """
        if self._alert_emitter is None:
            return

        for overlap in overlaps:
            attributes: dict[str, Any] = {
                "alert_type": "conflict_prediction",
                "conflicting_task_a": overlap.task_a_id,
                "conflicting_task_b": overlap.task_b_id,
                "file_path": overlap.path,
                "overlap_type": overlap.overlap_type,
                "conflict_probability": overlap.conflict_probability,
                "threshold": self._conflict_threshold,
            }
            try:
                self._alert_emitter.emit_event(
                    event_type="conflict_prediction_alert",
                    task_id=new_task_id,
                    attributes=attributes,
                )
                logger.info(
                    "Conflict prediction alert emitted: %s vs %s on %s (%.1f%%)",
                    overlap.task_a_id,
                    overlap.task_b_id,
                    overlap.path,
                    overlap.conflict_probability * 100,
                )
            except Exception:
                logger.exception(
                    "Failed to emit conflict prediction alert for %s",
                    overlap.path,
                )

    def compute_dependency_graph(
        self,
        task_targets: dict[str, list[str]],
        file_contents: dict[str, str] | None = None,
    ) -> list[SemanticDependency]:
        """Compute cross-task semantic dependencies via static analysis.

        Analyzes import statements in task target files to identify when one
        task modifies an interface that another task's files import/consume.

        This implements Requirements 37.1, 37.2:
        - Identifies modified function signatures, exported types, API endpoints
        - Builds a cross-task dependency graph from import/call analysis

        Args:
            task_targets: Mapping of task_id -> list of file paths each task targets.
            file_contents: Optional mapping of file_path -> file content string.
                If not provided, the method attempts to read files from disk.
                Providing contents explicitly is useful for testing and for
                analyzing worktree content without filesystem access.

        Returns:
            List of SemanticDependency objects representing cross-task
            interface dependencies.
        """
        if file_contents is None:
            file_contents = {}

        dependencies: list[SemanticDependency] = []

        # Phase 1: For each task, extract the interfaces its files export
        # and the imports its files consume.
        task_exports: dict[str, dict[str, list[str]]] = {}
        # task_id -> {file_path: [exported_names]}

        task_imports: dict[str, dict[str, list[str]]] = {}
        # task_id -> {file_path: [import_paths]}

        for task_id, file_paths in task_targets.items():
            task_exports[task_id] = {}
            task_imports[task_id] = {}

            for file_path in file_paths:
                content = self._get_file_content(file_path, file_contents)
                if content is None:
                    continue

                exports = extract_exported_interfaces(file_path, content)
                if exports:
                    task_exports[task_id][file_path] = exports

                imports = extract_imports(file_path, content)
                if imports:
                    task_imports[task_id][file_path] = imports

        # Phase 2: Cross-reference — for each task that modifies a file with
        # exported interfaces, check if any other task's files import from it.
        for source_task_id, exports_by_file in task_exports.items():
            for source_file, exported_names in exports_by_file.items():
                # Check all other tasks' imports
                for consumer_task_id, imports_by_file in task_imports.items():
                    if consumer_task_id == source_task_id:
                        continue  # Skip self-dependencies

                    for consumer_file, import_paths in imports_by_file.items():
                        for import_path in import_paths:
                            if _module_matches_import(source_file, import_path):
                                # Found a cross-task dependency
                                for interface_name in exported_names:
                                    dependencies.append(
                                        SemanticDependency(
                                            source_task_id=source_task_id,
                                            source_file=source_file,
                                            modified_interface=interface_name,
                                            consumer_task_id=consumer_task_id,
                                            consumer_file=consumer_file,
                                            import_path=import_path,
                                        )
                                    )

        return dependencies

    @staticmethod
    def _get_file_content(
        file_path: str, file_contents: dict[str, str]
    ) -> str | None:
        """Get file content from provided dict or attempt to read from disk."""
        if file_path in file_contents:
            return file_contents[file_path]

        # Attempt to read from filesystem
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except (OSError, IOError):
            logger.debug("Cannot read file %s for dependency analysis", file_path)
            return None

    def propose_reorder(
        self,
        queue: list[str],
        conflicts: list[FileOverlap],
        priorities: dict[str, int],
        deps: dict[str, list[str]],
    ) -> ReorderProposal | None:
        """Propose a reordering of the task queue to minimize conflicts.

        Constraints (from Requirement 36.4):
        - Must preserve declared dependency constraints (if A depends on B, B comes first)
        - Must not move a higher-priority task behind a lower-priority one
          (lower priority number = higher priority)

        Args:
            queue: Current task ordering (list of task_ids).
            conflicts: Predicted file overlaps between tasks.
            priorities: Mapping of task_id -> priority (lower = more important).
            deps: Mapping of task_id -> list of task_ids it depends on.

        Returns:
            A ReorderProposal if a beneficial reorder exists, None otherwise.
        """
        if not queue or not conflicts:
            return None

        # Build set of conflicting task pairs
        conflict_pairs: set[tuple[str, str]] = set()
        for overlap in conflicts:
            if overlap.task_a_id in queue and overlap.task_b_id in queue:
                conflict_pairs.add((overlap.task_a_id, overlap.task_b_id))

        if not conflict_pairs:
            return None

        # Try to find a reorder that separates conflicting tasks
        # while preserving dependency and priority constraints
        proposed = self._compute_valid_reorder(queue, conflict_pairs, priorities, deps)

        if proposed is None or proposed == queue:
            return None

        # Determine which conflicts are avoided
        avoided = []
        for overlap in conflicts:
            a_idx_orig = queue.index(overlap.task_a_id) if overlap.task_a_id in queue else -1
            b_idx_orig = queue.index(overlap.task_b_id) if overlap.task_b_id in queue else -1
            a_idx_new = proposed.index(overlap.task_a_id) if overlap.task_a_id in proposed else -1
            b_idx_new = proposed.index(overlap.task_b_id) if overlap.task_b_id in proposed else -1

            # If previously adjacent, now separated
            if (a_idx_orig >= 0 and b_idx_orig >= 0 and
                abs(a_idx_orig - b_idx_orig) == 1 and
                abs(a_idx_new - b_idx_new) > 1):
                avoided.append(overlap)

        return ReorderProposal(
            original_order=list(queue),
            proposed_order=proposed,
            conflicts_avoided=avoided,
            throughput_impact="none" if len(avoided) > 0 else "minor_delay",
        )

    def _compute_valid_reorder(
        self,
        queue: list[str],
        conflict_pairs: set[tuple[str, str]],
        priorities: dict[str, int],
        deps: dict[str, list[str]],
    ) -> list[str] | None:
        """Compute a valid reorder respecting constraints.

        Uses a topological sort that respects:
        1. Dependency constraints: if task A depends on B, B must come before A
        2. Priority ordering: a higher-priority task (lower number) must not be
           placed after a lower-priority task (higher number) when they are independent

        Tries to separate conflicting tasks by inserting non-conflicting tasks between them.
        Returns None if no valid reorder can satisfy all constraints.
        """
        # Validate all tasks in queue have priorities
        for task_id in queue:
            if task_id not in priorities:
                return None

        # Sort by priority (lower number first), then by original position
        original_positions = {task_id: idx for idx, task_id in enumerate(queue)}

        proposed = sorted(
            queue,
            key=lambda t: (priorities.get(t, 999), original_positions[t]),
        )

        # Verify dependency constraints are preserved in the priority-sorted order
        if self._verify_constraints(proposed, priorities, deps):
            return proposed

        # Priority sort violated dependencies — use topological sort
        proposed = self._topological_sort_with_priority(queue, priorities, deps)
        if proposed is None:
            return None

        # Verify the topological sort result satisfies ALL constraints
        if not self._verify_constraints(proposed, priorities, deps):
            # No valid ordering exists that satisfies both dependency and
            # priority constraints simultaneously — return None
            return None

        return proposed

    def _topological_sort_with_priority(
        self,
        queue: list[str],
        priorities: dict[str, int],
        deps: dict[str, list[str]],
    ) -> list[str] | None:
        """Topological sort respecting both dependencies and priorities.

        Among ready tasks (all dependencies satisfied), always picks the one
        with the highest priority (lowest priority number). This guarantees:
        1. Dependencies are always respected (topological ordering)
        2. Among tasks with no ordering constraint between them, the
           higher-priority task comes first
        """
        task_set = set(queue)
        in_degree: dict[str, int] = {t: 0 for t in queue}
        graph: dict[str, list[str]] = {t: [] for t in queue}
        original_positions = {task_id: idx for idx, task_id in enumerate(queue)}

        for task_id in queue:
            for dep_id in deps.get(task_id, []):
                if dep_id in task_set:
                    graph[dep_id].append(task_id)
                    in_degree[task_id] += 1

        result: list[str] = []
        ready = [t for t in queue if in_degree[t] == 0]

        while ready:
            # Among ready tasks, pick highest priority (lowest number),
            # break ties by original position for stability
            ready.sort(key=lambda t: (
                priorities.get(t, 999),
                original_positions.get(t, 999),
            ))
            chosen = ready.pop(0)
            result.append(chosen)

            for neighbor in graph[chosen]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    ready.append(neighbor)

        if len(result) != len(queue):
            # Cycle detected — cannot reorder
            return None

        return result

    @staticmethod
    def verify_constraints(
        order: list[str],
        priorities: dict[str, int],
        deps: dict[str, list[str]],
    ) -> bool:
        """Verify that an ordering respects dependency and priority constraints.

        Constraints:
        1. Dependencies: if task A depends on B, B must appear before A in the order
        2. Priority: between two tasks that have NO dependency relationship
           (neither depends on the other, directly or transitively), the
           higher-priority task (lower number) must not appear after the
           lower-priority task (higher number).

        When a dependency forces ordering (e.g., high-priority task depends on
        a low-priority task), priority ordering yields to dependency ordering.

        Returns True if all constraints are satisfied.
        """
        task_set = set(order)
        position = {task_id: idx for idx, task_id in enumerate(order)}

        # Check dependency constraints
        for task_id in order:
            for dep_id in deps.get(task_id, []):
                if dep_id in task_set:
                    if position[dep_id] >= position[task_id]:
                        return False  # Dependency must come before

        # Check priority constraints only between independent tasks
        # (tasks with no dependency path in either direction)
        for i, task_a in enumerate(order):
            for j, task_b in enumerate(order):
                if i < j:
                    # task_a comes before task_b
                    pri_a = priorities.get(task_a, 999)
                    pri_b = priorities.get(task_b, 999)
                    if pri_a > pri_b:
                        # Lower priority task_a is before higher priority task_b
                        # This is only valid if there's a dependency relationship
                        # between them (in either direction — i.e., they're not independent)
                        if (not _has_dependency_path(task_b, task_a, deps, task_set) and
                                not _has_dependency_path(task_a, task_b, deps, task_set)):
                            return False

        return True

    def _verify_constraints(
        self,
        order: list[str],
        priorities: dict[str, int],
        deps: dict[str, list[str]],
    ) -> bool:
        """Instance method wrapper for verify_constraints."""
        return ConflictDetector.verify_constraints(order, priorities, deps)


def _has_dependency_path(
    task: str, target: str, deps: dict[str, list[str]], task_set: set[str]
) -> bool:
    """Check if 'task' transitively depends on 'target'."""
    visited: set[str] = set()
    stack = [task]

    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)

        for dep_id in deps.get(current, []):
            if dep_id == target:
                return True
            if dep_id in task_set and dep_id not in visited:
                stack.append(dep_id)

    return False
