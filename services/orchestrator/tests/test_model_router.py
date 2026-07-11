"""Unit tests for the ModelRouter complexity classification.

Validates: Requirements 11.1, 11.2
"""

from __future__ import annotations

import unittest

from vikram_orchestrator.model_router import (
    ComplexitySignals,
    ComplexityTier,
    ModelRouter,
    ModelSelection,
)


def _make_signals(**overrides) -> ComplexitySignals:
    """Build a ComplexitySignals with sensible defaults, applying overrides."""
    defaults = {
        "objective_scope": "Fix typo in README",
        "target_file_count": 1,
        "repo_size_files": 100,
        "language_count": 1,
        "change_type": "documentation",
        "security_relevant": False,
        "test_modification": False,
    }
    defaults.update(overrides)
    return ComplexitySignals(**defaults)


class TestClassifyComplexityRoutine(unittest.TestCase):
    """Tests that verify ROUTINE tier classification."""

    def test_single_file_documentation(self) -> None:
        """Single doc file, no security, single language -> ROUTINE (score=5)."""
        signals = _make_signals(
            target_file_count=1,
            change_type="documentation",
            security_relevant=False,
            language_count=1,
        )
        router = ModelRouter()
        result = router.classify_complexity(signals)
        self.assertEqual(result, ComplexityTier.ROUTINE)

    def test_single_file_config(self) -> None:
        """Single config file, no security, single language -> MODERATE (score=15)."""
        signals = _make_signals(
            target_file_count=1,
            change_type="config",
            security_relevant=False,
            language_count=1,
        )
        router = ModelRouter()
        result = router.classify_complexity(signals)
        # score = 5 (1 file) + 10 (config) = 15 -> ROUTINE
        self.assertEqual(result, ComplexityTier.ROUTINE)


class TestClassifyComplexityModerate(unittest.TestCase):
    """Tests that verify MODERATE tier classification."""

    def test_few_files_config(self) -> None:
        """2-3 files, config change -> MODERATE (score=25)."""
        signals = _make_signals(
            target_file_count=2,
            change_type="config",
            security_relevant=False,
            language_count=1,
        )
        router = ModelRouter()
        result = router.classify_complexity(signals)
        # score = 15 (2-3 files) + 10 (config) = 25 -> MODERATE
        self.assertEqual(result, ComplexityTier.MODERATE)

    def test_single_file_logic(self) -> None:
        """Single file, logic change -> MODERATE (score=30)."""
        signals = _make_signals(
            target_file_count=1,
            change_type="logic",
            security_relevant=False,
            language_count=1,
        )
        router = ModelRouter()
        result = router.classify_complexity(signals)
        # score = 5 (1 file) + 25 (logic) = 30 -> MODERATE
        self.assertEqual(result, ComplexityTier.MODERATE)

    def test_few_files_logic_multi_language(self) -> None:
        """2-3 files, logic, 2 languages -> MODERATE (score=45 -> COMPLEX)."""
        signals = _make_signals(
            target_file_count=3,
            change_type="logic",
            security_relevant=False,
            language_count=2,
        )
        router = ModelRouter()
        result = router.classify_complexity(signals)
        # score = 15 (2-3 files) + 25 (logic) + 5 (2 languages) = 45 -> COMPLEX
        self.assertEqual(result, ComplexityTier.COMPLEX)


class TestClassifyComplexityComplex(unittest.TestCase):
    """Tests that verify COMPLEX tier classification."""

    def test_many_files_logic(self) -> None:
        """4-8 files, logic change -> COMPLEX (score=50)."""
        signals = _make_signals(
            target_file_count=5,
            change_type="logic",
            security_relevant=False,
            language_count=1,
        )
        router = ModelRouter()
        result = router.classify_complexity(signals)
        # score = 25 (4-8 files) + 25 (logic) = 50 -> COMPLEX
        self.assertEqual(result, ComplexityTier.COMPLEX)

    def test_few_files_logic_with_security(self) -> None:
        """2-3 files, logic, security -> COMPLEX (score=60)."""
        signals = _make_signals(
            target_file_count=2,
            change_type="logic",
            security_relevant=True,
            language_count=1,
        )
        router = ModelRouter()
        result = router.classify_complexity(signals)
        # score = 15 (2-3 files) + 25 (logic) + 20 (security) = 60 -> COMPLEX
        self.assertEqual(result, ComplexityTier.COMPLEX)

    def test_many_files_config_security_multi_lang(self) -> None:
        """4-8 files, config, security, 3 languages -> COMPLEX (score=65)."""
        signals = _make_signals(
            target_file_count=6,
            change_type="config",
            security_relevant=True,
            language_count=3,
        )
        router = ModelRouter()
        result = router.classify_complexity(signals)
        # score = 25 (4-8 files) + 10 (config) + 20 (security) + 10 (3 langs) = 65 -> COMPLEX
        self.assertEqual(result, ComplexityTier.COMPLEX)


class TestClassifyComplexityCritical(unittest.TestCase):
    """Tests that verify CRITICAL tier classification."""

    def test_architecture_many_files_security(self) -> None:
        """9+ files, architecture, security -> CRITICAL (score=90)."""
        signals = _make_signals(
            target_file_count=10,
            change_type="architecture",
            security_relevant=True,
            language_count=1,
        )
        router = ModelRouter()
        result = router.classify_complexity(signals)
        # score = 30 (9+ files) + 40 (architecture) + 20 (security) = 90 -> CRITICAL
        self.assertEqual(result, ComplexityTier.CRITICAL)

    def test_architecture_many_files_multi_lang(self) -> None:
        """9+ files, architecture, 3+ languages -> CRITICAL (score=80)."""
        signals = _make_signals(
            target_file_count=12,
            change_type="architecture",
            security_relevant=False,
            language_count=4,
        )
        router = ModelRouter()
        result = router.classify_complexity(signals)
        # score = 30 (9+ files) + 40 (architecture) + 10 (4 langs) = 80 -> CRITICAL
        self.assertEqual(result, ComplexityTier.CRITICAL)

    def test_max_score(self) -> None:
        """All factors at maximum -> CRITICAL (score=100)."""
        signals = _make_signals(
            target_file_count=20,
            change_type="architecture",
            security_relevant=True,
            language_count=5,
        )
        router = ModelRouter()
        result = router.classify_complexity(signals)
        # score = 30 + 40 + 20 + 10 = 100 -> CRITICAL
        self.assertEqual(result, ComplexityTier.CRITICAL)


class TestClassifyComplexityBoundaries(unittest.TestCase):
    """Tests for exact boundary values in the scoring algorithm."""

    def test_score_exactly_15_is_routine(self) -> None:
        """Score of exactly 15 -> ROUTINE."""
        # 5 (1 file) + 10 (config) = 15
        signals = _make_signals(
            target_file_count=1,
            change_type="config",
            language_count=1,
            security_relevant=False,
        )
        router = ModelRouter()
        self.assertEqual(router.classify_complexity(signals), ComplexityTier.ROUTINE)

    def test_score_exactly_40_is_moderate(self) -> None:
        """Score of exactly 40 -> MODERATE."""
        # 15 (2-3 files) + 25 (logic) = 40
        signals = _make_signals(
            target_file_count=2,
            change_type="logic",
            language_count=1,
            security_relevant=False,
        )
        router = ModelRouter()
        self.assertEqual(router.classify_complexity(signals), ComplexityTier.MODERATE)

    def test_score_exactly_70_is_complex(self) -> None:
        """Score of exactly 70 -> COMPLEX."""
        # 30 (9+ files) + 40 (architecture) = 70
        signals = _make_signals(
            target_file_count=9,
            change_type="architecture",
            language_count=1,
            security_relevant=False,
        )
        router = ModelRouter()
        self.assertEqual(router.classify_complexity(signals), ComplexityTier.COMPLEX)

    def test_score_71_is_critical(self) -> None:
        """Score of 71 -> CRITICAL."""
        # 30 (9+ files) + 40 (architecture) + 5 (2 languages) = 75
        signals = _make_signals(
            target_file_count=9,
            change_type="architecture",
            language_count=2,
            security_relevant=False,
        )
        router = ModelRouter()
        self.assertEqual(router.classify_complexity(signals), ComplexityTier.CRITICAL)


class TestClassifyComplexityValidation(unittest.TestCase):
    """Tests for input validation."""

    def test_invalid_change_type_raises(self) -> None:
        """Invalid change_type raises ValueError."""
        signals = _make_signals(change_type="invalid")
        router = ModelRouter()
        with self.assertRaises(ValueError) as ctx:
            router.classify_complexity(signals)
        self.assertIn("invalid", str(ctx.exception))
        self.assertIn("Must be one of", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
