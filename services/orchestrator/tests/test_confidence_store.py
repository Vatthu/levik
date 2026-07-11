"""Unit tests for the ConfidenceStore SQLite persistence and promotion logic.

Validates: Requirements 32.1, 32.2, 32.3, 32.4, 32.5, 33.1
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from vikram_orchestrator.approval_matrix import (
    ApprovalPolicyDecision,
    ConfidenceStore,
    DEFAULT_DECREMENT,
    DEFAULT_INCREMENT,
    PROMOTION_THRESHOLDS,
    TIER_CEILINGS,
)


class TestConfidenceStorePersistence(unittest.TestCase):
    """Tests for SQLite-backed confidence score persistence.

    Validates: Requirements 32.1, 32.2, 32.4, 32.5
    """

    def setUp(self) -> None:
        self.store = ConfidenceStore(db_path=":memory:")

    def tearDown(self) -> None:
        self.store.close()

    def test_get_confidence_creates_default(self) -> None:
        """Getting a non-existent score creates one with score=0."""
        cs = self.store.get_confidence("routine", "my-repo")
        self.assertEqual(cs.score, 0.0)
        self.assertEqual(cs.complexity_tier, "routine")
        self.assertEqual(cs.repository, "my-repo")
        self.assertEqual(cs.ceiling, TIER_CEILINGS["routine"])

    def test_increment_confidence(self) -> None:
        """Incrementing adds the default amount (+1)."""
        self.store.increment_confidence("routine", "repo-a")
        cs = self.store.get_confidence("routine", "repo-a")
        self.assertAlmostEqual(cs.score, 1.0)

    def test_decrement_confidence(self) -> None:
        """Decrementing subtracts the default amount (-3), floored at 0."""
        # Start by incrementing to 5
        for _ in range(5):
            self.store.increment_confidence("routine", "repo-a")
        self.store.decrement_confidence("routine", "repo-a")
        cs = self.store.get_confidence("routine", "repo-a")
        self.assertAlmostEqual(cs.score, 2.0)  # 5 - 3 = 2

    def test_decrement_floors_at_zero(self) -> None:
        """Decrementing from 0 stays at 0."""
        self.store.decrement_confidence("routine", "repo-a")
        cs = self.store.get_confidence("routine", "repo-a")
        self.assertEqual(cs.score, 0.0)

    def test_increment_respects_ceiling(self) -> None:
        """Score cannot exceed the tier ceiling."""
        ceiling = TIER_CEILINGS["routine"]  # 30
        for _ in range(50):
            self.store.increment_confidence("routine", "repo-a")
        cs = self.store.get_confidence("routine", "repo-a")
        self.assertAlmostEqual(cs.score, ceiling)

    def test_different_tiers_have_different_ceilings(self) -> None:
        """Each tier has its own ceiling."""
        for tier, expected_ceiling in TIER_CEILINGS.items():
            cs = self.store.get_confidence(tier, "repo-a")
            self.assertEqual(cs.ceiling, expected_ceiling)

    def test_different_repos_are_independent(self) -> None:
        """Scores for different repos are tracked independently."""
        self.store.increment_confidence("routine", "repo-a")
        self.store.increment_confidence("routine", "repo-a")
        self.store.increment_confidence("routine", "repo-b")

        cs_a = self.store.get_confidence("routine", "repo-a")
        cs_b = self.store.get_confidence("routine", "repo-b")

        self.assertAlmostEqual(cs_a.score, 2.0)
        self.assertAlmostEqual(cs_b.score, 1.0)

    def test_persistence_survives_reload(self) -> None:
        """Scores are persisted to disk and survive a new ConfidenceStore instance."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            # Write scores
            store1 = ConfidenceStore(db_path=db_path)
            for _ in range(5):
                store1.increment_confidence("moderate", "persistent-repo")
            store1.close()

            # Read back from a fresh instance
            store2 = ConfidenceStore(db_path=db_path)
            cs = store2.get_confidence("moderate", "persistent-repo")
            self.assertAlmostEqual(cs.score, 5.0)
            store2.close()
        finally:
            Path(db_path).unlink(missing_ok=True)

    def test_custom_increment_amount(self) -> None:
        """Custom increment amount is applied."""
        self.store.increment_confidence("routine", "repo-a", amount=5.0)
        cs = self.store.get_confidence("routine", "repo-a")
        self.assertAlmostEqual(cs.score, 5.0)

    def test_custom_decrement_amount(self) -> None:
        """Custom decrement amount is applied."""
        for _ in range(10):
            self.store.increment_confidence("routine", "repo-a")
        self.store.decrement_confidence("routine", "repo-a", amount=7.0)
        cs = self.store.get_confidence("routine", "repo-a")
        self.assertAlmostEqual(cs.score, 3.0)  # 10 - 7 = 3


class TestConfidenceStorePromotion(unittest.TestCase):
    """Tests for promotion threshold logic.

    Validates: Requirements 32.3, 32.5
    """

    def setUp(self) -> None:
        self.store = ConfidenceStore(db_path=":memory:")

    def tearDown(self) -> None:
        self.store.close()

    def test_promotion_not_reached_initially(self) -> None:
        """A new score (0) does not meet promotion threshold."""
        self.assertFalse(self.store.check_promotion("routine", "repo"))

    def test_routine_promotion_at_threshold(self) -> None:
        """Routine tier promotes at score >= 10."""
        for _ in range(10):
            self.store.increment_confidence("routine", "repo")
        self.assertTrue(self.store.check_promotion("routine", "repo"))

    def test_moderate_promotion_at_threshold(self) -> None:
        """Moderate tier promotes at score >= 20."""
        for _ in range(20):
            self.store.increment_confidence("moderate", "repo")
        self.assertTrue(self.store.check_promotion("moderate", "repo"))

    def test_complex_promotion_at_threshold(self) -> None:
        """Complex tier promotes at score >= 50."""
        for _ in range(50):
            self.store.increment_confidence("complex", "repo")
        self.assertTrue(self.store.check_promotion("complex", "repo"))

    def test_critical_never_promotes(self) -> None:
        """Critical tier never reaches promotion (threshold is infinity).

        Validates: Requirements 32.5
        """
        # Max out the critical tier ceiling (10)
        for _ in range(100):
            self.store.increment_confidence("critical", "repo")
        cs = self.store.get_confidence("critical", "repo")
        # Ceiling is 10, threshold is infinity
        self.assertAlmostEqual(cs.score, TIER_CEILINGS["critical"])
        self.assertFalse(self.store.check_promotion("critical", "repo"))

    def test_promotion_below_threshold(self) -> None:
        """Score just below threshold does not promote."""
        # Routine threshold is 10; score 9 should not promote
        for _ in range(9):
            self.store.increment_confidence("routine", "repo")
        self.assertFalse(self.store.check_promotion("routine", "repo"))

    def test_promotion_after_decrement_below_threshold(self) -> None:
        """After decrementing below threshold, promotion reverts to False."""
        for _ in range(10):
            self.store.increment_confidence("routine", "repo")
        self.assertTrue(self.store.check_promotion("routine", "repo"))

        self.store.decrement_confidence("routine", "repo")  # -3 -> score 7
        self.assertFalse(self.store.check_promotion("routine", "repo"))


class TestApprovalAudit(unittest.TestCase):
    """Tests for approval audit trail recording.

    Validates: Requirements 33.1
    """

    def setUp(self) -> None:
        self.store = ConfidenceStore(db_path=":memory:")

    def tearDown(self) -> None:
        self.store.close()

    def test_record_audit_basic(self) -> None:
        """Recording an audit creates a retrievable record."""
        decision = ApprovalPolicyDecision(
            routing="auto_approve",
            matched_rule_name="docs-auto",
            reason="Matched rule 'docs-auto' (priority 10)",
        )
        change_context = {
            "risk_level": "low",
            "changed_files": ["README.md"],
            "confidence_score": 15.0,
        }

        audit_id = self.store.record_audit(
            task_id="task-123",
            change_context=change_context,
            decision=decision,
            confidence_at_decision=15.0,
        )

        self.assertIsNotNone(audit_id)
        records = self.store.get_audit_records(task_id="task-123")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["task_id"], "task-123")
        self.assertEqual(records[0]["routing_outcome"], "auto_approve")
        self.assertEqual(records[0]["rule_matched"], "docs-auto")
        self.assertAlmostEqual(records[0]["confidence_at_decision"], 15.0)

    def test_record_audit_stores_change_context_json(self) -> None:
        """Change context is stored as JSON."""
        decision = ApprovalPolicyDecision(
            routing="founder_review",
            matched_rule_name=None,
            reason="No rule matched",
        )
        change_context = {"risk_level": "high", "files_changed": 10}

        self.store.record_audit(
            task_id="task-456",
            change_context=change_context,
            decision=decision,
        )

        records = self.store.get_audit_records(task_id="task-456")
        stored_context = json.loads(records[0]["change_context_json"])
        self.assertEqual(stored_context["risk_level"], "high")
        self.assertEqual(stored_context["files_changed"], 10)

    def test_filter_by_routing_outcome(self) -> None:
        """Audit records can be filtered by routing outcome."""
        for routing in ["auto_approve", "founder_review", "escalate_and_halt"]:
            decision = ApprovalPolicyDecision(
                routing=routing, matched_rule_name=None, reason="test"
            )
            self.store.record_audit(
                task_id=f"task-{routing}",
                change_context={},
                decision=decision,
            )

        auto_records = self.store.get_audit_records(routing_outcome="auto_approve")
        self.assertEqual(len(auto_records), 1)
        self.assertEqual(auto_records[0]["routing_outcome"], "auto_approve")

    def test_multiple_audits_for_same_task(self) -> None:
        """Multiple audit records can exist for the same task."""
        decision = ApprovalPolicyDecision(
            routing="founder_review", matched_rule_name=None, reason="test"
        )
        self.store.record_audit(task_id="task-X", change_context={}, decision=decision)
        self.store.record_audit(task_id="task-X", change_context={}, decision=decision)

        records = self.store.get_audit_records(task_id="task-X")
        self.assertEqual(len(records), 2)

    def test_audit_limit(self) -> None:
        """Audit query respects the limit parameter."""
        decision = ApprovalPolicyDecision(
            routing="auto_approve", matched_rule_name=None, reason="test"
        )
        for i in range(20):
            self.store.record_audit(
                task_id=f"task-{i}", change_context={}, decision=decision
            )

        records = self.store.get_audit_records(limit=5)
        self.assertEqual(len(records), 5)

    def test_audit_without_confidence(self) -> None:
        """Audit records work when confidence is not provided (None)."""
        decision = ApprovalPolicyDecision(
            routing="founder_review", matched_rule_name="catchall", reason="test"
        )
        self.store.record_audit(
            task_id="task-no-conf",
            change_context={"risk_level": "medium"},
            decision=decision,
            confidence_at_decision=None,
        )

        records = self.store.get_audit_records(task_id="task-no-conf")
        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0]["confidence_at_decision"])


if __name__ == "__main__":
    unittest.main()
