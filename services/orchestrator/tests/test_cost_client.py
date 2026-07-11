"""Unit tests for the CostClient module."""

from __future__ import annotations

import json
import unittest

import httpx

from vikram_orchestrator.cost_client import CostClient, CostRecordRequest


def _make_client(handler) -> CostClient:
    """Build a CostClient with a mocked transport."""
    client = CostClient.__new__(CostClient)
    client.socket_path = "/tmp/vikramd.sock"
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="http://vikramd",
    )
    return client


class TestGetTaskCost(unittest.TestCase):
    def test_returns_cumulative_cost(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/cost/task/task-42"
            return httpx.Response(
                200,
                json={"task_id": "task-42", "cumulative_cost_usd": 1.23},
            )

        client = _make_client(handler)
        result = client.get_task_cost("task-42")
        self.assertAlmostEqual(result, 1.23)

    def test_raises_on_not_found(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": "task not found"})

        client = _make_client(handler)
        with self.assertRaises(httpx.HTTPStatusError) as ctx:
            client.get_task_cost("nonexistent")
        self.assertIn("task not found", str(ctx.exception))


class TestGetPhaseRemaining(unittest.TestCase):
    def test_returns_remaining_budget(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/cost/task/task-1/phase/implementation"
            return httpx.Response(
                200,
                json={
                    "task_id": "task-1",
                    "phase": "implementation",
                    "remaining_usd": 4.56,
                },
            )

        client = _make_client(handler)
        result = client.get_phase_remaining("task-1", "implementation")
        self.assertAlmostEqual(result, 4.56)


class TestGetForecast(unittest.TestCase):
    def test_returns_forecast_dict(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body == {"complexity": "moderate", "target_files": 5}
            return httpx.Response(
                200,
                json={
                    "min_cost_usd": 0.50,
                    "expected_cost_usd": 1.20,
                    "max_cost_usd": 3.00,
                    "confidence_level": 0.85,
                    "basis_task_count": 12,
                },
            )

        client = _make_client(handler)
        result = client.get_forecast("moderate", 5)
        self.assertAlmostEqual(result["min_cost_usd"], 0.50)
        self.assertAlmostEqual(result["expected_cost_usd"], 1.20)
        self.assertAlmostEqual(result["max_cost_usd"], 3.00)
        self.assertAlmostEqual(result["confidence_level"], 0.85)
        self.assertEqual(result["basis_task_count"], 12)


class TestGetDailyTotal(unittest.TestCase):
    def test_returns_daily_spend(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/cost/daily"
            return httpx.Response(
                200,
                json={"total_cost_usd": 15.75, "reset_at": "2025-01-01T00:00:00Z"},
            )

        client = _make_client(handler)
        result = client.get_daily_total()
        self.assertAlmostEqual(result, 15.75)


class TestRecordCost(unittest.TestCase):
    def test_sends_cost_record(self) -> None:
        captured_body: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/cost/record"
            captured_body.update(json.loads(request.content))
            return httpx.Response(200, json={})

        client = _make_client(handler)
        client.record_cost(
            CostRecordRequest(
                task_id="task-7",
                role="implementer",
                model="claude-sonnet-4-20250514",
                provider="anthropic",
                work_phase="implementation",
                input_tokens=1500,
                output_tokens=800,
                cost_usd=0.042,
                estimated=False,
                duration_ms=3200,
                invocation_id="inv-abc",
            )
        )

        self.assertEqual(captured_body["task_id"], "task-7")
        self.assertEqual(captured_body["role"], "implementer")
        self.assertEqual(captured_body["model"], "claude-sonnet-4-20250514")
        self.assertEqual(captured_body["provider"], "anthropic")
        self.assertEqual(captured_body["work_phase"], "implementation")
        self.assertEqual(captured_body["input_tokens"], 1500)
        self.assertEqual(captured_body["output_tokens"], 800)
        self.assertAlmostEqual(captured_body["cost_usd"], 0.042)
        self.assertFalse(captured_body["estimated"])
        self.assertEqual(captured_body["duration_ms"], 3200)
        self.assertEqual(captured_body["invocation_id"], "inv-abc")


class TestErrorHandling(unittest.TestCase):
    def test_includes_response_body_in_error(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                500,
                json={"error": "internal ledger failure"},
            )

        client = _make_client(handler)
        with self.assertRaises(httpx.HTTPStatusError) as ctx:
            client.get_daily_total()
        self.assertIn("Response body:", str(ctx.exception))
        self.assertIn("internal ledger failure", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
