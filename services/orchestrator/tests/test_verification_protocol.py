"""Unit tests for the Verification Protocol subsystem.

Tests strategy selection logic and execution trace recording.

Run with:
  cd services/orchestrator && .venv/bin/python -m pytest tests/test_verification_protocol.py -v

Requirements: 28.1, 28.2, 28.3, 28.4
"""

from __future__ import annotations

import unittest

from vikram_orchestrator.execution_trace import ExecutionTrace
from vikram_orchestrator.verification_protocol import (
    FeedbackLoopResult,
    GeneratedProperty,
    MAX_FIX_ITERATIONS,
    PropertyResult,
    PropertyType,
    StrategySelection,
    VerificationExecutionResult,
    VerificationOutcome,
    VerificationProtocol,
    VerificationStrategy,
)


class TestStrategySelectionPureFunctions(unittest.TestCase):
    """Strategy selection for pure functions/parsers/serializers → PROPERTY (Req 28.1)."""

    def setUp(self) -> None:
        self.protocol = VerificationProtocol()

    def test_pure_function_selects_property(self) -> None:
        result = self.protocol.select_strategy("pure_function")
        self.assertEqual(result, VerificationStrategy.PROPERTY)

    def test_parser_selects_property(self) -> None:
        result = self.protocol.select_strategy("parser")
        self.assertEqual(result, VerificationStrategy.PROPERTY)

    def test_serializer_selects_property(self) -> None:
        result = self.protocol.select_strategy("serializer")
        self.assertEqual(result, VerificationStrategy.PROPERTY)

    def test_codec_selects_property(self) -> None:
        result = self.protocol.select_strategy("codec")
        self.assertEqual(result, VerificationStrategy.PROPERTY)

    def test_transform_selects_property(self) -> None:
        result = self.protocol.select_strategy("transform")
        self.assertEqual(result, VerificationStrategy.PROPERTY)


class TestStrategySelectionStateMachines(unittest.TestCase):
    """Strategy selection for state machines/workflows → PROPERTY (Req 28.2)."""

    def setUp(self) -> None:
        self.protocol = VerificationProtocol()

    def test_state_machine_selects_property(self) -> None:
        result = self.protocol.select_strategy("state_machine")
        self.assertEqual(result, VerificationStrategy.PROPERTY)

    def test_workflow_selects_property(self) -> None:
        result = self.protocol.select_strategy("workflow")
        self.assertEqual(result, VerificationStrategy.PROPERTY)

    def test_protocol_selects_property(self) -> None:
        result = self.protocol.select_strategy("protocol")
        self.assertEqual(result, VerificationStrategy.PROPERTY)


class TestStrategySelectionMinimal(unittest.TestCase):
    """Strategy selection for docs/config → MINIMAL (Req 28.3)."""

    def setUp(self) -> None:
        self.protocol = VerificationProtocol()

    def test_documentation_selects_minimal(self) -> None:
        result = self.protocol.select_strategy("documentation")
        self.assertEqual(result, VerificationStrategy.MINIMAL)

    def test_docs_selects_minimal(self) -> None:
        result = self.protocol.select_strategy("docs")
        self.assertEqual(result, VerificationStrategy.MINIMAL)

    def test_config_selects_minimal(self) -> None:
        result = self.protocol.select_strategy("config")
        self.assertEqual(result, VerificationStrategy.MINIMAL)

    def test_configuration_selects_minimal(self) -> None:
        result = self.protocol.select_strategy("configuration")
        self.assertEqual(result, VerificationStrategy.MINIMAL)

    def test_readme_selects_minimal(self) -> None:
        result = self.protocol.select_strategy("readme")
        self.assertEqual(result, VerificationStrategy.MINIMAL)

    def test_changelog_selects_minimal(self) -> None:
        result = self.protocol.select_strategy("changelog")
        self.assertEqual(result, VerificationStrategy.MINIMAL)

    def test_all_doc_file_types_selects_minimal(self) -> None:
        """When all file types are doc/config extensions, use MINIMAL even if change_type is generic."""
        result = self.protocol.select_strategy("unknown", file_types=[".md", ".yaml"])
        self.assertEqual(result, VerificationStrategy.MINIMAL)

    def test_mixed_file_types_does_not_use_minimal(self) -> None:
        """Mixed doc + code files should not default to MINIMAL."""
        result = self.protocol.select_strategy("unknown", file_types=[".md", ".py"])
        self.assertNotEqual(result, VerificationStrategy.MINIMAL)


class TestStrategySelectionStandard(unittest.TestCase):
    """Strategy selection for refactoring/feature/unknown → STANDARD."""

    def setUp(self) -> None:
        self.protocol = VerificationProtocol()

    def test_refactoring_selects_standard(self) -> None:
        result = self.protocol.select_strategy("refactoring")
        self.assertEqual(result, VerificationStrategy.STANDARD)

    def test_new_feature_selects_standard(self) -> None:
        result = self.protocol.select_strategy("new_feature")
        self.assertEqual(result, VerificationStrategy.STANDARD)

    def test_unknown_selects_standard(self) -> None:
        result = self.protocol.select_strategy("something_else")
        self.assertEqual(result, VerificationStrategy.STANDARD)

    def test_case_insensitive_matching(self) -> None:
        """Strategy selection should be case-insensitive."""
        result = self.protocol.select_strategy("Pure_Function")
        self.assertEqual(result, VerificationStrategy.PROPERTY)

    def test_whitespace_trimmed(self) -> None:
        """Strategy selection should trim whitespace."""
        result = self.protocol.select_strategy("  documentation  ")
        self.assertEqual(result, VerificationStrategy.MINIMAL)


class TestStrategySelectionTraceRecording(unittest.TestCase):
    """Strategy selection records decisions in Execution Trace (Req 28.4)."""

    def test_records_decision_in_trace(self) -> None:
        trace = ExecutionTrace()
        protocol = VerificationProtocol(execution_trace=trace)

        protocol.select_strategy("pure_function", task_id="task-123")

        records = trace.query(task_id="task-123")
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.decision_type, "verification_strategy_selection")
        self.assertEqual(record.outcome, "property")
        self.assertEqual(record.state_snapshot["change_type"], "pure_function")

    def test_rationale_recorded_in_nd_inputs(self) -> None:
        trace = ExecutionTrace()
        protocol = VerificationProtocol(execution_trace=trace)

        protocol.select_strategy("documentation", task_id="task-456")

        records = trace.query(task_id="task-456")
        self.assertEqual(len(records), 1)
        self.assertIn("rationale", records[0].non_deterministic_inputs)
        self.assertIn("28.3", records[0].non_deterministic_inputs["rationale"])

    def test_no_trace_when_trace_not_provided(self) -> None:
        """When no ExecutionTrace is provided, no error occurs."""
        protocol = VerificationProtocol()  # No trace
        # Should not raise
        result = protocol.select_strategy("pure_function", task_id="task-789")
        self.assertEqual(result, VerificationStrategy.PROPERTY)


class TestFeedbackLoop(unittest.TestCase):
    """Tests for the feedback loop iteration bound (Requirements 29.2, 29.3)."""

    def setUp(self) -> None:
        self.protocol = VerificationProtocol()
        self.failure = PropertyResult(
            property_id="prop-1",
            passed=False,
            counterexample="input=[1, 2, 3]",
            iterations=50,
            shrunk_input="[1]",
            diagnostic="Expected sorted output but got [2, 1, 3]",
        )

    def test_attempt_1_retries(self) -> None:
        result = self.protocol.feedback_loop(self.failure, 1)
        self.assertTrue(result.should_retry)
        self.assertFalse(result.escalate)
        self.assertIsNotNone(result.fix_prompt)

    def test_attempt_at_max_still_retries(self) -> None:
        result = self.protocol.feedback_loop(self.failure, MAX_FIX_ITERATIONS)
        self.assertTrue(result.should_retry)
        self.assertFalse(result.escalate)

    def test_attempt_past_max_escalates(self) -> None:
        result = self.protocol.feedback_loop(self.failure, MAX_FIX_ITERATIONS + 1)
        self.assertFalse(result.should_retry)
        self.assertTrue(result.escalate)
        self.assertIsNotNone(result.failure_details)

    def test_escalation_preserves_failure_details(self) -> None:
        result = self.protocol.feedback_loop(self.failure, MAX_FIX_ITERATIONS + 1)
        self.assertEqual(result.failure_details.property_id, "prop-1")
        self.assertEqual(result.failure_details.counterexample, "input=[1, 2, 3]")

    def test_fix_prompt_includes_counterexample(self) -> None:
        result = self.protocol.feedback_loop(self.failure, 1)
        self.assertIn("input=[1, 2, 3]", result.fix_prompt)

    def test_fix_prompt_includes_property_id(self) -> None:
        result = self.protocol.feedback_loop(self.failure, 1)
        self.assertIn("prop-1", result.fix_prompt)


class TestModels(unittest.TestCase):
    """Test that data models are properly structured."""

    def test_property_type_values(self) -> None:
        self.assertEqual(PropertyType.INVARIANT, "invariant_preservation")
        self.assertEqual(PropertyType.ROUND_TRIP, "round_trip_consistency")
        self.assertEqual(PropertyType.IDEMPOTENCE, "idempotence")
        self.assertEqual(PropertyType.METAMORPHIC, "metamorphic_relationship")
        self.assertEqual(PropertyType.ERROR_CONDITION, "error_condition")
        self.assertEqual(PropertyType.STATE_INVARIANT, "state_invariant")
        self.assertEqual(PropertyType.TRANSITION_COVERAGE, "transition_coverage")

    def test_verification_strategy_values(self) -> None:
        self.assertEqual(VerificationStrategy.MINIMAL, "minimal")
        self.assertEqual(VerificationStrategy.STANDARD, "standard")
        self.assertEqual(VerificationStrategy.PROPERTY, "property")
        self.assertEqual(VerificationStrategy.COMPREHENSIVE, "comprehensive")

    def test_generated_property_model(self) -> None:
        prop = GeneratedProperty(
            property_id="test-1",
            property_type=PropertyType.ROUND_TRIP,
            description="Round-trip encoding/decoding",
            test_code="def test_roundtrip(): ...",
            framework="hypothesis",
            derivation_rationale="Plan states encode/decode must be lossless",
            plan_statement_ref="plan.invariants[0]",
        )
        self.assertEqual(prop.property_id, "test-1")
        self.assertEqual(prop.property_type, PropertyType.ROUND_TRIP)
        self.assertEqual(prop.framework, "hypothesis")

    def test_property_result_model(self) -> None:
        result = PropertyResult(
            property_id="test-1",
            passed=True,
            counterexample=None,
            iterations=100,
            shrunk_input=None,
            diagnostic=None,
        )
        self.assertTrue(result.passed)
        self.assertIsNone(result.counterexample)

    def test_feedback_loop_result_model(self) -> None:
        result = FeedbackLoopResult(
            should_retry=True,
            escalate=False,
            fix_prompt="Fix the bug",
            failure_details=None,
        )
        self.assertTrue(result.should_retry)
        self.assertFalse(result.escalate)


class TestStrategySelectionEdgeCases(unittest.TestCase):
    """Additional edge cases for strategy selection (Req 28.1, 28.2, 28.3).

    Validates Requirements: 28.1, 28.2, 28.3
    """

    def setUp(self) -> None:
        self.protocol = VerificationProtocol()

    def test_finite_automaton_selects_property(self) -> None:
        """finite_automaton is a state machine variant → PROPERTY (Req 28.2)."""
        result = self.protocol.select_strategy("finite_automaton")
        self.assertEqual(result, VerificationStrategy.PROPERTY)

    def test_comment_selects_minimal(self) -> None:
        """Comment-only changes use MINIMAL verification (Req 28.3)."""
        result = self.protocol.select_strategy("comment")
        self.assertEqual(result, VerificationStrategy.MINIMAL)

    def test_empty_string_defaults_to_standard(self) -> None:
        """Empty string change type defaults to STANDARD."""
        result = self.protocol.select_strategy("")
        self.assertEqual(result, VerificationStrategy.STANDARD)

    def test_mixed_case_state_machine(self) -> None:
        """Mixed case 'State_Machine' should still select PROPERTY."""
        result = self.protocol.select_strategy("State_Machine")
        self.assertEqual(result, VerificationStrategy.PROPERTY)

    def test_file_types_only_code_does_not_override_to_minimal(self) -> None:
        """When file_types are all code (.py, .ts), unknown change_type → STANDARD, not MINIMAL."""
        result = self.protocol.select_strategy("unknown", file_types=[".py", ".ts"])
        self.assertEqual(result, VerificationStrategy.STANDARD)

    def test_file_types_all_config_selects_minimal(self) -> None:
        """When all file_types are config extensions, even with generic change_type → MINIMAL."""
        result = self.protocol.select_strategy("unknown", file_types=[".json", ".toml", ".ini"])
        self.assertEqual(result, VerificationStrategy.MINIMAL)

    def test_file_types_empty_list_defaults_to_standard(self) -> None:
        """Empty file_types list with unknown change_type → STANDARD."""
        result = self.protocol.select_strategy("unknown", file_types=[])
        self.assertEqual(result, VerificationStrategy.STANDARD)

    def test_feature_selects_standard(self) -> None:
        """'feature' (alias of new_feature) → STANDARD."""
        result = self.protocol.select_strategy("feature")
        self.assertEqual(result, VerificationStrategy.STANDARD)

    def test_codec_is_property_not_standard(self) -> None:
        """Codec is a serializer variant and must use PROPERTY, not STANDARD."""
        result = self.protocol.select_strategy("codec")
        self.assertNotEqual(result, VerificationStrategy.STANDARD)
        self.assertEqual(result, VerificationStrategy.PROPERTY)

    def test_strategy_type_takes_precedence_over_file_types(self) -> None:
        """When change_type is 'pure_function' but file_types include .md, type wins."""
        result = self.protocol.select_strategy("pure_function", file_types=[".md", ".py"])
        self.assertEqual(result, VerificationStrategy.PROPERTY)

    def test_docs_type_takes_precedence_over_code_file_types(self) -> None:
        """When change_type is 'documentation' but file_types include .py, type wins."""
        result = self.protocol.select_strategy("documentation", file_types=[".py"])
        self.assertEqual(result, VerificationStrategy.MINIMAL)


class TestFeedbackLoopBoundary(unittest.TestCase):
    """Boundary testing for feedback loop 3-iteration maximum (Req 29.2).

    Validates Requirements: 29.2, 29.3
    """

    def setUp(self) -> None:
        self.protocol = VerificationProtocol()
        self.failure = PropertyResult(
            property_id="prop-boundary",
            passed=False,
            counterexample="x = -1",
            iterations=100,
            shrunk_input="-1",
            diagnostic="Negative input not handled",
        )

    def test_attempt_1_allows_retry(self) -> None:
        """First attempt always allows retry."""
        result = self.protocol.feedback_loop(self.failure, 1)
        self.assertTrue(result.should_retry)
        self.assertFalse(result.escalate)

    def test_attempt_2_allows_retry(self) -> None:
        """Second attempt allows retry."""
        result = self.protocol.feedback_loop(self.failure, 2)
        self.assertTrue(result.should_retry)
        self.assertFalse(result.escalate)

    def test_attempt_3_allows_retry(self) -> None:
        """Third attempt (boundary) still allows retry — this is the last fix attempt."""
        result = self.protocol.feedback_loop(self.failure, 3)
        self.assertTrue(result.should_retry)
        self.assertFalse(result.escalate)
        self.assertIsNotNone(result.fix_prompt)

    def test_attempt_4_escalates(self) -> None:
        """Fourth attempt (MAX+1) triggers escalation to founder."""
        result = self.protocol.feedback_loop(self.failure, 4)
        self.assertFalse(result.should_retry)
        self.assertTrue(result.escalate)
        self.assertIsNotNone(result.failure_details)

    def test_attempt_5_still_escalates(self) -> None:
        """Attempts beyond 4 also escalate (no infinite retries possible)."""
        result = self.protocol.feedback_loop(self.failure, 5)
        self.assertFalse(result.should_retry)
        self.assertTrue(result.escalate)

    def test_escalation_preserves_all_failure_fields(self) -> None:
        """Escalation includes full failure context for founder (Req 29.3)."""
        result = self.protocol.feedback_loop(self.failure, 4)
        self.assertEqual(result.failure_details.property_id, "prop-boundary")
        self.assertEqual(result.failure_details.counterexample, "x = -1")
        self.assertEqual(result.failure_details.shrunk_input, "-1")
        self.assertEqual(result.failure_details.diagnostic, "Negative input not handled")

    def test_sequential_attempts_produce_correct_sequence(self) -> None:
        """Running attempts 1 through 4 produces exactly 3 retries then 1 escalation."""
        retries = 0
        escalated_at = None

        for attempt in range(1, 5):
            result = self.protocol.feedback_loop(self.failure, attempt)
            if result.should_retry:
                retries += 1
            if result.escalate:
                escalated_at = attempt
                break

        self.assertEqual(retries, 3)
        self.assertEqual(escalated_at, 4)

    def test_fix_prompt_includes_attempt_number(self) -> None:
        """Fix prompt should include attempt/max info for agent context."""
        result = self.protocol.feedback_loop(self.failure, 2)
        self.assertIn("2", result.fix_prompt)
        self.assertIn(str(MAX_FIX_ITERATIONS), result.fix_prompt)

    def test_fix_prompt_includes_diagnostic(self) -> None:
        """Fix prompt should include diagnostic info when available."""
        result = self.protocol.feedback_loop(self.failure, 1)
        self.assertIn("Negative input not handled", result.fix_prompt)


class TestPropertyGenerationStructure(unittest.TestCase):
    """Test that GeneratedProperty objects have valid structure (Req 26.3).

    Validates Requirements: 26.2, 26.3

    Since generate_properties() is not yet implemented, these tests validate
    that the GeneratedProperty model enforces the correct structure for
    properties produced in executable format compatible with test frameworks.
    """

    def test_generated_property_requires_all_fields(self) -> None:
        """GeneratedProperty must have all required fields populated."""
        prop = GeneratedProperty(
            property_id="prop-001",
            property_type=PropertyType.ROUND_TRIP,
            description="Encode then decode produces original",
            test_code="@given(st.text())\ndef test_roundtrip(s):\n    assert decode(encode(s)) == s",
            framework="hypothesis",
            derivation_rationale="Plan invariant: encoding is lossless",
            plan_statement_ref="plan.invariants[0]",
        )
        self.assertEqual(prop.property_id, "prop-001")
        self.assertEqual(prop.property_type, PropertyType.ROUND_TRIP)
        self.assertIn("@given", prop.test_code)
        self.assertEqual(prop.framework, "hypothesis")

    def test_generated_property_test_code_nonempty(self) -> None:
        """test_code must be non-empty to be executable (Req 26.3)."""
        prop = GeneratedProperty(
            property_id="prop-002",
            property_type=PropertyType.INVARIANT,
            description="List length preserved after sort",
            test_code="@given(st.lists(st.integers()))\ndef test_sort_length(lst):\n    assert len(sorted(lst)) == len(lst)",
            framework="hypothesis",
            derivation_rationale="Sorting preserves length",
            plan_statement_ref="plan.postconditions[1]",
        )
        self.assertTrue(len(prop.test_code) > 0)

    def test_supported_frameworks_are_valid(self) -> None:
        """Framework field should accept known test frameworks (Req 26.3)."""
        for framework in ("hypothesis", "fast-check", "quickcheck"):
            prop = GeneratedProperty(
                property_id=f"prop-{framework}",
                property_type=PropertyType.IDEMPOTENCE,
                description="Idempotence test",
                test_code="def test(): pass",
                framework=framework,
                derivation_rationale="Test",
                plan_statement_ref="plan.invariants[0]",
            )
            self.assertEqual(prop.framework, framework)

    def test_all_property_types_can_be_used(self) -> None:
        """All PropertyType values can be assigned to GeneratedProperty (Req 26.2)."""
        for pt in PropertyType:
            prop = GeneratedProperty(
                property_id=f"prop-{pt.value}",
                property_type=pt,
                description=f"Test for {pt.value}",
                test_code="def test(): pass",
                framework="hypothesis",
                derivation_rationale=f"Derived for {pt.value}",
                plan_statement_ref="plan.invariants[0]",
            )
            self.assertEqual(prop.property_type, pt)

    def test_derivation_rationale_links_to_plan(self) -> None:
        """derivation_rationale and plan_statement_ref provide traceability (Req 26.4)."""
        prop = GeneratedProperty(
            property_id="prop-trace",
            property_type=PropertyType.METAMORPHIC,
            description="Doubling input doubles output",
            test_code="@given(st.integers())\ndef test_metamorphic(x):\n    assert f(2*x) == 2*f(x)",
            framework="hypothesis",
            derivation_rationale="Plan states: function is linear, f(kx) = k*f(x)",
            plan_statement_ref="plan.invariants[2]",
        )
        self.assertIn("Plan states", prop.derivation_rationale)
        self.assertTrue(prop.plan_statement_ref.startswith("plan."))

    def test_hypothesis_property_code_structure(self) -> None:
        """Hypothesis property test code should contain expected decorators/structure."""
        test_code = (
            "from hypothesis import given\n"
            "import hypothesis.strategies as st\n\n"
            "@given(data=st.lists(st.integers(), min_size=1))\n"
            "def test_sort_idempotent(data):\n"
            "    result = sorted(data)\n"
            "    assert sorted(result) == result\n"
        )
        prop = GeneratedProperty(
            property_id="prop-idem-001",
            property_type=PropertyType.IDEMPOTENCE,
            description="Sorting is idempotent",
            test_code=test_code,
            framework="hypothesis",
            derivation_rationale="Plan postcondition: sort(sort(x)) == sort(x)",
            plan_statement_ref="plan.postconditions[0]",
        )
        # Validate structural expectations for hypothesis tests
        self.assertIn("@given", prop.test_code)
        self.assertIn("def test_", prop.test_code)
        self.assertIn("assert", prop.test_code)

    def test_fastcheck_property_code_structure(self) -> None:
        """fast-check property test code should contain expected structure for TypeScript."""
        test_code = (
            "import * as fc from 'fast-check';\n\n"
            "test('sort is idempotent', () => {\n"
            "  fc.assert(fc.property(\n"
            "    fc.array(fc.integer()),\n"
            "    (arr) => {\n"
            "      const sorted1 = arr.slice().sort();\n"
            "      const sorted2 = sorted1.slice().sort();\n"
            "      expect(sorted2).toEqual(sorted1);\n"
            "    }\n"
            "  ));\n"
            "});\n"
        )
        prop = GeneratedProperty(
            property_id="prop-fc-001",
            property_type=PropertyType.IDEMPOTENCE,
            description="Sorting is idempotent (TypeScript)",
            test_code=test_code,
            framework="fast-check",
            derivation_rationale="Plan postcondition: sort(sort(x)) == sort(x)",
            plan_statement_ref="plan.postconditions[0]",
        )
        self.assertIn("fc.assert", prop.test_code)
        self.assertIn("fc.property", prop.test_code)
        self.assertEqual(prop.framework, "fast-check")


class TestGenerateProperties(unittest.TestCase):
    """Tests for generate_properties() — property generation from execution plans.

    Validates Requirements: 26.1, 26.2, 26.3, 26.4
    """

    def setUp(self) -> None:
        self.protocol = VerificationProtocol()

    def test_empty_plan_returns_empty_list(self) -> None:
        """Empty plan text produces no properties."""
        self.assertEqual(self.protocol.generate_properties("", ["f.py"], "hypothesis"), [])
        self.assertEqual(self.protocol.generate_properties("   ", ["f.py"], "hypothesis"), [])

    def test_extracts_invariants_from_section(self) -> None:
        """Properties are extracted from ## Invariants section (Req 26.1)."""
        plan = "## Invariants\n- The list length is preserved after sorting"
        props = self.protocol.generate_properties(plan, ["src/sort.py"], "hypothesis")
        self.assertEqual(len(props), 1)
        self.assertIn("list length is preserved", props[0].description)

    def test_extracts_preconditions_from_section(self) -> None:
        """Properties are extracted from ## Preconditions section (Req 26.1)."""
        plan = "## Preconditions\n- Input must be a non-empty string"
        props = self.protocol.generate_properties(plan, ["src/parser.py"], "hypothesis")
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0].description, "Input must be a non-empty string")

    def test_extracts_postconditions_from_section(self) -> None:
        """Properties are extracted from ## Postconditions section (Req 26.1)."""
        plan = "## Postconditions\n- Output is always sorted in ascending order"
        props = self.protocol.generate_properties(plan, ["src/sort.py"], "hypothesis")
        self.assertEqual(len(props), 1)

    def test_extracts_inline_invariant_markers(self) -> None:
        """Properties are extracted from inline 'Invariant: ...' markers."""
        plan = "Invariant: total balance must equal sum of all accounts"
        props = self.protocol.generate_properties(plan, ["src/ledger.py"], "hypothesis")
        self.assertEqual(len(props), 1)
        self.assertIn("total balance", props[0].description)

    def test_extracts_inline_precondition(self) -> None:
        """Properties are extracted from 'Precondition: ...' marker."""
        plan = "Precondition: all inputs must be valid UTF-8 strings"
        props = self.protocol.generate_properties(plan, ["src/parser.py"], "hypothesis")
        self.assertEqual(len(props), 1)

    def test_extracts_inline_postcondition(self) -> None:
        """Properties are extracted from 'Postcondition: ...' marker."""
        plan = "Postcondition: output file contains all input records"
        props = self.protocol.generate_properties(plan, ["src/writer.py"], "hypothesis")
        self.assertEqual(len(props), 1)

    def test_extracts_multiple_statements(self) -> None:
        """Multiple invariants/preconditions/postconditions all extracted (Req 26.1)."""
        plan = (
            "## Invariants\n"
            "- Balance never negative\n"
            "- Transaction count is monotonically increasing\n"
            "\n"
            "## Postconditions\n"
            "- Serialize then deserialize is lossless\n"
        )
        props = self.protocol.generate_properties(plan, ["src/bank.py"], "hypothesis")
        self.assertEqual(len(props), 3)

    def test_classifies_round_trip_property(self) -> None:
        """Statements about encode/decode are classified as ROUND_TRIP (Req 26.2)."""
        plan = "Invariant: encoding then decoding produces the original value"
        props = self.protocol.generate_properties(plan, ["src/codec.py"], "hypothesis")
        self.assertEqual(props[0].property_type, PropertyType.ROUND_TRIP)

    def test_classifies_idempotence_property(self) -> None:
        """Statements about repeated application are classified as IDEMPOTENCE (Req 26.2)."""
        plan = "Postcondition: normalization is idempotent"
        props = self.protocol.generate_properties(plan, ["src/norm.py"], "hypothesis")
        self.assertEqual(props[0].property_type, PropertyType.IDEMPOTENCE)

    def test_classifies_metamorphic_property(self) -> None:
        """Statements about metamorphic relations are classified as METAMORPHIC (Req 26.2)."""
        plan = "Property: the metamorphic relationship f(sorted(x)) == sorted(f(x)) holds"
        props = self.protocol.generate_properties(plan, ["src/fn.py"], "hypothesis")
        self.assertEqual(props[0].property_type, PropertyType.METAMORPHIC)

    def test_classifies_error_condition_property(self) -> None:
        """Statements about error handling are classified as ERROR_CONDITION (Req 26.2)."""
        plan = "Invariant: the function must not crash on invalid input"
        props = self.protocol.generate_properties(plan, ["src/fn.py"], "hypothesis")
        self.assertEqual(props[0].property_type, PropertyType.ERROR_CONDITION)

    def test_classifies_state_invariant_property(self) -> None:
        """Statements about state preservation are classified as STATE_INVARIANT (Req 26.2)."""
        plan = "Invariant: consistent state is maintained after every operation"
        props = self.protocol.generate_properties(plan, ["src/sm.py"], "hypothesis")
        self.assertEqual(props[0].property_type, PropertyType.STATE_INVARIANT)

    def test_classifies_transition_coverage_property(self) -> None:
        """Statements about state transitions are classified as TRANSITION_COVERAGE (Req 26.2)."""
        plan = "Postcondition: all states are reachable from the initial state"
        props = self.protocol.generate_properties(plan, ["src/fsm.py"], "hypothesis")
        self.assertEqual(props[0].property_type, PropertyType.TRANSITION_COVERAGE)

    def test_generates_hypothesis_code(self) -> None:
        """Hypothesis framework generates Python test code (Req 26.3)."""
        plan = "Invariant: output is always positive"
        props = self.protocol.generate_properties(plan, ["src/math.py"], "hypothesis")
        self.assertIn("@given", props[0].test_code)
        self.assertIn("hypothesis", props[0].test_code)
        self.assertEqual(props[0].framework, "hypothesis")

    def test_generates_fast_check_code(self) -> None:
        """fast-check framework generates TypeScript test code (Req 26.3)."""
        plan = "Invariant: output is always positive"
        props = self.protocol.generate_properties(plan, ["src/math.ts"], "fast-check")
        self.assertIn("fc.assert", props[0].test_code)
        self.assertIn("fc.property", props[0].test_code)
        self.assertEqual(props[0].framework, "fast-check")

    def test_generates_quickcheck_code(self) -> None:
        """QuickCheck framework generates Haskell test code (Req 26.3)."""
        plan = "Invariant: output is always positive"
        props = self.protocol.generate_properties(plan, ["Lib.hs"], "quickcheck")
        self.assertIn("QuickCheck", props[0].test_code)
        self.assertIn("prop_", props[0].test_code)
        self.assertEqual(props[0].framework, "quickcheck")

    def test_invalid_framework_defaults_to_hypothesis(self) -> None:
        """Unknown framework name falls back to hypothesis (Req 26.3)."""
        plan = "Invariant: value must be non-negative"
        props = self.protocol.generate_properties(plan, ["f.py"], "unknown_framework")
        self.assertEqual(props[0].framework, "hypothesis")

    def test_derivation_rationale_references_plan(self) -> None:
        """Each property records derivation rationale (Req 26.4)."""
        plan = "Invariant: the count is never negative"
        props = self.protocol.generate_properties(plan, ["src/counter.py"], "hypothesis")
        self.assertIn("Derived from execution plan", props[0].derivation_rationale)
        self.assertIn("count is never negative", props[0].derivation_rationale)

    def test_plan_statement_ref_contains_line_reference(self) -> None:
        """plan_statement_ref links back to plan line (Req 26.4)."""
        plan = "Invariant: data integrity is maintained"
        props = self.protocol.generate_properties(plan, ["src/db.py"], "hypothesis")
        self.assertTrue(props[0].plan_statement_ref.startswith("line:"))

    def test_property_ids_are_unique(self) -> None:
        """Each generated property has a unique property_id."""
        plan = (
            "## Invariants\n"
            "- Balance is non-negative\n"
            "- Total equals sum of parts\n"
            "- Count is monotonically increasing\n"
        )
        props = self.protocol.generate_properties(plan, ["src/acct.py"], "hypothesis")
        ids = [p.property_id for p in props]
        self.assertEqual(len(ids), len(set(ids)))

    def test_section_and_inline_markers_combined(self) -> None:
        """Both section-based and inline property markers are extracted."""
        plan = (
            "Precondition: input must not be empty\n"
            "\n"
            "## Postconditions\n"
            "- Result is always sorted\n"
        )
        props = self.protocol.generate_properties(plan, ["src/sort.py"], "hypothesis")
        self.assertEqual(len(props), 2)

    def test_short_bullet_items_ignored(self) -> None:
        """Very short bullet items (<=5 chars) are skipped as noise."""
        plan = "## Invariants\n- ok\n- This is a real invariant with meaningful content"
        props = self.protocol.generate_properties(plan, ["f.py"], "hypothesis")
        self.assertEqual(len(props), 1)
        self.assertIn("real invariant", props[0].description)


class TestExecuteVerification(unittest.TestCase):
    """Tests for execute_verification() — full verification execution cycle.

    Validates Requirements: 27.1, 27.2, 27.4, 27.5
    """

    def setUp(self) -> None:
        self.protocol = VerificationProtocol()
        self.sample_properties = [
            GeneratedProperty(
                property_id="prop-001-round",
                property_type=PropertyType.ROUND_TRIP,
                description="Encode/decode round-trip",
                test_code="@given(st.text())\ndef test_roundtrip(s):\n    assert decode(encode(s)) == s",
                framework="hypothesis",
                derivation_rationale="Plan says encoding is lossless",
                plan_statement_ref="line:5",
            ),
            GeneratedProperty(
                property_id="prop-002-invariant",
                property_type=PropertyType.INVARIANT,
                description="Length is preserved",
                test_code="@given(st.lists(st.integers()))\ndef test_len(xs):\n    assert len(sort(xs)) == len(xs)",
                framework="hypothesis",
                derivation_rationale="Sorting preserves length",
                plan_statement_ref="line:10",
            ),
        ]

    def _make_mock_host_client(
        self, property_success: bool = True, standard_success: bool = True
    ) -> "MockHostClient":
        """Create a mock HostClient that returns configurable results."""

        class MockHostClient:
            def __init__(self, prop_success: bool, std_success: bool) -> None:
                self._prop_success = prop_success
                self._std_success = std_success
                self.exec_calls: list[object] = []

            def exec(self, request: object) -> object:
                self.exec_calls.append(request)
                from vikram_orchestrator.models import HostObservation

                # Distinguish property tests from standard tests by checking command content
                args = getattr(request, "arguments", {})
                command = args.get("command", "")
                if "make test" in command:
                    return HostObservation(
                        task_id=getattr(request, "task_id", ""),
                        action_name="exec",
                        success=self._std_success,
                        exit_code=0 if self._std_success else 1,
                        summary="test run",
                        output="100 tests passed" if self._std_success else "FAILED",
                    )
                else:
                    output = (
                        "100 tests passed"
                        if self._prop_success
                        else "Falsifying example: func(x='abc')\nFailed after 50 tests"
                    )
                    return HostObservation(
                        task_id=getattr(request, "task_id", ""),
                        action_name="exec",
                        success=self._prop_success,
                        exit_code=0 if self._prop_success else 1,
                        summary="property test",
                        output=output,
                    )

        return MockHostClient(property_success, standard_success)

    def test_all_properties_pass_with_standard_tests(self) -> None:
        """When all properties and standard tests pass → VERIFIED_CORRECT (Req 27.4)."""
        mock_client = self._make_mock_host_client(
            property_success=True, standard_success=True
        )
        result = self.protocol.execute_verification(
            worktree="/tmp/worktree",
            strategy=VerificationStrategy.PROPERTY,
            properties=self.sample_properties,
            host_client=mock_client,
            task_id="task-001",
        )
        self.assertEqual(result.outcome, VerificationOutcome.VERIFIED_CORRECT)
        self.assertTrue(result.all_properties_passed)
        self.assertTrue(result.standard_tests_passed)

    def test_property_failure_returns_failed_outcome(self) -> None:
        """When a property test fails → FAILED outcome (Req 27.2)."""
        mock_client = self._make_mock_host_client(
            property_success=False, standard_success=True
        )
        result = self.protocol.execute_verification(
            worktree="/tmp/worktree",
            strategy=VerificationStrategy.PROPERTY,
            properties=self.sample_properties,
            host_client=mock_client,
            task_id="task-002",
        )
        self.assertEqual(result.outcome, VerificationOutcome.FAILED)
        self.assertFalse(result.all_properties_passed)

    def test_standard_tests_fail_returns_failed(self) -> None:
        """When standard tests fail even if properties pass → FAILED."""
        mock_client = self._make_mock_host_client(
            property_success=True, standard_success=False
        )
        result = self.protocol.execute_verification(
            worktree="/tmp/worktree",
            strategy=VerificationStrategy.PROPERTY,
            properties=self.sample_properties,
            host_client=mock_client,
            task_id="task-003",
        )
        # Properties pass but standard tests fail — overall not verified-correct
        # The outcome depends on the implementation; check it's not VERIFIED_CORRECT
        self.assertNotEqual(result.outcome, VerificationOutcome.VERIFIED_CORRECT)

    def test_no_properties_with_passing_tests_is_tests_pass(self) -> None:
        """When no properties generated but standard tests pass → TESTS_PASS (Req 27.4)."""
        mock_client = self._make_mock_host_client(standard_success=True)
        result = self.protocol.execute_verification(
            worktree="/tmp/worktree",
            strategy=VerificationStrategy.STANDARD,
            properties=[],
            host_client=mock_client,
            task_id="task-004",
        )
        self.assertEqual(result.outcome, VerificationOutcome.TESTS_PASS)

    def test_property_results_populated(self) -> None:
        """Property results list matches the number of input properties (Req 27.1)."""
        mock_client = self._make_mock_host_client(property_success=True)
        result = self.protocol.execute_verification(
            worktree="/tmp/worktree",
            strategy=VerificationStrategy.PROPERTY,
            properties=self.sample_properties,
            host_client=mock_client,
            task_id="task-005",
        )
        self.assertEqual(len(result.property_results), 2)
        for pr in result.property_results:
            self.assertTrue(pr.passed)

    def test_failed_property_captures_counterexample(self) -> None:
        """Failed property result includes counterexample from output (Req 27.2)."""
        mock_client = self._make_mock_host_client(property_success=False)
        result = self.protocol.execute_verification(
            worktree="/tmp/worktree",
            strategy=VerificationStrategy.PROPERTY,
            properties=self.sample_properties,
            host_client=mock_client,
            task_id="task-006",
        )
        # At least one result should have a counterexample extracted
        failed_results = [r for r in result.property_results if not r.passed]
        self.assertTrue(len(failed_results) > 0)
        # Check counterexample extraction from "Falsifying example: func(x='abc')"
        has_counterexample = any(r.counterexample for r in failed_results)
        self.assertTrue(has_counterexample)

    def test_merge_gate_readiness_score_range(self) -> None:
        """Merge gate readiness should be between 0.0 and 1.0 (Req 27.5)."""
        mock_client = self._make_mock_host_client(property_success=True)
        result = self.protocol.execute_verification(
            worktree="/tmp/worktree",
            strategy=VerificationStrategy.PROPERTY,
            properties=self.sample_properties,
            host_client=mock_client,
            task_id="task-007",
        )
        self.assertGreaterEqual(result.merge_gate_readiness, 0.0)
        self.assertLessEqual(result.merge_gate_readiness, 1.0)

    def test_execution_records_in_trace(self) -> None:
        """Verification execution is recorded in ExecutionTrace (Req 27.4)."""
        trace = ExecutionTrace()
        protocol = VerificationProtocol(execution_trace=trace)
        mock_client = self._make_mock_host_client(property_success=True)

        protocol.execute_verification(
            worktree="/tmp/worktree",
            strategy=VerificationStrategy.PROPERTY,
            properties=self.sample_properties,
            host_client=mock_client,
            task_id="task-trace",
        )

        records = trace.query(task_id="task-trace")
        # Should have at least one verification_execution decision recorded
        exec_records = [
            r for r in records if r.decision_type == "verification_execution"
        ]
        self.assertEqual(len(exec_records), 1)
        self.assertEqual(exec_records[0].outcome, "verified-correct")


class TestMergeGateReadiness(unittest.TestCase):
    """Tests for merge gate readiness score computation (Req 27.5).

    Validates Requirements: 27.5
    """

    def test_all_pass_full_readiness(self) -> None:
        """All properties pass + standard pass → 1.0 readiness."""
        results = [
            PropertyResult(property_id="p1", passed=True, iterations=100),
            PropertyResult(property_id="p2", passed=True, iterations=100),
        ]
        score = VerificationProtocol._compute_merge_gate_readiness(
            all_properties_passed=True,
            standard_tests_passed=True,
            property_results=results,
        )
        self.assertEqual(score, 1.0)

    def test_no_properties_standard_pass(self) -> None:
        """No properties + standard pass → 1.0 (standard carries full weight)."""
        score = VerificationProtocol._compute_merge_gate_readiness(
            all_properties_passed=False,
            standard_tests_passed=True,
            property_results=[],
        )
        self.assertEqual(score, 1.0)

    def test_no_properties_standard_fail(self) -> None:
        """No properties + standard fail → 0.0."""
        score = VerificationProtocol._compute_merge_gate_readiness(
            all_properties_passed=False,
            standard_tests_passed=False,
            property_results=[],
        )
        self.assertEqual(score, 0.0)

    def test_half_properties_pass(self) -> None:
        """Half properties pass + standard pass → partial score."""
        results = [
            PropertyResult(property_id="p1", passed=True, iterations=100),
            PropertyResult(property_id="p2", passed=False, counterexample="x=0", iterations=50),
        ]
        score = VerificationProtocol._compute_merge_gate_readiness(
            all_properties_passed=False,
            standard_tests_passed=True,
            property_results=results,
        )
        # 0.7 * (1/2) + 0.3 * 1.0 = 0.35 + 0.30 = 0.65
        self.assertAlmostEqual(score, 0.65, places=2)

    def test_all_properties_fail_standard_pass(self) -> None:
        """All properties fail + standard pass → only standard weight."""
        results = [
            PropertyResult(property_id="p1", passed=False, counterexample="x=1", iterations=50),
            PropertyResult(property_id="p2", passed=False, counterexample="x=2", iterations=50),
        ]
        score = VerificationProtocol._compute_merge_gate_readiness(
            all_properties_passed=False,
            standard_tests_passed=True,
            property_results=results,
        )
        # 0.7 * 0 + 0.3 * 1.0 = 0.30
        self.assertAlmostEqual(score, 0.30, places=2)

    def test_all_properties_pass_standard_fail(self) -> None:
        """All properties pass + standard fail → only property weight."""
        results = [
            PropertyResult(property_id="p1", passed=True, iterations=100),
        ]
        score = VerificationProtocol._compute_merge_gate_readiness(
            all_properties_passed=True,
            standard_tests_passed=False,
            property_results=results,
        )
        # 0.7 * 1.0 + 0.3 * 0.0 = 0.70
        self.assertAlmostEqual(score, 0.70, places=2)


class TestOutcomeClassification(unittest.TestCase):
    """Tests for verification outcome classification (Req 27.4).

    Validates Requirements: 27.4
    """

    def setUp(self) -> None:
        self.protocol = VerificationProtocol()
        self.sample_properties = [
            GeneratedProperty(
                property_id="prop-001",
                property_type=PropertyType.INVARIANT,
                description="Test",
                test_code="def test(): pass",
                framework="hypothesis",
                derivation_rationale="Test",
                plan_statement_ref="line:1",
            ),
        ]

    def test_verified_correct_requires_properties_and_standard(self) -> None:
        """VERIFIED_CORRECT requires both properties and standard tests pass."""
        outcome = self.protocol._classify_outcome(
            strategy=VerificationStrategy.PROPERTY,
            all_properties_passed=True,
            standard_tests_passed=True,
            properties=self.sample_properties,
        )
        self.assertEqual(outcome, VerificationOutcome.VERIFIED_CORRECT)

    def test_no_properties_gives_tests_pass(self) -> None:
        """No properties + standard pass → TESTS_PASS."""
        outcome = self.protocol._classify_outcome(
            strategy=VerificationStrategy.STANDARD,
            all_properties_passed=False,
            standard_tests_passed=True,
            properties=[],
        )
        self.assertEqual(outcome, VerificationOutcome.TESTS_PASS)

    def test_properties_fail_gives_failed(self) -> None:
        """Properties fail + standard pass → FAILED."""
        outcome = self.protocol._classify_outcome(
            strategy=VerificationStrategy.PROPERTY,
            all_properties_passed=False,
            standard_tests_passed=True,
            properties=self.sample_properties,
        )
        self.assertEqual(outcome, VerificationOutcome.FAILED)

    def test_all_fail_gives_failed(self) -> None:
        """Both properties and standard tests fail → FAILED."""
        outcome = self.protocol._classify_outcome(
            strategy=VerificationStrategy.PROPERTY,
            all_properties_passed=False,
            standard_tests_passed=False,
            properties=self.sample_properties,
        )
        self.assertEqual(outcome, VerificationOutcome.FAILED)

    def test_minimal_strategy_with_properties_pass(self) -> None:
        """MINIMAL strategy + properties pass → VERIFIED_CORRECT (no standard tests needed)."""
        outcome = self.protocol._classify_outcome(
            strategy=VerificationStrategy.MINIMAL,
            all_properties_passed=True,
            standard_tests_passed=False,
            properties=self.sample_properties,
        )
        self.assertEqual(outcome, VerificationOutcome.VERIFIED_CORRECT)


class TestGeneratePropertiesEdgeCases(unittest.TestCase):
    """Additional edge cases for property generation not covered by existing tests.

    Validates Requirements: 26.1, 26.2, 26.3
    """

    def setUp(self) -> None:
        self.protocol = VerificationProtocol()

    def test_multiple_inline_markers_in_plan(self) -> None:
        """Multiple inline markers (Invariant:, Postcondition:) produce multiple properties."""
        plan = (
            "Invariant: balance is always non-negative\n"
            "Postcondition: total matches sum of parts\n"
            "Precondition: input list is non-empty\n"
        )
        props = self.protocol.generate_properties(plan, ["src/acct.py"], "hypothesis")
        self.assertEqual(len(props), 3)
        # Each should have unique IDs
        ids = {p.property_id for p in props}
        self.assertEqual(len(ids), 3)

    def test_generated_property_test_code_is_nonempty_for_all_types(self) -> None:
        """All property types produce non-empty test code (Req 26.3)."""
        plans_by_type = {
            PropertyType.ROUND_TRIP: "Invariant: encoding then decoding is lossless",
            PropertyType.IDEMPOTENCE: "Postcondition: normalization is idempotent",
            PropertyType.ERROR_CONDITION: "Invariant: function must not crash on invalid input",
            PropertyType.STATE_INVARIANT: "Invariant: consistent state is maintained after every operation",
            PropertyType.TRANSITION_COVERAGE: "Postcondition: all states are reachable from the initial state",
        }
        for expected_type, plan_text in plans_by_type.items():
            props = self.protocol.generate_properties(plan_text, ["src/mod.py"], "hypothesis")
            self.assertEqual(len(props), 1, f"Expected 1 property for {expected_type}")
            self.assertTrue(
                len(props[0].test_code) > 0,
                f"Empty test_code for {expected_type}",
            )
            self.assertEqual(props[0].property_type, expected_type)

    def test_fast_check_framework_produces_typescript_style_code(self) -> None:
        """fast-check framework generates code with fc.assert and fc.property."""
        plan = "Invariant: output length matches input length"
        props = self.protocol.generate_properties(plan, ["src/fn.ts"], "fast-check")
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0].framework, "fast-check")
        self.assertIn("fc.assert", props[0].test_code)
        self.assertIn("fc.property", props[0].test_code)

    def test_quickcheck_framework_produces_haskell_style_code(self) -> None:
        """quickcheck framework generates code with prop_ prefix and QuickCheck import."""
        plan = "Invariant: output length matches input length"
        props = self.protocol.generate_properties(plan, ["Lib.hs"], "quickcheck")
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0].framework, "quickcheck")
        self.assertIn("QuickCheck", props[0].test_code)
        self.assertIn("prop_", props[0].test_code)

    def test_plan_with_only_non_property_content_returns_empty(self) -> None:
        """Plan with no invariants/preconditions/postconditions returns empty list."""
        plan = (
            "## Overview\n"
            "This change adds logging to the auth module.\n\n"
            "## Implementation\n"
            "- Modify auth.py to add log statements\n"
        )
        props = self.protocol.generate_properties(plan, ["src/auth.py"], "hypothesis")
        self.assertEqual(props, [])

    def test_whitespace_only_plan_returns_empty(self) -> None:
        """Whitespace-only plan returns empty list."""
        self.assertEqual(
            self.protocol.generate_properties("   \n\t\n  ", ["f.py"], "hypothesis"), []
        )

    def test_property_derivation_rationale_mentions_plan_reference(self) -> None:
        """Each property's derivation_rationale references the plan statement location."""
        plan = "## Invariants\n- Data integrity is preserved across writes"
        props = self.protocol.generate_properties(plan, ["src/db.py"], "hypothesis")
        self.assertEqual(len(props), 1)
        self.assertIn("Derived from execution plan", props[0].derivation_rationale)
        self.assertIn("Data integrity", props[0].derivation_rationale)
        self.assertTrue(props[0].plan_statement_ref.startswith("line:"))


class TestFeedbackLoopEdgeCases(unittest.TestCase):
    """Additional edge cases for feedback loop (Req 29.2).

    Validates Requirements: 29.1, 29.2
    """

    def setUp(self) -> None:
        self.protocol = VerificationProtocol()

    def test_fix_prompt_with_no_counterexample(self) -> None:
        """Fix prompt is still generated when counterexample is None."""
        failure = PropertyResult(
            property_id="prop-no-ce",
            passed=False,
            counterexample=None,
            iterations=100,
            shrunk_input=None,
            diagnostic="Property timed out",
        )
        result = self.protocol.feedback_loop(failure, 1)
        self.assertTrue(result.should_retry)
        self.assertIsNotNone(result.fix_prompt)
        self.assertIn("prop-no-ce", result.fix_prompt)
        # Diagnostic should still appear
        self.assertIn("Property timed out", result.fix_prompt)

    def test_fix_prompt_with_no_diagnostic(self) -> None:
        """Fix prompt is generated even when diagnostic is None."""
        failure = PropertyResult(
            property_id="prop-no-diag",
            passed=False,
            counterexample="x=42",
            iterations=10,
            shrunk_input="42",
            diagnostic=None,
        )
        result = self.protocol.feedback_loop(failure, 2)
        self.assertTrue(result.should_retry)
        self.assertIn("prop-no-diag", result.fix_prompt)
        self.assertIn("x=42", result.fix_prompt)

    def test_attempt_zero_allows_retry(self) -> None:
        """Attempt 0 (edge case) allows retry since it's <= MAX_FIX_ITERATIONS."""
        failure = PropertyResult(
            property_id="prop-zero",
            passed=False,
            counterexample="edge",
            iterations=1,
            shrunk_input=None,
            diagnostic=None,
        )
        result = self.protocol.feedback_loop(failure, 0)
        self.assertTrue(result.should_retry)
        self.assertFalse(result.escalate)


class TestGeneratePropertiesTraceRecording(unittest.TestCase):
    """Tests for Execution Trace recording during property generation (Req 26.4).

    Validates Requirements: 26.4
    """

    def setUp(self) -> None:
        self.trace = ExecutionTrace()
        self.protocol = VerificationProtocol(execution_trace=self.trace)

    def test_records_property_generation_in_trace(self) -> None:
        """Property generation records a decision in the Execution Trace (Req 26.4)."""
        plan = "Invariant: balance is never negative"
        self.protocol.generate_properties(plan, ["src/ledger.py"], "hypothesis", task_id="task-gen-1")

        records = self.trace.query(task_id="task-gen-1")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].decision_type, "property_generation")

    def test_trace_includes_property_count(self) -> None:
        """Trace records the number of properties generated."""
        plan = (
            "## Invariants\n"
            "- Balance is non-negative\n"
            "- Count is monotonically increasing\n"
        )
        self.protocol.generate_properties(plan, ["src/acct.py"], "hypothesis", task_id="task-gen-2")

        records = self.trace.query(task_id="task-gen-2")
        self.assertEqual(records[0].state_snapshot["properties_generated"], 2)

    def test_trace_includes_framework(self) -> None:
        """Trace records which framework was used."""
        plan = "Invariant: data is valid"
        self.protocol.generate_properties(plan, ["src/data.ts"], "fast-check", task_id="task-gen-3")

        records = self.trace.query(task_id="task-gen-3")
        self.assertEqual(records[0].state_snapshot["framework"], "fast-check")

    def test_trace_includes_target_files(self) -> None:
        """Trace records target files for context."""
        plan = "Invariant: output is sorted"
        files = ["src/sort.py", "src/utils.py"]
        self.protocol.generate_properties(plan, files, "hypothesis", task_id="task-gen-4")

        records = self.trace.query(task_id="task-gen-4")
        self.assertEqual(records[0].state_snapshot["target_files"], files)

    def test_trace_includes_derivation_rationale_per_property(self) -> None:
        """Each property's derivation rationale is recorded in nd_inputs (Req 26.4)."""
        plan = "Invariant: encoding then decoding produces the original value"
        self.protocol.generate_properties(plan, ["src/codec.py"], "hypothesis", task_id="task-gen-5")

        records = self.trace.query(task_id="task-gen-5")
        generated = records[0].non_deterministic_inputs["generated_properties"]
        self.assertEqual(len(generated), 1)
        self.assertIn("derivation_rationale", generated[0])
        self.assertIn("Derived from execution plan", generated[0]["derivation_rationale"])

    def test_trace_includes_plan_statement_ref(self) -> None:
        """Each property's plan_statement_ref is recorded for traceability (Req 26.4)."""
        plan = "Postcondition: result is always sorted"
        self.protocol.generate_properties(plan, ["src/sort.py"], "hypothesis", task_id="task-gen-6")

        records = self.trace.query(task_id="task-gen-6")
        generated = records[0].non_deterministic_inputs["generated_properties"]
        self.assertTrue(generated[0]["plan_statement_ref"].startswith("line:"))

    def test_no_trace_when_no_trace_provided(self) -> None:
        """No error when ExecutionTrace is not provided."""
        protocol = VerificationProtocol()  # No trace
        plan = "Invariant: value is positive"
        # Should not raise
        props = protocol.generate_properties(plan, ["f.py"], "hypothesis", task_id="task-gen-7")
        self.assertEqual(len(props), 1)

    def test_no_trace_when_no_task_id(self) -> None:
        """No trace recorded when task_id is empty."""
        plan = "Invariant: value is positive"
        self.protocol.generate_properties(plan, ["f.py"], "hypothesis")

        # No records should exist for empty task_id
        all_records = self.trace.query()
        self.assertEqual(len(all_records), 0)

    def test_trace_outcome_includes_count(self) -> None:
        """Trace outcome includes the number of generated properties."""
        plan = (
            "## Invariants\n"
            "- Value is positive\n"
            "- Sum is preserved\n"
            "- Order is maintained\n"
        )
        self.protocol.generate_properties(plan, ["src/fn.py"], "hypothesis", task_id="task-gen-8")

        records = self.trace.query(task_id="task-gen-8")
        self.assertEqual(records[0].outcome, "generated_3_properties")


if __name__ == "__main__":
    unittest.main()
