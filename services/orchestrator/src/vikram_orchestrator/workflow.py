from __future__ import annotations

import difflib
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from .approval_matrix import ApprovalMatrix
from .conflict_detector import ConflictDetector
from .cost_client import CostClient, CostRecordRequest
from .execution_trace import ExecutionTrace
from .host_client import HostClient
from .knowledge_store import KnowledgeStore
from .model_router import ComplexitySignals, ModelRouter
from .models import (
    ActionTransition,
    AgentThinkRequest,
    GitRollbackRequest,
    ChangeReviewRequest,
    LintDiscoveryRequest,
    LintRunRequest,
    ApprovalDecision,
    ApprovalPolicyDecision,
    ApprovalRequest,
    AppliedEditEvidence,
    Artifact,
    ArtifactPreview,
    ArtifactWriteRequest,
    BrowserTestRequest,
    ChannelNotificationRequest,
    FileReadRequest,
    FileReplaceRequest,
    GitWorktreeCreateRequest,
    HostActionRequest,
    RepoTargetDiscoveryRequest,
    RepoRef,
    RepoInspectRequest,
    TaskChangeRequest,
    TaskCreateRequest,
    TaskReviewDetail,
    TaskSession,
    TaskStatus,
    VerificationDiscoveryRequest,
    VerificationRunEvidence,
    WorkspaceProvisionRequest,
    FollowUpContext,
    MergeAssessment,
)
from .policy import decide_approval_policy
from .scheduler import Scheduler
from .settings import settings
from .team import AgentCallFailedError, AgentUnavailableError, TeamRouter
from .telemetry_client import TelemetryClient
from .verification_protocol import VerificationProtocol

logger = logging.getLogger(__name__)


class OrchestratorState(TypedDict, total=False):
    task_id: str
    source: str
    requested_by: str
    objective: str
    repo_path: str
    repo_default_branch: str
    operator_channel: str | None
    operator_chat_id: str | None
    require_human_approval: bool
    max_parallel_workers: int
    max_cost_usd: float | None
    allow_network: bool
    status: str
    phase: str
    summary: str
    workspace_root: str
    host_socket: str
    restrict_to_workspace: bool
    sandboxed: bool
    telegram_enabled: bool
    team_roster: list[dict[str, object]]
    team_router_summary: str
    task_root: str
    artifacts_dir: str
    logs_dir: str
    scratch_dir: str
    worktree_path: str
    worktree_branch: str
    worktree_created: bool
    repo_branch: str
    repo_head_ref: str
    repo_dirty: bool
    repo_changed_file_count: int
    repo_additions: int
    repo_deletions: int
    repo_diff_short_stat: str
    repo_top_level_entries: list[str]
    repo_status_lines: list[str]
    repo_changed_files: list[dict[str, str | int | bool]]
    repo_key_files: list[dict[str, str | int]]
    target_candidates: list[dict[str, str | int]]
    target_file_previews: list[dict[str, str | int | bool]]
    plan_content: str
    grill_rounds: int
    grill_summary: str
    plan_artifact_id: str
    plan_artifact_path: str
    implementation_artifact_id: str
    implementation_artifact_path: str
    lint_candidates: list[dict[str, str]]
    lint_baseline: str
    lint_command: str
    lint_new_errors: list[str]
    lint_passed: bool
    review_verdict: str
    review_issues: list[str]
    review_summary: str
    impl_content: str
    parsed_edits: list[dict[str, str]]
    runner_verdict: str
    runner_summary: str
    runner_issues: list[str]
    qa_result: str
    verification_candidates: list[dict[str, str]]
    verification_artifact_id: str
    verification_artifact_path: str
    change_request: dict[str, object]
    change_attempt: int
    applied_edits: list[dict[str, str | int]]
    change_artifact_id: str
    change_artifact_path: str
    verification_runs: list[dict[str, str | bool | int]]
    verification_outcome: str
    verification_result_artifact_id: str
    verification_result_artifact_path: str
    approval_request: dict[str, object]
    approval_artifact_id: str
    approval_artifact_path: str
    founder_decision: dict[str, object]
    founder_decision_artifact_id: str
    founder_decision_artifact_path: str
    approval_risk: str
    approval_route: str
    approval_reasons: list[str]
    approval_summary: str
    approval_options: list[str]
    pending_follow_up_required: bool
    pending_follow_up_phase: str
    pending_follow_up_comment: str
    pending_follow_up_proposed_edits: dict[str, object]
    active_follow_up_phase: str
    active_follow_up_comment: str
    active_follow_up_proposed_edits: dict[str, object]
    post_change_dirty: bool
    post_change_branch: str
    post_change_head_ref: str
    post_change_status_lines: list[str]
    post_change_changed_file_count: int
    post_change_additions: int
    post_change_deletions: int
    post_change_diff_short_stat: str
    post_change_changed_files: list[dict[str, str | int | bool]]
    merge_readiness: str
    merge_summary: str
    merge_blockers: list[str]
    merge_notes: list[str]
    merge_artifact_id: str
    merge_artifact_path: str
    archive_artifact_id: str
    archive_artifact_path: str
    operator_notifications: list[dict[str, str | bool]]


def initial_state_from_request(request: TaskCreateRequest) -> OrchestratorState:
    return {
        "task_id": request.task_id,
        "source": request.source,
        "requested_by": request.requested_by,
        "objective": request.objective,
        "repo_path": request.repo.path,
        "repo_default_branch": request.repo.default_branch,
        "operator_channel": request.operator_channel,
        "operator_chat_id": request.operator_chat_id,
        "require_human_approval": request.constraints.require_human_approval,
        "max_parallel_workers": request.constraints.max_parallel_workers,
        "max_cost_usd": request.constraints.max_cost_usd,
        "allow_network": request.constraints.allow_network,
        "status": "queued",
        "phase": "intake",
        "summary": "Task received by orchestrator",
    }


def state_to_task_session(
    request: TaskCreateRequest, state: OrchestratorState
) -> TaskSession:
    return TaskSession(
        task_id=request.task_id,
        source=request.source,
        requested_by=request.requested_by,
        objective=request.objective,
        repo=request.repo,
        constraints=request.constraints,
        operator_channel=request.operator_channel,
        operator_chat_id=request.operator_chat_id,
        status=state.get("status", "running"),
        phase=state.get("phase", "intake"),
        summary=state.get("summary", "Task accepted by orchestrator"),
        risk_class=state.get("approval_risk"),
        approval_route=state.get("approval_route"),
        requires_founder_review=state.get("approval_route") == "founder_review",
        follow_up_required=bool(state.get("pending_follow_up_required", False)),
        follow_up_summary=str(state.get("pending_follow_up_comment", "")).strip() or None,
        merge_readiness=state.get("merge_readiness"),
        merge_summary=str(state.get("merge_summary", "")).strip() or None,
    )


def task_session_from_existing(
    task: TaskSession, state: OrchestratorState
) -> TaskSession:
    pending_follow_up = bool(
        state.get("pending_follow_up_required", task.follow_up_required)
    )
    phase = state.get("phase", task.phase)
    merge_blocked = phase == "merge_blocked"
    follow_up_summary = str(
        state.get("pending_follow_up_comment", task.follow_up_summary or "")
    ).strip()
    if merge_blocked and not follow_up_summary:
        follow_up_summary = str(state.get("merge_summary", "")).strip()
    return TaskSession(
        task_id=task.task_id,
        source=task.source,
        requested_by=task.requested_by,
        objective=task.objective,
        repo=task.repo,
        constraints=task.constraints,
        operator_channel=task.operator_channel,
        operator_chat_id=task.operator_chat_id,
        status=state.get("status", task.status),
        phase=phase,
        summary=state.get("summary", task.summary),
        risk_class=state.get("approval_risk", task.risk_class),
        approval_route=state.get("approval_route", task.approval_route),
        requires_founder_review=state.get("approval_route", task.approval_route)
        == "founder_review",
        follow_up_required=pending_follow_up or merge_blocked,
        follow_up_summary=follow_up_summary or None,
        merge_readiness=state.get("merge_readiness", task.merge_readiness),
        merge_summary=str(state.get("merge_summary", task.merge_summary or "")).strip()
        or None,
    )


def task_review_from_state(task: TaskSession, state: OrchestratorState) -> TaskReviewDetail:
    session = task_session_from_existing(task, state)
    approval_request_data = state.get("approval_request") or None
    founder_decision_data = state.get("founder_decision") or None
    action_transitions = review_action_transitions(session, state)
    pending_follow_up_required = bool(state.get("pending_follow_up_required", False))
    follow_up_phase = str(state.get("pending_follow_up_phase", "")).strip() or None
    follow_up_comment = str(state.get("pending_follow_up_comment", "")).strip()
    follow_up_proposed_edits = dict(
        state.get("pending_follow_up_proposed_edits", {}) or {}
    )
    if not pending_follow_up_required and session.phase == "merge_blocked":
        pending_follow_up_required = True
        follow_up_phase = "merge_blocked"
        follow_up_comment = str(state.get("merge_summary", "")).strip()
        follow_up_proposed_edits = {
            "merge_blockers": list(state.get("merge_blockers", []) or []),
            "merge_notes": list(state.get("merge_notes", []) or []),
        }
    follow_up = FollowUpContext(
        required=pending_follow_up_required,
        phase=follow_up_phase,
        comment=follow_up_comment,
        proposed_edits=follow_up_proposed_edits,
    )
    applied_edits = [
        AppliedEditEvidence(
            path=str(item.get("path", "")),
            full_path=str(item.get("full_path", "")).strip() or None,
            bytes_written=int(item.get("bytes_written", 0) or 0),
            rationale=str(item.get("rationale", "")).strip(),
            old_text_preview=str(item.get("old_text_preview", "")),
            new_text_preview=str(item.get("new_text_preview", "")),
            diff_preview=str(item.get("diff_preview", "")),
        )
        for item in state.get("applied_edits", [])
        if str(item.get("path", "")).strip()
    ]
    verification_runs = [
        VerificationRunEvidence(
            command=str(item.get("command", "")),
            success=bool(item.get("success", False)),
            summary=str(item.get("summary", "")).strip(),
            output_preview=preview_text(str(item.get("output", "")).strip(), limit=600),
        )
        for item in state.get("verification_runs", [])
        if str(item.get("command", "")).strip()
    ]
    return TaskReviewDetail(
        task=session,
        approval_request=ApprovalRequest.model_validate(approval_request_data)
        if approval_request_data
        else None,
        approval_artifact_path=str(state.get("approval_artifact_path", "")).strip() or None,
        change_artifact_path=str(state.get("change_artifact_path", "")).strip() or None,
        verification_result_artifact_path=str(
            state.get("verification_result_artifact_path", "")
        ).strip()
        or None,
        founder_decision=ApprovalDecision.model_validate(founder_decision_data)
        if founder_decision_data
        else None,
        founder_decision_artifact_path=str(
            state.get("founder_decision_artifact_path", "")
        ).strip()
        or None,
        follow_up=follow_up,
        merge_assessment=MergeAssessment(
            state=str(state.get("merge_readiness", "unknown") or "unknown"),
            summary=str(state.get("merge_summary", "")).strip(),
            branch=str(state.get("post_change_branch", "")).strip(),
            head_ref=str(state.get("post_change_head_ref", "")).strip(),
            changed_file_count=int(state.get("post_change_changed_file_count", 0) or 0),
            additions=int(state.get("post_change_additions", 0) or 0),
            deletions=int(state.get("post_change_deletions", 0) or 0),
            diff_short_stat=str(state.get("post_change_diff_short_stat", "")).strip(),
            changed_files=list(state.get("post_change_changed_files", []) or []),
            blockers=[
                str(item).strip()
                for item in state.get("merge_blockers", [])
                if str(item).strip()
            ],
            notes=[
                str(item).strip()
                for item in state.get("merge_notes", [])
                if str(item).strip()
            ],
            status_lines=[
                str(item).strip()
                for item in state.get("post_change_status_lines", [])
                if str(item).strip()
            ],
        ),
        merge_artifact_path=str(state.get("merge_artifact_path", "")).strip() or None,
        applied_edits=applied_edits,
        verification_runs=verification_runs,
        artifact_previews=artifact_previews(state),
        action_transitions=action_transitions,
        can_resume=any(item.state == "awaiting_approval" for item in action_transitions),
        can_apply_follow_up=session.phase
        in {
            "founder_edit_requested",
            "founder_clarification_requested",
            "change_ready",
            "merge_blocked",
        }
        or any(
            item.state in {"retryable", "blocked"} for item in action_transitions
        ),
    )


def review_action_transitions(
    session: TaskSession, state: OrchestratorState
) -> list[ActionTransition]:
    transitions: list[ActionTransition] = []
    verification_outcome = str(state.get("verification_outcome", "")).strip()
    active_follow_up_phase = str(state.get("active_follow_up_phase", "")).strip()
    has_retry_context = session.phase == "merge_blocked" or active_follow_up_phase in {
        "founder_edit_requested",
        "founder_clarification_requested",
        "merge_blocked",
    }

    if session.status == "awaiting_approval":
        for option in approval_options(state):
            if option == "approve":
                target_phase = "merge_ready"
                target_status: TaskStatus = "completed"
                summary = "Approve the change and continue to merge readiness"
                if verification_outcome == "failed":
                    target_phase = "merge_blocked"
                    target_status = "paused"
                    summary = (
                        "Approve the change despite failed verification and keep the task "
                        "blocked until a passing run exists"
                    )
                transitions.append(
                    ActionTransition(
                        state="awaiting_approval",
                        action=option,
                        target_phase=target_phase,
                        target_status=target_status,
                        summary=summary,
                    )
                )
                continue

            if option == "reject":
                transitions.append(
                    ActionTransition(
                        state="awaiting_approval",
                        action=option,
                        target_phase="founder_rejected",
                        target_status="failed",
                        summary="Reject the change and roll back the worktree",
                    )
                )
                continue

            if option == "edit_and_approve":
                transitions.append(
                    ActionTransition(
                        state="awaiting_approval",
                        action=option,
                        target_phase="founder_edit_requested",
                        target_status="paused",
                        summary="Request a bounded follow-up edit before approval",
                    )
                )
                continue

            transitions.append(
                ActionTransition(
                    state="awaiting_approval",
                    action=option,
                    target_phase="founder_clarification_requested",
                    target_status="paused",
                    summary="Request clarification before approval",
                )
            )

    if has_retry_context:
        retry_state = "retryable"
        retry_label = "Retry the bounded change with the founder's follow-up context"
        if session.phase == "merge_blocked" or active_follow_up_phase == "merge_blocked":
            retry_state = "blocked"
            retry_label = "Retry the merge handoff after addressing the recorded blockers"
        transitions.append(
            ActionTransition(
                state=retry_state,
                action="retry_change",
                target_phase="change_requested",
                target_status="running",
                summary=retry_label,
            )
        )

    if session.phase == "merge_ready":
        transitions.append(
            ActionTransition(
                state="merge_ready",
                action="complete",
                target_phase="merge_ready",
                target_status="completed",
                summary="No further action is required; the task is ready to merge",
            )
        )

    return transitions


def artifact_previews(state: OrchestratorState) -> list[ArtifactPreview]:
    previews: list[ArtifactPreview] = []

    change_path = str(state.get("change_artifact_path", "")).strip()
    if change_path:
        previews.append(
            ArtifactPreview(
                title="Applied Change",
                kind="implementation",
                path=change_path,
                content_preview=preview_text(change_artifact_content(state), limit=2200),
            )
        )

    verification_path = str(state.get("verification_result_artifact_path", "")).strip()
    if verification_path:
        previews.append(
            ArtifactPreview(
                title="Verification Result",
                kind="verification",
                path=verification_path,
                content_preview=preview_text(
                    verification_result_artifact_content(state), limit=2200
                ),
            )
        )

    approval_path = str(state.get("approval_artifact_path", "")).strip()
    approval_request_data = state.get("approval_request") or None
    if approval_path and approval_request_data:
        approval_request = ApprovalRequest.model_validate(approval_request_data)
        previews.append(
            ArtifactPreview(
                title="Founder Approval Request",
                kind="founder_approval",
                path=approval_path,
                content_preview=preview_text(
                    format_approval_request_artifact(state, approval_request), limit=2200
                ),
            )
        )

    founder_decision_path = str(state.get("founder_decision_artifact_path", "")).strip()
    if founder_decision_path:
        previews.append(
            ArtifactPreview(
                title="Founder Decision",
                kind="founder_approval",
                path=founder_decision_path,
                content_preview=preview_text(
                    format_founder_decision_artifact(state), limit=2200
                ),
            )
        )

    merge_path = str(state.get("merge_artifact_path", "")).strip()
    if merge_path:
        previews.append(
            ArtifactPreview(
                title="Merge Readiness",
                kind="review",
                path=merge_path,
                content_preview=preview_text(
                    merge_readiness_artifact_content(state), limit=2200
                ),
            )
        )

    archive_path = str(state.get("archive_artifact_path", "")).strip()
    if archive_path:
        previews.append(
            ArtifactPreview(
                title="Archive Email Draft",
                kind="archive",
                path=archive_path,
                content_preview=preview_text(
                    archive_email_draft_content(state), limit=2200
                ),
            )
        )

    return previews


def preview_text(text: str, limit: int = 400) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[:limit].rstrip() + "\n...<truncated>"


def build_diff_preview(old_text: str, new_text: str, limit_lines: int = 18) -> str:
    diff_lines = list(
        difflib.unified_diff(
            old_text.splitlines(),
            new_text.splitlines(),
            fromfile="before",
            tofile="after",
            lineterm="",
        )
    )
    if not diff_lines:
        return "No textual diff."
    if len(diff_lines) > limit_lines:
        diff_lines = diff_lines[:limit_lines] + ["...<truncated>"]
    return "\n".join(diff_lines)


def is_valid_plan(plan: str) -> bool:
    """Check if a plan has substantive content worthy of adversarial validation.

    A valid plan must:
    - Not be empty/whitespace
    - Have at least settings.plan_min_lines lines of content
    - Not consist entirely of error messages
    """
    stripped = plan.strip()
    if not stripped:
        return False

    lines = [line for line in stripped.splitlines() if line.strip()]
    if len(lines) < settings.plan_min_lines:
        return False

    # Check if plan consists entirely of error patterns
    error_patterns = re.compile(
        r"^(plan )?(unavailable|failed|error|timeout|unreachable|not configured)",
        re.IGNORECASE,
    )
    # If ALL non-empty lines match error patterns, it's not a valid plan
    if all(error_patterns.match(line.strip()) for line in lines):
        return False

    return True


def build_graph(
    host_client: HostClient,
    checkpoint_db: Path | None = None,
    *,
    model_router: ModelRouter | None = None,
    cost_client: CostClient | None = None,
    telemetry_client: TelemetryClient | None = None,
    execution_trace: ExecutionTrace | None = None,
    approval_matrix: ApprovalMatrix | None = None,
    conflict_detector: ConflictDetector | None = None,
    knowledge_store: KnowledgeStore | None = None,
    verification_protocol: VerificationProtocol | None = None,
    scheduler: Scheduler | None = None,
):
    checkpoint_path = checkpoint_db or settings.checkpoint_db
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_conn = sqlite3.connect(checkpoint_path, check_same_thread=False)
    checkpointer = SqliteSaver(checkpoint_conn)

    def ask_agent(state: OrchestratorState, role: str, prompt: str):
        router = TeamRouter.from_state(state.get("team_roster", []))
        task_id = state["task_id"]
        phase = state.get("phase", "unknown")

        # --- Cost Client: budget check before agent call (Requirement 3.2) ---
        if cost_client:
            try:
                remaining = cost_client.get_phase_remaining(task_id, phase)
                if remaining <= 0:
                    raise AgentCallFailedError(
                        role=role,
                        attempts=0,
                        last_error=RuntimeError(
                            f"Budget exhausted for phase '{phase}' (remaining={remaining:.4f} USD)"
                        ),
                    )
            except AgentCallFailedError:
                raise
            except Exception:
                # Budget service unavailable — proceed (fail-open for cost)
                pass

        # --- Model Router: select model if configured (Requirement 1.1) ---
        model_selection = None
        if model_router and cost_client:
            try:
                budget_position = 1.0
                max_cost = state.get("max_cost_usd")
                if max_cost and max_cost > 0:
                    cumulative = cost_client.get_task_cost(task_id)
                    budget_position = max(0.0, (max_cost - cumulative) / max_cost)

                # Classify complexity from available signals
                target_count = len(state.get("target_candidates", []) or [])
                signals = ComplexitySignals(
                    objective_scope=state.get("objective", ""),
                    target_file_count=max(target_count, 1),
                    repo_size_files=len(state.get("repo_top_level_entries", []) or []),
                    language_count=1,
                    change_type="logic",
                    security_relevant=False,
                    test_modification=False,
                )
                tier = model_router.classify_complexity(signals)
                model_selection = model_router.select_model(
                    task_id=task_id,
                    role=role,
                    tier=tier,
                    budget_position=budget_position,
                )
            except Exception:
                # Router unavailable — fall back to team roster selection
                pass

        request = router.request(task_id=task_id, role=role, prompt=prompt)

        # Override provider/model if Model Router made a selection
        if model_selection:
            request = AgentThinkRequest(
                task_id=request.task_id,
                role=request.role,
                prompt=request.prompt,
                provider=model_selection.provider,
                model=model_selection.model,
            )

        # --- Execution Trace: record model selection decision (Requirement 6.1) ---
        if execution_trace and model_selection:
            try:
                execution_trace.record_decision(
                    task_id=task_id,
                    decision_type="model_selection",
                    state_snapshot={
                        "role": role,
                        "phase": phase,
                        "model": model_selection.model,
                        "provider": model_selection.provider,
                        "tier": model_selection.tier.value,
                        "downgraded": model_selection.downgraded,
                        "budget_remaining_pct": model_selection.budget_remaining_pct,
                    },
                    policy="model_router.select_model",
                    outcome=model_selection.reason,
                )
            except Exception:
                pass

        # --- Telemetry: emit agent_call_start (Requirement 8.3) ---
        call_start_time = time.time()
        if telemetry_client:
            try:
                telemetry_client.emit_event(
                    event_type="agent_call_start",
                    task_id=task_id,
                    attributes={"role": role, "phase": phase, "model": request.model or ""},
                )
            except Exception:
                pass

        last_error: Exception | None = None
        for attempt in range(settings.agent_retry_count):
            try:
                result = host_client.agent_think(request)
                # --- Telemetry: emit agent_call_end ---
                if telemetry_client:
                    try:
                        telemetry_client.emit_event(
                            event_type="agent_call_end",
                            task_id=task_id,
                            attributes={
                                "role": role,
                                "phase": phase,
                                "model": request.model or "",
                                "duration_ms": int((time.time() - call_start_time) * 1000),
                                "success": True,
                            },
                        )
                    except Exception:
                        pass

                # --- Cost Client: record cost event ---
                if cost_client:
                    try:
                        cost_client.record_cost(CostRecordRequest(
                            task_id=task_id,
                            role=role,
                            model=request.model or "",
                            provider=request.provider or "",
                            work_phase=phase,
                            input_tokens=getattr(result, "input_tokens", 0) or 0,
                            output_tokens=getattr(result, "output_tokens", 0) or 0,
                            cost_usd=getattr(result, "cost_usd", 0.0) or 0.0,
                            duration_ms=int((time.time() - call_start_time) * 1000),
                        ))
                    except Exception:
                        pass

                # --- Model Router: record outcome ---
                if model_router and model_selection:
                    try:
                        model_router.record_outcome(
                            model=model_selection.model,
                            role=role,
                            tier=model_selection.tier,
                            outcome="success",
                        )
                    except Exception:
                        pass

                return result
            except Exception as exc:
                last_error = exc
                if attempt < settings.agent_retry_count - 1:
                    time.sleep(settings.agent_retry_backoff_seconds * (2 ** attempt))

        # --- Telemetry: emit agent_call_end with failure ---
        if telemetry_client:
            try:
                telemetry_client.emit_event(
                    event_type="agent_call_end",
                    task_id=task_id,
                    attributes={
                        "role": role,
                        "phase": phase,
                        "model": request.model or "",
                        "duration_ms": int((time.time() - call_start_time) * 1000),
                        "success": False,
                        "error": str(last_error)[:200] if last_error else "",
                    },
                )
            except Exception:
                pass

        # --- Model Router: record failure ---
        if model_router and model_selection:
            try:
                model_router.record_outcome(
                    model=model_selection.model,
                    role=role,
                    tier=model_selection.tier,
                    outcome="failure",
                )
            except Exception:
                pass

        # Primary role exhausted, try fallback
        try:
            fallback_agent = router.select_with_fallback(role, [])
            fallback_request = AgentThinkRequest(
                task_id=state["task_id"],
                role=fallback_agent.role,
                prompt=prompt,
                provider=fallback_agent.provider,
                model=fallback_agent.model,
            )
            return host_client.agent_think(fallback_request)
        except (AgentUnavailableError, Exception):
            pass

        raise AgentCallFailedError(
            role=role,
            attempts=settings.agent_retry_count,
            last_error=last_error,
        )

    def _emit_phase_transition(
        state: OrchestratorState, to_phase: str, reason: str
    ) -> None:
        """Emit telemetry and record execution trace for phase transitions."""
        task_id = state.get("task_id", "")
        from_phase = state.get("phase", "unknown")
        if telemetry_client and task_id:
            try:
                telemetry_client.emit_phase_transition(
                    task_id=task_id,
                    from_phase=from_phase,
                    to_phase=to_phase,
                    reason=reason,
                )
            except Exception:
                pass
        if execution_trace and task_id:
            try:
                execution_trace.record_decision(
                    task_id=task_id,
                    decision_type="phase_transition",
                    state_snapshot={"from_phase": from_phase, "to_phase": to_phase},
                    policy="workflow_state_machine",
                    outcome=to_phase,
                    nd_inputs={"reason": reason},
                )
            except Exception:
                pass

    def notify_operator_state(
        state: OrchestratorState, operator_state: str, content: str
    ) -> OrchestratorState:
        channel = str(state.get("operator_channel") or "telegram").strip() or "telegram"
        chat_id = str(state.get("operator_chat_id") or "").strip()
        notifications = [
            dict(item) for item in state.get("operator_notifications", [])
        ]
        entry: dict[str, str | bool] = {
            "state": operator_state,
            "channel": channel,
            "delivered": False,
            "summary": "no operator chat configured",
        }

        if chat_id and channel == "telegram":
            try:
                response = host_client.notify_telegram(
                    ChannelNotificationRequest(
                        channel=channel,
                        chat_id=chat_id,
                        content=content,
                    )
                )
                entry["delivered"] = response.delivered
                entry["summary"] = response.summary
            except Exception as exc:
                entry["summary"] = f"notification failed: {exc}"
        elif chat_id:
            entry["summary"] = f"unsupported operator channel: {channel}"

        notifications.append(entry)
        return {**state, "operator_notifications": notifications}

    def verify_host(state: OrchestratorState) -> OrchestratorState:
        health = host_client.health()
        _emit_phase_transition(state, "host_ready", "host_health_check_passed")
        return {
            **state,
            "status": "running",
            "phase": "host_ready",
            "summary": f"Host ready at {health.workspace_root}",
            "workspace_root": health.workspace_root,
            "host_socket": health.socket_path,
            "restrict_to_workspace": health.restrict_to_workspace,
            "sandboxed": health.sandboxed,
            "telegram_enabled": health.telegram_enabled,
        }

    def discover_team(state: OrchestratorState) -> OrchestratorState:
        try:
            roster = host_client.agent_roster()
        except Exception as exc:
            return {
                **state,
                "status": "failed",
                "phase": "team_unavailable",
                "team_roster": [],
                "team_router_summary": f"Team roster unavailable: {exc}",
                "summary": f"Team discovery failed: {exc}",
            }

        agents = [agent.model_dump() for agent in roster.agents]
        return {
            **state,
            "team_roster": agents,
            "team_router_summary": f"{len(agents)} team route(s) available",
        }

    def provision_workspace(state: OrchestratorState) -> OrchestratorState:
        workspace = host_client.provision_workspace(
            WorkspaceProvisionRequest(
                task_id=state["task_id"],
                repo=RepoRef(
                    path=state["repo_path"],
                    default_branch=state["repo_default_branch"],
                ),
            )
        )
        return {
            **state,
            "status": "running",
            "phase": "workspace_ready",
            "summary": f"Workspace provisioned at {workspace.worktree_path}",
            "task_root": workspace.task_root,
            "artifacts_dir": workspace.artifacts_dir,
            "logs_dir": workspace.logs_dir,
            "scratch_dir": workspace.scratch_dir,
            "worktree_path": workspace.worktree_path,
        }

    def create_worktree(state: OrchestratorState) -> OrchestratorState:
        branch = f"vikram/{state['task_id']}"
        worktree = host_client.create_worktree(
            GitWorktreeCreateRequest(
                task_id=state["task_id"],
                repo=RepoRef(
                    path=state["repo_path"],
                    default_branch=state["repo_default_branch"],
                ),
                worktree_path=state["worktree_path"],
                branch=branch,
                base_ref=state["repo_default_branch"],
            )
        )
        created_text = "created" if worktree.created else "reused"
        return {
            **state,
            "phase": "worktree_ready",
            "summary": f"Git worktree {created_text} at {worktree.worktree_path}",
            "worktree_path": worktree.worktree_path,
            "worktree_branch": worktree.branch,
            "worktree_created": worktree.created,
        }

    def inspect_repo(state: OrchestratorState) -> OrchestratorState:
        inspection = host_client.inspect_repo(
            RepoInspectRequest(
                task_id=state["task_id"],
                repo_path=state["repo_path"],
                worktree_path=state["worktree_path"],
            )
        )
        return {
            **state,
            "phase": "repo_inspected",
            "summary": f"Repository inspected at {inspection.worktree_path}",
            "repo_branch": inspection.branch,
            "repo_head_ref": inspection.head_ref,
            "repo_dirty": inspection.dirty,
            "repo_changed_file_count": inspection.changed_file_count,
            "repo_additions": inspection.additions,
            "repo_deletions": inspection.deletions,
            "repo_diff_short_stat": inspection.diff_short_stat,
            "repo_top_level_entries": inspection.top_level_entries,
            "repo_status_lines": inspection.status_lines,
            "repo_changed_files": [
                {
                    "path": item.path,
                    "status": item.status,
                    "additions": item.additions,
                    "deletions": item.deletions,
                    "binary": item.binary,
                }
                for item in inspection.changed_files
            ],
            "repo_key_files": [
                {
                    "path": item.path,
                    "preview": item.preview,
                    "bytes": item.bytes,
                }
                for item in inspection.key_files
            ],
        }

    def discover_targets(state: OrchestratorState) -> OrchestratorState:
        discovery = host_client.discover_targets(
            RepoTargetDiscoveryRequest(
                task_id=state["task_id"],
                worktree_path=state["worktree_path"],
                objective=state["objective"],
                limit=5,
            )
        )
        return {
            **state,
            "phase": "targets_discovered",
            "summary": f"Discovered {len(discovery.candidates)} target candidates",
            "target_candidates": [
                {
                    "path": item.path,
                    "score": item.score,
                    "reason": item.reason,
                }
                for item in discovery.candidates
            ],
        }

    def read_target_files(state: OrchestratorState) -> OrchestratorState:
        previews: list[dict[str, str | int | bool]] = []
        for candidate in state.get("target_candidates", [])[:3]:
            path = str(candidate.get("path", ""))
            if not path:
                continue
            file_result = host_client.read_file(
                FileReadRequest(
                    task_id=state["task_id"],
                    worktree_path=state["worktree_path"],
                    path=path,
                    max_bytes=3500,
                )
            )
            previews.append(
                {
                    "path": file_result.path,
                    "full_path": file_result.full_path,
                    "content": file_result.content,
                    "bytes_read": file_result.bytes_read,
                    "truncated": file_result.truncated,
                    "reason": str(candidate.get("reason", "")),
                    "score": int(candidate.get("score", 0)),
                }
            )
        return {
            **state,
            "phase": "implementation_ready",
            "summary": f"Loaded {len(previews)} target file previews",
            "target_file_previews": previews,
        }

    def agent_plan(state: OrchestratorState) -> OrchestratorState:
        """Ask the lead agent to analyze the task and produce a plan."""
        objective = str(state.get("objective", ""))
        targets = state.get("target_candidates", [])
        previews = state.get("target_file_previews", [])
        task_id = state["task_id"]
        repo_path = state.get("repo_path", "")

        target_summary = "\n".join(
            f"{t.get('path', '')} (score: {t.get('score', 0)}, reason: {t.get('reason', '')})"
            for t in targets[:6]
        )
        file_content = "\n---\n".join(
            f"File: {p.get('path', '')}\n{p.get('content', '')[:2000]}"
            for p in previews[:3]
        )

        # --- Knowledge Store: inject repo context into planning (Requirement 38.3) ---
        knowledge_context = ""
        if knowledge_store and repo_path:
            try:
                compressed = knowledge_store.get_compressed_context(
                    repo_path=repo_path,
                    objective=objective,
                    max_tokens=2000,
                )
                parts: list[str] = []
                if compressed.module_graph:
                    parts.append("Module dependencies: " + "; ".join(
                        f"{k}->{','.join(v)}" for k, v in list(compressed.module_graph.items())[:10]
                    ))
                if compressed.interface_summaries:
                    parts.append("Key interfaces:\n" + "\n".join(
                        f"  {fp}: {summary[:120]}"
                        for fp, summary in list(compressed.interface_summaries.items())[:5]
                    ))
                # Also inject failure warnings
                warnings = knowledge_store.get_failure_warnings(repo_path, {"objective": objective})
                if warnings:
                    parts.append("Known pitfalls:\n" + "\n".join(
                        f"  - {w.failure_class}: {w.error_signature[:100]}"
                        for w in warnings[:3]
                    ))
                if parts:
                    knowledge_context = "\n\nREPOSITORY KNOWLEDGE:\n" + "\n".join(parts)
            except Exception:
                pass

        # --- Conflict Detector: check for conflicts at planning entry (Requirement 35.1) ---
        conflict_warning = ""
        if conflict_detector and repo_path:
            try:
                # Get active tasks from scheduler if available
                active_tasks_targets: dict[str, list[str]] = {}
                if scheduler:
                    for entry in scheduler.get_queue():
                        if entry.status == "running" and entry.task_id != task_id:
                            active_tasks_targets[entry.task_id] = entry.repos
                if active_tasks_targets:
                    target_paths = [str(t.get("path", "")) for t in targets if t.get("path")]
                    conflicts = conflict_detector.predict_conflicts(
                        new_task_id=task_id,
                        new_task_targets=target_paths or [repo_path],
                        active_tasks=active_tasks_targets,
                    )
                    if conflicts:
                        conflict_warning = "\n\nCONFLICT WARNING: Predicted overlaps with running tasks:\n" + "\n".join(
                            f"  - {c.path} (probability={c.conflict_probability:.0%}, tasks: {c.task_a_id} vs {c.task_b_id})"
                            for c in conflicts[:5]
                        )
            except Exception:
                pass

        _emit_phase_transition(state, "planning", "agent_plan_start")

        prompt = f"""You are the lead engineer. Analyze this task and create a concrete implementation plan.

TASK: {objective}

RELEVANT FILES:
{target_summary}

FILE CONTENTS (preview):
{file_content}{knowledge_context}{conflict_warning}

Produce a plan with:
1. What files need to change and why
2. The sequence of changes (order matters)
3. Risks or edge cases to watch for
4. How to verify the changes work

Keep it specific and actionable. Each step should reference exact file paths."""

        resp = ask_agent(state, "lead", prompt)
        plan_content = resp.content

        return {
            **state,
            "plan_content": plan_content,
            "phase": "planned_by_lead",
            "summary": "Lead agent produced implementation plan",
        }

    def grill_spec(state: OrchestratorState) -> OrchestratorState:
        """Adversarial spec validation: Devil's Advocate attacks the plan, lead revises."""
        plan = str(state.get("plan_content", ""))
        objective = str(state.get("objective", ""))
        if not is_valid_plan(plan):
            return {
                **state,
                "grill_rounds": 0,
                "grill_summary": "Plan quality gate failed",
                "status": "failed",
                "phase": "planning_failed",
                "summary": "Plan quality gate: content is empty, too short, or consists of error messages",
            }

        max_rounds = 3
        critique_history: list[str] = []
        revised_plan = plan

        for round_num in range(1, max_rounds + 1):
            # Round: Devil's Advocate (reviewer model) attacks the plan
            attack_prompt = f"""You are a Devil's Advocate reviewing an implementation plan. Your job is to find flaws.

TASK OBJECTIVE: {objective}

PLAN TO ATTACK:
{revised_plan}

{"PREVIOUS CRITIQUES (already addressed):\n" + "\n---\n".join(critique_history) if critique_history else ""}

Find: architectural flaws, missing edge cases, security concerns, impossible assumptions, scope creep, or risky shortcuts. Be specific — cite exact parts of the plan. If the plan is solid, say CONCEDE."""

            attack = ask_agent(state, "reviewer", attack_prompt).content

            if "CONCEDE" in attack.upper():
                revised_plan += f"\n\n[Round {round_num}: Devil's Advocate conceded — plan accepted]"
                break

            critique_history.append(f"Round {round_num} critique: {attack[:500]}")

            # Round: Lead revises based on critique
            revise_prompt = f"""You are the lead engineer. Your plan was critiqued. Revise it.

ORIGINAL TASK: {objective}

YOUR CURRENT PLAN:
{revised_plan}

CRITIQUE TO ADDRESS:
{attack}

Revise the plan to address all VALID criticisms. Ignore invalid or nitpicking critiques. Produce the complete revised plan (not a diff)."""

            revised_plan = ask_agent(state, "lead", revise_prompt).content

        return {
            **state,
            "plan_content": revised_plan,
            "grill_rounds": round_num,
            "grill_summary": f"Adversarial validation: {round_num} round(s), {len(critique_history)} critiques addressed",
            "phase": "spec_grilled",
        }

    def write_initial_plan(state: OrchestratorState) -> OrchestratorState:
        artifact_id = "plan-initial"
        repo_observations = format_repo_observations(state)
        target_observations = format_target_observations(state)
        next_steps = format_next_steps(state)
        known_risks = format_known_risks(state)
        content = "\n".join(
            [
                "# Initial Plan",
                "",
                f"- Task: {state['task_id']}",
                f"- Objective: {state['objective']}",
                f"- Repo: {state['repo_path']}",
                f"- Branch: {state['worktree_branch']}",
                f"- Worktree: {state['worktree_path']}",
                f"- HEAD: {state['repo_head_ref'][:12]}",
                "",
                "## Repo Observations",
                "",
                *repo_observations,
                "",
                "## Candidate Targets",
                "",
                *target_observations,
                "",
                "## Initial Steps",
                "",
                *next_steps,
                "",
                "## Known Risks",
                "",
                *known_risks,
            ]
        )
        artifact = host_client.write_artifact(
            ArtifactWriteRequest(
                artifact=Artifact(
                    task_id=state["task_id"],
                    artifact_id=artifact_id,
                    kind="plan",
                    title="Initial Plan",
                    summary="First planning artifact for the task",
                ),
                content=content + "\n",
                format="markdown",
            )
        )
        return {
            **state,
            "status": "running",
            "phase": "implementation_ready",
            "summary": f"Initial plan written to {artifact.path}",
            "plan_artifact_id": artifact.artifact.artifact_id,
            "plan_artifact_path": artifact.path,
        }

    def write_implementation_brief(state: OrchestratorState) -> OrchestratorState:
        artifact_id = "implementation-brief"
        content = "\n".join(
            [
                "# Implementation Brief",
                "",
                f"- Task: {state['task_id']}",
                f"- Objective: {state['objective']}",
                "",
                "## Primary Targets",
                "",
                *format_target_observations(state),
                "",
                "## Edit Guidance",
                "",
                *format_edit_guidance(state),
            ]
        )
        artifact = host_client.write_artifact(
            ArtifactWriteRequest(
                artifact=Artifact(
                    task_id=state["task_id"],
                    artifact_id=artifact_id,
                    kind="implementation",
                    title="Implementation Brief",
                    summary="Bounded edit preparation for the task",
                ),
                content=content + "\n",
                format="markdown",
            )
        )
        return {
            **state,
            "phase": "edit_prepared",
            "summary": f"Implementation brief written to {artifact.path}",
            "implementation_artifact_id": artifact.artifact.artifact_id,
            "implementation_artifact_path": artifact.path,
        }

    def discover_lint(state: OrchestratorState) -> OrchestratorState:
        worktree_path = str(state.get("worktree_path", ""))
        lint_candidates: list[dict[str, str]] = []
        try:
            response = host_client.discover_lint(
                LintDiscoveryRequest(
                    task_id=state["task_id"],
                    worktree_path=worktree_path,
                )
            )
            for c in response.candidates:
                lint_candidates.append({
                    "command": c.command,
                    "working_dir": c.working_dir,
                    "runtime": c.runtime,
                    "reason": c.reason,
                })
        except Exception:
            pass  # lint optional

        return {
            **state,
            "lint_candidates": lint_candidates,
        }

    def discover_verification(state: OrchestratorState) -> OrchestratorState:
        target_paths = [
            str(item.get("path", ""))
            for item in state.get("target_candidates", [])
            if str(item.get("path", ""))
        ]
        verification = host_client.discover_verification(
            VerificationDiscoveryRequest(
                task_id=state["task_id"],
                worktree_path=state["worktree_path"],
                target_paths=target_paths[:3],
            )
        )
        return {
            **state,
            "phase": "verification_discovered",
            "summary": f"Discovered {len(verification.candidates)} verification candidates",
            "verification_candidates": [
                {
                    "command": item.command,
                    "working_dir": item.working_dir,
                    "runtime": item.runtime,
                    "reason": item.reason,
                }
                for item in verification.candidates
            ],
        }

    def write_verification_plan(state: OrchestratorState) -> OrchestratorState:
        artifact_id = "verification-initial"
        content = "\n".join(
            [
                "# Verification Plan",
                "",
                f"- Task: {state['task_id']}",
                "",
                "## Candidate Commands",
                "",
                *format_verification_candidates(state),
                "",
                "## Operator Guidance",
                "",
                "- Start with the most targeted verification command before escalating to repository-wide checks.",
                "- Preserve command output as a verification artifact once real execution is wired into the workflow.",
                "- Escalate if no candidate command can validate the touched files with confidence.",
            ]
        )
        artifact = host_client.write_artifact(
            ArtifactWriteRequest(
                artifact=Artifact(
                    task_id=state["task_id"],
                    artifact_id=artifact_id,
                    kind="verification",
                    title="Verification Plan",
                    summary="Focused verification preparation for the task",
                ),
                content=content + "\n",
                format="markdown",
            )
        )
        return {
            **state,
            "phase": "change_ready",
            "summary": f"Implementation and verification prepared in {artifact.path}",
            "verification_artifact_id": artifact.artifact.artifact_id,
            "verification_artifact_path": artifact.path,
        }

    def route_after_discover_team(state: OrchestratorState) -> str:
        if state.get("phase") == "team_unavailable":
            return END
        return "provision_workspace"

    def route_after_preparation(state: OrchestratorState) -> str:
        if state.get("change_request"):
            return "agent_implement"
        return END

    def route_after_agent_verify(state: OrchestratorState):
        """Route after agent_verify: fan out to agent_qa and review_change in parallel
        when max_parallel_workers > 1, otherwise proceed sequentially."""
        max_workers = int(state.get("max_parallel_workers", 1) or 1)
        if max_workers > 1:
            return [Send("agent_qa", state), Send("review_change", state)]
        return "agent_qa"

    def route_after_agent_qa(state: OrchestratorState) -> str:
        """Route after agent_qa: in parallel mode, go directly to
        write_verification_result_artifact. In sequential mode, go to review_change."""
        max_workers = int(state.get("max_parallel_workers", 1) or 1)
        if max_workers > 1:
            return "write_verification_result_artifact"
        return "review_change"

    def agent_implement(state: OrchestratorState) -> OrchestratorState:
        """Ask the engineer agent to produce structured edit specifications."""
        import json

        plan = str(state.get("plan_content", ""))
        objective = str(state.get("objective", ""))
        targets = state.get("target_file_previews", [])
        if not plan:
            return {**state, "impl_content": "No plan available"}

        file_context = "\n".join(
            f"File: {p.get('path','')}\nContent preview:\n{p.get('content','')[:1500]}"
            for p in targets[:5]
        )

        prompt = f"""You are a software engineer. Produce exact code changes.

TASK: {objective}

PLAN:
{plan}

FILE CONTEXT:
{file_context}

Respond with a JSON array of edit objects. Each object must have:
- "path": relative file path
- "old_text": exact text to replace (must appear exactly once in the file)
- "new_text": replacement text
- "rationale": why this change

Example: [{{"path":"src/main.py","old_text":"return x+y","new_text":"return x+y+z","rationale":"add third parameter"}}]

Return ONLY the JSON array, no other text."""

        # ask_agent now handles retries internally via task 3.4
        resp = ask_agent(state, "engineer", prompt)

        # Extract JSON from response
        content = resp.content.strip()
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        content = content.strip()

        # Attempt to parse, with corrective retry on failure
        max_parse_retries = 2
        parse_error: str | None = None
        malformed_output: str = content

        for parse_attempt in range(max_parse_retries + 1):
            try:
                parsed = json.loads(content)
                if isinstance(parsed, list):
                    parsed_edits = [
                        {"path": e.get("path",""), "old_text": e.get("old_text",""),
                         "new_text": e.get("new_text",""), "rationale": e.get("rationale","")}
                        for e in parsed if e.get("path") and e.get("old_text")
                    ]
                    return {
                        **state,
                        "impl_content": "Engineer produced edit specifications",
                        "parsed_edits": parsed_edits,
                        "phase": "implemented_by_engineer",
                    }
                else:
                    parse_error = f"Expected JSON array, got {type(parsed).__name__}"
            except json.JSONDecodeError as exc:
                parse_error = str(exc)

            # If we haven't exhausted retries, send corrective prompt
            if parse_attempt < max_parse_retries:
                corrective_prompt = f"""Your previous response could not be parsed as a JSON array.

PARSE ERROR: {parse_error}

YOUR PREVIOUS OUTPUT (truncated):
{content[:500]}

Please respond with ONLY a valid JSON array of edit objects. Each object must have:
- "path": relative file path
- "old_text": exact text to replace
- "new_text": replacement text
- "rationale": why this change

Return ONLY the JSON array, no markdown, no explanation."""

                resp = ask_agent(state, "engineer", corrective_prompt)
                content = resp.content.strip()
                if "```" in content:
                    content = content.split("```")[1]
                    if content.startswith("json"):
                        content = content[4:]
                content = content.strip()

        # All parse retries exhausted
        return {
            **state,
            "impl_content": f"Implementation parse failed after {max_parse_retries + 1} attempts: {parse_error}",
            "parsed_edits": [],
            "status": "failed",
            "phase": "implementation_parse_failed",
            "summary": f"Engineer output could not be parsed: {parse_error}. Malformed output: {malformed_output[:200]}",
        }

    def agent_verify(state: OrchestratorState) -> OrchestratorState:
        """Ask the runner agent to analyze verification results."""
        runs = state.get("verification_runs", [])
        if not runs:
            return {**state, "runner_verdict": "PASSED", "runner_summary": "No verification runs"}

        output = "\n".join(f"Command: {r.get('command','')}\nSuccess: {r.get('success','')}\n{r.get('output','')[:800]}" for r in runs)
        prompt = f"""Analyze these test/verification results.

{output}

Respond with a JSON object:
{{"verdict": "PASSED" or "FAILED", "summary": "brief explanation", "issues": ["issue1", "issue2"]}}

Return ONLY the JSON object."""

        verdict = "PASSED"
        summary = "Runner unavailable"
        issues: list[str] = []
        try:
            resp = ask_agent(state, "runner", prompt)
            content = resp.content.strip()
            if "```" in content:
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            content = content.strip()
            import json
            result = json.loads(content)
            verdict = str(result.get("verdict", "PASSED")).upper()
            summary = str(result.get("summary", resp.content[:200]))
            issues = [str(i) for i in result.get("issues", [])]
        except Exception:
            pass

        return {
            **state,
            "runner_verdict": verdict,
            "runner_summary": summary,
            "runner_issues": issues,
        }

    def apply_requested_change(state: OrchestratorState) -> OrchestratorState:
        _emit_phase_transition(state, "change_applied", "applying_edits")
        request = state.get("change_request") or {}
        edits = request.get("edits", [])
        # If the engineer produced structured edits, prefer those over the request edits.
        parsed = state.get("parsed_edits")
        if parsed and len(parsed) > 0 and (not edits or len(edits) == 0):
            edits = parsed
        applied_edits: list[dict[str, str | int]] = []

        # Capture lint baseline before edits so we can detect new errors.
        lint_baseline = ""
        lint_command = ""
        lint_candidates = state.get("lint_candidates", [])
        if lint_candidates:
            lint_command = str(lint_candidates[0].get("command", ""))
        worktree = str(state.get("worktree_path", ""))
        if lint_command and worktree:
            try:
                baseline_resp = host_client.run_lint(
                    LintRunRequest(
                        task_id=state["task_id"],
                        worktree_path=worktree,
                        command=lint_command,
                    )
                )
                lint_baseline = baseline_resp.output
            except Exception:
                pass

        for edit in edits:
            path = str(edit.get("path", ""))
            old_text = str(edit.get("old_text", ""))
            new_text = str(edit.get("new_text", ""))
            if not path or not old_text:
                continue

            result = host_client.replace_in_file(
                FileReplaceRequest(
                    task_id=state["task_id"],
                    worktree_path=state["worktree_path"],
                    path=path,
                    old_text=old_text,
                    new_text=new_text,
                )
            )
            applied_edits.append(
                {
                    "path": result.path,
                    "full_path": result.full_path,
                    "bytes_written": result.bytes_written,
                    "rationale": str(edit.get("rationale", "")),
                    "old_text_preview": preview_text(old_text),
                    "new_text_preview": preview_text(new_text),
                    "diff_preview": build_diff_preview(old_text, new_text),
                }
            )

        new_lint_errors: list[str] = []
        if lint_command and worktree:
            try:
                after_resp = host_client.run_lint(
                    LintRunRequest(
                        task_id=state["task_id"],
                        worktree_path=worktree,
                        command=lint_command,
                        baseline=lint_baseline,
                    )
                )
                new_lint_errors = list(after_resp.new_errors)
            except Exception:
                pass
        lint_passed = len(new_lint_errors) == 0 or lint_command == ""

        return {
            **state,
            "phase": "change_applied",
            "status": "running",
            "summary": f"Applied {len(applied_edits)} bounded edits in {state['worktree_path']}",
            "applied_edits": applied_edits,
            "lint_baseline": lint_baseline,
            "lint_command": lint_command,
            "lint_new_errors": new_lint_errors,
            "lint_passed": lint_passed,
            "pending_follow_up_required": False,
            "pending_follow_up_phase": "",
            "pending_follow_up_comment": "",
            "pending_follow_up_proposed_edits": {},
        }

    def write_change_artifact(state: OrchestratorState) -> OrchestratorState:
        attempt = int(state.get("change_attempt", 1) or 1)
        artifact_id = f"change-applied-{attempt}"
        content = change_artifact_content(state)
        artifact = host_client.write_artifact(
            ArtifactWriteRequest(
                artifact=Artifact(
                    task_id=state["task_id"],
                    artifact_id=artifact_id,
                    kind="implementation",
                    title="Applied Change",
                    summary="Bounded edits applied to the task worktree",
                ),
                content=content + "\n",
                format="markdown",
            )
        )
        return {
            **state,
            "phase": "change_recorded",
            "summary": f"Applied change recorded in {artifact.path}",
            "change_artifact_id": artifact.artifact.artifact_id,
            "change_artifact_path": artifact.path,
        }

    def agent_qa(state: OrchestratorState) -> OrchestratorState:
        """Ask the QA agent to generate and evaluate browser tests."""
        objective = str(state.get("objective", ""))
        worktree = str(state.get("worktree_path", ""))
        if not worktree:
            return {**state, "qa_result": "No worktree for QA"}

        prompt = f"""You are a QA engineer. Generate a simple Playwright browser test for this task.

TASK: {objective}

Write a Node.js script using Playwright that:
1. Opens a browser
2. Navigates to the relevant page
3. Takes a screenshot
4. Verifies basic functionality
5. Closes the browser

Return ONLY the JavaScript code, no explanation."""

        qa_result = "QA unavailable"
        try:
            resp = ask_agent(state, "qa", prompt)
            test_script = resp.content.strip()
            if test_script.startswith("```"):
                test_script = test_script.split("```")[1]
                if test_script.startswith("javascript") or test_script.startswith("js"):
                    test_script = "\n".join(test_script.split("\n")[1:])
            test_script = test_script.strip()

            if test_script and "require" in test_script:
                browser_resp = host_client.browser_test(
                    BrowserTestRequest(task_id=state["task_id"], worktree_path=worktree, test_script=test_script)
                )
                qa_result = f"Browser test: {'PASSED' if browser_resp.success else 'FAILED'}\n{browser_resp.output[:500]}"
            else:
                qa_result = "QA agent could not generate a valid test script"
        except Exception as exc:
            qa_result = f"QA failed: {exc}"

        return {**state, "qa_result": qa_result}

    def review_change(state: OrchestratorState) -> OrchestratorState:
        """Ask the reviewer model (different from the implementer) to evaluate changes."""
        review_verdict = "APPROVE"
        review_issues: list[str] = []
        review_summary = "No reviewer configured"

        worktree = str(state.get("worktree_path", ""))
        if not worktree:
            return {**state, "review_verdict": review_verdict, "review_issues": review_issues, "review_summary": review_summary}

        # Build a diff summary from applied edits and verification output
        diff_lines: list[str] = []
        for edit in state.get("applied_edits", []):
            path = str(edit.get("path", ""))
            old_preview = str(edit.get("old_text_preview", ""))
            new_preview = str(edit.get("new_text_preview", ""))
            if path:
                diff_lines.append(f"--- {path}")
                diff_lines.append(f"- {old_preview}")
                diff_lines.append(f"+ {new_preview}")
                diff_lines.append("")
        diff_text = "\n".join(diff_lines) if diff_lines else "(no diff available)"

        test_output = ""
        for run in state.get("verification_runs", []):
            test_output += f"Command: {run.get('command', '')}\n"
            test_output += f"Success: {run.get('success', False)}\n"
            test_output += f"Output: {run.get('output', '')}\n\n"

        lint_errors = [str(e) for e in state.get("lint_new_errors", [])]

        try:
            response = host_client.review_change(
                ChangeReviewRequest(
                    task_id=state["task_id"],
                    objective=str(state.get("objective", "")),
                    diff=diff_text,
                    test_output=test_output,
                    lint_errors=lint_errors,
                )
            )
            review_verdict = response.verdict
            review_issues = list(response.issues)
            review_summary = response.summary
        except Exception as exc:
            review_summary = f"Review unavailable: {exc}"

        return {
            **state,
            "review_verdict": review_verdict,
            "review_issues": review_issues,
            "review_summary": review_summary,
        }

    def run_verification(state: OrchestratorState) -> OrchestratorState:
        request = state.get("change_request") or {}
        requested_commands = [
            str(item).strip()
            for item in request.get("verification_commands", [])
            if str(item).strip()
        ]
        candidate_commands = [
            str(item.get("command", "")).strip()
            for item in state.get("verification_candidates", [])
            if str(item.get("command", "")).strip()
        ]
        commands = requested_commands or candidate_commands[:1]

        task_id = state["task_id"]
        worktree = state.get("worktree_path", "")

        _emit_phase_transition(state, "verification", "run_verification_start")

        # --- Verification Protocol: select strategy and run properties (Requirement 26.1) ---
        if verification_protocol and worktree:
            try:
                # Determine change type from plan/context
                change_type = "logic"  # default
                plan = str(state.get("plan_content", "")).lower()
                if "documentation" in plan or "readme" in plan:
                    change_type = "documentation"
                elif "config" in plan:
                    change_type = "config"
                elif "refactor" in plan:
                    change_type = "refactoring"

                # Get file types from targets
                file_types = list({
                    Path(str(t.get("path", ""))).suffix
                    for t in state.get("target_candidates", [])
                    if Path(str(t.get("path", ""))).suffix
                })

                strategy = verification_protocol.select_strategy(
                    change_type=change_type,
                    file_types=file_types,
                    task_id=task_id,
                )

                # Generate properties from the plan
                properties = verification_protocol.generate_properties(
                    plan=str(state.get("plan_content", "")),
                    target_files=[str(t.get("path", "")) for t in state.get("target_candidates", [])],
                    language="python" if ".py" in str(file_types) else "typescript",
                    task_id=task_id,
                )

                # Execute full verification with the protocol
                if properties:
                    vp_result = verification_protocol.run_full_verification(
                        worktree=str(worktree),
                        strategy=strategy,
                        properties=properties,
                        host_client=host_client,
                        task_id=task_id,
                    )
                    # Convert VP results into verification_runs format
                    for prop_result in vp_result.property_results:
                        commands_already_run = True  # VP handled execution
                        verification_runs: list[dict[str, str | bool | int]] = []
                        verification_runs.append({
                            "command": f"property: {prop_result.property_name}",
                            "success": prop_result.passed,
                            "summary": prop_result.diagnostic or "passed",
                            "output": prop_result.output[:500] if prop_result.output else "",
                        })

                    all_success = vp_result.outcome.value in ("verified_correct", "tests_pass")
                    return {
                        **state,
                        "phase": "verification_passed" if all_success else "verification_failed",
                        "status": "running" if all_success else "failed",
                        "summary": f"Verification Protocol: {vp_result.outcome.value} ({len(properties)} properties)",
                        "verification_runs": verification_runs,
                        "verification_outcome": "passed" if all_success else "failed",
                    }
            except Exception as exc:
                logger.warning("Verification Protocol failed, falling back to standard: %s", exc)
                # Fall through to standard verification below

        # Standard verification fallback
        verification_runs: list[dict[str, str | bool | int]] = []
        all_success = True
        for command in commands:
            observation = host_client.exec(
                HostActionRequest(
                    task_id=state["task_id"],
                    action_name="exec",
                    arguments={"command": command},
                    working_dir=state["worktree_path"],
                )
            )
            verification_runs.append(
                {
                    "command": command,
                    "success": observation.success,
                    "summary": observation.summary,
                    "output": observation.output,
                }
            )
            if not observation.success:
                all_success = False
                break

        return {
            **state,
            "phase": "verification_passed" if all_success else "verification_failed",
            "status": "running" if all_success else "failed",
            "summary": verification_summary(state["worktree_path"], verification_runs, all_success),
            "verification_runs": verification_runs,
            "verification_outcome": "passed" if all_success else "failed",
        }

    def write_verification_result_artifact(
        state: OrchestratorState,
    ) -> OrchestratorState:
        attempt = int(state.get("change_attempt", 1) or 1)
        artifact_id = f"verification-result-{attempt}"
        content = verification_result_artifact_content(state)
        artifact = host_client.write_artifact(
            ArtifactWriteRequest(
                artifact=Artifact(
                    task_id=state["task_id"],
                    artifact_id=artifact_id,
                    kind="verification",
                    title="Verification Result",
                    summary="Verification output after bounded change application",
                ),
                content=content + "\n",
                format="markdown",
            )
        )
        return {
            **state,
            "phase": state.get("phase", "verification_unknown"),
            "summary": f"{state.get('summary', 'Verification complete')} ({artifact.path})",
            "verification_result_artifact_id": artifact.artifact.artifact_id,
            "verification_result_artifact_path": artifact.path,
            "change_request": {},
        }

    def evaluate_approval_policy(state: OrchestratorState) -> OrchestratorState:
        task_id = state.get("task_id", "")

        _emit_phase_transition(state, "approval", "evaluate_approval_policy")

        # --- Approval Matrix: evaluate risk and routing (Requirement 30.1) ---
        if approval_matrix:
            try:
                # Build change_context for the approval matrix
                applied_paths = [
                    str(edit.get("path", ""))
                    for edit in state.get("applied_edits", [])
                    if str(edit.get("path", "")).strip()
                ]
                change_context: dict[str, Any] = {
                    "changed_files": applied_paths,
                    "files_changed": len(applied_paths),
                    "lines_changed": sum(
                        int(edit.get("bytes_written", 0))
                        for edit in state.get("applied_edits", [])
                    ),
                    "repo_path": state.get("repo_path", ""),
                    "cost_consumed_pct": 0.0,
                }

                # Get confidence score if available
                tier = "moderate"  # default
                if model_router:
                    try:
                        target_count = len(state.get("target_candidates", []) or [])
                        signals = ComplexitySignals(
                            objective_scope=state.get("objective", ""),
                            target_file_count=max(target_count, 1),
                            repo_size_files=len(state.get("repo_top_level_entries", []) or []),
                            language_count=1,
                            change_type="logic",
                            security_relevant=False,
                            test_modification=False,
                        )
                        tier = model_router.classify_complexity(signals).value
                    except Exception:
                        pass

                confidence = approval_matrix.get_confidence(
                    tier=tier,
                    repo=state.get("repo_path", ""),
                )
                change_context["confidence_score"] = confidence.score

                # Get cost consumed percentage
                if cost_client and state.get("max_cost_usd"):
                    try:
                        cumulative = cost_client.get_task_cost(task_id)
                        change_context["cost_consumed_pct"] = cumulative / float(state["max_cost_usd"])
                    except Exception:
                        pass

                # Classify risk
                risk = approval_matrix.classify_risk([
                    {"signal": "files_changed", "value": str(len(applied_paths))},
                    {"signal": "verification_outcome", "value": str(state.get("verification_outcome", ""))},
                ])
                change_context["risk_level"] = risk.level

                # Evaluate through matrix
                matrix_decision = approval_matrix.evaluate(change_context)

                # Map matrix routing to workflow routing
                route_map = {
                    "auto_approve": "auto_complete",
                    "founder_review": "founder_review",
                    "escalate_and_halt": "stop",
                }
                mapped_route = route_map.get(matrix_decision.routing, "founder_review")

                # --- Execution Trace: record approval evaluation (Requirement 6.1) ---
                if execution_trace:
                    try:
                        execution_trace.record_decision(
                            task_id=task_id,
                            decision_type="approval_routing",
                            state_snapshot={
                                "risk_level": risk.level,
                                "matched_rule": matrix_decision.matched_rule_name,
                                "confidence_score": confidence.score,
                                "files_changed": len(applied_paths),
                            },
                            policy=f"approval_matrix.rule:{matrix_decision.matched_rule_name or 'default'}",
                            outcome=mapped_route,
                        )
                    except Exception:
                        pass

                # Build compatible ApprovalPolicyDecision
                reasons = [matrix_decision.reason]
                if risk.level in ("high", "critical"):
                    reasons.append(f"risk classified as {risk.level}")
                verification_outcome = str(state.get("verification_outcome", "")).strip()
                options = approval_options_for_route_internal(mapped_route, verification_outcome)

                return {
                    **state,
                    "approval_risk": risk.level,
                    "approval_route": mapped_route,
                    "approval_reasons": reasons,
                    "approval_summary": matrix_decision.reason,
                    "approval_options": options,
                }
            except Exception as exc:
                logger.warning("Approval Matrix evaluation failed, falling back to policy: %s", exc)
                # Fall through to legacy policy

        # Fallback to existing policy logic
        decision: ApprovalPolicyDecision = decide_approval_policy(state)
        updated = {
            **state,
            "approval_risk": decision.risk_class,
            "approval_route": decision.route,
            "approval_reasons": decision.reasons,
            "approval_summary": decision.summary,
            "approval_options": decision.options,
        }
        if decision.route == "stop":
            updated["summary"] = decision.summary

        # --- Execution Trace: record fallback approval evaluation ---
        if execution_trace and task_id:
            try:
                execution_trace.record_decision(
                    task_id=task_id,
                    decision_type="approval_routing",
                    state_snapshot={
                        "risk_class": decision.risk_class,
                        "route": decision.route,
                    },
                    policy="policy.decide_approval_policy",
                    outcome=decision.route,
                )
            except Exception:
                pass

        return updated

    def route_after_approval_policy(state: OrchestratorState) -> str:
        route = str(state.get("approval_route", "stop")).strip() or "stop"
        if route == "founder_review":
            return "write_approval_request_artifact"
        if route == "auto_complete":
            return "finalize_without_review"
        return END

    def approval_options_for_route_internal(route: str, verification_outcome: str) -> list[str]:
        """Build approval options list for the given route."""
        if route != "founder_review":
            return []
        options = ["approve", "reject", "clarify"]
        if verification_outcome != "failed":
            options.insert(1, "edit_and_approve")
        return options

    def finalize_without_review(state: OrchestratorState) -> OrchestratorState:
        summary = str(state.get("approval_summary", "")).strip()
        if not summary:
            summary = "Change completed without founder review"
        return {
            **state,
            "status": "running",
            "phase": "auto_approved",
            "summary": summary,
        }

    def write_approval_request_artifact(
        state: OrchestratorState,
    ) -> OrchestratorState:
        attempt = int(state.get("change_attempt", 1) or 1)
        reasons = founder_review_reasons(state)
        approval_request = ApprovalRequest(
            task_id=state["task_id"],
            phase="founder_review_requested",
            risk_class=str(state.get("approval_risk") or "high"),
            route="founder_review",
            reasons=reasons,
            reason="; ".join(reasons) or "founder review required",
            summary=founder_review_summary(state),
            options=approval_options(state),
            related_artifacts=[
                item
                for item in [
                    state.get("change_artifact_path", ""),
                    state.get("verification_result_artifact_path", ""),
                ]
                if item
            ],
        )
        artifact_id = f"approval-request-{attempt}"
        artifact = host_client.write_artifact(
            ArtifactWriteRequest(
                artifact=Artifact(
                    task_id=state["task_id"],
                    artifact_id=artifact_id,
                    kind="founder_approval",
                    title="Founder Approval Request",
                    summary="Founder review requested for the latest change attempt",
                ),
                content=format_approval_request_artifact(state, approval_request) + "\n",
                format="markdown",
            )
        )
        return {
            **state,
            "status": "awaiting_approval",
            "phase": "founder_review_requested",
            "summary": f"Founder review requested in {artifact.path}",
            "approval_request": approval_request.model_dump(),
            "approval_artifact_id": artifact.artifact.artifact_id,
            "approval_artifact_path": artifact.path,
        }

    def notify_founder(state: OrchestratorState) -> OrchestratorState:
        channel = str(state.get("operator_channel") or "telegram").strip() or "telegram"
        updated = notify_operator_state(
            state, "awaiting_approval", format_founder_notification(state)
        )
        latest = updated.get("operator_notifications", [])[-1]
        if not latest.get("delivered"):
            return {
                **updated,
                "summary": f"{state['summary']} ({latest.get('summary', 'not notified')})",
            }
        return {
            **updated,
            "summary": f"{state['summary']} (founder notified on {channel})",
        }

    def founder_review_gate(state: OrchestratorState):
        decision = interrupt(state.get("approval_request", {}))
        decision_kind = str(decision.get("decision", "clarify")).strip() or "clarify"
        update = {"founder_decision": decision}

        if decision_kind == "approve":
            return Command(update=update, goto="founder_approve")
        if decision_kind == "reject":
            return Command(update=update, goto="founder_reject")
        if decision_kind == "edit_and_approve":
            return Command(update=update, goto="founder_edit_requested")
        return Command(update=update, goto="founder_clarify")

    def founder_approve(state: OrchestratorState) -> OrchestratorState:
        decision = state.get("founder_decision", {})
        comment = str(decision.get("comment", "")).strip()
        if state.get("verification_outcome") == "failed":
            summary = "Founder approved the change despite failed verification"
            if comment:
                summary += f": {comment}"
            return {
                **state,
                "status": "completed",
                "phase": "founder_override_approved",
                "summary": summary,
                "pending_follow_up_required": False,
                "pending_follow_up_phase": "",
                "pending_follow_up_comment": "",
                "pending_follow_up_proposed_edits": {},
            }
        summary = "Founder approved the change"
        if comment:
            summary += f": {comment}"
        return {
            **state,
            "status": "completed",
            "phase": "founder_approved",
            "summary": summary,
            "pending_follow_up_required": False,
            "pending_follow_up_phase": "",
            "pending_follow_up_comment": "",
            "pending_follow_up_proposed_edits": {},
        }

    def founder_reject(state: OrchestratorState) -> OrchestratorState:
        decision = state.get("founder_decision", {})
        comment = str(decision.get("comment", "")).strip()
        summary = "Founder rejected the change"
        if comment:
            summary += f": {comment}"

        _emit_phase_transition(state, "founder_rejected", "founder_decision_reject")

        # --- Approval Matrix: decrement confidence on rejection ---
        if approval_matrix and state.get("repo_path"):
            try:
                tier = "moderate"
                if model_router:
                    target_count = len(state.get("target_candidates", []) or [])
                    signals = ComplexitySignals(
                        objective_scope=state.get("objective", ""),
                        target_file_count=max(target_count, 1),
                        repo_size_files=len(state.get("repo_top_level_entries", []) or []),
                        language_count=1,
                        change_type="logic",
                        security_relevant=False,
                        test_modification=False,
                    )
                    tier = model_router.classify_complexity(signals).value
                approval_matrix.decrement_confidence(tier=tier, repo=state["repo_path"])
            except Exception:
                pass

        # --- Knowledge Store: record failure for learning ---
        if knowledge_store and state.get("repo_path") and state.get("task_id"):
            try:
                knowledge_store.extract_from_task(
                    task_id=state["task_id"],
                    repo_path=state["repo_path"],
                    outcome="failure",
                )
            except Exception:
                pass

        # Rollback the worktree to the pre-edit snapshot.
        worktree_path = state.get("worktree_path", "")
        if worktree_path:
            try:
                rollback_resp = host_client.rollback_worktree(
                    GitRollbackRequest(
                        task_id=state["task_id"],
                        worktree_path=str(worktree_path),
                    )
                )
                if rollback_resp.rolled_back:
                    summary += " (worktree rolled back to pre-edit state)"
            except Exception as exc:
                summary += f" (rollback unavailable: {exc})"

        return {
            **state,
            "status": "failed",
            "phase": "founder_rejected",
            "summary": summary,
            "pending_follow_up_required": False,
            "pending_follow_up_phase": "",
            "pending_follow_up_comment": "",
            "pending_follow_up_proposed_edits": {},
        }

    def founder_edit_requested(state: OrchestratorState) -> OrchestratorState:
        decision = state.get("founder_decision", {})
        comment = str(decision.get("comment", "")).strip()
        summary = "Founder requested follow-up edits before approval"
        if comment:
            summary += f": {comment}"
        updated = {
            **state,
            "status": "paused",
            "phase": "founder_edit_requested",
            "summary": summary,
            "pending_follow_up_required": True,
            "pending_follow_up_phase": "founder_edit_requested",
            "pending_follow_up_comment": comment,
            "pending_follow_up_proposed_edits": dict(decision.get("proposed_edits", {})),
        }
        return notify_operator_state(
            updated,
            "retryable",
            format_operator_state_notification(updated, "retryable"),
        )

    def founder_clarify(state: OrchestratorState) -> OrchestratorState:
        decision = state.get("founder_decision", {})
        comment = str(decision.get("comment", "")).strip()
        summary = "Founder requested clarification"
        if comment:
            summary += f": {comment}"
        updated = {
            **state,
            "status": "paused",
            "phase": "founder_clarification_requested",
            "summary": summary,
            "pending_follow_up_required": True,
            "pending_follow_up_phase": "founder_clarification_requested",
            "pending_follow_up_comment": comment,
            "pending_follow_up_proposed_edits": dict(decision.get("proposed_edits", {})),
        }
        return notify_operator_state(
            updated,
            "retryable",
            format_operator_state_notification(updated, "retryable"),
        )

    def write_founder_decision_artifact(
        state: OrchestratorState,
    ) -> OrchestratorState:
        attempt = int(state.get("change_attempt", 1) or 1)
        artifact_id = f"founder-decision-{attempt}"
        artifact = host_client.write_artifact(
            ArtifactWriteRequest(
                artifact=Artifact(
                    task_id=state["task_id"],
                    artifact_id=artifact_id,
                    kind="founder_approval",
                    title="Founder Decision",
                    summary="Founder response for the latest approval request",
                ),
                content=format_founder_decision_artifact(state) + "\n",
                format="markdown",
            )
        )
        return {
            **state,
            "summary": f"{state.get('summary', 'Founder decision recorded')} ({artifact.path})",
            "founder_decision_artifact_id": artifact.artifact.artifact_id,
            "founder_decision_artifact_path": artifact.path,
        }

    def inspect_post_change_repo(state: OrchestratorState) -> OrchestratorState:
        inspection = host_client.inspect_repo(
            RepoInspectRequest(
                task_id=state["task_id"],
                repo_path=state["repo_path"],
                worktree_path=state["worktree_path"],
            )
        )
        return {
            **state,
            "status": "running",
            "post_change_dirty": inspection.dirty,
            "post_change_branch": inspection.branch,
            "post_change_head_ref": inspection.head_ref,
            "post_change_status_lines": inspection.status_lines,
            "post_change_changed_file_count": inspection.changed_file_count,
            "post_change_additions": inspection.additions,
            "post_change_deletions": inspection.deletions,
            "post_change_diff_short_stat": inspection.diff_short_stat,
            "post_change_changed_files": [
                {
                    "path": item.path,
                    "status": item.status,
                    "additions": item.additions,
                    "deletions": item.deletions,
                    "binary": item.binary,
                }
                for item in inspection.changed_files
            ],
            "summary": f"Post-change repo state inspected in {inspection.worktree_path}",
        }

    def evaluate_merge_readiness(state: OrchestratorState) -> OrchestratorState:
        blockers: list[str] = []
        notes: list[str] = []
        changed_file_count = int(state.get("post_change_changed_file_count", 0) or 0)
        additions = int(state.get("post_change_additions", 0) or 0)
        deletions = int(state.get("post_change_deletions", 0) or 0)
        post_branch = str(state.get("post_change_branch", "")).strip()
        expected_branch = str(state.get("worktree_branch", "")).strip()
        if not state.get("applied_edits"):
            blockers.append("No bounded edits were recorded for this attempt.")
        if state.get("verification_outcome") != "passed":
            blockers.append("Focused verification did not pass for this attempt.")
        if changed_file_count == 0:
            blockers.append("No changed files were detected in the task worktree.")
        if expected_branch and post_branch and post_branch != expected_branch:
            blockers.append(
                f"Post-change branch `{post_branch}` does not match expected task branch `{expected_branch}`."
            )
        if state.get("pending_follow_up_required"):
            blockers.append("Pending follow-up is still open.")
        if state.get("phase") == "founder_override_approved":
            blockers.append(
                "Founder override approved a change with failed verification; merge remains blocked until a passing run exists."
            )
        if post_branch:
            notes.append(f"Merge handoff branch: `{post_branch}`.")
        if state.get("post_change_head_ref"):
            notes.append(f"HEAD at handoff: `{str(state['post_change_head_ref'])[:12]}`.")
        if state.get("post_change_status_lines"):
            notes.append("Current git status captured for merge review.")
        if state.get("post_change_diff_short_stat"):
            notes.append(f"Diff summary: {state['post_change_diff_short_stat']}.")
        if state.get("approval_route") == "auto_complete":
            notes.append("Task followed the low-risk auto-approval route.")
        if state.get("phase") == "founder_approved":
            notes.append("Founder approved the current bounded change attempt.")

        if blockers:
            summary = "Change is not merge-ready because " + "; ".join(blockers)
            return {
                **state,
                "merge_readiness": "blocked",
                "merge_summary": summary,
                "merge_blockers": blockers,
                "merge_notes": notes,
            }

        summary = (
            f"Change is ready to merge from `{post_branch or expected_branch or 'task branch'}`"
            f" with {changed_file_count} changed file"
            f"{'' if changed_file_count == 1 else 's'}"
            f" (+{additions}/-{deletions})."
        )
        return {
            **state,
            "merge_readiness": "ready",
            "merge_summary": summary,
            "merge_blockers": [],
            "merge_notes": notes,
        }

    def write_merge_readiness_artifact(state: OrchestratorState) -> OrchestratorState:
        attempt = int(state.get("change_attempt", 1) or 1)
        artifact_id = f"merge-readiness-{attempt}"
        content = merge_readiness_artifact_content(state)
        artifact = host_client.write_artifact(
            ArtifactWriteRequest(
                artifact=Artifact(
                    task_id=state["task_id"],
                    artifact_id=artifact_id,
                    kind="review",
                    title="Merge Readiness",
                    summary="Post-approval merge-readiness assessment for the latest attempt",
                ),
                content=content + "\n",
                format="markdown",
            )
        )
        return {
            **state,
            "merge_artifact_id": artifact.artifact.artifact_id,
            "merge_artifact_path": artifact.path,
            "summary": f"{state.get('merge_summary', 'Merge readiness assessed')} ({artifact.path})",
        }

    def write_archive_email_draft(state: OrchestratorState) -> OrchestratorState:
        if state.get("merge_readiness") != "ready":
            return state
        attempt = int(state.get("change_attempt", 1) or 1)
        artifact_id = f"archive-email-draft-{attempt}"
        artifact = host_client.write_artifact(
            ArtifactWriteRequest(
                artifact=Artifact(
                    task_id=state["task_id"],
                    artifact_id=artifact_id,
                    kind="archive",
                    title="Archive Email Draft",
                    summary="Founder archival email draft for the approved merge-ready handoff",
                ),
                content=archive_email_draft_content(state) + "\n",
                format="eml",
            )
        )
        return {
            **state,
            "archive_artifact_id": artifact.artifact.artifact_id,
            "archive_artifact_path": artifact.path,
            "summary": f"{state.get('summary', 'Archive email draft prepared')} ({artifact.path})",
        }

    def finalize_merge_readiness(state: OrchestratorState) -> OrchestratorState:
        summary = str(state.get("summary", state.get("merge_summary", ""))).strip()
        task_id = state.get("task_id", "")
        repo_path = state.get("repo_path", "")

        if state.get("merge_readiness") == "ready":
            _emit_phase_transition(state, "merge_ready", "merge_readiness_confirmed")

            # --- Knowledge Store: record outcome for learning (Requirement 38.3) ---
            if knowledge_store and repo_path and task_id:
                try:
                    knowledge_store.extract_from_task(
                        task_id=task_id,
                        repo_path=repo_path,
                        outcome="success",
                    )
                except Exception:
                    pass

            # --- Approval Matrix: increment confidence on success ---
            if approval_matrix and repo_path:
                try:
                    tier = "moderate"
                    if model_router:
                        target_count = len(state.get("target_candidates", []) or [])
                        signals = ComplexitySignals(
                            objective_scope=state.get("objective", ""),
                            target_file_count=max(target_count, 1),
                            repo_size_files=len(state.get("repo_top_level_entries", []) or []),
                            language_count=1,
                            change_type="logic",
                            security_relevant=False,
                            test_modification=False,
                        )
                        tier = model_router.classify_complexity(signals).value
                    approval_matrix.increment_confidence(tier=tier, repo=repo_path)
                except Exception:
                    pass

            updated = {
                **state,
                "status": "completed",
                "phase": "merge_ready",
                "summary": summary or "Change is ready to merge",
            }
            return notify_operator_state(
                updated,
                "merge_ready",
                format_operator_state_notification(updated, "merge_ready"),
            )

        _emit_phase_transition(state, "merge_blocked", "merge_readiness_blocked")

        updated = {
            **state,
            "status": "paused",
            "phase": "merge_blocked",
            "summary": summary or "Change is not merge-ready",
        }
        return notify_operator_state(
            updated,
            "blocked",
            format_operator_state_notification(updated, "blocked"),
        )

    def route_after_founder_decision_artifact(state: OrchestratorState) -> str:
        if state.get("phase") in {"founder_approved", "founder_override_approved"}:
            return "inspect_post_change_repo"
        return END

    builder = StateGraph(OrchestratorState)
    builder.add_node("verify_host", verify_host)
    builder.add_node("discover_team", discover_team)
    builder.add_node("provision_workspace", provision_workspace)
    builder.add_node("create_worktree", create_worktree)
    builder.add_node("inspect_repo", inspect_repo)
    builder.add_node("discover_targets", discover_targets)
    builder.add_node("read_target_files", read_target_files)
    builder.add_node("agent_plan", agent_plan)
    builder.add_node("grill_spec", grill_spec)
    builder.add_node("write_initial_plan", write_initial_plan)
    builder.add_node("write_implementation_brief", write_implementation_brief)
    builder.add_node("discover_lint", discover_lint)
    builder.add_node("discover_verification", discover_verification)
    builder.add_node("write_verification_plan", write_verification_plan)
    builder.add_node("agent_implement", agent_implement)
    builder.add_node("agent_verify", agent_verify)
    builder.add_node("apply_requested_change", apply_requested_change)
    builder.add_node("write_change_artifact", write_change_artifact)
    builder.add_node("run_verification", run_verification)
    builder.add_node("agent_qa", agent_qa)
    builder.add_node("review_change", review_change)
    builder.add_node("write_verification_result_artifact", write_verification_result_artifact)
    builder.add_node("evaluate_approval_policy", evaluate_approval_policy)
    builder.add_node("finalize_without_review", finalize_without_review)
    builder.add_node("write_approval_request_artifact", write_approval_request_artifact)
    builder.add_node("notify_founder", notify_founder)
    builder.add_node("founder_review_gate", founder_review_gate)
    builder.add_node("founder_approve", founder_approve)
    builder.add_node("founder_reject", founder_reject)
    builder.add_node("founder_edit_requested", founder_edit_requested)
    builder.add_node("founder_clarify", founder_clarify)
    builder.add_node("write_founder_decision_artifact", write_founder_decision_artifact)
    builder.add_node("inspect_post_change_repo", inspect_post_change_repo)
    builder.add_node("evaluate_merge_readiness", evaluate_merge_readiness)
    builder.add_node("write_merge_readiness_artifact", write_merge_readiness_artifact)
    builder.add_node("write_archive_email_draft", write_archive_email_draft)
    builder.add_node("finalize_merge_readiness", finalize_merge_readiness)
    builder.add_edge(START, "verify_host")
    builder.add_edge("verify_host", "discover_team")
    builder.add_conditional_edges("discover_team", route_after_discover_team)
    builder.add_edge("provision_workspace", "create_worktree")
    builder.add_edge("create_worktree", "inspect_repo")
    builder.add_edge("inspect_repo", "discover_targets")
    builder.add_edge("discover_targets", "read_target_files")
    builder.add_edge("read_target_files", "agent_plan")
    builder.add_edge("agent_plan", "grill_spec")
    builder.add_edge("grill_spec", "write_initial_plan")
    builder.add_edge("write_initial_plan", "write_implementation_brief")
    builder.add_edge("write_implementation_brief", "discover_lint")
    builder.add_edge("discover_lint", "discover_verification")
    builder.add_edge("discover_verification", "write_verification_plan")
    builder.add_conditional_edges("write_verification_plan", route_after_preparation)
    builder.add_edge("agent_implement", "apply_requested_change")
    builder.add_edge("apply_requested_change", "write_change_artifact")
    builder.add_edge("write_change_artifact", "run_verification")
    builder.add_edge("run_verification", "agent_verify")
    builder.add_conditional_edges(
        "agent_verify",
        route_after_agent_verify,
        ["agent_qa", "review_change"],
    )
    builder.add_conditional_edges(
        "agent_qa",
        route_after_agent_qa,
        ["write_verification_result_artifact", "review_change"],
    )
    builder.add_edge("review_change", "write_verification_result_artifact")
    builder.add_edge("write_verification_result_artifact", "evaluate_approval_policy")
    builder.add_conditional_edges("evaluate_approval_policy", route_after_approval_policy)
    builder.add_edge("finalize_without_review", "inspect_post_change_repo")
    builder.add_edge("write_approval_request_artifact", "notify_founder")
    builder.add_edge("notify_founder", "founder_review_gate")
    builder.add_edge("founder_approve", "write_founder_decision_artifact")
    builder.add_edge("founder_reject", "write_founder_decision_artifact")
    builder.add_edge("founder_edit_requested", "write_founder_decision_artifact")
    builder.add_edge("founder_clarify", "write_founder_decision_artifact")
    builder.add_conditional_edges(
        "write_founder_decision_artifact", route_after_founder_decision_artifact
    )
    builder.add_edge("inspect_post_change_repo", "evaluate_merge_readiness")
    builder.add_edge("evaluate_merge_readiness", "write_merge_readiness_artifact")
    builder.add_edge("write_merge_readiness_artifact", "write_archive_email_draft")
    builder.add_edge("write_archive_email_draft", "finalize_merge_readiness")
    builder.add_edge("finalize_merge_readiness", END)
    graph = builder.compile(checkpointer=checkpointer)
    setattr(graph, "_vikram_checkpoint_conn", checkpoint_conn)
    return graph


def close_graph(graph) -> None:
    checkpoint_conn = getattr(graph, "_vikram_checkpoint_conn", None)
    if checkpoint_conn is None:
        return
    checkpoint_conn.close()
    setattr(graph, "_vikram_checkpoint_conn", None)


def apply_change_request(graph, task: TaskSession, request: TaskChangeRequest) -> TaskSession:
    config = {"configurable": {"thread_id": task.task_id}}
    snapshot = graph.get_state(config)
    snapshot_values = snapshot.values or {}
    change_attempt = int(snapshot_values.get("change_attempt", 0)) + 1
    follow_up_comment = str(snapshot_values.get("pending_follow_up_comment", "")).strip()
    follow_up_phase = str(snapshot_values.get("pending_follow_up_phase", "")).strip()
    follow_up_proposed_edits = dict(
        snapshot_values.get("pending_follow_up_proposed_edits", {}) or {}
    )
    if not follow_up_comment and snapshot_values.get("phase") == "merge_blocked":
        follow_up_comment = str(snapshot_values.get("merge_summary", "")).strip()
        follow_up_phase = "merge_blocked"
        follow_up_proposed_edits = {
            "merge_blockers": list(snapshot_values.get("merge_blockers", []) or []),
            "merge_notes": list(snapshot_values.get("merge_notes", []) or []),
        }
    summary = request.summary.strip() or f"Received {len(request.edits)} bounded edits for execution"
    if follow_up_comment:
        summary = f"{summary} (founder follow-up: {follow_up_comment})"
    resumed_config = graph.update_state(
        snapshot.config,
        values={
            "change_request": request.model_dump(),
            "change_attempt": change_attempt,
            "status": "running",
            "phase": "change_requested",
            "summary": summary,
            "active_follow_up_phase": follow_up_phase,
            "active_follow_up_comment": follow_up_comment,
            "active_follow_up_proposed_edits": follow_up_proposed_edits,
            "pending_follow_up_required": False,
            "pending_follow_up_phase": "",
            "pending_follow_up_comment": "",
            "pending_follow_up_proposed_edits": {},
            "merge_readiness": "unknown",
            "merge_summary": "",
            "merge_blockers": [],
            "merge_notes": [],
            "merge_artifact_id": "",
            "merge_artifact_path": "",
            "archive_artifact_id": "",
            "archive_artifact_path": "",
            "post_change_dirty": False,
            "post_change_branch": "",
            "post_change_head_ref": "",
            "post_change_status_lines": [],
            "post_change_changed_file_count": 0,
            "post_change_additions": 0,
            "post_change_deletions": 0,
            "post_change_diff_short_stat": "",
            "post_change_changed_files": [],
        },
        as_node="write_verification_plan",
    )
    result = graph.invoke(None, resumed_config)
    return task_session_from_existing(task, result)


def resume_from_founder_decision(
    graph, task: TaskSession, decision: ApprovalDecision
) -> TaskSession:
    config = {"configurable": {"thread_id": task.task_id}}
    result = graph.invoke(Command(resume=decision.model_dump()), config)
    return task_session_from_existing(task, result)


def change_artifact_content(state: OrchestratorState) -> str:
    attempt = int(state.get("change_attempt", 1) or 1)
    request = state.get("change_request") or {}
    parts = [
        "# Applied Change",
        "",
        f"- Task: {state['task_id']}",
        f"- Attempt: {attempt}",
        f"- Summary: {str(request.get('summary', '')).strip() or 'bounded change proposal applied'}",
        "",
    ]
    follow_up_lines = format_follow_up_context(state)
    if follow_up_lines:
        parts.extend(follow_up_lines)
        parts.append("")
    parts.extend(
        [
            "## Applied Edits",
            "",
            *format_applied_edits(state),
        ]
    )
    return "\n".join(parts)


def verification_result_artifact_content(state: OrchestratorState) -> str:
    attempt = int(state.get("change_attempt", 1) or 1)
    return "\n".join(
        [
            "# Verification Result",
            "",
            f"- Task: {state['task_id']}",
            f"- Attempt: {attempt}",
            f"- Outcome: {state.get('phase', 'verification_unknown')}",
            "",
            "## Command Runs",
            "",
            *format_verification_runs(state),
        ]
    )


def merge_readiness_artifact_content(state: OrchestratorState) -> str:
    blockers = [
        f"- {item}"
        for item in state.get("merge_blockers", [])
        if str(item).strip()
    ] or ["- No merge blockers recorded."]
    notes = [
        f"- {item}"
        for item in state.get("merge_notes", [])
        if str(item).strip()
    ] or ["- No additional notes."]
    status_lines = [
        f"- `{item}`"
        for item in state.get("post_change_status_lines", [])
        if str(item).strip()
    ] or ["- No git status lines recorded."]
    changed_files = [
        format_changed_file(item)
        for item in state.get("post_change_changed_files", [])
        if str(item.get("path", "")).strip()
    ] or ["- No changed files recorded."]
    return "\n".join(
        [
            "# Merge Readiness",
            "",
            f"- Task: {state['task_id']}",
            f"- Attempt: {int(state.get('change_attempt', 1) or 1)}",
            f"- Assessment: {state.get('merge_readiness', 'unknown')}",
            f"- Summary: {state.get('merge_summary', '')}",
            f"- Branch: `{state.get('post_change_branch', '') or state.get('worktree_branch', '')}`",
            f"- HEAD: `{str(state.get('post_change_head_ref', '') or state.get('repo_head_ref', ''))[:12]}`",
            f"- Changed files: {int(state.get('post_change_changed_file_count', 0) or 0)}",
            f"- Additions: {int(state.get('post_change_additions', 0) or 0)}",
            f"- Deletions: {int(state.get('post_change_deletions', 0) or 0)}",
            f"- Diff stat: {state.get('post_change_diff_short_stat', '') or 'No tracked diff stat recorded.'}",
            "",
            "## Blockers",
            "",
            *blockers,
            "",
            "## Notes",
            "",
            *notes,
            "",
            "## Git Status",
            "",
            *status_lines,
            "",
            "## Changed Files",
            "",
            *changed_files,
        ]
    )


def archive_email_draft_content(state: OrchestratorState) -> str:
    task_id = state["task_id"]
    subject = f"[Vikram] {task_id} merge-ready handoff"
    branch = str(state.get("post_change_branch", "") or state.get("worktree_branch", ""))
    head = str(state.get("post_change_head_ref", "") or state.get("repo_head_ref", ""))
    artifact_paths = [
        str(state.get("change_artifact_path", "")).strip(),
        str(state.get("verification_result_artifact_path", "")).strip(),
        str(state.get("approval_artifact_path", "")).strip(),
        str(state.get("founder_decision_artifact_path", "")).strip(),
        str(state.get("merge_artifact_path", "")).strip(),
    ]
    artifact_paths = [item for item in artifact_paths if item]
    return "\n".join(
        [
            "To: founder-archive@vikram.local",
            f"Subject: {subject}",
            "Content-Type: text/plain; charset=utf-8",
            f"X-Vikram-Task-ID: {task_id}",
            f"X-Vikram-Phase: {state.get('phase', '')}",
            f"X-Vikram-Merge-Readiness: {state.get('merge_readiness', 'unknown')}",
            "",
            "Vikram prepared a merge-ready engineering handoff.",
            "",
            "Task",
            f"- Objective: {state.get('objective', '')}",
            f"- Summary: {state.get('summary', '')}",
            f"- Risk: {state.get('approval_risk', 'unknown')}",
            f"- Approval route: {state.get('approval_route', 'unknown')}",
            "",
            "Merge Handoff",
            f"- Branch: {branch or 'unknown'}",
            f"- HEAD: {head[:12] if head else 'unknown'}",
            f"- Merge summary: {state.get('merge_summary', '')}",
            f"- Changed files: {int(state.get('post_change_changed_file_count', 0) or 0)}",
            f"- Diff stat: {state.get('post_change_diff_short_stat', '') or 'No tracked diff stat recorded.'}",
            "",
            "Verification",
            *format_verification_runs(state),
            "",
            "Artifacts",
            *([f"- {item}" for item in artifact_paths] or ["- No artifacts recorded."]),
            "",
            "Changed Files",
            *format_changed_file_lines_for_archive(state),
        ]
    )


def format_changed_file_lines_for_archive(state: OrchestratorState) -> list[str]:
    return [
        format_changed_file(item)
        for item in state.get("post_change_changed_files", [])
        if str(item.get("path", "")).strip()
    ] or ["- No changed files recorded."]


def format_changed_file(item: dict[str, str | int | bool]) -> str:
    path = str(item.get("path", "")).strip()
    status = str(item.get("status", "")).strip() or "changed"
    additions = int(item.get("additions", 0) or 0)
    deletions = int(item.get("deletions", 0) or 0)
    binary = bool(item.get("binary", False))
    stat = "binary" if binary else f"+{additions}/-{deletions}"
    return f"- `{status}` `{path}` ({stat})"


def format_repo_observations(state: OrchestratorState) -> list[str]:
    observations = [
        f"- Active branch in worktree: `{state['repo_branch']}`",
        f"- Worktree dirty: `{'yes' if state['repo_dirty'] else 'no'}`",
    ]

    top_level_entries = state.get("repo_top_level_entries", [])
    if top_level_entries:
        observations.append(f"- Top-level entries: {', '.join(top_level_entries[:8])}")

    key_files = state.get("repo_key_files", [])
    if key_files:
        observations.append("- Key files detected:")
        for item in key_files[:5]:
            preview = str(item.get("preview", "")).strip().splitlines()
            first_line = preview[0] if preview else ""
            observations.append(
                f"  - `{item.get('path', '')}`: {first_line[:120] or 'file present'}"
            )
    else:
        observations.append("- No standard key files detected in the initial scan.")

    status_lines = state.get("repo_status_lines", [])
    if status_lines:
        observations.append("- Existing status lines:")
        for line in status_lines[:5]:
            observations.append(f"  - `{line}`")

    return observations


def format_target_observations(state: OrchestratorState) -> list[str]:
    targets = state.get("target_file_previews", [])
    if not targets:
        return ["- No target previews were loaded, so planning must fall back to repository-level cues."]

    observations: list[str] = []
    for item in targets:
        path = str(item.get("path", ""))
        reason = str(item.get("reason", ""))
        score = int(item.get("score", 0))
        content = str(item.get("content", "")).strip().splitlines()
        first_line = content[0] if content else "preview available"
        observations.append(
            f"- `{path}` (score {score}): {reason or 'candidate selected'}"
        )
        observations.append(f"  - Preview: {first_line[:140]}")
    return observations


def format_edit_guidance(state: OrchestratorState) -> list[str]:
    targets = state.get("target_file_previews", [])
    if not targets:
        return [
            "- No bounded previews are available yet, so the first edit pass must begin with another inspection step."
        ]

    guidance: list[str] = []
    for item in targets[:3]:
        path = str(item.get("path", ""))
        content = str(item.get("content", "")).splitlines()
        anchor = content[0][:120] if content else "preview available"
        guidance.append(f"- Prefer starting in `{path}` near: `{anchor}`")
    guidance.append(
        "- Keep the first patch bounded to the smallest target file set before widening scope."
    )
    return guidance


def format_next_steps(state: OrchestratorState) -> list[str]:
    candidate_paths = [
        str(item.get("path", ""))
        for item in state.get("target_candidates", [])
        if str(item.get("path", ""))
    ]
    if candidate_paths:
        first_targets = ", ".join(candidate_paths[:3])
        steps = [
            f"1. Start with the localized candidate files: {first_targets}.",
            "2. Narrow the implementation slice to the smallest file set that can satisfy the objective.",
            "3. Make the bounded code change on the task branch.",
        ]
    else:
        steps = [
            "1. Inspect the identified key files inside the task worktree to locate the best change point.",
            "2. Narrow the implementation slice to the smallest file set that can satisfy the objective.",
            "3. Make the bounded code change on the task branch.",
        ]

    if detected_runtime(state):
        steps.append(
            f"4. Run focused verification using the detected runtime or build surface: `{detected_runtime(state)}`."
        )
    else:
        steps.append("4. Discover the smallest verification command available in the repository.")

    steps.append(
        "5. Produce review evidence and escalate only on blocker or approval gate."
    )
    return steps


def format_verification_candidates(state: OrchestratorState) -> list[str]:
    candidates = state.get("verification_candidates", [])
    if not candidates:
        return ["- No verification command candidates discovered yet."]

    lines: list[str] = []
    for item in candidates:
        command = str(item.get("command", ""))
        reason = str(item.get("reason", ""))
        runtime = str(item.get("runtime", ""))
        lines.append(
            f"- `{command}`"
            + (f" ({runtime})" if runtime else "")
            + (f": {reason}" if reason else "")
        )
    return lines


def format_applied_edits(state: OrchestratorState) -> list[str]:
    applied_edits = state.get("applied_edits", [])
    if not applied_edits:
        return ["- No bounded edits were applied."]

    lines: list[str] = []
    for item in applied_edits:
        path = str(item.get("path", ""))
        bytes_written = int(item.get("bytes_written", 0))
        rationale = str(item.get("rationale", ""))
        line = f"- `{path}` ({bytes_written} bytes written)"
        if rationale:
            line += f": {rationale}"
        lines.append(line)
        old_text_preview = str(item.get("old_text_preview", "")).strip()
        new_text_preview = str(item.get("new_text_preview", "")).strip()
        diff_preview = str(item.get("diff_preview", "")).strip()
        if old_text_preview:
            lines.append(f"  - Before: {old_text_preview}")
        if new_text_preview:
            lines.append(f"  - After: {new_text_preview}")
        if diff_preview:
            lines.append("  - Diff preview:")
            for diff_line in diff_preview.splitlines():
                lines.append(f"    {diff_line}")
    return lines


def format_follow_up_context(state: OrchestratorState) -> list[str]:
    comment = str(state.get("active_follow_up_comment", "")).strip()
    proposed = state.get("active_follow_up_proposed_edits", {}) or {}
    phase = str(state.get("active_follow_up_phase", "")).strip()
    if not comment and not proposed:
        return []

    lines = ["## Founder Follow-Up Context", ""]
    if phase:
        lines.append(f"- Requested During Phase: `{phase}`")
    if comment:
        lines.append(f"- Founder Comment: {comment}")
    if proposed:
        lines.append("- Founder Proposed Edits:")
        for key, value in proposed.items():
            lines.append(f"  - `{key}`: {value}")
    return lines


def format_verification_runs(state: OrchestratorState) -> list[str]:
    verification_runs = state.get("verification_runs", [])
    if not verification_runs:
        return ["- No verification commands were executed."]

    lines: list[str] = []
    for item in verification_runs:
        command = str(item.get("command", ""))
        success = bool(item.get("success", False))
        summary = str(item.get("summary", ""))
        output = str(item.get("output", "")).strip()
        lines.append(
            f"- `{command}`: {'passed' if success else 'failed'}"
            + (f" ({summary})" if summary else "")
        )
        if output:
            first_line = output.splitlines()[0][:180]
            lines.append(f"  - Output: {first_line}")
    return lines


def founder_review_required(state: OrchestratorState) -> bool:
    if state.get("approval_route") == "founder_review":
        return True
    if state.get("approval_route") in {"auto_complete", "stop"}:
        return False
    return decide_approval_policy(state).route == "founder_review"


def founder_review_reasons(state: OrchestratorState) -> list[str]:
    reasons = state.get("approval_reasons", [])
    if isinstance(reasons, list) and reasons:
        return [str(item).strip() for item in reasons if str(item).strip()]
    return decide_approval_policy(state).reasons


def approval_options(state: OrchestratorState) -> list[str]:
    stored = state.get("approval_options", [])
    if isinstance(stored, list) and stored:
        return [str(item).strip() for item in stored if str(item).strip()]
    options = ["approve", "reject", "clarify"]
    if state.get("verification_outcome") != "failed":
        options.insert(1, "edit_and_approve")
    return options


def founder_review_summary(state: OrchestratorState) -> str:
    if str(state.get("approval_summary", "")).strip():
        return str(state["approval_summary"]).strip()
    reasons = founder_review_reasons(state)
    if reasons:
        return "Founder review required because " + "; ".join(reasons)
    return "Founder review requested"


def format_approval_request_artifact(
    state: OrchestratorState, approval_request: ApprovalRequest
) -> str:
    related_artifacts = approval_request.related_artifacts or []
    artifact_lines = [f"- `{item}`" for item in related_artifacts] or [
        "- No related artifacts were attached."
    ]
    return "\n".join(
        [
            "# Founder Approval Request",
            "",
            f"- Task: {state['task_id']}",
            f"- Attempt: {int(state.get('change_attempt', 1) or 1)}",
            f"- Risk Class: {approval_request.risk_class}",
            f"- Policy Route: {approval_request.route}",
            f"- Reason: {approval_request.reason}",
            f"- Summary: {approval_request.summary}",
            "",
            "## Policy Reasons",
            "",
            *(
                [f"- {item}" for item in approval_request.reasons]
                or ["- No policy reasons were recorded."]
            ),
            "",
            "## Related Artifacts",
            "",
            *artifact_lines,
            "",
            "## Available Decisions",
            "",
            *[f"- `{item}`" for item in approval_request.options],
        ]
    )


def format_founder_notification(state: OrchestratorState) -> str:
    approval_request = state.get("approval_request", {})
    lines = [
        f"Vikram operator state: `awaiting_approval` for `{state['task_id']}`.",
        "",
        f"Risk class: `{approval_request.get('risk_class', 'unknown')}`",
        "",
        str(approval_request.get("summary", "Founder review requested")),
    ]
    if state.get("verification_result_artifact_path"):
        lines.append("")
        lines.append(
            f"Verification artifact: `{state['verification_result_artifact_path']}`"
        )
    if state.get("approval_artifact_path"):
        lines.append(f"Approval artifact: `{state['approval_artifact_path']}`")
    return "\n".join(lines)


def format_operator_state_notification(
    state: OrchestratorState, operator_state: str
) -> str:
    lines = [
        f"Vikram operator state: `{operator_state}` for `{state['task_id']}`.",
        "",
        str(state.get("summary") or state.get("merge_summary") or "State updated."),
    ]
    if state.get("approval_route"):
        lines.extend(["", f"Approval route: `{state['approval_route']}`"])
    if state.get("merge_readiness"):
        lines.append(f"Merge readiness: `{state['merge_readiness']}`")
    if state.get("merge_summary"):
        lines.append(f"Merge summary: {state['merge_summary']}")
    blockers = [
        str(item).strip()
        for item in state.get("merge_blockers", [])
        if str(item).strip()
    ]
    if blockers:
        lines.extend(["", "Blockers:"])
        lines.extend(f"- {item}" for item in blockers)
    artifact_paths = [
        str(state.get("approval_artifact_path", "")).strip(),
        str(state.get("founder_decision_artifact_path", "")).strip(),
        str(state.get("merge_artifact_path", "")).strip(),
    ]
    artifact_paths = [item for item in artifact_paths if item]
    if artifact_paths:
        lines.extend(["", "Artifacts:"])
        lines.extend(f"- `{item}`" for item in artifact_paths)
    return "\n".join(lines)


def format_founder_decision_artifact(state: OrchestratorState) -> str:
    decision = state.get("founder_decision", {})
    return "\n".join(
        [
            "# Founder Decision",
            "",
            f"- Task: {state['task_id']}",
            f"- Attempt: {int(state.get('change_attempt', 1) or 1)}",
            f"- Decision: {str(decision.get('decision', 'unknown'))}",
            f"- Comment: {str(decision.get('comment', '')).strip() or 'none'}",
            f"- Resulting Phase: {state.get('phase', 'unknown')}",
        ]
    )


def format_known_risks(state: OrchestratorState) -> list[str]:
    risks = []
    if state.get("repo_dirty"):
        risks.append(
            "- The worktree already has git status entries, so implementation must avoid trampling existing changes."
        )
    if not state.get("target_candidates"):
        risks.append(
            "- Target discovery returned no scored candidates, so localization is still weak for this objective."
        )
    if not state.get("verification_candidates"):
        risks.append(
            "- No focused verification commands have been discovered yet, so patch validation may still fall back to broad checks."
        )
    if not state.get("repo_key_files"):
        risks.append(
            "- The initial repo scan found no standard manifest files, so project structure may be custom."
        )
    risks.append(
        "- The objective may still need clarification once repository internals are inspected in detail."
    )
    return risks


def detected_runtime(state: OrchestratorState) -> str:
    key_files = state.get("repo_key_files", [])
    paths = {str(item.get("path", "")) for item in key_files}
    if "go.mod" in paths:
        return "go"
    if "package.json" in paths:
        return "node"
    if "pyproject.toml" in paths or "requirements.txt" in paths:
        return "python"
    if "Cargo.toml" in paths:
        return "rust"
    return ""


def verification_summary(
    worktree_path: str,
    verification_runs: list[dict[str, str | bool | int]],
    all_success: bool,
) -> str:
    if not verification_runs:
        return f"No verification command was available for {worktree_path}"
    first = verification_runs[0]
    command = str(first.get("command", ""))
    if all_success:
        return f"Verification passed in {worktree_path} using `{command}`"
    return f"Verification failed in {worktree_path} using `{command}`"
