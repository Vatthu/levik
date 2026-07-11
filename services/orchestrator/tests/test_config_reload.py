"""Tests for configuration hot-reload.

Validates: Requirements 54.1, 54.2, 54.3, 54.4
- Config changes are applied within 5 seconds (validated via timing)
- Invalid configurations are rejected with descriptive errors
- Changes are applied at next decision point without interruption
- Config changes are recorded in Execution_Trace with previous/new values and source
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from vikram_orchestrator.config_reload import (
    AlertThresholds,
    AutonomyMode,
    BudgetConfig,
    ConfigReloader,
    ConfigValidationError,
    EscalationPolicy,
    FormationConfig,
    PlatformConfig,
)
from vikram_orchestrator.execution_trace import ExecutionTrace


# ---------------------------------------------------------------------------
# Unit tests for ConfigReloader
# ---------------------------------------------------------------------------


class TestConfigReloaderValidation:
    """Test config validation logic (Requirement 54.2)."""

    def setup_method(self):
        self.trace = ExecutionTrace()
        self.reloader = ConfigReloader(execution_trace=self.trace)

    def test_valid_budget_config(self):
        config = PlatformConfig(
            budgets=BudgetConfig(
                daily_ceiling_usd=100.0,
                default_task_max_cost_usd=10.0,
                warning_threshold_pct=80.0,
            )
        )
        errors = self.reloader.validate_config(config)
        assert errors == []

    def test_invalid_budget_negative_ceiling(self):
        config = PlatformConfig(
            budgets=BudgetConfig(daily_ceiling_usd=-5.0)
        )
        errors = self.reloader.validate_config(config)
        assert len(errors) == 1
        assert "daily_ceiling_usd must be positive" in errors[0].message

    def test_invalid_budget_zero_task_cost(self):
        config = PlatformConfig(
            budgets=BudgetConfig(default_task_max_cost_usd=0.0)
        )
        errors = self.reloader.validate_config(config)
        assert len(errors) == 1
        assert "default_task_max_cost_usd must be positive" in errors[0].message

    def test_invalid_warning_threshold(self):
        config = PlatformConfig(
            budgets=BudgetConfig(warning_threshold_pct=0.0)
        )
        errors = self.reloader.validate_config(config)
        assert any("warning_threshold_pct" in e.message for e in errors)

    def test_valid_alert_thresholds(self):
        config = PlatformConfig(
            alert_thresholds=AlertThresholds(
                error_rate_pct=50.0,
                latency_seconds=30.0,
                provider_failure_count=5,
                rolling_window_minutes=15.0,
            )
        )
        errors = self.reloader.validate_config(config)
        assert errors == []

    def test_invalid_alert_thresholds(self):
        config = PlatformConfig(
            alert_thresholds=AlertThresholds(
                error_rate_pct=0.0,
                latency_seconds=-1.0,
                provider_failure_count=0,
                rolling_window_minutes=0.0,
            )
        )
        errors = self.reloader.validate_config(config)
        assert len(errors) == 4

    def test_valid_formation(self):
        config = PlatformConfig(
            formations=[
                FormationConfig(
                    name="fast-bugfix",
                    task_type="bugfix",
                    budget_strategy={"planning": 0.1, "implementation": 0.6, "verification": 0.2, "review": 0.1},
                    verification_protocol="standard",
                )
            ]
        )
        errors = self.reloader.validate_config(config)
        assert errors == []

    def test_invalid_formation_task_type(self):
        config = PlatformConfig(
            formations=[
                FormationConfig(name="invalid", task_type="unknown")
            ]
        )
        errors = self.reloader.validate_config(config)
        assert len(errors) == 1
        assert "task_type" in errors[0].message

    def test_invalid_formation_budget_sum(self):
        config = PlatformConfig(
            formations=[
                FormationConfig(
                    name="bad-budget",
                    task_type="bugfix",
                    budget_strategy={"planning": 0.5, "implementation": 0.6},
                )
            ]
        )
        errors = self.reloader.validate_config(config)
        assert any("sum to 1.0" in e.message for e in errors)

    def test_duplicate_formation_names(self):
        config = PlatformConfig(
            formations=[
                FormationConfig(name="dup", task_type="bugfix"),
                FormationConfig(name="dup", task_type="feature"),
            ]
        )
        errors = self.reloader.validate_config(config)
        assert any("duplicate formation name" in e.message for e in errors)

    def test_valid_escalation_policy(self):
        config = PlatformConfig(
            escalation_policies=[
                EscalationPolicy(
                    name="critical-halt",
                    priority=1,
                    risk_levels=["high", "critical"],
                    routing="escalate_and_halt",
                )
            ]
        )
        errors = self.reloader.validate_config(config)
        assert errors == []

    def test_invalid_escalation_routing(self):
        config = PlatformConfig(
            escalation_policies=[
                EscalationPolicy(
                    name="bad-route",
                    priority=1,
                    routing="invalid_routing",
                )
            ]
        )
        errors = self.reloader.validate_config(config)
        assert any("routing" in e.message for e in errors)

    def test_invalid_escalation_risk_level(self):
        config = PlatformConfig(
            escalation_policies=[
                EscalationPolicy(
                    name="bad-risk",
                    priority=1,
                    risk_levels=["extreme"],
                    routing="founder_review",
                )
            ]
        )
        errors = self.reloader.validate_config(config)
        assert any("invalid risk level" in e.message for e in errors)

    def test_duplicate_policy_names(self):
        config = PlatformConfig(
            escalation_policies=[
                EscalationPolicy(name="dup", priority=1, routing="founder_review"),
                EscalationPolicy(name="dup", priority=2, routing="auto_approve"),
            ]
        )
        errors = self.reloader.validate_config(config)
        assert any("duplicate policy name" in e.message for e in errors)

    def test_empty_config_is_valid(self):
        config = PlatformConfig()
        errors = self.reloader.validate_config(config)
        assert errors == []


class TestConfigReloaderApply:
    """Test config apply logic (Requirements 54.1, 54.3)."""

    def setup_method(self):
        self.trace = ExecutionTrace()
        self.reloader = ConfigReloader(execution_trace=self.trace)

    def test_successful_reload_updates_config(self):
        config = PlatformConfig(
            budgets=BudgetConfig(daily_ceiling_usd=200.0),
            autonomy_mode=AutonomyMode.supervised,
        )
        result = self.reloader.reload(config, source="api")
        assert result.success is True
        assert result.applied_at is not None
        assert self.reloader.current_config.budgets is not None
        assert self.reloader.current_config.budgets.daily_ceiling_usd == 200.0
        assert self.reloader.current_config.autonomy_mode == AutonomyMode.supervised

    def test_failed_reload_retains_previous_config(self):
        # Apply a valid config first
        valid = PlatformConfig(budgets=BudgetConfig(daily_ceiling_usd=100.0))
        self.reloader.reload(valid, source="api")

        # Try to apply invalid config
        invalid = PlatformConfig(budgets=BudgetConfig(daily_ceiling_usd=-50.0))
        result = self.reloader.reload(invalid, source="api")

        assert result.success is False
        assert len(result.errors) > 0
        # Previous config is retained
        assert self.reloader.current_config.budgets is not None
        assert self.reloader.current_config.budgets.daily_ceiling_usd == 100.0

    def test_partial_update_only_changes_specified_fields(self):
        # Apply budgets first
        self.reloader.reload(
            PlatformConfig(budgets=BudgetConfig(daily_ceiling_usd=100.0)),
            source="api",
        )
        # Apply alert thresholds — budgets should remain
        self.reloader.reload(
            PlatformConfig(alert_thresholds=AlertThresholds(error_rate_pct=50.0)),
            source="api",
        )
        assert self.reloader.current_config.budgets is not None
        assert self.reloader.current_config.budgets.daily_ceiling_usd == 100.0
        assert self.reloader.current_config.alert_thresholds is not None
        assert self.reloader.current_config.alert_thresholds.error_rate_pct == 50.0

    def test_reload_within_5_seconds(self):
        """Config changes should be applied within 5 seconds (Requirement 54.1)."""
        config = PlatformConfig(
            budgets=BudgetConfig(daily_ceiling_usd=500.0),
            autonomy_mode=AutonomyMode.full_autonomy,
            alert_thresholds=AlertThresholds(error_rate_pct=20.0),
        )
        start = time.time()
        result = self.reloader.reload(config, source="console")
        elapsed = time.time() - start
        assert result.success is True
        assert elapsed < 5.0  # Must complete within 5 seconds

    def test_reload_records_last_reload_timestamp(self):
        before = time.time()
        self.reloader.reload(
            PlatformConfig(autonomy_mode=AutonomyMode.approval_required),
            source="api",
        )
        assert self.reloader.last_reload_at >= before


class TestConfigReloaderTrace:
    """Test config change recording in Execution_Trace (Requirement 54.4)."""

    def setup_method(self):
        self.trace = ExecutionTrace()
        self.reloader = ConfigReloader(execution_trace=self.trace)

    def test_config_change_recorded_in_trace(self):
        config = PlatformConfig(
            budgets=BudgetConfig(daily_ceiling_usd=150.0)
        )
        self.reloader.reload(config, source="api")

        records = self.trace.query(
            task_id="__platform__", decision_type="config_change"
        )
        assert len(records) == 1
        record = records[0]
        assert record.state_snapshot["field"] == "budgets"
        assert record.state_snapshot["source"] == "api"
        assert record.state_snapshot["previous_value"] is None
        assert record.state_snapshot["new_value"]["daily_ceiling_usd"] == 150.0

    def test_config_change_records_previous_and_new(self):
        # Apply initial config
        self.reloader.reload(
            PlatformConfig(budgets=BudgetConfig(daily_ceiling_usd=100.0)),
            source="api",
        )
        # Update it
        self.reloader.reload(
            PlatformConfig(budgets=BudgetConfig(daily_ceiling_usd=200.0)),
            source="console",
        )

        records = self.trace.query(
            task_id="__platform__", decision_type="config_change"
        )
        assert len(records) == 2
        second = records[1]
        assert second.state_snapshot["previous_value"]["daily_ceiling_usd"] == 100.0
        assert second.state_snapshot["new_value"]["daily_ceiling_usd"] == 200.0
        assert second.state_snapshot["source"] == "console"

    def test_multiple_fields_produce_multiple_trace_records(self):
        config = PlatformConfig(
            budgets=BudgetConfig(daily_ceiling_usd=100.0),
            autonomy_mode=AutonomyMode.supervised,
        )
        self.reloader.reload(config, source="file_reload")

        records = self.trace.query(
            task_id="__platform__", decision_type="config_change"
        )
        assert len(records) == 2
        fields = {r.state_snapshot["field"] for r in records}
        assert "budgets" in fields
        assert "autonomy_mode" in fields

    def test_no_change_produces_no_trace_record(self):
        config = PlatformConfig(budgets=BudgetConfig(daily_ceiling_usd=100.0))
        self.reloader.reload(config, source="api")
        # Reload with the same config
        self.reloader.reload(config, source="api")

        records = self.trace.query(
            task_id="__platform__", decision_type="config_change"
        )
        # Only 1 record from the first change
        assert len(records) == 1

    def test_no_trace_recording_when_trace_is_none(self):
        reloader = ConfigReloader(execution_trace=None)
        config = PlatformConfig(budgets=BudgetConfig(daily_ceiling_usd=100.0))
        result = reloader.reload(config, source="api")
        assert result.success is True


# ---------------------------------------------------------------------------
# Integration tests for POST /v1/config/reload endpoint
# ---------------------------------------------------------------------------


class StubHostClientForConfigReload:
    """Minimal stub for host client in config reload tests."""

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


class TestConfigReloadEndpoint:
    """Test POST /v1/config/reload API endpoint."""

    def setup_method(self):
        from vikram_orchestrator.server import build_app
        from vikram_orchestrator.store import TaskStore
        import tempfile
        from pathlib import Path

        self.tmp_dir = tempfile.mkdtemp()
        store = TaskStore(Path(self.tmp_dir) / "tasks.json")
        self.trace = ExecutionTrace()
        self.reloader = ConfigReloader(execution_trace=self.trace)
        app = build_app(
            host_client=StubHostClientForConfigReload(),
            store=store,
            checkpoint_db=Path(self.tmp_dir) / "checkpoint.db",
            execution_trace=self.trace,
            config_reloader=self.reloader,
        )
        self.client = TestClient(app)

    def test_reload_valid_config(self):
        response = self.client.post("/v1/config/reload", json={
            "config": {
                "budgets": {
                    "daily_ceiling_usd": 250.0,
                    "default_task_max_cost_usd": 25.0,
                }
            },
            "source": "api",
        })
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert len(data["changes"]) == 1
        assert data["changes"][0]["field"] == "budgets"

    def test_reload_invalid_config_returns_400(self):
        response = self.client.post("/v1/config/reload", json={
            "config": {
                "budgets": {
                    "daily_ceiling_usd": -10.0,
                }
            },
            "source": "api",
        })
        assert response.status_code == 400
        data = response.json()
        assert "errors" in data["detail"]

    def test_reload_invalid_source_returns_400(self):
        response = self.client.post("/v1/config/reload", json={
            "config": {"autonomy_mode": "supervised"},
            "source": "unknown_source",
        })
        assert response.status_code == 400

    def test_reload_invalid_structure_returns_400(self):
        response = self.client.post("/v1/config/reload", json={
            "config": {
                "budgets": "not_a_dict",
            },
            "source": "api",
        })
        assert response.status_code == 400

    def test_get_config_endpoint(self):
        # Apply some config
        self.client.post("/v1/config/reload", json={
            "config": {"autonomy_mode": "full_autonomy"},
            "source": "console",
        })
        response = self.client.get("/v1/config")
        assert response.status_code == 200
        data = response.json()
        assert data["config"]["autonomy_mode"] == "full_autonomy"
        assert data["last_reload_at"] > 0

    def test_reload_formations(self):
        response = self.client.post("/v1/config/reload", json={
            "config": {
                "formations": [
                    {
                        "name": "quick-fix",
                        "task_type": "bugfix",
                        "role_model_mappings": {"lead": "gpt-4"},
                        "budget_strategy": {
                            "planning": 0.1,
                            "implementation": 0.6,
                            "verification": 0.2,
                            "review": 0.1,
                        },
                        "verification_protocol": "standard",
                    }
                ]
            },
            "source": "api",
        })
        assert response.status_code == 200
        assert response.json()["success"] is True

    def test_reload_escalation_policies(self):
        response = self.client.post("/v1/config/reload", json={
            "config": {
                "escalation_policies": [
                    {
                        "name": "halt-on-critical",
                        "priority": 1,
                        "risk_levels": ["critical"],
                        "routing": "escalate_and_halt",
                    }
                ]
            },
            "source": "api",
        })
        assert response.status_code == 200
        assert response.json()["success"] is True

    def test_reload_records_in_execution_trace(self):
        self.client.post("/v1/config/reload", json={
            "config": {"autonomy_mode": "approval_required"},
            "source": "console",
        })
        records = self.trace.query(
            task_id="__platform__", decision_type="config_change"
        )
        assert len(records) == 1
        assert records[0].state_snapshot["source"] == "console"
        assert records[0].state_snapshot["new_value"] == "approval_required"
