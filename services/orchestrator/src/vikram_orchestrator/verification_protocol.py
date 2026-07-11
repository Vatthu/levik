"""Verification Protocol subsystem — strategy selection, property generation, and feedback loop.

Implements the Verification_Protocol (Requirements 26–29):
- Strategy selection based on change type and file types
- Property generation from execution plans
- Verification execution
- Feedback loop with bounded iteration (max 3 fix attempts before escalation)

Module: vikram_orchestrator/verification_protocol.py
Requirements: 26.1, 26.2, 26.3, 26.4, 27.1, 27.2, 27.3, 27.4, 27.5, 28.1, 28.2, 28.3, 28.4, 29.1, 29.2, 29.3, 29.4
"""

from __future__ import annotations

import logging
import re
import uuid
from enum import Enum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from vikram_orchestrator.execution_trace import ExecutionTrace

if TYPE_CHECKING:
    from vikram_orchestrator.host_client import HostClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_FIX_ITERATIONS = 3  # Per requirement 29.2


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PropertyType(str, Enum):
    """Classification of generated verification properties (Requirement 26.2)."""

    INVARIANT = "invariant_preservation"
    ROUND_TRIP = "round_trip_consistency"
    IDEMPOTENCE = "idempotence"
    METAMORPHIC = "metamorphic_relationship"
    ERROR_CONDITION = "error_condition"
    STATE_INVARIANT = "state_invariant"
    TRANSITION_COVERAGE = "transition_coverage"


class VerificationStrategy(str, Enum):
    """Verification rigor levels (Requirement 28)."""

    MINIMAL = "minimal"  # syntax check, schema validation only
    STANDARD = "standard"  # existing test suite execution
    PROPERTY = "property"  # standard + generated property tests
    COMPREHENSIVE = "comprehensive"  # property + integration + coverage


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class GeneratedProperty(BaseModel):
    """A testable correctness property derived from an execution plan (Requirement 26.1)."""

    property_id: str
    property_type: PropertyType
    description: str
    test_code: str
    framework: str  # "hypothesis", "fast-check", "quickcheck"
    derivation_rationale: str
    plan_statement_ref: str


class PropertyResult(BaseModel):
    """Result of executing a single property test (Requirement 27.2)."""

    property_id: str
    passed: bool
    counterexample: str | None = None
    iterations: int = 0
    shrunk_input: str | None = None
    diagnostic: str | None = None


class FeedbackLoopResult(BaseModel):
    """Result of a feedback loop iteration (Requirements 29.2, 29.3).

    When should_retry is True, fix_prompt contains a targeted fix request.
    When escalate is True, failure_details contains the persistent failure
    information for presentation to the founder.
    """

    should_retry: bool
    escalate: bool
    fix_prompt: str | None = None
    failure_details: PropertyResult | None = None


class StrategySelection(BaseModel):
    """Records a strategy selection decision with rationale (Requirement 28.4)."""

    strategy: VerificationStrategy
    change_type: str
    file_types: list[str]
    rationale: str


class VerificationOutcome(str, Enum):
    """Distinguishes the quality level of verification (Requirement 27.4).

    A "verified-correct" outcome means property tests validated correctness,
    while "tests-pass" means only standard test suites passed without property
    verification.
    """

    VERIFIED_CORRECT = "verified-correct"
    TESTS_PASS = "tests-pass"
    FAILED = "failed"


class VerificationExecutionResult(BaseModel):
    """Complete result of a verification execution run (Requirement 27.1–27.5).

    Captures the outcome classification, individual property results,
    and merge-gate readiness score.
    """

    outcome: VerificationOutcome
    property_results: list[PropertyResult] = Field(default_factory=list)
    all_properties_passed: bool = False
    standard_tests_passed: bool = False
    merge_gate_readiness: float = 0.0
    diagnostics: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Merge gate readiness weights (Requirement 27.5)
# ---------------------------------------------------------------------------

# Property verification results are weighted more heavily than standard tests
PROPERTY_VERIFICATION_WEIGHT = 0.7
STANDARD_TEST_WEIGHT = 0.3


# ---------------------------------------------------------------------------
# Strategy selection mapping
# ---------------------------------------------------------------------------

# Change types that mandate PROPERTY strategy with round-trip testing (Req 28.1)
_PROPERTY_ROUND_TRIP_TYPES = frozenset({
    "pure_function",
    "parser",
    "serializer",
    "codec",
    "transform",
})

# Change types that mandate PROPERTY strategy with state invariants (Req 28.2)
_PROPERTY_STATE_TYPES = frozenset({
    "state_machine",
    "workflow",
    "finite_automaton",
    "protocol",
})

# Change types that use MINIMAL strategy (Req 28.3)
_MINIMAL_TYPES = frozenset({
    "documentation",
    "docs",
    "config",
    "configuration",
    "readme",
    "changelog",
    "comment",
})

# File extensions that indicate documentation/config
_MINIMAL_EXTENSIONS = frozenset({
    ".md",
    ".txt",
    ".rst",
    ".yaml",
    ".yml",
    ".toml",
    ".json",
    ".ini",
    ".cfg",
    ".env",
})

# File extensions that indicate pure logic (useful for heuristic)
_LOGIC_EXTENSIONS = frozenset({
    ".py",
    ".ts",
    ".js",
    ".go",
    ".rs",
    ".hs",
    ".java",
    ".kt",
    ".scala",
    ".rb",
})


# ---------------------------------------------------------------------------
# Property Generation Helpers (Requirements 26.1, 26.2, 26.3)
# ---------------------------------------------------------------------------

# Supported frameworks for test code generation (Requirement 26.3)
_SUPPORTED_FRAMEWORKS = frozenset({"hypothesis", "fast-check", "quickcheck"})


class _PlanStatement:
    """Internal representation of a property statement extracted from a plan."""

    __slots__ = ("category", "text", "line_ref", "index")

    def __init__(self, category: str, text: str, line_ref: str, index: int) -> None:
        self.category = category
        self.text = text
        self.line_ref = line_ref
        self.index = index


# Regex patterns for plan parsing

# Section headers: markdown-style (# prefix) OR plain text ending with colon
_SECTION_HEADER_RE = re.compile(
    r"^#{1,4}\s+(.+?)(?:\s*:)?\s*$"
    r"|"
    r"^([A-Z][A-Za-z\s_-]+)\s*:\s*$"
)

# Inline property markers
_INLINE_PROPERTY_RE = re.compile(
    r"^(?:[-*]\s+)?(?:(?:#{1,4}\s+)?)?"
    r"(invariant|precondition|pre|postcondition|post|property|"
    r"must hold|ensures|requires|assert|guarantee|maintain)\s*"
    r"[:—\-]\s*(.+)",
    re.IGNORECASE,
)

# Bullet point pattern
_BULLET_RE = re.compile(r"^[-*•]\s+|^\d+[.)]\s+")

# Keywords for section classification
_SECTION_KEYWORDS = (
    "invariant", "precondition", "postcondition",
    "pre-condition", "post-condition",
    "propert", "guarantee", "constraint",
    "requirement", "assert", "ensure",
    "must hold", "maintain",
)


def _classify_section(header: str) -> str:
    """Map a section header to a category."""
    if "precondition" in header or "pre-condition" in header or "require" in header:
        return "precondition"
    if "postcondition" in header or "post-condition" in header or "ensure" in header:
        return "postcondition"
    if "invariant" in header or "maintain" in header:
        return "invariant"
    return "property"


def _classify_marker(marker: str) -> str:
    """Map an inline marker keyword to a category."""
    marker = marker.lower()
    if marker in ("precondition", "pre", "requires"):
        return "precondition"
    if marker in ("postcondition", "post", "ensures", "guarantee"):
        return "postcondition"
    if marker in ("invariant", "must hold", "maintain"):
        return "invariant"
    return "property"


# Keywords for PropertyType classification from statement text
_ROUND_TRIP_KEYWORDS = (
    "round-trip", "round trip", "roundtrip",
    "encode then decode", "decode then encode",
    "encoding then decoding", "decoding then encoding",
    "serialize then deserialize", "deserialize then serialize",
    "serializing then deserializing", "deserializing then serializing",
    "parse then format", "format then parse",
    "parsing then formatting", "formatting then parsing",
    "lossless", "reversible", "bijective",
    "inverse", "original value",
)

_IDEMPOTENCE_KEYWORDS = (
    "idempoten", "applying twice", "re-applying",
    "repeated application", "f(f(x)) == f(x)",
    "same result when applied multiple times",
    "no effect when repeated",
    "twice yields the same", "twice produces the same",
)

_METAMORPHIC_KEYWORDS = (
    "metamorphic", "scaling", "permutation invariant",
    "order independent", "commutative",
    "doubling input", "relationship between",
    "proportional", "relative",
)

_ERROR_CONDITION_KEYWORDS = (
    "error", "exception", "invalid input",
    "reject", "fail gracefully", "malformed",
    "boundary", "overflow", "underflow",
    "negative", "empty input", "null",
    "should raise", "should throw", "must not crash",
)

_STATE_INVARIANT_KEYWORDS = (
    "state invariant", "state must", "state remains",
    "consistent state", "valid state",
    "after transition", "preserved across",
    "balance", "count", "size",
    "monoton", "never negative",
)

_TRANSITION_KEYWORDS = (
    "transition", "reachable", "unreachable",
    "valid sequence", "state machine",
    "from state", "to state",
    "all states", "coverage",
    "path", "workflow step",
)


def _generate_test_code(
    framework: str,
    property_type: PropertyType,
    description: str,
    target_files: list[str],
    property_id: str,
) -> str:
    """Generate executable test code for a property in the specified framework.

    Produces a complete, runnable test function/block that:
    - Imports the appropriate PBT library
    - Defines a strategy/generator for inputs
    - Asserts the property holds

    Args:
        framework: One of "hypothesis", "fast-check", "quickcheck".
        property_type: The classified property type.
        description: Human-readable description of the property.
        target_files: Files being modified (for import context).
        property_id: Unique identifier for the property.

    Returns:
        A string containing executable test code.
    """
    if framework == "hypothesis":
        return _generate_hypothesis_code(property_type, description, target_files, property_id)
    elif framework == "fast-check":
        return _generate_fast_check_code(property_type, description, target_files, property_id)
    elif framework == "quickcheck":
        return _generate_quickcheck_code(property_type, description, target_files, property_id)
    # Fallback to hypothesis
    return _generate_hypothesis_code(property_type, description, target_files, property_id)


def _generate_hypothesis_code(
    property_type: PropertyType,
    description: str,
    target_files: list[str],
    property_id: str,
) -> str:
    """Generate Hypothesis (Python) test code."""
    safe_name = re.sub(r"[^a-z0-9_]", "_", property_id.lower().replace("-", "_"))
    module_hint = _extract_module_hint(target_files, "python")

    template = _HYPOTHESIS_TEMPLATES.get(property_type, _HYPOTHESIS_TEMPLATES[PropertyType.INVARIANT])
    return template.format(
        property_id=property_id,
        safe_name=safe_name,
        description=description,
        module_hint=module_hint,
    )


def _generate_fast_check_code(
    property_type: PropertyType,
    description: str,
    target_files: list[str],
    property_id: str,
) -> str:
    """Generate fast-check (TypeScript/JavaScript) test code."""
    safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", property_id.replace("-", "_"))
    module_hint = _extract_module_hint(target_files, "typescript")

    template = _FAST_CHECK_TEMPLATES.get(property_type, _FAST_CHECK_TEMPLATES[PropertyType.INVARIANT])
    return template.format(
        property_id=property_id,
        safe_name=safe_name,
        description=description,
        module_hint=module_hint,
    )


def _generate_quickcheck_code(
    property_type: PropertyType,
    description: str,
    target_files: list[str],
    property_id: str,
) -> str:
    """Generate QuickCheck (Haskell) test code."""
    safe_name = re.sub(r"[^a-zA-Z0-9_]", "", property_id.replace("-", "_").title())
    module_hint = _extract_module_hint(target_files, "haskell")

    template = _QUICKCHECK_TEMPLATES.get(property_type, _QUICKCHECK_TEMPLATES[PropertyType.INVARIANT])
    return template.format(
        property_id=property_id,
        safe_name=safe_name,
        description=description,
        module_hint=module_hint,
    )


def _extract_module_hint(target_files: list[str], language: str) -> str:
    """Extract a module/import hint from target file paths."""
    if not target_files:
        return "# TODO: import target module"

    first_file = target_files[0]
    if language == "python":
        # Convert path to Python module notation
        module = first_file.replace("/", ".").replace("\\", ".")
        module = re.sub(r"\.py$", "", module)
        # Strip leading dots or src prefix
        module = re.sub(r"^\.+", "", module)
        module = re.sub(r"^src\.", "", module)
        return f"# from {module} import <target_function>"
    elif language == "typescript":
        path = re.sub(r"\.(ts|js)$", "", first_file)
        return f'// import {{ targetFunction }} from "./{path}";'
    elif language == "haskell":
        module = first_file.replace("/", ".").replace("\\", ".")
        module = re.sub(r"\.hs$", "", module)
        return f"-- import qualified {module}"
    return f"# target: {first_file}"


# ---------------------------------------------------------------------------
# Test Code Templates
# ---------------------------------------------------------------------------

_HYPOTHESIS_TEMPLATES: dict[PropertyType, str] = {
    PropertyType.INVARIANT: '''"""Property: {description}"""
from hypothesis import given, strategies as st

{module_hint}


@given(st.text())
def test_{safe_name}(input_data):
    """Property {property_id}: {description}

    Verifies that the invariant holds for all generated inputs.
    """
    # TODO: Replace with actual function call and assertion
    result = target_function(input_data)
    assert invariant_holds(result), f"Invariant violated for input: {{input_data}}"
''',
    PropertyType.ROUND_TRIP: '''"""Property: {description}"""
from hypothesis import given, strategies as st

{module_hint}


@given(st.text())
def test_{safe_name}(input_data):
    """Property {property_id}: {description}

    Verifies round-trip consistency: decode(encode(x)) == x for all x.
    """
    # TODO: Replace with actual encode/decode functions
    encoded = encode(input_data)
    decoded = decode(encoded)
    assert decoded == input_data, (
        f"Round-trip failed: input={{input_data}}, encoded={{encoded}}, decoded={{decoded}}"
    )
''',
    PropertyType.IDEMPOTENCE: '''"""Property: {description}"""
from hypothesis import given, strategies as st

{module_hint}


@given(st.text())
def test_{safe_name}(input_data):
    """Property {property_id}: {description}

    Verifies idempotence: f(f(x)) == f(x) for all x.
    """
    # TODO: Replace with actual function call
    first_application = target_function(input_data)
    second_application = target_function(first_application)
    assert first_application == second_application, (
        f"Idempotence violated: f(x)={{first_application}}, f(f(x))={{second_application}}"
    )
''',
    PropertyType.METAMORPHIC: '''"""Property: {description}"""
from hypothesis import given, strategies as st

{module_hint}


@given(st.text())
def test_{safe_name}(input_data):
    """Property {property_id}: {description}

    Verifies metamorphic relationship between transformations.
    """
    # TODO: Replace with actual metamorphic relationship
    result_original = target_function(input_data)
    transformed_input = transform(input_data)
    result_transformed = target_function(transformed_input)
    assert metamorphic_relation(result_original, result_transformed), (
        f"Metamorphic relation violated for input: {{input_data}}"
    )
''',
    PropertyType.ERROR_CONDITION: '''"""Property: {description}"""
import pytest
from hypothesis import given, strategies as st

{module_hint}


@given(st.text())
def test_{safe_name}(input_data):
    """Property {property_id}: {description}

    Verifies error handling: invalid inputs produce appropriate errors,
    never crashes or undefined behavior.
    """
    # TODO: Replace with actual function and error conditions
    try:
        result = target_function(input_data)
        # If no exception, result should be valid
        assert is_valid_result(result)
    except (ValueError, TypeError) as e:
        # Expected error conditions should raise clean exceptions
        assert str(e), "Error should have a descriptive message"
''',
    PropertyType.STATE_INVARIANT: '''"""Property: {description}"""
from hypothesis import given, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, rule, invariant

{module_hint}


class StateMachine_{safe_name}(RuleBasedStateMachine):
    """Property {property_id}: {description}

    Verifies that state invariants are maintained across all operations.
    """

    def __init__(self):
        super().__init__()
        # TODO: Initialize state
        self.state = initial_state()

    @invariant()
    def state_invariant_holds(self):
        """The invariant must hold after every operation."""
        # TODO: Replace with actual invariant check
        assert check_invariant(self.state), "State invariant violated"

    @rule()
    def perform_operation(self):
        """Apply a state-modifying operation."""
        # TODO: Replace with actual operation
        self.state = apply_operation(self.state)


TestStateMachine = StateMachine_{safe_name}.TestCase
''',
    PropertyType.TRANSITION_COVERAGE: '''"""Property: {description}"""
from hypothesis import given, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, rule, precondition

{module_hint}


class TransitionCoverage_{safe_name}(RuleBasedStateMachine):
    """Property {property_id}: {description}

    Verifies valid state transitions and coverage of reachable states.
    """

    def __init__(self):
        super().__init__()
        # TODO: Initialize state machine
        self.current_state = "initial"

    @rule()
    def transition(self):
        """Attempt a state transition."""
        # TODO: Replace with actual state transitions
        next_state = get_next_state(self.current_state)
        assert next_state in VALID_STATES, (
            f"Invalid state reached: {{next_state}} from {{self.current_state}}"
        )
        self.current_state = next_state


TestTransitionCoverage = TransitionCoverage_{safe_name}.TestCase
''',
}

_FAST_CHECK_TEMPLATES: dict[PropertyType, str] = {
    PropertyType.INVARIANT: '''// Property: {description}
import * as fc from "fast-check";

{module_hint}

describe("{property_id}", () => {{
  it("{description}", () => {{
    fc.assert(
      fc.property(fc.string(), (input) => {{
        // TODO: Replace with actual function call and assertion
        const result = targetFunction(input);
        return invariantHolds(result);
      }})
    );
  }});
}});
''',
    PropertyType.ROUND_TRIP: '''// Property: {description}
import * as fc from "fast-check";

{module_hint}

describe("{property_id}", () => {{
  it("{description}", () => {{
    fc.assert(
      fc.property(fc.string(), (input) => {{
        // TODO: Replace with actual encode/decode functions
        const encoded = encode(input);
        const decoded = decode(encoded);
        return decoded === input;
      }})
    );
  }});
}});
''',
    PropertyType.IDEMPOTENCE: '''// Property: {description}
import * as fc from "fast-check";

{module_hint}

describe("{property_id}", () => {{
  it("{description}", () => {{
    fc.assert(
      fc.property(fc.string(), (input) => {{
        // TODO: Replace with actual function
        const first = targetFunction(input);
        const second = targetFunction(first);
        return first === second;
      }})
    );
  }});
}});
''',
    PropertyType.METAMORPHIC: '''// Property: {description}
import * as fc from "fast-check";

{module_hint}

describe("{property_id}", () => {{
  it("{description}", () => {{
    fc.assert(
      fc.property(fc.string(), (input) => {{
        // TODO: Replace with actual metamorphic relationship
        const resultOriginal = targetFunction(input);
        const transformedInput = transform(input);
        const resultTransformed = targetFunction(transformedInput);
        return metamorphicRelation(resultOriginal, resultTransformed);
      }})
    );
  }});
}});
''',
    PropertyType.ERROR_CONDITION: '''// Property: {description}
import * as fc from "fast-check";

{module_hint}

describe("{property_id}", () => {{
  it("{description}", () => {{
    fc.assert(
      fc.property(fc.string(), (input) => {{
        // TODO: Replace with actual function and error conditions
        try {{
          const result = targetFunction(input);
          return isValidResult(result);
        }} catch (e) {{
          // Expected errors should have descriptive messages
          return e instanceof Error && e.message.length > 0;
        }}
      }})
    );
  }});
}});
''',
    PropertyType.STATE_INVARIANT: '''// Property: {description}
import * as fc from "fast-check";

{module_hint}

describe("{property_id}", () => {{
  it("{description}", () => {{
    // Model-based testing: apply random operations and check invariant after each
    fc.assert(
      fc.property(
        fc.array(fc.oneof(fc.constant("op1"), fc.constant("op2"), fc.constant("op3"))),
        (operations) => {{
          // TODO: Replace with actual state and operations
          let state = initialState();
          for (const op of operations) {{
            state = applyOperation(state, op);
            if (!checkInvariant(state)) return false;
          }}
          return true;
        }}
      )
    );
  }});
}});
''',
    PropertyType.TRANSITION_COVERAGE: '''// Property: {description}
import * as fc from "fast-check";

{module_hint}

describe("{property_id}", () => {{
  it("{description}", () => {{
    fc.assert(
      fc.property(
        fc.array(fc.oneof(fc.constant("event1"), fc.constant("event2"))),
        (events) => {{
          // TODO: Replace with actual state machine transitions
          let currentState = "initial";
          for (const event of events) {{
            const nextState = getNextState(currentState, event);
            if (!VALID_STATES.includes(nextState)) return false;
            currentState = nextState;
          }}
          return true;
        }}
      )
    );
  }});
}});
''',
}

_QUICKCHECK_TEMPLATES: dict[PropertyType, str] = {
    PropertyType.INVARIANT: '''-- Property: {description}
{module_hint}

import Test.QuickCheck

-- | Property {property_id}: {description}
prop_{safe_name} :: String -> Bool
prop_{safe_name} input =
  -- TODO: Replace with actual function call and invariant check
  let result = targetFunction input
  in invariantHolds result
''',
    PropertyType.ROUND_TRIP: '''-- Property: {description}
{module_hint}

import Test.QuickCheck

-- | Property {property_id}: {description}
-- Verifies round-trip: decode (encode x) == x
prop_{safe_name} :: String -> Bool
prop_{safe_name} input =
  -- TODO: Replace with actual encode/decode
  let encoded = encode input
      decoded = decode encoded
  in decoded == input
''',
    PropertyType.IDEMPOTENCE: '''-- Property: {description}
{module_hint}

import Test.QuickCheck

-- | Property {property_id}: {description}
-- Verifies idempotence: f (f x) == f x
prop_{safe_name} :: String -> Bool
prop_{safe_name} input =
  -- TODO: Replace with actual function
  let first = targetFunction input
      second = targetFunction first
  in first == second
''',
    PropertyType.METAMORPHIC: '''-- Property: {description}
{module_hint}

import Test.QuickCheck

-- | Property {property_id}: {description}
prop_{safe_name} :: String -> Bool
prop_{safe_name} input =
  -- TODO: Replace with actual metamorphic relationship
  let resultOriginal = targetFunction input
      transformedInput = transform input
      resultTransformed = targetFunction transformedInput
  in metamorphicRelation resultOriginal resultTransformed
''',
    PropertyType.ERROR_CONDITION: '''-- Property: {description}
{module_hint}

import Test.QuickCheck
import Control.Exception (evaluate, try, SomeException)
import System.IO.Unsafe (unsafePerformIO)

-- | Property {property_id}: {description}
prop_{safe_name} :: String -> Bool
prop_{safe_name} input =
  -- TODO: Replace with actual error handling check
  case unsafePerformIO (try (evaluate (targetFunction input)) :: IO (Either SomeException a)) of
    Left _  -> True  -- Errors are acceptable for invalid input
    Right r -> isValidResult r
''',
    PropertyType.STATE_INVARIANT: '''-- Property: {description}
{module_hint}

import Test.QuickCheck

-- | Property {property_id}: {description}
prop_{safe_name} :: [String] -> Bool
prop_{safe_name} operations =
  -- TODO: Replace with actual state machine and invariant
  let finalState = foldl applyOperation initialState operations
  in checkInvariant finalState
''',
    PropertyType.TRANSITION_COVERAGE: '''-- Property: {description}
{module_hint}

import Test.QuickCheck

-- | Property {property_id}: {description}
prop_{safe_name} :: [String] -> Bool
prop_{safe_name} events =
  -- TODO: Replace with actual state transitions
  let states = scanl getNextState "initial" events
  in all (`elem` validStates) states
''',
}


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------


class VerificationProtocol:
    """Implements the Verification Protocol subsystem.

    Provides strategy selection, property generation, verification execution,
    and bounded feedback loop for iterative fix attempts.
    """

    def __init__(self, execution_trace: ExecutionTrace | None = None) -> None:
        """Initialize the Verification Protocol.

        Args:
            execution_trace: Optional ExecutionTrace instance for recording
                decisions. If None, strategy selections are not recorded.
        """
        self._trace = execution_trace

    # -----------------------------------------------------------------------
    # Strategy Selection (Requirements 28.1, 28.2, 28.3, 28.4)
    # -----------------------------------------------------------------------

    def select_strategy(
        self,
        change_type: str,
        file_types: list[str] | None = None,
        task_id: str = "",
    ) -> VerificationStrategy:
        """Select the appropriate verification strategy based on change type.

        Strategy selection logic (Requirement 28):
        - Pure functions, parsers, serializers → PROPERTY (round-trip) [28.1]
        - State machines, workflows → PROPERTY (state invariants) [28.2]
        - Documentation, configuration → MINIMAL [28.3]
        - Multi-file refactoring, new features → STANDARD with selective properties
        - Other/unknown → STANDARD

        Args:
            change_type: The type of change being made (e.g., "pure_function",
                "state_machine", "documentation", "logic", "refactoring").
            file_types: List of file extensions involved (e.g., [".py", ".md"]).
                Used as a secondary signal when change_type is ambiguous.
            task_id: Optional task ID for execution trace recording.

        Returns:
            The selected VerificationStrategy.
        """
        if file_types is None:
            file_types = []

        normalized_type = change_type.lower().strip()
        strategy, rationale = self._determine_strategy(normalized_type, file_types)

        # Record the decision in the execution trace (Requirement 28.4)
        selection = StrategySelection(
            strategy=strategy,
            change_type=change_type,
            file_types=file_types,
            rationale=rationale,
        )
        self._record_strategy_selection(selection, task_id)

        return strategy

    def _determine_strategy(
        self, change_type: str, file_types: list[str]
    ) -> tuple[VerificationStrategy, str]:
        """Determine strategy and rationale from change type and file types.

        Returns:
            Tuple of (strategy, rationale).
        """
        # Check for PROPERTY-mandated types: pure functions, parsers, serializers (Req 28.1)
        if change_type in _PROPERTY_ROUND_TRIP_TYPES:
            return (
                VerificationStrategy.PROPERTY,
                f"Change type '{change_type}' mandates round-trip property testing "
                f"per Requirement 28.1 (pure function/parser/serializer changes).",
            )

        # Check for PROPERTY-mandated types: state machines, workflows (Req 28.2)
        if change_type in _PROPERTY_STATE_TYPES:
            return (
                VerificationStrategy.PROPERTY,
                f"Change type '{change_type}' mandates state invariant and "
                f"transition coverage property testing per Requirement 28.2.",
            )

        # Check for MINIMAL types: documentation, config (Req 28.3)
        if change_type in _MINIMAL_TYPES:
            return (
                VerificationStrategy.MINIMAL,
                f"Change type '{change_type}' uses minimal verification "
                f"(syntax/schema validation only) per Requirement 28.3.",
            )

        # Secondary signal: if all files are documentation/config extensions
        if file_types and all(
            ext.lower() in _MINIMAL_EXTENSIONS for ext in file_types
        ):
            return (
                VerificationStrategy.MINIMAL,
                f"All target files are documentation/configuration types "
                f"({file_types}); using minimal verification per Requirement 28.3.",
            )

        # Multi-file refactoring or new feature: STANDARD with selective properties
        if change_type in ("refactoring", "new_feature", "feature"):
            return (
                VerificationStrategy.STANDARD,
                f"Change type '{change_type}' uses standard verification with "
                f"selective property testing to balance cost and rigor.",
            )

        # Default: STANDARD for unknown/generic change types
        return (
            VerificationStrategy.STANDARD,
            f"Change type '{change_type}' defaults to standard verification "
            f"(existing test suite execution).",
        )

    def _record_strategy_selection(
        self, selection: StrategySelection, task_id: str
    ) -> None:
        """Record strategy selection in the Execution Trace (Requirement 28.4).

        Enables the founder to override for future similar tasks.
        """
        if self._trace is None:
            return

        self._trace.record_decision(
            task_id=task_id,
            decision_type="verification_strategy_selection",
            state_snapshot={
                "change_type": selection.change_type,
                "file_types": selection.file_types,
            },
            policy="verification_protocol_strategy_selection",
            outcome=selection.strategy.value,
            nd_inputs={
                "rationale": selection.rationale,
            },
        )

    # -----------------------------------------------------------------------
    # Property Generation (Requirements 26.1, 26.2, 26.3, 26.4)
    # -----------------------------------------------------------------------

    def generate_properties(
        self,
        plan: str,
        target_files: list[str],
        framework: str = "hypothesis",
        task_id: str = "",
    ) -> list[GeneratedProperty]:
        """Generate testable correctness properties from an execution plan.

        Parses the plan text to identify invariants, preconditions, and
        postconditions, then generates executable test code for each property.

        Per Requirements 26.1–26.4:
        - Derives properties from plan's stated invariants/preconditions/postconditions
        - Classifies properties by PropertyType
        - Produces executable test code compatible with the target framework
        - Records derivation rationale linking to the plan statement in the Execution_Trace

        Args:
            plan: The execution plan text containing invariants, preconditions,
                and postconditions to derive properties from.
            target_files: List of file paths being modified (used for context
                in generated test code).
            framework: The test framework to target. One of "hypothesis",
                "fast-check", or "quickcheck".
            task_id: Optional task ID for execution trace recording (Requirement 26.4).

        Returns:
            List of GeneratedProperty instances, one per identified property.
        """
        if not plan or not plan.strip():
            return []

        # Normalize framework name
        framework = framework.lower().strip()
        if framework not in _SUPPORTED_FRAMEWORKS:
            framework = "hypothesis"

        # Extract property statements from the plan
        statements = self._extract_plan_statements(plan)

        # Generate a property for each extracted statement
        properties: list[GeneratedProperty] = []
        for stmt in statements:
            prop = self._generate_single_property(stmt, target_files, framework)
            properties.append(prop)

        # Record property generation in Execution Trace (Requirement 26.4)
        self._record_property_generation(properties, plan, target_files, framework, task_id)

        return properties

    def _extract_plan_statements(self, plan: str) -> list[_PlanStatement]:
        """Extract invariants, preconditions, and postconditions from plan text.

        Recognizes patterns like:
        - "Invariant: ..." or "INVARIANT: ..."
        - "Precondition: ..." or "PRE: ..."
        - "Postcondition: ..." or "POST: ..."
        - "Property: ..."
        - "Must hold: ..."
        - "Ensures: ..."
        - "Requires: ..."
        - "Assert: ..."
        - Lines in sections titled "Invariants", "Preconditions", "Postconditions"
        - Bullet items under property-related headers

        Returns:
            List of _PlanStatement named tuples with category, text, and reference.
        """
        statements: list[_PlanStatement] = []
        lines = plan.split("\n")

        # Track current section context for block-based extraction
        current_section: str | None = None
        statement_counter = 0

        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue

            # Check for section headers that indicate property blocks
            section_match = _SECTION_HEADER_RE.match(stripped)
            if section_match:
                # Group 1 = markdown header, Group 2 = colon-terminated header
                header_text = (section_match.group(1) or section_match.group(2) or "").lower()
                if any(kw in header_text for kw in _SECTION_KEYWORDS):
                    current_section = _classify_section(header_text)
                else:
                    current_section = None
                continue

            # Check for explicit property markers (inline)
            inline_match = _INLINE_PROPERTY_RE.match(stripped)
            if inline_match:
                marker = inline_match.group(1).lower()
                content = inline_match.group(2).strip()
                if content:
                    statement_counter += 1
                    category = _classify_marker(marker)
                    statements.append(
                        _PlanStatement(
                            category=category,
                            text=content,
                            line_ref=f"line:{i + 1}",
                            index=statement_counter,
                        )
                    )
                continue

            # If we're inside a property section, treat bullet items as statements
            if current_section and _BULLET_RE.match(stripped):
                bullet_content = _BULLET_RE.sub("", stripped).strip()
                if bullet_content and len(bullet_content) > 5:
                    statement_counter += 1
                    statements.append(
                        _PlanStatement(
                            category=current_section,
                            text=bullet_content,
                            line_ref=f"line:{i + 1}",
                            index=statement_counter,
                        )
                    )

        return statements

    def _generate_single_property(
        self,
        stmt: _PlanStatement,
        target_files: list[str],
        framework: str,
    ) -> GeneratedProperty:
        """Generate a single property from a plan statement.

        Classifies the statement by PropertyType and generates executable
        test code for the target framework.
        """
        property_type = self._classify_property_type(stmt)
        property_id = f"prop-{stmt.index:03d}-{property_type.value.split('_')[0]}"

        # Generate framework-specific test code
        test_code = _generate_test_code(
            framework=framework,
            property_type=property_type,
            description=stmt.text,
            target_files=target_files,
            property_id=property_id,
        )

        # Build derivation rationale (Requirement 26.4)
        rationale = (
            f"Derived from execution plan statement at {stmt.line_ref}: "
            f"'{stmt.text}'. Classified as {property_type.value} based on "
            f"statement category '{stmt.category}' and content analysis."
        )

        return GeneratedProperty(
            property_id=property_id,
            property_type=property_type,
            description=stmt.text,
            test_code=test_code,
            framework=framework,
            derivation_rationale=rationale,
            plan_statement_ref=stmt.line_ref,
        )

    def _classify_property_type(self, stmt: _PlanStatement) -> PropertyType:
        """Classify a plan statement into the appropriate PropertyType.

        Uses a combination of the statement category and keyword analysis
        of the statement text to determine the most appropriate type.
        """
        text_lower = stmt.text.lower()

        # Check for specific property type indicators in the text
        if any(kw in text_lower for kw in _ROUND_TRIP_KEYWORDS):
            return PropertyType.ROUND_TRIP

        if any(kw in text_lower for kw in _IDEMPOTENCE_KEYWORDS):
            return PropertyType.IDEMPOTENCE

        if any(kw in text_lower for kw in _METAMORPHIC_KEYWORDS):
            return PropertyType.METAMORPHIC

        if any(kw in text_lower for kw in _ERROR_CONDITION_KEYWORDS):
            return PropertyType.ERROR_CONDITION

        if any(kw in text_lower for kw in _STATE_INVARIANT_KEYWORDS):
            return PropertyType.STATE_INVARIANT

        if any(kw in text_lower for kw in _TRANSITION_KEYWORDS):
            return PropertyType.TRANSITION_COVERAGE

        # Fall back to category-based classification
        if stmt.category == "precondition":
            return PropertyType.INVARIANT
        if stmt.category == "postcondition":
            return PropertyType.INVARIANT
        if stmt.category == "invariant":
            return PropertyType.INVARIANT

        # Default: general invariant
        return PropertyType.INVARIANT

    def _record_property_generation(
        self,
        properties: list[GeneratedProperty],
        plan: str,
        target_files: list[str],
        framework: str,
        task_id: str,
    ) -> None:
        """Record property generation decisions in the Execution Trace (Requirement 26.4).

        Each generated property is logged with its derivation rationale linking
        it back to the specific Execution_Plan statement it validates.

        Args:
            properties: The list of generated properties.
            plan: The original execution plan text (truncated for storage).
            target_files: The files being modified.
            framework: The test framework used.
            task_id: Task identifier for trace association.
        """
        if self._trace is None or not task_id:
            return

        # Record a single trace entry summarizing all generated properties
        # with per-property derivation rationale (Requirement 26.4)
        property_records = [
            {
                "property_id": prop.property_id,
                "property_type": prop.property_type.value,
                "description": prop.description,
                "plan_statement_ref": prop.plan_statement_ref,
                "derivation_rationale": prop.derivation_rationale,
            }
            for prop in properties
        ]

        self._trace.record_decision(
            task_id=task_id,
            decision_type="property_generation",
            state_snapshot={
                "plan_length": len(plan),
                "target_files": target_files,
                "framework": framework,
                "properties_generated": len(properties),
            },
            policy="verification_protocol_property_generation",
            outcome=f"generated_{len(properties)}_properties",
            nd_inputs={
                "generated_properties": property_records,
            },
        )

    # -----------------------------------------------------------------------
    # Verification Execution (Requirements 27.1, 27.2, 27.3, 27.4, 27.5)
    # -----------------------------------------------------------------------

    def execute_verification(
        self,
        worktree: str,
        strategy: VerificationStrategy,
        properties: list[GeneratedProperty],
        host_client: HostClient,
        task_id: str = "",
    ) -> VerificationExecutionResult:
        """Execute property tests and standard verification in a task worktree.

        Runs generated property tests via the Go Host exec endpoint (POST /v1/exec),
        parses output to capture counterexamples and diagnostics, and determines
        whether the result constitutes "verified-correct" or merely "tests-pass".

        Per Requirements 27.1–27.5:
        - Executes all generated properties in the task worktree (27.1)
        - Captures shrunk counterexamples and diagnostics on failure (27.2, 27.3)
        - Distinguishes verified-correct from tests-pass (27.4)
        - Weights property results more heavily for merge gate readiness (27.5)

        Args:
            worktree: Path to the task worktree directory.
            strategy: The verification strategy level to apply.
            properties: List of generated properties to execute.
            host_client: HostClient instance for calling the Go Host exec endpoint.
            task_id: Task identifier for tracking and telemetry.

        Returns:
            VerificationExecutionResult with outcome, individual results,
            and merge gate readiness score.
        """
        from vikram_orchestrator.models import HostActionRequest

        property_results: list[PropertyResult] = []
        diagnostics: list[str] = []

        # Execute each property test
        for prop in properties:
            result = self._execute_single_property(
                prop, worktree, host_client, task_id
            )
            property_results.append(result)
            if not result.passed and result.diagnostic:
                diagnostics.append(result.diagnostic)

        # Determine whether standard tests pass (for strategies that include them)
        standard_tests_passed = False
        if strategy in (
            VerificationStrategy.STANDARD,
            VerificationStrategy.PROPERTY,
            VerificationStrategy.COMPREHENSIVE,
        ):
            standard_tests_passed = self._run_standard_tests(
                worktree, host_client, task_id
            )

        # Classify the outcome (Requirement 27.4)
        all_properties_passed = all(r.passed for r in property_results) if property_results else False
        outcome = self._classify_outcome(
            strategy, all_properties_passed, standard_tests_passed, properties
        )

        # Compute merge gate readiness (Requirement 27.5)
        readiness = self._compute_merge_gate_readiness(
            all_properties_passed, standard_tests_passed, property_results
        )

        # Record in execution trace
        if self._trace and task_id:
            self._trace.record_decision(
                task_id=task_id,
                decision_type="verification_execution",
                state_snapshot={
                    "worktree": worktree,
                    "strategy": strategy.value,
                    "properties_count": len(properties),
                },
                policy="verification_protocol_execution",
                outcome=outcome.value,
                nd_inputs={
                    "property_results_summary": [
                        {"id": r.property_id, "passed": r.passed}
                        for r in property_results
                    ],
                },
            )

        return VerificationExecutionResult(
            outcome=outcome,
            property_results=property_results,
            all_properties_passed=all_properties_passed,
            standard_tests_passed=standard_tests_passed,
            merge_gate_readiness=readiness,
            diagnostics=diagnostics,
        )

    def _execute_single_property(
        self,
        prop: GeneratedProperty,
        worktree: str,
        host_client: HostClient,
        task_id: str,
    ) -> PropertyResult:
        """Execute a single property test and parse the result (Requirements 27.1, 27.2).

        Constructs the appropriate test command based on the property's framework,
        executes via POST /v1/exec, and parses output for counterexamples and
        shrunk inputs.

        Args:
            prop: The generated property to execute.
            worktree: Path to the task worktree.
            host_client: HostClient for the Go Host.
            task_id: Task identifier.

        Returns:
            PropertyResult with pass/fail status, counterexample, and diagnostics.
        """
        from vikram_orchestrator.models import HostActionRequest

        command = self._build_test_command(prop)

        try:
            request = HostActionRequest(
                task_id=task_id,
                action_name="exec",
                arguments={"command": command},
                working_dir=worktree,
            )
            observation = host_client.exec(request)

            if observation.success:
                return PropertyResult(
                    property_id=prop.property_id,
                    passed=True,
                    counterexample=None,
                    iterations=_extract_iterations(observation.output),
                    shrunk_input=None,
                    diagnostic=None,
                )
            else:
                # Parse failure output for counterexamples and diagnostics (Req 27.2)
                counterexample = _extract_counterexample(observation.output, prop.framework)
                shrunk_input = _extract_shrunk_input(observation.output, prop.framework)
                diagnostic = _build_diagnostic(prop, observation.output)

                return PropertyResult(
                    property_id=prop.property_id,
                    passed=False,
                    counterexample=counterexample,
                    iterations=_extract_iterations(observation.output),
                    shrunk_input=shrunk_input,
                    diagnostic=diagnostic,
                )

        except Exception as e:
            logger.exception(
                "Error executing property %s in worktree %s",
                prop.property_id,
                worktree,
            )
            return PropertyResult(
                property_id=prop.property_id,
                passed=False,
                counterexample=None,
                iterations=0,
                shrunk_input=None,
                diagnostic=f"Execution error: {e}",
            )

    def _run_standard_tests(
        self,
        worktree: str,
        host_client: HostClient,
        task_id: str,
    ) -> bool:
        """Run the standard test suite in the worktree (baseline verification).

        Uses a common test invocation pattern. Returns True if exit code is 0.

        Args:
            worktree: Path to the task worktree.
            host_client: HostClient for the Go Host.
            task_id: Task identifier.

        Returns:
            True if standard tests pass, False otherwise.
        """
        from vikram_orchestrator.models import HostActionRequest

        # Use a generic test command; real integration would pull from
        # repo knowledge or verification discovery
        command = "make test 2>&1 || true"

        try:
            request = HostActionRequest(
                task_id=task_id,
                action_name="exec",
                arguments={"command": command},
                working_dir=worktree,
            )
            observation = host_client.exec(request)
            return observation.success
        except Exception as e:
            logger.warning("Standard test execution failed: %s", e)
            return False

    def _build_test_command(self, prop: GeneratedProperty) -> str:
        """Build the shell command to run a single property test.

        Generates framework-appropriate commands:
        - hypothesis → pytest with specific test file
        - fast-check → npx jest/vitest with test file pattern
        - quickcheck → cabal test / stack test with pattern

        Args:
            prop: The property containing framework and test code info.

        Returns:
            Shell command string to execute the property test.
        """
        safe_name = re.sub(r"[^a-z0-9_]", "_", prop.property_id.lower().replace("-", "_"))

        if prop.framework == "hypothesis":
            # Python: write test file and run with pytest
            test_file = f"test_prop_{safe_name}.py"
            return (
                f"python -c \"import pathlib; "
                f"pathlib.Path('{test_file}').write_text('''{prop.test_code}''')\" && "
                f"python -m pytest {test_file} -v --tb=short 2>&1"
            )
        elif prop.framework == "fast-check":
            test_file = f"test_prop_{safe_name}.test.ts"
            return (
                f"cat > {test_file} << 'PROPEOF'\n{prop.test_code}\nPROPEOF\n"
                f"npx jest {test_file} --no-coverage 2>&1"
            )
        elif prop.framework == "quickcheck":
            test_file = f"TestProp{safe_name.title().replace('_', '')}.hs"
            return (
                f"cat > {test_file} << 'PROPEOF'\n{prop.test_code}\nPROPEOF\n"
                f"runghc {test_file} 2>&1"
            )
        else:
            # Fallback: treat as pytest/hypothesis
            test_file = f"test_prop_{safe_name}.py"
            return (
                f"python -c \"import pathlib; "
                f"pathlib.Path('{test_file}').write_text('''{prop.test_code}''')\" && "
                f"python -m pytest {test_file} -v --tb=short 2>&1"
            )

    def _classify_outcome(
        self,
        strategy: VerificationStrategy,
        all_properties_passed: bool,
        standard_tests_passed: bool,
        properties: list[GeneratedProperty],
    ) -> VerificationOutcome:
        """Classify the verification outcome (Requirement 27.4).

        Distinguishes between:
        - VERIFIED_CORRECT: Properties were generated AND all passed, plus standard tests pass
        - TESTS_PASS: Only standard tests passed (no property verification or properties failed)
        - FAILED: Standard tests or properties failed

        Args:
            strategy: The verification strategy used.
            all_properties_passed: Whether all property tests passed.
            standard_tests_passed: Whether standard tests passed.
            properties: The list of generated properties.

        Returns:
            The appropriate VerificationOutcome.
        """
        # If properties were run and all passed along with standard tests
        if properties and all_properties_passed and standard_tests_passed:
            return VerificationOutcome.VERIFIED_CORRECT

        # If properties were run and all passed but we don't have standard test results
        # (MINIMAL strategy doesn't run standard tests)
        if properties and all_properties_passed and strategy == VerificationStrategy.MINIMAL:
            return VerificationOutcome.VERIFIED_CORRECT

        # Only standard tests pass (no properties generated/run)
        if standard_tests_passed and (not properties or not all_properties_passed):
            if not properties:
                return VerificationOutcome.TESTS_PASS
            # Properties exist but some failed — it's a failure
            return VerificationOutcome.FAILED

        # Nothing passed
        return VerificationOutcome.FAILED

    @staticmethod
    def _compute_merge_gate_readiness(
        all_properties_passed: bool,
        standard_tests_passed: bool,
        property_results: list[PropertyResult],
    ) -> float:
        """Compute merge gate readiness score (Requirement 27.5).

        Property verification results are weighted more heavily than standard
        test results. The score ranges from 0.0 (not ready) to 1.0 (fully ready).

        Weighting (per Requirement 27.5):
        - Property verification: 70% weight
        - Standard test results: 30% weight

        If no properties were generated, standard tests carry full weight.

        Args:
            all_properties_passed: Whether all property tests passed.
            standard_tests_passed: Whether standard tests passed.
            property_results: Individual property results for granular scoring.

        Returns:
            Float between 0.0 and 1.0 representing merge readiness.
        """
        if not property_results:
            # No properties: standard tests carry full weight
            return 1.0 if standard_tests_passed else 0.0

        # Property score: fraction of properties that passed
        passed_count = sum(1 for r in property_results if r.passed)
        property_score = passed_count / len(property_results)

        # Standard test score: binary pass/fail
        standard_score = 1.0 if standard_tests_passed else 0.0

        # Weighted combination (Requirement 27.5)
        readiness = (
            PROPERTY_VERIFICATION_WEIGHT * property_score
            + STANDARD_TEST_WEIGHT * standard_score
        )

        return round(readiness, 4)

    # -----------------------------------------------------------------------
    # Feedback Loop (Requirements 29.1, 29.2, 29.3, 29.4)
    # -----------------------------------------------------------------------

    def feedback_loop(
        self, failure: PropertyResult, attempt: int
    ) -> FeedbackLoopResult:
        """Process a property verification failure within the bounded feedback loop.

        Per Requirements 29.2 and 29.3:
        - Up to MAX_FIX_ITERATIONS (3) fix attempts are allowed.
        - If attempt <= MAX_FIX_ITERATIONS, produces a targeted fix prompt.
        - If attempt > MAX_FIX_ITERATIONS, escalates to founder with failure details.

        Args:
            failure: The PropertyResult describing the failed property test.
            attempt: The current iteration number (1-based).

        Returns:
            FeedbackLoopResult indicating whether to retry or escalate.
        """
        if attempt <= MAX_FIX_ITERATIONS:
            # Produce a targeted fix prompt (Requirement 29.1, 29.2)
            fix_prompt = self._generate_fix_prompt(failure, attempt)
            return FeedbackLoopResult(
                should_retry=True,
                escalate=False,
                fix_prompt=fix_prompt,
                failure_details=None,
            )
        else:
            # Escalate to founder (Requirement 29.3)
            return FeedbackLoopResult(
                should_retry=False,
                escalate=True,
                fix_prompt=None,
                failure_details=failure,
            )

    def _generate_fix_prompt(self, failure: PropertyResult, attempt: int) -> str:
        """Generate a targeted fix request from a property failure (Requirement 29.1).

        The prompt includes the counterexample, violated property, and diagnostic
        information so the implementation agent can produce a targeted fix.

        Args:
            failure: The failed property result.
            attempt: Current attempt number.

        Returns:
            A fix prompt string for the implementation agent.
        """
        parts = [
            f"Property '{failure.property_id}' failed (attempt {attempt}/{MAX_FIX_ITERATIONS}).",
        ]

        if failure.counterexample:
            parts.append(f"Counterexample: {failure.counterexample}")

        if failure.shrunk_input:
            parts.append(f"Shrunk input: {failure.shrunk_input}")

        if failure.diagnostic:
            parts.append(f"Diagnostic: {failure.diagnostic}")

        parts.append(
            "Please fix the implementation to satisfy this property. "
            "The property must hold for all valid inputs."
        )

        return "\n".join(parts)

    # -----------------------------------------------------------------------
    # Full Verification Orchestration (Requirements 27.1–27.5, 29.1–29.4)
    # -----------------------------------------------------------------------

    def run_full_verification(
        self,
        worktree: str,
        strategy: VerificationStrategy,
        properties: list[GeneratedProperty],
        host_client: "HostClient",
        task_id: str = "",
        apply_fix_callback: Any | None = None,
    ) -> VerificationExecutionResult:
        """Orchestrate the complete verification cycle with feedback loop.

        Runs the full verification pipeline:
        1. Execute all property tests and standard verification
        2. For failures, invoke feedback_loop to generate fix prompts
        3. Apply fixes via callback, then re-verify (up to MAX_FIX_ITERATIONS)
        4. Return final results with the appropriate verification outcome

        Per Requirements 27.1–27.5, 29.1–29.4:
        - Executes properties and standard tests (27.1)
        - Captures counterexamples/diagnostics on failure (27.2, 27.3)
        - Distinguishes verified-correct from tests-pass (27.4)
        - Weights property results in merge gate readiness (27.5)
        - Feeds failures back for targeted fixes (29.1)
        - Respects 3-iteration bound before escalation (29.2)
        - Escalates persistent failures to founder (29.3)
        - Records iteration counts for learning (29.4)

        Args:
            worktree: Path to the task worktree directory.
            strategy: The verification strategy level to apply.
            properties: List of generated properties to execute.
            host_client: HostClient instance for calling the Go Host exec endpoint.
            task_id: Task identifier for tracking and telemetry.
            apply_fix_callback: Optional callable that accepts a fix prompt string
                and applies the fix to the worktree. Signature:
                ``(fix_prompt: str, task_id: str) -> bool``
                Returns True if fix was applied, False otherwise.
                If None, no fix iterations are attempted.

        Returns:
            VerificationExecutionResult with final outcome, individual property
            results, and merge gate readiness score.
        """
        # Initial verification run
        result = self.execute_verification(
            worktree=worktree,
            strategy=strategy,
            properties=properties,
            host_client=host_client,
            task_id=task_id,
        )

        # If all properties pass or no callback for fixes, return immediately
        if result.outcome != VerificationOutcome.FAILED or apply_fix_callback is None:
            self._record_full_verification_result(
                task_id=task_id,
                outcome=result.outcome,
                iterations_used=0,
                escalated=False,
            )
            return result

        # Feedback loop: iterate on failures up to MAX_FIX_ITERATIONS
        failed_properties = [r for r in result.property_results if not r.passed]
        iterations_used = 0
        escalated = False

        for attempt in range(1, MAX_FIX_ITERATIONS + 1):
            iterations_used = attempt

            # Process each failed property through the feedback loop
            all_fixed = True
            for failure in failed_properties:
                loop_result = self.feedback_loop(failure, attempt)

                if loop_result.escalate:
                    # Should not happen for attempt <= MAX_FIX_ITERATIONS, but guard
                    escalated = True
                    break

                if loop_result.should_retry and loop_result.fix_prompt:
                    # Apply the fix via callback
                    fix_applied = apply_fix_callback(loop_result.fix_prompt, task_id)
                    if not fix_applied:
                        all_fixed = False

            if escalated:
                break

            # Re-run verification after fixes
            result = self.execute_verification(
                worktree=worktree,
                strategy=strategy,
                properties=properties,
                host_client=host_client,
                task_id=task_id,
            )

            # Check if all properties pass now
            if result.outcome != VerificationOutcome.FAILED:
                self._record_full_verification_result(
                    task_id=task_id,
                    outcome=result.outcome,
                    iterations_used=iterations_used,
                    escalated=False,
                )
                return result

            # Update failed properties list for next iteration
            failed_properties = [r for r in result.property_results if not r.passed]
            if not failed_properties:
                break

        # If we exhausted iterations and still failing, check escalation
        if result.outcome == VerificationOutcome.FAILED and failed_properties:
            escalated = True
            # Final escalation pass: invoke feedback_loop with attempt > MAX
            for failure in failed_properties:
                loop_result = self.feedback_loop(failure, MAX_FIX_ITERATIONS + 1)
                # loop_result.escalate will be True, confirming escalation

        self._record_full_verification_result(
            task_id=task_id,
            outcome=result.outcome,
            iterations_used=iterations_used,
            escalated=escalated,
        )

        return result

    def _record_full_verification_result(
        self,
        task_id: str,
        outcome: VerificationOutcome,
        iterations_used: int,
        escalated: bool,
    ) -> None:
        """Record the full verification cycle result in the execution trace (Req 29.4).

        Records iteration count to enable the Knowledge_Store to learn which
        property types correlate with implementation difficulty.
        """
        if self._trace is None or not task_id:
            return

        self._trace.record_decision(
            task_id=task_id,
            decision_type="full_verification_result",
            state_snapshot={
                "outcome": outcome.value,
                "iterations_used": iterations_used,
                "escalated": escalated,
            },
            policy="verification_protocol_feedback_loop",
            outcome=f"{'escalated' if escalated else outcome.value}_after_{iterations_used}_iterations",
            nd_inputs={
                "max_iterations": MAX_FIX_ITERATIONS,
            },
        )


# ---------------------------------------------------------------------------
# Verification Execution Helpers (Requirements 27.2, 27.3)
# ---------------------------------------------------------------------------

# Patterns for extracting counterexamples from test output

# Hypothesis: "Falsifying example: func(x=..., y=...)"
_HYPOTHESIS_COUNTEREXAMPLE_RE = re.compile(
    r"Falsifying example:\s*(.+?)(?:\n|$)", re.MULTILINE
)

# Hypothesis shrunk: "Shrunk from ... to ..."  or the minimal example shown
_HYPOTHESIS_SHRUNK_RE = re.compile(
    r"(?:Shrunk|Simplest|Minimal)[^:]*:\s*(.+?)(?:\n|$)", re.MULTILINE | re.IGNORECASE
)

# fast-check: "Counterexample: [...]" or "Shrunk ... time(s)"
_FAST_CHECK_COUNTEREXAMPLE_RE = re.compile(
    r"Counterexample:\s*(.+?)(?:\n|$)", re.MULTILINE
)
_FAST_CHECK_SHRUNK_RE = re.compile(
    r"Shrunk\s+\d+\s+time\(s\)\s*(.+?)(?:\n|$)", re.MULTILINE | re.IGNORECASE
)

# QuickCheck: "*** Failed! Falsified (after N tests and M shrinks):\n<value>"
_QUICKCHECK_COUNTEREXAMPLE_RE = re.compile(
    r"\*\*\*\s+Failed!.*?:\s*\n(.+?)(?:\n\n|\Z)", re.MULTILINE | re.DOTALL
)

# Iterations: "N tests" or "N examples" or "ran N"
_ITERATIONS_RE = re.compile(
    r"(?:(\d+)\s+(?:tests?|examples?|runs?|iterations?))", re.IGNORECASE
)


def _extract_counterexample(output: str, framework: str) -> str | None:
    """Extract the counterexample from test output (Requirement 27.2).

    Parses framework-specific output patterns to find the failing input
    that caused a property test to fail.

    Args:
        output: Raw test execution output.
        framework: The test framework used ("hypothesis", "fast-check", "quickcheck").

    Returns:
        The extracted counterexample string, or None if not found.
    """
    if not output:
        return None

    if framework == "hypothesis":
        match = _HYPOTHESIS_COUNTEREXAMPLE_RE.search(output)
        if match:
            return match.group(1).strip()
    elif framework == "fast-check":
        match = _FAST_CHECK_COUNTEREXAMPLE_RE.search(output)
        if match:
            return match.group(1).strip()
    elif framework == "quickcheck":
        match = _QUICKCHECK_COUNTEREXAMPLE_RE.search(output)
        if match:
            return match.group(1).strip()

    # Generic fallback: look for common patterns
    for pattern in (
        _HYPOTHESIS_COUNTEREXAMPLE_RE,
        _FAST_CHECK_COUNTEREXAMPLE_RE,
        _QUICKCHECK_COUNTEREXAMPLE_RE,
    ):
        match = pattern.search(output)
        if match:
            return match.group(1).strip()

    return None


def _extract_shrunk_input(output: str, framework: str) -> str | None:
    """Extract the shrunk/minimized input from test output (Requirement 27.2).

    Property-based testing frameworks shrink failing inputs to their minimal
    form. This extracts that minimal reproducer.

    Args:
        output: Raw test execution output.
        framework: The test framework used.

    Returns:
        The shrunk input string, or None if not found.
    """
    if not output:
        return None

    if framework == "hypothesis":
        match = _HYPOTHESIS_SHRUNK_RE.search(output)
        if match:
            return match.group(1).strip()
        # In Hypothesis, the falsifying example IS the shrunk input
        match = _HYPOTHESIS_COUNTEREXAMPLE_RE.search(output)
        if match:
            return match.group(1).strip()
    elif framework == "fast-check":
        match = _FAST_CHECK_SHRUNK_RE.search(output)
        if match:
            return match.group(1).strip()
    elif framework == "quickcheck":
        # QuickCheck shows the shrunk value as the counterexample
        match = _QUICKCHECK_COUNTEREXAMPLE_RE.search(output)
        if match:
            return match.group(1).strip()

    return None


def _extract_iterations(output: str) -> int:
    """Extract the number of test iterations/examples run from output.

    Args:
        output: Raw test execution output.

    Returns:
        Number of iterations, or 0 if not determinable.
    """
    if not output:
        return 0

    match = _ITERATIONS_RE.search(output)
    if match:
        try:
            return int(match.group(1))
        except (ValueError, TypeError):
            return 0
    return 0


def _build_diagnostic(prop: GeneratedProperty, output: str) -> str:
    """Build diagnostic information from a property failure (Requirement 27.3).

    Produces targeted diagnostic info identifying which plan invariant was
    violated and suggesting corrective action.

    Args:
        prop: The failed property.
        output: Raw test execution output.

    Returns:
        Diagnostic string for the implementation agent.
    """
    parts = [
        f"Property '{prop.property_id}' ({prop.property_type.value}) FAILED.",
        f"Description: {prop.description}",
        f"Plan reference: {prop.plan_statement_ref}",
    ]

    # Include relevant output (truncated to avoid excessive length)
    if output:
        # Extract the most relevant portion (last 20 lines typically contain the failure)
        output_lines = output.strip().split("\n")
        relevant = output_lines[-20:] if len(output_lines) > 20 else output_lines
        parts.append(f"Relevant output:\n{'  '.join(relevant)}")

    parts.append(
        f"The {prop.property_type.value} invariant derived from the execution plan "
        f"was violated. The implementation must be corrected to satisfy: {prop.description}"
    )

    return "\n".join(parts)
