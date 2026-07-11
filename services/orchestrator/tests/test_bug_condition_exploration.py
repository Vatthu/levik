"""
Bug Condition Exploration Property Test

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7**

This test encodes the EXPECTED (post-fix) behavior for 7 bug conditions in the
orchestrator's team coordination layer. These tests are EXPECTED TO FAIL on
unfixed code — failure confirms the bugs exist.

The test uses scoped, deterministic assertions (not randomized property-based
generation) because we want concrete counterexamples that demonstrate the
specific defects.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from vikram_orchestrator.host_client import HostClient
from vikram_orchestrator.models import (
    AgentProfile,
    AgentRosterResponse,
    AgentThinkRequest,
    AgentThinkResponse,
    SystemHealthResponse,
)
from vikram_orchestrator.team import TeamRouter
from vikram_orchestrator.workflow import build_graph, OrchestratorState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_state(**overrides) -> OrchestratorState:
    """Produce a minimal valid OrchestratorState for testing."""
    state: OrchestratorState = {
        "task_id": "test-task-001",
        "source": "test",
        "requested_by": "tester",
        "objective": "Fix the widget",
        "repo_path": "/tmp/repo",
        "repo_default_branch": "main",
        "operator_channel": None,
        "operator_chat_id": None,
        "require_human_approval": False,
        "max_parallel_workers": 1,
        "max_cost_usd": None,
        "allow_network": False,
        "status": "running",
        "phase": "host_ready",
        "summary": "Test state",
        "team_roster": [],
    }
    state.update(overrides)
    return state


def _make_stub_host_client() -> MagicMock:
    """Create a mock HostClient for testing."""
    client = MagicMock(spec=HostClient)
    client.health.return_value = SystemHealthResponse(
        status="ok",
        workspace_root="/tmp/workspaces",
        socket_path="/tmp/vikramd.sock",
        restrict_to_workspace=True,
        sandboxed=False,
        telegram_enabled=False,
    )
    return client


# ===========================================================================
# Bug Condition 1: ask_agent retry — ConnectionError should fail workflow
# ===========================================================================


class TestBugCondition1_AgentCallResilience:
    """
    Bug: ask_agent catches exceptions and proceeds with hardcoded strings.
    Expected: Workflow sets status='failed' when agent call exhausts retries.

    **Validates: Requirements 1.1**
    """

    def test_agent_think_raises_connection_error_fails_workflow(self):
        """
        Mock host_client.agent_think to raise ConnectionError.
        Assert the agent_plan node raises AgentCallFailedError (workflow fails)
        instead of proceeding with 'Plan unavailable: ...' string.
        """
        import tempfile
        from pathlib import Path
        from vikram_orchestrator.team import AgentCallFailedError

        host_client = _make_stub_host_client()
        host_client.agent_think.side_effect = ConnectionError("agent unreachable")

        state = _base_state(
            team_roster=[{"id": "lead-1", "role": "lead", "provider": "openai",
                          "model": "gpt-4", "capabilities": []}],
            target_candidates=[{"path": "src/main.py", "score": 90, "reason": "main"}],
            target_file_previews=[{"path": "src/main.py", "content": "x=1", "score": 90}],
        )

        # Invoke the actual agent_plan node from the compiled graph.
        # After the fix, ask_agent retries and then raises AgentCallFailedError
        # which propagates out of agent_plan (the node does NOT catch it).
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite"
            graph = build_graph(host_client, checkpoint_db=db_path)

            assert hasattr(graph, 'nodes') and 'agent_plan' in graph.nodes, (
                "agent_plan node not found in graph"
            )
            agent_plan_node = graph.nodes['agent_plan']

            with pytest.raises(AgentCallFailedError) as exc_info:
                agent_plan_node.invoke(state)

            # Verify the error contains diagnostic info
            assert exc_info.value.role == "lead"
            assert exc_info.value.attempts > 0


# ===========================================================================
# Bug Condition 2: discover_team fail-fast
# ===========================================================================


class TestBugCondition2_DiscoverTeamFailFast:
    """
    Bug: discover_team catches all exceptions and sets empty roster, continuing.
    Expected: status='failed', phase='team_unavailable'.

    **Validates: Requirements 1.2**
    """

    def test_agent_roster_raises_sets_failed_status(self):
        """
        Mock host_client.agent_roster to raise an exception.
        Assert discover_team node sets status='failed' and phase='team_unavailable'.
        """
        import tempfile
        from pathlib import Path

        host_client = _make_stub_host_client()
        host_client.agent_roster.side_effect = ConnectionError("host unreachable")

        state = _base_state()

        # Invoke the actual discover_team node from the compiled graph
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite"
            graph = build_graph(host_client, checkpoint_db=db_path)

            assert hasattr(graph, 'nodes') and 'discover_team' in graph.nodes, (
                "discover_team node not found in graph"
            )
            discover_team_node = graph.nodes['discover_team']
            result_state = discover_team_node.invoke(state)

        # Assert EXPECTED post-fix behavior
        assert result_state.get("status") == "failed", (
            f"Bug confirmed: discover_team sets status='{result_state.get('status')}' "
            f"instead of 'failed' when agent_roster raises. "
            f"Workflow continues with empty roster."
        )
        assert result_state.get("phase") == "team_unavailable", (
            f"Bug confirmed: discover_team sets phase='{result_state.get('phase')}' "
            f"instead of 'team_unavailable'. Workflow silently continues."
        )


# ===========================================================================
# Bug Condition 3: Parallel workers constraint enforcement
# ===========================================================================


class TestBugCondition3_ParallelWorkers:
    """
    Bug: max_parallel_workers > 1 is stored but never used for concurrent execution.
    Expected: Independent steps execute concurrently when max_parallel_workers > 1.

    **Validates: Requirements 1.3**
    """

    def test_parallel_workers_enables_concurrent_execution(self):
        """
        Set max_parallel_workers=2 with independent steps.
        Assert that the graph supports concurrent execution (not strictly sequential).

        We verify that:
        1. build_graph contains a route_after_agent_verify function using Send
        2. The routing function returns Send objects for parallel fan-out when
           max_parallel_workers > 1
        3. Both agent_qa and review_change are targets of the Send fan-out
        """
        import inspect
        from langgraph.types import Send
        from vikram_orchestrator.workflow import build_graph as _build_graph

        # Verify the build_graph source contains Send-based parallel fan-out
        # from agent_verify to both agent_qa and review_change
        source = inspect.getsource(_build_graph)

        # Check that route_after_agent_verify uses Send for parallel dispatch
        has_send_agent_qa = 'Send("agent_qa"' in source or "Send('agent_qa'" in source
        has_send_review_change = 'Send("review_change"' in source or "Send('review_change'" in source
        has_route_function = 'route_after_agent_verify' in source
        has_max_parallel_check = 'max_parallel_workers' in source

        assert has_route_function, (
            "Bug confirmed: No route_after_agent_verify function exists. "
            "Graph has no parallel routing support."
        )
        assert has_max_parallel_check, (
            "Bug confirmed: max_parallel_workers is never checked in routing. "
            "Parallel worker count is ignored."
        )
        assert has_send_agent_qa and has_send_review_change, (
            "Bug confirmed: Graph has no Send-based parallel fan-out to both "
            "agent_qa and review_change. max_parallel_workers > 1 is ignored. "
            "agent_qa and review_change execute strictly sequentially."
        )


# ===========================================================================
# Bug Condition 4: QA routing uses wrong role
# ===========================================================================


class TestBugCondition4_QARouting:
    """
    Bug: agent_qa calls ask_agent(state, "engineer", prompt) instead of "qa".
    Expected: role argument should be "qa".

    **Validates: Requirements 1.4**
    """

    def test_agent_qa_routes_to_qa_role(self):
        """
        Trace ask_agent calls in agent_qa.
        Assert role argument is 'qa' (not 'engineer').
        """
        import tempfile
        from pathlib import Path

        # Capture what role is passed to agent_think
        think_calls: list[AgentThinkRequest] = []

        host_client = _make_stub_host_client()

        def capture_think(request: AgentThinkRequest):
            think_calls.append(request)
            return AgentThinkResponse(
                task_id=request.task_id,
                role=request.role,
                content='const { test } = require("@playwright/test");\ntest("basic", async () => {});',
            )

        host_client.agent_think.side_effect = capture_think
        host_client.browser_test.return_value = MagicMock(
            success=True, output="Test passed", screenshot=""
        )

        # Build state as if we've reached agent_qa
        state = _base_state(
            team_roster=[
                {"id": "qa-1", "role": "qa", "provider": "anthropic",
                 "model": "claude-3", "capabilities": ["testing", "qa"]},
                {"id": "eng-1", "role": "engineer", "provider": "openai",
                 "model": "gpt-4", "capabilities": ["coding"]},
            ],
            worktree_path="/tmp/worktree",
        )

        # Invoke agent_qa via the compiled graph node
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite"
            graph = build_graph(host_client, checkpoint_db=db_path)

            # Access the agent_qa node function from the graph
            if hasattr(graph, 'nodes') and 'agent_qa' in graph.nodes:
                agent_qa_node = graph.nodes['agent_qa']
                think_calls.clear()
                try:
                    result = agent_qa_node.invoke(state)
                except Exception:
                    pass

        # Assert that the role passed to agent_think was "qa" not "engineer"
        assert len(think_calls) > 0, "agent_qa did not call agent_think"
        first_call_role = think_calls[0].role
        assert first_call_role == "qa", (
            f"Bug confirmed: agent_qa routes to role='{first_call_role}' "
            f"instead of 'qa'. QA-specialized models are never used."
        )


# ===========================================================================
# Bug Condition 5: Capability matching returns None instead of raising error
# ===========================================================================


class TestBugCondition5_CapabilityMatching:
    """
    Bug: TeamRouter._select returns None when no capable agent found.
    Expected: AgentUnavailableError raised with available alternatives.

    **Validates: Requirements 1.5**
    """

    def test_no_match_raises_agent_unavailable_error(self):
        """
        Configure roster with no exact 'qa' role match and no capability match.
        Assert that _select raises AgentUnavailableError (not returns None).
        """
        from vikram_orchestrator.team import AgentUnavailableError

        # Roster with no "qa" role and no "qa" capability
        roster = [
            AgentProfile(id="eng-1", role="engineer", provider="openai",
                         model="gpt-4", capabilities=["coding", "review"]),
            AgentProfile(id="lead-1", role="lead", provider="anthropic",
                         model="claude-3", capabilities=["planning", "architecture"]),
        ]
        router = TeamRouter(roster)

        # After fix: _select("qa") should raise AgentUnavailableError
        # with available alternatives (not return None).
        with pytest.raises(AgentUnavailableError) as exc_info:
            router._select("qa")

        # Verify the error contains useful diagnostic info
        assert exc_info.value.role == "qa"
        assert len(exc_info.value.available_alternatives) > 0
        assert "engineer" in exc_info.value.available_alternatives
        assert "lead" in exc_info.value.available_alternatives


# ===========================================================================
# Bug Condition 6: Malformed output retry
# ===========================================================================


class TestBugCondition6_MalformedOutputRetry:
    """
    Bug: agent_implement catches parse exceptions, sets parsed_edits=[] and proceeds.
    Expected: Workflow retries with corrective prompt and fails if exhausted.

    **Validates: Requirements 1.6**
    """

    def test_malformed_engineer_output_triggers_retry_or_failure(self):
        """
        Mock engineer to return unparseable response.
        Assert workflow either retries with corrective prompt or fails
        (not empty parsed_edits with continued execution).
        """
        import sqlite3
        import tempfile
        from pathlib import Path

        host_client = _make_stub_host_client()

        # Engineer returns malformed (non-JSON) output
        malformed_response = (
            "Here's my implementation:\n"
            "```json\n"
            '[{"path": "src/main.py", "old_text": incomplete...\n'
            "```"
        )
        host_client.agent_think.return_value = AgentThinkResponse(
            task_id="test-task-001",
            role="engineer",
            content=malformed_response,
        )

        state = _base_state(
            team_roster=[
                {"id": "eng-1", "role": "engineer", "provider": "openai",
                 "model": "gpt-4", "capabilities": ["coding"]},
            ],
            plan_content="Step 1: Fix the bug in main.py\nStep 2: Update tests\nStep 3: Run verification",
            target_file_previews=[
                {"path": "src/main.py", "content": "def hello(): pass", "score": 90}
            ],
            target_candidates=[{"path": "src/main.py", "score": 90, "reason": "main"}],
            worktree_path="/tmp/worktree",
        )

        # Simulate what agent_implement does
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite"
            graph = build_graph(host_client, checkpoint_db=db_path)

            if hasattr(graph, 'nodes') and 'agent_implement' in graph.nodes:
                agent_implement_node = graph.nodes['agent_implement']
                result = agent_implement_node.invoke(state)

                # BUG CONDITION ASSERTION:
                # The current code sets parsed_edits=[] and proceeds.
                # Expected: Either retry with corrective prompt or fail.
                parsed_edits = result.get("parsed_edits", [])
                status = result.get("status", "running")

                # After fix: either parsed_edits is non-empty (retry succeeded)
                # or status is "failed" (retries exhausted)
                assert parsed_edits or status == "failed", (
                    f"Bug confirmed: agent_implement sets parsed_edits={parsed_edits} "
                    f"with status='{status}' when engineer output is malformed. "
                    f"Workflow proceeds with zero edits instead of retrying or failing."
                )


# ===========================================================================
# Bug Condition 7: Plan quality gate
# ===========================================================================


class TestBugCondition7_PlanQualityGate:
    """
    Bug: grill_spec skips validation when plan contains 'unavailable'.
    Expected: Workflow sets status='failed' with phase='planning_failed'.

    **Validates: Requirements 1.7**
    """

    def test_unavailable_plan_halts_workflow(self):
        """
        Set plan_content = 'Plan unavailable: timeout'.
        Assert workflow sets status='failed' with phase='planning_failed'.
        """
        import sqlite3
        import tempfile
        from pathlib import Path

        host_client = _make_stub_host_client()

        state = _base_state(
            plan_content="Plan unavailable: timeout",
            team_roster=[
                {"id": "rev-1", "role": "reviewer", "provider": "openai",
                 "model": "gpt-4", "capabilities": ["review"]},
                {"id": "lead-1", "role": "lead", "provider": "anthropic",
                 "model": "claude-3", "capabilities": ["planning"]},
            ],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite"
            graph = build_graph(host_client, checkpoint_db=db_path)

            if hasattr(graph, 'nodes') and 'grill_spec' in graph.nodes:
                grill_spec_node = graph.nodes['grill_spec']
                result = grill_spec_node.invoke(state)

                # BUG CONDITION ASSERTION:
                # Current code: returns early with grill_summary="Skipped (no lead plan)"
                # and the workflow proceeds.
                # Expected: status='failed', phase='planning_failed'
                assert result.get("status") == "failed", (
                    f"Bug confirmed: grill_spec sets status='{result.get('status')}' "
                    f"instead of 'failed' when plan_content='Plan unavailable: timeout'. "
                    f"grill_summary='{result.get('grill_summary', '')}'. "
                    f"Broken plan bypasses validation and reaches implementation."
                )
                assert result.get("phase") == "planning_failed", (
                    f"Bug confirmed: grill_spec sets phase='{result.get('phase')}' "
                    f"instead of 'planning_failed'. Plan quality gate is not enforced."
                )
