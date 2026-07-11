"""Execution Trace subsystem — tamper-evident decision recording with hash chain.

Records every Orchestrator decision in an append-only log where each record
includes a cryptographic hash chaining to the previous record, enabling
integrity verification and deterministic replay.

Supports both in-memory storage (for testing/lightweight use) and persistent
append-only SQLite storage (for production use).

Module: vikram_orchestrator/execution_trace.py
Requirements: 6.1, 6.2, 6.3, 6.4, 7.1, 7.2, 7.3, 7.4
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from threading import Lock
from typing import Any

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Genesis hash constant — SHA-256 of platform version string
# ---------------------------------------------------------------------------
PLATFORM_VERSION = "vikram-0.1.0"
GENESIS_HASH = hashlib.sha256(PLATFORM_VERSION.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TraceRecord(BaseModel):
    """A single execution trace record with hash chain linkage."""

    sequence_number: int
    task_id: str
    decision_type: str  # "phase_transition", "model_selection", "approval_routing", "escalation"
    timestamp: float
    state_snapshot: dict[str, Any]
    policy_evaluated: str
    outcome: str
    non_deterministic_inputs: dict[str, Any]  # timestamps, seeds, external responses
    previous_hash: str
    record_hash: str


# ---------------------------------------------------------------------------
# Canonical JSON serialization for deterministic hashing
# ---------------------------------------------------------------------------


def canonical_json(obj: dict[str, Any]) -> str:
    """Produce a canonical JSON string: sorted keys, no whitespace, ensure_ascii."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


# ---------------------------------------------------------------------------
# Hash computation
# ---------------------------------------------------------------------------


def compute_record_hash(
    sequence_number: int,
    task_id: str,
    decision_type: str,
    timestamp: float,
    state_snapshot: dict[str, Any],
    policy_evaluated: str,
    outcome: str,
    non_deterministic_inputs: dict[str, Any],
    previous_hash: str,
) -> str:
    """Compute SHA-256 hash for a trace record per the design spec.

    record_hash = SHA-256(
        sequence_number || task_id || decision_type || timestamp ||
        canonical_json(state_snapshot) || policy_evaluated || outcome ||
        canonical_json(non_deterministic_inputs) || previous_hash
    )
    """
    parts = [
        str(sequence_number),
        task_id,
        decision_type,
        str(timestamp),
        canonical_json(state_snapshot),
        policy_evaluated,
        outcome,
        canonical_json(non_deterministic_inputs),
        previous_hash,
    ]
    payload = "||".join(parts)
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Policy evaluation registry (for replay)
# ---------------------------------------------------------------------------

# Map of policy_name -> callable that takes (state_snapshot, nd_inputs) -> outcome
_policy_registry: dict[str, Any] = {}


def register_policy(name: str, func: Any) -> None:
    """Register a policy evaluation function for replay verification."""
    _policy_registry[name] = func


def get_policy(name: str) -> Any | None:
    """Get a registered policy evaluation function."""
    return _policy_registry.get(name)


# ---------------------------------------------------------------------------
# SQLite schema for persistent execution trace storage
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS execution_trace (
    sequence_number INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    timestamp REAL NOT NULL,
    state_snapshot TEXT NOT NULL,
    policy_evaluated TEXT NOT NULL,
    outcome TEXT NOT NULL,
    non_deterministic_inputs TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    record_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trace_integrity_checkpoints (
    checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence_number INTEGER NOT NULL,
    hash TEXT NOT NULL,
    verified_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trace_task_id ON execution_trace(task_id);
CREATE INDEX IF NOT EXISTS idx_trace_decision_type ON execution_trace(decision_type);
CREATE INDEX IF NOT EXISTS idx_trace_timestamp ON execution_trace(timestamp);
"""


# ---------------------------------------------------------------------------
# ExecutionTrace class — supports both in-memory and SQLite-backed storage
# ---------------------------------------------------------------------------


class ExecutionTrace:
    """Append-only execution trace with SHA-256 hash chain.

    Supports two storage modes:
    - In-memory (default, for testing): pass no db_path
    - SQLite-backed (production): pass db_path to persist records

    Provides recording, querying, replay verification, and chain integrity checks.
    """

    def __init__(
        self,
        genesis_hash: str = GENESIS_HASH,
        db_path: str | None = None,
    ) -> None:
        self._genesis_hash = genesis_hash
        self._db_path = db_path
        self._lock = Lock()

        # In-memory storage (always maintained for fast access)
        self._records: list[TraceRecord] = []
        self._next_sequence: int = 0

        # SQLite connection (optional)
        self._conn: sqlite3.Connection | None = None
        if db_path is not None:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._create_schema()
            self._load_from_db()

    def _create_schema(self) -> None:
        """Create SQLite tables if they don't exist."""
        assert self._conn is not None
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()

    def _load_from_db(self) -> None:
        """Load existing records from SQLite into in-memory list on startup."""
        assert self._conn is not None
        cursor = self._conn.execute(
            "SELECT sequence_number, task_id, decision_type, timestamp, "
            "state_snapshot, policy_evaluated, outcome, "
            "non_deterministic_inputs, previous_hash, record_hash "
            "FROM execution_trace ORDER BY sequence_number ASC"
        )
        for row in cursor.fetchall():
            record = TraceRecord(
                sequence_number=row[0],
                task_id=row[1],
                decision_type=row[2],
                timestamp=row[3],
                state_snapshot=json.loads(row[4]),
                policy_evaluated=row[5],
                outcome=row[6],
                non_deterministic_inputs=json.loads(row[7]),
                previous_hash=row[8],
                record_hash=row[9],
            )
            self._records.append(record)

        if self._records:
            self._next_sequence = self._records[-1].sequence_number + 1

    @property
    def genesis_hash(self) -> str:
        return self._genesis_hash

    @property
    def records(self) -> list[TraceRecord]:
        return list(self._records)

    def record_decision(
        self,
        task_id: str,
        decision_type: str,
        state_snapshot: dict[str, Any],
        policy: str,
        outcome: str,
        nd_inputs: dict[str, Any] | None = None,
    ) -> TraceRecord:
        """Record a new decision in the trace with hash chain linkage.

        Args:
            task_id: The task this decision belongs to.
            decision_type: Type of decision (phase_transition, model_selection, etc.)
            state_snapshot: The state evaluated at decision time.
            policy: The policy or rule that fired.
            outcome: The decision outcome produced.
            nd_inputs: Non-deterministic inputs (timestamps, seeds, external responses).

        Returns:
            The created TraceRecord with computed hash.
        """
        if nd_inputs is None:
            nd_inputs = {}

        timestamp = time.time()

        with self._lock:
            sequence_number = self._next_sequence

            # Determine previous hash
            if sequence_number == 0:
                previous_hash = self._genesis_hash
            else:
                previous_hash = self._records[-1].record_hash

            # Compute record hash
            record_hash = compute_record_hash(
                sequence_number=sequence_number,
                task_id=task_id,
                decision_type=decision_type,
                timestamp=timestamp,
                state_snapshot=state_snapshot,
                policy_evaluated=policy,
                outcome=outcome,
                non_deterministic_inputs=nd_inputs,
                previous_hash=previous_hash,
            )

            record = TraceRecord(
                sequence_number=sequence_number,
                task_id=task_id,
                decision_type=decision_type,
                timestamp=timestamp,
                state_snapshot=state_snapshot,
                policy_evaluated=policy,
                outcome=outcome,
                non_deterministic_inputs=nd_inputs,
                previous_hash=previous_hash,
                record_hash=record_hash,
            )

            # Persist to SQLite if configured
            if self._conn is not None:
                self._conn.execute(
                    "INSERT INTO execution_trace "
                    "(sequence_number, task_id, decision_type, timestamp, "
                    "state_snapshot, policy_evaluated, outcome, "
                    "non_deterministic_inputs, previous_hash, record_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        sequence_number,
                        task_id,
                        decision_type,
                        timestamp,
                        canonical_json(state_snapshot),
                        policy,
                        outcome,
                        canonical_json(nd_inputs),
                        previous_hash,
                        record_hash,
                    ),
                )
                self._conn.commit()

            self._records.append(record)
            self._next_sequence += 1

        return record

    def query(
        self,
        task_id: str | None = None,
        decision_type: str | None = None,
        start_time: float | None = None,
        end_time: float | None = None,
        outcome: str | None = None,
    ) -> list[TraceRecord]:
        """Query trace records with optional filters.

        All filters are AND-combined. Results are ordered by sequence number.

        Args:
            task_id: Filter by task ID.
            decision_type: Filter by decision type.
            start_time: Filter records with timestamp >= start_time.
            end_time: Filter records with timestamp <= end_time.
            outcome: Filter by decision outcome.

        Returns:
            List of matching TraceRecords ordered by sequence_number.
        """
        results = self._records

        if task_id is not None:
            results = [r for r in results if r.task_id == task_id]

        if decision_type is not None:
            results = [r for r in results if r.decision_type == decision_type]

        if start_time is not None:
            results = [r for r in results if r.timestamp >= start_time]

        if end_time is not None:
            results = [r for r in results if r.timestamp <= end_time]

        if outcome is not None:
            results = [r for r in results if r.outcome == outcome]

        # Results are inherently ordered by sequence_number since _records is append-only
        return sorted(results, key=lambda r: r.sequence_number)

    def replay_verify(self, record_id: int) -> tuple[bool, str]:
        """Replay a recorded decision and verify the outcome matches.

        Looks up the trace record by sequence_number, retrieves the registered
        policy function, and re-evaluates with the recorded state and nd_inputs.

        Args:
            record_id: The sequence_number of the record to replay.

        Returns:
            Tuple of (matches: bool, message: str).
            If policy not registered or record not found, returns (False, reason).
        """
        # Find the record
        record = None
        for r in self._records:
            if r.sequence_number == record_id:
                record = r
                break

        if record is None:
            return False, f"Record with sequence_number {record_id} not found"

        # Look up the policy
        policy_func = get_policy(record.policy_evaluated)
        if policy_func is None:
            return False, f"Policy '{record.policy_evaluated}' not registered for replay"

        # Replay the decision
        try:
            replayed_outcome = policy_func(
                record.state_snapshot, record.non_deterministic_inputs
            )
        except Exception as e:
            return False, f"Policy replay raised exception: {e}"

        if replayed_outcome == record.outcome:
            return True, "Replay matches original outcome"
        else:
            return (
                False,
                f"Replay diverged: original='{record.outcome}', replayed='{replayed_outcome}'",
            )

    def verify_chain_integrity(self, start_seq: int, end_seq: int) -> bool:
        """Verify hash chain integrity over a range of sequence numbers.

        Recomputes each record's hash from its fields and verifies:
        1. Each record's previous_hash matches the prior record's record_hash
        2. Each record's stored record_hash matches the recomputed hash

        Args:
            start_seq: Starting sequence number (inclusive).
            end_seq: Ending sequence number (inclusive).

        Returns:
            True if the chain is valid over the specified range.
        """
        # Get records in range
        records_in_range = [
            r for r in self._records if start_seq <= r.sequence_number <= end_seq
        ]
        records_in_range.sort(key=lambda r: r.sequence_number)

        if not records_in_range:
            return True  # Empty range is trivially valid

        for i, record in enumerate(records_in_range):
            # Verify previous_hash linkage
            if record.sequence_number == 0:
                expected_prev = self._genesis_hash
            elif i == 0:
                # First record in range but not sequence 0 — find predecessor
                predecessors = [
                    r
                    for r in self._records
                    if r.sequence_number == record.sequence_number - 1
                ]
                if not predecessors:
                    return False
                expected_prev = predecessors[0].record_hash
            else:
                expected_prev = records_in_range[i - 1].record_hash

            if record.previous_hash != expected_prev:
                return False

            # Verify record_hash recomputation
            recomputed = compute_record_hash(
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
            if record.record_hash != recomputed:
                return False

        return True

    def save_integrity_checkpoint(self, sequence_number: int, hash_value: str) -> None:
        """Save an integrity checkpoint for periodic verification by Go side.

        Args:
            sequence_number: The sequence number being checkpointed.
            hash_value: The verified hash at this sequence number.
        """
        if self._conn is None:
            return
        self._conn.execute(
            "INSERT INTO trace_integrity_checkpoints (sequence_number, hash, verified_at) "
            "VALUES (?, ?, ?)",
            (sequence_number, hash_value, time.time()),
        )
        self._conn.commit()

    def close(self) -> None:
        """Close the SQLite database connection if open."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
