"""Unit tests for the TelemetryClient with mocked HTTP responses."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import httpx

from vikram_orchestrator.telemetry_client import TelemetryClient


def _make_client(handler) -> TelemetryClient:
    """Create a TelemetryClient with a mocked transport."""
    client = TelemetryClient.__new__(TelemetryClient)
    client.socket_path = "/tmp/vikramd.sock"
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="http://vikramd",
    )
    return client


class TestEmitEvent(unittest.TestCase):
    def test_emit_event_sends_correct_payload(self) -> None:
        captured_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_requests.append(request)
            return httpx.Response(200, json={"status": "ok"})

        client = _make_client(handler)
        result = client.emit_event(
            event_type="host_action",
            task_id="task-001",
            attributes={"action": "git_checkout", "duration_ms": 120},
        )

        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(len(captured_requests), 1)

        req = captured_requests[0]
        self.assertEqual(req.url.path, "/v1/telemetry/emit")
        body = json.loads(req.content)
        self.assertEqual(body["event_type"], "host_action")
        self.assertEqual(body["task_id"], "task-001")
        self.assertEqual(body["attributes"]["action"], "git_checkout")
        self.assertEqual(body["attributes"]["duration_ms"], 120)
        self.assertIn("event_id", body)
        self.assertIn("timestamp", body)

    def test_emit_event_defaults_attributes_to_empty_dict(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.assertEqual(body["attributes"], {})
            return httpx.Response(200, json={"status": "ok"})

        client = _make_client(handler)
        client.emit_event(event_type="shutdown", task_id="task-002")


class TestEmitPhaseTransition(unittest.TestCase):
    def test_emit_phase_transition_sends_correct_attributes(self) -> None:
        captured_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_requests.append(request)
            return httpx.Response(200, json={"status": "ok"})

        client = _make_client(handler)
        result = client.emit_phase_transition(
            task_id="task-010",
            from_phase="planning",
            to_phase="implementation",
            reason="plan_approved",
        )

        self.assertEqual(result, {"status": "ok"})
        body = json.loads(captured_requests[0].content)
        self.assertEqual(body["event_type"], "phase_transition")
        self.assertEqual(body["task_id"], "task-010")
        self.assertEqual(body["attributes"]["from_phase"], "planning")
        self.assertEqual(body["attributes"]["to_phase"], "implementation")
        self.assertEqual(body["attributes"]["reason"], "plan_approved")


class TestGetSummary(unittest.TestCase):
    def test_get_summary_with_group_by(self) -> None:
        summary_response = {
            "total_cost": 12.50,
            "total_tokens": 150000,
            "call_count": 45,
            "avg_latency_ms": 2300.5,
            "error_rate": 0.02,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v1/telemetry/summary")
            params = dict(request.url.params)
            self.assertEqual(params["start_time"], "2024-01-01T00:00:00Z")
            self.assertEqual(params["end_time"], "2024-01-02T00:00:00Z")
            self.assertEqual(params["group_by"], "role,model")
            return httpx.Response(200, json=summary_response)

        client = _make_client(handler)
        result = client.get_summary(
            start_time="2024-01-01T00:00:00Z",
            end_time="2024-01-02T00:00:00Z",
            group_by=["role", "model"],
        )
        self.assertEqual(result["total_cost"], 12.50)
        self.assertEqual(result["call_count"], 45)

    def test_get_summary_without_group_by(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            self.assertNotIn("group_by", params)
            return httpx.Response(200, json={"total_cost": 5.0})

        client = _make_client(handler)
        result = client.get_summary(
            start_time="2024-01-01T00:00:00Z",
            end_time="2024-01-02T00:00:00Z",
        )
        self.assertEqual(result["total_cost"], 5.0)


class TestGetEvents(unittest.TestCase):
    def test_get_events_with_filters(self) -> None:
        events_response = {
            "events": [{"event_id": "ev1", "event_type": "phase_transition"}],
            "total": 1,
            "page": 1,
            "page_size": 50,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v1/telemetry/events")
            params = dict(request.url.params)
            self.assertEqual(params["task_id"], "task-005")
            self.assertEqual(params["event_type"], "phase_transition")
            self.assertEqual(params["page"], "1")
            self.assertEqual(params["page_size"], "50")
            return httpx.Response(200, json=events_response)

        client = _make_client(handler)
        result = client.get_events(
            task_id="task-005",
            event_type="phase_transition",
        )
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["events"][0]["event_type"], "phase_transition")

    def test_get_events_with_pagination(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            self.assertEqual(params["page"], "3")
            self.assertEqual(params["page_size"], "20")
            self.assertNotIn("task_id", params)
            self.assertNotIn("event_type", params)
            return httpx.Response(200, json={"events": [], "total": 0})

        client = _make_client(handler)
        result = client.get_events(page=3, page_size=20)
        self.assertEqual(result["events"], [])


class TestGetCostBreakdown(unittest.TestCase):
    def test_get_cost_breakdown_with_all_params(self) -> None:
        cost_response = {
            "total_cost": 45.00,
            "by_task": {"task-001": 20.0, "task-002": 25.0},
        }

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v1/telemetry/cost")
            params = dict(request.url.params)
            self.assertEqual(params["start_time"], "2024-01-01T00:00:00Z")
            self.assertEqual(params["end_time"], "2024-01-31T23:59:59Z")
            self.assertEqual(params["group_by"], "task,role")
            return httpx.Response(200, json=cost_response)

        client = _make_client(handler)
        result = client.get_cost_breakdown(
            start_time="2024-01-01T00:00:00Z",
            end_time="2024-01-31T23:59:59Z",
            group_by=["task", "role"],
        )
        self.assertEqual(result["total_cost"], 45.00)

    def test_get_cost_breakdown_with_no_params(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            self.assertEqual(len(params), 0)
            return httpx.Response(200, json={"total_cost": 100.0})

        client = _make_client(handler)
        result = client.get_cost_breakdown()
        self.assertEqual(result["total_cost"], 100.0)


class TestErrorHandling(unittest.TestCase):
    def test_http_error_includes_response_body(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                500,
                json={"error": "internal server error"},
            )

        client = _make_client(handler)
        with self.assertRaises(httpx.HTTPStatusError) as ctx:
            client.emit_event(
                event_type="phase_transition",
                task_id="task-999",
            )
        self.assertIn("Response body:", str(ctx.exception))
        self.assertIn("internal server error", str(ctx.exception))

    def test_http_error_without_body(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(502, text="")

        client = _make_client(handler)
        with self.assertRaises(httpx.HTTPStatusError):
            client.get_summary(
                start_time="2024-01-01T00:00:00Z",
                end_time="2024-01-02T00:00:00Z",
            )


class TestClientLifecycle(unittest.TestCase):
    def test_close_closes_underlying_client(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={})

        client = _make_client(handler)
        client.close()
        # After close, further requests should fail
        with self.assertRaises(Exception):
            client.emit_event(event_type="test", task_id="task-001")


if __name__ == "__main__":
    unittest.main()
