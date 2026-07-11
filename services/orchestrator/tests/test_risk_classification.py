"""Unit tests for the Risk Classification Engine.

Validates: Requirements 34.1, 34.2, 34.3, 34.4
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vikram_orchestrator.risk_classification import (
    DEFAULT_SENSITIVITY_PATTERNS,
    FAILURE_RATE_CRITICAL_THRESHOLD,
    FAILURE_RATE_HIGH_THRESHOLD,
    FAILURE_RATE_MEDIUM_THRESHOLD,
    FILES_HIGH_THRESHOLD,
    FILES_MEDIUM_THRESHOLD,
    LINES_HIGH_THRESHOLD,
    LINES_MEDIUM_THRESHOLD,
    RiskClassificationEngine,
    RiskPattern,
)


class TestRiskClassificationFileSensitivity(unittest.TestCase):
    """Tests for file sensitivity signal evaluation.

    Validates: Requirements 34.1, 34.2
    """

    def test_auth_file_classified_critical(self) -> None:
        """Files in auth/ directory are classified as critical."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "changed_files": ["src/auth/login.py"],
        })
        self.assertEqual(result.level, "critical")
        self.assertIn("file_sensitivity", result.signals)

    def test_security_file_classified_critical(self) -> None:
        """Files in security/ directory are classified as critical."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "changed_files": ["pkg/security/tokens.go"],
        })
        self.assertEqual(result.level, "critical")

    def test_env_file_classified_critical(self) -> None:
        """Environment files are classified as critical."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "changed_files": [".env.production"],
        })
        self.assertEqual(result.level, "critical")

    def test_config_file_classified_high(self) -> None:
        """Files in config/ directory are classified as high."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "changed_files": ["src/config/settings.yaml"],
        })
        self.assertEqual(result.level, "high")

    def test_no_sensitive_files_no_signal(self) -> None:
        """Non-sensitive files produce no file_sensitivity signal."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "changed_files": ["src/utils/helpers.py"],
        })
        self.assertNotIn("file_sensitivity", result.signals)

    def test_matched_patterns_recorded(self) -> None:
        """Matched patterns are recorded in the result."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "changed_files": ["src/auth/login.py"],
        })
        self.assertTrue(len(result.matched_patterns) > 0)

    def test_multiple_files_highest_level_wins(self) -> None:
        """When multiple files match different levels, highest wins.

        Validates: Requirements 34.3
        """
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "changed_files": [
                "src/config/db.yaml",   # high
                "src/auth/tokens.py",   # critical
            ],
        })
        self.assertEqual(result.level, "critical")

    def test_no_files_no_signal(self) -> None:
        """Empty file list produces no file sensitivity signal."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "changed_files": [],
        })
        self.assertEqual(result.level, "low")


class TestRiskClassificationLinesChanged(unittest.TestCase):
    """Tests for lines changed signal evaluation.

    Validates: Requirements 34.1
    """

    def test_high_lines_changed(self) -> None:
        """>=500 lines changed classified as high."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "lines_changed": LINES_HIGH_THRESHOLD,
        })
        self.assertEqual(result.level, "high")
        self.assertIn("lines_changed", result.signals)

    def test_medium_lines_changed(self) -> None:
        """>=100 lines changed classified as medium."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "lines_changed": LINES_MEDIUM_THRESHOLD,
        })
        self.assertEqual(result.level, "medium")

    def test_low_lines_changed_no_signal(self) -> None:
        """<100 lines changed produces no lines_changed signal."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "lines_changed": LINES_MEDIUM_THRESHOLD - 1,
        })
        self.assertNotIn("lines_changed", result.signals)

    def test_zero_lines_no_signal(self) -> None:
        """Zero lines changed produces no signal."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "lines_changed": 0,
        })
        self.assertNotIn("lines_changed", result.signals)


class TestRiskClassificationFilesChanged(unittest.TestCase):
    """Tests for files changed signal evaluation.

    Validates: Requirements 34.1
    """

    def test_high_files_changed(self) -> None:
        """>=10 files changed classified as high."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "files_changed": FILES_HIGH_THRESHOLD,
        })
        self.assertEqual(result.level, "high")
        self.assertIn("files_changed", result.signals)

    def test_medium_files_changed(self) -> None:
        """>=5 files changed classified as medium."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "files_changed": FILES_MEDIUM_THRESHOLD,
        })
        self.assertEqual(result.level, "medium")

    def test_low_files_changed_no_signal(self) -> None:
        """<5 files changed produces no files_changed signal."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "files_changed": FILES_MEDIUM_THRESHOLD - 1,
        })
        self.assertNotIn("files_changed", result.signals)


class TestRiskClassificationTestModification(unittest.TestCase):
    """Tests for test modification signal evaluation.

    Validates: Requirements 34.1
    """

    def test_test_modified_medium_risk(self) -> None:
        """Modifying tests signals medium risk."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "test_modified": True,
        })
        self.assertEqual(result.level, "medium")
        self.assertIn("test_modification", result.signals)

    def test_no_test_modification_no_signal(self) -> None:
        """No test modification produces no signal."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "test_modified": False,
        })
        self.assertNotIn("test_modification", result.signals)


class TestRiskClassificationSecurityCode(unittest.TestCase):
    """Tests for security code signal evaluation.

    Validates: Requirements 34.1
    """

    def test_security_relevant_critical(self) -> None:
        """Security-relevant code classified as critical."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "security_relevant": True,
        })
        self.assertEqual(result.level, "critical")
        self.assertIn("security_code", result.signals)

    def test_not_security_relevant_no_signal(self) -> None:
        """Non-security code produces no signal."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "security_relevant": False,
        })
        self.assertNotIn("security_code", result.signals)


class TestRiskClassificationHistoricalFailureRate(unittest.TestCase):
    """Tests for historical failure rate signal evaluation.

    Validates: Requirements 34.1
    """

    def test_critical_failure_rate(self) -> None:
        """>=50% failure rate classified as critical."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "historical_failure_rate": FAILURE_RATE_CRITICAL_THRESHOLD,
        })
        self.assertEqual(result.level, "critical")
        self.assertIn("historical_failure_rate", result.signals)

    def test_high_failure_rate(self) -> None:
        """>=30% failure rate classified as high."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "historical_failure_rate": FAILURE_RATE_HIGH_THRESHOLD,
        })
        self.assertEqual(result.level, "high")

    def test_medium_failure_rate(self) -> None:
        """>=15% failure rate classified as medium."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "historical_failure_rate": FAILURE_RATE_MEDIUM_THRESHOLD,
        })
        self.assertEqual(result.level, "medium")

    def test_low_failure_rate_no_signal(self) -> None:
        """<15% failure rate produces no signal."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "historical_failure_rate": FAILURE_RATE_MEDIUM_THRESHOLD - 0.01,
        })
        self.assertNotIn("historical_failure_rate", result.signals)


class TestRiskClassificationMaximumPrinciple(unittest.TestCase):
    """Tests for the Maximum Risk Principle.

    Validates: Requirements 34.3
    """

    def test_highest_signal_wins(self) -> None:
        """The overall level equals the highest individual signal level."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "lines_changed": LINES_MEDIUM_THRESHOLD,  # medium
            "security_relevant": True,                 # critical
            "files_changed": FILES_MEDIUM_THRESHOLD,   # medium
        })
        self.assertEqual(result.level, "critical")

    def test_multiple_medium_stays_medium(self) -> None:
        """Multiple medium signals don't escalate to high."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "lines_changed": LINES_MEDIUM_THRESHOLD,   # medium
            "files_changed": FILES_MEDIUM_THRESHOLD,   # medium
            "test_modified": True,                      # medium
        })
        self.assertEqual(result.level, "medium")

    def test_no_signals_defaults_to_low(self) -> None:
        """When no signals trigger, risk defaults to low."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({})
        self.assertEqual(result.level, "low")

    def test_all_signals_present(self) -> None:
        """All signals evaluated; highest wins."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "changed_files": ["src/auth/login.py"],     # critical (file sensitivity)
            "lines_changed": LINES_HIGH_THRESHOLD,      # high
            "files_changed": FILES_HIGH_THRESHOLD,      # high
            "test_modified": True,                       # medium
            "security_relevant": True,                   # critical
            "historical_failure_rate": 0.6,             # critical
        })
        self.assertEqual(result.level, "critical")
        # All signal names should be present
        self.assertIn("file_sensitivity", result.signals)
        self.assertIn("lines_changed", result.signals)
        self.assertIn("files_changed", result.signals)
        self.assertIn("test_modification", result.signals)
        self.assertIn("security_code", result.signals)
        self.assertIn("historical_failure_rate", result.signals)


class TestRiskClassificationRepoPatterns(unittest.TestCase):
    """Tests for repository-specific risk patterns from .vikram/risk-patterns.yaml.

    Validates: Requirements 34.2
    """

    def _create_repo_with_patterns(self, yaml_content: str) -> Path:
        """Create a temp directory with .vikram/risk-patterns.yaml."""
        repo_dir = Path(tempfile.mkdtemp())
        vikram_dir = repo_dir / ".vikram"
        vikram_dir.mkdir()
        config_file = vikram_dir / "risk-patterns.yaml"
        config_file.write_text(yaml_content)
        return repo_dir

    def test_load_custom_patterns(self) -> None:
        """Custom patterns from risk-patterns.yaml override defaults."""
        yaml_content = """
patterns:
  - pattern: "**/payments/**"
    level: critical
  - pattern: "**/api/**"
    level: high
"""
        repo_dir = self._create_repo_with_patterns(yaml_content)
        engine = RiskClassificationEngine(repo_path=repo_dir)

        # Custom pattern matches
        result = engine.classify_change_risk({
            "changed_files": ["src/payments/stripe.py"],
        })
        self.assertEqual(result.level, "critical")

        # Default patterns should NOT apply (overridden)
        result = engine.classify_change_risk({
            "changed_files": ["src/auth/login.py"],
        })
        # auth is NOT in the custom patterns, so no file_sensitivity signal
        self.assertNotIn("file_sensitivity", result.signals)

    def test_fallback_to_defaults_when_no_config(self) -> None:
        """Falls back to defaults when .vikram/risk-patterns.yaml doesn't exist."""
        repo_dir = Path(tempfile.mkdtemp())
        engine = RiskClassificationEngine(repo_path=repo_dir)

        result = engine.classify_change_risk({
            "changed_files": ["src/auth/tokens.py"],
        })
        self.assertEqual(result.level, "critical")

    def test_fallback_to_defaults_on_invalid_yaml(self) -> None:
        """Falls back to defaults when YAML is invalid."""
        repo_dir = Path(tempfile.mkdtemp())
        vikram_dir = repo_dir / ".vikram"
        vikram_dir.mkdir()
        config_file = vikram_dir / "risk-patterns.yaml"
        config_file.write_text("not: [valid: yaml: for: patterns")

        engine = RiskClassificationEngine(repo_path=repo_dir)
        # Should use defaults, so auth/ → critical
        result = engine.classify_change_risk({
            "changed_files": ["src/auth/login.py"],
        })
        self.assertEqual(result.level, "critical")

    def test_fallback_when_patterns_list_empty(self) -> None:
        """Falls back to defaults when patterns list is empty."""
        yaml_content = """
patterns: []
"""
        repo_dir = self._create_repo_with_patterns(yaml_content)
        engine = RiskClassificationEngine(repo_path=repo_dir)

        result = engine.classify_change_risk({
            "changed_files": ["src/auth/login.py"],
        })
        self.assertEqual(result.level, "critical")

    def test_explicit_patterns_override_all(self) -> None:
        """Explicitly passed patterns override repo config and defaults."""
        custom_patterns = [
            RiskPattern(pattern="**/custom/**", level="high"),
        ]
        engine = RiskClassificationEngine(patterns=custom_patterns)

        result = engine.classify_change_risk({
            "changed_files": ["src/custom/module.py"],
        })
        self.assertEqual(result.level, "high")

        # Default auth pattern does NOT match
        result = engine.classify_change_risk({
            "changed_files": ["src/auth/login.py"],
        })
        self.assertNotIn("file_sensitivity", result.signals)


class TestRiskClassificationSignalRecording(unittest.TestCase):
    """Tests that all signals are properly recorded for traceability.

    Validates: Requirements 34.4
    """

    def test_signals_dict_contains_all_active_signals(self) -> None:
        """The signals dict records all contributing signal names with scores."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "changed_files": ["src/auth/login.py"],
            "lines_changed": 600,
            "test_modified": True,
        })
        self.assertIn("file_sensitivity", result.signals)
        self.assertIn("lines_changed", result.signals)
        self.assertIn("test_modification", result.signals)

    def test_signal_scores_are_numeric(self) -> None:
        """Signal scores are numeric level ordinals."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "security_relevant": True,      # critical = 3
            "lines_changed": 200,           # medium = 1
        })
        self.assertEqual(result.signals["security_code"], 3.0)
        self.assertEqual(result.signals["lines_changed"], 1.0)

    def test_total_score_matches_max_level(self) -> None:
        """Total score is the ordinal of the overall level."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "security_relevant": True,
        })
        self.assertEqual(result.total_score, 3.0)  # critical = 3

    def test_matched_patterns_deduplicated(self) -> None:
        """Matched patterns are deduplicated when same pattern matches multiple files."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "changed_files": [
                "src/auth/login.py",
                "src/auth/tokens.py",
            ],
        })
        # The **/auth/** pattern should appear only once
        auth_count = result.matched_patterns.count("**/auth/**")
        self.assertEqual(auth_count, 1)


class TestRiskClassificationEdgeCases(unittest.TestCase):
    """Edge case tests for robustness."""

    def test_empty_context(self) -> None:
        """Empty change context defaults to low risk."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({})
        self.assertEqual(result.level, "low")
        self.assertEqual(result.signals, {})
        self.assertEqual(result.matched_patterns, [])

    def test_zero_failure_rate(self) -> None:
        """Zero failure rate produces no signal."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "historical_failure_rate": 0.0,
        })
        self.assertEqual(result.level, "low")

    def test_boundary_lines_just_below_medium(self) -> None:
        """99 lines changed does not trigger medium."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "lines_changed": 99,
        })
        self.assertNotIn("lines_changed", result.signals)

    def test_boundary_files_just_below_medium(self) -> None:
        """4 files changed does not trigger medium."""
        engine = RiskClassificationEngine()
        result = engine.classify_change_risk({
            "files_changed": 4,
        })
        self.assertNotIn("files_changed", result.signals)


if __name__ == "__main__":
    unittest.main()
