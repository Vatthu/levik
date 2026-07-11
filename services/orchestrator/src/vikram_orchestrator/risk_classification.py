"""Risk Classification Engine: classifies change sets by risk level.

Evaluates change context (file paths, lines changed, test modifications, etc.)
against multiple risk dimensions and applies the Maximum Risk Principle —
the overall classification is the highest level among all contributing signals.

Supports repository-specific sensitivity rules via `.vikram/risk-patterns.yaml`.

Validates: Requirements 34.1, 34.2, 34.3, 34.4
"""

from __future__ import annotations

import fnmatch
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from pydantic import BaseModel, Field

from vikram_orchestrator.approval_matrix import (
    RISK_LEVEL_ORDER,
    RiskClassification,
)


class RiskPattern(BaseModel):
    """A single file pattern with an associated risk level."""

    pattern: str
    level: str  # "low", "medium", "high", "critical"


class RiskPatternsConfig(BaseModel):
    """Repository-specific risk patterns loaded from .vikram/risk-patterns.yaml."""

    patterns: list[RiskPattern] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Default sensitivity patterns (used when no repo-specific config exists)
# ---------------------------------------------------------------------------

DEFAULT_SENSITIVITY_PATTERNS: list[RiskPattern] = [
    RiskPattern(pattern="**/auth/**", level="critical"),
    RiskPattern(pattern="**/security/**", level="critical"),
    RiskPattern(pattern="**/*.env*", level="critical"),
    RiskPattern(pattern="**/*secret*", level="critical"),
    RiskPattern(pattern="**/*credential*", level="critical"),
    RiskPattern(pattern="**/config/**", level="high"),
    RiskPattern(pattern="**/deploy/**", level="high"),
    RiskPattern(pattern="**/infrastructure/**", level="high"),
    RiskPattern(pattern="**/*.tf", level="high"),
    RiskPattern(pattern="**/migrations/**", level="high"),
]

# ---------------------------------------------------------------------------
# Thresholds for scale-based risk signals
# ---------------------------------------------------------------------------

# Lines changed thresholds
LINES_HIGH_THRESHOLD = 500
LINES_MEDIUM_THRESHOLD = 100

# Files changed thresholds
FILES_HIGH_THRESHOLD = 10
FILES_MEDIUM_THRESHOLD = 5

# Historical failure rate thresholds
FAILURE_RATE_CRITICAL_THRESHOLD = 0.5
FAILURE_RATE_HIGH_THRESHOLD = 0.3
FAILURE_RATE_MEDIUM_THRESHOLD = 0.15


class RiskClassificationEngine:
    """Generates risk classification signals from change context.

    Evaluates multiple dimensions:
    1. File sensitivity (patterns like **/auth/** → critical)
    2. Lines/files changed (>500 lines or >10 files → high)
    3. Test modification flag
    4. Security code flag
    5. Historical failure rate

    Applies the Maximum Risk Principle: the overall risk level is
    the highest level among all applicable signals.
    """

    def __init__(
        self,
        repo_path: Path | None = None,
        patterns: list[RiskPattern] | None = None,
    ) -> None:
        """Initialize the engine.

        Args:
            repo_path: Path to the repository root. If provided, attempts to
                load `.vikram/risk-patterns.yaml` from this path.
            patterns: Explicit patterns to use. If provided, overrides both
                the repo-specific config and the defaults.
        """
        if patterns is not None:
            self._patterns = patterns
        elif repo_path is not None:
            self._patterns = self._load_repo_patterns(repo_path)
        else:
            self._patterns = list(DEFAULT_SENSITIVITY_PATTERNS)

    @staticmethod
    def _load_repo_patterns(repo_path: Path) -> list[RiskPattern]:
        """Load risk patterns from .vikram/risk-patterns.yaml.

        Falls back to default patterns if the file doesn't exist or is invalid.
        """
        config_path = repo_path / ".vikram" / "risk-patterns.yaml"
        if not config_path.exists():
            return list(DEFAULT_SENSITIVITY_PATTERNS)

        try:
            with open(config_path) as f:
                data = yaml.safe_load(f)

            if not isinstance(data, dict):
                return list(DEFAULT_SENSITIVITY_PATTERNS)

            config = RiskPatternsConfig(**data)
            if not config.patterns:
                return list(DEFAULT_SENSITIVITY_PATTERNS)
            return config.patterns

        except Exception:
            # On any parse error, fall back to defaults
            return list(DEFAULT_SENSITIVITY_PATTERNS)

    def classify_change_risk(
        self, change_context: dict[str, Any]
    ) -> RiskClassification:
        """Classify a change set's risk level from change context signals.

        The change_context dict may contain:
            - changed_files: list[str] — file paths that were changed
            - lines_changed: int — total lines changed
            - files_changed: int — number of files changed
            - test_modified: bool — whether test files are modified
            - security_relevant: bool — whether security code is touched
            - historical_failure_rate: float — failure rate (0.0-1.0) for
              similar changes in this repo/tier

        Returns:
            RiskClassification with the overall level (maximum of all signals),
            individual signal scores, and matched file patterns.
        """
        signals: list[dict[str, str]] = []
        matched_patterns: list[str] = []

        # --- Signal 1: File sensitivity ---
        file_signal = self._evaluate_file_sensitivity(
            change_context.get("changed_files", []),
            matched_patterns,
        )
        if file_signal:
            signals.append(file_signal)

        # --- Signal 2: Lines changed ---
        lines_signal = self._evaluate_lines_changed(
            change_context.get("lines_changed", 0)
        )
        if lines_signal:
            signals.append(lines_signal)

        # --- Signal 3: Files changed ---
        files_signal = self._evaluate_files_changed(
            change_context.get("files_changed", 0)
        )
        if files_signal:
            signals.append(files_signal)

        # --- Signal 4: Test modification ---
        test_signal = self._evaluate_test_modification(
            change_context.get("test_modified", False)
        )
        if test_signal:
            signals.append(test_signal)

        # --- Signal 5: Security code ---
        security_signal = self._evaluate_security_code(
            change_context.get("security_relevant", False)
        )
        if security_signal:
            signals.append(security_signal)

        # --- Signal 6: Historical failure rate ---
        history_signal = self._evaluate_historical_failure_rate(
            change_context.get("historical_failure_rate", 0.0)
        )
        if history_signal:
            signals.append(history_signal)

        # --- Apply Maximum Risk Principle ---
        return self._apply_maximum_risk(signals, matched_patterns)

    def _evaluate_file_sensitivity(
        self,
        changed_files: list[str],
        matched_patterns: list[str],
    ) -> dict[str, str] | None:
        """Evaluate file sensitivity using configured patterns.

        Returns the highest-level signal from all matching patterns.
        """
        if not changed_files:
            return None

        max_level = "low"
        any_match = False

        for file_path in changed_files:
            for risk_pattern in self._patterns:
                if self._file_matches_pattern(file_path, risk_pattern.pattern):
                    any_match = True
                    matched_patterns.append(risk_pattern.pattern)
                    level_order = RISK_LEVEL_ORDER.get(risk_pattern.level, 0)
                    if level_order > RISK_LEVEL_ORDER.get(max_level, 0):
                        max_level = risk_pattern.level

        if not any_match:
            return None

        return {"name": "file_sensitivity", "level": max_level}

    @staticmethod
    def _file_matches_pattern(file_path: str, pattern: str) -> bool:
        """Check if a file path matches a glob pattern.

        Supports ** for recursive directory matching.
        """
        path = PurePosixPath(file_path)
        if path.match(pattern):
            return True
        # When pattern starts with **/, also try matching the stripped pattern
        if pattern.startswith("**/"):
            stripped = pattern[3:]
            if path.match(stripped):
                return True
        # fnmatch fallback
        if fnmatch.fnmatch(file_path, pattern):
            return True
        return False

    @staticmethod
    def _evaluate_lines_changed(lines_changed: int) -> dict[str, str] | None:
        """Evaluate risk based on total lines changed."""
        if lines_changed >= LINES_HIGH_THRESHOLD:
            return {"name": "lines_changed", "level": "high"}
        elif lines_changed >= LINES_MEDIUM_THRESHOLD:
            return {"name": "lines_changed", "level": "medium"}
        return None

    @staticmethod
    def _evaluate_files_changed(files_changed: int) -> dict[str, str] | None:
        """Evaluate risk based on number of files changed."""
        if files_changed >= FILES_HIGH_THRESHOLD:
            return {"name": "files_changed", "level": "high"}
        elif files_changed >= FILES_MEDIUM_THRESHOLD:
            return {"name": "files_changed", "level": "medium"}
        return None

    @staticmethod
    def _evaluate_test_modification(test_modified: bool) -> dict[str, str] | None:
        """Evaluate risk from test file modifications.

        Test modifications indicate the change is non-trivial (medium risk)
        since it's altering verification criteria.
        """
        if test_modified:
            return {"name": "test_modification", "level": "medium"}
        return None

    @staticmethod
    def _evaluate_security_code(security_relevant: bool) -> dict[str, str] | None:
        """Evaluate risk from security-relevant code changes."""
        if security_relevant:
            return {"name": "security_code", "level": "critical"}
        return None

    @staticmethod
    def _evaluate_historical_failure_rate(
        failure_rate: float,
    ) -> dict[str, str] | None:
        """Evaluate risk based on historical failure rate for similar changes."""
        if failure_rate >= FAILURE_RATE_CRITICAL_THRESHOLD:
            return {"name": "historical_failure_rate", "level": "critical"}
        elif failure_rate >= FAILURE_RATE_HIGH_THRESHOLD:
            return {"name": "historical_failure_rate", "level": "high"}
        elif failure_rate >= FAILURE_RATE_MEDIUM_THRESHOLD:
            return {"name": "historical_failure_rate", "level": "medium"}
        return None

    @staticmethod
    def _apply_maximum_risk(
        signals: list[dict[str, str]],
        matched_patterns: list[str],
    ) -> RiskClassification:
        """Apply the Maximum Risk Principle across all signals.

        The overall risk level is the highest level among all signals.
        If no signals are provided, defaults to 'low' risk.
        """
        if not signals:
            return RiskClassification(level="low")

        max_level = "low"
        signal_scores: dict[str, float] = {}

        for signal in signals:
            level = signal.get("level", "low")
            name = signal.get("name", "unknown")
            level_order = RISK_LEVEL_ORDER.get(level, 0)
            signal_scores[name] = float(level_order)
            if level_order > RISK_LEVEL_ORDER.get(max_level, 0):
                max_level = level

        # Deduplicate matched_patterns while preserving order
        seen: set[str] = set()
        unique_patterns: list[str] = []
        for p in matched_patterns:
            if p not in seen:
                seen.add(p)
                unique_patterns.append(p)

        return RiskClassification(
            level=max_level,
            signals=signal_scores,
            matched_patterns=unique_patterns,
            total_score=float(RISK_LEVEL_ORDER.get(max_level, 0)),
        )
