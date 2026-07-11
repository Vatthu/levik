"""Unit tests for Verification Protocol execution and feedback loop integration.

Tests execute_verification(), run_full_verification(), and outcome classification.

Run with:
  cd services/orchestrator && .venv/bin/python -m pytest tests/test_verification_execution.py -v

Requirements: 27.1, 27.2, 27.3, 27.4, 27.5, 29.1, 29.2, 29.3, 29.4
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from vikram_orchestrator.execution_trace import ExecutionTrace
from vikram_orchestrator.models import HostActionRequest, HostObservation
from vikram_orchestrator.verification_protocol import (
    GeneratedProperty,
    MAX_FIX_ITERATIONS,
    PROPERTY_VERIFICATION_WEIGHT,
    STANDARD_TEST_WEIGHT,
    PropertyResult,
    PropertyType,
    VerificationExecutionResult,
    VerificationOutcome,
    VerificationProtocol,
    VerificationStrategy,
    _extract_counterexample,
    _extract_iterations,
    _extract_shrunk_input,
    _build_diagnostic,
)


def _make_property(
    property_id: str = "prop-001-invariant",
    property_type: PropertyType = PropertyType.INVARIANT,
    framework: str = "hypothesis",
) -> GeneratedProperty:
    """Create a test GeneratedProperty."""
    return GeneratedProperty(
        property_id=property_id,
        property_type=property_type,
        description="Test property description",
        test_code="def test_prop(): assert True",
        framework=framework,
        derivation_rationale="Derived from plan statement",
        plan_statement_ref="line:5",
    )


def _make_host_client(exec_results: list[HostObservation] | None = None) -> MagicMock:
    """Create a mock HostClient with configurable exec results."""
    client = MagicMock()
    if exec_results:
        client.exec.side_effect = exec_results
    else:
        client.exec.return_value = HostObservation(
            task_id="test-task",
            action_name="exec",
            success=True,
            exit_code=0,
            summary="Tests passed",
            output="1 passed in 0.5s\n100 tests",
        )
    return client


class TestExecuteVerification(unittest.TestCase):
    """Tests for execute_verification() (Requirements 27.1–27.5)."""

    def setUp(self) -> None:
        self.protocol = VerificationProtocol()

    def test_all_properties_pass_with_standard_tests(self) -> None:
        """Verified-correct when all properties and standard tests pass (Req 27.4)."""
        client = _make_host_client()
        properties = [_make_property("prop-001-invariant"), _make_property("prop-002-invariant")]

        result = self.protocol.execute_verification(
            worktree="/tmp/worktree",
            strategy=VerificationStrategy.PROPERTY,
            properties=properties,
            host_client=client,
            task_id="test-task",
        )

        self.assertEqual(result.outcome, VerificationOutcome.VERIFIED_CORRECT)
        self.assertTrue(result.all_properties_passed)
        self.assertTrue(result.standard_tests_passed)
        self.assertEqual(len(result.property_results), 2)

    def test_no_properties_standard_pass(self) -> None:
        """Tests-pass outcome when no properties generated (Req 27.4)."""
        client = _make_host_client()

        result = self.protocol.execute_verification(
            worktree="/tmp/worktree",
            strategy=VerificationStrategy.STANDARD,
            properties=[],
            host_client=client,
            task_id="test-task",
        )

        self.assertEqual(result.outcome, VerificationOutcome.TESTS_PASS)
        self.assertFalse(result.all_properties_passed)
        self.assertTrue(result.standard_tests_passed)

    def test_property_failure_captures_counterexample(self) -> None:
        """Failures capture counterexample and diagnostics (Req 27.2, 27.3)."""
        fail_output = (
            "FAILED test_prop.py::test_prop - AssertionError\n"
            "Falsifying example: func(x='abc', y=42)\n"
            "Shrunk: func(x='', y=0)\n"
            "50 tests"
        )
        fail_obs = HostObservation(
            task_id="test-task",
            action_name="exec",
            success=False,
            exit_code=1,
            summary="Tests failed",
            output=fail_output,
        )
        # First call fails (property), second succeeds (standard tests)
        pass_obs = HostObservation(
            task_id="test-task",
            action_name="exec",
            success=True,
            exit_code=0,
            summary="OK",
            output="All tests passed\n10 tests",
        )
        client = _make_host_client([fail_obs, pass_obs])
        properties = [_make_property()]

        result = self.protocol.execute_verification(
            worktree="/tmp/worktree",
            strategy=VerificationStrategy.PROPERTY,
            properties=properties,
            host_client=client,
            task_id="test-task",
        )

        self.assertEqual(result.outcome, VerificationOutcome.FAILED)
        self.assertFalse(result.all_properties_passed)
        self.assertEqual(len(result.property_results), 1)

        prop_result = result.property_results[0]
        self.assertFalse(prop_result.passed)
        self.assertEqual(prop_result.counterexample, "func(x='abc', y=42)")
        self.assertIsNotNone(prop_result.diagnostic)

    def test_exec_exception_produces_failed_result(self) -> None:
        """Execution exceptions produce a failed PropertyResult with diagnostic."""
        client = MagicMock()
        client.exec.side_effect = Exception("Connection refused")
        properties = [_make_property()]

        result = self.protocol.execute_verification(
            worktree="/tmp/worktree",
            strategy=VerificationStrategy.PROPERTY,
            properties=properties,
            host_client=client,
            task_id="test-task",
        )

        self.assertEqual(result.outcome, VerificationOutcome.FAILED)
        prop_result = result.property_results[0]
        self.assertFalse(prop_result.passed)
        self.assertIn("Connection refused", prop_result.diagnostic)

    def test_minimal_strategy_skips_standard_tests(self) -> None:
        """MINIMAL strategy with passing properties returns verified-correct."""
        client = _make_host_client()
        properties = [_make_property()]

        result = self.protocol.execute_verification(
            worktree="/tmp/worktree",
            strategy=VerificationStrategy.MINIMAL,
            properties=properties,
            host_client=client,
            task_id="test-task",
        )

        # MINIMAL strategy doesn't run standard tests
        self.assertEqual(result.outcome, VerificationOutcome.VERIFIED_CORRECT)

    def test_merge_gate_readiness_all_pass(self) -> None:
        """Readiness is 1.0 when all properties and standard tests pass (Req 27.5)."""
        client = _make_host_client()
        properties = [_make_property()]

        result = self.protocol.execute_verification(
            worktree="/tmp/worktree",
            strategy=VerificationStrategy.PROPERTY,
            properties=properties,
            host_client=client,
            task_id="test-task",
        )

        self.assertEqual(result.merge_gate_readiness, 1.0)

    def test_merge_gate_readiness_partial_property_pass(self) -> None:
        """Readiness reflects partial property pass with proper weighting (Req 27.5)."""
        pass_obs = HostObservation(
            task_id="t", action_name="exec", success=True,
            exit_code=0, summary="OK", output="100 tests",
        )
        fail_obs = HostObservation(
            task_id="t", action_name="exec", success=False,
            exit_code=1, summary="Fail", output="Falsifying example: x=1\n50 tests",
        )
        # Property 1 passes, property 2 fails, standard tests pass
        client = _make_host_client([pass_obs, fail_obs, pass_obs])
        properties = [_make_property("p1"), _make_property("p2")]

        result = self.protocol.execute_verification(
            worktree="/tmp/worktree",
            strategy=VerificationStrategy.PROPERTY,
            properties=properties,
            host_client=client,
            task_id="test-task",
        )

        # 1/2 properties pass = 0.5 property score
        # Standard tests pass = 1.0
        expected = PROPERTY_VERIFICATION_WEIGHT * 0.5 + STANDARD_TEST_WEIGHT * 1.0
        self.assertAlmostEqual(result.merge_gate_readiness, expected, places=4)

    def test_merge_gate_readiness_no_properties(self) -> None:
        """Without properties, standard tests carry full weight (Req 27.5)."""
        client = _make_host_client()

        result = self.protocol.execute_verification(
            worktree="/tmp/worktree",
            strategy=VerificationStrategy.STANDARD,
            properties=[],
            host_client=client,
            task_id="test-task",
        )

        self.assertEqual(result.merge_gate_readiness, 1.0)

    def test_execution_trace_recorded(self) -> None:
        """Verification execution records decision in trace."""
        trace = ExecutionTrace()
        protocol = VerificationProtocol(execution_trace=trace)
        client = _make_host_client()
        properties = [_make_property()]

        protocol.execute_verification(
            worktree="/tmp/worktree",
            strategy=VerificationStrategy.PROPERTY,
            properties=properties,
            host_client=client,
            task_id="test-task-123",
        )

        # Verify trace was called
        records = trace.query(task_id="test-task-123")
        self.assertTrue(len(records) > 0)


class TestRunFullVerification(unittest.TestCase):
    """Tests for run_full_verification() (Requirements 27.1–27.5, 29.1–29.4)."""

    def setUp(self) -> None:
        self.protocol = VerificationProtocol()

    def test_immediate_success_no_iterations(self) -> None:
        """When all pass on first try, returns immediately without fix iterations."""
        client = _make_host_client()
        properties = [_make_property()]

        result = self.protocol.run_full_verification(
            worktree="/tmp/worktree",
            strategy=VerificationStrategy.PROPERTY,
            properties=properties,
            host_client=client,
            task_id="test-task",
        )

        self.assertEqual(result.outcome, VerificationOutcome.VERIFIED_CORRECT)
        self.assertTrue(result.all_properties_passed)

    def test_no_callback_returns_failure_without_retry(self) -> None:
        """Without fix callback, failures are returned immediately."""
        fail_obs = HostObservation(
            task_id="t", action_name="exec", success=False,
            exit_code=1, summary="Fail", output="Falsifying example: x=0\n10 tests",
        )
        pass_obs = HostObservation(
            task_id="t", action_name="exec", success=True,
            exit_code=0, summary="OK", output="5 tests",
        )
        client = _make_host_client([fail_obs, pass_obs])
        properties = [_make_property()]

        result = self.protocol.run_full_verification(
            worktree="/tmp/worktree",
            strategy=VerificationStrategy.PROPERTY,
            properties=properties,
            host_client=client,
            task_id="test-task",
            apply_fix_callback=None,
        )

        self.assertEqual(result.outcome, VerificationOutcome.FAILED)

    def test_fix_succeeds_on_second_iteration(self) -> None:
        """Fix callback resolves failure on second try (Req 29.1, 29.4)."""
        fail_obs = HostObservation(
            task_id="t", action_name="exec", success=False,
            exit_code=1, summary="Fail", output="Falsifying example: x=1\n10 tests",
        )
        pass_obs = HostObservation(
            task_id="t", action_name="exec", success=True,
            exit_code=0, summary="OK", output="100 tests",
        )
        # Sequence: prop fails, standard passes, then (after fix) prop passes, standard passes
        client = _make_host_client([fail_obs, pass_obs, pass_obs, pass_obs])
        properties = [_make_property()]

        fix_callback = MagicMock(return_value=True)

        result = self.protocol.run_full_verification(
            worktree="/tmp/worktree",
            strategy=VerificationStrategy.PROPERTY,
            properties=properties,
            host_client=client,
            task_id="test-task",
            apply_fix_callback=fix_callback,
        )

        self.assertEqual(result.outcome, VerificationOutcome.VERIFIED_CORRECT)
        fix_callback.assert_called_once()
        # Verify the fix prompt was passed to callback
        call_args = fix_callback.call_args[0]
        self.assertIn("prop-001-invariant", call_args[0])

    def test_escalates_after_max_iterations(self) -> None:
        """Escalates to founder after MAX_FIX_ITERATIONS failures (Req 29.2, 29.3)."""
        fail_obs = HostObservation(
            task_id="t", action_name="exec", success=False,
            exit_code=1, summary="Fail", output="Falsifying example: x=bad\n10 tests",
        )
        pass_obs = HostObservation(
            task_id="t", action_name="exec", success=True,
            exit_code=0, summary="OK", output="5 tests",
        )
        # All iterations fail: each iteration = 1 prop fail + 1 standard pass
        exec_results = []
        for _ in range(MAX_FIX_ITERATIONS + 1):  # initial + MAX retries
            exec_results.append(fail_obs)
            exec_results.append(pass_obs)
        client = _make_host_client(exec_results)
        properties = [_make_property()]

        fix_callback = MagicMock(return_value=True)

        result = self.protocol.run_full_verification(
            worktree="/tmp/worktree",
            strategy=VerificationStrategy.PROPERTY,
            properties=properties,
            host_client=client,
            task_id="test-task",
            apply_fix_callback=fix_callback,
        )

        self.assertEqual(result.outcome, VerificationOutcome.FAILED)
        # Fix callback called MAX_FIX_ITERATIONS times
        self.assertEqual(fix_callback.call_count, MAX_FIX_ITERATIONS)

    def test_max_iterations_is_three(self) -> None:
        """Verify MAX_FIX_ITERATIONS is 3 per Requirement 29.2."""
        self.assertEqual(MAX_FIX_ITERATIONS, 3)

    def test_records_trace_with_iteration_count(self) -> None:
        """Records iteration count in trace for learning (Req 29.4)."""
        trace = ExecutionTrace()
        protocol = VerificationProtocol(execution_trace=trace)
        client = _make_host_client()
        properties = [_make_property()]

        protocol.run_full_verification(
            worktree="/tmp/worktree",
            strategy=VerificationStrategy.PROPERTY,
            properties=properties,
            host_client=client,
            task_id="trace-task",
        )

        records = trace.query(task_id="trace-task")
        # Should have verification_execution + full_verification_result
        full_result_records = [
            r for r in records if r.decision_type == "full_verification_result"
        ]
        self.assertEqual(len(full_result_records), 1)
        snapshot = full_result_records[0].state_snapshot
        self.assertEqual(snapshot["iterations_used"], 0)
        self.assertFalse(snapshot["escalated"])

    def test_tests_pass_outcome_without_properties(self) -> None:
        """Standard strategy without properties yields TESTS_PASS (Req 27.4)."""
        client = _make_host_client()

        result = self.protocol.run_full_verification(
            worktree="/tmp/worktree",
            strategy=VerificationStrategy.STANDARD,
            properties=[],
            host_client=client,
            task_id="test-task",
        )

        self.assertEqual(result.outcome, VerificationOutcome.TESTS_PASS)


class TestCounterexampleExtraction(unittest.TestCase):
    """Tests for counterexample and shrunk input extraction (Req 27.2)."""

    def test_hypothesis_counterexample(self) -> None:
        output = "FAILED\nFalsifying example: func(x='hello', y=3)\nmore output"
        result = _extract_counterexample(output, "hypothesis")
        self.assertEqual(result, "func(x='hello', y=3)")

    def test_fast_check_counterexample(self) -> None:
        output = "Property failed:\nCounterexample: [42, \"abc\"]\nShrunk 3 time(s)"
        result = _extract_counterexample(output, "fast-check")
        self.assertEqual(result, "[42, \"abc\"]")

    def test_quickcheck_counterexample(self) -> None:
        output = "*** Failed! Falsified (after 5 tests and 2 shrinks):\n\"bad input\"\n\nDone"
        result = _extract_counterexample(output, "quickcheck")
        self.assertEqual(result, "\"bad input\"")

    def test_no_counterexample_returns_none(self) -> None:
        result = _extract_counterexample("All tests passed", "hypothesis")
        self.assertIsNone(result)

    def test_empty_output_returns_none(self) -> None:
        result = _extract_counterexample("", "hypothesis")
        self.assertIsNone(result)

    def test_hypothesis_shrunk_input(self) -> None:
        output = "Shrunk: func(x='')\nFalsifying example: func(x='abc')"
        result = _extract_shrunk_input(output, "hypothesis")
        self.assertEqual(result, "func(x='')")

    def test_fast_check_shrunk_input(self) -> None:
        output = "Shrunk 5 time(s) [0, \"\"]"
        result = _extract_shrunk_input(output, "fast-check")
        self.assertEqual(result, "[0, \"\"]")


class TestIterationsExtraction(unittest.TestCase):
    """Tests for iterations extraction from test output."""

    def test_extracts_test_count(self) -> None:
        self.assertEqual(_extract_iterations("100 tests"), 100)

    def test_extracts_examples_count(self) -> None:
        self.assertEqual(_extract_iterations("ran 50 examples"), 50)

    def test_empty_output_returns_zero(self) -> None:
        self.assertEqual(_extract_iterations(""), 0)

    def test_no_match_returns_zero(self) -> None:
        self.assertEqual(_extract_iterations("all good"), 0)


class TestBuildDiagnostic(unittest.TestCase):
    """Tests for diagnostic building (Req 27.3)."""

    def test_includes_property_id(self) -> None:
        prop = _make_property("my-prop-id")
        diagnostic = _build_diagnostic(prop, "some output")
        self.assertIn("my-prop-id", diagnostic)

    def test_includes_property_type(self) -> None:
        prop = _make_property(property_type=PropertyType.ROUND_TRIP)
        diagnostic = _build_diagnostic(prop, "some output")
        self.assertIn("round_trip_consistency", diagnostic)

    def test_includes_plan_reference(self) -> None:
        prop = _make_property()
        diagnostic = _build_diagnostic(prop, "some output")
        self.assertIn("line:5", diagnostic)

    def test_includes_description(self) -> None:
        prop = _make_property()
        diagnostic = _build_diagnostic(prop, "")
        self.assertIn("Test property description", diagnostic)


class TestOutcomeClassification(unittest.TestCase):
    """Tests for _classify_outcome distinguishing verified-correct vs tests-pass (Req 27.4)."""

    def setUp(self) -> None:
        self.protocol = VerificationProtocol()

    def test_verified_correct_requires_properties_and_standard(self) -> None:
        """All properties pass + standard tests pass = VERIFIED_CORRECT."""
        props = [_make_property()]
        outcome = self.protocol._classify_outcome(
            VerificationStrategy.PROPERTY, True, True, props
        )
        self.assertEqual(outcome, VerificationOutcome.VERIFIED_CORRECT)

    def test_tests_pass_without_properties(self) -> None:
        """Standard tests pass, no properties = TESTS_PASS."""
        outcome = self.protocol._classify_outcome(
            VerificationStrategy.STANDARD, False, True, []
        )
        self.assertEqual(outcome, VerificationOutcome.TESTS_PASS)

    def test_failed_when_properties_fail(self) -> None:
        """Properties fail even if standard tests pass = FAILED."""
        props = [_make_property()]
        outcome = self.protocol._classify_outcome(
            VerificationStrategy.PROPERTY, False, True, props
        )
        self.assertEqual(outcome, VerificationOutcome.FAILED)

    def test_failed_when_nothing_passes(self) -> None:
        """Nothing passes = FAILED."""
        props = [_make_property()]
        outcome = self.protocol._classify_outcome(
            VerificationStrategy.PROPERTY, False, False, props
        )
        self.assertEqual(outcome, VerificationOutcome.FAILED)

    def test_minimal_strategy_verified_correct_without_standard(self) -> None:
        """MINIMAL strategy: properties pass without running standard tests."""
        props = [_make_property()]
        outcome = self.protocol._classify_outcome(
            VerificationStrategy.MINIMAL, True, False, props
        )
        self.assertEqual(outcome, VerificationOutcome.VERIFIED_CORRECT)


class TestMergeGateReadiness(unittest.TestCase):
    """Tests for merge gate readiness weighting (Req 27.5)."""

    def test_property_weight_is_70_percent(self) -> None:
        self.assertEqual(PROPERTY_VERIFICATION_WEIGHT, 0.7)

    def test_standard_weight_is_30_percent(self) -> None:
        self.assertEqual(STANDARD_TEST_WEIGHT, 0.3)

    def test_full_pass_yields_1_0(self) -> None:
        results = [PropertyResult(property_id="p1", passed=True)]
        readiness = VerificationProtocol._compute_merge_gate_readiness(True, True, results)
        self.assertEqual(readiness, 1.0)

    def test_no_properties_standard_pass_yields_1_0(self) -> None:
        readiness = VerificationProtocol._compute_merge_gate_readiness(False, True, [])
        self.assertEqual(readiness, 1.0)

    def test_no_properties_standard_fail_yields_0_0(self) -> None:
        readiness = VerificationProtocol._compute_merge_gate_readiness(False, False, [])
        self.assertEqual(readiness, 0.0)

    def test_half_properties_pass(self) -> None:
        results = [
            PropertyResult(property_id="p1", passed=True),
            PropertyResult(property_id="p2", passed=False),
        ]
        readiness = VerificationProtocol._compute_merge_gate_readiness(False, True, results)
        expected = 0.7 * 0.5 + 0.3 * 1.0
        self.assertAlmostEqual(readiness, expected, places=4)


if __name__ == "__main__":
    unittest.main()
