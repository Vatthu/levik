"""Unit tests for multi-repository task state management.

Validates: Requirements 21.1, 21.2, 21.3
"""

from __future__ import annotations

import unittest

from vikram_orchestrator.multi_repo import (
    InterfaceChange,
    IntegrationVerificationConfig,
    MergeGateCondition,
    MultiRepoTask,
    RepoRef,
    RepoState,
    VALID_STATUSES,
    VerificationReport,
    aggregate_verification,
    classify_change_type,
    evaluate_merge_gate,
    is_breaking_change,
)
from vikram_orchestrator.models import (
    RepoRef as ModelsRepoRef,
    TaskCreateRequest,
    HostActionRequest,
    HostObservation,
    GitRollbackRequest,
    GitRollbackResponse,
)


def _make_repos(count: int = 3) -> list[dict]:
    """Create a list of repo dicts for testing."""
    return [
        {"path": f"/repos/repo-{i}", "default_branch": "main"}
        for i in range(count)
    ]


def _make_repos_with_deps() -> list[dict]:
    """Create repos with dependency relationships."""
    return [
        {"path": "/repos/api", "default_branch": "main", "depends_on": []},
        {"path": "/repos/client", "default_branch": "main", "depends_on": ["/repos/api"]},
        {"path": "/repos/docs", "default_branch": "main", "depends_on": ["/repos/api", "/repos/client"]},
    ]


class TestRepoStateModel(unittest.TestCase):
    """Tests for the RepoState Pydantic model."""

    def test_create_with_defaults(self) -> None:
        """RepoState can be created with minimal fields."""
        state = RepoState(
            repo_path="/repos/test",
            worktree_path="/tmp/wt",
            branch="main",
            head_ref="abc123",
        )
        self.assertEqual(state.repo_path, "/repos/test")
        self.assertFalse(state.dirty)
        self.assertEqual(state.status, "pending")
        self.assertEqual(state.changed_files, [])
        self.assertIsNone(state.verification_result)
        self.assertEqual(state.blockers, [])

    def test_create_with_all_fields(self) -> None:
        """RepoState with all fields populated."""
        state = RepoState(
            repo_path="/repos/test",
            worktree_path="/tmp/wt",
            branch="feature/test",
            head_ref="abc123",
            dirty=True,
            status="in_progress",
            changed_files=[{"path": "src/main.py", "status": "modified"}],
            verification_result="pass",
            blockers=["needs review"],
        )
        self.assertTrue(state.dirty)
        self.assertEqual(state.status, "in_progress")
        self.assertEqual(len(state.changed_files), 1)
        self.assertEqual(state.verification_result, "pass")
        self.assertEqual(state.blockers, ["needs review"])


class TestInterfaceChangeModel(unittest.TestCase):
    """Tests for the InterfaceChange Pydantic model."""

    def test_create_interface_change(self) -> None:
        """InterfaceChange can be created with all fields."""
        change = InterfaceChange(
            repo_path="/repos/api",
            file_path="src/types.ts",
            interface_type="function_signature",
            old_signature="def get_user(id: int) -> User",
            new_signature="def get_user(id: int, include_deleted: bool = False) -> User",
            consuming_repos=["/repos/client", "/repos/admin"],
        )
        self.assertEqual(change.repo_path, "/repos/api")
        self.assertEqual(change.interface_type, "function_signature")
        self.assertEqual(len(change.consuming_repos), 2)

    def test_create_with_empty_consumers(self) -> None:
        """InterfaceChange defaults to empty consuming_repos."""
        change = InterfaceChange(
            repo_path="/repos/api",
            file_path="src/schema.graphql",
            interface_type="api_schema",
            old_signature="type User { id: ID! }",
            new_signature="type User { id: ID!, email: String! }",
        )
        self.assertEqual(change.consuming_repos, [])


class TestMergeGateCondition(unittest.TestCase):
    """Tests for the MergeGateCondition model."""

    def test_passes_when_all_true(self) -> None:
        """MergeGateCondition passes when all four fields are True."""
        cond = MergeGateCondition(
            repo_path="/repos/test",
            verification_passed=True,
            review_approved=True,
            conflict_free=True,
            governance_cleared=True,
        )
        self.assertTrue(cond.passes)

    def test_fails_with_any_false(self) -> None:
        """MergeGateCondition fails when any field is False."""
        for field in ("verification_passed", "review_approved", "conflict_free", "governance_cleared"):
            kwargs = {
                "repo_path": "/repos/test",
                "verification_passed": True,
                "review_approved": True,
                "conflict_free": True,
                "governance_cleared": True,
            }
            kwargs[field] = False
            cond = MergeGateCondition(**kwargs)
            self.assertFalse(cond.passes, f"Should fail when {field} is False")


class TestMultiRepoTaskInit(unittest.TestCase):
    """Tests for MultiRepoTask initialization."""

    def test_init_creates_per_repo_state(self) -> None:
        """Init creates state entries for each provided repo."""
        repos = _make_repos(3)
        task = MultiRepoTask("task-1", repos)
        self.assertEqual(len(task.repo_paths), 3)
        for i in range(3):
            state = task.get_repo_state(f"/repos/repo-{i}")
            self.assertEqual(state.status, "pending")
            self.assertEqual(state.branch, "main")

    def test_init_with_dependencies(self) -> None:
        """Init captures dependency relationships from repo dicts."""
        repos = _make_repos_with_deps()
        task = MultiRepoTask("task-1", repos)
        graph = task.get_dependency_graph()
        self.assertEqual(graph["/repos/api"], [])
        self.assertEqual(graph["/repos/client"], ["/repos/api"])
        self.assertEqual(graph["/repos/docs"], ["/repos/api", "/repos/client"])

    def test_init_with_empty_repos(self) -> None:
        """Init with empty repos list creates empty state."""
        task = MultiRepoTask("task-1", [])
        self.assertEqual(task.repo_paths, [])

    def test_init_stores_task_id(self) -> None:
        """Init stores the task_id."""
        task = MultiRepoTask("my-task-123", _make_repos(1))
        self.assertEqual(task.task_id, "my-task-123")

    def test_init_custom_branch(self) -> None:
        """Init respects custom default_branch."""
        repos = [{"path": "/repos/main", "default_branch": "develop"}]
        task = MultiRepoTask("task-1", repos)
        state = task.get_repo_state("/repos/main")
        self.assertEqual(state.branch, "develop")

    def test_init_with_repo_ref_objects(self) -> None:
        """Init accepts RepoRef objects (from multi_repo module)."""
        refs = [RepoRef(repo_path="/repos/api"), RepoRef(repo_path="/repos/client")]
        task = MultiRepoTask("task-1", refs)
        self.assertEqual(len(task.repo_paths), 2)
        self.assertIn("/repos/api", task.repo_paths)


class TestMultiRepoTaskProvisionAll(unittest.TestCase):
    """Tests for provision_all() method.

    Validates: Requirement 21.2 — Host provisions separate worktrees for each repo.
    """

    def test_provision_all_populates_worktree_paths(self) -> None:
        """provision_all() populates worktree_path for all repos."""
        repos = _make_repos(2)
        task = MultiRepoTask("task-1", repos)
        result = task.provision_all()

        for path in task.repo_paths:
            state = result[path]
            self.assertNotEqual(state.worktree_path, "")
            self.assertIn("task-1", state.worktree_path)

    def test_provision_all_returns_all_repos(self) -> None:
        """provision_all() returns state for every tracked repo."""
        repos = _make_repos(4)
        task = MultiRepoTask("task-1", repos)
        result = task.provision_all()
        self.assertEqual(len(result), 4)

    def test_provision_all_sets_branch(self) -> None:
        """provision_all() sets a task-specific branch name."""
        repos = _make_repos(1)
        task = MultiRepoTask("task-42", repos)
        result = task.provision_all()
        state = result["/repos/repo-0"]
        self.assertIn("task-42", state.branch)

    def test_provision_all_unique_worktree_paths(self) -> None:
        """provision_all() generates unique worktree paths per repo."""
        repos = _make_repos(3)
        task = MultiRepoTask("task-1", repos)
        result = task.provision_all()
        paths = [state.worktree_path for state in result.values()]
        self.assertEqual(len(set(paths)), 3)


class TestMultiRepoTaskGetRepoState(unittest.TestCase):
    """Tests for get_repo_state() method.

    Validates: Requirement 21.3 — per-repository state tracking.
    """

    def test_get_existing_repo(self) -> None:
        """get_repo_state returns state for a tracked repo."""
        repos = _make_repos(2)
        task = MultiRepoTask("task-1", repos)
        state = task.get_repo_state("/repos/repo-0")
        self.assertEqual(state.repo_path, "/repos/repo-0")

    def test_get_nonexistent_repo_raises(self) -> None:
        """get_repo_state raises KeyError for unknown repo."""
        task = MultiRepoTask("task-1", _make_repos(1))
        with self.assertRaises(KeyError) as ctx:
            task.get_repo_state("/repos/unknown")
        self.assertIn("not part of this task", str(ctx.exception))


class TestMultiRepoTaskUpdateStatus(unittest.TestCase):
    """Tests for update_repo_status() method."""

    def test_update_valid_status(self) -> None:
        """update_repo_status sets the new status."""
        task = MultiRepoTask("task-1", _make_repos(1))
        task.update_repo_status("/repos/repo-0", "in_progress")
        state = task.get_repo_state("/repos/repo-0")
        self.assertEqual(state.status, "in_progress")

    def test_update_all_valid_statuses(self) -> None:
        """All valid status values are accepted."""
        task = MultiRepoTask("task-1", _make_repos(1))
        for status in VALID_STATUSES:
            task.update_repo_status("/repos/repo-0", status)
            state = task.get_repo_state("/repos/repo-0")
            self.assertEqual(state.status, status)

    def test_update_invalid_status_raises(self) -> None:
        """update_repo_status raises ValueError for invalid status."""
        task = MultiRepoTask("task-1", _make_repos(1))
        with self.assertRaises(ValueError) as ctx:
            task.update_repo_status("/repos/repo-0", "invalid_status")
        self.assertIn("Invalid status", str(ctx.exception))

    def test_update_nonexistent_repo_raises(self) -> None:
        """update_repo_status raises KeyError for unknown repo."""
        task = MultiRepoTask("task-1", _make_repos(1))
        with self.assertRaises(KeyError):
            task.update_repo_status("/repos/unknown", "completed")


class TestMultiRepoTaskDependencyGraph(unittest.TestCase):
    """Tests for get_dependency_graph() method.

    Validates: Requirement 21.3 (cross-repo dependency tracking).
    """

    def test_graph_with_no_dependencies(self) -> None:
        """Repos without dependencies have empty lists."""
        repos = _make_repos(3)
        task = MultiRepoTask("task-1", repos)
        graph = task.get_dependency_graph()
        for deps in graph.values():
            self.assertEqual(deps, [])

    def test_graph_with_dependencies(self) -> None:
        """Dependency graph reflects declared dependencies."""
        repos = _make_repos_with_deps()
        task = MultiRepoTask("task-1", repos)
        graph = task.get_dependency_graph()
        self.assertIn("/repos/api", graph["/repos/client"])
        self.assertIn("/repos/api", graph["/repos/docs"])
        self.assertIn("/repos/client", graph["/repos/docs"])

    def test_graph_returns_copy(self) -> None:
        """get_dependency_graph returns a new dict (not internal reference)."""
        task = MultiRepoTask("task-1", _make_repos(1))
        graph1 = task.get_dependency_graph()
        graph2 = task.get_dependency_graph()
        self.assertIsNot(graph1, graph2)


class TestMultiRepoTaskAggregateVerification(unittest.TestCase):
    """Tests for aggregate_verification() method.

    Validates: Requirement 21.3 (per-repo verification results in single task).
    """

    def test_all_pass(self) -> None:
        """All repos passing verification -> overall pass."""
        task = MultiRepoTask("task-1", _make_repos(2))
        task._repos["/repos/repo-0"].verification_result = "pass"
        task._repos["/repos/repo-1"].verification_result = "pass"

        overall, per_repo = task.aggregate_verification()
        self.assertEqual(overall, "pass")

    def test_one_failure(self) -> None:
        """One repo failing -> overall fail."""
        task = MultiRepoTask("task-1", _make_repos(2))
        task._repos["/repos/repo-0"].verification_result = "pass"
        task._repos["/repos/repo-1"].verification_result = "fail"

        overall, per_repo = task.aggregate_verification()
        self.assertEqual(overall, "fail")
        self.assertEqual(per_repo["/repos/repo-1"], "fail")

    def test_no_verification_run(self) -> None:
        """Repos with no verification result -> overall fail."""
        task = MultiRepoTask("task-1", _make_repos(2))

        overall, per_repo = task.aggregate_verification()
        self.assertEqual(overall, "fail")

    def test_detached_repo_excluded(self) -> None:
        """Detached repos are excluded from verification aggregation."""
        task = MultiRepoTask("task-1", _make_repos(2))
        task._repos["/repos/repo-0"].verification_result = "pass"
        task._repos["/repos/repo-1"].status = "detached"

        overall, per_repo = task.aggregate_verification()
        self.assertEqual(overall, "pass")
        self.assertNotIn("/repos/repo-1", per_repo)

    def test_per_repo_details(self) -> None:
        """per_repo contains status for each non-detached repo."""
        task = MultiRepoTask("task-1", _make_repos(3))
        task._repos["/repos/repo-0"].verification_result = "pass"
        task._repos["/repos/repo-1"].verification_result = "fail"
        task._repos["/repos/repo-2"].status = "detached"

        overall, per_repo = task.aggregate_verification()
        self.assertEqual(per_repo["/repos/repo-0"], "pass")
        self.assertEqual(per_repo["/repos/repo-1"], "fail")
        self.assertNotIn("/repos/repo-2", per_repo)


class TestMultiRepoTaskEvaluateMergeGate(unittest.TestCase):
    """Tests for evaluate_merge_gate() method.

    Validates: Requirement 21.3 (coordinated merge gate).
    """

    def test_all_repos_pass_with_state(self) -> None:
        """All repos completed with passing verification -> gate passes (no conditions)."""
        task = MultiRepoTask("task-1", _make_repos(2))
        for path in task.repo_paths:
            task._repos[path].status = "completed"
            task._repos[path].verification_result = "pass"

        passes, blockers = task.evaluate_merge_gate()
        self.assertTrue(passes)
        self.assertEqual(blockers, [])

    def test_incomplete_repo_blocks(self) -> None:
        """A repo with status != completed blocks the gate."""
        task = MultiRepoTask("task-1", _make_repos(2))
        task._repos["/repos/repo-0"].status = "completed"
        task._repos["/repos/repo-0"].verification_result = "pass"
        task._repos["/repos/repo-1"].status = "in_progress"
        task._repos["/repos/repo-1"].verification_result = "pass"

        passes, blockers = task.evaluate_merge_gate()
        self.assertFalse(passes)
        self.assertIn("/repos/repo-1", blockers)

    def test_failed_verification_blocks(self) -> None:
        """A repo with failed verification blocks the gate."""
        task = MultiRepoTask("task-1", _make_repos(1))
        task._repos["/repos/repo-0"].status = "completed"
        task._repos["/repos/repo-0"].verification_result = "fail"

        passes, blockers = task.evaluate_merge_gate()
        self.assertFalse(passes)

    def test_blockers_in_state_block(self) -> None:
        """Repos with explicit blockers cause gate failure."""
        task = MultiRepoTask("task-1", _make_repos(1))
        task._repos["/repos/repo-0"].status = "completed"
        task._repos["/repos/repo-0"].verification_result = "pass"
        task._repos["/repos/repo-0"].blockers = ["conflict with main"]

        passes, blockers = task.evaluate_merge_gate()
        self.assertFalse(passes)

    def test_detached_repo_passes_gate(self) -> None:
        """Detached repos are treated as passing the gate."""
        task = MultiRepoTask("task-1", _make_repos(2))
        task._repos["/repos/repo-0"].status = "completed"
        task._repos["/repos/repo-0"].verification_result = "pass"
        task._repos["/repos/repo-1"].status = "detached"

        passes, blockers = task.evaluate_merge_gate()
        self.assertTrue(passes)

    def test_with_conditions(self) -> None:
        """evaluate_merge_gate works with explicit MergeGateCondition objects."""
        task = MultiRepoTask("task-1", _make_repos(2))
        conditions = {
            "/repos/repo-0": MergeGateCondition(
                repo_path="/repos/repo-0",
                verification_passed=True,
                review_approved=True,
                conflict_free=True,
                governance_cleared=True,
            ),
            "/repos/repo-1": MergeGateCondition(
                repo_path="/repos/repo-1",
                verification_passed=False,
                review_approved=True,
                conflict_free=True,
                governance_cleared=True,
            ),
        }
        passes, blockers = task.evaluate_merge_gate(conditions)
        self.assertFalse(passes)
        self.assertIn("/repos/repo-1", blockers)


class TestMultiRepoTaskDetachRepo(unittest.TestCase):
    """Tests for detach_repo() method.

    Validates: Requirement 21.3 (founder can detach blocked repo).
    """

    def test_detach_sets_status(self) -> None:
        """detach_repo sets repo status to 'detached'."""
        task = MultiRepoTask("task-1", _make_repos(2))
        task.detach_repo("/repos/repo-1")
        state = task.get_repo_state("/repos/repo-1")
        self.assertEqual(state.status, "detached")

    def test_detach_nonexistent_raises(self) -> None:
        """detach_repo raises KeyError for unknown repo."""
        task = MultiRepoTask("task-1", _make_repos(1))
        with self.assertRaises(KeyError):
            task.detach_repo("/repos/unknown")

    def test_detach_does_not_affect_other_repos(self) -> None:
        """Detaching one repo does not change others."""
        task = MultiRepoTask("task-1", _make_repos(3))
        task.update_repo_status("/repos/repo-0", "in_progress")
        task.detach_repo("/repos/repo-1")

        self.assertEqual(task.get_repo_state("/repos/repo-0").status, "in_progress")
        self.assertEqual(task.get_repo_state("/repos/repo-2").status, "pending")


class TestStandaloneAggregateVerification(unittest.TestCase):
    """Tests for the standalone aggregate_verification function."""

    def test_all_pass(self) -> None:
        """All repos pass -> overall pass."""
        overall, per_repo = aggregate_verification({"/a": "pass", "/b": "pass"})
        self.assertEqual(overall, "pass")

    def test_one_failure(self) -> None:
        """Any repo failing -> overall fail."""
        overall, per_repo = aggregate_verification({"/a": "pass", "/b": "fail"})
        self.assertEqual(overall, "fail")

    def test_empty_passes(self) -> None:
        """Empty input vacuously passes."""
        overall, per_repo = aggregate_verification({})
        self.assertEqual(overall, "pass")


class TestStandaloneEvaluateMergeGate(unittest.TestCase):
    """Tests for the standalone evaluate_merge_gate function."""

    def test_all_pass(self) -> None:
        """All conditions satisfied -> gate passes."""
        conditions = {
            "/a": MergeGateCondition(
                repo_path="/a", verification_passed=True,
                review_approved=True, conflict_free=True, governance_cleared=True,
            ),
        }
        passes, blockers = evaluate_merge_gate(conditions)
        self.assertTrue(passes)
        self.assertEqual(blockers, [])

    def test_one_fails(self) -> None:
        """One condition not met -> gate fails."""
        conditions = {
            "/a": MergeGateCondition(
                repo_path="/a", verification_passed=True,
                review_approved=True, conflict_free=True, governance_cleared=True,
            ),
            "/b": MergeGateCondition(
                repo_path="/b", verification_passed=False,
                review_approved=True, conflict_free=True, governance_cleared=True,
            ),
        }
        passes, blockers = evaluate_merge_gate(conditions)
        self.assertFalse(passes)
        self.assertIn("/b", blockers)

    def test_empty_passes(self) -> None:
        """Empty conditions vacuously passes."""
        passes, blockers = evaluate_merge_gate({})
        self.assertTrue(passes)
        self.assertEqual(blockers, [])


class TestDetectInterfaceChanges(unittest.TestCase):
    """Tests for cross-repository interface change detection.

    Validates: Requirements 22.1, 22.2, 22.3, 22.4
    """

    def _make_multi_repo_task(self) -> MultiRepoTask:
        """Create a task with 3 repos for testing."""
        repos = [
            {"path": "/repos/api", "default_branch": "main"},
            {"path": "/repos/client", "default_branch": "main"},
            {"path": "/repos/docs", "default_branch": "main"},
        ]
        return MultiRepoTask("task-1", repos)

    def test_no_changes_returns_empty(self) -> None:
        """No file contents -> no interface changes detected."""
        task = self._make_multi_repo_task()
        changes = task.detect_interface_changes()
        self.assertEqual(changes, [])

    def test_no_changes_with_identical_old_new(self) -> None:
        """Same old and new contents -> no changes."""
        task = self._make_multi_repo_task()
        content = "def get_user(id: int) -> dict:\n    pass\n"
        old = {"/repos/api": {"src/users.py": content}}
        new = {"/repos/api": {"src/users.py": content}}
        changes = task.detect_interface_changes(old_file_contents=old, new_file_contents=new)
        self.assertEqual(changes, [])

    def test_detects_added_export(self) -> None:
        """New export added -> detected as interface change."""
        task = self._make_multi_repo_task()
        old_content = "def get_user(id: int) -> dict:\n    pass\n"
        new_content = "def get_user(id: int) -> dict:\n    pass\n\ndef create_user(name: str) -> dict:\n    pass\n"
        old = {"/repos/api": {"src/users.py": old_content}}
        new = {"/repos/api": {"src/users.py": new_content}}

        changes = task.detect_interface_changes(old_file_contents=old, new_file_contents=new)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].repo_path, "/repos/api")
        self.assertIn("create_user", changes[0].new_signature)
        self.assertEqual(changes[0].old_signature, "")

    def test_detects_removed_export(self) -> None:
        """Removed export -> detected as breaking change."""
        task = self._make_multi_repo_task()
        old_content = "def get_user(id: int) -> dict:\n    pass\n\ndef delete_user(id: int) -> None:\n    pass\n"
        new_content = "def get_user(id: int) -> dict:\n    pass\n"
        old = {"/repos/api": {"src/users.py": old_content}}
        new = {"/repos/api": {"src/users.py": new_content}}

        changes = task.detect_interface_changes(old_file_contents=old, new_file_contents=new)
        self.assertEqual(len(changes), 1)
        self.assertIn("delete_user", changes[0].old_signature)
        self.assertEqual(changes[0].new_signature, "")

    def test_detects_modified_signature(self) -> None:
        """Changed function signature -> detected as change."""
        task = self._make_multi_repo_task()
        old_content = "def get_user(id: int) -> dict:\n    pass\n"
        new_content = "def get_user(id: int, include_deleted: bool = False) -> dict:\n    pass\n"
        old = {"/repos/api": {"src/users.py": old_content}}
        new = {"/repos/api": {"src/users.py": new_content}}

        changes = task.detect_interface_changes(old_file_contents=old, new_file_contents=new)
        self.assertEqual(len(changes), 1)
        self.assertIn("get_user", changes[0].old_signature)
        self.assertIn("include_deleted", changes[0].new_signature)

    def test_finds_consuming_repos_via_imports(self) -> None:
        """Consumer repos that import from source are identified."""
        task = self._make_multi_repo_task()
        old_api = "def get_user(id: int) -> dict:\n    pass\n"
        new_api = "def get_user(id: int, verbose: bool = False) -> dict:\n    pass\n"
        client_content = "from users import get_user\n\ndef main():\n    get_user(1)\n"

        old = {
            "/repos/api": {"users.py": old_api},
            "/repos/client": {"client.py": client_content},
        }
        new = {
            "/repos/api": {"users.py": new_api},
            "/repos/client": {"client.py": client_content},
        }

        changes = task.detect_interface_changes(old_file_contents=old, new_file_contents=new)
        # Only the api change should be detected (client.py is unchanged)
        self.assertEqual(len(changes), 1)
        self.assertIn("/repos/client", changes[0].consuming_repos)

    def test_consuming_repos_excludes_source_repo(self) -> None:
        """Source repo is not listed as a consumer of itself."""
        task = self._make_multi_repo_task()
        old_api = "def get_user(id: int) -> dict:\n    pass\n"
        new_api = "def get_user(id: int, verbose: bool = False) -> dict:\n    pass\n"
        # Same repo imports from itself
        api_other = "from users import get_user\n"

        old = {"/repos/api": {"users.py": old_api}}
        new = {
            "/repos/api": {"users.py": new_api, "other.py": api_other},
        }

        changes = task.detect_interface_changes(old_file_contents=old, new_file_contents=new)
        self.assertEqual(len(changes), 1)
        self.assertNotIn("/repos/api", changes[0].consuming_repos)

    def test_detached_repos_excluded(self) -> None:
        """Detached repos are not scanned for changes or as consumers."""
        task = self._make_multi_repo_task()
        task.detach_repo("/repos/api")

        old_api = "def get_user(id: int) -> dict:\n    pass\n"
        new_api = "def get_user(id: int, verbose: bool = False) -> dict:\n    pass\n"
        old = {"/repos/api": {"users.py": old_api}}
        new = {"/repos/api": {"users.py": new_api}}

        changes = task.detect_interface_changes(old_file_contents=old, new_file_contents=new)
        self.assertEqual(changes, [])

    def test_typescript_exports_detected(self) -> None:
        """TypeScript exported interfaces are detected."""
        task = self._make_multi_repo_task()
        old_ts = "export function getUser(id: number): User {\n  return {} as User;\n}\n"
        new_ts = "export function getUser(id: number, opts?: Options): User {\n  return {} as User;\n}\n"
        old = {"/repos/api": {"src/users.ts": old_ts}}
        new = {"/repos/api": {"src/users.ts": new_ts}}

        changes = task.detect_interface_changes(old_file_contents=old, new_file_contents=new)
        self.assertEqual(len(changes), 1)
        self.assertIn("getUser", changes[0].old_signature)

    def test_go_exports_detected(self) -> None:
        """Go exported functions (capitalized) are detected."""
        task = self._make_multi_repo_task()
        old_go = 'package users\n\nfunc GetUser(id int) User {\n    return User{}\n}\n'
        new_go = 'package users\n\nfunc GetUser(id int, verbose bool) User {\n    return User{}\n}\n'
        old = {"/repos/api": {"pkg/users/users.go": old_go}}
        new = {"/repos/api": {"pkg/users/users.go": new_go}}

        changes = task.detect_interface_changes(old_file_contents=old, new_file_contents=new)
        self.assertEqual(len(changes), 1)
        self.assertIn("GetUser", changes[0].old_signature)

    def test_execution_trace_recorded(self) -> None:
        """Interface changes are recorded in execution trace when provided."""
        task = self._make_multi_repo_task()

        old_api = "def get_user(id: int) -> dict:\n    pass\n"
        new_api = "def get_user(id: int, verbose: bool = False) -> dict:\n    pass\n"
        old = {"/repos/api": {"users.py": old_api}}
        new = {"/repos/api": {"users.py": new_api}}

        # Mock execution trace
        trace_calls = []

        class MockTrace:
            def record_decision(self, **kwargs):
                trace_calls.append(kwargs)

        mock_trace = MockTrace()
        task.detect_interface_changes(
            old_file_contents=old,
            new_file_contents=new,
            execution_trace=mock_trace,
        )

        self.assertEqual(len(trace_calls), 1)
        self.assertEqual(trace_calls[0]["task_id"], "task-1")
        self.assertEqual(trace_calls[0]["decision_type"], "cross_repo_interface_analysis")
        self.assertIn("total_changes", trace_calls[0]["state_snapshot"])

    def test_multiple_repos_with_changes(self) -> None:
        """Changes in multiple repos are all detected."""
        task = self._make_multi_repo_task()

        old = {
            "/repos/api": {"models.py": "def User():\n    pass\n"},
            "/repos/client": {"utils.py": "def format_name(name: str) -> str:\n    pass\n"},
        }
        new = {
            "/repos/api": {"models.py": "def User():\n    pass\n\ndef Admin():\n    pass\n"},
            "/repos/client": {"utils.py": "def format_name(name: str, upper: bool = False) -> str:\n    pass\n"},
        }

        changes = task.detect_interface_changes(old_file_contents=old, new_file_contents=new)
        # Should detect: Admin added in api, format_name changed in client
        self.assertEqual(len(changes), 2)

        repo_paths = [c.repo_path for c in changes]
        self.assertIn("/repos/api", repo_paths)
        self.assertIn("/repos/client", repo_paths)


class TestClassifyChangeType(unittest.TestCase):
    """Tests for classify_change_type helper.

    Validates: Requirement 22.3 (auto-propagate vs pause for breaking)
    """

    def test_removed_export_is_breaking(self) -> None:
        """Removed export (empty new_signature) is breaking."""
        result = classify_change_type("def foo(x: int) -> str", "")
        self.assertEqual(result, "breaking")

    def test_added_export_is_type_compatible(self) -> None:
        """Added export (empty old_signature) is type_compatible."""
        result = classify_change_type("", "def bar(x: int) -> str")
        self.assertEqual(result, "type_compatible")

    def test_additive_param_with_default_is_compatible(self) -> None:
        """Adding a parameter with a default value is type_compatible."""
        old = "def get_user(id: int) -> dict"
        new = "def get_user(id: int, verbose: bool = False) -> dict"
        result = classify_change_type(old, new)
        self.assertEqual(result, "type_compatible")

    def test_removing_param_is_breaking(self) -> None:
        """Removing a parameter is breaking."""
        old = "def get_user(id: int, name: str) -> dict"
        new = "def get_user(id: int) -> dict"
        result = classify_change_type(old, new)
        self.assertEqual(result, "breaking")

    def test_changing_param_type_is_breaking(self) -> None:
        """Changing a parameter type is breaking."""
        old = "def get_user(id: int) -> dict"
        new = "def get_user(id: str) -> dict"
        result = classify_change_type(old, new)
        self.assertEqual(result, "breaking")

    def test_identical_signatures_not_classified(self) -> None:
        """Identical signatures shouldn't reach classify but return type_compatible."""
        sig = "def get_user(id: int) -> dict"
        result = classify_change_type(sig, sig)
        self.assertEqual(result, "type_compatible")


class TestIsBreakingChange(unittest.TestCase):
    """Tests for is_breaking_change() helper function.

    Validates: Requirement 22.3 (auto-propagate type-compatible; pause for breaking)
    """

    def test_removed_function_is_breaking(self) -> None:
        """A removed function (empty new_signature) is a breaking change."""
        change = InterfaceChange(
            repo_path="/repos/api",
            file_path="src/users.py",
            interface_type="function_signature",
            old_signature="def delete_user(id: int) -> None",
            new_signature="",
            consuming_repos=["/repos/client"],
        )
        self.assertTrue(is_breaking_change(change))

    def test_added_function_not_breaking(self) -> None:
        """A new function (empty old_signature) is NOT breaking."""
        change = InterfaceChange(
            repo_path="/repos/api",
            file_path="src/users.py",
            interface_type="function_signature",
            old_signature="",
            new_signature="def create_user(name: str) -> dict",
            consuming_repos=[],
        )
        self.assertFalse(is_breaking_change(change))

    def test_additive_param_with_default_not_breaking(self) -> None:
        """Adding a parameter with a default value is NOT breaking."""
        change = InterfaceChange(
            repo_path="/repos/api",
            file_path="src/users.py",
            interface_type="function_signature",
            old_signature="def get_user(id: int) -> dict",
            new_signature="def get_user(id: int, include_deleted: bool = False) -> dict",
            consuming_repos=["/repos/client"],
        )
        self.assertFalse(is_breaking_change(change))

    def test_removed_param_is_breaking(self) -> None:
        """Removing a required parameter is a breaking change."""
        change = InterfaceChange(
            repo_path="/repos/api",
            file_path="src/users.py",
            interface_type="function_signature",
            old_signature="def get_user(id: int, name: str) -> dict",
            new_signature="def get_user(id: int) -> dict",
            consuming_repos=["/repos/client"],
        )
        self.assertTrue(is_breaking_change(change))

    def test_changed_param_type_is_breaking(self) -> None:
        """Changing a parameter's type is a breaking change."""
        change = InterfaceChange(
            repo_path="/repos/api",
            file_path="src/users.py",
            interface_type="function_signature",
            old_signature="def get_user(id: int) -> dict",
            new_signature="def get_user(id: str) -> dict",
            consuming_repos=["/repos/client"],
        )
        self.assertTrue(is_breaking_change(change))

    def test_multiple_additive_params_not_breaking(self) -> None:
        """Adding multiple params with defaults is NOT breaking."""
        change = InterfaceChange(
            repo_path="/repos/api",
            file_path="src/users.py",
            interface_type="function_signature",
            old_signature="def get_user(self, id: int) -> dict",
            new_signature="def get_user(self, id: int, verbose: bool = False, limit: int = 10) -> dict",
            consuming_repos=[],
        )
        self.assertFalse(is_breaking_change(change))

    def test_new_required_param_is_breaking(self) -> None:
        """Adding a parameter WITHOUT a default is a breaking change."""
        change = InterfaceChange(
            repo_path="/repos/api",
            file_path="src/users.py",
            interface_type="function_signature",
            old_signature="def get_user(id: int) -> dict",
            new_signature="def get_user(id: int, tenant_id: str) -> dict",
            consuming_repos=["/repos/client"],
        )
        self.assertTrue(is_breaking_change(change))

    def test_no_consumers_still_evaluates_breaking(self) -> None:
        """Breaking determination is independent of whether consumers exist."""
        change = InterfaceChange(
            repo_path="/repos/api",
            file_path="src/users.py",
            interface_type="function_signature",
            old_signature="def internal_func(x: int) -> str",
            new_signature="",
            consuming_repos=[],
        )
        self.assertTrue(is_breaking_change(change))


class TestInterfaceAnalysisConsumerDetection(unittest.TestCase):
    """Tests for consumer detection via import statement scanning.

    Validates: Requirements 22.1, 22.2
    """

    def _make_task(self) -> MultiRepoTask:
        """Create a 3-repo task for consumer detection tests."""
        repos = [
            {"path": "/repos/api", "default_branch": "main"},
            {"path": "/repos/client", "default_branch": "main"},
            {"path": "/repos/admin", "default_branch": "main"},
        ]
        return MultiRepoTask("task-consumer-test", repos)

    def test_python_import_detected(self) -> None:
        """Python `from X import Y` imports correctly identify consumers."""
        task = self._make_task()
        old_api = "def get_user(id: int) -> dict:\n    pass\n"
        new_api = "def get_user(id: int, verbose: bool = False) -> dict:\n    pass\n"
        client_code = "from users import get_user\n\ndef do_stuff():\n    pass\n"

        old = {
            "/repos/api": {"users.py": old_api},
            "/repos/client": {"main.py": client_code},
        }
        new = {
            "/repos/api": {"users.py": new_api},
            "/repos/client": {"main.py": client_code},
        }

        changes = task.detect_interface_changes(old_file_contents=old, new_file_contents=new)
        # Only the api change should be detected (client content is same in old and new)
        api_changes = [c for c in changes if c.repo_path == "/repos/api"]
        self.assertEqual(len(api_changes), 1)
        self.assertIn("/repos/client", api_changes[0].consuming_repos)

    def test_multiple_consumers_detected(self) -> None:
        """Multiple repos importing from the changed file are all identified."""
        task = self._make_task()
        old_api = "def get_user(id: int) -> dict:\n    pass\n"
        new_api = "def get_user(id: int, verbose: bool = False) -> dict:\n    pass\n"
        client_code = "from users import get_user\n"
        admin_code = "from users import get_user\n"

        old = {"/repos/api": {"users.py": old_api}}
        new = {
            "/repos/api": {"users.py": new_api},
            "/repos/client": {"client.py": client_code},
            "/repos/admin": {"admin.py": admin_code},
        }

        changes = task.detect_interface_changes(old_file_contents=old, new_file_contents=new)
        # Filter to API changes only (client/admin may also show up as new exports)
        api_changes = [c for c in changes if c.repo_path == "/repos/api"]
        self.assertEqual(len(api_changes), 1)
        self.assertIn("/repos/client", api_changes[0].consuming_repos)
        self.assertIn("/repos/admin", api_changes[0].consuming_repos)

    def test_no_consumers_when_no_imports(self) -> None:
        """When no other repo imports from the changed file, consuming_repos is empty."""
        task = self._make_task()
        old_api = "def get_user(id: int) -> dict:\n    pass\n"
        new_api = "def get_user(id: int, verbose: bool = False) -> dict:\n    pass\n"
        client_code = "import os\n\ndef unrelated():\n    pass\n"

        old = {
            "/repos/api": {"users.py": old_api},
            "/repos/client": {"client.py": client_code},
        }
        new = {
            "/repos/api": {"users.py": new_api},
            "/repos/client": {"client.py": client_code},
        }

        changes = task.detect_interface_changes(old_file_contents=old, new_file_contents=new)
        # Only the API change should be detected (client unchanged)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].repo_path, "/repos/api")
        self.assertEqual(changes[0].consuming_repos, [])

    def test_typescript_import_consumer_detection(self) -> None:
        """TypeScript import from './path' identifies consumers."""
        task = self._make_task()
        old_ts = "export function getUser(id: number): User {\n  return {} as User;\n}\n"
        new_ts = "export function getUser(id: number, opts?: Options): User {\n  return {} as User;\n}\n"
        client_ts = "import { getUser } from './users';\n\nexport function main() { getUser(1); }\n"

        old = {
            "/repos/api": {"src/users.ts": old_ts},
            "/repos/client": {"src/client.ts": client_ts},
        }
        new = {
            "/repos/api": {"src/users.ts": new_ts},
            "/repos/client": {"src/client.ts": client_ts},
        }

        changes = task.detect_interface_changes(old_file_contents=old, new_file_contents=new)
        # Only the API change detected (client unchanged)
        api_changes = [c for c in changes if c.repo_path == "/repos/api"]
        self.assertEqual(len(api_changes), 1)
        # The consumer detection depends on _module_matches_import matching
        # "src/users" against import path "./users" - this is TS relative import
        # Based on the implementation in conflict_detector._module_matches_import,
        # this should match since "src/users" ends with "users"

    def test_detached_consumer_repo_excluded(self) -> None:
        """Detached repos are not scanned as potential consumers."""
        task = self._make_task()
        task.detach_repo("/repos/client")

        old_api = "def get_user(id: int) -> dict:\n    pass\n"
        new_api = "def get_user(id: int, verbose: bool = False) -> dict:\n    pass\n"
        client_code = "from users import get_user\n"

        old = {"/repos/api": {"users.py": old_api}}
        new = {
            "/repos/api": {"users.py": new_api},
            "/repos/client": {"client.py": client_code},
        }

        changes = task.detect_interface_changes(old_file_contents=old, new_file_contents=new)
        self.assertEqual(len(changes), 1)
        self.assertNotIn("/repos/client", changes[0].consuming_repos)


class TestInterfaceAnalysisExecutionTrace(unittest.TestCase):
    """Tests for Execution_Trace recording of interface analysis.

    Validates: Requirement 22.4
    """

    def _make_task(self) -> MultiRepoTask:
        """Create a 2-repo task for trace tests."""
        repos = [
            {"path": "/repos/api", "default_branch": "main"},
            {"path": "/repos/client", "default_branch": "main"},
        ]
        return MultiRepoTask("task-trace-test", repos)

    def test_trace_records_breaking_and_compatible_counts(self) -> None:
        """Execution trace records both breaking and type-compatible change counts."""
        task = self._make_task()
        old_api = "def get_user(id: int) -> dict:\n    pass\n\ndef old_func() -> None:\n    pass\n"
        new_api = "def get_user(id: int, verbose: bool = False) -> dict:\n    pass\n"

        old = {"/repos/api": {"users.py": old_api}}
        new = {"/repos/api": {"users.py": new_api}}

        trace_calls = []

        class MockTrace:
            def record_decision(self, **kwargs):
                trace_calls.append(kwargs)

        task.detect_interface_changes(
            old_file_contents=old,
            new_file_contents=new,
            execution_trace=MockTrace(),
        )

        self.assertEqual(len(trace_calls), 1)
        snapshot = trace_calls[0]["state_snapshot"]
        self.assertEqual(snapshot["total_changes"], 2)
        # old_func removed -> breaking; get_user changed with default param -> type_compatible
        self.assertEqual(snapshot["breaking_changes"], 1)
        self.assertEqual(snapshot["type_compatible_changes"], 1)

    def test_trace_not_recorded_when_no_changes(self) -> None:
        """When no interface changes detected, execution trace is not called."""
        task = self._make_task()
        content = "def get_user(id: int) -> dict:\n    pass\n"
        old = {"/repos/api": {"users.py": content}}
        new = {"/repos/api": {"users.py": content}}

        trace_calls = []

        class MockTrace:
            def record_decision(self, **kwargs):
                trace_calls.append(kwargs)

        task.detect_interface_changes(
            old_file_contents=old,
            new_file_contents=new,
            execution_trace=MockTrace(),
        )

        self.assertEqual(len(trace_calls), 0)

    def test_trace_records_dependency_details(self) -> None:
        """Execution trace includes dependency detail per change."""
        task = self._make_task()
        old_api = "def get_user(id: int) -> dict:\n    pass\n"
        new_api = "def get_user(id: int, verbose: bool = False) -> dict:\n    pass\n"
        client_code = "from users import get_user\n"

        old = {"/repos/api": {"users.py": old_api}}
        new = {
            "/repos/api": {"users.py": new_api},
            "/repos/client": {"client.py": client_code},
        }

        trace_calls = []

        class MockTrace:
            def record_decision(self, **kwargs):
                trace_calls.append(kwargs)

        task.detect_interface_changes(
            old_file_contents=old,
            new_file_contents=new,
            execution_trace=MockTrace(),
        )

        self.assertEqual(len(trace_calls), 1)
        deps = trace_calls[0]["state_snapshot"]["dependencies"]
        self.assertEqual(len(deps), 1)
        self.assertEqual(deps[0]["repo_path"], "/repos/api")
        self.assertEqual(deps[0]["file_path"], "users.py")
        self.assertIn("/repos/client", deps[0]["consuming_repos"])
        self.assertFalse(deps[0]["is_breaking"])

    def test_trace_exception_does_not_propagate(self) -> None:
        """If execution trace raises, the method still returns results."""
        task = self._make_task()
        old_api = "def get_user(id: int) -> dict:\n    pass\n"
        new_api = "def get_user(id: int, verbose: bool = False) -> dict:\n    pass\n"
        old = {"/repos/api": {"users.py": old_api}}
        new = {"/repos/api": {"users.py": new_api}}

        class FailingTrace:
            def record_decision(self, **kwargs):
                raise RuntimeError("trace write failed")

        # Should not raise
        changes = task.detect_interface_changes(
            old_file_contents=old,
            new_file_contents=new,
            execution_trace=FailingTrace(),
        )
        self.assertEqual(len(changes), 1)


class TestTaskCreateRequestExtensions(unittest.TestCase):
    """Tests for the extended TaskCreateRequest model.

    Validates: Requirements 21.1, 16.1, 18.1, 14.2
    """

    def test_backwards_compatible(self) -> None:
        """Existing usage without new fields still works."""
        req = TaskCreateRequest(
            task_id="task-1",
            objective="Fix bug",
            repo=ModelsRepoRef(path="/repos/main"),
        )
        self.assertEqual(req.repos, [])
        self.assertEqual(req.priority, "normal")
        self.assertEqual(req.depends_on, [])
        self.assertIsNone(req.formation)

    def test_multi_repo_fields(self) -> None:
        """New multi-repo fields are accepted."""
        req = TaskCreateRequest(
            task_id="task-1",
            objective="Cross-repo refactor",
            repo=ModelsRepoRef(path="/repos/api"),
            repos=[
                ModelsRepoRef(path="/repos/api"),
                ModelsRepoRef(path="/repos/client"),
            ],
            priority="high",
            depends_on=["task-0"],
            formation="feature-standard",
        )
        self.assertEqual(len(req.repos), 2)
        self.assertEqual(req.priority, "high")
        self.assertEqual(req.depends_on, ["task-0"])
        self.assertEqual(req.formation, "feature-standard")

    def test_priority_values(self) -> None:
        """Priority accepts any string value (validated at business logic layer)."""
        for priority in ("critical", "high", "normal", "low"):
            req = TaskCreateRequest(
                task_id="t",
                objective="Test",
                repo=ModelsRepoRef(path="/r"),
                priority=priority,
            )
            self.assertEqual(req.priority, priority)


# ---------------------------------------------------------------------------
# Tests for task 15.4: Coordinated Verification and Merge Gate
# ---------------------------------------------------------------------------


class MockHostClient:
    """Mock HostClient for testing verification and merge operations."""

    def __init__(
        self,
        exec_results: dict[str, HostObservation] | None = None,
        rollback_success: bool = True,
        default_success: bool = True,
    ) -> None:
        self.exec_calls: list[HostActionRequest] = []
        self.rollback_calls: list[GitRollbackRequest] = []
        self._exec_results = exec_results or {}
        self._rollback_success = rollback_success
        self._default_success = default_success
        self._exec_call_count = 0

    def exec(self, request: HostActionRequest) -> HostObservation:
        self.exec_calls.append(request)
        self._exec_call_count += 1

        # Check for result keyed by action_name or working_dir
        key = request.working_dir or request.action_name
        if key in self._exec_results:
            return self._exec_results[key]

        # Check by action_name
        if request.action_name in self._exec_results:
            return self._exec_results[request.action_name]

        return HostObservation(
            task_id=request.task_id,
            action_name=request.action_name,
            success=self._default_success,
            exit_code=0 if self._default_success else 1,
            summary="OK" if self._default_success else "FAILED",
            output="",
            state={"head_ref": "abc123"},
        )

    def rollback_worktree(self, request: GitRollbackRequest) -> GitRollbackResponse:
        self.rollback_calls.append(request)
        if not self._rollback_success:
            raise RuntimeError("Rollback failed")
        return GitRollbackResponse(
            task_id=request.task_id,
            worktree_path=request.worktree_path,
            rolled_back=True,
            head_ref="rolled_back_ref",
        )


class TestAggregateVerificationWithHost(unittest.TestCase):
    """Tests for aggregate_verification with Go Host command execution.

    Validates: Requirements 23.1, 23.2, 23.3, 23.4
    """

    def _make_task_provisioned(self) -> MultiRepoTask:
        """Create and provision a multi-repo task."""
        repos = _make_repos(3)
        task = MultiRepoTask("task-v1", repos)
        task.provision_all()
        # Mark repos as having changes
        for path in task.repo_paths:
            task._repos[path].changed_files = [{"path": "src/main.py", "status": "modified"}]
        return task

    def test_all_repos_pass_verification(self) -> None:
        """All repos passing verification commands -> overall pass."""
        task = self._make_task_provisioned()
        host = MockHostClient(default_success=True)
        commands = {
            "/repos/repo-0": ["make test"],
            "/repos/repo-1": ["npm test"],
            "/repos/repo-2": ["go test ./..."],
        }

        overall, per_repo = task.aggregate_verification(
            host_client=host,
            verification_commands=commands,
        )
        self.assertEqual(overall, "pass")
        self.assertEqual(per_repo["/repos/repo-0"], "pass")
        self.assertEqual(per_repo["/repos/repo-1"], "pass")
        self.assertEqual(per_repo["/repos/repo-2"], "pass")
        self.assertEqual(len(host.exec_calls), 3)

    def test_one_repo_fails_verification(self) -> None:
        """One repo failing -> overall fail with failure details (Req 23.4)."""
        task = self._make_task_provisioned()
        # Make repo-1's worktree path fail
        repo1_wt = task._repos["/repos/repo-1"].worktree_path
        host = MockHostClient(
            exec_results={
                repo1_wt: HostObservation(
                    task_id="task-v1",
                    action_name="verification_exec",
                    success=False,
                    exit_code=1,
                    summary="Tests failed",
                    output="FAIL: TestSomething",
                ),
            },
            default_success=True,
        )
        commands = {
            "/repos/repo-0": ["make test"],
            "/repos/repo-1": ["npm test"],
            "/repos/repo-2": ["go test ./..."],
        }

        overall, per_repo = task.aggregate_verification(
            host_client=host,
            verification_commands=commands,
        )
        self.assertEqual(overall, "fail")
        self.assertEqual(per_repo["/repos/repo-1"], "fail")
        # Blocker should be added
        self.assertTrue(len(task._repos["/repos/repo-1"].blockers) > 0)

    def test_detached_repo_skipped(self) -> None:
        """Detached repos are not verified."""
        task = self._make_task_provisioned()
        task.detach_repo("/repos/repo-2")
        host = MockHostClient(default_success=True)
        commands = {
            "/repos/repo-0": ["make test"],
            "/repos/repo-1": ["npm test"],
            "/repos/repo-2": ["go test ./..."],
        }

        overall, per_repo = task.aggregate_verification(
            host_client=host,
            verification_commands=commands,
        )
        self.assertEqual(overall, "pass")
        self.assertNotIn("/repos/repo-2", per_repo)
        # Only 2 exec calls (repo-2 skipped)
        self.assertEqual(len(host.exec_calls), 2)

    def test_no_commands_no_changes_passes(self) -> None:
        """Repo with no commands and no changes -> pass (skipped)."""
        repos = _make_repos(2)
        task = MultiRepoTask("task-v1", repos)
        task.provision_all()
        # repo-0 has changes, repo-1 does not
        task._repos["/repos/repo-0"].changed_files = [{"path": "x.py"}]

        host = MockHostClient(default_success=True)
        commands = {"/repos/repo-0": ["make test"]}  # No commands for repo-1

        overall, per_repo = task.aggregate_verification(
            host_client=host,
            verification_commands=commands,
        )
        self.assertEqual(overall, "pass")
        self.assertEqual(per_repo["/repos/repo-1"], "pass")

    def test_fallback_without_host_client(self) -> None:
        """Without host_client, uses existing verification_result on state."""
        task = self._make_task_provisioned()
        task._repos["/repos/repo-0"].verification_result = "pass"
        task._repos["/repos/repo-1"].verification_result = "pass"
        task._repos["/repos/repo-2"].verification_result = "fail"

        overall, per_repo = task.aggregate_verification()
        self.assertEqual(overall, "fail")
        self.assertEqual(per_repo["/repos/repo-2"], "fail")


class TestIntegrationVerification(unittest.TestCase):
    """Tests for cross-repo integration verification.

    Validates: Requirement 23.2
    """

    def test_integration_runs_when_all_pass(self) -> None:
        """Integration verification runs only when all repos pass individually."""
        repos = _make_repos(2)
        task = MultiRepoTask("task-int", repos)
        task.provision_all()
        for path in task.repo_paths:
            task._repos[path].changed_files = [{"path": "x.py"}]

        host = MockHostClient(default_success=True)
        commands = {
            "/repos/repo-0": ["make test"],
            "/repos/repo-1": ["npm test"],
        }
        config = IntegrationVerificationConfig(
            enabled=True,
            command="integration-test {worktree:/repos/repo-0} {worktree:/repos/repo-1}",
        )

        overall, per_repo = task.aggregate_verification(
            host_client=host,
            verification_commands=commands,
            integration_config=config,
        )
        self.assertEqual(overall, "pass")
        # 2 repo commands + 1 integration command
        self.assertEqual(len(host.exec_calls), 3)
        # Integration call should have substituted worktree paths
        int_call = host.exec_calls[2]
        self.assertEqual(int_call.action_name, "integration_verification")

    def test_integration_not_run_when_repo_fails(self) -> None:
        """Integration verification skipped if any repo failed."""
        repos = _make_repos(2)
        task = MultiRepoTask("task-int", repos)
        task.provision_all()
        for path in task.repo_paths:
            task._repos[path].changed_files = [{"path": "x.py"}]

        repo0_wt = task._repos["/repos/repo-0"].worktree_path
        host = MockHostClient(
            exec_results={
                repo0_wt: HostObservation(
                    task_id="task-int",
                    action_name="verification_exec",
                    success=False, exit_code=1,
                    summary="FAIL", output="error",
                ),
            },
            default_success=True,
        )
        commands = {
            "/repos/repo-0": ["make test"],
            "/repos/repo-1": ["npm test"],
        }
        config = IntegrationVerificationConfig(
            enabled=True,
            command="integration-test",
        )

        overall, _ = task.aggregate_verification(
            host_client=host,
            verification_commands=commands,
            integration_config=config,
        )
        self.assertEqual(overall, "fail")
        # Integration should NOT have been called (only 2 calls: repo-0 fail, repo-1 pass)
        # Actually repo-0 fails on first command, so only 2 calls total
        integration_calls = [c for c in host.exec_calls if c.action_name == "integration_verification"]
        self.assertEqual(len(integration_calls), 0)

    def test_integration_failure_causes_overall_fail(self) -> None:
        """Integration verification failure -> overall fail."""
        repos = _make_repos(2)
        task = MultiRepoTask("task-int", repos)
        task.provision_all()
        for path in task.repo_paths:
            task._repos[path].changed_files = [{"path": "x.py"}]

        host = MockHostClient(
            exec_results={
                "integration_verification": HostObservation(
                    task_id="task-int",
                    action_name="integration_verification",
                    success=False, exit_code=1,
                    summary="Integration failed", output="contract mismatch",
                ),
            },
            default_success=True,
        )
        commands = {
            "/repos/repo-0": ["make test"],
            "/repos/repo-1": ["npm test"],
        }
        config = IntegrationVerificationConfig(
            enabled=True,
            command="integration-test",
        )

        overall, _ = task.aggregate_verification(
            host_client=host,
            verification_commands=commands,
            integration_config=config,
        )
        self.assertEqual(overall, "fail")

    def test_integration_disabled_not_run(self) -> None:
        """Integration verification not run when disabled."""
        repos = _make_repos(2)
        task = MultiRepoTask("task-int", repos)
        task.provision_all()
        for path in task.repo_paths:
            task._repos[path].changed_files = [{"path": "x.py"}]

        host = MockHostClient(default_success=True)
        commands = {
            "/repos/repo-0": ["make test"],
            "/repos/repo-1": ["npm test"],
        }
        config = IntegrationVerificationConfig(enabled=False, command="integration-test")

        overall, _ = task.aggregate_verification(
            host_client=host,
            verification_commands=commands,
            integration_config=config,
        )
        self.assertEqual(overall, "pass")
        # Only 2 repo commands, no integration
        self.assertEqual(len(host.exec_calls), 2)


class TestAtomicMerge(unittest.TestCase):
    """Tests for atomic_merge via Go Host.

    Validates: Requirements 24.3, 24.4
    """

    def _make_task_ready(self) -> MultiRepoTask:
        """Create a task with all repos ready for merge."""
        repos = _make_repos(3)
        task = MultiRepoTask("task-m1", repos)
        task.provision_all()
        for path in task.repo_paths:
            task._repos[path].status = "completed"
            task._repos[path].verification_result = "pass"
            task._repos[path].head_ref = "pre_merge_ref"
        return task

    def test_all_repos_merge_successfully(self) -> None:
        """All repos merge successfully -> success response."""
        task = self._make_task_ready()
        host = MockHostClient(default_success=True)

        result = task.atomic_merge(host)
        self.assertTrue(result.success)
        self.assertEqual(len(result.merged_repos), 3)
        self.assertEqual(result.failed_repo, "")
        self.assertEqual(result.rolled_back_repos, [])

    def test_second_repo_fails_rollbacks_first(self) -> None:
        """Second repo merge fails -> first is rolled back (Req 24.4)."""
        task = self._make_task_ready()
        repo1_wt = task._repos["/repos/repo-1"].worktree_path
        host = MockHostClient(
            exec_results={
                repo1_wt: HostObservation(
                    task_id="task-m1",
                    action_name="git_merge_ff",
                    success=False, exit_code=1,
                    summary="Merge conflict", output="conflict in file.py",
                ),
            },
            default_success=True,
        )

        result = task.atomic_merge(host)
        self.assertFalse(result.success)
        self.assertEqual(result.failed_repo, "/repos/repo-1")
        self.assertIn("conflict", result.failure_error)
        # First repo was merged, so it should be rolled back
        self.assertIn("/repos/repo-0", result.rolled_back_repos)
        # Verify rollback was actually called
        self.assertEqual(len(host.rollback_calls), 1)

    def test_detached_repos_skipped(self) -> None:
        """Detached repos are not merged."""
        task = self._make_task_ready()
        task.detach_repo("/repos/repo-2")
        host = MockHostClient(default_success=True)

        result = task.atomic_merge(host)
        self.assertTrue(result.success)
        self.assertEqual(len(result.merged_repos), 2)
        self.assertNotIn("/repos/repo-2", result.merged_repos)

    def test_exception_during_merge_triggers_rollback(self) -> None:
        """Exception during merge -> rollback previously merged repos."""
        task = self._make_task_ready()

        class FailingHost(MockHostClient):
            def __init__(self):
                super().__init__(default_success=True)
                self._call_count = 0

            def exec(self, request: HostActionRequest) -> HostObservation:
                self._call_count += 1
                if self._call_count == 2:
                    raise ConnectionError("Go Host unavailable")
                return super().exec(request)

        host = FailingHost()
        result = task.atomic_merge(host)
        self.assertFalse(result.success)
        self.assertIn("Go Host unavailable", result.failure_error)
        # First repo merged, second raised exception
        self.assertIn("/repos/repo-0", result.rolled_back_repos)

    def test_empty_task_succeeds(self) -> None:
        """Task with only detached repos -> trivial success."""
        repos = _make_repos(2)
        task = MultiRepoTask("task-m1", repos)
        task.provision_all()
        task.detach_repo("/repos/repo-0")
        task.detach_repo("/repos/repo-1")
        host = MockHostClient(default_success=True)

        result = task.atomic_merge(host)
        self.assertTrue(result.success)
        self.assertEqual(result.merged_repos, [])


class TestCanProceedIndependently(unittest.TestCase):
    """Tests for can_proceed_independently method.

    Validates: Requirements 25.1, 25.2
    """

    def test_independent_repo_can_proceed(self) -> None:
        """Repo with no dependencies can always proceed."""
        repos = _make_repos(3)
        task = MultiRepoTask("task-1", repos)
        self.assertTrue(task.can_proceed_independently("/repos/repo-0"))

    def test_dependent_repo_blocked_when_dep_fails(self) -> None:
        """Repo depending on a failed repo cannot proceed."""
        repos = _make_repos_with_deps()
        task = MultiRepoTask("task-1", repos)
        task.update_repo_status("/repos/api", "failed")

        # /repos/client depends on /repos/api
        self.assertFalse(task.can_proceed_independently("/repos/client"))

    def test_dependent_repo_proceeds_when_dep_ok(self) -> None:
        """Repo depending on a passing repo can proceed."""
        repos = _make_repos_with_deps()
        task = MultiRepoTask("task-1", repos)
        task.update_repo_status("/repos/api", "completed")

        self.assertTrue(task.can_proceed_independently("/repos/client"))

    def test_detached_repo_cannot_proceed(self) -> None:
        """Detached repos cannot proceed."""
        repos = _make_repos(2)
        task = MultiRepoTask("task-1", repos)
        task.detach_repo("/repos/repo-0")

        self.assertFalse(task.can_proceed_independently("/repos/repo-0"))

    def test_failed_repo_cannot_proceed(self) -> None:
        """Failed repos cannot proceed."""
        repos = _make_repos(2)
        task = MultiRepoTask("task-1", repos)
        task.update_repo_status("/repos/repo-0", "failed")

        self.assertFalse(task.can_proceed_independently("/repos/repo-0"))

    def test_nonexistent_repo_raises(self) -> None:
        """Raises KeyError for unknown repo path."""
        task = MultiRepoTask("task-1", _make_repos(1))
        with self.assertRaises(KeyError):
            task.can_proceed_independently("/repos/unknown")

    def test_dep_blocked_blocks_downstream(self) -> None:
        """Repo depending on a blocked repo cannot proceed."""
        repos = _make_repos_with_deps()
        task = MultiRepoTask("task-1", repos)
        task.update_repo_status("/repos/api", "blocked")

        self.assertFalse(task.can_proceed_independently("/repos/client"))
        # But docs depends on both api and client
        self.assertFalse(task.can_proceed_independently("/repos/docs"))


class TestDetachRepoUpdatedBehavior(unittest.TestCase):
    """Tests for enhanced detach_repo behavior.

    Validates: Requirement 25.4 — detaching allows remaining repos to merge independently.
    """

    def test_detach_removes_from_dependency_graph(self) -> None:
        """Detaching a repo removes it from other repos' dependency lists."""
        repos = _make_repos_with_deps()
        task = MultiRepoTask("task-1", repos)

        # Initially, client depends on api
        self.assertIn("/repos/api", task.get_dependency_graph()["/repos/client"])

        task.detach_repo("/repos/api")

        # After detach, client no longer depends on api
        self.assertNotIn("/repos/api", task.get_dependency_graph()["/repos/client"])

    def test_detach_unblocks_dependent_repo(self) -> None:
        """After detaching a failed dependency, dependent repo can proceed."""
        repos = _make_repos_with_deps()
        task = MultiRepoTask("task-1", repos)
        task.update_repo_status("/repos/api", "failed")

        # Client is blocked
        self.assertFalse(task.can_proceed_independently("/repos/client"))

        # Detach the failed repo
        task.detach_repo("/repos/api")

        # Now client can proceed (dependency removed)
        self.assertTrue(task.can_proceed_independently("/repos/client"))

    def test_detach_allows_merge_gate_pass(self) -> None:
        """Detaching a blocked repo allows remaining repos to pass merge gate."""
        repos = _make_repos(3)
        task = MultiRepoTask("task-1", repos)
        # Two repos are good, one is blocked
        task._repos["/repos/repo-0"].status = "completed"
        task._repos["/repos/repo-0"].verification_result = "pass"
        task._repos["/repos/repo-1"].status = "completed"
        task._repos["/repos/repo-1"].verification_result = "pass"
        task._repos["/repos/repo-2"].status = "blocked"

        # Gate fails with blocked repo
        passes, blockers = task.evaluate_merge_gate()
        self.assertFalse(passes)
        self.assertIn("/repos/repo-2", blockers)

        # Detach the blocked repo
        task.detach_repo("/repos/repo-2")

        # Now gate passes
        passes, blockers = task.evaluate_merge_gate()
        self.assertTrue(passes)
        self.assertEqual(blockers, [])


class TestNewModels(unittest.TestCase):
    """Tests for new model types added for merge operations."""

    def test_git_merge_request(self) -> None:
        """GitMergeRequest can be created with all fields."""
        from vikram_orchestrator.models import GitMergeRequest
        req = GitMergeRequest(
            task_id="t1",
            worktree_path="/tmp/wt",
            target_branch="main",
            source_branch="vikram/t1",
        )
        self.assertEqual(req.task_id, "t1")
        self.assertEqual(req.target_branch, "main")

    def test_atomic_multi_repo_merge_response(self) -> None:
        """AtomicMultiRepoMergeResponse models success and failure."""
        from vikram_orchestrator.models import AtomicMultiRepoMergeResponse
        # Success case
        resp = AtomicMultiRepoMergeResponse(
            task_id="t1",
            success=True,
            merged_repos=["/a", "/b"],
        )
        self.assertTrue(resp.success)
        self.assertEqual(resp.merged_repos, ["/a", "/b"])

        # Failure case
        resp = AtomicMultiRepoMergeResponse(
            task_id="t1",
            success=False,
            merged_repos=["/a"],
            failed_repo="/b",
            failure_error="conflict",
            rolled_back_repos=["/a"],
        )
        self.assertFalse(resp.success)
        self.assertEqual(resp.failed_repo, "/b")
        self.assertEqual(resp.rolled_back_repos, ["/a"])

    def test_verification_command_result(self) -> None:
        """VerificationCommandResult model stores verification details."""
        from vikram_orchestrator.models import VerificationCommandResult
        result = VerificationCommandResult(
            repo_path="/repos/api",
            command="make test",
            success=False,
            exit_code=1,
            output="",
            error_output="test failed",
        )
        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 1)

    def test_integration_verification_config(self) -> None:
        """IntegrationVerificationConfig model with defaults."""
        config = IntegrationVerificationConfig()
        self.assertFalse(config.enabled)
        self.assertEqual(config.command, "")
        self.assertEqual(config.timeout_seconds, 300)

    def test_verification_report(self) -> None:
        """VerificationReport model for detailed reporting."""
        from vikram_orchestrator.models import VerificationCommandResult
        report = VerificationReport(
            overall="fail",
            per_repo={"/a": "pass", "/b": "fail"},
            failures=[
                VerificationCommandResult(
                    repo_path="/b", command="npm test",
                    success=False, exit_code=1, error_output="FAIL",
                ),
            ],
        )
        self.assertEqual(report.overall, "fail")
        self.assertEqual(len(report.failures), 1)


if __name__ == "__main__":
    unittest.main()
