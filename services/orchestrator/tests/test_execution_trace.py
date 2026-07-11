"""Unit tests for execution_trace module — SQLite storage and hash chain.

Validates SHA-256 hash chain construction, append-only SQLite storage,
monotonic sequence numbers, chain integrity verification, and persistence.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from vikram_orchestrator.execution_trace import (
    ExecutionTrace,
    TraceRecord,
    canonical_json,
    compute_record_hash,
    GENESIS_HASH,
    PLATFORM_VERSION,
)


class TestCanonicalJson(unittest.TestCase):
    """Tests for canonical JSON serialization."""

    def test_sorted_keys(self) -> None:
        result = canonical_json({"z": 1, "a": 2, "m": 3})
        self.assertEqual(result, '{"a":2,"m":3,"z":1}')

    def test_compact_separators(self) -> None:
        result = canonical_json({"key": "value"})
        self.assertNotIn(" ", result)
        self.assertEqual(result, '{"key":"value"}')

    def test_nested_sorted(self) -> None:
        result = canonical_json({"b": {"z": 1, "a": 2}, "a": 1})
        self.assertEqual(result, '{"a":1,"b":{"a":2,"z":1}}')

    def test_empty_dict(self) -> None:
        result = canonical_json({})
        self.assertEqual(result, "{}")


class TestComputeRecordHash(unittest.TestCase):
    """Tests for hash computation correctness."""

    def test_deterministic(self) -> None:
        kwargs = {
            "sequence_number": 0,
            "task_id": "task-001",
            "decision_type": "phase_transition",
            "timestamp": 1700000000.0,
            "state_snapshot": {"phase": "planning"},
            "policy_evaluated": "budget_check",
            "outcome": "proceed",
            "non_deterministic_inputs": {},
            "previous_hash": "abc123",
        }
        hash1 = compute_record_hash(**kwargs)
        hash2 = compute_record_hash(**kwargs)
        self.assertEqual(hash1, hash2)

    def test_different_inputs_different_hash(self) -> None:
        base = {
            "sequence_number": 0,
            "task_id": "task-001",
            "decision_type": "phase_transition",
            "timestamp": 1700000000.0,
            "state_snapshot": {"phase": "planning"},
            "policy_evaluated": "budget_check",
            "outcome": "proceed",
            "non_deterministic_inputs": {},
            "previous_hash": "abc123",
        }
        hash1 = compute_record_hash(**base)
        modified = {**base, "outcome": "halt"}
        hash2 = compute_record_hash(**modified)
        self.assertNotEqual(hash1, hash2)

    def test_sha256_format(self) -> None:
        h = compute_record_hash(
            sequence_number=0,
            task_id="t",
            decision_type="d",
            timestamp=0.0,
            state_snapshot={},
            policy_evaluated="p",
            outcome="o",
            non_deterministic_inputs={},
            previous_hash="prev",
        )
        # SHA-256 hex digest is 64 characters
        self.assertEqual(len(h), 64)
        # Only hex characters
        self.assertTrue(all(c in "0123456789abcdef" for c in h))


class TestGenesisHash(unittest.TestCase):
    """Tests for genesis hash configuration."""

    def test_default_genesis_is_sha256_of_version(self) -> None:
        expected = hashlib.sha256(PLATFORM_VERSION.encode()).hexdigest()
        self.assertEqual(GENESIS_HASH, expected)

    def test_genesis_hash_is_64_hex_chars(self) -> None:
        self.assertEqual(len(GENESIS_HASH), 64)


class TestExecutionTraceInMemory(unittest.TestCase):
    """Tests for the in-memory execution trace (no db_path)."""

    def setUp(self) -> None:
        self.trace = ExecutionTrace()

    def test_first_record_uses_genesis_hash(self) -> None:
        record = self.trace.record_decision(
            task_id="task-001",
            decision_type="phase_transition",
            state_snapshot={"phase": "planning"},
            policy="start_policy",
            outcome="proceed",
        )
        self.assertEqual(record.sequence_number, 0)
        self.assertEqual(record.previous_hash, GENESIS_HASH)

    def test_monotonic_sequence_numbers(self) -> None:
        r0 = self.trace.record_decision(
            task_id="task-001",
            decision_type="phase_transition",
            state_snapshot={},
            policy="p1",
            outcome="o1",
        )
        r1 = self.trace.record_decision(
            task_id="task-001",
            decision_type="model_selection",
            state_snapshot={},
            policy="p2",
            outcome="o2",
        )
        r2 = self.trace.record_decision(
            task_id="task-002",
            decision_type="escalation",
            state_snapshot={},
            policy="p3",
            outcome="o3",
        )
        self.assertEqual(r0.sequence_number, 0)
        self.assertEqual(r1.sequence_number, 1)
        self.assertEqual(r2.sequence_number, 2)

    def test_hash_chain_linkage(self) -> None:
        r0 = self.trace.record_decision(
            task_id="task-001",
            decision_type="phase_transition",
            state_snapshot={"a": 1},
            policy="p1",
            outcome="o1",
        )
        r1 = self.trace.record_decision(
            task_id="task-001",
            decision_type="model_selection",
            state_snapshot={"b": 2},
            policy="p2",
            outcome="o2",
        )
        # r1's previous_hash should be r0's record_hash
        self.assertEqual(r1.previous_hash, r0.record_hash)

    def test_record_hash_is_verifiable(self) -> None:
        record = self.trace.record_decision(
            task_id="task-001",
            decision_type="approval_routing",
            state_snapshot={"risk": "low"},
            policy="auto_approve_docs",
            outcome="auto_approve",
            nd_inputs={"api_response_time": 0.5},
        )
        expected = compute_record_hash(
            sequence_number=record.sequence_number,
            task_id=record.task_id,
            decision_type=record.decision_type,
            timestamp=record.timestamp,
            state_snapshot=record.state_snapshot,
            policy_evaluated=record.policy_evaluated,
            outcome=record.outcome,
            non_deterministic_inputs=record.non_deterministic_inputs,
            previous_hash=record.previous_hash,
        )
        self.assertEqual(record.record_hash, expected)

    def test_verify_chain_integrity_valid(self) -> None:
        for i in range(10):
            self.trace.record_decision(
                task_id=f"task-{i:03d}",
                decision_type="phase_transition",
                state_snapshot={"i": i},
                policy=f"policy_{i}",
                outcome=f"outcome_{i}",
            )
        self.assertTrue(self.trace.verify_chain_integrity(0, 9))


class TestExecutionTraceSQLite(unittest.TestCase):
    """Tests for SQLite-backed execution trace."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "trace.db")
        self.trace = ExecutionTrace(db_path=self.db_path)

    def tearDown(self) -> None:
        self.trace.close()
        self._tmpdir.cleanup()

    def test_schema_creates_tables(self) -> None:
        """Verify both required tables exist in the database."""
        conn = sqlite3.connect(self.db_path)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = [t[0] for t in tables]
        self.assertIn("execution_trace", table_names)
        self.assertIn("trace_integrity_checkpoints", table_names)
        conn.close()

    def test_schema_creates_indexes(self) -> None:
        """Verify indexes are created for efficient querying."""
        conn = sqlite3.connect(self.db_path)
        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' ORDER BY name"
        ).fetchall()
        index_names = [i[0] for i in indexes]
        self.assertIn("idx_trace_task_id", index_names)
        self.assertIn("idx_trace_decision_type", index_names)
        self.assertIn("idx_trace_timestamp", index_names)
        conn.close()

    def test_record_persisted_to_sqlite(self) -> None:
        """Records are stored in SQLite."""
        self.trace.record_decision(
            task_id="task-001",
            decision_type="phase_transition",
            state_snapshot={"phase": "planning"},
            policy="start_policy",
            outcome="proceed",
        )
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM execution_trace").fetchone()[0]
        self.assertEqual(count, 1)

        row = conn.execute(
            "SELECT task_id, decision_type, outcome FROM execution_trace WHERE sequence_number = 0"
        ).fetchone()
        self.assertEqual(row[0], "task-001")
        self.assertEqual(row[1], "phase_transition")
        self.assertEqual(row[2], "proceed")
        conn.close()

    def test_state_snapshot_stored_as_canonical_json(self) -> None:
        """State snapshot is stored as canonical JSON in SQLite."""
        self.trace.record_decision(
            task_id="task-001",
            decision_type="phase_transition",
            state_snapshot={"z_key": "z_val", "a_key": "a_val"},
            policy="p",
            outcome="o",
        )
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT state_snapshot FROM execution_trace WHERE sequence_number = 0"
        ).fetchone()
        # Should be canonical: sorted keys, compact
        self.assertEqual(row[0], '{"a_key":"a_val","z_key":"z_val"}')
        conn.close()

    def test_persistence_across_instances(self) -> None:
        """Records persist and chain continues across ExecutionTrace instances."""
        r0 = self.trace.record_decision(
            task_id="task-001",
            decision_type="phase_transition",
            state_snapshot={"x": 1},
            policy="p",
            outcome="o",
        )
        self.trace.close()

        # Open a new instance pointing to the same DB
        trace2 = ExecutionTrace(db_path=self.db_path)
        r1 = trace2.record_decision(
            task_id="task-002",
            decision_type="model_selection",
            state_snapshot={"y": 2},
            policy="p2",
            outcome="o2",
        )
        # Chain should continue from where first instance left off
        self.assertEqual(r1.sequence_number, 1)
        self.assertEqual(r1.previous_hash, r0.record_hash)
        trace2.close()

        # Reassign for tearDown
        self.trace = ExecutionTrace(db_path=self.db_path)

    def test_hash_chain_valid_with_sqlite(self) -> None:
        """Hash chain is valid when backed by SQLite storage."""
        for i in range(5):
            self.trace.record_decision(
                task_id=f"task-{i:03d}",
                decision_type="phase_transition",
                state_snapshot={"i": i},
                policy=f"policy_{i}",
                outcome=f"outcome_{i}",
            )
        self.assertTrue(self.trace.verify_chain_integrity(0, 4))

    def test_query_by_task_id_with_sqlite(self) -> None:
        """Query filters work with SQLite-backed trace."""
        self.trace.record_decision(
            task_id="task-001",
            decision_type="phase_transition",
            state_snapshot={},
            policy="p",
            outcome="o",
        )
        self.trace.record_decision(
            task_id="task-002",
            decision_type="model_selection",
            state_snapshot={},
            policy="p",
            outcome="o",
        )
        self.trace.record_decision(
            task_id="task-001",
            decision_type="escalation",
            state_snapshot={},
            policy="p",
            outcome="o",
        )
        results = self.trace.query(task_id="task-001")
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.task_id == "task-001" for r in results))

    def test_save_integrity_checkpoint(self) -> None:
        """Integrity checkpoints are persisted to the checkpoints table."""
        record = self.trace.record_decision(
            task_id="task-001",
            decision_type="phase_transition",
            state_snapshot={},
            policy="p",
            outcome="o",
        )
        self.trace.save_integrity_checkpoint(
            record.sequence_number, record.record_hash
        )
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT sequence_number, hash FROM trace_integrity_checkpoints"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 0)
        self.assertEqual(row[1], record.record_hash)
        conn.close()

    def test_custom_genesis_hash_with_sqlite(self) -> None:
        """Custom genesis hash is used for the first record's previous_hash."""
        custom_genesis = hashlib.sha256(b"custom-version-2.0").hexdigest()
        trace = ExecutionTrace(
            genesis_hash=custom_genesis,
            db_path=str(Path(self._tmpdir.name) / "custom.db"),
        )
        record = trace.record_decision(
            task_id="task-001",
            decision_type="model_selection",
            state_snapshot={},
            policy="complexity_rule",
            outcome="gpt-4",
        )
        self.assertEqual(record.previous_hash, custom_genesis)
        trace.close()

    def test_non_deterministic_inputs_persisted(self) -> None:
        """Non-deterministic inputs are stored and retrievable from SQLite."""
        nd = {"api_latency": 1.5, "random_seed": 12345, "external_response": "ok"}
        self.trace.record_decision(
            task_id="task-001",
            decision_type="model_selection",
            state_snapshot={"budget": 50.0},
            policy="router_policy",
            outcome="claude-3-5-sonnet",
            nd_inputs=nd,
        )
        self.trace.close()

        # Reload from DB
        trace2 = ExecutionTrace(db_path=self.db_path)
        results = trace2.query(task_id="task-001")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].non_deterministic_inputs, nd)
        trace2.close()

        # Reassign for tearDown
        self.trace = ExecutionTrace(db_path=self.db_path)

    def test_append_only_multiple_records(self) -> None:
        """Multiple records can be appended and all are stored."""
        for i in range(20):
            self.trace.record_decision(
                task_id=f"task-{i % 3:03d}",
                decision_type="phase_transition",
                state_snapshot={"step": i},
                policy=f"policy_{i}",
                outcome=f"outcome_{i}",
            )
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM execution_trace").fetchone()[0]
        self.assertEqual(count, 20)
        conn.close()

    def test_query_by_outcome(self) -> None:
        """Query filters by outcome correctly."""
        self.trace.record_decision(
            task_id="task-001",
            decision_type="phase_transition",
            state_snapshot={},
            policy="p",
            outcome="approved",
        )
        self.trace.record_decision(
            task_id="task-002",
            decision_type="model_selection",
            state_snapshot={},
            policy="p",
            outcome="rejected",
        )
        self.trace.record_decision(
            task_id="task-003",
            decision_type="escalation",
            state_snapshot={},
            policy="p",
            outcome="approved",
        )
        results = self.trace.query(outcome="approved")
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.outcome == "approved" for r in results))

        results_rejected = self.trace.query(outcome="rejected")
        self.assertEqual(len(results_rejected), 1)
        self.assertEqual(results_rejected[0].task_id, "task-002")

    def test_query_combined_outcome_and_task_id(self) -> None:
        """Query combines outcome and task_id filters (AND semantics)."""
        self.trace.record_decision(
            task_id="task-001",
            decision_type="phase_transition",
            state_snapshot={},
            policy="p",
            outcome="approved",
        )
        self.trace.record_decision(
            task_id="task-001",
            decision_type="model_selection",
            state_snapshot={},
            policy="p",
            outcome="rejected",
        )
        self.trace.record_decision(
            task_id="task-002",
            decision_type="escalation",
            state_snapshot={},
            policy="p",
            outcome="approved",
        )
        results = self.trace.query(task_id="task-001", outcome="approved")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].decision_type, "phase_transition")

    def test_detect_tampering_via_integrity_check(self) -> None:
        """Integrity verification detects tampered records."""
        for i in range(5):
            self.trace.record_decision(
                task_id=f"task-{i:03d}",
                decision_type="phase_transition",
                state_snapshot={"i": i},
                policy=f"policy_{i}",
                outcome=f"outcome_{i}",
            )

        # Tamper directly in SQLite
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE execution_trace SET outcome = 'tampered' WHERE sequence_number = 2"
        )
        conn.commit()
        conn.close()

        # Reload from tampered DB
        self.trace.close()
        self.trace = ExecutionTrace(db_path=self.db_path)

        # The in-memory records were loaded from DB, so the tampered record
        # will have mismatched hash when verify_chain_integrity recomputes
        self.assertFalse(self.trace.verify_chain_integrity(0, 4))


if __name__ == "__main__":
    unittest.main()
