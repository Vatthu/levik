"""Integration tests for Multi-Repository coordination.

Tests the coordination logic of the MultiRepoTask class end-to-end,
verifying that provisioning, verification aggregation, merge gate evaluation,
and repo detachment all work together correctly.

Validates: Requirements 23.3, 24.1, 25.4
"""

from __future__ import annotations

from typing import Any

import pytest

from vikram_orchestrator.multi_repo import (
    MergeGateCondition,
    MultiRepoTask,
    RepoRef,
    aggregate_verification,
    evaluate_merge_gate,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_repos(count: int = 3) -> list[dict[str, Any]]:
    """Create a list of repo dicts for testing."""
    return [
        {"path": f"/repos/repo-{i}", "default_branch": "main"}
        for i in range(count)
    ]


def _make_repos_with_deps() -> list[dict[str, Any]]:
    """Create repos with dependency relationships for integration scenarios."""
    return [
        {"path": "/repos/api", "default_branch": "main", "depends_on": []},
        {"path": "/repos/client", "default_branch": "main", "depends_on": ["/repos/api"]},
        {"path": "/repos/docs", "default_branch": "main", "depends_on": ["/repos/api"]},
        {"path": "/repos/admin", "default_branch": "main", "depends_on": ["/repos/api", "/repos/client"]},
    ]


class MockExecutionTrace:
    """Records decisions for verifying trace integration."""

    def __init__(self) -> None:
        self.decisions: list[dict[str, Any]] = []

    def record_decision(self, **kwargs: Any) -> None:
        self.decisions.append(kwargs)


# ---------------------------------------------------------------------------
# Integration: Multi-Repo Provision and Independent State Tracking
# ---------------------------------------------------------------------------


class TestMultiRepoProvisionAndStateTracking:
    """Integration tests for provisioning multiple repos and tracking their states independently.

    Validates: Requirements 23.3, 24.1
    """

    def test_provision_all_repos_get_independent_state(self) -> None:
        """After provisioning, each repo has its own independent state that
        can be modified without affecting other repos."""
        repos = _make_repos(4)
        task = MultiRepoTask("task-provision-1", repos)
        provisioned = task.provision_all()

        # Each repo gets a unique worktree path
        worktree_paths = [s.worktree_path for s in provisioned.values()]
        assert len(set(worktree_paths)) == 4

        # Update one repo's status without affecting others
        task.update_repo_status("/repos/repo-0", "in_progress")
        task.update_repo_status("/repos/repo-1", "completed")

        assert task.get_repo_state("/repos/repo-0").status == "in_progress"
        assert task.get_repo_state("/repos/repo-1").status == "completed"
        assert task.get_repo_state("/repos/repo-2").status == "pending"
        assert task.get_repo_state("/repos/repo-3").status == "pending"

    def test_provision_then_progress_through_lifecycle(self) -> None:
        """Repos can independently progress through the full lifecycle:
        pending -> in_progress -> completed."""
        repos = _make_repos(3)
        task = MultiRepoTask("task-lifecycle-1", repos)
        task.provision_all()

        # All start as pending
        for path in task.repo_paths:
            assert task.get_repo_state(path).status == "pending"

        # Progress repo-0 to in_progress, then completed
        task.update_repo_status("/repos/repo-0", "in_progress")
        task.update_repo_status("/repos/repo-0", "completed")
        task._repos["/repos/repo-0"].verification_result = "pass"

        # repo-1 gets stuck
        task.update_repo_status("/repos/repo-1", "in_progress")
        task.update_repo_status("/repos/repo-1", "blocked")
        task._repos["/repos/repo-1"].blockers = ["failing tests"]

        # repo-2 is still pending
        assert task.get_repo_state("/repos/repo-0").status == "completed"
        assert task.get_repo_state("/repos/repo-1").status == "blocked"
        assert task.get_repo_state("/repos/repo-2").status == "pending"

    def test_provision_assigns_task_specific_branches(self) -> None:
        """Provisioned repos get branches that include the task ID for isolation."""
        task = MultiRepoTask("feature-xyz-123", _make_repos(2))
        provisioned = task.provision_all()

        for state in provisioned.values():
            assert "feature-xyz-123" in state.branch

    def test_provision_with_dependencies_tracks_graph(self) -> None:
        """Repos with dependencies have their graph accessible after provisioning."""
        repos = _make_repos_with_deps()
        task = MultiRepoTask("task-deps-1", repos)
        task.provision_all()

        graph = task.get_dependency_graph()
        assert graph["/repos/api"] == []
        assert "/repos/api" in graph["/repos/client"]
        assert "/repos/api" in graph["/repos/admin"]
        assert "/repos/client" in graph["/repos/admin"]


# ---------------------------------------------------------------------------
# Integration: Verification Aggregation (one repo fails → task fails)
# ---------------------------------------------------------------------------


class TestVerificationAggregationIntegration:
    """Integration tests verifying that verification aggregation correctly
    reports failure when any single repo fails verification.

    Validates: Requirement 23.3 — aggregate verification results across all
    repositories into a unified pass/fail outcome with per-repository detail.
    """

    def test_all_repos_pass_verification_after_provision(self) -> None:
        """Full flow: provision -> set verification results -> aggregate passes."""
        task = MultiRepoTask("task-verify-1", _make_repos(3))
        task.provision_all()

        # Simulate all repos completing verification successfully
        for path in task.repo_paths:
            task.update_repo_status(path, "in_progress")
            task._repos[path].verification_result = "pass"

        overall, per_repo = task.aggregate_verification()
        assert overall == "pass"
        assert all(r == "pass" for r in per_repo.values())
        assert len(per_repo) == 3

    def test_one_repo_fails_verification_whole_task_fails(self) -> None:
        """If one repo's verification fails, the overall task verification fails.
        Validates Requirement 23.3: unified pass/fail outcome."""
        task = MultiRepoTask("task-verify-2", _make_repos(3))
        task.provision_all()

        # Two repos pass, one fails
        task._repos["/repos/repo-0"].verification_result = "pass"
        task._repos["/repos/repo-1"].verification_result = "fail"
        task._repos["/repos/repo-2"].verification_result = "pass"

        overall, per_repo = task.aggregate_verification()
        assert overall == "fail"
        assert per_repo["/repos/repo-0"] == "pass"
        assert per_repo["/repos/repo-1"] == "fail"
        assert per_repo["/repos/repo-2"] == "pass"

    def test_unverified_repos_cause_failure(self) -> None:
        """Repos that haven't been verified (None result) cause overall failure."""
        task = MultiRepoTask("task-verify-3", _make_repos(3))
        task.provision_all()

        task._repos["/repos/repo-0"].verification_result = "pass"
        # repo-1 and repo-2 have no verification result (None)

        overall, per_repo = task.aggregate_verification()
        assert overall == "fail"
        assert per_repo["/repos/repo-0"] == "pass"
        assert per_repo["/repos/repo-1"] == "not_run"
        assert per_repo["/repos/repo-2"] == "not_run"

    def test_verification_aggregation_excludes_detached_repos(self) -> None:
        """Detached repos are excluded from verification aggregation,
        so remaining repos determine the outcome."""
        task = MultiRepoTask("task-verify-4", _make_repos(3))
        task.provision_all()

        task._repos["/repos/repo-0"].verification_result = "pass"
        task._repos["/repos/repo-1"].verification_result = "fail"
        task._repos["/repos/repo-2"].verification_result = "pass"

        # Detach the failing repo
        task.detach_repo("/repos/repo-1")

        overall, per_repo = task.aggregate_verification()
        assert overall == "pass"
        assert "/repos/repo-1" not in per_repo
        assert per_repo["/repos/repo-0"] == "pass"
        assert per_repo["/repos/repo-2"] == "pass"

    def test_standalone_aggregate_verification_one_failure(self) -> None:
        """The standalone aggregate_verification function also fails on one failure."""
        results = {
            "/repos/api": "pass",
            "/repos/client": "fail",
            "/repos/docs": "pass",
        }
        overall, per_repo = aggregate_verification(results)
        assert overall == "fail"
        assert per_repo["/repos/client"] == "fail"


# ---------------------------------------------------------------------------
# Integration: Merge Gate Conjunction (all must pass)
# ---------------------------------------------------------------------------


class TestMergeGateConjunctionIntegration:
    """Integration tests verifying that the merge gate requires ALL repos to pass.

    Validates: Requirement 24.1 — Merge_Gate passes only when ALL constituent
    repositories satisfy their individual gate conditions.
    """

    def test_all_repos_satisfy_gate_conditions(self) -> None:
        """When all repos are completed with passing verification and no blockers,
        the merge gate passes."""
        task = MultiRepoTask("task-gate-1", _make_repos(3))
        task.provision_all()

        for path in task.repo_paths:
            task.update_repo_status(path, "completed")
            task._repos[path].verification_result = "pass"

        passes, blockers = task.evaluate_merge_gate()
        assert passes is True
        assert blockers == []

    def test_one_repo_incomplete_blocks_merge_gate(self) -> None:
        """If any repo is not completed, the merge gate fails."""
        task = MultiRepoTask("task-gate-2", _make_repos(3))
        task.provision_all()

        task.update_repo_status("/repos/repo-0", "completed")
        task._repos["/repos/repo-0"].verification_result = "pass"

        task.update_repo_status("/repos/repo-1", "completed")
        task._repos["/repos/repo-1"].verification_result = "pass"

        # repo-2 is still in_progress
        task.update_repo_status("/repos/repo-2", "in_progress")
        task._repos["/repos/repo-2"].verification_result = "pass"

        passes, blockers = task.evaluate_merge_gate()
        assert passes is False
        assert "/repos/repo-2" in blockers

    def test_one_repo_verification_failed_blocks_merge_gate(self) -> None:
        """Failed verification in one repo blocks the entire merge gate."""
        task = MultiRepoTask("task-gate-3", _make_repos(3))
        task.provision_all()

        for path in task.repo_paths:
            task.update_repo_status(path, "completed")
            task._repos[path].verification_result = "pass"

        # Override one repo to have failed verification
        task._repos["/repos/repo-1"].verification_result = "fail"

        passes, blockers = task.evaluate_merge_gate()
        assert passes is False
        assert "/repos/repo-1" in blockers

    def test_repo_with_blockers_blocks_merge_gate(self) -> None:
        """A repo with explicit blockers prevents the merge gate from passing."""
        task = MultiRepoTask("task-gate-4", _make_repos(2))
        task.provision_all()

        for path in task.repo_paths:
            task.update_repo_status(path, "completed")
            task._repos[path].verification_result = "pass"

        task._repos["/repos/repo-0"].blockers = ["conflict with main branch"]

        passes, blockers = task.evaluate_merge_gate()
        assert passes is False
        assert "/repos/repo-0" in blockers

    def test_merge_gate_with_explicit_conditions_conjunction(self) -> None:
        """Using MergeGateCondition objects: ALL must pass for gate to pass."""
        task = MultiRepoTask("task-gate-5", _make_repos(3))
        task.provision_all()

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
                verification_passed=True,
                review_approved=True,
                conflict_free=True,
                governance_cleared=True,
            ),
            "/repos/repo-2": MergeGateCondition(
                repo_path="/repos/repo-2",
                verification_passed=True,
                review_approved=False,  # review not approved
                conflict_free=True,
                governance_cleared=True,
            ),
        }

        passes, blockers = task.evaluate_merge_gate(conditions)
        assert passes is False
        assert "/repos/repo-2" in blockers
        assert "/repos/repo-0" not in blockers
        assert "/repos/repo-1" not in blockers

    def test_merge_gate_all_conditions_satisfied(self) -> None:
        """When all explicit conditions pass, gate passes."""
        task = MultiRepoTask("task-gate-6", _make_repos(2))
        task.provision_all()

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
                verification_passed=True,
                review_approved=True,
                conflict_free=True,
                governance_cleared=True,
            ),
        }

        passes, blockers = task.evaluate_merge_gate(conditions)
        assert passes is True
        assert blockers == []

    def test_standalone_evaluate_merge_gate_conjunction(self) -> None:
        """The standalone evaluate_merge_gate function enforces conjunction."""
        conditions = {
            "/repos/api": MergeGateCondition(
                repo_path="/repos/api",
                verification_passed=True,
                review_approved=True,
                conflict_free=True,
                governance_cleared=True,
            ),
            "/repos/client": MergeGateCondition(
                repo_path="/repos/client",
                verification_passed=True,
                review_approved=True,
                conflict_free=False,  # conflict present
                governance_cleared=True,
            ),
        }

        passes, blockers = evaluate_merge_gate(conditions)
        assert passes is False
        assert "/repos/client" in blockers


# ---------------------------------------------------------------------------
# Integration: Repo Detachment Allows Remaining Repos to Proceed
# ---------------------------------------------------------------------------


class TestRepoDetachmentIntegration:
    """Integration tests verifying that detaching a blocked repo allows
    the remaining repos to proceed through verification and merge gate.

    Validates: Requirement 25.4 — founder can detach a blocked repository,
    allowing remaining repositories to proceed to merge independently.
    """

    def test_detach_blocked_repo_unblocks_verification(self) -> None:
        """Detaching a repo that failed verification allows the remaining
        repos to show an overall 'pass' result."""
        task = MultiRepoTask("task-detach-1", _make_repos(3))
        task.provision_all()

        task._repos["/repos/repo-0"].verification_result = "pass"
        task._repos["/repos/repo-1"].verification_result = "fail"
        task._repos["/repos/repo-2"].verification_result = "pass"

        # Before detachment: overall fails
        overall_before, _ = task.aggregate_verification()
        assert overall_before == "fail"

        # Detach the failing repo
        task.detach_repo("/repos/repo-1")

        # After detachment: remaining repos pass
        overall_after, per_repo = task.aggregate_verification()
        assert overall_after == "pass"
        assert "/repos/repo-1" not in per_repo

    def test_detach_blocked_repo_unblocks_merge_gate(self) -> None:
        """Detaching a repo that blocks the merge gate allows remaining
        repos to pass the gate."""
        task = MultiRepoTask("task-detach-2", _make_repos(3))
        task.provision_all()

        # Complete all repos
        for path in task.repo_paths:
            task.update_repo_status(path, "completed")
            task._repos[path].verification_result = "pass"

        # Block one repo
        task._repos["/repos/repo-2"].blockers = ["merge conflict"]

        # Before detachment: gate fails
        passes_before, blockers_before = task.evaluate_merge_gate()
        assert passes_before is False
        assert "/repos/repo-2" in blockers_before

        # Detach the blocked repo
        task.detach_repo("/repos/repo-2")

        # After detachment: gate passes with remaining repos
        passes_after, blockers_after = task.evaluate_merge_gate()
        assert passes_after is True
        assert blockers_after == []

    def test_detach_repo_with_conditions_unblocks_gate(self) -> None:
        """Detaching a repo excluded from explicit MergeGateCondition evaluation."""
        task = MultiRepoTask("task-detach-3", _make_repos(3))
        task.provision_all()

        # Detach repo-1 before evaluating with conditions
        task.detach_repo("/repos/repo-1")

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
                verification_passed=False,  # would block if not detached
                review_approved=False,
                conflict_free=False,
                governance_cleared=False,
            ),
            "/repos/repo-2": MergeGateCondition(
                repo_path="/repos/repo-2",
                verification_passed=True,
                review_approved=True,
                conflict_free=True,
                governance_cleared=True,
            ),
        }

        passes, blockers = task.evaluate_merge_gate(conditions)
        assert passes is True
        assert blockers == []

    def test_detach_multiple_repos_remaining_proceed(self) -> None:
        """Detaching multiple repos still allows the remaining one to proceed."""
        task = MultiRepoTask("task-detach-4", _make_repos(4))
        task.provision_all()

        # Only repo-0 completes successfully
        task.update_repo_status("/repos/repo-0", "completed")
        task._repos["/repos/repo-0"].verification_result = "pass"

        # Others are blocked/failed
        task.update_repo_status("/repos/repo-1", "blocked")
        task.update_repo_status("/repos/repo-2", "failed")
        task.update_repo_status("/repos/repo-3", "blocked")

        # Detach all blocked/failed repos
        task.detach_repo("/repos/repo-1")
        task.detach_repo("/repos/repo-2")
        task.detach_repo("/repos/repo-3")

        # Remaining repo-0 passes both verification and merge gate
        overall, per_repo = task.aggregate_verification()
        assert overall == "pass"
        assert list(per_repo.keys()) == ["/repos/repo-0"]

        passes, blockers = task.evaluate_merge_gate()
        assert passes is True
        assert blockers == []

    def test_detached_repo_not_counted_as_consumer_in_interface_analysis(self) -> None:
        """A detached repo is excluded from cross-repo interface analysis."""
        repos = [
            {"path": "/repos/api", "default_branch": "main"},
            {"path": "/repos/client", "default_branch": "main"},
            {"path": "/repos/admin", "default_branch": "main"},
        ]
        task = MultiRepoTask("task-detach-5", repos)
        task.provision_all()

        # Detach the client repo
        task.detach_repo("/repos/client")

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

        changes = task.detect_interface_changes(
            old_file_contents=old, new_file_contents=new
        )
        assert len(changes) == 1
        # Detached client should NOT be in consuming_repos
        assert "/repos/client" not in changes[0].consuming_repos
        # Admin should still be detected
        assert "/repos/admin" in changes[0].consuming_repos


# ---------------------------------------------------------------------------
# Integration: Full Lifecycle Coordination Scenario
# ---------------------------------------------------------------------------


class TestFullLifecycleCoordination:
    """End-to-end integration tests simulating realistic multi-repo workflows.

    Validates: Requirements 23.3, 24.1, 25.4 (combined)
    """

    def test_full_success_flow(self) -> None:
        """Simulate a complete successful multi-repo task:
        provision -> implement -> verify -> merge gate pass."""
        task = MultiRepoTask("task-full-1", _make_repos_with_deps())
        provisioned = task.provision_all()
        assert len(provisioned) == 4

        # Implementation phase
        for path in task.repo_paths:
            task.update_repo_status(path, "in_progress")

        # All repos complete successfully
        for path in task.repo_paths:
            task.update_repo_status(path, "completed")
            task._repos[path].verification_result = "pass"

        # Verification aggregation passes
        overall, per_repo = task.aggregate_verification()
        assert overall == "pass"
        assert len(per_repo) == 4

        # Merge gate passes
        passes, blockers = task.evaluate_merge_gate()
        assert passes is True
        assert blockers == []

    def test_partial_failure_with_detachment_recovery(self) -> None:
        """Simulate a multi-repo task where one repo fails and is detached,
        allowing the remaining repos to proceed."""
        repos = _make_repos_with_deps()
        task = MultiRepoTask("task-partial-1", repos)
        task.provision_all()

        # Implementation phase
        for path in task.repo_paths:
            task.update_repo_status(path, "in_progress")

        # api and docs complete, client fails, admin blocked by client
        task.update_repo_status("/repos/api", "completed")
        task._repos["/repos/api"].verification_result = "pass"

        task.update_repo_status("/repos/docs", "completed")
        task._repos["/repos/docs"].verification_result = "pass"

        task.update_repo_status("/repos/client", "failed")
        task._repos["/repos/client"].verification_result = "fail"

        task.update_repo_status("/repos/admin", "blocked")
        task._repos["/repos/admin"].blockers = ["depends on /repos/client"]

        # Overall verification fails
        overall, _ = task.aggregate_verification()
        assert overall == "fail"

        # Merge gate also fails
        passes, blockers = task.evaluate_merge_gate()
        assert passes is False
        assert "/repos/client" in blockers
        assert "/repos/admin" in blockers

        # Detach the blocked repos
        task.detach_repo("/repos/client")
        task.detach_repo("/repos/admin")

        # Now remaining repos (api, docs) pass verification
        overall, per_repo = task.aggregate_verification()
        assert overall == "pass"
        assert set(per_repo.keys()) == {"/repos/api", "/repos/docs"}

        # And pass merge gate
        passes, blockers = task.evaluate_merge_gate()
        assert passes is True
        assert blockers == []

    def test_verification_failure_blocks_merge_gate_even_if_completed(self) -> None:
        """A completed repo that fails verification still blocks the merge gate.
        Verifies the conjunction: completion AND verification AND no blockers."""
        task = MultiRepoTask("task-full-2", _make_repos(2))
        task.provision_all()

        # Both repos are "completed" but one has failed verification
        for path in task.repo_paths:
            task.update_repo_status(path, "completed")

        task._repos["/repos/repo-0"].verification_result = "pass"
        task._repos["/repos/repo-1"].verification_result = "fail"

        # Verification aggregation correctly reports failure
        overall, per_repo = task.aggregate_verification()
        assert overall == "fail"

        # Merge gate also fails
        passes, blockers = task.evaluate_merge_gate()
        assert passes is False
        assert "/repos/repo-1" in blockers

    def test_interface_change_detection_with_trace_recording(self) -> None:
        """Full flow of interface analysis with execution trace recording."""
        repos = [
            {"path": "/repos/api", "default_branch": "main"},
            {"path": "/repos/client", "default_branch": "main"},
        ]
        task = MultiRepoTask("task-interface-1", repos)
        task.provision_all()

        old_api = "def get_user(id: int) -> dict:\n    pass\n"
        new_api = "def get_user(id: int, verbose: bool = False) -> dict:\n    pass\n"
        client_code = "from users import get_user\n\ndef main():\n    pass\n"

        old = {
            "/repos/api": {"users.py": old_api},
            "/repos/client": {"main.py": client_code},
        }
        new = {
            "/repos/api": {"users.py": new_api},
            "/repos/client": {"main.py": client_code},
        }

        trace = MockExecutionTrace()
        changes = task.detect_interface_changes(
            old_file_contents=old,
            new_file_contents=new,
            execution_trace=trace,
        )

        # Interface change detected
        assert len(changes) == 1
        assert changes[0].repo_path == "/repos/api"
        assert "/repos/client" in changes[0].consuming_repos

        # Trace recorded
        assert len(trace.decisions) == 1
        assert trace.decisions[0]["decision_type"] == "cross_repo_interface_analysis"
        snapshot = trace.decisions[0]["state_snapshot"]
        assert snapshot["total_changes"] == 1
        assert snapshot["type_compatible_changes"] == 1
        assert snapshot["breaking_changes"] == 0
