"""Unit tests for the ApprovalMatrix policy engine.

Validates: Requirements 30.1, 30.2, 30.3, 30.4, 30.5
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vikram_orchestrator.approval_matrix import (
    ApprovalMatrix,
    ApprovalPolicyDecision,
    ConfidenceScore,
    PolicyRule,
    RiskClassification,
    RuleConditions,
)


def _make_rule(
    name: str = "test-rule",
    priority: int = 10,
    routing: str = "founder_review",
    **condition_kwargs,
) -> PolicyRule:
    """Helper to create a rule with specified conditions."""
    conditions = RuleConditions(**condition_kwargs)
    return PolicyRule(
        name=name,
        priority=priority,
        conditions=conditions,
        routing=routing,
    )


class TestApprovalMatrixBasicEvaluation(unittest.TestCase):
    """Tests for basic rule evaluation logic.

    Validates: Requirements 30.2, 30.4
    """

    def test_no_rules_defaults_to_founder_review(self) -> None:
        """When no rules are configured, defaults to founder_review.

        Validates: Requirements 30.4
        """
        matrix = ApprovalMatrix(rules=[])
        decision = matrix.evaluate({"risk_level": "low"})
        self.assertEqual(decision.routing, "founder_review")
        self.assertIsNone(decision.matched_rule_name)
        self.assertIn("No rule matched", decision.reason)

    def test_single_matching_rule(self) -> None:
        """A single rule that matches returns its routing."""
        rule = _make_rule(name="auto-docs", routing="auto_approve", risk_level=["low"])
        matrix = ApprovalMatrix(rules=[rule])
        decision = matrix.evaluate({"risk_level": "low"})
        self.assertEqual(decision.routing, "auto_approve")
        self.assertEqual(decision.matched_rule_name, "auto-docs")

    def test_no_matching_rule_defaults_to_founder_review(self) -> None:
        """When no rule matches the context, defaults to founder_review.

        Validates: Requirements 30.4
        """
        rule = _make_rule(name="high-only", routing="escalate_and_halt", risk_level=["high"])
        matrix = ApprovalMatrix(rules=[rule])
        decision = matrix.evaluate({"risk_level": "low"})
        self.assertEqual(decision.routing, "founder_review")
        self.assertIsNone(decision.matched_rule_name)

    def test_empty_conditions_matches_everything(self) -> None:
        """A rule with empty conditions matches any context."""
        rule = _make_rule(name="catch-all", priority=999, routing="founder_review")
        matrix = ApprovalMatrix(rules=[rule])
        decision = matrix.evaluate({"risk_level": "critical", "lines_changed": 9999})
        self.assertEqual(decision.routing, "founder_review")
        self.assertEqual(decision.matched_rule_name, "catch-all")


class TestApprovalMatrixPriorityOrdering(unittest.TestCase):
    """Tests for priority-based rule evaluation.

    Validates: Requirements 30.2
    """

    def test_first_matching_rule_by_priority(self) -> None:
        """Rules are evaluated in priority order; first match wins."""
        rules = [
            _make_rule(name="low-priority", priority=100, routing="auto_approve", risk_level=["low"]),
            _make_rule(name="high-priority", priority=1, routing="founder_review", risk_level=["low"]),
        ]
        matrix = ApprovalMatrix(rules=rules)
        decision = matrix.evaluate({"risk_level": "low"})
        # Priority 1 is evaluated first
        self.assertEqual(decision.matched_rule_name, "high-priority")
        self.assertEqual(decision.routing, "founder_review")

    def test_skips_non_matching_higher_priority_rule(self) -> None:
        """A higher-priority rule that doesn't match is skipped."""
        rules = [
            _make_rule(name="security", priority=1, routing="escalate_and_halt", risk_level=["critical"]),
            _make_rule(name="docs", priority=10, routing="auto_approve", risk_level=["low"]),
        ]
        matrix = ApprovalMatrix(rules=rules)
        decision = matrix.evaluate({"risk_level": "low"})
        self.assertEqual(decision.matched_rule_name, "docs")
        self.assertEqual(decision.routing, "auto_approve")

    def test_rules_sorted_regardless_of_input_order(self) -> None:
        """Rules are sorted by priority regardless of the order they are provided."""
        rules = [
            _make_rule(name="last", priority=999, routing="founder_review"),
            _make_rule(name="first", priority=1, routing="auto_approve"),
            _make_rule(name="middle", priority=50, routing="escalate_and_halt"),
        ]
        matrix = ApprovalMatrix(rules=rules)
        # "first" (priority=1) has no conditions, matches everything
        decision = matrix.evaluate({})
        self.assertEqual(decision.matched_rule_name, "first")


class TestApprovalMatrixConditions(unittest.TestCase):
    """Tests for individual condition matching.

    Validates: Requirements 30.1
    """

    def test_risk_level_condition(self) -> None:
        """Rule matches when context risk_level is in the rule's list."""
        rule = _make_rule(routing="auto_approve", risk_level=["low", "medium"])
        matrix = ApprovalMatrix(rules=[rule])

        self.assertEqual(matrix.evaluate({"risk_level": "low"}).routing, "auto_approve")
        self.assertEqual(matrix.evaluate({"risk_level": "medium"}).routing, "auto_approve")
        # high is not in the list
        self.assertEqual(matrix.evaluate({"risk_level": "high"}).routing, "founder_review")

    def test_risk_level_missing_from_context(self) -> None:
        """Rule with risk_level condition doesn't match when context lacks it."""
        rule = _make_rule(routing="auto_approve", risk_level=["low"])
        matrix = ApprovalMatrix(rules=[rule])
        decision = matrix.evaluate({})
        self.assertEqual(decision.routing, "founder_review")

    def test_file_patterns_condition(self) -> None:
        """Rule matches when at least one file matches a glob pattern."""
        rule = _make_rule(routing="founder_review", file_patterns=["**/auth/**", "**/security/**"])
        matrix = ApprovalMatrix(rules=[rule])

        decision = matrix.evaluate({"changed_files": ["src/auth/login.py"]})
        self.assertEqual(decision.routing, "founder_review")

        decision = matrix.evaluate({"changed_files": ["src/utils.py"]})
        self.assertEqual(decision.routing, "founder_review")  # falls through to default

    def test_file_patterns_no_changed_files(self) -> None:
        """Rule with file_patterns doesn't match when no changed_files."""
        rule = _make_rule(routing="auto_approve", file_patterns=["**/*.md"])
        matrix = ApprovalMatrix(rules=[rule])
        decision = matrix.evaluate({"changed_files": []})
        self.assertEqual(decision.routing, "founder_review")

    def test_file_patterns_glob_matching(self) -> None:
        """Glob patterns match correctly for various file paths."""
        rule = _make_rule(routing="auto_approve", file_patterns=["**/*.md", "docs/**"])
        matrix = ApprovalMatrix(rules=[rule])

        decision = matrix.evaluate({"changed_files": ["README.md"]})
        self.assertEqual(decision.routing, "auto_approve")

        decision = matrix.evaluate({"changed_files": ["docs/guide.txt"]})
        self.assertEqual(decision.routing, "auto_approve")

        decision = matrix.evaluate({"changed_files": ["src/main.py"]})
        self.assertEqual(decision.routing, "founder_review")

    def test_max_lines_changed_condition(self) -> None:
        """Rule matches when lines changed is within the limit."""
        rule = _make_rule(routing="auto_approve", max_lines_changed=50)
        matrix = ApprovalMatrix(rules=[rule])

        self.assertEqual(matrix.evaluate({"lines_changed": 30}).routing, "auto_approve")
        self.assertEqual(matrix.evaluate({"lines_changed": 50}).routing, "auto_approve")
        self.assertEqual(matrix.evaluate({"lines_changed": 51}).routing, "founder_review")

    def test_max_files_changed_condition(self) -> None:
        """Rule matches when files changed is within the limit."""
        rule = _make_rule(routing="auto_approve", max_files_changed=3)
        matrix = ApprovalMatrix(rules=[rule])

        self.assertEqual(matrix.evaluate({"files_changed": 2}).routing, "auto_approve")
        self.assertEqual(matrix.evaluate({"files_changed": 3}).routing, "auto_approve")
        self.assertEqual(matrix.evaluate({"files_changed": 4}).routing, "founder_review")

    def test_min_confidence_score_condition(self) -> None:
        """Rule matches when confidence score meets the minimum."""
        rule = _make_rule(routing="auto_approve", min_confidence_score=5.0)
        matrix = ApprovalMatrix(rules=[rule])

        self.assertEqual(matrix.evaluate({"confidence_score": 10.0}).routing, "auto_approve")
        self.assertEqual(matrix.evaluate({"confidence_score": 5.0}).routing, "auto_approve")
        self.assertEqual(matrix.evaluate({"confidence_score": 4.9}).routing, "founder_review")

    def test_max_cost_consumed_pct_condition(self) -> None:
        """Rule matches when cost consumed percentage is within the limit."""
        rule = _make_rule(routing="auto_approve", max_cost_consumed_pct=80.0)
        matrix = ApprovalMatrix(rules=[rule])

        self.assertEqual(matrix.evaluate({"cost_consumed_pct": 50.0}).routing, "auto_approve")
        self.assertEqual(matrix.evaluate({"cost_consumed_pct": 80.0}).routing, "auto_approve")
        self.assertEqual(matrix.evaluate({"cost_consumed_pct": 81.0}).routing, "founder_review")

    def test_repo_path_prefix_condition(self) -> None:
        """Rule matches when repo path starts with one of the prefixes."""
        rule = _make_rule(routing="auto_approve", repo_path_prefix=["/repos/docs", "/repos/config"])
        matrix = ApprovalMatrix(rules=[rule])

        self.assertEqual(
            matrix.evaluate({"repo_path": "/repos/docs/wiki"}).routing, "auto_approve"
        )
        self.assertEqual(
            matrix.evaluate({"repo_path": "/repos/config"}).routing, "auto_approve"
        )
        self.assertEqual(
            matrix.evaluate({"repo_path": "/repos/main"}).routing, "founder_review"
        )

    def test_all_conditions_must_match(self) -> None:
        """All conditions must be satisfied for a rule to match (AND logic)."""
        rule = _make_rule(
            routing="auto_approve",
            risk_level=["low"],
            max_files_changed=3,
            min_confidence_score=5.0,
        )
        matrix = ApprovalMatrix(rules=[rule])

        # All conditions satisfied
        decision = matrix.evaluate({
            "risk_level": "low",
            "files_changed": 2,
            "confidence_score": 10.0,
        })
        self.assertEqual(decision.routing, "auto_approve")

        # One condition fails (confidence too low)
        decision = matrix.evaluate({
            "risk_level": "low",
            "files_changed": 2,
            "confidence_score": 3.0,
        })
        self.assertEqual(decision.routing, "founder_review")


class TestApprovalMatrixYAMLLoading(unittest.TestCase):
    """Tests for YAML configuration file loading.

    Validates: Requirements 30.3
    """

    def _write_yaml(self, content: str) -> Path:
        """Write YAML content to a temp file and return its path."""
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        tmp.write(content)
        tmp.close()
        return Path(tmp.name)

    def test_load_valid_yaml(self) -> None:
        """Valid YAML config is loaded correctly."""
        config = """
version: 1
rules:
  - name: security-always-review
    priority: 1
    conditions:
      file_patterns: ["**/auth/**", "**/security/**"]
    routing: founder_review
  - name: docs-auto-approve
    priority: 10
    conditions:
      risk_level: [low]
      file_patterns: ["**/*.md", "docs/**"]
      min_confidence_score: 5
    routing: auto_approve
  - name: default-review
    priority: 999
    conditions: {}
    routing: founder_review
"""
        path = self._write_yaml(config)
        matrix = ApprovalMatrix(config_path=path)
        self.assertEqual(len(matrix.rules), 3)
        self.assertEqual(matrix.rules[0].name, "security-always-review")
        self.assertEqual(matrix.rules[0].priority, 1)
        self.assertEqual(matrix.rules[1].name, "docs-auto-approve")
        self.assertEqual(matrix.rules[2].name, "default-review")

    def test_load_yaml_evaluates_correctly(self) -> None:
        """Loaded YAML rules evaluate as expected."""
        config = """
version: 1
rules:
  - name: docs-auto
    priority: 10
    conditions:
      risk_level: [low]
      file_patterns: ["**/*.md"]
    routing: auto_approve
  - name: catchall
    priority: 999
    conditions: {}
    routing: founder_review
"""
        path = self._write_yaml(config)
        matrix = ApprovalMatrix(config_path=path)

        decision = matrix.evaluate({
            "risk_level": "low",
            "changed_files": ["README.md"],
        })
        self.assertEqual(decision.routing, "auto_approve")
        self.assertEqual(decision.matched_rule_name, "docs-auto")

    def test_load_nonexistent_file_raises(self) -> None:
        """Loading from a nonexistent path raises FileNotFoundError."""
        with self.assertRaises(FileNotFoundError):
            ApprovalMatrix(config_path=Path("/nonexistent/approval-matrix.yaml"))

    def test_load_invalid_yaml_raises(self) -> None:
        """Loading invalid YAML raises ValueError."""
        path = self._write_yaml("not a mapping: [")
        with self.assertRaises(Exception):
            ApprovalMatrix(config_path=path)

    def test_load_yaml_with_null_conditions(self) -> None:
        """YAML rules with null conditions are treated as empty conditions."""
        config = """
version: 1
rules:
  - name: catchall
    priority: 999
    conditions:
    routing: founder_review
"""
        path = self._write_yaml(config)
        matrix = ApprovalMatrix(config_path=path)
        decision = matrix.evaluate({"risk_level": "critical"})
        self.assertEqual(decision.routing, "founder_review")
        self.assertEqual(decision.matched_rule_name, "catchall")

    def test_reload_updates_rules(self) -> None:
        """Reload replaces the active rules with new config."""
        config_v1 = """
version: 1
rules:
  - name: v1-rule
    priority: 1
    conditions: {}
    routing: auto_approve
"""
        config_v2 = """
version: 1
rules:
  - name: v2-rule
    priority: 1
    conditions: {}
    routing: escalate_and_halt
"""
        path = self._write_yaml(config_v1)
        matrix = ApprovalMatrix(config_path=path)
        self.assertEqual(matrix.evaluate({}).routing, "auto_approve")

        # Overwrite with v2
        with open(path, "w") as f:
            f.write(config_v2)

        success, error = matrix.reload(path)
        self.assertTrue(success)
        self.assertEqual(error, "")
        self.assertEqual(matrix.evaluate({}).routing, "escalate_and_halt")

    def test_reload_invalid_retains_old_config(self) -> None:
        """Reload with invalid config retains the previous valid config."""
        config_v1 = """
version: 1
rules:
  - name: v1-rule
    priority: 1
    conditions: {}
    routing: auto_approve
"""
        path = self._write_yaml(config_v1)
        matrix = ApprovalMatrix(config_path=path)

        # Overwrite with invalid content
        with open(path, "w") as f:
            f.write("not: [valid: yaml: for: rules")

        success, error = matrix.reload(path)
        self.assertFalse(success)
        self.assertNotEqual(error, "")
        # Old rules still active
        self.assertEqual(matrix.evaluate({}).routing, "auto_approve")


class TestApprovalMatrixDecisionModel(unittest.TestCase):
    """Tests for the ApprovalPolicyDecision model."""

    def test_decision_fields(self) -> None:
        """Decision model contains routing, matched_rule_name, and reason."""
        decision = ApprovalPolicyDecision(
            routing="auto_approve",
            matched_rule_name="test-rule",
            reason="Matched rule 'test-rule' (priority 5)",
        )
        self.assertEqual(decision.routing, "auto_approve")
        self.assertEqual(decision.matched_rule_name, "test-rule")
        self.assertIn("test-rule", decision.reason)

    def test_decision_no_match(self) -> None:
        """Decision with no match has None matched_rule_name."""
        decision = ApprovalPolicyDecision(
            routing="founder_review",
            matched_rule_name=None,
            reason="No rule matched",
        )
        self.assertIsNone(decision.matched_rule_name)


class TestApprovalMatrixModels(unittest.TestCase):
    """Tests for supporting Pydantic models."""

    def test_risk_classification_model(self) -> None:
        """RiskClassification model works with all fields."""
        rc = RiskClassification(
            level="high",
            signals={"security_touch": 0.8, "file_count": 0.3},
            matched_patterns=["**/auth/**"],
            total_score=0.85,
        )
        self.assertEqual(rc.level, "high")
        self.assertEqual(len(rc.signals), 2)
        self.assertEqual(rc.matched_patterns, ["**/auth/**"])

    def test_confidence_score_model(self) -> None:
        """ConfidenceScore model works with all fields."""
        cs = ConfidenceScore(
            complexity_tier="routine",
            repository="/repos/my-app",
            score=15.0,
            ceiling=30.0,
            last_updated=1700000000.0,
        )
        self.assertEqual(cs.complexity_tier, "routine")
        self.assertEqual(cs.score, 15.0)
        self.assertEqual(cs.ceiling, 30.0)

    def test_rule_conditions_defaults(self) -> None:
        """RuleConditions fields default to None."""
        conditions = RuleConditions()
        self.assertIsNone(conditions.risk_level)
        self.assertIsNone(conditions.file_patterns)
        self.assertIsNone(conditions.max_lines_changed)
        self.assertIsNone(conditions.max_files_changed)
        self.assertIsNone(conditions.min_confidence_score)
        self.assertIsNone(conditions.max_cost_consumed_pct)
        self.assertIsNone(conditions.repo_path_prefix)

    def test_policy_rule_model(self) -> None:
        """PolicyRule model works with all fields."""
        rule = PolicyRule(
            name="test",
            priority=5,
            conditions=RuleConditions(risk_level=["low"]),
            routing="auto_approve",
        )
        self.assertEqual(rule.name, "test")
        self.assertEqual(rule.priority, 5)
        self.assertEqual(rule.conditions.risk_level, ["low"])
        self.assertEqual(rule.routing, "auto_approve")


class TestApprovalMatrixIntegrationScenario(unittest.TestCase):
    """Integration-style tests simulating realistic approval matrix usage.

    Validates: Requirements 30.1, 30.2, 30.3, 30.4, 30.5
    """

    def _sample_rules(self) -> list[PolicyRule]:
        """Create a realistic set of rules mimicking .vikram/approval-matrix.yaml."""
        return [
            _make_rule(
                name="security-always-review",
                priority=1,
                routing="founder_review",
                file_patterns=["**/auth/**", "**/security/**"],
            ),
            _make_rule(
                name="docs-auto-approve",
                priority=10,
                routing="auto_approve",
                risk_level=["low"],
                file_patterns=["**/*.md", "docs/**"],
                min_confidence_score=5.0,
            ),
            _make_rule(
                name="small-low-risk",
                priority=20,
                routing="auto_approve",
                risk_level=["low"],
                max_lines_changed=20,
                max_files_changed=2,
                min_confidence_score=10.0,
            ),
            _make_rule(
                name="default-review",
                priority=999,
                routing="founder_review",
            ),
        ]

    def test_security_file_triggers_review(self) -> None:
        """Security-related files always trigger founder review regardless of other signals."""
        matrix = ApprovalMatrix(rules=self._sample_rules())
        decision = matrix.evaluate({
            "risk_level": "low",
            "changed_files": ["src/auth/tokens.py"],
            "lines_changed": 5,
            "files_changed": 1,
            "confidence_score": 50.0,
        })
        self.assertEqual(decision.routing, "founder_review")
        self.assertEqual(decision.matched_rule_name, "security-always-review")

    def test_docs_auto_approved_with_confidence(self) -> None:
        """Documentation changes auto-approve when confidence is sufficient."""
        matrix = ApprovalMatrix(rules=self._sample_rules())
        decision = matrix.evaluate({
            "risk_level": "low",
            "changed_files": ["docs/api-guide.md"],
            "confidence_score": 10.0,
        })
        self.assertEqual(decision.routing, "auto_approve")
        self.assertEqual(decision.matched_rule_name, "docs-auto-approve")

    def test_docs_without_confidence_falls_through(self) -> None:
        """Documentation changes without confidence fall to next matching rule."""
        matrix = ApprovalMatrix(rules=self._sample_rules())
        decision = matrix.evaluate({
            "risk_level": "low",
            "changed_files": ["docs/api-guide.md"],
            "confidence_score": 2.0,
            "lines_changed": 5,
            "files_changed": 1,
        })
        # Falls through docs-auto-approve (confidence too low)
        # Doesn't match small-low-risk (needs confidence 10)
        # Matches default-review
        self.assertEqual(decision.routing, "founder_review")
        self.assertEqual(decision.matched_rule_name, "default-review")

    def test_small_low_risk_auto_approved(self) -> None:
        """Small, low-risk changes auto-approve with high confidence."""
        matrix = ApprovalMatrix(rules=self._sample_rules())
        decision = matrix.evaluate({
            "risk_level": "low",
            "changed_files": ["src/utils.py"],
            "lines_changed": 10,
            "files_changed": 1,
            "confidence_score": 15.0,
        })
        self.assertEqual(decision.routing, "auto_approve")
        self.assertEqual(decision.matched_rule_name, "small-low-risk")

    def test_large_change_falls_to_default(self) -> None:
        """Large changes fall through to the default review rule."""
        matrix = ApprovalMatrix(rules=self._sample_rules())
        decision = matrix.evaluate({
            "risk_level": "high",
            "changed_files": ["src/core/engine.py", "src/core/parser.py"],
            "lines_changed": 500,
            "files_changed": 10,
            "confidence_score": 5.0,
        })
        self.assertEqual(decision.routing, "founder_review")
        self.assertEqual(decision.matched_rule_name, "default-review")


if __name__ == "__main__":
    unittest.main()
