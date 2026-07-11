"""Model Router: complexity classification, model selection, budget-responsive downgrade, performance tracking.

This module implements the Model_Router subsystem (Requirements 11-15):
- Complexity-based model selection
- Budget-responsive model downgrade
- Model performance tracking
- Formation recommendations
"""

from __future__ import annotations

from collections import deque
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ComplexityTier(str, Enum):
    """Task complexity classification tiers."""

    ROUTINE = "routine"  # docs, config, single-line fixes
    MODERATE = "moderate"  # single-file logic, test additions
    COMPLEX = "complex"  # multi-file refactoring, new features
    CRITICAL = "critical"  # architecture changes, security code


class ComplexitySignals(BaseModel):
    """Input signals for complexity classification."""

    objective_scope: str = ""
    target_file_count: int = 1
    repo_size_files: int = 100
    language_count: int = 1
    change_type: str = "logic"  # "documentation", "config", "logic", "architecture"
    security_relevant: bool = False
    test_modification: bool = False


class ModelCapability(BaseModel):
    """Defines a model's capabilities and cost tier."""

    model: str
    provider: str
    cost_tier: int = 1  # 1=cheapest, 4=most expensive
    capability_score: int = 1  # 1=basic, 4=most capable
    supports_structured_output: bool = False


class RoleCapabilityFloor(BaseModel):
    """Minimum capability requirements for a role."""

    role: str
    min_capability_score: int = 1
    requires_structured_output: bool = False


class ModelSelection(BaseModel):
    """Result of model selection."""

    model: str
    provider: str
    tier: ComplexityTier
    reason: str
    budget_remaining_pct: float
    downgraded: bool = False


class ModelPerformanceRecord(BaseModel):
    """Rolling performance stats for a model+tier combination."""

    model: str
    provider: str
    role: str
    complexity_tier: ComplexityTier
    success_rate: float  # rolling over last 200 calls
    avg_latency_ms: float = 0.0
    cost_per_success: float = 0.0
    total_calls: int = 0


class ModelRouter:
    """Routes model selection based on complexity, budget, and performance."""

    WINDOW_SIZE = 200  # Rolling window for success rate computation

    def __init__(
        self,
        available_models: list[ModelCapability] | None = None,
        role_floors: list[RoleCapabilityFloor] | None = None,
    ) -> None:
        self.available_models = available_models or []
        self.role_floors = {rf.role: rf for rf in (role_floors or [])}
        # Performance tracking: key = (model, provider, role, tier)
        self._outcomes: dict[tuple[str, str, str, str], deque] = {}

    # Valid change types for scoring
    _CHANGE_TYPE_SCORES: dict[str, int] = {
        "documentation": 0,
        "config": 10,
        "logic": 25,
        "architecture": 40,
    }

    def classify_complexity(self, signals: ComplexitySignals) -> ComplexityTier:
        """Classify task complexity based on objective signals.

        Scoring components:
        - File count contribution (0-30 points)
        - Change type contribution (0-40 points)
        - Security flag (0-20 points)
        - Language diversity (0-10 points)

        Mapping:
          score <= 15 -> ROUTINE
          score <= 40 -> MODERATE
          score <= 70 -> COMPLEX
          score > 70  -> CRITICAL

        Validates: Requirements 11.1, 11.2
        """
        if signals.change_type not in self._CHANGE_TYPE_SCORES:
            raise ValueError(
                f"Invalid change_type '{signals.change_type}'. "
                f"Must be one of: {sorted(self._CHANGE_TYPE_SCORES.keys())}"
            )

        score = 0

        # File count contribution (0-30 points)
        if signals.target_file_count == 1:
            score += 5
        elif signals.target_file_count <= 3:
            score += 15
        elif signals.target_file_count <= 8:
            score += 25
        else:
            score += 30

        # Change type contribution (0-40 points)
        score += self._CHANGE_TYPE_SCORES[signals.change_type]

        # Security flag (0-20 points)
        if signals.security_relevant:
            score += 20

        # Language diversity (0-10 points)
        if signals.language_count > 2:
            score += 10
        elif signals.language_count > 1:
            score += 5

        # Map to tier
        if score <= 15:
            return ComplexityTier.ROUTINE
        elif score <= 40:
            return ComplexityTier.MODERATE
        elif score <= 70:
            return ComplexityTier.COMPLEX
        else:
            return ComplexityTier.CRITICAL

    def select_model(
        self,
        task_id: str,
        role: str,
        tier: ComplexityTier,
        budget_position: float,
    ) -> ModelSelection:
        """Select optimal model based on tier and budget position.

        Budget-responsive downgrade:
          70%+ remaining -> optimal model for tier
          30-70% remaining -> one tier cheaper
          <30% remaining -> cheapest meeting capability floor

        Tier-appropriate selection (at comfortable budget):
          ROUTINE   -> cheapest model meeting floor (cost_tier 1)
          MODERATE  -> mid-tier model (cost_tier 2)
          COMPLEX   -> capable model (cost_tier 3)
          CRITICAL  -> most capable model regardless of cost (highest capability_score)

        Validates: Requirements 11.3, 11.4, 12.1, 12.2, 12.3, 12.4
        """
        if not self.available_models:
            raise ValueError("No available models configured for selection")

        # Determine the capability floor for this role
        floor = self.role_floors.get(role)
        min_capability = floor.min_capability_score if floor else 1
        requires_structured = floor.requires_structured_output if floor else False

        # Filter models meeting the capability floor
        eligible_models = [
            m for m in self.available_models
            if m.capability_score >= min_capability
            and (not requires_structured or m.supports_structured_output)
        ]

        if not eligible_models:
            # Fallback: use all models if none meet the floor (shouldn't happen
            # if configured properly, but avoids crash)
            eligible_models = list(self.available_models)

        # CRITICAL tier always selects most capable regardless of budget
        if tier == ComplexityTier.CRITICAL:
            selected = max(eligible_models, key=lambda m: m.capability_score)
            return ModelSelection(
                model=selected.model,
                provider=selected.provider,
                tier=tier,
                reason=f"Critical tier: selected most capable model ({selected.model})",
                budget_remaining_pct=budget_position,
                downgraded=False,
            )

        # Determine the target cost tier based on complexity
        tier_to_target_cost: dict[ComplexityTier, int] = {
            ComplexityTier.ROUTINE: 1,
            ComplexityTier.MODERATE: 2,
            ComplexityTier.COMPLEX: 3,
        }
        target_cost_tier = tier_to_target_cost[tier]

        # Apply budget-responsive downgrade
        downgraded = False
        if budget_position < 0.30:
            # Severe budget pressure: cheapest meeting floor
            target_cost_tier = min(m.cost_tier for m in eligible_models)
            downgraded = True
        elif budget_position < 0.70:
            # Moderate budget pressure: one tier cheaper
            target_cost_tier = max(target_cost_tier - 1, min(m.cost_tier for m in eligible_models))
            downgraded = True

        # Find the best model at the target cost tier (or closest available)
        selected = self._select_at_cost_tier(eligible_models, target_cost_tier)

        # Build reason string
        if downgraded:
            reason = (
                f"Budget-responsive downgrade at {budget_position:.0%} remaining: "
                f"selected {selected.model} (cost_tier={selected.cost_tier})"
            )
        else:
            reason = (
                f"Tier {tier.value}: selected {selected.model} "
                f"(cost_tier={selected.cost_tier})"
            )

        return ModelSelection(
            model=selected.model,
            provider=selected.provider,
            tier=tier,
            reason=reason,
            budget_remaining_pct=budget_position,
            downgraded=downgraded,
        )

    def _select_at_cost_tier(
        self,
        eligible_models: list[ModelCapability],
        target_cost_tier: int,
    ) -> ModelCapability:
        """Select the best model at or near the target cost tier.

        Prefers exact match on cost_tier. If no exact match, selects the
        closest available model (preferring cheaper if equidistant).

        When multiple models have the same cost tier, prefer the one with
        the higher rolling success rate (Requirement 13.3).
        """
        # Try exact match first
        exact = [m for m in eligible_models if m.cost_tier == target_cost_tier]
        if exact:
            # Among exact matches, prefer higher success rate, then higher capability
            return max(exact, key=lambda m: (self._model_success_score(m), m.capability_score))

        # No exact match: find closest cost tier
        sorted_by_distance = sorted(
            eligible_models,
            key=lambda m: (abs(m.cost_tier - target_cost_tier), m.cost_tier),
        )
        return sorted_by_distance[0]

    def _model_success_score(self, model: ModelCapability) -> float:
        """Get the best rolling success rate across roles/tiers for a model.

        Used for tie-breaking among same-cost models. Returns 0.0 if no data.
        """
        best_rate = 0.0
        for (m, p, _role, _tier), window in self._outcomes.items():
            if m == model.model and p == model.provider and window:
                rate = sum(1 for o in window if o == "success") / len(window)
                best_rate = max(best_rate, rate)
        return best_rate

    def record_outcome(
        self,
        model: str,
        provider: str,
        role: str,
        tier: ComplexityTier,
        outcome: str,
    ) -> None:
        """Record call outcome for performance tracking.

        Outcomes: "success", "partial_success", "failure", "timeout"

        Appends the outcome to a deque bounded at WINDOW_SIZE (200).
        The key is (model, provider, role, tier_value).

        Validates: Requirements 13.1
        """
        key = (model, provider, role, tier.value if isinstance(tier, ComplexityTier) else tier)
        if key not in self._outcomes:
            self._outcomes[key] = deque(maxlen=self.WINDOW_SIZE)
        self._outcomes[key].append(outcome)

    def get_rolling_success_rate(
        self, model: str, provider: str, role: str, tier: ComplexityTier
    ) -> float:
        """Compute rolling success rate over last 200 calls.

        Returns count(success) / total in [0, 1].
        Returns 0.0 if no calls recorded.

        Validates: Requirements 13.2
        """
        key = (model, provider, role, tier.value if isinstance(tier, ComplexityTier) else tier)
        window = self._outcomes.get(key)
        if not window:
            return 0.0
        return sum(1 for o in window if o == "success") / len(window)

    def get_performance(self) -> list[ModelPerformanceRecord]:
        """Return performance records for all tracked model+tier combinations.

        Validates: Requirements 13.4
        """
        records: list[ModelPerformanceRecord] = []
        for (model, provider, role, tier_value), window in self._outcomes.items():
            if not window:
                continue
            success_count = sum(1 for o in window if o == "success")
            total = len(window)
            success_rate = success_count / total
            records.append(
                ModelPerformanceRecord(
                    model=model,
                    provider=provider,
                    role=role,
                    complexity_tier=ComplexityTier(tier_value),
                    success_rate=success_rate,
                    avg_latency_ms=0.0,
                    cost_per_success=0.0,
                    total_calls=total,
                )
            )
        return records
