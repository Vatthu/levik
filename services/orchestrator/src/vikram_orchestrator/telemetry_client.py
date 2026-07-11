"""Telemetry client for emitting events and querying metrics from the Go Host.

This module provides a thin Python client that communicates with the Go Host's
telemetry subsystem over the Unix domain socket. It handles phase_transition
event emission (Requirement 8.3) and telemetry query access (Requirement 9.1).
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx

from .settings import settings


class TelemetryClient:
    """Client for the Go Host telemetry endpoints.

    Communicates over a Unix domain socket using HTTP+JSON, following the same
    pattern as HostClient.
    """

    def __init__(self, socket_path: str | None = None) -> None:
        self.socket_path = socket_path or settings.host_socket
        transport = httpx.HTTPTransport(uds=self.socket_path)
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

    def _post(self, path: str, payload: dict[str, object]) -> httpx.Response:
        response = self._client.post(path, json=payload)
        self._raise_for_status(response)
        return response

    def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        response = self._client.get(path, params=params)
        self._raise_for_status(response)
        return response

    def emit_event(
        self,
        event_type: str,
        task_id: str,
        attributes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Emit a generic telemetry event to the Go Host.

        Args:
            event_type: The type of event (e.g. "phase_transition", "host_action").
            task_id: The originating task identifier.
            attributes: Additional key-value attributes for the event.

        Returns:
            The JSON response from the telemetry emit endpoint.
        """
        payload: dict[str, object] = {
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "task_id": task_id,
            "timestamp": time.time(),
            "attributes": attributes or {},
        }
        response = self._post("/v1/telemetry/emit", payload)
        return response.json()

    def emit_phase_transition(
        self,
        task_id: str,
        from_phase: str,
        to_phase: str,
        reason: str,
    ) -> dict[str, Any]:
        """Emit a phase_transition telemetry event for a Work_Phase change.

        Per Requirement 8.3: WHEN the Orchestrator transitions a Task_Session
        between Work_Phases, it SHALL emit a phase_transition event with task_id,
        from_phase, to_phase, trigger reason, and timestamp.

        Args:
            task_id: The task undergoing the phase transition.
            from_phase: The phase being exited.
            to_phase: The phase being entered.
            reason: The trigger reason for the transition.

        Returns:
            The JSON response from the telemetry emit endpoint.
        """
        attributes: dict[str, Any] = {
            "from_phase": from_phase,
            "to_phase": to_phase,
            "reason": reason,
        }
        return self.emit_event(
            event_type="phase_transition",
            task_id=task_id,
            attributes=attributes,
        )

    def get_summary(
        self,
        start_time: str,
        end_time: str,
        group_by: list[str] | None = None,
    ) -> dict[str, Any]:
        """Query aggregated telemetry metrics for a time window.

        Per Requirement 9.1: GET /v1/telemetry/summary returns aggregated metrics
        (total cost, total tokens, call count, average latency, error rate) for a
        specified time window grouped by role and model.

        Args:
            start_time: ISO-8601 start of the query window.
            end_time: ISO-8601 end of the query window.
            group_by: Optional list of grouping dimensions (e.g. ["role", "model"]).

        Returns:
            Aggregated summary result as a dictionary.
        """
        params: dict[str, Any] = {
            "start_time": start_time,
            "end_time": end_time,
        }
        if group_by:
            params["group_by"] = ",".join(group_by)
        response = self._get("/v1/telemetry/summary", params=params)
        return response.json()

    def get_events(
        self,
        task_id: str | None = None,
        event_type: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """Query paginated raw telemetry events.

        Per Requirement 9.1: GET /v1/telemetry/events returns paginated raw
        telemetry events filtered by task_id, event_type, etc.

        Args:
            task_id: Optional filter by task ID.
            event_type: Optional filter by event type.
            page: Page number (1-indexed).
            page_size: Number of events per page.

        Returns:
            Paginated event listing as a dictionary.
        """
        params: dict[str, Any] = {
            "page": page,
            "page_size": page_size,
        }
        if task_id is not None:
            params["task_id"] = task_id
        if event_type is not None:
            params["event_type"] = event_type
        response = self._get("/v1/telemetry/events", params=params)
        return response.json()

    def get_cost_breakdown(
        self,
        start_time: str | None = None,
        end_time: str | None = None,
        group_by: list[str] | None = None,
    ) -> dict[str, Any]:
        """Query cost breakdown by task, role, model, and Work_Phase.

        Per Requirement 9.1: GET /v1/telemetry/cost returns cost breakdown by task,
        role, model, and Work_Phase for a specified time window.

        Args:
            start_time: Optional ISO-8601 start of the query window.
            end_time: Optional ISO-8601 end of the query window.
            group_by: Optional list of grouping dimensions (e.g. ["task", "role"]).

        Returns:
            Cost breakdown result as a dictionary.
        """
        params: dict[str, Any] = {}
        if start_time is not None:
            params["start_time"] = start_time
        if end_time is not None:
            params["end_time"] = end_time
        if group_by:
            params["group_by"] = ",".join(group_by)
        response = self._get("/v1/telemetry/cost", params=params)
        return response.json()

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()
