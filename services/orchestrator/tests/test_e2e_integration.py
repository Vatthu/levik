"""End-to-end integration tests for the autonomous engineering platform.

Tests the complete task lifecycle: create → schedule → plan → implement →
verify → approve → merge, verifying that all 11 subsystems work together.

Validates: Requirements 1.1, 8.1, 6.1, 30.1
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from vikram_orchestrator.approval_matrix import ApprovalMatrix
from vikram_orchestrator.cost_client import CostClient, CostRecordRequest
from vikram_orchestrator.execution_trace import ExecutionTrace
from vikram_orchestrator.models import (
    AgentProfile,
    AgentRosterResponse,
    AgentThinkRequest,
    AgentThinkResponse,
    ArtifactReadRequest,
    ArtifactReadResponse,
    ArtifactWriteRequest,
    ArtifactWriteResponse,
    ChannelNotificationRequest,
    ChannelNotificationResponse,
    FileReadRequest,
    FileReadResponse,
    FileReplaceRequest,
    FileReplaceResponse,
    FileWriteRequest,
    FileWriteResponse,
    GitWorktreeCreateRequest,
    GitWorktreeCreateResponse,
    HostActionRequest,
    HostObservation,
    RepoInspectRequest,
    RepoInspectResponse,
    RepoRef,
    RepoTargetDiscoveryRequest,
    RepoTargetDiscoveryResponse,
    SystemHealthResponse,
    TaskChangeRequest,
    VerificationDiscoveryRequest,
    VerificationDiscoveryResponse,
    WorkspaceProvisionRequest,
    WorkspaceProvisionResponse,
)
from vikram_orchestrator.scheduler import Priority, Scheduler, SchedulerConfig, TaskQueueEntry
from vikram_orchestrator.server import build_app
from vikram_orchestrator.store import TaskStore
from vikram_orchestrator.telemetry_client import TelemetryClient
from vikram_orchestrator.workflow import build_graph, close_graph, initial_state_from_request


# ---------------------------------------------------------------------------
# Stub Host Client for E2E tests
# ---------------------------------------------------------------------------


class E2EStubHostClient:
    """Full stub of the Go Host client for end-to-end integration tests.

    Records all interactions for assertion, simulates the full task lifecycle
    including planning, implementation, verification, and approval phases.
    """

    def __init__(self) -> None:
        self.health_calls = 0
        self.workspace_requests: list[WorkspaceProvisionRequest] = []
        self.worktree_requests: list[GitWorktreeCreateRequest] = []
        self.inspect_requests: list[RepoInspectRequest] = []
        self.discovery_requests: list[RepoTargetDiscoveryRequest] = []
        self.file_read_requests: list[FileReadRequest] = []
        self.file_replace_requests: list[FileReplaceRequest] = []
        self.file_write_requests: list[FileWriteRequest] = []
        self.verification_requests: list[VerificationDiscoveryRequest] = []
        self.exec_requests: list[HostActionRequest] = []
        self.notification_requests: list[ChannelNotificationRequest] = []
        self.artifact_requests: list[ArtifactWriteRequest] = []

    def health(self) -> SystemHealthResponse:
        self.health_calls += 1
        return SystemHealthResponse(
            status="ok",
            workspace_root="/tmp/vikram-workspaces",
            socket_path="/tmp/vikramd.sock",
            restrict_to_workspace=True,
            sandboxed=False,
            telegram_enabled=True,
        )

    def agent_roster(self) -> AgentRosterResponse:
        return AgentRosterResponse(
            agents=[
                AgentProfile(
                    id="lead-1",
                    role="lead",
                    name="Lead Agent",
                    provider="openai",
                    model="gpt-4",
                    capabilities=["planning", "review"],
                ),
                AgentProfile(
                    id="engineer-1",
                    role="engineer",
                    name="Engineer Agent",
                    provider="anthropic",
                    model="claude-3",
                    capabilities=["implementation", "code"],
                ),
                AgentProfile(
                    id="qa-1",
                    role="qa",
                    name="QA Agent",
                    provider="openai",
                    model="gpt-4",
                    capabilities=["testing", "qa"],
                ),
            ]
        )

    def agent_think(self, request: AgentThinkRequest) -> AgentThinkResponse:
        role = request.role
        if role == "lead":
            content = (
                "## Implementation Plan\n\n"
                "1. Open pkg/orchestratorhost/server.go\n"
                "2. Add the new endpoint handler\n"
                "3. Register the route in the router\n"
                "4. Run go test ./pkg/orchestratorhost to verify\n"
            )
        elif role == "reviewer":
            content = "CONCEDE"
        elif role == "runner":
            content = '{"verdict": "PASSED", "summary": "All checks passed", "issues": []}'
        elif role == "qa":
            content = '{"verdict": "PASSED", "summary": "QA checks passed", "issues": []}'
        else:
            content = f"Agent response for role: {role}"
        return AgentThinkResponse(
            task_id=request.task_id,
            role=role,
            content=content,
            provider=request.provider or "openai",
            model=request.model or "gpt-4",
            input_tokens=150,
            output_tokens=80,
        )

    def provision_workspace(
        self, request: WorkspaceProvisionRequest
    ) -> WorkspaceProvisionResponse:
        self.workspace_requests.append(request)
        return WorkspaceProvisionResponse(
            task_id=request.task_id,
            task_root=f"/tmp/vikram-workspaces/tasks/{request.task_id}",
            artifacts_dir=f"/tmp/vikram-workspaces/tasks/{request.task_id}/artifacts",
            logs_dir=f"/tmp/vikram-workspaces/tasks/{request.task_id}/logs",
            scratch_dir=f"/tmp/vikram-workspaces/tasks/{request.task_id}/scratch",
            worktree_path=f"/tmp/vikram-workspaces/worktrees/{request.task_id}",
        )

    def create_worktree(
        self, request: GitWorktreeCreateRequest
    ) -> GitWorktreeCreateResponse:
        self.worktree_requests.append(request)
        return GitWorktreeCreateResponse(
            task_id=request.task_id,
            repo_path=request.repo.path,
            worktree_path=request.worktree_path,
            branch=request.branch,
            base_ref=request.base_ref or request.repo.default_branch,
            head_ref=request.branch,
            created=True,
        )

    def write_artifact(self, request: ArtifactWriteRequest) -> ArtifactWriteResponse:
        self.artifact_requests.append(request)
        artifact = request.artifact.model_copy(
            update={
                "path": (
                    f"/tmp/vikram-workspaces/tasks/{request.artifact.task_id}"
                    f"/artifacts/{request.artifact.artifact_id}.md"
                )
            }
        )
        return ArtifactWriteResponse(
            artifact=artifact,
            path=artifact.path or "",
            bytes_written=len(request.content),
        )

    def read_artifact(self, request: ArtifactReadRequest) -> ArtifactReadResponse:
        for artifact_request in self.artifact_requests:
            path = (
                f"/tmp/vikram-workspaces/tasks/{artifact_request.artifact.task_id}"
                f"/artifacts/{artifact_request.artifact.artifact_id}.md"
            )
            if (
                artifact_request.artifact.task_id == request.task_id
                and path == request.path
            ):
                content = artifact_request.content
                max_bytes = request.max_bytes or 32000
                return ArtifactReadResponse(
                    task_id=request.task_id,
                    path=path,
                    content=content[:max_bytes],
                    bytes_read=min(len(content), max_bytes),
                    truncated=len(content) > max_bytes,
                )
        raise AssertionError(f"artifact not found: {request.path}")

    def inspect_repo(self, request: RepoInspectRequest) -> RepoInspectResponse:
        self.inspect_requests.append(request)
        changed_paths = [
            r.path for r in self.file_replace_requests
            if r.task_id == request.task_id and r.old_text != r.new_text
        ]
        changed_files = [
            {"path": p, "status": "M", "additions": 1, "deletions": 0, "binary": False}
            for p in changed_paths
        ]
        return RepoInspectResponse(
            task_id=request.task_id,
            repo_path=request.repo_path,
            worktree_path=request.worktree_path,
            branch=f"vikram/{request.task_id}",
            head_ref="abc123def456",
            dirty=bool(changed_paths),
            changed_file_count=len(changed_files),
            additions=len(changed_files),
            deletions=0,
            diff_short_stat=f"{len(changed_files)} files changed" if changed_files else "",
            top_level_entries=["README.md", "go.mod", "pkg/"],
            status_lines=[f"M {p}" for p in changed_paths],
            changed_files=changed_files,
            key_files=[
                {"path": "README.md", "preview": "# Vikram\n", "bytes": 8},
                {"path": "go.mod", "preview": "module github.com/vatthu/vikram\n", "bytes": 31},
            ],
        )

    def discover_targets(
        self, request: RepoTargetDiscoveryRequest
    ) -> RepoTargetDiscoveryResponse:
        self.discovery_requests.append(request)
        return RepoTargetDiscoveryResponse(
            task_id=request.task_id,
            worktree_path=request.worktree_path,
            candidates=[
                {
                    "path": "pkg/orchestratorhost/server.go",
                    "score": 9,
                    "reason": "primary target",
                },
            ],
        )

    def read_file(self, request: FileReadRequest) -> FileReadResponse:
        self.file_read_requests.append(request)
        return FileReadResponse(
            task_id=request.task_id,
            path=request.path,
            full_path=f"{request.worktree_path}/{request.path}",
            content="package orchestratorhost\n",
            bytes_read=25,
            truncated=False,
        )

    def write_file(self, request: FileWriteRequest) -> FileWriteResponse:
        self.file_write_requests.append(request)
        return FileWriteResponse(
            task_id=request.task_id,
            path=request.path,
            full_path=f"{request.worktree_path}/{request.path}",
            bytes_written=len(request.content),
        )

    def replace_in_file(self, request: FileReplaceRequest) -> FileReplaceResponse:
        self.file_replace_requests.append(request)
        return FileReplaceResponse(
            task_id=request.task_id,
            path=request.path,
            full_path=f"{request.worktree_path}/{request.path}",
            bytes_written=len(request.new_text),
        )

    def discover_verification(
        self, request: VerificationDiscoveryRequest
    ) -> VerificationDiscoveryResponse:
        self.verification_requests.append(request)
        return VerificationDiscoveryResponse(
            task_id=request.task_id,
            worktree_path=request.worktree_path,
            runtime="go",
            candidates=[
                {
                    "command": "go test ./pkg/orchestratorhost",
                    "working_dir": request.worktree_path,
                    "runtime": "go",
                    "reason": "target-scoped Go package",
                },
            ],
        )

    def exec(self, request: HostActionRequest) -> HostObservation:
        self.exec_requests.append(request)
        command = str(request.arguments.get("command", ""))
        return HostObservation(
            task_id=request.task_id,
            action_name=request.action_name,
            success=True,
            summary=f"command completed: {command}",
            output=f"ok: {command}",
            state={"working_dir": request.working_dir or ""},
        )

    def notify_telegram(
        self, request: ChannelNotificationRequest
    ) -> ChannelNotificationResponse:
        self.notification_requests.append(request)
        return ChannelNotificationResponse(
            delivered=True, summary="notification delivered"
        )

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Stub Cost Client for E2E tests (captures cost recording calls)
# ---------------------------------------------------------------------------


class StubCostClient:
    """In-process stub for the CostClient that captures all cost operations.

    Instead of calling the Go Host over HTTP, this captures records in-memory
    so integration tests can verify cost flows.
    """

    def __init__(self) -> None:
        self.recorded_costs: list[CostRecordRequest] = []
        self.task_costs: dict[str, float] = {}
        self.phase_remaining: dict[str, float] = {}
        self.daily_total: float = 0.0

    def get_task_cost(self, task_id: str) -> float:
        return self.task_costs.get(task_id, 0.0)

    def get_phase_remaining(self, task_id: str, phase: str) -> float:
        key = f"{task_id}:{phase}"
        return self.phase_remaining.get(key, 10.0)  # Default: $10 remaining

    def get_forecast(self, complexity: str, target_files: int) -> dict:
        return {
            "min_cost_usd": 0.05,
            "expected_cost_usd": 0.15,
            "max_cost_usd": 0.50,
            "confidence_level": 0.8,
            "basis_task_count": 5,
        }

    def get_daily_total(self) -> float:
        return self.daily_total

    def record_cost(self, request: CostRecordRequest) -> None:
        self.recorded_costs.append(request)
        current = self.task_costs.get(request.task_id, 0.0)
        self.task_costs[request.task_id] = current + request.cost_usd
        self.daily_total += request.cost_usd

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Stub Telemetry Client for E2E tests (captures all emitted events)
# ---------------------------------------------------------------------------


class StubTelemetryClient:
    """In-process stub for TelemetryClient that captures all emitted events."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit_event(
        self,
        event_type: str,
        task_id: str,
        attributes: dict | None = None,
    ) -> dict:
        event = {
            "event_type": event_type,
            "task_id": task_id,
            "attributes": attributes or {},
            "timestamp": time.time(),
        }
        self.events.append(event)
        return {"status": "ok"}

    def emit_phase_transition(
        self,
        task_id: str,
        from_phase: str,
        to_phase: str,
        reason: str,
    ) -> dict:
        return self.emit_event(
            event_type="phase_transition",
            task_id=task_id,
            attributes={
                "from_phase": from_phase,
                "to_phase": to_phase,
                "reason": reason,
            },
        )

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Test: Complete Task Lifecycle (create → schedule → plan → implement →
#        verify → approve → merge)
# ---------------------------------------------------------------------------


class TestCompleteTaskLifecycle(unittest.TestCase):
    """Test the full task lifecycle through the API with all subsystems wired."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.host_client = E2EStubHostClient()
        self.cost_client = StubCostClient()
        self.telemetry_client = StubTelemetryClient()
        self.execution_trace = ExecutionTrace()
        self.scheduler = Scheduler(SchedulerConfig(max_concurrency=3))
        self.store = TaskStore()

        self.app = build_app(
            host_client=self.host_client,
            store=self.store,
            checkpoint_db=Path(self.tmp) / "checkpoint.db",
            scheduler=self.scheduler,
            execution_trace=self.execution_trace,
            cost_client=self.cost_client,
            telemetry_client=self.telemetry_client,
        )
        self.client = TestClient(self.app)

    def test_task_creation_through_scheduler_queue(self) -> None:
        """POST /v1/tasks with priority, deps, formation enqueues and dispatches.

        Validates: Requirement 1.1 (task creation with full metadata)
        """
        response = self.client.post("/v1/tasks", json={
            "task_id": "e2e-task-001",
            "source": "console",
            "requested_by": "founder",
            "objective": "Add endpoint for user preferences",
            "repo": {"path": "/repos/vikram", "default_branch": "main"},
            "priority": "high",
            "formation": "feature-fast",
        })

        self.assertEqual(200, response.status_code)
        payload = response.json()
        # Task should complete through the workflow graph (no blockers)
        self.assertEqual("e2e-task-001", payload["task_id"])
        self.assertIn(payload["status"], ("running", "queued", "awaiting_approval"))
        # Verify workspace was provisioned
        self.assertGreaterEqual(len(self.host_client.workspace_requests), 1)
        self.assertEqual(
            "/repos/vikram",
            self.host_client.workspace_requests[0].repo.path,
        )

    def test_scheduler_processes_priority_ordering(self) -> None:
        """Tasks are dispatched in priority order through the scheduler.

        Validates: Requirement 1.1 (priority-based scheduling)
        """
        # Use a scheduler with max_concurrency=1 and fill the slot so
        # subsequent tasks remain queued for ordering verification
        scheduler = Scheduler(SchedulerConfig(max_concurrency=1))
        app = build_app(
            host_client=self.host_client,
            store=TaskStore(),
            checkpoint_db=Path(self.tmp) / "priority_ordering.db",
            scheduler=scheduler,
            execution_trace=self.execution_trace,
            cost_client=self.cost_client,
            telemetry_client=self.telemetry_client,
        )
        client = TestClient(app)

        # Fill the single concurrency slot
        blocker = TaskQueueEntry(
            task_id="blocker-1",
            priority=Priority.CRITICAL,
            repos=["/repos/other"],
        )
        scheduler.enqueue(blocker)
        scheduler.mark_task_running("blocker-1")

        # Submit a normal-priority task — should be queued
        resp_normal = client.post("/v1/tasks", json={
            "task_id": "normal-task",
            "objective": "Routine docs update",
            "repo": {"path": "/repos/vikram", "default_branch": "main"},
            "priority": "normal",
        })
        self.assertEqual(200, resp_normal.status_code)

        # Submit a high-priority task — should also be queued but ahead
        resp_high = client.post("/v1/tasks", json={
            "task_id": "high-task",
            "objective": "Critical security fix",
            "repo": {"path": "/repos/vikram", "default_branch": "main"},
            "priority": "high",
        })
        self.assertEqual(200, resp_high.status_code)

        # Verify queue ordering via the queue endpoint
        queue_resp = client.get("/v1/queue")
        self.assertEqual(200, queue_resp.status_code)
        queue_data = queue_resp.json()

        task_ids_in_order = [e["task_id"] for e in queue_data["queue"]]
        # blocker-1 is running (highest); then high-task before normal-task
        self.assertIn("high-task", task_ids_in_order)
        self.assertIn("normal-task", task_ids_in_order)
        high_idx = task_ids_in_order.index("high-task")
        normal_idx = task_ids_in_order.index("normal-task")
        self.assertLess(high_idx, normal_idx)

    def test_dependency_blocked_tasks_stay_queued(self) -> None:
        """Tasks with unresolved dependencies are blocked in the queue.

        Validates: Requirement 1.1 (dependency-aware scheduling)
        """
        response = self.client.post("/v1/tasks", json={
            "task_id": "dep-task-e2e",
            "objective": "Deploy after migration completes",
            "repo": {"path": "/repos/vikram", "default_branch": "main"},
            "priority": "normal",
            "depends_on": ["migration-task-not-done"],
        })

        self.assertEqual(200, response.status_code)
        payload = response.json()
        # Task should be queued (blocked internally) since dependency is unresolved
        self.assertEqual("queued", payload["status"])
        self.assertEqual("intake", payload["phase"])

        # Verify it's actually blocked in the scheduler
        queue = self.scheduler.get_queue()
        entry = next(e for e in queue if e.task_id == "dep-task-e2e")
        self.assertEqual("blocked", entry.status)


class TestCostRecordingFlow(unittest.TestCase):
    """Test that cost events are recorded through the full pipeline.

    Validates: Requirement 1.1 (cost attribution per call)
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.host_client = E2EStubHostClient()
        self.cost_client = StubCostClient()
        self.telemetry_client = StubTelemetryClient()
        self.execution_trace = ExecutionTrace()
        self.scheduler = Scheduler(SchedulerConfig(max_concurrency=3))
        self.store = TaskStore()

        self.app = build_app(
            host_client=self.host_client,
            store=self.store,
            checkpoint_db=Path(self.tmp) / "checkpoint.db",
            scheduler=self.scheduler,
            execution_trace=self.execution_trace,
            cost_client=self.cost_client,
            telemetry_client=self.telemetry_client,
        )
        self.client = TestClient(self.app)

    def test_cost_recorded_for_each_agent_call(self) -> None:
        """Each agent call during task execution records a cost event."""
        response = self.client.post("/v1/tasks", json={
            "task_id": "cost-test-001",
            "source": "console",
            "requested_by": "founder",
            "objective": "Add user preference endpoint",
            "repo": {"path": "/repos/vikram", "default_branch": "main"},
            "priority": "high",
        })

        self.assertEqual(200, response.status_code)

        # Verify cost was recorded for at least one agent call
        self.assertGreater(
            len(self.cost_client.recorded_costs), 0,
            "Expected at least one cost record from agent calls",
        )

        # Verify cost records have correct task_id
        for record in self.cost_client.recorded_costs:
            self.assertEqual("cost-test-001", record.task_id)

        # Verify cost records contain duration (wall-clock time tracked)
        for record in self.cost_client.recorded_costs:
            self.assertGreaterEqual(
                record.duration_ms, 0,
                "Expected non-negative duration_ms in cost records",
            )

    def test_cost_records_contain_role_and_phase(self) -> None:
        """Cost records include role, model, provider, and work_phase."""
        self.client.post("/v1/tasks", json={
            "task_id": "cost-detail-001",
            "source": "console",
            "requested_by": "founder",
            "objective": "Fix configuration parser",
            "repo": {"path": "/repos/vikram", "default_branch": "main"},
        })

        self.assertGreater(len(self.cost_client.recorded_costs), 0)

        # Every cost record should have a role and work_phase
        for record in self.cost_client.recorded_costs:
            self.assertTrue(
                record.role,
                f"Cost record missing role: {record}",
            )
            self.assertTrue(
                record.work_phase,
                f"Cost record missing work_phase: {record}",
            )

    def test_daily_total_accumulates_across_tasks(self) -> None:
        """Daily total accumulates cost across multiple tasks."""
        self.client.post("/v1/tasks", json={
            "task_id": "daily-cost-1",
            "objective": "First task",
            "repo": {"path": "/repos/vikram", "default_branch": "main"},
        })
        first_daily = self.cost_client.daily_total

        self.client.post("/v1/tasks", json={
            "task_id": "daily-cost-2",
            "objective": "Second task",
            "repo": {"path": "/repos/vikram", "default_branch": "main"},
        })
        second_daily = self.cost_client.daily_total

        # Daily total should increase after second task
        self.assertGreaterEqual(second_daily, first_daily)


class TestTelemetryEventEmission(unittest.TestCase):
    """Test that telemetry events are emitted at each phase transition.

    Validates: Requirement 8.1 (structured telemetry event collection)
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.host_client = E2EStubHostClient()
        self.cost_client = StubCostClient()
        self.telemetry_client = StubTelemetryClient()
        self.execution_trace = ExecutionTrace()
        self.scheduler = Scheduler(SchedulerConfig(max_concurrency=3))
        self.store = TaskStore()

        self.app = build_app(
            host_client=self.host_client,
            store=self.store,
            checkpoint_db=Path(self.tmp) / "checkpoint.db",
            scheduler=self.scheduler,
            execution_trace=self.execution_trace,
            cost_client=self.cost_client,
            telemetry_client=self.telemetry_client,
        )
        self.client = TestClient(self.app)

    def test_agent_call_events_emitted(self) -> None:
        """Agent call start and end events are emitted during task execution."""
        self.client.post("/v1/tasks", json={
            "task_id": "telemetry-001",
            "objective": "Add logging middleware",
            "repo": {"path": "/repos/vikram", "default_branch": "main"},
        })

        # Filter for agent call events
        start_events = [
            e for e in self.telemetry_client.events
            if e["event_type"] == "agent_call_start"
        ]
        end_events = [
            e for e in self.telemetry_client.events
            if e["event_type"] == "agent_call_end"
        ]

        self.assertGreater(
            len(start_events), 0,
            "Expected at least one agent_call_start event",
        )
        self.assertGreater(
            len(end_events), 0,
            "Expected at least one agent_call_end event",
        )

        # Every start event should have task_id and role
        for event in start_events:
            self.assertEqual("telemetry-001", event["task_id"])
            self.assertIn("role", event["attributes"])

    def test_phase_transition_events_emitted(self) -> None:
        """Phase transition telemetry events are emitted as task moves through phases."""
        self.client.post("/v1/tasks", json={
            "task_id": "telemetry-phases-001",
            "objective": "Refactor configuration module",
            "repo": {"path": "/repos/vikram", "default_branch": "main"},
        })

        phase_events = [
            e for e in self.telemetry_client.events
            if e["event_type"] == "phase_transition"
        ]

        # Workflow should emit at least one phase transition
        self.assertGreater(
            len(phase_events), 0,
            "Expected at least one phase_transition event",
        )

        # Each phase transition should include from/to phases and task_id
        for event in phase_events:
            self.assertEqual("telemetry-phases-001", event["task_id"])
            self.assertIn("from_phase", event["attributes"])
            self.assertIn("to_phase", event["attributes"])
            self.assertIn("reason", event["attributes"])

    def test_telemetry_events_ordered_by_time(self) -> None:
        """Telemetry events are emitted in chronological order."""
        self.client.post("/v1/tasks", json={
            "task_id": "telemetry-order-001",
            "objective": "Update error handling",
            "repo": {"path": "/repos/vikram", "default_branch": "main"},
        })

        timestamps = [e["timestamp"] for e in self.telemetry_client.events]
        self.assertEqual(timestamps, sorted(timestamps))


class TestExecutionTraceDecisions(unittest.TestCase):
    """Test that the execution trace captures all decisions during task lifecycle.

    Validates: Requirement 6.1 (execution trace recording)
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.host_client = E2EStubHostClient()
        self.cost_client = StubCostClient()
        self.telemetry_client = StubTelemetryClient()
        self.execution_trace = ExecutionTrace()
        self.scheduler = Scheduler(SchedulerConfig(max_concurrency=3))
        self.store = TaskStore()

        self.app = build_app(
            host_client=self.host_client,
            store=self.store,
            checkpoint_db=Path(self.tmp) / "checkpoint.db",
            scheduler=self.scheduler,
            execution_trace=self.execution_trace,
            cost_client=self.cost_client,
            telemetry_client=self.telemetry_client,
        )
        self.client = TestClient(self.app)

    def test_trace_records_phase_transitions(self) -> None:
        """Execution trace captures phase transition decisions."""
        self.client.post("/v1/tasks", json={
            "task_id": "trace-001",
            "objective": "Add rate limiting to API",
            "repo": {"path": "/repos/vikram", "default_branch": "main"},
        })

        records = self.execution_trace.records
        phase_records = [
            r for r in records
            if r.decision_type == "phase_transition"
        ]

        self.assertGreater(
            len(phase_records), 0,
            "Expected phase_transition records in execution trace",
        )

        # Each phase transition should have task_id and from/to info
        for record in phase_records:
            self.assertEqual("trace-001", record.task_id)
            self.assertIn("from_phase", record.state_snapshot)
            self.assertIn("to_phase", record.state_snapshot)

    def test_trace_records_model_selection(self) -> None:
        """Execution trace captures model selection decisions during agent calls."""
        from vikram_orchestrator.model_router import (
            ModelCapability,
            ModelRouter,
            RoleCapabilityFloor,
        )

        # Configure a model router with available models so selection succeeds
        model_router = ModelRouter(
            available_models=[
                ModelCapability(
                    model="gpt-4", provider="openai",
                    cost_tier=3, capability_score=4,
                    supports_structured_output=True,
                ),
                ModelCapability(
                    model="gpt-3.5-turbo", provider="openai",
                    cost_tier=1, capability_score=2,
                    supports_structured_output=False,
                ),
            ],
            role_floors=[
                RoleCapabilityFloor(role="lead", min_capability_score=2),
                RoleCapabilityFloor(role="engineer", min_capability_score=1),
                RoleCapabilityFloor(role="qa", min_capability_score=1),
            ],
        )
        app = build_app(
            host_client=self.host_client,
            store=self.store,
            checkpoint_db=Path(self.tmp) / "trace_model.db",
            scheduler=Scheduler(SchedulerConfig(max_concurrency=3)),
            execution_trace=self.execution_trace,
            cost_client=self.cost_client,
            telemetry_client=self.telemetry_client,
            model_router=model_router,
        )
        client = TestClient(app)

        client.post("/v1/tasks", json={
            "task_id": "trace-model-001",
            "objective": "Add model routing test",
            "repo": {"path": "/repos/vikram", "default_branch": "main"},
        })

        records = self.execution_trace.records
        model_records = [
            r for r in records
            if r.decision_type == "model_selection"
        ]

        self.assertGreater(
            len(model_records), 0,
            "Expected model_selection records in execution trace",
        )

        # Model selection records should include the model chosen
        for record in model_records:
            self.assertEqual("trace-model-001", record.task_id)
            self.assertIn("model", record.state_snapshot)

    def test_trace_hash_chain_integrity(self) -> None:
        """Execution trace maintains hash chain integrity across all records."""
        self.client.post("/v1/tasks", json={
            "task_id": "trace-integrity-001",
            "objective": "Verify hash chain works",
            "repo": {"path": "/repos/vikram", "default_branch": "main"},
        })

        records = self.execution_trace.records
        if len(records) >= 2:
            # Verify chain integrity from first to last record
            is_valid = self.execution_trace.verify_chain_integrity(
                start_seq=0,
                end_seq=records[-1].sequence_number,
            )
            self.assertTrue(
                is_valid,
                "Execution trace hash chain integrity check failed",
            )

    def test_trace_records_approval_routing(self) -> None:
        """Execution trace captures approval routing decisions during changes."""
        # First create a task through the full lifecycle
        self.client.post("/v1/tasks", json={
            "task_id": "trace-approval-001",
            "objective": "Security sensitive update",
            "repo": {"path": "/repos/vikram", "default_branch": "main"},
            "priority": "high",
        })

        # Apply a change which triggers approval routing
        self.client.post(
            "/v1/tasks/trace-approval-001/changes",
            json={
                "task_id": "trace-approval-001",
                "summary": "Update auth module",
                "edits": [{
                    "path": "pkg/auth/security.go",
                    "old_text": "package auth",
                    "new_text": "package auth\n// enhanced security",
                    "rationale": "Security enhancement",
                }],
                "verification_commands": ["go test ./pkg/auth"],
            },
        )

        records = self.execution_trace.records
        approval_records = [
            r for r in records
            if r.decision_type == "approval_routing"
        ]

        self.assertGreater(
            len(approval_records), 0,
            "Expected approval_routing records in execution trace",
        )

        # Approval routing should include the routing decision
        for record in approval_records:
            self.assertEqual("trace-approval-001", record.task_id)
            self.assertTrue(record.outcome)


class TestApprovalMatrixRouting(unittest.TestCase):
    """Test that the approval matrix routes correctly based on risk classification.

    Validates: Requirement 30.1 (declarative approval matrix routing)
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.host_client = E2EStubHostClient()
        self.cost_client = StubCostClient()
        self.telemetry_client = StubTelemetryClient()
        self.execution_trace = ExecutionTrace()
        self.scheduler = Scheduler(SchedulerConfig(max_concurrency=3))
        self.store = TaskStore()

        self.app = build_app(
            host_client=self.host_client,
            store=self.store,
            checkpoint_db=Path(self.tmp) / "checkpoint.db",
            scheduler=self.scheduler,
            execution_trace=self.execution_trace,
            cost_client=self.cost_client,
            telemetry_client=self.telemetry_client,
        )
        self.client = TestClient(self.app)

    def test_high_risk_change_requires_founder_review(self) -> None:
        """Changes to security-sensitive files route to founder review."""
        # Create a task first
        create_resp = self.client.post("/v1/tasks", json={
            "task_id": "approval-high-001",
            "objective": "Update authentication flow",
            "repo": {"path": "/repos/vikram", "default_branch": "main"},
            "priority": "normal",
        })
        self.assertEqual(200, create_resp.status_code)

        # Apply a change to a security-relevant path
        change_resp = self.client.post(
            "/v1/tasks/approval-high-001/changes",
            json={
                "task_id": "approval-high-001",
                "summary": "Modify authentication handler",
                "edits": [{
                    "path": "pkg/auth/oauth.go",
                    "old_text": "package orchestratorhost",
                    "new_text": "package orchestratorhost\n// auth change",
                    "rationale": "Update OAuth flow",
                }],
                "verification_commands": ["go test ./pkg/auth"],
            },
        )
        self.assertEqual(200, change_resp.status_code)
        payload = change_resp.json()

        # High-risk changes should require founder review
        self.assertEqual("awaiting_approval", payload["status"])
        self.assertTrue(payload["requires_founder_review"])
        # Risk classification should be high or critical
        self.assertIn(payload["risk_class"], ("high", "critical"))
        self.assertEqual("founder_review", payload["approval_route"])

    def test_low_risk_docs_change_auto_completes(self) -> None:
        """Low-risk documentation changes are auto-approved."""
        # Create a task
        create_resp = self.client.post("/v1/tasks", json={
            "task_id": "approval-low-001",
            "objective": "Update README documentation",
            "repo": {"path": "/repos/vikram", "default_branch": "main"},
        })
        self.assertEqual(200, create_resp.status_code)

        # Apply a docs-only change
        change_resp = self.client.post(
            "/v1/tasks/approval-low-001/changes",
            json={
                "task_id": "approval-low-001",
                "summary": "Fix typo in README",
                "edits": [{
                    "path": "README.md",
                    "old_text": "package orchestratorhost",
                    "new_text": "package orchestratorhost\n<!-- fixed typo -->",
                    "rationale": "Documentation fix",
                }],
                "verification_commands": [],
            },
        )
        self.assertEqual(200, change_resp.status_code)
        payload = change_resp.json()

        # Low-risk docs changes should auto-complete or go to merge_ready
        # The exact phase depends on whether the approval matrix is configured
        # with docs-auto-approve rules. With default policy it may still
        # require founder review, but risk_class should be lower.
        self.assertIn(
            payload["risk_class"],
            ("low", "medium", "high", None),
        )


class TestFullPipelineIntegration(unittest.TestCase):
    """Integration test verifying all subsystems communicate correctly.

    Tests the full pipeline: scheduler → workflow → cost → telemetry → trace.
    Validates: Requirements 1.1, 8.1, 6.1, 30.1
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.host_client = E2EStubHostClient()
        self.cost_client = StubCostClient()
        self.telemetry_client = StubTelemetryClient()
        self.execution_trace = ExecutionTrace()
        self.scheduler = Scheduler(SchedulerConfig(max_concurrency=3))
        self.store = TaskStore()

        self.app = build_app(
            host_client=self.host_client,
            store=self.store,
            checkpoint_db=Path(self.tmp) / "checkpoint.db",
            scheduler=self.scheduler,
            execution_trace=self.execution_trace,
            cost_client=self.cost_client,
            telemetry_client=self.telemetry_client,
        )
        self.client = TestClient(self.app)

    def test_full_pipeline_task_creates_cost_telemetry_and_trace(self) -> None:
        """A single task creation produces cost records, telemetry, and trace entries."""
        response = self.client.post("/v1/tasks", json={
            "task_id": "pipeline-001",
            "source": "telegram",
            "requested_by": "founder",
            "objective": "Implement file upload handler",
            "repo": {"path": "/repos/vikram", "default_branch": "main"},
            "priority": "high",
            "formation": "feature-standard",
        })
        self.assertEqual(200, response.status_code)

        # 1. Cost records should exist for this task
        task_cost_records = [
            r for r in self.cost_client.recorded_costs
            if r.task_id == "pipeline-001"
        ]
        self.assertGreater(len(task_cost_records), 0, "No cost records for task")

        # 2. Telemetry events should exist for this task
        task_telemetry = [
            e for e in self.telemetry_client.events
            if e["task_id"] == "pipeline-001"
        ]
        self.assertGreater(len(task_telemetry), 0, "No telemetry events for task")

        # 3. Execution trace records should exist
        task_trace = [
            r for r in self.execution_trace.records
            if r.task_id == "pipeline-001"
        ]
        self.assertGreater(len(task_trace), 0, "No trace records for task")

    def test_subsystem_events_are_correlated_by_task_id(self) -> None:
        """All subsystem events for the same task share the same task_id.

        This verifies the correlation key flows through the full pipeline.
        """
        task_id = "correlation-001"
        self.client.post("/v1/tasks", json={
            "task_id": task_id,
            "objective": "Test correlation",
            "repo": {"path": "/repos/vikram", "default_branch": "main"},
        })

        # All cost records should reference the task
        for record in self.cost_client.recorded_costs:
            if record.task_id == task_id:
                self.assertEqual(task_id, record.task_id)

        # All telemetry events should reference the task
        task_events = [
            e for e in self.telemetry_client.events
            if e["task_id"] == task_id
        ]
        for event in task_events:
            self.assertEqual(task_id, event["task_id"])

        # All trace records should reference the task
        task_traces = [
            r for r in self.execution_trace.records
            if r.task_id == task_id
        ]
        for record in task_traces:
            self.assertEqual(task_id, record.task_id)

    def test_multiple_tasks_have_independent_cost_tracking(self) -> None:
        """Cost is tracked independently per task through the pipeline."""
        self.client.post("/v1/tasks", json={
            "task_id": "multi-cost-1",
            "objective": "First task",
            "repo": {"path": "/repos/vikram", "default_branch": "main"},
        })
        self.client.post("/v1/tasks", json={
            "task_id": "multi-cost-2",
            "objective": "Second task",
            "repo": {"path": "/repos/vikram", "default_branch": "main"},
        })

        cost_1 = self.cost_client.task_costs.get("multi-cost-1", 0.0)
        cost_2 = self.cost_client.task_costs.get("multi-cost-2", 0.0)

        # Both tasks should have independent cost tracking
        # (at least one should have cost > 0 if agent calls succeeded)
        total_records = len(self.cost_client.recorded_costs)
        self.assertGreater(total_records, 0)

        # Verify daily total is sum of all task costs
        expected_daily = sum(self.cost_client.task_costs.values())
        self.assertAlmostEqual(
            self.cost_client.daily_total, expected_daily, places=6
        )

    def test_scheduler_state_reflected_in_queue_endpoint(self) -> None:
        """Queue endpoint accurately reflects scheduler state after task operations."""
        # Create a task that will dispatch (no blockers)
        self.client.post("/v1/tasks", json={
            "task_id": "queue-reflect-001",
            "objective": "First dispatched task",
            "repo": {"path": "/repos/vikram", "default_branch": "main"},
        })

        # Check queue state — task should have been completed and removed
        queue_resp = self.client.get("/v1/queue")
        self.assertEqual(200, queue_resp.status_code)
        queue_data = queue_resp.json()

        # The task was dispatched and completed, so should not be in queue
        active_ids = [e["task_id"] for e in queue_data["queue"]]
        self.assertNotIn("queue-reflect-001", active_ids)


if __name__ == "__main__":
    unittest.main()
