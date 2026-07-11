"""System-level tests for the Conflict Detector prediction engine.

These tests validate the full ConflictDetector end-to-end:
- Threshold filtering: overlaps below threshold are excluded
- Alert emission: alerts are emitted via telemetry when threshold exceeded
- Configurable threshold: changing the threshold changes which overlaps are reported
- predict_conflicts_unfiltered: returns all overlaps regardless of threshold

Validates: Requirements 35.1, 35.2, 35.3, 35.4
"""

from __future__ import annotations

from typing import Any

import pytest

from vikram_orchestrator.conflict_detector import (
    AlertEmitter,
    ConflictDetector,
    DEFAULT_CONFLICT_THRESHOLD,
    FileOverlap,
    conflict_probability,
)


# ---------------------------------------------------------------------------
# Test Helpers
# ---------------------------------------------------------------------------


class FakeAlertEmitter:
    """Records all emitted events for assertion."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit_event(
        self,
        event_type: str,
        task_id: str,
        attributes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = {
            "event_type": event_type,
            "task_id": task_id,
            "attributes": attributes or {},
        }
        self.events.append(record)
        return {"status": "ok"}


class FailingAlertEmitter:
    """Simulates a telemetry client that raises on emit."""

    def emit_event(
        self,
        event_type: str,
        task_id: str,
        attributes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise ConnectionError("Telemetry service unavailable")


# ---------------------------------------------------------------------------
# Tests: Default Threshold Behavior
# ---------------------------------------------------------------------------


class TestDefaultThreshold:
    """Verify the default conflict threshold is 70%."""

    def test_default_threshold_value(self) -> None:
        """Default threshold matches design spec (70%)."""
        assert DEFAULT_CONFLICT_THRESHOLD == 0.70

        detector = ConflictDetector()
        assert detector.conflict_threshold == 0.70

    def test_custom_threshold_applied(self) -> None:
        """Custom threshold is stored and used."""
        detector = ConflictDetector(conflict_threshold=0.50)
        assert detector.conflict_threshold == 0.50

    def test_invalid_threshold_raises(self) -> None:
        """Thresholds outside [0, 1] raise ValueError."""
        with pytest.raises(ValueError, match="conflict_threshold must be in"):
            ConflictDetector(conflict_threshold=1.5)

        with pytest.raises(ValueError, match="conflict_threshold must be in"):
            ConflictDetector(conflict_threshold=-0.1)

    def test_threshold_setter(self) -> None:
        """Threshold can be updated at runtime."""
        detector = ConflictDetector()
        detector.conflict_threshold = 0.50
        assert detector.conflict_threshold == 0.50

    def test_threshold_setter_invalid(self) -> None:
        """Setting invalid threshold at runtime raises."""
        detector = ConflictDetector()
        with pytest.raises(ValueError):
            detector.conflict_threshold = 2.0


# ---------------------------------------------------------------------------
# Tests: Threshold Filtering in predict_conflicts
# ---------------------------------------------------------------------------


class TestThresholdFiltering:
    """Verify predict_conflicts filters overlaps by threshold."""

    def test_base_probability_below_default_threshold(self) -> None:
        """With default params (base only), probability ~0.255 < 0.70 threshold.

        This means basic same-file overlaps should NOT be returned with
        the default threshold.
        """
        detector = ConflictDetector()  # threshold=0.70

        # Both tasks touch the same file
        results = detector.predict_conflicts(
            new_task_id="task-new",
            new_task_targets=["src/app.py"],
            active_tasks={"task-active": ["src/app.py"]},
        )

        # Base probability = 0.3 * (0.7 + 0.3*0.5) = 0.255 < 0.70
        assert results == [], (
            f"Expected no results above 0.70 threshold, got {results}"
        )

    def test_low_threshold_returns_all_overlaps(self) -> None:
        """With threshold=0.0, all overlaps are returned."""
        detector = ConflictDetector(conflict_threshold=0.0)

        results = detector.predict_conflicts(
            new_task_id="task-new",
            new_task_targets=["src/app.py", "src/utils.py"],
            active_tasks={
                "task-1": ["src/app.py"],
                "task-2": ["src/utils.py", "src/other.py"],
            },
        )

        # Should find 2 overlaps: app.py (new vs task-1) and utils.py (new vs task-2)
        assert len(results) == 2
        paths = {r.path for r in results}
        assert paths == {"src/app.py", "src/utils.py"}

    def test_high_threshold_filters_everything(self) -> None:
        """With threshold=1.0, only probability=1.0 passes."""
        detector = ConflictDetector(conflict_threshold=1.0)

        results = detector.predict_conflicts(
            new_task_id="task-new",
            new_task_targets=["src/app.py"],
            active_tasks={"task-active": ["src/app.py"]},
        )

        # Base probability 0.255 < 1.0, so nothing passes
        assert results == []

    def test_threshold_exactly_at_probability(self) -> None:
        """Threshold equals the computed probability — overlap is included."""
        # Base probability with historical_rate=0.5 → 0.3 * 0.85 = 0.255
        threshold = 0.255
        detector = ConflictDetector(conflict_threshold=threshold)

        results = detector.predict_conflicts(
            new_task_id="task-new",
            new_task_targets=["src/app.py"],
            active_tasks={"task-active": ["src/app.py"]},
        )

        # 0.255 >= 0.255, so it should be included
        assert len(results) == 1
        assert results[0].path == "src/app.py"

    def test_multiple_active_tasks_with_overlaps(self) -> None:
        """Multiple active tasks overlapping are all checked."""
        detector = ConflictDetector(conflict_threshold=0.0)

        results = detector.predict_conflicts(
            new_task_id="task-new",
            new_task_targets=["a.py", "b.py", "c.py"],
            active_tasks={
                "task-1": ["a.py", "d.py"],
                "task-2": ["b.py", "c.py"],
                "task-3": ["e.py"],
            },
        )

        # task-1 overlaps on a.py, task-2 overlaps on b.py and c.py
        assert len(results) == 3
        paths_and_tasks = {(r.path, r.task_b_id) for r in results}
        assert ("a.py", "task-1") in paths_and_tasks
        assert ("b.py", "task-2") in paths_and_tasks
        assert ("c.py", "task-2") in paths_and_tasks

    def test_no_overlaps_returns_empty(self) -> None:
        """No file overlaps means no conflicts regardless of threshold."""
        detector = ConflictDetector(conflict_threshold=0.0)

        results = detector.predict_conflicts(
            new_task_id="task-new",
            new_task_targets=["src/app.py"],
            active_tasks={"task-1": ["src/other.py"]},
        )

        assert results == []


# ---------------------------------------------------------------------------
# Tests: predict_conflicts_unfiltered
# ---------------------------------------------------------------------------


class TestPredictConflictsUnfiltered:
    """Verify that unfiltered prediction returns all overlaps."""

    def test_unfiltered_returns_all_regardless_of_threshold(self) -> None:
        """predict_conflicts_unfiltered ignores the threshold."""
        detector = ConflictDetector(conflict_threshold=1.0)  # Very high threshold

        results = detector.predict_conflicts_unfiltered(
            new_task_id="task-new",
            new_task_targets=["a.py", "b.py"],
            active_tasks={"task-active": ["a.py", "b.py"]},
        )

        # Both overlaps returned even though threshold is 1.0
        assert len(results) == 2


# ---------------------------------------------------------------------------
# Tests: Alert Emission
# ---------------------------------------------------------------------------


class TestAlertEmission:
    """Verify conflict prediction alerts are emitted correctly."""

    def test_alert_emitted_when_threshold_exceeded(self) -> None:
        """Alerts are emitted for each overlap above threshold."""
        emitter = FakeAlertEmitter()
        # Use low threshold so base probability (0.255) triggers alerts
        detector = ConflictDetector(
            conflict_threshold=0.20,
            alert_emitter=emitter,
        )

        results = detector.predict_conflicts(
            new_task_id="task-new",
            new_task_targets=["src/app.py", "src/db.py"],
            active_tasks={"task-active": ["src/app.py", "src/db.py"]},
        )

        assert len(results) == 2
        assert len(emitter.events) == 2

        # Verify event structure
        for event in emitter.events:
            assert event["event_type"] == "conflict_prediction_alert"
            assert event["task_id"] == "task-new"
            attrs = event["attributes"]
            assert attrs["alert_type"] == "conflict_prediction"
            assert attrs["conflicting_task_a"] == "task-new"
            assert attrs["conflicting_task_b"] == "task-active"
            assert attrs["conflict_probability"] >= 0.20
            assert attrs["threshold"] == 0.20
            assert attrs["file_path"] in ("src/app.py", "src/db.py")
            assert attrs["overlap_type"] == "same_file"

    def test_no_alert_when_below_threshold(self) -> None:
        """No alerts emitted when all overlaps are below threshold."""
        emitter = FakeAlertEmitter()
        detector = ConflictDetector(
            conflict_threshold=0.70,  # Default — base prob 0.255 won't trigger
            alert_emitter=emitter,
        )

        results = detector.predict_conflicts(
            new_task_id="task-new",
            new_task_targets=["src/app.py"],
            active_tasks={"task-active": ["src/app.py"]},
        )

        assert results == []
        assert emitter.events == []

    def test_no_alert_when_no_emitter_configured(self) -> None:
        """Without an alert_emitter, no errors even when threshold exceeded."""
        detector = ConflictDetector(
            conflict_threshold=0.0,
            alert_emitter=None,
        )

        # Should work fine, just no alerts emitted
        results = detector.predict_conflicts(
            new_task_id="task-new",
            new_task_targets=["src/app.py"],
            active_tasks={"task-active": ["src/app.py"]},
        )

        assert len(results) == 1

    def test_alert_emission_failure_does_not_crash(self) -> None:
        """If alert emission fails, predict_conflicts still returns results."""
        emitter = FailingAlertEmitter()
        detector = ConflictDetector(
            conflict_threshold=0.0,
            alert_emitter=emitter,
        )

        # Should not raise even though emitter raises ConnectionError
        results = detector.predict_conflicts(
            new_task_id="task-new",
            new_task_targets=["src/app.py"],
            active_tasks={"task-active": ["src/app.py"]},
        )

        # Results are still returned despite emission failure
        assert len(results) == 1


# ---------------------------------------------------------------------------
# Tests: End-to-End System Scenario
# ---------------------------------------------------------------------------


class TestEndToEndConflictPrediction:
    """Full system-level scenario with multiple tasks, threshold, and alerts."""

    def test_full_scenario_multiple_overlapping_tasks(self) -> None:
        """Simulate a realistic scenario with multiple concurrent tasks.

        Setup:
        - task-new targets: auth.py, db.py, api.py, utils.py
        - task-active-1 targets: auth.py, config.py
        - task-active-2 targets: db.py, api.py, models.py
        - task-active-3 targets: tests.py, README.md

        Expected overlaps:
        - auth.py (new vs active-1)
        - db.py (new vs active-2)
        - api.py (new vs active-2)
        Total: 3 overlaps, all at base probability ~0.255
        """
        emitter = FakeAlertEmitter()

        # Low threshold to capture all overlaps
        detector = ConflictDetector(
            conflict_threshold=0.20,
            alert_emitter=emitter,
        )

        results = detector.predict_conflicts(
            new_task_id="task-new",
            new_task_targets=["auth.py", "db.py", "api.py", "utils.py"],
            active_tasks={
                "task-active-1": ["auth.py", "config.py"],
                "task-active-2": ["db.py", "api.py", "models.py"],
                "task-active-3": ["tests.py", "README.md"],
            },
        )

        # 3 overlaps: auth.py, db.py, api.py
        assert len(results) == 3

        # Verify correct task pairings
        overlap_map = {r.path: r for r in results}
        assert overlap_map["auth.py"].task_b_id == "task-active-1"
        assert overlap_map["db.py"].task_b_id == "task-active-2"
        assert overlap_map["api.py"].task_b_id == "task-active-2"

        # All overlaps have the same base probability
        for r in results:
            expected_prob = 0.3 * (0.7 + 0.3 * 0.5)  # 0.255
            assert abs(r.conflict_probability - expected_prob) < 1e-9

        # 3 alerts emitted
        assert len(emitter.events) == 3

    def test_threshold_change_affects_filtering(self) -> None:
        """Changing threshold at runtime changes which overlaps are reported."""
        emitter = FakeAlertEmitter()
        detector = ConflictDetector(
            conflict_threshold=0.20,
            alert_emitter=emitter,
        )

        active_tasks = {"task-active": ["src/app.py"]}
        targets = ["src/app.py"]

        # At threshold 0.20, the overlap (prob ~0.255) is reported
        results = detector.predict_conflicts("task-1", targets, active_tasks)
        assert len(results) == 1
        assert len(emitter.events) == 1

        # Raise threshold above the probability
        emitter.events.clear()
        detector.conflict_threshold = 0.50

        results = detector.predict_conflicts("task-2", targets, active_tasks)
        assert len(results) == 0
        assert len(emitter.events) == 0

    def test_probability_scoring_components(self) -> None:
        """Verify the probability scoring formula components work correctly.

        This validates the algorithm from design.md:
        - base = 0.3
        - +0.4 for same functions
        - +0.2 for proximity
        - * (0.7 + 0.3 * historical_rate) for historical correction
        """
        # Just base
        p = conflict_probability(
            "test.py", {}, {},
            same_functions=False,
            changes_within_proximity=False,
            historical_rate=0.0,
        )
        assert abs(p - 0.3 * 0.7) < 1e-9  # 0.21

        # Base + same functions
        p = conflict_probability(
            "test.py", {}, {},
            same_functions=True,
            changes_within_proximity=False,
            historical_rate=0.0,
        )
        assert abs(p - 0.7 * 0.7) < 1e-9  # 0.49

        # Base + proximity
        p = conflict_probability(
            "test.py", {}, {},
            same_functions=False,
            changes_within_proximity=True,
            historical_rate=0.0,
        )
        assert abs(p - 0.5 * 0.7) < 1e-9  # 0.35

        # All factors with max historical rate
        p = conflict_probability(
            "test.py", {}, {},
            same_functions=True,
            changes_within_proximity=True,
            historical_rate=1.0,
        )
        assert abs(p - min(0.9 * 1.0, 1.0)) < 1e-9  # 0.9

    def test_telemetry_client_compatibility(self) -> None:
        """Verify that TelemetryClient's emit_event signature is compatible.

        The AlertEmitter protocol matches TelemetryClient.emit_event().
        We verify this with our FakeAlertEmitter which implements the same
        interface.
        """
        emitter = FakeAlertEmitter()
        detector = ConflictDetector(
            conflict_threshold=0.0,
            alert_emitter=emitter,
        )

        detector.predict_conflicts(
            new_task_id="compatibility-test",
            new_task_targets=["file.py"],
            active_tasks={"other": ["file.py"]},
        )

        assert len(emitter.events) == 1
        event = emitter.events[0]
        assert event["event_type"] == "conflict_prediction_alert"
        assert event["task_id"] == "compatibility-test"
        assert "conflict_probability" in event["attributes"]
        assert "threshold" in event["attributes"]
