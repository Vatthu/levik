from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


TaskStatus = Literal[
    "queued",
    "running",
    "paused",
    "awaiting_approval",
    "completed",
    "failed",
]
ApprovalDecisionKind = Literal["approve", "reject", "edit_and_approve", "clarify"]
ActionTransitionState = Literal[
    "awaiting_approval",
    "retryable",
    "blocked",
    "merge_ready",
]
ActionTransitionKind = Literal[
    "approve",
    "reject",
    "edit_and_approve",
    "clarify",
    "retry_change",
    "complete",
]
ApprovalPolicy = Literal["never", "on_request", "always"]
ApprovalRisk = Literal["low", "medium", "high", "critical"]
ApprovalRoute = Literal["auto_complete", "founder_review", "stop"]
MergeReadiness = Literal["unknown", "ready", "blocked"]
HostActionKind = Literal["shell", "filesystem", "git", "workspace", "notify"]
SideEffectLevel = Literal["read_only", "workspace_write", "repo_write", "external"]
ArtifactKind = Literal[
    "task_spec",
    "plan",
    "implementation",
    "verification",
    "review",
    "founder_approval",
    "blocker",
    "archive",
]


class RepoRef(BaseModel):
    path: str
    default_branch: str = "main"


class SystemHealthResponse(BaseModel):
    status: str
    workspace_root: str
    socket_path: str
    restrict_to_workspace: bool
    sandboxed: bool
    telegram_enabled: bool


class TaskConstraints(BaseModel):
    require_human_approval: bool = True
    max_parallel_workers: int = 1
    max_cost_usd: float | None = None
    allow_network: bool = False


class TaskSession(BaseModel):
    task_id: str
    source: str = "local"
    requested_by: str = "unknown"
    objective: str = Field(min_length=1)
    repo: RepoRef
    constraints: TaskConstraints = Field(default_factory=TaskConstraints)
    operator_channel: str | None = None
    operator_chat_id: str | None = None
    status: TaskStatus
    phase: str
    summary: str
    risk_class: ApprovalRisk | None = None
    approval_route: ApprovalRoute | None = None
    requires_founder_review: bool = False
    follow_up_required: bool = False
    follow_up_summary: str | None = None
    merge_readiness: MergeReadiness | None = None
    merge_summary: str | None = None


class HostActionSpec(BaseModel):
    name: str
    kind: HostActionKind
    description: str
    arguments_schema: dict[str, Any] = Field(default_factory=dict)
    approval_policy: ApprovalPolicy = "on_request"
    timeout_seconds: int = 30
    state_probe: list[str] = Field(default_factory=list)
    observation_policy: dict[str, Any] = Field(default_factory=dict)
    side_effect_level: SideEffectLevel = "read_only"


class HostActionRequest(BaseModel):
    task_id: str
    action_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    working_dir: str | None = None
    idempotency_key: str | None = None


class HostObservation(BaseModel):
    task_id: str
    action_name: str
    success: bool
    exit_code: int | None = None
    summary: str
    output: str = ""
    state: dict[str, Any] = Field(default_factory=dict)


class WorkspaceProvisionRequest(BaseModel):
    task_id: str
    repo: RepoRef


class WorkspaceProvisionResponse(BaseModel):
    task_id: str
    task_root: str
    artifacts_dir: str
    logs_dir: str
    scratch_dir: str
    worktree_path: str


class GitWorktreeCreateRequest(BaseModel):
    task_id: str
    repo: RepoRef
    worktree_path: str
    branch: str
    base_ref: str | None = None


class GitWorktreeCreateResponse(BaseModel):
    task_id: str
    repo_path: str
    worktree_path: str
    branch: str
    base_ref: str
    head_ref: str = ""
    created: bool


class GitWorktreeRemoveRequest(BaseModel):
    task_id: str
    worktree_path: str
    force: bool = False


class GitWorktreeRemoveResponse(BaseModel):
    task_id: str
    worktree_path: str
    removed: bool


class RepoFileSummary(BaseModel):
    path: str
    preview: str
    bytes: int


class RepoChangedFile(BaseModel):
    path: str
    status: str
    additions: int = 0
    deletions: int = 0
    binary: bool = False


class RepoInspectRequest(BaseModel):
    task_id: str
    repo_path: str
    worktree_path: str


class RepoInspectResponse(BaseModel):
    task_id: str
    repo_path: str
    worktree_path: str
    branch: str
    head_ref: str
    dirty: bool
    changed_file_count: int = 0
    additions: int = 0
    deletions: int = 0
    diff_short_stat: str = ""
    top_level_entries: list[str] = Field(default_factory=list)
    status_lines: list[str] = Field(default_factory=list)
    changed_files: list[RepoChangedFile] = Field(default_factory=list)
    key_files: list[RepoFileSummary] = Field(default_factory=list)


class RepoTargetCandidate(BaseModel):
    path: str
    score: int
    reason: str


class RepoTargetDiscoveryRequest(BaseModel):
    task_id: str
    worktree_path: str
    objective: str
    limit: int = 6


class RepoTargetDiscoveryResponse(BaseModel):
    task_id: str
    worktree_path: str
    candidates: list[RepoTargetCandidate] = Field(default_factory=list)


class FileReadRequest(BaseModel):
    task_id: str
    worktree_path: str
    path: str
    max_bytes: int = 4000


class FileReadResponse(BaseModel):
    task_id: str
    path: str
    full_path: str
    content: str
    bytes_read: int
    truncated: bool


class FileWriteRequest(BaseModel):
    task_id: str
    worktree_path: str
    path: str
    content: str


class FileWriteResponse(BaseModel):
    task_id: str
    path: str
    full_path: str
    bytes_written: int


class FileReplaceRequest(BaseModel):
    task_id: str
    worktree_path: str
    path: str
    old_text: str
    new_text: str


class FileReplaceResponse(BaseModel):
    task_id: str
    path: str
    full_path: str
    bytes_written: int


class VerificationCommandCandidate(BaseModel):
    command: str
    working_dir: str
    runtime: str
    reason: str


class VerificationDiscoveryRequest(BaseModel):
    task_id: str
    worktree_path: str
    target_paths: list[str] = Field(default_factory=list)


class VerificationDiscoveryResponse(BaseModel):
    task_id: str
    worktree_path: str
    runtime: str
    candidates: list[VerificationCommandCandidate] = Field(default_factory=list)


class Artifact(BaseModel):
    task_id: str
    artifact_id: str
    kind: ArtifactKind
    title: str
    summary: str
    path: str | None = None


class ArtifactWriteRequest(BaseModel):
    artifact: Artifact
    content: str
    format: str = "markdown"


class ArtifactWriteResponse(BaseModel):
    artifact: Artifact
    path: str
    bytes_written: int


class ArtifactReadRequest(BaseModel):
    task_id: str
    path: str
    max_bytes: int = 32000


class ArtifactReadResponse(BaseModel):
    task_id: str
    path: str
    content: str
    bytes_read: int
    truncated: bool


class ApprovalRequest(BaseModel):
    task_id: str
    phase: str
    risk_class: ApprovalRisk
    route: ApprovalRoute = "founder_review"
    reasons: list[str] = Field(default_factory=list)
    reason: str
    summary: str
    options: list[ApprovalDecisionKind]
    related_artifacts: list[str] = Field(default_factory=list)


class ApprovalPolicyDecision(BaseModel):
    risk_class: ApprovalRisk
    route: ApprovalRoute
    reasons: list[str] = Field(default_factory=list)
    summary: str
    options: list[ApprovalDecisionKind] = Field(default_factory=list)


class ApprovalDecision(BaseModel):
    task_id: str
    decision: ApprovalDecisionKind
    comment: str = ""
    proposed_edits: dict[str, Any] = Field(default_factory=dict)


class ChannelNotificationRequest(BaseModel):
    channel: str = "telegram"
    chat_id: str
    content: str


class ChannelNotificationResponse(BaseModel):
    delivered: bool
    summary: str


class TextReplacement(BaseModel):
    path: str
    old_text: str
    new_text: str
    rationale: str = ""


class TaskCreateRequest(BaseModel):
    task_id: str
    source: str = "local"
    requested_by: str = "unknown"
    objective: str = Field(min_length=1)
    repo: RepoRef
    constraints: TaskConstraints = Field(default_factory=TaskConstraints)
    operator_channel: str | None = None
    operator_chat_id: str | None = None
    # Multi-repo and scheduling extensions (Requirements 21.1, 16.1, 18.1, 14.2)
    repos: list[RepoRef] = Field(default_factory=list)
    priority: str = "normal"  # "critical", "high", "normal", "low"
    depends_on: list[str] = Field(default_factory=list)
    formation: str | None = None


class TaskChangeRequest(BaseModel):
    task_id: str
    summary: str = ""
    edits: list[TextReplacement] = Field(min_length=1)
    verification_commands: list[str] = Field(default_factory=list)


class FollowUpContext(BaseModel):
    required: bool = False
    phase: str | None = None
    comment: str = ""
    proposed_edits: dict[str, Any] = Field(default_factory=dict)


class AppliedEditEvidence(BaseModel):
    path: str
    full_path: str | None = None
    bytes_written: int = 0
    rationale: str = ""
    old_text_preview: str = ""
    new_text_preview: str = ""
    diff_preview: str = ""


class VerificationRunEvidence(BaseModel):
    command: str
    success: bool
    summary: str = ""
    output_preview: str = ""


class ArtifactPreview(BaseModel):
    title: str
    kind: ArtifactKind
    path: str | None = None
    content_preview: str = ""


class MergeAssessment(BaseModel):
    state: MergeReadiness = "unknown"
    summary: str = ""
    branch: str = ""
    head_ref: str = ""
    changed_file_count: int = 0
    additions: int = 0
    deletions: int = 0
    diff_short_stat: str = ""
    changed_files: list[RepoChangedFile] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    status_lines: list[str] = Field(default_factory=list)


class ActionTransition(BaseModel):
    state: ActionTransitionState
    action: ActionTransitionKind
    target_phase: str
    target_status: TaskStatus
    summary: str
    enabled: bool = True


class TaskReviewDetail(BaseModel):
    task: TaskSession
    approval_request: ApprovalRequest | None = None
    approval_artifact_path: str | None = None
    change_artifact_path: str | None = None
    verification_result_artifact_path: str | None = None
    founder_decision: ApprovalDecision | None = None
    founder_decision_artifact_path: str | None = None
    follow_up: FollowUpContext = Field(default_factory=FollowUpContext)
    merge_assessment: MergeAssessment = Field(default_factory=MergeAssessment)
    merge_artifact_path: str | None = None
    applied_edits: list[AppliedEditEvidence] = Field(default_factory=list)
    verification_runs: list[VerificationRunEvidence] = Field(default_factory=list)
    artifact_previews: list[ArtifactPreview] = Field(default_factory=list)
    action_transitions: list[ActionTransition] = Field(default_factory=list)
    can_resume: bool = False
    can_apply_follow_up: bool = False


class GitRollbackRequest(BaseModel):
    task_id: str
    worktree_path: str


class GitRollbackResponse(BaseModel):
    task_id: str
    worktree_path: str
    rolled_back: bool
    head_ref: str | None = None


class LintDiscoveryRequest(BaseModel):
    task_id: str
    worktree_path: str


class LintCommandCandidate(BaseModel):
    command: str
    working_dir: str
    runtime: str
    reason: str


class LintDiscoveryResponse(BaseModel):
    task_id: str
    worktree_path: str
    runtime: str
    candidates: list[LintCommandCandidate] = Field(default_factory=list)


class LintRunRequest(BaseModel):
    task_id: str
    worktree_path: str
    command: str
    baseline: str = ""


class LintRunResponse(BaseModel):
    task_id: str
    command: str
    success: bool
    exit_code: int
    output: str
    new_errors: list[str] = Field(default_factory=list)


TaskStatusResponse = TaskSession

class ChangeReviewRequest(BaseModel):
    task_id: str
    objective: str
    diff: str
    test_output: str = ""
    lint_errors: list[str] = Field(default_factory=list)


class ChangeReviewResponse(BaseModel):
    task_id: str
    verdict: str  # APPROVE, CHANGES_REQUESTED, REJECT
    issues: list[str] = Field(default_factory=list)
    summary: str


class AgentThinkRequest(BaseModel):
    task_id: str
    role: str
    prompt: str
    model: str | None = None
    provider: str | None = None


class AgentThinkResponse(BaseModel):
    task_id: str
    role: str
    content: str


class AgentProfile(BaseModel):
    id: str
    role: str
    name: str | None = None
    provider: str | None = None
    model: str | None = None
    capabilities: list[str] = Field(default_factory=list)


class AgentRosterResponse(BaseModel):
    agents: list[AgentProfile] = Field(default_factory=list)

class BrowserTestRequest(BaseModel):
    task_id: str
    worktree_path: str
    test_script: str
    url: str = ""


class BrowserTestResponse(BaseModel):
    task_id: str
    success: bool
    exit_code: int
    output: str
    screenshot: str = ""


# ---------------------------------------------------------------------------
# Multi-Repo Merge Models (Requirements 24.3, 24.4)
# ---------------------------------------------------------------------------


class GitMergeRequest(BaseModel):
    """Request to merge a worktree branch into its target branch via fast-forward."""

    task_id: str
    worktree_path: str
    target_branch: str
    source_branch: str


class GitMergeResponse(BaseModel):
    """Response from a git merge operation."""

    task_id: str
    worktree_path: str
    success: bool
    head_ref: str = ""
    error: str = ""


class AtomicMultiRepoMergeRequest(BaseModel):
    """Request for atomic merge of multiple repos with rollback on failure.

    Validates: Requirements 24.3, 24.4
    """

    task_id: str
    repos: list[GitMergeRequest] = Field(default_factory=list)


class AtomicMultiRepoMergeResponse(BaseModel):
    """Response from an atomic multi-repo merge attempt.

    If any repo merge fails, all previously merged repos are rolled back.
    """

    task_id: str
    success: bool
    merged_repos: list[str] = Field(default_factory=list)
    failed_repo: str = ""
    failure_error: str = ""
    rolled_back_repos: list[str] = Field(default_factory=list)


class VerificationCommandResult(BaseModel):
    """Result of running a verification command in a specific repo's worktree."""

    repo_path: str
    command: str
    success: bool
    exit_code: int = 0
    output: str = ""
    error_output: str = ""
