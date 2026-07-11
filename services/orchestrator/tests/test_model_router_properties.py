"""Property-based tests for the Model Router subsystem.

These tests define correctness properties for:
- Complexity classification (Property 12)
- Budget-responsive model downgrade (Property 13)
- Rolling success rate computation (Property 14)

Run with:
  cd services/orchestrator && .venv/bin/python -m pytest tests/test_model_router_properties.py -v

Validates: Requirements 11.1, 11.2, 11.3, 11.4, 12.1, 12.3, 13.2, 13.3
"""

from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import given, settings, assume

from vikram_orchestrator.model_router import (
    ComplexitySignals,
    ComplexityTier,
    ModelCapability,
    ModelRouter,
    ModelSelection,
    RoleCapabilityFloor,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


def change_type_st() -> st.SearchStrategy[str]:
    """Generate valid change type strings."""
    return st.sampled_from(["documentation", "config", "logic", "architecture"])


def complexity_signals_st() -> st.SearchStrategy[ComplexitySignals]:
    """Generate random ComplexitySignals covering all valid input ranges."""
    return st.builds(
        ComplexitySignals,
        objective_scope=st.text(min_size=0, max_size=100),
        target_file_count=st.integers(min_value=1, max_value=100),
        repo_size_files=st.integers(min_value=1, max_value=100000),
        language_count=st.integers(min_value=1, max_value=20),
        change_type=change_type_st(),
        security_relevant=st.booleans(),
        test_modification=st.booleans(),
    )


def budget_position_st() -> st.SearchStrategy[float]:
    """Generate budget positions in [0.0, 1.0]."""
    return st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)


def model_capability_st(
    cost_tier: int | None = None,
    capability_score: int | None = None,
) -> st.SearchStrategy[ModelCapability]:
    """Generate a model capability entry."""
    return st.builds(
        ModelCapability,
        model=st.sampled_from(["gpt-4o", "gpt-4o-mini", "claude-sonnet", "claude-haiku", "nova-pro", "nova-lite"]),
        provider=st.sampled_from(["openai", "anthropic", "bedrock"]),
        cost_tier=st.just(cost_tier) if cost_tier else st.integers(min_value=1, max_value=4),
        capability_score=st.just(capability_score) if capability_score else st.integers(min_value=1, max_value=4),
        supports_structured_output=st.booleans(),
    )


def outcome_st() -> st.SearchStrategy[str]:
    """Generate valid call outcome strings."""
    return st.sampled_from(["success", "partial_success", "failure", "timeout"])


def outcome_sequence_st() -> st.SearchStrategy[list[str]]:
    """Generate sequences of outcomes for success rate computation."""
    return st.lists(outcome_st(), min_size=1, max_size=400)


def available_models_st() -> st.SearchStrategy[list[ModelCapability]]:
    """Generate a list of available models with diverse cost/capability tiers."""
    # Ensure we have at least one model per cost tier for meaningful selection tests
    cheapest = st.builds(
        ModelCapability,
        model=st.just("nova-lite"),
        provider=st.just("bedrock"),
        cost_tier=st.just(1),
        capability_score=st.just(1),
        supports_structured_output=st.just(False),
    )
    mid_tier = st.builds(
        ModelCapability,
        model=st.just("gpt-4o-mini"),
        provider=st.just("openai"),
        cost_tier=st.just(2),
        capability_score=st.just(2),
        supports_structured_output=st.just(True),
    )
    capable = st.builds(
        ModelCapability,
        model=st.just("claude-sonnet"),
        provider=st.just("anthropic"),
        cost_tier=st.just(3),
        capability_score=st.just(3),
        supports_structured_output=st.just(True),
    )
    most_capable = st.builds(
        ModelCapability,
        model=st.just("gpt-4o"),
        provider=st.just("openai"),
        cost_tier=st.just(4),
        capability_score=st.just(4),
        supports_structured_output=st.just(True),
    )
    return st.tuples(cheapest, mid_tier, capable, most_capable).map(list)


def role_floor_st() -> st.SearchStrategy[RoleCapabilityFloor]:
    """Generate a role capability floor."""
    return st.builds(
        RoleCapabilityFloor,
        role=st.sampled_from(["lead", "engineer", "reviewer", "runner", "qa"]),
        min_capability_score=st.integers(min_value=1, max_value=3),
        requires_structured_output=st.booleans(),
    )


# ---------------------------------------------------------------------------
# Property 12: Complexity Classification Validity and Tier-Appropriate
#              Model Selection
# Validates: Requirements 11.1, 11.2, 11.3, 11.4
# ---------------------------------------------------------------------------


class TestComplexityClassificationValidity:
    """classify_complexity always returns one of 4 valid tiers.
    Selection chooses most economical for routine, most capable for critical."""

    @given(signals=complexity_signals_st())
    @settings(max_examples=200)
    def test_classify_returns_valid_tier(self, signals: ComplexitySignals) -> None:
        """**Validates: Requirements 11.1, 11.2**

        For any valid ComplexitySignals input, classify_complexity must return
        exactly one of the four defined complexity tiers.
        """
        router = ModelRouter()
        tier = router.classify_complexity(signals)

        valid_tiers = {
            ComplexityTier.ROUTINE,
            ComplexityTier.MODERATE,
            ComplexityTier.COMPLEX,
            ComplexityTier.CRITICAL,
        }
        assert tier in valid_tiers, (
            f"classify_complexity returned {tier!r}, expected one of {valid_tiers}"
        )

    @given(signals=complexity_signals_st())
    @settings(max_examples=200)
    def test_classify_is_deterministic(self, signals: ComplexitySignals) -> None:
        """**Validates: Requirements 11.1, 11.2**

        classify_complexity is a pure function: same signals → same tier.
        """
        router = ModelRouter()
        tier1 = router.classify_complexity(signals)
        tier2 = router.classify_complexity(signals)
        assert tier1 == tier2

    @given(signals=complexity_signals_st(), models=available_models_st())
    @settings(max_examples=100)
    def test_routine_selects_most_economical(
        self, signals: ComplexitySignals, models: list[ModelCapability]
    ) -> None:
        """**Validates: Requirements 11.3**

        When complexity is classified as routine, model selection chooses
        the most economical model (lowest cost_tier) meeting capability floor.
        """
        router = ModelRouter(available_models=models)
        tier = router.classify_complexity(signals)
        assume(tier == ComplexityTier.ROUTINE)

        selection = router.select_model(
            task_id="test-001",
            role="engineer",
            tier=tier,
            budget_position=0.8,  # 80% remaining, no downgrade pressure
        )

        # For routine tasks at comfortable budget, should pick cheapest
        cheapest_cost_tier = min(m.cost_tier for m in models)
        selected_model = next(m for m in models if m.model == selection.model)
        assert selected_model.cost_tier == cheapest_cost_tier, (
            f"Routine task should select cheapest model (cost_tier={cheapest_cost_tier}), "
            f"but got {selected_model.model} with cost_tier={selected_model.cost_tier}"
        )

    @given(signals=complexity_signals_st(), models=available_models_st())
    @settings(max_examples=100)
    def test_critical_selects_most_capable(
        self, signals: ComplexitySignals, models: list[ModelCapability]
    ) -> None:
        """**Validates: Requirements 11.4**

        When complexity is classified as critical, model selection chooses
        the most capable model (highest capability_score) regardless of cost.
        """
        router = ModelRouter(available_models=models)
        tier = router.classify_complexity(signals)
        assume(tier == ComplexityTier.CRITICAL)

        selection = router.select_model(
            task_id="test-002",
            role="engineer",
            tier=tier,
            budget_position=0.8,
        )

        # For critical tasks, should pick most capable
        max_capability = max(m.capability_score for m in models)
        selected_model = next(m for m in models if m.model == selection.model)
        assert selected_model.capability_score == max_capability, (
            f"Critical task should select most capable model (capability={max_capability}), "
            f"but got {selected_model.model} with capability={selected_model.capability_score}"
        )

    @given(
        data=st.data(),
        models=available_models_st(),
    )
    @settings(max_examples=100)
    def test_selection_returns_valid_model_selection(
        self, data: st.DataObject, models: list[ModelCapability]
    ) -> None:
        """**Validates: Requirements 11.3, 11.4**

        select_model always returns a ModelSelection with a model that exists
        in the available models list and a valid ComplexityTier.
        """
        tier = data.draw(st.sampled_from(list(ComplexityTier)))
        budget = data.draw(budget_position_st())
        role = data.draw(st.sampled_from(["lead", "engineer", "reviewer", "runner", "qa"]))

        router = ModelRouter(available_models=models)
        selection = router.select_model(
            task_id="test-003",
            role=role,
            tier=tier,
            budget_position=budget,
        )

        assert isinstance(selection, ModelSelection)
        assert selection.tier == tier
        assert selection.model in [m.model for m in models]
        assert selection.provider in [m.provider for m in models]
        assert 0.0 <= selection.budget_remaining_pct <= 1.0


# ---------------------------------------------------------------------------
# Property 13: Budget-Responsive Downgrade with Capability Floor
# Validates: Requirements 12.1, 12.3
# ---------------------------------------------------------------------------


class TestBudgetResponsiveDowngrade:
    """At budget <30%, model downgrades to cheapest meeting capability floor.
    Never downgrades below capability floor."""

    @given(
        budget=st.floats(min_value=0.0, max_value=0.29, allow_nan=False, allow_infinity=False),
        models=available_models_st(),
        role_floor=role_floor_st(),
    )
    @settings(max_examples=200)
    def test_low_budget_downgrades_to_cheapest_meeting_floor(
        self, budget: float, models: list[ModelCapability], role_floor: RoleCapabilityFloor
    ) -> None:
        """**Validates: Requirements 12.1, 12.3**

        When remaining budget is below 30%, the router selects the cheapest
        model that still meets the capability floor for the role.
        """
        router = ModelRouter(
            available_models=models,
            role_floors=[role_floor],
        )

        # Find models meeting the floor
        meeting_floor = [
            m for m in models
            if m.capability_score >= role_floor.min_capability_score
            and (not role_floor.requires_structured_output or m.supports_structured_output)
        ]
        assume(len(meeting_floor) > 0)

        selection = router.select_model(
            task_id="test-budget-low",
            role=role_floor.role,
            tier=ComplexityTier.COMPLEX,  # Use complex to ensure downgrade is from budget, not tier
            budget_position=budget,
        )

        # Verify the selected model meets the capability floor
        selected = next(m for m in models if m.model == selection.model)
        assert selected.capability_score >= role_floor.min_capability_score, (
            f"Selected model {selected.model} (capability={selected.capability_score}) "
            f"is below floor ({role_floor.min_capability_score}) for role {role_floor.role}"
        )
        if role_floor.requires_structured_output:
            assert selected.supports_structured_output, (
                f"Role {role_floor.role} requires structured output but selected "
                f"{selected.model} does not support it"
            )

        # Verify it's the cheapest among those meeting the floor
        cheapest_meeting_floor = min(m.cost_tier for m in meeting_floor)
        assert selected.cost_tier == cheapest_meeting_floor, (
            f"At budget {budget:.1%}, should select cheapest meeting floor "
            f"(cost_tier={cheapest_meeting_floor}), got {selected.model} "
            f"with cost_tier={selected.cost_tier}"
        )

        # Verify the selection is marked as downgraded
        assert selection.downgraded is True

    @given(
        budget=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
        models=available_models_st(),
        role_floor=role_floor_st(),
        tier=st.sampled_from(list(ComplexityTier)),
    )
    @settings(max_examples=200)
    def test_never_downgrades_below_capability_floor(
        self, budget: float, models: list[ModelCapability],
        role_floor: RoleCapabilityFloor, tier: ComplexityTier
    ) -> None:
        """**Validates: Requirements 12.3**

        Regardless of budget position, the selected model never has a
        capability score below the role's minimum floor.
        """
        router = ModelRouter(
            available_models=models,
            role_floors=[role_floor],
        )

        # Ensure at least one model meets the floor
        meeting_floor = [
            m for m in models
            if m.capability_score >= role_floor.min_capability_score
            and (not role_floor.requires_structured_output or m.supports_structured_output)
        ]
        assume(len(meeting_floor) > 0)

        selection = router.select_model(
            task_id="test-floor",
            role=role_floor.role,
            tier=tier,
            budget_position=budget,
        )

        selected = next(m for m in models if m.model == selection.model)
        assert selected.capability_score >= role_floor.min_capability_score, (
            f"Model {selected.model} (capability={selected.capability_score}) "
            f"violates floor ({role_floor.min_capability_score}) at budget {budget:.1%}"
        )
        if role_floor.requires_structured_output:
            assert selected.supports_structured_output, (
                f"Model {selected.model} does not support structured output "
                f"required by role {role_floor.role}"
            )

    @given(
        budget=st.floats(min_value=0.70, max_value=1.0, allow_nan=False, allow_infinity=False),
        models=available_models_st(),
    )
    @settings(max_examples=100)
    def test_comfortable_budget_no_downgrade(
        self, budget: float, models: list[ModelCapability]
    ) -> None:
        """**Validates: Requirements 12.1**

        When budget is at 70%+ remaining, selection is not marked as downgraded.
        The optimal model for the tier is used.
        """
        router = ModelRouter(available_models=models)

        selection = router.select_model(
            task_id="test-no-downgrade",
            role="engineer",
            tier=ComplexityTier.COMPLEX,
            budget_position=budget,
        )

        assert selection.downgraded is False, (
            f"At budget {budget:.1%} (>=70%), selection should not be downgraded"
        )


# ---------------------------------------------------------------------------
# Property 14: Rolling Success Rate Computation
# Validates: Requirements 13.2, 13.3
# ---------------------------------------------------------------------------


class TestRollingSuccessRate:
    """Rolling success rate is count(success)/total over last 200 calls.
    Rate is always in [0, 1]."""

    @given(outcomes=outcome_sequence_st())
    @settings(max_examples=200)
    def test_success_rate_in_valid_range(self, outcomes: list[str]) -> None:
        """**Validates: Requirements 13.2**

        Rolling success rate is always in [0.0, 1.0] regardless of outcome
        sequence.
        """
        router = ModelRouter()

        for outcome in outcomes:
            router.record_outcome(
                model="test-model",
                provider="test-provider",
                role="engineer",
                tier=ComplexityTier.COMPLEX,
                outcome=outcome,
            )

        rate = router.get_rolling_success_rate(
            model="test-model",
            provider="test-provider",
            role="engineer",
            tier=ComplexityTier.COMPLEX,
        )

        assert 0.0 <= rate <= 1.0, (
            f"Success rate {rate} is outside valid range [0, 1]"
        )

    @given(outcomes=outcome_sequence_st())
    @settings(max_examples=200)
    def test_success_rate_computation_correctness(self, outcomes: list[str]) -> None:
        """**Validates: Requirements 13.2**

        Rolling success rate equals count(success) / total over the last
        200 calls (or fewer if less than 200 calls recorded).
        """
        router = ModelRouter()

        for outcome in outcomes:
            router.record_outcome(
                model="test-model",
                provider="test-provider",
                role="engineer",
                tier=ComplexityTier.MODERATE,
                outcome=outcome,
            )

        rate = router.get_rolling_success_rate(
            model="test-model",
            provider="test-provider",
            role="engineer",
            tier=ComplexityTier.MODERATE,
        )

        # Compute expected rate over last 200 (or all if fewer)
        window = outcomes[-ModelRouter.WINDOW_SIZE:]
        expected_rate = sum(1 for o in window if o == "success") / len(window)

        assert abs(rate - expected_rate) < 1e-9, (
            f"Success rate {rate} != expected {expected_rate} "
            f"(window size {len(window)}, successes={sum(1 for o in window if o == 'success')})"
        )

    @given(
        outcomes=st.lists(st.just("success"), min_size=1, max_size=300),
    )
    @settings(max_examples=50)
    def test_all_successes_gives_rate_one(self, outcomes: list[str]) -> None:
        """**Validates: Requirements 13.2**

        When all outcomes are "success", the rolling rate is exactly 1.0.
        """
        router = ModelRouter()

        for outcome in outcomes:
            router.record_outcome(
                model="model-a",
                provider="provider-a",
                role="lead",
                tier=ComplexityTier.ROUTINE,
                outcome=outcome,
            )

        rate = router.get_rolling_success_rate(
            model="model-a",
            provider="provider-a",
            role="lead",
            tier=ComplexityTier.ROUTINE,
        )

        assert rate == 1.0

    @given(
        outcomes=st.lists(st.just("failure"), min_size=1, max_size=300),
    )
    @settings(max_examples=50)
    def test_all_failures_gives_rate_zero(self, outcomes: list[str]) -> None:
        """**Validates: Requirements 13.2**

        When all outcomes are "failure", the rolling rate is exactly 0.0.
        """
        router = ModelRouter()

        for outcome in outcomes:
            router.record_outcome(
                model="model-b",
                provider="provider-b",
                role="reviewer",
                tier=ComplexityTier.CRITICAL,
                outcome=outcome,
            )

        rate = router.get_rolling_success_rate(
            model="model-b",
            provider="provider-b",
            role="reviewer",
            tier=ComplexityTier.CRITICAL,
        )

        assert rate == 0.0

    @given(
        prefix=st.lists(outcome_st(), min_size=201, max_size=400),
        suffix=st.lists(outcome_st(), min_size=1, max_size=200),
    )
    @settings(max_examples=100)
    def test_rolling_window_only_considers_last_200(
        self, prefix: list[str], suffix: list[str]
    ) -> None:
        """**Validates: Requirements 13.2, 13.3**

        The rolling success rate only considers the last 200 calls.
        Older outcomes are dropped from the computation.
        """
        router = ModelRouter()

        all_outcomes = prefix + suffix
        for outcome in all_outcomes:
            router.record_outcome(
                model="model-c",
                provider="provider-c",
                role="qa",
                tier=ComplexityTier.COMPLEX,
                outcome=outcome,
            )

        rate = router.get_rolling_success_rate(
            model="model-c",
            provider="provider-c",
            role="qa",
            tier=ComplexityTier.COMPLEX,
        )

        # Only the last 200 should matter
        window = all_outcomes[-ModelRouter.WINDOW_SIZE:]
        expected_rate = sum(1 for o in window if o == "success") / len(window)

        assert abs(rate - expected_rate) < 1e-9, (
            f"Rate {rate} != expected {expected_rate}. "
            f"Window should be last {ModelRouter.WINDOW_SIZE} of {len(all_outcomes)} total calls."
        )

    def test_no_calls_returns_zero(self) -> None:
        """**Validates: Requirements 13.2**

        When no outcomes have been recorded, success rate returns 0.0.
        """
        router = ModelRouter()

        rate = router.get_rolling_success_rate(
            model="nonexistent",
            provider="nonexistent",
            role="engineer",
            tier=ComplexityTier.ROUTINE,
        )

        assert rate == 0.0
