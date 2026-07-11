"""Formation Manager: CRUD, effectiveness scoring, and automatic recommendations.

This module implements Formations (Requirements 14–15, 40):
- Formation CRUD (create, read, update, delete)
- Default Formations for common task types
- Effectiveness scoring using weighted composite formula
- Automatic Formation recommendation based on historical success
- Underperformance detection and founder notification (Requirement 40.3)
- Telemetry event emission on underperformance detection (Requirement 40.3)
"""

from __future__ import annotations

import logging
from statistics import mean
from typing import TYPE_CHECKING, Callable

from pydantic import BaseModel, Field

from vikram_orchestrator.model_router import ModelCapability

if TYPE_CHECKING:
    from vikram_orchestrator.telemetry_client import TelemetryClient

logger = logging.getLogger(__name__)


class BudgetStrategy(BaseModel):
    """Percentage allocation of budget across work phases."""

    planning: float = 0.10
    implementation: float = 0.60
    verification: float = 0.20
    review: float = 0.10


class Formation(BaseModel):
    """A team topology configuration for a task type.

    Maps roles to specific model/provider combinations, defines
    budget allocation and verification protocol.

    Validates: Requirements 14.1
    """

    name: str
    task_type: str  # "bugfix", "feature", "refactor", "documentation", "security-audit"
    role_model_mappings: dict[str, ModelCapability] = Field(default_factory=dict)
    budget_strategy: BudgetStrategy = Field(default_factory=BudgetStrategy)
    verification_protocol: str = "standard"  # "standard", "property-based", "formal"


class FormationRecord(BaseModel):
    """Historical record of a formation's performance on a task.

    Used to compute effectiveness scores and detect underperformance.

    Validates: Requirements 15.2
    """

    formation_name: str
    task_type: str
    outcome: str  # "success", "partial", "failure", "rollback"
    forecast: float  # forecasted cost in USD
    actual_cost: float  # actual cost in USD
    time_limit: float  # time limit in seconds
    actual_duration: float  # actual duration in seconds
    verification_first_pass: bool = False


class RecommendationNotification(BaseModel):
    """Notification sent to the founder when a formation is underperforming.

    Validates: Requirements 40.3
    """

    formation_name: str
    task_type: str
    success_rate: float
    task_count: int
    message: str
    recommended_action: str = "review_or_replace"


# --- Default Formations ---

_DEFAULT_MODELS = {
    "planner": ModelCapability(
        model="claude-sonnet-4-20250514",
        provider="anthropic",
        cost_tier=3,
        capability_score=3,
        supports_structured_output=True,
    ),
    "implementer": ModelCapability(
        model="claude-sonnet-4-20250514",
        provider="anthropic",
        cost_tier=3,
        capability_score=3,
        supports_structured_output=True,
    ),
    "reviewer": ModelCapability(
        model="claude-sonnet-4-20250514",
        provider="anthropic",
        cost_tier=3,
        capability_score=3,
        supports_structured_output=True,
    ),
    "verifier": ModelCapability(
        model="claude-sonnet-4-20250514",
        provider="anthropic",
        cost_tier=3,
        capability_score=3,
        supports_structured_output=True,
    ),
}

_ECONOMICAL_MODELS = {
    "planner": ModelCapability(
        model="claude-haiku",
        provider="anthropic",
        cost_tier=1,
        capability_score=1,
        supports_structured_output=True,
    ),
    "implementer": ModelCapability(
        model="claude-haiku",
        provider="anthropic",
        cost_tier=1,
        capability_score=1,
        supports_structured_output=True,
    ),
    "reviewer": ModelCapability(
        model="claude-haiku",
        provider="anthropic",
        cost_tier=1,
        capability_score=1,
        supports_structured_output=True,
    ),
    "verifier": ModelCapability(
        model="claude-haiku",
        provider="anthropic",
        cost_tier=1,
        capability_score=1,
        supports_structured_output=True,
    ),
}

_CAPABLE_MODELS = {
    "planner": ModelCapability(
        model="claude-opus",
        provider="anthropic",
        cost_tier=4,
        capability_score=4,
        supports_structured_output=True,
    ),
    "implementer": ModelCapability(
        model="claude-sonnet-4-20250514",
        provider="anthropic",
        cost_tier=3,
        capability_score=3,
        supports_structured_output=True,
    ),
    "reviewer": ModelCapability(
        model="claude-opus",
        provider="anthropic",
        cost_tier=4,
        capability_score=4,
        supports_structured_output=True,
    ),
    "verifier": ModelCapability(
        model="claude-sonnet-4-20250514",
        provider="anthropic",
        cost_tier=3,
        capability_score=3,
        supports_structured_output=True,
    ),
}


def _build_default_formations() -> list[Formation]:
    """Build the set of default formations for common task types.

    Validates: Requirements 14.3
    """
    return [
        Formation(
            name="bugfix-standard",
            task_type="bugfix",
            role_model_mappings=_DEFAULT_MODELS,
            budget_strategy=BudgetStrategy(
                planning=0.15,
                implementation=0.50,
                verification=0.25,
                review=0.10,
            ),
            verification_protocol="property-based",
        ),
        Formation(
            name="feature-standard",
            task_type="feature",
            role_model_mappings=_DEFAULT_MODELS,
            budget_strategy=BudgetStrategy(
                planning=0.15,
                implementation=0.55,
                verification=0.20,
                review=0.10,
            ),
            verification_protocol="standard",
        ),
        Formation(
            name="refactor-standard",
            task_type="refactor",
            role_model_mappings=_DEFAULT_MODELS,
            budget_strategy=BudgetStrategy(
                planning=0.20,
                implementation=0.45,
                verification=0.25,
                review=0.10,
            ),
            verification_protocol="property-based",
        ),
        Formation(
            name="documentation-standard",
            task_type="documentation",
            role_model_mappings=_ECONOMICAL_MODELS,
            budget_strategy=BudgetStrategy(
                planning=0.10,
                implementation=0.70,
                verification=0.10,
                review=0.10,
            ),
            verification_protocol="standard",
        ),
        Formation(
            name="security-audit-standard",
            task_type="security-audit",
            role_model_mappings=_CAPABLE_MODELS,
            budget_strategy=BudgetStrategy(
                planning=0.20,
                implementation=0.35,
                verification=0.30,
                review=0.15,
            ),
            verification_protocol="formal",
        ),
    ]


# Threshold for underperformance notification (Requirement 15.3)
UNDERPERFORMANCE_THRESHOLD = 0.60
UNDERPERFORMANCE_TASK_COUNT = 20


class FormationManager:
    """Manages Formation lifecycle and effectiveness scoring.

    Provides CRUD operations, effectiveness computation, and
    automatic recommendation based on historical performance data.

    Validates: Requirements 14.1, 14.2, 14.3, 14.4, 15.1, 15.2, 15.3, 15.4, 40.1, 40.2, 40.3
    """

    def __init__(
        self,
        on_recommendation: Callable[[RecommendationNotification], None] | None = None,
        telemetry_client: TelemetryClient | None = None,
    ) -> None:
        # In-memory storage (SQLite can be added later)
        self._formations: dict[str, Formation] = {}
        self._records: list[FormationRecord] = []
        self._on_recommendation = on_recommendation
        self._telemetry_client = telemetry_client

        # Load default formations
        for formation in _build_default_formations():
            self._formations[formation.name] = formation

    def create(self, formation: Formation) -> Formation:
        """Create a new formation.

        Raises ValueError if a formation with the same name already exists.

        Validates: Requirements 14.4
        """
        if formation.name in self._formations:
            raise ValueError(f"Formation '{formation.name}' already exists")
        self._formations[formation.name] = formation
        return formation

    def get(self, name: str) -> Formation | None:
        """Get a formation by name. Returns None if not found.

        Validates: Requirements 14.2
        """
        return self._formations.get(name)

    def list(self) -> list[Formation]:
        """List all formations.

        Validates: Requirements 14.4
        """
        return list(self._formations.values())

    def update(self, name: str, formation: Formation) -> Formation:
        """Update an existing formation.

        Raises ValueError if the formation does not exist.

        Validates: Requirements 14.4
        """
        if name not in self._formations:
            raise ValueError(f"Formation '{name}' not found")
        # If the name is changing, remove old key and insert new
        if formation.name != name:
            del self._formations[name]
        self._formations[formation.name] = formation
        return formation

    def delete(self, name: str) -> bool:
        """Delete a formation by name. Returns True if deleted, False if not found.

        Validates: Requirements 14.4
        """
        if name in self._formations:
            del self._formations[name]
            return True
        return False

    def record_outcome(self, record: FormationRecord) -> None:
        """Record a task outcome for a formation.

        After recording, checks if the formation is now underperforming
        and fires a recommendation notification to the founder if so.

        Validates: Requirements 15.2, 40.1, 40.3
        """
        self._records.append(record)

        # Check underperformance and notify founder if needed (Requirement 40.3)
        if self.is_underperforming(record.formation_name, record.task_type):
            self._notify_underperformance(record.formation_name, record.task_type)

    def get_effectiveness(self, formation_name: str, task_type: str) -> float:
        """Compute effectiveness score for a formation on a given task type.

        Uses the weighted composite formula from the design:
          effectiveness = (
              0.40 * success_rate +
              0.20 * min(cost_efficiency, 1.5) / 1.5 +
              0.15 * min(time_efficiency, 2.0) / 2.0 +
              0.25 * first_pass_rate
          )

        Returns 0.5 (neutral) if fewer than 5 records exist.

        Validates: Requirements 15.2
        """
        records = self._get_formation_records(formation_name, task_type, last_n=50)

        if len(records) < 5:
            return 0.5  # insufficient data, neutral score

        success_count = sum(1 for r in records if r.outcome == "success")
        success_rate = success_count / len(records)

        # Cost efficiency: forecast / actual_cost. >1 means under-budget.
        # Guard against zero actual_cost.
        cost_efficiencies = [
            r.forecast / r.actual_cost
            for r in records
            if r.actual_cost > 0
        ]
        cost_efficiency = mean(cost_efficiencies) if cost_efficiencies else 1.0

        # Time efficiency: time_limit / actual_duration. >1 means faster than limit.
        # Guard against zero actual_duration.
        time_efficiencies = [
            r.time_limit / r.actual_duration
            for r in records
            if r.actual_duration > 0
        ]
        time_efficiency = mean(time_efficiencies) if time_efficiencies else 1.0

        first_pass_count = sum(1 for r in records if r.verification_first_pass)
        first_pass_rate = first_pass_count / len(records)

        # Weighted composite
        return (
            0.40 * success_rate
            + 0.20 * min(cost_efficiency, 1.5) / 1.5
            + 0.15 * min(time_efficiency, 2.0) / 2.0
            + 0.25 * first_pass_rate
        )

    def recommend(self, task_type: str, complexity: str = "moderate") -> Formation | None:
        """Recommend the best formation for a given task type based on effectiveness.

        Returns the formation with the highest effectiveness score for the
        given task type. If no formations match the task type, returns None.

        Validates: Requirements 15.1
        """
        candidates = [
            f for f in self._formations.values()
            if f.task_type == task_type
        ]
        if not candidates:
            return None

        # Score each candidate
        scored = [
            (f, self.get_effectiveness(f.name, task_type))
            for f in candidates
        ]

        # Sort by effectiveness (highest first)
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[0][0]

    def is_underperforming(self, formation_name: str, task_type: str) -> bool:
        """Check if a formation is consistently underperforming.

        A formation is underperforming if its success rate is below 60%
        over the last 20 tasks.

        Validates: Requirements 15.3
        """
        records = self._get_formation_records(
            formation_name, task_type, last_n=UNDERPERFORMANCE_TASK_COUNT
        )
        if len(records) < UNDERPERFORMANCE_TASK_COUNT:
            return False  # not enough data to judge

        success_count = sum(1 for r in records if r.outcome == "success")
        success_rate = success_count / len(records)
        return success_rate < UNDERPERFORMANCE_THRESHOLD

    def get_effectiveness_comparison(self) -> dict[str, dict[str, float]]:
        """Get effectiveness scores for all formations grouped by task type.

        Returns a dict of {formation_name: {task_type: effectiveness_score}}.

        Validates: Requirements 15.4
        """
        comparison: dict[str, dict[str, float]] = {}
        for formation in self._formations.values():
            name = formation.name
            task_type = formation.task_type
            score = self.get_effectiveness(name, task_type)
            if name not in comparison:
                comparison[name] = {}
            comparison[name][task_type] = score
        return comparison

    def set_recommendation_callback(
        self,
        callback: Callable[[RecommendationNotification], None] | None,
    ) -> None:
        """Set or replace the callback invoked when a formation underperforms.

        Validates: Requirements 40.3
        """
        self._on_recommendation = callback

    def set_telemetry_client(self, client: TelemetryClient | None) -> None:
        """Set or replace the telemetry client for emitting underperformance events.

        Validates: Requirements 40.3
        """
        self._telemetry_client = client

    def _notify_underperformance(self, formation_name: str, task_type: str) -> None:
        """Send recommendation notification when a formation underperforms.

        Emits a telemetry event and invokes the on_recommendation callback
        to notify the founder via their configured channel.

        Validates: Requirements 40.3
        """
        records = self._get_formation_records(
            formation_name, task_type, last_n=UNDERPERFORMANCE_TASK_COUNT
        )
        success_count = sum(1 for r in records if r.outcome == "success")
        success_rate = success_count / len(records) if records else 0.0

        notification = RecommendationNotification(
            formation_name=formation_name,
            task_type=task_type,
            success_rate=success_rate,
            task_count=len(records),
            message=(
                f"Formation '{formation_name}' is underperforming for task type "
                f"'{task_type}': success rate {success_rate:.0%} over {len(records)} "
                f"tasks (threshold: {UNDERPERFORMANCE_THRESHOLD:.0%}). "
                f"Consider reviewing or replacing this formation."
            ),
            recommended_action="review_or_replace",
        )

        logger.warning(
            "Formation underperformance detected: %s (task_type=%s, success_rate=%.2f)",
            formation_name,
            task_type,
            success_rate,
        )

        # Emit telemetry event for underperformance detection (Requirement 40.3)
        if self._telemetry_client is not None:
            try:
                self._telemetry_client.emit_event(
                    event_type="formation_underperformance",
                    task_id="system",
                    attributes={
                        "formation_name": formation_name,
                        "task_type": task_type,
                        "success_rate": success_rate,
                        "task_count": len(records),
                        "threshold": UNDERPERFORMANCE_THRESHOLD,
                        "recommended_action": "review_or_replace",
                    },
                )
            except Exception:
                logger.exception(
                    "Failed to emit telemetry event for formation underperformance '%s'",
                    formation_name,
                )

        if self._on_recommendation is not None:
            try:
                self._on_recommendation(notification)
            except Exception:
                logger.exception(
                    "Failed to deliver recommendation notification for formation '%s'",
                    formation_name,
                )

    def _get_formation_records(
        self, formation_name: str, task_type: str, last_n: int = 50
    ) -> list[FormationRecord]:
        """Get the last N records for a formation+task_type pair."""
        matching = [
            r for r in self._records
            if r.formation_name == formation_name and r.task_type == task_type
        ]
        # Return the last N records (most recent)
        return matching[-last_n:]
