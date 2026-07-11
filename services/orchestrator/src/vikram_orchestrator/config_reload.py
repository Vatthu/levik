"""Configuration Hot-Reload: apply config changes without service restart.

Supports hot-reload of platform configuration including:
- Budgets (per-task max_cost_usd, daily ceiling)
- Autonomy mode (autonomy level settings)
- Formations (team topology configurations)
- Escalation policies (approval matrix rules)
- Alert thresholds (health alerting parameters)

Changes are validated before applying, rejected with descriptive errors if
invalid, and applied at the next decision point without interrupting current
operations. Every config change is recorded in the Execution_Trace.

Requirements: 54.1, 54.2, 54.3, 54.4
"""

from __future__ import annotations

import time
from enum import Enum
from threading import Lock
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Configuration domain models
# ---------------------------------------------------------------------------


class AutonomyMode(str, Enum):
    """Platform autonomy modes controlling founder involvement."""

    full_autonomy = "full_autonomy"
    supervised = "supervised"
    approval_required = "approval_required"


class BudgetConfig(BaseModel):
    """Budget configuration for the platform."""

    daily_ceiling_usd: float | None = None  # Global daily cost ceiling
    default_task_max_cost_usd: float | None = None  # Default per-task budget
    warning_threshold_pct: float = 80.0  # Percentage at which to warn

    def validate_values(self) -> list[str]:
        """Return list of validation errors, empty if valid."""
        errors: list[str] = []
        if self.daily_ceiling_usd is not None and self.daily_ceiling_usd <= 0:
            errors.append("daily_ceiling_usd must be positive")
        if self.default_task_max_cost_usd is not None and self.default_task_max_cost_usd <= 0:
            errors.append("default_task_max_cost_usd must be positive")
        if not (0 < self.warning_threshold_pct <= 100):
            errors.append("warning_threshold_pct must be between 0 (exclusive) and 100 (inclusive)")
        return errors


class AlertThresholds(BaseModel):
    """Configurable alert thresholds for platform health monitoring."""

    error_rate_pct: float = 30.0  # Platform-wide error rate threshold
    latency_seconds: float = 60.0  # Average latency threshold
    provider_failure_count: int = 3  # Consecutive failures before alert
    rolling_window_minutes: float = 10.0  # Window for error rate calculation

    def validate_values(self) -> list[str]:
        """Return list of validation errors, empty if valid."""
        errors: list[str] = []
        if not (0 < self.error_rate_pct <= 100):
            errors.append("error_rate_pct must be between 0 (exclusive) and 100 (inclusive)")
        if self.latency_seconds <= 0:
            errors.append("latency_seconds must be positive")
        if self.provider_failure_count < 1:
            errors.append("provider_failure_count must be at least 1")
        if self.rolling_window_minutes <= 0:
            errors.append("rolling_window_minutes must be positive")
        return errors


class EscalationPolicy(BaseModel):
    """An escalation policy rule for the approval matrix."""

    name: str
    priority: int
    risk_levels: list[str] = Field(default_factory=list)
    routing: str = "founder_review"  # "auto_approve", "founder_review", "escalate_and_halt"

    def validate_values(self) -> list[str]:
        """Return list of validation errors, empty if valid."""
        errors: list[str] = []
        if not self.name:
            errors.append("escalation policy name must not be empty")
        valid_routings = {"auto_approve", "founder_review", "escalate_and_halt"}
        if self.routing not in valid_routings:
            errors.append(
                f"routing must be one of {sorted(valid_routings)}, got '{self.routing}'"
            )
        valid_risk_levels = {"low", "medium", "high", "critical"}
        for level in self.risk_levels:
            if level not in valid_risk_levels:
                errors.append(
                    f"invalid risk level '{level}'; must be one of {sorted(valid_risk_levels)}"
                )
        if self.priority < 0:
            errors.append("priority must be non-negative")
        return errors


class FormationConfig(BaseModel):
    """Simplified formation configuration for hot-reload."""

    name: str
    task_type: str
    role_model_mappings: dict[str, str] = Field(default_factory=dict)
    budget_strategy: dict[str, float] = Field(default_factory=dict)
    verification_protocol: str = "standard"

    def validate_values(self) -> list[str]:
        """Return list of validation errors, empty if valid."""
        errors: list[str] = []
        if not self.name:
            errors.append("formation name must not be empty")
        valid_task_types = {"bugfix", "feature", "refactor", "documentation", "security-audit"}
        if self.task_type not in valid_task_types:
            errors.append(
                f"task_type must be one of {sorted(valid_task_types)}, got '{self.task_type}'"
            )
        valid_protocols = {"standard", "property-based", "formal"}
        if self.verification_protocol not in valid_protocols:
            errors.append(
                f"verification_protocol must be one of {sorted(valid_protocols)}, "
                f"got '{self.verification_protocol}'"
            )
        # Validate budget_strategy percentages sum
        if self.budget_strategy:
            total = sum(self.budget_strategy.values())
            if abs(total - 1.0) > 0.01:
                errors.append(
                    f"budget_strategy percentages must sum to 1.0, got {total:.4f}"
                )
            for key, val in self.budget_strategy.items():
                if val < 0 or val > 1:
                    errors.append(f"budget_strategy['{key}'] must be between 0 and 1")
        return errors


class PlatformConfig(BaseModel):
    """Complete platform configuration that can be hot-reloaded."""

    budgets: BudgetConfig | None = None
    autonomy_mode: AutonomyMode | None = None
    formations: list[FormationConfig] | None = None
    escalation_policies: list[EscalationPolicy] | None = None
    alert_thresholds: AlertThresholds | None = None


# ---------------------------------------------------------------------------
# Validation result models
# ---------------------------------------------------------------------------


class ConfigValidationError(BaseModel):
    """A single validation error with field path and message."""

    field: str
    message: str


class ConfigReloadResult(BaseModel):
    """Result of a configuration reload attempt."""

    success: bool
    applied_at: float | None = None  # Timestamp when config was applied
    changes: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[ConfigValidationError] = Field(default_factory=list)
    message: str = ""


# ---------------------------------------------------------------------------
# ConfigReloader class
# ---------------------------------------------------------------------------


class ConfigReloader:
    """Loads, validates, and applies platform configuration changes.

    Supports hot-reload of configuration without service restart.
    Validates changes before applying, records changes in ExecutionTrace,
    and applies changes at the next decision point without interrupting
    current operations.

    Requirements: 54.1, 54.2, 54.3, 54.4
    """

    def __init__(self, execution_trace: Any | None = None) -> None:
        """Initialize the ConfigReloader.

        Args:
            execution_trace: Optional ExecutionTrace instance for recording
                config changes. If None, changes are not recorded in trace.
        """
        self._lock = Lock()
        self._execution_trace = execution_trace
        self._current_config = PlatformConfig()
        self._pending_changes: list[dict[str, Any]] = []
        self._last_reload_at: float = 0.0

    @property
    def current_config(self) -> PlatformConfig:
        """Return the current active platform configuration."""
        return self._current_config

    @property
    def last_reload_at(self) -> float:
        """Return timestamp of last successful reload."""
        return self._last_reload_at

    def validate_config(self, config: PlatformConfig) -> list[ConfigValidationError]:
        """Validate a platform configuration without applying it.

        Checks types, ranges, required fields, and internal consistency.

        Args:
            config: The PlatformConfig to validate.

        Returns:
            List of validation errors. Empty list means config is valid.

        Validates: Requirement 54.2
        """
        errors: list[ConfigValidationError] = []

        # Validate budgets
        if config.budgets is not None:
            for err_msg in config.budgets.validate_values():
                errors.append(ConfigValidationError(field="budgets", message=err_msg))

        # Validate autonomy_mode (enum already validates via Pydantic)
        # No additional validation needed since Pydantic enforces the enum

        # Validate formations
        if config.formations is not None:
            seen_names: set[str] = set()
            for i, formation in enumerate(config.formations):
                for err_msg in formation.validate_values():
                    errors.append(
                        ConfigValidationError(
                            field=f"formations[{i}]",
                            message=err_msg,
                        )
                    )
                if formation.name in seen_names:
                    errors.append(
                        ConfigValidationError(
                            field=f"formations[{i}]",
                            message=f"duplicate formation name: '{formation.name}'",
                        )
                    )
                seen_names.add(formation.name)

        # Validate escalation policies
        if config.escalation_policies is not None:
            seen_policy_names: set[str] = set()
            for i, policy in enumerate(config.escalation_policies):
                for err_msg in policy.validate_values():
                    errors.append(
                        ConfigValidationError(
                            field=f"escalation_policies[{i}]",
                            message=err_msg,
                        )
                    )
                if policy.name in seen_policy_names:
                    errors.append(
                        ConfigValidationError(
                            field=f"escalation_policies[{i}]",
                            message=f"duplicate policy name: '{policy.name}'",
                        )
                    )
                seen_policy_names.add(policy.name)

        # Validate alert thresholds
        if config.alert_thresholds is not None:
            for err_msg in config.alert_thresholds.validate_values():
                errors.append(
                    ConfigValidationError(field="alert_thresholds", message=err_msg)
                )

        return errors

    def reload(
        self,
        config: PlatformConfig,
        source: str = "api",
    ) -> ConfigReloadResult:
        """Validate and apply a new configuration.

        If validation fails, the previous configuration is retained and
        descriptive errors are returned. If validation passes, the config
        is applied and changes are recorded in the Execution_Trace.

        Args:
            config: The new PlatformConfig to apply.
            source: The change source ("api", "console", "file_reload").

        Returns:
            ConfigReloadResult with success status, changes made, or errors.

        Validates: Requirements 54.1, 54.2, 54.3, 54.4
        """
        # Step 1: Validate before applying (Requirement 54.2)
        errors = self.validate_config(config)
        if errors:
            return ConfigReloadResult(
                success=False,
                errors=errors,
                message="Configuration rejected: validation failed",
            )

        # Step 2: Compute changes (diff old vs new)
        with self._lock:
            changes = self._compute_changes(self._current_config, config)
            applied_at = time.time()

            # Step 3: Apply the configuration
            self._apply_config(config)
            self._last_reload_at = applied_at

            # Step 4: Record changes in Execution_Trace (Requirement 54.4)
            if changes and self._execution_trace is not None:
                self._record_changes_in_trace(changes, source, applied_at)

        return ConfigReloadResult(
            success=True,
            applied_at=applied_at,
            changes=changes,
            message=f"Configuration applied successfully ({len(changes)} change(s))",
        )

    def _apply_config(self, config: PlatformConfig) -> None:
        """Apply validated configuration to current state.

        Only updates fields that are explicitly set (not None).
        This enables partial configuration updates.

        Validates: Requirement 54.3 (apply at next decision point)
        """
        if config.budgets is not None:
            self._current_config.budgets = config.budgets
        if config.autonomy_mode is not None:
            self._current_config.autonomy_mode = config.autonomy_mode
        if config.formations is not None:
            self._current_config.formations = config.formations
        if config.escalation_policies is not None:
            self._current_config.escalation_policies = config.escalation_policies
        if config.alert_thresholds is not None:
            self._current_config.alert_thresholds = config.alert_thresholds

    def _compute_changes(
        self, old: PlatformConfig, new: PlatformConfig
    ) -> list[dict[str, Any]]:
        """Compute a list of changes between old and new configuration.

        Each change records: field, previous_value, new_value.
        """
        changes: list[dict[str, Any]] = []

        # Compare budgets
        if new.budgets is not None:
            old_budgets = old.budgets.model_dump() if old.budgets else None
            new_budgets = new.budgets.model_dump()
            if old_budgets != new_budgets:
                changes.append({
                    "field": "budgets",
                    "previous_value": old_budgets,
                    "new_value": new_budgets,
                })

        # Compare autonomy_mode
        if new.autonomy_mode is not None:
            old_mode = old.autonomy_mode.value if old.autonomy_mode else None
            new_mode = new.autonomy_mode.value
            if old_mode != new_mode:
                changes.append({
                    "field": "autonomy_mode",
                    "previous_value": old_mode,
                    "new_value": new_mode,
                })

        # Compare formations
        if new.formations is not None:
            old_formations = (
                [f.model_dump() for f in old.formations] if old.formations else None
            )
            new_formations = [f.model_dump() for f in new.formations]
            if old_formations != new_formations:
                changes.append({
                    "field": "formations",
                    "previous_value": old_formations,
                    "new_value": new_formations,
                })

        # Compare escalation policies
        if new.escalation_policies is not None:
            old_policies = (
                [p.model_dump() for p in old.escalation_policies]
                if old.escalation_policies
                else None
            )
            new_policies = [p.model_dump() for p in new.escalation_policies]
            if old_policies != new_policies:
                changes.append({
                    "field": "escalation_policies",
                    "previous_value": old_policies,
                    "new_value": new_policies,
                })

        # Compare alert thresholds
        if new.alert_thresholds is not None:
            old_thresholds = (
                old.alert_thresholds.model_dump() if old.alert_thresholds else None
            )
            new_thresholds = new.alert_thresholds.model_dump()
            if old_thresholds != new_thresholds:
                changes.append({
                    "field": "alert_thresholds",
                    "previous_value": old_thresholds,
                    "new_value": new_thresholds,
                })

        return changes

    def _record_changes_in_trace(
        self,
        changes: list[dict[str, Any]],
        source: str,
        applied_at: float,
    ) -> None:
        """Record config changes in the Execution_Trace.

        Each change is recorded as a decision with type 'config_change',
        including previous/new values and the source of the change.

        Validates: Requirement 54.4
        """
        for change in changes:
            state_snapshot = {
                "field": change["field"],
                "previous_value": change["previous_value"],
                "new_value": change["new_value"],
                "source": source,
            }
            self._execution_trace.record_decision(
                task_id="__platform__",
                decision_type="config_change",
                state_snapshot=state_snapshot,
                policy="config_hot_reload",
                outcome=f"applied_{change['field']}",
                nd_inputs={"timestamp": applied_at, "source": source},
            )
