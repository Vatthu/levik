"""Approval Matrix: declarative policy engine for governance routing.

Evaluates change context against priority-ordered rules to determine
approval routing (auto_approve, founder_review, escalate_and_halt).

Includes SQLite-backed confidence score persistence and approval audit trail.

Validates: Requirements 30.1, 30.2, 30.3, 30.4, 30.5, 32.1, 32.2, 32.3, 32.4, 32.5
"""

from __future__ import annotations

import fnmatch
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class RuleConditions(BaseModel):
    """Conditions that must ALL be satisfied for a rule to match."""

    risk_level: list[str] | None = None
    file_patterns: list[str] | None = None  # glob patterns
    max_lines_changed: int | None = None
    max_files_changed: int | None = None
    min_confidence_score: float | None = None
    max_cost_consumed_pct: float | None = None
    repo_path_prefix: list[str] | None = None


class PolicyRule(BaseModel):
    """A single policy rule in the approval matrix."""

    name: str
    priority: int  # lower number = higher priority
    conditions: RuleConditions
    routing: str  # "auto_approve", "founder_review", "escalate_and_halt"


class RiskClassification(BaseModel):
    """Result of risk classification for a change set."""

    level: str  # "low", "medium", "high", "critical"
    signals: dict[str, float] = Field(default_factory=dict)
    matched_patterns: list[str] = Field(default_factory=list)
    total_score: float = 0.0


class ConfidenceScore(BaseModel):
    """Confidence score for a complexity tier + repository combination."""

    complexity_tier: str
    repository: str
    score: float = 0.0
    ceiling: float = 30.0
    last_updated: float = 0.0


class ApprovalPolicyDecision(BaseModel):
    """Result of an approval matrix evaluation."""

    routing: str  # "auto_approve", "founder_review", "escalate_and_halt"
    matched_rule_name: str | None = None
    reason: str


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Confidence score defaults
DEFAULT_INCREMENT: float = 1.0
DEFAULT_DECREMENT: float = 3.0

# Ceiling per complexity tier
TIER_CEILINGS: dict[str, float] = {
    "routine": 30.0,
    "moderate": 40.0,
    "complex": 60.0,
    "critical": 10.0,
}

# Promotion thresholds per complexity tier
# When a score reaches this threshold, approval can be relaxed for the tier.
# "critical" uses infinity — it can never auto-promote.
PROMOTION_THRESHOLDS: dict[str, float] = {
    "routine": 10.0,
    "moderate": 20.0,
    "complex": 50.0,
    "critical": float("inf"),
}

# Risk level ordering for maximum principle
RISK_LEVEL_ORDER: dict[str, int] = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "critical": 3,
}


class ApprovalMatrix:
    """Declarative policy engine that evaluates change context against rules.

    Rules are evaluated in priority order (lower priority number = higher precedence).
    The first rule whose conditions are ALL satisfied determines the routing.
    When no rule matches, defaults to 'founder_review'.
    """

    def __init__(
        self,
        config_path: Path | None = None,
        rules: list[PolicyRule] | None = None,
    ) -> None:
        self._rules: list[PolicyRule] = []
        self._confidence_scores: dict[tuple[str, str], ConfidenceScore] = {}

        if rules is not None:
            self._rules = sorted(rules, key=lambda r: r.priority)
        elif config_path is not None:
            self._load_config(config_path)

    @property
    def rules(self) -> list[PolicyRule]:
        """Return the current rules sorted by priority."""
        return list(self._rules)

    def _load_config(self, config_path: Path) -> None:
        """Load rules from a YAML or JSON configuration file."""
        if not config_path.exists():
            raise FileNotFoundError(
                f"Approval matrix config not found: {config_path}"
            )

        with open(config_path) as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            raise ValueError("Approval matrix config must be a YAML mapping")

        raw_rules = data.get("rules", [])
        if not isinstance(raw_rules, list):
            raise ValueError("'rules' must be a list in approval matrix config")

        parsed_rules: list[PolicyRule] = []
        for raw_rule in raw_rules:
            conditions_data = raw_rule.get("conditions", {})
            if conditions_data is None:
                conditions_data = {}
            conditions = RuleConditions(**conditions_data)
            rule = PolicyRule(
                name=raw_rule["name"],
                priority=raw_rule["priority"],
                conditions=conditions,
                routing=raw_rule["routing"],
            )
            parsed_rules.append(rule)

        self._rules = sorted(parsed_rules, key=lambda r: r.priority)

    def reload(self, config_path: Path) -> tuple[bool, str]:
        """Reload configuration from file. Returns (success, error_message)."""
        try:
            old_rules = self._rules
            self._load_config(config_path)
            return True, ""
        except Exception as e:
            # Retain previous valid configuration on failure
            self._rules = old_rules  # type: ignore[possibly-undefined]
            return False, str(e)

    def evaluate(self, change_context: dict[str, Any]) -> ApprovalPolicyDecision:
        """Evaluate a change context against all rules in priority order.

        Returns the routing decision from the first matching rule.
        Defaults to 'founder_review' when no rule matches.

        The change_context dict may contain:
            - risk_level: str (e.g., "low", "medium", "high", "critical")
            - changed_files: list[str] (file paths that were changed)
            - lines_changed: int (total lines changed)
            - files_changed: int (number of files changed)
            - confidence_score: float (current confidence score)
            - cost_consumed_pct: float (percentage of budget consumed)
            - repo_path: str (repository path)
        """
        for rule in self._rules:
            if self._rule_matches(rule, change_context):
                return ApprovalPolicyDecision(
                    routing=rule.routing,
                    matched_rule_name=rule.name,
                    reason=f"Matched rule '{rule.name}' (priority {rule.priority})",
                )

        return ApprovalPolicyDecision(
            routing="founder_review",
            matched_rule_name=None,
            reason="No rule matched; defaulting to founder_review",
        )

    def _rule_matches(
        self, rule: PolicyRule, change_context: dict[str, Any]
    ) -> bool:
        """Check if ALL conditions of a rule are satisfied by the change context."""
        conditions = rule.conditions

        # Check risk_level
        if conditions.risk_level is not None:
            ctx_risk = change_context.get("risk_level")
            if ctx_risk is None or ctx_risk not in conditions.risk_level:
                return False

        # Check file_patterns (at least one changed file must match at least one pattern)
        if conditions.file_patterns is not None:
            changed_files: list[str] = change_context.get("changed_files", [])
            if not changed_files:
                return False
            if not self._any_file_matches_patterns(
                changed_files, conditions.file_patterns
            ):
                return False

        # Check max_lines_changed
        if conditions.max_lines_changed is not None:
            lines_changed = change_context.get("lines_changed", 0)
            if lines_changed > conditions.max_lines_changed:
                return False

        # Check max_files_changed
        if conditions.max_files_changed is not None:
            files_changed = change_context.get("files_changed", 0)
            if files_changed > conditions.max_files_changed:
                return False

        # Check min_confidence_score
        if conditions.min_confidence_score is not None:
            confidence = change_context.get("confidence_score", 0.0)
            if confidence < conditions.min_confidence_score:
                return False

        # Check max_cost_consumed_pct
        if conditions.max_cost_consumed_pct is not None:
            cost_pct = change_context.get("cost_consumed_pct", 0.0)
            if cost_pct > conditions.max_cost_consumed_pct:
                return False

        # Check repo_path_prefix
        if conditions.repo_path_prefix is not None:
            repo_path = change_context.get("repo_path", "")
            if not any(
                repo_path.startswith(prefix)
                for prefix in conditions.repo_path_prefix
            ):
                return False

        return True

    def _any_file_matches_patterns(
        self, files: list[str], patterns: list[str]
    ) -> bool:
        """Check if any file matches any of the glob patterns.

        Supports ** for recursive directory matching (e.g., '**/auth/**').
        Uses PurePosixPath.match() for pattern matching, with special handling
        for ** prefix patterns to also match files without directory components.
        """
        from pathlib import PurePosixPath

        for file_path in files:
            path = PurePosixPath(file_path)
            for pattern in patterns:
                if path.match(pattern):
                    return True
                # When pattern starts with **/, also try matching without the
                # **/ prefix for files at the root level (e.g., README.md
                # should match **/*.md)
                if pattern.startswith("**/"):
                    stripped = pattern[3:]
                    if path.match(stripped):
                        return True
                # fnmatch fallback for patterns that don't use **
                if fnmatch.fnmatch(file_path, pattern):
                    return True
        return False

    # ------------------------------------------------------------------
    # Confidence Score Management
    # ------------------------------------------------------------------

    def get_confidence(self, tier: str, repo: str) -> ConfidenceScore:
        """Get the confidence score for a tier/repo combination."""
        key = (tier, repo)
        if key not in self._confidence_scores:
            ceiling = TIER_CEILINGS.get(tier, 30.0)
            self._confidence_scores[key] = ConfidenceScore(
                complexity_tier=tier,
                repository=repo,
                score=0.0,
                ceiling=ceiling,
            )
        return self._confidence_scores[key]

    def increment_confidence(
        self, tier: str, repo: str, amount: float = DEFAULT_INCREMENT
    ) -> None:
        """Increment confidence score, capped at the tier ceiling."""
        cs = self.get_confidence(tier, repo)
        cs.score = min(cs.score + amount, cs.ceiling)

    def decrement_confidence(
        self, tier: str, repo: str, amount: float = DEFAULT_DECREMENT
    ) -> None:
        """Decrement confidence score, floored at 0."""
        cs = self.get_confidence(tier, repo)
        cs.score = max(cs.score - amount, 0.0)

    # ------------------------------------------------------------------
    # Risk Classification
    # ------------------------------------------------------------------

    @staticmethod
    def classify_risk(signals: list[dict[str, str]]) -> RiskClassification:
        """Classify overall risk as the MAXIMUM of all applicable risk signals.

        Each signal is a dict with 'name' and 'level' keys.
        If no signals are provided, defaults to 'low' risk.

        Implements the Maximum Risk Principle: the overall risk level is
        the highest level among all applicable signals.
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

        return RiskClassification(
            level=max_level,
            signals=signal_scores,
            total_score=float(RISK_LEVEL_ORDER.get(max_level, 0)),
        )



# ---------------------------------------------------------------------------
# SQLite Confidence Store
# ---------------------------------------------------------------------------

_CONFIDENCE_SCORES_SCHEMA = """
CREATE TABLE IF NOT EXISTS confidence_scores (
    complexity_tier TEXT NOT NULL,
    repository TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0.0,
    ceiling REAL NOT NULL,
    last_updated REAL NOT NULL,
    PRIMARY KEY (complexity_tier, repository)
);
"""

_APPROVAL_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_audit (
    audit_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    timestamp REAL NOT NULL,
    change_context_json TEXT NOT NULL,
    rule_matched TEXT,
    routing_outcome TEXT NOT NULL,
    confidence_at_decision REAL
);
"""


class ConfidenceStore:
    """SQLite-backed persistence for confidence scores and approval audit trail.

    Provides durable storage so confidence scores survive restarts,
    and records every approval evaluation for audit compliance.

    Validates: Requirements 32.1, 32.2, 32.3, 32.4, 32.5, 33.1
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        """Initialize the store and create tables if needed.

        Args:
            db_path: Path to the SQLite database file. Use ":memory:" for testing.
        """
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._create_tables()
        # In-memory cache for fast reads
        self._cache: dict[tuple[str, str], ConfidenceScore] = {}
        self._load_cache()

    def _create_tables(self) -> None:
        """Create the confidence_scores and approval_audit tables."""
        self._conn.executescript(
            _CONFIDENCE_SCORES_SCHEMA + _APPROVAL_AUDIT_SCHEMA
        )
        self._conn.commit()

    def _load_cache(self) -> None:
        """Load all confidence scores from SQLite into memory."""
        cursor = self._conn.execute(
            "SELECT complexity_tier, repository, score, ceiling, last_updated "
            "FROM confidence_scores"
        )
        for row in cursor.fetchall():
            tier, repo, score, ceiling, last_updated = row
            key = (tier, repo)
            self._cache[key] = ConfidenceScore(
                complexity_tier=tier,
                repository=repo,
                score=score,
                ceiling=ceiling,
                last_updated=last_updated,
            )

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    # ------------------------------------------------------------------
    # Confidence Score CRUD
    # ------------------------------------------------------------------

    def get_confidence(self, tier: str, repo: str) -> ConfidenceScore:
        """Get the confidence score for a tier/repo combination.

        If no record exists, creates one with score=0 and the tier's ceiling.
        """
        key = (tier, repo)
        if key not in self._cache:
            ceiling = TIER_CEILINGS.get(tier, 30.0)
            cs = ConfidenceScore(
                complexity_tier=tier,
                repository=repo,
                score=0.0,
                ceiling=ceiling,
                last_updated=time.time(),
            )
            self._persist_score(cs)
            self._cache[key] = cs
        return self._cache[key]

    def increment_confidence(
        self, tier: str, repo: str, amount: float = DEFAULT_INCREMENT
    ) -> None:
        """Increment confidence score, capped at the tier ceiling."""
        cs = self.get_confidence(tier, repo)
        cs.score = min(cs.score + amount, cs.ceiling)
        cs.last_updated = time.time()
        self._persist_score(cs)

    def decrement_confidence(
        self, tier: str, repo: str, amount: float = DEFAULT_DECREMENT
    ) -> None:
        """Decrement confidence score, floored at 0."""
        cs = self.get_confidence(tier, repo)
        cs.score = max(cs.score - amount, 0.0)
        cs.last_updated = time.time()
        self._persist_score(cs)

    def _persist_score(self, cs: ConfidenceScore) -> None:
        """Upsert a confidence score to SQLite."""
        self._conn.execute(
            "INSERT INTO confidence_scores "
            "(complexity_tier, repository, score, ceiling, last_updated) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(complexity_tier, repository) DO UPDATE SET "
            "score=excluded.score, ceiling=excluded.ceiling, last_updated=excluded.last_updated",
            (cs.complexity_tier, cs.repository, cs.score, cs.ceiling, cs.last_updated),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Promotion Threshold Logic
    # ------------------------------------------------------------------

    def check_promotion(self, tier: str, repo: str) -> bool:
        """Check whether the confidence score has reached the promotion threshold.

        Returns True if the tier's score >= the promotion threshold for that tier.
        Critical tier always returns False (threshold is infinity).

        Validates: Requirements 32.3, 32.5
        """
        cs = self.get_confidence(tier, repo)
        threshold = PROMOTION_THRESHOLDS.get(tier, float("inf"))
        return cs.score >= threshold

    # ------------------------------------------------------------------
    # Approval Audit Trail
    # ------------------------------------------------------------------

    def record_audit(
        self,
        task_id: str,
        change_context: dict[str, Any],
        decision: ApprovalPolicyDecision,
        confidence_at_decision: float | None = None,
    ) -> str:
        """Record an approval evaluation to the audit trail.

        Args:
            task_id: The task being evaluated.
            change_context: The full change context dict evaluated.
            decision: The approval routing decision produced.
            confidence_at_decision: The confidence score at decision time.

        Returns:
            The generated audit_id for this record.

        Validates: Requirements 33.1
        """
        audit_id = str(uuid.uuid4())
        timestamp = time.time()
        change_context_json = json.dumps(change_context, default=str)

        self._conn.execute(
            "INSERT INTO approval_audit "
            "(audit_id, task_id, timestamp, change_context_json, "
            "rule_matched, routing_outcome, confidence_at_decision) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                audit_id,
                task_id,
                timestamp,
                change_context_json,
                decision.matched_rule_name,
                decision.routing,
                confidence_at_decision,
            ),
        )
        self._conn.commit()
        return audit_id

    def get_audit_records(
        self,
        task_id: str | None = None,
        routing_outcome: str | None = None,
        time_range_start: float | None = None,
        time_range_end: float | None = None,
        rule: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query audit records with optional filtering.

        Args:
            task_id: Filter by task_id (optional).
            routing_outcome: Filter by routing outcome (optional).
            time_range_start: Filter records with timestamp >= this value (optional).
            time_range_end: Filter records with timestamp <= this value (optional).
            rule: Filter by matched rule name (optional).
            limit: Maximum records to return.

        Returns:
            List of audit record dicts.

        Validates: Requirements 33.2
        """
        query = "SELECT audit_id, task_id, timestamp, change_context_json, rule_matched, routing_outcome, confidence_at_decision FROM approval_audit"
        conditions: list[str] = []
        params: list[Any] = []

        if task_id is not None:
            conditions.append("task_id = ?")
            params.append(task_id)
        if routing_outcome is not None:
            conditions.append("routing_outcome = ?")
            params.append(routing_outcome)
        if time_range_start is not None:
            conditions.append("timestamp >= ?")
            params.append(time_range_start)
        if time_range_end is not None:
            conditions.append("timestamp <= ?")
            params.append(time_range_end)
        if rule is not None:
            conditions.append("rule_matched = ?")
            params.append(rule)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        cursor = self._conn.execute(query, params)
        rows = cursor.fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            results.append({
                "audit_id": row[0],
                "task_id": row[1],
                "timestamp": row[2],
                "change_context_json": row[3],
                "rule_matched": row[4],
                "routing_outcome": row[5],
                "confidence_at_decision": row[6],
            })
        return results
