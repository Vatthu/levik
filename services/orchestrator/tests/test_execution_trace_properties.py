"""Property-based tests for the Execution Trace subsystem.

Tests verify hash chain integrity, query correctness, and replay determinism
using Hypothesis to generate arbitrary decision sequences.

Run with: cd services/orchestrator && .venv/bin/python -m pytest tests/test_execution_trace_properties.py -v

Validates: Requirements 6.2, 6.3, 7.1, 7.2
"""

from __future__ import annotations

import hashlib
from typing import Any

import hypothesis.strategies as st
from hypothesis import given, settings, assume

from vikram_orchestrator.execution_trace import (
    GENESIS_HASH,
    ExecutionTrace,
    TraceRecord,
    canonical_json,
    compute_record_hash,
    register_policy,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


def decision_type_st() -> st.SearchStrategy[str]:
    """Generate valid decision type strings."""
    return st.sampled_from([
        "phase_transition",
        "model_selection",
        "approval_routing",
        "escalation",
    ])


def task_id_st() -> st.SearchStrategy[str]:
    """Generate valid task IDs."""
    return st.text(
        min_size=1,
        max_size=30,
        alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
    )


def state_snapshot_st() -> st.SearchStrategy[dict[str, Any]]:
    """Generate valid state snapshot dictionaries."""
    return st.fixed_dictionaries({
        "phase": st.sampled_from(["planning", "implementation", "verification", "review"]),
        "budget_remaining": st.floats(min_value=0.0, max_value=1000.0, allow_nan=False, allow_infinity=False),
        "confidence": st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False),
    })


def policy_st() -> st.SearchStrategy[str]:
    """Generate valid policy names."""
    return st.sampled_from([
        "budget_threshold_check",
        "complexity_routing",
        "risk_assessment",
        "auto_approve_low_risk",
        "founder_review_required",
    ])


def outcome_st() -> st.SearchStrategy[str]:
    """Generate valid outcome strings."""
    return st.sampled_from([
        "approved",
        "rejected",
        "escalated",
        "downgraded",
        "paused",
        "proceed",
    ])


def nd_inputs_st() -> st.SearchStrategy[dict[str, Any]]:
    """Generate non-deterministic inputs dictionaries."""
    return st.fixed_dictionaries({
        "seed": st.integers(min_value=0, max_value=2**32 - 1),
        "external_response": st.sampled_from(["ok", "timeout", "error", "partial"]),
    })


def decision_st() -> st.SearchStrategy[dict[str, Any]]:
    """Generate a complete decision record input."""
    return st.fixed_dictionaries({
        "task_id": task_id_st(),
        "decision_type": decision_type_st(),
        "state_snapshot": state_snapshot_st(),
        "policy": policy_st(),
        "outcome": outcome_st(),
        "nd_inputs": nd_inputs_st(),
    })


# ---------------------------------------------------------------------------
# Property 9: Execution Trace Hash Chain Integrity
# Validates: Requirements 6.2
# ---------------------------------------------------------------------------


class TestHashChainIntegrity:
    """For any sequence of N trace records, record[n].previous_hash SHALL equal
    record[n-1].record_hash for all n > 0, and record[0].previous_hash SHALL
    equal the configured genesis hash. Furthermore, recomputing record[n].record_hash
    from its fields produces the stored hash value."""

    @given(decisions=st.lists(decision_st(), min_size=1, max_size=20))
    @settings(max_examples=100)
    def test_first_record_chains_to_genesis(
        self, decisions: list[dict[str, Any]]
    ) -> None:
        """**Validates: Requirements 6.2**

        The first record's previous_hash must equal the configured genesis hash.
        """
        trace = ExecutionTrace()

        # Record the first decision
        d = decisions[0]
        record = trace.record_decision(
            task_id=d["task_id"],
            decision_type=d["decision_type"],
            state_snapshot=d["state_snapshot"],
            policy=d["policy"],
            outcome=d["outcome"],
            nd_inputs=d["nd_inputs"],
        )

        assert record.previous_hash == GENESIS_HASH
        assert record.sequence_number == 0

    @given(decisions=st.lists(decision_st(), min_size=2, max_size=20))
    @settings(max_examples=100)
    def test_each_record_chains_to_previous(
        self, decisions: list[dict[str, Any]]
    ) -> None:
        """**Validates: Requirements 6.2**

        For all n > 0, record[n].previous_hash equals record[n-1].record_hash.
        """
        trace = ExecutionTrace()

        records: list[TraceRecord] = []
        for d in decisions:
            record = trace.record_decision(
                task_id=d["task_id"],
                decision_type=d["decision_type"],
                state_snapshot=d["state_snapshot"],
                policy=d["policy"],
                outcome=d["outcome"],
                nd_inputs=d["nd_inputs"],
            )
            records.append(record)

        # Verify chain linkage
        for i in range(1, len(records)):
            assert records[i].previous_hash == records[i - 1].record_hash, (
                f"Record {i} previous_hash does not match record {i-1} record_hash"
            )

    @given(decisions=st.lists(decision_st(), min_size=1, max_size=20))
    @settings(max_examples=100)
    def test_record_hash_recomputation_matches(
        self, decisions: list[dict[str, Any]]
    ) -> None:
        """**Validates: Requirements 6.2**

        Recomputing any record's hash from its fields produces the stored hash.
        """
        trace = ExecutionTrace()

        for d in decisions:
            trace.record_decision(
                task_id=d["task_id"],
                decision_type=d["decision_type"],
                state_snapshot=d["state_snapshot"],
                policy=d["policy"],
                outcome=d["outcome"],
                nd_inputs=d["nd_inputs"],
            )

        # Verify each record's hash can be recomputed
        for record in trace.records:
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
            assert record.record_hash == recomputed, (
                f"Record {record.sequence_number} stored hash does not match recomputed"
            )

    @given(decisions=st.lists(decision_st(), min_size=1, max_size=20))
    @settings(max_examples=100)
    def test_verify_chain_integrity_succeeds(
        self, decisions: list[dict[str, Any]]
    ) -> None:
        """**Validates: Requirements 6.2**

        verify_chain_integrity() over the full range succeeds for any valid
        sequence of recorded decisions.
        """
        trace = ExecutionTrace()

        for d in decisions:
            trace.record_decision(
                task_id=d["task_id"],
                decision_type=d["decision_type"],
                state_snapshot=d["state_snapshot"],
                policy=d["policy"],
                outcome=d["outcome"],
                nd_inputs=d["nd_inputs"],
            )

        n = len(decisions)
        assert trace.verify_chain_integrity(0, n - 1) is True

    @given(
        decisions=st.lists(decision_st(), min_size=3, max_size=20),
        start_offset=st.integers(min_value=0, max_value=5),
    )
    @settings(max_examples=50)
    def test_verify_chain_integrity_partial_range(
        self, decisions: list[dict[str, Any]], start_offset: int
    ) -> None:
        """**Validates: Requirements 6.2**

        Verifying the chain from any point backward must succeed. Partial range
        verification works correctly.
        """
        trace = ExecutionTrace()

        for d in decisions:
            trace.record_decision(
                task_id=d["task_id"],
                decision_type=d["decision_type"],
                state_snapshot=d["state_snapshot"],
                policy=d["policy"],
                outcome=d["outcome"],
                nd_inputs=d["nd_inputs"],
            )

        n = len(decisions)
        start = min(start_offset, n - 1)
        assert trace.verify_chain_integrity(start, n - 1) is True


# ---------------------------------------------------------------------------
# Property 10: Execution Trace Query Correctness
# Validates: Requirements 6.3
# ---------------------------------------------------------------------------


class TestQueryCorrectness:
    """query() filtered by task_id returns only records for that task. Filtering
    by decision_type returns only matching records. Results are ordered by
    sequence number."""

    @given(
        decisions=st.lists(decision_st(), min_size=2, max_size=15),
        filter_task_id=task_id_st(),
    )
    @settings(max_examples=100)
    def test_query_by_task_id_returns_only_matching(
        self, decisions: list[dict[str, Any]], filter_task_id: str
    ) -> None:
        """**Validates: Requirements 6.3**

        query() filtered by task_id returns only records for that task.
        """
        trace = ExecutionTrace()

        # Ensure at least one decision uses the filter_task_id
        decisions[0]["task_id"] = filter_task_id

        for d in decisions:
            trace.record_decision(
                task_id=d["task_id"],
                decision_type=d["decision_type"],
                state_snapshot=d["state_snapshot"],
                policy=d["policy"],
                outcome=d["outcome"],
                nd_inputs=d["nd_inputs"],
            )

        results = trace.query(task_id=filter_task_id)

        # All returned records must match the filter
        for r in results:
            assert r.task_id == filter_task_id

        # Count should match the expected number
        expected_count = sum(1 for d in decisions if d["task_id"] == filter_task_id)
        assert len(results) == expected_count

    @given(
        decisions=st.lists(decision_st(), min_size=2, max_size=15),
        filter_type=decision_type_st(),
    )
    @settings(max_examples=100)
    def test_query_by_decision_type_returns_only_matching(
        self, decisions: list[dict[str, Any]], filter_type: str
    ) -> None:
        """**Validates: Requirements 6.3**

        query() filtered by decision_type returns only matching records.
        """
        trace = ExecutionTrace()

        # Ensure at least one decision uses the filter type
        decisions[0]["decision_type"] = filter_type

        for d in decisions:
            trace.record_decision(
                task_id=d["task_id"],
                decision_type=d["decision_type"],
                state_snapshot=d["state_snapshot"],
                policy=d["policy"],
                outcome=d["outcome"],
                nd_inputs=d["nd_inputs"],
            )

        results = trace.query(decision_type=filter_type)

        # All returned records must match the filter
        for r in results:
            assert r.decision_type == filter_type

        # Count should match the expected number
        expected_count = sum(1 for d in decisions if d["decision_type"] == filter_type)
        assert len(results) == expected_count

    @given(decisions=st.lists(decision_st(), min_size=2, max_size=15))
    @settings(max_examples=100)
    def test_query_results_ordered_by_sequence_number(
        self, decisions: list[dict[str, Any]]
    ) -> None:
        """**Validates: Requirements 6.3**

        Query results are always ordered by sequence number regardless of filters.
        """
        trace = ExecutionTrace()

        for d in decisions:
            trace.record_decision(
                task_id=d["task_id"],
                decision_type=d["decision_type"],
                state_snapshot=d["state_snapshot"],
                policy=d["policy"],
                outcome=d["outcome"],
                nd_inputs=d["nd_inputs"],
            )

        # Query all records
        results = trace.query()
        for i in range(1, len(results)):
            assert results[i].sequence_number > results[i - 1].sequence_number

        # Query by task_id (pick first task)
        first_task_id = decisions[0]["task_id"]
        filtered_results = trace.query(task_id=first_task_id)
        for i in range(1, len(filtered_results)):
            assert filtered_results[i].sequence_number > filtered_results[i - 1].sequence_number

    @given(
        decisions=st.lists(decision_st(), min_size=3, max_size=15),
        filter_task_id=task_id_st(),
        filter_type=decision_type_st(),
    )
    @settings(max_examples=100)
    def test_query_combined_filters_are_intersection(
        self, decisions: list[dict[str, Any]], filter_task_id: str, filter_type: str
    ) -> None:
        """**Validates: Requirements 6.3**

        Combined filters produce the intersection (AND) of individual filters.
        """
        trace = ExecutionTrace()

        # Ensure at least one decision matches both filters
        decisions[0]["task_id"] = filter_task_id
        decisions[0]["decision_type"] = filter_type

        for d in decisions:
            trace.record_decision(
                task_id=d["task_id"],
                decision_type=d["decision_type"],
                state_snapshot=d["state_snapshot"],
                policy=d["policy"],
                outcome=d["outcome"],
                nd_inputs=d["nd_inputs"],
            )

        results = trace.query(task_id=filter_task_id, decision_type=filter_type)

        # All results must match BOTH filters
        for r in results:
            assert r.task_id == filter_task_id
            assert r.decision_type == filter_type

        # Count should match expected
        expected_count = sum(
            1
            for d in decisions
            if d["task_id"] == filter_task_id and d["decision_type"] == filter_type
        )
        assert len(results) == expected_count

    @given(decisions=st.lists(decision_st(), min_size=1, max_size=15))
    @settings(max_examples=50)
    def test_query_no_filter_returns_all(
        self, decisions: list[dict[str, Any]]
    ) -> None:
        """**Validates: Requirements 6.3**

        Query with no filters returns all recorded decisions.
        """
        trace = ExecutionTrace()

        for d in decisions:
            trace.record_decision(
                task_id=d["task_id"],
                decision_type=d["decision_type"],
                state_snapshot=d["state_snapshot"],
                policy=d["policy"],
                outcome=d["outcome"],
                nd_inputs=d["nd_inputs"],
            )

        results = trace.query()
        assert len(results) == len(decisions)


# ---------------------------------------------------------------------------
# Property 11: Execution Trace Replay Determinism
# Validates: Requirements 7.1, 7.2
# ---------------------------------------------------------------------------


class TestReplayDeterminism:
    """replay_verify() with the same inputs produces the same outcome. Given
    deterministic policy, the replay must match the original."""

    @given(decisions=st.lists(decision_st(), min_size=1, max_size=10))
    @settings(max_examples=100)
    def test_replay_matches_original_with_deterministic_policy(
        self, decisions: list[dict[str, Any]]
    ) -> None:
        """**Validates: Requirements 7.1**

        For any trace record recorded with a deterministic policy, replaying
        produces the identical outcome.
        """
        # Register a deterministic policy that derives outcome from state+nd_inputs
        def deterministic_policy(state_snapshot: dict, nd_inputs: dict) -> str:
            """A deterministic policy: outcome based on phase and seed."""
            phase = state_snapshot.get("phase", "planning")
            seed = nd_inputs.get("seed", 0)
            outcomes = ["approved", "rejected", "escalated", "downgraded", "paused", "proceed"]
            idx = (hash(phase) + seed) % len(outcomes)
            return outcomes[idx]

        register_policy("deterministic_test_policy", deterministic_policy)

        trace = ExecutionTrace()

        for d in decisions:
            # Compute expected outcome using the deterministic policy
            expected_outcome = deterministic_policy(d["state_snapshot"], d["nd_inputs"])
            trace.record_decision(
                task_id=d["task_id"],
                decision_type=d["decision_type"],
                state_snapshot=d["state_snapshot"],
                policy="deterministic_test_policy",
                outcome=expected_outcome,
                nd_inputs=d["nd_inputs"],
            )

        # Replay every record and verify match
        for record in trace.records:
            matches, message = trace.replay_verify(record.sequence_number)
            assert matches, (
                f"Replay failed for record {record.sequence_number}: {message}"
            )

    @given(decisions=st.lists(decision_st(), min_size=1, max_size=10))
    @settings(max_examples=100)
    def test_replay_same_inputs_same_outcome(
        self, decisions: list[dict[str, Any]]
    ) -> None:
        """**Validates: Requirements 7.1, 7.2**

        Replaying the same record multiple times always produces the same result.
        """
        def stable_policy(state_snapshot: dict, nd_inputs: dict) -> str:
            budget = state_snapshot.get("budget_remaining", 0.0)
            if budget > 500:
                return "proceed"
            elif budget > 100:
                return "downgraded"
            else:
                return "paused"

        register_policy("stable_budget_policy", stable_policy)

        trace = ExecutionTrace()

        for d in decisions:
            expected = stable_policy(d["state_snapshot"], d["nd_inputs"])
            trace.record_decision(
                task_id=d["task_id"],
                decision_type=d["decision_type"],
                state_snapshot=d["state_snapshot"],
                policy="stable_budget_policy",
                outcome=expected,
                nd_inputs=d["nd_inputs"],
            )

        # Replay each record twice and confirm identical results
        for record in trace.records:
            result1 = trace.replay_verify(record.sequence_number)
            result2 = trace.replay_verify(record.sequence_number)
            assert result1 == result2

    @given(decision=decision_st())
    @settings(max_examples=100)
    def test_replay_captures_non_deterministic_inputs(
        self, decision: dict[str, Any]
    ) -> None:
        """**Validates: Requirements 7.2**

        The trace captures non-deterministic inputs so replay can account for them.
        A policy that uses nd_inputs still replays correctly.
        """
        def nd_aware_policy(state_snapshot: dict, nd_inputs: dict) -> str:
            """Policy whose outcome depends on non-deterministic inputs."""
            response = nd_inputs.get("external_response", "ok")
            if response == "timeout":
                return "escalated"
            elif response == "error":
                return "paused"
            else:
                return "approved"

        register_policy("nd_aware_policy", nd_aware_policy)

        trace = ExecutionTrace()
        expected = nd_aware_policy(decision["state_snapshot"], decision["nd_inputs"])

        trace.record_decision(
            task_id=decision["task_id"],
            decision_type=decision["decision_type"],
            state_snapshot=decision["state_snapshot"],
            policy="nd_aware_policy",
            outcome=expected,
            nd_inputs=decision["nd_inputs"],
        )

        # Verify replay works
        matches, msg = trace.replay_verify(0)
        assert matches, f"Replay with nd_inputs should match: {msg}"

        # Verify the nd_inputs are stored
        stored_record = trace.records[0]
        assert stored_record.non_deterministic_inputs == decision["nd_inputs"]

    @given(decision=decision_st())
    @settings(max_examples=50)
    def test_replay_fails_for_unregistered_policy(
        self, decision: dict[str, Any]
    ) -> None:
        """**Validates: Requirements 7.1**

        Replay returns failure when the policy is not registered.
        """
        trace = ExecutionTrace()

        trace.record_decision(
            task_id=decision["task_id"],
            decision_type=decision["decision_type"],
            state_snapshot=decision["state_snapshot"],
            policy="nonexistent_policy_xyz",
            outcome=decision["outcome"],
            nd_inputs=decision["nd_inputs"],
        )

        matches, msg = trace.replay_verify(0)
        assert not matches
        assert "not registered" in msg

    @given(decision=decision_st())
    @settings(max_examples=50)
    def test_replay_detects_outcome_divergence(
        self, decision: dict[str, Any]
    ) -> None:
        """**Validates: Requirements 7.1**

        When the recorded outcome doesn't match what the policy would produce,
        replay_verify detects the divergence.
        """
        def always_approve(state_snapshot: dict, nd_inputs: dict) -> str:
            return "approved"

        register_policy("always_approve", always_approve)

        trace = ExecutionTrace()

        # Record with a DIFFERENT outcome than what the policy produces
        wrong_outcome = "rejected"  # policy always returns "approved"
        trace.record_decision(
            task_id=decision["task_id"],
            decision_type=decision["decision_type"],
            state_snapshot=decision["state_snapshot"],
            policy="always_approve",
            outcome=wrong_outcome,
            nd_inputs=decision["nd_inputs"],
        )

        matches, msg = trace.replay_verify(0)
        assert not matches
        assert "diverged" in msg
