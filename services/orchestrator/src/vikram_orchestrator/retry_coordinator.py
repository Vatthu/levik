"""Retry Coordinator — agent call retry with fallback chain.

Wraps agent calls with automatic retry using exponential backoff, maintains
a fallback chain per role, and escalates to the founder when all fallback
models are exhausted.

Module: vikram_orchestrator/retry_coordinator.py
Requirements: 53.1, 53.2, 53.3, 53.4
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol

from pydantic import BaseModel, Field

from .execution_trace import ExecutionTrace
from .platform_resilience import BackoffConfig, RetriesExhausted, compute_backoff


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_BACKOFF_CONFIG = BackoffConfig(
    base_delay=1.0,
    multiplier=2,
    max_delay=60.0,
    max_attempts=3,
)


# ---------------------------------------------------------------------------
# Error Classification
# ---------------------------------------------------------------------------


class RetryableErrorType(str, Enum):
    """Classification of retryable errors per Requirement 53.1."""

    RATE_LIMIT = "rate_limit_429"
    TIMEOUT = "timeout"
    SERVER_ERROR = "server_error_5xx"


def is_retryable_error(error: Exception) -> tuple[bool, RetryableErrorType | None]:
    """Determine if an error is retryable per the retry policy.

    Retryable errors: rate limit (429), timeout, server error (5xx).

    Args:
        error: The exception raised by the agent call.

    Returns:
        Tuple of (is_retryable, error_type). error_type is None if not retryable.
    """
    error_str = str(error).lower()
    error_type_str = type(error).__name__.lower()

    # Check for rate limit (429)
    if "429" in error_str or "rate limit" in error_str or "rate_limit" in error_str:
        return True, RetryableErrorType.RATE_LIMIT

    # Check for timeout
    if (
        "timeout" in error_str
        or "timeout" in error_type_str
        or "timed out" in error_str
    ):
        return True, RetryableErrorType.TIMEOUT

    # Check for server errors (5xx)
    for code in ("500", "502", "503", "504"):
        if code in error_str:
            return True, RetryableErrorType.SERVER_ERROR
    if "server error" in error_str or "internal server error" in error_str:
        return True, RetryableErrorType.SERVER_ERROR

    return False, None


# ---------------------------------------------------------------------------
# Escalation Policy
# ---------------------------------------------------------------------------


class EscalationPolicy(str, Enum):
    """Policy to follow when all fallback models are exhausted."""

    NOTIFY_FOUNDER = "notify_founder"
    FAIL_TASK = "fail_task"
    PAUSE_TASK = "pause_task"


# ---------------------------------------------------------------------------
# Fallback Chain Model
# ---------------------------------------------------------------------------


class FallbackModel(BaseModel):
    """A single model alternative in the fallback chain."""

    model: str
    provider: str
    priority: int = 0  # Lower number = higher priority (tried first)


class FallbackChain(BaseModel):
    """Ordered list of model alternatives for a specific role.

    When the primary model exhausts all retries, the system tries each
    fallback model in priority order (lowest priority number first).
    Each fallback model also gets max_attempts retries with backoff.
    """

    role: str
    primary_model: str
    primary_provider: str
    fallbacks: list[FallbackModel] = Field(default_factory=list)
    escalation_policy: EscalationPolicy = EscalationPolicy.NOTIFY_FOUNDER

    def get_ordered_fallbacks(self) -> list[FallbackModel]:
        """Return fallback models sorted by priority (lowest first)."""
        return sorted(self.fallbacks, key=lambda f: f.priority)


# ---------------------------------------------------------------------------
# Retry Attempt Record
# ---------------------------------------------------------------------------


class RetryAttemptRecord(BaseModel):
    """Record of a single retry attempt for trace recording."""

    attempt_number: int
    model: str
    provider: str
    error_type: str
    backoff_duration: float
    timestamp: float
    is_fallback: bool = False
    fallback_index: int | None = None


class RetryResolution(BaseModel):
    """Final resolution of the retry/fallback sequence."""

    success: bool
    model_used: str
    provider_used: str
    total_attempts: int
    fallback_activated: bool
    fallback_models_tried: int
    escalated: bool
    escalation_policy: str | None = None
    attempts: list[RetryAttemptRecord] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Agent Call Protocol
# ---------------------------------------------------------------------------


class AgentCallFunc(Protocol):
    """Protocol for agent call functions that can be retried."""

    def __call__(
        self,
        role: str,
        prompt: str,
        model: str | None = None,
        provider: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Make an agent call and return the response content."""
        ...


# ---------------------------------------------------------------------------
# All Fallback Models Exhausted Exception
# ---------------------------------------------------------------------------


class AllFallbacksExhausted(Exception):
    """Raised when all models in the fallback chain are exhausted."""

    def __init__(
        self,
        role: str,
        models_tried: list[str],
        resolution: RetryResolution,
    ) -> None:
        self.role = role
        self.models_tried = models_tried
        self.resolution = resolution
        super().__init__(
            f"All fallback models exhausted for role '{role}'. "
            f"Models tried: {models_tried}"
        )


# ---------------------------------------------------------------------------
# Retry Coordinator
# ---------------------------------------------------------------------------


class RetryCoordinator:
    """Coordinates agent call retries with fallback chain and escalation.

    Wraps agent calls with automatic retry using exponential backoff (via
    compute_backoff from platform_resilience). When retries for the primary
    model are exhausted, activates the fallback chain. When all fallback
    models are also exhausted, triggers the configured escalation policy.

    All retry attempts and fallback activations are recorded in the
    Execution_Trace.

    Usage:
        coordinator = RetryCoordinator(
            execution_trace=trace,
            fallback_chains={"planner": chain},
        )
        result = coordinator.call_with_retry(
            agent_call_fn=my_agent_call,
            task_id="task-123",
            role="planner",
            prompt="Plan the implementation...",
        )
    """

    def __init__(
        self,
        execution_trace: ExecutionTrace,
        fallback_chains: dict[str, FallbackChain] | None = None,
        backoff_config: BackoffConfig | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        escalation_handler: Callable[[str, str, RetryResolution], None] | None = None,
    ) -> None:
        """Initialize the RetryCoordinator.

        Args:
            execution_trace: The execution trace for recording decisions.
            fallback_chains: Map of role -> FallbackChain configuration.
            backoff_config: Backoff configuration (defaults to standard config).
            sleep_fn: Function to call for delays (defaults to time.sleep).
                      Can be overridden for testing.
            escalation_handler: Callback invoked when escalation is triggered.
                               Receives (task_id, role, resolution).
        """
        self._trace = execution_trace
        self._fallback_chains = fallback_chains or {}
        self._backoff_config = backoff_config or DEFAULT_BACKOFF_CONFIG
        self._sleep_fn = sleep_fn or time.sleep
        self._escalation_handler = escalation_handler

    @property
    def fallback_chains(self) -> dict[str, FallbackChain]:
        """Return the configured fallback chains."""
        return self._fallback_chains

    def set_fallback_chain(self, role: str, chain: FallbackChain) -> None:
        """Configure or update the fallback chain for a role."""
        self._fallback_chains[role] = chain

    def call_with_retry(
        self,
        agent_call_fn: Callable[..., str],
        task_id: str,
        role: str,
        prompt: str,
        model: str | None = None,
        provider: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Execute an agent call with retry and fallback chain.

        Attempts the call with the primary model using exponential backoff.
        On exhaustion of primary retries, activates fallback chain models
        in order. On exhaustion of all fallback models, escalates per policy.

        Args:
            agent_call_fn: The function to call (must accept role, prompt,
                          model, provider keyword arguments).
            task_id: The task this call belongs to.
            role: The agent role being called.
            prompt: The prompt to send.
            model: Override model (uses chain primary if not specified).
            provider: Override provider (uses chain primary if not specified).
            **kwargs: Additional arguments passed to agent_call_fn.

        Returns:
            The response content string from the successful agent call.

        Raises:
            AllFallbacksExhausted: When all models are exhausted and
                escalation_policy is FAIL_TASK.
        """
        attempts: list[RetryAttemptRecord] = []
        chain = self._fallback_chains.get(role)

        # Determine primary model/provider
        primary_model = model or (chain.primary_model if chain else None)
        primary_provider = provider or (chain.primary_provider if chain else None)

        # Try primary model with retries
        result = self._try_with_retries(
            agent_call_fn=agent_call_fn,
            task_id=task_id,
            role=role,
            prompt=prompt,
            model=primary_model,
            provider=primary_provider,
            attempts=attempts,
            is_fallback=False,
            fallback_index=None,
            **kwargs,
        )

        if result is not None:
            # Success on primary model
            resolution = RetryResolution(
                success=True,
                model_used=primary_model or "default",
                provider_used=primary_provider or "default",
                total_attempts=len(attempts) + 1,
                fallback_activated=False,
                fallback_models_tried=0,
                escalated=False,
                attempts=attempts,
            )
            self._record_resolution(task_id, role, resolution)
            return result

        # Primary exhausted — activate fallback chain
        if chain and chain.fallbacks:
            ordered_fallbacks = chain.get_ordered_fallbacks()
            for fb_idx, fallback in enumerate(ordered_fallbacks):
                # Record fallback activation in trace
                self._record_fallback_activation(
                    task_id=task_id,
                    role=role,
                    from_model=primary_model or "default",
                    to_model=fallback.model,
                    fallback_index=fb_idx,
                )

                result = self._try_with_retries(
                    agent_call_fn=agent_call_fn,
                    task_id=task_id,
                    role=role,
                    prompt=prompt,
                    model=fallback.model,
                    provider=fallback.provider,
                    attempts=attempts,
                    is_fallback=True,
                    fallback_index=fb_idx,
                    **kwargs,
                )

                if result is not None:
                    resolution = RetryResolution(
                        success=True,
                        model_used=fallback.model,
                        provider_used=fallback.provider,
                        total_attempts=len(attempts) + 1,
                        fallback_activated=True,
                        fallback_models_tried=fb_idx + 1,
                        escalated=False,
                        attempts=attempts,
                    )
                    self._record_resolution(task_id, role, resolution)
                    return result

        # All models exhausted — escalate
        models_tried = [primary_model or "default"]
        if chain and chain.fallbacks:
            models_tried.extend(fb.model for fb in chain.get_ordered_fallbacks())

        escalation_policy = (
            chain.escalation_policy if chain else EscalationPolicy.NOTIFY_FOUNDER
        )

        resolution = RetryResolution(
            success=False,
            model_used="none",
            provider_used="none",
            total_attempts=len(attempts),
            fallback_activated=bool(chain and chain.fallbacks),
            fallback_models_tried=len(chain.fallbacks) if chain else 0,
            escalated=True,
            escalation_policy=escalation_policy.value,
            attempts=attempts,
        )

        self._record_escalation(task_id, role, resolution, escalation_policy)

        # Invoke escalation handler if configured
        if self._escalation_handler:
            self._escalation_handler(task_id, role, resolution)

        if escalation_policy == EscalationPolicy.FAIL_TASK:
            raise AllFallbacksExhausted(
                role=role,
                models_tried=models_tried,
                resolution=resolution,
            )

        # For NOTIFY_FOUNDER and PAUSE_TASK, raise to signal the caller
        raise AllFallbacksExhausted(
            role=role,
            models_tried=models_tried,
            resolution=resolution,
        )

    def _try_with_retries(
        self,
        agent_call_fn: Callable[..., str],
        task_id: str,
        role: str,
        prompt: str,
        model: str | None,
        provider: str | None,
        attempts: list[RetryAttemptRecord],
        is_fallback: bool,
        fallback_index: int | None,
        **kwargs: Any,
    ) -> str | None:
        """Try calling the agent with exponential backoff retries.

        Returns the response content on success, or None if all retries
        are exhausted.
        """
        last_error: Exception | None = None

        for attempt_num in range(1, self._backoff_config.max_attempts + 1):
            try:
                result = agent_call_fn(
                    role=role,
                    prompt=prompt,
                    model=model,
                    provider=provider,
                    **kwargs,
                )
                return result
            except Exception as e:
                retryable, error_type = is_retryable_error(e)
                last_error = e

                if not retryable:
                    # Non-retryable error — record and give up immediately
                    attempts.append(
                        RetryAttemptRecord(
                            attempt_number=attempt_num,
                            model=model or "default",
                            provider=provider or "default",
                            error_type=f"non_retryable: {type(e).__name__}",
                            backoff_duration=0.0,
                            timestamp=time.time(),
                            is_fallback=is_fallback,
                            fallback_index=fallback_index,
                        )
                    )
                    self._record_retry_attempt(
                        task_id=task_id,
                        role=role,
                        attempt_num=attempt_num,
                        model=model or "default",
                        error_type=f"non_retryable: {type(e).__name__}",
                        backoff_duration=0.0,
                        is_fallback=is_fallback,
                    )
                    return None

                # Retryable error — compute backoff and wait
                backoff_delay = compute_backoff(attempt_num, self._backoff_config)

                attempts.append(
                    RetryAttemptRecord(
                        attempt_number=attempt_num,
                        model=model or "default",
                        provider=provider or "default",
                        error_type=error_type.value if error_type else "unknown",
                        backoff_duration=backoff_delay,
                        timestamp=time.time(),
                        is_fallback=is_fallback,
                        fallback_index=fallback_index,
                    )
                )

                self._record_retry_attempt(
                    task_id=task_id,
                    role=role,
                    attempt_num=attempt_num,
                    model=model or "default",
                    error_type=error_type.value if error_type else "unknown",
                    backoff_duration=backoff_delay,
                    is_fallback=is_fallback,
                )

                # Wait before next attempt (unless it's the last attempt)
                if attempt_num < self._backoff_config.max_attempts:
                    self._sleep_fn(backoff_delay)

        # All retries exhausted for this model
        return None

    # -----------------------------------------------------------------------
    # Execution Trace Recording
    # -----------------------------------------------------------------------

    def _record_retry_attempt(
        self,
        task_id: str,
        role: str,
        attempt_num: int,
        model: str,
        error_type: str,
        backoff_duration: float,
        is_fallback: bool,
    ) -> None:
        """Record a retry attempt in the Execution_Trace."""
        self._trace.record_decision(
            task_id=task_id,
            decision_type="retry_attempt",
            state_snapshot={
                "role": role,
                "attempt_number": attempt_num,
                "model": model,
                "is_fallback": is_fallback,
                "max_attempts": self._backoff_config.max_attempts,
            },
            policy="retry_with_exponential_backoff",
            outcome=f"retry_attempt_{attempt_num}",
            nd_inputs={
                "error_type": error_type,
                "backoff_duration": backoff_duration,
                "timestamp": time.time(),
            },
        )

    def _record_fallback_activation(
        self,
        task_id: str,
        role: str,
        from_model: str,
        to_model: str,
        fallback_index: int,
    ) -> None:
        """Record a fallback chain activation in the Execution_Trace."""
        self._trace.record_decision(
            task_id=task_id,
            decision_type="fallback_activation",
            state_snapshot={
                "role": role,
                "from_model": from_model,
                "to_model": to_model,
                "fallback_index": fallback_index,
            },
            policy="fallback_chain_activation",
            outcome=f"activated_fallback_{fallback_index}",
            nd_inputs={"timestamp": time.time()},
        )

    def _record_resolution(
        self,
        task_id: str,
        role: str,
        resolution: RetryResolution,
    ) -> None:
        """Record the final resolution in the Execution_Trace."""
        self._trace.record_decision(
            task_id=task_id,
            decision_type="retry_resolution",
            state_snapshot={
                "role": role,
                "model_used": resolution.model_used,
                "provider_used": resolution.provider_used,
                "total_attempts": resolution.total_attempts,
                "fallback_activated": resolution.fallback_activated,
                "fallback_models_tried": resolution.fallback_models_tried,
            },
            policy="retry_resolution",
            outcome="success" if resolution.success else "exhausted",
            nd_inputs={"timestamp": time.time()},
        )

    def _record_escalation(
        self,
        task_id: str,
        role: str,
        resolution: RetryResolution,
        escalation_policy: EscalationPolicy,
    ) -> None:
        """Record an escalation event in the Execution_Trace."""
        self._trace.record_decision(
            task_id=task_id,
            decision_type="escalation",
            state_snapshot={
                "role": role,
                "total_attempts": resolution.total_attempts,
                "fallback_models_tried": resolution.fallback_models_tried,
                "escalation_policy": escalation_policy.value,
            },
            policy="all_fallbacks_exhausted_escalation",
            outcome=f"escalated_{escalation_policy.value}",
            nd_inputs={"timestamp": time.time()},
        )
