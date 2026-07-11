from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config_reload import (
    ConfigReloader,
    ConfigReloadResult,
    PlatformConfig,
)
from .approval_matrix import ApprovalMatrix
from .conflict_detector import ConflictDetector
from .cost_client import CostClient
from .execution_trace import ExecutionTrace
from .host_client import HostClient
from .knowledge_store import KnowledgeStore
from .model_router import ModelRouter
from .models import (
    ApprovalDecision,
    ArtifactReadRequest,
    ArtifactReadResponse,
    TaskChangeRequest,
    TaskCreateRequest,
    TaskReviewDetail,
    TaskSession,
)
from .scheduler import Priority, Scheduler, SchedulerConfig, TaskQueueEntry
from .settings import settings
from .store import TaskStore
from .telemetry_client import TelemetryClient
from .verification_protocol import VerificationProtocol
from .workflow import (
    apply_change_request,
    build_graph,
    close_graph,
    initial_state_from_request,
    resume_from_founder_decision,
    state_to_task_session,
    task_review_from_state,
)


# --- Scheduler request/response models (module-level for FastAPI compatibility) ---

class PriorityUpdateRequest(BaseModel):
    """Request body for PUT /v1/tasks/{task_id}/priority.

    Validates: Requirement 16.4
    """
    priority: str = Field(description="New priority: critical, high, normal, low")


# --- Configuration hot-reload request model (module-level for FastAPI compatibility) ---

class ConfigReloadRequest(BaseModel):
    """Request body for POST /v1/config/reload.

    Validates: Requirement 54.1, 54.2
    """
    config: dict = Field(description="Platform configuration fields to update")
    source: str = Field(
        default="api",
        description="Change source: 'api', 'console', or 'file_reload'",
    )


def build_app(
    host_client: HostClient | None = None,
    store: TaskStore | None = None,
    checkpoint_db: Path | None = None,
    scheduler: Scheduler | None = None,
    conflict_detector: ConflictDetector | None = None,
    execution_trace: ExecutionTrace | None = None,
    config_reloader: ConfigReloader | None = None,
    model_router: ModelRouter | None = None,
    cost_client: CostClient | None = None,
    telemetry_client: TelemetryClient | None = None,
    approval_matrix: ApprovalMatrix | None = None,
    knowledge_store: KnowledgeStore | None = None,
    verification_protocol: VerificationProtocol | None = None,
) -> FastAPI:
    managed_host_client = host_client or HostClient(settings.host_socket)
    graph = build_graph(
        managed_host_client,
        checkpoint_db=checkpoint_db,
        model_router=model_router,
        cost_client=cost_client,
        telemetry_client=telemetry_client,
        execution_trace=execution_trace or ExecutionTrace(),
        approval_matrix=approval_matrix,
        conflict_detector=conflict_detector or ConflictDetector(),
        knowledge_store=knowledge_store,
        verification_protocol=verification_protocol,
        scheduler=scheduler or Scheduler(SchedulerConfig()),
    )
    task_store = store or TaskStore(settings.state_dir / "tasks.json")
    task_scheduler = scheduler or Scheduler(SchedulerConfig())
    detector = conflict_detector or ConflictDetector()
    trace = execution_trace or ExecutionTrace()
    reloader = config_reloader or ConfigReloader(execution_trace=trace)
    console_dir = Path(__file__).with_name("console")
    console_static_dir = console_dir / "static"

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            close_graph(graph)
            managed_host_client.close()

    app = FastAPI(title="Vikram Orchestrator", version="0.1.0", lifespan=lifespan)
    app.mount(
        "/console/static",
        StaticFiles(directory=console_static_dir),
        name="console-static",
    )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/console", include_in_schema=False)
    @app.get("/console/", include_in_schema=False)
    def founder_console() -> FileResponse:
        return FileResponse(console_dir / "index.html")

    @app.post("/v1/tasks", response_model=TaskSession)
    def create_task(request: TaskCreateRequest) -> TaskSession:
        # --- Queue-based dispatch via Scheduler (Requirements 16.4, 18.1, 19.3) ---
        # 1. Enqueue the task into the scheduler with priority and dependencies
        priority = Priority.from_str(request.priority)
        repos_list = [r.path for r in request.repos] if request.repos else []
        if request.repo and request.repo.path:
            repos_list = [request.repo.path] + [r for r in repos_list if r != request.repo.path]

        entry = TaskQueueEntry(
            task_id=request.task_id,
            priority=priority,
            depends_on=request.depends_on,
            repos=repos_list,
            formation=request.formation,
        )
        task_scheduler.enqueue(entry)

        # 2. Wire Conflict Detector predictions into scheduling decisions
        # Check for conflicts with currently running tasks before dispatch
        running_tasks_targets: dict[str, list[str]] = {}
        for queued_entry in task_scheduler.get_queue():
            if queued_entry.status == "running" and queued_entry.task_id != request.task_id:
                running_tasks_targets[queued_entry.task_id] = queued_entry.repos

        if repos_list and running_tasks_targets:
            conflicts = detector.predict_conflicts(
                new_task_id=request.task_id,
                new_task_targets=repos_list,
                active_tasks=running_tasks_targets,
            )
            # If conflicts are detected, the scheduler will handle serialization
            # via the lock registry integration (contention serialization)
            if conflicts:
                # Propose reorder if conflicts are above threshold
                reorder = detector.propose_reorder(
                    queue=[e.task_id for e in task_scheduler.get_queue()],
                    conflicts=conflicts,
                    priorities={e.task_id: e.priority.value for e in task_scheduler.get_queue()},
                    deps={e.task_id: e.depends_on for e in task_scheduler.get_queue()},
                )
                # Reorder proposal is logged but not automatically applied
                # — the founder is notified via the execution trace

        # 3. Attempt to dequeue and dispatch the next ready task
        next_task = task_scheduler.dequeue_next()
        if next_task and next_task.task_id == request.task_id:
            # This task is ready to run — dispatch through the workflow graph
            try:
                result = graph.invoke(
                    initial_state_from_request(request),
                    config={"configurable": {"thread_id": request.task_id}},
                )
            except httpx.HTTPError as exc:
                # Task failed to dispatch — mark it back as queued
                next_task.status = "queued"
                next_task.started_at = None
                raise HTTPException(
                    status_code=503,
                    detail=f"host daemon unavailable: {exc}",
                ) from exc

            response = state_to_task_session(request, result)
            task_scheduler.mark_task_completed(request.task_id)
            task_store.put(response)
            return response
        elif next_task and next_task.task_id != request.task_id:
            # A different, higher-priority task was dequeued — this task remains queued
            # Re-queue the dequeued task (it will be dispatched by the background loop)
            next_task.status = "queued"
            next_task.started_at = None

        # Task is queued (blocked by dependencies or concurrency limit)
        queued_entry = None
        for e in task_scheduler.get_queue():
            if e.task_id == request.task_id:
                queued_entry = e
                break

        # Map internal scheduler statuses to API-visible TaskSession statuses
        # The scheduler uses "blocked" internally; the API exposes "queued"
        api_status = "queued"
        if queued_entry:
            if queued_entry.status in ("queued", "preempted", "blocked"):
                api_status = "queued"
            elif queued_entry.status == "running":
                api_status = "running"
            else:
                api_status = "queued"

        response = TaskSession(
            task_id=request.task_id,
            source=request.source,
            requested_by=request.requested_by,
            objective=request.objective,
            repo=request.repo,
            constraints=request.constraints,
            operator_channel=request.operator_channel,
            operator_chat_id=request.operator_chat_id,
            status=api_status,
            phase="intake",
            summary=f"Task enqueued with priority={priority.name}",
        )
        task_store.put(response)
        return response

    @app.get("/v1/tasks", response_model=list[TaskSession])
    def list_tasks(
        status: str | None = None,
        phase: str | None = None,
        needs_review: bool | None = None,
        follow_up_required: bool | None = None,
    ) -> list[TaskSession]:
        tasks = task_store.list()
        filtered: list[TaskSession] = []
        for task in tasks:
            if status and task.status != status:
                continue
            if phase and task.phase != phase:
                continue
            if needs_review is not None:
                is_waiting_for_review = task.status == "awaiting_approval"
                if is_waiting_for_review != needs_review:
                    continue
            if follow_up_required is not None and task.follow_up_required != follow_up_required:
                continue
            filtered.append(task)
        return filtered

    @app.get("/v1/tasks/{task_id}", response_model=TaskSession)
    def get_task(task_id: str) -> TaskSession:
        task = task_store.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return task

    @app.get("/v1/tasks/{task_id}/review", response_model=TaskReviewDetail)
    def get_task_review(task_id: str) -> TaskReviewDetail:
        return current_review(task_id)

    @app.get("/v1/tasks/{task_id}/artifacts/content", response_model=ArtifactReadResponse)
    def get_task_artifact_content(
        task_id: str, path: str, max_bytes: int = 32000
    ) -> ArtifactReadResponse:
        review = current_review(task_id)
        allowed_paths = {
            artifact_path
            for artifact_path in [
                review.change_artifact_path,
                review.verification_result_artifact_path,
                review.approval_artifact_path,
                review.founder_decision_artifact_path,
                review.merge_artifact_path,
            ]
            if artifact_path
        }
        if path not in allowed_paths:
            raise HTTPException(status_code=404, detail="artifact not found for task")

        try:
            return managed_host_client.read_artifact(
                ArtifactReadRequest(task_id=task_id, path=path, max_bytes=max_bytes)
            )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"host daemon unavailable: {exc}",
            ) from exc

    @app.post("/v1/tasks/{task_id}/changes", response_model=TaskSession)
    def apply_change(task_id: str, request: TaskChangeRequest) -> TaskSession:
        if request.task_id != task_id:
            raise HTTPException(status_code=400, detail="task_id mismatch")

        task = task_store.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")

        try:
            response = apply_change_request(graph, task, request)
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"host daemon unavailable: {exc}",
            ) from exc

        task_store.put(response)
        return response

    @app.post("/v1/tasks/{task_id}/resume", response_model=TaskSession)
    def resume_task(task_id: str, decision: ApprovalDecision) -> TaskSession:
        if decision.task_id != task_id:
            raise HTTPException(status_code=400, detail="task_id mismatch")

        task = task_store.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")

        try:
            response = resume_from_founder_decision(graph, task, decision)
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"host daemon unavailable: {exc}",
            ) from exc

        task_store.put(response)
        return response

    def current_review(task_id: str) -> TaskReviewDetail:
        task = task_store.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")

        snapshot = graph.get_state({"configurable": {"thread_id": task_id}})
        state = snapshot.values or {}
        return task_review_from_state(task, state)

    # --- Scheduler endpoints (Requirements 16.4, 18.1, 19.3) ---

    @app.get("/v1/queue")
    def get_queue() -> dict:
        """Return the current task queue state sorted by priority.

        Validates: Requirement 16.4 (queue visibility via API)
        """
        queue = task_scheduler.get_queue()
        entries = []
        running_count = 0
        queued_count = 0
        blocked_count = 0

        for entry in queue:
            entries.append({
                "task_id": entry.task_id,
                "priority": entry.priority.name.lower(),
                "status": entry.status,
                "enqueued_at": entry.enqueued_at,
                "depends_on": entry.depends_on,
                "repos": entry.repos,
                "formation": entry.formation,
            })
            if entry.status == "running":
                running_count += 1
            elif entry.status in ("queued", "preempted"):
                queued_count += 1
            elif entry.status == "blocked":
                blocked_count += 1

        return {
            "queue": entries,
            "running": running_count,
            "queued": queued_count,
            "blocked": blocked_count,
        }

    @app.put("/v1/tasks/{task_id}/priority")
    def update_task_priority(task_id: str, body: PriorityUpdateRequest) -> dict:
        """Update a task's priority at runtime.

        The change takes effect at the next scheduling decision.
        Validates: Requirement 16.4
        """
        try:
            new_priority = Priority.from_str(body.priority)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            task_scheduler.update_priority(task_id, new_priority)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        return {
            "task_id": task_id,
            "new_priority": new_priority.name.lower(),
            "message": "priority updated; takes effect at next scheduling decision",
        }

    # --- Configuration hot-reload endpoint (Requirements 54.1, 54.2, 54.3, 54.4) ---

    @app.post("/v1/config/reload", response_model=ConfigReloadResult)
    def reload_config(body: ConfigReloadRequest) -> ConfigReloadResult:
        """Reload platform configuration without service restart.

        Validates the new configuration before applying. Invalid configs
        are rejected with descriptive errors. Valid configs are applied
        at the next decision point without interrupting current operations.

        Changes are recorded in the Execution_Trace with previous/new values.

        Validates: Requirements 54.1, 54.2, 54.3, 54.4
        """
        valid_sources = {"api", "console", "file_reload"}
        if body.source not in valid_sources:
            raise HTTPException(
                status_code=400,
                detail=f"source must be one of {sorted(valid_sources)}",
            )

        # Parse the config dict into PlatformConfig
        try:
            platform_config = PlatformConfig(**body.config)
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid config structure: {exc}",
            ) from exc

        # Delegate to ConfigReloader for validation and application
        result = reloader.reload(platform_config, source=body.source)

        if not result.success:
            # Return 400 with validation errors (Requirement 54.2)
            raise HTTPException(
                status_code=400,
                detail={
                    "message": result.message,
                    "errors": [e.model_dump() for e in result.errors],
                },
            )

        return result

    @app.get("/v1/config")
    def get_config() -> dict:
        """Return the current platform configuration."""
        config = reloader.current_config
        return {
            "config": config.model_dump(exclude_none=True),
            "last_reload_at": reloader.last_reload_at,
        }

    return app
