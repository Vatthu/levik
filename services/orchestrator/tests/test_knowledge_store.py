"""Unit tests for knowledge_store module.

Tests SQLite-backed knowledge extraction, persistence, and querying
for repository learning from completed tasks.

Validates: Requirements 38.1, 38.2, 38.3, 38.4
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from vikram_orchestrator.knowledge_store import (
    ApproachEffectiveness,
    ApproachFingerprint,
    ContextCompression,
    FAILURE_CLASSES,
    FailurePattern,
    KnowledgeStore,
    RepoKnowledge,
    _CONTEXT_FAILURE_CLASS_MAP,
    _merge_list,
)


class TestMergeList(unittest.TestCase):
    """Tests for the _merge_list helper."""

    def test_empty_lists(self) -> None:
        result = _merge_list([], [])
        self.assertEqual(result, [])

    def test_no_overlap(self) -> None:
        result = _merge_list(["a", "b"], ["c", "d"])
        self.assertEqual(result, ["a", "b", "c", "d"])

    def test_with_duplicates(self) -> None:
        result = _merge_list(["a", "b", "c"], ["b", "c", "d"])
        self.assertEqual(result, ["a", "b", "c", "d"])

    def test_preserves_existing_order(self) -> None:
        result = _merge_list(["z", "a", "m"], ["x", "a"])
        self.assertEqual(result, ["z", "a", "m", "x"])

    def test_ignores_empty_strings(self) -> None:
        result = _merge_list(["a"], ["", "b", ""])
        self.assertEqual(result, ["a", "b"])


class TestKnowledgeStoreInit(unittest.TestCase):
    """Tests for KnowledgeStore initialization."""

    def test_creates_in_memory_store(self) -> None:
        store = KnowledgeStore()
        self.assertIsNotNone(store)
        store.close()

    def test_creates_file_backed_store(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        store = KnowledgeStore(db_path=db_path)
        store.close()

        # Verify tables exist
        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = {row[0] for row in cursor.fetchall()}
        conn.close()

        self.assertIn("repo_knowledge", tables)
        self.assertIn("approach_records", tables)
        self.assertIn("failure_patterns", tables)
        self.assertIn("structural_summaries", tables)

    def test_schema_uses_wal_mode(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        store = KnowledgeStore(db_path=db_path)
        # Check WAL mode is set
        cursor = store._conn.execute("PRAGMA journal_mode")
        mode = cursor.fetchone()[0]
        self.assertEqual(mode, "wal")
        store.close()


class TestExtractFromTask(unittest.TestCase):
    """Tests for extract_from_task method."""

    def setUp(self) -> None:
        self.store = KnowledgeStore()

    def tearDown(self) -> None:
        self.store.close()

    def test_extract_creates_repo_knowledge(self) -> None:
        self.store.extract_from_task(
            task_id="task-001",
            repo_path="/repos/myproject",
            outcome="success",
            build_commands=["make build"],
            test_commands=["pytest"],
            pitfalls=["needs migrations before tests"],
        )

        knowledge = self.store.get_repo_knowledge("/repos/myproject")
        self.assertIsNotNone(knowledge)
        self.assertEqual(knowledge.build_commands, ["make build"])
        self.assertEqual(knowledge.test_commands, ["pytest"])
        self.assertEqual(knowledge.pitfalls, ["needs migrations before tests"])

    def test_extract_creates_approach_record(self) -> None:
        self.store.extract_from_task(
            task_id="task-002",
            repo_path="/repos/myproject",
            outcome="success",
            task_type="bugfix",
            complexity_tier="moderate",
            models_used=["gpt-4", "claude-3"],
            verification_strategy="property_tests",
            iteration_count=2,
            cost_efficiency=1.2,
            time_efficiency=0.8,
        )

        approaches = self.store.get_relevant_approaches(
            repo_path="/repos/myproject",
            task_type="bugfix",
            tier="moderate",
        )
        self.assertEqual(len(approaches), 1)
        self.assertEqual(approaches[0].task_id, "task-002")
        self.assertEqual(approaches[0].models_used, ["gpt-4", "claude-3"])
        self.assertEqual(approaches[0].iteration_count, 2)

    def test_extract_merges_knowledge_on_subsequent_tasks(self) -> None:
        # First task discovers some commands
        self.store.extract_from_task(
            task_id="task-001",
            repo_path="/repos/myproject",
            outcome="success",
            build_commands=["make build"],
            test_commands=["pytest"],
        )

        # Second task discovers more
        self.store.extract_from_task(
            task_id="task-002",
            repo_path="/repos/myproject",
            outcome="success",
            build_commands=["make build", "cargo build"],
            test_commands=["pytest", "cargo test"],
            pitfalls=["slow CI"],
        )

        knowledge = self.store.get_repo_knowledge("/repos/myproject")
        self.assertIn("make build", knowledge.build_commands)
        self.assertIn("cargo build", knowledge.build_commands)
        self.assertIn("pytest", knowledge.test_commands)
        self.assertIn("cargo test", knowledge.test_commands)
        self.assertIn("slow CI", knowledge.pitfalls)

    def test_extract_increments_familiarity(self) -> None:
        self.store.extract_from_task(
            task_id="task-001",
            repo_path="/repos/myproject",
            outcome="success",
            build_commands=["make"],
        )
        k1 = self.store.get_repo_knowledge("/repos/myproject")
        score1 = k1.familiarity_score

        self.store.extract_from_task(
            task_id="task-002",
            repo_path="/repos/myproject",
            outcome="success",
        )
        k2 = self.store.get_repo_knowledge("/repos/myproject")
        score2 = k2.familiarity_score

        self.assertGreater(score2, score1)

    def test_extract_with_directory_conventions(self) -> None:
        self.store.extract_from_task(
            task_id="task-001",
            repo_path="/repos/myproject",
            outcome="success",
            directory_conventions={"src": "source code", "tests": "test suite"},
        )

        knowledge = self.store.get_repo_knowledge("/repos/myproject")
        self.assertEqual(knowledge.directory_conventions["src"], "source code")
        self.assertEqual(knowledge.directory_conventions["tests"], "test suite")


class TestGetRepoKnowledge(unittest.TestCase):
    """Tests for get_repo_knowledge method."""

    def setUp(self) -> None:
        self.store = KnowledgeStore()

    def tearDown(self) -> None:
        self.store.close()

    def test_returns_none_for_unknown_repo(self) -> None:
        result = self.store.get_repo_knowledge("/unknown/repo")
        self.assertIsNone(result)

    def test_returns_repo_knowledge_model(self) -> None:
        self.store.extract_from_task(
            task_id="task-001",
            repo_path="/repos/known",
            outcome="success",
            build_commands=["npm run build"],
            framework_usage=["react", "typescript"],
        )

        result = self.store.get_repo_knowledge("/repos/known")
        self.assertIsInstance(result, RepoKnowledge)
        self.assertEqual(result.repo_path, "/repos/known")
        self.assertEqual(result.framework_usage, ["react", "typescript"])


class TestGetRelevantApproaches(unittest.TestCase):
    """Tests for get_relevant_approaches method."""

    def setUp(self) -> None:
        self.store = KnowledgeStore()

    def tearDown(self) -> None:
        self.store.close()

    def test_returns_empty_for_no_matches(self) -> None:
        result = self.store.get_relevant_approaches("/repos/x", "bugfix", "routine")
        self.assertEqual(result, [])

    def test_filters_by_repo_type_tier(self) -> None:
        self.store.extract_from_task(
            task_id="t1", repo_path="/repos/a", outcome="success",
            task_type="bugfix", complexity_tier="moderate",
        )
        self.store.extract_from_task(
            task_id="t2", repo_path="/repos/a", outcome="success",
            task_type="feature", complexity_tier="moderate",
        )
        self.store.extract_from_task(
            task_id="t3", repo_path="/repos/b", outcome="success",
            task_type="bugfix", complexity_tier="moderate",
        )

        result = self.store.get_relevant_approaches("/repos/a", "bugfix", "moderate")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].task_id, "t1")

    def test_respects_limit(self) -> None:
        for i in range(10):
            self.store.extract_from_task(
                task_id=f"t{i}", repo_path="/repos/a", outcome="success",
                task_type="bugfix", complexity_tier="routine",
            )

        result = self.store.get_relevant_approaches(
            "/repos/a", "bugfix", "routine", limit=3
        )
        self.assertEqual(len(result), 3)


class TestGetFailureWarnings(unittest.TestCase):
    """Tests for get_failure_warnings method."""

    def setUp(self) -> None:
        self.store = KnowledgeStore()

    def tearDown(self) -> None:
        self.store.close()

    def test_returns_empty_for_no_patterns(self) -> None:
        result = self.store.get_failure_warnings("/repos/x")
        self.assertEqual(result, [])

    def test_returns_recorded_failures(self) -> None:
        self.store.record_failure_pattern(
            repo_path="/repos/a",
            failure_class="missing_context",
            error_signature="ImportError: no module named X",
            successful_alternatives=["add X to requirements"],
        )

        result = self.store.get_failure_warnings("/repos/a")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].failure_class, "missing_context")
        self.assertEqual(result[0].frequency, 1)
        self.assertEqual(
            result[0].successful_alternatives, ["add X to requirements"]
        )

    def test_ordered_by_frequency(self) -> None:
        # Record same pattern multiple times to increase frequency
        self.store.record_failure_pattern(
            repo_path="/repos/a",
            failure_class="timeout",
            error_signature="TimeoutError",
        )
        self.store.record_failure_pattern(
            repo_path="/repos/a",
            failure_class="timeout",
            error_signature="TimeoutError",
        )
        self.store.record_failure_pattern(
            repo_path="/repos/a",
            failure_class="missing_context",
            error_signature="KeyError: config",
        )

        result = self.store.get_failure_warnings("/repos/a")
        self.assertEqual(len(result), 2)
        # Timeout should come first (frequency=2)
        self.assertEqual(result[0].error_signature, "TimeoutError")
        self.assertEqual(result[0].frequency, 2)


class TestRecordFailurePattern(unittest.TestCase):
    """Tests for record_failure_pattern method."""

    def setUp(self) -> None:
        self.store = KnowledgeStore()

    def tearDown(self) -> None:
        self.store.close()

    def test_creates_new_pattern(self) -> None:
        pattern_id = self.store.record_failure_pattern(
            repo_path="/repos/a",
            failure_class="model_limitation",
            error_signature="context_exceeded",
        )
        self.assertIsNotNone(pattern_id)

        warnings = self.store.get_failure_warnings("/repos/a")
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].pattern_id, pattern_id)

    def test_increments_frequency_on_duplicate_signature(self) -> None:
        self.store.record_failure_pattern(
            repo_path="/repos/a",
            failure_class="timeout",
            error_signature="ConnectionTimeout",
        )
        self.store.record_failure_pattern(
            repo_path="/repos/a",
            failure_class="timeout",
            error_signature="ConnectionTimeout",
        )

        warnings = self.store.get_failure_warnings("/repos/a")
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].frequency, 2)

    def test_merges_successful_alternatives(self) -> None:
        self.store.record_failure_pattern(
            repo_path="/repos/a",
            failure_class="timeout",
            error_signature="ConnectionTimeout",
            successful_alternatives=["retry with backoff"],
        )
        self.store.record_failure_pattern(
            repo_path="/repos/a",
            failure_class="timeout",
            error_signature="ConnectionTimeout",
            successful_alternatives=["use cache", "retry with backoff"],
        )

        warnings = self.store.get_failure_warnings("/repos/a")
        self.assertIn("retry with backoff", warnings[0].successful_alternatives)
        self.assertIn("use cache", warnings[0].successful_alternatives)

    def test_rejects_invalid_failure_class(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.store.record_failure_pattern(
                repo_path="/repos/a",
                failure_class="unknown_class",
                error_signature="some error",
            )
        self.assertIn("unknown_class", str(ctx.exception))
        self.assertIn("Must be one of", str(ctx.exception))

    def test_accepts_all_valid_failure_classes(self) -> None:
        for fc in FAILURE_CLASSES:
            pattern_id = self.store.record_failure_pattern(
                repo_path="/repos/taxonomy",
                failure_class=fc,
                error_signature=f"error_{fc}",
            )
            self.assertIsNotNone(pattern_id)

        warnings = self.store.get_failure_warnings("/repos/taxonomy")
        self.assertEqual(len(warnings), len(FAILURE_CLASSES))


class TestFailureTaxonomy(unittest.TestCase):
    """Tests for failure taxonomy classification (Requirement 41.3)."""

    def test_failure_classes_constant_defined(self) -> None:
        self.assertEqual(len(FAILURE_CLASSES), 6)
        self.assertIn("model_limitation", FAILURE_CLASSES)
        self.assertIn("incorrect_assumption", FAILURE_CLASSES)
        self.assertIn("missing_context", FAILURE_CLASSES)
        self.assertIn("timeout", FAILURE_CLASSES)
        self.assertIn("provider_error", FAILURE_CLASSES)
        self.assertIn("specification_ambiguity", FAILURE_CLASSES)

    def test_context_failure_class_map_covers_phases(self) -> None:
        expected_phases = {"planning", "implementation", "verification", "review", "provider_call"}
        self.assertEqual(set(_CONTEXT_FAILURE_CLASS_MAP.keys()), expected_phases)

    def test_all_mapped_classes_are_valid(self) -> None:
        for phase, classes in _CONTEXT_FAILURE_CLASS_MAP.items():
            for fc in classes:
                self.assertIn(
                    fc, FAILURE_CLASSES,
                    f"Phase '{phase}' maps to invalid class '{fc}'"
                )


class TestGetFailureWarningsWithContext(unittest.TestCase):
    """Tests for context-based filtering in get_failure_warnings (Requirement 41.2)."""

    def setUp(self) -> None:
        self.store = KnowledgeStore()
        # Seed failure patterns across all classes
        self.store.record_failure_pattern(
            repo_path="/repos/ctx",
            failure_class="model_limitation",
            error_signature="context_window_exceeded",
            successful_alternatives=["use smaller model context"],
        )
        self.store.record_failure_pattern(
            repo_path="/repos/ctx",
            failure_class="incorrect_assumption",
            error_signature="assumed_python3.8_but_3.11",
        )
        self.store.record_failure_pattern(
            repo_path="/repos/ctx",
            failure_class="missing_context",
            error_signature="unknown_env_variable",
        )
        self.store.record_failure_pattern(
            repo_path="/repos/ctx",
            failure_class="timeout",
            error_signature="build_timeout_300s",
        )
        self.store.record_failure_pattern(
            repo_path="/repos/ctx",
            failure_class="provider_error",
            error_signature="openai_rate_limited",
        )
        self.store.record_failure_pattern(
            repo_path="/repos/ctx",
            failure_class="specification_ambiguity",
            error_signature="conflicting_requirements",
        )

    def tearDown(self) -> None:
        self.store.close()

    def test_no_context_returns_all(self) -> None:
        result = self.store.get_failure_warnings("/repos/ctx")
        self.assertEqual(len(result), 6)

    def test_filter_by_work_phase_planning(self) -> None:
        result = self.store.get_failure_warnings(
            "/repos/ctx", context={"work_phase": "planning"}
        )
        classes = {p.failure_class for p in result}
        # Planning should include: incorrect_assumption, missing_context, specification_ambiguity
        self.assertEqual(
            classes, {"incorrect_assumption", "missing_context", "specification_ambiguity"}
        )

    def test_filter_by_work_phase_implementation(self) -> None:
        result = self.store.get_failure_warnings(
            "/repos/ctx", context={"work_phase": "implementation"}
        )
        classes = {p.failure_class for p in result}
        self.assertEqual(
            classes, {"model_limitation", "incorrect_assumption", "missing_context"}
        )

    def test_filter_by_work_phase_verification(self) -> None:
        result = self.store.get_failure_warnings(
            "/repos/ctx", context={"work_phase": "verification"}
        )
        classes = {p.failure_class for p in result}
        self.assertEqual(
            classes, {"timeout", "missing_context", "incorrect_assumption"}
        )

    def test_filter_by_work_phase_provider_call(self) -> None:
        result = self.store.get_failure_warnings(
            "/repos/ctx", context={"work_phase": "provider_call"}
        )
        classes = {p.failure_class for p in result}
        self.assertEqual(
            classes, {"provider_error", "timeout", "model_limitation"}
        )

    def test_filter_by_specific_failure_class(self) -> None:
        result = self.store.get_failure_warnings(
            "/repos/ctx", context={"failure_class": "timeout"}
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].failure_class, "timeout")

    def test_filter_by_min_frequency(self) -> None:
        # Record timeout again to bump its frequency to 2
        self.store.record_failure_pattern(
            repo_path="/repos/ctx",
            failure_class="timeout",
            error_signature="build_timeout_300s",
        )
        result = self.store.get_failure_warnings(
            "/repos/ctx", context={"min_frequency": 2}
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].failure_class, "timeout")
        self.assertEqual(result[0].frequency, 2)

    def test_unknown_work_phase_returns_all(self) -> None:
        result = self.store.get_failure_warnings(
            "/repos/ctx", context={"work_phase": "unknown_phase"}
        )
        # Unknown phase should not filter — returns all patterns
        self.assertEqual(len(result), 6)

    def test_combined_filters(self) -> None:
        # Record timeout again
        self.store.record_failure_pattern(
            repo_path="/repos/ctx",
            failure_class="timeout",
            error_signature="build_timeout_300s",
        )
        # Filter by verification phase AND min_frequency=2
        result = self.store.get_failure_warnings(
            "/repos/ctx",
            context={"work_phase": "verification", "min_frequency": 2},
        )
        # Only timeout matches both filters (in verification phase + freq=2)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].failure_class, "timeout")


class TestGetCompressedContext(unittest.TestCase):
    """Tests for get_compressed_context method."""

    def setUp(self) -> None:
        self.store = KnowledgeStore()

    def tearDown(self) -> None:
        self.store.close()

    def test_returns_empty_for_no_summaries(self) -> None:
        result = self.store.get_compressed_context("/repos/a", "fix bug")
        self.assertIsNotNone(result)
        self.assertEqual(result.module_graph, {})
        self.assertEqual(result.interface_summaries, {})

    def test_returns_stored_summaries(self) -> None:
        # Insert a summary directly
        self.store._conn.execute(
            "INSERT INTO structural_summaries "
            "(repo_path, module_graph, interface_summaries, "
            "last_generated, file_count_at_generation) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "/repos/a",
                json.dumps({"main": ["utils", "models"]}),
                json.dumps({"main.py": "def run(): ..."}),
                time.time(),
                10,
            ),
        )
        self.store._conn.commit()

        result = self.store.get_compressed_context("/repos/a", "fix bug")
        self.assertIsNotNone(result)
        self.assertIsInstance(result, ContextCompression)
        self.assertEqual(result.module_graph, {"main": ["utils", "models"]})
        self.assertEqual(result.interface_summaries, {"main.py": "def run(): ..."})


class TestUpdateFamiliarity(unittest.TestCase):
    """Tests for update_familiarity method."""

    def setUp(self) -> None:
        self.store = KnowledgeStore()

    def tearDown(self) -> None:
        self.store.close()

    def test_no_op_for_empty_files(self) -> None:
        self.store.extract_from_task(
            task_id="t1", repo_path="/repos/a", outcome="success",
            build_commands=["make"],
        )
        k1 = self.store.get_repo_knowledge("/repos/a")

        self.store.update_familiarity("/repos/a", [])
        k2 = self.store.get_repo_knowledge("/repos/a")
        self.assertEqual(k1.familiarity_score, k2.familiarity_score)

    def test_increases_score_with_files(self) -> None:
        self.store.extract_from_task(
            task_id="t1", repo_path="/repos/a", outcome="success",
            build_commands=["make"],
        )
        k1 = self.store.get_repo_knowledge("/repos/a")

        self.store.update_familiarity(
            "/repos/a", ["a.py", "b.py", "c.py"], total_files=100
        )
        k2 = self.store.get_repo_knowledge("/repos/a")
        self.assertGreater(k2.familiarity_score, k1.familiarity_score)

    def test_does_not_exceed_max(self) -> None:
        self.store.extract_from_task(
            task_id="t1", repo_path="/repos/a", outcome="success",
            build_commands=["make"],
        )

        # Touch many files with total_files equal to files touched
        files = [f"file_{i}.py" for i in range(100)]
        self.store.update_familiarity("/repos/a", files, total_files=100)
        k = self.store.get_repo_knowledge("/repos/a")
        self.assertLessEqual(k.familiarity_score, 1.0)

    def test_no_op_for_unknown_repo(self) -> None:
        # Should not raise
        self.store.update_familiarity("/unknown/repo", ["a.py"])

    def test_formula_coverage_component(self) -> None:
        """Verify coverage = unique_files / total_files contributes to score."""
        self.store.extract_from_task(
            task_id="t1", repo_path="/repos/a", outcome="success",
            build_commands=["make"],
        )

        # Touch 50 out of 100 files => coverage = 0.5
        files = [f"file_{i}.py" for i in range(50)]
        self.store.update_familiarity("/repos/a", files, total_files=100)
        k = self.store.get_repo_knowledge("/repos/a")

        # Expected: 0.4 * 0.5 (coverage) + 0.4 * (1/20) (experience) + 0.2 * 1.0 (recency≈1)
        # = 0.2 + 0.02 + 0.2 = 0.42
        self.assertAlmostEqual(k.familiarity_score, 0.42, places=1)

    def test_formula_experience_saturates_at_20(self) -> None:
        """Verify experience component saturates at 20 tasks."""
        self.store.extract_from_task(
            task_id="t1", repo_path="/repos/a", outcome="success",
            build_commands=["make"],
        )

        # Simulate 20 calls to update_familiarity (20 completed tasks)
        for i in range(20):
            self.store.update_familiarity(
                "/repos/a", [f"file_{i}.py"], total_files=100
            )

        k = self.store.get_repo_knowledge("/repos/a")
        # experience = min(20/20, 1.0) = 1.0
        # coverage = 20/100 = 0.2
        # recency ≈ 1.0 (just now)
        # expected ≈ 0.4*0.2 + 0.4*1.0 + 0.2*1.0 = 0.08 + 0.4 + 0.2 = 0.68
        self.assertAlmostEqual(k.familiarity_score, 0.68, places=1)

    def test_formula_recency_decays(self) -> None:
        """Verify recency component uses exponential decay."""
        # Compute familiarity for a task that happened 30 days ago
        now = time.time()
        thirty_days_ago = now - (30 * 86400)

        # recency after 30 days (half-life=30): e^(-ln2) = 0.5
        score = KnowledgeStore._compute_familiarity(
            unique_files=50,
            total_files=100,
            completed_tasks=10,
            last_task_timestamp=thirty_days_ago,
            now=now,
        )
        # coverage = 0.5, experience = 0.5, recency ≈ 0.5
        # expected = 0.4*0.5 + 0.4*0.5 + 0.2*0.5 = 0.2 + 0.2 + 0.1 = 0.5
        self.assertAlmostEqual(score, 0.5, places=1)

    def test_deduplicates_files_across_calls(self) -> None:
        """Verify same file touched multiple times counts as one unique file."""
        self.store.extract_from_task(
            task_id="t1", repo_path="/repos/a", outcome="success",
            build_commands=["make"],
        )

        self.store.update_familiarity("/repos/a", ["a.py", "b.py"], total_files=10)
        self.store.update_familiarity("/repos/a", ["a.py", "c.py"], total_files=10)

        # unique_files should be 3, not 4
        k = self.store.get_repo_knowledge("/repos/a")
        # coverage = 3/10 = 0.3, experience = min(2/20, 1.0) = 0.1, recency ≈ 1.0
        # expected ≈ 0.4*0.3 + 0.4*0.1 + 0.2*1.0 = 0.12 + 0.04 + 0.2 = 0.36
        self.assertAlmostEqual(k.familiarity_score, 0.36, places=1)


class TestGetApproachEffectiveness(unittest.TestCase):
    """Tests for get_approach_effectiveness method.

    Validates: Requirement 39.3, 39.4
    """

    def setUp(self) -> None:
        self.store = KnowledgeStore()

    def tearDown(self) -> None:
        self.store.close()

    def test_returns_empty_for_no_records(self) -> None:
        result = self.store.get_approach_effectiveness(repo_path="/repos/x")
        self.assertEqual(result, [])

    def test_computes_success_rate(self) -> None:
        # 3 successes, 1 failure = 75% success rate
        for i, outcome in enumerate(["success", "success", "success", "failure"]):
            self.store.extract_from_task(
                task_id=f"t{i}", repo_path="/repos/a", outcome=outcome,
                task_type="bugfix", complexity_tier="moderate",
                cost_efficiency=1.0, time_efficiency=1.0,
                iteration_count=1,
            )

        results = self.store.get_approach_effectiveness(
            repo_path="/repos/a", task_type="bugfix", tier="moderate"
        )
        self.assertEqual(len(results), 1)
        self.assertAlmostEqual(results[0].success_rate, 0.75)

    def test_computes_cost_efficiency_capped(self) -> None:
        # All tasks with cost_efficiency = 2.0 (should be capped at 1.5)
        for i in range(3):
            self.store.extract_from_task(
                task_id=f"t{i}", repo_path="/repos/a", outcome="success",
                task_type="feature", complexity_tier="complex",
                cost_efficiency=2.0, time_efficiency=1.0,
                iteration_count=1,
            )

        results = self.store.get_approach_effectiveness(
            repo_path="/repos/a", task_type="feature", tier="complex"
        )
        self.assertEqual(len(results), 1)
        self.assertAlmostEqual(results[0].cost_efficiency, 1.5)

    def test_computes_time_efficiency_capped(self) -> None:
        # All tasks with time_efficiency = 3.0 (should be capped at 2.0)
        for i in range(3):
            self.store.extract_from_task(
                task_id=f"t{i}", repo_path="/repos/a", outcome="success",
                task_type="feature", complexity_tier="routine",
                cost_efficiency=1.0, time_efficiency=3.0,
                iteration_count=1,
            )

        results = self.store.get_approach_effectiveness(
            repo_path="/repos/a", task_type="feature", tier="routine"
        )
        self.assertEqual(len(results), 1)
        self.assertAlmostEqual(results[0].time_efficiency, 2.0)

    def test_computes_first_pass_rate(self) -> None:
        # 2 first-pass (iteration=1), 2 multi-pass (iteration>1)
        for i, iters in enumerate([1, 1, 3, 2]):
            self.store.extract_from_task(
                task_id=f"t{i}", repo_path="/repos/a", outcome="success",
                task_type="bugfix", complexity_tier="routine",
                cost_efficiency=1.0, time_efficiency=1.0,
                iteration_count=iters,
            )

        results = self.store.get_approach_effectiveness(
            repo_path="/repos/a", task_type="bugfix", tier="routine"
        )
        self.assertEqual(len(results), 1)
        self.assertAlmostEqual(results[0].first_pass_rate, 0.5)

    def test_composite_score_formula(self) -> None:
        # All perfect: success_rate=1.0, cost_eff=1.5 (max), time_eff=2.0 (max), first_pass=1.0
        for i in range(5):
            self.store.extract_from_task(
                task_id=f"t{i}", repo_path="/repos/a", outcome="success",
                task_type="feature", complexity_tier="moderate",
                cost_efficiency=1.5, time_efficiency=2.0,
                iteration_count=1,
            )

        results = self.store.get_approach_effectiveness(
            repo_path="/repos/a", task_type="feature", tier="moderate"
        )
        self.assertEqual(len(results), 1)
        # Expected: 0.40*1.0 + 0.20*(1.5/1.5) + 0.15*(2.0/2.0) + 0.25*1.0
        # = 0.40 + 0.20 + 0.15 + 0.25 = 1.0
        self.assertAlmostEqual(results[0].composite_score, 1.0, places=2)

    def test_composite_score_all_failures(self) -> None:
        # All failures, low efficiency, many iterations
        for i in range(5):
            self.store.extract_from_task(
                task_id=f"t{i}", repo_path="/repos/a", outcome="failure",
                task_type="refactor", complexity_tier="critical",
                cost_efficiency=0.0, time_efficiency=0.0,
                iteration_count=5,
            )

        results = self.store.get_approach_effectiveness(
            repo_path="/repos/a", task_type="refactor", tier="critical"
        )
        self.assertEqual(len(results), 1)
        # Expected: 0.40*0 + 0.20*0 + 0.15*0 + 0.25*0 = 0.0
        self.assertAlmostEqual(results[0].composite_score, 0.0, places=2)

    def test_filters_by_repo(self) -> None:
        self.store.extract_from_task(
            task_id="t1", repo_path="/repos/a", outcome="success",
            task_type="bugfix", complexity_tier="moderate",
        )
        self.store.extract_from_task(
            task_id="t2", repo_path="/repos/b", outcome="success",
            task_type="bugfix", complexity_tier="moderate",
        )

        results = self.store.get_approach_effectiveness(repo_path="/repos/a")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].repo_path, "/repos/a")

    def test_groups_by_task_type_and_tier(self) -> None:
        self.store.extract_from_task(
            task_id="t1", repo_path="/repos/a", outcome="success",
            task_type="bugfix", complexity_tier="moderate",
        )
        self.store.extract_from_task(
            task_id="t2", repo_path="/repos/a", outcome="success",
            task_type="feature", complexity_tier="moderate",
        )

        results = self.store.get_approach_effectiveness(repo_path="/repos/a")
        self.assertEqual(len(results), 2)
        task_types = {r.task_type for r in results}
        self.assertEqual(task_types, {"bugfix", "feature"})

    def test_no_filters_returns_all(self) -> None:
        self.store.extract_from_task(
            task_id="t1", repo_path="/repos/a", outcome="success",
            task_type="bugfix", complexity_tier="moderate",
        )
        self.store.extract_from_task(
            task_id="t2", repo_path="/repos/b", outcome="failure",
            task_type="feature", complexity_tier="complex",
        )

        results = self.store.get_approach_effectiveness()
        self.assertEqual(len(results), 2)


class TestPersistence(unittest.TestCase):
    """Tests for data persistence across store instances."""

    def test_data_persists_across_connections(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        # Write data
        store1 = KnowledgeStore(db_path=db_path)
        store1.extract_from_task(
            task_id="task-persist",
            repo_path="/repos/persistent",
            outcome="success",
            build_commands=["gradle build"],
            pitfalls=["flaky test on CI"],
        )
        store1.close()

        # Read data from new connection
        store2 = KnowledgeStore(db_path=db_path)
        knowledge = store2.get_repo_knowledge("/repos/persistent")
        self.assertIsNotNone(knowledge)
        self.assertEqual(knowledge.build_commands, ["gradle build"])
        self.assertEqual(knowledge.pitfalls, ["flaky test on CI"])
        store2.close()


class TestTFIDFRelevanceRanking(unittest.TestCase):
    """Tests for TF-IDF relevance ranking in context compression.

    Validates: Requirement 42.3
    """

    def setUp(self) -> None:
        self.store = KnowledgeStore()

    def tearDown(self) -> None:
        self.store.close()

    def test_tfidf_ranks_relevant_file_higher(self) -> None:
        """TF-IDF should rank files with matching function names higher."""
        summaries = {
            "auth/login.py": "def authenticate_user(username, password); def validate_token(token)",
            "utils/math.py": "def calculate_sum(a, b); def multiply_numbers(x, y)",
            "db/models.py": "class UserModel(Base); def create_user(name, email)",
        }
        self.store._conn.execute(
            "INSERT INTO structural_summaries "
            "(repo_path, module_graph, interface_summaries, last_generated, file_count_at_generation) "
            "VALUES (?, ?, ?, ?, ?)",
            ("/repos/tfidf", json.dumps({}), json.dumps(summaries), time.time(), 3),
        )
        self.store._conn.commit()

        result = self.store.get_compressed_context(
            "/repos/tfidf", "fix user authentication bug", max_tokens=10000
        )

        # auth/login.py should appear in the results (ranked highest due to TF-IDF match)
        self.assertIn("auth/login.py", result.interface_summaries)

    def test_tfidf_with_empty_objective(self) -> None:
        """TF-IDF should handle empty-like objectives gracefully."""
        summaries = {"a.py": "def foo(); def bar()"}
        self.store._conn.execute(
            "INSERT INTO structural_summaries "
            "(repo_path, module_graph, interface_summaries, last_generated, file_count_at_generation) "
            "VALUES (?, ?, ?, ?, ?)",
            ("/repos/empty", json.dumps({}), json.dumps(summaries), time.time(), 1),
        )
        self.store._conn.commit()

        # Should not raise
        result = self.store.get_compressed_context("/repos/empty", "x", max_tokens=10000)
        self.assertIsNotNone(result)


class TestRegenerateStructuralSummary(unittest.TestCase):
    """Tests for regenerate_structural_summary method.

    Validates: Requirements 42.2, 42.4
    """

    def setUp(self) -> None:
        self.store = KnowledgeStore()

    def tearDown(self) -> None:
        self.store.close()

    def test_generates_module_graph_from_imports(self) -> None:
        """Should extract import dependencies into the module graph."""
        files = {
            "app/main.py": "import os\nfrom app.utils import helper\n\ndef run(): pass\n",
            "app/utils.py": "import json\n\ndef helper(): pass\n",
        }
        self.store.regenerate_structural_summary("/repos/regen", files)

        result = self.store.get_compressed_context("/repos/regen", "run app", max_tokens=10000)
        # Module graph should have entries from import analysis
        self.assertTrue(len(result.module_graph) > 0)

    def test_generates_interface_summaries(self) -> None:
        """Should extract public function/class signatures."""
        files = {
            "service.py": "def handle_request(req): pass\ndef _internal(): pass\nclass Router: pass\n",
        }
        self.store.regenerate_structural_summary("/repos/iface", files)

        result = self.store.get_compressed_context("/repos/iface", "handle request", max_tokens=10000)
        self.assertIn("service.py", result.interface_summaries)
        # Should include public function and class, not private
        summary = result.interface_summaries["service.py"]
        self.assertIn("handle_request", summary)
        self.assertIn("Router", summary)
        self.assertNotIn("_internal", summary)

    def test_overwrites_existing_summary(self) -> None:
        """Regeneration should replace existing summaries."""
        files_v1 = {"old.py": "def old_func(): pass\n"}
        self.store.regenerate_structural_summary("/repos/overwrite", files_v1)

        files_v2 = {"new.py": "def new_func(): pass\n"}
        self.store.regenerate_structural_summary("/repos/overwrite", files_v2)

        result = self.store.get_compressed_context("/repos/overwrite", "new func", max_tokens=10000)
        self.assertIn("new.py", result.interface_summaries)
        self.assertNotIn("old.py", result.interface_summaries)

    def test_handles_empty_files(self) -> None:
        """Should handle repos with no public interfaces."""
        files = {"_private.py": "def _internal(): pass\n"}
        self.store.regenerate_structural_summary("/repos/empty", files)

        result = self.store.get_compressed_context("/repos/empty", "anything", max_tokens=10000)
        self.assertEqual(result.interface_summaries, {})


class TestShouldRegenerateSummary(unittest.TestCase):
    """Tests for should_regenerate_summary method.

    Validates: Requirement 42.4
    """

    def setUp(self) -> None:
        self.store = KnowledgeStore()

    def tearDown(self) -> None:
        self.store.close()

    def test_returns_true_when_no_summary_exists(self) -> None:
        """Always regenerate when no summary exists."""
        result = self.store.should_regenerate_summary("/repos/new", 100, 0)
        self.assertTrue(result)

    def test_returns_false_when_change_below_threshold(self) -> None:
        """Should not regenerate when less than 20% changed."""
        files = {f"file_{i}.py": f"def func_{i}(): pass\n" for i in range(100)}
        self.store.regenerate_structural_summary("/repos/stable", files)

        # 10 out of 100 changed = 10% < 20%
        result = self.store.should_regenerate_summary("/repos/stable", 100, 10)
        self.assertFalse(result)

    def test_returns_true_when_change_above_threshold(self) -> None:
        """Should regenerate when more than 20% changed."""
        files = {f"file_{i}.py": f"def func_{i}(): pass\n" for i in range(100)}
        self.store.regenerate_structural_summary("/repos/changed", files)

        # 25 out of 100 changed = 25% > 20%
        result = self.store.should_regenerate_summary("/repos/changed", 100, 25)
        self.assertTrue(result)

    def test_returns_false_at_exactly_20_percent(self) -> None:
        """At exactly 20%, should NOT regenerate (strictly greater than 20%)."""
        files = {f"file_{i}.py": f"def func_{i}(): pass\n" for i in range(100)}
        self.store.regenerate_structural_summary("/repos/boundary", files)

        # 20 out of 100 changed = exactly 20%
        result = self.store.should_regenerate_summary("/repos/boundary", 100, 20)
        self.assertFalse(result)

    def test_uses_larger_count_as_reference(self) -> None:
        """Reference count should be max of old and new file counts."""
        files = {f"file_{i}.py": f"def func_{i}(): pass\n" for i in range(50)}
        self.store.regenerate_structural_summary("/repos/grow", files)

        # Repo grew to 100 files, 15 changed
        # reference = max(50, 100) = 100, ratio = 15/100 = 15% < 20%
        result = self.store.should_regenerate_summary("/repos/grow", 100, 15)
        self.assertFalse(result)


class TestTokenizeText(unittest.TestCase):
    """Tests for the _tokenize_text helper."""

    def test_splits_camel_case(self) -> None:
        from vikram_orchestrator.knowledge_store import _tokenize_text

        tokens = _tokenize_text("getUserName")
        self.assertIn("get", tokens)
        self.assertIn("user", tokens)
        self.assertIn("name", tokens)

    def test_splits_snake_case(self) -> None:
        from vikram_orchestrator.knowledge_store import _tokenize_text

        tokens = _tokenize_text("get_user_name")
        self.assertIn("get", tokens)
        self.assertIn("user", tokens)
        self.assertIn("name", tokens)

    def test_handles_file_paths(self) -> None:
        from vikram_orchestrator.knowledge_store import _tokenize_text

        tokens = _tokenize_text("auth/login.py")
        self.assertIn("auth", tokens)
        self.assertIn("login", tokens)

    def test_filters_short_tokens(self) -> None:
        from vikram_orchestrator.knowledge_store import _tokenize_text

        tokens = _tokenize_text("a b cd ef")
        self.assertNotIn("a", tokens)
        self.assertNotIn("b", tokens)
        self.assertIn("cd", tokens)
        self.assertIn("ef", tokens)


class TestComputeTfidfScores(unittest.TestCase):
    """Tests for the _compute_tfidf_scores helper."""

    def test_empty_documents(self) -> None:
        from vikram_orchestrator.knowledge_store import _compute_tfidf_scores

        result = _compute_tfidf_scores({}, ["query"])
        self.assertEqual(result, {})

    def test_empty_query(self) -> None:
        from vikram_orchestrator.knowledge_store import _compute_tfidf_scores

        result = _compute_tfidf_scores({"doc1": "some text"}, [])
        self.assertEqual(result, {})

    def test_ranks_matching_document_higher(self) -> None:
        from vikram_orchestrator.knowledge_store import _compute_tfidf_scores

        docs = {
            "auth.py": "authenticate user validate token",
            "math.py": "calculate sum multiply numbers",
        }
        scores = _compute_tfidf_scores(docs, ["authenticate", "user"])
        self.assertGreater(scores["auth.py"], scores["math.py"])

    def test_idf_boosts_rare_terms(self) -> None:
        from vikram_orchestrator.knowledge_store import _compute_tfidf_scores

        # "unique" only appears in doc1, "common" appears in both
        docs = {
            "doc1": "unique common word",
            "doc2": "common other word",
        }
        scores = _compute_tfidf_scores(docs, ["unique"])
        # doc1 should score higher because "unique" is rare (high IDF)
        self.assertGreater(scores["doc1"], scores["doc2"])


class TestRelevanceRankedExcerpts(unittest.TestCase):
    """Tests for relevance_ranked_excerpts in get_compressed_context.

    Validates: Requirements 42.1, 42.3
    """

    def setUp(self) -> None:
        self.store = KnowledgeStore()

    def tearDown(self) -> None:
        self.store.close()

    def test_excerpts_populated_when_budget_insufficient_for_full_summaries(self) -> None:
        """When budget is too small for all full summaries, remaining relevant
        files appear as ranked excerpts."""
        # Create many interface summaries — the first few fit, the rest overflow
        summaries = {}
        for i in range(20):
            summaries[f"module_{i}.py"] = f"def process_auth_{i}(user, token): validate credentials for user {i}"

        self.store._conn.execute(
            "INSERT INTO structural_summaries "
            "(repo_path, module_graph, interface_summaries, last_generated, file_count_at_generation) "
            "VALUES (?, ?, ?, ?, ?)",
            ("/repos/big", json.dumps({}), json.dumps(summaries), time.time(), 20),
        )
        self.store._conn.commit()

        # Use a small token budget so not all summaries fit as full entries
        result = self.store.get_compressed_context("/repos/big", "auth token validation", max_tokens=200)

        # Some summaries included fully, remaining as excerpts
        total_included = len(result.interface_summaries) + len(result.relevance_ranked_excerpts)
        self.assertGreater(total_included, 0)
        # If there are excerpts, they should have positive relevance scores
        for path, excerpt, score in result.relevance_ranked_excerpts:
            self.assertGreater(score, 0.0)
            self.assertIsInstance(excerpt, str)
            self.assertIsInstance(path, str)

    def test_excerpts_have_tfidf_relevance_scores(self) -> None:
        """Excerpts carry their TF-IDF relevance score."""
        summaries = {
            "auth.py": "def authenticate_user(username, password): login handler",
            "math.py": "def calculate_sum(numbers): sum numbers",
            "db.py": "def connect_database(host, port): open connection",
        }
        self.store._conn.execute(
            "INSERT INTO structural_summaries "
            "(repo_path, module_graph, interface_summaries, last_generated, file_count_at_generation) "
            "VALUES (?, ?, ?, ?, ?)",
            ("/repos/scored", json.dumps({}), json.dumps(summaries), time.time(), 3),
        )
        self.store._conn.commit()

        # Very small budget so only one summary fits fully
        result = self.store.get_compressed_context("/repos/scored", "authenticate user", max_tokens=30)

        # Check that if excerpts exist, they have scores
        for _path, _excerpt, score in result.relevance_ranked_excerpts:
            self.assertIsInstance(score, float)

    def test_excerpts_truncated_to_120_chars(self) -> None:
        """Long summaries are truncated to 120 chars when used as excerpts."""
        long_summary = "def " + "x" * 200 + "(arg): very long function signature"
        summaries = {
            "long.py": long_summary,
            "short.py": "def foo(): bar",
        }
        self.store._conn.execute(
            "INSERT INTO structural_summaries "
            "(repo_path, module_graph, interface_summaries, last_generated, file_count_at_generation) "
            "VALUES (?, ?, ?, ?, ?)",
            ("/repos/trunc", json.dumps({}), json.dumps(summaries), time.time(), 2),
        )
        self.store._conn.commit()

        # Budget enough for one full summary but not both
        result = self.store.get_compressed_context("/repos/trunc", "foo bar", max_tokens=20)

        for _path, excerpt, _score in result.relevance_ranked_excerpts:
            self.assertLessEqual(len(excerpt), 120)

    def test_no_excerpts_when_all_summaries_fit(self) -> None:
        """When all summaries fit within budget as full entries, no excerpts needed."""
        summaries = {
            "a.py": "def foo(): pass",
            "b.py": "def bar(): pass",
        }
        self.store._conn.execute(
            "INSERT INTO structural_summaries "
            "(repo_path, module_graph, interface_summaries, last_generated, file_count_at_generation) "
            "VALUES (?, ?, ?, ?, ?)",
            ("/repos/small", json.dumps({}), json.dumps(summaries), time.time(), 2),
        )
        self.store._conn.commit()

        # Large budget — everything fits
        result = self.store.get_compressed_context("/repos/small", "foo bar", max_tokens=5000)

        self.assertEqual(len(result.interface_summaries), 2)
        self.assertEqual(len(result.relevance_ranked_excerpts), 0)


if __name__ == "__main__":
    unittest.main()
