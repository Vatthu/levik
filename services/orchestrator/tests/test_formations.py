"""Unit tests for the FormationManager.

Validates: Requirements 14.1, 14.2, 14.3, 14.4, 15.1, 15.2, 15.3, 15.4
"""

from __future__ import annotations

import unittest

from vikram_orchestrator.formations import (
    BudgetStrategy,
    Formation,
    FormationManager,
    FormationRecord,
    RecommendationNotification,
    UNDERPERFORMANCE_TASK_COUNT,
    UNDERPERFORMANCE_THRESHOLD,
)
from vikram_orchestrator.model_router import ModelCapability


def _make_formation(name: str = "test-formation", task_type: str = "bugfix") -> Formation:
    """Create a minimal test formation."""
    return Formation(
        name=name,
        task_type=task_type,
        role_model_mappings={
            "planner": ModelCapability(
                model="test-model", provider="test-provider", cost_tier=2, capability_score=2
            ),
        },
        budget_strategy=BudgetStrategy(),
        verification_protocol="standard",
    )


def _make_record(
    formation_name: str = "test-formation",
    task_type: str = "bugfix",
    outcome: str = "success",
    forecast: float = 1.0,
    actual_cost: float = 0.8,
    time_limit: float = 3600.0,
    actual_duration: float = 1800.0,
    verification_first_pass: bool = True,
) -> FormationRecord:
    """Create a test formation record with sensible defaults."""
    return FormationRecord(
        formation_name=formation_name,
        task_type=task_type,
        outcome=outcome,
        forecast=forecast,
        actual_cost=actual_cost,
        time_limit=time_limit,
        actual_duration=actual_duration,
        verification_first_pass=verification_first_pass,
    )


class TestFormationManagerDefaults(unittest.TestCase):
    """Tests for default formation loading."""

    def test_default_formations_loaded(self) -> None:
        """Default formations for 5 task types are loaded on init.

        Validates: Requirements 14.3
        """
        manager = FormationManager()
        formations = manager.list()
        self.assertEqual(len(formations), 5)

    def test_default_formation_names(self) -> None:
        """Default formations have expected names."""
        manager = FormationManager()
        names = {f.name for f in manager.list()}
        expected = {
            "bugfix-standard",
            "feature-standard",
            "refactor-standard",
            "documentation-standard",
            "security-audit-standard",
        }
        self.assertEqual(names, expected)

    def test_default_formations_task_types(self) -> None:
        """Default formations cover all common task types."""
        manager = FormationManager()
        task_types = {f.task_type for f in manager.list()}
        expected = {"bugfix", "feature", "refactor", "documentation", "security-audit"}
        self.assertEqual(task_types, expected)

    def test_default_bugfix_uses_property_based_verification(self) -> None:
        """Bugfix default uses property-based verification."""
        manager = FormationManager()
        f = manager.get("bugfix-standard")
        self.assertIsNotNone(f)
        self.assertEqual(f.verification_protocol, "property-based")

    def test_default_security_uses_formal_verification(self) -> None:
        """Security-audit default uses formal verification."""
        manager = FormationManager()
        f = manager.get("security-audit-standard")
        self.assertIsNotNone(f)
        self.assertEqual(f.verification_protocol, "formal")


class TestFormationCRUD(unittest.TestCase):
    """Tests for Formation create, read, update, delete operations.

    Validates: Requirements 14.4
    """

    def test_create_new_formation(self) -> None:
        """Creating a new formation succeeds and returns it."""
        manager = FormationManager()
        formation = _make_formation(name="my-custom", task_type="feature")
        result = manager.create(formation)
        self.assertEqual(result.name, "my-custom")
        self.assertEqual(result.task_type, "feature")

    def test_create_duplicate_raises(self) -> None:
        """Creating a formation with an existing name raises ValueError."""
        manager = FormationManager()
        formation = _make_formation(name="bugfix-standard")
        with self.assertRaises(ValueError) as ctx:
            manager.create(formation)
        self.assertIn("already exists", str(ctx.exception))

    def test_get_existing_formation(self) -> None:
        """Getting an existing formation returns it."""
        manager = FormationManager()
        result = manager.get("bugfix-standard")
        self.assertIsNotNone(result)
        self.assertEqual(result.name, "bugfix-standard")

    def test_get_nonexistent_returns_none(self) -> None:
        """Getting a nonexistent formation returns None."""
        manager = FormationManager()
        result = manager.get("does-not-exist")
        self.assertIsNone(result)

    def test_list_returns_all(self) -> None:
        """List returns all formations including custom ones."""
        manager = FormationManager()
        manager.create(_make_formation(name="custom-1", task_type="feature"))
        formations = manager.list()
        # 5 defaults + 1 custom
        self.assertEqual(len(formations), 6)

    def test_update_existing_formation(self) -> None:
        """Updating an existing formation replaces its config."""
        manager = FormationManager()
        updated = Formation(
            name="bugfix-standard",
            task_type="bugfix",
            role_model_mappings={},
            budget_strategy=BudgetStrategy(planning=0.30, implementation=0.40, verification=0.20, review=0.10),
            verification_protocol="formal",
        )
        result = manager.update("bugfix-standard", updated)
        self.assertEqual(result.verification_protocol, "formal")
        self.assertEqual(result.budget_strategy.planning, 0.30)

    def test_update_with_name_change(self) -> None:
        """Updating with a different name removes old key and adds new."""
        manager = FormationManager()
        updated = Formation(
            name="bugfix-renamed",
            task_type="bugfix",
            role_model_mappings={},
            budget_strategy=BudgetStrategy(),
            verification_protocol="standard",
        )
        result = manager.update("bugfix-standard", updated)
        self.assertEqual(result.name, "bugfix-renamed")
        self.assertIsNone(manager.get("bugfix-standard"))
        self.assertIsNotNone(manager.get("bugfix-renamed"))

    def test_update_nonexistent_raises(self) -> None:
        """Updating a nonexistent formation raises ValueError."""
        manager = FormationManager()
        formation = _make_formation(name="ghost")
        with self.assertRaises(ValueError) as ctx:
            manager.update("ghost", formation)
        self.assertIn("not found", str(ctx.exception))

    def test_delete_existing_returns_true(self) -> None:
        """Deleting an existing formation returns True and removes it."""
        manager = FormationManager()
        result = manager.delete("bugfix-standard")
        self.assertTrue(result)
        self.assertIsNone(manager.get("bugfix-standard"))

    def test_delete_nonexistent_returns_false(self) -> None:
        """Deleting a nonexistent formation returns False."""
        manager = FormationManager()
        result = manager.delete("does-not-exist")
        self.assertFalse(result)


class TestFormationEffectiveness(unittest.TestCase):
    """Tests for the effectiveness scoring algorithm.

    Validates: Requirements 15.2
    """

    def test_insufficient_data_returns_neutral(self) -> None:
        """Fewer than 5 records returns 0.5 (neutral score)."""
        manager = FormationManager()
        # Add only 4 records
        for _ in range(4):
            manager.record_outcome(_make_record())
        score = manager.get_effectiveness("test-formation", "bugfix")
        self.assertEqual(score, 0.5)

    def test_zero_records_returns_neutral(self) -> None:
        """No records returns 0.5."""
        manager = FormationManager()
        score = manager.get_effectiveness("test-formation", "bugfix")
        self.assertEqual(score, 0.5)

    def test_perfect_records_returns_high_score(self) -> None:
        """All-success, under-budget, fast, first-pass -> high effectiveness."""
        manager = FormationManager()
        for _ in range(10):
            manager.record_outcome(_make_record(
                outcome="success",
                forecast=2.0,
                actual_cost=1.0,  # cost_efficiency = 2.0, capped at 1.5
                time_limit=3600.0,
                actual_duration=1000.0,  # time_efficiency = 3.6, capped at 2.0
                verification_first_pass=True,
            ))
        score = manager.get_effectiveness("test-formation", "bugfix")
        # success_rate = 1.0
        # cost_efficiency capped = min(2.0, 1.5)/1.5 = 1.0
        # time_efficiency capped = min(3.6, 2.0)/2.0 = 1.0
        # first_pass_rate = 1.0
        # score = 0.40*1.0 + 0.20*1.0 + 0.15*1.0 + 0.25*1.0 = 1.0
        self.assertAlmostEqual(score, 1.0, places=4)

    def test_all_failures_returns_low_score(self) -> None:
        """All-failure records produce a low score."""
        manager = FormationManager()
        for _ in range(10):
            manager.record_outcome(_make_record(
                outcome="failure",
                forecast=1.0,
                actual_cost=3.0,  # cost_efficiency = 0.33
                time_limit=3600.0,
                actual_duration=7200.0,  # time_efficiency = 0.5
                verification_first_pass=False,
            ))
        score = manager.get_effectiveness("test-formation", "bugfix")
        # success_rate = 0.0
        # cost_efficiency = 1/3 -> min(0.33, 1.5)/1.5 = 0.222
        # time_efficiency = 0.5 -> min(0.5, 2.0)/2.0 = 0.25
        # first_pass_rate = 0.0
        # score = 0.40*0.0 + 0.20*0.222 + 0.15*0.25 + 0.25*0.0 = 0.0444 + 0.0375 = 0.082
        self.assertLess(score, 0.15)

    def test_mixed_records(self) -> None:
        """Mix of success and failure produces intermediate score."""
        manager = FormationManager()
        # 6 successes
        for _ in range(6):
            manager.record_outcome(_make_record(
                outcome="success",
                forecast=1.0,
                actual_cost=1.0,
                time_limit=3600.0,
                actual_duration=3600.0,
                verification_first_pass=True,
            ))
        # 4 failures
        for _ in range(4):
            manager.record_outcome(_make_record(
                outcome="failure",
                forecast=1.0,
                actual_cost=1.5,
                time_limit=3600.0,
                actual_duration=4000.0,
                verification_first_pass=False,
            ))
        score = manager.get_effectiveness("test-formation", "bugfix")
        # Should be between 0 and 1
        self.assertGreater(score, 0.2)
        self.assertLess(score, 0.9)

    def test_only_counts_matching_formation_and_type(self) -> None:
        """Effectiveness only considers records matching formation+task_type."""
        manager = FormationManager()
        # Records for different formation
        for _ in range(10):
            manager.record_outcome(_make_record(formation_name="other-formation"))
        # Records for different task type
        for _ in range(10):
            manager.record_outcome(_make_record(task_type="feature"))

        score = manager.get_effectiveness("test-formation", "bugfix")
        # No matching records -> neutral
        self.assertEqual(score, 0.5)

    def test_uses_last_50_records(self) -> None:
        """Effectiveness uses at most the last 50 records."""
        manager = FormationManager()
        # 60 failure records
        for _ in range(60):
            manager.record_outcome(_make_record(
                outcome="failure",
                forecast=1.0,
                actual_cost=2.0,
                time_limit=3600.0,
                actual_duration=5000.0,
                verification_first_pass=False,
            ))
        # 10 success records at the end (these are in the last 50)
        for _ in range(10):
            manager.record_outcome(_make_record(
                outcome="success",
                forecast=1.0,
                actual_cost=0.8,
                time_limit=3600.0,
                actual_duration=1800.0,
                verification_first_pass=True,
            ))

        score = manager.get_effectiveness("test-formation", "bugfix")
        # Last 50 = 40 failures + 10 successes
        # success_rate = 10/50 = 0.2
        self.assertGreater(score, 0.0)
        self.assertLess(score, 0.5)


class TestFormationRecommendation(unittest.TestCase):
    """Tests for automatic formation recommendation.

    Validates: Requirements 15.1
    """

    def test_recommend_returns_best_formation(self) -> None:
        """Recommend returns the formation with highest effectiveness."""
        manager = FormationManager()
        # Create two custom formations for the same task type
        manager.create(_make_formation(name="bugfix-good", task_type="bugfix"))
        manager.create(_make_formation(name="bugfix-bad", task_type="bugfix"))

        # Give bugfix-good great records
        for _ in range(10):
            manager.record_outcome(_make_record(
                formation_name="bugfix-good",
                task_type="bugfix",
                outcome="success",
                forecast=1.0,
                actual_cost=0.5,
                time_limit=3600.0,
                actual_duration=1000.0,
                verification_first_pass=True,
            ))
        # Give bugfix-bad poor records
        for _ in range(10):
            manager.record_outcome(_make_record(
                formation_name="bugfix-bad",
                task_type="bugfix",
                outcome="failure",
                forecast=1.0,
                actual_cost=3.0,
                time_limit=3600.0,
                actual_duration=7200.0,
                verification_first_pass=False,
            ))

        result = manager.recommend("bugfix")
        self.assertIsNotNone(result)
        self.assertEqual(result.name, "bugfix-good")

    def test_recommend_no_matching_type_returns_none(self) -> None:
        """Recommend returns None when no formations match the task type."""
        manager = FormationManager()
        result = manager.recommend("unknown-type")
        self.assertIsNone(result)

    def test_recommend_with_no_records_returns_any_matching(self) -> None:
        """With no records, all formations have neutral 0.5 score - returns first."""
        manager = FormationManager()
        result = manager.recommend("bugfix")
        # Should return the bugfix-standard default
        self.assertIsNotNone(result)
        self.assertEqual(result.task_type, "bugfix")


class TestFormationUnderperformance(unittest.TestCase):
    """Tests for underperformance detection.

    Validates: Requirements 15.3
    """

    def test_not_underperforming_with_insufficient_data(self) -> None:
        """With fewer than 20 records, is_underperforming returns False."""
        manager = FormationManager()
        for _ in range(10):
            manager.record_outcome(_make_record(outcome="failure"))
        result = manager.is_underperforming("test-formation", "bugfix")
        self.assertFalse(result)

    def test_underperforming_below_threshold(self) -> None:
        """With 20+ records and <60% success, is_underperforming returns True."""
        manager = FormationManager()
        # 8 successes, 12 failures -> 40% success rate
        for _ in range(8):
            manager.record_outcome(_make_record(outcome="success"))
        for _ in range(12):
            manager.record_outcome(_make_record(outcome="failure"))
        result = manager.is_underperforming("test-formation", "bugfix")
        self.assertTrue(result)

    def test_not_underperforming_above_threshold(self) -> None:
        """With 20+ records and >=60% success, is_underperforming returns False."""
        manager = FormationManager()
        # 15 successes, 5 failures -> 75% success rate
        for _ in range(15):
            manager.record_outcome(_make_record(outcome="success"))
        for _ in range(5):
            manager.record_outcome(_make_record(outcome="failure"))
        result = manager.is_underperforming("test-formation", "bugfix")
        self.assertFalse(result)

    def test_exactly_at_threshold_not_underperforming(self) -> None:
        """Exactly 60% success rate is NOT underperforming (< not <=)."""
        manager = FormationManager()
        # 12 successes, 8 failures -> 60% success rate
        for _ in range(12):
            manager.record_outcome(_make_record(outcome="success"))
        for _ in range(8):
            manager.record_outcome(_make_record(outcome="failure"))
        result = manager.is_underperforming("test-formation", "bugfix")
        self.assertFalse(result)


class TestFormationEffectivenessComparison(unittest.TestCase):
    """Tests for effectiveness comparison endpoint.

    Validates: Requirements 15.4
    """

    def test_comparison_returns_all_formations(self) -> None:
        """get_effectiveness_comparison returns entries for all formations."""
        manager = FormationManager()
        comparison = manager.get_effectiveness_comparison()
        self.assertEqual(len(comparison), 5)  # 5 defaults

    def test_comparison_format(self) -> None:
        """Each entry maps formation_name -> {task_type: score}."""
        manager = FormationManager()
        comparison = manager.get_effectiveness_comparison()
        # All defaults have neutral 0.5 (no records)
        for name, type_scores in comparison.items():
            self.assertIsInstance(type_scores, dict)
            for task_type, score in type_scores.items():
                self.assertIsInstance(score, float)
                self.assertEqual(score, 0.5)


class TestFormationRecordOutcome(unittest.TestCase):
    """Tests for recording outcomes."""

    def test_record_outcome_stores_record(self) -> None:
        """record_outcome adds a record to internal storage."""
        manager = FormationManager()
        record = _make_record()
        manager.record_outcome(record)
        # Verify by checking effectiveness changes from neutral after 5+ records
        for _ in range(5):
            manager.record_outcome(record)
        score = manager.get_effectiveness("test-formation", "bugfix")
        # With 6 perfect records, should be > 0.5
        self.assertGreater(score, 0.5)

    def test_record_outcome_with_zero_actual_cost(self) -> None:
        """Records with zero actual_cost are handled gracefully."""
        manager = FormationManager()
        for _ in range(6):
            manager.record_outcome(_make_record(
                actual_cost=0.0,
                actual_duration=1800.0,
            ))
        # Should not raise; zero-cost records are excluded from cost_efficiency
        score = manager.get_effectiveness("test-formation", "bugfix")
        self.assertIsInstance(score, float)

    def test_record_outcome_with_zero_duration(self) -> None:
        """Records with zero actual_duration are handled gracefully."""
        manager = FormationManager()
        for _ in range(6):
            manager.record_outcome(_make_record(
                actual_cost=1.0,
                actual_duration=0.0,
            ))
        # Should not raise; zero-duration records are excluded from time_efficiency
        score = manager.get_effectiveness("test-formation", "bugfix")
        self.assertIsInstance(score, float)


class TestFormationTelemetryNotification(unittest.TestCase):
    """Tests for telemetry event emission on underperformance detection.

    Validates: Requirements 40.3
    """

    def test_telemetry_event_emitted_on_underperformance(self) -> None:
        """When underperformance is detected, a telemetry event is emitted."""
        emitted_events: list[dict] = []

        class MockTelemetryClient:
            def emit_event(self, event_type: str, task_id: str, attributes: dict | None = None) -> dict:
                emitted_events.append({
                    "event_type": event_type,
                    "task_id": task_id,
                    "attributes": attributes or {},
                })
                return {"status": "ok"}

        mock_client = MockTelemetryClient()
        manager = FormationManager(telemetry_client=mock_client)  # type: ignore[arg-type]

        # Record enough failures to trigger underperformance (20 tasks, <60% success)
        for _ in range(8):
            manager.record_outcome(_make_record(outcome="success"))
        for _ in range(12):
            manager.record_outcome(_make_record(outcome="failure"))

        # The last failure record (the 20th overall) should trigger notification
        self.assertTrue(len(emitted_events) > 0)

        last_event = emitted_events[-1]
        self.assertEqual(last_event["event_type"], "formation_underperformance")
        self.assertEqual(last_event["task_id"], "system")
        self.assertEqual(last_event["attributes"]["formation_name"], "test-formation")
        self.assertEqual(last_event["attributes"]["task_type"], "bugfix")
        self.assertLess(last_event["attributes"]["success_rate"], UNDERPERFORMANCE_THRESHOLD)
        self.assertEqual(last_event["attributes"]["task_count"], UNDERPERFORMANCE_TASK_COUNT)

    def test_no_telemetry_without_client(self) -> None:
        """Without a telemetry client, underperformance notification still works via callback."""
        notifications: list = []

        def on_rec(notification):
            notifications.append(notification)

        manager = FormationManager(on_recommendation=on_rec)

        # Trigger underperformance
        for _ in range(8):
            manager.record_outcome(_make_record(outcome="success"))
        for _ in range(12):
            manager.record_outcome(_make_record(outcome="failure"))

        # Callback should still fire
        self.assertTrue(len(notifications) > 0)
        self.assertEqual(notifications[-1].formation_name, "test-formation")

    def test_telemetry_client_error_does_not_crash(self) -> None:
        """If the telemetry client raises, the error is handled gracefully."""

        class FailingTelemetryClient:
            def emit_event(self, event_type: str, task_id: str, attributes: dict | None = None) -> dict:
                raise ConnectionError("telemetry unavailable")

        failing_client = FailingTelemetryClient()
        manager = FormationManager(telemetry_client=failing_client)  # type: ignore[arg-type]

        # Should not raise despite telemetry failure
        for _ in range(8):
            manager.record_outcome(_make_record(outcome="success"))
        for _ in range(12):
            manager.record_outcome(_make_record(outcome="failure"))

    def test_set_telemetry_client_at_runtime(self) -> None:
        """Telemetry client can be set after construction."""
        emitted_events: list[dict] = []

        class MockTelemetryClient:
            def emit_event(self, event_type: str, task_id: str, attributes: dict | None = None) -> dict:
                emitted_events.append({
                    "event_type": event_type,
                    "task_id": task_id,
                    "attributes": attributes or {},
                })
                return {"status": "ok"}

        manager = FormationManager()

        # Record 19 failures (not yet at threshold)
        for _ in range(7):
            manager.record_outcome(_make_record(outcome="success"))
        for _ in range(12):
            manager.record_outcome(_make_record(outcome="failure"))

        # No events yet — no client was set
        self.assertEqual(len(emitted_events), 0)

        # Set the client now
        mock_client = MockTelemetryClient()
        manager.set_telemetry_client(mock_client)  # type: ignore[arg-type]

        # Add one more failure to trigger underperformance check with 20 records
        manager.record_outcome(_make_record(outcome="failure"))

        # Should have emitted since we now have 20 records with <60% success
        self.assertTrue(len(emitted_events) > 0)
        self.assertEqual(emitted_events[-1]["event_type"], "formation_underperformance")


if __name__ == "__main__":
    unittest.main()
