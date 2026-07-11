"""Tests for scheduler endpoint integration in server.py.

Validates: Requirements 16.4, 18.1, 19.3
- GET /v1/queue returns task queue state sorted by priority
- PUT /v1/tasks/{task_id}/priority updates a task's priority
- POST /v1/tasks enqueues via scheduler with priority/deps/repos
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from vikram_orchestrator.scheduler import Priority, Scheduler, SchedulerConfig, TaskQueueEntry
from vikram_orchestrator.server import build_app
from vikram_orchestrator.store import TaskStore


class StubHostClientForScheduler:
    """Minimal stub that satisfies the host client interface for scheduler tests."""

    def health(self):
        from vikram_orchestrator.models import SystemHealthResponse
        return SystemHealthResponse(
            status="ok",
            workspace_root="/tmp/vikram-workspaces",
            socket_path="/tmp/vikramd.sock",
            restrict_to_workspace=True,
            sandboxed=False,
            telegram_enabled=True,
        )

    def agent_roster(self):
        from vikram_orchestrator.models import AgentRosterResponse, AgentProfile
        return AgentRosterResponse(
            agents=[
                AgentProfile(
                    id="lead-1", role="lead", name="Lead",
                    provider="openai", model="gpt-4", capabilities=["planning"],
                ),
            ]
        )

    def agent_think(self, request):
        from vikram_orchestrator.models import AgentThinkResponse
        return AgentThinkResponse(
            task_id=request.task_id, role=request.role,
            content="stub response", provider="openai", model="gpt-4",
            input_tokens=10, output_tokens=20,
        )

    def provision_workspace(self, request):
        from vikram_orchestrator.models import WorkspaceProvisionResponse
        return WorkspaceProvisionResponse(
            workspace_root="/tmp/ws", task_root="/tmp/ws/t",
            artifacts_dir="/tmp/ws/a", logs_dir="/tmp/ws/l",
            scratch_dir="/tmp/ws/s",
        )

    def create_worktree(self, request):
        from vikram_orchestrator.models import GitWorktreeCreateResponse
        return GitWorktreeCreateResponse(
            worktree_path="/tmp/ws/wt", branch="feature-1",
        )

    def inspect_repo(self, request):
        from vikram_orchestrator.models import RepoInspectResponse
        return RepoInspectResponse(
            path="/tmp/repo", default_branch="main", head_ref="abc123",
            dirty=False, changed_file_count=0, additions=0, deletions=0,
            diff_short_stat="", status_lines=[], changed_files=[],
            top_level_entries=[], key_files=[],
        )

    def discover_targets(self, request):
        from vikram_orchestrator.models import RepoTargetDiscoveryResponse
        return RepoTargetDiscoveryResponse(candidates=[])

    def read_file(self, request):
        from vikram_orchestrator.models import FileReadResponse
        return FileReadResponse(path="x", content="", size=0, truncated=False)

    def write_file(self, request):
        from vikram_orchestrator.models import FileWriteResponse
        return FileWriteResponse(path="x", bytes_written=0)

    def replace_file(self, request):
        from vikram_orchestrator.models import FileReplaceResponse
        return FileReplaceResponse(path="x", replacements_made=0)

    def notify(self, request):
        from vikram_orchestrator.models import ChannelNotificationResponse
        return ChannelNotificationResponse(delivered=True)

    def write_artifact(self, request):
        from vikram_orchestrator.models import ArtifactWriteResponse
        return ArtifactWriteResponse(path="x", bytes_written=0)

    def read_artifact(self, request):
        from vikram_orchestrator.models import ArtifactReadResponse
        return ArtifactReadResponse(path="x", content="", size=0, truncated=False)

    def close(self):
        pass


class TestGetQueueEndpoint(unittest.TestCase):
    """Tests for GET /v1/queue endpoint."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.scheduler = Scheduler(SchedulerConfig(max_concurrency=3))
        self.app = build_app(
            host_client=StubHostClientForScheduler(),
            store=TaskStore(Path(self.tmp) / "tasks.json"),
            checkpoint_db=Path(self.tmp) / "checkpoint.db",
            scheduler=self.scheduler,
        )
        self.client = TestClient(self.app)

    def test_empty_queue(self):
        """GET /v1/queue returns empty queue with all counts at zero."""
        resp = self.client.get("/v1/queue")
        assert resp.status_code == 200
        data = resp.json()
        assert data["queue"] == []
        assert data["running"] == 0
        assert data["queued"] == 0
        assert data["blocked"] == 0

    def test_queue_with_entries(self):
        """GET /v1/queue returns enqueued tasks sorted by priority."""
        self.scheduler.enqueue(TaskQueueEntry(
            task_id="low-task", priority=Priority.LOW, repos=["/repo/a"],
        ))
        self.scheduler.enqueue(TaskQueueEntry(
            task_id="high-task", priority=Priority.HIGH, repos=["/repo/b"],
        ))
        self.scheduler.enqueue(TaskQueueEntry(
            task_id="normal-task", priority=Priority.NORMAL, repos=["/repo/c"],
        ))

        resp = self.client.get("/v1/queue")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["queue"]) == 3
        # Should be sorted: high, normal, low
        assert data["queue"][0]["task_id"] == "high-task"
        assert data["queue"][0]["priority"] == "high"
        assert data["queue"][1]["task_id"] == "normal-task"
        assert data["queue"][2]["task_id"] == "low-task"
        assert data["queued"] == 3
        assert data["running"] == 0

    def test_queue_shows_blocked_tasks(self):
        """GET /v1/queue shows blocked tasks with correct status count."""
        self.scheduler.enqueue(TaskQueueEntry(
            task_id="blocked-task", priority=Priority.NORMAL,
            depends_on=["other-task"],
        ))

        resp = self.client.get("/v1/queue")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["queue"]) == 1
        assert data["queue"][0]["status"] == "blocked"
        assert data["blocked"] == 1


class TestUpdatePriorityEndpoint(unittest.TestCase):
    """Tests for PUT /v1/tasks/{task_id}/priority endpoint."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.scheduler = Scheduler(SchedulerConfig(max_concurrency=3))
        self.app = build_app(
            host_client=StubHostClientForScheduler(),
            store=TaskStore(Path(self.tmp) / "tasks.json"),
            checkpoint_db=Path(self.tmp) / "checkpoint.db",
            scheduler=self.scheduler,
        )
        self.client = TestClient(self.app)

    def test_update_priority_success(self):
        """PUT /v1/tasks/{task_id}/priority updates task priority."""
        self.scheduler.enqueue(TaskQueueEntry(
            task_id="task-1", priority=Priority.NORMAL,
        ))

        resp = self.client.put(
            "/v1/tasks/task-1/priority",
            json={"priority": "critical"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_id"] == "task-1"
        assert data["new_priority"] == "critical"

        # Verify the priority was actually updated in the scheduler
        queue = self.scheduler.get_queue()
        assert queue[0].priority == Priority.CRITICAL

    def test_update_priority_invalid(self):
        """PUT /v1/tasks/{task_id}/priority rejects invalid priority."""
        self.scheduler.enqueue(TaskQueueEntry(
            task_id="task-1", priority=Priority.NORMAL,
        ))

        resp = self.client.put(
            "/v1/tasks/task-1/priority",
            json={"priority": "mega"},
        )
        assert resp.status_code == 400

    def test_update_priority_task_not_found(self):
        """PUT /v1/tasks/{task_id}/priority returns 404 for unknown task."""
        resp = self.client.put(
            "/v1/tasks/nonexistent/priority",
            json={"priority": "high"},
        )
        assert resp.status_code == 404


class TestCreateTaskWithScheduler(unittest.TestCase):
    """Tests for POST /v1/tasks with scheduler integration."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Use max_concurrency=0 effectively by filling the running slots
        # so tasks stay queued rather than being dispatched through the graph
        self.scheduler = Scheduler(SchedulerConfig(max_concurrency=1))
        self.app = build_app(
            host_client=StubHostClientForScheduler(),
            store=TaskStore(Path(self.tmp) / "tasks.json"),
            checkpoint_db=Path(self.tmp) / "checkpoint.db",
            scheduler=self.scheduler,
        )
        self.client = TestClient(self.app)

    def test_create_task_with_priority_enqueues(self):
        """POST /v1/tasks with priority enqueues in scheduler and queue shows it."""
        # First, fill the concurrency slot so new tasks stay queued
        blocker = TaskQueueEntry(
            task_id="blocker", priority=Priority.CRITICAL, repos=["/repos/other"],
        )
        self.scheduler.enqueue(blocker)
        self.scheduler.mark_task_running("blocker")

        resp = self.client.post("/v1/tasks", json={
            "task_id": "sched-task-1",
            "objective": "fix bug in auth",
            "repo": {"path": "/repos/main", "default_branch": "main"},
            "priority": "high",
        })
        # Task should be accepted as queued (concurrency limit reached)
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_id"] == "sched-task-1"
        assert data["status"] == "queued"

        # Verify it's in the scheduler queue
        queue = self.scheduler.get_queue()
        task_ids = [e.task_id for e in queue]
        assert "sched-task-1" in task_ids

    def test_create_task_with_dependencies_blocked(self):
        """POST /v1/tasks with unresolved depends_on enqueues as queued (blocked internally)."""
        resp = self.client.post("/v1/tasks", json={
            "task_id": "dep-task",
            "objective": "deploy after migration",
            "repo": {"path": "/repos/main", "default_branch": "main"},
            "priority": "normal",
            "depends_on": ["migration-task"],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_id"] == "dep-task"
        # Blocked tasks are reported as "queued" in the API
        assert data["status"] == "queued"

        # Internally the scheduler knows it's blocked
        queue = self.scheduler.get_queue()
        dep_entry = next(e for e in queue if e.task_id == "dep-task")
        assert dep_entry.status == "blocked"

    def test_create_task_default_priority(self):
        """POST /v1/tasks defaults to normal priority in scheduler."""
        # Fill concurrency so it doesn't dispatch
        blocker = TaskQueueEntry(
            task_id="blocker", priority=Priority.CRITICAL, repos=["/repos/other"],
        )
        self.scheduler.enqueue(blocker)
        self.scheduler.mark_task_running("blocker")

        resp = self.client.post("/v1/tasks", json={
            "task_id": "default-task",
            "objective": "routine maintenance",
            "repo": {"path": "/repos/main", "default_branch": "main"},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_id"] == "default-task"

        # Verify priority is NORMAL in the scheduler
        queue = self.scheduler.get_queue()
        entry = next(e for e in queue if e.task_id == "default-task")
        assert entry.priority == Priority.NORMAL


if __name__ == "__main__":
    unittest.main()
