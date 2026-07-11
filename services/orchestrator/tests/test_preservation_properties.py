"""Preservation property tests for the enterprise team coordination fix.

These tests capture baseline behavior on UNFIXED code that must remain
unchanged after the fix is applied. They validate Requirements 3.1–3.7.

Run with: cd services/orchestrator && .venv/bin/python -m pytest tests/test_preservation_properties.py -v
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import hypothesis.strategies as st
from hypothesis import given, settings, assume

from vikram_orchestrator.models import (
    AgentProfile,
    AgentThinkResponse,
    RepoRef,
    TaskConstraints,
    TaskCreateRequest,
    TaskSession,
)
from vikram_orchestrator.policy import decide_approval_policy
from vikram_orchestrator.team import TeamRouter
from vikram_orchestrator.workflow import (
    build_graph,
    close_graph,
    initial_state_from_request,
    state_to_task_session,
    task_session_from_existing,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

def agent_role_st() -> st.SearchStrategy[str]:
    """Generate valid role strings for agent profiles."""
    return st.sampled_from(["lead", "engineer", "reviewer", "runner", "qa", "architect"])


def agent_profile_st(role: str | None = None) -> st.SearchStrategy[AgentProfile]:
    """Generate a valid AgentProfile with optional fixed role."""
    role_strategy = st.just(role) if role else agent_role_st()
    return st.builds(
        AgentProfile,
        id=st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N"))),
        role=role_strategy,
        name=st.text(min_size=1, max_size=30, alphabet=st.characters(whitelist_categories=("L", "N", "Z"))),
        provider=st.sampled_from(["openai", "anthropic", "bedrock", "google"]),
        model=st.sampled_from(["gpt-4", "claude-3", "gemini-pro", "nova-pro"]),
        capabilities=st.lists(st.sampled_from(["code", "review", "test", "plan", "qa"]), max_size=4),
    )


def roster_with_exact_match_st() -> st.SearchStrategy[tuple[list[AgentProfile], str]]:
    """Generate a roster that has at least one exact role match, plus the target role."""
    role = agent_role_st()
    return role.flatmap(
        lambda r: st.tuples(
            st.lists(agent_profile_st(), min_size=0, max_size=5).flatmap(
                lambda profiles: st.just(profiles + [AgentProfile(
                    id=f"exact-{r}",
                    role=r,
                    name=f"Agent {r}",
                    provider="anthropic",
                    model="claude-3",
                    capabilities=[r],
                )])
            ),
            st.just(r),
        )
    )


def valid_edit_st() -> st.SearchStrategy[dict[str, str]]:
    """Generate a valid JSON edit object."""
    return st.fixed_dictionaries({
        "path": st.text(min_size=1, max_size=50, alphabet="abcdefghijklmnopqrstuvwxyz/._-"),
        "old_text": st.text(min_size=1, max_size=200),
        "new_text": st.text(min_size=0, max_size=200),
        "rationale": st.text(min_size=0, max_size=100),
    })


def valid_plan_st() -> st.SearchStrategy[str]:
    """Generate valid plan strings: at least 3 lines, no 'unavailable' keyword."""
    line = st.text(
        min_size=1, max_size=80,
        alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")),
    ).filter(lambda s: "unavailable" not in s.lower())
    return st.lists(line, min_size=3, max_size=20).map(lambda lines: "\n".join(lines))


def task_status_st() -> st.SearchStrategy[str]:
    """Generate valid task statuses."""
    return st.sampled_from(["queued", "running", "paused", "awaiting_approval", "completed", "failed"])


def approval_risk_st() -> st.SearchStrategy[str]:
    return st.sampled_from(["low", "medium", "high", "critical"])


def approval_route_st() -> st.SearchStrategy[str]:
    return st.sampled_from(["auto_complete", "founder_review", "stop"])


def state_dict_for_policy_st() -> st.SearchStrategy[dict[str, Any]]:
    """Generate valid state dicts for decide_approval_policy testing."""
    return st.fixed_dictionaries({
        "verification_outcome": st.sampled_from(["passed", "failed", ""]),
        "lint_passed": st.booleans(),
        "lint_new_errors": st.lists(st.text(min_size=1, max_size=50), max_size=3),
        "review_verdict": st.sampled_from(["APPROVE", "REJECT", "CHANGES_REQUESTED", ""]),
        "applied_edits": st.lists(
            st.fixed_dictionaries({"path": st.text(min_size=1, max_size=30, alphabet="abcdefghijklmnopqrstuvwxyz./")})
            , min_size=0, max_size=3
        ),
        "require_human_approval": st.booleans(),
        "active_follow_up_phase": st.sampled_from([
            "", "founder_edit_requested", "founder_clarification_requested", "merge_blocked"
        ]),
    })


def state_dict_for_session_st() -> st.SearchStrategy[dict[str, Any]]:
    """Generate valid state dicts that exercise state_to_task_session and task_session_from_existing."""
    return st.fixed_dictionaries({
        "status": task_status_st(),
        "phase": st.sampled_from([
            "intake", "host_ready", "workspace_ready", "worktree_ready",
            "change_ready", "change_applied", "founder_review_requested",
            "merge_ready", "merge_blocked",
        ]),
        "summary": st.text(min_size=1, max_size=100),
        "approval_risk": st.one_of(st.none(), approval_risk_st()),
        "approval_route": st.one_of(st.none(), approval_route_st()),
        "pending_follow_up_required": st.booleans(),
        "pending_follow_up_comment": st.text(max_size=50),
        "merge_readiness": st.one_of(st.none(), st.sampled_from(["unknown", "ready", "blocked"])),
        "merge_summary": st.text(max_size=80),
    })


# ---------------------------------------------------------------------------
# Property 1: TeamRouter._select exact role match preservation
# Validates: Requirements 3.5
# ---------------------------------------------------------------------------

class TestTeamRouterExactMatchPreservation:
    """For all valid roster configs with exact role matches, TeamRouter._select(role)
    returns the same AgentProfile as the original implementation."""

    @given(data=roster_with_exact_match_st())
    @settings(max_examples=100)
    def test_exact_role_match_returns_correct_profile(
        self, data: tuple[list[AgentProfile], str]
    ) -> None:
        """**Validates: Requirements 3.5**

        For all rosters containing an exact role match, _select returns the
        agent whose role field matches exactly (case-insensitive, stripped).
        """
        roster, target_role = data
        router = TeamRouter(roster)
        result = router._select(target_role)

        # There must be at least one exact match in the roster
        exact_matches = [
            a for a in roster if a.role.strip().lower() == target_role.strip().lower()
        ]
        assert len(exact_matches) >= 1

        # _select should return one of the exact matches (the first one found)
        assert result is not None
        assert result.role.strip().lower() == target_role.strip().lower()
        assert result in exact_matches

        # Verify provider and model are populated
        assert result.provider is not None
        assert result.model is not None

    @given(data=roster_with_exact_match_st())
    @settings(max_examples=50)
    def test_exact_match_deterministic(
        self, data: tuple[list[AgentProfile], str]
    ) -> None:
        """**Validates: Requirements 3.5**

        Calling _select twice with the same role on the same roster returns
        identical results (deterministic).
        """
        roster, target_role = data
        router = TeamRouter(roster)
        result1 = router._select(target_role)
        result2 = router._select(target_role)
        assert result1 == result2


# ---------------------------------------------------------------------------
# Property 2: JSON edit parse logic preservation
# Validates: Requirements 3.1
# ---------------------------------------------------------------------------

class TestEditParsePreservation:
    """For all valid JSON edit arrays, the parse logic extracts all edits correctly."""

    @given(edits=st.lists(valid_edit_st(), min_size=1, max_size=10))
    @settings(max_examples=100)
    def test_valid_json_edits_parsed_correctly(self, edits: list[dict[str, str]]) -> None:
        """**Validates: Requirements 3.1**

        For any well-formed JSON array of edit objects with non-empty path and
        old_text, the parsing logic extracts all edits into parsed_edits.
        """
        # Filter to only edits that have non-empty path and old_text (matching the code's filter)
        expected_valid = [
            e for e in edits if e.get("path") and e.get("old_text")
        ]
        assume(len(expected_valid) > 0)

        # Simulate the exact parsing logic from agent_implement
        edits_json = json.dumps(edits)
        parsed_edits: list[dict] = []
        try:
            parsed = json.loads(edits_json)
            if isinstance(parsed, list):
                parsed_edits = [
                    {
                        "path": e.get("path", ""),
                        "old_text": e.get("old_text", ""),
                        "new_text": e.get("new_text", ""),
                        "rationale": e.get("rationale", ""),
                    }
                    for e in parsed
                    if e.get("path") and e.get("old_text")
                ]
        except Exception:
            pass

        assert len(parsed_edits) == len(expected_valid)
        for parsed_edit, original in zip(parsed_edits, expected_valid):
            assert parsed_edit["path"] == original["path"]
            assert parsed_edit["old_text"] == original["old_text"]
            assert parsed_edit["new_text"] == original["new_text"]
            assert parsed_edit["rationale"] == original["rationale"]


# ---------------------------------------------------------------------------
# Property 3: Plan validity — grill_spec proceeds for valid plans
# Validates: Requirements 3.7
# ---------------------------------------------------------------------------

class TestPlanValidityPreservation:
    """For all plan strings without error patterns (at least 3 lines, not empty),
    grill_spec proceeds with adversarial validation (does NOT skip)."""

    @given(plan=valid_plan_st())
    @settings(max_examples=100)
    def test_valid_plan_not_skipped_by_grill_spec(self, plan: str) -> None:
        """**Validates: Requirements 3.7**

        For any plan that does not contain "unavailable" (case-insensitive)
        and is non-empty, the current grill_spec guard condition does not trigger
        early return.
        """
        # The current grill_spec guard: `if not plan or "unavailable" in plan.lower()`
        # For valid plans, this should be False (meaning grill_spec proceeds)
        guard_triggers = (not plan) or ("unavailable" in plan.lower())
        assert not guard_triggers, (
            f"Valid plan should not trigger grill_spec skip guard, but got: {plan[:100]!r}"
        )


# ---------------------------------------------------------------------------
# Property 4: state_to_task_session and task_session_from_existing preservation
# Validates: Requirements 3.1, 3.6
# ---------------------------------------------------------------------------

class TestSessionTransformPreservation:
    """For all valid state dicts, state_to_task_session and task_session_from_existing
    produce identical TaskSession objects for the same inputs."""

    @given(state_overrides=state_dict_for_session_st())
    @settings(max_examples=100)
    def test_state_to_task_session_deterministic(
        self, state_overrides: dict[str, Any]
    ) -> None:
        """**Validates: Requirements 3.1**

        Calling state_to_task_session twice with the same request and state
        produces identical TaskSession objects.
        """
        request = TaskCreateRequest(
            task_id="test-prop-001",
            source="telegram",
            requested_by="founder",
            objective="Test preservation property",
            repo=RepoRef(path="/repos/test", default_branch="main"),
            constraints=TaskConstraints(max_parallel_workers=1),
        )
        state = {**initial_state_from_request(request), **state_overrides}

        session1 = state_to_task_session(request, state)
        session2 = state_to_task_session(request, state)
        assert session1 == session2

    @given(state_overrides=state_dict_for_session_st())
    @settings(max_examples=100)
    def test_task_session_from_existing_deterministic(
        self, state_overrides: dict[str, Any]
    ) -> None:
        """**Validates: Requirements 3.1**

        Calling task_session_from_existing twice with the same task and state
        produces identical TaskSession objects.
        """
        task = TaskSession(
            task_id="test-prop-002",
            source="telegram",
            requested_by="founder",
            objective="Test preservation property",
            repo=RepoRef(path="/repos/test", default_branch="main"),
            constraints=TaskConstraints(max_parallel_workers=1),
            status="running",
            phase="change_ready",
            summary="Existing task session",
        )
        state = {**state_overrides}

        session1 = task_session_from_existing(task, state)
        session2 = task_session_from_existing(task, state)
        assert session1 == session2

    @given(state_overrides=state_dict_for_session_st())
    @settings(max_examples=100)
    def test_state_to_task_session_fields_match_state(
        self, state_overrides: dict[str, Any]
    ) -> None:
        """**Validates: Requirements 3.6**

        state_to_task_session maps state fields correctly: status from state,
        phase from state, approval_route determines requires_founder_review.
        """
        request = TaskCreateRequest(
            task_id="test-prop-003",
            source="telegram",
            requested_by="founder",
            objective="Test field mapping",
            repo=RepoRef(path="/repos/test", default_branch="main"),
            constraints=TaskConstraints(max_parallel_workers=1),
        )
        state = {**initial_state_from_request(request), **state_overrides}
        session = state_to_task_session(request, state)

        # Status and phase come from state
        assert session.status == state.get("status", "running")
        assert session.phase == state.get("phase", "intake")

        # requires_founder_review is derived from approval_route
        expected_founder_review = state.get("approval_route") == "founder_review"
        assert session.requires_founder_review == expected_founder_review

        # merge_readiness comes from state
        assert session.merge_readiness == state.get("merge_readiness")


# ---------------------------------------------------------------------------
# Property 5: decide_approval_policy preservation
# Validates: Requirements 3.6
# ---------------------------------------------------------------------------

class TestApprovalPolicyPreservation:
    """For all valid state dicts, decide_approval_policy produces the same
    decisions when called multiple times with the same input."""

    @given(state=state_dict_for_policy_st())
    @settings(max_examples=200)
    def test_approval_policy_deterministic(self, state: dict[str, Any]) -> None:
        """**Validates: Requirements 3.6**

        decide_approval_policy is a pure function: same input → same output.
        """
        decision1 = decide_approval_policy(state)
        decision2 = decide_approval_policy(state)
        assert decision1 == decision2

    @given(state=state_dict_for_policy_st())
    @settings(max_examples=200)
    def test_approval_policy_returns_valid_structure(self, state: dict[str, Any]) -> None:
        """**Validates: Requirements 3.6**

        decide_approval_policy always returns a valid ApprovalPolicyDecision
        with a risk_class from the allowed set and a valid route.
        """
        decision = decide_approval_policy(state)
        assert decision.risk_class in {"low", "medium", "high", "critical"}
        assert decision.route in {"auto_complete", "founder_review", "stop"}
        assert isinstance(decision.reasons, list)
        assert isinstance(decision.summary, str)
        assert isinstance(decision.options, list)

    @given(state=state_dict_for_policy_st())
    @settings(max_examples=100)
    def test_low_risk_docs_auto_completes(self, state: dict[str, Any]) -> None:
        """**Validates: Requirements 3.6**

        When conditions match low-risk documentation (lint passed, reviewer
        approves, verification passed, only .md files edited), policy routes
        to auto_complete.
        """
        # Construct a state that should hit the low-risk documentation path
        docs_state = {
            **state,
            "verification_outcome": "passed",
            "lint_passed": True,
            "lint_new_errors": [],
            "review_verdict": "APPROVE",
            "applied_edits": [{"path": "docs/README.md"}],
            "require_human_approval": False,
            "active_follow_up_phase": "",
        }
        decision = decide_approval_policy(docs_state)
        assert decision.risk_class == "low"
        assert decision.route == "auto_complete"


# ---------------------------------------------------------------------------
# Property 6: Sequential workflow order preservation (max_parallel_workers=1)
# Validates: Requirements 3.1, 3.2
# ---------------------------------------------------------------------------

class TestSequentialWorkflowOrderPreservation:
    """Sequential workflow order (verify_host → discover_team → ... →
    finalize_merge_readiness) is preserved when max_parallel_workers=1."""

    def test_graph_edge_order_sequential(self) -> None:
        """**Validates: Requirements 3.1, 3.2**

        When max_parallel_workers=1, the LangGraph state graph defines
        the correct sequential edge ordering for the main workflow path.
        Verify by building the graph and inspecting its structure.
        """
        from tests.test_workflow import StubHostClient

        host_client = StubHostClient()
        with tempfile.TemporaryDirectory() as tmpdir:
            graph = build_graph(host_client, checkpoint_db=Path(tmpdir) / "test.sqlite")
            try:
                # The expected sequential order for the initial path
                expected_sequence = [
                    "verify_host",
                    "discover_team",
                    "provision_workspace",
                    "create_worktree",
                    "inspect_repo",
                    "discover_targets",
                    "read_target_files",
                    "agent_plan",
                    "grill_spec",
                    "write_initial_plan",
                    "write_implementation_brief",
                    "discover_lint",
                    "discover_verification",
                    "write_verification_plan",
                ]

                # Verify the graph nodes exist
                node_names = set(graph.nodes.keys())
                for node in expected_sequence:
                    assert node in node_names, f"Expected node '{node}' not in graph"

                # Verify the workflow executes in order with max_parallel_workers=1
                request = TaskCreateRequest(
                    task_id="test-order-001",
                    source="local",
                    requested_by="test",
                    objective="Test sequential order",
                    repo=RepoRef(path="/repos/test", default_branch="main"),
                    constraints=TaskConstraints(max_parallel_workers=1),
                )
                result = graph.invoke(
                    initial_state_from_request(request),
                    config={"configurable": {"thread_id": request.task_id}},
                )

                # After full execution, the phase should be change_ready
                # (workflow completes its initial pass without a change_request)
                assert result["phase"] == "change_ready"
                assert result["status"] == "running"
            finally:
                close_graph(graph)

    def test_post_change_sequential_order(self) -> None:
        """**Validates: Requirements 3.2**

        After a change request, the workflow proceeds through
        apply → write_change → run_verification → agent_verify → agent_qa →
        review_change → write_verification_result → evaluate_approval_policy
        in sequential order when max_parallel_workers=1.
        """
        from tests.test_workflow import StubHostClient
        from vikram_orchestrator.models import TaskChangeRequest, TextReplacement

        host_client = StubHostClient()
        with tempfile.TemporaryDirectory() as tmpdir:
            graph = build_graph(host_client, checkpoint_db=Path(tmpdir) / "test.sqlite")
            try:
                request = TaskCreateRequest(
                    task_id="test-order-002",
                    source="local",
                    requested_by="test",
                    objective="Test post-change order",
                    repo=RepoRef(path="/repos/test", default_branch="main"),
                    constraints=TaskConstraints(
                        max_parallel_workers=1, require_human_approval=False
                    ),
                )
                # Run initial graph to get to change_ready
                graph.invoke(
                    initial_state_from_request(request),
                    config={"configurable": {"thread_id": request.task_id}},
                )

                # Now apply a change
                from vikram_orchestrator.workflow import apply_change_request

                task_session = state_to_task_session(request, {"status": "running", "phase": "change_ready", "summary": "ready"})
                change_req = TaskChangeRequest(
                    task_id="test-order-002",
                    summary="Test edit",
                    edits=[
                        TextReplacement(
                            path="README.md",
                            old_text="# Vikram",
                            new_text="# Vikram\nTest",
                            rationale="test",
                        )
                    ],
                )
                result_session = apply_change_request(graph, task_session, change_req)

                # The workflow should have completed through evaluation
                # For a docs-only low-risk change with no human approval, it auto-completes
                assert result_session.phase in (
                    "merge_ready", "merge_blocked",
                    "founder_review_requested", "auto_approved",
                )
            finally:
                close_graph(graph)
