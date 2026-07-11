from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from vikram_orchestrator.models import (
    AgentProfile,
    AgentRosterResponse,
    AgentThinkRequest,
    AgentThinkResponse,
    ApprovalDecision,
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
    RepoTargetDiscoveryRequest,
    RepoTargetDiscoveryResponse,
    RepoRef,
    SystemHealthResponse,
    TaskChangeRequest,
    TaskCreateRequest,
    VerificationDiscoveryRequest,
    VerificationDiscoveryResponse,
    WorkspaceProvisionRequest,
    WorkspaceProvisionResponse,
)
from vikram_orchestrator.server import build_app
from vikram_orchestrator.store import TaskStore
from vikram_orchestrator.workflow import (
    build_graph,
    close_graph,
    initial_state_from_request,
    state_to_task_session,
)


class StubHostClient:
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
                    provider="openai",
                    model="gpt-4",
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
        elif role == "engineer":
            content = (
                '[{"path": "README.md", "old_text": "# Vikram", '
                '"new_text": "# Vikram\\nTest", "rationale": "test change"}]'
            )
        elif role == "reviewer":
            content = "CONCEDE - the plan is solid and covers all necessary steps."
        elif role == "qa":
            content = '{"verdict": "PASSED", "summary": "All checks pass", "issues": []}'
        else:
            content = f'{{"verdict": "PASSED", "summary": "Agent {role} completed", "issues": []}}'
        return AgentThinkResponse(
            task_id=request.task_id,
            role=role,
            content=content,
        )

    def agent_think(self, request: AgentThinkRequest) -> AgentThinkResponse:
        role = request.role
        if role == "lead":
            content = (
                "## Implementation Plan\n\n"
                "1. Modify workflow.py to add retry logic\n"
                "2. Update settings.py with configuration\n"
                "3. Add error types to team.py\n"
                "4. Verify changes with tests\n"
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
                "path": f"/tmp/vikram-workspaces/tasks/{request.artifact.task_id}/artifacts/{request.artifact.artifact_id}.md"
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
                f"/tmp/vikram-workspaces/tasks/{artifact_request.artifact.task_id}/artifacts/"
                f"{artifact_request.artifact.artifact_id}.md"
            )
            if (
                artifact_request.artifact.task_id == request.task_id
                and path == request.path
            ):
                max_bytes = request.max_bytes or 32000
                content = artifact_request.content
                truncated = len(content) > max_bytes
                return ArtifactReadResponse(
                    task_id=request.task_id,
                    path=path,
                    content=content[:max_bytes],
                    bytes_read=min(len(content), max_bytes),
                    truncated=truncated,
                )
        raise AssertionError(f"artifact not found for test stub: {request.path}")

    def inspect_repo(self, request: RepoInspectRequest) -> RepoInspectResponse:
        self.inspect_requests.append(request)
        changed_paths = [
            replace_request.path
            for replace_request in self.file_replace_requests
            if replace_request.task_id == request.task_id
            and replace_request.old_text != replace_request.new_text
        ]
        changed_files = [
            {
                "path": path,
                "status": "M",
                "additions": 1,
                "deletions": 0,
                "binary": False,
            }
            for path in changed_paths
        ]
        return RepoInspectResponse(
            task_id=request.task_id,
            repo_path=request.repo_path,
            worktree_path=request.worktree_path,
            branch=f"vikram/{request.task_id}",
            head_ref="0123456789abcdef0123456789abcdef01234567",
            dirty=bool(changed_paths),
            changed_file_count=len(changed_files),
            additions=sum(item["additions"] for item in changed_files),
            deletions=sum(item["deletions"] for item in changed_files),
            diff_short_stat=(
                f"{len(changed_files)} file changed, "
                f"{sum(item['additions'] for item in changed_files)} insertion(+)"
                if changed_files
                else ""
            ),
            top_level_entries=["README.md", "go.mod", "pkg/"],
            status_lines=[f"M {path}" for path in changed_paths],
            changed_files=changed_files,
            key_files=[
                {
                    "path": "README.md",
                    "preview": "# Vikram\n",
                    "bytes": 8,
                },
                {
                    "path": "go.mod",
                    "preview": "module github.com/vatthu/vikram\n",
                    "bytes": 31,
                },
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
                    "reason": "path matched `workflow`; content matched `plan`",
                },
                {
                    "path": "services/orchestrator/src/vikram_orchestrator/workflow.py",
                    "score": 7,
                    "reason": "fallback to key repository file",
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
                {
                    "command": "go test ./...",
                    "working_dir": request.worktree_path,
                    "runtime": "go",
                    "reason": "full Go repository verification",
                },
            ],
        )

    def exec(self, request: HostActionRequest) -> HostObservation:
        self.exec_requests.append(request)
        command = str(request.arguments.get("command", ""))
        success = "FAIL" not in command
        return HostObservation(
            task_id=request.task_id,
            action_name=request.action_name,
            success=success,
            summary=f"command {'completed' if success else 'failed'}: {command}",
            output=f"{'ok' if success else 'error'}: {command}",
            state={"working_dir": request.working_dir or ""},
        )

    def notify_telegram(
        self, request: ChannelNotificationRequest
    ) -> ChannelNotificationResponse:
        self.notification_requests.append(request)
        return ChannelNotificationResponse(
            delivered=True, summary="telegram notification delivered"
        )

    def close(self) -> None:
        return None


class WorkflowTests(unittest.TestCase):
    def test_task_intake_verifies_host_and_provisions_workspace(self) -> None:
        request = TaskCreateRequest(
            task_id="task-001",
            source="telegram",
            requested_by="founder",
            objective="Create the first Vikram workflow",
            repo=RepoRef(path="/repos/vikram", default_branch="main"),
        )
        host_client = StubHostClient()

        with tempfile.TemporaryDirectory() as tmpdir:
            graph = build_graph(
                host_client, checkpoint_db=Path(tmpdir) / "orchestrator.sqlite"
            )
            try:
                result = graph.invoke(
                    initial_state_from_request(request),
                    config={"configurable": {"thread_id": request.task_id}},
                )
            finally:
                close_graph(graph)

        self.assertEqual(1, host_client.health_calls)
        self.assertEqual(1, len(host_client.workspace_requests))
        self.assertEqual(1, len(host_client.worktree_requests))
        self.assertEqual(1, len(host_client.inspect_requests))
        self.assertEqual(1, len(host_client.discovery_requests))
        self.assertEqual(1, len(host_client.verification_requests))
        self.assertEqual(2, len(host_client.file_read_requests))
        self.assertEqual(3, len(host_client.artifact_requests))
        self.assertEqual("/repos/vikram", host_client.workspace_requests[0].repo.path)
        self.assertEqual("change_ready", result["phase"])
        self.assertEqual(
            "/tmp/vikram-workspaces/worktrees/task-001", result["worktree_path"]
        )
        self.assertEqual("vikram/task-001", result["worktree_branch"])
        self.assertEqual("vikram/task-001", result["repo_branch"])
        self.assertTrue(result["plan_artifact_path"].endswith("plan-initial.md"))
        self.assertEqual(["README.md", "go.mod", "pkg/"], result["repo_top_level_entries"])
        self.assertTrue(
            result["implementation_artifact_path"].endswith("implementation-brief.md")
        )
        self.assertTrue(
            result["verification_artifact_path"].endswith("verification-initial.md")
        )
        self.assertIn(
            "pkg/orchestratorhost/server.go", host_client.artifact_requests[0].content
        )
        self.assertIn("Implementation Brief", host_client.artifact_requests[1].content)
        self.assertIn("go test ./pkg/orchestratorhost", host_client.artifact_requests[2].content)

        session = state_to_task_session(request, result)
        self.assertEqual("running", session.status)
        self.assertEqual("change_ready", session.phase)
        self.assertIn("verification-initial.md", session.summary)

    def test_create_task_endpoint_returns_host_backed_session(self) -> None:
        host_client = StubHostClient()
        store = TaskStore()

        with tempfile.TemporaryDirectory() as tmpdir:
            app = build_app(
                host_client=host_client,
                store=store,
                checkpoint_db=Path(tmpdir) / "orchestrator.sqlite",
            )
            with TestClient(app) as client:
                response = client.post(
                    "/v1/tasks",
                    json={
                        "task_id": "task-002",
                        "source": "telegram",
                        "requested_by": "founder",
                        "objective": "Verify the API path",
                        "repo": {
                            "path": "/repos/vikram",
                            "default_branch": "main",
                        },
                    },
                )

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("change_ready", payload["phase"])
        self.assertEqual("running", payload["status"])
        self.assertIn("verification-initial.md", payload["summary"])
        self.assertEqual(1, len(host_client.inspect_requests))
        self.assertEqual(1, len(host_client.discovery_requests))
        self.assertEqual(1, len(host_client.verification_requests))

        stored = store.get("task-002")
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual("change_ready", stored.phase)

    def test_founder_console_shell_is_served(self) -> None:
        host_client = StubHostClient()
        store = TaskStore()

        with tempfile.TemporaryDirectory() as tmpdir:
            app = build_app(
                host_client=host_client,
                store=store,
                checkpoint_db=Path(tmpdir) / "orchestrator.sqlite",
            )
            with TestClient(app) as client:
                response = client.get("/console")

        self.assertEqual(200, response.status_code)
        self.assertIn("text/html", response.headers.get("content-type", ""))
        self.assertIn("Vikram Founder Console", response.text)

    def test_apply_change_endpoint_requests_founder_review_for_risky_change(self) -> None:
        host_client = StubHostClient()
        store = TaskStore()

        with tempfile.TemporaryDirectory() as tmpdir:
            app = build_app(
                host_client=host_client,
                store=store,
                checkpoint_db=Path(tmpdir) / "orchestrator.sqlite",
            )
            with TestClient(app) as client:
                create_response = client.post(
                    "/v1/tasks",
                    json={
                        "task_id": "task-003",
                        "source": "telegram",
                        "requested_by": "founder",
                        "objective": "Tighten workflow verification",
                        "repo": {
                            "path": "/repos/vikram",
                            "default_branch": "main",
                        },
                        "operator_channel": "telegram",
                        "operator_chat_id": "123456",
                    },
                )
                self.assertEqual(200, create_response.status_code)

                change_response = client.post(
                    "/v1/tasks/task-003/changes",
                    json=TaskChangeRequest(
                        task_id="task-003",
                        summary="Rename a bounded anchor and run focused verification",
                        edits=[
                            {
                                "path": "pkg/orchestratorhost/server.go",
                                "old_text": "package orchestratorhost",
                                "new_text": "package orchestratorhost\n// review evidence fixture",
                                "rationale": "Bounded replacement for review-evidence test",
                            }
                        ],
                        verification_commands=["go test ./pkg/orchestratorhost"],
                    ).model_dump(),
                )

        self.assertEqual(200, change_response.status_code)
        payload = change_response.json()
        self.assertEqual("founder_review_requested", payload["phase"])
        self.assertEqual("awaiting_approval", payload["status"])
        self.assertEqual("high", payload["risk_class"])
        self.assertEqual("founder_review", payload["approval_route"])
        self.assertTrue(payload["requires_founder_review"])
        self.assertIn("approval-request-1.md", payload["summary"])
        self.assertEqual(1, len(host_client.file_replace_requests))
        self.assertEqual(1, len(host_client.exec_requests))
        self.assertEqual(1, len(host_client.notification_requests))
        self.assertIn(
            "Vikram operator state: `awaiting_approval`",
            host_client.notification_requests[0].content,
        )
        self.assertEqual(
            "go test ./pkg/orchestratorhost",
            host_client.exec_requests[0].arguments["command"],
        )
        self.assertEqual(6, len(host_client.artifact_requests))
        self.assertIn("Applied Change", host_client.artifact_requests[3].content)
        self.assertIn(
            "go test ./pkg/orchestratorhost", host_client.artifact_requests[4].content
        )
        self.assertIn(
            "Founder Approval Request", host_client.artifact_requests[5].content
        )
        self.assertIn(
            "Founder review required",
            host_client.notification_requests[0].content,
        )

        stored = store.get("task-003")
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual("founder_review_requested", stored.phase)

    def test_founder_console_endpoints_expose_review_state(self) -> None:
        host_client = StubHostClient()
        store = TaskStore()

        with tempfile.TemporaryDirectory() as tmpdir:
            app = build_app(
                host_client=host_client,
                store=store,
                checkpoint_db=Path(tmpdir) / "orchestrator.sqlite",
            )
            with TestClient(app) as client:
                client.post(
                    "/v1/tasks",
                    json={
                        "task_id": "task-003a",
                        "source": "telegram",
                        "requested_by": "founder",
                        "objective": "Tighten workflow verification",
                        "repo": {
                            "path": "/repos/vikram",
                            "default_branch": "main",
                        },
                        "operator_channel": "telegram",
                        "operator_chat_id": "123456",
                    },
                )
                client.post(
                    "/v1/tasks/task-003a/changes",
                    json=TaskChangeRequest(
                        task_id="task-003a",
                        summary="Rename a bounded anchor and run focused verification",
                        edits=[
                            {
                                "path": "pkg/orchestratorhost/server.go",
                                "old_text": "package orchestratorhost",
                                "new_text": "package orchestratorhost\n// review evidence fixture",
                                "rationale": "Bounded replacement for review-evidence test",
                            }
                        ],
                        verification_commands=["go test ./pkg/orchestratorhost"],
                    ).model_dump(),
                )

                list_response = client.get("/v1/tasks", params={"needs_review": "true"})
                review_response = client.get("/v1/tasks/task-003a/review")

        self.assertEqual(200, list_response.status_code)
        tasks = list_response.json()
        self.assertEqual(1, len(tasks))
        self.assertEqual("task-003a", tasks[0]["task_id"])
        self.assertTrue(tasks[0]["requires_founder_review"])

        self.assertEqual(200, review_response.status_code)
        review = review_response.json()
        self.assertEqual("task-003a", review["task"]["task_id"])
        self.assertEqual("high", review["task"]["risk_class"])
        self.assertTrue(review["can_resume"])
        self.assertFalse(review["follow_up"]["required"])
        self.assertEqual("high", review["approval_request"]["risk_class"])
        self.assertTrue(review["approval_artifact_path"].endswith("approval-request-1.md"))
        self.assertEqual(1, len(review["applied_edits"]))
        self.assertIn("--- before", review["applied_edits"][0]["diff_preview"])
        self.assertEqual(1, len(review["verification_runs"]))
        self.assertEqual(
            "go test ./pkg/orchestratorhost", review["verification_runs"][0]["command"]
        )
        self.assertGreaterEqual(len(review["artifact_previews"]), 3)
        self.assertTrue(review["can_resume"])
        self.assertFalse(review["can_apply_follow_up"])
        self.assertTrue(
            any(
                transition["state"] == "awaiting_approval"
                and transition["action"] == "approve"
                and transition["target_phase"] == "merge_ready"
                for transition in review["action_transitions"]
            )
        )
        self.assertTrue(
            any(
                transition["state"] == "awaiting_approval"
                and transition["action"] == "edit_and_approve"
                for transition in review["action_transitions"]
            )
        )

    def test_review_artifact_content_endpoint_reads_allowed_artifact(self) -> None:
        host_client = StubHostClient()
        store = TaskStore()

        with tempfile.TemporaryDirectory() as tmpdir:
            app = build_app(
                host_client=host_client,
                store=store,
                checkpoint_db=Path(tmpdir) / "orchestrator.sqlite",
            )
            with TestClient(app) as client:
                client.post(
                    "/v1/tasks",
                    json={
                        "task_id": "task-003b",
                        "source": "telegram",
                        "requested_by": "founder",
                        "objective": "Tighten workflow verification",
                        "repo": {
                            "path": "/repos/vikram",
                            "default_branch": "main",
                        },
                        "operator_channel": "telegram",
                        "operator_chat_id": "123456",
                    },
                )
                client.post(
                    "/v1/tasks/task-003b/changes",
                    json=TaskChangeRequest(
                        task_id="task-003b",
                        summary="Rename a bounded anchor and run focused verification",
                        edits=[
                            {
                                "path": "pkg/orchestratorhost/server.go",
                                "old_text": "package orchestratorhost",
                                "new_text": "package orchestratorhost\n// review evidence fixture",
                                "rationale": "Bounded replacement for review-evidence test",
                            }
                        ],
                        verification_commands=["go test ./pkg/orchestratorhost"],
                    ).model_dump(),
                )
                review_response = client.get("/v1/tasks/task-003b/review")
                review = review_response.json()
                artifact_response = client.get(
                    "/v1/tasks/task-003b/artifacts/content",
                    params={"path": review["approval_artifact_path"]},
                )

        self.assertEqual(200, artifact_response.status_code)
        artifact = artifact_response.json()
        self.assertTrue(artifact["path"].endswith("approval-request-1.md"))
        self.assertIn("Founder Approval Request", artifact["content"])
        self.assertFalse(artifact["truncated"])

    def test_apply_change_endpoint_auto_completes_low_risk_docs_change(self) -> None:
        host_client = StubHostClient()
        store = TaskStore()

        with tempfile.TemporaryDirectory() as tmpdir:
            app = build_app(
                host_client=host_client,
                store=store,
                checkpoint_db=Path(tmpdir) / "orchestrator.sqlite",
            )
            with TestClient(app) as client:
                create_response = client.post(
                    "/v1/tasks",
                    json={
                        "task_id": "task-004",
                        "source": "telegram",
                        "requested_by": "founder",
                        "objective": "Refresh the founder guide",
                        "repo": {
                            "path": "/repos/vikram",
                            "default_branch": "main",
                        },
                    },
                )
                self.assertEqual(200, create_response.status_code)

                change_response = client.post(
                    "/v1/tasks/task-004/changes",
                    json=TaskChangeRequest(
                        task_id="task-004",
                        summary="Tighten README wording",
                        edits=[
                            {
                                "path": "README.md",
                                "old_text": "# Vikram",
                                "new_text": "# Vikram\nFounder guide refresh",
                                "rationale": "Bounded documentation replacement for merge-readiness test",
                            }
                        ],
                    ).model_dump(),
                )

        self.assertEqual(200, change_response.status_code)
        payload = change_response.json()
        self.assertEqual("merge_ready", payload["phase"])
        self.assertEqual("completed", payload["status"])
        self.assertEqual("low", payload["risk_class"])
        self.assertEqual("auto_complete", payload["approval_route"])
        self.assertFalse(payload["requires_founder_review"])
        self.assertEqual("ready", payload["merge_readiness"])
        self.assertEqual(0, len(host_client.notification_requests))

        stored = store.get("task-004")
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual("merge_ready", stored.phase)

    def test_apply_change_endpoint_critical_on_failed_verification(self) -> None:
        host_client = StubHostClient()
        store = TaskStore()

        with tempfile.TemporaryDirectory() as tmpdir:
            app = build_app(
                host_client=host_client,
                store=store,
                checkpoint_db=Path(tmpdir) / "orchestrator.sqlite",
            )
            with TestClient(app) as client:
                create_response = client.post(
                    "/v1/tasks",
                    json={
                        "task_id": "task-005",
                        "source": "telegram",
                        "requested_by": "founder",
                        "objective": "Force a failing verification path",
                        "repo": {
                            "path": "/repos/vikram",
                            "default_branch": "main",
                        },
                        "operator_channel": "telegram",
                        "operator_chat_id": "123456",
                    },
                )
                self.assertEqual(200, create_response.status_code)

                change_response = client.post(
                    "/v1/tasks/task-005/changes",
                    json=TaskChangeRequest(
                        task_id="task-005",
                        summary="Trigger a failing verification run",
                        edits=[
                            {
                                "path": "pkg/orchestratorhost/server.go",
                                "old_text": "package orchestratorhost",
                                "new_text": "package orchestratorhost",
                                "rationale": "No-op replacement for failure-path test",
                            }
                        ],
                        verification_commands=["FAIL go test ./pkg/orchestratorhost"],
                    ).model_dump(),
                )
                review_response = client.get("/v1/tasks/task-005/review")

        self.assertEqual(200, change_response.status_code)
        payload = change_response.json()
        self.assertEqual("founder_review_requested", payload["phase"])
        self.assertEqual("awaiting_approval", payload["status"])
        self.assertEqual("critical", payload["risk_class"])
        self.assertEqual("founder_review", payload["approval_route"])
        self.assertTrue(payload["requires_founder_review"])

        self.assertEqual(200, review_response.status_code)
        review_payload = review_response.json()
        self.assertTrue(
            any(
                transition["state"] == "awaiting_approval"
                and transition["action"] == "approve"
                and transition["target_phase"] == "merge_blocked"
                and transition["target_status"] == "paused"
                for transition in review_payload["action_transitions"]
            )
        )

    def test_follow_up_edit_request_carries_into_next_attempt(self) -> None:
        host_client = StubHostClient()
        store = TaskStore()

        with tempfile.TemporaryDirectory() as tmpdir:
            app = build_app(
                host_client=host_client,
                store=store,
                checkpoint_db=Path(tmpdir) / "orchestrator.sqlite",
            )
            with TestClient(app) as client:
                create_response = client.post(
                    "/v1/tasks",
                    json={
                        "task_id": "task-005a",
                        "source": "telegram",
                        "requested_by": "founder",
                        "objective": "Refine a bounded workflow change",
                        "repo": {
                            "path": "/repos/vikram",
                            "default_branch": "main",
                        },
                        "operator_channel": "telegram",
                        "operator_chat_id": "123456",
                    },
                )
                self.assertEqual(200, create_response.status_code)

                first_change = client.post(
                    "/v1/tasks/task-005a/changes",
                    json=TaskChangeRequest(
                        task_id="task-005a",
                        summary="First bounded attempt",
                        edits=[
                            {
                                "path": "pkg/orchestratorhost/server.go",
                                "old_text": "package orchestratorhost",
                                "new_text": "package orchestratorhost",
                                "rationale": "No-op replacement for follow-up test",
                            }
                        ],
                        verification_commands=["go test ./pkg/orchestratorhost"],
                    ).model_dump(),
                )
                self.assertEqual(200, first_change.status_code)
                self.assertEqual("founder_review_requested", first_change.json()["phase"])

                edit_response = client.post(
                    "/v1/tasks/task-005a/resume",
                    json=ApprovalDecision(
                        task_id="task-005a",
                        decision="edit_and_approve",
                        comment="Keep the change but tighten the rationale wording",
                        proposed_edits={"note": "mention why the bounded replacement is safe"},
                    ).model_dump(),
                )
                self.assertEqual(200, edit_response.status_code)
                edit_payload = edit_response.json()
                self.assertEqual("founder_edit_requested", edit_payload["phase"])
                self.assertTrue(edit_payload["follow_up_required"])
                self.assertEqual(
                    "Keep the change but tighten the rationale wording",
                    edit_payload["follow_up_summary"],
                )

                review_response = client.get("/v1/tasks/task-005a/review")
                self.assertEqual(200, review_response.status_code)
                review_payload = review_response.json()
                self.assertTrue(review_payload["follow_up"]["required"])
                self.assertEqual(
                    "Keep the change but tighten the rationale wording",
                    review_payload["follow_up"]["comment"],
                )
                self.assertEqual(
                    "mention why the bounded replacement is safe",
                    review_payload["follow_up"]["proposed_edits"]["note"],
                )

                second_change = client.post(
                    "/v1/tasks/task-005a/changes",
                    json=TaskChangeRequest(
                        task_id="task-005a",
                        summary="Second bounded attempt",
                        edits=[
                            {
                                "path": "pkg/orchestratorhost/server.go",
                                "old_text": "package orchestratorhost",
                                "new_text": "package orchestratorhost",
                                "rationale": "Updated bounded replacement for follow-up test",
                            }
                        ],
                        verification_commands=["go test ./pkg/orchestratorhost"],
                    ).model_dump(),
                )

        self.assertEqual(200, second_change.status_code)
        second_payload = second_change.json()
        self.assertFalse(second_payload["follow_up_required"])
        self.assertEqual("founder_review_requested", second_payload["phase"])
        self.assertTrue(
            any(
                "Founder Follow-Up Context" in request.content
                and "tighten the rationale wording" in request.content
                for request in host_client.artifact_requests
            )
        )

    def test_merge_blocked_retry_carries_blockers_into_next_attempt(self) -> None:
        host_client = StubHostClient()
        store = TaskStore()

        with tempfile.TemporaryDirectory() as tmpdir:
            app = build_app(
                host_client=host_client,
                store=store,
                checkpoint_db=Path(tmpdir) / "orchestrator.sqlite",
            )
            with TestClient(app) as client:
                create_response = client.post(
                    "/v1/tasks",
                    json={
                        "task_id": "task-005b",
                        "source": "telegram",
                        "requested_by": "founder",
                        "objective": "Retry after a blocked merge handoff",
                        "repo": {
                            "path": "/repos/vikram",
                            "default_branch": "main",
                        },
                        "operator_channel": "telegram",
                        "operator_chat_id": "123456",
                    },
                )
                self.assertEqual(200, create_response.status_code)

                failed_change = client.post(
                    "/v1/tasks/task-005b/changes",
                    json=TaskChangeRequest(
                        task_id="task-005b",
                        summary="First attempt with failing verification",
                        edits=[
                            {
                                "path": "pkg/orchestratorhost/server.go",
                                "old_text": "package orchestratorhost",
                                "new_text": "package orchestratorhost\n// failed handoff fixture",
                                "rationale": "Create a failed merge handoff for retry testing",
                            }
                        ],
                        verification_commands=["FAIL go test ./pkg/orchestratorhost"],
                    ).model_dump(),
                )
                self.assertEqual(200, failed_change.status_code)
                self.assertEqual("founder_review_requested", failed_change.json()["phase"])

                override_response = client.post(
                    "/v1/tasks/task-005b/resume",
                    json=ApprovalDecision(
                        task_id="task-005b",
                        decision="approve",
                        comment="Approve only to test blocked merge retry",
                    ).model_dump(),
                )
                self.assertEqual(200, override_response.status_code)
                override_payload = override_response.json()
                self.assertEqual("merge_blocked", override_payload["phase"])
                self.assertEqual("paused", override_payload["status"])
                self.assertEqual("blocked", override_payload["merge_readiness"])
                self.assertTrue(override_payload["follow_up_required"])

                review_response = client.get("/v1/tasks/task-005b/review")
                self.assertEqual(200, review_response.status_code)
                review_payload = review_response.json()
                self.assertTrue(review_payload["can_apply_follow_up"])
                self.assertEqual("merge_blocked", review_payload["follow_up"]["phase"])
                self.assertIn(
                    "Focused verification did not pass",
                    review_payload["follow_up"]["proposed_edits"]["merge_blockers"][0],
                )
                self.assertTrue(
                    any(
                        transition["state"] == "blocked"
                        and transition["action"] == "retry_change"
                        for transition in review_payload["action_transitions"]
                    )
                )
                self.assertTrue(
                    any(
                        "Vikram operator state: `blocked`" in request.content
                        for request in host_client.notification_requests
                    )
                )

                retry_response = client.post(
                    "/v1/tasks/task-005b/changes",
                    json=TaskChangeRequest(
                        task_id="task-005b",
                        summary="Retry after blocked merge handoff",
                        edits=[
                            {
                                "path": "pkg/orchestratorhost/server.go",
                                "old_text": "package orchestratorhost",
                                "new_text": "package orchestratorhost\n// retry handoff fixture",
                                "rationale": "Retry with passing verification after merge blockers",
                            }
                        ],
                        verification_commands=["go test ./pkg/orchestratorhost"],
                    ).model_dump(),
                )

        self.assertEqual(200, retry_response.status_code)
        retry_payload = retry_response.json()
        self.assertEqual("founder_review_requested", retry_payload["phase"])
        self.assertEqual("unknown", retry_payload["merge_readiness"])
        self.assertTrue(
            any(
                "Founder Follow-Up Context" in request.content
                and "merge_blocked" in request.content
                and "Focused verification did not pass" in request.content
                for request in host_client.artifact_requests
            )
        )

    def test_resume_endpoint_completes_founder_approval_flow(self) -> None:
        host_client = StubHostClient()
        store = TaskStore()

        with tempfile.TemporaryDirectory() as tmpdir:
            app = build_app(
                host_client=host_client,
                store=store,
                checkpoint_db=Path(tmpdir) / "orchestrator.sqlite",
            )
            with TestClient(app) as client:
                create_response = client.post(
                    "/v1/tasks",
                    json={
                        "task_id": "task-006",
                        "source": "telegram",
                        "requested_by": "founder",
                        "objective": "Tighten workflow verification",
                        "repo": {
                            "path": "/repos/vikram",
                            "default_branch": "main",
                        },
                        "operator_channel": "telegram",
                        "operator_chat_id": "123456",
                    },
                )
                self.assertEqual(200, create_response.status_code)

                change_response = client.post(
                    "/v1/tasks/task-006/changes",
                    json=TaskChangeRequest(
                        task_id="task-006",
                        summary="Rename a bounded anchor and run focused verification",
                        edits=[
                            {
                                "path": "pkg/orchestratorhost/server.go",
                                "old_text": "package orchestratorhost",
                                "new_text": "package orchestratorhost\n// founder approval fixture",
                                "rationale": "Bounded replacement for founder-approval merge-ready test",
                            }
                        ],
                        verification_commands=["go test ./pkg/orchestratorhost"],
                    ).model_dump(),
                )
                self.assertEqual(200, change_response.status_code)
                self.assertEqual(
                    "founder_review_requested", change_response.json()["phase"]
                )

                resume_response = client.post(
                    "/v1/tasks/task-006/resume",
                    json=ApprovalDecision(
                        task_id="task-006",
                        decision="approve",
                        comment="Proceed with this bounded change",
                    ).model_dump(),
                )

                review_response = client.get("/v1/tasks/task-006/review")

        self.assertEqual(200, resume_response.status_code)
        payload = resume_response.json()
        self.assertEqual("merge_ready", payload["phase"])
        self.assertEqual("completed", payload["status"])
        self.assertEqual("ready", payload["merge_readiness"])
        self.assertIn("merge-readiness-1.md", payload["summary"])
        self.assertEqual(9, len(host_client.artifact_requests))
        self.assertEqual("archive", host_client.artifact_requests[8].artifact.kind)
        self.assertIn("Subject: [Vikram] task-006 merge-ready handoff", host_client.artifact_requests[8].content)
        self.assertTrue(
            any(
                "Vikram operator state: `merge_ready`" in request.content
                for request in host_client.notification_requests
            )
        )

        self.assertEqual(200, review_response.status_code)
        review_payload = review_response.json()
        self.assertTrue(
            any(
                transition["state"] == "merge_ready"
                and transition["action"] == "complete"
                for transition in review_payload["action_transitions"]
            )
        )

        stored = store.get("task-006")
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual("merge_ready", stored.phase)

    def test_retry_from_follow_up_keeps_founder_review_gate(self) -> None:
        host_client = StubHostClient()
        store = TaskStore()

        with tempfile.TemporaryDirectory() as tmpdir:
            app = build_app(
                host_client=host_client,
                store=store,
                checkpoint_db=Path(tmpdir) / "orchestrator.sqlite",
            )
            with TestClient(app) as client:
                create_response = client.post(
                    "/v1/tasks",
                    json={
                        "task_id": "task-007",
                        "source": "telegram",
                        "requested_by": "founder",
                        "objective": "Keep retry flows on the founder-review path",
                        "repo": {
                            "path": "/repos/vikram",
                            "default_branch": "main",
                        },
                        "operator_channel": "telegram",
                        "operator_chat_id": "123456",
                    },
                )
                self.assertEqual(200, create_response.status_code)

                first_change = client.post(
                    "/v1/tasks/task-007/changes",
                    json=TaskChangeRequest(
                        task_id="task-007",
                        summary="Start with a bounded code change",
                        edits=[
                            {
                                "path": "pkg/orchestratorhost/server.go",
                                "old_text": "package orchestratorhost",
                                "new_text": "package orchestratorhost\n// follow-up gating fixture",
                                "rationale": "Create a review-gated task before retry hardening",
                            }
                        ],
                        verification_commands=["go test ./pkg/orchestratorhost"],
                    ).model_dump(),
                )
                self.assertEqual(200, first_change.status_code)

                edit_response = client.post(
                    "/v1/tasks/task-007/resume",
                    json=ApprovalDecision(
                        task_id="task-007",
                        decision="edit_and_approve",
                        comment="Keep the task on a founder-reviewed retry path",
                    ).model_dump(),
                )
                self.assertEqual(200, edit_response.status_code)

                second_change = client.post(
                    "/v1/tasks/task-007/changes",
                    json=TaskChangeRequest(
                        task_id="task-007",
                        summary="Retry with a lower-risk documentation follow-up",
                        edits=[
                            {
                                "path": "README.md",
                                "old_text": "# Vikram",
                                "new_text": "# Vikram\nRetry follow-up still requires founder review",
                                "rationale": "Exercise the hardened retry policy on a low-risk edit",
                            }
                        ],
                        verification_commands=["go test ./pkg/orchestratorhost"],
                    ).model_dump(),
                )

        self.assertEqual(200, second_change.status_code)
        second_payload = second_change.json()
        self.assertEqual("founder_review_requested", second_payload["phase"])
        self.assertEqual("awaiting_approval", second_payload["status"])
        self.assertEqual("founder_review", second_payload["approval_route"])
        self.assertTrue(second_payload["requires_founder_review"])
        self.assertTrue(
            any(
                "Vikram operator state: `retryable`" in request.content
                for request in host_client.notification_requests
            )
        )


if __name__ == "__main__":
    unittest.main()
