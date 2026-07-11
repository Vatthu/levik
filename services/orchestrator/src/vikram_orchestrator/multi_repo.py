"""Multi-repository task state management.

Provides models and logic for coordinating tasks that span multiple repositories,
including per-repo state tracking, dependency graphs, merge gate evaluation,
worktree provisioning, cross-repository interface contract analysis,
coordinated verification, and atomic merge operations via Go Host.

Validates: Requirements 21.1, 21.2, 21.3, 22.1, 22.2, 22.3, 22.4, 23.1, 23.2, 23.3, 23.4,
           24.1, 24.2, 24.3, 24.4, 25.1, 25.2, 25.3, 25.4
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from pydantic import BaseModel, Field

from .conflict_detector import extract_exported_interfaces, extract_imports, _module_matches_import
from .models import (
    RepoRef as _ModelsRepoRef,  # noqa: F401
    GitMergeRequest,
    GitMergeResponse,
    AtomicMultiRepoMergeRequest,
    AtomicMultiRepoMergeResponse,
    VerificationCommandResult,
    HostActionRequest,
    HostObservation,
    GitRollbackRequest,
)

if TYPE_CHECKING:
    from .host_client import HostClient

logger = logging.getLogger(__name__)


class RepoRef(BaseModel):
    """Reference to a repository within a multi-repo task.

    Supports both 'repo_path' (used in multi-repo context) and 'path' for compat.
    """

    repo_path: str = ""
    path: str = ""
    default_branch: str = "main"

    def model_post_init(self, __context: Any) -> None:
        """Ensure both path and repo_path are populated."""
        if self.repo_path and not self.path:
            object.__setattr__(self, "path", self.repo_path)
        elif self.path and not self.repo_path:
            object.__setattr__(self, "repo_path", self.path)


# Valid status values for RepoState
VALID_STATUSES = frozenset(
    {"pending", "in_progress", "completed", "blocked", "failed", "detached"}
)


class RepoState(BaseModel):
    """Per-repository state within a multi-repo task."""

    repo_path: str
    worktree_path: str = ""
    branch: str = ""
    head_ref: str = ""
    dirty: bool = False
    status: str = "pending"  # "pending", "in_progress", "completed", "blocked", "failed", "detached"
    changed_files: list[dict[str, Any]] = Field(default_factory=list)
    verification_result: str | None = None
    blockers: list[str] = Field(default_factory=list)


class InterfaceChange(BaseModel):
    """Represents a detected change to an exported interface in one repo."""

    repo_path: str
    file_path: str
    interface_type: str  # "function_signature", "api_schema", "shared_type"
    old_signature: str
    new_signature: str
    consuming_repos: list[str] = Field(default_factory=list)


class MergeGateCondition(BaseModel):
    """Per-repo merge gate condition result."""

    repo_path: str
    verification_passed: bool = False
    review_approved: bool = False
    conflict_free: bool = False
    governance_cleared: bool = False

    @property
    def passes(self) -> bool:
        """A repo passes the merge gate when ALL conditions are met."""
        return (
            self.verification_passed
            and self.review_approved
            and self.conflict_free
            and self.governance_cleared
        )


class IntegrationVerificationConfig(BaseModel):
    """Configuration for cross-repository integration verification.

    Validates: Requirement 23.2
    """

    enabled: bool = False
    command: str = ""
    # Placeholder tokens: {worktree:<repo_path>} will be replaced with actual worktree paths
    timeout_seconds: int = 300


class VerificationReport(BaseModel):
    """Detailed verification report for a multi-repo task.

    Validates: Requirements 23.3, 23.4
    """

    overall: str  # "pass" or "fail"
    per_repo: dict[str, str] = Field(default_factory=dict)
    failures: list[VerificationCommandResult] = Field(default_factory=list)
    integration_result: VerificationCommandResult | None = None


class MultiRepoTask:
    """Manages state for a task spanning multiple repositories.

    Tracks per-repo state, provisions worktrees via the Go Host,
    maintains a dependency graph, and evaluates the coordinated merge gate.
    """

    def __init__(self, task_id: str, repos: list[dict[str, Any]] | list[RepoRef]) -> None:
        """Initialize multi-repo task with per-repo state tracking.

        Args:
            task_id: Unique identifier for the parent task.
            repos: List of repo dicts (each with 'path', optionally 'default_branch',
                   'depends_on') OR list of RepoRef objects.
        """
        self.task_id = task_id
        self._repos: dict[str, RepoState] = {}
        self._dependencies: dict[str, list[str]] = {}
        self.repo_states: dict[str, RepoState] = self._repos  # alias for compat

        for repo in repos:
            if isinstance(repo, (RepoRef, _ModelsRepoRef)):
                path = getattr(repo, "repo_path", None) or getattr(repo, "path", "")
                branch = repo.default_branch
                depends_on: list[str] = []
            else:
                path = repo["path"]
                branch = repo.get("default_branch", "main")
                depends_on = repo.get("depends_on", [])

            self._repos[path] = RepoState(
                repo_path=path,
                worktree_path="",
                branch=branch,
                head_ref="",
                dirty=False,
                status="pending",
            )
            self._dependencies[path] = depends_on

    @property
    def repo_paths(self) -> list[str]:
        """Return all tracked repository paths."""
        return list(self._repos.keys())

    def provision_all(self) -> dict[str, RepoState]:
        """Request the Go Host to provision worktrees for all repositories.

        This is a stub that simulates worktree provisioning. In production,
        this would call the Go Host's workspace provisioning endpoint for
        each repository.

        Returns:
            Dictionary mapping repo_path -> RepoState with worktree_path populated.
        """
        for path, state in self._repos.items():
            # In production: call Go Host POST /v1/workspaces/provision
            # and POST /v1/git/worktrees/create for each repo.
            # Stub: generate a deterministic worktree path.
            worktree_path = f"/tmp/vikram/{self.task_id}/worktrees/{path.replace('/', '_')}"
            branch = f"vikram/{self.task_id}"
            state.worktree_path = worktree_path
            state.branch = branch
            state.head_ref = ""  # Would be populated by git response
            state.status = "pending"

        return dict(self._repos)

    def get_repo_state(self, repo_path: str) -> RepoState:
        """Get the current state for a specific repository.

        Args:
            repo_path: Path to the repository.

        Returns:
            The RepoState for the given repo.

        Raises:
            KeyError: If repo_path is not tracked by this task.
        """
        if repo_path not in self._repos:
            raise KeyError(f"Repository '{repo_path}' is not part of this task")
        return self._repos[repo_path]

    def update_repo_status(self, repo_path: str, status: str) -> None:
        """Update the status of a specific repository.

        Args:
            repo_path: Path to the repository.
            status: New status value. Must be one of: pending, in_progress,
                    completed, blocked, failed, detached.

        Raises:
            KeyError: If repo_path is not tracked by this task.
            ValueError: If status is not a valid value.
        """
        if repo_path not in self._repos:
            raise KeyError(f"Repository '{repo_path}' is not part of this task")
        if status not in VALID_STATUSES:
            raise ValueError(
                f"Invalid status '{status}'. Must be one of: {sorted(VALID_STATUSES)}"
            )
        self._repos[repo_path].status = status

    def get_dependency_graph(self) -> dict[str, list[str]]:
        """Return the dependency graph for repositories in this task.

        Returns:
            Dictionary mapping each repo_path to a list of repo_paths it depends on.
        """
        return dict(self._dependencies)

    def aggregate_verification(
        self,
        host_client: "HostClient | None" = None,
        verification_commands: dict[str, list[str]] | None = None,
        integration_config: IntegrationVerificationConfig | None = None,
    ) -> tuple[str, dict[str, str]]:
        """Aggregate verification results across all repositories.

        If host_client and verification_commands are provided, executes per-repo
        verification commands in each worktree that has changes (Req 23.1).
        When integration_config is enabled, also runs cross-repo integration
        verification (Req 23.2).

        Validates: Requirements 23.1, 23.2, 23.3, 23.4

        Args:
            host_client: Optional HostClient for executing verification commands.
            verification_commands: Optional per-repo commands to execute.
                Maps repo_path -> list of shell commands.
            integration_config: Optional cross-repo integration verification config.

        Returns:
            Tuple of (overall_result, per_repo_results) where:
            - overall_result is "pass" if all non-detached repos passed, else "fail"
            - per_repo_results maps repo_path -> verification result string
        """
        per_repo: dict[str, str] = {}
        failures: list[str] = []

        # Execute per-repo verification commands if host_client is available
        if host_client is not None and verification_commands is not None:
            for path, state in self._repos.items():
                if state.status == "detached":
                    continue

                commands = verification_commands.get(path, [])
                if not commands and not state.changed_files:
                    # No commands and no changes — skip verification
                    per_repo[path] = "pass"
                    state.verification_result = "pass"
                    continue

                repo_passed = True
                for cmd in commands:
                    result = self._run_verification_command(
                        host_client, path, state.worktree_path, cmd
                    )
                    if not result.success:
                        repo_passed = False
                        per_repo[path] = "fail"
                        state.verification_result = "fail"
                        state.blockers.append(
                            f"Verification failed: {cmd} (exit {result.exit_code})"
                        )
                        failures.append(path)
                        logger.warning(
                            "Verification failed for %s: command=%s exit_code=%d",
                            path, cmd, result.exit_code,
                        )
                        break

                if repo_passed:
                    per_repo[path] = "pass"
                    state.verification_result = "pass"

        else:
            # Fallback: use existing verification_result on repo state
            for path, state in self._repos.items():
                if state.status == "detached":
                    continue

                result = state.verification_result
                if result is None:
                    per_repo[path] = "not_run"
                    failures.append(path)
                elif result == "pass":
                    per_repo[path] = "pass"
                else:
                    per_repo[path] = result
                    failures.append(path)

        # Cross-repo integration verification (Req 23.2)
        if (
            integration_config is not None
            and integration_config.enabled
            and host_client is not None
            and not failures  # Only run integration if all repos passed individually
        ):
            integration_result = self._run_integration_verification(
                host_client, integration_config
            )
            if not integration_result.success:
                failures.append("__integration__")
                logger.warning(
                    "Cross-repo integration verification failed: %s",
                    integration_result.error_output or integration_result.output,
                )

        overall = "pass" if not failures else "fail"
        return (overall, per_repo)

    def evaluate_merge_gate(
        self, conditions: dict[str, MergeGateCondition] | None = None
    ) -> tuple[bool, list[str]]:
        """Evaluate whether all repositories pass the merge gate.

        When conditions are provided, uses MergeGateCondition.passes for each repo.
        Otherwise derives pass/fail from repo_states.

        The merge gate passes only when ALL non-detached repositories satisfy
        their gate conditions (Req 24.1).

        If any repository is merge-blocked, the entire task is marked as
        merge-blocked with per-repository blocker details (Req 24.2).

        Validates: Requirements 24.1, 24.2

        Args:
            conditions: Optional per-repo merge gate conditions. If None,
                        uses internal state (status==completed, verification_result==pass,
                        no blockers).

        Returns:
            Tuple of (passes, blockers) where:
            - passes: bool indicating overall pass/fail
            - blockers: list of repo_paths that are blocked
        """
        blockers: list[str] = []

        if conditions is not None:
            for repo_path, condition in conditions.items():
                if repo_path in self._repos and self._repos[repo_path].status == "detached":
                    continue
                if not condition.passes:
                    blockers.append(repo_path)
        else:
            for path, state in self._repos.items():
                if state.status == "detached":
                    continue

                repo_blocked = False
                if state.status != "completed":
                    repo_blocked = True
                if state.verification_result != "pass":
                    repo_blocked = True
                if state.blockers:
                    repo_blocked = True

                if repo_blocked:
                    blockers.append(path)

        passes = len(blockers) == 0
        return (passes, blockers)

    def atomic_merge(
        self, host_client: "HostClient"
    ) -> AtomicMultiRepoMergeResponse:
        """Perform atomic merge of all non-detached repos via Go Host.

        Merges repos sequentially (fast-forward). If any merge fails,
        rolls back all previously merged repos to their pre-merge state.

        Validates: Requirements 24.3, 24.4

        Args:
            host_client: HostClient for executing git operations.

        Returns:
            AtomicMultiRepoMergeResponse with success status and details.
        """
        merged_repos: list[str] = []
        pre_merge_refs: dict[str, str] = {}

        # Capture pre-merge state for rollback
        for path, state in self._repos.items():
            if state.status == "detached":
                continue
            pre_merge_refs[path] = state.head_ref

        # Sequential fast-forward merge
        for path, state in self._repos.items():
            if state.status == "detached":
                continue

            try:
                merge_result = self._merge_repo(host_client, path, state)
                if not merge_result.success:
                    # Merge failed — rollback all previously merged repos
                    rolled_back = self._rollback_merged_repos(
                        host_client, merged_repos, pre_merge_refs
                    )
                    return AtomicMultiRepoMergeResponse(
                        task_id=self.task_id,
                        success=False,
                        merged_repos=merged_repos,
                        failed_repo=path,
                        failure_error=merge_result.error,
                        rolled_back_repos=rolled_back,
                    )
                merged_repos.append(path)
            except Exception as e:
                # Exception during merge — rollback
                rolled_back = self._rollback_merged_repos(
                    host_client, merged_repos, pre_merge_refs
                )
                return AtomicMultiRepoMergeResponse(
                    task_id=self.task_id,
                    success=False,
                    merged_repos=merged_repos,
                    failed_repo=path,
                    failure_error=str(e),
                    rolled_back_repos=rolled_back,
                )

        return AtomicMultiRepoMergeResponse(
            task_id=self.task_id,
            success=True,
            merged_repos=merged_repos,
        )

    def can_proceed_independently(self, repo_path: str) -> bool:
        """Check if a repository can proceed despite failures in other repos.

        A repo can proceed independently if none of the repos it depends on
        are in a failed or blocked state.

        Validates: Requirements 25.1, 25.2

        Args:
            repo_path: The repository path to check.

        Returns:
            True if the repo has no blocking dependencies.

        Raises:
            KeyError: If repo_path is not tracked by this task.
        """
        if repo_path not in self._repos:
            raise KeyError(f"Repository '{repo_path}' is not part of this task")

        state = self._repos[repo_path]
        if state.status in ("detached", "failed"):
            return False

        # Check dependencies
        dependencies = self._dependencies.get(repo_path, [])
        for dep_path in dependencies:
            if dep_path not in self._repos:
                continue
            dep_state = self._repos[dep_path]
            if dep_state.status in ("failed", "blocked"):
                return False

        return True

    def detach_repo(self, repo_path: str) -> None:
        """Mark a repository as detached from this multi-repo task.

        A detached repo is excluded from merge gate evaluation and
        verification aggregation, allowing remaining repos to proceed
        to merge independently.

        Validates: Requirements 25.4

        Args:
            repo_path: Path to the repository to detach.

        Raises:
            KeyError: If repo_path is not tracked by this task.
        """
        if repo_path not in self._repos:
            raise KeyError(f"Repository '{repo_path}' is not part of this task")
        self._repos[repo_path].status = "detached"

        # Remove detached repo from other repos' dependency lists
        # so they can proceed independently
        for path, deps in self._dependencies.items():
            if repo_path in deps:
                self._dependencies[path] = [d for d in deps if d != repo_path]

        logger.info(
            "Detached repo %s from task %s — remaining repos may proceed independently",
            repo_path,
            self.task_id,
        )

    def _run_verification_command(
        self,
        host_client: "HostClient",
        repo_path: str,
        worktree_path: str,
        command: str,
    ) -> VerificationCommandResult:
        """Execute a verification command in a repo's worktree via Go Host.

        Validates: Requirement 23.1

        Args:
            host_client: HostClient for executing commands.
            repo_path: The repository path (for reporting).
            worktree_path: The worktree directory to execute in.
            command: The shell command to run.

        Returns:
            VerificationCommandResult with execution details.
        """
        try:
            request = HostActionRequest(
                task_id=self.task_id,
                action_name="verification_exec",
                arguments={"command": command},
                working_dir=worktree_path,
            )
            observation: HostObservation = host_client.exec(request)
            return VerificationCommandResult(
                repo_path=repo_path,
                command=command,
                success=observation.success,
                exit_code=observation.exit_code or 0,
                output=observation.output,
                error_output="" if observation.success else observation.output,
            )
        except Exception as e:
            logger.exception(
                "Error executing verification command in %s: %s", repo_path, command
            )
            return VerificationCommandResult(
                repo_path=repo_path,
                command=command,
                success=False,
                exit_code=-1,
                output="",
                error_output=str(e),
            )

    def _run_integration_verification(
        self,
        host_client: "HostClient",
        config: IntegrationVerificationConfig,
    ) -> VerificationCommandResult:
        """Execute cross-repo integration verification.

        Substitutes {worktree:<repo_path>} placeholders in the command with
        actual worktree paths, enabling integration tests that reference
        multiple repos simultaneously.

        Validates: Requirement 23.2

        Args:
            host_client: HostClient for executing commands.
            config: Integration verification configuration.

        Returns:
            VerificationCommandResult for the integration check.
        """
        command = config.command
        # Substitute worktree path placeholders
        for path, state in self._repos.items():
            if state.status == "detached":
                continue
            placeholder = "{worktree:" + path + "}"
            command = command.replace(placeholder, state.worktree_path)

        # Use the first non-detached repo's worktree as working directory
        working_dir = ""
        for state in self._repos.values():
            if state.status != "detached" and state.worktree_path:
                working_dir = state.worktree_path
                break

        try:
            request = HostActionRequest(
                task_id=self.task_id,
                action_name="integration_verification",
                arguments={"command": command, "timeout_seconds": config.timeout_seconds},
                working_dir=working_dir,
            )
            observation: HostObservation = host_client.exec(request)
            return VerificationCommandResult(
                repo_path="__integration__",
                command=command,
                success=observation.success,
                exit_code=observation.exit_code or 0,
                output=observation.output,
                error_output="" if observation.success else observation.output,
            )
        except Exception as e:
            logger.exception("Error executing integration verification")
            return VerificationCommandResult(
                repo_path="__integration__",
                command=command,
                success=False,
                exit_code=-1,
                output="",
                error_output=str(e),
            )

    def _merge_repo(
        self,
        host_client: "HostClient",
        repo_path: str,
        state: RepoState,
    ) -> GitMergeResponse:
        """Merge a single repo via Go Host (fast-forward).

        Validates: Requirement 24.3

        Args:
            host_client: HostClient for git operations.
            repo_path: The repository path.
            state: The repo's current state.

        Returns:
            GitMergeResponse with merge result.
        """
        try:
            # Use exec to perform fast-forward merge via Go Host
            request = HostActionRequest(
                task_id=self.task_id,
                action_name="git_merge_ff",
                arguments={
                    "worktree_path": state.worktree_path,
                    "source_branch": state.branch,
                    "target_branch": state.branch.replace(f"vikram/{self.task_id}", "main")
                    if "vikram/" in state.branch
                    else "main",
                },
                working_dir=state.worktree_path,
            )
            observation: HostObservation = host_client.exec(request)
            return GitMergeResponse(
                task_id=self.task_id,
                worktree_path=state.worktree_path,
                success=observation.success,
                head_ref=observation.state.get("head_ref", ""),
                error=observation.output if not observation.success else "",
            )
        except Exception as e:
            return GitMergeResponse(
                task_id=self.task_id,
                worktree_path=state.worktree_path,
                success=False,
                error=str(e),
            )

    def _rollback_merged_repos(
        self,
        host_client: "HostClient",
        merged_repos: list[str],
        pre_merge_refs: dict[str, str],
    ) -> list[str]:
        """Rollback all previously merged repos to pre-merge state.

        Validates: Requirement 24.4

        Args:
            host_client: HostClient for git operations.
            merged_repos: List of repo paths that were already merged.
            pre_merge_refs: Original head refs before merge.

        Returns:
            List of repo paths that were successfully rolled back.
        """
        rolled_back: list[str] = []
        for path in merged_repos:
            state = self._repos[path]
            try:
                request = GitRollbackRequest(
                    task_id=self.task_id,
                    worktree_path=state.worktree_path,
                )
                host_client.rollback_worktree(request)
                rolled_back.append(path)
                logger.info("Rolled back merge for repo %s in task %s", path, self.task_id)
            except Exception:
                logger.exception(
                    "Failed to rollback merge for repo %s in task %s", path, self.task_id
                )
        return rolled_back

    def detect_interface_changes(
        self,
        old_file_contents: dict[str, dict[str, str]] | None = None,
        new_file_contents: dict[str, dict[str, str]] | None = None,
        execution_trace: Any | None = None,
    ) -> list[InterfaceChange]:
        """Detect interface changes across repos and identify consuming repos.

        For each repo with changes, extracts exported interfaces from old and
        new versions of modified files. Compares signatures to identify changes,
        then finds consuming repos by scanning imports across all other repos.

        Changes are classified as:
        - type_compatible (additive): new parameters with defaults, new exports
        - breaking: removed exports, changed signatures incompatibly

        Validates: Requirements 22.1, 22.2, 22.3, 22.4

        Args:
            old_file_contents: Mapping of repo_path -> {file_path: content_before_change}.
                Represents the state before implementation.
            new_file_contents: Mapping of repo_path -> {file_path: content_after_change}.
                Represents the state after implementation.
            execution_trace: Optional ExecutionTrace instance for recording
                cross-repo dependency decisions.

        Returns:
            List of InterfaceChange objects with consuming_repos populated.
        """
        if old_file_contents is None:
            old_file_contents = {}
        if new_file_contents is None:
            new_file_contents = {}

        changes: list[InterfaceChange] = []

        # Phase 1: For each repo, identify modified exported interfaces
        for repo_path, state in self._repos.items():
            if state.status == "detached":
                continue

            old_contents = old_file_contents.get(repo_path, {})
            new_contents = new_file_contents.get(repo_path, {})

            # Only process files that exist in new_contents (modified files)
            modified_files = set(new_contents.keys())
            if not modified_files:
                continue

            for file_path in modified_files:
                old_content = old_contents.get(file_path, "")
                new_content = new_contents.get(file_path, "")

                old_exports = extract_exported_interfaces(file_path, old_content)
                new_exports = extract_exported_interfaces(file_path, new_content)

                # Detect changes: removed, added, and signature differences
                file_changes = _compare_interfaces(
                    repo_path=repo_path,
                    file_path=file_path,
                    old_exports=old_exports,
                    new_exports=new_exports,
                    old_content=old_content,
                    new_content=new_content,
                )

                if file_changes:
                    changes.extend(file_changes)

        # Phase 2: For each change, find consuming repos
        for change in changes:
            consuming = self._find_consuming_repos(
                source_repo=change.repo_path,
                source_file=change.file_path,
                new_file_contents=new_file_contents,
                old_file_contents=old_file_contents,
            )
            change.consuming_repos = consuming

        # Phase 3: Record in Execution_Trace if provided
        if execution_trace is not None and changes:
            self._record_interface_analysis(execution_trace, changes)

        return changes

    def _find_consuming_repos(
        self,
        source_repo: str,
        source_file: str,
        new_file_contents: dict[str, dict[str, str]],
        old_file_contents: dict[str, dict[str, str]],
    ) -> list[str]:
        """Find repos that consume (import from) the given source file.

        Scans import statements across all other repos in this task to
        identify consumers of the modified interface.

        Args:
            source_repo: The repo containing the modified interface.
            source_file: The file path within source_repo with the changed interface.
            new_file_contents: All repos' new file contents for scanning.
            old_file_contents: All repos' old file contents as fallback.

        Returns:
            List of repo_paths that import from the source file.
        """
        consuming_repos: list[str] = []

        for repo_path, state in self._repos.items():
            if repo_path == source_repo:
                continue
            if state.status == "detached":
                continue

            # Gather all files in this consumer repo
            repo_files = new_file_contents.get(repo_path, {})
            if not repo_files:
                repo_files = old_file_contents.get(repo_path, {})

            repo_imports_source = False
            for consumer_file, content in repo_files.items():
                imports = extract_imports(consumer_file, content)
                for import_path in imports:
                    if _module_matches_import(source_file, import_path):
                        repo_imports_source = True
                        break
                if repo_imports_source:
                    break

            if repo_imports_source:
                consuming_repos.append(repo_path)

        return consuming_repos

    def _record_interface_analysis(
        self,
        execution_trace: Any,
        changes: list[InterfaceChange],
    ) -> None:
        """Record cross-repository interface dependencies in the Execution_Trace.

        Validates: Requirement 22.4

        Args:
            execution_trace: ExecutionTrace instance with record_decision method.
            changes: The detected interface changes to record.
        """
        dependencies_record = []
        for change in changes:
            dependencies_record.append({
                "repo_path": change.repo_path,
                "file_path": change.file_path,
                "interface_type": change.interface_type,
                "old_signature": change.old_signature,
                "new_signature": change.new_signature,
                "consuming_repos": change.consuming_repos,
                "is_breaking": classify_change_type(
                    change.old_signature, change.new_signature
                ) == "breaking",
            })

        try:
            execution_trace.record_decision(
                task_id=self.task_id,
                decision_type="cross_repo_interface_analysis",
                state_snapshot={
                    "total_changes": len(changes),
                    "breaking_changes": sum(
                        1 for d in dependencies_record if d["is_breaking"]
                    ),
                    "type_compatible_changes": sum(
                        1 for d in dependencies_record if not d["is_breaking"]
                    ),
                    "dependencies": dependencies_record,
                },
                policy="cross_repo_interface_contract_analysis",
                outcome="changes_detected",
            )
        except Exception:
            logger.exception(
                "Failed to record interface analysis in execution trace for task %s",
                self.task_id,
            )



# ---------------------------------------------------------------------------
# Interface Comparison Helpers
# ---------------------------------------------------------------------------


def _compare_interfaces(
    repo_path: str,
    file_path: str,
    old_exports: list[str],
    new_exports: list[str],
    old_content: str,
    new_content: str,
) -> list[InterfaceChange]:
    """Compare old and new exported interfaces to identify changes.

    Detects:
    - Removed exports (present in old, absent in new) -> breaking
    - Added exports (absent in old, present in new) -> type_compatible
    - Signature changes (present in both but definition changed) -> depends on analysis

    Args:
        repo_path: The repository path.
        file_path: The file within the repo.
        old_exports: List of exported names from old version.
        new_exports: List of exported names from new version.
        old_content: Full file content before change.
        new_content: Full file content after change.

    Returns:
        List of InterfaceChange objects for detected changes.
    """
    changes: list[InterfaceChange] = []

    old_set = set(old_exports)
    new_set = set(new_exports)

    # Removed exports (breaking change)
    removed = old_set - new_set
    for name in removed:
        old_sig = _extract_signature(name, old_content, file_path)
        changes.append(InterfaceChange(
            repo_path=repo_path,
            file_path=file_path,
            interface_type="function_signature",
            old_signature=old_sig,
            new_signature="",  # removed
            consuming_repos=[],
        ))

    # Added exports (type-compatible — additive change)
    added = new_set - old_set
    for name in added:
        new_sig = _extract_signature(name, new_content, file_path)
        changes.append(InterfaceChange(
            repo_path=repo_path,
            file_path=file_path,
            interface_type="function_signature",
            old_signature="",  # new export
            new_signature=new_sig,
            consuming_repos=[],
        ))

    # Modified exports (present in both — check for signature changes)
    common = old_set & new_set
    for name in common:
        old_sig = _extract_signature(name, old_content, file_path)
        new_sig = _extract_signature(name, new_content, file_path)
        if old_sig != new_sig:
            changes.append(InterfaceChange(
                repo_path=repo_path,
                file_path=file_path,
                interface_type="function_signature",
                old_signature=old_sig,
                new_signature=new_sig,
                consuming_repos=[],
            ))

    return changes


def _extract_signature(name: str, content: str, file_path: str) -> str:
    """Extract the signature line for a given exported name.

    Searches for the definition/declaration line in the content and returns
    the first line of the definition (the signature).

    Args:
        name: The exported name to find.
        content: The file content to search.
        file_path: The file path (used for language detection).

    Returns:
        The signature string, or the name itself if not found.
    """
    import os
    import re

    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".py":
        # Match "def name(...)" or "class name(...)" or "async def name(...)"
        pattern = re.compile(
            rf"^\s*(?:async\s+)?(?:def|class)\s+{re.escape(name)}\s*(\([^)]*\))?.*$",
            re.MULTILINE,
        )
    elif ext == ".go":
        # Match "func name(" or "func (receiver) name(" or "type name"
        pattern = re.compile(
            rf"^(?:func\s+(?:\([^)]*\)\s+)?{re.escape(name)}\s*\([^)]*\).*|type\s+{re.escape(name)}\b.*)$",
            re.MULTILINE,
        )
    elif ext in (".ts", ".tsx", ".js", ".jsx", ".mts", ".mjs"):
        # Match "export function name(" or "export const name" etc.
        pattern = re.compile(
            rf"^\s*export\s+(?:default\s+)?(?:function|class|const|let|var|type|interface|enum)\s+{re.escape(name)}\b.*$",
            re.MULTILINE,
        )
    else:
        return name

    match = pattern.search(content)
    if match:
        return match.group(0).strip()
    return name


def classify_change_type(old_signature: str, new_signature: str) -> str:
    """Classify an interface change as type_compatible or breaking.

    Classification rules:
    - Removed export (new_signature empty) -> "breaking"
    - Added export (old_signature empty) -> "type_compatible"
    - Signature modified:
      - If new signature is a superset of old (additive params with defaults) -> "type_compatible"
      - Otherwise -> "breaking"

    Args:
        old_signature: The original signature string.
        new_signature: The new signature string.

    Returns:
        "type_compatible" or "breaking"
    """
    # Removed export
    if not new_signature:
        return "breaking"

    # Added export (purely additive)
    if not old_signature:
        return "type_compatible"

    # Check if change is additive (new params with defaults, etc.)
    if _is_additive_change(old_signature, new_signature):
        return "type_compatible"

    return "breaking"


def _is_additive_change(old_signature: str, new_signature: str) -> bool:
    """Determine if a signature change is additive (type-compatible).

    A change is considered additive if:
    - The new signature starts with the same base as the old
    - New parameters all have default values
    - The return type hasn't changed in a way that narrows

    This is a heuristic; for production use, AST-level analysis would be more accurate.

    Args:
        old_signature: The original signature.
        new_signature: The modified signature.

    Returns:
        True if the change is additive/type-compatible.
    """
    import re

    # Extract parameter lists for Python functions
    old_params = re.search(r"\((.*)\)", old_signature)
    new_params = re.search(r"\((.*)\)", new_signature)

    if not old_params or not new_params:
        # If we can't parse params, check if new contains old as prefix
        return new_signature.startswith(old_signature.rstrip(":"))

    old_param_str = old_params.group(1).strip()
    new_param_str = new_params.group(1).strip()

    # If old params are empty and new has params with defaults, that's additive
    if not old_param_str and new_param_str:
        # All new params must have defaults
        new_parts = [p.strip() for p in new_param_str.split(",") if p.strip()]
        return all("=" in p for p in new_parts)

    # Split into individual parameters
    old_parts = [p.strip() for p in old_param_str.split(",") if p.strip()]
    new_parts = [p.strip() for p in new_param_str.split(",") if p.strip()]

    # If new has fewer params, it's breaking
    if len(new_parts) < len(old_parts):
        return False

    # Check that all old params are preserved at the beginning
    for i, old_part in enumerate(old_parts):
        if i >= len(new_parts):
            return False
        # Normalize for comparison (strip whitespace, self)
        old_normalized = old_part.strip()
        new_normalized = new_parts[i].strip()
        # Skip 'self' parameter
        if old_normalized == "self" and new_normalized == "self":
            continue
        if old_normalized != new_normalized:
            return False

    # Any additional params in new must have defaults
    extra_params = new_parts[len(old_parts):]
    if extra_params:
        return all("=" in p for p in extra_params)

    return True


# ---------------------------------------------------------------------------
# Standalone functions (used by property tests and other consumers)
# ---------------------------------------------------------------------------


def is_breaking_change(change: InterfaceChange) -> bool:
    """Determine if an interface change is breaking (not backwards-compatible).

    A change is considered breaking if:
    - A function/export was removed (new_signature is empty)
    - The signature parameters changed incompatibly (params removed, types changed)

    A change is NOT breaking (type-compatible) if:
    - A new export was added (old_signature is empty)
    - New parameters were added with default values (additive change)

    This is a simplistic heuristic suitable for static analysis; full semantic
    compatibility would require type-system-level analysis.

    Validates: Requirement 22.3 (auto-propagate type-compatible; pause for breaking)

    Args:
        change: The InterfaceChange to evaluate.

    Returns:
        True if the change is breaking, False if type-compatible.
    """
    return classify_change_type(change.old_signature, change.new_signature) == "breaking"


def aggregate_verification(repo_results: dict[str, str]) -> tuple[str, dict[str, str]]:
    """Aggregate verification results across multiple repos.

    Args:
        repo_results: Mapping of repo_path -> verification result ("pass" or "fail").

    Returns:
        (overall, per_repo) where overall is "pass" IFF all repos pass.

    Validates: Requirements 23.3, 23.4
    """
    if not repo_results:
        return ("pass", repo_results)

    all_pass = all(r == "pass" for r in repo_results.values())
    overall = "pass" if all_pass else "fail"
    return (overall, dict(repo_results))


def evaluate_merge_gate(conditions: dict[str, MergeGateCondition]) -> tuple[bool, list[str]]:
    """Evaluate merge gate for multiple repos using explicit conditions.

    Args:
        conditions: Per-repo merge gate conditions.

    Returns:
        (passes, blockers) where passes is True IFF ALL repos satisfy conditions.

    Validates: Requirements 24.1, 24.2
    """
    blockers: list[str] = []
    for repo_path, condition in conditions.items():
        if not condition.passes:
            blockers.append(repo_path)

    passes = len(blockers) == 0
    return (passes, blockers)
