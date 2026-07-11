"""Unit tests for Retry Coordinator — agent call retry with fallback chain.

Tests cover:
- Exponential backoff retry behavior (Requirement 53.1)
- Fallback chain activation after primary retries exhausted (Requirement 53.2)
- Escalation policy when all fallback models exhausted (Requirement 53.3)
- Execution trace recording of retry attempts and fallback activations (Requirement 53.4)

Run with:
  cd services/orchestrator && .venv/bin/python -m pytest tests/test_retry_coordinator.py -v
"""

from __future__ import annotations

import pytest

from vikram_orchestrator.execution_trace import ExecutionTrace
from vikram_orchestrator.platform_resilience import BackoffConfig
from vikram_orchestrator.retry_coordinator import (
    AllFallbacksExhausted,
    DEFAULT_BACKOFF_CONFIG,
    EscalationPolicy,
    FallbackChain,
    FallbackModel,
    RetryCoordinator,
    RetryableErrorType,
    is_retryable_error,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class RateLimitError(Exception):
    """Simulates a 429 rate limit error."""

    def __init__(self) -> None:
        super().__init__("HTTP 429: Rate limit exceeded")


class TimeoutError(Exception):
    """Simulates a timeout error."""

    def __init__(self) -> None:
        super().__init__("Request timed out")


class ServerError(Exception):
    """Simulates a 500 server error."""

    def __init__(self) -> None:
        super().__init__("HTTP 500: Internal server error")


class NonRetryableError(Exception):
    """Simulates a non-retryable error (e.g., 400 bad request)."""

    def __init__(self) -> None:
        super().__init__("HTTP 400: Bad request - invalid prompt")


def make_failing_then_succeeding_fn(
    fail_count: int, error_class: type[Exception] = RateLimitError
):
    """Create a function that fails `fail_count` times then succeeds."""
    call_count = {"n": 0}

    def fn(role: str, prompt: str, model: str | None = None, provider: str | None = None, **kwargs):
        call_count["n"] += 1
        if call_count["n"] <= fail_count:
            raise error_class()
        return f"success from {model or 'default'}"

    fn.call_count = call_count
    return fn


def make_always_failing_fn(error_class: type[Exception] = RateLimitError):
    """Create a function that always raises the given error."""
    call_count = {"n": 0}

    def fn(role: str, prompt: str, model: str | None = None, provider: str | None = None, **kwargs):
        call_count["n"] += 1
        raise error_class()

    fn.call_count = call_count
    return fn


def make_model_aware_fn(success_model: str):
    """Create a function that only succeeds for a specific model."""
    call_count = {"n": 0}

    def fn(role: str, prompt: str, model: str | None = None, provider: str | None = None, **kwargs):
        call_count["n"] += 1
        if model == success_model:
            return f"success from {model}"
        raise ServerError()

    fn.call_count = call_count
    return fn


def noop_sleep(seconds: float) -> None:
    """No-op sleep for testing — records sleep calls but doesn't wait."""
    pass


def make_recording_sleep():
    """Create a sleep function that records all sleep durations."""
    sleeps: list[float] = []

    def sleep_fn(seconds: float) -> None:
        sleeps.append(seconds)

    sleep_fn.sleeps = sleeps
    return sleep_fn


# ---------------------------------------------------------------------------
# Test: Error classification
# ---------------------------------------------------------------------------


class TestErrorClassification:
    """Test is_retryable_error correctly classifies errors."""

    def test_rate_limit_429(self):
        retryable, error_type = is_retryable_error(RateLimitError())
        assert retryable is True
        assert error_type == RetryableErrorType.RATE_LIMIT

    def test_timeout_error(self):
        retryable, error_type = is_retryable_error(TimeoutError())
        assert retryable is True
        assert error_type == RetryableErrorType.TIMEOUT

    def test_server_error_500(self):
        retryable, error_type = is_retryable_error(ServerError())
        assert retryable is True
        assert error_type == RetryableErrorType.SERVER_ERROR

    def test_non_retryable_error(self):
        retryable, error_type = is_retryable_error(NonRetryableError())
        assert retryable is False
        assert error_type is None

    def test_generic_error_not_retryable(self):
        retryable, error_type = is_retryable_error(ValueError("something wrong"))
        assert retryable is False
        assert error_type is None


# ---------------------------------------------------------------------------
# Test: Exponential backoff retry (Requirement 53.1)
# ---------------------------------------------------------------------------


class TestExponentialBackoffRetry:
    """Test that retries use exponential backoff correctly."""

    def test_success_on_first_attempt_no_retry(self):
        """Agent call succeeds on first attempt — no retries needed."""
        trace = ExecutionTrace()
        coordinator = RetryCoordinator(
            execution_trace=trace,
            sleep_fn=noop_sleep,
        )

        fn = make_failing_then_succeeding_fn(fail_count=0)
        result = coordinator.call_with_retry(
            agent_call_fn=fn,
            task_id="task-1",
            role="planner",
            prompt="Plan something",
        )

        assert result == "success from default"
        assert fn.call_count["n"] == 1

    def test_success_after_retries(self):
        """Agent call succeeds after 2 transient failures."""
        trace = ExecutionTrace()
        sleep_fn = make_recording_sleep()
        coordinator = RetryCoordinator(
            execution_trace=trace,
            sleep_fn=sleep_fn,
        )

        fn = make_failing_then_succeeding_fn(fail_count=2)
        result = coordinator.call_with_retry(
            agent_call_fn=fn,
            task_id="task-1",
            role="planner",
            prompt="Plan something",
        )

        assert result == "success from default"
        assert fn.call_count["n"] == 3  # 2 failures + 1 success
        # Should have slept between retries with exponential backoff
        assert len(sleep_fn.sleeps) == 2
        assert sleep_fn.sleeps[0] == 1.0  # base_delay * 2^0
        assert sleep_fn.sleeps[1] == 2.0  # base_delay * 2^1

    def test_backoff_delays_correct_for_all_attempts(self):
        """Verify backoff delays follow the exponential formula."""
        trace = ExecutionTrace()
        sleep_fn = make_recording_sleep()
        config = BackoffConfig(base_delay=1.0, multiplier=2, max_delay=60.0, max_attempts=3)
        coordinator = RetryCoordinator(
            execution_trace=trace,
            backoff_config=config,
            sleep_fn=sleep_fn,
        )

        # Fail all 3 attempts on primary, then exhaust
        chain = FallbackChain(
            role="planner",
            primary_model="gpt-4",
            primary_provider="openai",
            fallbacks=[],
            escalation_policy=EscalationPolicy.FAIL_TASK,
        )
        coordinator.set_fallback_chain("planner", chain)

        fn = make_always_failing_fn(RateLimitError)

        with pytest.raises(AllFallbacksExhausted):
            coordinator.call_with_retry(
                agent_call_fn=fn,
                task_id="task-1",
                role="planner",
                prompt="Plan something",
                model="gpt-4",
                provider="openai",
            )

        # 3 attempts, sleeps between attempts 1→2 and 2→3
        assert len(sleep_fn.sleeps) == 2
        assert sleep_fn.sleeps[0] == 1.0  # min(1 * 2^0, 60) = 1
        assert sleep_fn.sleeps[1] == 2.0  # min(1 * 2^1, 60) = 2

    def test_non_retryable_error_fails_immediately(self):
        """Non-retryable errors don't trigger retries — fail immediately."""
        trace = ExecutionTrace()
        sleep_fn = make_recording_sleep()
        chain = FallbackChain(
            role="planner",
            primary_model="gpt-4",
            primary_provider="openai",
            fallbacks=[],
            escalation_policy=EscalationPolicy.FAIL_TASK,
        )
        coordinator = RetryCoordinator(
            execution_trace=trace,
            fallback_chains={"planner": chain},
            sleep_fn=sleep_fn,
        )

        fn = make_always_failing_fn(NonRetryableError)

        with pytest.raises(AllFallbacksExhausted):
            coordinator.call_with_retry(
                agent_call_fn=fn,
                task_id="task-1",
                role="planner",
                prompt="Plan something",
            )

        # Only 1 attempt made — no retries for non-retryable errors
        assert fn.call_count["n"] == 1
        assert len(sleep_fn.sleeps) == 0


# ---------------------------------------------------------------------------
# Test: Fallback chain activation (Requirement 53.2)
# ---------------------------------------------------------------------------


class TestFallbackChainActivation:
    """Test that fallback chain activates after primary retries are exhausted."""

    def test_fallback_activated_after_primary_exhaustion(self):
        """When primary model exhausts retries, fallback model is tried."""
        trace = ExecutionTrace()
        chain = FallbackChain(
            role="planner",
            primary_model="gpt-4",
            primary_provider="openai",
            fallbacks=[
                FallbackModel(model="claude-3", provider="anthropic", priority=0),
            ],
        )
        coordinator = RetryCoordinator(
            execution_trace=trace,
            fallback_chains={"planner": chain},
            sleep_fn=noop_sleep,
        )

        # Only succeeds for claude-3
        fn = make_model_aware_fn(success_model="claude-3")

        result = coordinator.call_with_retry(
            agent_call_fn=fn,
            task_id="task-1",
            role="planner",
            prompt="Plan something",
        )

        assert result == "success from claude-3"
        # Primary: 3 attempts all failed, then fallback: 1 success
        assert fn.call_count["n"] == 4

    def test_fallback_chain_order_respected(self):
        """Fallback models are tried in priority order (lowest first)."""
        trace = ExecutionTrace()
        chain = FallbackChain(
            role="planner",
            primary_model="gpt-4",
            primary_provider="openai",
            fallbacks=[
                FallbackModel(model="gemini-pro", provider="google", priority=2),
                FallbackModel(model="claude-3", provider="anthropic", priority=1),
                FallbackModel(model="llama-3", provider="meta", priority=0),
            ],
        )
        coordinator = RetryCoordinator(
            execution_trace=trace,
            fallback_chains={"planner": chain},
            sleep_fn=noop_sleep,
        )

        # Only succeeds for claude-3 (priority 1, tried second)
        fn = make_model_aware_fn(success_model="claude-3")

        result = coordinator.call_with_retry(
            agent_call_fn=fn,
            task_id="task-1",
            role="planner",
            prompt="Plan something",
        )

        assert result == "success from claude-3"
        # Primary (3 failures) + llama-3 (3 failures) + claude-3 (1 success)
        assert fn.call_count["n"] == 7

    def test_each_fallback_gets_full_retries(self):
        """Each fallback model gets the full max_attempts retries."""
        trace = ExecutionTrace()
        config = BackoffConfig(base_delay=1.0, multiplier=2, max_delay=60.0, max_attempts=3)
        chain = FallbackChain(
            role="planner",
            primary_model="gpt-4",
            primary_provider="openai",
            fallbacks=[
                FallbackModel(model="claude-3", provider="anthropic", priority=0),
            ],
            escalation_policy=EscalationPolicy.FAIL_TASK,
        )
        coordinator = RetryCoordinator(
            execution_trace=trace,
            fallback_chains={"planner": chain},
            backoff_config=config,
            sleep_fn=noop_sleep,
        )

        fn = make_always_failing_fn(RateLimitError)

        with pytest.raises(AllFallbacksExhausted):
            coordinator.call_with_retry(
                agent_call_fn=fn,
                task_id="task-1",
                role="planner",
                prompt="Plan something",
            )

        # Primary: 3 attempts + Fallback: 3 attempts = 6 total
        assert fn.call_count["n"] == 6

    def test_no_fallback_chain_configured(self):
        """When no fallback chain is configured, escalates after primary exhaustion."""
        trace = ExecutionTrace()
        coordinator = RetryCoordinator(
            execution_trace=trace,
            sleep_fn=noop_sleep,
        )

        fn = make_always_failing_fn(RateLimitError)

        with pytest.raises(AllFallbacksExhausted) as exc_info:
            coordinator.call_with_retry(
                agent_call_fn=fn,
                task_id="task-1",
                role="planner",
                prompt="Plan something",
            )

        assert exc_info.value.role == "planner"
        assert exc_info.value.resolution.escalated is True


# ---------------------------------------------------------------------------
# Test: Escalation policy (Requirement 53.3)
# ---------------------------------------------------------------------------


class TestEscalationPolicy:
    """Test escalation policies when all fallback models are exhausted."""

    def test_escalation_fail_task(self):
        """FAIL_TASK escalation raises AllFallbacksExhausted."""
        trace = ExecutionTrace()
        chain = FallbackChain(
            role="planner",
            primary_model="gpt-4",
            primary_provider="openai",
            fallbacks=[
                FallbackModel(model="claude-3", provider="anthropic", priority=0),
            ],
            escalation_policy=EscalationPolicy.FAIL_TASK,
        )
        coordinator = RetryCoordinator(
            execution_trace=trace,
            fallback_chains={"planner": chain},
            sleep_fn=noop_sleep,
        )

        fn = make_always_failing_fn(ServerError)

        with pytest.raises(AllFallbacksExhausted) as exc_info:
            coordinator.call_with_retry(
                agent_call_fn=fn,
                task_id="task-1",
                role="planner",
                prompt="Plan something",
            )

        assert exc_info.value.resolution.escalated is True
        assert exc_info.value.resolution.escalation_policy == "fail_task"
        assert "gpt-4" in exc_info.value.models_tried
        assert "claude-3" in exc_info.value.models_tried

    def test_escalation_notify_founder(self):
        """NOTIFY_FOUNDER escalation raises AllFallbacksExhausted."""
        trace = ExecutionTrace()
        chain = FallbackChain(
            role="planner",
            primary_model="gpt-4",
            primary_provider="openai",
            fallbacks=[],
            escalation_policy=EscalationPolicy.NOTIFY_FOUNDER,
        )
        coordinator = RetryCoordinator(
            execution_trace=trace,
            fallback_chains={"planner": chain},
            sleep_fn=noop_sleep,
        )

        fn = make_always_failing_fn(RateLimitError)

        with pytest.raises(AllFallbacksExhausted) as exc_info:
            coordinator.call_with_retry(
                agent_call_fn=fn,
                task_id="task-1",
                role="planner",
                prompt="Plan something",
            )

        assert exc_info.value.resolution.escalation_policy == "notify_founder"

    def test_escalation_handler_called(self):
        """Custom escalation handler is invoked on exhaustion."""
        trace = ExecutionTrace()
        handler_calls: list[tuple] = []

        def handler(task_id, role, resolution):
            handler_calls.append((task_id, role, resolution))

        chain = FallbackChain(
            role="planner",
            primary_model="gpt-4",
            primary_provider="openai",
            fallbacks=[],
            escalation_policy=EscalationPolicy.FAIL_TASK,
        )
        coordinator = RetryCoordinator(
            execution_trace=trace,
            fallback_chains={"planner": chain},
            sleep_fn=noop_sleep,
            escalation_handler=handler,
        )

        fn = make_always_failing_fn(RateLimitError)

        with pytest.raises(AllFallbacksExhausted):
            coordinator.call_with_retry(
                agent_call_fn=fn,
                task_id="task-1",
                role="planner",
                prompt="Plan something",
            )

        assert len(handler_calls) == 1
        assert handler_calls[0][0] == "task-1"
        assert handler_calls[0][1] == "planner"
        assert handler_calls[0][2].escalated is True


# ---------------------------------------------------------------------------
# Test: Execution Trace recording (Requirement 53.4)
# ---------------------------------------------------------------------------


class TestExecutionTraceRecording:
    """Test that retry attempts and fallback activations are recorded in trace."""

    def test_retry_attempts_recorded(self):
        """Each retry attempt is recorded in the Execution_Trace."""
        trace = ExecutionTrace()
        coordinator = RetryCoordinator(
            execution_trace=trace,
            sleep_fn=noop_sleep,
        )

        fn = make_failing_then_succeeding_fn(fail_count=2)
        coordinator.call_with_retry(
            agent_call_fn=fn,
            task_id="task-1",
            role="planner",
            prompt="Plan something",
        )

        # Should have retry_attempt records
        retry_records = trace.query(
            task_id="task-1", decision_type="retry_attempt"
        )
        assert len(retry_records) == 2  # 2 failed attempts recorded

        # Check first record details
        assert retry_records[0].state_snapshot["role"] == "planner"
        assert retry_records[0].state_snapshot["attempt_number"] == 1

    def test_fallback_activation_recorded(self):
        """Fallback activation is recorded in the Execution_Trace."""
        trace = ExecutionTrace()
        chain = FallbackChain(
            role="planner",
            primary_model="gpt-4",
            primary_provider="openai",
            fallbacks=[
                FallbackModel(model="claude-3", provider="anthropic", priority=0),
            ],
        )
        coordinator = RetryCoordinator(
            execution_trace=trace,
            fallback_chains={"planner": chain},
            sleep_fn=noop_sleep,
        )

        fn = make_model_aware_fn(success_model="claude-3")
        coordinator.call_with_retry(
            agent_call_fn=fn,
            task_id="task-1",
            role="planner",
            prompt="Plan something",
        )

        # Should have fallback_activation record
        fallback_records = trace.query(
            task_id="task-1", decision_type="fallback_activation"
        )
        assert len(fallback_records) == 1
        assert fallback_records[0].state_snapshot["from_model"] == "gpt-4"
        assert fallback_records[0].state_snapshot["to_model"] == "claude-3"

    def test_resolution_recorded_on_success(self):
        """Successful resolution is recorded in the Execution_Trace."""
        trace = ExecutionTrace()
        coordinator = RetryCoordinator(
            execution_trace=trace,
            sleep_fn=noop_sleep,
        )

        fn = make_failing_then_succeeding_fn(fail_count=1)
        coordinator.call_with_retry(
            agent_call_fn=fn,
            task_id="task-1",
            role="planner",
            prompt="Plan something",
        )

        resolution_records = trace.query(
            task_id="task-1", decision_type="retry_resolution"
        )
        assert len(resolution_records) == 1
        assert resolution_records[0].outcome == "success"

    def test_escalation_recorded(self):
        """Escalation is recorded in the Execution_Trace."""
        trace = ExecutionTrace()
        chain = FallbackChain(
            role="planner",
            primary_model="gpt-4",
            primary_provider="openai",
            fallbacks=[],
            escalation_policy=EscalationPolicy.FAIL_TASK,
        )
        coordinator = RetryCoordinator(
            execution_trace=trace,
            fallback_chains={"planner": chain},
            sleep_fn=noop_sleep,
        )

        fn = make_always_failing_fn(RateLimitError)

        with pytest.raises(AllFallbacksExhausted):
            coordinator.call_with_retry(
                agent_call_fn=fn,
                task_id="task-1",
                role="planner",
                prompt="Plan something",
            )

        escalation_records = trace.query(
            task_id="task-1", decision_type="escalation"
        )
        assert len(escalation_records) == 1
        assert "escalated" in escalation_records[0].outcome
        assert escalation_records[0].state_snapshot["escalation_policy"] == "fail_task"

    def test_trace_records_error_type_and_backoff(self):
        """Trace records include error type and backoff duration per Req 53.4."""
        trace = ExecutionTrace()
        coordinator = RetryCoordinator(
            execution_trace=trace,
            sleep_fn=noop_sleep,
        )

        fn = make_failing_then_succeeding_fn(fail_count=1, error_class=TimeoutError)
        coordinator.call_with_retry(
            agent_call_fn=fn,
            task_id="task-1",
            role="planner",
            prompt="Plan something",
        )

        retry_records = trace.query(
            task_id="task-1", decision_type="retry_attempt"
        )
        assert len(retry_records) == 1
        assert retry_records[0].state_snapshot["model"] == "default"
        nd = retry_records[0].non_deterministic_inputs
        assert nd["error_type"] == "timeout"
        assert nd["backoff_duration"] == 1.0  # First attempt: base_delay * 2^0


# ---------------------------------------------------------------------------
# Test: FallbackChain model
# ---------------------------------------------------------------------------


class TestFallbackChainModel:
    """Test FallbackChain configuration model."""

    def test_ordered_fallbacks_by_priority(self):
        """Fallbacks are returned in priority order (lowest first)."""
        chain = FallbackChain(
            role="planner",
            primary_model="gpt-4",
            primary_provider="openai",
            fallbacks=[
                FallbackModel(model="gemini", provider="google", priority=3),
                FallbackModel(model="claude", provider="anthropic", priority=1),
                FallbackModel(model="llama", provider="meta", priority=2),
            ],
        )

        ordered = chain.get_ordered_fallbacks()
        assert [f.model for f in ordered] == ["claude", "llama", "gemini"]

    def test_empty_fallback_chain(self):
        """Empty fallback chain returns empty ordered list."""
        chain = FallbackChain(
            role="planner",
            primary_model="gpt-4",
            primary_provider="openai",
        )

        assert chain.get_ordered_fallbacks() == []

    def test_default_escalation_policy(self):
        """Default escalation policy is NOTIFY_FOUNDER."""
        chain = FallbackChain(
            role="planner",
            primary_model="gpt-4",
            primary_provider="openai",
        )
        assert chain.escalation_policy == EscalationPolicy.NOTIFY_FOUNDER
