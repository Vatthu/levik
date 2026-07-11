"""Knowledge Store — Repository knowledge extraction, persistence, and querying.

Implements persistent learning from completed tasks: build/test commands,
codebase conventions, approach fingerprints, failure patterns, and context
compression for planning prompts.

Validates: Requirements 38.1, 38.2, 38.3, 38.4, 39.1, 39.2, 39.3, 39.4, 41.1, 41.2, 41.3, 41.4, 42.1, 42.2, 42.3, 42.4
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
import uuid
from collections import Counter
from pathlib import Path
from threading import Lock

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Failure Taxonomy Constants (Requirement 41.3)
# ---------------------------------------------------------------------------

FAILURE_CLASSES: tuple[str, ...] = (
    "model_limitation",
    "incorrect_assumption",
    "missing_context",
    "timeout",
    "provider_error",
    "specification_ambiguity",
)
"""Valid failure classification categories for the failure taxonomy.

- model_limitation: The model lacked capability to complete the task.
- incorrect_assumption: The plan was based on a wrong assumption about the codebase.
- missing_context: Required context was not available during execution.
- timeout: The operation exceeded its time limit.
- provider_error: The LLM provider returned an error or was unavailable.
- specification_ambiguity: The task specification was unclear or contradictory.
"""

# Mapping from task context signals to relevant failure classes.
# Used by get_failure_warnings() to filter patterns by relevance.
_CONTEXT_FAILURE_CLASS_MAP: dict[str, list[str]] = {
    "planning": ["incorrect_assumption", "missing_context", "specification_ambiguity"],
    "implementation": ["model_limitation", "incorrect_assumption", "missing_context"],
    "verification": ["timeout", "missing_context", "incorrect_assumption"],
    "review": ["model_limitation", "specification_ambiguity"],
    "provider_call": ["provider_error", "timeout", "model_limitation"],
}


# ---------------------------------------------------------------------------
# TF-IDF Helpers (Requirement 42.3)
# ---------------------------------------------------------------------------

# Pattern to split identifiers into words (camelCase, snake_case, PascalCase)
_IDENTIFIER_SPLIT_RE = re.compile(r"[_\-./\s]+")
_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _tokenize_text(text: str) -> list[str]:
    """Tokenize text into lowercase words, splitting on common code patterns."""
    # First split on separators (underscore, dash, dot, slash, whitespace)
    parts = _IDENTIFIER_SPLIT_RE.split(text)
    # Then split each part on camelCase boundaries
    tokens: list[str] = []
    for part in parts:
        sub_parts = _CAMEL_SPLIT_RE.split(part)
        for word in sub_parts:
            word = word.strip("()[]{}:;,.'\"").lower()
            if len(word) >= 2:
                tokens.append(word)
    return tokens


def _compute_tfidf_scores(
    documents: dict[str, str],
    query_terms: list[str],
) -> dict[str, float]:
    """Compute TF-IDF relevance scores for documents against query terms.

    Uses term frequency (TF) within each document and inverse document
    frequency (IDF) across all documents to rank relevance.

    Args:
        documents: Mapping of document_id -> document_text.
        query_terms: List of query tokens to score against.

    Returns:
        Mapping of document_id -> TF-IDF relevance score.
    """
    if not documents or not query_terms:
        return {}

    num_docs = len(documents)
    query_term_set = set(query_terms)

    # Compute document frequency (DF) for each query term
    doc_freq: Counter[str] = Counter()
    doc_tokens: dict[str, list[str]] = {}

    for doc_id, text in documents.items():
        tokens = _tokenize_text(text)
        doc_tokens[doc_id] = tokens
        unique_tokens = set(tokens)
        for term in query_term_set:
            if term in unique_tokens:
                doc_freq[term] += 1

    # Compute IDF: log(N / (1 + df)) to avoid division by zero
    idf: dict[str, float] = {}
    for term in query_term_set:
        df = doc_freq.get(term, 0)
        idf[term] = math.log((num_docs + 1) / (1 + df))

    # Compute TF-IDF score for each document
    scores: dict[str, float] = {}
    for doc_id, tokens in doc_tokens.items():
        if not tokens:
            scores[doc_id] = 0.0
            continue

        # Term frequency: count / total_tokens in document
        token_count = Counter(tokens)
        total_tokens = len(tokens)

        score = 0.0
        for term in query_term_set:
            tf = token_count.get(term, 0) / total_tokens
            score += tf * idf.get(term, 0.0)

        scores[doc_id] = score

    return scores


# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------


class RepoKnowledge(BaseModel):
    """Accumulated knowledge about a specific repository."""

    repo_path: str
    build_commands: list[str]
    test_commands: list[str]
    lint_config: str
    directory_conventions: dict[str, str]
    naming_patterns: list[str]
    common_imports: list[str]
    framework_usage: list[str]
    pitfalls: list[str]
    familiarity_score: float
    last_updated: float


class ApproachFingerprint(BaseModel):
    """Record of an engineering approach and its outcome."""

    task_id: str
    repo_path: str
    task_type: str
    complexity_tier: str
    plan_structure: str  # hash of plan skeleton
    models_used: list[str]
    verification_strategy: str
    iteration_count: int
    outcome: str  # "success", "partial", "failure", "rollback"
    cost_efficiency: float  # forecast / actual
    time_efficiency: float


class FailurePattern(BaseModel):
    """A recognized recurring failure pattern."""

    pattern_id: str
    repo_path: str
    failure_class: str  # "model_limitation", "incorrect_assumption", etc.
    error_signature: str  # normalized error fingerprint
    frequency: int
    last_seen: float
    successful_alternatives: list[str]


class ContextCompression(BaseModel):
    """Compressed repository context for planning prompts."""

    repo_path: str
    module_graph: dict[str, list[str]]  # module -> dependencies
    interface_summaries: dict[str, str]  # file -> public interface summary
    relevance_ranked_excerpts: list[tuple[str, str, float]]  # (path, excerpt, relevance)


class ApproachEffectiveness(BaseModel):
    """Effectiveness scoring for a group of approaches.

    Validates: Requirement 39.3
    """

    repo_path: str
    task_type: str
    complexity_tier: str
    total_records: int
    success_rate: float  # fraction of successful outcomes
    cost_efficiency: float  # mean(cost_efficiency) capped at 1.5
    time_efficiency: float  # mean(time_efficiency) capped at 2.0
    first_pass_rate: float  # fraction with iteration_count == 1
    composite_score: float  # weighted composite effectiveness score


# ---------------------------------------------------------------------------
# SQLite Schema
# ---------------------------------------------------------------------------

_REPO_KNOWLEDGE_SCHEMA = """\
CREATE TABLE IF NOT EXISTS repo_knowledge (
    repo_path TEXT PRIMARY KEY,
    build_commands TEXT NOT NULL DEFAULT '[]',
    test_commands TEXT NOT NULL DEFAULT '[]',
    lint_config TEXT NOT NULL DEFAULT '',
    directory_conventions TEXT NOT NULL DEFAULT '{}',
    naming_patterns TEXT NOT NULL DEFAULT '[]',
    common_imports TEXT NOT NULL DEFAULT '[]',
    framework_usage TEXT NOT NULL DEFAULT '[]',
    pitfalls TEXT NOT NULL DEFAULT '[]',
    familiarity_score REAL NOT NULL DEFAULT 0.0,
    last_updated REAL NOT NULL
);
"""

_APPROACH_RECORDS_SCHEMA = """\
CREATE TABLE IF NOT EXISTS approach_records (
    task_id TEXT PRIMARY KEY,
    repo_path TEXT NOT NULL,
    task_type TEXT NOT NULL,
    complexity_tier TEXT NOT NULL,
    plan_structure TEXT NOT NULL DEFAULT '',
    models_used TEXT NOT NULL DEFAULT '[]',
    verification_strategy TEXT NOT NULL DEFAULT '',
    iteration_count INTEGER NOT NULL DEFAULT 1,
    outcome TEXT NOT NULL,
    cost_efficiency REAL NOT NULL DEFAULT 1.0,
    time_efficiency REAL NOT NULL DEFAULT 1.0,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_approach_repo ON approach_records(repo_path);
CREATE INDEX IF NOT EXISTS idx_approach_type_tier ON approach_records(task_type, complexity_tier);
"""

_FAILURE_PATTERNS_SCHEMA = """\
CREATE TABLE IF NOT EXISTS failure_patterns (
    pattern_id TEXT PRIMARY KEY,
    repo_path TEXT NOT NULL,
    failure_class TEXT NOT NULL,
    error_signature TEXT NOT NULL,
    frequency INTEGER NOT NULL DEFAULT 1,
    last_seen REAL NOT NULL,
    successful_alternatives TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_failure_repo ON failure_patterns(repo_path);
CREATE INDEX IF NOT EXISTS idx_failure_class ON failure_patterns(failure_class);
"""

_STRUCTURAL_SUMMARIES_SCHEMA = """\
CREATE TABLE IF NOT EXISTS structural_summaries (
    repo_path TEXT PRIMARY KEY,
    module_graph TEXT NOT NULL DEFAULT '{}',
    interface_summaries TEXT NOT NULL DEFAULT '{}',
    last_generated REAL NOT NULL,
    file_count_at_generation INTEGER NOT NULL DEFAULT 0
);
"""

_FAMILIARITY_FILES_SCHEMA = """\
CREATE TABLE IF NOT EXISTS familiarity_files (
    repo_path TEXT NOT NULL,
    file_path TEXT NOT NULL,
    first_touched REAL NOT NULL,
    last_touched REAL NOT NULL,
    PRIMARY KEY (repo_path, file_path)
);

CREATE INDEX IF NOT EXISTS idx_familiarity_repo ON familiarity_files(repo_path);
"""

_FAMILIARITY_META_SCHEMA = """\
CREATE TABLE IF NOT EXISTS familiarity_meta (
    repo_path TEXT PRIMARY KEY,
    total_files INTEGER NOT NULL DEFAULT 0,
    completed_tasks INTEGER NOT NULL DEFAULT 0,
    last_task_timestamp REAL NOT NULL DEFAULT 0.0
);
"""


# ---------------------------------------------------------------------------
# KnowledgeStore class
# ---------------------------------------------------------------------------


class KnowledgeStore:
    """SQLite-backed knowledge store for repository learning.

    Extracts and persists knowledge from completed tasks including build commands,
    test commands, conventions, pitfalls, approach fingerprints, and failure patterns.

    Validates: Requirements 38.1, 38.2, 38.3, 38.4
    """

    def __init__(self, db_path: str | None = None) -> None:
        """Initialize the knowledge store.

        Args:
            db_path: Path to the SQLite database file.
                     Uses ":memory:" if None (for testing).
        """
        self._db_path = db_path or ":memory:"
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = Lock()
        self._create_schema()

    def _create_schema(self) -> None:
        """Create all knowledge store tables if they don't exist."""
        self._conn.executescript(
            _REPO_KNOWLEDGE_SCHEMA
            + _APPROACH_RECORDS_SCHEMA
            + _FAILURE_PATTERNS_SCHEMA
            + _STRUCTURAL_SUMMARIES_SCHEMA
            + _FAMILIARITY_FILES_SCHEMA
            + _FAMILIARITY_META_SCHEMA
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Core Methods
    # ------------------------------------------------------------------

    def extract_from_task(
        self,
        task_id: str,
        repo_path: str,
        outcome: str,
        *,
        build_commands: list[str] | None = None,
        test_commands: list[str] | None = None,
        lint_config: str | None = None,
        directory_conventions: dict[str, str] | None = None,
        naming_patterns: list[str] | None = None,
        common_imports: list[str] | None = None,
        framework_usage: list[str] | None = None,
        pitfalls: list[str] | None = None,
        task_type: str = "general",
        complexity_tier: str = "moderate",
        plan_structure: str = "",
        models_used: list[str] | None = None,
        verification_strategy: str = "",
        iteration_count: int = 1,
        cost_efficiency: float = 1.0,
        time_efficiency: float = 1.0,
    ) -> None:
        """Extract and persist knowledge from a completed task.

        Updates repo_knowledge with any new build/test commands, conventions,
        and pitfalls discovered. Records the approach fingerprint for future
        planning reference.

        Args:
            task_id: Unique identifier of the completed task.
            repo_path: Path to the repository the task targeted.
            outcome: Task outcome — "success", "partial", "failure", "rollback".
            build_commands: Discovered build commands for the repo.
            test_commands: Discovered test commands for the repo.
            lint_config: Lint configuration path or content identifier.
            directory_conventions: Mapping of directory purposes (e.g., "src" -> "source code").
            naming_patterns: Observed naming conventions (e.g., "snake_case for modules").
            common_imports: Frequently used import paths.
            framework_usage: Frameworks/libraries used in the repo.
            pitfalls: Known gotchas discovered during the task.
            task_type: Classification of the task (bugfix, feature, refactor, etc.).
            complexity_tier: Complexity tier (routine, moderate, complex, critical).
            plan_structure: Hash or summary of the plan structure used.
            models_used: Models used during the task.
            verification_strategy: Verification approach used.
            iteration_count: Number of implementation iterations needed.
            cost_efficiency: Forecast cost / actual cost ratio.
            time_efficiency: Time limit / actual duration ratio.
        """
        now = time.time()

        with self._lock:
            # Update repo knowledge (merge new info with existing)
            self._upsert_repo_knowledge(
                repo_path=repo_path,
                build_commands=build_commands or [],
                test_commands=test_commands or [],
                lint_config=lint_config or "",
                directory_conventions=directory_conventions or {},
                naming_patterns=naming_patterns or [],
                common_imports=common_imports or [],
                framework_usage=framework_usage or [],
                pitfalls=pitfalls or [],
                now=now,
            )

            # Record approach fingerprint
            self._conn.execute(
                "INSERT OR REPLACE INTO approach_records "
                "(task_id, repo_path, task_type, complexity_tier, plan_structure, "
                "models_used, verification_strategy, iteration_count, outcome, "
                "cost_efficiency, time_efficiency, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    repo_path,
                    task_type,
                    complexity_tier,
                    plan_structure,
                    json.dumps(models_used or []),
                    verification_strategy,
                    iteration_count,
                    outcome,
                    cost_efficiency,
                    time_efficiency,
                    now,
                ),
            )
            self._conn.commit()

    def _upsert_repo_knowledge(
        self,
        repo_path: str,
        build_commands: list[str],
        test_commands: list[str],
        lint_config: str,
        directory_conventions: dict[str, str],
        naming_patterns: list[str],
        common_imports: list[str],
        framework_usage: list[str],
        pitfalls: list[str],
        now: float,
    ) -> None:
        """Merge new knowledge into existing repo_knowledge record."""
        # Fetch existing knowledge
        cursor = self._conn.execute(
            "SELECT build_commands, test_commands, lint_config, "
            "directory_conventions, naming_patterns, common_imports, "
            "framework_usage, pitfalls, familiarity_score "
            "FROM repo_knowledge WHERE repo_path = ?",
            (repo_path,),
        )
        row = cursor.fetchone()

        if row is not None:
            # Merge with existing — deduplicate lists, merge dicts
            existing_build = json.loads(row[0])
            existing_test = json.loads(row[1])
            existing_lint = row[2]
            existing_conventions = json.loads(row[3])
            existing_naming = json.loads(row[4])
            existing_imports = json.loads(row[5])
            existing_frameworks = json.loads(row[6])
            existing_pitfalls = json.loads(row[7])
            existing_familiarity = row[8]

            merged_build = _merge_list(existing_build, build_commands)
            merged_test = _merge_list(existing_test, test_commands)
            merged_lint = lint_config if lint_config else existing_lint
            merged_conventions = {**existing_conventions, **directory_conventions}
            merged_naming = _merge_list(existing_naming, naming_patterns)
            merged_imports = _merge_list(existing_imports, common_imports)
            merged_frameworks = _merge_list(existing_frameworks, framework_usage)
            merged_pitfalls = _merge_list(existing_pitfalls, pitfalls)
            # Increment familiarity slightly for each task
            new_familiarity = min(existing_familiarity + 0.05, 1.0)

            self._conn.execute(
                "UPDATE repo_knowledge SET "
                "build_commands = ?, test_commands = ?, lint_config = ?, "
                "directory_conventions = ?, naming_patterns = ?, "
                "common_imports = ?, framework_usage = ?, pitfalls = ?, "
                "familiarity_score = ?, last_updated = ? "
                "WHERE repo_path = ?",
                (
                    json.dumps(merged_build),
                    json.dumps(merged_test),
                    merged_lint,
                    json.dumps(merged_conventions),
                    json.dumps(merged_naming),
                    json.dumps(merged_imports),
                    json.dumps(merged_frameworks),
                    json.dumps(merged_pitfalls),
                    new_familiarity,
                    now,
                    repo_path,
                ),
            )
        else:
            # Insert new record
            initial_familiarity = 0.05 if build_commands or test_commands else 0.0
            self._conn.execute(
                "INSERT INTO repo_knowledge "
                "(repo_path, build_commands, test_commands, lint_config, "
                "directory_conventions, naming_patterns, common_imports, "
                "framework_usage, pitfalls, familiarity_score, last_updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    repo_path,
                    json.dumps(build_commands),
                    json.dumps(test_commands),
                    lint_config,
                    json.dumps(directory_conventions),
                    json.dumps(naming_patterns),
                    json.dumps(common_imports),
                    json.dumps(framework_usage),
                    json.dumps(pitfalls),
                    initial_familiarity,
                    now,
                ),
            )

    def get_repo_knowledge(self, repo_path: str) -> RepoKnowledge | None:
        """Retrieve accumulated knowledge for a repository.

        Args:
            repo_path: Path to the repository.

        Returns:
            RepoKnowledge model or None if no knowledge exists.
        """
        with self._lock:
            cursor = self._conn.execute(
                "SELECT repo_path, build_commands, test_commands, lint_config, "
                "directory_conventions, naming_patterns, common_imports, "
                "framework_usage, pitfalls, familiarity_score, last_updated "
                "FROM repo_knowledge WHERE repo_path = ?",
                (repo_path,),
            )
            row = cursor.fetchone()

        if row is None:
            return None

        return RepoKnowledge(
            repo_path=row[0],
            build_commands=json.loads(row[1]),
            test_commands=json.loads(row[2]),
            lint_config=row[3],
            directory_conventions=json.loads(row[4]),
            naming_patterns=json.loads(row[5]),
            common_imports=json.loads(row[6]),
            framework_usage=json.loads(row[7]),
            pitfalls=json.loads(row[8]),
            familiarity_score=row[9],
            last_updated=row[10],
        )

    # ------------------------------------------------------------------
    # Stub Methods (for later tasks)
    # ------------------------------------------------------------------

    def get_relevant_approaches(
        self,
        repo_path: str,
        task_type: str,
        tier: str,
        limit: int = 5,
    ) -> list[ApproachFingerprint]:
        """Get relevant successful approaches for planning reference.

        Retrieves approach fingerprints matching the repository, task type,
        and complexity tier, ordered by most recent first.

        Validates: Requirements 39.1, 39.2

        Args:
            repo_path: Target repository path.
            task_type: Type of task being planned.
            tier: Complexity tier of the task.
            limit: Maximum number of approaches to return.

        Returns:
            List of matching approach fingerprints.
        """
        with self._lock:
            cursor = self._conn.execute(
                "SELECT task_id, repo_path, task_type, complexity_tier, "
                "plan_structure, models_used, verification_strategy, "
                "iteration_count, outcome, cost_efficiency, time_efficiency "
                "FROM approach_records "
                "WHERE repo_path = ? AND task_type = ? AND complexity_tier = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (repo_path, task_type, tier, limit),
            )
            rows = cursor.fetchall()

        return [
            ApproachFingerprint(
                task_id=row[0],
                repo_path=row[1],
                task_type=row[2],
                complexity_tier=row[3],
                plan_structure=row[4],
                models_used=json.loads(row[5]),
                verification_strategy=row[6],
                iteration_count=row[7],
                outcome=row[8],
                cost_efficiency=row[9],
                time_efficiency=row[10],
            )
            for row in rows
        ]

    def get_approach_effectiveness(
        self,
        repo_path: str | None = None,
        task_type: str | None = None,
        tier: str | None = None,
        limit: int = 50,
    ) -> list[ApproachEffectiveness]:
        """Compute approach effectiveness scores from stored approach records.

        Computes effectiveness from: success rate, cost efficiency (actual vs
        forecast), time efficiency, and verification pass rate on first attempt.

        The composite score uses the design formula:
            0.40 * success_rate +
            0.20 * min(cost_efficiency, 1.5) / 1.5 +
            0.15 * min(time_efficiency, 2.0) / 2.0 +
            0.25 * first_pass_rate

        Validates: Requirements 39.3, 39.4

        Args:
            repo_path: Optional filter by repository path.
            task_type: Optional filter by task type.
            tier: Optional filter by complexity tier.
            limit: Maximum number of records to consider per group.

        Returns:
            List of ApproachEffectiveness scores, one per unique
            (repo_path, task_type, complexity_tier) combination.
        """
        with self._lock:
            # Build query with optional filters
            conditions: list[str] = []
            params: list[str | int] = []

            if repo_path is not None:
                conditions.append("repo_path = ?")
                params.append(repo_path)
            if task_type is not None:
                conditions.append("task_type = ?")
                params.append(task_type)
            if tier is not None:
                conditions.append("complexity_tier = ?")
                params.append(tier)

            where_clause = ""
            if conditions:
                where_clause = "WHERE " + " AND ".join(conditions)

            # Get all relevant records grouped by (repo_path, task_type, complexity_tier)
            query = (
                "SELECT repo_path, task_type, complexity_tier, "
                "outcome, cost_efficiency, time_efficiency, iteration_count "
                f"FROM approach_records {where_clause} "
                "ORDER BY created_at DESC"
            )
            cursor = self._conn.execute(query, params)
            rows = cursor.fetchall()

        # Group records by (repo_path, task_type, complexity_tier)
        groups: dict[tuple[str, str, str], list[tuple[str, float, float, int]]] = {}
        for row in rows:
            key = (row[0], row[1], row[2])
            if key not in groups:
                groups[key] = []
            if len(groups[key]) < limit:
                groups[key].append((row[3], row[4], row[5], row[6]))

        results: list[ApproachEffectiveness] = []
        for (g_repo, g_type, g_tier), records in groups.items():
            if not records:
                continue

            total = len(records)

            # Success rate: fraction of outcomes == "success"
            success_count = sum(1 for r in records if r[0] == "success")
            success_rate = success_count / total

            # Cost efficiency: mean of cost_efficiency values, capped at 1.5
            raw_cost_eff = sum(r[1] for r in records) / total
            cost_efficiency = min(raw_cost_eff, 1.5)

            # Time efficiency: mean of time_efficiency values, capped at 2.0
            raw_time_eff = sum(r[2] for r in records) / total
            time_efficiency = min(raw_time_eff, 2.0)

            # First-pass rate: fraction with iteration_count == 1
            first_pass_count = sum(1 for r in records if r[3] == 1)
            first_pass_rate = first_pass_count / total

            # Composite score (design formula)
            composite = (
                0.40 * success_rate
                + 0.20 * cost_efficiency / 1.5
                + 0.15 * time_efficiency / 2.0
                + 0.25 * first_pass_rate
            )

            results.append(
                ApproachEffectiveness(
                    repo_path=g_repo,
                    task_type=g_type,
                    complexity_tier=g_tier,
                    total_records=total,
                    success_rate=success_rate,
                    cost_efficiency=cost_efficiency,
                    time_efficiency=time_efficiency,
                    first_pass_rate=first_pass_rate,
                    composite_score=composite,
                )
            )

        return results

    def get_failure_warnings(
        self,
        repo_path: str,
        context: dict | None = None,
    ) -> list[FailurePattern]:
        """Get known failure patterns for a repository.

        Returns failure patterns sorted by frequency (most common first).
        When context is provided, filters patterns to those relevant to the
        current task context (e.g., work phase, failure class filter).

        Context keys supported:
            - work_phase: str — filters to failure classes relevant to the phase
              (planning, implementation, verification, review, provider_call)
            - failure_class: str — filters to a specific failure class
            - min_frequency: int — only return patterns seen at least N times

        Args:
            repo_path: Target repository path.
            context: Optional context dict for filtering relevance.

        Returns:
            List of failure patterns for the repository, filtered by context.

        Validates: Requirements 41.2, 41.4
        """
        with self._lock:
            cursor = self._conn.execute(
                "SELECT pattern_id, repo_path, failure_class, error_signature, "
                "frequency, last_seen, successful_alternatives "
                "FROM failure_patterns "
                "WHERE repo_path = ? "
                "ORDER BY frequency DESC",
                (repo_path,),
            )
            rows = cursor.fetchall()

        patterns = [
            FailurePattern(
                pattern_id=row[0],
                repo_path=row[1],
                failure_class=row[2],
                error_signature=row[3],
                frequency=row[4],
                last_seen=row[5],
                successful_alternatives=json.loads(row[6]),
            )
            for row in rows
        ]

        if context is None:
            return patterns

        # Filter by work_phase: only include failure classes relevant to phase
        work_phase = context.get("work_phase")
        if work_phase and work_phase in _CONTEXT_FAILURE_CLASS_MAP:
            relevant_classes = _CONTEXT_FAILURE_CLASS_MAP[work_phase]
            patterns = [p for p in patterns if p.failure_class in relevant_classes]

        # Filter by specific failure_class
        failure_class = context.get("failure_class")
        if failure_class:
            patterns = [p for p in patterns if p.failure_class == failure_class]

        # Filter by minimum frequency threshold
        min_frequency = context.get("min_frequency")
        if min_frequency is not None:
            patterns = [p for p in patterns if p.frequency >= min_frequency]

        return patterns

    def get_compressed_context(
        self,
        repo_path: str,
        objective: str,
        max_tokens: int = 4000,
    ) -> ContextCompression:
        """Get compressed codebase context for planning prompts.

        Generates a compressed representation that fits within max_tokens.
        Token counting approximation: len(full_serialized_text) // 4.

        The output token count MUST never exceed max_tokens.

        Strategy:
        1. Include module dependency graph entries (truncated if needed)
        2. Include interface summaries ranked by TF-IDF relevance to objective
        3. Include relevance-ranked excerpts derived from interface summaries
           with their TF-IDF scores
        All are added incrementally until the token budget is exhausted.

        Uses TF-IDF relevance ranking on function/class names within interface
        summaries to prioritize the most relevant files for the current objective.

        Validates: Requirements 42.1, 42.2, 42.3, 42.4

        Args:
            repo_path: Target repository path.
            objective: Task objective for relevance ranking.
            max_tokens: Maximum token budget for the compressed context.

        Returns:
            ContextCompression model (empty if no summaries exist or budget is 0).
        """
        with self._lock:
            cursor = self._conn.execute(
                "SELECT module_graph, interface_summaries "
                "FROM structural_summaries WHERE repo_path = ?",
                (repo_path,),
            )
            row = cursor.fetchone()

        if row is None:
            return ContextCompression(
                repo_path=repo_path,
                module_graph={},
                interface_summaries={},
                relevance_ranked_excerpts=[],
            )

        full_module_graph: dict[str, list[str]] = json.loads(row[0])
        full_interface_summaries: dict[str, str] = json.loads(row[1])

        # Build output within token budget
        result = ContextCompression(
            repo_path=repo_path,
            module_graph={},
            interface_summaries={},
            relevance_ranked_excerpts=[],
        )

        # We accumulate text parts and check the token count of the full output.
        # Token approximation: len(full_text) // 4
        parts: list[str] = []

        # Add module graph entries within budget
        for module, deps in full_module_graph.items():
            entry_text = f"{module}: {', '.join(deps)}"
            candidate_parts = parts + [entry_text]
            candidate_tokens = len("\n".join(candidate_parts)) // 4
            if candidate_tokens > max_tokens:
                break
            result.module_graph[module] = deps
            parts.append(entry_text)

        # Rank interface summaries using TF-IDF on function/class names
        objective_tokens = _tokenize_text(objective)

        if full_interface_summaries and objective_tokens:
            tfidf_scores = _compute_tfidf_scores(full_interface_summaries, objective_tokens)
            ranked_summaries = sorted(
                full_interface_summaries.items(),
                key=lambda item: tfidf_scores.get(item[0], 0.0),
                reverse=True,
            )
        else:
            # Fallback: simple word overlap when no objective tokens available
            objective_words = set(objective.lower().split())
            ranked_summaries = sorted(
                full_interface_summaries.items(),
                key=lambda item: len(
                    set(item[1].lower().split()) & objective_words
                ),
                reverse=True,
            )
            # Build a fallback score map for excerpt generation
            tfidf_scores = {}
            for file_path, summary in full_interface_summaries.items():
                overlap = len(set(summary.lower().split()) & objective_words)
                tfidf_scores[file_path] = float(overlap)

        for file_path, summary in ranked_summaries:
            candidate_parts = parts + [summary]
            candidate_tokens = len("\n".join(candidate_parts)) // 4
            if candidate_tokens > max_tokens:
                break
            result.interface_summaries[file_path] = summary
            parts.append(summary)

        # Add relevance-ranked excerpts from remaining summaries not already
        # included as full interface summaries. These are shorter excerpts
        # with their TF-IDF relevance scores for prioritized context injection.
        included_files = set(result.interface_summaries.keys())
        remaining_summaries = [
            (fp, summary)
            for fp, summary in ranked_summaries
            if fp not in included_files
        ]

        for file_path, summary in remaining_summaries:
            score = tfidf_scores.get(file_path, 0.0)
            if score <= 0.0:
                continue
            # Use a truncated excerpt (first 120 chars) for space efficiency
            excerpt = summary[:120] if len(summary) > 120 else summary
            candidate_parts = parts + [excerpt]
            candidate_tokens = len("\n".join(candidate_parts)) // 4
            if candidate_tokens > max_tokens:
                break
            result.relevance_ranked_excerpts.append((file_path, excerpt, score))
            parts.append(excerpt)

        return result

    def regenerate_structural_summary(
        self,
        repo_path: str,
        files: dict[str, str],
    ) -> None:
        """Regenerate the module dependency graph and interface summaries for a repo.

        Rebuilds the structural summaries from the provided file contents. This
        should be called when >20% of tracked files have changed since last generation.

        The method extracts:
        - Module dependency graph: import relationships between modules
        - Interface summaries: public function/class signatures per file

        Validates: Requirements 42.2, 42.4

        Args:
            repo_path: Target repository path.
            files: Mapping of file_path -> file_content for all tracked files.
        """
        module_graph: dict[str, list[str]] = {}
        interface_summaries: dict[str, str] = {}

        for file_path, content in files.items():
            # Extract module name from file path
            module_name = file_path.replace("/", ".").replace("\\", ".")
            if module_name.endswith(".py"):
                module_name = module_name[:-3]

            # Extract imports as dependencies
            deps: list[str] = []
            for line in content.split("\n"):
                line = line.strip()
                if line.startswith("import "):
                    # import foo.bar
                    parts = line[7:].strip().split(",")
                    for part in parts:
                        dep = part.strip().split(" as ")[0].strip()
                        if dep:
                            deps.append(dep)
                elif line.startswith("from "):
                    # from foo.bar import baz
                    match = re.match(r"from\s+(\S+)\s+import", line)
                    if match:
                        deps.append(match.group(1))

            if deps:
                module_graph[module_name] = deps

            # Extract public interface (function and class signatures)
            signatures: list[str] = []
            for line in content.split("\n"):
                stripped = line.strip()
                if stripped.startswith("def ") and not stripped.startswith("def _"):
                    # Public function
                    sig_match = re.match(r"(def \w+\([^)]*\))", stripped)
                    if sig_match:
                        signatures.append(sig_match.group(1))
                elif stripped.startswith("class ") and not stripped.startswith("class _"):
                    # Public class
                    class_match = re.match(r"(class \w+(?:\([^)]*\))?)", stripped)
                    if class_match:
                        signatures.append(class_match.group(1))

            if signatures:
                interface_summaries[file_path] = "; ".join(signatures)

        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO structural_summaries "
                "(repo_path, module_graph, interface_summaries, "
                "last_generated, file_count_at_generation) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    repo_path,
                    json.dumps(module_graph),
                    json.dumps(interface_summaries),
                    now,
                    len(files),
                ),
            )
            self._conn.commit()

    def should_regenerate_summary(
        self,
        repo_path: str,
        current_file_count: int,
        changed_files_count: int,
    ) -> bool:
        """Check if structural summaries should be regenerated.

        Triggers regeneration when >20% of tracked files have changed since
        the last summary generation.

        Validates: Requirement 42.4

        Args:
            repo_path: Target repository path.
            current_file_count: Current total number of files in the repo.
            changed_files_count: Number of files that changed since last generation.

        Returns:
            True if regeneration should be triggered.
        """
        with self._lock:
            cursor = self._conn.execute(
                "SELECT file_count_at_generation FROM structural_summaries "
                "WHERE repo_path = ?",
                (repo_path,),
            )
            row = cursor.fetchone()

        if row is None:
            # No summary exists yet — always regenerate
            return True

        file_count_at_generation = row[0]
        # Use the larger of the two counts as the reference for percentage
        reference_count = max(file_count_at_generation, current_file_count, 1)
        change_ratio = changed_files_count / reference_count

        return change_ratio > 0.20

    def update_familiarity(
        self,
        repo_path: str,
        files_touched: list[str],
        *,
        total_files: int | None = None,
    ) -> None:
        """Update familiarity score using coverage + experience + recency formula.

        Formula from design:
            familiarity = 0.4 * coverage + 0.4 * experience + 0.2 * recency

        Where:
            coverage = unique_files_touched / total_files
            experience = min(completed_tasks / 20, 1.0)
            recency = exponential_decay(days_since_last_task, half_life=30)

        Validates: Requirement 38.2

        Args:
            repo_path: Target repository path.
            files_touched: List of file paths touched in the task.
            total_files: Total number of files in the repository.
                         If not provided, uses previously stored value or defaults
                         to max(unique_files, 1) to avoid division by zero.
        """
        if not files_touched:
            return

        now = time.time()

        with self._lock:
            # Ensure repo_knowledge row exists
            cursor = self._conn.execute(
                "SELECT familiarity_score FROM repo_knowledge WHERE repo_path = ?",
                (repo_path,),
            )
            row = cursor.fetchone()
            if row is None:
                return

            # Record each file touched in familiarity_files
            for file_path in files_touched:
                if not file_path:
                    continue
                self._conn.execute(
                    "INSERT INTO familiarity_files (repo_path, file_path, first_touched, last_touched) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(repo_path, file_path) DO UPDATE SET last_touched = ?",
                    (repo_path, file_path, now, now, now),
                )

            # Update meta: increment completed_tasks and update total_files
            meta_cursor = self._conn.execute(
                "SELECT total_files, completed_tasks FROM familiarity_meta WHERE repo_path = ?",
                (repo_path,),
            )
            meta_row = meta_cursor.fetchone()

            if meta_row is None:
                stored_total = total_files or 0
                completed_tasks = 1
                self._conn.execute(
                    "INSERT INTO familiarity_meta (repo_path, total_files, completed_tasks, last_task_timestamp) "
                    "VALUES (?, ?, ?, ?)",
                    (repo_path, stored_total, completed_tasks, now),
                )
            else:
                stored_total = total_files if total_files is not None else meta_row[0]
                completed_tasks = meta_row[1] + 1
                self._conn.execute(
                    "UPDATE familiarity_meta SET total_files = ?, completed_tasks = ?, "
                    "last_task_timestamp = ? WHERE repo_path = ?",
                    (stored_total, completed_tasks, now, repo_path),
                )

            # Count unique files touched for this repo
            unique_cursor = self._conn.execute(
                "SELECT COUNT(*) FROM familiarity_files WHERE repo_path = ?",
                (repo_path,),
            )
            unique_files = unique_cursor.fetchone()[0]

            # Compute familiarity score using design formula
            new_score = self._compute_familiarity(
                unique_files=unique_files,
                total_files=stored_total,
                completed_tasks=completed_tasks,
                last_task_timestamp=now,
                now=now,
            )

            self._conn.execute(
                "UPDATE repo_knowledge SET familiarity_score = ?, last_updated = ? "
                "WHERE repo_path = ?",
                (new_score, now, repo_path),
            )
            self._conn.commit()

    @staticmethod
    def _compute_familiarity(
        *,
        unique_files: int,
        total_files: int,
        completed_tasks: int,
        last_task_timestamp: float,
        now: float,
    ) -> float:
        """Compute familiarity score using the design formula.

        familiarity = 0.4 * coverage + 0.4 * experience + 0.2 * recency

        Args:
            unique_files: Number of unique files touched in this repo.
            total_files: Total number of files in the repo.
            completed_tasks: Number of completed tasks for this repo.
            last_task_timestamp: Timestamp of the most recent task.
            now: Current timestamp.

        Returns:
            Familiarity score in [0.0, 1.0].
        """
        # Coverage: unique_files / total_files
        effective_total = max(total_files, unique_files, 1)
        coverage = min(unique_files / effective_total, 1.0)

        # Experience: saturates at 20 tasks
        experience = min(completed_tasks / 20.0, 1.0)

        # Recency: exponential decay with half-life of 30 days
        days_since_last = (now - last_task_timestamp) / 86400.0
        half_life = 30.0
        recency = math.exp(-0.693147 * days_since_last / half_life)  # ln(2) ≈ 0.693147

        score = 0.4 * coverage + 0.4 * experience + 0.2 * recency
        return min(max(score, 0.0), 1.0)

    # ------------------------------------------------------------------
    # Failure Pattern Recording
    # ------------------------------------------------------------------

    def record_failure_pattern(
        self,
        repo_path: str,
        failure_class: str,
        error_signature: str,
        successful_alternatives: list[str] | None = None,
    ) -> str:
        """Record or update a failure pattern.

        If a pattern with the same error_signature exists for the repo,
        increments its frequency. Otherwise creates a new pattern.

        Args:
            repo_path: Repository where the failure occurred.
            failure_class: Classification of the failure root cause.
                Must be one of FAILURE_CLASSES: model_limitation,
                incorrect_assumption, missing_context, timeout,
                provider_error, specification_ambiguity.
            error_signature: Normalized error fingerprint.
            successful_alternatives: Known approaches that work around this failure.

        Returns:
            The pattern_id of the recorded/updated pattern.

        Raises:
            ValueError: If failure_class is not a valid taxonomy category.

        Validates: Requirements 41.1, 41.3
        """
        if failure_class not in FAILURE_CLASSES:
            raise ValueError(
                f"Invalid failure_class '{failure_class}'. "
                f"Must be one of: {', '.join(FAILURE_CLASSES)}"
            )

        now = time.time()

        with self._lock:
            # Check for existing pattern with same signature
            cursor = self._conn.execute(
                "SELECT pattern_id, frequency, successful_alternatives "
                "FROM failure_patterns "
                "WHERE repo_path = ? AND error_signature = ?",
                (repo_path, error_signature),
            )
            row = cursor.fetchone()

            if row is not None:
                pattern_id = row[0]
                new_frequency = row[1] + 1
                existing_alts = json.loads(row[2])
                merged_alts = _merge_list(
                    existing_alts, successful_alternatives or []
                )

                self._conn.execute(
                    "UPDATE failure_patterns SET "
                    "frequency = ?, last_seen = ?, successful_alternatives = ? "
                    "WHERE pattern_id = ?",
                    (new_frequency, now, json.dumps(merged_alts), pattern_id),
                )
            else:
                pattern_id = str(uuid.uuid4())
                self._conn.execute(
                    "INSERT INTO failure_patterns "
                    "(pattern_id, repo_path, failure_class, error_signature, "
                    "frequency, last_seen, successful_alternatives) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        pattern_id,
                        repo_path,
                        failure_class,
                        error_signature,
                        1,
                        now,
                        json.dumps(successful_alternatives or []),
                    ),
                )

            self._conn.commit()
            return pattern_id

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _merge_list(existing: list[str], new: list[str]) -> list[str]:
    """Merge two lists, deduplicating while preserving order."""
    seen = set(existing)
    merged = list(existing)
    for item in new:
        if item and item not in seen:
            seen.add(item)
            merged.append(item)
    return merged
