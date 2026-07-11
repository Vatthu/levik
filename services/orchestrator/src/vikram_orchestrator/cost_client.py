"""Thin HTTP client for the Go Host Cost Ledger endpoints.

Provides budget queries and cost recording via the Unix domain socket
contract defined in the autonomous engineering platform spec.

Validates: Requirements 3.2, 3.3, 4.1
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel


# --- Response Models ---


class CostForecast(BaseModel):
    """Forecast returned by POST /v1/cost/forecast."""

    min_cost_usd: float
    expected_cost_usd: float
    max_cost_usd: float
    confidence_level: float
    basis_task_count: int


class TaskCostResponse(BaseModel):
    """Response from GET /v1/cost/task/{task_id}."""

    task_id: str
    cumulative_cost_usd: float


class DailyCostResponse(BaseModel):
    """Response from GET /v1/cost/daily."""

    total_cost_usd: float
    reset_at: str


class PhaseBudgetResponse(BaseModel):
    """Response from GET /v1/cost/task/{task_id}/phase/{phase}."""

    task_id: str
    phase: str
    remaining_usd: float


class CostRecordRequest(BaseModel):
    """Payload for POST /v1/cost/record."""

    task_id: str
    role: str
    model: str
    provider: str
    work_phase: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    estimated: bool = False
    duration_ms: int = 0
    invocation_id: str = ""


# --- Client ---


class CostClient:
    """HTTP client for the Go Host Cost Ledger subsystem.

    Communicates over a Unix domain socket following the same pattern
    as HostClient.
    """

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        transport = httpx.HTTPTransport(uds=socket_path)
        self._client = httpx.Client(transport=transport, base_url="http://vikramd")

    def _raise_for_status(self, response: httpx.Response) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text.strip()
            if detail:
                raise httpx.HTTPStatusError(
                    f"{exc}. Response body: {detail}",
                    request=exc.request,
                    response=exc.response,
                ) from exc
            raise

    def _get(self, path: str) -> httpx.Response:
        response = self._client.get(path)
        self._raise_for_status(response)
        return response

    def _post(self, path: str, payload: dict[str, Any] | None = None) -> httpx.Response:
        response = self._client.post(path, json=payload or {})
        self._raise_for_status(response)
        return response

    def get_task_cost(self, task_id: str) -> float:
        """Get cumulative cost for a task.

        Calls GET /v1/cost/task/{task_id}.
        """
        response = self._get(f"/v1/cost/task/{task_id}")
        data = TaskCostResponse.model_validate(response.json())
        return data.cumulative_cost_usd

    def get_phase_remaining(self, task_id: str, phase: str) -> float:
        """Get remaining budget for a specific work phase of a task.

        Calls GET /v1/cost/task/{task_id}/phase/{phase}.
        """
        response = self._get(f"/v1/cost/task/{task_id}/phase/{phase}")
        data = PhaseBudgetResponse.model_validate(response.json())
        return data.remaining_usd

    def get_forecast(self, complexity: str, target_files: int) -> dict[str, Any]:
        """Produce a cost forecast for a new task.

        Calls POST /v1/cost/forecast with complexity and target file count.
        Returns the full forecast as a dict.
        """
        payload = {"complexity": complexity, "target_files": target_files}
        response = self._post("/v1/cost/forecast", payload)
        forecast = CostForecast.model_validate(response.json())
        return forecast.model_dump()

    def get_daily_total(self) -> float:
        """Get system-wide daily spend.

        Calls GET /v1/cost/daily.
        """
        response = self._get("/v1/cost/daily")
        data = DailyCostResponse.model_validate(response.json())
        return data.total_cost_usd

    def record_cost(self, request: CostRecordRequest) -> None:
        """Record a cost event.

        Calls POST /v1/cost/record.
        """
        self._post("/v1/cost/record", request.model_dump())

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()
